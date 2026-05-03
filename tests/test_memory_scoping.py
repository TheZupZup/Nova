"""
Tests for per-user memory scoping (issue #106).

Covers the migration that adds user_id to `memories` and `natural_memories`
plus the data-layer and HTTP-endpoint behaviours that prevent one user
from reading, retrieving, or deleting another user's memories.
"""

from __future__ import annotations

import contextlib
import sqlite3
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core import memory as core_memory, users
from core.memory_command import handle_manual_memory_command
from memory import store as natural_store
from memory.retriever import get_relevant_memories
from memory.schema import Memory


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


def _mem(**kwargs) -> Memory:
    defaults = dict(kind="general", topic="test", content="test content", confidence=0.9)
    defaults.update(kwargs)
    return Memory(**defaults)


# ── Migration ───────────────────────────────────────────────────────────────

class TestMigration:
    def test_fresh_db_has_user_id_column_on_memories(self, db_path):
        with sqlite3.connect(db_path) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
        assert "user_id" in cols

    def test_fresh_db_has_user_id_column_on_natural_memories(self, db_path):
        with sqlite3.connect(db_path) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(natural_memories)").fetchall()}
        assert "user_id" in cols

    def test_existing_memories_are_backfilled_to_legacy_admin(self, tmp_path, monkeypatch):
        """A pre-multi-user DB with manual memories is migrated to the default admin."""
        path = str(tmp_path / "nova.db")
        monkeypatch.setattr(core_memory, "DB_PATH", path)
        monkeypatch.setattr(natural_store, "DB_PATH", path)
        monkeypatch.setenv("NOVA_USERNAME", "legacyadmin")
        monkeypatch.setenv("NOVA_PASSWORD", "legacypw")

        # Simulate the legacy single-user DB shape: memories table without user_id.
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE memories ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "category TEXT NOT NULL, content TEXT NOT NULL, created TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO memories (category, content, created) "
                "VALUES ('manual', 'legacy memory', '2024-01-01T00:00:00')"
            )

        core_memory.initialize_db()

        with sqlite3.connect(path) as conn:
            row = conn.execute(
                "SELECT user_id, content FROM memories"
            ).fetchone()
            admin = conn.execute(
                "SELECT id FROM users WHERE username = 'legacyadmin'"
            ).fetchone()
        assert admin is not None
        assert row[0] == admin[0]
        assert row[1] == "legacy memory"

    def test_existing_natural_memories_are_backfilled_to_legacy_admin(self, tmp_path, monkeypatch):
        path = str(tmp_path / "nova.db")
        monkeypatch.setattr(core_memory, "DB_PATH", path)
        monkeypatch.setattr(natural_store, "DB_PATH", path)
        monkeypatch.setenv("NOVA_USERNAME", "legacyadmin")
        monkeypatch.setenv("NOVA_PASSWORD", "legacypw")

        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE natural_memories ("
                "id TEXT PRIMARY KEY, kind TEXT NOT NULL, topic TEXT NOT NULL, "
                "content TEXT NOT NULL, confidence REAL NOT NULL, source TEXT NOT NULL, "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                "last_seen_at TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO natural_memories "
                "(id, kind, topic, content, confidence, source, "
                " created_at, updated_at, last_seen_at) "
                "VALUES ('legacy-id', 'preference', 'editor', "
                "        'User prefers neovim.', 0.9, 'extractor', "
                "        '2024-01-01', '2024-01-01', '2024-01-01')"
            )

        core_memory.initialize_db()

        with sqlite3.connect(path) as conn:
            row = conn.execute(
                "SELECT user_id, content FROM natural_memories WHERE id = 'legacy-id'"
            ).fetchone()
            admin = conn.execute(
                "SELECT id FROM users WHERE username = 'legacyadmin'"
            ).fetchone()
        assert admin is not None
        assert row[0] == admin[0]

    def test_migration_is_idempotent(self, db_path):
        # Running initialize_db a second time must not raise or duplicate work.
        core_memory.initialize_db()
        core_memory.initialize_db()
        with sqlite3.connect(db_path) as conn:
            cols = [
                r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()
            ]
            nat_cols = [
                r[1] for r in conn.execute("PRAGMA table_info(natural_memories)").fetchall()
            ]
        assert cols.count("user_id") == 1
        assert nat_cols.count("user_id") == 1

    def test_legacy_admin_still_sees_their_memories(self, tmp_path, monkeypatch):
        """End-to-end: legacy single-user manual memories remain visible to the admin."""
        path = str(tmp_path / "nova.db")
        monkeypatch.setattr(core_memory, "DB_PATH", path)
        monkeypatch.setattr(natural_store, "DB_PATH", path)
        monkeypatch.setenv("NOVA_USERNAME", "nova")
        monkeypatch.setenv("NOVA_PASSWORD", "nova")

        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE memories ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "category TEXT NOT NULL, content TEXT NOT NULL, created TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO memories (category, content, created) "
                "VALUES ('manual', 'old fact', '2024-01-01')"
            )

        core_memory.initialize_db()

        with sqlite3.connect(path) as conn:
            admin_id = conn.execute(
                "SELECT id FROM users WHERE username = 'nova'"
            ).fetchone()[0]

        loaded = core_memory.load_memories(admin_id)
        assert any(m["content"] == "old fact" for m in loaded)


