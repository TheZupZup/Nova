"""
Tests for admin-facing model-download warnings (issue #126).

Covers:
  * `build_pull_warnings` returns disk / RAM / slowdown guidance for every
    pull, plus an `unknown_size` warning when no size estimate is available.
  * Large size estimates trigger the `large_model` warning but do *not*
    block the pull — there is no hard maximum size enforced.
  * `estimate_model_size` is best-effort: it returns None on Ollama
    failures, on missing fields, or on payloads it doesn't recognise.
  * `request_pull` returns the warning metadata alongside the job row so
    admins see resource impact with the pull response.
  * The `/admin/models/pull` and `/admin/models/pull/preview` endpoints
    surface warnings for admins, reject invalid names, and stay locked
    down to admin role only (non-admin and restricted users get 403,
    unauthenticated callers get 401/403).
  * Already-installed handling still works (no warnings needed) and
    duplicate-active-pull behaviour from #111 is unaffected.
  * No new tables are introduced — #126 stays purely on the warning side.
"""

from __future__ import annotations

import contextlib
import sqlite3
from unittest.mock import MagicMock, patch

import httpx
import ollama
import pytest
from fastapi.testclient import TestClient

from core import memory as core_memory, model_pulls, users
from memory import store as natural_store


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_active_models():
    with model_pulls._state_lock:
        model_pulls._active_models.clear()
    yield
    with model_pulls._state_lock:
        model_pulls._active_models.clear()


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "nova.db")
    monkeypatch.setattr(core_memory, "DB_PATH", path)
    monkeypatch.setattr(natural_store, "DB_PATH", path)
    core_memory.initialize_db()
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM users")
    return path


def _make_user(db_path, username, password="pw", role=users.ROLE_USER, is_restricted=False):
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


def _seed_registry(path, model_name, *, installed=False):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO model_registry "
            "(model_name, display_name, purpose, enabled, installed, "
            "created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?, ?)",
            (model_name, model_name, "extra", 1 if installed else 0, now, now),
        )


def _sync_runner(pull_id, model_name, db_path):
    model_pulls._run_pull(pull_id, model_name, db_path)


def _codes(warnings):
    return {w["code"] for w in warnings}


# ── build_pull_warnings ─────────────────────────────────────────────────────


class TestBuildPullWarnings:
    def test_unknown_size_includes_unknown_warning(self):
        out = model_pulls.build_pull_warnings("any:tag", None)
        assert out["unknown_size"] is True
        assert out["is_large"] is False
        assert out["estimated_size_bytes"] is None
        codes = _codes(out["warnings"])
        # Always-present resource guidance.
        assert model_pulls.WARNING_DISK_USAGE in codes
        assert model_pulls.WARNING_RAM_VRAM_IMPACT in codes
        assert model_pulls.WARNING_POSSIBLE_SLOWDOWN in codes
        # Unknown-size specific warning.
        assert model_pulls.WARNING_UNKNOWN_SIZE in codes
        assert model_pulls.WARNING_LARGE_MODEL not in codes

    def test_small_known_size_omits_unknown_and_large(self):
        out = model_pulls.build_pull_warnings("small:1b", 500 * 1024 ** 2)  # 500 MiB
        assert out["unknown_size"] is False
        assert out["is_large"] is False
        assert out["estimated_size_bytes"] == 500 * 1024 ** 2
        codes = _codes(out["warnings"])
        assert model_pulls.WARNING_UNKNOWN_SIZE not in codes
        assert model_pulls.WARNING_LARGE_MODEL not in codes
        # Resource guidance still present.
        assert model_pulls.WARNING_DISK_USAGE in codes
        assert model_pulls.WARNING_RAM_VRAM_IMPACT in codes
        assert model_pulls.WARNING_POSSIBLE_SLOWDOWN in codes

    def test_large_known_size_emits_large_warning(self):
        size = 40 * 1024 ** 3  # 40 GiB — way above the soft threshold
        out = model_pulls.build_pull_warnings("huge:70b", size)
        assert out["unknown_size"] is False
        assert out["is_large"] is True
        assert out["estimated_size_bytes"] == size
        codes = _codes(out["warnings"])
        assert model_pulls.WARNING_LARGE_MODEL in codes
        # The large message includes a human-readable size hint.
        large = next(w for w in out["warnings"] if w["code"] == model_pulls.WARNING_LARGE_MODEL)
        assert "GiB" in large["message"]

    def test_zero_or_negative_size_treated_as_unknown(self):
        for bad in (0, -1, -1024):
            out = model_pulls.build_pull_warnings("x", bad)
            assert out["unknown_size"] is True
            assert out["estimated_size_bytes"] is None
            assert out["is_large"] is False

    def test_warnings_are_serialisable_dicts(self):
        out = model_pulls.build_pull_warnings("x", None)
        for w in out["warnings"]:
            assert set(w.keys()) == {"code", "level", "message"}
            assert isinstance(w["code"], str) and w["code"]
            assert w["level"] in {"info", "warning"}
            assert isinstance(w["message"], str) and w["message"]


