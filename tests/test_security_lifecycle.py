"""
Tests for the SilentGuard lifecycle helper.

The lifecycle helper is the *only* file in ``core.security`` allowed to
import :mod:`subprocess` and :mod:`shutil`. These tests pin the safety
contract:

  * disabled integration is silent — no probe, no spawn;
  * auto-start is firmly opt-in (off by default);
  * only ``systemctl --user start <validated-unit>`` is ever spawned;
  * unit-name validation rejects every shape that could escape the
    intended argv (path traversal, shell metacharacters, sudo / firewall
    aliases, whitespace, system-level systemctl);
  * a failed spawn (timeout, non-zero exit, missing binary, OSError)
    surfaces as a calm ``could_not_start`` snapshot;
  * a successful spawn whose API has not yet bound surfaces as
    ``starting`` — never as a fake ``connected``;
  * the helper never raises into the caller, even when the provider
    misbehaves.

Provider behaviour (file / HTTP transport, prompt-context block,
existing per-user gate) is unaffected by this module and continues to
be covered by ``test_security_provider``, ``test_security_context``,
and ``test_integrations_silentguard``.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional
from unittest.mock import MagicMock

import pytest

# Heavy / optional deps that ``web.py`` pulls in at module load. Stub
# them before any test imports ``web`` so a missing wheel (e.g.
# ``sgmllib`` removed in Python 3.10) cannot block this file. Only the
# minimal attribute surface the importers actually touch is provided;
# everything else degrades to a MagicMock.
for _mod in ("ddgs", "ollama", "sgmllib", "feedparser"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from core.security import lifecycle as lifecycle_module  # noqa: E402
from core.security.lifecycle import (  # noqa: E402
    DEFAULT_SYSTEMD_UNIT,
    LifecycleStatus,
    STATE_CONNECTED,
    STATE_COULD_NOT_START,
    STATE_DISABLED,
    STATE_STARTING,
    STATE_UNAVAILABLE,
    START_MODE_DISABLED,
    START_MODE_SYSTEMD_USER,
    disabled_status,
    ensure_running,
    validate_unit_name,
)
from core.security.provider import (  # noqa: E402
    STATE_AVAILABLE,
    STATE_OFFLINE,
    STATE_UNAVAILABLE as PROVIDER_STATE_UNAVAILABLE,
    SecurityStatus,
)


# ── Test doubles ────────────────────────────────────────────────────


@dataclass
class _FakeProbeProvider:
    """Provider stub that records call counts and returns canned status."""

    name: str = "silentguard"
    status_sequence: tuple = ()
    raise_on_call: bool = False
    calls: int = 0

    def get_status(self) -> SecurityStatus:
        self.calls += 1
        if self.raise_on_call:
            raise RuntimeError("provider boom")
        if not self.status_sequence:
            return SecurityStatus(
                available=False,
                service=self.name,
                state=STATE_OFFLINE,
                message="stub offline",
            )
        idx = min(self.calls - 1, len(self.status_sequence) - 1)
        return self.status_sequence[idx]


def _available() -> SecurityStatus:
    return SecurityStatus(
        available=True,
        service="silentguard",
        state=STATE_AVAILABLE,
        message="stub available",
    )


def _offline() -> SecurityStatus:
    return SecurityStatus(
        available=False,
        service="silentguard",
        state=STATE_OFFLINE,
        message="stub offline",
    )


def _unconfigured() -> SecurityStatus:
    return SecurityStatus(
        available=False,
        service="silentguard",
        state=PROVIDER_STATE_UNAVAILABLE,
        message="stub unconfigured",
    )


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_lifecycle_env(monkeypatch):
    """Strip every host-level env var so each test starts fresh."""
    for var in (
        "NOVA_SILENTGUARD_ENABLED",
        "NOVA_SILENTGUARD_AUTO_START",
        "NOVA_SILENTGUARD_START_MODE",
        "NOVA_SILENTGUARD_SYSTEMD_UNIT",
        "NOVA_SILENTGUARD_PATH",
        "NOVA_SILENTGUARD_API_URL",
        "NOVA_SILENTGUARD_API_BASE_URL",
        "NOVA_SILENTGUARD_API_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture
def _no_sleep(monkeypatch):
    """Replace the lifecycle module's sleeper with a no-op for speed."""
    yield lambda _seconds: None