# ── Manual memory data-layer scoping ────────────────────────────────────────

class TestManualMemoryScoping:
    def test_save_memory_records_user_id(self, db_path):
        a = _make_user(db_path, "alice")
        core_memory.save_memory("manual", "alice's secret", a)
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT user_id, content FROM memories WHERE content = 'alice''s secret'"
            ).fetchone()
        assert row[0] == a

    def test_load_memories_filters_by_user(self, db_path):
        a = _make_user(db_path, "alice")
        b = _make_user(db_path, "bob")
        core_memory.save_memory("manual", "alice fact", a)
        core_memory.save_memory("manual", "bob fact", b)

        a_mems = core_memory.load_memories(a)
        b_mems = core_memory.load_memories(b)
        assert [m["content"] for m in a_mems] == ["alice fact"]
        assert [m["content"] for m in b_mems] == ["bob fact"]

    def test_list_memories_filters_by_user(self, db_path):
        a = _make_user(db_path, "alice")
        b = _make_user(db_path, "bob")
        core_memory.save_memory("manual", "alice fact", a)
        core_memory.save_memory("manual", "bob fact", b)

        a_mems = core_memory.list_memories(a)
        b_mems = core_memory.list_memories(b)
        assert [m["content"] for m in a_mems] == ["alice fact"]
        assert [m["content"] for m in b_mems] == ["bob fact"]

    def test_delete_memory_refuses_other_user(self, db_path):
        a = _make_user(db_path, "alice")
        b = _make_user(db_path, "bob")
        core_memory.save_memory("manual", "alice secret", a)
        with sqlite3.connect(db_path) as conn:
            mid = conn.execute(
                "SELECT id FROM memories WHERE content = 'alice secret'"
            ).fetchone()[0]

        # Bob trying to delete Alice's memory must be denied.
        assert core_memory.delete_memory(mid, b) is False
        # Alice's memory still exists.
        assert any(m["content"] == "alice secret" for m in core_memory.load_memories(a))

        # Alice can delete her own memory.
        assert core_memory.delete_memory(mid, a) is True
        assert core_memory.load_memories(a) == []

    def test_update_memory_refuses_other_user(self, db_path):
        a = _make_user(db_path, "alice")
        b = _make_user(db_path, "bob")
        core_memory.save_memory("manual", "alice fact", a)
        with sqlite3.connect(db_path) as conn:
            mid = conn.execute(
                "SELECT id FROM memories WHERE content = 'alice fact'"
            ).fetchone()[0]

        assert core_memory.update_memory(mid, "manual", "hijacked", b) is False
        assert any(m["content"] == "alice fact" for m in core_memory.load_memories(a))

        assert core_memory.update_memory(mid, "manual", "renamed", a) is True
        assert any(m["content"] == "renamed" for m in core_memory.load_memories(a))

    def test_manual_memory_command_writes_for_current_user_only(self, db_path):
        a = _make_user(db_path, "alice")
        b = _make_user(db_path, "bob")
        handle_manual_memory_command("Retiens ça: alice prefers tea", a)
        handle_manual_memory_command("Retiens ça: bob prefers coffee", b)

        a_mems = [m["content"] for m in core_memory.load_memories(a)]
        b_mems = [m["content"] for m in core_memory.load_memories(b)]
        assert a_mems == ["alice prefers tea"]
        assert b_mems == ["bob prefers coffee"]

    def test_parse_and_save_attributes_to_user(self, db_path):
        a = _make_user(db_path, "alice")
        b = _make_user(db_path, "bob")
        core_memory.parse_and_save("SAVE:knowledge:alice fact", a)
        core_memory.parse_and_save("SAVE:knowledge:bob fact", b)

        a_mems = [m["content"] for m in core_memory.load_memories(a)]
        b_mems = [m["content"] for m in core_memory.load_memories(b)]
        assert a_mems == ["alice fact"]
        assert b_mems == ["bob fact"]

    def test_cleanup_old_knowledge_only_affects_target_user(self, db_path):
        a = _make_user(db_path, "alice")
        b = _make_user(db_path, "bob")
        # Alice has 3 knowledge rows, bob has 2.
        for i in range(3):
            core_memory.save_memory("knowledge", f"alice fact {i}", a)
        for i in range(2):
            core_memory.save_memory("knowledge", f"bob fact {i}", b)

        # Trim Alice down to 1 knowledge row; Bob's must be untouched.
        core_memory.cleanup_old_knowledge(a, max_count=1)

        a_mems = [m["content"] for m in core_memory.load_memories(a)]
        b_mems = [m["content"] for m in core_memory.load_memories(b)]
        assert len(a_mems) == 1
        assert len(b_mems) == 2


