"""
Tests for the Dev Workspace foundation — Phase 1 (read-only).

Covers:
  * allowed-root resolution from ``NOVA_DEV_WORKSPACE_ROOTS``
  * hard path validation (existence, ``.git``, containment, denylist,
    top-level / ``..`` / ``~`` refusal, symlink-escape refusal)
  * the module safety contract (no ``shell=True``, no privilege
    escalation, no ``os.system``; the git argv allowlist is read-only
    and refuses anything outside it)
  * the read-only git helpers against a real throwaway repo
  * the ``RepoStatus`` snapshot states
  * the projects ``local_repo_path`` migration + user-scoped setter
  * the read-only HTTP endpoints (link / unlink / status)

These tests never modify a repo: the only repository written to is a
disposable one created under ``tmp_path`` purely so the read helpers
have something real to observe.
"""

from __future__ import annotations

import ast
import contextlib
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core import dev_workspace as dw
from core import memory as core_memory, projects as core_projects, users
from memory import store as natural_store

_GIT = shutil.which("git")
_needs_git = pytest.mark.skipif(_GIT is None, reason="git not on PATH")


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "nova.db")
    monkeypatch.setattr(core_memory, "DB_PATH", path)
    monkeypatch.setattr(natural_store, "DB_PATH", path)
    core_memory.initialize_db()
    return path


def _make_user(db_path, username, password="pw"):
    with sqlite3.connect(db_path) as conn:
        return users.create_user(conn, username, password)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [_GIT, *args],
        cwd=str(repo),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


@pytest.fixture
def real_repo(tmp_path):
    """A real, committed git repo under an allowed root.

    Returns ``(repo_path, allowed_root)``. The caller points
    ``NOVA_DEV_WORKSPACE_ROOTS`` at ``allowed_root`` (or passes it
    explicitly via ``roots=``).
    """
    root = tmp_path / "workspace"
    repo = root / "demo"
    repo.mkdir(parents=True)
    if _GIT is not None:
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "t@example.com")
        _git(repo, "config", "user.name", "Test")
        # Disposable repo: never sign throwaway test commits (some
        # hosts enforce commit signing globally). This only affects
        # the test scaffold — Nova's helpers never commit.
        _git(repo, "config", "commit.gpgsign", "false")
        _git(repo, "config", "tag.gpgsign", "false")
        (repo / "README.md").write_text("hello\n", encoding="utf-8")
        _git(repo, "add", "README.md")
        _git(repo, "commit", "-q", "-m", "initial commit")
    else:
        # Still create the marker so path-validation tests (which do
        # not need git) work even on a git-less host.
        (repo / ".git").mkdir()
    return repo, root


# ── Allowed-root resolution ─────────────────────────────────────────


class TestConfiguredRoots:
    def test_unset_means_feature_off(self, monkeypatch):
        monkeypatch.delenv(dw.ENV_ROOTS, raising=False)
        assert dw.configured_roots() == ()
        assert dw.feature_enabled() is False

    def test_blank_is_off(self, monkeypatch):
        monkeypatch.setenv(dw.ENV_ROOTS, "   ")
        assert dw.configured_roots() == ()
        assert dw.feature_enabled() is False

    def test_comma_and_pathsep_separators(self, tmp_path, monkeypatch):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        monkeypatch.setenv(
            dw.ENV_ROOTS, f"{a}{os.pathsep}{b},{tmp_path / 'c'}"
        )
        roots = dw.configured_roots()
        assert a.resolve() in roots and b.resolve() in roots
        assert dw.feature_enabled() is True

    def test_relative_entries_dropped(self, monkeypatch):
        monkeypatch.setenv(dw.ENV_ROOTS, "relative/path,./also")
        assert dw.configured_roots() == ()

    def test_denied_or_top_level_roots_dropped(self, monkeypatch):
        monkeypatch.setenv(dw.ENV_ROOTS, f"/{os.pathsep}/home{os.pathsep}/mnt")
        assert dw.configured_roots() == ()
        assert dw.feature_enabled() is False


# ── Path validation ─────────────────────────────────────────────────


