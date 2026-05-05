"""
Tests for the local Ollama model detection registry.

Covers:
  * `core.ollama_client.list_local_models` parses `/api/tags`,
    handles empty output, and surfaces unavailability cleanly.
  * `core.local_models` upserts by (provider, model_name), is
    idempotent across refreshes, and never deletes missing models.
  * Admin endpoints `/admin/ollama/refresh` and `/admin/ollama/models`
    are admin-only and produce a controlled 503 when Ollama is down.

Ollama is never contacted for real — the HTTP layer is mocked.
"""

from __future__ import annotations

import contextlib
import sqlite3
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from core import local_models, memory as core_memory, ollama_client, users
from core.ollama_client import OllamaUnavailable
from memory import store as natural_store


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "nova.db")
    monkeypatch.setattr(core_memory, "DB_PATH", path)
    monkeypatch.setattr(natural_store, "DB_PATH", path)
    core_memory.initialize_db()
    # The seeded admin row would interfere with explicit user creation
    # in the auth-flow tests below; clear it for a clean slate.
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


def _make_response(status_code=200, json_body=None):
    request = httpx.Request("GET", "http://localhost:11434/api/tags")
    return httpx.Response(status_code, json=json_body, request=request)


# ── Ollama client (unit) ────────────────────────────────────────────────────


class TestOllamaClientListLocalModels:
    def test_parses_api_tags_response(self):
        body = {"models": [
            {
                "name": "gemma3:1b",
                "digest": "abc123",
                "size": 1234567,
                "modified_at": "2024-01-01T00:00:00Z",
            },
            {
                "name": "llama3:8b",
                "digest": "def456",
                "size": 7654321,
                "modified_at": "2024-02-02T00:00:00Z",
            },
        ]}
        with patch.object(
            ollama_client.httpx, "get",
            return_value=_make_response(200, body),
        ):
            models = ollama_client.list_local_models()
        assert {m["name"] for m in models} == {"gemma3:1b", "llama3:8b"}
        first = next(m for m in models if m["name"] == "gemma3:1b")
        assert first["digest"] == "abc123"
        assert first["size"] == 1234567
        assert first["modified_at"] == "2024-01-01T00:00:00Z"

    def test_empty_models_list_returns_empty(self):
        with patch.object(
            ollama_client.httpx, "get",
            return_value=_make_response(200, {"models": []}),
        ):
            assert ollama_client.list_local_models() == []

    def test_missing_models_field_returns_empty(self):
        with patch.object(
            ollama_client.httpx, "get",
            return_value=_make_response(200, {}),
        ):
            assert ollama_client.list_local_models() == []

    def test_uses_model_field_when_name_missing(self):
        body = {"models": [{"model": "fallback-name", "digest": "x"}]}
        with patch.object(
            ollama_client.httpx, "get",
            return_value=_make_response(200, body),
        ):
            models = ollama_client.list_local_models()
        assert len(models) == 1
        assert models[0]["name"] == "fallback-name"

    def test_skips_entries_without_name(self):
        body = {"models": [
            {"name": "good"},
            {"digest": "no-name"},
            {"name": ""},
            "not-a-dict",
            {"name": 42},
        ]}
        with patch.object(
            ollama_client.httpx, "get",
            return_value=_make_response(200, body),
        ):
            models = ollama_client.list_local_models()
        assert {m["name"] for m in models} == {"good"}

    def test_connection_error_raises_unavailable(self):
        with patch.object(
            ollama_client.httpx, "get",
            side_effect=httpx.ConnectError("refused"),
        ):
            with pytest.raises(OllamaUnavailable):
                ollama_client.list_local_models()

    def test_timeout_raises_unavailable(self):
        with patch.object(
            ollama_client.httpx, "get",
            side_effect=httpx.TimeoutException("slow"),
        ):
            with pytest.raises(OllamaUnavailable):
                ollama_client.list_local_models()

    def test_http_error_status_raises_unavailable(self):
        with patch.object(
            ollama_client.httpx, "get",
            return_value=_make_response(500, {"error": "boom"}),
        ):
            with pytest.raises(OllamaUnavailable):
                ollama_client.list_local_models()

    def test_non_dict_payload_raises_unavailable(self):
        with patch.object(
            ollama_client.httpx, "get",
            return_value=_make_response(200, ["not", "a", "dict"]),
        ):
            with pytest.raises(OllamaUnavailable):
                ollama_client.list_local_models()

    def test_uses_ollama_host_default(self):
        # Confirm the call hits the configured OLLAMA_HOST + /api/tags.
        captured = {}

        def fake_get(url, timeout=None):
            captured["url"] = url
            return _make_response(200, {"models": []})

        with patch.object(ollama_client.httpx, "get", side_effect=fake_get):
            ollama_client.list_local_models()
        assert captured["url"].endswith("/api/tags")


