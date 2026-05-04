"""
Tests for per-user / per-role model and mode access controls (issue #112).

Covers:
  * Schema: `role_model_access` and `user_model_access` tables exist
    after migration and the migration is idempotent.
  * Effective access resolution for admin / normal / restricted users
    with and without role/user overrides.
  * Per-role overrides can deny a mode.
  * Per-user overrides can further restrict mode/model access.
  * Overrides cannot grant access the base policy refuses (intersect
    semantics — the family-controls invariants stay intact).
  * Disabled registry models are filtered out of non-admin access.
  * /chat returns 403 for disallowed modes / models, even when crafted
    to bypass any frontend.
  * /me exposes available modes with friendly labels and never raw
    model names for non-admin callers.
  * No Ollama pull is triggered by anything in this module.
  * Existing default-admin behaviour is preserved.
"""

from __future__ import annotations

import contextlib
import sqlite3
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core import (
    memory as core_memory,
    model_access,
    users,
)
from memory import store as natural_store


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "nova.db")
    monkeypatch.setattr(core_memory, "DB_PATH", path)
    monkeypatch.setattr(natural_store, "DB_PATH", path)
    core_memory.initialize_db()
    return path


def _make_user(
    db_path,
    username,
    password="pw",
    role=users.ROLE_USER,
    is_restricted=False,
):
    with sqlite3.connect(db_path) as conn:
        return users.create_user(
            conn, username, password, role=role, is_restricted=is_restricted
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


class _Identity:
    """Minimal duck-typed CurrentUser for direct module tests."""

    def __init__(self, id, role, is_restricted=False):
        self.id = id
        self.role = role
        self.is_restricted = is_restricted


def _set_model_enabled(db_path, model_name, enabled):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE model_registry SET enabled = ? WHERE model_name = ?",
            (1 if enabled else 0, model_name),
        )


# ── Schema ──────────────────────────────────────────────────────────────────


class TestSchema:
    def test_role_model_access_table_exists(self, db_path):
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='role_model_access'"
            ).fetchone()
        assert row is not None

    def test_user_model_access_table_exists(self, db_path):
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='user_model_access'"
            ).fetchone()
        assert row is not None

    def test_migration_is_idempotent(self, db_path):
        core_memory.initialize_db()
        core_memory.initialize_db()
        with sqlite3.connect(db_path) as conn:
            tables = sorted(
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name IN "
                    "('role_model_access', 'user_model_access')"
                ).fetchall()
            )
        assert tables == ["role_model_access", "user_model_access"]


# ── Effective access resolution ─────────────────────────────────────────────


class TestEffectiveAccess:
    def test_admin_has_full_access(self, db_path):
        admin = _Identity(1, users.ROLE_ADMIN)
        access = model_access.get_effective_access(admin, db_path=db_path)
        assert access.is_admin
        # Admin sees all modes.
        for m in ("auto", "chat", "code", "deep"):
            assert access.mode_allowed(m)
        # Admin sees all enabled registry models. The seed inserts the
        # four config.MODELS purposes (router/default/code/advanced).
        assert access.allowed_models  # non-empty
        from config import MODELS
        for raw in MODELS.values():
            assert access.model_allowed(raw)

    def test_admin_can_use_disabled_models(self, db_path):
        # Admin must keep access to a disabled registry row — disabling
        # is admin-facing and does not lock the admin out.
        from config import MODELS
        target = MODELS["code"]
        _set_model_enabled(db_path, target, False)
        admin = _Identity(1, users.ROLE_ADMIN)
        access = model_access.get_effective_access(admin, db_path=db_path)
        assert access.model_allowed(target)

    def test_normal_user_default_is_all_modes(self, db_path):
        u = _Identity(2, users.ROLE_USER)
        access = model_access.get_effective_access(u, db_path=db_path)
        assert not access.is_admin
        assert access.allowed_modes == frozenset({"auto", "chat", "code", "deep"})

    def test_normal_user_default_has_all_enabled_models(self, db_path):
        u = _Identity(2, users.ROLE_USER)
        access = model_access.get_effective_access(u, db_path=db_path)
        from config import MODELS
        for raw in MODELS.values():
            assert access.model_allowed(raw)

    def test_normal_user_does_not_see_disabled_models(self, db_path):
        from config import MODELS
        target = MODELS["code"]
        _set_model_enabled(db_path, target, False)
        u = _Identity(2, users.ROLE_USER)
        access = model_access.get_effective_access(u, db_path=db_path)
        assert not access.model_allowed(target)

    def test_restricted_user_default_is_chat_only(self, db_path):
        uid = _make_user(db_path, "kid", is_restricted=True)
        u = _Identity(uid, users.ROLE_USER, is_restricted=True)
        access = model_access.get_effective_access(u, db_path=db_path)
        assert access.allowed_modes == frozenset({"chat"})
        assert not access.mode_allowed("code")
        assert not access.mode_allowed("deep")
        assert not access.mode_allowed("auto")