class TestValidateRepoPath:
    @pytest.mark.parametrize(
        "bad", [None, True, False, 123, b"x"]
    )
    def test_non_string_rejected(self, bad):
        with pytest.raises(dw.RepoPathError):
            dw.validate_repo_path(bad, roots=[Path("/tmp")])

    @pytest.mark.parametrize("bad", ["", "   ", "\t"])
    def test_empty_rejected(self, bad):
        with pytest.raises(dw.RepoPathError):
            dw.validate_repo_path(bad, roots=[Path("/tmp")])

    def test_relative_rejected(self):
        with pytest.raises(dw.RepoPathError):
            dw.validate_repo_path("relative/repo", roots=[Path("/")])

    def test_dotdot_rejected(self):
        with pytest.raises(dw.RepoPathError, match="'\\.\\.'"):
            dw.validate_repo_path("/srv/code/../../etc", roots=[Path("/srv")])

    def test_tilde_rejected(self):
        with pytest.raises(dw.RepoPathError):
            dw.validate_repo_path("~/code/nova", roots=[Path("/home")])

    @pytest.mark.parametrize("bad", ["/srv/a\x00b", "/srv/a\nb", "/srv/a\rb"])
    def test_control_chars_rejected(self, bad):
        with pytest.raises(dw.RepoPathError):
            dw.validate_repo_path(bad, roots=[Path("/srv")])

    def test_too_long_rejected(self):
        with pytest.raises(dw.RepoPathError):
            dw.validate_repo_path("/" + "a" * 5000, roots=[Path("/")])

    @pytest.mark.parametrize(
        "p", ["/", "/home", "/mnt", "/etc", "/usr", "/var", "/root"]
    )
    def test_denylisted_system_dirs_rejected(self, p):
        # Even with the broadest possible explicit root, a denied path
        # can never validate.
        with pytest.raises(dw.RepoPathError):
            dw.validate_repo_path(p, roots=[Path("/")])

    def test_top_level_dir_rejected(self):
        with pytest.raises(dw.RepoPathError, match="top-level"):
            dw.validate_repo_path("/nova_repo_xyz", roots=[Path("/")])

    def test_nonexistent_rejected(self, tmp_path):
        missing = tmp_path / "nope" / "repo"
        with pytest.raises(dw.RepoPathError):
            dw.validate_repo_path(str(missing), roots=[tmp_path])

    def test_not_a_dir_rejected(self, tmp_path):
        f = tmp_path / "file"
        f.write_text("x")
        with pytest.raises(dw.RepoPathError):
            dw.validate_repo_path(str(f), roots=[tmp_path])

    def test_missing_git_marker_rejected(self, tmp_path):
        d = tmp_path / "plain"
        d.mkdir()
        with pytest.raises(dw.RepoPathError, match="Git checkout"):
            dw.validate_repo_path(str(d), roots=[tmp_path])

    def test_no_roots_configured_rejected(self, real_repo, monkeypatch):
        repo, _ = real_repo
        monkeypatch.delenv(dw.ENV_ROOTS, raising=False)
        with pytest.raises(dw.RepoPathError, match="allowed workspace roots"):
            dw.validate_repo_path(str(repo))

    def test_outside_allowed_root_rejected(self, real_repo, tmp_path):
        repo, _ = real_repo
        other = tmp_path / "elsewhere"
        other.mkdir()
        with pytest.raises(dw.RepoPathError, match="outside"):
            dw.validate_repo_path(str(repo), roots=[other])

    def test_happy_path_returns_resolved(self, real_repo):
        repo, root = real_repo
        resolved = dw.validate_repo_path(str(repo), roots=[root])
        assert resolved == repo.resolve()
        assert resolved.is_absolute()

    def test_env_driven_happy_path(self, real_repo, monkeypatch):
        repo, root = real_repo
        monkeypatch.setenv(dw.ENV_ROOTS, str(root))
        assert dw.is_valid_repo_path(str(repo)) is True

    def test_symlink_escape_rejected(self, real_repo, tmp_path):
        """A symlink inside an allowed root that points outside it
        must resolve and be refused — containment is checked on the
        *resolved* path, so the link cannot smuggle an outside repo in.
        """
        repo, root = real_repo
        outside = tmp_path / "outside_repo"
        outside.mkdir()
        (outside / ".git").mkdir()
        link = root / "sneaky"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")
        with pytest.raises(dw.RepoPathError, match="outside"):
            dw.validate_repo_path(str(link), roots=[root])


