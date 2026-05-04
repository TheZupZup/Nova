"""
Tests for the local Ollama model registry (issue #110).

Covers:
  * `model_registry` table is created by initialize_db().
  * Configured models from `config.MODELS` are seeded once.
  * Seeding is idempotent — second call inserts no rows.
  * Admin can read the registry through `GET /admin/models`.
  * Non-admin and restricted users get 403 — raw model names stay
    behind the admin gate.
  * `reconcile_installed` updates the `installed` flag from
    `client.list()` and never triggers a pull.
  * If Ollama is unreachable, reconcile leaves persisted flags intact.
  * Existing chat routing keeps working when registry rows are
    disabled or unknown — the registry is read-only this issue.
  * No Ollama pull subprocess is launched.
"""

from __future__ import annotations

import contextlib
import sqlite3
import subprocess
from unittest.mock import MagicMock, patch

import httpx
import ollama
import pytest
from fastapi.testclient import TestClient

from core import memory as core_memory, model_registry, users
from memory import store as natural_store


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "nova.db")
    monkeypatch.setattr(core_memory, "DB_PATH", path)
    monkeypatch.setattr(natural_store, "DB_PATH", path)
    core_memory.initialize_db()
    # `initialize_db` seeds a default admin ("nova") and the four entries
    # from config.MODELS. Tests in this file want a deterministic users
    # table; the model_registry is left as seeded and asserted against.
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


def _table_exists(path: str, name: str) -> bool:
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None


def _registry_rows(path: str) -> list[dict]:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM model_registry ORDER BY id ASC"
        ).fetchall()
    return [dict(r) for r in rows]


# ── Migration & seeding (unit) ──────────────────────────────────────────────


class TestMigrationAndSeed:
    def test_initialize_db_creates_model_registry_table(self, db_path):
        assert _table_exists(db_path, "model_registry")

    def test_seed_inserts_one_row_per_configured_model(self, db_path):
        from config import MODELS
        rows = _registry_rows(db_path)
        # Every value from config.MODELS should land in the registry.
        seeded_names = {r["model_name"] for r in rows}
        assert seeded_names == set(MODELS.values())

    def test_seed_records_purpose_and_display_name(self, db_path):
        rows = _registry_rows(db_path)
        by_purpose = {r["purpose"]: r for r in rows}
        # The four canonical purposes are present.
        assert {"router", "default", "code", "advanced"}.issubset(by_purpose)
        assert by_purpose["router"]["display_name"] == "Router"
        assert by_purpose["default"]["display_name"] == "Default"
        assert by_purpose["code"]["display_name"] == "Code"
        assert by_purpose["advanced"]["display_name"] == "Advanced"

    def test_seed_defaults_enabled_true_installed_false(self, db_path):
        rows = _registry_rows(db_path)
        assert rows  # sanity
        for r in rows:
            assert r["enabled"] == 1
            assert r["installed"] == 0

    def test_seed_is_idempotent(self, db_path):
        before = _registry_rows(db_path)
        # Re-running the seed must not add or duplicate rows.
        inserted = model_registry.seed_from_config(db_path)
        after = _registry_rows(db_path)
        assert inserted == 0
        assert before == after

    def test_seed_does_not_overwrite_existing_rows(self, db_path):
        # Simulate an admin having toggled `enabled` off — re-seeding should
        # not flip it back to the default.
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE model_registry SET enabled = 0 WHERE purpose = 'router'"
            )
        model_registry.seed_from_config(db_path)
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT enabled FROM model_registry WHERE purpose = 'router'"
            ).fetchone()
        assert row[0] == 0

    def test_seed_with_explicit_models_dict(self, tmp_path):
        path = str(tmp_path / "fresh.db")
        model_registry.migrate(path)
        inserted = model_registry.seed_from_config(
            path, models={"router": "tiny:1b", "default": "med:7b"}
        )
        assert inserted == 2
        names = {r["model_name"] for r in _registry_rows(path)}
        assert names == {"tiny:1b", "med:7b"}

    def test_migrate_is_idempotent(self, tmp_path):
        path = str(tmp_path / "fresh.db")
        model_registry.migrate(path)
        model_registry.migrate(path)
        assert _table_exists(path, "model_registry")

    def test_unique_model_name(self, tmp_path):
        path = str(tmp_path / "fresh.db")
        model_registry.migrate(path)
        # Seeding the same model_name twice still results in a single row.
        model_registry.seed_from_config(path, models={"a": "same"})
        model_registry.seed_from_config(path, models={"b": "same"})
        rows = _registry_rows(path)
        assert len(rows) == 1


# ── list_registered (unit) ──────────────────────────────────────────────────


