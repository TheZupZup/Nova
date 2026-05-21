"""Wire-level tests for the admin-only ``/admin/provider/gguf*`` endpoints.

Pins the contract of the GGUF Phase-2 surface:

* all three endpoints are admin-only — non-admin and restricted users get
  403, unauthenticated callers get 401 / 403;
* ``GET /admin/provider/gguf`` returns the stable status shape and shows
  the configured GGUF path safely (the model directory + the one
  operator-set path, never an arbitrary file listing);
* ``POST /admin/provider/gguf/model-path`` validates the pasted path
  against the model directory before persisting — a path outside the dir
  / a non-``.gguf`` file / a missing file is a sanitised 400 and a
  follow-up status shows nothing was written; a valid path persists;
* ``POST /admin/provider/gguf/test`` is a calm 200 with the
  ``{ok, provider, detail, filename, path_valid}`` shape even when no
  path is configured;
* Ollama remains the default provider throughout.

Mirrors ``tests/test_provider_endpoints.py`` fixtures so the provider
suites stay consistent.
"""

from __future__ import annotations

import contextlib
import sqlite3
import sys
from unittest.mock import MagicMock, patch

import pytest

for _mod in ("ddgs", "duckduckgo_search", "ollama", "sgmllib", "feedparser"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from fastapi.testclient import TestClient  # noqa: E402

from core import memory as core_memory, users  # noqa: E402
from core.model_providers import reset as reset_registry  # noqa: E402
from memory import store as natural_store  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_registry():
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


@pytest.fixture
def model_dir(tmp_path):
    d = tmp_path / "nova-models"
    d.mkdir()
    return d


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
    resp = client.post("/login", json={"username": username, "password": password})
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
    ("GET", "/admin/provider/gguf", None),
    ("POST", "/admin/provider/gguf/model-path", {"path": "/x/m.gguf"}),
    ("POST", "/admin/provider/gguf/test", {}),
]


class TestGgufEndpointsAuth:
    @pytest.mark.parametrize("method,path,body", _ENDPOINTS)
    def test_non_admin_forbidden(self, web_client, user_token, method, path, body):
        if method == "GET":
            resp = web_client.get(path, headers=_h(user_token))
        else:
            resp = web_client.post(path, headers=_h(user_token), json=body)
        assert resp.status_code == 403

    @pytest.mark.parametrize("method,path,body", _ENDPOINTS)
    def test_restricted_forbidden(self, web_client, restricted_token, method, path, body):
        if method == "GET":
            resp = web_client.get(path, headers=_h(restricted_token))
        else:
            resp = web_client.post(path, headers=_h(restricted_token), json=body)
        assert resp.status_code == 403

    @pytest.mark.parametrize("method,path,body", _ENDPOINTS)
    def test_unauthenticated_blocked(self, web_client, method, path, body):
        if method == "GET":
            resp = web_client.get(path)
        else:
            resp = web_client.post(path, json=body)
        assert resp.status_code in (401, 403)


# ── GET /admin/provider/gguf ────────────────────────────────────────


class TestGgufStatusEndpoint:
    def test_status_shape_defaults(self, web_client, admin_token, monkeypatch, model_dir):
        monkeypatch.setattr("config.MODEL_PROVIDER", "ollama")
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        resp = web_client.get("/admin/provider/gguf", headers=_h(admin_token))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["provider"] == "ollama"
        assert body["is_llamacpp"] is False
        assert body["default_provider"] == "ollama"
        assert body["model_dir"] == str(model_dir)
        assert body["configured_path"] == ""
        assert body["path_source"] == "unset"
        assert body["path_valid"] is False

    def test_status_shows_configured_path_safely(
        self, web_client, admin_token, monkeypatch, model_dir,
    ):
        f = model_dir / "model.gguf"
        f.write_bytes(b"GGUF\x00")
        monkeypatch.setattr("config.MODEL_PROVIDER", "llamacpp")
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", str(f))
        resp = web_client.get("/admin/provider/gguf", headers=_h(admin_token))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["is_llamacpp"] is True
        assert body["configured_path"] == str(f.resolve())
        assert body["filename"] == "model.gguf"
        assert body["path_valid"] is True
        assert body["path_source"] == "env"