# ── Registry (unit) ─────────────────────────────────────────────────────────


class TestMigration:
    def test_migrate_creates_table(self, tmp_path):
        path = str(tmp_path / "fresh.db")
        local_models.migrate(path)
        with sqlite3.connect(path) as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='local_ollama_models'"
            ).fetchone()
        assert row is not None

    def test_migrate_is_idempotent(self, tmp_path):
        path = str(tmp_path / "fresh.db")
        local_models.migrate(path)
        local_models.migrate(path)
        # No error == idempotent. Sanity: still exactly one table.
        with sqlite3.connect(path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='local_ollama_models'"
            ).fetchone()[0]
        assert count == 1

    def test_initialize_db_creates_table(self, db_path):
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='local_ollama_models'"
            ).fetchone()
        assert row is not None


class TestUpsert:
    def test_inserts_new_rows(self, db_path):
        detected = [
            {"name": "gemma3:1b", "digest": "abc", "size": 100,
             "modified_at": "t1"},
            {"name": "llama3:8b", "digest": "def", "size": 200,
             "modified_at": "t2"},
        ]
        stats = local_models.upsert_models(detected, db_path=db_path)
        assert stats == {"inserted": 2, "updated": 0, "seen": 2}
        rows = local_models.list_models(db_path)
        assert {r["model_name"] for r in rows} == {"gemma3:1b", "llama3:8b"}
        first = next(r for r in rows if r["model_name"] == "gemma3:1b")
        assert first["provider"] == "ollama"
        assert first["digest"] == "abc"
        assert first["size_bytes"] == 100
        assert first["modified_at"] == "t1"
        assert first["first_seen_at"] == first["last_seen_at"]

    def test_is_idempotent_for_same_input(self, db_path):
        detected = [{"name": "gemma3:1b", "digest": "abc", "size": 100}]
        first = local_models.upsert_models(detected, db_path=db_path)
        second = local_models.upsert_models(detected, db_path=db_path)
        assert first == {"inserted": 1, "updated": 0, "seen": 1}
        assert second == {"inserted": 0, "updated": 1, "seen": 1}
        rows = local_models.list_models(db_path)
        assert len(rows) == 1

    def test_updates_existing_row(self, db_path):
        local_models.upsert_models(
            [{"name": "gemma3:1b", "digest": "v1", "size": 100}],
            db_path=db_path,
        )
        local_models.upsert_models(
            [{"name": "gemma3:1b", "digest": "v2", "size": 200,
              "modified_at": "newer"}],
            db_path=db_path,
        )
        rows = local_models.list_models(db_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["digest"] == "v2"
        assert row["size_bytes"] == 200
        assert row["modified_at"] == "newer"
        # last_seen_at should have advanced; first_seen_at stays put.
        assert row["first_seen_at"] <= row["last_seen_at"]

    def test_does_not_delete_missing_models(self, db_path):
        local_models.upsert_models(
            [{"name": "gemma3:1b"}, {"name": "llama3:8b"}], db_path=db_path
        )
        # A subsequent refresh sees only one model — the other must
        # NOT be deleted.
        local_models.upsert_models([{"name": "gemma3:1b"}], db_path=db_path)
        rows = local_models.list_models(db_path)
        assert {r["model_name"] for r in rows} == {"gemma3:1b", "llama3:8b"}

    def test_skips_entries_without_name(self, db_path):
        detected = [
            {"name": "valid"},
            {},
            {"name": ""},
            {"size": 99},
            "not-a-dict",
        ]
        stats = local_models.upsert_models(detected, db_path=db_path)
        assert stats == {"inserted": 1, "updated": 0, "seen": 1}
        rows = local_models.list_models(db_path)
        assert {r["model_name"] for r in rows} == {"valid"}

    def test_handles_empty_list(self, db_path):
        stats = local_models.upsert_models([], db_path=db_path)
        assert stats == {"inserted": 0, "updated": 0, "seen": 0}
        assert local_models.list_models(db_path) == []

    def test_unique_provider_model_name(self, db_path):
        # Same model name under different providers is allowed.
        local_models.upsert_models(
            [{"name": "shared"}], db_path=db_path, provider="ollama"
        )
        local_models.upsert_models(
            [{"name": "shared"}], db_path=db_path, provider="other"
        )
        rows = local_models.list_models(db_path)
        assert {r["provider"] for r in rows} == {"ollama", "other"}

    def test_negative_size_is_dropped(self, db_path):
        local_models.upsert_models(
            [{"name": "weird", "size": -1}], db_path=db_path
        )
        rows = local_models.list_models(db_path)
        assert rows[0]["size_bytes"] is None


class TestListModels:
    def test_orders_by_id(self, db_path):
        local_models.upsert_models(
            [{"name": "first"}, {"name": "second"}], db_path=db_path
        )
        rows = local_models.list_models(db_path)
        assert [r["model_name"] for r in rows] == ["first", "second"]
        assert rows == sorted(rows, key=lambda r: r["id"])

    def test_returns_full_shape(self, db_path):
        local_models.upsert_models([{"name": "m"}], db_path=db_path)
        row = local_models.list_models(db_path)[0]
        assert set(row) == {
            "id", "provider", "model_name", "digest", "size_bytes",
            "modified_at", "first_seen_at", "last_seen_at",
        }


class TestRefreshFromOllama:
    def test_calls_client_and_imports(self, db_path):
        with patch(
            "core.local_models._client_list_local_models",
            return_value=[{"name": "gemma3:1b", "digest": "abc"}],
        ):
            stats = local_models.refresh_from_ollama(db_path)
        assert stats == {"inserted": 1, "updated": 0, "seen": 1}
        rows = local_models.list_models(db_path)
        assert {r["model_name"] for r in rows} == {"gemma3:1b"}

    def test_empty_ollama_works(self, db_path):
        with patch(
            "core.local_models._client_list_local_models",
            return_value=[],
        ):
            stats = local_models.refresh_from_ollama(db_path)
        assert stats == {"inserted": 0, "updated": 0, "seen": 0}
        assert local_models.list_models(db_path) == []

    def test_propagates_unavailable(self, db_path):
        with patch(
            "core.local_models._client_list_local_models",
            side_effect=OllamaUnavailable("down"),
        ):
            with pytest.raises(OllamaUnavailable):
                local_models.refresh_from_ollama(db_path)

    def test_two_refreshes_are_idempotent(self, db_path):
        detected = [{"name": "gemma3:1b", "digest": "abc"}]
        with patch(
            "core.local_models._client_list_local_models",
            return_value=detected,
        ):
            first = local_models.refresh_from_ollama(db_path)
            second = local_models.refresh_from_ollama(db_path)
        assert first["inserted"] == 1
        assert second["inserted"] == 0
        assert second["updated"] == 1
        rows = local_models.list_models(db_path)
        assert len(rows) == 1


# ── Admin endpoints ─────────────────────────────────────────────────────────


class TestAdminRefreshEndpoint:
    def test_admin_can_refresh(self, db_path, web_client, admin_token):
        with patch(
            "core.local_models._client_list_local_models",
            return_value=[{"name": "gemma3:1b"}, {"name": "llama3:8b"}],
        ):
            resp = web_client.post(
                "/admin/ollama/refresh", headers=_h(admin_token)
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["inserted"] == 2
        assert body["updated"] == 0
        assert body["seen"] == 2

    def test_refresh_is_idempotent(self, db_path, web_client, admin_token):
        with patch(
            "core.local_models._client_list_local_models",
            return_value=[{"name": "gemma3:1b"}],
        ):
            r1 = web_client.post(
                "/admin/ollama/refresh", headers=_h(admin_token)
            )
            r2 = web_client.post(
                "/admin/ollama/refresh", headers=_h(admin_token)
            )
        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.json()["inserted"] == 1
        assert r2.json()["inserted"] == 0
        assert r2.json()["updated"] == 1

    def test_refresh_handles_empty_model_list(
        self, db_path, web_client, admin_token
    ):
        with patch(
            "core.local_models._client_list_local_models",
            return_value=[],
        ):
            resp = web_client.post(
                "/admin/ollama/refresh", headers=_h(admin_token)
            )
        assert resp.status_code == 200
        assert resp.json()["seen"] == 0

    def test_503_when_ollama_unavailable(
        self, db_path, web_client, admin_token
    ):
        with patch(
            "core.local_models._client_list_local_models",
            side_effect=OllamaUnavailable("down"),
        ):
            resp = web_client.post(
                "/admin/ollama/refresh", headers=_h(admin_token)
            )
        assert resp.status_code == 503

    def test_non_admin_user_forbidden(
        self, db_path, web_client, admin_token
    ):
        _make_user(db_path, "bob")
        token = _login(web_client, "bob")
        resp = web_client.post(
            "/admin/ollama/refresh", headers=_h(token)
        )
        assert resp.status_code == 403

    def test_restricted_user_forbidden(
        self, db_path, web_client, admin_token
    ):
        _make_user(db_path, "kid", is_restricted=True)
        token = _login(web_client, "kid")
        resp = web_client.post(
            "/admin/ollama/refresh", headers=_h(token)
        )
        assert resp.status_code == 403

    def test_unauthenticated_blocked(self, web_client):
        resp = web_client.post("/admin/ollama/refresh")
        assert resp.status_code in (401, 403)


class TestAdminListEndpoint:
    def test_admin_can_list(self, db_path, web_client, admin_token):
        local_models.upsert_models(
            [{"name": "gemma3:1b", "digest": "abc", "size": 100}],
            db_path=db_path,
        )
        resp = web_client.get(
            "/admin/ollama/models", headers=_h(admin_token)
        )
        assert resp.status_code == 200
        body = resp.json()
        assert {m["model_name"] for m in body} == {"gemma3:1b"}
        m = body[0]
        assert {"id", "provider", "model_name", "digest", "size_bytes",
                "modified_at", "first_seen_at",
                "last_seen_at"}.issubset(m)
        assert m["provider"] == "ollama"
        assert m["digest"] == "abc"
        assert m["size_bytes"] == 100

    def test_list_empty_when_no_refresh(
        self, db_path, web_client, admin_token
    ):
        resp = web_client.get(
            "/admin/ollama/models", headers=_h(admin_token)
        )
        assert resp.status_code == 200
        assert resp.json() == []

    def test_non_admin_user_forbidden(
        self, db_path, web_client, admin_token
    ):
        _make_user(db_path, "bob")
        token = _login(web_client, "bob")
        resp = web_client.get(
            "/admin/ollama/models", headers=_h(token)
        )
        assert resp.status_code == 403

    def test_restricted_user_forbidden(
        self, db_path, web_client, admin_token
    ):
        _make_user(db_path, "kid", is_restricted=True)
        token = _login(web_client, "kid")
        resp = web_client.get(
            "/admin/ollama/models", headers=_h(token)
        )
        assert resp.status_code == 403

    def test_unauthenticated_blocked(self, web_client):
        resp = web_client.get("/admin/ollama/models")
        assert resp.status_code in (401, 403)
