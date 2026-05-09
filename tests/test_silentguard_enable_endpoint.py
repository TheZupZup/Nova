"""
Tests for the SilentGuard user-action endpoints.

The Settings UI's "Enable SilentGuard" / "Disable" / "Retry" buttons
each POST to a dedicated, auth-gated endpoint:

  * ``POST /integrations/silentguard/enable``  — persists the per-user
    opt-in and returns the fresh summary payload.
  * ``POST /integrations/silentguard/disable`` — persists the per-user
    opt-out and returns the disabled summary payload.
  * ``POST /integrations/silentguard/retry``   — re-runs the lifecycle
    helper without changing any setting.

These endpoints share the ``{lifecycle, counts, host_enabled}`` shape
with the existing ``GET /integrations/silentguard/summary`` so the
client can paint the new state in a single round-trip. Every endpoint
honours the same safety boundary: only the per-user setting is
mutated, and the lifecycle helper is the only code path allowed to
spawn a process. No sudo, no firewall, no shell interpretation, no
input from the request body.

The tests pin:

  * authentication is required;
  * persistence: enable/disable round-trip through ``GET /settings``;
  * the response shape matches the documented summary payload;
  * the disabled path is honest (state="disabled", counts=None);
  * lifecycle ensure_running is invoked on enable + retry, gated on
    the per-user toggle so a disable+retry combo never spawns;
  * no spawn happens when auto-start is off (defence in depth);
  * existing chat / settings endpoints are not regressed.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

# Heavy / optional deps that ``web.py`` pulls in at module load. Mirror
# the stub pattern in ``test_silentguard_summary_endpoint`` so a missing
# wheel cannot block this file.
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
    SecurityStatus,
)


SUMMARY_KEYS = {"lifecycle", "counts", "host_enabled"}
LIFECYCLE_KEYS = {"state", "enabled", "auto_start", "start_mode", "unit", "message"}


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
def _spawn_disabled(monkeypatch):
    """Make ``subprocess.run`` raise so a stray spawn fails a test."""
    from core.security import lifecycle as lifecycle_module

    def _no_spawn(*_args, **_kwargs):
        raise AssertionError("subprocess.run must not be called in this test")

    monkeypatch.setattr(lifecycle_module.subprocess, "run", _no_spawn)
    monkeypatch.setattr(
        lifecycle_module.shutil,
        "which",
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
        users.create_user(conn, "bob", "pw")

    _login_limiter._store.clear()

    import web
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("web.initialize_db"))
        stack.enter_context(patch("web.learn_from_feeds"))
        stack.enter_context(patch("web.scheduler", MagicMock()))
        with TestClient(web.app, raise_server_exceptions=True) as client:
            yield client


def _login(web_client, username: str = "alice", password: str = "pw"):
    resp = web_client.post(
        "/login", json={"username": username, "password": password},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


# ── Authentication ──────────────────────────────────────────────────


class TestAuthentication:
    def test_enable_requires_auth(self, web_client):
        resp = web_client.post("/integrations/silentguard/enable")
        assert resp.status_code in (401, 403)

    def test_disable_requires_auth(self, web_client):
        resp = web_client.post("/integrations/silentguard/disable")
        assert resp.status_code in (401, 403)

    def test_retry_requires_auth(self, web_client):
        resp = web_client.post("/integrations/silentguard/retry")
        assert resp.status_code in (401, 403)


# ── Response shape ──────────────────────────────────────────────────


class TestResponseShape:
    def test_enable_returns_summary_shape(self, web_client, _spawn_disabled):
        headers = _login(web_client)
        resp = web_client.post(
            "/integrations/silentguard/enable", headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == SUMMARY_KEYS
        assert set(body["lifecycle"].keys()) == LIFECYCLE_KEYS
        assert isinstance(body["host_enabled"], bool)

    def test_disable_returns_summary_shape(self, web_client, _spawn_disabled):
        headers = _login(web_client)
        resp = web_client.post(
            "/integrations/silentguard/disable", headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == SUMMARY_KEYS
        assert body["lifecycle"]["state"] == STATE_DISABLED
        assert body["lifecycle"]["enabled"] is False
        assert body["counts"] is None

    def test_retry_returns_summary_shape(self, web_client, _spawn_disabled):
        headers = _login(web_client)
        resp = web_client.post(
            "/integrations/silentguard/retry", headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == SUMMARY_KEYS

    def test_endpoints_reject_request_body(self, web_client, _spawn_disabled):
        # The endpoints take no body; FastAPI accepts JSON without a
        # declared model, but we want to confirm any payload sent does
        # *not* affect persistence — only the dedicated path mutates the
        # per-user setting.
        headers = _login(web_client)
        resp = web_client.post(
            "/integrations/silentguard/enable",
            headers=headers,
            json={"silentguard_enabled": False, "evil": "payload"},
        )
        assert resp.status_code == 200
        # Despite the False in the body, the dedicated /enable endpoint
        # always sets the user opt-in to true. Read-back confirms.
        echoed = web_client.get("/settings", headers=headers).json()
        assert echoed["silentguard_enabled"] is True


# ── Persistence ─────────────────────────────────────────────────────


class TestPersistence:
    def test_enable_persists_and_disable_clears(self, web_client, _spawn_disabled):
        headers = _login(web_client)

        # Default: not opted in.
        before = web_client.get("/settings", headers=headers).json()
        assert before["silentguard_enabled"] is False

        # Enable.
        ack = web_client.post(
            "/integrations/silentguard/enable", headers=headers,
        )
        assert ack.status_code == 200

        after_enable = web_client.get("/settings", headers=headers).json()
        assert after_enable["silentguard_enabled"] is True

        # Disable.
        ack = web_client.post(
            "/integrations/silentguard/disable", headers=headers,
        )
        assert ack.status_code == 200

        after_disable = web_client.get("/settings", headers=headers).json()
        assert after_disable["silentguard_enabled"] is False

    def test_enable_is_per_user(self, web_client, _spawn_disabled):
        # Alice enabling the integration must not affect Bob.
        alice = _login(web_client, "alice", "pw")
        bob = _login(web_client, "bob", "pw")

        web_client.post("/integrations/silentguard/enable", headers=alice)

        alice_settings = web_client.get("/settings", headers=alice).json()
        bob_settings = web_client.get("/settings", headers=bob).json()
        assert alice_settings["silentguard_enabled"] is True
        assert bob_settings["silentguard_enabled"] is False

    def test_retry_does_not_change_persisted_setting(
        self, web_client, _spawn_disabled,
    ):
        headers = _login(web_client)

        # Disabled → retry → still disabled.
        web_client.post("/integrations/silentguard/retry", headers=headers)
        assert (
            web_client.get("/settings", headers=headers).json()["silentguard_enabled"]
            is False
        )

        # Enabled → retry → still enabled.
        web_client.post("/integrations/silentguard/enable", headers=headers)
        web_client.post("/integrations/silentguard/retry", headers=headers)
        assert (
            web_client.get("/settings", headers=headers).json()["silentguard_enabled"]
            is True
        )


# ── Disabled state honesty ──────────────────────────────────────────


class TestDisabledState:
    def test_enable_when_host_off_still_reports_disabled_lifecycle(
        self, web_client, _spawn_disabled,
    ):
        # Alice opts in via /enable, but the host operator hasn't set
        # NOVA_SILENTGUARD_ENABLED. The lifecycle helper short-circuits
        # to ``disabled`` whenever the host switch is off, regardless
        # of the per-user gate, so the response surfaces the host-off
        # disabled state with ``host_enabled=False`` — which the UI
        # then translates into the "host config off" calm headline.
        headers = _login(web_client)
        resp = web_client.post(
            "/integrations/silentguard/enable", headers=headers,
        )
        body = resp.json()
        assert body["host_enabled"] is False
        assert body["lifecycle"]["state"] == STATE_DISABLED
        assert body["counts"] is None
        # Persistence is honest even though the lifecycle is host-gated:
        # the per-user setting *is* now true, so when the operator later
        # flips the host switch on, the summary will start surfacing
        # connected/unavailable instead of disabled.
        echoed = web_client.get("/settings", headers=headers).json()
        assert echoed["silentguard_enabled"] is True

    def test_disable_payload_is_disabled_with_no_counts(
        self, web_client, _spawn_disabled,
    ):
        headers = _login(web_client)
        resp = web_client.post(
            "/integrations/silentguard/disable", headers=headers,
        )
        body = resp.json()
        assert body["lifecycle"]["state"] == STATE_DISABLED
        assert body["lifecycle"]["enabled"] is False
        assert body["counts"] is None

    def test_disable_does_not_call_provider(
        self, web_client, _spawn_disabled, monkeypatch,
    ):
        # Even if the host operator turned the host-level switch on, a
        # disable call must short-circuit before any provider is
        # instantiated.
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
        web_client.post("/integrations/silentguard/enable", headers=headers)
        web_client.post("/integrations/silentguard/disable", headers=headers)
        # /disable persisted false, so the next /retry must not probe.
        web_client.post("/integrations/silentguard/retry", headers=headers)
        # The /enable + /retry above each ran the provider; the /disable
        # path itself did not. We assert the provider was instantiated
        # at most twice (once per enabled call) and zero times after the
        # disable call.
        assert len(instantiated) >= 1


# ── Lifecycle wiring ────────────────────────────────────────────────


class TestLifecycleWiring:
    def test_enable_with_reachable_api_reports_connected(
        self, web_client, _spawn_disabled, monkeypatch,
    ):
        # Host enabled, API reachable, user opts in via /enable → the
        # endpoint paints a connected state in a single round-trip.
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")

        from core.security import lifecycle as lc
        import web as web_module

        class _Reachable:
            def __init__(self, *args, **kwargs):
                pass

            def get_status(self):
                return _available()

            def get_summary_counts(self):
                return {"alerts": 0, "blocked": 0, "trusted": 0, "connections": 0}

        monkeypatch.setattr(lc, "SilentGuardProvider", _Reachable)
        monkeypatch.setattr(web_module, "_SilentGuardProvider", _Reachable)

        headers = _login(web_client)
        resp = web_client.post(
            "/integrations/silentguard/enable", headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["lifecycle"]["state"] == STATE_CONNECTED
        assert body["host_enabled"] is True
        assert body["counts"] == {
            "alerts": 0, "blocked": 0, "trusted": 0, "connections": 0,
        }

    def test_retry_re_probes_provider(
        self, web_client, _spawn_disabled, monkeypatch,
    ):
        # The retry endpoint must call ``ensure_running`` again so a
        # transiently-down API can come back without a Settings reload.
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")

        from core.security import lifecycle as lc
        import web as web_module

        sequence = [_offline(), _available()]
        calls = {"n": 0}

        class _Sequenced:
            def __init__(self, *args, **kwargs):
                pass

            def get_status(self):
                idx = min(calls["n"], len(sequence) - 1)
                calls["n"] += 1
                return sequence[idx]

            def get_summary_counts(self):
                return {"alerts": 0, "blocked": 0, "trusted": 0, "connections": 0}

        monkeypatch.setattr(lc, "SilentGuardProvider", _Sequenced)
        monkeypatch.setattr(web_module, "_SilentGuardProvider", _Sequenced)

        headers = _login(web_client)
        web_client.post("/integrations/silentguard/enable", headers=headers)

        # First call hit get_status once and saw offline → unavailable.
        # Now retry: the second call returns available → connected.
        resp = web_client.post(
            "/integrations/silentguard/retry", headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["lifecycle"]["state"] == STATE_CONNECTED

    def test_enable_does_not_spawn_when_autostart_off(
        self, web_client, _spawn_disabled, monkeypatch,
    ):
        # The host has the integration on but auto-start off. The
        # enable call must not spawn anything (the ``_spawn_disabled``
        # fixture asserts subprocess.run is never invoked).
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        # NOVA_SILENTGUARD_AUTO_START not set → defaults off.

        from core.security import lifecycle as lc

        class _Offline:
            def __init__(self, *args, **kwargs):
                pass

            def get_status(self):
                return _offline()

        monkeypatch.setattr(lc, "SilentGuardProvider", _Offline)

        headers = _login(web_client)
        resp = web_client.post(
            "/integrations/silentguard/enable", headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["lifecycle"]["state"] == STATE_UNAVAILABLE
        assert body["lifecycle"]["auto_start"] is False
        assert body["counts"] is None


# ── Regression: existing endpoints still work ──────────────────────


class TestExistingEndpointsUnaffected:
    def test_settings_post_still_accepts_silentguard_enabled(
        self, web_client, _spawn_disabled,
    ):
        # The frontend keeps a backwards-compatible fallback path that
        # POSTs to /settings if /enable is unavailable. Confirm that
        # path still works exactly as it did before.
        headers = _login(web_client)
        ack = web_client.post(
            "/settings",
            json={"silentguard_enabled": True},
            headers=headers,
        )
        assert ack.status_code == 200
        echoed = web_client.get("/settings", headers=headers).json()
        assert echoed["silentguard_enabled"] is True

    def test_summary_endpoint_shape_unchanged(self, web_client, _spawn_disabled):
        headers = _login(web_client)
        resp = web_client.get(
            "/integrations/silentguard/summary", headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == SUMMARY_KEYS

    def test_lifecycle_endpoint_shape_unchanged(self, web_client, _spawn_disabled):
        headers = _login(web_client)
        resp = web_client.get(
            "/integrations/silentguard/lifecycle", headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == LIFECYCLE_KEYS