# ── estimate_model_size ─────────────────────────────────────────────────────


class TestEstimateModelSize:
    def test_returns_none_when_ollama_unreachable(self):
        with patch(
            "core.model_pulls.client.show",
            side_effect=httpx.ConnectError("nope"),
        ):
            assert model_pulls.estimate_model_size("x:1b") is None

    def test_returns_none_on_response_error(self):
        with patch(
            "core.model_pulls.client.show",
            side_effect=ollama.ResponseError("not found"),
        ):
            assert model_pulls.estimate_model_size("x:1b") is None

    def test_returns_none_on_unexpected_exception(self):
        with patch(
            "core.model_pulls.client.show",
            side_effect=RuntimeError("kaboom"),
        ):
            assert model_pulls.estimate_model_size("x:1b") is None

    def test_returns_size_from_top_level_field(self):
        with patch(
            "core.model_pulls.client.show",
            return_value={"size": 1234567890},
        ):
            assert model_pulls.estimate_model_size("x:1b") == 1234567890

    def test_returns_size_from_total_size_alias(self):
        with patch(
            "core.model_pulls.client.show",
            return_value={"total_size": 4242},
        ):
            assert model_pulls.estimate_model_size("x:1b") == 4242

    def test_returns_size_from_details(self):
        with patch(
            "core.model_pulls.client.show",
            return_value={"details": {"size": 999}},
        ):
            assert model_pulls.estimate_model_size("x:1b") == 999

    def test_returns_none_when_no_size_in_payload(self):
        with patch(
            "core.model_pulls.client.show",
            return_value={"modelfile": "FROM x", "details": {"format": "gguf"}},
        ):
            assert model_pulls.estimate_model_size("x:1b") is None

    def test_supports_pydantic_like_payload(self):
        class Fake:
            def model_dump(self):
                return {"size": 7777}

        with patch("core.model_pulls.client.show", return_value=Fake()):
            assert model_pulls.estimate_model_size("x:1b") == 7777

    def test_ignores_non_dict_payload(self):
        with patch("core.model_pulls.client.show", return_value="???"):
            assert model_pulls.estimate_model_size("x:1b") is None


# ── preview_pull ────────────────────────────────────────────────────────────


class TestPreviewPull:
    def test_validates_name_before_calling_show(self):
        with patch("core.model_pulls.client.show") as show_mock:
            with pytest.raises(model_pulls.InvalidModelName):
                model_pulls.preview_pull("bad; rm -rf /")
        show_mock.assert_not_called()

    def test_returns_warnings_with_unknown_size_when_show_fails(self):
        with patch(
            "core.model_pulls.client.show",
            side_effect=httpx.ConnectError("down"),
        ):
            out = model_pulls.preview_pull("any:tag")
        assert out["unknown_size"] is True
        assert model_pulls.WARNING_UNKNOWN_SIZE in _codes(out["warnings"])

    def test_returns_warnings_with_size_when_show_succeeds(self):
        with patch(
            "core.model_pulls.client.show",
            return_value={"size": 12 * 1024 ** 3},
        ):
            out = model_pulls.preview_pull("big:27b")
        assert out["unknown_size"] is False
        assert out["is_large"] is True
        assert out["estimated_size_bytes"] == 12 * 1024 ** 3