# ── Natural memory data-layer scoping ───────────────────────────────────────

class TestNaturalMemoryScoping:
    @pytest.fixture(autouse=True)
    def _no_ollama(self, monkeypatch):
        monkeypatch.setattr("memory.store.generate_embedding", lambda _: None)
        monkeypatch.setattr("memory.retriever.generate_embedding", lambda _: None)

    def test_list_returns_only_owned_memories(self, db_path):
        a = _make_user(db_path, "alice")
        b = _make_user(db_path, "bob")
        natural_store.save_memory(
            _mem(kind="preference", topic="editor", content="User A prefers vim."),
            a,
        )
        natural_store.save_memory(
            _mem(kind="preference", topic="editor", content="User B prefers emacs."),
            b,
        )

        a_mems = natural_store.list_memories(a)
        b_mems = natural_store.list_memories(b)
        assert [m.content for m in a_mems] == ["User A prefers vim."]
        assert [m.content for m in b_mems] == ["User B prefers emacs."]

    def test_search_returns_only_owned_memories(self, db_path):
        a = _make_user(db_path, "alice")
        b = _make_user(db_path, "bob")
        natural_store.save_memory(
            _mem(kind="preference", topic="editor", content="User A prefers vim editor."),
            a,
        )
        natural_store.save_memory(
            _mem(kind="preference", topic="editor", content="User B prefers vim editor."),
            b,
        )

        a_mems = natural_store.search_memories("vim editor", a)
        b_mems = natural_store.search_memories("vim editor", b)
        assert len(a_mems) == 1
        assert "User A" in a_mems[0].content
        assert len(b_mems) == 1
        assert "User B" in b_mems[0].content

    def test_get_relevant_memories_isolated_per_user(self, db_path):
        a = _make_user(db_path, "alice")
        b = _make_user(db_path, "bob")
        natural_store.save_memory(
            _mem(kind="preference", topic="distro", content="Alice prefers Fedora KDE."),
            a,
        )
        natural_store.save_memory(
            _mem(kind="preference", topic="distro", content="Bob prefers Ubuntu GNOME."),
            b,
        )

        a_results = get_relevant_memories("which linux distro", a)
        b_results = get_relevant_memories("which linux distro", b)
        a_text = " ".join(m.content for m in a_results)
        b_text = " ".join(m.content for m in b_results)
        assert "Alice" in a_text
        assert "Bob" not in a_text
        assert "Bob" in b_text
        assert "Alice" not in b_text

    def test_delete_memory_refuses_other_user(self, db_path):
        a = _make_user(db_path, "alice")
        b = _make_user(db_path, "bob")
        m = _mem(kind="preference", topic="editor", content="User A prefers vim.")
        natural_store.save_memory(m, a)

        # Bob tries to delete Alice's memory — silently no-op.
        natural_store.delete_memory(m.id, b)
        assert any(x.id == m.id for x in natural_store.list_memories(a))

        natural_store.delete_memory(m.id, a)
        assert natural_store.list_memories(a) == []

    def test_delete_memories_matching_only_deletes_owned(self, db_path):
        a = _make_user(db_path, "alice")
        b = _make_user(db_path, "bob")
        natural_store.save_memory(
            _mem(kind="preference", topic="distro", content="Alice prefers Fedora."),
            a,
        )
        natural_store.save_memory(
            _mem(kind="preference", topic="distro", content="Bob prefers Fedora."),
            b,
        )

        count = natural_store.delete_memories_matching("fedora", a)
        assert count == 1
        # Alice's row gone, Bob's row preserved.
        assert natural_store.list_memories(a) == []
        assert len(natural_store.list_memories(b)) == 1

    def test_dedup_is_per_user(self, db_path):
        """Two users with identical content+topic are NOT deduped against each other."""
        a = _make_user(db_path, "alice")
        b = _make_user(db_path, "bob")

        m_a = _mem(kind="preference", topic="editor", content="User prefers neovim.")
        m_a.embedding = [1.0, 0.0]
        natural_store.save_memory(m_a, a)

        m_b = _mem(kind="preference", topic="editor", content="User prefers neovim.")
        m_b.embedding = [1.0, 0.0]
        natural_store.save_memory(m_b, b)

        assert len(natural_store.list_memories(a)) == 1
        assert len(natural_store.list_memories(b)) == 1


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


