"""Tests for the read-only storage status reporter.

Pinned contracts (see ``core/storage_status.py``):

* the reporter is read-only — no directory is created, no file is
  touched, no symlink is resolved by the classifier;
* it reports configured ``NOVA_DATA_DIR``, the resolved database
  path, and the four reserved subdirectories;
* it warns when ``NOVA_DATA_DIR`` is unset, when the data directory
  sits inside a Git checkout, when a configured path is on a
  transient mount (``/run/media/...``) or in ``/tmp``, and when a
  directory exists but is not writable;
* it surfaces Ollama models as informational (``OLLAMA_MODELS``
  honoured; default falls back to ``~/.ollama/models``);
* every response field is JSON-serialisable.

The tests never assume host-specific paths — everything is rooted
at ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from core import paths as core_paths
from core import storage_status as ss


# ── classify_mount ───────────────────────────────────────────────────


class TestClassifyMount:
    """Coarse mount classification is purely lexical and stable."""

    @pytest.mark.parametrize("path,expected", [
        ("/mnt/fastdata/NovaData", ss.MOUNT_STABLE),
        ("/var/lib/nova", ss.MOUNT_STABLE),
        ("/srv/nova-data", ss.MOUNT_STABLE),
        ("/opt/nova", ss.MOUNT_STABLE),
        ("/run/media/alice/disk1", ss.MOUNT_TRANSIENT),
        ("/media/alice/usb", ss.MOUNT_TRANSIENT),
        ("/tmp/nova-test", ss.MOUNT_TMP),
        ("/var/tmp/nova-test", ss.MOUNT_TMP),
        ("/etc/nova", ss.MOUNT_OTHER),
        ("relative/path", ss.MOUNT_OTHER),
        ("", ss.MOUNT_OTHER),
    ])
    def test_classifies_known_prefixes(self, path, expected, monkeypatch):
        # HOME is unset so the user_home classifier cannot interfere.
        monkeypatch.delenv("HOME", raising=False)
        assert ss.classify_mount(path) == expected

    def test_user_home_under_home(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/alice")
        assert ss.classify_mount("/home/alice/NovaData") == ss.MOUNT_USER_HOME
        assert ss.classify_mount("/home/alice") == ss.MOUNT_USER_HOME

    def test_user_home_does_not_match_other_users(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/alice")
        # A neighbouring user's home is not classified as the running
        # user's home — that distinction matters for permissions.
        assert ss.classify_mount("/home/bob/NovaData") == ss.MOUNT_OTHER


# ── get_storage_status ──────────────────────────────────────────────


class TestStorageStatusUnconfigured:
    """When NOVA_DATA_DIR is unset, the report is calm but warned."""

    def test_returns_warning_when_unset(self, monkeypatch, tmp_path):
        monkeypatch.delenv(core_paths.ENV_VAR, raising=False)
        monkeypatch.chdir(tmp_path)
        status = ss.get_storage_status()
        assert status.data_dir_configured is False
        assert any(
            "NOVA_DATA_DIR is not set" in w for w in status.warnings
        )

    def test_warns_when_data_root_contains_git_checkout(
        self, monkeypatch, tmp_path,
    ):
        # Simulate the legacy layout where ``./nova.db`` sits in a
        # directory that also contains ``.git``. The top-level
        # warning surface must surface that explicitly.
        monkeypatch.delenv(core_paths.ENV_VAR, raising=False)
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)
        status = ss.get_storage_status()
        joined = " | ".join(status.warnings)
        assert "Git checkout" in joined

    def test_paths_are_serialisable(self, monkeypatch, tmp_path):
        monkeypatch.delenv(core_paths.ENV_VAR, raising=False)
        monkeypatch.chdir(tmp_path)
        status = ss.get_storage_status()
        # The whole report must be a plain JSON-compatible dict so
        # the admin endpoint can hand it to FastAPI without
        # bespoke encoders.
        body = status.as_dict()
        rendered = json.dumps(body)
        parsed = json.loads(rendered)
        assert parsed["data_dir_configured"] is False
        # The reporter does not include None for unknown free space
        # by surfacing it as JSON null — make sure that survives.
        assert isinstance(parsed["paths"], list)


class TestStorageStatusConfigured:
    """When NOVA_DATA_DIR is set, every helper points under it."""

    @pytest.fixture
    def configured_root(self, monkeypatch, tmp_path):
        root = tmp_path / "mnt" / "fastdata" / "NovaData"
        root.mkdir(parents=True)
        # Ensure the classifier sees a stable prefix — we deliberately
        # built ``tmp_path / "mnt" / ...``; since classify_mount is
        # lexical, the full path under tmp_path resolves to a path
        # that does NOT start with ``/mnt/``. We use this root mostly
        # for existence / writability / disk-space assertions.
        monkeypatch.setenv(core_paths.ENV_VAR, str(root))
        return root

    def test_reports_configured_root(self, configured_root):
        status = ss.get_storage_status()
        assert status.data_dir_configured is True
        assert status.data_dir == str(configured_root)

    def test_each_canonical_path_is_present(self, configured_root):
        status = ss.get_storage_status()
        names = {p.name for p in status.paths}
        assert {
            "data_dir", "database", "backups", "exports",
            "memory_packs", "logs", "ollama_models",
        }.issubset(names)

    def test_database_path_is_under_root(self, configured_root):
        status = ss.get_storage_status()
        db_entry = next(p for p in status.paths if p.name == "database")
        assert db_entry.path == str(configured_root / "nova.db")
        # Pre-first-run: the DB does not exist yet, but the entry
        # still resolves cleanly.
        assert db_entry.exists is False

    def test_subdir_entries_under_root(self, configured_root):
        status = ss.get_storage_status()
        for name, sub in (
            ("backups", "backups"),
            ("exports", "exports"),
            ("memory_packs", "memory-packs"),
            ("logs", "logs"),
        ):
            entry = next(p for p in status.paths if p.name == name)
            assert entry.path == str(configured_root / sub)

    def test_disk_free_is_reported_for_existing_root(self, configured_root):
        status = ss.get_storage_status()
        data_entry = next(p for p in status.paths if p.name == "data_dir")
        # tmp_path lives on a real filesystem; free / total bytes
        # must be positive integers, not None.
        assert data_entry.free_bytes is not None
        assert data_entry.total_bytes is not None
        assert data_entry.free_bytes >= 0
        assert data_entry.total_bytes > 0


class TestStorageStatusUnwritable:
    """An unwritable data directory surfaces a clear warning."""

    def test_unwritable_directory_warning(self, monkeypatch, tmp_path):
        if os.geteuid() == 0:
            pytest.skip("running as root — cannot make a directory unwritable")
        root = tmp_path / "ReadOnlyData"
        root.mkdir()
        monkeypatch.setenv(core_paths.ENV_VAR, str(root))
        os.chmod(root, stat.S_IRUSR | stat.S_IXUSR)
        try:
            status = ss.get_storage_status()
            data_entry = next(
                p for p in status.paths if p.name == "data_dir"
            )
            assert data_entry.writable is False
            assert any(
                "not writable" in w for w in data_entry.warnings
            )
        finally:
            os.chmod(root, stat.S_IRWXU)


class TestStorageStatusMissingDirectory:
    """A configured-but-missing directory is reported, never created."""

    def test_missing_directory_does_not_create_it(
        self, monkeypatch, tmp_path,
    ):
        root = tmp_path / "Missing" / "NovaData"
        monkeypatch.setenv(core_paths.ENV_VAR, str(root))
        assert not root.exists()
        status = ss.get_storage_status()
        data_entry = next(p for p in status.paths if p.name == "data_dir")
        # The reporter must NOT create the directory.
        assert not root.exists(), (
            "get_storage_status must not create the data directory"
        )
        # exists=False, but the report still produces a value.
        assert data_entry.exists is False


class TestStorageStatusTransientMount:
    """Transient mount paths trigger a clear warning."""

    def test_run_media_warning(self, monkeypatch):
        # We don't actually write under /run/media — the classifier
        # only looks at the lexical path. Set NOVA_DATA_DIR to a
        # /run/media path and assert the warning is present on the
        # data_dir entry.
        monkeypatch.setenv(
            core_paths.ENV_VAR, "/run/media/alice/disk1/NovaData",
        )
        status = ss.get_storage_status()
        data_entry = next(p for p in status.paths if p.name == "data_dir")
        assert data_entry.mount_class == ss.MOUNT_TRANSIENT
        assert any(
            "transient mount" in w for w in data_entry.warnings
        )

    def test_tmp_warning(self, monkeypatch):
        monkeypatch.setenv(core_paths.ENV_VAR, "/tmp/NovaData-test")
        status = ss.get_storage_status()
        data_entry = next(p for p in status.paths if p.name == "data_dir")
        assert data_entry.mount_class == ss.MOUNT_TMP
        assert any("/tmp" in w for w in data_entry.warnings)


class TestOllamaPath:
    """OLLAMA_MODELS is honoured; default falls back to ~/.ollama/models."""

    def test_configured_env_var(self, monkeypatch, tmp_path):
        ollama_dir = tmp_path / "ollama-models"
        ollama_dir.mkdir()
        monkeypatch.setenv(ss.OLLAMA_MODELS_ENV, str(ollama_dir))
        monkeypatch.setenv(core_paths.ENV_VAR, str(tmp_path / "NovaData"))
        status = ss.get_storage_status()
        entry = next(p for p in status.paths if p.name == "ollama_models")
        assert entry.path == str(ollama_dir)
        assert entry.configured is True

    def test_default_falls_back_to_home(self, monkeypatch, tmp_path):
        monkeypatch.delenv(ss.OLLAMA_MODELS_ENV, raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv(core_paths.ENV_VAR, str(tmp_path / "NovaData"))
        status = ss.get_storage_status()
        entry = next(p for p in status.paths if p.name == "ollama_models")
        assert entry.path == str(tmp_path / ".ollama" / "models")
        assert entry.configured is False
        # Informational warning explains the fall-back.
        assert any(
            "informational" in w.lower() or "default" in w.lower()
            for w in entry.warnings
        )


class TestRecommendations:
    """Recommendations are static, stable, and human-readable."""

    def test_recommendations_are_non_empty(self, monkeypatch, tmp_path):
        monkeypatch.delenv(core_paths.ENV_VAR, raising=False)
        monkeypatch.chdir(tmp_path)
        status = ss.get_storage_status()
        assert len(status.recommendations) >= 3
        for rec in status.recommendations:
            assert isinstance(rec, str)
            assert rec.strip()

    def test_recommendations_mention_ssd_and_ollama(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.delenv(core_paths.ENV_VAR, raising=False)
        monkeypatch.chdir(tmp_path)
        status = ss.get_storage_status()
        joined = " ".join(status.recommendations)
        assert "SSD" in joined
        assert "Ollama" in joined


class TestReadOnly:
    """The reporter must never create or modify files."""

    def test_no_files_created(self, monkeypatch, tmp_path):
        root = tmp_path / "NovaData"
        monkeypatch.setenv(core_paths.ENV_VAR, str(root))
        # The data root does not exist before the call.
        assert not root.exists()
        ss.get_storage_status()
        # And it must still not exist after the call — the reporter
        # is strictly read-only.
        assert not root.exists()