# ── request_pull surfaces warnings ──────────────────────────────────────────


class TestRequestPullWarnings:
    def test_response_includes_warning_metadata_for_unknown_size(self, db_path):
        _seed_registry(db_path, "newmodel:1b")
        with patch(
            "core.model_pulls.client.show",
            side_effect=httpx.ConnectError("down"),
        ), patch(
            "core.model_pulls.client.pull",
            return_value=iter([{"status": "success"}]),
        ):
            job = model_pulls.request_pull("newmodel:1b", db_path, runner=_sync_runner)

        assert job["unknown_size"] is True
        assert job["estimated_size_bytes"] is None
        assert job["is_large"] is False
        assert model_pulls.WARNING_UNKNOWN_SIZE in _codes(job["warnings"])

    def test_response_includes_large_warning_when_size_known(self, db_path):
        _seed_registry(db_path, "huge:70b")
        with patch(
            "core.model_pulls.client.show",
            return_value={"size": 40 * 1024 ** 3},
        ), patch(
            "core.model_pulls.client.pull",
            return_value=iter([{"status": "success"}]),
        ):
            job = model_pulls.request_pull("huge:70b", db_path, runner=_sync_runner)

        assert job["unknown_size"] is False
        assert job["is_large"] is True
        assert model_pulls.WARNING_LARGE_MODEL in _codes(job["warnings"])

    def test_large_model_is_not_blocked(self, db_path):
        # Size well above the soft `_LARGE_MODEL_BYTES` threshold must
        # still pull successfully — #126 is informational only.
        _seed_registry(db_path, "huge:70b")
        with patch(
            "core.model_pulls.client.show",
            return_value={"size": 100 * 1024 ** 3},
        ), patch(
            "core.model_pulls.client.pull",
            return_value=iter([{"status": "success"}]),
        ):
            job = model_pulls.request_pull("huge:70b", db_path, runner=_sync_runner)

        final = model_pulls.get_pull(job["id"], db_path)
        assert final["status"] == model_pulls.STATUS_DONE

    def test_unknown_size_is_not_blocked(self, db_path):
        _seed_registry(db_path, "mystery:1b")
        with patch(
            "core.model_pulls.client.show",
            side_effect=httpx.ConnectError("down"),
        ), patch(
            "core.model_pulls.client.pull",
            return_value=iter([{"status": "success"}]),
        ):
            job = model_pulls.request_pull("mystery:1b", db_path, runner=_sync_runner)

        final = model_pulls.get_pull(job["id"], db_path)
        assert final["status"] == model_pulls.STATUS_DONE

    def test_warnings_not_persisted_on_pull_row(self, db_path):
        # The warnings ride along with the request_pull return value but
        # must not pollute the DB row — `get_pull` returns plain rows.
        _seed_registry(db_path, "newmodel:1b")
        with patch(
            "core.model_pulls.client.show",
            return_value={"size": 100},
        ), patch(
            "core.model_pulls.client.pull",
            return_value=iter([{"status": "success"}]),
        ):
            job = model_pulls.request_pull("newmodel:1b", db_path, runner=_sync_runner)

        final = model_pulls.get_pull(job["id"], db_path)
        assert "warnings" not in final
        assert "estimated_size_bytes" not in final
        assert "unknown_size" not in final
        assert "is_large" not in final


# ── HTTP endpoints ──────────────────────────────────────────────────────────


