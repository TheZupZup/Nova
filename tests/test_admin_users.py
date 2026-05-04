"""
Tests for the admin user-management endpoints (issue #109).

Covers:
  * Admin can list / create / disable / enable users.
  * Admin can change role and reset password.
  * Disabling or password reset bumps token_version, invalidating old JWTs.
  * Disabled users cannot log in.
  * Non-admin (user, restricted) callers receive 403 on every admin route.
  * Admin cannot disable or demote the last active admin.
  * Admin endpoints never expose password_hash.
  * Family controls can be edited only on restricted users.
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
    # `initialize_db` runs the users migration which seeds a default admin
    # ("nova"). Tests in this file create their own admin / user fixtures and
    # need a deterministic users table to assert on.
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM users")
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


@pytest.fixture
def admin_token(db_path, web_client):
    _make_user(db_path, "alice", role=users.ROLE_ADMIN)
    return _login(web_client, "alice")


# ── Core helpers (unit) ─────────────────────────────────────────────────────

class TestUserHelpers:
    def test_list_users_excludes_password_hash(self, db_path):
        _make_user(db_path, "alice", role=users.ROLE_ADMIN)
        _make_user(db_path, "bob")
        with sqlite3.connect(db_path) as conn:
            rows = users.list_users(conn)
        assert len(rows) == 2
        for r in rows:
            assert "password_hash" not in r

    def test_set_disabled_bumps_token_version(self, db_path):
        uid = _make_user(db_path, "bob")
        with sqlite3.connect(db_path) as conn:
            tv0 = users.get_user_by_id(conn, uid)["token_version"]
            users.set_disabled(conn, uid, True)
            row = users.get_user_by_id(conn, uid)
        assert row["disabled_at"] is not None
        assert row["token_version"] == tv0 + 1

    def test_set_disabled_re_enable_clears_disabled_at(self, db_path):
        uid = _make_user(db_path, "bob")
        with sqlite3.connect(db_path) as conn:
            users.set_disabled(conn, uid, True)
            users.set_disabled(conn, uid, False)
            row = users.get_user_by_id(conn, uid)
        assert row["disabled_at"] is None

    def test_set_role_bumps_token_version_on_change(self, db_path):
        uid = _make_user(db_path, "bob")
        with sqlite3.connect(db_path) as conn:
            tv0 = users.get_user_by_id(conn, uid)["token_version"]
            users.set_role(conn, uid, users.ROLE_ADMIN, False)
            row = users.get_user_by_id(conn, uid)
        assert row["role"] == "admin"
        assert row["token_version"] == tv0 + 1

    def test_set_role_no_change_keeps_token_version(self, db_path):
        uid = _make_user(db_path, "bob")
        with sqlite3.connect(db_path) as conn:
            tv0 = users.get_user_by_id(conn, uid)["token_version"]
            users.set_role(conn, uid, users.ROLE_USER, False)
            tv1 = users.get_user_by_id(conn, uid)["token_version"]
        assert tv1 == tv0

    def test_set_role_rejects_admin_restricted(self, db_path):
        uid = _make_user(db_path, "bob")
        with sqlite3.connect(db_path) as conn, pytest.raises(ValueError):
            users.set_role(conn, uid, users.ROLE_ADMIN, True)

    def test_reset_password_bumps_token_and_changes_hash(self, db_path):
        uid = _make_user(db_path, "bob", password="old")
        with sqlite3.connect(db_path) as conn:
            old_hash = users.get_user_by_id(conn, uid)["password_hash"]
            tv0 = users.get_user_by_id(conn, uid)["token_version"]
            users.reset_password(conn, uid, "new")
            row = users.get_user_by_id(conn, uid)
        assert row["password_hash"] != old_hash
        assert row["token_version"] == tv0 + 1

    def test_count_active_admins(self, db_path):
        _make_user(db_path, "a1", role=users.ROLE_ADMIN)
        _make_user(db_path, "a2", role=users.ROLE_ADMIN)
        _make_user(db_path, "u1")
        with sqlite3.connect(db_path) as conn:
            assert users.count_active_admins(conn) == 2
            uid = users.get_user_by_username(conn, "a2")["id"]
            users.set_disabled(conn, uid, True)
            assert users.count_active_admins(conn) == 1


# ── Endpoint: list users ────────────────────────────────────────────────────

class TestListUsers:
    def test_admin_can_list_users(self, db_path, web_client, admin_token):
        _make_user(db_path, "bob")
        _make_user(db_path, "kid", is_restricted=True)
        resp = web_client.get("/admin/users", headers=_h(admin_token))
        assert resp.status_code == 200
        body = resp.json()
        names = [u["username"] for u in body]
        assert names == ["alice", "bob", "kid"]

    def test_list_does_not_expose_password_hash(
        self, db_path, web_client, admin_token
    ):
        _make_user(db_path, "bob")
        resp = web_client.get("/admin/users", headers=_h(admin_token))
        for u in resp.json():
            assert "password_hash" not in u

    def test_list_includes_family_controls_for_restricted(
        self, db_path, web_client, admin_token
    ):
        uid = _make_user(db_path, "kid", is_restricted=True)
        policies.set_family_controls(
            uid, max_prompt_chars=500, daily_message_limit=20, db_path=db_path,
        )
        resp = web_client.get("/admin/users", headers=_h(admin_token))
        kid = next(u for u in resp.json() if u["username"] == "kid")
        assert kid["family_controls"]["max_prompt_chars"] == 500
        assert kid["family_controls"]["daily_message_limit"] == 20

    def test_non_admin_user_forbidden(self, db_path, web_client, admin_token):
        _make_user(db_path, "bob")
        token = _login(web_client, "bob")
        resp = web_client.get("/admin/users", headers=_h(token))
        assert resp.status_code == 403

    def test_restricted_user_forbidden(self, db_path, web_client, admin_token):
        _make_user(db_path, "kid", is_restricted=True)
        token = _login(web_client, "kid")
        resp = web_client.get("/admin/users", headers=_h(token))
        assert resp.status_code == 403

    def test_unauthenticated_blocked(self, web_client):
        resp = web_client.get("/admin/users")
        # FastAPI's HTTPBearer auto-rejects calls without credentials.
        assert resp.status_code in (401, 403)


# ── Endpoint: create user ───────────────────────────────────────────────────

class TestCreateUser:
    def test_admin_can_create_normal_user(
        self, db_path, web_client, admin_token
    ):
        resp = web_client.post(
            "/admin/users",
            headers=_h(admin_token),
            json={"username": "bob", "password": "pw"},
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["username"] == "bob"
        assert data["role"] == "user"
        assert data["is_restricted"] is False
        assert "password_hash" not in data
        assert _login(web_client, "bob") is not None

    def test_admin_can_create_restricted_user(
        self, db_path, web_client, admin_token
    ):
        resp = web_client.post(
            "/admin/users",
            headers=_h(admin_token),
            json={
                "username": "kid",
                "password": "pw",
                "role": "user",
                "is_restricted": True,
            },
        )
        assert resp.status_code == 201
        assert resp.json()["is_restricted"] is True

    def test_admin_can_create_admin_user(
        self, db_path, web_client, admin_token
    ):
        resp = web_client.post(
            "/admin/users",
            headers=_h(admin_token),
            json={"username": "carol", "password": "pw", "role": "admin"},
        )
        assert resp.status_code == 201
        assert resp.json()["role"] == "admin"

    def test_admin_restricted_combo_rejected(
        self, db_path, web_client, admin_token
    ):
        resp = web_client.post(
            "/admin/users",
            headers=_h(admin_token),
            json={
                "username": "x",
                "password": "pw",
                "role": "admin",
                "is_restricted": True,
            },
        )
        assert resp.status_code == 400

    def test_duplicate_username_returns_409(
        self, db_path, web_client, admin_token
    ):
        web_client.post(
            "/admin/users",
            headers=_h(admin_token),
            json={"username": "bob", "password": "pw"},
        )
        resp = web_client.post(
            "/admin/users",
            headers=_h(admin_token),
            json={"username": "bob", "password": "pw"},
        )
        assert resp.status_code == 409

    def test_non_admin_cannot_create(self, db_path, web_client, admin_token):
        _make_user(db_path, "bob")
        token = _login(web_client, "bob")
        resp = web_client.post(
            "/admin/users",
            headers=_h(token),
            json={"username": "eve", "password": "pw"},
        )
        assert resp.status_code == 403


# ── Endpoint: disable / enable ──────────────────────────────────────────────

class TestDisableEnable:
    def test_disable_user_blocks_login(
        self, db_path, web_client, admin_token
    ):
        uid = _make_user(db_path, "bob")
        resp = web_client.post(
            f"/admin/users/{uid}/disable",
            headers=_h(admin_token),
            json={"disabled": True},
        )
        assert resp.status_code == 200
        login = web_client.post(
            "/login", json={"username": "bob", "password": "pw"}
        )
        assert login.status_code == 401

    def test_disable_invalidates_existing_token(
        self, db_path, web_client, admin_token
    ):
        _make_user(db_path, "bob")
        bob_token = _login(web_client, "bob")
        # Token is valid pre-disable
        assert web_client.get("/me", headers=_h(bob_token)).status_code == 200
        with sqlite3.connect(db_path) as conn:
            uid = users.get_user_by_username(conn, "bob")["id"]
        web_client.post(
            f"/admin/users/{uid}/disable",
            headers=_h(admin_token),
            json={"disabled": True},
        )
        assert web_client.get("/me", headers=_h(bob_token)).status_code == 401

    def test_re_enable_allows_login_again(
        self, db_path, web_client, admin_token
    ):
        uid = _make_user(db_path, "bob")
        web_client.post(
            f"/admin/users/{uid}/disable",
            headers=_h(admin_token),
            json={"disabled": True},
        )
        web_client.post(
            f"/admin/users/{uid}/disable",
            headers=_h(admin_token),
            json={"disabled": False},
        )
        login = web_client.post(
            "/login", json={"username": "bob", "password": "pw"}
        )
        assert login.status_code == 200

    def test_cannot_disable_only_active_admin(
        self, db_path, web_client, admin_token
    ):
        with sqlite3.connect(db_path) as conn:
            alice_id = users.get_user_by_username(conn, "alice")["id"]
        # alice is the sole admin
        resp = web_client.post(
            f"/admin/users/{alice_id}/disable",
            headers=_h(admin_token),
            json={"disabled": True},
        )
        assert resp.status_code == 400

    def test_cannot_self_disable(self, db_path, web_client, admin_token):
        with sqlite3.connect(db_path) as conn:
            alice_id = users.get_user_by_username(conn, "alice")["id"]
        # Add a second admin so the "last admin" guard does not trip first.
        _make_user(db_path, "alice2", role=users.ROLE_ADMIN)
        resp = web_client.post(
            f"/admin/users/{alice_id}/disable",
            headers=_h(admin_token),
            json={"disabled": True},
        )
        assert resp.status_code == 400

    def test_can_disable_admin_when_other_active_admin_exists(
        self, db_path, web_client, admin_token
    ):
        uid = _make_user(db_path, "carol", role=users.ROLE_ADMIN)
        resp = web_client.post(
            f"/admin/users/{uid}/disable",
            headers=_h(admin_token),
            json={"disabled": True},
        )
        assert resp.status_code == 200

    def test_disable_unknown_user_returns_404(self, web_client, admin_token):
        resp = web_client.post(
            "/admin/users/9999/disable",
            headers=_h(admin_token),
            json={"disabled": True},
        )
        assert resp.status_code == 404

    def test_non_admin_cannot_disable(
        self, db_path, web_client, admin_token
    ):
        bob_id = _make_user(db_path, "bob")
        _make_user(db_path, "mallory")
        token = _login(web_client, "mallory")
        resp = web_client.post(
            f"/admin/users/{bob_id}/disable",
            headers=_h(token),
            json={"disabled": True},
        )
        assert resp.status_code == 403


# ── Endpoint: change role ───────────────────────────────────────────────────

class TestSetRole:
    def test_admin_can_promote_to_admin(
        self, db_path, web_client, admin_token
    ):
        uid = _make_user(db_path, "bob")
        resp = web_client.post(
            f"/admin/users/{uid}/role",
            headers=_h(admin_token),
            json={"role": "admin"},
        )
        assert resp.status_code == 200
        with sqlite3.connect(db_path) as conn:
            row = users.get_user_by_id(conn, uid)
        assert row["role"] == "admin"

    def test_admin_can_set_restricted(
        self, db_path, web_client, admin_token
    ):
        uid = _make_user(db_path, "bob")
        resp = web_client.post(
            f"/admin/users/{uid}/role",
            headers=_h(admin_token),
            json={"role": "user", "is_restricted": True},
        )
        assert resp.status_code == 200

    def test_role_change_invalidates_existing_token(
        self, db_path, web_client, admin_token
    ):
        _make_user(db_path, "bob")
        bob_token = _login(web_client, "bob")
        assert web_client.get("/me", headers=_h(bob_token)).status_code == 200
        with sqlite3.connect(db_path) as conn:
            uid = users.get_user_by_username(conn, "bob")["id"]
        web_client.post(
            f"/admin/users/{uid}/role",
            headers=_h(admin_token),
            json={"role": "user", "is_restricted": True},
        )
        assert web_client.get("/me", headers=_h(bob_token)).status_code == 401

    def test_cannot_demote_only_admin(
        self, db_path, web_client, admin_token
    ):
        with sqlite3.connect(db_path) as conn:
            alice_id = users.get_user_by_username(conn, "alice")["id"]
        resp = web_client.post(
            f"/admin/users/{alice_id}/role",
            headers=_h(admin_token),
            json={"role": "user"},
        )
        assert resp.status_code == 400

    def test_admin_restricted_combo_rejected(
        self, db_path, web_client, admin_token
    ):
        uid = _make_user(db_path, "bob")
        resp = web_client.post(
            f"/admin/users/{uid}/role",
            headers=_h(admin_token),
            json={"role": "admin", "is_restricted": True},
        )
        assert resp.status_code == 400

    def test_non_admin_cannot_change_role(
        self, db_path, web_client, admin_token
    ):
        uid = _make_user(db_path, "bob")
        _make_user(db_path, "mallory")
        token = _login(web_client, "mallory")
        resp = web_client.post(
            f"/admin/users/{uid}/role",
            headers=_h(token),
            json={"role": "admin"},
        )
        assert resp.status_code == 403


# ── Endpoint: reset password ────────────────────────────────────────────────

class TestResetPassword:
    def test_admin_can_reset_password(
        self, db_path, web_client, admin_token
    ):
        uid = _make_user(db_path, "bob", password="old")
        resp = web_client.post(
            f"/admin/users/{uid}/password",
            headers=_h(admin_token),
            json={"password": "newpw"},
        )
        assert resp.status_code == 200
        # Old password rejected.
        assert web_client.post(
            "/login", json={"username": "bob", "password": "old"}
        ).status_code == 401
        # New password works.
        assert web_client.post(
            "/login", json={"username": "bob", "password": "newpw"}
        ).status_code == 200

    def test_password_reset_invalidates_existing_token(
        self, db_path, web_client, admin_token
    ):
        _make_user(db_path, "bob", password="pw")
        old_token = _login(web_client, "bob")
        with sqlite3.connect(db_path) as conn:
            uid = users.get_user_by_username(conn, "bob")["id"]
        web_client.post(
            f"/admin/users/{uid}/password",
            headers=_h(admin_token),
            json={"password": "another"},
        )
        assert web_client.get("/me", headers=_h(old_token)).status_code == 401

    def test_non_admin_cannot_reset_password(
        self, db_path, web_client, admin_token
    ):
        uid = _make_user(db_path, "bob")
        _make_user(db_path, "mallory")
        token = _login(web_client, "mallory")
        resp = web_client.post(
            f"/admin/users/{uid}/password",
            headers=_h(token),
            json={"password": "newpw"},
        )
        assert resp.status_code == 403


# ── Endpoint: family controls ───────────────────────────────────────────────

class TestFamilyControlsEndpoint:
    def test_admin_can_set_family_controls_on_restricted(
        self, db_path, web_client, admin_token
    ):
        uid = _make_user(db_path, "kid", is_restricted=True)
        resp = web_client.put(
            f"/admin/users/{uid}/family-controls",
            headers=_h(admin_token),
            json={
                "allowed_modes": ["chat", "code"],
                "daily_message_limit": 50,
                "max_prompt_chars": 800,
                "web_search_enabled": True,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["family_controls"]["daily_message_limit"] == 50
        assert sorted(body["family_controls"]["allowed_modes"]) == ["chat", "code"]

    def test_cannot_set_family_controls_on_non_restricted(
        self, db_path, web_client, admin_token
    ):
        uid = _make_user(db_path, "bob")
        resp = web_client.put(
            f"/admin/users/{uid}/family-controls",
            headers=_h(admin_token),
            json={"daily_message_limit": 5},
        )
        assert resp.status_code == 400

    def test_non_admin_cannot_set_family_controls(
        self, db_path, web_client, admin_token
    ):
        uid = _make_user(db_path, "kid", is_restricted=True)
        _make_user(db_path, "mallory")
        token = _login(web_client, "mallory")
        resp = web_client.put(
            f"/admin/users/{uid}/family-controls",
            headers=_h(token),
            json={"daily_message_limit": 5},
        )
        assert resp.status_code == 403


# ── /me endpoint ────────────────────────────────────────────────────────────

class TestWhoami:
    def test_me_returns_role(self, db_path, web_client, admin_token):
        resp = web_client.get("/me", headers=_h(admin_token))
        assert resp.status_code == 200
        body = resp.json()
        assert body["username"] == "alice"
        assert body["role"] == "admin"
        assert body["is_restricted"] is False