class TestRoleOverrides:
    def test_role_override_can_deny_a_mode(self, db_path):
        # Take "deep" away from non-restricted users at the role level.
        model_access.set_role_access(
            users.ROLE_USER,
            False,
            allowed_modes={"chat", "code", "auto"},
            db_path=db_path,
        )
        u = _Identity(2, users.ROLE_USER)
        access = model_access.get_effective_access(u, db_path=db_path)
        assert access.mode_allowed("chat")
        assert access.mode_allowed("code")
        assert not access.mode_allowed("deep")

    def test_role_override_can_allow_only_a_subset(self, db_path):
        model_access.set_role_access(
            users.ROLE_USER,
            False,
            allowed_modes={"chat"},
            db_path=db_path,
        )
        u = _Identity(2, users.ROLE_USER)
        access = model_access.get_effective_access(u, db_path=db_path)
        assert access.allowed_modes == frozenset({"chat"})

    def test_role_override_restricts_models(self, db_path):
        from config import MODELS
        keep = MODELS["default"]
        model_access.set_role_access(
            users.ROLE_USER,
            False,
            allowed_models={keep},
            db_path=db_path,
        )
        u = _Identity(2, users.ROLE_USER)
        access = model_access.get_effective_access(u, db_path=db_path)
        assert access.model_allowed(keep)
        assert not access.model_allowed(MODELS["code"])

    def test_role_override_can_target_restricted_role(self, db_path):
        # Even restricted users obey role overrides on top of family
        # controls. Default is just {"chat"}; an override that adds
        # "code" cannot grant it because the base policy refuses it.
        uid = _make_user(db_path, "kid", is_restricted=True)
        model_access.set_role_access(
            users.ROLE_USER,
            True,
            allowed_modes={"chat", "code"},
            db_path=db_path,
        )
        u = _Identity(uid, users.ROLE_USER, is_restricted=True)
        access = model_access.get_effective_access(u, db_path=db_path)
        # `code` is intersected away by the base policy; `chat` survives.
        assert access.allowed_modes == frozenset({"chat"})

    def test_clear_role_override_restores_defaults(self, db_path):
        model_access.set_role_access(
            users.ROLE_USER, False, allowed_modes={"chat"}, db_path=db_path
        )
        model_access.clear_role_access(
            users.ROLE_USER, False, db_path=db_path
        )
        u = _Identity(2, users.ROLE_USER)
        access = model_access.get_effective_access(u, db_path=db_path)
        assert access.allowed_modes == frozenset(
            {"auto", "chat", "code", "deep"}
        )

    def test_get_role_access_round_trips(self, db_path):
        model_access.set_role_access(
            users.ROLE_USER,
            False,
            allowed_modes={"chat", "code"},
            allowed_models={"gemma4"},
            db_path=db_path,
        )
        got = model_access.get_role_access(
            users.ROLE_USER, False, db_path=db_path
        )
        assert got is not None
        assert got["allowed_modes"] == ["chat", "code"]
        assert got["allowed_models"] == ["gemma4"]

    def test_admin_role_override_does_not_lock_admin_out(self, db_path):
        # Even if an admin row gets created, admins keep full access.
        model_access.set_role_access(
            users.ROLE_ADMIN,
            False,
            allowed_modes={"chat"},
            db_path=db_path,
        )
        admin = _Identity(1, users.ROLE_ADMIN)
        access = model_access.get_effective_access(admin, db_path=db_path)
        assert access.is_admin
        assert access.mode_allowed("deep")