class TestListRegistered:
    def test_returns_dicts_in_id_order(self, db_path):
        rows = model_registry.list_registered(db_path)
        assert rows == sorted(rows, key=lambda r: r["id"])
        for r in rows:
            assert set(r) == {
                "id", "model_name", "display_name", "purpose",
                "enabled", "installed", "created_at", "updated_at",
            }

    def test_returns_python_bools(self, db_path):
        rows = model_registry.list_registered(db_path)
        for r in rows:
            assert isinstance(r["enabled"], bool)
            assert isinstance(r["installed"], bool)


# ── reconcile_installed (unit) ──────────────────────────────────────────────


class TestReconcileInstalled:
    def test_updates_installed_flag_from_client_list(self, db_path):
        from config import MODELS
        installed = MODELS["default"]
        with patch(
            "core.model_registry.client.list",
            return_value={"models": [{"name": installed}]},
        ) as m:
            snapshot = model_registry.reconcile_installed(db_path)

        assert snapshot is not None
        assert snapshot[installed] is True
        # Every other registered model is reported as not installed.
        for name, value in snapshot.items():
            if name != installed:
                assert value is False

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT installed FROM model_registry WHERE model_name = ?",
                (installed,),
            ).fetchone()
        assert row[0] == 1
        m.assert_called_once_with()

    def test_returns_none_when_ollama_unreachable(self, db_path):
        # Existing flag must remain unchanged when reconcile fails.
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE model_registry SET installed = 1 "
                "WHERE purpose = 'default'"
            )
        with patch(
            "core.model_registry.client.list",
            side_effect=ConnectionError("ollama down"),
        ):
            snapshot = model_registry.reconcile_installed(db_path)
        assert snapshot is None
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT installed FROM model_registry WHERE purpose = 'default'"
            ).fetchone()
        assert row[0] == 1

    def test_handles_ollama_response_error(self, db_path):
        with patch(
            "core.model_registry.client.list",
            side_effect=ollama.ResponseError("nope"),
        ):
            assert model_registry.reconcile_installed(db_path) is None

    def test_handles_http_error(self, db_path):
        with patch(
            "core.model_registry.client.list",
            side_effect=httpx.ConnectError("refused"),
        ):
            assert model_registry.reconcile_installed(db_path) is None

    def test_matches_tagged_and_base_names(self, db_path):
        # Ollama often reports `name:tag`; matching both directions keeps
        # the registry in sync regardless of how the model was registered.
        with patch(
            "core.model_registry.client.list",
            return_value={
                "models": [
                    {"name": "gemma4:latest"},
                    {"name": "deepseek-coder-v2"},
                ]
            },
        ):
            snapshot = model_registry.reconcile_installed(db_path)
        assert snapshot is not None
        assert snapshot.get("gemma4") is True
        assert snapshot.get("deepseek-coder-v2") is True

    def test_does_not_call_subprocess_or_pull(self, db_path):
        # A pull would shell out to `ollama pull …`; reconcile must never
        # do that. Patch subprocess.run + the registry's `client.list`,
        # then assert the run mock was never touched.
        with patch("subprocess.run") as run_mock, patch(
            "core.model_registry.client.list",
            return_value={"models": []},
        ):
            model_registry.reconcile_installed(db_path)
        run_mock.assert_not_called()


# ── Admin endpoint ──────────────────────────────────────────────────────────