@pytest.fixture
def _spawn_recorder(monkeypatch):
    """Record every ``subprocess.run`` invocation; never actually exec."""
    calls: list[dict] = []

    class _FakeCompleted:
        def __init__(self, returncode: int = 0):
            self.returncode = returncode
            self.stdout = b""
            self.stderr = b""

    def _fake_run(argv, **kwargs):
        calls.append({"argv": list(argv), "kwargs": dict(kwargs)})
        rc = _fake_run.next_returncode
        if _fake_run.next_exception is not None:
            raise _fake_run.next_exception
        return _FakeCompleted(returncode=rc)

    _fake_run.next_returncode = 0
    _fake_run.next_exception: Optional[BaseException] = None

    monkeypatch.setattr(lifecycle_module.subprocess, "run", _fake_run)
    # Pretend systemctl is on PATH so we don't depend on the host having it.
    monkeypatch.setattr(
        lifecycle_module.shutil, "which",
        lambda name: f"/usr/bin/{name}" if name == "systemctl" else None,
    )

    return calls, _fake_run


# ── Disabled integration ────────────────────────────────────────────


class TestDisabledIntegration:
    def test_disabled_when_env_unset(self, _spawn_recorder):
        provider = _FakeProbeProvider(status_sequence=(_available(),))
        result = ensure_running(provider=provider)
        assert result.state == STATE_DISABLED
        assert result.enabled is False
        assert result.message == "SilentGuard integration disabled."
        # Disabled means: no probe, no spawn.
        assert provider.calls == 0
        calls, _ = _spawn_recorder
        assert calls == []

    def test_disabled_when_env_explicitly_false(self, monkeypatch, _spawn_recorder):
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "false")
        provider = _FakeProbeProvider(status_sequence=(_available(),))
        result = ensure_running(provider=provider)
        assert result.state == STATE_DISABLED
        assert provider.calls == 0
        calls, _ = _spawn_recorder
        assert calls == []

    def test_disabled_status_helper_returns_safe_snapshot(self, monkeypatch):
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        monkeypatch.setenv("NOVA_SILENTGUARD_AUTO_START", "true")
        snapshot = disabled_status()
        assert snapshot.state == STATE_DISABLED
        assert snapshot.enabled is False
        # auto_start is hard-coded False in the disabled snapshot — even
        # if the host enabled it, a disabled-for-this-user view must
        # never claim auto-start is reachable.
        assert snapshot.auto_start is False
        assert "disabled" in snapshot.message.lower()


# ── Enabled, reachable ──────────────────────────────────────────────


class TestEnabledReachable:
    def test_connected_when_provider_available(
        self, monkeypatch, _spawn_recorder, _no_sleep,
    ):
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        provider = _FakeProbeProvider(status_sequence=(_available(),))
        result = ensure_running(provider=provider, sleep=_no_sleep)
        assert result.state == STATE_CONNECTED
        assert result.enabled is True
        assert "read-only" in result.message.lower()
        assert provider.calls == 1
        # Reachable on first probe → no spawn ever.
        calls, _ = _spawn_recorder
        assert calls == []


# ── Enabled, unreachable, auto-start off ────────────────────────────


