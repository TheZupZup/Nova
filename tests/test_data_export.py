"""Tests for the data export builder, inspector, and restore planner.

Pinned contracts (see ``core/data_export.py``):

* Phase 1 supports only ``mode="data-only"``. Workspace mode raises.
* The export archive contains a top-level ``manifest.json`` and
  ``RESTORE.md`` plus a ``data/`` tree of Nova-owned files.
* Only canonical Nova entries are included: ``nova.db``,
  ``nova.db.*`` sidecars, and the four reserved subdirectories.
  ``.env``, ``.git``, ``.venv``, caches, SSH keys, and arbitrary
  files at the data root are excluded with a reason.
* Symlinks whose targets escape the data root are recorded as
  ``symlink_escape`` and never followed.
* The manifest pins the format identifier and version.
* Inspect refuses tarballs with path traversal, hardlinks, devices,
  or symlinks pointing outside the archive.
* Restore is dry-run only: it refuses to overwrite an existing
  ``nova.db`` at the target and never writes a file.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from core import paths as core_paths
from core import data_export as de


# ── Helpers ─────────────────────────────────────────────────────────


def _seed_data_dir(root: Path) -> None:
    """Drop a small canonical Nova data layout into ``root``.

    The helper writes the SQLite-shaped file, one sidecar backup,
    one preupgrade backup, and one file under each of the reserved
    subdirectories. Content is deterministic so SHA-256s in the
    manifest are reproducible across runs.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "nova.db").write_bytes(b"-- fake SQLite content --")
    (root / "nova.db.backup").write_bytes(b"-- backup --")
    (root / "nova.db.preupgrade-20260101T000000Z").write_bytes(b"-- preup --")
    for sub in ("backups", "exports", "memory-packs", "logs"):
        (root / sub).mkdir(exist_ok=True)
    (root / "backups" / "nova-2026-05-14.db").write_bytes(b"-- backup pack --")
    (root / "memory-packs" / "trip-2026.json").write_bytes(b'{"mem": []}')
    (root / "logs" / "import.log").write_bytes(b"loaded ok\n")


@pytest.fixture
def configured_data_dir(monkeypatch, tmp_path):
    root = tmp_path / "NovaData"
    monkeypatch.setenv(core_paths.ENV_VAR, str(root))
    _seed_data_dir(root)
    return root


# ── create_data_export ──────────────────────────────────────────────


class TestCreateExportHappyPath:
    """The default ``data-only`` export bundles a clean archive."""

    def test_returns_existing_archive(self, configured_data_dir):
        result = de.create_data_export()
        assert os.path.isfile(result.archive_path)
        assert result.archive_size > 0
        assert len(result.archive_sha256) == 64  # sha256 hex digest

    def test_archive_contains_manifest_and_restore_doc(
        self, configured_data_dir,
    ):
        result = de.create_data_export()
        with tarfile.open(result.archive_path, mode="r:*") as tar:
            names = tar.getnames()
        assert de.ARCHIVE_MANIFEST_NAME in names
        assert de.ARCHIVE_RESTORE_DOC_NAME in names

    def test_archive_contains_nova_db_and_subdirs(self, configured_data_dir):
        result = de.create_data_export()
        with tarfile.open(result.archive_path, mode="r:*") as tar:
            names = set(tar.getnames())
        # Top-level data files
        assert "data/nova.db" in names
        assert "data/nova.db.backup" in names
        assert any(n.startswith("data/nova.db.preupgrade-") for n in names)
        # Subdirectory contents
        assert "data/backups/nova-2026-05-14.db" in names
        assert "data/memory-packs/trip-2026.json" in names
        assert "data/logs/import.log" in names

    def test_manifest_pins_format_and_version(self, configured_data_dir):
        result = de.create_data_export()
        assert result.manifest["format"] == de.FORMAT_ID
        assert result.manifest["format_version"] == de.FORMAT_VERSION
        assert result.manifest["mode"] == de.MODE_DATA_ONLY
        assert result.manifest["created_at"]

    def test_manifest_lists_included_files_with_sha256(
        self, configured_data_dir,
    ):
        result = de.create_data_export()
        files = result.manifest["files"]
        # Build a {path: sha256} map and verify by re-hashing the
        # corresponding file on disk.
        paths = {entry["path"]: entry for entry in files}
        assert "nova.db" in paths
        on_disk = (configured_data_dir / "nova.db").read_bytes()
        expected = hashlib.sha256(on_disk).hexdigest()
        assert paths["nova.db"]["sha256"] == expected
        assert paths["nova.db"]["size"] == len(on_disk)

    def test_archive_is_atomic_no_partial_left_on_success(
        self, configured_data_dir,
    ):
        result = de.create_data_export()
        partial = Path(result.archive_path + ".partial")
        assert not partial.exists()


