"""
Tests for core/auth.py — users-table-backed login + JWT identity (issue #104).

Conversation/memory scoping by user_id is intentionally out of scope; those
behaviours land in #105 / #106.
"""

import contextlib
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient

from core import auth, users


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """A fresh nova.db with the users table created. core.memory.DB_PATH is
    redirected at it so core.auth picks it up automatically."""
    path = str(tmp_path / "nova.db")
    monkeypatch.setattr("core.memory.DB_PATH", path)
    with sqlite3.connect(path) as conn:
        users.create_users_table(conn)
    return path


def _create(db_path, username="admin", password="pw", role=users.ROLE_ADMIN):
    with sqlite3.connect(db_path) as conn:
        return users.create_user(conn, username, password, role=role)


def _disable(db_path, username):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE users SET disabled_at = ? WHERE username = ?",
            ("2026-01-01T00:00:00+00:00", username),
        )


# ── authenticate() ────────────────────────────────────────────────────────────

class TestAuthenticate:
    def test_success_returns_current_user(self, db_path):
        uid = _create(db_path, "admin", "pw")
        user = auth.authenticate("admin", "pw")
        assert user is not None
        assert user.id == uid
        assert user.username == "admin"
        assert user.role == "admin"
        assert user.token_version == 1
        assert user.is_restricted is False

    def test_wrong_password_returns_none(self, db_path):
        _create(db_path, "admin", "pw")
        assert auth.authenticate("admin", "wrong") is None

    def test_unknown_user_returns_none(self, db_path):
        assert auth.authenticate("ghost", "x") is None

    def test_disabled_user_cannot_authenticate(self, db_path):
        _create(db_path, "admin", "pw")
        _disable(db_path, "admin")
        assert auth.authenticate("admin", "pw") is None

    def test_empty_inputs_return_none(self, db_path):
        assert auth.authenticate("", "x") is None
        assert auth.authenticate("admin", "") is None

    def test_role_user_authenticates_as_user(self, db_path):
        _create(db_path, "alice", "pw", role=users.ROLE_USER)
        user = auth.authenticate("alice", "pw")
        assert user is not None
        assert user.role == "user"


# ── create_token() ────────────────────────────────────────────────────────────

class TestCreateToken:
    def test_token_includes_user_id_and_role(self, db_path):
        uid = _create(db_path, "admin", "pw")
        user = auth.authenticate("admin", "pw")
        token = auth.create_token(user)
        payload = jwt.decode(token, auth.SECRET_KEY, algorithms=[auth.JWT_ALGORITHM])
        assert payload["sub"] == str(uid)
        assert payload["username"] == "admin"
        assert payload["role"] == "admin"
        assert payload["tv"] == 1
        assert "exp" in payload
        assert "iat" in payload


# ── verify_token() ────────────────────────────────────────────────────────────

class TestVerifyToken:
    def test_valid_token_returns_payload(self, db_path):
        _create(db_path, "admin", "pw")
        user = auth.authenticate("admin", "pw")
        payload = auth.verify_token(auth.create_token(user))
        assert payload is not None
        assert payload["username"] == "admin"
        assert payload["role"] == "admin"

    def test_malformed_token_returns_none(self):
        assert auth.verify_token("not.a.jwt") is None
        assert auth.verify_token("") is None

    def test_signature_mismatch_returns_none(self):
        token = jwt.encode(
            {
                "sub": "1", "username": "x", "role": "user", "tv": 1,
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            "different-secret",
            algorithm="HS256",
        )
        assert auth.verify_token(token) is None

    def test_expired_token_returns_none(self):
        token = jwt.encode(
            {
                "sub": "1", "username": "x", "role": "user", "tv": 1,
                "exp": datetime.now(timezone.utc) - timedelta(hours=1),
            },
            auth.SECRET_KEY,
            algorithm=auth.JWT_ALGORITHM,
        )
        assert auth.verify_token(token) is None

    def test_payload_missing_required_claim_returns_none(self):
        # Missing "username".
        token = jwt.encode(
            {
                "sub": "1", "role": "user", "tv": 1,
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            auth.SECRET_KEY,
            algorithm=auth.JWT_ALGORITHM,
        )
        assert auth.verify_token(token) is None


# ── load_current_user() ───────────────────────────────────────────────────────

class TestLoadCurrentUser:
    def test_valid_token_returns_user(self, db_path):
        _create(db_path, "admin", "pw")
        token = auth.create_token(auth.authenticate("admin", "pw"))
        loaded = auth.load_current_user(token)
        assert loaded is not None
        assert loaded.username == "admin"
        assert loaded.role == "admin"

    def test_stale_token_version_is_rejected(self, db_path):
        _create(db_path, "admin", "pw")
        token = auth.create_token(auth.authenticate("admin", "pw"))
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE users SET token_version = token_version + 1 "
                "WHERE username = 'admin'"
            )
        assert auth.load_current_user(token) is None

    def test_disabled_user_cannot_use_existing_token(self, db_path):
        _create(db_path, "admin", "pw")
        token = auth.create_token(auth.authenticate("admin", "pw"))
        _disable(db_path, "admin")
        assert auth.load_current_user(token) is None

    def test_invalid_token_returns_none(self, db_path):
        assert auth.load_current_user("not.a.jwt") is None


# ── /login + protected-endpoint integration ──────────────────────────────────

@pytest.fixture
def web_client(db_path, monkeypatch):
    """A TestClient backed by the temp DB, with background jobs suppressed."""
    monkeypatch.setattr("core.memory.DB_PATH", db_path)
    # The login limiter is a module-level singleton keyed by client IP.
    # TestClient always presents itself as "testclient", so successive
    # tests share the same bucket; reset it before each test.
    from core.rate_limiter import _login_limiter
    _login_limiter._store.clear()

    import web
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("web.initialize_db"))
        stack.enter_context(patch("web.learn_from_feeds"))
        stack.enter_context(patch("web.scheduler", MagicMock()))
        with TestClient(web.app, raise_server_exceptions=True) as client:
            yield client