class TestPullEndpointWarnings:
    def test_admin_pull_response_includes_warnings(
        self, db_path, web_client, admin_token
    ):
        _seed_registry(db_path, "newmodel:1b")
        with patch(
            "core.model_pulls._dispatch_in_thread"
        ), patch(
            "core.model_pulls.client.show",
            side_effect=httpx.ConnectError("down"),
        ):
            resp = web_client.post(
                "/admin/models/pull",
                headers=_h(admin_token),
                json={"model": "newmodel:1b"},
            )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["unknown_size"] is True
        assert body["estimated_size_bytes"] is None
        assert body["is_large"] is False
        assert any(
            w["code"] == model_pulls.WARNING_UNKNOWN_SIZE
            for w in body["warnings"]
        )

    def test_admin_pull_large_model_returns_warning_not_block(
        self, db_path, web_client, admin_token
    ):
        _seed_registry(db_path, "huge:70b")
        with patch(
            "core.model_pulls._dispatch_in_thread"
        ), patch(
            "core.model_pulls.client.show",
            return_value={"size": 40 * 1024 ** 3},
        ):
            resp = web_client.post(
                "/admin/models/pull",
                headers=_h(admin_token),
                json={"model": "huge:70b"},
            )
        # Allowed: 202 Accepted, never 4xx for "model is too large".
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["is_large"] is True
        assert any(
            w["code"] == model_pulls.WARNING_LARGE_MODEL
            for w in body["warnings"]
        )