class TestAdminListModelsEndpoint:
    def test_admin_can_list_models(self, db_path, web_client, admin_token):
        with patch(
            "core.model_registry.client.list",
            return_value={"models": []},
        ):
            resp = web_client.get("/admin/models", headers=_h(admin_token))
        assert resp.status_code == 200
        body = resp.json()
        from config import MODELS
        seeded_names = {row["model_name"] for row in body}
        assert seeded_names == set(MODELS.values())
        for r in body:
            assert {"model_name", "display_name", "purpose", "enabled",
                    "installed"}.issubset(r)

    def test_endpoint_reflects_reconciled_install_status(
        self, db_path, web_client, admin_token
    ):
        from config import MODELS
        with patch(
            "core.model_registry.client.list",
            return_value={"models": [{"name": MODELS["default"]}]},
        ):
            resp = web_client.get("/admin/models", headers=_h(admin_token))
        assert resp.status_code == 200
        by_purpose = {r["purpose"]: r for r in resp.json()}
        assert by_purpose["default"]["installed"] is True
        assert by_purpose["router"]["installed"] is False

    def test_endpoint_does_not_pull_models(
        self, db_path, web_client, admin_token
    ):
        with patch("subprocess.run") as run_mock, patch(
            "core.model_registry.client.list",
            return_value={"models": []},
        ):
            resp = web_client.get("/admin/models", headers=_h(admin_token))
        assert resp.status_code == 200
        run_mock.assert_not_called()

    def test_non_admin_user_forbidden(self, db_path, web_client, admin_token):
        _make_user(db_path, "bob")
        token = _login(web_client, "bob")
        with patch(
            "core.model_registry.client.list",
            return_value={"models": []},
        ):
            resp = web_client.get("/admin/models", headers=_h(token))
        assert resp.status_code == 403

    def test_restricted_user_forbidden(self, db_path, web_client, admin_token):
        _make_user(db_path, "kid", is_restricted=True)
        token = _login(web_client, "kid")
        with patch(
            "core.model_registry.client.list",
            return_value={"models": []},
        ):
            resp = web_client.get("/admin/models", headers=_h(token))
        assert resp.status_code == 403

    def test_unauthenticated_blocked(self, web_client):
        resp = web_client.get("/admin/models")
        assert resp.status_code in (401, 403)

    def test_raw_model_name_not_leaked_to_non_admin(
        self, db_path, web_client, admin_token
    ):
        _make_user(db_path, "bob")
        token = _login(web_client, "bob")
        from config import MODELS
        with patch(
            "core.model_registry.client.list",
            return_value={"models": []},
        ):
            resp = web_client.get("/admin/models", headers=_h(token))
        assert resp.status_code == 403
        # Defence in depth: even the 403 body must not echo a raw name.
        body_text = resp.text
        for raw in MODELS.values():
            assert raw not in body_text


# ── Chat routing is unaffected ──────────────────────────────────────────────


class TestChatRoutingPreserved:
    def test_router_module_still_uses_config_models(self):
        # The registry must not have rewired routing — `core.router.MODEL_MAP`
        # is still keyed off `config.MODELS`. (#112 owns per-user access.)
        from core import router
        from config import MODELS
        assert router.MODEL_MAP["simple"] == MODELS["default"]
        assert router.MODEL_MAP["normal"] == MODELS["default"]
        assert router.MODEL_MAP["code"] == MODELS["code"]
        assert router.MODEL_MAP["advanced"] == MODELS["advanced"]
        assert router.FALLBACK_MODEL == MODELS["default"]

    def test_disabling_registry_row_does_not_break_chat(self, db_path):
        # The registry is informational only this issue. A row marked
        # `enabled = 0` must not cause the chat layer to refuse to route.
        with sqlite3.connect(db_path) as conn:
            conn.execute("UPDATE model_registry SET enabled = 0")
        from core.router import route, FALLBACK_MODEL
        with patch(
            "core.router.client.chat",
            side_effect=ollama.ResponseError("router down"),
        ):
            chosen = route("hello")
        assert chosen == FALLBACK_MODEL

    def test_unknown_registry_entry_does_not_break_chat(self, db_path):
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO model_registry "
                "(model_name, display_name, purpose, enabled, installed, "
                "created_at, updated_at) "
                "VALUES ('phantom:1b', 'Phantom', 'experimental', 1, 0, "
                "datetime('now'), datetime('now'))"
            )
        from core.router import route, FALLBACK_MODEL
        with patch(
            "core.router.client.chat",
            side_effect=ollama.ResponseError("router down"),
        ):
            chosen = route("hello")
        assert chosen == FALLBACK_MODEL


# ── Hard guarantee: no pulls happen anywhere in this PR ─────────────────────


class TestNoPullsTriggered:
    def test_initialize_db_does_not_call_subprocess(self, tmp_path, monkeypatch):
        path = str(tmp_path / "nova.db")
        monkeypatch.setattr(core_memory, "DB_PATH", path)
        monkeypatch.setattr(natural_store, "DB_PATH", path)
        with patch("subprocess.run") as run_mock:
            core_memory.initialize_db()
        run_mock.assert_not_called()

    def test_initialize_db_does_not_call_ollama_client_list(
        self, tmp_path, monkeypatch
    ):
        path = str(tmp_path / "nova.db")
        monkeypatch.setattr(core_memory, "DB_PATH", path)
        monkeypatch.setattr(natural_store, "DB_PATH", path)
        with patch("core.model_registry.client.list") as list_mock:
            core_memory.initialize_db()
        # Seeding alone must not even hit Ollama — only the reconcile
        # helper does, and it is invoked from the admin endpoint.
        list_mock.assert_not_called()

    def test_subprocess_is_not_imported_by_model_registry(self):
        # Catch a regression where someone wires `subprocess.run("ollama pull")`
        # into the registry. The module should not need subprocess at all.
        import core.model_registry as mr
        assert not hasattr(mr, "subprocess")
        # Sanity: the symbol does exist in the global subprocess module
        # (the import above succeeded).
        assert subprocess.run is not None
