"""
HTTP tests for the local feedback endpoints (POST/GET/DELETE /feedback)
and the assistant_message_id surfaced on the chat-stream `done` event.

These pin the user-visible contract:

  * a user can record positive/negative ratings via the API;
  * a user can list and delete only their own ratings;
  * the streaming endpoint exposes the assistant message id so the
    browser can attach feedback to the row that was just persisted;
  * the existing non-streaming endpoint exposes the same id for
    consistency.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

for _mod in ("ddgs", "ollama", "sgmllib", "feedparser"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from core import chat as chat_module  # noqa: E402
from core import memory as core_memory, users  # noqa: E402
from memory import store as natural_store  # noqa: E402


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "nova.db")
    monkeypatch.setattr(core_memory, "DB_PATH", path)
    monkeypatch.setattr(natural_store, "DB_PATH", path)
    core_memory.initialize_db()
    return path


def _make_user(db_path, username, password="pw", role=users.ROLE_USER):
    with sqlite3.connect(db_path) as conn:
        return users.create_user(conn, username, password, role=role)


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


@contextlib.contextmanager
def _stub_chat_stream_runtime(chunks):
    def fake_chat(*args, **kwargs):
        if kwargs.get("stream"):
            def gen():
                for c in chunks:
                    yield {"message": {"content": c}, "done": False}
                yield {"message": {"content": ""}, "done": True}
            return gen()
        return {"message": {"content": "".join(chunks)}}

    with patch.object(chat_module.client, "chat", side_effect=fake_chat), \
            patch.object(chat_module, "route", lambda _msg: "default"), \
            patch.object(chat_module, "should_search", lambda _msg: False), \
            patch.object(chat_module, "is_security_query", lambda _msg: False), \
            patch.object(chat_module, "detect_weather_city", lambda _msg: None), \
            patch.object(chat_module, "get_relevant_memories", lambda *_a, **_k: []), \
            patch.object(chat_module, "extract_and_save_memory", lambda *_a, **_k: None), \
            patch.object(
                chat_module, "_extract_and_save_natural_memories",
                lambda *_a, **_k: None,
            ):
        yield


# ── Endpoint contract ───────────────────────────────────────────────────────

class TestFeedbackEndpoints:
    def test_post_positive_returns_id(self, db_path, web_client):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        resp = web_client.post(
            "/feedback",
            json={"sentiment": "positive"},
            headers=_h(token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["feedback_id"] > 0

    def test_post_negative_with_reason_persists_clean_text(
        self, db_path, web_client,
    ):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        web_client.post(
            "/feedback",
            json={
                "sentiment": "negative",
                "reason": "  Be more concrete.  ",
            },
            headers=_h(token),
        )
        listed = web_client.get("/feedback", headers=_h(token)).json()
        assert listed[0]["reason"] == "Be more concrete."

    def test_invalid_sentiment_rejected(self, db_path, web_client):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        resp = web_client.post(
            "/feedback",
            json={"sentiment": "maybe"},
            headers=_h(token),
        )
        assert resp.status_code == 422

    def test_secret_shaped_reason_rejected_400(self, db_path, web_client):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        resp = web_client.post(
            "/feedback",
            json={
                "sentiment": "negative",
                "reason": "token = ghp_" + "x" * 30,
            },
            headers=_h(token),
        )
        assert resp.status_code == 400
        # Nothing was stored.
        listed = web_client.get("/feedback", headers=_h(token)).json()
        assert listed == []

    def test_list_only_returns_callers_rows(self, db_path, web_client):
        _make_user(db_path, "alice")
        _make_user(db_path, "bob")
        a = _login(web_client, "alice")
        b = _login(web_client, "bob")
        web_client.post(
            "/feedback", json={"sentiment": "positive"}, headers=_h(a),
        )
        web_client.post(
            "/feedback",
            json={"sentiment": "negative", "reason": "meh"},
            headers=_h(b),
        )
        alice_rows = web_client.get("/feedback", headers=_h(a)).json()
        bob_rows = web_client.get("/feedback", headers=_h(b)).json()
        assert [r["sentiment"] for r in alice_rows] == ["positive"]
        assert [r["sentiment"] for r in bob_rows] == ["negative"]
        assert [r["reason"] for r in bob_rows] == ["meh"]

    def test_delete_refuses_other_user_with_404(self, db_path, web_client):
        _make_user(db_path, "alice")
        _make_user(db_path, "bob")
        a = _login(web_client, "alice")
        b = _login(web_client, "bob")
        rid = web_client.post(
            "/feedback", json={"sentiment": "positive"}, headers=_h(a),
        ).json()["feedback_id"]
        # 404 (not 403) so cross-user probing cannot reveal existence.
        resp = web_client.delete(f"/feedback/{rid}", headers=_h(b))
        assert resp.status_code == 404
        # Alice's row survives.
        assert len(web_client.get("/feedback", headers=_h(a)).json()) == 1

    def test_delete_succeeds_for_owner(self, db_path, web_client):
        _make_user(db_path, "alice")
        a = _login(web_client, "alice")
        rid = web_client.post(
            "/feedback", json={"sentiment": "positive"}, headers=_h(a),
        ).json()["feedback_id"]
        resp = web_client.delete(f"/feedback/{rid}", headers=_h(a))
        assert resp.status_code == 200
        assert web_client.get("/feedback", headers=_h(a)).json() == []

    def test_missing_auth_rejected(self, web_client):
        resp = web_client.post("/feedback", json={"sentiment": "positive"})
        # FastAPI's HTTPBearer dependency answers with 401 when no
        # Authorization header is present, 403 in some configurations.
        # Accept either — the contract is "no anonymous writes".
        assert resp.status_code in (401, 403)


# ── Streaming wire ──────────────────────────────────────────────────────────

class TestStreamSurfacesMessageId:
    def test_done_event_carries_assistant_message_id(
        self, db_path, web_client,
    ):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        with _stub_chat_stream_runtime(["hello"]):
            with web_client.stream(
                "POST", "/chat/stream",
                json={"message": "hi"},
                headers=_h(token),
            ) as resp:
                assert resp.status_code == 200
                lines = [
                    json.loads(line) for line in resp.iter_lines() if line
                ]
        done_events = [e for e in lines if e.get("type") == "done"]
        assert done_events
        # The id is surfaced so the browser can attach feedback to the
        # row that was just persisted.
        assert isinstance(done_events[-1].get("assistant_message_id"), int)
        assert done_events[-1]["assistant_message_id"] > 0

    def test_feedback_attaches_to_emitted_message_id(
        self, db_path, web_client,
    ):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        with _stub_chat_stream_runtime(["hello"]):
            with web_client.stream(
                "POST", "/chat/stream",
                json={"message": "hi"},
                headers=_h(token),
            ) as resp:
                lines = [
                    json.loads(line) for line in resp.iter_lines() if line
                ]
        done = next(e for e in lines if e.get("type") == "done")
        mid = done["assistant_message_id"]

        web_client.post(
            "/feedback",
            json={"sentiment": "negative", "message_id": mid,
                  "reason": "Too generic"},
            headers=_h(token),
        )
        rows = web_client.get("/feedback", headers=_h(token)).json()
        assert rows[0]["message_id"] == mid


# ── Non-streaming parity ────────────────────────────────────────────────────

class TestNonStreamingExposesMessageId:
    def test_chat_endpoint_returns_assistant_message_id(
        self, db_path, web_client,
    ):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        fake = MagicMock(return_value={"message": {"content": "hello"}})
        with patch.object(chat_module.client, "chat", fake), \
                patch.object(chat_module, "route", lambda _msg: "default"), \
                patch.object(
                    chat_module, "should_search", lambda _msg: False,
                ), \
                patch.object(
                    chat_module, "is_security_query", lambda _msg: False,
                ), \
                patch.object(
                    chat_module, "detect_weather_city", lambda _msg: None,
                ), \
                patch.object(
                    chat_module, "get_relevant_memories",
                    lambda *_a, **_k: [],
                ), \
                patch.object(
                    chat_module, "extract_and_save_memory",
                    lambda *_a, **_k: None,
                ), \
                patch.object(
                    chat_module, "_extract_and_save_natural_memories",
                    lambda *_a, **_k: None,
                ):
            resp = web_client.post(
                "/chat", json={"message": "hi"}, headers=_h(token),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body.get("assistant_message_id"), int)
        assert body["assistant_message_id"] > 0
