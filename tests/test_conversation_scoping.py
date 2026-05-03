"""
Tests for per-user conversation/message scoping (issue #105).

Covers the migration that adds a user_id column and backfills existing
single-user data, plus the data-layer and HTTP-endpoint behaviours that
prevent one user from reading or deleting another user's conversations.
"""

from __future__ import annotations

import contextlib
import sqlite3
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core import memory as core_memory, users
from memory import store as natural_store


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Initialise a fresh nova.db with all tables and the users migration run."""
    path = str(tmp_path / "nova.db")
    monkeypatch.setattr(core_memory, "DB_PATH", path)
    monkeypatch.setattr(natural_store, "DB_PATH", path)
    core_memory.initialize_db()
    return path


def _make_user(db_path, username, password="pw", role=users.ROLE_USER):
    with sqlite3.connect(db_path) as conn:
        return users.create_user(conn, username, password, role=role)


# ── Migration ───────────────────────────────────────────────────────────────

class TestMigration:
    def test_fresh_db_has_user_id_column_after_initialize(self, db_path):
        with sqlite3.connect(db_path) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(conversations)").fetchall()}
        assert "user_id" in cols

    def test_existing_conversations_are_backfilled_to_legacy_admin(self, tmp_path, monkeypatch):
        """A pre-multi-user DB with conversations is migrated to the default admin."""
        path = str(tmp_path / "nova.db")
        monkeypatch.setattr(core_memory, "DB_PATH", path)
        monkeypatch.setattr(natural_store, "DB_PATH", path)
        monkeypatch.setenv("NOVA_USERNAME", "legacyadmin")
        monkeypatch.setenv("NOVA_PASSWORD", "legacypw")

        # Simulate the legacy single-user DB shape: conversations and messages
        # without a user_id column.
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE conversations ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "title TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO conversations (title, created, updated) VALUES "
                "('legacy chat', '2024-01-01T00:00:00', '2024-01-01T00:00:00')"
            )

        core_memory.initialize_db()

        with sqlite3.connect(path) as conn:
            row = conn.execute(
                "SELECT user_id, title FROM conversations"
            ).fetchone()
            admin = conn.execute(
                "SELECT id FROM users WHERE username = 'legacyadmin'"
            ).fetchone()
        assert admin is not None
        assert row[0] == admin[0]
        assert row[1] == "legacy chat"

    def test_migration_is_idempotent(self, db_path):
        # Running initialize_db a second time must not raise or duplicate work.
        core_memory.initialize_db()
        core_memory.initialize_db()
        with sqlite3.connect(db_path) as conn:
            cols = [
                r[1] for r in conn.execute("PRAGMA table_info(conversations)").fetchall()
            ]
        assert cols.count("user_id") == 1

    def test_legacy_admin_still_sees_their_conversation(self, tmp_path, monkeypatch):
        """End-to-end: legacy single-user data is still visible to that user."""
        path = str(tmp_path / "nova.db")
        monkeypatch.setattr(core_memory, "DB_PATH", path)
        monkeypatch.setattr(natural_store, "DB_PATH", path)
        monkeypatch.setenv("NOVA_USERNAME", "nova")
        monkeypatch.setenv("NOVA_PASSWORD", "nova")

        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE conversations ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "title TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE messages ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "conversation_id INTEGER NOT NULL, role TEXT NOT NULL, "
                "content TEXT NOT NULL, model TEXT, created TEXT NOT NULL)"
            )
            cur = conn.execute(
                "INSERT INTO conversations (title, created, updated) "
                "VALUES ('old chat', '2024-01-01', '2024-01-01')"
            )
            conv_id = cur.lastrowid
            conn.execute(
                "INSERT INTO messages (conversation_id, role, content, created) "
                "VALUES (?, 'user', 'hello world', '2024-01-01')",
                (conv_id,),
            )

        core_memory.initialize_db()

        with sqlite3.connect(path) as conn:
            admin_id = conn.execute(
                "SELECT id FROM users WHERE username = 'nova'"
            ).fetchone()[0]

        listed = core_memory.load_conversations(admin_id)
        assert [c["title"] for c in listed] == ["old chat"]

        msgs = core_memory.load_conversation_messages(conv_id, admin_id)
        assert msgs is not None
        assert msgs[0]["content"] == "hello world"


# ── Data-layer ownership ────────────────────────────────────────────────────

class TestDataLayerScoping:
    def test_create_conversation_records_user_id(self, db_path):
        uid = _make_user(db_path, "alice")
        cid = core_memory.create_conversation("hi", uid)
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT user_id FROM conversations WHERE id = ?", (cid,)
            ).fetchone()
        assert row[0] == uid

    def test_load_conversations_filters_by_user(self, db_path):
        a = _make_user(db_path, "alice")
        b = _make_user(db_path, "bob")
        core_memory.create_conversation("alice's chat", a)
        core_memory.create_conversation("bob's chat", b)

        a_list = core_memory.load_conversations(a)
        b_list = core_memory.load_conversations(b)

        assert [c["title"] for c in a_list] == ["alice's chat"]
        assert [c["title"] for c in b_list] == ["bob's chat"]

    def test_load_conversation_messages_returns_none_for_other_user(self, db_path):
        a = _make_user(db_path, "alice")
        b = _make_user(db_path, "bob")
        cid = core_memory.create_conversation("alice's chat", a)
        core_memory.save_message(cid, "user", "secret")

        # Bob requesting Alice's conversation gets None — not an empty list,
        # so the web layer can translate the distinction into a 404.
        assert core_memory.load_conversation_messages(cid, b) is None
        # Alice gets the message back.
        msgs = core_memory.load_conversation_messages(cid, a)
        assert msgs is not None
        assert msgs[0]["content"] == "secret"

    def test_load_conversation_messages_returns_none_for_missing_id(self, db_path):
        a = _make_user(db_path, "alice")
        assert core_memory.load_conversation_messages(99999, a) is None

    def test_delete_conversation_refuses_other_user(self, db_path):
        a = _make_user(db_path, "alice")
        b = _make_user(db_path, "bob")
        cid = core_memory.create_conversation("alice's chat", a)
        core_memory.save_message(cid, "user", "secret")

        assert core_memory.delete_conversation(cid, b) is False
        # Conversation and message survive Bob's failed delete.
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM conversations WHERE id = ?", (cid,)
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (cid,)
            ).fetchone()[0] == 1

    def test_delete_conversation_succeeds_for_owner(self, db_path):
        a = _make_user(db_path, "alice")
        cid = core_memory.create_conversation("alice's chat", a)
        core_memory.save_message(cid, "user", "bye")

        assert core_memory.delete_conversation(cid, a) is True
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM conversations WHERE id = ?", (cid,)
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (cid,)
            ).fetchone()[0] == 0

    def test_conversation_belongs_to(self, db_path):
        a = _make_user(db_path, "alice")
        b = _make_user(db_path, "bob")
        cid = core_memory.create_conversation("alice's chat", a)
        assert core_memory.conversation_belongs_to(cid, a) is True
        assert core_memory.conversation_belongs_to(cid, b) is False
        assert core_memory.conversation_belongs_to(999999, a) is False


# ── HTTP endpoint scoping ───────────────────────────────────────────────────

@pytest.fixture
def web_client(db_path, monkeypatch):
    """A TestClient backed by the temp DB, with background jobs suppressed."""
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


class TestConversationEndpoints:
    def test_list_only_returns_current_users_conversations(self, db_path, web_client):
        _make_user(db_path, "alice")
        _make_user(db_path, "bob")
        a_token = _login(web_client, "alice")
        b_token = _login(web_client, "bob")

        web_client.post("/conversations", json={"title": "alice-1"}, headers=_h(a_token))
        web_client.post("/conversations", json={"title": "alice-2"}, headers=_h(a_token))
        web_client.post("/conversations", json={"title": "bob-1"}, headers=_h(b_token))

        a_list = web_client.get("/conversations", headers=_h(a_token)).json()
        b_list = web_client.get("/conversations", headers=_h(b_token)).json()
        assert sorted(c["title"] for c in a_list) == ["alice-1", "alice-2"]
        assert sorted(c["title"] for c in b_list) == ["bob-1"]

    def test_post_creates_conversation_owned_by_current_user(self, db_path, web_client):
        uid = _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        resp = web_client.post(
            "/conversations", json={"title": "mine"}, headers=_h(token)
        )
        cid = resp.json()["id"]
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT user_id FROM conversations WHERE id = ?", (cid,)
            ).fetchone()
        assert row[0] == uid

    def test_user_a_cannot_read_user_b_messages_returns_404(self, db_path, web_client):
        _make_user(db_path, "alice")
        _make_user(db_path, "bob")
        a_token = _login(web_client, "alice")
        b_token = _login(web_client, "bob")

        cid = web_client.post(
            "/conversations", json={"title": "alice-private"}, headers=_h(a_token)
        ).json()["id"]

        # Cross-user read returns 404 (not 403) so existence isn't leaked.
        resp = web_client.get(f"/conversations/{cid}/messages", headers=_h(b_token))
        assert resp.status_code == 404

        # Owner can still read.
        resp = web_client.get(f"/conversations/{cid}/messages", headers=_h(a_token))
        assert resp.status_code == 200

    def test_user_a_cannot_delete_user_b_conversation_returns_404(self, db_path, web_client):
        _make_user(db_path, "alice")
        _make_user(db_path, "bob")
        a_token = _login(web_client, "alice")
        b_token = _login(web_client, "bob")

        cid = web_client.post(
            "/conversations", json={"title": "alice-private"}, headers=_h(a_token)
        ).json()["id"]

        resp = web_client.delete(f"/conversations/{cid}", headers=_h(b_token))
        assert resp.status_code == 404

        # Conversation still belongs to Alice.
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM conversations WHERE id = ?", (cid,)
            ).fetchone()[0] == 1

    def test_unknown_conversation_id_returns_404(self, db_path, web_client):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")

        assert web_client.get(
            "/conversations/99999/messages", headers=_h(token)
        ).status_code == 404
        assert web_client.delete(
            "/conversations/99999", headers=_h(token)
        ).status_code == 404


class TestChatEndpoint:
    def test_chat_creates_conversation_owned_by_current_user(self, db_path, web_client):
        uid = _make_user(db_path, "alice")
        token = _login(web_client, "alice")

        with patch("web.chat", return_value=("hi back", "stub-model")):
            resp = web_client.post(
                "/chat",
                json={"message": "hello", "mode": "chat"},
                headers=_h(token),
            )
        assert resp.status_code == 200
        cid = resp.json()["conversation_id"]
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT user_id FROM conversations WHERE id = ?", (cid,)
            ).fetchone()
        assert row[0] == uid

    def test_chat_refuses_to_post_into_other_users_conversation(self, db_path, web_client):
        _make_user(db_path, "alice")
        _make_user(db_path, "bob")
        a_token = _login(web_client, "alice")
        b_token = _login(web_client, "bob")

        cid = web_client.post(
            "/conversations", json={"title": "alice-thread"}, headers=_h(a_token)
        ).json()["id"]

        with patch("web.chat", return_value=("nope", "stub-model")):
            resp = web_client.post(
                "/chat",
                json={"message": "hi", "conversation_id": cid, "mode": "chat"},
                headers=_h(b_token),
            )
        assert resp.status_code == 404
        # No message was written under Alice's conversation.
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (cid,)
            ).fetchone()[0] == 0

    def test_chat_continues_owned_conversation(self, db_path, web_client):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")

        with patch("web.chat", return_value=("first reply", "stub-model")):
            first = web_client.post(
                "/chat", json={"message": "hello", "mode": "chat"}, headers=_h(token)
            )
        cid = first.json()["conversation_id"]

        with patch("web.chat", return_value=("second reply", "stub-model")):
            second = web_client.post(
                "/chat",
                json={"message": "follow-up", "conversation_id": cid, "mode": "chat"},
                headers=_h(token),
            )
        assert second.status_code == 200
        assert second.json()["conversation_id"] == cid

        with sqlite3.connect(db_path) as conn:
            n_msgs = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (cid,)
            ).fetchone()[0]
        # Two user messages + two assistant replies.
        assert n_msgs == 4


class TestSingleUserBehaviorPreserved:
    def test_default_admin_sees_migrated_history(self, tmp_path, monkeypatch):
        """Single-user upgrade path: legacy chats remain accessible to nova/nova."""
        path = str(tmp_path / "nova.db")
        monkeypatch.setattr(core_memory, "DB_PATH", path)
        monkeypatch.setattr(natural_store, "DB_PATH", path)
        monkeypatch.setenv("NOVA_USERNAME", "nova")
        monkeypatch.setenv("NOVA_PASSWORD", "nova")

        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE conversations ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "title TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO conversations (title, created, updated) "
                "VALUES ('legacy', '2024-01-01', '2024-01-01')"
            )

        core_memory.initialize_db()

        from core.rate_limiter import _login_limiter
        _login_limiter._store.clear()

        import web
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("web.initialize_db"))
            stack.enter_context(patch("web.learn_from_feeds"))
            stack.enter_context(patch("web.scheduler", MagicMock()))
            with TestClient(web.app, raise_server_exceptions=True) as client:
                token = _login(client, "nova", "nova")
                resp = client.get("/conversations", headers=_h(token))
        assert resp.status_code == 200
        titles = [c["title"] for c in resp.json()]
        assert "legacy" in titles
