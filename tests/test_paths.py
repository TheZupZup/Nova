"""Tests for the Nova data-directory foundation (``core.paths``).

The contract under test:

* When ``NOVA_DATA_DIR`` is unset, every path helper returns the same
  legacy values Nova used before this PR, and :func:`prepare` is a
  strict no-op. Existing installs and existing tests that bypass the
  data-dir story must not be disturbed.
* When ``NOVA_DATA_DIR`` is set, the database lives under it, the
  documented subdirectories are created by :func:`prepare`, an
  unwritable target raises a clear error, and the legacy-migration
  reporter describes (without performing) what the operator would
  need to copy.
* ``core.memory`` and ``memory.store`` agree with ``core.paths`` on
  the live database path at module-import time.

These tests never set ``NOVA_DATA_DIR`` to a user-specific absolute
path — every concrete location is rooted at ``tmp_path`` so the suite
can run on any developer host and on CI without local cleanup.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Heavy / optional deps that ``core.memory`` ultimately pulls in via
# its migration imports. Stub them defensively so importing this test
# module never depends on the host having ollama, feedparser, etc.
for _mod in ("ddgs", "ollama", "sgmllib", "feedparser"):
    sys.modules.setdefault(_mod, MagicMock())

from core import paths as core_paths  # noqa: E402


# ── default / unset NOVA_DATA_DIR ───────────────────────────────────


class TestDefaultBehaviour:
    """When ``NOVA_DATA_DIR`` is unset, paths match legacy behaviour."""

    def test_configured_data_dir_returns_none_when_unset(self, monkeypatch):
        monkeypatch.delenv(core_paths.ENV_VAR, raising=False)
        assert core_paths.configured_data_dir() is None

    def test_configured_data_dir_returns_none_when_blank(self, monkeypatch):
        monkeypatch.setenv(core_paths.ENV_VAR, "")
        assert core_paths.configured_data_dir() is None

    def test_configured_data_dir_returns_none_when_whitespace(self, monkeypatch):
        # A blank line in .env should not silently send Nova to "".
        monkeypatch.setenv(core_paths.ENV_VAR, "   ")
        assert core_paths.configured_data_dir() is None

    def test_database_path_is_legacy_relative_when_unset(self, monkeypatch):
        monkeypatch.delenv(core_paths.ENV_VAR, raising=False)
        # The legacy default — relative ``nova.db`` in the working
        # directory — must be preserved bit-for-bit.
        assert core_paths.database_path() == Path("nova.db")

    def test_database_path_returns_pathlib_path(self, monkeypatch):
        monkeypatch.delenv(core_paths.ENV_VAR, raising=False)
        assert isinstance(core_paths.database_path(), Path)

    def test_subdirs_are_relative_when_unset(self, monkeypatch):
        monkeypatch.delenv(core_paths.ENV_VAR, raising=False)
        assert core_paths.backups_dir() == Path("backups")
        assert core_paths.exports_dir() == Path("exports")
        assert core_paths.memory_packs_dir() == Path("memory-packs")
        assert core_paths.logs_dir() == Path("logs")

    def test_prepare_is_noop_when_unset(self, monkeypatch, tmp_path):
        monkeypatch.delenv(core_paths.ENV_VAR, raising=False)
        # Run prepare() in a clean cwd; it must not create directories
        # underneath us and must return None.
        monkeypatch.chdir(tmp_path)
        result = core_paths.prepare()
        assert result is None
        # Nothing should have been touched on disk.
        assert sorted(p.name for p in tmp_path.iterdir()) == []

    def test_describe_legacy_migration_returns_none_when_unset(
        self, monkeypatch
    ):
        monkeypatch.delenv(core_paths.ENV_VAR, raising=False)
        assert core_paths.describe_legacy_migration() is None


# ── NOVA_DATA_DIR configured ────────────────────────────────────────


class TestConfiguredDirectory:
    """When ``NOVA_DATA_DIR`` is set, every helper points under it."""

    def test_configured_data_dir_returns_path(self, monkeypatch, tmp_path):
        monkeypatch.setenv(core_paths.ENV_VAR, str(tmp_path / "NovaData"))
        configured = core_paths.configured_data_dir()
        assert configured == tmp_path / "NovaData"
        assert isinstance(configured, Path)

    def test_database_path_lives_under_configured_root(
        self, monkeypatch, tmp_path
    ):
        root = tmp_path / "NovaData"
        monkeypatch.setenv(core_paths.ENV_VAR, str(root))
        assert core_paths.database_path() == root / "nova.db"

    def test_subdirs_live_under_configured_root(self, monkeypatch, tmp_path):
        root = tmp_path / "NovaData"
        monkeypatch.setenv(core_paths.ENV_VAR, str(root))
        assert core_paths.backups_dir() == root / "backups"
        assert core_paths.exports_dir() == root / "exports"
        assert core_paths.memory_packs_dir() == root / "memory-packs"
        assert core_paths.logs_dir() == root / "logs"

    def test_user_expansion(self, monkeypatch, tmp_path):
        # ``~`` should expand against the user's home; for the test we
        # point ``HOME`` at a tmp dir so the assertion is deterministic.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv(core_paths.ENV_VAR, "~/NovaData")
        assert core_paths.configured_data_dir() == tmp_path / "NovaData"


class TestPrepare:
    """``prepare()`` creates the layout when ``NOVA_DATA_DIR`` is set."""

    def test_creates_root_and_subdirs(self, monkeypatch, tmp_path):
        root = tmp_path / "NovaData"
        monkeypatch.setenv(core_paths.ENV_VAR, str(root))
        returned = core_paths.prepare()
        assert returned == root
        assert root.is_dir()
        for name in ("backups", "exports", "memory-packs", "logs"):
            assert (root / name).is_dir(), f"{name} should be created"

    def test_idempotent(self, monkeypatch, tmp_path):
        root = tmp_path / "NovaData"
        monkeypatch.setenv(core_paths.ENV_VAR, str(root))
        # Pre-create the root and one subdir to simulate a second run.
        (root / "backups").mkdir(parents=True)
        # Drop a marker file to prove prepare() does not nuke contents.
        marker = root / "backups" / "keep.txt"
        marker.write_text("hello", encoding="utf-8")

        core_paths.prepare()
        core_paths.prepare()

        assert marker.exists()
        assert marker.read_text(encoding="utf-8") == "hello"

    def test_existing_file_at_root_path_is_rejected(
        self, monkeypatch, tmp_path
    ):
        # An existing file (not a directory) at the data-dir path must
        # be reported clearly rather than silently coerced.
        path = tmp_path / "NovaData"
        path.write_text("not a directory", encoding="utf-8")
        monkeypatch.setenv(core_paths.ENV_VAR, str(path))

        with pytest.raises(RuntimeError) as exc_info:
            core_paths.prepare()
        # ``mkdir(exist_ok=True)`` raises ``FileExistsError`` (a subclass
        # of ``OSError``) when the path is a regular file; either that
        # branch or the explicit ``is_dir`` check should fire.
        message = str(exc_info.value)
        assert str(path) in message

    def test_unwritable_directory_is_rejected(self, monkeypatch, tmp_path):
        # Skip on platforms where chmod cannot make a directory
        # unwritable (e.g. running as root, which is the default in the
        # CI sandbox). ``os.access`` uses the effective uid; root sees
        # every directory as writable, so we cannot exercise this path.
        if os.geteuid() == 0:
            pytest.skip("running as root — cannot make a directory unwritable")

        root = tmp_path / "NovaData"
        root.mkdir()
        # Remove write bits for the user — Nova must refuse, not retry.
        os.chmod(root, stat.S_IRUSR | stat.S_IXUSR)
        try:
            monkeypatch.setenv(core_paths.ENV_VAR, str(root))
            with pytest.raises(RuntimeError) as exc_info:
                core_paths.prepare()
            assert "not writable" in str(exc_info.value)
        finally:
            # Restore so pytest can clean tmp_path on teardown.
            os.chmod(root, stat.S_IRWXU)


class TestLegacyMigrationReport:
    """``describe_legacy_migration()`` reports without moving anything."""

    def test_should_advise_copy_when_legacy_exists_and_target_empty(
        self, monkeypatch, tmp_path
    ):
        # Pretend the current working directory holds a legacy DB.
        cwd = tmp_path / "checkout"
        cwd.mkdir()
        legacy_db = cwd / "nova.db"
        legacy_db.write_bytes(b"")  # empty file is enough for the check.

        target_root = tmp_path / "NovaData"
        target_root.mkdir()

        monkeypatch.chdir(cwd)
        monkeypatch.setenv(core_paths.ENV_VAR, str(target_root))

        status = core_paths.describe_legacy_migration()
        assert status is not None
        assert status.legacy_exists is True
        assert status.configured_exists is False
        assert status.should_advise_copy is True

        # Critically: the report must not have touched either file.
        assert legacy_db.exists()
        assert not (target_root / "nova.db").exists()

    def test_no_advice_when_target_already_has_db(
        self, monkeypatch, tmp_path
    ):
        cwd = tmp_path / "checkout"
        cwd.mkdir()
        (cwd / "nova.db").write_bytes(b"")
        target_root = tmp_path / "NovaData"
        target_root.mkdir()
        (target_root / "nova.db").write_bytes(b"")

        monkeypatch.chdir(cwd)
        monkeypatch.setenv(core_paths.ENV_VAR, str(target_root))

        status = core_paths.describe_legacy_migration()
        assert status is not None
        assert status.configured_exists is True
        assert status.should_advise_copy is False

    def test_no_advice_when_no_legacy_db(self, monkeypatch, tmp_path):
        cwd = tmp_path / "checkout"
        cwd.mkdir()
        target_root = tmp_path / "NovaData"
        target_root.mkdir()

        monkeypatch.chdir(cwd)
        monkeypatch.setenv(core_paths.ENV_VAR, str(target_root))

        status = core_paths.describe_legacy_migration()
        assert status is not None
        assert status.legacy_exists is False
        assert status.should_advise_copy is False


# ── integration with ``core.memory`` and ``memory.store`` ───────────


class TestModuleIntegration:
    """The DB path attributes pick up ``core.paths`` resolution."""

    def test_core_memory_module_db_path_is_a_string(self):
        # Tests monkeypatch ``core.memory.DB_PATH`` to a plain string;
        # the module attribute must therefore be a string, not a Path,
        # to keep that contract intact.
        from core import memory as core_memory
        assert isinstance(core_memory.DB_PATH, str)

    def test_memory_store_module_db_path_is_a_string(self):
        from memory import store as natural_store
        assert isinstance(natural_store.DB_PATH, str)

    def test_initialize_db_creates_db_at_configured_path(
        self, monkeypatch, tmp_path
    ):
        # End-to-end: with NOVA_DATA_DIR set, ``initialize_db`` should
        # create the database at the configured location and the
        # standard subdirectories alongside it.
        root = tmp_path / "NovaData"
        monkeypatch.setenv(core_paths.ENV_VAR, str(root))
        monkeypatch.setenv("NOVA_USERNAME", "admin")
        monkeypatch.setenv("NOVA_PASSWORD", "pw")

        db_path = str(root / "nova.db")
        from core import memory as core_memory
        from memory import store as natural_store
        monkeypatch.setattr(core_memory, "DB_PATH", db_path)
        monkeypatch.setattr(natural_store, "DB_PATH", db_path)

        core_memory.initialize_db()

        assert (root / "nova.db").is_file()
        # Subdirectories should have been created by prepare().
        for name in ("backups", "exports", "memory-packs", "logs"):
            assert (root / name).is_dir(), f"{name} should be created"

        # The migrated users table should exist with the seeded admin.
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT username FROM users WHERE username = ?", ("admin",)
            ).fetchone()
        assert row is not None

    def test_initialize_db_warns_when_legacy_db_present(
        self, monkeypatch, tmp_path, caplog
    ):
        # When NOVA_DATA_DIR is set and the legacy ./nova.db still
        # exists, ``initialize_db`` should emit a single WARNING log
        # line. The legacy file must remain untouched.
        cwd = tmp_path / "checkout"
        cwd.mkdir()
        legacy_db = cwd / "nova.db"
        legacy_db.write_bytes(b"")
        target_root = tmp_path / "NovaData"

        monkeypatch.chdir(cwd)
        monkeypatch.setenv(core_paths.ENV_VAR, str(target_root))
        monkeypatch.setenv("NOVA_USERNAME", "admin")
        monkeypatch.setenv("NOVA_PASSWORD", "pw")

        db_path = str(target_root / "nova.db")
        from core import memory as core_memory
        from memory import store as natural_store
        monkeypatch.setattr(core_memory, "DB_PATH", db_path)
        monkeypatch.setattr(natural_store, "DB_PATH", db_path)

        import logging
        with caplog.at_level(logging.WARNING, logger="core.memory"):
            core_memory.initialize_db()

        assert legacy_db.exists()
        assert legacy_db.read_bytes() == b""
        warned = [
            r for r in caplog.records
            if r.levelno == logging.WARNING
            and "legacy database" in r.getMessage()
        ]
        assert warned, "Expected a WARNING about the legacy database"

    def test_initialize_db_does_not_overwrite_existing_db(
        self, monkeypatch, tmp_path
    ):
        # An existing database at the configured path must be left
        # alone. ``initialize_db`` is idempotent — it CREATES TABLE
        # IF NOT EXISTS — and must not truncate, replace, or move the
        # underlying file.
        root = tmp_path / "NovaData"
        root.mkdir()
        db_path = root / "nova.db"
        # Seed a DB with one row of recognisable content. We use the
        # ``settings`` table since the schema knows about it.
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                "CREATE TABLE settings (key TEXT PRIMARY KEY, "
                "value TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?)",
                ("preexisting", "yes"),
            )

        monkeypatch.setenv(core_paths.ENV_VAR, str(root))
        monkeypatch.setenv("NOVA_USERNAME", "admin")
        monkeypatch.setenv("NOVA_PASSWORD", "pw")
        from core import memory as core_memory
        from memory import store as natural_store
        monkeypatch.setattr(core_memory, "DB_PATH", str(db_path))
        monkeypatch.setattr(natural_store, "DB_PATH", str(db_path))

        core_memory.initialize_db()

        # The pre-existing row must survive.
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = 'preexisting'"
            ).fetchone()
        assert row is not None
        assert row[0] == "yes"


# ── stability of path helpers ───────────────────────────────────────


class TestStability:
    """Path helpers return stable values across repeated calls."""

    def test_repeated_calls_return_equal_paths_when_unset(self, monkeypatch):
        monkeypatch.delenv(core_paths.ENV_VAR, raising=False)
        assert core_paths.database_path() == core_paths.database_path()
        assert core_paths.backups_dir() == core_paths.backups_dir()

    def test_repeated_calls_return_equal_paths_when_set(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv(core_paths.ENV_VAR, str(tmp_path / "x"))
        assert core_paths.database_path() == core_paths.database_path()
        assert core_paths.exports_dir() == core_paths.exports_dir()

    def test_changing_env_changes_resolution(self, monkeypatch, tmp_path):
        # Reading the env on every call is a deliberate contract — it
        # is what lets pytest's ``monkeypatch.setenv`` propagate. Pin
        # that behaviour with a direct test.
        monkeypatch.setenv(core_paths.ENV_VAR, str(tmp_path / "a"))
        assert core_paths.database_path() == tmp_path / "a" / "nova.db"
        monkeypatch.setenv(core_paths.ENV_VAR, str(tmp_path / "b"))
        assert core_paths.database_path() == tmp_path / "b" / "nova.db"
