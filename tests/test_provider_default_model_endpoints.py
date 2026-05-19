"""Wire-level tests for the Phase-2 admin default-model endpoints.

Pins the contract of ``GET /admin/provider/models`` and
``POST /admin/provider/default-model``:

* both are admin-only — non-admin and restricted users get 403,
  unauthenticated callers get 401 / 403;
* listing returns the stable shape (models + current/compiled-in
  default) and stays a calm 200 even when the provider is unreachable;
* setting validates the chosen model against the active provider's
  reported list before persisting — a not-installed model is a 400, an
  empty / extra-field body is a 422 (schema), and an unreachable
  provider is a sanitised 400 (no host / transport leak);
* a successful set actually persists (a follow-up list reflects it).

Mirrors ``tests/test_provider_endpoints.py`` fixtures so the two
provider suites stay consistent.
"""

from __future__ import annotations

import contextlib
import sqlite3
import sys
from unittest.mock import MagicMock, patch

import pytest

for _mod in ("ddgs", "ollama", "sgmllib", "feedparser"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from fastapi.testclient import TestClient  # noqa: E402

from core import memory as core_memory, users  # noqa: E402
from core.model_providers import (  # noqa: E402
    MockProvider,
    ModelProviderError,
    reset as reset_registry,
    set_override,
)
from memory import store as natural_store  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_registry():
    """A registry override / cached instance never leaks between tests."""
    reset_registry()
    yield
    reset_registry()


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
    resp = client.post(
        "/login", json={"username": username, "password": password},
    )
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


# ── Auth gating ─────────────────────────────────────────────────────


_ENDPOINTS = [
    ("GET", "/admin/provider/models", None),
    ("POST", "/admin/provider/default-model", {"model": "x"}),
]


class TestDefaultModelEndpointsAuth:
    @pytest.mark.parametrize("method,path,body", _ENDPOINTS)
    def test_non_admin_forbidden(
        self, web_client, user_token, method, path, body,
    ):
        if method == "GET":
            resp = web_client.get(path, headers=_h(user_token))
        else:
            resp = web_client.post(path, headers=_h(user_token), json=body)
        assert resp.status_code == 403

    @pytest.mark.parametrize("method,path,body", _ENDPOINTS)
    def test_restricted_forbidden(
        self, web_client, restricted_token, method, path, body,
    ):
        if method == "GET":
            resp = web_client.get(path, headers=_h(restricted_token))
        else:
            resp = web_client.post(
                path, headers=_h(restricted_token), json=body,
            )
        assert resp.status_code == 403

    @pytest.mark.parametrize("method,path,body", _ENDPOINTS)
    def test_unauthenticated_blocked(self, web_client, method, path, body):
        if method == "GET":
            resp = web_client.get(path)
        else:
            resp = web_client.post(path, json=body)
        assert resp.status_code in (401, 403)


# ── GET /admin/provider/models ──────────────────────────────────────


class TestModelsEndpoint:
    def test_lists_models_and_default(self, web_client, admin_token):
        set_override(MockProvider(healthy=True, models=["m1", "m2"]))
        resp = web_client.get(
            "/admin/provider/models", headers=_h(admin_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["models"] == ["m1", "m2"]
        assert isinstance(body["default_model"], str) and body["default_model"]
        assert isinstance(body["config_default_model"], str)
        assert body["is_custom"] is False

    def test_unreachable_is_calm_200(self, web_client, admin_token):
        set_override(MockProvider(healthy=False))
        resp = web_client.get(
            "/admin/provider/models", headers=_h(admin_token),
        )
        # Unreachable is data, not a server error (mirrors Phase 1).
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is False
        assert body["models"] == []
        assert body["default_model"]


# ── POST /admin/provider/default-model ──────────────────────────────


class TestSetDefaultModelEndpoint:
    def test_valid_model_persists(self, web_client, admin_token):
        set_override(MockProvider(healthy=True, models=["keep", "chosen"]))
        resp = web_client.post(
            "/admin/provider/default-model",
            headers=_h(admin_token),
            json={"model": "chosen"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["default_model"] == "chosen"
        assert body["is_custom"] is True
        # A follow-up list reflects the persisted choice.
        listing = web_client.get(
            "/admin/provider/models", headers=_h(admin_token),
        ).json()
        assert listing["default_model"] == "chosen"
        assert listing["is_custom"] is True

    def test_not_installed_model_is_400(self, web_client, admin_token):
        set_override(MockProvider(healthy=True, models=["only-this"]))
        resp = web_client.post(
            "/admin/provider/default-model",
            headers=_h(admin_token),
            json={"model": "not-installed"},
        )
        assert resp.status_code == 400, resp.text
        # Default is unchanged — nothing was written.
        listing = web_client.get(
            "/admin/provider/models", headers=_h(admin_token),
        ).json()
        assert listing["is_custom"] is False

    def test_empty_model_is_422(self, web_client, admin_token):
        set_override(MockProvider(healthy=True, models=["m"]))
        resp = web_client.post(
            "/admin/provider/default-model",
            headers=_h(admin_token),
            json={"model": ""},
        )
        assert resp.status_code == 422

    def test_extra_field_is_rejected(self, web_client, admin_token):
        set_override(MockProvider(healthy=True, models=["m"]))
        resp = web_client.post(
            "/admin/provider/default-model",
            headers=_h(admin_token),
            json={"model": "m", "provider": "evil"},
        )
        # `extra="forbid"` — a smuggled provider name never reaches the
        # core (which never accepts one anyway).
        assert resp.status_code == 422

    def test_unreachable_provider_is_sanitised_400(
        self, web_client, admin_token,
    ):
        secret_host = "http://bob:hunter2@ollama.internal:11434"
        set_override(
            MockProvider(
                healthy=False,
                error=ModelProviderError(secret_host),
            )
        )
        resp = web_client.post(
            "/admin/provider/default-model",
            headers=_h(admin_token),
            json={"model": "anything"},
        )
        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert "hunter2" not in detail
        assert "ollama.internal" not in detail
        assert "unreachable" in detail.lower()