class TestCreateExportSafetyExclusions:
    """The walk respects the strict allowlist for top-level entries."""

    def test_env_file_at_root_is_excluded(self, configured_data_dir):
        (configured_data_dir / ".env").write_text(
            "SECRET_KEY=topsecret\n", encoding="utf-8",
        )
        result = de.create_data_export()
        with tarfile.open(result.archive_path, mode="r:*") as tar:
            names = set(tar.getnames())
        # Nothing called .env should be inside the archive.
        for n in names:
            assert os.path.basename(n).lower() != ".env"
        # And the excluded list must record the reason.
        excluded_names = {e["path"] for e in result.manifest["excluded"]}
        assert ".env" in excluded_names

    def test_git_directory_at_root_is_excluded(self, configured_data_dir):
        (configured_data_dir / ".git").mkdir()
        (configured_data_dir / ".git" / "HEAD").write_text(
            "ref: refs/heads/main\n", encoding="utf-8",
        )
        result = de.create_data_export()
        with tarfile.open(result.archive_path, mode="r:*") as tar:
            names = set(tar.getnames())
        # No .git/* in the archive.
        for n in names:
            assert ".git" not in n.split("/")
        excluded_names = {e["path"] for e in result.manifest["excluded"]}
        assert ".git/" in excluded_names

    def test_venv_directory_at_root_is_excluded(self, configured_data_dir):
        (configured_data_dir / ".venv").mkdir()
        (configured_data_dir / ".venv" / "pyvenv.cfg").write_text(
            "ok\n", encoding="utf-8",
        )
        result = de.create_data_export()
        with tarfile.open(result.archive_path, mode="r:*") as tar:
            names = set(tar.getnames())
        for n in names:
            assert ".venv" not in n.split("/")
        excluded_names = {e["path"] for e in result.manifest["excluded"]}
        assert ".venv/" in excluded_names

    def test_cache_dirs_excluded(self, configured_data_dir):
        (configured_data_dir / "backups" / "__pycache__").mkdir(parents=True)
        (configured_data_dir / "backups" / "__pycache__" / "x.pyc").write_bytes(b"")
        result = de.create_data_export()
        with tarfile.open(result.archive_path, mode="r:*") as tar:
            names = set(tar.getnames())
        for n in names:
            assert "__pycache__" not in n.split("/")
        excluded_names = {e["path"] for e in result.manifest["excluded"]}
        assert any("__pycache__" in e for e in excluded_names)

    def test_ssh_directory_excluded(self, configured_data_dir):
        (configured_data_dir / ".ssh").mkdir()
        (configured_data_dir / ".ssh" / "id_rsa").write_text(
            "PRIVATE KEY\n", encoding="utf-8",
        )
        result = de.create_data_export()
        with tarfile.open(result.archive_path, mode="r:*") as tar:
            names = set(tar.getnames())
        # No SSH key, no .ssh directory.
        for n in names:
            assert ".ssh" not in n.split("/")
            assert os.path.basename(n).lower() != "id_rsa"

    def test_arbitrary_file_outside_allowlist_excluded(
        self, configured_data_dir,
    ):
        # A bogus file dropped next to nova.db must not be picked up.
        (configured_data_dir / "README.txt").write_text(
            "Surprise!", encoding="utf-8",
        )
        result = de.create_data_export()
        with tarfile.open(result.archive_path, mode="r:*") as tar:
            names = set(tar.getnames())
        assert "data/README.txt" not in names
        excluded_names = {e["path"] for e in result.manifest["excluded"]}
        assert "README.txt" in excluded_names

    def test_secret_file_inside_logs_excluded(self, configured_data_dir):
        # A .pem file inside an allowed subdirectory still trips the
        # secret-name detector.
        (configured_data_dir / "logs" / "leaked.pem").write_text(
            "BAD\n", encoding="utf-8",
        )
        result = de.create_data_export()
        with tarfile.open(result.archive_path, mode="r:*") as tar:
            names = set(tar.getnames())
        assert "data/logs/leaked.pem" not in names
        excluded_names = {e["path"] for e in result.manifest["excluded"]}
        assert "logs/leaked.pem" in excluded_names


