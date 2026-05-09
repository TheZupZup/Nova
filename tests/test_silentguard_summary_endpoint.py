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
        # The new ``host_enabled`` top-level field still surfaces the
        # honest host-level state so the UI can tell the operator that
        # only the per-user toggle is missing.
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
        assert set(body.keys()) == {
            "lifecycle", "counts", "connection_summary", "host_enabled",
        }
        assert body["lifecycle"]["state"] == STATE_DISABLED
        assert body["lifecycle"]["enabled"] is False
        assert body["counts"] is None
        # ``host_enabled`` reflects the env var, not the per-user
        # toggle — it tells the UI "the host config is on; the user
        # just hasn't flipped their toggle".
        assert body["host_enabled"] is True
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
        # No env var set, so the host-level switch reads as off.
        assert body["host_enabled"] is False


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


# ── Rich /connections/summary surfacing ─────────────────────────────


class TestConnectedWithRichConnectionSummary:
    """The Settings UI calls one endpoint and expects the optional
    ``connection_summary`` field alongside ``counts``. These tests pin
    that wiring without changing the existing ``counts`` shape.
    """

    def test_connection_summary_attached_when_provider_exposes_it(
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

        class _RichProvider:
            def __init__(self, *args, **kwargs):
                pass

            def get_status(self):
                return _available()

            def get_summary_counts(self):
                return {
                    "alerts": 0, "blocked": 0,
                    "trusted": 0, "connections": 5,
                }

            def get_connection_summary(self):
                return {
                    "total": 55, "local": 38, "known": 12, "unknown": 5,
                    "top_processes": [{"name": "firefox", "count": 8}],
                }

        monkeypatch.setattr(lc, "SilentGuardProvider", _ReachableLifecycleProvider)
        monkeypatch.setattr(web_module, "_SilentGuardProvider", _RichProvider)

        headers = _login(web_client)
        _enable_for_alice(web_client, headers)
        resp = web_client.get(
            "/integrations/silentguard/summary", headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["lifecycle"]["state"] == STATE_CONNECTED
        # Existing counts shape is unchanged — back-compat for the UI.
        assert body["counts"] == {
            "alerts": 0, "blocked": 0, "trusted": 0, "connections": 5,
        }
        # The new field carries the rich summary verbatim.
        assert body["connection_summary"] == {
            "total": 55, "local": 38, "known": 12, "unknown": 5,
            "top_processes": [{"name": "firefox", "count": 8}],
        }

    def test_connection_summary_none_when_provider_returns_none(
        self, web_client, _spawn_disabled, monkeypatch,
    ):
        # Older SilentGuard build / file-only fallback: the provider's
        # ``get_connection_summary`` returns None. Endpoint surfaces
        # ``connection_summary: null`` while counts still flow.
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        from core.security import lifecycle as lc
        import web as web_module

        class _ReachableLifecycleProvider:
            def __init__(self, *args, **kwargs):
                pass

            def get_status(self):
                return _available()

        class _NoRichSummaryProvider:
            def __init__(self, *args, **kwargs):
                pass

            def get_status(self):
                return _available()

            def get_summary_counts(self):
                return {
                    "alerts": 0, "blocked": 0,
                    "trusted": 0, "connections": 0,
                }

            def get_connection_summary(self):
                return None

        monkeypatch.setattr(lc, "SilentGuardProvider", _ReachableLifecycleProvider)
        monkeypatch.setattr(web_module, "_SilentGuardProvider", _NoRichSummaryProvider)

        headers = _login(web_client)
        _enable_for_alice(web_client, headers)
        resp = web_client.get(
            "/integrations/silentguard/summary", headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["connection_summary"] is None
        # Counts still come through — the two surfaces are independent.
        assert body["counts"] == {
            "alerts": 0, "blocked": 0, "trusted": 0, "connections": 0,
        }

    def test_connection_summary_none_when_probe_raises(
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

        class _BoomRichProvider:
            def __init__(self, *args, **kwargs):
                pass

            def get_status(self):
                return _available()

            def get_summary_counts(self):
                return {
                    "alerts": 0, "blocked": 0,
                    "trusted": 0, "connections": 0,
                }

            def get_connection_summary(self):
                raise RuntimeError("rich-summary boom")

        monkeypatch.setattr(lc, "SilentGuardProvider", _ReachableLifecycleProvider)
        monkeypatch.setattr(web_module, "_SilentGuardProvider", _BoomRichProvider)

        headers = _login(web_client)
        _enable_for_alice(web_client, headers)
        resp = web_client.get(
            "/integrations/silentguard/summary", headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        # The summary call swallowed the failure, kept the lifecycle
        # honest, and surfaced ``connection_summary: null`` so the UI
        # falls back to the basic counts row.
        assert body["lifecycle"]["state"] == STATE_CONNECTED
        assert body["connection_summary"] is None
        # The ``counts`` probe is independent from the rich-summary
        # probe — a failure in one must not poison the other.
        assert body["counts"] == {
            "alerts": 0, "blocked": 0, "trusted": 0, "connections": 0,
        }


# ── Auth and shape ──────────────────────────────────────────────────


class TestAuthAndShape:
    def test_unauthenticated_call_is_rejected(self, web_client):
        resp = web_client.get("/integrations/silentguard/summary")
        assert resp.status_code in (401, 403)

    def test_response_keys_are_stable(
        self, web_client, _spawn_disabled,
    ):
        # The Settings UI relies on a fixed top-level key set:
        # ``lifecycle`` (the full LifecycleStatus), ``counts`` (the
        # optional summary numbers), and ``host_enabled`` (the
        # operator-level switch the UI uses to distinguish a host-off
        # disabled state from a per-user-off disabled state).
        headers = _login(web_client)
        resp = web_client.get(
            "/integrations/silentguard/summary", headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {
            "lifecycle", "counts", "connection_summary", "host_enabled",
        }
        # Lifecycle must carry the full LifecycleStatus shape so the UI
        # can render any state without checking for missing keys.
        assert set(body["lifecycle"].keys()) == {
            "state", "enabled", "auto_start", "start_mode", "unit", "message",
        }
        # ``host_enabled`` is always a bool (never null / missing) so
        # the UI never has to defend against an undefined value.
        assert isinstance(body["host_enabled"], bool)

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


# ── Real failure mode: env says enabled but UI says disabled ────────
#
# The bug report: an operator sets every host-level env var, sees the
# SilentGuard API responding to ``curl /status``, and still gets
# "SilentGuard integration disabled" in Settings. The previous summary
# response could not tell the UI *why* the integration was disabled —
# was the host config off, or just the per-user toggle? The new
# ``host_enabled`` field plus the per-user opt-in path together let
# the UI render the right calm message and let the user fix it from
# the Settings card alone.


class TestEnvSaysEnabledButUserSeesDisabled:
    def test_env_enabled_per_user_off_surfaces_host_enabled_true(
        self, web_client, _spawn_disabled, monkeypatch,
    ):
        # The exact failure mode from the bug report. ``NOVA_SILENTGUARD_ENABLED``
        # is "true", a SilentGuard API would be reachable, but Alice
        # has not flipped her per-user toggle. The endpoint must:
        #   * still return ``state="disabled"`` (the per-user gate is
        #     authoritative for this user), and
        #   * surface ``host_enabled=True`` so the UI can show "Turn
        #     on SilentGuard in Settings to use it" instead of the
        #     misleading "integration disabled" headline.
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        headers = _login(web_client)
        # NOTE: deliberately do NOT call _enable_for_alice — Alice is
        # the operator who set env vars but hasn't found the UI toggle.
        resp = web_client.get(
            "/integrations/silentguard/summary", headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["lifecycle"]["state"] == STATE_DISABLED
        assert body["host_enabled"] is True
        assert body["counts"] is None

    def test_env_disabled_surfaces_host_enabled_false(
        self, web_client, _spawn_disabled,
    ):
        # The other half of the same disambiguation: when the host
        # config is off, ``host_enabled`` must be False so the UI can
        # tell the user the *server* config needs changing, not just
        # the toggle.
        headers = _login(web_client)
        resp = web_client.get(
            "/integrations/silentguard/summary", headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["lifecycle"]["state"] == STATE_DISABLED
        assert body["host_enabled"] is False
        assert body["counts"] is None

    def test_env_accepts_string_true_lowercase_uppercase_and_aliases(
        self, web_client, _spawn_disabled, monkeypatch,
    ):
        # The bug report flagged boolean parsing as a possible cause.
        # Confirm the lifecycle helper accepts the documented set of
        # truthy values surfaced by every common env-var convention.
        headers = _login(web_client)
        for raw in ("true", "True", "TRUE", "1", "yes", "on"):
            monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", raw)
            resp = web_client.get(
                "/integrations/silentguard/summary", headers=headers,
            )
            assert resp.status_code == 200, raw
            body = resp.json()
            assert body["host_enabled"] is True, (
                f"NOVA_SILENTGUARD_ENABLED={raw!r} did not parse as truthy"
            )

    def test_env_enabled_plus_per_user_toggle_with_reachable_api_reports_connected(
        self, web_client, _spawn_disabled, monkeypatch,
    ):
        # End-to-end happy path the bug report describes: env on, API
        # reachable, user opted in → state="connected" with counts
        # attached, ``host_enabled`` honest at True. This is the path
        # the Settings card paints as "SilentGuard connected in
        # read-only mode".
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")

        from core.security import lifecycle as lc
        import web as web_module

        class _ReachableLifecycleProvider:
            def __init__(self, *args, **kwargs):
                pass

            def get_status(self):
                return _available()

        class _CountingProvider:
            def __init__(self, *args, **kwargs):
                pass

            def get_status(self):
                return _available()

            def get_summary_counts(self):
                return {
                    "alerts": 0,
                    "blocked": 0,
                    "trusted": 0,
                    "connections": 0,
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
        assert body["host_enabled"] is True
        assert body["counts"] == {
            "alerts": 0, "blocked": 0, "trusted": 0, "connections": 0,
        }

    def test_per_user_toggle_can_be_flipped_on_via_settings_endpoint(
        self, web_client, _spawn_disabled, monkeypatch,
    ):
        # The fix for the bug also requires that a user actually has
        # a way to opt in. The /settings endpoint already accepts
        # ``silentguard_enabled``; this test pins that contract end-
        # to-end so the new UI toggle has a backend it can call.
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        headers = _login(web_client)

        # Before opt-in: disabled.
        before = web_client.get(
            "/integrations/silentguard/summary", headers=headers,
        ).json()
        assert before["lifecycle"]["state"] == STATE_DISABLED

        # Flip the per-user toggle via the same JSON endpoint the UI
        # calls. The response is the standard ``{"ok": True}`` ack.
        ack = web_client.post(
            "/settings",
            json={"silentguard_enabled": True},
            headers=headers,
        )
        assert ack.status_code == 200, ack.text

        # Read it back via GET /settings — the UI uses this to render
        # the toggle's initial state on Settings open.
        echoed = web_client.get("/settings", headers=headers).json()
        assert echoed["silentguard_enabled"] is True

        # Now the summary either reports ``connected``/``unavailable``
        # depending on the API reachability stub; the key invariant is
        # that the per-user gate no longer short-circuits to disabled.
        after = web_client.get(
            "/integrations/silentguard/summary", headers=headers,
        ).json()
        assert after["lifecycle"]["state"] != STATE_DISABLED

    def test_host_enabled_field_is_a_bool_in_every_path(
        self, web_client, _spawn_disabled, monkeypatch,
    ):
        # Defensive: the UI guards against ``host_enabled``
        # disappearing or going non-bool. Verify that across the four
        # major code paths (host off + user off, host off + user on,
        # host on + user off, host on + user on) the field is always a
        # plain Python bool — never null, never a string.
        headers = _login(web_client)

        # host off + user off
        body = web_client.get(
            "/integrations/silentguard/summary", headers=headers,
        ).json()
        assert isinstance(body["host_enabled"], bool)
        assert body["host_enabled"] is False

        # host off + user on
        _enable_for_alice(web_client, headers)
        body = web_client.get(
            "/integrations/silentguard/summary", headers=headers,
        ).json()
        assert isinstance(body["host_enabled"], bool)
        assert body["host_enabled"] is False

        # host on + user on
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        body = web_client.get(
            "/integrations/silentguard/summary", headers=headers,
        ).json()
        assert isinstance(body["host_enabled"], bool)
        assert body["host_enabled"] is True

        # host on + user off
        ack = web_client.post(
            "/settings",
            json={"silentguard_enabled": False},
            headers=headers,
        )
        assert ack.status_code == 200
        body = web_client.get(
            "/integrations/silentguard/summary", headers=headers,
        ).json()
        assert isinstance(body["host_enabled"], bool)
        assert body["host_enabled"] is True


class TestHostEnabledHelper:
    """Direct tests for the public ``host_enabled()`` helper.

    The helper is the only sanctioned way to read the host-level
    ``NOVA_SILENTGUARD_ENABLED`` switch outside the lifecycle module.
    The summary endpoint depends on it; pinning the parsing rules here
    catches regressions without having to spin up the full app.
    """

    def test_unset_env_returns_false(self, monkeypatch):
        from core.security import lifecycle as lc
        monkeypatch.delenv("NOVA_SILENTGUARD_ENABLED", raising=False)
        # Also defang the config import path so a stray real .env
        # cannot pollute the assertion.
        import config as config_mod
        monkeypatch.setattr(config_mod, "NOVA_SILENTGUARD_ENABLED", False)
        assert lc.host_enabled() is False

    def test_truthy_strings_resolve_true(self, monkeypatch):
        from core.security import lifecycle as lc
        for raw in ("1", "true", "True", "TRUE", "yes", "YES", "on", "ON"):
            monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", raw)
            assert lc.host_enabled() is True, raw

    def test_falsy_strings_resolve_false(self, monkeypatch):
        from core.security import lifecycle as lc
        for raw in ("0", "false", "no", "off", "", "  ", "maybe"):
            monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", raw)
            assert lc.host_enabled() is False, raw
