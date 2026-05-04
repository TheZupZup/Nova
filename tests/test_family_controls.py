"""
Tests for role-based family controls (issue #108).

Covers:
  * Schema: `family_controls` and `user_daily_usage` tables exist after
    migration and the migration is idempotent.
  * Policy resolution: admins / normal users / restricted users with and
    without an explicit family_controls row.
  * `set_family_controls` upsert behaviour.
  * Daily-usage accounting.
  * `/chat` enforcement: allowed modes, web search, prompt length, daily
    message limit, manual memory commands, even when the request is
    crafted to bypass any frontend.
  * `/memories` POST gated by memory_save_enabled.
  * Existing single-user/default-admin behaviour is preserved.
"""

from __future__ import annotations

import contextlib
import sqlite3
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core import memory as core_memory, policies, users
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


# ── Schema ──────────────────────────────────────────────────────────────────

class TestSchema:
    def test_family_controls_table_exists(self, db_path):
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='family_controls'"
            ).fetchone()
        assert row is not None

    def test_user_daily_usage_table_exists(self, db_path):
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='user_daily_usage'"
            ).fetchone()
        assert row is not None

    def test_migration_is_idempotent(self, db_path):
        core_memory.initialize_db()
        core_memory.initialize_db()
        with sqlite3.connect(db_path) as conn:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name IN "
                "('family_controls', 'user_daily_usage')"
            ).fetchall()]
        assert sorted(tables) == ["family_controls", "user_daily_usage"]


# ── Policy resolution ───────────────────────────────────────────────────────

class _Identity:
    """Minimal duck-typed CurrentUser for direct policy tests."""

    def __init__(self, id, role, is_restricted=False):
        self.id = id
        self.role = role
        self.is_restricted = is_restricted


class TestPolicyResolution:
    def test_admin_gets_full_policy(self, db_path):
        admin = _Identity(1, users.ROLE_ADMIN)
        p = policies.get_policy(admin, db_path=db_path)
        assert p is policies.ADMIN_POLICY
        assert p.is_admin
        assert p.web_search_enabled
        assert p.weather_enabled
        assert p.memory_save_enabled
        assert p.daily_message_limit is None
        assert p.max_prompt_chars == 0

    def test_normal_user_gets_permissive_defaults(self, db_path):
        u = _Identity(2, users.ROLE_USER)
        p = policies.get_policy(u, db_path=db_path)
        assert p is policies.DEFAULT_USER_POLICY
        assert p.web_search_enabled
        assert p.memory_save_enabled
        assert p.daily_message_limit is None

    def test_restricted_user_with_no_row_uses_defaults(self, db_path):
        uid = _make_user(db_path, "kid", is_restricted=True)
        u = _Identity(uid, users.ROLE_USER, is_restricted=True)
        p = policies.get_policy(u, db_path=db_path)
        assert p.is_restricted
        assert not p.web_search_enabled
        assert p.weather_enabled
        assert not p.memory_save_enabled
        assert "chat" in p.allowed_modes
        assert "code" not in p.allowed_modes
        assert p.max_prompt_chars == 2000
        assert p.daily_message_limit == 200

    def test_restricted_user_with_row_overrides_defaults(self, db_path):
        uid = _make_user(db_path, "kid", is_restricted=True)
        policies.set_family_controls(
            uid,
            allowed_modes=("chat", "code"),
            web_search_enabled=True,
            memory_save_enabled=True,
            max_prompt_chars=500,
            daily_message_limit=10,
            db_path=db_path,
        )
        u = _Identity(uid, users.ROLE_USER, is_restricted=True)
        p = policies.get_policy(u, db_path=db_path)
        assert p.allowed_modes == frozenset({"chat", "code"})
        assert p.web_search_enabled
        assert p.memory_save_enabled
        assert p.max_prompt_chars == 500
        assert p.daily_message_limit == 10

    def test_set_family_controls_update_preserves_unspecified_fields(self, db_path):
        uid = _make_user(db_path, "kid", is_restricted=True)
        policies.set_family_controls(
            uid, web_search_enabled=True, max_prompt_chars=1000, db_path=db_path,
        )
        # Update only one field; the other must stick.
        policies.set_family_controls(
            uid, max_prompt_chars=750, db_path=db_path,
        )
        u = _Identity(uid, users.ROLE_USER, is_restricted=True)
        p = policies.get_policy(u, db_path=db_path)
        assert p.web_search_enabled is True
        assert p.max_prompt_chars == 750


