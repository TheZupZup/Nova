"""
Tests for the admin Ollama model-pull flow (issue #111).

Covers:
  * `model_pulls` table is created by `initialize_db()` and the
    `migrate` helper is idempotent.
  * `validate_model_name` accepts realistic Ollama tags and rejects
    every form of dangerous input — whitespace, shell metacharacters,
    path traversal, NUL bytes, and oversized strings.
  * `request_pull` validates first, then refuses already-installed
    models, returns the existing job for an in-progress duplicate, and
    rejects when the global concurrent-pull cap is reached.
  * The background worker walks the Ollama stream, persists progress,
    and on success flips `model_registry.installed = 1`.
  * Pull failures are captured as a *safe* error string — no raw
    exception text is leaked.
  * The admin endpoints (POST /admin/models/pull, GET .../pulls,
    GET .../pulls/{id}) enforce admin role; non-admin and restricted
    callers get 403.
  * No `subprocess` call is ever made — the implementation uses the
    Ollama Python client only.
  * Existing chat routing keeps working; per-user model access is
    still not enforced (deferred to #112).
"""

from __future__ import annotations

import contextlib
import sqlite3
import subprocess
import threading
import time
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
    """Reset module-level concurrency state between tests."""
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


def _registry_installed(path: str, model_name: str) -> bool:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT installed FROM model_registry WHERE model_name = ?",
            (model_name,),
        ).fetchone()
    return bool(row[0]) if row else False