class TestMemoryEndpoints:
    def test_list_memories_returns_only_current_users(self, db_path, web_client):
        _make_user(db_path, "alice")
        _make_user(db_path, "bob")
        a_token = _login(web_client, "alice")
        b_token = _login(web_client, "bob")

        web_client.post("/memories", json={"category": "manual", "content": "alice fact"}, headers=_h(a_token))
        web_client.post("/memories", json={"category": "manual", "content": "bob fact"}, headers=_h(b_token))

        a_list = web_client.get("/memories", headers=_h(a_token)).json()
        b_list = web_client.get("/memories", headers=_h(b_token)).json()
        assert sorted(m["content"] for m in a_list) == ["alice fact"]
        assert sorted(m["content"] for m in b_list) == ["bob fact"]

    def test_post_memory_attributes_to_current_user(self, db_path, web_client):
        uid = _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        web_client.post(
            "/memories",
            json={"category": "manual", "content": "mine"},
            headers=_h(token),
        )
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT user_id FROM memories WHERE content = 'mine'"
            ).fetchone()
        assert row[0] == uid

    def test_user_a_cannot_delete_user_b_memory_returns_404(self, db_path, web_client):
        _make_user(db_path, "alice")
        _make_user(db_path, "bob")
        a_token = _login(web_client, "alice")
        b_token = _login(web_client, "bob")

        web_client.post(
            "/memories", json={"category": "manual", "content": "alice's fact"}, headers=_h(a_token)
        )
        with sqlite3.connect(db_path) as conn:
            mid = conn.execute(
                "SELECT id FROM memories WHERE content = 'alice''s fact'"
            ).fetchone()[0]

        resp = web_client.delete(f"/memories/{mid}", headers=_h(b_token))
        assert resp.status_code == 404

        # Memory still exists for Alice.
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM memories WHERE id = ?", (mid,)
            ).fetchone()[0] == 1

    def test_user_a_cannot_update_user_b_memory_returns_404(self, db_path, web_client):
        _make_user(db_path, "alice")
        _make_user(db_path, "bob")
        a_token = _login(web_client, "alice")
        b_token = _login(web_client, "bob")

        web_client.post(
            "/memories", json={"category": "manual", "content": "alice fact"}, headers=_h(a_token)
        )
        with sqlite3.connect(db_path) as conn:
            mid = conn.execute(
                "SELECT id FROM memories WHERE content = 'alice fact'"
            ).fetchone()[0]

        resp = web_client.put(
            f"/memories/{mid}",
            json={"category": "manual", "content": "hijacked"},
            headers=_h(b_token),
        )
        assert resp.status_code == 404

    def test_unknown_memory_id_returns_404(self, db_path, web_client):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        assert web_client.delete(
            "/memories/99999", headers=_h(token)
        ).status_code == 404
        assert web_client.put(
            "/memories/99999",
            json={"category": "manual", "content": "anything"},
            headers=_h(token),
        ).status_code == 404