class TestEnabledAutoStartOff:
    def test_unavailable_when_auto_start_disabled(
        self, monkeypatch, _spawn_recorder,
    ):
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        # NOVA_SILENTGUARD_AUTO_START defaults off.
        provider = _FakeProbeProvider(status_sequence=(_offline(),))
        result = ensure_running(provider=provider)
        assert result.state == STATE_UNAVAILABLE
        assert result.auto_start is False
        assert "unavailable" in result.message.lower()
        assert provider.calls == 1
        calls, _ = _spawn_recorder
        assert calls == []

    def test_unavailable_when_start_mode_disabled(
        self, monkeypatch, _spawn_recorder,
    ):
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        monkeypatch.setenv("NOVA_SILENTGUARD_AUTO_START", "true")
        monkeypatch.setenv("NOVA_SILENTGUARD_START_MODE", "disabled")
        provider = _FakeProbeProvider(status_sequence=(_offline(),))
        result = ensure_running(provider=provider)
        assert result.state == STATE_UNAVAILABLE
        # Auto-start was on, but start_mode was disabled → no spawn.
        calls, _ = _spawn_recorder
        assert calls == []

    def test_unavailable_when_start_mode_unrecognised(
        self, monkeypatch, _spawn_recorder,
    ):
        # A typo or unsupported backend (sudo / firewall / etc.) must
        # normalise to disabled, never silently spawn something.
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        monkeypatch.setenv("NOVA_SILENTGUARD_AUTO_START", "true")
        monkeypatch.setenv("NOVA_SILENTGUARD_START_MODE", "sudo-systemctl")
        provider = _FakeProbeProvider(status_sequence=(_offline(),))
        result = ensure_running(provider=provider)
        assert result.state == STATE_UNAVAILABLE
        assert result.start_mode == START_MODE_DISABLED
        calls, _ = _spawn_recorder
        assert calls == []


# ── Enabled, unreachable, auto-start on, valid config ───────────────


