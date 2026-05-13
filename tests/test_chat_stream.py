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


class _SubscriptableEvent:
    """Stand-in for ``ollama.ChatResponse`` (a ``SubscriptableBaseModel``).

    Real ollama-python streams Pydantic objects that expose ``.get()``
    and ``[key]`` access but are **not** ``dict`` instances. The plain
    dict mocks elsewhere in this file happen to satisfy any
    ``isinstance(event, dict)`` filter; production ``ChatResponse``
    objects do not — that mismatch was the root cause of empty replies
    in production. Using this shape in regression tests pins the
    contract without importing the live ollama package.
    """

    def __init__(self, **fields):
        self._fields = fields

    def get(self, key, default=None):
        return self._fields.get(key, default)

    def __getitem__(self, key):
        return self._fields[key]


@contextlib.contextmanager
def stub_chat_stream_runtime(chunks, *, event_shape="dict"):
    """Replace Ollama's chat() with a fake streaming iterator.

    ``chunks`` is a list of strings to surface as message.content
    fragments. ``event_shape`` controls whether each streamed event is a
    plain ``dict`` (legacy) or a ``_SubscriptableEvent`` mirroring the
    real ``ollama.ChatResponse`` Pydantic model — the latter is the
    shape that ships in production and the one that broke generation
    before this guardrail existed. Routing, weather, search, security
    and memory hooks are all neutralised so the generator follows the
    plain chat path.
    """
    def _wrap(content, done):
        payload = {"message": {"content": content}, "done": done}
        if event_shape == "subscriptable":
            return _SubscriptableEvent(
                message=_SubscriptableEvent(content=content),
                done=done,
            )
        return payload

    def fake_chat(*args, **kwargs):
        if kwargs.get("stream"):
            def gen():
                for c in chunks:
                    yield _wrap(c, False)
                yield _wrap("", True)
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

    def test_subscriptable_chat_response_events_are_not_dropped(self, db_path):
        # Regression guard: ``ollama>=0.4`` streams ``ChatResponse``
        # Pydantic objects, which are subscriptable but **not** ``dict``
        # instances. A previous implementation used
        # ``isinstance(event, dict)`` to filter the stream and silently
        # dropped every production chunk — leaving the final accumulator
        # empty and surfacing "Nova didn't produce a reply." for even a
        # trivial "bonjour". This exercises the same shape end-to-end.
        alice = _make_user(db_path, "alice")
        with stub_chat_stream_runtime(
            ["bon", "jour"], event_shape="subscriptable",
        ):
            events = list(chat_stream([], "bonjour", [], alice))
        deltas = [e["content"] for e in events if e["type"] == "delta"]
        assert deltas == ["bon", "jour"]
        done = events[-1]
        assert done["type"] == "done"
        assert done["reply"] == "bonjour"

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

    def test_many_small_chunks_each_emit_a_delta(self, db_path, web_client):
        # Ollama frequently emits 1–2 character chunks. The endpoint must
        # forward each one as its own `delta` event so the frontend's
        # buffered renderer can flush them at its own cadence — never
        # batch them server-side. The total concatenated text must still
        # match exactly what was sent and what gets persisted.
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")

        pieces = ["H", "el", "lo", ", ", "Nova", "!", " "]
        with stub_chat_stream_runtime(pieces):
            resp = web_client.post(
                "/chat/stream",
                json={"message": "salut", "mode": "chat"},
                headers=_h(token),
            )
        assert resp.status_code == 200
        events = _decode_ndjson(resp.content)
        deltas = [e["content"] for e in events if e["type"] == "delta"]
        # Each chunk arrives as its own delta — no server-side coalescing.
        assert deltas == pieces
        # And the final, persisted reply concatenates exactly the same bytes.
        done = next(e for e in events if e["type"] == "done")
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT content FROM messages WHERE id = ?",
                (done["assistant_message_id"],),
            ).fetchone()
        assert row[0] == "".join(pieces)

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

    def test_empty_model_output_surfaces_error_and_persists_nothing(
        self, db_path, web_client
    ):
        # When Ollama yields no content tokens at all, the endpoint must
        # NOT save an empty assistant row (it would render as a stray
        # Nova bubble on reload). Instead it surfaces an `error` event
        # so the frontend can render a calm fallback message.
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")

        with stub_chat_stream_runtime([]):
            resp = web_client.post(
                "/chat/stream",
                json={"message": "hi", "mode": "chat"},
                headers=_h(token),
            )
        assert resp.status_code == 200
        events = _decode_ndjson(resp.content)
        types = [e["type"] for e in events]
        assert "error" in types
        assert "done" not in types
        # No deltas — nothing reached the bubble.
        assert not any(e["type"] == "delta" for e in events)

        # No conversation should hold any messages: empty reply means
        # the turn is dropped on the floor, identical to a mid-stream
        # error. The frontend re-asks; we don't pollute history.
        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        assert count == 0

    def test_whitespace_only_reply_is_treated_as_error(
        self, db_path, web_client
    ):
        # Models occasionally emit whitespace-only deltas (e.g. just
        # newlines). Those carry no information so the endpoint should
        # treat them the same as an empty reply.
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")

        with stub_chat_stream_runtime(["   ", "\n"]):
            resp = web_client.post(
                "/chat/stream",
                json={"message": "hi", "mode": "chat"},
                headers=_h(token),
            )
        assert resp.status_code == 200
        events = _decode_ndjson(resp.content)
        types = [e["type"] for e in events]
        assert "error" in types
        assert "done" not in types
        with sqlite3.connect(db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE role = 'assistant'"
            ).fetchone()[0]
        assert count == 0

    def test_reload_after_empty_reply_has_no_assistant_rows(
        self, db_path, web_client
    ):
        # Bug guard: previously an empty model output left an empty
        # assistant message in the DB, so reloading the conversation
        # would render a stray empty bubble alongside the user message.
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")

        cid = web_client.post(
            "/conversations", json={"title": "alice"}, headers=_h(token)
        ).json()["id"]

        with stub_chat_stream_runtime([]):
            resp = web_client.post(
                "/chat/stream",
                json={"message": "hi", "conversation_id": cid, "mode": "chat"},
                headers=_h(token),
            )
        assert resp.status_code == 200

        msgs = web_client.get(
            f"/conversations/{cid}/messages", headers=_h(token)
        ).json()
        assistant = [m for m in msgs if m["role"] == "assistant"]
        assert assistant == []

    def test_error_event_during_stream_does_not_persist(
        self, db_path, web_client
    ):
        # Mid-stream backend failure (Ollama unreachable) → endpoint
        # forwards an `error` event and persists nothing.
        import ollama as _ollama

        class _FakeResponseError(Exception):
            pass

        _make_user(db_path, "alice")
        token = _login(web_client, "alice")

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
            resp = web_client.post(
                "/chat/stream",
                json={"message": "hi", "mode": "chat"},
                headers=_h(token),
            )
        assert resp.status_code == 200
        events = _decode_ndjson(resp.content)
        types = [e["type"] for e in events]
        assert "error" in types
        assert "done" not in types
        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        assert count == 0

    def test_bonjour_produces_reply_not_empty_fallback(
        self, db_path, web_client,
    ):
        # End-to-end regression: a simple "bonjour" prompt against a
        # production-shaped Ollama stream (``ChatResponse`` Pydantic
        # objects, not dicts) must produce a real assistant reply and
        # **not** the "Nova didn't produce a reply." fallback. This is
        # the exact user-visible bug that motivated this regression set.
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")

        with stub_chat_stream_runtime(
            ["Bon", "jour", " !"], event_shape="subscriptable",
        ):
            resp = web_client.post(
                "/chat/stream",
                json={"message": "bonjour", "mode": "chat"},
                headers=_h(token),
            )
        assert resp.status_code == 200
        events = _decode_ndjson(resp.content)
        types = [e["type"] for e in events]
        assert "done" in types
        assert "error" not in types
        deltas = [e["content"] for e in events if e["type"] == "delta"]
        assert "".join(deltas) == "Bonjour !"

        done = next(e for e in events if e["type"] == "done")
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT role, content FROM messages WHERE id = ?",
                (done["assistant_message_id"],),
            ).fetchone()
        assert row == ("assistant", "Bonjour !")

    def test_assistant_message_id_only_emitted_when_persisted(
        self, db_path, web_client
    ):
        # Feedback wiring requires the assistant_message_id surfaced on
        # `done` to match a real saved row. Make sure the empty-reply
        # path never emits a stale or fabricated id.
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")

        with stub_chat_stream_runtime([]):
            resp = web_client.post(
                "/chat/stream",
                json={"message": "hi", "mode": "chat"},
                headers=_h(token),
            )
        for ev in _decode_ndjson(resp.content):
            assert "assistant_message_id" not in ev or ev.get("type") == "done"

        # And the happy path *does* emit one tied to the saved row.
        with stub_chat_stream_runtime(["yo"]):
            resp = web_client.post(
                "/chat/stream",
                json={"message": "ping", "mode": "chat"},
                headers=_h(token),
            )
        done = next(e for e in _decode_ndjson(resp.content) if e["type"] == "done")
        assert isinstance(done.get("assistant_message_id"), int)
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT role, content FROM messages WHERE id = ?",
                (done["assistant_message_id"],),
            ).fetchone()
        assert row == ("assistant", "yo")


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