class TestPreviewEndpoint:
    def test_admin_can_preview_warnings(self, db_path, web_client, admin_token):
        with patch(
            "core.model_pulls.client.show",
            return_value={"size": 12 * 1024 ** 3},
        ):
            resp = web_client.post(
                "/admin/models/pull/preview",
                headers=_h(admin_token),
                json={"model": "qwen3.6:27b"},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["model"] == "qwen3.6:27b"
        assert body["estimated_size_bytes"] == 12 * 1024 ** 3
        assert body["is_large"] is True
        assert body["unknown_size"] is False
        codes = {w["code"] for w in body["warnings"]}
        assert model_pulls.WARNING_LARGE_MODEL in codes
        assert model_pulls.WARNING_DISK_USAGE in codes
        assert model_pulls.WARNING_RAM_VRAM_IMPACT in codes
        assert model_pulls.WARNING_POSSIBLE_SLOWDOWN in codes

    def test_preview_returns_unknown_size_when_show_fails(
        self, db_path, web_client, admin_token
    ):
        with patch(
            "core.model_pulls.client.show",
            side_effect=ollama.ResponseError("model not found"),
        ):
            resp = web_client.post(
                "/admin/models/pull/preview",
                headers=_h(admin_token),
                json={"model": "rare:tag"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["unknown_size"] is True
        assert body["estimated_size_bytes"] is None
        assert any(
            w["code"] == model_pulls.WARNING_UNKNOWN_SIZE
            for w in body["warnings"]
        )

    def test_preview_does_not_start_a_pull(self, db_path, web_client, admin_token):
        with patch(
            "core.model_pulls._dispatch_in_thread"
        ) as dispatch_mock, patch(
            "core.model_pulls.client.pull"
        ) as pull_mock, patch(
            "core.model_pulls.client.show",
            return_value={"size": 1},
        ):
            resp = web_client.post(
                "/admin/models/pull/preview",
                headers=_h(admin_token),
                json={"model": "anything:1b"},
            )
        assert resp.status_code == 200
        dispatch_mock.assert_not_called()
        pull_mock.assert_not_called()
        # No pull row was inserted.
        assert model_pulls.list_pulls(db_path) == []

    def test_preview_rejects_invalid_name(self, db_path, web_client, admin_token):
        with patch("core.model_pulls.client.show") as show_mock:
            resp = web_client.post(
                "/admin/models/pull/preview",
                headers=_h(admin_token),
                json={"model": "bad; rm -rf /"},
            )
        assert resp.status_code == 400
        show_mock.assert_not_called()

    def test_preview_rejects_extra_fields(self, db_path, web_client, admin_token):
        resp = web_client.post(
            "/admin/models/pull/preview",
            headers=_h(admin_token),
            json={"model": "gemma4", "as_user": 1},
        )
        assert resp.status_code == 422

    def test_non_admin_cannot_preview(self, db_path, web_client, admin_token):
        _make_user(db_path, "bob")
        token = _login(web_client, "bob")
        with patch("core.model_pulls.client.show") as show_mock:
            resp = web_client.post(
                "/admin/models/pull/preview",
                headers=_h(token),
                json={"model": "any:1b"},
            )
        assert resp.status_code == 403
        show_mock.assert_not_called()

    def test_restricted_user_cannot_preview(self, db_path, web_client, admin_token):
        _make_user(db_path, "kid", is_restricted=True)
        token = _login(web_client, "kid")
        with patch("core.model_pulls.client.show") as show_mock:
            resp = web_client.post(
                "/admin/models/pull/preview",
                headers=_h(token),
                json={"model": "any:1b"},
            )
        assert resp.status_code == 403
        show_mock.assert_not_called()

    def test_unauthenticated_cannot_preview(self, web_client):
        resp = web_client.post(
            "/admin/models/pull/preview", json={"model": "any:1b"}
        )
        assert resp.status_code in (401, 403)


# ── Existing #111 behaviours preserved ──────────────────────────────────────


class TestExistingBehaviourUnchanged:
    def test_already_installed_path_still_works(
        self, db_path, web_client, admin_token
    ):
        from config import MODELS
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE model_registry SET installed = 1 WHERE model_name = ?",
                (MODELS["default"],),
            )
        with patch("core.model_pulls._dispatch_in_thread") as dispatch_mock, patch(
            "core.model_pulls.client.show", return_value={"size": 1}
        ):
            resp = web_client.post(
                "/admin/models/pull",
                headers=_h(admin_token),
                json={"model": MODELS["default"]},
            )
        assert resp.status_code == 200
        assert resp.json() == {
            "status": "already_installed",
            "model": MODELS["default"],
        }
        dispatch_mock.assert_not_called()

    def test_duplicate_active_pull_returns_existing_job(
        self, db_path, web_client, admin_token
    ):
        # Same #111 contract: a second request for the same model must
        # return the existing job; warnings live only on the first
        # (fresh) response.
        _seed_registry(db_path, "newmodel:1b")
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute(
                "INSERT INTO model_pulls "
                "(model_name, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                ("newmodel:1b", model_pulls.STATUS_PULLING, now, now),
            )
            first_id = cur.lastrowid
        model_pulls._try_reserve("newmodel:1b")

        with patch("core.model_pulls._dispatch_in_thread") as dispatch_mock, patch(
            "core.model_pulls.client.show", return_value={"size": 1}
        ):
            resp = web_client.post(
                "/admin/models/pull",
                headers=_h(admin_token),
                json={"model": "newmodel:1b"},
            )
        assert resp.status_code == 200
        assert resp.json()["id"] == first_id
        dispatch_mock.assert_not_called()

    def test_no_hard_max_size_constant_added(self):
        # Defensive: make sure no "max size" constant has crept in. #126
        # explicitly forbids a hard upper bound on model size.
        public = {n for n in dir(model_pulls) if not n.startswith("_")}
        for forbidden in ("MAX_MODEL_SIZE_BYTES", "MAX_PULL_BYTES"):
            assert forbidden not in public

    def test_no_new_tables_introduced(self, db_path):
        # #126 must stay on the warning side — no schema changes.
        with sqlite3.connect(db_path) as conn:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        for forbidden in (
            "model_warnings",
            "model_size_limits",
            "user_model_access",  # #112
            "model_access",       # #112
        ):
            assert forbidden not in tables