# ── Daily usage ─────────────────────────────────────────────────────────────

class TestDailyUsage:
    def test_record_message_increments(self, db_path):
        uid = _make_user(db_path, "alice")
        assert policies.today_usage(uid, db_path=db_path) == 0
        assert policies.record_message(uid, db_path=db_path) == 1
        assert policies.record_message(uid, db_path=db_path) == 2
        assert policies.today_usage(uid, db_path=db_path) == 2

    def test_enforce_daily_limit_blocks_after_limit(self, db_path):
        uid = _make_user(db_path, "kid", is_restricted=True)
        policies.set_family_controls(
            uid, daily_message_limit=2, db_path=db_path,
        )
        u = _Identity(uid, users.ROLE_USER, is_restricted=True)
        policy = policies.get_policy(u, db_path=db_path)

        assert policies.enforce_daily_limit(policy, uid, db_path=db_path) is None
        assert policies.enforce_daily_limit(policy, uid, db_path=db_path) is None
        denial = policies.enforce_daily_limit(policy, uid, db_path=db_path)
        assert denial is not None
        assert denial.status_code == 429
        assert "Retry-After" in denial.headers

    def test_no_limit_for_admin(self, db_path):
        # Admin policy has daily_message_limit=None, so enforcement is a no-op.
        admin = _Identity(1, users.ROLE_ADMIN)
        p = policies.get_policy(admin, db_path=db_path)
        for _ in range(10):
            assert policies.enforce_daily_limit(p, 1, db_path=db_path) is None


# ── /chat enforcement (HTTP) ────────────────────────────────────────────────

def _restricted_user(db_path, username, **overrides):
    uid = _make_user(db_path, username, is_restricted=True)
    if overrides:
        policies.set_family_controls(uid, db_path=db_path, **overrides)
    return uid