class TestChatMemoryCommands:
    def test_show_my_memories_lists_only_current_user(self, db_path, web_client, monkeypatch):
        a = _make_user(db_path, "alice")
        b = _make_user(db_path, "bob")
        a_token = _login(web_client, "alice")
        b_token = _login(web_client, "bob")

        # Stub embeddings so dedup doesn't get tangled.
        monkeypatch.setattr("memory.store.generate_embedding", lambda _: None)

        natural_store.save_memory(
            _mem(kind="preference", topic="editor", content="Alice prefers vim."),
            a,
        )
        natural_store.save_memory(
            _mem(kind="preference", topic="editor", content="Bob prefers emacs."),
            b,
        )

        a_resp = web_client.post(
            "/chat",
            json={"message": "show my memories", "mode": "chat"},
            headers=_h(a_token),
        )
        b_resp = web_client.post(
            "/chat",
            json={"message": "show my memories", "mode": "chat"},
            headers=_h(b_token),
        )
        assert "Alice prefers vim." in a_resp.json()["response"]
        assert "Bob prefers emacs." not in a_resp.json()["response"]
        assert "Bob prefers emacs." in b_resp.json()["response"]
        assert "Alice prefers vim." not in b_resp.json()["response"]

    def test_forget_command_only_deletes_current_users_memories(self, db_path, web_client, monkeypatch):
        a = _make_user(db_path, "alice")
        b = _make_user(db_path, "bob")
        a_token = _login(web_client, "alice")

        monkeypatch.setattr("memory.store.generate_embedding", lambda _: None)

        natural_store.save_memory(
            _mem(kind="preference", topic="distro", content="Alice prefers Fedora."),
            a,
        )
        natural_store.save_memory(
            _mem(kind="preference", topic="distro", content="Bob prefers Fedora."),
            b,
        )

        resp = web_client.post(
            "/chat",
            json={"message": "forget that fedora", "mode": "chat"},
            headers=_h(a_token),
        )
        assert resp.status_code == 200
        # Alice's memory is gone, Bob's remains.
        assert natural_store.list_memories(a) == []
        assert any("Bob" in m.content for m in natural_store.list_memories(b))

    def test_manual_memory_command_through_chat_attributes_to_current_user(
        self, db_path, web_client
    ):
        a = _make_user(db_path, "alice")
        token = _login(web_client, "alice")

        web_client.post(
            "/chat",
            json={"message": "Retiens ça: alice prefers tea", "mode": "chat"},
            headers=_h(token),
        )
        mems = core_memory.load_memories(a)
        assert any(m["content"] == "alice prefers tea" for m in mems)

    def test_chat_memory_injection_uses_current_users_memories(
        self, db_path, web_client, monkeypatch
    ):
        """Verify the chat handler hands the current user's memories to the model."""
        a = _make_user(db_path, "alice")
        b = _make_user(db_path, "bob")
        a_token = _login(web_client, "alice")

        core_memory.save_memory("manual", "alice fact", a)
        core_memory.save_memory("manual", "bob fact", b)

        captured = {}

        def fake_chat(history, user_input, memories, user_id, **kwargs):
            captured["memories"] = memories
            captured["user_id"] = user_id
            return ("ok", "stub-model")

        monkeypatch.setattr("web.chat", fake_chat)

        web_client.post(
            "/chat",
            json={"message": "hi", "mode": "chat"},
            headers=_h(a_token),
        )
        assert captured["user_id"] == a
        contents = [m["content"] for m in captured["memories"]]
        assert "alice fact" in contents
        assert "bob fact" not in contents


class TestSingleUserBehaviorPreserved:
    def test_default_admin_sees_migrated_memories(self, tmp_path, monkeypatch):
        """Single-user upgrade path: legacy memories remain visible to nova/nova."""
        path = str(tmp_path / "nova.db")
        monkeypatch.setattr(core_memory, "DB_PATH", path)
        monkeypatch.setattr(natural_store, "DB_PATH", path)
        monkeypatch.setenv("NOVA_USERNAME", "nova")
        monkeypatch.setenv("NOVA_PASSWORD", "nova")

        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE memories ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "category TEXT NOT NULL, content TEXT NOT NULL, created TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO memories (category, content, created) "
                "VALUES ('manual', 'I prefer tea', '2024-01-01')"
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
                resp = client.get("/memories", headers=_h(token))
        assert resp.status_code == 200
        contents = [m["content"] for m in resp.json()]
        assert "I prefer tea" in contents
