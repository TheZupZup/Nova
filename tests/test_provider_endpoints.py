"""Tests for the admin-only ``/admin/provider/*`` endpoints.

These pin the wire-level contract of the Phase-1 provider settings
surface: who can call, who can't, and the JSON shape the admin UI
renders.

* Non-admin and restricted users get 403; unauthenticated callers get
  401 / 403 (FastAPI's HTTPBearer default).
* ``GET /admin/provider/status`` returns the configured provider, the
  default (always ``ollama``), the selectable providers (test-only
  ``mock`` filtered out), the redacted Ollama host, and warnings — and
  reports an unknown configured provider as a calm 200 + ``error``,
  never a 500.
* ``POST /admin/provider/test-connection`` returns the stable
  ``{ok, provider, detail, models}`` liveness shape with status 200,
  including when the backend is unreachable or unknown.
* The endpoints are read-only: they never mutate the provider and
  Ollama stays the default.
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
    ("GET", "/admin/provider/status", None),
    ("POST", "/admin/provider/test-connection", {}),
]


class TestProviderEndpointsAuth:
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


# ── /admin/provider/status ──────────────────────────────────────────


class TestStatusEndpoint:
    def test_status_shape_defaults_to_ollama(
        self, web_client, admin_token, monkeypatch,
    ):
        monkeypatch.setattr("config.MODEL_PROVIDER", "ollama")
        resp = web_client.get(
            "/admin/provider/status", headers=_h(admin_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["default_provider"] == "ollama"
        assert body["configured_provider"] == "ollama"
        assert body["is_default"] is True
        assert isinstance(body["selectable_providers"], list)
        # MockProvider stays test-only — never advertised as selectable.
        assert "mock" not in body["selectable_providers"]
        assert "mock" in body["test_only_providers"]
        assert "ollama" in body["selectable_providers"]
        assert isinstance(body["warnings"], list)
        assert body["error"] == ""

    def test_status_unknown_provider_is_calm_200(
        self, web_client, admin_token, monkeypatch,
    ):
        # An unknown configured provider must surface as a calm 200 +
        # error/warning, never a 500.
        monkeypatch.setattr("config.MODEL_PROVIDER", "does-not-exist")
        resp = web_client.get(
            "/admin/provider/status", headers=_h(admin_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["active_provider"] is None
        assert body["error"]
        assert any("not registered" in w for w in body["warnings"])

    def test_status_redacts_ollama_host(
        self, web_client, admin_token, monkeypatch,
    ):
        monkeypatch.setattr("config.MODEL_PROVIDER", "ollama")
        monkeypatch.setattr(
            "config.OLLAMA_HOST", "http://bob:hunter2@ollama.lan:11434",
        )
        resp = web_client.get(
            "/admin/provider/status", headers=_h(admin_token),
        )
        assert resp.status_code == 200, resp.text
        assert "hunter2" not in resp.json()["ollama_host"]


# ── /admin/provider/test-connection ─────────────────────────────────


class TestTestConnectionEndpoint:
    def test_reachable_backend_reports_ok(
        self, web_client, admin_token,
    ):
        set_override(MockProvider(healthy=True))
        resp = web_client.post(
            "/admin/provider/test-connection", headers=_h(admin_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["provider"] == "mock"
        assert isinstance(body["models"], list)

    def test_unreachable_backend_reports_clean_failure(
        self, web_client, admin_token,
    ):
        set_override(MockProvider(healthy=False))
        resp = web_client.post(
            "/admin/provider/test-connection", headers=_h(admin_token),
        )
        # Still a 200 — an unreachable backend is data, not a server
        # error (mirrors the maintenance / storage endpoints).
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is False
        assert body["detail"]

    def test_unknown_provider_reports_clean_failure(
        self, web_client, admin_token, monkeypatch,
    ):
        monkeypatch.setattr("config.MODEL_PROVIDER", "does-not-exist")
        resp = web_client.post(
            "/admin/provider/test-connection", headers=_h(admin_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is False
        assert body["provider"] == "does-not-exist"
        assert body["models"] == []