class TestChatPolicyEnforcement:
    def test_admin_can_use_all_features(self, db_path, web_client):
        _make_user(db_path, "boss", role=users.ROLE_ADMIN)
        token = _login(web_client, "boss")
        with patch("web.chat", return_value=("ok", "stub")) as m:
            resp = web_client.post(
                "/chat",
                json={"message": "hello", "mode": "deep", "search": True},
                headers=_h(token),
            )
        assert resp.status_code == 200
        # Admin's policy: web_search+weather+memory_save all on.
        passed_policy = m.call_args.kwargs["policy"]
        assert passed_policy.is_admin
        assert passed_policy.web_search_enabled
        assert passed_policy.memory_save_enabled

    def test_normal_user_keeps_default_features(self, db_path, web_client):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        with patch("web.chat", return_value=("ok", "stub")) as m:
            resp = web_client.post(
                "/chat",
                json={"message": "hi", "mode": "code", "search": True},
                headers=_h(token),
            )
        assert resp.status_code == 200
        passed_policy = m.call_args.kwargs["policy"]
        assert not passed_policy.is_admin
        assert passed_policy.web_search_enabled
        assert passed_policy.memory_save_enabled

    def test_restricted_user_cannot_use_disallowed_mode(self, db_path, web_client):
        _restricted_user(db_path, "kid")  # default allowed_modes = {"chat"}
        token = _login(web_client, "kid")
        resp = web_client.post(
            "/chat",
            json={"message": "hi", "mode": "code"},
            headers=_h(token),
        )
        assert resp.status_code == 403
        assert "Mode" in resp.json()["detail"]

    def test_restricted_user_cannot_force_web_search(self, db_path, web_client):
        _restricted_user(db_path, "kid")  # default web_search_enabled = False
        token = _login(web_client, "kid")
        resp = web_client.post(
            "/chat",
            json={"message": "hi", "mode": "chat", "search": True},
            headers=_h(token),
        )
        assert resp.status_code == 403
        assert "search" in resp.json()["detail"].lower()

    def test_restricted_user_can_chat_when_within_policy(self, db_path, web_client):
        _restricted_user(db_path, "kid")
        token = _login(web_client, "kid")
        with patch("web.chat", return_value=("ok", "stub")) as m:
            resp = web_client.post(
                "/chat",
                json={"message": "hi", "mode": "chat", "search": False},
                headers=_h(token),
            )
        assert resp.status_code == 200
        passed_policy = m.call_args.kwargs["policy"]
        assert passed_policy.is_restricted
        assert not passed_policy.web_search_enabled
        assert not passed_policy.memory_save_enabled

    def test_prompt_length_limit_is_enforced(self, db_path, web_client):
        _restricted_user(db_path, "kid", max_prompt_chars=20)
        token = _login(web_client, "kid")
        resp = web_client.post(
            "/chat",
            json={"message": "x" * 50, "mode": "chat"},
            headers=_h(token),
        )
        assert resp.status_code == 403
        assert "20" in resp.json()["detail"]

    def test_daily_message_limit_is_enforced(self, db_path, web_client):
        _restricted_user(db_path, "kid", daily_message_limit=2)
        token = _login(web_client, "kid")
        with patch("web.chat", return_value=("ok", "stub")):
            r1 = web_client.post(
                "/chat", json={"message": "a", "mode": "chat"}, headers=_h(token),
            )
            r2 = web_client.post(
                "/chat", json={"message": "b", "mode": "chat"}, headers=_h(token),
            )
            r3 = web_client.post(
                "/chat", json={"message": "c", "mode": "chat"}, headers=_h(token),
            )
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r3.status_code == 429
        assert r3.headers.get("retry-after") is not None

    def test_manual_memory_command_blocked_when_memory_save_disabled(
        self, db_path, web_client,
    ):
        _restricted_user(db_path, "kid")  # default memory_save_enabled = False
        token = _login(web_client, "kid")
        resp = web_client.post(
            "/chat",
            json={"message": "Souviens-toi: nothing", "mode": "chat"},
            headers=_h(token),
        )
        assert resp.status_code == 403
        assert "memory" in resp.json()["detail"].lower()

    def test_manual_memory_command_allowed_when_memory_save_enabled(
        self, db_path, web_client,
    ):
        _restricted_user(db_path, "kid", memory_save_enabled=True)
        token = _login(web_client, "kid")
        resp = web_client.post(
            "/chat",
            json={"message": "Souviens-toi: ma couleur préférée", "mode": "chat"},
            headers=_h(token),
        )
        assert resp.status_code == 200
        assert "Souvenir" in resp.json()["response"]

    def test_backend_rejects_forbidden_request_even_when_crafted(
        self, db_path, web_client,
    ):
        """A manually crafted JSON request bypassing a hypothetical UI is still refused."""
        _restricted_user(db_path, "kid")
        token = _login(web_client, "kid")
        # Frontend would not allow these — the backend must refuse anyway.
        attempts = [
            {"message": "x", "mode": "deep"},
            {"message": "x", "mode": "chat", "search": True},
        ]
        for body in attempts:
            resp = web_client.post("/chat", json=body, headers=_h(token))
            assert resp.status_code == 403, body


class TestMemoryEndpointPolicy:
    def test_memory_post_blocked_when_memory_save_disabled(
        self, db_path, web_client,
    ):
        _restricted_user(db_path, "kid")
        token = _login(web_client, "kid")
        resp = web_client.post(
            "/memories",
            json={"category": "x", "content": "y"},
            headers=_h(token),
        )
        assert resp.status_code == 403

    def test_memory_post_allowed_for_normal_user(self, db_path, web_client):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        resp = web_client.post(
            "/memories",
            json={"category": "x", "content": "y"},
            headers=_h(token),
        )
        assert resp.status_code == 200


# ── Default admin / single-user behaviour preserved ─────────────────────────

class TestSingleUserBehaviorPreserved:
    def test_default_admin_has_unrestricted_policy(self, db_path):
        admin_id = users.get_legacy_admin_id(db_path)
        assert admin_id is not None
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT role, is_restricted FROM users WHERE id = ?",
                (admin_id,),
            ).fetchone()
        assert row[0] == "admin"
        assert row[1] == 0
        admin = _Identity(admin_id, "admin")
        p = policies.get_policy(admin, db_path=db_path)
        assert p is policies.ADMIN_POLICY

    def test_default_admin_chat_unrestricted(self, db_path, web_client):
        # Logging in as the seeded admin still yields full feature access.
        token = _login(web_client, "nova", "nova")
        with patch("web.chat", return_value=("ok", "stub")) as m:
            resp = web_client.post(
                "/chat",
                json={"message": "y", "mode": "deep", "search": True},
                headers=_h(token),
            )
        assert resp.status_code == 200
        assert m.call_args.kwargs["policy"].is_admin