# ── Module safety contract ──────────────────────────────────────────


class TestModuleSafetyContract:
    def test_no_shell_true_anywhere(self):
        tree = ast.parse(Path(dw.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords or []:
                    if kw.arg == "shell":
                        assert isinstance(kw.value, ast.Constant)
                        assert kw.value.value is False

    def test_no_privilege_escalation_strings(self):
        source = Path(dw.__file__).read_text(encoding="utf-8")
        for needle in (
            '"sudo"', "'sudo'", " sudo ",
            '"pkexec"', "'pkexec'",
            '"doas"', "'doas'",
            '"runuser"', "'runuser'",
        ):
            assert needle not in source

    def test_no_os_system_or_popen(self):
        tree = ast.parse(Path(dw.__file__).read_text(encoding="utf-8"))
        bad = {"system", "popen", "spawnl", "spawnv", "spawnlp", "spawnvp"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(
                node.value, ast.Name
            ):
                if node.value.id == "os" and node.attr in bad:
                    pytest.fail(f"must not call os.{node.attr}")

    def test_allowlist_is_read_only(self):
        # No write / network / history-rewriting subcommand may ever
        # appear in the allowlist.
        forbidden = {
            "push", "commit", "fetch", "pull", "clone", "remote",
            "checkout", "merge", "rebase", "reset", "add", "rm",
            "mv", "tag", "stash", "apply", "am", "cherry-pick",
            "revert", "gc", "prune", "fsck", "init", "config",
            "switch", "restore", "worktree", "submodule",
        }
        for argv in dw._ALLOWED_GIT_ARGV:
            assert argv[0] in {"status", "branch", "log", "diff"}
            assert not (set(argv) & forbidden)

    def test_run_git_refuses_non_allowlisted_argv(self, real_repo):
        repo, _ = real_repo
        with pytest.raises(ValueError, match="not allowlisted"):
            dw._run_git(["push", "origin", "main"], repo_path=str(repo))
        with pytest.raises(ValueError, match="not allowlisted"):
            dw._run_git(["status"], repo_path=str(repo))

    def test_repo_path_is_never_in_argv(self, real_repo, monkeypatch):
        """The repo path is only ever the cwd — never an argv element."""
        repo, _ = real_repo
        seen = {}

        class _R:
            returncode = 0
            stdout = b""
            stderr = b""

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen["cwd"] = kwargs.get("cwd")
            seen["shell"] = kwargs.get("shell")
            return _R()

        monkeypatch.setattr(dw.subprocess, "run", fake_run)
        dw._run_git(["status", "--short"], repo_path=str(repo))
        assert seen["cwd"] == str(repo)
        assert seen["shell"] is False
        assert str(repo) not in seen["argv"][1:]
        assert seen["argv"][1:] == ["status", "--short"]


# ── Read-only git helpers (real repo) ───────────────────────────────


@_needs_git
class TestGitHelpers:
    def test_branch_and_clean_after_commit(self, real_repo):
        repo, _ = real_repo
        assert dw.git_current_branch(str(repo)) in ("main", "master")
        assert dw.git_is_clean(str(repo)) is True
        assert dw.git_status_short(str(repo)) == ()

    def test_dirty_after_untracked_file(self, real_repo):
        repo, _ = real_repo
        (repo / "new.txt").write_text("data\n", encoding="utf-8")
        assert dw.git_is_clean(str(repo)) is False
        short = dw.git_status_short(str(repo))
        assert any("new.txt" in line for line in short)
        changed = dw.git_changed_files(str(repo))
        assert any(e["path"].endswith("new.txt") for e in changed)
        assert all("status" in e and "path" in e for e in changed)

    def test_diff_stat_after_modifying_tracked_file(self, real_repo):
        repo, _ = real_repo
        (repo / "README.md").write_text("hello\nworld\n", encoding="utf-8")
        diff = dw.git_diff_stat(str(repo))
        assert any("README.md" in line for line in diff)

    def test_log_oneline(self, real_repo):
        repo, _ = real_repo
        log = dw.git_log_oneline(str(repo))
        assert len(log) == 1
        assert "initial commit" in log[0]

    def test_log_capped_at_twenty(self, real_repo):
        repo, _ = real_repo
        for i in range(25):
            (repo / f"f{i}").write_text("x", encoding="utf-8")
            _git(repo, "add", f"f{i}")
            _git(repo, "commit", "-q", "-m", f"commit {i}")
        log = dw.git_log_oneline(str(repo))
        assert len(log) == dw._MAX_LOG_LINES == 20

    def test_read_status_ready_snapshot(self, real_repo):
        repo, root = real_repo
        snap = dw.read_status(str(repo), roots=[root])
        d = snap.as_dict()
        assert d["state"] == dw.STATE_READY
        assert d["repo_path"] == str(repo.resolve())
        assert d["clean"] is True
        assert d["branch"] in ("main", "master")
        assert isinstance(d["recent_commits"], list)
        assert d["recent_commits"]

    def test_read_status_reports_dirty(self, real_repo):
        repo, root = real_repo
        (repo / "wip.py").write_text("x = 1\n", encoding="utf-8")
        snap = dw.read_status(str(repo), roots=[root]).as_dict()
        assert snap["state"] == dw.STATE_READY
        assert snap["clean"] is False
        assert snap["detail"]


class TestReadStatusErrorStates:
    def test_disabled_when_no_roots(self, real_repo, monkeypatch):
        repo, _ = real_repo
        monkeypatch.delenv(dw.ENV_ROOTS, raising=False)
        snap = dw.read_status(str(repo)).as_dict()
        assert snap["state"] == dw.STATE_DISABLED

    def test_invalid_path_state(self, tmp_path):
        snap = dw.read_status(str(tmp_path / "gone"), roots=[tmp_path])
        assert snap.state == dw.STATE_INVALID_PATH

    def test_git_unavailable_state(self, real_repo, monkeypatch):
        repo, root = real_repo
        monkeypatch.setattr(dw, "_git_path", lambda: None)
        snap = dw.read_status(str(repo), roots=[root]).as_dict()
        assert snap["state"] == dw.STATE_GIT_UNAVAILABLE


# ── Projects data-layer integration ─────────────────────────────────


class TestProjectsRepoColumn:
    def test_column_added_and_idempotent(self, db_path):
        core_projects.migrate(db_path)
        core_projects.migrate(db_path)  # second run must be a no-op
        with sqlite3.connect(db_path) as conn:
            cols = {
                r[1]
                for r in conn.execute(
                    "PRAGMA table_info(projects)"
                ).fetchall()
            }
        assert "local_repo_path" in cols

    def test_new_project_has_no_repo(self, db_path):
        uid = _make_user(db_path, "alice")
        p = core_projects.create_project("Nova", uid, db_path=db_path)
        assert p["local_repo_path"] is None
        assert p["has_local_repo"] is False

    def test_set_and_get_repo_path(self, db_path, real_repo):
        repo, root = real_repo
        uid = _make_user(db_path, "alice")
        p = core_projects.create_project("Nova", uid, db_path=db_path)
        with patch.object(dw, "configured_roots", lambda: (root.resolve(),)):
            updated = core_projects.set_local_repo_path(
                p["id"], uid, str(repo), db_path=db_path
            )
        assert updated["local_repo_path"] == str(repo.resolve())
        assert updated["has_local_repo"] is True
        assert core_projects.get_local_repo_path(
            p["id"], uid, db_path=db_path
        ) == str(repo.resolve())

    def test_unlink_with_none(self, db_path, real_repo):
        repo, root = real_repo
        uid = _make_user(db_path, "alice")
        p = core_projects.create_project("Nova", uid, db_path=db_path)
        with patch.object(dw, "configured_roots", lambda: (root.resolve(),)):
            core_projects.set_local_repo_path(
                p["id"], uid, str(repo), db_path=db_path
            )
        cleared = core_projects.set_local_repo_path(
            p["id"], uid, None, db_path=db_path
        )
        assert cleared["local_repo_path"] is None
        assert cleared["has_local_repo"] is False

    def test_invalid_path_raises_project_error(self, db_path):
        uid = _make_user(db_path, "alice")
        p = core_projects.create_project("Nova", uid, db_path=db_path)
        with pytest.raises(core_projects.ProjectError):
            core_projects.set_local_repo_path(
                p["id"], uid, "/etc", db_path=db_path
            )

    def test_set_is_user_scoped(self, db_path, real_repo):
        repo, root = real_repo
        a = _make_user(db_path, "alice")
        b = _make_user(db_path, "bob")
        p = core_projects.create_project("Nova", a, db_path=db_path)
        with patch.object(dw, "configured_roots", lambda: (root.resolve(),)):
            # Bob cannot touch Alice's project — returns None (web → 404).
            assert core_projects.set_local_repo_path(
                p["id"], b, str(repo), db_path=db_path
            ) is None
        assert core_projects.get_local_repo_path(
            p["id"], b, db_path=db_path
        ) is None


# ── HTTP endpoints ──────────────────────────────────────────────────


@pytest.fixture
def web_client(db_path, monkeypatch):
    monkeypatch.setattr(core_memory, "DB_PATH", db_path)
    monkeypatch.setattr(natural_store, "DB_PATH", db_path)
    from core.rate_limiter import _login_limiter
    _login_limiter._store.clear()

    import web
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("web.initialize_db"))
        stack.enter_context(patch("web.learn_from_feeds"))
        stack.enter_context(patch("web.scheduler", MagicMock()))
        with TestClient(web.app, raise_server_exceptions=True) as client:
            yield client


def _login(client, username, password="pw"):
    resp = client.post(
        "/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _h(token):
    return {"Authorization": f"Bearer {token}"}


class TestRepoEndpoints:
    def test_status_unlinked_returns_linked_false(self, db_path, web_client):
        _make_user(db_path, "alice")
        tok = _login(web_client, "alice")
        pid = web_client.post(
            "/projects", json={"name": "Nova"}, headers=_h(tok)
        ).json()["id"]
        resp = web_client.get(
            f"/projects/{pid}/repo/status", headers=_h(tok)
        )
        assert resp.status_code == 200
        assert resp.json() == {"linked": False}

    def test_link_invalid_path_is_400(self, db_path, web_client):
        _make_user(db_path, "alice")
        tok = _login(web_client, "alice")
        pid = web_client.post(
            "/projects", json={"name": "Nova"}, headers=_h(tok)
        ).json()["id"]
        resp = web_client.put(
            f"/projects/{pid}/repo",
            json={"path": "/etc"},
            headers=_h(tok),
        )
        assert resp.status_code == 400
        assert "detail" in resp.json()

    def test_link_foreign_project_is_404(self, db_path, web_client):
        _make_user(db_path, "alice")
        _make_user(db_path, "bob")
        a = _login(web_client, "alice")
        b = _login(web_client, "bob")
        pid = web_client.post(
            "/projects", json={"name": "Priv"}, headers=_h(a)
        ).json()["id"]
        assert web_client.put(
            f"/projects/{pid}/repo",
            json={"path": None},
            headers=_h(b),
        ).status_code == 404
        assert web_client.get(
            f"/projects/{pid}/repo/status", headers=_h(b)
        ).status_code == 404

    def test_unlink_is_always_allowed(self, db_path, web_client):
        _make_user(db_path, "alice")
        tok = _login(web_client, "alice")
        pid = web_client.post(
            "/projects", json={"name": "Nova"}, headers=_h(tok)
        ).json()["id"]
        resp = web_client.put(
            f"/projects/{pid}/repo", json={"path": None}, headers=_h(tok)
        )
        assert resp.status_code == 200
        assert resp.json()["local_repo_path"] is None

    @_needs_git
    def test_full_link_then_status(
        self, db_path, web_client, real_repo, monkeypatch
    ):
        repo, root = real_repo
        monkeypatch.setenv(dw.ENV_ROOTS, str(root))
        _make_user(db_path, "alice")
        tok = _login(web_client, "alice")
        pid = web_client.post(
            "/projects", json={"name": "Nova"}, headers=_h(tok)
        ).json()["id"]

        linked = web_client.put(
            f"/projects/{pid}/repo",
            json={"path": str(repo)},
            headers=_h(tok),
        )
        assert linked.status_code == 200, linked.text
        assert linked.json()["local_repo_path"] == str(repo.resolve())

        status = web_client.get(
            f"/projects/{pid}/repo/status", headers=_h(tok)
        ).json()
        assert status["linked"] is True
        assert status["state"] == dw.STATE_READY
        assert status["branch"] in ("main", "master")
        assert status["clean"] is True
        assert status["recent_commits"]
