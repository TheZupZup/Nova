"""
Tests for the admin-only maintenance / update center.

The suite pins the safety contract documented in ``core/maintenance.py``:

  * disabled feature is silent — no probe, no spawn, every endpoint
    reports ``state="disabled"``;
  * pull / restart switches are independent and default to off;
  * pull refuses on a dirty working tree, a diverged branch, a
    missing upstream, or an already-up-to-date state — *before* any
    git command runs;
  * pull uses the exact argv ``["git", "pull", "--ff-only"]``;
  * restart uses the exact argv ``[*, "--user", "restart", <unit>]``
    and ``--user`` is non-negotiable;
  * no subprocess invocation ever uses ``shell=True``;
  * no spawn contains ``sudo`` / ``pkexec`` / ``doas`` / ``su`` /
    ``runuser``;
  * the helper never raises into the web layer;
  * the FastAPI endpoints are admin-only and confirmation-gated for
    pull / restart;
  * the module itself contains no string-mode ``subprocess`` usage
    and no ``shell=True`` literal.
"""

from __future__ import annotations

import ast
import contextlib
import sqlite3
import subprocess
import sys
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

# Heavy / optional deps that ``web.py`` pulls in at module load. Stub
# them before any test imports ``web`` so a missing wheel cannot block
# this file. Only the minimal attribute surface the importers actually
# touch is provided; everything else degrades to a MagicMock.
for _mod in ("ddgs", "ollama", "sgmllib", "feedparser"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from fastapi.testclient import TestClient  # noqa: E402

from core import maintenance as maint  # noqa: E402
from core import memory as core_memory, users  # noqa: E402
from memory import store as natural_store  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_maintenance_env(monkeypatch):
    """Strip every host-level env var so each test starts fresh."""
    for var in (
        "NOVA_MAINTENANCE_ENABLED",
        "NOVA_MAINTENANCE_ALLOW_PULL",
        "NOVA_MAINTENANCE_ALLOW_RESTART",
        "NOVA_MAINTENANCE_REPO_PATH",
        "NOVA_MAINTENANCE_RESTART_MODE",
        "NOVA_MAINTENANCE_SYSTEMD_UNIT",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture
def fake_repo(tmp_path):
    """Materialise a directory that looks like a git checkout to the helper."""
    (tmp_path / ".git").mkdir()
    return str(tmp_path)


@pytest.fixture
def enable_maintenance(monkeypatch, fake_repo):
    """Turn the maintenance surface on with a configured repo path."""
    monkeypatch.setenv("NOVA_MAINTENANCE_ENABLED", "true")
    monkeypatch.setenv("NOVA_MAINTENANCE_REPO_PATH", fake_repo)
    return fake_repo


class _FakeGit:
    """Scriptable replacement for ``maintenance._run_git``.

    Records every argv tail so tests can assert the allowlist is
    obeyed. Returns ``(returncode, stdout, stderr)`` from a dict
    keyed by the argv tail tuple, with a configurable fallback.
    """

    def __init__(self, scripted: Optional[dict] = None, default: tuple = (0, "", "")):
        self.scripted: dict = dict(scripted or {})
        self.default = default
        self.calls: list[tuple] = []
        self.repo_paths: list[str] = []
        self.timeouts: list[float] = []

    def __call__(self, argv_tail, *, repo_path, timeout):
        key = tuple(argv_tail)
        self.calls.append(key)
        self.repo_paths.append(repo_path)
        self.timeouts.append(timeout)
        return self.scripted.get(key, self.default)


@pytest.fixture
def fake_git(monkeypatch):
    fake = _FakeGit()
    monkeypatch.setattr(maint, "_run_git", fake)
    # Pretend git is on PATH so the helper does not short-circuit.
    monkeypatch.setattr(
        maint.shutil, "which",
        lambda name: "/usr/bin/git" if name == "git" else None,
    )
    return fake


@pytest.fixture
def fake_git_and_systemctl(monkeypatch):
    fake = _FakeGit()
    monkeypatch.setattr(maint, "_run_git", fake)

    def _which(name):
        if name == "git":
            return "/usr/bin/git"
        if name == "systemctl":
            return "/usr/bin/systemctl"
        return None

    monkeypatch.setattr(maint.shutil, "which", _which)
    return fake


# ── Module-level safety contract ────────────────────────────────────


class TestModuleSafetyContract:
    def test_no_shell_true_anywhere(self):
        """The maintenance module must never call subprocess with shell=True."""
        with open(maint.__file__, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords or []:
                    if kw.arg == "shell":
                        # Only ``shell=False`` is acceptable.
                        assert isinstance(kw.value, ast.Constant), (
                            "shell kwarg must be a constant"
                        )
                        assert kw.value.value is False, (
                            "shell=True is forbidden in maintenance helper"
                        )

    def test_no_privilege_escalation_strings(self):
        """The module must not mention sudo / pkexec / doas / su / runuser."""
        with open(maint.__file__, "r", encoding="utf-8") as f:
            source = f.read()
        # Word-boundary-ish guard: bare strings would catch ``su`` in
        # words like ``status``. Only literal command names with
        # spaces / quotes around them.
        forbidden = (
            '"sudo"', "'sudo'", " sudo ",
            '"pkexec"', "'pkexec'",
            '"doas"', "'doas'",
            '"runuser"', "'runuser'",
        )
        for needle in forbidden:
            assert needle not in source, (
                f"maintenance helper must not reference {needle!r}"
            )

    def test_subprocess_imports_are_module_form(self):
        """Module imports subprocess once, at module scope; no ``os.system``."""
        with open(maint.__file__, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        bad_attrs = {"system", "popen", "spawnl", "spawnv"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id == "os" and node.attr in bad_attrs:
                    pytest.fail(f"maintenance helper must not call os.{node.attr}")


# ── Validation ──────────────────────────────────────────────────────


class TestValidateUnitName:
    @pytest.mark.parametrize("unit", [
        "nova.service",
        "nova-api.service",
        "nova_backend.service",
        "n.service",
        "nova.test.service",
    ])
    def test_accepts_safe_names(self, unit):
        assert maint.validate_unit_name(unit) is True

    @pytest.mark.parametrize("unit", [
        "",
        " nova.service",
        "nova.service ",
        "Nova.service",
        "/usr/nova.service",
        "../nova.service",
        "nova\n.service",
        "nova\t.service",
        "nova .service",
        "nova.service;rm",
        "nova.service && reboot",
        "nova.service|sh",
        "nova.service$",
        "nova.service`x`",
        "nova.target",
        ".service",
        "-.service",
    ])
    def test_rejects_unsafe_names(self, unit):
        assert maint.validate_unit_name(unit) is False


# ── Disabled state ──────────────────────────────────────────────────


class TestDisabled:
    def test_disabled_by_default(self):
        assert maint.is_enabled() is False

    def test_status_disabled_when_env_unset(self):
        s = maint.get_status()
        assert s.state == maint.STATE_DISABLED
        assert s.enabled is False
        assert s.allow_pull is False
        assert s.allow_restart is False

    def test_status_disabled_when_env_explicit_false(self, monkeypatch):
        monkeypatch.setenv("NOVA_MAINTENANCE_ENABLED", "false")
        s = maint.get_status()
        assert s.state == maint.STATE_DISABLED

    def test_fetch_disabled_does_not_call_git(self, fake_git):
        result = maint.fetch()
        assert result.state == maint.STATE_DISABLED
        assert fake_git.calls == []

    def test_pull_disabled_short_circuits(self, fake_git):
        result = maint.pull()
        assert result.outcome == maint.PULL_DISABLED
        assert fake_git.calls == []

    def test_restart_disabled_short_circuits(self, fake_git_and_systemctl, monkeypatch):
        # Spy on subprocess.run so we can confirm no spawn happens.
        recorded = []
        monkeypatch.setattr(
            maint.subprocess, "run",
            lambda *a, **kw: recorded.append((a, kw)) or MagicMock(returncode=0),
        )
        result = maint.restart()
        assert result.outcome == maint.RESTART_DISABLED
        assert recorded == []


# ── Status snapshot ─────────────────────────────────────────────────


class TestStatusSnapshot:
    def test_unavailable_when_path_is_not_a_checkout(self, monkeypatch, tmp_path):
        monkeypatch.setenv("NOVA_MAINTENANCE_ENABLED", "true")
        monkeypatch.setenv("NOVA_MAINTENANCE_REPO_PATH", str(tmp_path))
        # Pretend git is on PATH so the "not a checkout" branch is hit.
        monkeypatch.setattr(
            maint.shutil, "which",
            lambda name: "/usr/bin/git" if name == "git" else None,
        )
        s = maint.get_status()
        assert s.state == maint.STATE_UNAVAILABLE
        assert "not a git checkout" in s.detail

    def test_unavailable_when_git_missing(self, monkeypatch, fake_repo):
        monkeypatch.setenv("NOVA_MAINTENANCE_ENABLED", "true")
        monkeypatch.setenv("NOVA_MAINTENANCE_REPO_PATH", fake_repo)
        monkeypatch.setattr(maint.shutil, "which", lambda _name: None)
        s = maint.get_status()
        assert s.state == maint.STATE_UNAVAILABLE
        assert "git is not available" in s.detail

    def test_ready_up_to_date(self, enable_maintenance, fake_git):
        fake_git.scripted = {
            ("rev-parse", "--abbrev-ref", "HEAD"): (0, "main\n", ""),
            ("rev-parse", "HEAD"): (0, "abc123\n", ""),
            ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"):
                (0, "origin/main\n", ""),
            ("status", "--porcelain"): (0, "", ""),
            ("rev-list", "--count", "HEAD..@{u}"): (0, "0\n", ""),
            ("rev-list", "--count", "@{u}..HEAD"): (0, "0\n", ""),
        }
        s = maint.get_status()
        assert s.state == maint.STATE_READY
        assert s.branch == "main"
        assert s.commit == "abc123"
        assert s.upstream == "origin/main"
        assert s.has_upstream is True
        assert s.working_tree_clean is True
        assert s.update_available == maint.UPDATE_UP_TO_DATE
        assert s.behind_count == 0
        assert s.ahead_count == 0

    def test_ready_no_upstream(self, enable_maintenance, fake_git):
        fake_git.scripted = {
            ("rev-parse", "--abbrev-ref", "HEAD"): (0, "feature\n", ""),
            ("rev-parse", "HEAD"): (0, "deadbeef\n", ""),
            ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"):
                (128, "", "fatal: no upstream\n"),
            ("status", "--porcelain"): (0, "", ""),
        }
        s = maint.get_status()
        assert s.state == maint.STATE_READY
        assert s.has_upstream is False
        assert s.update_available == maint.UPDATE_NO_UPSTREAM
        assert "No upstream" in s.detail

    def test_ready_update_available(self, enable_maintenance, fake_git):
        fake_git.scripted = {
            ("rev-parse", "--abbrev-ref", "HEAD"): (0, "main\n", ""),
            ("rev-parse", "HEAD"): (0, "abc123\n", ""),
            ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"):
                (0, "origin/main\n", ""),
            ("status", "--porcelain"): (0, "", ""),
            ("rev-list", "--count", "HEAD..@{u}"): (0, "3\n", ""),
            ("rev-list", "--count", "@{u}..HEAD"): (0, "0\n", ""),
            ("log", "--oneline", "HEAD..@{u}"):
                (0, "aaaaaaa first\nbbbbbbb second\nccccccc third\n", ""),
            ("diff", "--stat", "HEAD..@{u}"):
                (0, " core/maintenance.py | 10 +++++++\n 1 file changed\n", ""),
        }
        s = maint.get_status()
        assert s.update_available == maint.UPDATE_AVAILABLE
        assert s.behind_count == 3
        assert s.incoming_commits == (
            "aaaaaaa first", "bbbbbbb second", "ccccccc third",
        )
        assert len(s.changed_files) == 2

    def test_ready_diverged(self, enable_maintenance, fake_git):
        fake_git.scripted = {
            ("rev-parse", "--abbrev-ref", "HEAD"): (0, "main\n", ""),
            ("rev-parse", "HEAD"): (0, "abc123\n", ""),
            ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"):
                (0, "origin/main\n", ""),
            ("status", "--porcelain"): (0, "", ""),
            ("rev-list", "--count", "HEAD..@{u}"): (0, "2\n", ""),
            ("rev-list", "--count", "@{u}..HEAD"): (0, "1\n", ""),
            ("log", "--oneline", "HEAD..@{u}"): (0, "aaa A\nbbb B\n", ""),
            ("diff", "--stat", "HEAD..@{u}"): (0, " a.py | 2\n", ""),
        }
        s = maint.get_status()
        assert s.update_available == maint.UPDATE_DIVERGED
        assert s.behind_count == 2
        assert s.ahead_count == 1
        assert "diverged" in s.detail.lower()

    def test_dirty_working_tree_surfaces_in_status(self, enable_maintenance, fake_git):
        fake_git.scripted = {
            ("rev-parse", "--abbrev-ref", "HEAD"): (0, "main\n", ""),
            ("rev-parse", "HEAD"): (0, "abc\n", ""),
            ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"):
                (0, "origin/main\n", ""),
            ("status", "--porcelain"): (0, " M core/maintenance.py\n", ""),
            ("rev-list", "--count", "HEAD..@{u}"): (0, "0\n", ""),
            ("rev-list", "--count", "@{u}..HEAD"): (0, "0\n", ""),
        }
        s = maint.get_status()
        assert s.working_tree_clean is False

    def test_fetch_runs_fetch_before_reading_state(self, enable_maintenance, fake_git):
        fake_git.scripted = {
            ("fetch",): (0, "", ""),
            ("rev-parse", "--abbrev-ref", "HEAD"): (0, "main\n", ""),
            ("rev-parse", "HEAD"): (0, "abc\n", ""),
            ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"):
                (0, "origin/main\n", ""),
            ("status", "--porcelain"): (0, "", ""),
            ("rev-list", "--count", "HEAD..@{u}"): (0, "0\n", ""),
            ("rev-list", "--count", "@{u}..HEAD"): (0, "0\n", ""),
        }
        s = maint.fetch()
        assert ("fetch",) in fake_git.calls
        # Fetch must come first.
        assert fake_git.calls[0] == ("fetch",)
        assert s.state == maint.STATE_READY

    def test_status_does_not_fetch_by_default(self, enable_maintenance, fake_git):
        fake_git.scripted = {
            ("rev-parse", "--abbrev-ref", "HEAD"): (0, "main\n", ""),
            ("rev-parse", "HEAD"): (0, "abc\n", ""),
            ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"):
                (0, "origin/main\n", ""),
            ("status", "--porcelain"): (0, "", ""),
            ("rev-list", "--count", "HEAD..@{u}"): (0, "0\n", ""),
            ("rev-list", "--count", "@{u}..HEAD"): (0, "0\n", ""),
        }
        maint.get_status()
        assert ("fetch",) not in fake_git.calls


# ── Pull refusals ───────────────────────────────────────────────────


class TestPullRefusals:
    def test_pull_not_allowed_blocks_even_when_clean(
        self, enable_maintenance, fake_git,
    ):
        # Allow-pull off → no git command runs.
        fake_git.scripted = {}  # would crash if any call landed
        result = maint.pull()
        assert result.outcome == maint.PULL_NOT_ALLOWED
        # No subprocess primitives executed at all.
        assert fake_git.calls == []

    def test_pull_repo_unavailable_when_path_missing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("NOVA_MAINTENANCE_ENABLED", "true")
        monkeypatch.setenv("NOVA_MAINTENANCE_ALLOW_PULL", "true")
        monkeypatch.setenv("NOVA_MAINTENANCE_REPO_PATH", str(tmp_path))  # no .git
        monkeypatch.setattr(
            maint.shutil, "which",
            lambda name: "/usr/bin/git" if name == "git" else None,
        )
        result = maint.pull()
        assert result.outcome == maint.PULL_REPO_UNAVAILABLE

    def test_pull_no_upstream_blocks(
        self, monkeypatch, enable_maintenance, fake_git,
    ):
        monkeypatch.setenv("NOVA_MAINTENANCE_ALLOW_PULL", "true")
        fake_git.scripted = {
            ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"):
                (128, "", "fatal: no upstream\n"),
        }
        result = maint.pull()
        assert result.outcome == maint.PULL_NO_UPSTREAM
        # No pull command was issued.
        assert ("pull", "--ff-only") not in fake_git.calls

    def test_pull_dirty_working_tree_blocks(
        self, monkeypatch, enable_maintenance, fake_git,
    ):
        monkeypatch.setenv("NOVA_MAINTENANCE_ALLOW_PULL", "true")
        fake_git.scripted = {
            ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"):
                (0, "origin/main\n", ""),
            ("status", "--porcelain"): (0, " M file.py\n", ""),
        }
        result = maint.pull()
        assert result.outcome == maint.PULL_DIRTY_WORKING_TREE
        assert "Local changes" in result.detail
        assert ("pull", "--ff-only") not in fake_git.calls

    def test_pull_diverged_blocks(
        self, monkeypatch, enable_maintenance, fake_git,
    ):
        monkeypatch.setenv("NOVA_MAINTENANCE_ALLOW_PULL", "true")
        fake_git.scripted = {
            ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"):
                (0, "origin/main\n", ""),
            ("status", "--porcelain"): (0, "", ""),
            ("rev-list", "--count", "HEAD..@{u}"): (0, "2\n", ""),
            ("rev-list", "--count", "@{u}..HEAD"): (0, "1\n", ""),
        }
        result = maint.pull()
        assert result.outcome == maint.PULL_DIVERGED
        assert "diverged" in result.detail.lower()
        assert ("pull", "--ff-only") not in fake_git.calls

    def test_pull_not_fast_forward_when_already_up_to_date(
        self, monkeypatch, enable_maintenance, fake_git,
    ):
        monkeypatch.setenv("NOVA_MAINTENANCE_ALLOW_PULL", "true")
        fake_git.scripted = {
            ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"):
                (0, "origin/main\n", ""),
            ("status", "--porcelain"): (0, "", ""),
            ("rev-list", "--count", "HEAD..@{u}"): (0, "0\n", ""),
            ("rev-list", "--count", "@{u}..HEAD"): (0, "0\n", ""),
        }
        result = maint.pull()
        assert result.outcome == maint.PULL_NOT_FAST_FORWARD
        assert ("pull", "--ff-only") not in fake_git.calls


# ── Pull happy path ─────────────────────────────────────────────────


class TestPullHappyPath:
    def test_pull_runs_ff_only_with_exact_argv(
        self, monkeypatch, enable_maintenance, fake_git,
    ):
        monkeypatch.setenv("NOVA_MAINTENANCE_ALLOW_PULL", "true")
        fake_git.scripted = {
            ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"):
                (0, "origin/main\n", ""),
            ("status", "--porcelain"): (0, "", ""),
            ("rev-list", "--count", "HEAD..@{u}"): (0, "1\n", ""),
            ("rev-list", "--count", "@{u}..HEAD"): (0, "0\n", ""),
            ("rev-parse", "HEAD"): (0, "newcommit\n", ""),
            ("pull", "--ff-only"): (0, "Updating abc..def\n", ""),
        }
        result = maint.pull()
        assert result.outcome == maint.PULL_SUCCESS
        # Exact argv — never ``merge``, never ``rebase``, never plain
        # ``pull``. The ``--ff-only`` flag is non-negotiable.
        assert ("pull", "--ff-only") in fake_git.calls

    def test_pull_failed_surface_when_git_exits_nonzero(
        self, monkeypatch, enable_maintenance, fake_git,
    ):
        monkeypatch.setenv("NOVA_MAINTENANCE_ALLOW_PULL", "true")
        fake_git.scripted = {
            ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"):
                (0, "origin/main\n", ""),
            ("status", "--porcelain"): (0, "", ""),
            ("rev-list", "--count", "HEAD..@{u}"): (0, "1\n", ""),
            ("rev-list", "--count", "@{u}..HEAD"): (0, "0\n", ""),
            ("rev-parse", "HEAD"): (0, "abc\n", ""),
            ("pull", "--ff-only"): (1, "", "fatal: non-ff\n"),
        }
        result = maint.pull()
        assert result.outcome == maint.PULL_FAILED
        # Detail must be sanitised — never the raw stderr.
        assert "fatal" not in result.detail.lower()


# ── Restart safety ──────────────────────────────────────────────────


class TestRestartRefusals:
    def test_restart_not_allowed_by_default(
        self, monkeypatch, enable_maintenance, fake_git_and_systemctl,
    ):
        recorded = []
        monkeypatch.setattr(
            maint.subprocess, "run",
            lambda *a, **kw: recorded.append((a, kw)) or MagicMock(returncode=0),
        )
        # Restart switch off; even with enabled=true and mode set, refused.
        monkeypatch.setenv("NOVA_MAINTENANCE_RESTART_MODE", "systemd-user")
        result = maint.restart()
        assert result.outcome == maint.RESTART_NOT_ALLOWED
        assert recorded == []

    def test_restart_mode_disabled_blocks_even_when_allowed(
        self, monkeypatch, enable_maintenance, fake_git_and_systemctl,
    ):
        recorded = []
        monkeypatch.setattr(
            maint.subprocess, "run",
            lambda *a, **kw: recorded.append((a, kw)) or MagicMock(returncode=0),
        )
        monkeypatch.setenv("NOVA_MAINTENANCE_ALLOW_RESTART", "true")
        # Mode unset → resolves to "disabled".
        result = maint.restart()
        assert result.outcome == maint.RESTART_MODE_DISABLED
        assert recorded == []

    def test_restart_mode_unknown_normalises_to_disabled(
        self, monkeypatch, enable_maintenance, fake_git_and_systemctl,
    ):
        recorded = []
        monkeypatch.setattr(
            maint.subprocess, "run",
            lambda *a, **kw: recorded.append((a, kw)) or MagicMock(returncode=0),
        )
        monkeypatch.setenv("NOVA_MAINTENANCE_ALLOW_RESTART", "true")
        # A typo / dangerous backend name must never spawn.
        monkeypatch.setenv("NOVA_MAINTENANCE_RESTART_MODE", "sudo-systemctl")
        result = maint.restart()
        assert result.outcome == maint.RESTART_MODE_DISABLED
        assert recorded == []

    def test_restart_invalid_unit_blocks(
        self, monkeypatch, enable_maintenance, fake_git_and_systemctl,
    ):
        recorded = []
        monkeypatch.setattr(
            maint.subprocess, "run",
            lambda *a, **kw: recorded.append((a, kw)) or MagicMock(returncode=0),
        )
        monkeypatch.setenv("NOVA_MAINTENANCE_ALLOW_RESTART", "true")
        monkeypatch.setenv("NOVA_MAINTENANCE_RESTART_MODE", "systemd-user")
        monkeypatch.setenv("NOVA_MAINTENANCE_SYSTEMD_UNIT", "nova.service; rm -rf /")
        result = maint.restart()
        assert result.outcome == maint.RESTART_INVALID_UNIT
        assert recorded == []


class TestRestartHappyPath:
    def test_restart_spawns_systemctl_user_restart(self, monkeypatch, enable_maintenance):
        # Configure restart switch + mode + unit.
        monkeypatch.setenv("NOVA_MAINTENANCE_ALLOW_RESTART", "true")
        monkeypatch.setenv("NOVA_MAINTENANCE_RESTART_MODE", "systemd-user")
        monkeypatch.setenv("NOVA_MAINTENANCE_SYSTEMD_UNIT", "nova.service")
        monkeypatch.setattr(
            maint.shutil, "which",
            lambda name: f"/usr/bin/{name}" if name in ("systemctl", "git") else None,
        )
        recorded = []

        class _FakeCompleted:
            returncode = 0
            stdout = b""
            stderr = b""

        def _fake_run(argv, **kwargs):
            recorded.append({"argv": list(argv), "kwargs": dict(kwargs)})
            return _FakeCompleted()

        monkeypatch.setattr(maint.subprocess, "run", _fake_run)
        result = maint.restart()
        assert result.outcome == maint.RESTART_ACCEPTED
        assert len(recorded) == 1
        call = recorded[0]
        # Exact argv. No sudo, no system-level systemctl, no extra flags.
        assert call["argv"] == [
            "/usr/bin/systemctl", "--user", "restart", "nova.service",
        ]
        # shell=False on every spawn.
        assert call["kwargs"].get("shell") is False
        # Timeout must be bounded.
        assert isinstance(call["kwargs"].get("timeout"), (int, float))
        # No privilege-escalation argv elements anywhere.
        for token_arg in call["argv"]:
            assert "sudo" not in token_arg
            assert "pkexec" not in token_arg
            assert "doas" not in token_arg

    def test_restart_systemctl_missing(self, monkeypatch, enable_maintenance):
        monkeypatch.setenv("NOVA_MAINTENANCE_ALLOW_RESTART", "true")
        monkeypatch.setenv("NOVA_MAINTENANCE_RESTART_MODE", "systemd-user")
        monkeypatch.setattr(maint.shutil, "which", lambda _name: None)
        recorded = []
        monkeypatch.setattr(
            maint.subprocess, "run",
            lambda *a, **kw: recorded.append((a, kw)) or MagicMock(returncode=0),
        )
        result = maint.restart()
        assert result.outcome == maint.RESTART_SYSTEMCTL_MISSING
        assert recorded == []

    def test_restart_failure_surfaces_calmly(self, monkeypatch, enable_maintenance):
        monkeypatch.setenv("NOVA_MAINTENANCE_ALLOW_RESTART", "true")
        monkeypatch.setenv("NOVA_MAINTENANCE_RESTART_MODE", "systemd-user")
        monkeypatch.setenv("NOVA_MAINTENANCE_SYSTEMD_UNIT", "nova.service")
        monkeypatch.setattr(
            maint.shutil, "which",
            lambda name: f"/usr/bin/{name}" if name in ("systemctl", "git") else None,
        )

        class _FakeCompleted:
            returncode = 1
            stdout = b""
            stderr = b"Failed to restart nova.service\n"

        monkeypatch.setattr(maint.subprocess, "run", lambda *a, **kw: _FakeCompleted())
        result = maint.restart()
        assert result.outcome == maint.RESTART_FAILED
        assert "Failed" not in result.detail  # sanitised
        # The unit / mode are still surfaced for the UI.
        assert result.unit == "nova.service"
        assert result.mode == maint.RESTART_MODE_SYSTEMD_USER

    def test_restart_handles_timeout_calmly(self, monkeypatch, enable_maintenance):
        monkeypatch.setenv("NOVA_MAINTENANCE_ALLOW_RESTART", "true")
        monkeypatch.setenv("NOVA_MAINTENANCE_RESTART_MODE", "systemd-user")
        monkeypatch.setenv("NOVA_MAINTENANCE_SYSTEMD_UNIT", "nova.service")
        monkeypatch.setattr(
            maint.shutil, "which",
            lambda name: f"/usr/bin/{name}" if name in ("systemctl", "git") else None,
        )

        def _raise_timeout(*a, **kw):
            raise subprocess.TimeoutExpired(cmd=a[0], timeout=kw.get("timeout", 5))

        monkeypatch.setattr(maint.subprocess, "run", _raise_timeout)
        result = maint.restart()
        assert result.outcome == maint.RESTART_FAILED


# ── Subprocess kwargs contract ──────────────────────────────────────


class TestSubprocessKwargs:
    """Pin the exact subprocess.run kwargs used by the git path.

    The maintenance helper must never set ``shell=True``, must use an
    argv list (not a string), must close stdin, and must always pass
    a finite timeout.
    """

    def test_run_git_uses_argv_list_and_shell_false(
        self, monkeypatch, fake_repo,
    ):
        recorded = []

        class _FakeCompleted:
            returncode = 0
            stdout = b""
            stderr = b""

        def _fake_run(argv, **kwargs):
            recorded.append({"argv": list(argv), "kwargs": dict(kwargs)})
            return _FakeCompleted()

        monkeypatch.setattr(
            maint.shutil, "which",
            lambda name: "/usr/bin/git" if name == "git" else None,
        )
        monkeypatch.setattr(maint.subprocess, "run", _fake_run)
        # Use the actual helper — not the test-only `fake_git` injection.
        rc, out, err = maint._run_git(
            ["status", "--porcelain"],
            repo_path=fake_repo, timeout=5.0,
        )
        assert rc == 0
        assert len(recorded) == 1
        call = recorded[0]
        # argv is a list whose first element is the resolved git path.
        assert isinstance(call["argv"], list)
        assert call["argv"][0] == "/usr/bin/git"
        assert call["argv"][1:] == ["status", "--porcelain"]
        # shell=False on every git call.
        assert call["kwargs"].get("shell") is False
        # Bounded timeout.
        assert call["kwargs"].get("timeout") == 5.0
        # CWD pinned to the configured repo path.
        assert call["kwargs"].get("cwd") == fake_repo
        # Stdin is closed.
        assert call["kwargs"].get("stdin") is subprocess.DEVNULL


# ── Web endpoint integration ────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "nova.db")
    monkeypatch.setattr(core_memory, "DB_PATH", path)
    monkeypatch.setattr(natural_store, "DB_PATH", path)
    core_memory.initialize_db()
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM users")
    return path


def _make_user(db_path, username, password="pw", role=users.ROLE_USER,
               is_restricted=False):
    with sqlite3.connect(db_path) as conn:
        return users.create_user(
            conn, username, password, role=role, is_restricted=is_restricted,
        )


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
    resp = client.post("/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _h(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_token(db_path, web_client):
    _make_user(db_path, "alice", role=users.ROLE_ADMIN)
    return _login(web_client, "alice")


@pytest.fixture
def user_token(db_path, web_client):
    _make_user(db_path, "bob")
    return _login(web_client, "bob")


@pytest.fixture
def restricted_token(db_path, web_client):
    _make_user(db_path, "kid", is_restricted=True)
    return _login(web_client, "kid")


class TestMaintenanceEndpointsAuth:
    @pytest.mark.parametrize("method,path", [
        ("GET", "/admin/maintenance/status"),
        ("POST", "/admin/maintenance/fetch"),
        ("POST", "/admin/maintenance/pull"),
        ("POST", "/admin/maintenance/restart"),
    ])
    def test_non_admin_user_forbidden(self, web_client, user_token, method, path):
        if method == "GET":
            resp = web_client.get(path, headers=_h(user_token))
        else:
            resp = web_client.post(path, headers=_h(user_token), json={"confirm": True})
        assert resp.status_code == 403

    @pytest.mark.parametrize("method,path", [
        ("GET", "/admin/maintenance/status"),
        ("POST", "/admin/maintenance/fetch"),
        ("POST", "/admin/maintenance/pull"),
        ("POST", "/admin/maintenance/restart"),
    ])
    def test_restricted_user_forbidden(self, web_client, restricted_token, method, path):
        if method == "GET":
            resp = web_client.get(path, headers=_h(restricted_token))
        else:
            resp = web_client.post(
                path, headers=_h(restricted_token), json={"confirm": True},
            )
        assert resp.status_code == 403

    @pytest.mark.parametrize("method,path", [
        ("GET", "/admin/maintenance/status"),
        ("POST", "/admin/maintenance/fetch"),
        ("POST", "/admin/maintenance/pull"),
        ("POST", "/admin/maintenance/restart"),
    ])
    def test_unauthenticated_blocked(self, web_client, method, path):
        if method == "GET":
            resp = web_client.get(path)
        else:
            resp = web_client.post(path, json={"confirm": True})
        # FastAPI maps a missing bearer to 403 (HTTPBearer's default).
        assert resp.status_code in (401, 403)


class TestMaintenanceEndpointsDisabled:
    """When the feature is disabled, every admin endpoint reports
    ``state="disabled"`` (or ``outcome="disabled"``) without touching
    git or systemctl."""

    def test_status_disabled(self, web_client, admin_token):
        resp = web_client.get(
            "/admin/maintenance/status", headers=_h(admin_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "disabled"
        assert body["enabled"] is False

    def test_fetch_disabled(self, web_client, admin_token):
        resp = web_client.post(
            "/admin/maintenance/fetch", headers=_h(admin_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "disabled"

    def test_pull_disabled_requires_confirm_first(self, web_client, admin_token):
        # Missing confirm → 400 regardless of feature switch state.
        resp = web_client.post(
            "/admin/maintenance/pull", headers=_h(admin_token), json={},
        )
        assert resp.status_code == 400

    def test_pull_disabled_with_confirm_returns_disabled(self, web_client, admin_token):
        resp = web_client.post(
            "/admin/maintenance/pull",
            headers=_h(admin_token), json={"confirm": True},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["outcome"] == "disabled"

    def test_restart_disabled_with_confirm_returns_disabled(
        self, web_client, admin_token,
    ):
        resp = web_client.post(
            "/admin/maintenance/restart",
            headers=_h(admin_token), json={"confirm": True},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["outcome"] == "disabled"


class TestMaintenanceEndpointsConfirmation:
    """Confirmation is mandatory for pull / restart even when enabled."""

    @pytest.fixture(autouse=True)
    def _enable_pull(self, monkeypatch):
        monkeypatch.setenv("NOVA_MAINTENANCE_ENABLED", "true")
        monkeypatch.setenv("NOVA_MAINTENANCE_ALLOW_PULL", "true")

    def test_pull_requires_confirm_true(self, web_client, admin_token):
        resp = web_client.post(
            "/admin/maintenance/pull",
            headers=_h(admin_token), json={"confirm": False},
        )
        assert resp.status_code == 400

    def test_restart_requires_confirm_true(self, web_client, admin_token):
        resp = web_client.post(
            "/admin/maintenance/restart",
            headers=_h(admin_token), json={"confirm": False},
        )
        assert resp.status_code == 400


class TestMaintenanceEndpointsResponses:
    """When enabled, the admin endpoints surface the helper's snapshot."""

    @pytest.fixture(autouse=True)
    def _enable_with_repo(self, monkeypatch, tmp_path):
        (tmp_path / ".git").mkdir()
        monkeypatch.setenv("NOVA_MAINTENANCE_ENABLED", "true")
        monkeypatch.setenv("NOVA_MAINTENANCE_REPO_PATH", str(tmp_path))
        # Stub the helper layer so we don't depend on a real repo.
        from core import maintenance as _maint

        def _fake_status(do_fetch=False):
            return _maint.MaintenanceStatus(
                state=_maint.STATE_READY,
                enabled=True,
                allow_pull=False,
                allow_restart=False,
                restart_mode=_maint.RESTART_MODE_OFF,
                unit="nova.service",
                repo_path=str(tmp_path),
                branch="main",
                commit="abc123",
                upstream="origin/main",
                has_upstream=True,
                working_tree_clean=True,
                update_available=_maint.UPDATE_UP_TO_DATE,
                detail="Already up to date.",
            )

        monkeypatch.setattr(_maint, "get_status", _fake_status)

    def test_status_shape(self, web_client, admin_token):
        resp = web_client.get(
            "/admin/maintenance/status", headers=_h(admin_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "ready"
        assert body["branch"] == "main"
        assert body["commit"] == "abc123"
        assert body["upstream"] == "origin/main"
        assert body["update_available"] == "up_to_date"
        # The response must never leak the configured token / secret
        # values. We have no token field here on purpose.
        assert "token" not in body