class TestCreateExportSymlinks:
    """Symlinks escaping the data root are recorded but never followed."""

    def test_symlink_escape_excluded(self, configured_data_dir, tmp_path):
        outside = tmp_path / "outside-secret.txt"
        outside.write_text("private", encoding="utf-8")
        link = configured_data_dir / "memory-packs" / "escape.json"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation unsupported on this host")

        result = de.create_data_export()
        with tarfile.open(result.archive_path, mode="r:*") as tar:
            names = set(tar.getnames())
        assert "data/memory-packs/escape.json" not in names
        excluded_names = {e["path"] for e in result.manifest["excluded"]}
        assert "memory-packs/escape.json" in excluded_names
        # Confirm the reason recorded is the escape one.
        reasons = {
            e["path"]: e["reason"]
            for e in result.manifest["excluded"]
        }
        assert reasons["memory-packs/escape.json"] == de.REASON_SYMLINK_ESCAPE

    def test_symlink_within_data_root_is_included(
        self, configured_data_dir,
    ):
        # A file referenced via a symlink that stays inside the data
        # root is fine — it represents a deliberate operator choice
        # and Nova dereferences during ``tarfile.add(... filter=...)``
        # via the manifest entry. The original target file is also
        # included via its own canonical path.
        inside = configured_data_dir / "memory-packs" / "real.json"
        inside.write_text('{"ok": true}', encoding="utf-8")
        link = configured_data_dir / "memory-packs" / "alias.json"
        try:
            link.symlink_to(inside)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation unsupported on this host")

        result = de.create_data_export()
        with tarfile.open(result.archive_path, mode="r:*") as tar:
            names = set(tar.getnames())
        # The real file is included; the alias is too because it
        # resolves under the data root.
        assert "data/memory-packs/real.json" in names
        assert "data/memory-packs/alias.json" in names


class TestCreateExportMode:
    """Workspace mode is reserved and currently rejected."""

    def test_workspace_mode_rejected(self, configured_data_dir):
        with pytest.raises(ValueError):
            de.create_data_export(mode=de.MODE_WORKSPACE)

    def test_unknown_mode_rejected(self, configured_data_dir):
        with pytest.raises(ValueError):
            de.create_data_export(mode="everything")


class TestCreateExportLegacyMode:
    """When NOVA_DATA_DIR is unset, exports still work but warn."""

    def test_legacy_mode_warning(self, monkeypatch, tmp_path):
        monkeypatch.delenv(core_paths.ENV_VAR, raising=False)
        monkeypatch.chdir(tmp_path)
        # Seed the CWD with a canonical layout.
        _seed_data_dir(tmp_path)
        dest = tmp_path / "exports-out"
        result = de.create_data_export(dest_dir=dest)
        assert any(
            "NOVA_DATA_DIR is not configured" in w
            for w in result.warnings
        )
        # And the archive should still contain nova.db.
        with tarfile.open(result.archive_path, mode="r:*") as tar:
            names = set(tar.getnames())
        assert "data/nova.db" in names


# ── inspect_export ─────────────────────────────────────────────────


class TestInspectExportHappyPath:
    """A freshly-built archive inspects clean."""

    def test_valid_archive(self, configured_data_dir):
        result = de.create_data_export()
        inspection = de.inspect_export(result.archive_path)
        assert inspection.valid is True, inspection.errors
        assert inspection.manifest is not None
        assert inspection.manifest["format"] == de.FORMAT_ID
        assert inspection.total_uncompressed_size > 0


