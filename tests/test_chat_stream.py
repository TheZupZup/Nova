"""
Tests for the streaming chat path introduced alongside the compact chat UI.

Covers both layers:
  * ``core.chat.chat_stream`` — the generator that turns an Ollama stream
    into ``meta`` / ``delta`` / ``replace`` / ``done`` events.
  * ``/chat/stream`` (HTTP) — the FastAPI endpoint that forwards those
    events as NDJSON and persists exactly one assistant message on a
    clean completion.

The Ollama client and tool-calling helpers are stubbed end-to-end so
these tests never touch the network or the routing logic of the live
chat() function.
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
from core.chat import chat_stream  # noqa: E402
from memory import store as natural_store  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────

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
def stub_chat_stream_runtime(chunks):
    """Replace Ollama's chat() with a fake streaming iterator.

    ``chunks`` is a list of strings to surface as message.content
    fragments. Routing, weather, search, security and memory hooks are
    all neutralised so the generator follows the plain chat path.
    """
    def fake_chat(*args, **kwargs):
        if kwargs.get("stream"):
            def gen():
                for c in chunks:
                    yield {"message": {"content": c}, "done": False}
                yield {"message": {"content": ""}, "done": True}
            return gen()
        # Non-streaming fallback (e.g. memory extractor) — return single shot.
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


# ── core.chat.chat_stream ───────────────────────────────────────────────────

class TestChatStreamGenerator:
    def test_yields_meta_then_deltas_then_done(self, db_path):
        alice = _make_user(db_path, "alice")
        with stub_chat_stream_runtime(["Hel", "lo, ", "world!"]):
            events = list(chat_stream([], "hi", [], alice))

        assert events[0]["type"] == "meta"
        assert events[0]["model"]  # whatever the route stub returns
        deltas = [e["content"] for e in events if e["type"] == "delta"]
        assert deltas == ["Hel", "lo, ", "world!"]

        last = events[-1]
        assert last["type"] == "done"
        assert last["reply"] == "Hello, world!"

    def test_empty_stream_still_emits_done(self, db_path):
        alice = _make_user(db_path, "alice")
        with stub_chat_stream_runtime([]):
            events = list(chat_stream([], "hi", [], alice))
        assert events[0]["type"] == "meta"
        assert events[-1]["type"] == "done"
        assert events[-1]["reply"] == ""

    def test_ollama_unreachable_yields_error(self, db_path):
        alice = _make_user(db_path, "alice")
        import ollama as _ollama  # MagicMock in this env

        # Real ollama.ResponseError is what core.chat catches; the conftest
        # stubs ollama as a MagicMock, so we wire its ResponseError attr
        # to a concrete Exception subclass so the except-clause matches.
        class _FakeResponseError(Exception):
            pass
        with patch.object(_ollama, "ResponseError", _FakeResponseError), \
                patch.object(
                    chat_module.client, "chat",
                    side_effect=_FakeResponseError("nope"),
                ), \
                patch.object(chat_module, "route", lambda _msg: "default"), \
                patch.object(chat_module, "should_search", lambda _msg: False), \
                patch.object(chat_module, "is_security_query", lambda _msg: False), \
                patch.object(chat_module, "detect_weather_city", lambda _msg: None), \
                patch.object(chat_module, "get_relevant_memories", lambda *_a, **_k: []):
            events = list(chat_stream([], "hi", [], alice))

        assert any(e["type"] == "error" for e in events)
        assert not any(e["type"] == "done" for e in events)


# ── /chat/stream endpoint ──────────────────────────────────────────────────

def _decode_ndjson(body: bytes) -> list[dict]:
    out = []
    for line in body.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


class TestChatStreamEndpoint:
    def test_streams_ndjson_and_persists_one_assistant_message(
        self, db_path, web_client
    ):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")

        with stub_chat_stream_runtime(["he", "llo"]):
            resp = web_client.post(
                "/chat/stream",
                json={"message": "salut", "mode": "chat"},
                headers=_h(token),
            )
        assert resp.status_code == 200
        events = _decode_ndjson(resp.content)
        types = [e["type"] for e in events]

        assert "meta" in types
        assert "done" in types
        deltas = [e["content"] for e in events if e["type"] == "delta"]
        assert "".join(deltas) == "hello"

        # Exactly one user message and one assistant message persisted.
        conv_id = next(e["conversation_id"] for e in events if e["type"] == "done")
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT role, content FROM messages "
                "WHERE conversation_id = ? ORDER BY id",
                (conv_id,),
            ).fetchall()
        assert rows == [("user", "salut"), ("assistant", "hello")]

    def test_first_message_updates_conversation_title(self, db_path, web_client):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")

        with stub_chat_stream_runtime(["ok"]):
            resp = web_client.post(
                "/chat/stream",
                json={"message": "premier message complet et long", "mode": "chat"},
                headers=_h(token),
            )
        assert resp.status_code == 200
        conv_id = next(
            e["conversation_id"] for e in _decode_ndjson(resp.content) if e["type"] == "done"
        )
        with sqlite3.connect(db_path) as conn:
            title = conn.execute(
                "SELECT title FROM conversations WHERE id = ?", (conv_id,)
            ).fetchone()[0]
        # Title trimmed to first 40 chars of the user prompt.
        assert title == "premier message complet et long"

    def test_short_circuit_memory_command_streams_one_chunk(
        self, db_path, web_client
    ):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")

        # The "what do you remember" shortcut never reaches Ollama. The
        # endpoint still surfaces it as a normal NDJSON stream so the
        # frontend can use a single consumer for every path.
        resp = web_client.post(
            "/chat/stream",
            json={
                "message": "what do you remember about me?",
                "mode": "chat",
            },
            headers=_h(token),
        )
        assert resp.status_code == 200
        events = _decode_ndjson(resp.content)
        types = [e["type"] for e in events]
        assert types[0] == "meta"
        assert types[-1] == "done"
        assert any(e["type"] == "delta" for e in events)

    def test_unowned_conversation_returns_404(self, db_path, web_client):
        _make_user(db_path, "alice")
        _make_user(db_path, "bob")
        a_token = _login(web_client, "alice")
        b_token = _login(web_client, "bob")

        cid = web_client.post(
            "/conversations", json={"title": "alice"}, headers=_h(a_token)
        ).json()["id"]

        with stub_chat_stream_runtime(["nope"]):
            resp = web_client.post(
                "/chat/stream",
                json={"message": "hi", "conversation_id": cid, "mode": "chat"},
                headers=_h(b_token),
            )
        assert resp.status_code == 404

    def test_missing_auth_returns_401(self, web_client):
        # No Bearer token — should not start a stream.
        resp = web_client.post(
            "/chat/stream",
            json={"message": "hello", "mode": "chat"},
        )
        assert resp.status_code in (401, 403)

    def test_reload_shows_one_assistant_message(self, db_path, web_client):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")

        with stub_chat_stream_runtime(["full ", "reply"]):
            resp = web_client.post(
                "/chat/stream",
                json={"message": "ping", "mode": "chat"},
                headers=_h(token),
            )
        conv_id = next(
            e["conversation_id"] for e in _decode_ndjson(resp.content) if e["type"] == "done"
        )

        msgs = web_client.get(
            f"/conversations/{conv_id}/messages", headers=_h(token)
        ).json()
        assistant = [m for m in msgs if m["role"] == "assistant"]
        assert len(assistant) == 1
        assert assistant[0]["content"] == "full reply"


# ── /chat fallback still works ──────────────────────────────────────────────

class TestNonStreamingFallback:
    def test_chat_endpoint_still_returns_full_reply(self, db_path, web_client):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")

        with patch("web.chat", return_value=("classic reply", "stub-model")):
            resp = web_client.post(
                "/chat",
                json={"message": "hi", "mode": "chat"},
                headers=_h(token),
            )
        assert resp.status_code == 200
        assert resp.json()["response"] == "classic reply"