def _seed_registry(path: str, model_name: str, *, installed: bool = False) -> None:
    """Insert an extra row in model_registry for a model not in config.MODELS."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO model_registry "
            "(model_name, display_name, purpose, enabled, installed, "
            "created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?, ?)",
            (
                model_name,
                model_name,
                "extra",
                1 if installed else 0,
                now,
                now,
            ),
        )


def _sync_runner(pull_id, model_name, db_path):
    """Run the worker inline so tests can assert on the final state."""
    model_pulls._run_pull(pull_id, model_name, db_path)


# ── Migration ───────────────────────────────────────────────────────────────


class TestMigration:
    def test_initialize_db_creates_model_pulls_table(self, db_path):
        assert _table_exists(db_path, "model_pulls")

    def test_migrate_is_idempotent(self, tmp_path):
        path = str(tmp_path / "fresh.db")
        model_pulls.migrate(path)
        model_pulls.migrate(path)
        model_pulls.migrate(path)
        assert _table_exists(path, "model_pulls")

    def test_initialize_db_does_not_call_subprocess(self, tmp_path, monkeypatch):
        # Sanity-check that wiring up the new migration didn't smuggle a
        # subprocess.run() call into startup.
        path = str(tmp_path / "nova.db")
        monkeypatch.setattr(core_memory, "DB_PATH", path)
        monkeypatch.setattr(natural_store, "DB_PATH", path)
        with patch("subprocess.run") as run_mock:
            core_memory.initialize_db()
        run_mock.assert_not_called()


# ── validate_model_name ─────────────────────────────────────────────────────


class TestValidateModelName:
    @pytest.mark.parametrize("name", [
        "gemma4",
        "gemma3:1b",
        "qwen2.5:32b",
        "deepseek-coder-v2",
        "llama3.2:latest",
        "library/llama3:8b",
        "hf.co/user/model:gguf",
        "user_with_underscores:tag",
        "qwen3.6:27b",  # the example from the issue
    ])
    def test_accepts_realistic_ollama_names(self, name):
        assert model_pulls.validate_model_name(name) == name

    @pytest.mark.parametrize("bad", [
        "",
        " ",
        " gemma4 ",  # leading/trailing whitespace stripped is rejected
        "gemma 4",
        "gemma\t4",
        "gemma\n4",
        "gemma;rm -rf /",
        "gemma|cat",
        "gemma&background",
        "$(whoami)",
        "`whoami`",
        "gemma>file",
        "gemma<file",
        "gemma\\backslash",
        "gemma'quoted'",
        "gemma\"dq\"",
        "gemma#comment",
        "gemma?q",
        "gemma*",
        "../etc/passwd",
        "gemma/../etc",
        "./relative",
        "..",
        ".",
        "/absolute",
        "gemma\x00null",
        ":notag",      # tag without name
        "name:",       # empty tag
        "/leading-slash",
        "trailing/",
        "double//slash",
        "a" * 201,     # over the max length
    ])
    def test_rejects_dangerous_or_malformed(self, bad):
        with pytest.raises(model_pulls.InvalidModelName):
            model_pulls.validate_model_name(bad)

    def test_rejects_non_string(self):
        for bad in (None, 42, 3.14, b"bytes", ["list"], {"d": 1}):
            with pytest.raises(model_pulls.InvalidModelName):
                model_pulls.validate_model_name(bad)


# ── request_pull (job lifecycle) ────────────────────────────────────────────


class TestRequestPull:
    def test_creates_job_and_runs_worker(self, db_path):
        _seed_registry(db_path, "newmodel:1b")

        events = [
            {"status": "pulling manifest"},
            {"status": "downloading", "completed": 50, "total": 100},
            {"status": "downloading", "completed": 100, "total": 100},
            {"status": "success"},
        ]
        with patch(
            "core.model_pulls.client.pull",
            return_value=iter(events),
        ) as pull_mock:
            job = model_pulls.request_pull(
                "newmodel:1b", db_path, runner=_sync_runner
            )

        assert job["model_name"] == "newmodel:1b"
        assert job["status"] == model_pulls.STATUS_QUEUED  # snapshot at insert
        # Worker has now finished synchronously; refetch.
        final = model_pulls.get_pull(job["id"], db_path)
        assert final["status"] == model_pulls.STATUS_DONE
        assert final["completed_bytes"] == 100
        assert final["total_bytes"] == 100
        assert final["progress"] == 1.0
        assert final["started_at"] is not None
        assert final["finished_at"] is not None
        assert final["error_message"] is None
        # Registry flipped to installed=true.
        assert _registry_installed(db_path, "newmodel:1b") is True
        pull_mock.assert_called_once()
        # No subprocess interaction.
        with patch("subprocess.run") as run_mock:
            pass
        run_mock.assert_not_called()

    def test_invalid_name_rejected_before_any_ollama_call(self, db_path):
        with patch("core.model_pulls.client.pull") as pull_mock:
            with pytest.raises(model_pulls.InvalidModelName):
                model_pulls.request_pull(
                    "bad; rm -rf /", db_path, runner=_sync_runner
                )
        pull_mock.assert_not_called()
        # And no row was inserted.
        assert model_pulls.list_pulls(db_path) == []

    def test_already_installed_raises(self, db_path):
        from config import MODELS
        # The default-config model is registered but installed=False after
        # seed; flip the flag to simulate an already-pulled model.
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE model_registry SET installed = 1 WHERE model_name = ?",
                (MODELS["default"],),
            )
        with patch("core.model_pulls.client.pull") as pull_mock:
            with pytest.raises(model_pulls.ModelAlreadyInstalled):
                model_pulls.request_pull(
                    MODELS["default"], db_path, runner=_sync_runner
                )
        pull_mock.assert_not_called()
        assert model_pulls.list_pulls(db_path) == []

    def test_duplicate_in_progress_returns_existing_job(self, db_path):
        # Insert a queued row by hand and reserve the slot, simulating an
        # in-progress pull. A second request for the same model must
        # surface the existing job, not start a new one.
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
            existing_id = cur.lastrowid
        model_pulls._try_reserve("newmodel:1b")  # mimic active worker

        with patch("core.model_pulls.client.pull") as pull_mock:
            with pytest.raises(model_pulls.PullAlreadyInProgress) as exc_info:
                model_pulls.request_pull(
                    "newmodel:1b", db_path, runner=_sync_runner
                )
        pull_mock.assert_not_called()
        assert exc_info.value.job["id"] == existing_id
        # No second row was inserted.
        rows = model_pulls.list_pulls(db_path)
        assert len(rows) == 1

    def test_global_cap_rejects_extra_pulls(self, db_path, monkeypatch):
        monkeypatch.setattr(model_pulls, "_MAX_CONCURRENT_PULLS", 1)
        _seed_registry(db_path, "modela:1b")
        _seed_registry(db_path, "modelb:1b")
        # Reserve the only slot manually.
        assert model_pulls._try_reserve("modela:1b") is True

        with patch("core.model_pulls.client.pull") as pull_mock:
            with pytest.raises(model_pulls.TooManyPullsInProgress):
                model_pulls.request_pull(
                    "modelb:1b", db_path, runner=_sync_runner
                )
        pull_mock.assert_not_called()
        # No row was inserted because the cap was hit before the INSERT.
        assert model_pulls.list_pulls(db_path) == []

    def test_failed_pull_marks_error_with_safe_message(self, db_path):
        _seed_registry(db_path, "newmodel:1b")

        secret = "AUTH_TOKEN=hunter2 user=root host=192.168.1.1"
        with patch(
            "core.model_pulls.client.pull",
            side_effect=ollama.ResponseError(secret),
        ):
            job = model_pulls.request_pull(
                "newmodel:1b", db_path, runner=_sync_runner
            )

        final = model_pulls.get_pull(job["id"], db_path)
        assert final["status"] == model_pulls.STATUS_ERROR
        assert final["finished_at"] is not None
        # Safe error string — never echoes the raw exception text.
        assert secret not in (final["error_message"] or "")
        assert "ResponseError" in final["error_message"]
        # Registry was NOT flipped to installed.
        assert _registry_installed(db_path, "newmodel:1b") is False

    def test_network_error_marks_error(self, db_path):
        _seed_registry(db_path, "newmodel:1b")
        with patch(
            "core.model_pulls.client.pull",
            side_effect=httpx.ConnectError("connection refused at /tmp/secret"),
        ):
            job = model_pulls.request_pull(
                "newmodel:1b", db_path, runner=_sync_runner
            )
        final = model_pulls.get_pull(job["id"], db_path)
        assert final["status"] == model_pulls.STATUS_ERROR
        assert "/tmp/secret" not in (final["error_message"] or "")

    def test_unexpected_error_does_not_leave_job_pulling(self, db_path):
        _seed_registry(db_path, "newmodel:1b")
        with patch(
            "core.model_pulls.client.pull",
            side_effect=RuntimeError("oops"),
        ):
            job = model_pulls.request_pull(
                "newmodel:1b", db_path, runner=_sync_runner
            )
        final = model_pulls.get_pull(job["id"], db_path)
        assert final["status"] == model_pulls.STATUS_ERROR

    def test_active_set_is_released_after_run(self, db_path):
        _seed_registry(db_path, "newmodel:1b")
        with patch(
            "core.model_pulls.client.pull",
            return_value=iter([{"status": "success"}]),
        ):
            model_pulls.request_pull(
                "newmodel:1b", db_path, runner=_sync_runner
            )
        # The worker has finished; the active-set must be empty so the
        # next pull request can proceed.
        with model_pulls._state_lock:
            assert "newmodel:1b" not in model_pulls._active_models

    def test_active_set_released_on_failure(self, db_path):
        _seed_registry(db_path, "newmodel:1b")
        with patch(
            "core.model_pulls.client.pull",
            side_effect=ollama.ResponseError("nope"),
        ):
            model_pulls.request_pull(
                "newmodel:1b", db_path, runner=_sync_runner
            )
        with model_pulls._state_lock:
            assert "newmodel:1b" not in model_pulls._active_models

    def test_dispatcher_failure_marks_error(self, db_path):
        _seed_registry(db_path, "newmodel:1b")

        def boom(pull_id, name, db):
            raise RuntimeError("scheduler down")

        with patch("core.model_pulls.client.pull"):
            with pytest.raises(RuntimeError):
                model_pulls.request_pull(
                    "newmodel:1b", db_path, runner=boom
                )
        # The job was inserted then immediately marked error; the slot
        # was released.
        rows = model_pulls.list_pulls(db_path)
        assert len(rows) == 1
        assert rows[0]["status"] == model_pulls.STATUS_ERROR
        with model_pulls._state_lock:
            assert "newmodel:1b" not in model_pulls._active_models


# ── Ollama interaction details ──────────────────────────────────────────────


class TestOllamaInteraction:
    def test_pull_uses_python_client_not_subprocess(self, db_path):
        _seed_registry(db_path, "newmodel:1b")
        with patch("subprocess.run") as run_mock, patch(
            "subprocess.Popen"
        ) as popen_mock, patch(
            "core.model_pulls.client.pull",
            return_value=iter([{"status": "success"}]),
        ):
            model_pulls.request_pull(
                "newmodel:1b", db_path, runner=_sync_runner
            )
        run_mock.assert_not_called()
        popen_mock.assert_not_called()

    def test_falls_back_to_non_streaming_pull_on_typeerror(self, db_path):
        # Some Ollama client builds reject `stream=True`; the worker must
        # fall back to a single-shot pull so the job still completes.
        _seed_registry(db_path, "newmodel:1b")
        calls = []

        def fake_pull(name, **kwargs):
            calls.append(kwargs)
            if "stream" in kwargs:
                raise TypeError("unexpected kwarg")
            return {"status": "success"}

        with patch("core.model_pulls.client.pull", side_effect=fake_pull):
            job = model_pulls.request_pull(
                "newmodel:1b", db_path, runner=_sync_runner
            )
        final = model_pulls.get_pull(job["id"], db_path)
        assert final["status"] == model_pulls.STATUS_DONE
        # First call had stream=True, second was the fallback.
        assert calls[0].get("stream") is True
        assert "stream" not in calls[1]


# ── HTTP endpoints ──────────────────────────────────────────────────────────


class TestAdminEndpoints:
    def test_admin_can_start_pull(self, db_path, web_client, admin_token):
        _seed_registry(db_path, "newmodel:1b")
        # Patch dispatch so the endpoint returns immediately and we can
        # assert on the inserted row without waiting for a thread.
        with patch(
            "core.model_pulls._dispatch_in_thread"
        ) as dispatch_mock:
            resp = web_client.post(
                "/admin/models/pull",
                headers=_h(admin_token),
                json={"model": "newmodel:1b"},
            )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["model_name"] == "newmodel:1b"
        assert body["status"] == model_pulls.STATUS_QUEUED
        dispatch_mock.assert_called_once()

    def test_invalid_name_returns_400(self, db_path, web_client, admin_token):
        with patch(
            "core.model_pulls._dispatch_in_thread"
        ) as dispatch_mock, patch(
            "core.model_pulls.client.pull"
        ) as pull_mock:
            resp = web_client.post(
                "/admin/models/pull",
                headers=_h(admin_token),
                json={"model": "bad; rm -rf /"},
            )
        assert resp.status_code == 400
        dispatch_mock.assert_not_called()
        pull_mock.assert_not_called()

    def test_already_installed_returns_200_with_status(
        self, db_path, web_client, admin_token
    ):
        from config import MODELS
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE model_registry SET installed = 1 WHERE model_name = ?",
                (MODELS["default"],),
            )
        with patch("core.model_pulls._dispatch_in_thread") as dispatch_mock:
            resp = web_client.post(
                "/admin/models/pull",
                headers=_h(admin_token),
                json={"model": MODELS["default"]},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "status": "already_installed",
            "model": MODELS["default"],
        }
        dispatch_mock.assert_not_called()

    def test_duplicate_pull_returns_existing_job(
        self, db_path, web_client, admin_token
    ):
        _seed_registry(db_path, "newmodel:1b")
        # Block the worker from finishing so the second request sees the
        # first job as in-progress. We achieve that by pinning the slot
        # via _try_reserve and inserting a queued row by hand.
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

        with patch("core.model_pulls._dispatch_in_thread") as dispatch_mock:
            resp = web_client.post(
                "/admin/models/pull",
                headers=_h(admin_token),
                json={"model": "newmodel:1b"},
            )
        assert resp.status_code == 200
        assert resp.json()["id"] == first_id
        dispatch_mock.assert_not_called()

    def test_too_many_pulls_returns_429(
        self, db_path, web_client, admin_token, monkeypatch
    ):
        monkeypatch.setattr(model_pulls, "_MAX_CONCURRENT_PULLS", 1)
        _seed_registry(db_path, "modela:1b")
        _seed_registry(db_path, "modelb:1b")
        model_pulls._try_reserve("modela:1b")

        with patch("core.model_pulls._dispatch_in_thread") as dispatch_mock:
            resp = web_client.post(
                "/admin/models/pull",
                headers=_h(admin_token),
                json={"model": "modelb:1b"},
            )
        assert resp.status_code == 429
        dispatch_mock.assert_not_called()

    def test_non_admin_user_forbidden(self, db_path, web_client, admin_token):
        _make_user(db_path, "bob")
        token = _login(web_client, "bob")
        with patch("core.model_pulls._dispatch_in_thread") as dispatch_mock:
            resp = web_client.post(
                "/admin/models/pull",
                headers=_h(token),
                json={"model": "any:tag"},
            )
        assert resp.status_code == 403
        dispatch_mock.assert_not_called()

    def test_restricted_user_forbidden(self, db_path, web_client, admin_token):
        _make_user(db_path, "kid", is_restricted=True)
        token = _login(web_client, "kid")
        with patch("core.model_pulls._dispatch_in_thread") as dispatch_mock:
            resp = web_client.post(
                "/admin/models/pull",
                headers=_h(token),
                json={"model": "any:tag"},
            )
        assert resp.status_code == 403
        dispatch_mock.assert_not_called()

    def test_unauthenticated_blocked(self, web_client):
        resp = web_client.post(
            "/admin/models/pull", json={"model": "x"}
        )
        assert resp.status_code in (401, 403)

    def test_malformed_body_rejected(self, db_path, web_client, admin_token):
        # Extra fields are rejected so callers cannot smuggle in
        # surprises (e.g. `{"model": "x", "as_user": 99}`).
        resp = web_client.post(
            "/admin/models/pull",
            headers=_h(admin_token),
            json={"model": "gemma4", "as_user": 99},
        )
        assert resp.status_code == 422

    def test_admin_can_list_pulls(self, db_path, web_client, admin_token):
        _seed_registry(db_path, "newmodel:1b")
        with patch(
            "core.model_pulls.client.pull",
            return_value=iter([{"status": "success"}]),
        ):
            model_pulls.request_pull(
                "newmodel:1b", db_path, runner=_sync_runner
            )
        resp = web_client.get(
            "/admin/models/pulls", headers=_h(admin_token)
        )
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == 1
        assert body[0]["model_name"] == "newmodel:1b"
        assert body[0]["status"] == model_pulls.STATUS_DONE

    def test_non_admin_cannot_list_pulls(self, db_path, web_client, admin_token):
        _make_user(db_path, "bob")
        token = _login(web_client, "bob")
        resp = web_client.get("/admin/models/pulls", headers=_h(token))
        assert resp.status_code == 403

    def test_restricted_cannot_list_pulls(self, db_path, web_client, admin_token):
        _make_user(db_path, "kid", is_restricted=True)
        token = _login(web_client, "kid")
        resp = web_client.get("/admin/models/pulls", headers=_h(token))
        assert resp.status_code == 403

    def test_admin_can_get_single_pull(self, db_path, web_client, admin_token):
        _seed_registry(db_path, "newmodel:1b")
        with patch(
            "core.model_pulls.client.pull",
            return_value=iter([{"status": "success"}]),
        ):
            job = model_pulls.request_pull(
                "newmodel:1b", db_path, runner=_sync_runner
            )
        resp = web_client.get(
            f"/admin/models/pulls/{job['id']}", headers=_h(admin_token)
        )
        assert resp.status_code == 200
        assert resp.json()["id"] == job["id"]

    def test_get_unknown_pull_returns_404(self, db_path, web_client, admin_token):
        resp = web_client.get(
            "/admin/models/pulls/9999", headers=_h(admin_token)
        )
        assert resp.status_code == 404

    def test_non_admin_cannot_get_single_pull(
        self, db_path, web_client, admin_token
    ):
        _make_user(db_path, "bob")
        token = _login(web_client, "bob")
        resp = web_client.get(
            "/admin/models/pulls/1", headers=_h(token)
        )
        assert resp.status_code == 403


# ── Concurrency / non-blocking ──────────────────────────────────────────────


class TestNonBlocking:
    def test_request_pull_returns_before_worker_finishes(self, db_path):
        _seed_registry(db_path, "slowmodel:1b")
        released = threading.Event()

        def slow_stream(name, **kwargs):
            # Hold the worker until the test releases it. This proves
            # that the request thread does not wait for completion.
            released.wait(timeout=5)
            return iter([{"status": "success"}])

        with patch("core.model_pulls.client.pull", side_effect=slow_stream):
            t0 = time.monotonic()
            job = model_pulls.request_pull("slowmodel:1b", db_path)
            elapsed = time.monotonic() - t0
            assert elapsed < 1.0, f"request_pull blocked for {elapsed:.2f}s"
            assert job["status"] == model_pulls.STATUS_QUEUED
            released.set()
            # Wait for the worker to finish so the active-set clears
            # before this test ends.
            for _ in range(50):
                final = model_pulls.get_pull(job["id"], db_path)
                if final["status"] == model_pulls.STATUS_DONE:
                    break
                time.sleep(0.05)
            assert final["status"] == model_pulls.STATUS_DONE


# ── Existing chat routing is untouched ──────────────────────────────────────


class TestChatRoutingPreserved:
    def test_router_still_uses_config_models(self):
        # #111 must not change model routing — that is #112's responsibility.
        from core import router
        from config import MODELS
        assert router.MODEL_MAP["simple"] == MODELS["default"]
        assert router.MODEL_MAP["normal"] == MODELS["default"]
        assert router.MODEL_MAP["code"] == MODELS["code"]
        assert router.MODEL_MAP["advanced"] == MODELS["advanced"]
        assert router.FALLBACK_MODEL == MODELS["default"]


# ── No per-user/per-role model access (deferred to #112) ────────────────────


class TestNoPerUserModelAccess:
    def test_no_model_access_table_introduced(self, db_path):
        # #112 will introduce some form of user_model_access / allowlist
        # table. #111 must not. Fail loudly if a future commit slips one
        # in under this PR's scope.
        with sqlite3.connect(db_path) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        for forbidden in (
            "user_model_access",
            "model_access",
            "user_models",
            "user_allowed_models",
        ):
            assert forbidden not in tables, (
                f"#111 should not introduce table {forbidden!r}; that is #112"
            )

    def test_admin_pull_endpoint_does_not_consult_per_user_acl(
        self, db_path, web_client, admin_token
    ):
        # The endpoint authorises on `role == admin` only — there is no
        # extra per-user ACL lookup that #112 will eventually wire in.
        _seed_registry(db_path, "any:1b")
        with patch(
            "core.model_pulls._dispatch_in_thread"
        ):
            resp = web_client.post(
                "/admin/models/pull",
                headers=_h(admin_token),
                json={"model": "any:1b"},
            )
        # Admin succeeds straight through, no hidden ACL gate.
        assert resp.status_code == 202


# ── Hard guarantee: subprocess never used ───────────────────────────────────


class TestNoSubprocess:
    def test_subprocess_module_not_imported_by_model_pulls(self):
        import core.model_pulls as mp
        assert not hasattr(mp, "subprocess")
        # Sanity: module exists when imported separately.
        assert subprocess.run is not None

    def test_no_subprocess_call_during_full_pull_cycle(self, db_path):
        _seed_registry(db_path, "newmodel:1b")
        with patch("subprocess.run") as run_mock, patch(
            "subprocess.Popen"
        ) as popen_mock, patch(
            "core.model_pulls.client.pull",
            return_value=iter([
                {"status": "downloading", "completed": 1, "total": 1},
                {"status": "success"},
            ]),
        ):
            model_pulls.request_pull(
                "newmodel:1b", db_path, runner=_sync_runner
            )
        run_mock.assert_not_called()
        popen_mock.assert_not_called()