# ── POST /admin/provider/gguf/model-path ────────────────────────────


class TestGgufSetPathEndpoint:
    def test_valid_path_persists(self, web_client, admin_token, monkeypatch, model_dir):
        f = model_dir / "model.gguf"
        f.write_bytes(b"GGUF\x00")
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.MODEL_PROVIDER", "llamacpp")
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        resp = web_client.post(
            "/admin/provider/gguf/model-path",
            headers=_h(admin_token),
            json={"path": str(f)},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["configured_path"] == str(f.resolve())
        assert body["path_source"] == "custom"
        assert body["path_valid"] is True
        # A follow-up status reflects the persisted choice.
        status = web_client.get("/admin/provider/gguf", headers=_h(admin_token)).json()
        assert status["configured_path"] == str(f.resolve())
        assert status["path_source"] == "custom"

    def test_path_outside_model_dir_is_400(self, web_client, admin_token, monkeypatch, tmp_path, model_dir):
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        outside = tmp_path / "evil.gguf"
        outside.write_bytes(b"GGUF\x00")
        resp = web_client.post(
            "/admin/provider/gguf/model-path",
            headers=_h(admin_token),
            json={"path": str(outside)},
        )
        assert resp.status_code == 400, resp.text
        assert "inside the configured model directory" in resp.json()["detail"]
        # Nothing was written.
        status = web_client.get("/admin/provider/gguf", headers=_h(admin_token)).json()
        assert status["configured_path"] == ""

    def test_non_gguf_file_is_400(self, web_client, admin_token, monkeypatch, model_dir):
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        bad = model_dir / "model.bin"
        bad.write_bytes(b"nope")
        resp = web_client.post(
            "/admin/provider/gguf/model-path",
            headers=_h(admin_token),
            json={"path": str(bad)},
        )
        assert resp.status_code == 400, resp.text
        assert ".gguf" in resp.json()["detail"]

    def test_missing_file_is_400(self, web_client, admin_token, monkeypatch, model_dir):
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        resp = web_client.post(
            "/admin/provider/gguf/model-path",
            headers=_h(admin_token),
            json={"path": str(model_dir / "absent.gguf")},
        )
        assert resp.status_code == 400, resp.text
        assert "No file exists" in resp.json()["detail"]

    def test_traversal_is_400(self, web_client, admin_token, monkeypatch, model_dir):
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        resp = web_client.post(
            "/admin/provider/gguf/model-path",
            headers=_h(admin_token),
            json={"path": str(model_dir / ".." / "secret.gguf")},
        )
        assert resp.status_code == 400, resp.text
        assert ".." in resp.json()["detail"]

    def test_empty_path_is_422(self, web_client, admin_token):
        resp = web_client.post(
            "/admin/provider/gguf/model-path",
            headers=_h(admin_token),
            json={"path": ""},
        )
        assert resp.status_code == 422

    def test_extra_field_is_rejected(self, web_client, admin_token, model_dir):
        resp = web_client.post(
            "/admin/provider/gguf/model-path",
            headers=_h(admin_token),
            json={"path": "/x/m.gguf", "provider": "evil"},
        )
        assert resp.status_code == 422


# ── POST /admin/provider/gguf/test ──────────────────────────────────


class TestGgufTestEndpoint:
    def test_no_path_is_calm_200(self, web_client, admin_token, monkeypatch):
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        resp = web_client.post("/admin/provider/gguf/test", headers=_h(admin_token))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is False
        assert body["provider"] == "llamacpp"
        assert body["path_valid"] is False
        assert body["detail"]

    def test_invalid_path_is_calm_200(self, web_client, admin_token, monkeypatch, tmp_path, model_dir):
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        outside = tmp_path / "outside.gguf"
        outside.write_bytes(b"GGUF\x00")
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", str(outside))
        resp = web_client.post("/admin/provider/gguf/test", headers=_h(admin_token))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is False
        assert body["path_valid"] is False
        assert "inside the configured model directory" in body["detail"]