class TestEnabledAutoStartOn:
    def _enable(self, monkeypatch, *, unit: str = DEFAULT_SYSTEMD_UNIT):
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        monkeypatch.setenv("NOVA_SILENTGUARD_AUTO_START", "true")
        monkeypatch.setenv("NOVA_SILENTGUARD_START_MODE", "systemd-user")
        monkeypatch.setenv("NOVA_SILENTGUARD_SYSTEMD_UNIT", unit)

    def test_connected_when_post_probe_succeeds(
        self, monkeypatch, _spawn_recorder, _no_sleep,
    ):
        self._enable(monkeypatch)
        provider = _FakeProbeProvider(
            status_sequence=(_offline(), _available()),
        )
        result = ensure_running(provider=provider, sleep=_no_sleep)
        assert result.state == STATE_CONNECTED
        assert provider.calls == 2

        calls, _ = _spawn_recorder
        assert len(calls) == 1
        assert calls[0]["argv"] == [
            "/usr/bin/systemctl", "--user", "start", DEFAULT_SYSTEMD_UNIT,
        ]
        # Strict spawn discipline.
        assert calls[0]["kwargs"]["shell"] is False
        assert calls[0]["kwargs"]["check"] is False
        assert calls[0]["kwargs"]["stdin"] == subprocess.DEVNULL

    def test_starting_when_post_probe_still_offline(
        self, monkeypatch, _spawn_recorder, _no_sleep,
    ):
        self._enable(monkeypatch)
        # Spawn returns 0 but the API does not bind in time.
        provider = _FakeProbeProvider(
            status_sequence=(_offline(), _offline()),
        )
        result = ensure_running(provider=provider, sleep=_no_sleep)
        assert result.state == STATE_STARTING
        assert "starting" in result.message.lower()
        assert provider.calls == 2

    def test_could_not_start_when_systemctl_returns_nonzero(
        self, monkeypatch, _spawn_recorder, _no_sleep,
    ):
        self._enable(monkeypatch)
        _, fake_run = _spawn_recorder
        fake_run.next_returncode = 1
        provider = _FakeProbeProvider(status_sequence=(_offline(),))
        result = ensure_running(provider=provider, sleep=_no_sleep)
        assert result.state == STATE_COULD_NOT_START
        assert "could not be started" in result.message.lower()
        # No second probe — the spawn failed before we got there.
        assert provider.calls == 1

    def test_could_not_start_when_systemctl_times_out(
        self, monkeypatch, _spawn_recorder, _no_sleep,
    ):
        self._enable(monkeypatch)
        _, fake_run = _spawn_recorder
        fake_run.next_exception = subprocess.TimeoutExpired(
            cmd="systemctl", timeout=3.0,
        )
        provider = _FakeProbeProvider(status_sequence=(_offline(),))
        result = ensure_running(provider=provider, sleep=_no_sleep)
        assert result.state == STATE_COULD_NOT_START
        assert provider.calls == 1

    def test_could_not_start_when_spawn_oserror(
        self, monkeypatch, _spawn_recorder, _no_sleep,
    ):
        self._enable(monkeypatch)
        _, fake_run = _spawn_recorder
        fake_run.next_exception = OSError("permission denied")
        provider = _FakeProbeProvider(status_sequence=(_offline(),))
        result = ensure_running(provider=provider, sleep=_no_sleep)
        assert result.state == STATE_COULD_NOT_START

    def test_could_not_start_when_systemctl_missing(
        self, monkeypatch, _spawn_recorder, _no_sleep,
    ):
        self._enable(monkeypatch)
        # Override the recorder's "which" to return None, simulating a
        # host without systemctl on PATH.
        monkeypatch.setattr(lifecycle_module.shutil, "which", lambda _n: None)
        provider = _FakeProbeProvider(status_sequence=(_offline(),))
        result = ensure_running(provider=provider, sleep=_no_sleep)
        assert result.state == STATE_COULD_NOT_START
        # Without systemctl we never even attempted to spawn.
        calls, _ = _spawn_recorder
        assert calls == []

    def test_could_not_start_when_unit_invalid(
        self, monkeypatch, _spawn_recorder, _no_sleep,
    ):
        self._enable(monkeypatch, unit="../etc/passwd")
        provider = _FakeProbeProvider(status_sequence=(_offline(),))
        result = ensure_running(provider=provider, sleep=_no_sleep)
        assert result.state == STATE_COULD_NOT_START
        # Validation must reject *before* any spawn happens.
        calls, _ = _spawn_recorder
        assert calls == []

    def test_could_not_start_when_unit_empty(
        self, monkeypatch, _spawn_recorder, _no_sleep,
    ):
        self._enable(monkeypatch, unit="   ")
        provider = _FakeProbeProvider(status_sequence=(_offline(),))
        result = ensure_running(provider=provider, sleep=_no_sleep)
        assert result.state == STATE_COULD_NOT_START
        calls, _ = _spawn_recorder
        assert calls == []


# ── Validator ───────────────────────────────────────────────────────


class TestValidateUnitName:
    @pytest.mark.parametrize("unit", [
        "silentguard-api.service",
        "silentguard.service",
        "my-silentguard_api.service",
        "sg.api.service",
        "a.service",
    ])
    def test_accepts_safe_unit_names(self, unit):
        assert validate_unit_name(unit) is True

    @pytest.mark.parametrize("unit", [
        "",
        "   ",
        ".service",                    # leading dot
        "../etc/passwd",
        "/etc/silentguard.service",    # absolute path
        "silentguard.service\n",       # newline
        "silentguard.service;rm -rf",  # shell metachar
        "silentguard.service & sleep",  # shell metachar
        "silentguard.service|cat",     # pipe
        "$(whoami).service",           # command substitution
        "`id`.service",                # backtick
        "silentguard service",         # whitespace
        "silentguard.timer",           # wrong suffix
        "SilentGuard.service",         # uppercase rejected by regex
        "silentguard.service\\extra",  # backslash
    ])
    def test_rejects_unsafe_unit_names(self, unit):
        assert validate_unit_name(unit) is False

    def test_rejects_non_string(self):
        assert validate_unit_name(None) is False  # type: ignore[arg-type]
        assert validate_unit_name(123) is False  # type: ignore[arg-type]
        assert validate_unit_name(["silentguard-api.service"]) is False  # type: ignore[arg-type]

    def test_rejects_overlong_unit(self):
        assert validate_unit_name("a" * 200 + ".service") is False


