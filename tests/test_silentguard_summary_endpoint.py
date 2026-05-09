"""
Tests for ``GET /integrations/silentguard/summary``.

The summary endpoint is the single call the Settings UI's SilentGuard
status card makes. It wraps the existing lifecycle helper and the
existing :class:`SilentGuardProvider` count surface so the frontend
can render a calm read-only status without changing any existing
endpoint shape.

These tests pin the user-visible contract:

  * the response shape is stable: ``{lifecycle: {...}, counts: ... | None}``;
  * the per-user gate is honoured (disabled user → ``state="disabled"``,
    ``counts=None``, no provider instantiation, no spawn);
  * the host-level switch and auto-start gating mirror the lifecycle
    endpoint exactly — this endpoint must not introduce a second policy;
  * counts are only attached when lifecycle.state == ``connected`` and
    the provider exposes them;
  * provider failures (raises, malformed payload, count probe boom) all
    map to ``counts=None`` with the lifecycle still honest;
  * the endpoint never raises — every error path is a calm 200 with a
    coherent payload.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

# Heavy / optional deps that ``web.py`` pulls in at module load. Mirror
# the stub pattern in ``test_security_lifecycle`` so a missing wheel
# cannot block this file.
for _mod in ("ddgs", "ollama", "sgmllib", "feedparser"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from core.security.lifecycle import (  # noqa: E402
    STATE_CONNECTED,
    STATE_DISABLED,
    STATE_UNAVAILABLE,
)
from core.security.provider import (  # noqa: E402
    STATE_AVAILABLE,
    STATE_OFFLINE,
    STATE_UNAVAILABLE as PROVIDER_STATE_UNAVAILABLE,
    SecurityStatus,
)


# ── Test doubles ────────────────────────────────────────────────────


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
def _isolate_env(monkeypatch):
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
    from core.security import lifecycle as lifecycle_module
    monkeypatch.setattr(lifecycle_module.time, "sleep", lambda _s: None)
    yield


@pytest.fixture
def _spawn_disabled(monkeypatch):
    """Make ``subprocess.run`` raise to assert no spawn happens.

    Tests that expect zero spawn activity inject this fixture; tests
    that need spawn behaviour use the recorder fixture instead.
    """
    from core.security import lifecycle as lifecycle_module

    def _no_spawn(*_args, **_kwargs):
        raise AssertionError("subprocess.run must not be called in this test")

    monkeypatch.setattr(lifecycle_module.subprocess, "run", _no_spawn)
    monkeypatch.setattr(
        lifecycle_module.shutil, "which",
        lambda name: f"/usr/bin/{name}" if name == "systemctl" else None,
    )
    yield


@pytest.fixture
def web_client(tmp_path, monkeypatch):
    """Spin up a real FastAPI TestClient against a tmp DB."""
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


def _login(web_client):
    resp = web_client.post(
        "/login", json={"username": "alice", "password": "pw"},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _enable_for_alice(web_client, headers):
    resp = web_client.post(
        "/settings",
        json={"silentguard_enabled": True},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text


# ── Disabled state ──────────────────────────────────────────────────


class TestDisabledStates:
    def test_user_not_opted_in_returns_disabled_with_no_counts(
        self, web_client, _spawn_disabled, monkeypatch,
    ):
        # Even if the host operator turned the host-level switch on, a
        # user who has not opted in must still see ``state="disabled"``
        # and ``counts=None``. The provider must not be instantiated.
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")

        instantiated: list[int] = []
        from core.security import lifecycle as lc

        class _ShouldNotBeUsedProvider:
            def __init__(self, *args, **kwargs):
                instantiated.append(1)

            def get_status(self):  # pragma: no cover — must not run
                raise AssertionError("must not probe for disabled user")

        monkeypatch.setattr(lc, "SilentGuardProvider", _ShouldNotBeUsedProvider)

        headers = _login(web_client)
        resp = web_client.get(
            "/integrations/silentguard/summary", headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"lifecycle", "counts"}
        assert body["lifecycle"]["state"] == STATE_DISABLED
        assert body["lifecycle"]["enabled"] is False
        assert body["counts"] is None
        # Defence-in-depth: confirm no provider was instantiated.
        assert instantiated == []

    def test_host_switch_off_returns_disabled_even_when_user_opted_in(
        self, web_client, _spawn_disabled,
    ):
        # Per-user opt-in alone is not enough — the host switch must
        # also be on for the lifecycle to do anything beyond "disabled".
        headers = _login(web_client)
        _enable_for_alice(web_client, headers)
        resp = web_client.get(
            "/integrations/silentguard/summary", headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["lifecycle"]["state"] == STATE_DISABLED
        assert body["counts"] is None


# ── Unavailable state ───────────────────────────────────────────────


class TestUnavailable:
    def test_offline_provider_with_autostart_off_reports_unavailable(
        self, web_client, _spawn_disabled, monkeypatch,
    ):
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        # NOVA_SILENTGUARD_AUTO_START defaults off, so the lifecycle
        # helper must short-circuit to "unavailable" with no spawn.
        from core.security import lifecycle as lc

        class _OfflineProvider:
            def __init__(self, *args, **kwargs):
                pass

            def get_status(self):
                return _offline()

        monkeypatch.setattr(lc, "SilentGuardProvider", _OfflineProvider)

        headers = _login(web_client)
        _enable_for_alice(web_client, headers)
        resp = web_client.get(
            "/integrations/silentguard/summary", headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["lifecycle"]["state"] == STATE_UNAVAILABLE
        assert body["lifecycle"]["enabled"] is True
        assert body["lifecycle"]["auto_start"] is False
        assert body["counts"] is None


# ── Connected state with counts ─────────────────────────────────────


class TestConnectedWithCounts:
    def test_counts_attached_when_provider_exposes_them(
        self, web_client, _spawn_disabled, monkeypatch,
    ):
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        from core.security import lifecycle as lc
        import web as web_module

        class _ReachableLifecycleProvider:
            def __init__(self, *args, **kwargs):
                pass

            def get_status(self):
                return _available()

        # The lifecycle helper instantiates ``SilentGuardProvider`` from
        # the lifecycle module; the summary endpoint instantiates a
        # second one (via ``_SilentGuardProvider`` in ``web``) for the
        # counts probe. Patch both names so each path returns counts
        # from a controlled stub.
        class _CountingProvider:
            def __init__(self, *args, **kwargs):
                pass

            def get_status(self):
                return _available()

            def get_summary_counts(self):
                return {
                    "alerts": 0,
                    "blocked": 0,
                    "trusted": 2,
                    "connections": 5,
                }

        monkeypatch.setattr(lc, "SilentGuardProvider", _ReachableLifecycleProvider)
        monkeypatch.setattr(web_module, "_SilentGuardProvider", _CountingProvider)

        headers = _login(web_client)
        _enable_for_alice(web_client, headers)
        resp = web_client.get(
            "/integrations/silentguard/summary", headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["lifecycle"]["state"] == STATE_CONNECTED
        assert body["counts"] == {
            "alerts": 0,
            "blocked": 0,
            "trusted": 2,
            "connections": 5,
        }

    def test_counts_none_when_provider_returns_none(
        self, web_client, _spawn_disabled, monkeypatch,
    ):
        # File-only fallback: provider is reachable, but
        # get_summary_counts returns None because no HTTP transport is
        # configured. Lifecycle still says connected.
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        from core.security import lifecycle as lc
        import web as web_module

        class _ReachableLifecycleProvider:
            def __init__(self, *args, **kwargs):
                pass

            def get_status(self):
                return _available()

        class _NoCountsProvider:
            def __init__(self, *args, **kwargs):
                pass

            def get_status(self):
                return _available()

            def get_summary_counts(self):
                return None

        monkeypatch.setattr(lc, "SilentGuardProvider", _ReachableLifecycleProvider)
        monkeypatch.setattr(web_module, "_SilentGuardProvider", _NoCountsProvider)

        headers = _login(web_client)
        _enable_for_alice(web_client, headers)
        resp = web_client.get(
            "/integrations/silentguard/summary", headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["lifecycle"]["state"] == STATE_CONNECTED
        assert body["counts"] is None

    def test_counts_none_when_count_probe_raises(
        self, web_client, _spawn_disabled, monkeypatch,
    ):
        # A misbehaving counts implementation must not propagate; the
        # endpoint must absorb the exception and surface counts=None.
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        from core.security import lifecycle as lc
        import web as web_module

        class _ReachableLifecycleProvider:
            def __init__(self, *args, **kwargs):
                pass

            def get_status(self):
                return _available()

        class _BoomProvider:
            def __init__(self, *args, **kwargs):
                pass

            def get_status(self):
                return _available()

            def get_summary_counts(self):
                raise RuntimeError("boom")

        monkeypatch.setattr(lc, "SilentGuardProvider", _ReachableLifecycleProvider)
        monkeypatch.setattr(web_module, "_SilentGuardProvider", _BoomProvider)

        headers = _login(web_client)
        _enable_for_alice(web_client, headers)
        resp = web_client.get(
            "/integrations/silentguard/summary", headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["lifecycle"]["state"] == STATE_CONNECTED
        assert body["counts"] is None


# ── Auth and shape ──────────────────────────────────────────────────


class TestAuthAndShape:
    def test_unauthenticated_call_is_rejected(self, web_client):
        resp = web_client.get("/integrations/silentguard/summary")
        assert resp.status_code in (401, 403)

    def test_response_keys_are_stable(
        self, web_client, _spawn_disabled,
    ):
        # The Settings UI relies on exactly two keys at the top level.
        headers = _login(web_client)
        resp = web_client.get(
            "/integrations/silentguard/summary", headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"lifecycle", "counts"}
        # Lifecycle must carry the full LifecycleStatus shape so the UI
        # can render any state without checking for missing keys.
        assert set(body["lifecycle"].keys()) == {
            "state", "enabled", "auto_start", "start_mode", "unit", "message",
        }

    def test_existing_lifecycle_endpoint_shape_unchanged(
        self, web_client, _spawn_disabled,
    ):
        # Adding the summary endpoint must not change the existing
        # lifecycle endpoint shape — the Settings card and any external
        # caller depend on it. This test pins the contract.
        headers = _login(web_client)
        resp = web_client.get(
            "/integrations/silentguard/lifecycle", headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {
            "state", "enabled", "auto_start", "start_mode", "unit", "message",
        }