class TestInspectExportRejections:
    """Hostile archives are refused with structured errors."""

    def test_missing_archive(self, tmp_path):
        inspection = de.inspect_export(tmp_path / "nope.tar.gz")
        assert inspection.valid is False
        assert any("does not exist" in e for e in inspection.errors)

    def test_not_a_tarball(self, tmp_path):
        bogus = tmp_path / "fake.tar.gz"
        bogus.write_text("not a tar", encoding="utf-8")
        inspection = de.inspect_export(bogus)
        assert inspection.valid is False
        assert any("not a tar archive" in e.lower() for e in inspection.errors)

    def test_archive_with_path_traversal_member(self, tmp_path):
        bad = tmp_path / "bad.tar.gz"
        with tarfile.open(bad, mode="w:gz") as tar:
            body = b"oops"
            info = tarfile.TarInfo(name="../escape.txt")
            info.size = len(body)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(body))
        inspection = de.inspect_export(bad)
        assert inspection.valid is False
        assert any(
            "unsafe" in e.lower() or "disallowed" in e.lower()
            for e in inspection.errors
        )

    def test_archive_with_absolute_member_name(self, tmp_path):
        bad = tmp_path / "abs.tar.gz"
        with tarfile.open(bad, mode="w:gz") as tar:
            body = b"oops"
            info = tarfile.TarInfo(name="/etc/passwd")
            info.size = len(body)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(body))
        inspection = de.inspect_export(bad)
        assert inspection.valid is False

    def test_archive_with_disallowed_top_level_entry(self, tmp_path):
        # A well-formed manifest plus an extra top-level dir Nova
        # never produces. Must be flagged.
        bad = tmp_path / "extra.tar.gz"
        manifest = json.dumps({
            "format": de.FORMAT_ID,
            "format_version": de.FORMAT_VERSION,
            "mode": de.MODE_DATA_ONLY,
            "files": [],
            "excluded": [],
        }).encode()
        with tarfile.open(bad, mode="w:gz") as tar:
            mi = tarfile.TarInfo(name=de.ARCHIVE_MANIFEST_NAME)
            mi.size = len(manifest)
            mi.mtime = 0
            tar.addfile(mi, io.BytesIO(manifest))
            payload = b"x"
            ei = tarfile.TarInfo(name="not-data/x.txt")
            ei.size = len(payload)
            ei.mtime = 0
            tar.addfile(ei, io.BytesIO(payload))
        inspection = de.inspect_export(bad)
        assert inspection.valid is False
        assert any(
            "Disallowed archive entry at root" in e for e in inspection.errors
        )

    def test_missing_manifest_invalid(self, tmp_path):
        bad = tmp_path / "no-manifest.tar.gz"
        with tarfile.open(bad, mode="w:gz") as tar:
            payload = b"x"
            ei = tarfile.TarInfo(name="data/nova.db")
            ei.size = len(payload)
            ei.mtime = 0
            tar.addfile(ei, io.BytesIO(payload))
        inspection = de.inspect_export(bad)
        assert inspection.valid is False
        assert any("manifest" in e.lower() for e in inspection.errors)

    def test_manifest_with_wrong_format_id(self, tmp_path):
        bad = tmp_path / "wrong-format.tar.gz"
        manifest = json.dumps({
            "format": "different-format",
            "format_version": 1,
            "mode": de.MODE_DATA_ONLY,
            "files": [],
            "excluded": [],
        }).encode()
        with tarfile.open(bad, mode="w:gz") as tar:
            mi = tarfile.TarInfo(name=de.ARCHIVE_MANIFEST_NAME)
            mi.size = len(manifest)
            mi.mtime = 0
            tar.addfile(mi, io.BytesIO(manifest))
        inspection = de.inspect_export(bad)
        assert inspection.valid is False
        assert any("format" in e.lower() for e in inspection.errors)


# ── plan_restore ───────────────────────────────────────────────────