# ── Spawner safety ──────────────────────────────────────────────────


class TestSpawnerSafety:
    def _enable(self, monkeypatch):
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        monkeypatch.setenv("NOVA_SILENTGUARD_AUTO_START", "true")
        monkeypatch.setenv("NOVA_SILENTGUARD_START_MODE", "systemd-user")

    def test_argv_uses_systemctl_user_start_only(
        self, monkeypatch, _spawn_recorder, _no_sleep,
    ):
        self._enable(monkeypatch)
        provider = _FakeProbeProvider(
            status_sequence=(_offline(), _available()),
        )
        ensure_running(provider=provider, sleep=_no_sleep)
        calls, _ = _spawn_recorder
        argv = calls[0]["argv"]
        # Verbs and flags are pinned: no stop / restart / enable, no
        # system-level systemctl, no extra arguments.
        assert argv[0].endswith("systemctl")
        assert argv[1] == "--user"
        assert argv[2] == "start"
        assert len(argv) == 4
        for forbidden in ("stop", "restart", "reload", "enable", "disable",
                          "daemon-reload", "kill", "mask"):
            assert forbidden not in argv

    def test_no_sudo_or_privilege_escalation_in_argv(
        self, monkeypatch, _spawn_recorder, _no_sleep,
    ):
        self._enable(monkeypatch)
        provider = _FakeProbeProvider(
            status_sequence=(_offline(), _available()),
        )
        ensure_running(provider=provider, sleep=_no_sleep)
        calls, _ = _spawn_recorder
        argv_str = " ".join(calls[0]["argv"]).lower()
        for tool in ("sudo", "pkexec", "doas", "su ", "runuser", "setpriv"):
            assert tool not in argv_str

    def test_no_firewall_command_in_argv(
        self, monkeypatch, _spawn_recorder, _no_sleep,
    ):
        self._enable(monkeypatch)
        provider = _FakeProbeProvider(
            status_sequence=(_offline(), _available()),
        )
        ensure_running(provider=provider, sleep=_no_sleep)
        calls, _ = _spawn_recorder
        argv_str = " ".join(calls[0]["argv"]).lower()
        for tool in ("iptables", "nftables", "firewalld", "ufw", "pf", "ipfw"):
            assert tool not in argv_str

    def test_subprocess_run_called_without_shell(
        self, monkeypatch, _spawn_recorder, _no_sleep,
    ):
        self._enable(monkeypatch)
        provider = _FakeProbeProvider(
            status_sequence=(_offline(), _available()),
        )
        ensure_running(provider=provider, sleep=_no_sleep)
        calls, _ = _spawn_recorder
        kwargs = calls[0]["kwargs"]
        assert kwargs["shell"] is False
        assert kwargs["check"] is False
        # Bounded timeout — never wait forever.
        assert kwargs["timeout"] == lifecycle_module.DEFAULT_START_TIMEOUT_SECONDS
        assert kwargs["timeout"] > 0
        # No inherited stdin.
        assert kwargs["stdin"] == subprocess.DEVNULL


# ── Provider robustness ─────────────────────────────────────────────


class TestProviderRobustness:
    def test_provider_raise_treated_as_unavailable(
        self, monkeypatch, _spawn_recorder, _no_sleep,
    ):
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        provider = _FakeProbeProvider(raise_on_call=True)
        result = ensure_running(provider=provider, sleep=_no_sleep)
        assert result.state == STATE_UNAVAILABLE

    def test_ensure_running_never_raises_on_provider_misbehaviour(
        self, monkeypatch,
    ):
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        provider = _FakeProbeProvider(raise_on_call=True)
        # Must return a value, not raise.
        ensure_running(provider=provider)


# ── Disabled-by-user disabled snapshot ──────────────────────────────


