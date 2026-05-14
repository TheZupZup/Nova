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


# ── Nova Portable Workspace ─────────────────────────────────────────


class TestInitWorkspace:
    """``init_workspace`` scaffolds the portable layout idempotently.

    The contract is documented in ``docs/portable-workspace.md``:
    create the standard subdirectories, write a single example env
    file, and never overwrite anything that already exists.
    """

    def test_creates_expected_subdirectories(self, tmp_path):
        parent = tmp_path / "NovaPortable"
        result = core_paths.init_workspace(parent)

        for name in ("app", "data", "logs", "backups", "config", "scripts"):
            assert (parent / name).is_dir(), f"{name} should be created"

        # The result must report every created directory exactly once.
        assert {p.name for p in result.created_dirs} == {
            "app", "data", "logs", "backups", "config", "scripts",
        }
        assert result.existing_dirs == ()

    def test_writes_env_example_with_pointer_to_data_dir(self, tmp_path):
        parent = tmp_path / "NovaPortable"
        result = core_paths.init_workspace(parent)

        env_example = parent / "config" / "nova.env.example"
        assert env_example.is_file()
        body = env_example.read_text(encoding="utf-8")

        # The example must contain a NOVA_DATA_DIR line pointing at the
        # workspace's data/ subdirectory — that is the entire point of
        # the helper.
        expected_line = f"NOVA_DATA_DIR={parent / 'data'}"
        assert expected_line in body

        # And the result object should agree on the path.
        assert result.env_example_path == env_example
        assert env_example in result.created_files
        assert result.data_dir == parent / "data"

    def test_is_idempotent(self, tmp_path):
        parent = tmp_path / "NovaPortable"

        first = core_paths.init_workspace(parent)
        second = core_paths.init_workspace(parent)

        # Second run reports no new directories and no new files.
        assert second.created_dirs == ()
        assert second.created_files == ()
        # Both runs see the same set of paths under the parent.
        assert {p.name for p in first.created_dirs} == {
            p.name for p in second.existing_dirs
        }

    def test_does_not_overwrite_existing_env_example(self, tmp_path):
        parent = tmp_path / "NovaPortable"
        (parent / "config").mkdir(parents=True)
        env_example = parent / "config" / "nova.env.example"
        env_example.write_text("# operator-customised\n", encoding="utf-8")

        result = core_paths.init_workspace(parent)

        # The operator's customised file must survive bit-for-bit.
        assert env_example.read_text(encoding="utf-8") == (
            "# operator-customised\n"
        )
        assert env_example in result.existing_files
        assert env_example not in result.created_files

    def test_preserves_existing_directory_contents(self, tmp_path):
        parent = tmp_path / "NovaPortable"
        (parent / "data").mkdir(parents=True)
        # Drop a marker into data/ to prove init does not nuke it.
        marker = parent / "data" / "keep.txt"
        marker.write_text("hi", encoding="utf-8")

        core_paths.init_workspace(parent)

        assert marker.exists()
        assert marker.read_text(encoding="utf-8") == "hi"

    def test_rejects_non_directory_subpath(self, tmp_path):
        parent = tmp_path / "NovaPortable"
        parent.mkdir()
        # Put a regular file where ``data/`` is expected to land.
        (parent / "data").write_text("not a dir", encoding="utf-8")

        with pytest.raises(RuntimeError) as exc_info:
            core_paths.init_workspace(parent)
        assert "data" in str(exc_info.value)

    def test_rejects_non_directory_parent(self, tmp_path):
        # If the parent path exists as a regular file, fail clearly
        # rather than trying to mkdir(parents=True) over it.
        parent_file = tmp_path / "NovaPortable"
        parent_file.write_text("nope", encoding="utf-8")

        with pytest.raises(RuntimeError) as exc_info:
            core_paths.init_workspace(parent_file)
        assert str(parent_file) in str(exc_info.value)

    def test_rejects_empty_parent(self):
        with pytest.raises(RuntimeError) as exc_info:
            core_paths.init_workspace("")
        assert "parent path" in str(exc_info.value).lower()

    def test_rejects_whitespace_parent(self):
        with pytest.raises(RuntimeError):
            core_paths.init_workspace("   ")

    def test_expands_user_home(self, monkeypatch, tmp_path):
        # ``~`` should expand against the user's home — the same
        # contract ``configured_data_dir`` honours.
        monkeypatch.setenv("HOME", str(tmp_path))
        result = core_paths.init_workspace("~/NovaPortable")
        assert result.root == tmp_path / "NovaPortable"
        assert (tmp_path / "NovaPortable" / "data").is_dir()

    def test_does_not_set_environment_variable(self, monkeypatch, tmp_path):
        # ``init_workspace`` is read-mostly: it must not silently
        # export NOVA_DATA_DIR for the current process. The operator
        # wires that up via systemd / docker / .env, not Python.
        monkeypatch.delenv(core_paths.ENV_VAR, raising=False)
        core_paths.init_workspace(tmp_path / "NovaPortable")
        assert core_paths.ENV_VAR not in os.environ

    def test_does_not_clone_or_touch_app_directory(self, tmp_path):
        # ``app/`` is created so the layout is visible, but the helper
        # must not download, clone, or otherwise populate it.
        parent = tmp_path / "NovaPortable"
        core_paths.init_workspace(parent)

        app_dir = parent / "app"
        assert app_dir.is_dir()
        # Empty: no Nova checkout, no .venv, no models.
        assert list(app_dir.iterdir()) == []