class TestPlanRestoreDryRun:
    """plan_restore never touches the disk and refuses to overwrite."""

    def test_plan_refuses_overwrite_when_target_has_nova_db(
        self, configured_data_dir, tmp_path,
    ):
        # Build an export from one data dir.
        result = de.create_data_export()
        # Plan a restore into a different target that already has
        # a nova.db.
        target = tmp_path / "TargetData"
        target.mkdir()
        existing = target / "nova.db"
        existing.write_bytes(b"-- pre-existing --")

        plan = de.plan_restore(
            result.archive_path, target_data_dir=target,
        )
        assert plan.allowed is False
        assert "already contains nova.db" in plan.refuse_reason
        # The existing file must still be there bit-for-bit.
        assert existing.read_bytes() == b"-- pre-existing --"

    def test_plan_allows_empty_target(self, configured_data_dir, tmp_path):
        result = de.create_data_export()
        target = tmp_path / "FreshTarget"
        target.mkdir()
        plan = de.plan_restore(
            result.archive_path, target_data_dir=target,
        )
        assert plan.allowed is True, plan.refuse_reason
        assert plan.target_data_dir == str(target.resolve())
        assert "nova.db" in plan.would_restore
        # The target directory remains untouched.
        assert list(target.iterdir()) == []

    def test_plan_refuses_archive_with_traversal_member(self, tmp_path):
        bad = tmp_path / "bad.tar.gz"
        # Build a valid manifest, but then write a member with ../.
        manifest = json.dumps({
            "format": de.FORMAT_ID,
            "format_version": de.FORMAT_VERSION,
            "mode": de.MODE_DATA_ONLY,
            "files": [],
            "excluded": [],
        }).encode()
        with tarfile.open(bad, mode="w:gz") as tar:
            mi = tarfile.TarInfo(name=de.ARCHIVE_MANIFEST_NAME)
            mi.size = len(manifest)
            mi.mtime = 0
            tar.addfile(mi, io.BytesIO(manifest))
            evil = b"x"
            ei = tarfile.TarInfo(name="data/../escape.txt")
            ei.size = len(evil)
            ei.mtime = 0
            tar.addfile(ei, io.BytesIO(evil))

        target = tmp_path / "Target"
        target.mkdir()
        plan = de.plan_restore(bad, target_data_dir=target)
        # The inspection step rejects this archive — plan_restore
        # surfaces that as not-allowed.
        assert plan.allowed is False

    def test_plan_without_target_uses_configured_data_dir(
        self, configured_data_dir, monkeypatch, tmp_path,
    ):
        # Build the export, then point NOVA_DATA_DIR at a fresh
        # empty directory and check plan_restore picks it up.
        result = de.create_data_export()
        fresh_target = tmp_path / "FreshConfigured"
        fresh_target.mkdir()
        monkeypatch.setenv(core_paths.ENV_VAR, str(fresh_target))
        plan = de.plan_restore(result.archive_path)
        assert plan.allowed is True
        assert plan.target_data_dir == str(fresh_target.resolve())

    def test_plan_refuses_when_data_dir_unset(
        self, configured_data_dir, monkeypatch,
    ):
        result = de.create_data_export()
        monkeypatch.delenv(core_paths.ENV_VAR, raising=False)
        plan = de.plan_restore(result.archive_path)
        assert plan.allowed is False
        assert "NOVA_DATA_DIR" in plan.refuse_reason

    def test_plan_does_not_write_any_file(
        self, configured_data_dir, tmp_path,
    ):
        result = de.create_data_export()
        target = tmp_path / "Untouched"
        target.mkdir()
        # Take a snapshot of the target tree before and after.
        before = {p for p in target.rglob("*")}
        plan = de.plan_restore(result.archive_path, target_data_dir=target)
        after = {p for p in target.rglob("*")}
        assert before == after
        assert plan.allowed is True


# ── Read-only inspection of public constants ───────────────────────


class TestPublicSurface:
    """The exported constants are stable enough to be wire format."""

    def test_format_id_is_stable_string(self):
        assert de.FORMAT_ID == "nova-data-export"
        assert isinstance(de.FORMAT_VERSION, int)
        assert de.FORMAT_VERSION >= 1

    def test_mode_data_only_is_string(self):
        assert de.MODE_DATA_ONLY == "data-only"

    def test_reasons_are_lower_snake(self):
        for reason in (
            de.REASON_SECRET, de.REASON_VCS, de.REASON_VENV,
            de.REASON_CACHE, de.REASON_NODE_MODULES,
            de.REASON_OLLAMA_MODEL, de.REASON_OUTSIDE_DATA_DIR,
            de.REASON_SYMLINK_ESCAPE, de.REASON_NOT_ALLOWLISTED,
            de.REASON_UNREADABLE, de.REASON_PATH_TRAVERSAL,
            de.REASON_DEVICE_OR_OTHER,
        ):
            assert reason == reason.lower()
            assert " " not in reason
