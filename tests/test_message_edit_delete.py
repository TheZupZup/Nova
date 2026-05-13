"""
HTTP + data-layer tests for the message edit / delete endpoints
(issue #94 — PUT/DELETE /messages/{id}).

The tests pin the user-visible contract:

  * a user can edit / delete their own chat messages;
  * ownership is enforced at every level — a cross-user request gets
    a 404, never a 200 + silent failure or a 403 that leaks existence;
  * editing rejects empty / oversized content with a sanitised error;
  * deleting a user message can optionally cascade to the immediately
    following assistant reply (and only when explicitly asked);
  * deleting a message does NOT remove memory entries — memory cleanup
    is a separate, explicit user action;
  * a conversation reload after edit/delete reflects the new state.
"""

from __future__ import annotations

import contextlib
import sqlite3
import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

for _mod in ("ddgs", "ollama", "sgmllib", "feedparser"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from core import memory as core_memory, users, feedback as core_feedback  # noqa: E402
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


def _seed_exchange(db_path, owner_id, *, user_text="hello", reply_text="hi"):
    """Create a conversation owned by `owner_id` and a user/assistant pair.

    Returns ``(conversation_id, user_message_id, assistant_message_id)``.
    """
    conv_id = core_memory.create_conversation("exchange", owner_id)
    user_id_msg = core_memory.save_message(conv_id, "user", user_text)
    asst_id_msg = core_memory.save_message(
        conv_id, "assistant", reply_text, "stub-model"
    )
    return conv_id, user_id_msg, asst_id_msg


# ── Data-layer ──────────────────────────────────────────────────────────────


class TestDataLayer:
    def test_get_owned_message_returns_dict_for_owner(self, db_path):
        a = _make_user(db_path, "alice")
        _conv_id, uid_msg, _aid_msg = _seed_exchange(db_path, a)
        row = core_memory.get_owned_message(uid_msg, a)
        assert row is not None
        assert row["role"] == "user"
        assert row["content"] == "hello"

    def test_get_owned_message_returns_none_for_other_user(self, db_path):
        a = _make_user(db_path, "alice")
        b = _make_user(db_path, "bob")
        _conv_id, uid_msg, _aid_msg = _seed_exchange(db_path, a)
        assert core_memory.get_owned_message(uid_msg, b) is None

    def test_get_owned_message_returns_none_for_missing_id(self, db_path):
        a = _make_user(db_path, "alice")
        assert core_memory.get_owned_message(99999, a) is None

    def test_update_message_content_persists(self, db_path):
        a = _make_user(db_path, "alice")
        _conv_id, uid_msg, _aid_msg = _seed_exchange(db_path, a)
        updated = core_memory.update_message_content(uid_msg, a, "edited!")
        assert updated is not None
        assert updated["content"] == "edited!"
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT content FROM messages WHERE id = ?", (uid_msg,)
            ).fetchone()
        assert row[0] == "edited!"

    def test_update_message_content_refuses_other_user(self, db_path):
        a = _make_user(db_path, "alice")
        b = _make_user(db_path, "bob")
        _conv_id, uid_msg, _aid_msg = _seed_exchange(db_path, a)
        assert core_memory.update_message_content(uid_msg, b, "hax") is None
        # Alice's content survives.
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT content FROM messages WHERE id = ?", (uid_msg,)
            ).fetchone()
        assert row[0] == "hello"

    def test_delete_message_removes_only_target_by_default(self, db_path):
        a = _make_user(db_path, "alice")
        _conv_id, uid_msg, aid_msg = _seed_exchange(db_path, a)
        result = core_memory.delete_message(uid_msg, a)
        assert result == {"deleted_id": uid_msg, "cascaded_assistant_id": None}
        with sqlite3.connect(db_path) as conn:
            ids = {
                r[0] for r in conn.execute(
                    "SELECT id FROM messages"
                ).fetchall()
            }
        # The assistant reply is left alone unless cascade is requested.
        assert ids == {aid_msg}

    def test_delete_message_cascades_following_assistant(self, db_path):
        a = _make_user(db_path, "alice")
        _conv_id, uid_msg, aid_msg = _seed_exchange(db_path, a)
        result = core_memory.delete_message(
            uid_msg, a, cascade_assistant=True
        )
        assert result["deleted_id"] == uid_msg
        assert result["cascaded_assistant_id"] == aid_msg
        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        assert count == 0

    def test_cascade_no_op_when_next_is_not_assistant(self, db_path):
        """User → user → assistant: deleting the first user message with
        cascade must not remove a non-assistant sibling."""
        a = _make_user(db_path, "alice")
        conv_id = core_memory.create_conversation("multi", a)
        m1 = core_memory.save_message(conv_id, "user", "one")
        m2 = core_memory.save_message(conv_id, "user", "two")
        m3 = core_memory.save_message(conv_id, "assistant", "reply")
        result = core_memory.delete_message(m1, a, cascade_assistant=True)
        assert result["cascaded_assistant_id"] is None
        with sqlite3.connect(db_path) as conn:
            ids = {
                r[0] for r in conn.execute(
                    "SELECT id FROM messages WHERE conversation_id = ?",
                    (conv_id,),
                ).fetchall()
            }
        # m1 is gone; m2 and m3 survive.
        assert ids == {m2, m3}

    def test_assistant_delete_never_cascades(self, db_path):
        """Cascade flag is ignored when the deleted row is itself an
        assistant reply — predictable v1 behaviour."""
        a = _make_user(db_path, "alice")
        _conv_id, uid_msg, aid_msg = _seed_exchange(db_path, a)
        result = core_memory.delete_message(
            aid_msg, a, cascade_assistant=True
        )
        assert result["deleted_id"] == aid_msg
        assert result["cascaded_assistant_id"] is None
        with sqlite3.connect(db_path) as conn:
            ids = {
                r[0] for r in conn.execute(
                    "SELECT id FROM messages"
                ).fetchall()
            }
        assert ids == {uid_msg}

    def test_delete_message_refuses_other_user(self, db_path):
        a = _make_user(db_path, "alice")
        b = _make_user(db_path, "bob")
        _conv_id, uid_msg, aid_msg = _seed_exchange(db_path, a)
        assert core_memory.delete_message(uid_msg, b) is None
        # Both rows survive.
        with sqlite3.connect(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        assert count == 2

    def test_delete_message_cleans_up_feedback(self, db_path):
        a = _make_user(db_path, "alice")
        _conv_id, _uid_msg, aid_msg = _seed_exchange(db_path, a)
        core_feedback.record_feedback(
            a, "positive", message_id=aid_msg, db_path=db_path,
        )
        # Sanity check: the feedback row exists.
        rows = core_feedback.list_feedback(a, db_path=db_path)
        assert len(rows) == 1

        core_memory.delete_message(aid_msg, a)

        # The orphan feedback row is gone, so the local table never
        # carries a dangling reference.
        rows = core_feedback.list_feedback(a, db_path=db_path)
        assert rows == []

    def test_delete_message_does_not_touch_memories(self, db_path):
        """Memory cleanup must remain a separate explicit action."""
        a = _make_user(db_path, "alice")
        _conv_id, uid_msg, _aid_msg = _seed_exchange(db_path, a)
        core_memory.save_memory("preferences", "alice prefers calm tone", a)
        assert len(core_memory.load_memories(a)) == 1

        core_memory.delete_message(uid_msg, a, cascade_assistant=True)

        # Memory survives unchanged.
        mems = core_memory.load_memories(a)
        assert len(mems) == 1
        assert mems[0]["content"] == "alice prefers calm tone"


# ── HTTP endpoints ──────────────────────────────────────────────────────────


class TestPutMessageEndpoint:
    def test_owner_can_edit_their_message(self, db_path, web_client):
        uid = _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        _conv_id, uid_msg, _aid_msg = _seed_exchange(db_path, uid)

        resp = web_client.put(
            f"/messages/{uid_msg}",
            json={"content": "corrected"},
            headers=_h(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["content"] == "corrected"

        # Conversation reload returns the new content.
        msgs = web_client.get(
            f"/conversations/{_conv_id}/messages", headers=_h(token)
        ).json()
        contents = [m["content"] for m in msgs]
        assert "corrected" in contents

    def test_cannot_edit_other_users_message_returns_404(
        self, db_path, web_client,
    ):
        a = _make_user(db_path, "alice")
        _make_user(db_path, "bob")
        b_token = _login(web_client, "bob")
        _conv_id, uid_msg, _aid_msg = _seed_exchange(db_path, a)

        resp = web_client.put(
            f"/messages/{uid_msg}",
            json={"content": "hax"},
            headers=_h(b_token),
        )
        assert resp.status_code == 404

        # Alice's content survives.
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT content FROM messages WHERE id = ?", (uid_msg,)
            ).fetchone()
        assert row[0] == "hello"

    def test_unknown_id_returns_404(self, db_path, web_client):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        resp = web_client.put(
            "/messages/99999",
            json={"content": "anything"},
            headers=_h(token),
        )
        assert resp.status_code == 404

    def test_empty_edit_is_rejected(self, db_path, web_client):
        uid = _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        _conv_id, uid_msg, _aid_msg = _seed_exchange(db_path, uid)
        for empty in ("", "   ", "\n\t  "):
            resp = web_client.put(
                f"/messages/{uid_msg}",
                json={"content": empty},
                headers=_h(token),
            )
            assert resp.status_code == 422, (empty, resp.text)

    def test_too_long_edit_is_rejected(self, db_path, web_client):
        uid = _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        _conv_id, uid_msg, _aid_msg = _seed_exchange(db_path, uid)
        too_long = "x" * (core_memory.MESSAGE_CONTENT_MAX_LEN + 1)
        resp = web_client.put(
            f"/messages/{uid_msg}",
            json={"content": too_long},
            headers=_h(token),
        )
        assert resp.status_code == 422
        # Original content survives.
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT content FROM messages WHERE id = ?", (uid_msg,)
            ).fetchone()
        assert row[0] == "hello"

    def test_missing_auth_rejected(self, db_path, web_client):
        uid = _make_user(db_path, "alice")
        _conv_id, uid_msg, _aid_msg = _seed_exchange(db_path, uid)
        resp = web_client.put(
            f"/messages/{uid_msg}", json={"content": "x"},
        )
        # FastAPI's HTTPBearer dependency returns 401 (some configs 403);
        # the contract is "no anonymous writes".
        assert resp.status_code in (401, 403)


class TestDeleteMessageEndpoint:
    def test_owner_can_delete_their_message(self, db_path, web_client):
        uid = _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        _conv_id, uid_msg, aid_msg = _seed_exchange(db_path, uid)

        resp = web_client.delete(
            f"/messages/{uid_msg}", headers=_h(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["deleted_id"] == uid_msg
        assert body["cascaded_assistant_id"] is None

        # Conversation reload no longer shows the deleted row.
        msgs = web_client.get(
            f"/conversations/{_conv_id}/messages", headers=_h(token)
        ).json()
        assert [m["id"] for m in msgs] == [aid_msg]

    def test_cascade_assistant_removes_following_reply(
        self, db_path, web_client,
    ):
        uid = _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        _conv_id, uid_msg, aid_msg = _seed_exchange(db_path, uid)

        resp = web_client.delete(
            f"/messages/{uid_msg}?cascade_assistant=true",
            headers=_h(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["deleted_id"] == uid_msg
        assert body["cascaded_assistant_id"] == aid_msg

        msgs = web_client.get(
            f"/conversations/{_conv_id}/messages", headers=_h(token)
        ).json()
        assert msgs == []

    def test_cannot_delete_other_users_message_returns_404(
        self, db_path, web_client,
    ):
        a = _make_user(db_path, "alice")
        _make_user(db_path, "bob")
        b_token = _login(web_client, "bob")
        _conv_id, uid_msg, aid_msg = _seed_exchange(db_path, a)

        resp = web_client.delete(
            f"/messages/{uid_msg}", headers=_h(b_token),
        )
        assert resp.status_code == 404

        # Both messages survive in Alice's conversation.
        with sqlite3.connect(db_path) as conn:
            ids = {
                r[0] for r in conn.execute(
                    "SELECT id FROM messages WHERE conversation_id = ?",
                    (_conv_id,),
                ).fetchall()
            }
        assert ids == {uid_msg, aid_msg}

    def test_unknown_id_returns_404(self, db_path, web_client):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        resp = web_client.delete("/messages/99999", headers=_h(token))
        assert resp.status_code == 404

    def test_delete_assistant_message_alone(self, db_path, web_client):
        """Deleting an assistant message removes only that row, even when
        the cascade flag is set (no chain reaction on assistant deletes)."""
        uid = _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        _conv_id, uid_msg, aid_msg = _seed_exchange(db_path, uid)

        resp = web_client.delete(
            f"/messages/{aid_msg}?cascade_assistant=true",
            headers=_h(token),
        )
        assert resp.status_code == 200
        msgs = web_client.get(
            f"/conversations/{_conv_id}/messages", headers=_h(token)
        ).json()
        assert [m["id"] for m in msgs] == [uid_msg]

    def test_delete_does_not_remove_memories(self, db_path, web_client):
        uid = _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        _conv_id, uid_msg, _aid_msg = _seed_exchange(db_path, uid)
        core_memory.save_memory("preferences", "alice likes brevity", uid)

        web_client.delete(
            f"/messages/{uid_msg}?cascade_assistant=true",
            headers=_h(token),
        )

        listed = web_client.get("/memories", headers=_h(token)).json()
        assert [m["content"] for m in listed] == ["alice likes brevity"]

    def test_delete_cleans_up_feedback(self, db_path, web_client):
        """Feedback rows for a deleted message must not linger in the
        local feedback table — the safety contract is "no orphan
        feedback"."""
        uid = _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        _conv_id, _uid_msg, aid_msg = _seed_exchange(db_path, uid)
        # Rate the assistant reply.
        web_client.post(
            "/feedback",
            json={"sentiment": "positive", "message_id": aid_msg},
            headers=_h(token),
        )
        assert len(web_client.get("/feedback", headers=_h(token)).json()) == 1

        # Delete that assistant reply.
        web_client.delete(f"/messages/{aid_msg}", headers=_h(token))

        # The feedback row is gone, not orphaned.
        assert web_client.get("/feedback", headers=_h(token)).json() == []

    def test_missing_auth_rejected(self, db_path, web_client):
        uid = _make_user(db_path, "alice")
        _conv_id, uid_msg, _aid_msg = _seed_exchange(db_path, uid)
        resp = web_client.delete(f"/messages/{uid_msg}")
        assert resp.status_code in (401, 403)


class TestConversationReloadAfterEditDelete:
    def test_reload_reflects_edited_content(self, db_path, web_client):
        uid = _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        _conv_id, uid_msg, _aid_msg = _seed_exchange(db_path, uid)
        web_client.put(
            f"/messages/{uid_msg}",
            json={"content": "rewritten"},
            headers=_h(token),
        )
        msgs = web_client.get(
            f"/conversations/{_conv_id}/messages", headers=_h(token),
        ).json()
        edited = next(m for m in msgs if m["id"] == uid_msg)
        assert edited["content"] == "rewritten"

    def test_reload_preserves_order_after_partial_delete(
        self, db_path, web_client,
    ):
        uid = _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        conv_id = core_memory.create_conversation("ordered", uid)
        m1 = core_memory.save_message(conv_id, "user", "one")
        _m2 = core_memory.save_message(conv_id, "assistant", "two")
        m3 = core_memory.save_message(conv_id, "user", "three")
        m4 = core_memory.save_message(conv_id, "assistant", "four")
        # Delete the second user message; its assistant reply stays.
        web_client.delete(f"/messages/{m3}", headers=_h(token))

        msgs = web_client.get(
            f"/conversations/{conv_id}/messages", headers=_h(token),
        ).json()
        assert [m["id"] for m in msgs] == [m1, _m2, m4]
