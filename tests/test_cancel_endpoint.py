import contextlib
import json
import sqlite3
import sys
import pytest

from unittest.mock import MagicMock, patch
from core import chat as chat_module, memory as core_memory, ollama_client, users
from memory import store as natural_store
from fastapi.testclient import TestClient


for _mod in ("ddgs", "ollama", "sgmllib", "feedparser"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


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


def _decode_ndjson(body: bytes) -> list[dict]:
    """Decode NDJSON response body into list of dicts."""
    out = []
    for line in body.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def test_cancel_endpoint_sets_event_and_requires_owner(db_path, web_client):
    # Create a user and register a synthetic active generation.
    import web

    alice = _make_user(db_path, "alice")
    token = _login(web_client, "alice")

    request_id = web.tracker._request_id()
    cancel_event = web.tracker._register_active_generation(request_id, alice, 1)

    resp = web_client.post(f"/chat/cancel/{request_id}", headers=_h(token))
    assert resp.status_code == 200
    assert resp.json().get("cancelled") is True
    assert cancel_event.is_set()

    # Cleanup
    web.tracker._unregister_active_generation(request_id)


def test_cancel_endpoint_returns_404_for_other_user(db_path, web_client):
    import web

    alice = _make_user(db_path, "alice")
    _make_user(db_path, "bob")
    b_token = _login(web_client, "bob")

    request_id = web.tracker._request_id()
    cancel_event = web.tracker._register_active_generation(request_id, alice, 1)

    resp = web_client.post(f"/chat/cancel/{request_id}", headers=_h(b_token))
    assert resp.status_code == 404
    assert not cancel_event.is_set()

    web.tracker._unregister_active_generation(request_id)


def test_cancelled_stream_does_not_persist(db_path, web_client):
    """When a stream is cancelled mid-generation, nothing gets persisted.
    Simulates the scenario where:
    1. Client calls /chat/stream and streaming begins
    2. Provider's cancellation check catches the cancel_event being set
    3. Generation stops mid-stream with RequestCancelled
    4. Endpoint catches the exception and doesn't persist any messages
    """
    _make_user(db_path, "alice")
    token = _login(web_client, "alice")

    def fake_chat_raises_cancelled(*args, **kwargs):
        """Generator that yields one chunk then raises RequestCancelled."""
        if kwargs.get("stream"):
            def gen():
                # Yield one chunk to show generation started
                yield _SubscriptableEvent(
                    message=_SubscriptableEvent(content="hel"), done=False
                )
                # Then cancel is detected and RequestCancelled is raised
                from core.chat import RequestCancelled
                raise RequestCancelled()
            return gen()
        return {"message": {"content": "hello"}}

    with patch.object(ollama_client.client, "chat", side_effect=fake_chat_raises_cancelled), \
            patch.object(chat_module, "route", lambda _msg: "default"), \
            patch.object(chat_module, "should_search", lambda _msg: False), \
            patch.object(chat_module, "detect_weather_city", lambda _msg: None), \
            patch.object(chat_module, "get_relevant_memories", lambda *_a, **_k: []), \
            patch.object(chat_module, "extract_and_save_memory", lambda *_a, **_k: None), \
            patch.object(
                chat_module, "_extract_and_save_natural_memories",
                lambda *_a, **_k: None,
            ):
        resp = web_client.post(
            "/chat/stream",
            json={"message": "hi", "mode": "chat"},
            headers=_h(token),
        )
    # Verify the stream ended with error, not done
    assert resp.status_code == 200
    events = _decode_ndjson(resp.content)
    types = [e["type"] for e in events]
    # Should have error event, NOT done (because generation was cancelled)
    assert "error" in types, f"Expected 'error' in event types, got: {types}"
    assert "done" not in types, "Cancelled stream should not have 'done' event"
    # Critically: verify nothing was persisted
    with sqlite3.connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert count == 0, "Cancelled stream should not persist any messages"


def test_cancelled_chat_does_not_persist(db_path, web_client):
    _make_user(db_path, "alice")
    token = _login(web_client, "alice")

    def fake_chat(*args, **kwargs):
        from core.chat import RequestCancelled
        raise RequestCancelled()

    import web
    with patch.object(web, "chat", side_effect=fake_chat):
        resp = web_client.post(
            "/chat",
            json={"message": "hi", "mode": "chat"},
            headers=_h(token),
        )

    assert resp.status_code == 503
    with sqlite3.connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert count == 0, "Cancelled /chat request should not persist any messages"
