"""
Tests for the SilentGuard mitigation HTTP surface in ``web.py``.

Three new endpoints sit between the Settings UI and SilentGuard's
mitigation API:

  * ``GET  /integrations/silentguard/mitigation``                 — read
  * ``POST /integrations/silentguard/mitigation/enable-temporary``— write
  * ``POST /integrations/silentguard/mitigation/disable``         — write

These tests pin the safety contract:

  * authentication is required on every endpoint;
  * the per-user ``silentguard_enabled`` setting gates every read
    *and* every write — a disabled user never causes Nova to issue
    HTTP traffic to SilentGuard;
  * write endpoints refuse without ``{"acknowledge": true}`` in the
    body and never call SilentGuard before that gate is satisfied;
  * write endpoints relay results from the provider verbatim and
    surface a calm "currently unavailable" message on any failure;
  * the read endpoint never triggers a write, even by accident;
  * the existing ``/summary`` / ``/enable`` / ``/disable`` / ``/retry``
    response shapes are *unchanged* so the existing UI keeps working.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

# Heavy / optional deps that ``web.py`` pulls in at module load.
for _mod in ("ddgs", "ollama", "sgmllib", "feedparser"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from core.security import (  # noqa: E402
    MitigationActionResult,
    MitigationState,
)
from core.security.provider import (  # noqa: E402
    STATE_AVAILABLE,
    STATE_OFFLINE,
    SecurityStatus,
)
from core.security.silentguard_mitigation import (  # noqa: E402
    MODE_DETECTION_ONLY,
    MODE_TEMPORARY_AUTO_BLOCK,
)


MITIGATION_KEYS = {"ok", "available", "state", "message"}
STATE_KEYS = {"mode", "active", "expires_at"}


# ── Helpers ─────────────────────────────────────────────────────────


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
    """Make sure no test in this file accidentally spawns systemctl."""
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


def _enable(web_client, headers):
    resp = web_client.post(
        "/integrations/silentguard/enable", headers=headers,
    )
    assert resp.status_code == 200, resp.text


# ── Test doubles for the provider ───────────────────────────────────


class _ProviderRecorder:
    """Replacement for ``SilentGuardProvider`` that records every call."""

    def __init__(
        self,
        *,
        status=None,
        mitigation_state=None,
        enable_result=None,
        disable_result=None,
    ):
        self._status = status or _available()
        self._mitigation_state = mitigation_state
        self._enable_result = enable_result or MitigationActionResult(
            ok=True,
            state=MitigationState(
                mode=MODE_TEMPORARY_AUTO_BLOCK, active=True,
            ),
            message="enabled",
        )
        self._disable_result = disable_result or MitigationActionResult(
            ok=True,
            state=MitigationState(
                mode=MODE_DETECTION_ONLY, active=False,
            ),
            message="disabled",
        )
        self.calls: list[str] = []

    def get_status(self):
        self.calls.append("get_status")
        return self._status

    def get_summary_counts(self):
        self.calls.append("get_summary_counts")
        return None

    def get_connection_summary(self):
        self.calls.append("get_connection_summary")
        return None

    def get_mitigation_state(self):
        self.calls.append("get_mitigation_state")
        return self._mitigation_state

    def enable_temporary_mitigation(self):
        self.calls.append("enable_temporary_mitigation")
        return self._enable_result

    def disable_mitigation(self):
        self.calls.append("disable_mitigation")
        return self._disable_result


def _patch_provider(monkeypatch, provider_factory):
    """Replace ``web._SilentGuardProvider`` with a factory in the test."""
    import web as web_module
    monkeypatch.setattr(web_module, "_SilentGuardProvider", provider_factory)


# ── Authentication ──────────────────────────────────────────────────


class TestAuthentication:
    def test_get_state_requires_auth(self, web_client):
        resp = web_client.get("/integrations/silentguard/mitigation")
        assert resp.status_code in (401, 403)

    def test_enable_requires_auth(self, web_client):
        resp = web_client.post(
            "/integrations/silentguard/mitigation/enable-temporary",
            json={"acknowledge": True},
        )
        assert resp.status_code in (401, 403)

    def test_disable_requires_auth(self, web_client):
        resp = web_client.post(
            "/integrations/silentguard/mitigation/disable",
            json={"acknowledge": True},
        )
        assert resp.status_code in (401, 403)


# ── Per-user gate ───────────────────────────────────────────────────


class TestPerUserGate:
    def test_disabled_user_get_state_returns_calm_payload_no_provider_call(
        self, web_client, _spawn_disabled, monkeypatch,
    ):
        # Even when the host operator turned on SilentGuard, a user
        # who has not opted in must see a calm "disabled" payload
        # with no provider instantiation. We assert that by failing
        # the test if the provider gets constructed at all.
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")

        instantiated: list[int] = []

        class _ShouldNotBeUsed(_ProviderRecorder):
            def __init__(self, *args, **kwargs):
                instantiated.append(1)
                super().__init__()

        _patch_provider(monkeypatch, _ShouldNotBeUsed)

        headers = _login(web_client)
        resp = web_client.get(
            "/integrations/silentguard/mitigation", headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == MITIGATION_KEYS
        assert body["available"] is False
        assert body["state"] is None
        assert body["ok"] is False
        assert instantiated == []

    def test_disabled_user_enable_returns_calm_payload_no_provider_call(
        self, web_client, _spawn_disabled, monkeypatch,
    ):
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        instantiated: list[int] = []

        class _ShouldNotBeUsed(_ProviderRecorder):
            def __init__(self, *args, **kwargs):
                instantiated.append(1)
                super().__init__()

        _patch_provider(monkeypatch, _ShouldNotBeUsed)

        headers = _login(web_client)
        resp = web_client.post(
            "/integrations/silentguard/mitigation/enable-temporary",
            json={"acknowledge": True},
            headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["available"] is False
        assert body["ok"] is False
        assert instantiated == []

    def test_disabled_user_disable_returns_calm_payload_no_provider_call(
        self, web_client, _spawn_disabled, monkeypatch,
    ):
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        instantiated: list[int] = []

        class _ShouldNotBeUsed(_ProviderRecorder):
            def __init__(self, *args, **kwargs):
                instantiated.append(1)
                super().__init__()

        _patch_provider(monkeypatch, _ShouldNotBeUsed)

        headers = _login(web_client)
        resp = web_client.post(
            "/integrations/silentguard/mitigation/disable",
            json={"acknowledge": True},
            headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["available"] is False
        assert body["ok"] is False
        assert instantiated == []


# ── Acknowledgement gating ──────────────────────────────────────────


class TestAcknowledgementGating:
    def test_enable_without_body_is_rejected(
        self, web_client, _spawn_disabled, monkeypatch,
    ):
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        recorder = _ProviderRecorder()
        _patch_provider(monkeypatch, lambda: recorder)

        headers = _login(web_client)
        _enable(web_client, headers)

        resp = web_client.post(
            "/integrations/silentguard/mitigation/enable-temporary",
            headers=headers,
        )
        # FastAPI will accept the empty body and parse it as
        # ``acknowledge=False``; Nova's gate then refuses with 400.
        assert resp.status_code == 400
        # The provider's mitigation method must never have run.
        assert "enable_temporary_mitigation" not in recorder.calls

    def test_enable_with_acknowledge_false_is_rejected(
        self, web_client, _spawn_disabled, monkeypatch,
    ):
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        recorder = _ProviderRecorder()
        _patch_provider(monkeypatch, lambda: recorder)

        headers = _login(web_client)
        _enable(web_client, headers)

        resp = web_client.post(
            "/integrations/silentguard/mitigation/enable-temporary",
            json={"acknowledge": False, "evil": "payload"},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "enable_temporary_mitigation" not in recorder.calls

    def test_disable_without_acknowledge_is_rejected(
        self, web_client, _spawn_disabled, monkeypatch,
    ):
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        recorder = _ProviderRecorder()
        _patch_provider(monkeypatch, lambda: recorder)

        headers = _login(web_client)
        _enable(web_client, headers)

        resp = web_client.post(
            "/integrations/silentguard/mitigation/disable",
            json={"acknowledge": False},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "disable_mitigation" not in recorder.calls

    def test_extra_fields_in_body_are_ignored(
        self, web_client, _spawn_disabled, monkeypatch,
    ):
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        recorder = _ProviderRecorder()
        _patch_provider(monkeypatch, lambda: recorder)

        headers = _login(web_client)
        _enable(web_client, headers)

        resp = web_client.post(
            "/integrations/silentguard/mitigation/enable-temporary",
            # Extra fields the model does not declare must be ignored;
            # the only honoured field is ``acknowledge``.
            json={
                "acknowledge": True,
                "mode": "permanent_block",
                "command": "rm -rf /",
            },
            headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        # The provider was called exactly once for the mitigation
        # action (and once for status, but the action itself is the
        # important assertion).
        assert "enable_temporary_mitigation" in recorder.calls


# ── Read endpoint ───────────────────────────────────────────────────


class TestGetMitigationState:
    def test_returns_mitigation_state_when_available(
        self, web_client, _spawn_disabled, monkeypatch,
    ):
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        recorder = _ProviderRecorder(
            mitigation_state=MitigationState(
                mode=MODE_DETECTION_ONLY, active=False,
            ),
        )
        _patch_provider(monkeypatch, lambda: recorder)

        headers = _login(web_client)
        _enable(web_client, headers)

        resp = web_client.get(
            "/integrations/silentguard/mitigation", headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == MITIGATION_KEYS
        assert body["available"] is True
        assert body["ok"] is True
        assert body["state"] is not None
        assert set(body["state"].keys()) == STATE_KEYS
        assert body["state"]["mode"] == MODE_DETECTION_ONLY
        assert body["state"]["active"] is False
        # The read endpoint must never invoke a write method.
        assert "enable_temporary_mitigation" not in recorder.calls
        assert "disable_mitigation" not in recorder.calls

    def test_returns_unavailable_when_provider_returns_none(
        self, web_client, _spawn_disabled, monkeypatch,
    ):
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        recorder = _ProviderRecorder(mitigation_state=None)
        _patch_provider(monkeypatch, lambda: recorder)

        headers = _login(web_client)
        _enable(web_client, headers)

        resp = web_client.get(
            "/integrations/silentguard/mitigation", headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["available"] is False
        assert body["state"] is None
        # Calm message — never a raw exception.
        assert isinstance(body["message"], str) and body["message"]

    def test_swallows_provider_exception(
        self, web_client, _spawn_disabled, monkeypatch,
    ):
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")

        class _ExplodingProvider(_ProviderRecorder):
            def get_mitigation_state(self):
                raise RuntimeError("boom")

        recorder = _ExplodingProvider()
        _patch_provider(monkeypatch, lambda: recorder)

        headers = _login(web_client)
        _enable(web_client, headers)

        resp = web_client.get(
            "/integrations/silentguard/mitigation", headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["available"] is False


# ── Enable / disable happy paths ────────────────────────────────────


class TestEnableTemporary:
    def test_acknowledged_call_relays_to_provider(
        self, web_client, _spawn_disabled, monkeypatch,
    ):
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        recorder = _ProviderRecorder()
        _patch_provider(monkeypatch, lambda: recorder)

        headers = _login(web_client)
        _enable(web_client, headers)

        resp = web_client.post(
            "/integrations/silentguard/mitigation/enable-temporary",
            json={"acknowledge": True},
            headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["state"]["mode"] == MODE_TEMPORARY_AUTO_BLOCK
        assert recorder.calls.count("enable_temporary_mitigation") == 1

    def test_failed_provider_action_returns_calm_payload(
        self, web_client, _spawn_disabled, monkeypatch,
    ):
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        recorder = _ProviderRecorder(
            enable_result=MitigationActionResult(
                ok=False, state=None,
                message="SilentGuard mitigation is currently unavailable.",
            ),
        )
        _patch_provider(monkeypatch, lambda: recorder)

        headers = _login(web_client)
        _enable(web_client, headers)

        resp = web_client.post(
            "/integrations/silentguard/mitigation/enable-temporary",
            json={"acknowledge": True},
            headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["state"] is None
        # Must surface the calm provider message verbatim — no raw
        # transport text is in there because the provider already
        # sanitised it.
        assert isinstance(body["message"], str) and body["message"]


class TestDisable:
    def test_acknowledged_call_relays_to_provider(
        self, web_client, _spawn_disabled, monkeypatch,
    ):
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        recorder = _ProviderRecorder()
        _patch_provider(monkeypatch, lambda: recorder)

        headers = _login(web_client)
        _enable(web_client, headers)

        resp = web_client.post(
            "/integrations/silentguard/mitigation/disable",
            json={"acknowledge": True},
            headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["state"]["mode"] == MODE_DETECTION_ONLY
        assert recorder.calls.count("disable_mitigation") == 1


# ── Read-only summary stays read-only ───────────────────────────────


class TestReadOnlyEndpointsDoNotCallMitigation:
    def test_summary_endpoint_does_not_call_mitigation(
        self, web_client, _spawn_disabled, monkeypatch,
    ):
        # Pin the no-poll guarantee from the roadmap: refreshing the
        # status / summary card never triggers any mitigation call.
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        recorder = _ProviderRecorder(
            mitigation_state=MitigationState(
                mode=MODE_TEMPORARY_AUTO_BLOCK, active=True,
            ),
        )
        _patch_provider(monkeypatch, lambda: recorder)

        headers = _login(web_client)
        _enable(web_client, headers)
        # Drop everything captured by the /enable round-trip — we
        # only care about what /summary triggers.
        recorder.calls.clear()

        resp = web_client.get(
            "/integrations/silentguard/summary", headers=headers,
        )
        assert resp.status_code == 200
        # No mitigation read or write fired off the summary path.
        assert "get_mitigation_state" not in recorder.calls
        assert "enable_temporary_mitigation" not in recorder.calls
        assert "disable_mitigation" not in recorder.calls

    def test_existing_summary_shape_unchanged(
        self, web_client, _spawn_disabled,
    ):
        # Regression: the four-key summary payload must keep its shape
        # so the existing UI keeps painting correctly.
        headers = _login(web_client)
        resp = web_client.get(
            "/integrations/silentguard/summary", headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert {"lifecycle", "counts", "connection_summary", "host_enabled"} == set(
            body.keys()
        )


# ── Per-user isolation ──────────────────────────────────────────────


class TestPerUserIsolation:
    def test_alice_enable_does_not_let_bob_in(
        self, web_client, _spawn_disabled, monkeypatch,
    ):
        # Alice opting in must not let Bob hit the mitigation
        # endpoint without his own opt-in. The instantiation counter
        # asserts the provider never runs for Bob's call.
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")

        instantiated_for_bob: list[int] = []
        alice_calls: list[str] = []

        class _Recorder(_ProviderRecorder):
            def __init__(self, *args, **kwargs):
                # We can't tell which user instantiated us from the
                # provider class; the test sets up the same
                # constructor for both and asserts the count.
                instantiated_for_bob.append(1)
                super().__init__()

            def get_mitigation_state(self):
                alice_calls.append("get_mitigation_state")
                return MitigationState(
                    mode=MODE_DETECTION_ONLY, active=False,
                )

        _patch_provider(monkeypatch, _Recorder)

        alice = _login(web_client, "alice", "pw")
        bob = _login(web_client, "bob", "pw")
        _enable(web_client, alice)

        # Track instantiations *only* during Bob's call.
        instantiated_for_bob.clear()
        bob_resp = web_client.get(
            "/integrations/silentguard/mitigation", headers=bob,
        )
        assert bob_resp.status_code == 200
        bob_body = bob_resp.json()
        assert bob_body["available"] is False
        assert instantiated_for_bob == [], (
            "provider must not be instantiated for a user who hasn't opted in"
        )

        # Alice still gets her real read.
        instantiated_for_bob.clear()
        alice_resp = web_client.get(
            "/integrations/silentguard/mitigation", headers=alice,
        )
        assert alice_resp.status_code == 200
        alice_body = alice_resp.json()
        assert alice_body["available"] is True
        assert alice_body["state"]["mode"] == MODE_DETECTION_ONLY