class TestUserOverrides:
    def test_user_override_can_deny_a_mode(self, db_path):
        uid = _make_user(db_path, "alice")
        model_access.set_user_access(
            uid, allowed_modes={"chat", "auto"}, db_path=db_path
        )
        u = _Identity(uid, users.ROLE_USER)
        access = model_access.get_effective_access(u, db_path=db_path)
        assert access.mode_allowed("chat")
        assert not access.mode_allowed("code")
        assert not access.mode_allowed("deep")

    def test_user_override_intersects_with_role_override(self, db_path):
        uid = _make_user(db_path, "alice")
        model_access.set_role_access(
            users.ROLE_USER, False,
            allowed_modes={"chat", "code"},
            db_path=db_path,
        )
        # User override re-allows "deep" at the user level — but the
        # role row already removed it, and overrides only intersect.
        model_access.set_user_access(
            uid, allowed_modes={"chat", "code", "deep"}, db_path=db_path
        )
        u = _Identity(uid, users.ROLE_USER)
        access = model_access.get_effective_access(u, db_path=db_path)
        assert access.allowed_modes == frozenset({"chat", "code"})

    def test_user_override_cannot_grant_beyond_policy(self, db_path):
        # A restricted user cannot escape their family-controls modes
        # via a per-user override row.
        uid = _make_user(db_path, "kid", is_restricted=True)
        model_access.set_user_access(
            uid,
            allowed_modes={"chat", "code", "deep"},
            db_path=db_path,
        )
        u = _Identity(uid, users.ROLE_USER, is_restricted=True)
        access = model_access.get_effective_access(u, db_path=db_path)
        assert access.allowed_modes == frozenset({"chat"})

    def test_user_override_restricts_models(self, db_path):
        from config import MODELS
        uid = _make_user(db_path, "alice")
        model_access.set_user_access(
            uid, allowed_models={MODELS["default"]}, db_path=db_path
        )
        u = _Identity(uid, users.ROLE_USER)
        access = model_access.get_effective_access(u, db_path=db_path)
        assert access.model_allowed(MODELS["default"])
        assert not access.model_allowed(MODELS["code"])

    def test_clear_user_override(self, db_path):
        uid = _make_user(db_path, "alice")
        model_access.set_user_access(
            uid, allowed_modes={"chat"}, db_path=db_path
        )
        model_access.clear_user_access(uid, db_path=db_path)
        u = _Identity(uid, users.ROLE_USER)
        access = model_access.get_effective_access(u, db_path=db_path)
        assert access.allowed_modes == frozenset(
            {"auto", "chat", "code", "deep"}
        )

    def test_admin_user_override_is_ignored(self, db_path):
        # A persisted user-row for an admin must not strip admin power.
        admin_id = users.get_legacy_admin_id(db_path)
        model_access.set_user_access(
            admin_id, allowed_modes={"chat"}, db_path=db_path
        )
        admin = _Identity(admin_id, users.ROLE_ADMIN)
        access = model_access.get_effective_access(admin, db_path=db_path)
        assert access.is_admin
        assert access.mode_allowed("deep")


# ── Friendly mode view (/me) ────────────────────────────────────────────────


class TestAvailableModesFor:
    def test_admin_gets_full_label_list(self, db_path):
        admin = _Identity(1, users.ROLE_ADMIN)
        modes = model_access.available_modes_for(admin, db_path=db_path)
        as_set = {entry["mode"] for entry in modes}
        assert as_set == {"auto", "chat", "code", "deep"}
        # Friendly labels are present and stable.
        labels = {entry["mode"]: entry["label"] for entry in modes}
        assert labels["chat"] == "Chat"
        assert labels["deep"] == "Deep"

    def test_restricted_user_only_sees_chat(self, db_path):
        uid = _make_user(db_path, "kid", is_restricted=True)
        u = _Identity(uid, users.ROLE_USER, is_restricted=True)
        modes = model_access.available_modes_for(u, db_path=db_path)
        as_list = [entry["mode"] for entry in modes]
        assert as_list == ["chat"]

    def test_normal_user_with_role_override(self, db_path):
        model_access.set_role_access(
            users.ROLE_USER, False,
            allowed_modes={"chat", "code"},
            db_path=db_path,
        )
        u = _Identity(2, users.ROLE_USER)
        modes = model_access.available_modes_for(u, db_path=db_path)
        as_list = [entry["mode"] for entry in modes]
        # Order is the stable user-facing one (chat, auto, code, deep).
        assert as_list == ["chat", "code"]

    def test_entries_never_carry_raw_model_names(self, db_path):
        u = _Identity(2, users.ROLE_USER)
        modes = model_access.available_modes_for(u, db_path=db_path)
        assert all(set(entry.keys()) == {"mode", "label"} for entry in modes)


# ── /chat enforcement (HTTP) ────────────────────────────────────────────────


