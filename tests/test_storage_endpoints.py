"""Tests for the admin-only ``/admin/storage/*`` endpoints.

These pin the wire-level contract: who can call, who can't, and
which inputs are rejected before the underlying helpers run.

* Non-admin and restricted users get 403.
* Unauthenticated callers get 401 / 403 (FastAPI's HTTPBearer
  default).
* The export endpoint requires ``{"confirm": true}``; a missing or
  false value returns 400.
* The inspect endpoint rejects path-traversal-shaped names at the
  validation layer, returning 422 / 400 without touching the
  underlying helper.
* Successful calls return a JSON shape the admin UI can render.
"""

from __future__ import annotations

import contextlib
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

for _mod in ("ddgs", "ollama", "sgmllib", "feedparser"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from fastapi.testclient import TestClient  # noqa: E402

from core import memory as core_memory, users  # noqa: E402
from core import paths as core_paths  # noqa: E402
from memory import store as natural_store  # noqa: E402


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


@pytest.fixture
def configured_data_root(monkeypatch, tmp_path):
    """Point NOVA_DATA_DIR at a temporary, populated data directory."""
    root = tmp_path / "NovaData"
    root.mkdir()
    (root / "nova.db").write_bytes(b"-- fake --")
    for sub in ("backups", "exports", "memory-packs", "logs"):
        (root / sub).mkdir()
    monkeypatch.setenv(core_paths.ENV_VAR, str(root))
    return root


# ── Auth gating ─────────────────────────────────────────────────────


class TestStorageEndpointsAuth:
    @pytest.mark.parametrize("method,path,body", [
        ("GET",  "/admin/storage/status",          None),
        ("POST", "/admin/storage/export",          {"confirm": True}),
        ("POST", "/admin/storage/inspect-export",  {"name": "x.tar.gz"}),
    ])
    def test_non_admin_forbidden(
        self, web_client, user_token, method, path, body,
    ):
        if method == "GET":
            resp = web_client.get(path, headers=_h(user_token))
        else:
            resp = web_client.post(path, headers=_h(user_token), json=body)
        assert resp.status_code == 403

    @pytest.mark.parametrize("method,path,body", [
        ("GET",  "/admin/storage/status",          None),
        ("POST", "/admin/storage/export",          {"confirm": True}),
        ("POST", "/admin/storage/inspect-export",  {"name": "x.tar.gz"}),
    ])
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

    @pytest.mark.parametrize("method,path,body", [
        ("GET",  "/admin/storage/status",          None),
        ("POST", "/admin/storage/export",          {"confirm": True}),
        ("POST", "/admin/storage/inspect-export",  {"name": "x.tar.gz"}),
    ])
    def test_unauthenticated_blocked(self, web_client, method, path, body):
        if method == "GET":
            resp = web_client.get(path)
        else:
            resp = web_client.post(path, json=body)
        assert resp.status_code in (401, 403)


# ── /admin/storage/status ─────────────────────────────────────────


class TestStatusEndpoint:
    def test_status_shape(
        self, web_client, admin_token, configured_data_root,
    ):
        resp = web_client.get(
            "/admin/storage/status", headers=_h(admin_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["data_dir_configured"] is True
        assert body["data_dir"] == str(configured_data_root)
        assert isinstance(body["paths"], list)
        names = {p["name"] for p in body["paths"]}
        assert {
            "data_dir", "database", "backups", "exports",
            "memory_packs", "logs", "ollama_models",
        }.issubset(names)
        assert isinstance(body["recommendations"], list)


# ── /admin/storage/export ─────────────────────────────────────────


class TestExportEndpoint:
    def test_export_requires_confirm(
        self, web_client, admin_token, configured_data_root,
    ):
        resp = web_client.post(
            "/admin/storage/export",
            headers=_h(admin_token), json={"confirm": False},
        )
        assert resp.status_code == 400

    def test_export_missing_confirm(
        self, web_client, admin_token, configured_data_root,
    ):
        resp = web_client.post(
            "/admin/storage/export",
            headers=_h(admin_token), json={},
        )
        assert resp.status_code == 400

    def test_export_creates_archive(
        self, web_client, admin_token, configured_data_root,
    ):
        resp = web_client.post(
            "/admin/storage/export",
            headers=_h(admin_token), json={"confirm": True},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["archive_path"].endswith(".tar.gz")
        assert body["archive_size"] > 0
        assert body["manifest"]["format"] == "nova-data-export"
        archive = Path(body["archive_path"])
        assert archive.is_file()

    def test_export_rejects_workspace_mode(
        self, web_client, admin_token, configured_data_root,
    ):
        resp = web_client.post(
            "/admin/storage/export",
            headers=_h(admin_token),
            json={"confirm": True, "mode": "workspace"},
        )
        assert resp.status_code == 400


# ── /admin/storage/inspect-export ─────────────────────────────────


class TestInspectEndpoint:
    def test_inspect_rejects_traversal_name(
        self, web_client, admin_token, configured_data_root,
    ):
        # The validator should reject this before the helper runs.
        resp = web_client.post(
            "/admin/storage/inspect-export",
            headers=_h(admin_token), json={"name": "../etc/passwd"},
        )
        assert resp.status_code in (400, 422)

    def test_inspect_rejects_absolute_path(
        self, web_client, admin_token, configured_data_root,
    ):
        resp = web_client.post(
            "/admin/storage/inspect-export",
            headers=_h(admin_token), json={"name": "/etc/passwd"},
        )
        assert resp.status_code in (400, 422)

    def test_inspect_rejects_dotfile(
        self, web_client, admin_token, configured_data_root,
    ):
        resp = web_client.post(
            "/admin/storage/inspect-export",
            headers=_h(admin_token), json={"name": ".bashrc"},
        )
        assert resp.status_code in (400, 422)

    def test_inspect_missing_archive_returns_404(
        self, web_client, admin_token, configured_data_root,
    ):
        resp = web_client.post(
            "/admin/storage/inspect-export",
            headers=_h(admin_token),
            json={"name": "nonexistent.tar.gz"},
        )
        assert resp.status_code == 404

    def test_inspect_returns_report_for_real_archive(
        self, web_client, admin_token, configured_data_root,
    ):
        # First, build an archive via the export endpoint.
        export = web_client.post(
            "/admin/storage/export",
            headers=_h(admin_token), json={"confirm": True},
        )
        assert export.status_code == 200, export.text
        archive_path = Path(export.json()["archive_path"])
        assert archive_path.is_file()

        # Then inspect it by its basename.
        resp = web_client.post(
            "/admin/storage/inspect-export",
            headers=_h(admin_token),
            json={"name": archive_path.name},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["valid"] is True
        assert body["manifest"]["format"] == "nova-data-export"