class TestLoginEndpoint:
    def test_login_succeeds_for_default_admin(self, db_path, web_client):
        _create(db_path, "nova", "nova")
        resp = web_client.post(
            "/login", json={"username": "nova", "password": "nova"}
        )
        assert resp.status_code == 200
        token = resp.json()["token"]
        assert token

        payload = jwt.decode(
            token, auth.SECRET_KEY, algorithms=[auth.JWT_ALGORITHM]
        )
        assert payload["username"] == "nova"
        assert payload["role"] == "admin"
        assert "sub" in payload
        assert payload["tv"] == 1

    def test_login_response_has_only_token_field(self, db_path, web_client):
        """Login response shape stays {"token": ...} — no breaking additions."""
        _create(db_path, "nova", "nova")
        resp = web_client.post(
            "/login", json={"username": "nova", "password": "nova"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"token"}

    def test_login_rejects_invalid_password(self, db_path, web_client):
        _create(db_path, "nova", "nova")
        resp = web_client.post(
            "/login", json={"username": "nova", "password": "wrong"}
        )
        assert resp.status_code == 401

    def test_login_rejects_unknown_user(self, db_path, web_client):
        resp = web_client.post(
            "/login", json={"username": "ghost", "password": "x"}
        )
        assert resp.status_code == 401

    def test_login_rejects_disabled_user(self, db_path, web_client):
        _create(db_path, "nova", "nova")
        _disable(db_path, "nova")
        resp = web_client.post(
            "/login", json={"username": "nova", "password": "nova"}
        )
        assert resp.status_code == 401


class TestProtectedEndpoint:
    def test_protected_endpoint_accepts_valid_token(self, db_path, web_client):
        _create(db_path, "nova", "nova")
        # /memories is protected by get_current_user — it must accept the
        # bearer token issued by /login. The endpoint reads from the
        # memories table, which doesn't exist on this temp DB, so we patch
        # the data accessor.
        login = web_client.post(
            "/login", json={"username": "nova", "password": "nova"}
        )
        token = login.json()["token"]

        with patch("web.list_memories", return_value=[]):
            resp = web_client.get(
                "/memories", headers={"Authorization": f"Bearer {token}"}
            )
        assert resp.status_code == 200

    def test_protected_endpoint_rejects_invalid_token(self, db_path, web_client):
        resp = web_client.get(
            "/memories", headers={"Authorization": "Bearer not.a.jwt"}
        )
        assert resp.status_code == 401

    def test_protected_endpoint_rejects_token_after_version_bump(
        self, db_path, web_client
    ):
        _create(db_path, "nova", "nova")
        login = web_client.post(
            "/login", json={"username": "nova", "password": "nova"}
        )
        token = login.json()["token"]

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE users SET token_version = token_version + 1 "
                "WHERE username = 'nova'"
            )

        resp = web_client.get(
            "/memories", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 401

    def test_get_current_user_returns_current_user_object(self, db_path):
        """Direct unit check: the dependency must yield a CurrentUser."""
        from fastapi.security import HTTPAuthorizationCredentials
        import web as web_mod

        _create(db_path, "nova", "nova")
        token = auth.create_token(auth.authenticate("nova", "nova"))
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

        result = web_mod.get_current_user(creds)
        assert isinstance(result, auth.CurrentUser)
        assert result.username == "nova"
        assert result.role == "admin"