class TestDisabledStatusHelper:
    def test_shape(self, monkeypatch):
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        monkeypatch.setenv("NOVA_SILENTGUARD_AUTO_START", "true")
        monkeypatch.setenv("NOVA_SILENTGUARD_START_MODE", "systemd-user")
        monkeypatch.setenv("NOVA_SILENTGUARD_SYSTEMD_UNIT", "silentguard-api.service")
        snap = disabled_status()
        d = snap.as_dict()
        assert set(d.keys()) == {
            "state", "enabled", "auto_start", "start_mode", "unit", "message",
        }
        assert d["state"] == STATE_DISABLED
        assert d["enabled"] is False
        assert d["auto_start"] is False
        # Host config still surfaces, for diagnostic value.
        assert d["start_mode"] == START_MODE_SYSTEMD_USER
        assert d["unit"] == "silentguard-api.service"


# ── Module-level safety: forbidden imports ──────────────────────────


_LIFECYCLE_ALLOWED_MUTATING_IMPORTS = {"subprocess", "shutil"}
_LIFECYCLE_FORBIDDEN_IMPORTS = {"socket", "ctypes", "signal"}


def _imports(module) -> set[str]:
    """Return the top-level import roots in ``module.__file__``."""
    with open(module.__file__, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            roots.add((node.module or "").split(".")[0])
    return roots


class TestLifecycleImportAllowlist:
    def test_lifecycle_imports_only_allowed_mutating_modules(self):
        roots = _imports(lifecycle_module)
        # Lifecycle is allowed subprocess + shutil; no other mutating
        # module is permitted to land here without an explicit review.
        for name in _LIFECYCLE_FORBIDDEN_IMPORTS:
            assert name not in roots, (
                f"core.security.lifecycle must not import {name!r}"
            )

    def test_lifecycle_uses_subprocess_and_shutil(self):
        # The whole point of this module — assert the imports we expect
        # are actually present, so a refactor that quietly drops them
        # also drops the test signal.
        roots = _imports(lifecycle_module)
        assert "subprocess" in roots
        assert "shutil" in roots

    def test_other_security_files_remain_subprocess_free(self):
        # Belt-and-braces: every other file in core.security must stay
        # off subprocess / shutil. We import them lazily here so this
        # test still runs even if a sibling regresses.
        from core.security import (
            provider as provider_module,
            silentguard as silentguard_module,
            silentguard_client as silentguard_client_module,
            context as context_module,
        )
        from core import security as security_pkg

        for module in (
            provider_module,
            silentguard_module,
            silentguard_client_module,
            context_module,
            security_pkg,
        ):
            roots = _imports(module)
            assert "subprocess" not in roots, (
                f"{module.__name__} must not import subprocess"
            )
            assert "shutil" not in roots, (
                f"{module.__name__} must not import shutil"
            )


# ── Web endpoint integration ────────────────────────────────────────


class TestWebLifecycleEndpoint:
    """``GET /integrations/silentguard/lifecycle`` end-to-end behaviour.

    Mirrors the pattern in ``test_admin_users.py``: a real FastAPI
    TestClient, a real DB created in tmp_path, and patched
    background-task entry points so the harness boots cleanly.
    """

    @pytest.fixture
    def web_client(self, tmp_path, monkeypatch):
        import contextlib
        import sqlite3 as _sqlite3
        from unittest.mock import MagicMock, patch
        from fastapi.testclient import TestClient

        from core import memory as core_memory, users
        from memory import store as natural_store
        from core.rate_limiter import _login_limiter

        path = str(tmp_path / "nova.db")
        monkeypatch.setattr(core_memory, "DB_PATH", path)
        monkeypatch.setattr(natural_store, "DB_PATH", path)
        core_memory.initialize_db()
        with _sqlite3.connect(path) as conn:
            conn.execute("DELETE FROM users")
            users.create_user(conn, "alice", "pw")

        _login_limiter._store.clear()

        import web
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("web.initialize_db"))
            stack.enter_context(patch("web.learn_from_feeds"))
            stack.enter_context(patch("web.scheduler", MagicMock()))
            with TestClient(web.app, raise_server_exceptions=True) as client:
                yield client

    def _login(self, web_client):
        resp = web_client.post("/login", json={"username": "alice", "password": "pw"})
        assert resp.status_code == 200, resp.text
        return {"Authorization": f"Bearer {resp.json()['token']}"}

    def _enable_for_alice(self, web_client, headers):
        resp = web_client.post(
            "/settings",
            json={"silentguard_enabled": True},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text

    def test_user_not_opted_in_returns_disabled_without_probe_or_spawn(
        self, web_client, _spawn_recorder, monkeypatch,
    ):
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        monkeypatch.setenv("NOVA_SILENTGUARD_AUTO_START", "true")
        monkeypatch.setenv("NOVA_SILENTGUARD_START_MODE", "systemd-user")

        # Counter to assert the provider is never instantiated.
        instantiated: list[int] = []
        from core.security import lifecycle as lc

        class _ShouldNotBeUsedProvider:
            def __init__(self, *args, **kwargs):
                instantiated.append(1)

            def get_status(self):  # pragma: no cover — should never run
                raise AssertionError("provider must not be probed for disabled user")

        monkeypatch.setattr(lc, "SilentGuardProvider", _ShouldNotBeUsedProvider)

        headers = self._login(web_client)
        # alice has not enabled silentguard_enabled.
        resp = web_client.get(
            "/integrations/silentguard/lifecycle", headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == STATE_DISABLED
        assert body["enabled"] is False
        assert body["auto_start"] is False
        # No provider was instantiated, no spawn happened.
        assert instantiated == []
        calls, _ = _spawn_recorder
        assert calls == []

    def test_opted_in_user_with_disabled_host_returns_disabled(
        self, web_client, _spawn_recorder, monkeypatch,
    ):
        # Host-level switch off → ensure_running reports disabled
        # regardless of per-user opt-in.
        headers = self._login(web_client)
        self._enable_for_alice(web_client, headers)
        resp = web_client.get(
            "/integrations/silentguard/lifecycle", headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["state"] == STATE_DISABLED
        calls, _ = _spawn_recorder
        assert calls == []

    def test_opted_in_user_with_reachable_provider_reports_connected(
        self, web_client, _spawn_recorder, monkeypatch,
    ):
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")

        from core.security import lifecycle as lc

        class _ReachableProvider:
            def __init__(self, *args, **kwargs):
                pass

            def get_status(self):
                return _available()

        monkeypatch.setattr(lc, "SilentGuardProvider", _ReachableProvider)

        headers = self._login(web_client)
        self._enable_for_alice(web_client, headers)
        resp = web_client.get(
            "/integrations/silentguard/lifecycle", headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == STATE_CONNECTED
        assert body["enabled"] is True
        # Reachable on first probe → no spawn ever.
        calls, _ = _spawn_recorder
        assert calls == []

    def test_unauthenticated_call_is_rejected(self, web_client):
        resp = web_client.get("/integrations/silentguard/lifecycle")
        assert resp.status_code in (401, 403)


class TestLifecycleStatusShape:
    def test_lifecycle_status_keys(self):
        snap = LifecycleStatus(
            state=STATE_CONNECTED,
            enabled=True,
            auto_start=False,
            start_mode=START_MODE_DISABLED,
            unit=DEFAULT_SYSTEMD_UNIT,
            message="x",
        )
        d = snap.as_dict()
        assert set(d.keys()) == {
            "state", "enabled", "auto_start", "start_mode", "unit", "message",
        }

    def test_lifecycle_status_is_frozen(self):
        snap = LifecycleStatus(
            state=STATE_CONNECTED,
            enabled=True,
            auto_start=False,
            start_mode=START_MODE_DISABLED,
            unit=DEFAULT_SYSTEMD_UNIT,
            message="x",
        )
        with pytest.raises(Exception):
            snap.state = STATE_STARTING  # type: ignore[misc]