class TestChatEnforcement:
    def test_admin_can_use_all_modes(self, db_path, web_client):
        _make_user(db_path, "boss", role=users.ROLE_ADMIN)
        token = _login(web_client, "boss")
        with patch("web.chat", return_value=("ok", "stub")):
            for mode in ("chat", "code", "deep", "auto"):
                resp = web_client.post(
                    "/chat",
                    json={"message": "hi", "mode": mode},
                    headers=_h(token),
                )
                assert resp.status_code == 200, (mode, resp.text)

    def test_normal_user_can_use_default_modes(self, db_path, web_client):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        with patch("web.chat", return_value=("ok", "stub")):
            for mode in ("chat", "code", "deep", "auto"):
                resp = web_client.post(
                    "/chat",
                    json={"message": "hi", "mode": mode},
                    headers=_h(token),
                )
                assert resp.status_code == 200, (mode, resp.text)

    def test_restricted_user_cannot_use_disallowed_mode(
        self, db_path, web_client,
    ):
        _make_user(db_path, "kid", is_restricted=True)
        token = _login(web_client, "kid")
        for body in (
            {"message": "x", "mode": "code"},
            {"message": "x", "mode": "deep"},
        ):
            resp = web_client.post("/chat", json=body, headers=_h(token))
            assert resp.status_code == 403, body
            # Detail does NOT contain raw model names (privacy).
            for raw in ("gemma4", "deepseek-coder-v2", "qwen2.5"):
                assert raw not in resp.json()["detail"]

    def test_role_override_enforced_in_chat(self, db_path, web_client):
        # Role-level: take "deep" away from normal users.
        _make_user(db_path, "alice")
        model_access.set_role_access(
            users.ROLE_USER, False,
            allowed_modes={"chat", "code", "auto"},
            db_path=db_path,
        )
        token = _login(web_client, "alice")
        resp = web_client.post(
            "/chat",
            json={"message": "x", "mode": "deep"},
            headers=_h(token),
        )
        assert resp.status_code == 403

    def test_user_override_enforced_in_chat(self, db_path, web_client):
        uid = _make_user(db_path, "alice")
        model_access.set_user_access(
            uid, allowed_modes={"chat"}, db_path=db_path,
        )
        token = _login(web_client, "alice")
        resp = web_client.post(
            "/chat",
            json={"message": "x", "mode": "code"},
            headers=_h(token),
        )
        assert resp.status_code == 403

    def test_crafted_request_for_disallowed_mode_is_refused(
        self, db_path, web_client,
    ):
        # Mirrors the family-controls "crafted request" coverage so a
        # non-UI client cannot bypass the model-access layer either.
        _make_user(db_path, "alice")
        model_access.set_user_access(
            _make_user(db_path, "child"),
            allowed_modes={"chat"},
            db_path=db_path,
        )
        token = _login(web_client, "child")
        for body in (
            {"message": "x", "mode": "deep"},
            {"message": "x", "mode": "code"},
        ):
            resp = web_client.post("/chat", json=body, headers=_h(token))
            assert resp.status_code == 403

    def test_disallowed_model_via_nova_assistant_is_refused(
        self, db_path, web_client,
    ):
        # A user who has the nova-assistant override turned on can pick
        # any model_name in their settings. /chat must still refuse a
        # raw model that is not in their allowed_models set.
        from core.settings import save_user_setting
        from config import MODELS
        uid = _make_user(db_path, "alice")
        save_user_setting(uid, "nova_model_enabled", "true")
        save_user_setting(uid, "nova_model_name", MODELS["code"])
        # Pin alice to gemma4 only — code model becomes off-limits.
        model_access.set_user_access(
            uid, allowed_models={MODELS["default"]}, db_path=db_path,
        )
        token = _login(web_client, "alice")
        resp = web_client.post(
            "/chat",
            json={"message": "x", "mode": "chat"},
            headers=_h(token),
        )
        assert resp.status_code == 403
        # Privacy: the denial does NOT echo the raw model name back.
        assert MODELS["code"] not in resp.json()["detail"]

    def test_allowed_mode_passes_through(self, db_path, web_client):
        # Sanity: the new check does not break the happy path.
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        with patch("web.chat", return_value=("ok", "stub")) as m:
            resp = web_client.post(
                "/chat",
                json={"message": "hi", "mode": "code"},
                headers=_h(token),
            )
        assert resp.status_code == 200
        assert m.called

    def test_disabled_registry_model_blocks_non_admin(
        self, db_path, web_client,
    ):
        from core.settings import save_user_setting
        from config import MODELS
        uid = _make_user(db_path, "alice")
        # User wants the code model via nova-assistant override.
        save_user_setting(uid, "nova_model_enabled", "true")
        save_user_setting(uid, "nova_model_name", MODELS["code"])
        # Admin disables that model in the registry.
        _set_model_enabled(db_path, MODELS["code"], False)
        token = _login(web_client, "alice")
        resp = web_client.post(
            "/chat",
            json={"message": "x", "mode": "chat"},
            headers=_h(token),
        )
        assert resp.status_code == 403