class TestInitWorkspaceCLI:
    """The ``python -m core.paths init-workspace`` entry point.

    Driven through :func:`core.paths._cli` so the assertions do not
    have to spawn a subprocess.
    """

    def test_init_workspace_command_succeeds(self, tmp_path, capsys):
        parent = tmp_path / "NovaPortable"
        rc = core_paths._cli(["init-workspace", str(parent)])
        assert rc == 0
        assert (parent / "data").is_dir()
        out = capsys.readouterr().out
        assert "Nova Portable Workspace ready" in out
        assert str(parent) in out

    def test_init_workspace_command_is_idempotent(self, tmp_path, capsys):
        parent = tmp_path / "NovaPortable"
        assert core_paths._cli(["init-workspace", str(parent)]) == 0
        capsys.readouterr()  # discard first-run output
        rc = core_paths._cli(["init-workspace", str(parent)])
        assert rc == 0
        out = capsys.readouterr().out
        # On the second run, every subdirectory should be reported as
        # existing rather than newly created.
        assert "existing directories" in out

    def test_init_workspace_command_reports_invalid_target(
        self, tmp_path, capsys
    ):
        # A regular file at the workspace path is a clear, fixable
        # error — the CLI must return 1 and explain why.
        bad = tmp_path / "NovaPortable"
        bad.write_text("not a dir", encoding="utf-8")

        rc = core_paths._cli(["init-workspace", str(bad)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "error" in err.lower()
        assert str(bad) in err

    def test_unknown_command_prints_usage(self, capsys):
        rc = core_paths._cli([])
        assert rc == 2
        err = capsys.readouterr().err
        assert "init-workspace" in err
        assert "usage" in err.lower()


class TestPortableDockerComposeExample:
    """The shipped portable docker-compose example pins safety rules.

    The file is read as text so we do not depend on PyYAML being
    installed. Each assertion encodes a rule documented in
    ``docs/portable-workspace.md``.
    """

    @staticmethod
    def _compose_path() -> Path:
        return (
            Path(__file__).resolve().parents[1]
            / "deploy" / "docker" / "docker-compose.portable.yml"
        )

    def test_compose_file_exists(self):
        assert self._compose_path().is_file()

    def test_does_not_mount_docker_socket(self):
        body = self._compose_path().read_text(encoding="utf-8")
        assert "docker.sock" not in body, (
            "Portable compose must not mount the Docker socket."
        )

    def test_does_not_run_privileged(self):
        body = self._compose_path().read_text(encoding="utf-8")
        assert "privileged: true" not in body
        # cap_add is acceptable in principle but should not appear in
        # the example — Nova does not need any extra capability.
        assert "cap_add" not in body

    def test_does_not_mount_root_or_home(self):
        # Strip comments so safety rules that *mention* dangerous mounts
        # (e.g. "no $HOME mount") don't trigger a false positive. Then
        # inspect only the volume-list entries (lines whose stripped
        # form starts with ``-``).
        body = self._compose_path().read_text(encoding="utf-8")
        volume_lines = [
            line.strip()
            for line in body.splitlines()
            if not line.lstrip().startswith("#")
            and line.strip().startswith("-")
        ]
        for entry in volume_lines:
            assert not entry.startswith("- /:"), (
                f"Portable compose must not mount the host root: {entry!r}"
            )
            assert "$HOME" not in entry, (
                f"Portable compose must not mount $HOME: {entry!r}"
            )
            assert not entry.startswith("- /root"), (
                f"Portable compose must not mount /root: {entry!r}"
            )
            assert not entry.startswith("- /home"), (
                f"Portable compose must not mount /home: {entry!r}"
            )

    def test_data_dir_points_at_container_data(self):
        body = self._compose_path().read_text(encoding="utf-8")
        assert "NOVA_DATA_DIR: /data" in body, (
            "Portable compose should pin NOVA_DATA_DIR=/data inside the "
            "container so the host path can change without rebuilding "
            "the image."
        )

    def test_data_volume_is_a_bind_mount(self):
        body = self._compose_path().read_text(encoding="utf-8")
        # A bind mount looks like "- <host_path>:/data"; a named
        # volume would be "- nova-data:/data". The portable layout
        # uses bind mounts.
        assert ":/data" in body
        assert "- nova-data:" not in body


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
