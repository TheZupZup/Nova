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

    def test_archive_with_symlink_member_is_invalid(self, tmp_path):
        """A symlink entry of any kind invalidates the archive.

        Nova builds tarballs with ``dereference=True`` so a
        Nova-built archive never contains a symlink. An archive
        with a symlink is therefore anomalous; refusing it at
        inspection keeps the dry-run plan honest (the real restore
        would also drop the symlink) and gives the operator a
        clear "this is not a Nova archive" signal.
        """
        bad = tmp_path / "symlinks.tar.gz"
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
            # A "safe" symlink (relative, no ..). Previously this
            # passed inspect but was silently dropped by extract.
            li = tarfile.TarInfo(name="data/alias.json")
            li.type = tarfile.SYMTYPE
            li.linkname = "real.json"
            li.mtime = 0
            tar.addfile(li)
        inspection = de.inspect_export(bad)
        assert inspection.valid is False
        assert any(
            "symlink" in e.lower() for e in inspection.errors
        )

    def test_archive_with_same_path_as_file_and_dir_is_invalid(
        self, tmp_path,
    ):
        """An archive that lists the same path as both a file and
        a directory is unrestorable: extraction creates the
        directory first, then ``open(file_dst, "wb")`` raises
        ``IsADirectoryError``. The previous dry-run would have
        said ``outcome=dry_run`` because it iterated only files;
        the real restore deterministically failed at extraction.
        Inspect now refuses such archives so dry-run and real
        restore agree.

        Both orderings (dir-first / file-first) are detected by
        the two-set tracker.
        """
        for order, members in (
            ("dir-first", [
                ("data/backups/a/", tarfile.DIRTYPE, b""),
                ("data/backups/a", tarfile.REGTYPE, b"file payload"),
            ]),
            ("file-first", [
                ("data/backups/a", tarfile.REGTYPE, b"file payload"),
                ("data/backups/a/", tarfile.DIRTYPE, b""),
            ]),
        ):
            bad = tmp_path / f"collide-{order}.tar.gz"
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
                for name, mtype, payload in members:
                    ti = tarfile.TarInfo(name=name)
                    ti.type = mtype
                    ti.size = len(payload)
                    ti.mtime = 0
                    if payload:
                        tar.addfile(ti, io.BytesIO(payload))
                    else:
                        tar.addfile(ti)
            inspection = de.inspect_export(bad)
            assert inspection.valid is False, (
                f"{order}: expected invalid, got {inspection.errors!r}"
            )
            assert any(
                "both" in e.lower() and (
                    "file" in e.lower() and "directory" in e.lower()
                )
                for e in inspection.errors
            ), (order, inspection.errors)

    def test_dry_run_and_restore_agree_on_file_dir_collision(
        self, tmp_path,
    ):
        """The dry-run and real-restore outcomes must match for an
        archive that has a file/directory same-path collision.
        Both should refuse via ``inspect_export``'s validation —
        not "dry_run" from preview vs "extract_failed" from real.
        """
        bad = tmp_path / "collision.tar.gz"
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
            di = tarfile.TarInfo(name="data/backups/a/")
            di.type = tarfile.DIRTYPE
            di.mtime = 0
            tar.addfile(di)
            fi = tarfile.TarInfo(name="data/backups/a")
            fi.size = 7
            fi.mtime = 0
            tar.addfile(fi, io.BytesIO(b"payload"))

        target = tmp_path / "Target"
        target.mkdir()
        dry = de.apply_restore(
            bad, target_data_dir=target, dry_run=True,
        )
        real = de.apply_restore(
            bad, target_data_dir=target, confirm=True,
        )
        assert dry.outcome == real.outcome, (
            f"dry-run = {dry.outcome!r}, real = {real.outcome!r}"
        )
        assert dry.outcome == de.RESTORE_OUTCOME_REFUSED
        # No backup or staging side effects from either flow.
        assert dry.backup_path == ""
        assert real.backup_path == ""
        assert not (target / de.RESTORE_STAGING_DIRNAME).exists()


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


# ── CLI ────────────────────────────────────────────────────────────