# ── /me view (HTTP) ─────────────────────────────────────────────────────────


class TestMeEndpoint:
    def test_me_returns_available_modes_for_admin(self, db_path, web_client):
        _make_user(db_path, "boss", role=users.ROLE_ADMIN)
        token = _login(web_client, "boss")
        resp = web_client.get("/me", headers=_h(token))
        assert resp.status_code == 200
        body = resp.json()
        modes = {m["mode"] for m in body["available_modes"]}
        assert modes == {"auto", "chat", "code", "deep"}

    def test_me_returns_restricted_modes_for_child_user(
        self, db_path, web_client,
    ):
        _make_user(db_path, "kid", is_restricted=True)
        token = _login(web_client, "kid")
        resp = web_client.get("/me", headers=_h(token))
        body = resp.json()
        modes = [m["mode"] for m in body["available_modes"]]
        assert modes == ["chat"]

    def test_me_does_not_expose_raw_model_names(self, db_path, web_client):
        from config import MODELS
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        resp = web_client.get("/me", headers=_h(token))
        body = resp.json()
        # No raw model name appears anywhere in the payload.
        text = repr(body)
        for raw in MODELS.values():
            assert raw not in text
        # Also: only friendly fields are exposed on each entry.
        for entry in body["available_modes"]:
            assert set(entry.keys()) == {"mode", "label"}

    def test_me_reflects_role_override(self, db_path, web_client):
        model_access.set_role_access(
            users.ROLE_USER, False,
            allowed_modes={"chat"},
            db_path=db_path,
        )
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        resp = web_client.get("/me", headers=_h(token))
        modes = [m["mode"] for m in resp.json()["available_modes"]]
        assert modes == ["chat"]

    def test_me_reflects_user_override(self, db_path, web_client):
        uid = _make_user(db_path, "alice")
        model_access.set_user_access(
            uid, allowed_modes={"chat", "code"}, db_path=db_path,
        )
        token = _login(web_client, "alice")
        resp = web_client.get("/me", headers=_h(token))
        modes = [m["mode"] for m in resp.json()["available_modes"]]
        # Stable, user-facing order: chat then code.
        assert modes == ["chat", "code"]


# ── Existing routing & no-side-effects ──────────────────────────────────────


class TestExistingRoutingPreserved:
    def test_existing_chat_routing_still_works(self, db_path, web_client):
        # Allowed mode + default user → chat() is invoked with the
        # MODE_MAP-resolved model.
        from config import MODELS
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        with patch("web.chat", return_value=("ok", "stub")) as m:
            resp = web_client.post(
                "/chat",
                json={"message": "hi", "mode": "chat"},
                headers=_h(token),
            )
        assert resp.status_code == 200
        assert m.call_args.kwargs["forced_model"] == MODELS["default"]

    def test_default_admin_still_unrestricted(self, db_path, web_client):
        token = _login(web_client, "nova", "nova")
        with patch("web.chat", return_value=("ok", "stub")):
            for mode in ("chat", "code", "deep"):
                resp = web_client.post(
                    "/chat",
                    json={"message": "hi", "mode": mode},
                    headers=_h(token),
                )
                assert resp.status_code == 200

    def test_no_ollama_pull_subprocess(self, db_path, web_client):
        # The whole module is read/enforce only — no pull side-effects.
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        with patch("subprocess.run") as run, patch(
            "subprocess.Popen"
        ) as popen, patch("web.chat", return_value=("ok", "stub")):
            web_client.post(
                "/chat",
                json={"message": "hi", "mode": "chat"},
                headers=_h(token),
            )
            web_client.get("/me", headers=_h(token))
            model_access.get_effective_access(
                _Identity(1, users.ROLE_ADMIN), db_path=db_path,
            )
        assert not run.called
        assert not popen.called

    def test_no_model_pull_module_used(self, db_path, web_client):
        # And nothing routes through `_model_pulls.request_pull`.
        from core import model_pulls
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        with patch.object(model_pulls, "request_pull") as rp, patch(
            "web.chat", return_value=("ok", "stub")
        ):
            web_client.post(
                "/chat",
                json={"message": "hi", "mode": "chat"},
                headers=_h(token),
            )
            web_client.get("/me", headers=_h(token))
        assert not rp.called