class TestCli:
    """The ``python -m core.data_export`` entry point.

    Driven through :func:`core.data_export._cli` so the assertions do
    not have to spawn a subprocess (mirroring the ``core.paths`` CLI
    test pattern). Each test verifies the exit code, the human-
    readable summary, and the on-disk side effects (or lack thereof).
    """

    def test_export_subcommand_writes_archive(
        self, configured_data_dir, capsys, tmp_path,
    ):
        dest = tmp_path / "out"
        rc = de._cli(["export", "--output", str(dest)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Nova export package created" in out
        # The archive file actually exists on disk.
        archives = list(dest.glob("nova-data-export-*.tar.gz"))
        assert len(archives) == 1

    def test_export_subcommand_rejects_workspace_mode(
        self, configured_data_dir, capsys,
    ):
        rc = de._cli(["export", "--mode", "workspace"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "error" in err.lower()

    def test_inspect_subcommand_on_valid_archive(
        self, configured_data_dir, capsys,
    ):
        # Build a real archive first.
        result = de.create_data_export()
        rc = de._cli(["inspect", result.archive_path])
        assert rc == 0
        out = capsys.readouterr().out
        assert "valid                  : True" in out
        assert "nova.db present        : True" in out

    def test_inspect_subcommand_on_invalid_archive(
        self, capsys, tmp_path,
    ):
        bogus = tmp_path / "broken.tar.gz"
        bogus.write_text("not a tar", encoding="utf-8")
        rc = de._cli(["inspect", str(bogus)])
        assert rc == 1
        out = capsys.readouterr().out
        assert "valid                  : False" in out

    def test_restore_dry_run_refuses_when_target_has_nova_db(
        self, configured_data_dir, tmp_path, capsys,
    ):
        result = de.create_data_export()
        target = tmp_path / "TargetData"
        target.mkdir()
        (target / "nova.db").write_bytes(b"existing")
        rc = de._cli([
            "restore-dry-run", result.archive_path,
            "--data-dir", str(target),
        ])
        # Refusal is a result, not a CLI error — exit 0 with the plan.
        assert rc == 0
        out = capsys.readouterr().out
        assert "allowed         : False" in out
        assert "already contains nova.db" in out
        # The dry-run never touched the target.
        assert (target / "nova.db").read_bytes() == b"existing"

    def test_restore_dry_run_allows_clean_target(
        self, configured_data_dir, tmp_path, capsys,
    ):
        result = de.create_data_export()
        target = tmp_path / "FreshTarget"
        target.mkdir()
        rc = de._cli([
            "restore-dry-run", result.archive_path,
            "--data-dir", str(target),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "allowed         : True" in out
        # Even on a clean target, the dry-run never extracts anything.
        assert list(target.iterdir()) == []

    def test_unknown_command_prints_usage(self, capsys):
        rc = de._cli([])
        assert rc == 2
        err = capsys.readouterr().err
        # argparse prints the program description / available subcommands.
        assert "export" in err
        assert "inspect" in err
        assert "restore-dry-run" in err

    def test_export_subcommand_rejects_unsafe_stem(
        self, configured_data_dir, capsys, tmp_path,
    ):
        dest = tmp_path / "out"
        rc = de._cli([
            "export", "--output", str(dest), "--stem", "../escape",
        ])
        assert rc == 1
        err = capsys.readouterr().err
        assert "error" in err.lower()


# ── apply_restore (Phase 3, real restore) ──────────────────────────


def _make_archive_with_payload(
    tmp_path: Path,
    files: dict[str, bytes],
    *,
    manifest_overrides: dict | None = None,
) -> Path:
    """Build a synthetic Nova export archive at ``tmp_path/test.tar.gz``.

    ``files`` maps POSIX-relative paths (under ``data/``) to bytes. The
    helper writes a well-formed manifest so the resulting archive
    inspects clean; tests that probe rejection paths build their own
    archive directly via ``tarfile``.
    """
    archive = tmp_path / "test.tar.gz"
    file_entries = []
    for rel, body in files.items():
        file_entries.append({
            "path": rel,
            "size": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        })
    manifest = {
        "format": de.FORMAT_ID,
        "format_version": de.FORMAT_VERSION,
        "mode": de.MODE_DATA_ONLY,
        "created_at": "20260514T120000Z",
        "nova_version": "0.0.0",
        "source_data_dir": "/source",
        "files": file_entries,
        "excluded": [],
        "warnings": [],
    }
    if manifest_overrides:
        manifest.update(manifest_overrides)
    with tarfile.open(archive, mode="w:gz") as tar:
        body = json.dumps(manifest, sort_keys=True).encode("utf-8")
        info = tarfile.TarInfo(name=de.ARCHIVE_MANIFEST_NAME)
        info.size = len(body)
        info.mtime = 0
        tar.addfile(info, io.BytesIO(body))
        for rel, payload in files.items():
            info = tarfile.TarInfo(name=f"data/{rel}")
            info.size = len(payload)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(payload))
    return archive


class TestApplyRestoreDryRun:
    """Dry-run mode never writes and surfaces what would happen."""

    def test_dry_run_does_not_write(self, configured_data_dir, tmp_path):
        result = de.create_data_export()
        target = tmp_path / "FreshTarget"
        target.mkdir()
        before = sorted(target.rglob("*"))
        out = de.apply_restore(
            result.archive_path,
            target_data_dir=target,
            dry_run=True,
        )
        after = sorted(target.rglob("*"))
        assert before == after
        assert out.outcome == de.RESTORE_OUTCOME_DRY_RUN
        assert "nova.db" in out.restored_files
        assert out.backup_path == ""

    def test_dry_run_flags_existing_nova_db(
        self, configured_data_dir, tmp_path,
    ):
        result = de.create_data_export()
        target = tmp_path / "TargetData"
        target.mkdir()
        (target / "nova.db").write_bytes(b"existing")
        out = de.apply_restore(
            result.archive_path,
            target_data_dir=target,
            dry_run=True,
        )
        assert out.outcome == de.RESTORE_OUTCOME_DRY_RUN
        assert out.restart_recommended is True
        assert "nova.db" in out.conflicts
        # Target file untouched.
        assert (target / "nova.db").read_bytes() == b"existing"

    def test_dry_run_refuses_invalid_archive(self, tmp_path):
        bogus = tmp_path / "broken.tar.gz"
        bogus.write_text("not a tar", encoding="utf-8")
        target = tmp_path / "T"
        target.mkdir()
        out = de.apply_restore(
            bogus, target_data_dir=target, dry_run=True,
        )
        assert out.outcome == de.RESTORE_OUTCOME_REFUSED

    def test_dry_run_does_not_create_missing_target(
        self, configured_data_dir, tmp_path,
    ):
        """A dry-run must be strictly read-only on the filesystem.

        Earlier implementations called ``target_root.mkdir(...)``
        before the dry-run early return, which created
        ``NOVA_DATA_DIR`` on disk whenever the configured target did
        not yet exist. The dry-run path is now deferred to *after*
        the early return so a dry-run against a missing target
        leaves the filesystem untouched.
        """
        result = de.create_data_export()
        missing_target = tmp_path / "DoesNotExist"
        assert not missing_target.exists()
        out = de.apply_restore(
            result.archive_path,
            target_data_dir=missing_target,
            dry_run=True,
        )
        # The plan was computed against the would-be target path.
        assert out.outcome == de.RESTORE_OUTCOME_DRY_RUN
        assert "nova.db" in out.restored_files
        # Critical: the dry-run did NOT create the target directory.
        assert not missing_target.exists()

    def test_dry_run_refuses_when_target_has_symlink_destination(
        self, configured_data_dir, tmp_path,
    ):
        """The dry-run must mirror the real-restore type-gate.

        If ``NOVA_DATA_DIR/nova.db`` is a symlink, the real restore
        will refuse in ``_copy_into_target``. The dry-run preflight
        therefore needs to refuse too — otherwise ``inspect →
        dry-run → confirm`` would report ``outcome=dry_run`` and
        the real restore would deterministically fail at execution
        time, breaking the contract.
        """
        result = de.create_data_export()
        target = tmp_path / "Target"
        target.mkdir()
        real = target / "actual.txt"
        real.write_text("real", encoding="utf-8")
        link = target / "nova.db"
        try:
            link.symlink_to(real)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation unsupported on this host")

        out = de.apply_restore(
            result.archive_path,
            target_data_dir=target,
            dry_run=True,
        )
        # Dry-run reports REFUSED — and never wrote a thing.
        assert out.outcome == de.RESTORE_OUTCOME_REFUSED
        assert "symlink" in out.refuse_reason.lower()
        assert out.backup_path == ""
        # No staging or partial state.
        assert not (target / de.RESTORE_STAGING_DIRNAME).exists()
        # Link + target both intact.
        assert link.is_symlink()
        assert real.read_text(encoding="utf-8") == "real"


class TestApplyRestoreConfirmation:
    """The real restore refuses without explicit confirmation."""

    def test_refuses_without_confirm(self, configured_data_dir, tmp_path):
        result = de.create_data_export()
        target = tmp_path / "Target"
        target.mkdir()
        out = de.apply_restore(
            result.archive_path,
            target_data_dir=target,
            confirm=False,
        )
        assert out.outcome == de.RESTORE_OUTCOME_REFUSED
        assert "confirmation" in out.refuse_reason.lower()
        # Target untouched.
        assert list(target.iterdir()) == []

    def test_refuses_unknown_manifest_id(
        self, configured_data_dir, tmp_path,
    ):
        result = de.create_data_export()
        target = tmp_path / "Target"
        target.mkdir()
        out = de.apply_restore(
            result.archive_path,
            target_data_dir=target,
            confirm=True,
            confirmed_manifest_id="not-the-right-id",
        )
        assert out.outcome == de.RESTORE_OUTCOME_REFUSED
        assert "manifest" in out.refuse_reason.lower()

    def test_accepts_matching_manifest_id(
        self, configured_data_dir, tmp_path,
    ):
        result = de.create_data_export()
        target = tmp_path / "Target"
        target.mkdir()
        manifest_id = result.manifest["created_at"]
        out = de.apply_restore(
            result.archive_path,
            target_data_dir=target,
            confirm=True,
            confirmed_manifest_id=manifest_id,
        )
        assert out.outcome == de.RESTORE_OUTCOME_RESTORED

    def test_pin_refuses_archive_with_missing_manifest_id(self, tmp_path):
        """A pin must refuse even when the archive has no created_at.

        Otherwise a hostile archive could omit ``created_at`` from
        its manifest and bypass the pin check entirely (the previous
        implementation only rejected when both strings were
        non-empty). The pin is a deliberate "I inspected exactly
        this archive" assertion — a missing archive id is not the
        same identity as the operator's pin.
        """
        archive = _make_archive_with_payload(
            tmp_path,
            {"nova.db": b"-- fake db --"},
            manifest_overrides={"created_at": ""},
        )
        target = tmp_path / "Target"
        target.mkdir()
        out = de.apply_restore(
            archive,
            target_data_dir=target,
            confirm=True,
            confirmed_manifest_id="20260514T120000Z",
        )
        assert out.outcome == de.RESTORE_OUTCOME_REFUSED
        assert "manifest" in out.refuse_reason.lower()
        # Target untouched.
        assert list(target.iterdir()) == []

    def test_empty_pin_is_treated_as_no_pin(self, tmp_path):
        """An empty ``confirmed_manifest_id`` is "no pin requested"."""
        archive = _make_archive_with_payload(
            tmp_path,
            {"nova.db": b"-- fake db --"},
            manifest_overrides={"created_at": ""},
        )
        target = tmp_path / "Target"
        target.mkdir()
        # Empty string and None both mean "no pin". Verify both.
        for pin in ("", None):
            (target / "nova.db").unlink(missing_ok=True)
            # Clear backups so the second pass starts fresh.
            backups = target / "backups"
            if backups.exists():
                import shutil
                shutil.rmtree(backups)
            out = de.apply_restore(
                archive,
                target_data_dir=target,
                confirm=True,
                confirmed_manifest_id=pin,
            )
            assert out.outcome == de.RESTORE_OUTCOME_RESTORED, (pin, out.refuse_reason)


class TestApplyRestoreSuccess:
    """A confirmed restore copies the archive's data files into target."""

    def test_restores_nova_db_into_empty_target(
        self, configured_data_dir, tmp_path,
    ):
        result = de.create_data_export()
        target = tmp_path / "FreshTarget"
        target.mkdir()
        out = de.apply_restore(
            result.archive_path,
            target_data_dir=target,
            confirm=True,
        )
        assert out.outcome == de.RESTORE_OUTCOME_RESTORED
        assert (target / "nova.db").is_file()
        # Content matches what the source nova.db had.
        assert (target / "nova.db").read_bytes() == (
            configured_data_dir / "nova.db"
        ).read_bytes()
        # restart hint surfaces because nova.db was in the archive.
        assert out.restart_recommended is True

    def test_replaces_existing_nova_db_after_backup(
        self, configured_data_dir, tmp_path,
    ):
        result = de.create_data_export()
        target = tmp_path / "TargetData"
        target.mkdir()
        old_db = b"-- old database --"
        (target / "nova.db").write_bytes(old_db)
        out = de.apply_restore(
            result.archive_path,
            target_data_dir=target,
            confirm=True,
        )
        assert out.outcome == de.RESTORE_OUTCOME_RESTORED
        assert out.backup_path != ""
        backup = Path(out.backup_path)
        assert backup.is_file()
        # Backup contains the previous nova.db.
        with tarfile.open(backup, mode="r:*") as tar:
            names = set(tar.getnames())
        assert "data/nova.db" in names
        # The new database lives at target.
        assert (target / "nova.db").read_bytes() != old_db
        # The conflict is recorded.
        assert "nova.db" in out.conflicts

    def test_staging_directory_cleaned_after_success(
        self, configured_data_dir, tmp_path,
    ):
        result = de.create_data_export()
        target = tmp_path / "Target"
        target.mkdir()
        out = de.apply_restore(
            result.archive_path,
            target_data_dir=target,
            confirm=True,
        )
        assert out.outcome == de.RESTORE_OUTCOME_RESTORED
        staging = target / de.RESTORE_STAGING_DIRNAME
        assert not staging.exists()

    def test_restore_writes_backup_under_pre_restore_subdir(
        self, configured_data_dir, tmp_path,
    ):
        result = de.create_data_export()
        target = tmp_path / "Target"
        target.mkdir()
        (target / "nova.db").write_bytes(b"orig")
        out = de.apply_restore(
            result.archive_path,
            target_data_dir=target,
            confirm=True,
        )
        assert out.outcome == de.RESTORE_OUTCOME_RESTORED
        backup_dir = (
            target / "backups" / de.PRE_RESTORE_BACKUP_SUBDIR
        )
        assert backup_dir.is_dir()
        archives = list(backup_dir.glob("nova-pre-restore-*.tar.gz"))
        assert len(archives) >= 1


class TestApplyRestoreSafety:
    """Hostile archives are refused — current data is never corrupted."""

    def test_refuses_path_traversal_archive(self, tmp_path):
        bad = tmp_path / "evil.tar.gz"
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
            payload = b"hostile"
            ei = tarfile.TarInfo(name="data/../escape.txt")
            ei.size = len(payload)
            ei.mtime = 0
            tar.addfile(ei, io.BytesIO(payload))
        target = tmp_path / "Target"
        target.mkdir()
        original = target / "nova.db"
        original.write_bytes(b"keep me")
        out = de.apply_restore(
            bad, target_data_dir=target, confirm=True,
        )
        assert out.outcome == de.RESTORE_OUTCOME_REFUSED
        # Existing data untouched.
        assert original.read_bytes() == b"keep me"
        # The bad escape file did not land outside the target.
        assert not (target.parent / "escape.txt").exists()

    def test_refuses_absolute_archive_entry(self, tmp_path):
        bad = tmp_path / "abs.tar.gz"
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
            ei = tarfile.TarInfo(name="/etc/passwd")
            ei.size = len(payload)
            ei.mtime = 0
            tar.addfile(ei, io.BytesIO(payload))
        target = tmp_path / "Target"
        target.mkdir()
        out = de.apply_restore(
            bad, target_data_dir=target, confirm=True,
        )
        assert out.outcome == de.RESTORE_OUTCOME_REFUSED

    def test_refuses_symlink_archive_member(self, tmp_path):
        bad = tmp_path / "link.tar.gz"
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
            # Insert a symlink-type entry pointing outside the target.
            li = tarfile.TarInfo(name="data/escape")
            li.type = tarfile.SYMTYPE
            li.linkname = "/etc/passwd"
            li.mtime = 0
            tar.addfile(li)
        target = tmp_path / "Target"
        target.mkdir()
        out = de.apply_restore(
            bad, target_data_dir=target, confirm=True,
        )
        # inspect_export refuses the symlink up-front.
        assert out.outcome == de.RESTORE_OUTCOME_REFUSED

    def test_refuses_unsupported_manifest_version(self, tmp_path):
        bad = tmp_path / "futureproof.tar.gz"
        manifest = json.dumps({
            "format": de.FORMAT_ID,
            "format_version": 999,
            "mode": de.MODE_DATA_ONLY,
            "files": [],
            "excluded": [],
        }).encode()
        with tarfile.open(bad, mode="w:gz") as tar:
            mi = tarfile.TarInfo(name=de.ARCHIVE_MANIFEST_NAME)
            mi.size = len(manifest)
            mi.mtime = 0
            tar.addfile(mi, io.BytesIO(manifest))
        target = tmp_path / "Target"
        target.mkdir()
        out = de.apply_restore(
            bad, target_data_dir=target, confirm=True,
        )
        assert out.outcome == de.RESTORE_OUTCOME_REFUSED

    def test_refuses_path_type_collision_file_then_subfile(
        self, tmp_path,
    ):
        """An archive with both ``data/backups/a`` (file) and
        ``data/backups/a/b.txt`` (file) is structurally unrestorable
        — ``mkdir`` cannot create ``backups/a/`` when ``backups/a``
        is already a file. The preflight refuses upfront so the
        operator gets a single clear refusal instead of a
        ``FAILED`` outcome mid-extraction.
        """
        # Ordering matters for the test of the "ancestor was added
        # as a file" branch: file ``backups/a`` is added first,
        # then ``backups/a/b.txt`` tries to walk ``backups/a`` as
        # an ancestor and finds it in ``file_paths_seen``.
        archive = _make_archive_with_payload(
            tmp_path,
            {
                "nova.db": b"-- db --",
                "backups/a": b"file-at-collision",
                "backups/a/b.txt": b"inner content",
            },
        )
        target = tmp_path / "Target"
        target.mkdir()
        out = de.apply_restore(
            archive, target_data_dir=target, confirm=True,
        )
        assert out.outcome == de.RESTORE_OUTCOME_REFUSED
        assert "collision" in out.refuse_reason.lower()
        # Target untouched — no backup, no staging.
        assert list(target.iterdir()) == []
        assert out.backup_path == ""

    def test_refuses_path_type_collision_subfile_then_file(
        self, tmp_path,
    ):
        """The opposite ordering (``backups/a/b.txt`` first, then
        ``backups/a`` as a file) must also be refused: when the
        later file is processed, its name already lives in
        ``dir_paths_seen`` from the earlier file's ancestor walk.
        """
        archive = tmp_path / "collide-reverse.tar.gz"
        manifest = json.dumps({
            "format": de.FORMAT_ID,
            "format_version": de.FORMAT_VERSION,
            "mode": de.MODE_DATA_ONLY,
            "created_at": "20260514T120000Z",
            "files": [],
            "excluded": [],
        }).encode()
        with tarfile.open(archive, mode="w:gz") as tar:
            mi = tarfile.TarInfo(name=de.ARCHIVE_MANIFEST_NAME)
            mi.size = len(manifest)
            mi.mtime = 0
            tar.addfile(mi, io.BytesIO(manifest))
            # Reverse order: deeper entry first.
            for path, payload in (
                ("data/nova.db", b"-- db --"),
                ("data/backups/a/b.txt", b"inner content"),
                ("data/backups/a", b"file-at-collision"),
            ):
                ti = tarfile.TarInfo(name=path)
                ti.size = len(payload)
                ti.mtime = 0
                tar.addfile(ti, io.BytesIO(payload))
        target = tmp_path / "Target"
        target.mkdir()
        out = de.apply_restore(
            archive, target_data_dir=target, confirm=True,
        )
        assert out.outcome == de.RESTORE_OUTCOME_REFUSED
        assert "collision" in out.refuse_reason.lower()
        assert list(target.iterdir()) == []
        assert out.backup_path == ""

    def test_dry_run_refuses_path_type_collision(self, tmp_path):
        """The dry-run preview must refuse path-type collisions too
        so inspect / dry-run / real restore stay consistent.
        """
        archive = _make_archive_with_payload(
            tmp_path,
            {
                "nova.db": b"-- db --",
                "backups/a": b"file-at-collision",
                "backups/a/b.txt": b"inner",
            },
        )
        target = tmp_path / "Target"
        target.mkdir()
        out = de.apply_restore(
            archive, target_data_dir=target, dry_run=True,
        )
        assert out.outcome == de.RESTORE_OUTCOME_REFUSED
        assert "collision" in out.refuse_reason.lower()
        # Dry-run never writes anything.
        assert list(target.iterdir()) == []

    def test_refuses_when_staging_directory_already_exists(
        self, configured_data_dir, tmp_path,
    ):
        """Two restores against the same target must not interfere.

        The previous implementation unconditionally cleaned up the
        staging directory at startup. A second restore launched
        while the first was still running would therefore delete
        the first restore's rollback stash, breaking the "failed
        restore leaves data intact" guarantee for the original
        caller. The fix uses ``mkdir(exist_ok=False)`` atomically
        and refuses when the staging directory is already present,
        whether from a concurrent restore or from a previous run
        that crashed before cleanup.
        """
        result = de.create_data_export()
        target = tmp_path / "Target"
        target.mkdir()
        marker = b"original-db"
        (target / "nova.db").write_bytes(marker)
        # Simulate a concurrent (or crashed) restore's staging:
        # the directory exists with in-flight state inside.
        staging = target / de.RESTORE_STAGING_DIRNAME
        staging.mkdir()
        stash = staging / ".replaced-originals"
        stash.mkdir()
        (stash / "nova.db").write_bytes(b"in-flight-stash-payload")

        out = de.apply_restore(
            result.archive_path,
            target_data_dir=target,
            confirm=True,
        )
        assert out.outcome == de.RESTORE_OUTCOME_REFUSED
        assert "staging" in out.refuse_reason.lower()
        # Existing target file untouched.
        assert (target / "nova.db").read_bytes() == marker
        # The "in-flight" staging area was not clobbered — the
        # stash file inside it is still intact, so the other
        # (hypothetical) restore can still roll back if it needs
        # to.
        assert stash.is_dir()
        assert (stash / "nova.db").read_bytes() == b"in-flight-stash-payload"

    def test_directory_only_archive_dry_run_and_restore_agree(
        self, tmp_path,
    ):
        """An archive with only directory members must produce the
        same outcome from dry-run and real restore.

        ``inspect_export`` used to add directory members to
        ``InspectionResult.files``; the preview loop then treated
        them as restorable, so ``apply_restore(dry_run=True)``
        reported ``outcome=dry_run`` while the real restore later
        refused with ``"Archive contained no extractable Nova data
        files."``. The fix excludes directory members from
        ``file_names`` so both paths agree.
        """
        # Build an archive with manifest + a single directory entry.
        archive = tmp_path / "dirs-only.tar.gz"
        manifest = json.dumps({
            "format": de.FORMAT_ID,
            "format_version": de.FORMAT_VERSION,
            "mode": de.MODE_DATA_ONLY,
            "created_at": "20260514T120000Z",
            "files": [],
            "excluded": [],
        }).encode()
        with tarfile.open(archive, mode="w:gz") as tar:
            mi = tarfile.TarInfo(name=de.ARCHIVE_MANIFEST_NAME)
            mi.size = len(manifest)
            mi.mtime = 0
            tar.addfile(mi, io.BytesIO(manifest))
            di = tarfile.TarInfo(name="data/backups/foo")
            di.type = tarfile.DIRTYPE
            di.mtime = 0
            tar.addfile(di)

        target = tmp_path / "Target"
        target.mkdir()

        dry = de.apply_restore(
            archive, target_data_dir=target, dry_run=True,
        )
        real = de.apply_restore(
            archive, target_data_dir=target, confirm=True,
        )
        # Both flows must converge to the same answer.
        assert dry.outcome == real.outcome, (
            f"dry-run reported {dry.outcome!r} but real restore "
            f"reported {real.outcome!r}; inspect → dry-run → "
            "confirm contract broken."
        )
        # Specifically, both should refuse — there's nothing to
        # restore. Real restore says "no extractable files"; the
        # dry-run should not be misleadingly "would proceed".
        assert real.outcome == de.RESTORE_OUTCOME_REFUSED
        assert dry.outcome == de.RESTORE_OUTCOME_REFUSED
        # No staging or backup state was produced.
        assert dry.backup_path == ""
        assert real.backup_path == ""
        assert not (target / de.RESTORE_STAGING_DIRNAME).exists()

    def test_failed_restore_leaves_existing_data_intact(
        self, configured_data_dir, tmp_path, monkeypatch,
    ):
        """If the copy phase fails halfway, the target keeps its files."""
        result = de.create_data_export()
        target = tmp_path / "Target"
        target.mkdir()
        marker = b"-- precious original --"
        (target / "nova.db").write_bytes(marker)

        # Force the copy step to fail. ``_copy_into_target`` is the
        # only place where the staged files migrate into the target;
        # patching it returns a controlled failure without disturbing
        # the backup-write path (which uses the same OS primitives).
        def fake_copy(staging, target_root, files):
            return [], [], "synthetic failure during restore copy"

        monkeypatch.setattr(de, "_copy_into_target", fake_copy)
        out = de.apply_restore(
            result.archive_path,
            target_data_dir=target,
            confirm=True,
        )
        # Restore was aborted.
        assert out.outcome == de.RESTORE_OUTCOME_FAILED
        # Original database survived intact (it was also captured
        # inside the pre-restore backup before any copy attempt).
        assert (target / "nova.db").read_bytes() == marker
        # Backup was created before the failure.
        assert out.backup_path != ""
        # Staging directory cleaned.
        assert not (target / de.RESTORE_STAGING_DIRNAME).exists()

    def test_backup_failure_aborts_restore(
        self, configured_data_dir, tmp_path, monkeypatch,
    ):
        """When the backup step fails the restore must abort cleanly."""
        result = de.create_data_export()
        target = tmp_path / "Target"
        target.mkdir()
        marker = b"-- keep me --"
        (target / "nova.db").write_bytes(marker)

        def fake_backup(target_root):
            return None, 0, ["Synthetic failure during backup"]

        monkeypatch.setattr(de, "_create_pre_restore_backup", fake_backup)
        out = de.apply_restore(
            result.archive_path,
            target_data_dir=target,
            confirm=True,
        )
        assert out.outcome == de.RESTORE_OUTCOME_BACKUP_FAILED
        # Existing data untouched.
        assert (target / "nova.db").read_bytes() == marker
        # No staging directory left behind.
        assert not (target / de.RESTORE_STAGING_DIRNAME).exists()

    def test_backup_read_failure_aborts_restore(
        self, configured_data_dir, tmp_path, monkeypatch,
    ):
        """An unreadable canonical file in the target must abort the
        restore, not be silently skipped.

        Treating a read failure as "exclude this file from the
        backup and continue" would let ``apply_restore`` proceed to
        replace a file the operator has no recoverable copy of.
        The backup builder now returns failure on the first OSError
        from ``stat()`` / ``_sha256_file`` so ``apply_restore``
        surfaces ``outcome=backup_failed`` and leaves the target
        intact.
        """
        result = de.create_data_export()
        target = tmp_path / "Target"
        target.mkdir()
        marker = b"-- precious --"
        (target / "nova.db").write_bytes(marker)

        def boom(path):
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(de, "_sha256_file", boom)
        out = de.apply_restore(
            result.archive_path,
            target_data_dir=target,
            confirm=True,
        )
        assert out.outcome == de.RESTORE_OUTCOME_BACKUP_FAILED
        # Existing data untouched even though the backup failed.
        assert (target / "nova.db").read_bytes() == marker
        # The warning identifies the unreadable file.
        assert any(
            "nova.db" in w for w in out.warnings
        ), out.warnings
        # No staging directory left behind.
        assert not (target / de.RESTORE_STAGING_DIRNAME).exists()

    def test_backup_never_overwrites_existing_file(
        self, configured_data_dir, tmp_path,
    ):
        result = de.create_data_export()
        target = tmp_path / "Target"
        target.mkdir()
        (target / "nova.db").write_bytes(b"v1")
        out1 = de.apply_restore(
            result.archive_path,
            target_data_dir=target,
            confirm=True,
        )
        assert out1.outcome == de.RESTORE_OUTCOME_RESTORED
        # Now run the restore again — the previous backup file should
        # still exist and a *new* backup file should be created with a
        # distinct name.
        (target / "nova.db").write_bytes(b"v2")
        out2 = de.apply_restore(
            result.archive_path,
            target_data_dir=target,
            confirm=True,
        )
        assert out2.outcome == de.RESTORE_OUTCOME_RESTORED
        assert out2.backup_path != out1.backup_path
        assert Path(out1.backup_path).is_file()
        assert Path(out2.backup_path).is_file()

    def test_pre_restore_backup_excludes_earlier_pre_restore_archives(
        self, configured_data_dir, tmp_path,
    ):
        """A new pre-restore backup must not contain earlier ones.

        Without filtering, every restore would walk the entire
        ``backups/`` tree — including the previous restore's
        pre-restore archive — and pack it into the new backup.
        Repeated restores would therefore stack "backups of
        backups" and grow on each run, eventually triggering
        spurious ``backup_failed`` outcomes from disk pressure.
        The filter in ``_create_pre_restore_backup`` excludes the
        ``backups/pre-restore/`` subtree from its own walk so each
        backup stays roughly the size of the live data dir.
        """
        result = de.create_data_export()
        target = tmp_path / "Target"
        target.mkdir()
        (target / "nova.db").write_bytes(b"first")

        out1 = de.apply_restore(
            result.archive_path,
            target_data_dir=target,
            confirm=True,
        )
        assert out1.outcome == de.RESTORE_OUTCOME_RESTORED
        backup1 = Path(out1.backup_path)
        assert backup1.is_file()
        size1 = backup1.stat().st_size

        # Run a second restore so the backup builder has a real
        # ``backups/pre-restore/`` subtree to walk past.
        (target / "nova.db").write_bytes(b"second")
        out2 = de.apply_restore(
            result.archive_path,
            target_data_dir=target,
            confirm=True,
        )
        assert out2.outcome == de.RESTORE_OUTCOME_RESTORED
        backup2 = Path(out2.backup_path)
        assert backup2.is_file()

        # The second backup must not contain any pre-restore entry
        # — otherwise we'd be packing backup1 inside backup2.
        with tarfile.open(backup2, mode="r:*") as tar:
            names = set(tar.getnames())
        nested = [
            n for n in names
            if "pre-restore" in n or "nova-pre-restore" in n
        ]
        assert nested == [], (
            "pre-restore archives leaked into the new backup: "
            f"{nested}"
        )
        # And the second backup is the same order of magnitude as
        # the first (not exponentially larger).
        size2 = backup2.stat().st_size
        assert size2 < size1 * 5

    def test_target_with_only_pre_restore_backups_skips_backup_step(
        self, configured_data_dir, tmp_path,
    ):
        """A target whose only canonical content is previous
        pre-restore archives must not retrigger a backup attempt.

        Without aligning ``_target_has_canonical_data`` with the
        new backup filter, a target that's just been wiped except
        for ``backups/pre-restore/`` would still be flagged as
        "has canonical data", causing the next restore to try
        backing up an effectively-empty set and either looping
        the backups-of-backups problem or refusing the restore
        spuriously.
        """
        result = de.create_data_export()
        target = tmp_path / "Target"
        target.mkdir()
        # Seed an "old" pre-restore backup as the only content.
        pre_dir = target / "backups" / de.PRE_RESTORE_BACKUP_SUBDIR
        pre_dir.mkdir(parents=True)
        (pre_dir / "old-pre-restore.tar.gz").write_bytes(b"opaque blob")
        # No nova.db, no other canonical files.
        assert not (target / "nova.db").exists()

        out = de.apply_restore(
            result.archive_path,
            target_data_dir=target,
            confirm=True,
        )
        # Restore proceeds, no backup step (nothing canonical to
        # back up), nova.db lands as expected.
        assert out.outcome == de.RESTORE_OUTCOME_RESTORED
        assert out.backup_path == ""
        assert (target / "nova.db").is_file()
        # The old pre-restore archive is left alone.
        assert (pre_dir / "old-pre-restore.tar.gz").is_file()

    def test_partial_copy_failure_rolls_back_earlier_files(
        self, configured_data_dir, tmp_path, monkeypatch,
    ):
        """A mid-run copy failure must roll back every earlier replacement.

        Without rollback, the per-file ``os.replace`` loop would
        leave the target in a mixed state (files 1..N-1 from the
        archive, file N original, files N+1.. original). The
        rollback path moves the stashed originals back into place
        so the target stays bit-for-bit identical to its pre-restore
        state when any file fails to copy.
        """
        result = de.create_data_export()
        target = tmp_path / "Target"
        target.mkdir()
        # Seed the target with two distinct canonical files so we
        # can verify both survive a partial-failure rollback.
        marker_db = b"-- original nova.db --"
        marker_pack = b'{"old": "pack"}'
        (target / "nova.db").write_bytes(marker_db)
        (target / "memory-packs").mkdir()
        (target / "memory-packs" / "trip-2026.json").write_bytes(marker_pack)

        # Fail the third os.replace call. The first two are:
        #   1. stash existing nova.db → .replaced-originals/nova.db
        #   2. move staged nova.db → nova.db (commits file 1)
        # The third is then either stash for file 2 or its copy. By
        # the time it fires at least one file has been "committed"
        # so rollback has work to do.
        original_replace = os.replace
        call_count = {"n": 0}

        def flaky_replace(src, dst):
            call_count["n"] += 1
            if call_count["n"] == 3:
                raise OSError("synthetic mid-run failure")
            return original_replace(src, dst)

        monkeypatch.setattr(de.os, "replace", flaky_replace)
        out = de.apply_restore(
            result.archive_path,
            target_data_dir=target,
            confirm=True,
        )
        monkeypatch.setattr(de.os, "replace", original_replace)
        assert out.outcome == de.RESTORE_OUTCOME_FAILED
        # Both target files are bit-for-bit identical to the original.
        assert (target / "nova.db").read_bytes() == marker_db
        assert (
            (target / "memory-packs" / "trip-2026.json").read_bytes()
            == marker_pack
        )
        # The failure path reports no restored files (rollback).
        assert out.restored_files == ()
        assert out.conflicts == ()
        # Staging cleaned up.
        assert not (target / de.RESTORE_STAGING_DIRNAME).exists()

    def test_failed_restore_removes_newly_created_parent_dirs(
        self, tmp_path, monkeypatch,
    ):
        """A failed restore must clean up parent directories it created.

        The previous rollback unwound the file moves but left
        empty parent directories behind (the ones materialised by
        ``dst.parent.mkdir(parents=True)``), so a failed restore
        still mutated the target tree. The fix tracks newly-
        created parents per commit and rmdirs them in
        deepest-first order — only empty directories are removed,
        so a parent that picked up another committed file in the
        same restore is left alone.
        """
        archive = _make_archive_with_payload(
            tmp_path,
            {
                "memory-packs/trip-2026.json": b'{"ok": true}',
                "logs/import.log": b"loaded\n",
            },
        )
        target = tmp_path / "Target"
        target.mkdir()
        # No pre-existing memory-packs or logs dir — those will
        # be created by the restore.

        original_replace = os.replace
        call_count = {"n": 0}

        def flaky_replace(src, dst):
            call_count["n"] += 1
            # Let the first file commit (one os.replace call for a
            # fresh, non-existing destination). Fail on the second
            # file's commit attempt.
            if call_count["n"] == 2:
                raise OSError("synthetic mid-run failure")
            return original_replace(src, dst)

        monkeypatch.setattr(de.os, "replace", flaky_replace)
        out = de.apply_restore(
            archive, target_data_dir=target, confirm=True,
        )
        monkeypatch.setattr(de.os, "replace", original_replace)
        assert out.outcome == de.RESTORE_OUTCOME_FAILED, (
            out.outcome, out.refuse_reason
        )
        # Both the first file *and* its newly-created parent dir
        # are gone — the target is bit-for-bit back to its
        # pre-restore state.
        assert not (target / "memory-packs").exists()
        assert not (target / "logs").exists()
        # No staging directory left behind.
        assert not (target / de.RESTORE_STAGING_DIRNAME).exists()
        # And the target itself contains nothing.
        assert list(target.iterdir()) == []

    def test_failed_restore_preserves_pre_existing_parent_dirs(
        self, tmp_path, monkeypatch,
    ):
        """Rollback must not delete parent dirs the target already had.

        If ``memory-packs/`` existed before the restore (with
        unrelated content), a rollback must leave that directory
        and its contents in place — only directories created **by
        this restore call** should be removed.
        """
        archive = _make_archive_with_payload(
            tmp_path,
            {
                "memory-packs/trip-2026.json": b'{"ok": true}',
                "logs/import.log": b"loaded\n",
            },
        )
        target = tmp_path / "Target"
        target.mkdir()
        # Pre-existing memory-packs/ with an unrelated (allow-listed)
        # file inside.
        (target / "memory-packs").mkdir()
        (target / "memory-packs" / "earlier.json").write_text(
            '{"keep": true}', encoding="utf-8"
        )

        original_replace = os.replace
        call_count = {"n": 0}

        def flaky_replace(src, dst):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise OSError("synthetic mid-run failure")
            return original_replace(src, dst)

        monkeypatch.setattr(de.os, "replace", flaky_replace)
        out = de.apply_restore(
            archive, target_data_dir=target, confirm=True,
        )
        monkeypatch.setattr(de.os, "replace", original_replace)
        assert out.outcome == de.RESTORE_OUTCOME_FAILED
        # The pre-existing directory and its content survived.
        assert (target / "memory-packs").is_dir()
        assert (
            (target / "memory-packs" / "earlier.json").read_text(encoding="utf-8")
            == '{"keep": true}'
        )
        # No staging directory left behind.
        assert not (target / de.RESTORE_STAGING_DIRNAME).exists()

    def test_refuses_to_replace_directory_at_target(
        self, configured_data_dir, tmp_path,
    ):
        """A target directory where the archive has a regular file
        must refuse the restore upfront — never stash the directory.

        ``os.replace`` happily moves a directory into the stash, but
        the rollback path cannot put a directory back over a regular
        file (``os.replace(dir, file)`` raises) and the staging
        cleanup would then permanently delete the stashed
        directory. The preview-loop preflight catches this before
        any backup or staging work happens — the outcome is
        ``REFUSED`` (we never started), not ``FAILED`` (we tried
        and stopped mid-way), and the target is byte-for-byte
        untouched.
        """
        result = de.create_data_export()
        target = tmp_path / "Target"
        target.mkdir()
        # Plant a directory at the path where nova.db should land.
        weird_dir = target / "nova.db"
        weird_dir.mkdir()
        weird_payload = weird_dir / "inside.txt"
        weird_payload.write_text("operator data", encoding="utf-8")

        out = de.apply_restore(
            result.archive_path,
            target_data_dir=target,
            confirm=True,
        )
        assert out.outcome == de.RESTORE_OUTCOME_REFUSED
        assert "regular file" in out.refuse_reason.lower()
        # The weird directory and its contents are untouched.
        assert weird_dir.is_dir()
        assert weird_payload.read_text(encoding="utf-8") == "operator data"
        # No staging directory or backup work happened.
        assert not (target / de.RESTORE_STAGING_DIRNAME).exists()
        assert out.backup_path == ""

    def test_refuses_to_replace_symlink_at_target(
        self, configured_data_dir, tmp_path,
    ):
        """A symlink at the target path must refuse the restore.

        ``os.replace`` on top of a symlink replaces the symlink
        itself, not the file it points at — which would silently
        relink whatever lives at the link target. The preview-loop
        preflight refuses upfront so the outcome is ``REFUSED``
        (no backup, no staging, no copy) rather than ``FAILED``.

        The symlink resolves *inside* the target so the earlier
        containment check in ``apply_restore`` passes — this test
        pins the preview-loop type-gate that mirrors
        ``_copy_into_target``'s own check.
        """
        result = de.create_data_export()
        target = tmp_path / "Target"
        target.mkdir()
        real = target / "actual-content.txt"
        real.write_text("inside content", encoding="utf-8")
        link = target / "nova.db"
        try:
            link.symlink_to(real)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation unsupported on this host")

        out = de.apply_restore(
            result.archive_path,
            target_data_dir=target,
            confirm=True,
        )
        assert out.outcome == de.RESTORE_OUTCOME_REFUSED
        assert "symlink" in out.refuse_reason.lower()
        # No backup work — preview loop refused before that step.
        assert out.backup_path == ""
        # Link and its target are both intact.
        assert link.is_symlink()
        assert real.read_text(encoding="utf-8") == "inside content"
        # Staging cleaned up.
        assert not (target / de.RESTORE_STAGING_DIRNAME).exists()


class TestApplyRestoreExcludedContent:
    """Hostile-extra entries in the archive are skipped, not extracted."""

    def test_secret_files_in_archive_are_skipped(self, tmp_path):
        archive = tmp_path / "evil.tar.gz"
        manifest = json.dumps({
            "format": de.FORMAT_ID,
            "format_version": de.FORMAT_VERSION,
            "mode": de.MODE_DATA_ONLY,
            "files": [],
            "excluded": [],
        }).encode()
        with tarfile.open(archive, mode="w:gz") as tar:
            mi = tarfile.TarInfo(name=de.ARCHIVE_MANIFEST_NAME)
            mi.size = len(manifest)
            mi.mtime = 0
            tar.addfile(mi, io.BytesIO(manifest))
            # A regular nova.db entry — should be restored.
            payload = b"db"
            di = tarfile.TarInfo(name="data/nova.db")
            di.size = len(payload)
            di.mtime = 0
            tar.addfile(di, io.BytesIO(payload))
        target = tmp_path / "Target"
        target.mkdir()
        out = de.apply_restore(
            archive, target_data_dir=target, confirm=True,
        )
        assert out.outcome == de.RESTORE_OUTCOME_RESTORED
        assert (target / "nova.db").is_file()
        # No surprise files at the target root.
        files = sorted(
            p.name for p in target.iterdir() if p.is_file()
        )
        assert files == ["nova.db"]

    def test_restore_refuses_to_materialise_dot_env(self, tmp_path):
        """A crafted archive cannot smuggle in ``data/.env``.

        The export builder refuses to *pack* a ``.env`` file. The
        restore path must mirror that allowlist — otherwise a
        crafted archive could write secrets into ``NOVA_DATA_DIR``
        that the exporter would never have produced.
        """
        archive = _make_archive_with_payload(
            tmp_path,
            {
                "nova.db": b"-- db --",
                ".env": b"SECRET_KEY=topsecret\n",
            },
        )
        target = tmp_path / "Target"
        target.mkdir()
        out = de.apply_restore(
            archive, target_data_dir=target, confirm=True,
        )
        assert out.outcome == de.RESTORE_OUTCOME_RESTORED
        # nova.db lands fine.
        assert (target / "nova.db").is_file()
        # The .env is never created.
        assert not (target / ".env").exists()
        # And it is surfaced as skipped with the secret reason.
        skipped_paths = {e.path: e.reason for e in out.skipped_files}
        assert ".env" in skipped_paths
        assert skipped_paths[".env"] == de.REASON_SECRET

    def test_restore_refuses_ssh_key_inside_archive(self, tmp_path):
        archive = _make_archive_with_payload(
            tmp_path,
            {
                "nova.db": b"-- db --",
                ".ssh/id_rsa": b"PRIVATE KEY\n",
            },
        )
        target = tmp_path / "Target"
        target.mkdir()
        out = de.apply_restore(
            archive, target_data_dir=target, confirm=True,
        )
        assert out.outcome == de.RESTORE_OUTCOME_RESTORED
        assert not (target / ".ssh").exists()
        assert not (target / ".ssh" / "id_rsa").exists()
        skipped_paths = {e.path: e.reason for e in out.skipped_files}
        assert ".ssh/id_rsa" in skipped_paths
        assert skipped_paths[".ssh/id_rsa"] == de.REASON_SECRET

    def test_restore_refuses_non_canonical_top_level_file(self, tmp_path):
        """A bare ``data/README.txt`` is not in the allowlist."""
        archive = _make_archive_with_payload(
            tmp_path,
            {
                "nova.db": b"-- db --",
                "README.txt": b"Surprise!\n",
            },
        )
        target = tmp_path / "Target"
        target.mkdir()
        out = de.apply_restore(
            archive, target_data_dir=target, confirm=True,
        )
        assert out.outcome == de.RESTORE_OUTCOME_RESTORED
        assert not (target / "README.txt").exists()
        skipped_paths = {e.path: e.reason for e in out.skipped_files}
        assert "README.txt" in skipped_paths
        assert skipped_paths["README.txt"] == de.REASON_NOT_ALLOWLISTED

    def test_restore_refuses_non_canonical_subdir(self, tmp_path):
        """Anything outside the four reserved subdirs is refused."""
        archive = _make_archive_with_payload(
            tmp_path,
            {
                "nova.db": b"-- db --",
                "notabackup/foo.txt": b"leaked\n",
            },
        )
        target = tmp_path / "Target"
        target.mkdir()
        out = de.apply_restore(
            archive, target_data_dir=target, confirm=True,
        )
        assert out.outcome == de.RESTORE_OUTCOME_RESTORED
        assert not (target / "notabackup").exists()
        skipped_paths = {e.path: e.reason for e in out.skipped_files}
        assert "notabackup/foo.txt" in skipped_paths
        assert skipped_paths["notabackup/foo.txt"] == de.REASON_NOT_ALLOWLISTED

    def test_restore_refuses_ollama_gguf_blob(self, tmp_path):
        """Ollama model files inside ``backups/`` are refused."""
        archive = _make_archive_with_payload(
            tmp_path,
            {
                "nova.db": b"-- db --",
                "backups/llama-7b.gguf": b"weights\n",
            },
        )
        target = tmp_path / "Target"
        target.mkdir()
        out = de.apply_restore(
            archive, target_data_dir=target, confirm=True,
        )
        assert out.outcome == de.RESTORE_OUTCOME_RESTORED
        assert not (target / "backups" / "llama-7b.gguf").exists()
        skipped_paths = {e.path: e.reason for e in out.skipped_files}
        assert "backups/llama-7b.gguf" in skipped_paths
        assert skipped_paths["backups/llama-7b.gguf"] == de.REASON_OLLAMA_MODEL

    def test_stray_excluded_files_do_not_trigger_backup_failed(
        self, tmp_path,
    ):
        """A target whose reserved subdir holds only excluded entries
        must not produce a spurious ``backup_failed`` outcome.

        ``_target_has_canonical_data`` used to flag any non-empty
        reserved subdirectory as "canonical data", but
        ``_create_pre_restore_backup`` filters through the same
        allowlist used by ``_walk_allowlisted``. A target like
        ``backups/.env`` (a stray secret left behind by tooling) is
        non-empty but contains nothing the backup builder would
        actually pack — flagging it as needing a backup would force
        the backup builder to produce an empty archive and the
        restore would refuse with ``outcome=backup_failed``. The
        detection now matches the allowlist, so this scenario
        succeeds as a normal restore.
        """
        # Build an archive with a real nova.db payload.
        archive = _make_archive_with_payload(
            tmp_path, {"nova.db": b"-- db --"},
        )
        # Seed the target with ONLY excluded entries — a stray
        # .env inside backups/ and a __pycache__ under logs/.
        target = tmp_path / "Target"
        target.mkdir()
        (target / "backups").mkdir()
        (target / "backups" / ".env").write_text("SECRET=x\n")
        (target / "logs").mkdir()
        (target / "logs" / "__pycache__").mkdir()
        (target / "logs" / "__pycache__" / "stale.pyc").write_bytes(b"\x00")

        out = de.apply_restore(
            archive, target_data_dir=target, confirm=True,
        )
        # Restore succeeds because the target has nothing canonical
        # to back up — no spurious backup_failed.
        assert out.outcome == de.RESTORE_OUTCOME_RESTORED, (
            out.outcome, out.refuse_reason
        )
        assert out.backup_path == ""
        # The new database lands at the target.
        assert (target / "nova.db").is_file()
        # The stray excluded files are still where they were —
        # restore never touches them.
        assert (target / "backups" / ".env").is_file()
        assert (target / "logs" / "__pycache__" / "stale.pyc").is_file()

    def test_dry_run_surfaces_allowlist_skips(self, tmp_path):
        """The dry-run preview lists disallowed entries up front."""
        archive = _make_archive_with_payload(
            tmp_path,
            {
                "nova.db": b"-- db --",
                ".env": b"SECRET=x\n",
                "memory-packs/trip.json": b'{"ok": true}',
            },
        )
        target = tmp_path / "Target"
        target.mkdir()
        out = de.apply_restore(
            archive, target_data_dir=target, dry_run=True,
        )
        assert out.outcome == de.RESTORE_OUTCOME_DRY_RUN
        assert "nova.db" in out.restored_files
        assert "memory-packs/trip.json" in out.restored_files
        assert ".env" not in out.restored_files
        skipped_paths = {e.path: e.reason for e in out.skipped_files}
        assert ".env" in skipped_paths
        # Dry-run must never write anything.
        assert list(target.iterdir()) == []

    def test_duplicate_archive_members_are_deduped(self, tmp_path):
        """An archive with duplicate ``data/nova.db`` entries restores
        cleanly instead of hitting ENOENT on the second copy attempt.

        Tar files can carry duplicate member names; at extraction
        time the filesystem keeps the *last* write. The restore
        engine deduplicates ``extracted`` (and ``would_restore``)
        accordingly so dry-run and real-restore both report one
        entry per POSIX-relative path and the copy phase never
        tries to move the same staging file twice.
        """
        archive = tmp_path / "dup.tar.gz"
        manifest = json.dumps({
            "format": de.FORMAT_ID,
            "format_version": de.FORMAT_VERSION,
            "mode": de.MODE_DATA_ONLY,
            "files": [],
            "excluded": [],
            "created_at": "20260514T120000Z",
        }).encode()
        with tarfile.open(archive, mode="w:gz") as tar:
            mi = tarfile.TarInfo(name=de.ARCHIVE_MANIFEST_NAME)
            mi.size = len(manifest)
            mi.mtime = 0
            tar.addfile(mi, io.BytesIO(manifest))
            # Two entries with the same archive name. Last write wins.
            for payload in (b"-- v1 --", b"-- v2 --"):
                di = tarfile.TarInfo(name="data/nova.db")
                di.size = len(payload)
                di.mtime = 0
                tar.addfile(di, io.BytesIO(payload))

        target = tmp_path / "Target"
        target.mkdir()
        out = de.apply_restore(
            archive, target_data_dir=target, confirm=True,
        )
        assert out.outcome == de.RESTORE_OUTCOME_RESTORED, (
            out.outcome, out.refuse_reason
        )
        # Exactly one entry reported, despite the duplicate.
        assert out.restored_files == ("nova.db",)
        # The restored content is the last-written payload (tar
        # semantics: last entry with a given name wins).
        assert (target / "nova.db").read_bytes() == b"-- v2 --"

    def test_dry_run_dedupes_duplicate_archive_members(self, tmp_path):
        archive = tmp_path / "dup.tar.gz"
        manifest = json.dumps({
            "format": de.FORMAT_ID,
            "format_version": de.FORMAT_VERSION,
            "mode": de.MODE_DATA_ONLY,
            "files": [],
            "excluded": [],
            "created_at": "20260514T120000Z",
        }).encode()
        with tarfile.open(archive, mode="w:gz") as tar:
            mi = tarfile.TarInfo(name=de.ARCHIVE_MANIFEST_NAME)
            mi.size = len(manifest)
            mi.mtime = 0
            tar.addfile(mi, io.BytesIO(manifest))
            for payload in (b"-- a --", b"-- b --"):
                di = tarfile.TarInfo(name="data/nova.db")
                di.size = len(payload)
                di.mtime = 0
                tar.addfile(di, io.BytesIO(payload))
        target = tmp_path / "Target"
        target.mkdir()
        out = de.apply_restore(
            archive, target_data_dir=target, dry_run=True,
        )
        assert out.outcome == de.RESTORE_OUTCOME_DRY_RUN
        # Dry-run preview reports one entry, matching what the real
        # restore would produce.
        assert out.restored_files == ("nova.db",)


# ── CLI: restore subcommand ─────────────────────────────────────────


class TestRestoreCli:
    """The ``restore`` subcommand exposes apply_restore via the CLI."""

    def test_restore_requires_confirm_flag(
        self, configured_data_dir, capsys, tmp_path,
    ):
        result = de.create_data_export()
        target = tmp_path / "Target"
        target.mkdir()
        rc = de._cli([
            "restore", result.archive_path,
            "--data-dir", str(target),
        ])
        assert rc == 1
        err = capsys.readouterr().err
        assert "confirm" in err.lower()
        # Nothing was written.
        assert list(target.iterdir()) == []

    def test_restore_with_confirm_writes_data(
        self, configured_data_dir, capsys, tmp_path,
    ):
        result = de.create_data_export()
        target = tmp_path / "Target"
        target.mkdir()
        rc = de._cli([
            "restore", result.archive_path,
            "--data-dir", str(target),
            "--confirm",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "restored" in out.lower()
        assert (target / "nova.db").is_file()

    def test_restore_refusal_exits_nonzero(
        self, configured_data_dir, capsys, tmp_path,
    ):
        # Pin a manifest id that does not match → refusal.
        result = de.create_data_export()
        target = tmp_path / "Target"
        target.mkdir()
        rc = de._cli([
            "restore", result.archive_path,
            "--data-dir", str(target),
            "--confirm",
            "--confirmed-manifest-id", "not-the-right-id",
        ])
        assert rc == 1
        # Target was not touched.
        assert list(target.iterdir()) == []

    def test_unknown_command_still_prints_restore_in_usage(
        self, capsys,
    ):
        rc = de._cli([])
        assert rc == 2
        err = capsys.readouterr().err
        assert "restore" in err
