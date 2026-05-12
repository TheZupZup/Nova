"""
Tests for the local feedback layer (thumbs up / thumbs down storage and
the derived preference block injected into the chat system prompt).

These tests pin the behaviours the Nova Safety and Trust Contract leans
on:

  * feedback is local, per-user, and inspectable;
  * feedback never bypasses the safety / identity contract — the
    preference block is appended *below* the identity contract and the
    personalization block, never *above* them;
  * obvious secret-shaped strings are refused at write time so the
    SQLite file never accumulates tokens the user pasted in by
    accident;
  * empty feedback contributes no system-prompt tokens (defaults stay
    cheap).
"""

from __future__ import annotations

import contextlib
import sqlite3
import sys
from unittest.mock import MagicMock, patch

import pytest

# Heavy / optional deps the imported modules pull in.
for _mod in ("ddgs", "ollama", "sgmllib", "feedparser"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from core import chat as chat_module  # noqa: E402
from core import memory as core_memory, users  # noqa: E402
from core.chat import build_messages, chat  # noqa: E402
from core.feedback import (  # noqa: E402
    REASON_MAX_LEN,
    SENTIMENT_NEGATIVE,
    SENTIMENT_POSITIVE,
    build_feedback_preferences_block,
    delete_feedback,
    list_feedback,
    record_feedback,
    sanitise_reason,
)
from core.identity import IDENTITY_CONTRACT  # noqa: E402
from memory import store as natural_store  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "nova.db")
    monkeypatch.setattr(core_memory, "DB_PATH", path)
    monkeypatch.setattr(natural_store, "DB_PATH", path)
    core_memory.initialize_db()
    return path


@pytest.fixture
def make_user(db_path):
    def _factory(username, password="pw", role=users.ROLE_USER):
        with sqlite3.connect(db_path) as conn:
            return users.create_user(conn, username, password, role=role)
    return _factory


# ── Reason sanitisation ─────────────────────────────────────────────────────

class TestSanitiseReason:
    def test_none_returns_empty(self):
        assert sanitise_reason(None) == ""

    def test_blank_returns_empty(self):
        assert sanitise_reason("   \t  ") == ""

    def test_trims_whitespace(self):
        assert sanitise_reason("  too generic  ") == "too generic"

    def test_strips_control_characters(self):
        raw = "be more\x00specific\x01"
        assert sanitise_reason(raw) == "be morespecific"

    def test_caps_to_reason_max_len(self):
        # Use non-hex characters so the long string does not accidentally
        # trip the API-key-shaped pattern in the sanitiser.
        long_text = "z" * (REASON_MAX_LEN + 50)
        cleaned = sanitise_reason(long_text)
        assert len(cleaned) <= REASON_MAX_LEN

    def test_rejects_obvious_api_key(self):
        with pytest.raises(ValueError):
            sanitise_reason("here is my key: " + "a" * 40)

    def test_rejects_github_pat(self):
        with pytest.raises(ValueError):
            sanitise_reason("token = ghp_" + "x" * 30)

    def test_rejects_password_assignment(self):
        with pytest.raises(ValueError):
            sanitise_reason("password = hunter2hunter2")

    def test_rejects_jwt_like_triplet(self):
        with pytest.raises(ValueError):
            sanitise_reason("eyJabc.eyJdef.signaturepart-stuff")

    def test_normal_feedback_passes(self):
        assert sanitise_reason(
            "Stop giving generic advice, focus on this project."
        ) == "Stop giving generic advice, focus on this project."


# ── Persistence ─────────────────────────────────────────────────────────────

class TestRecordAndList:
    def test_record_positive_then_list(self, db_path, make_user):
        alice = make_user("alice")
        fid = record_feedback(alice, SENTIMENT_POSITIVE)
        assert fid > 0
        rows = list_feedback(alice)
        assert len(rows) == 1
        assert rows[0]["sentiment"] == SENTIMENT_POSITIVE
        assert rows[0]["reason"] == ""
        assert rows[0]["source"] == "feedback"
        assert rows[0]["created_at"]

    def test_record_negative_with_reason(self, db_path, make_user):
        alice = make_user("alice")
        record_feedback(
            alice, SENTIMENT_NEGATIVE,
            reason="Too generic, want project-specific guidance.",
        )
        rows = list_feedback(alice)
        assert rows[0]["sentiment"] == SENTIMENT_NEGATIVE
        assert "project-specific" in rows[0]["reason"]

    def test_invalid_sentiment_raises(self, db_path, make_user):
        alice = make_user("alice")
        with pytest.raises(ValueError):
            record_feedback(alice, "meh")

    def test_secret_reason_is_refused(self, db_path, make_user):
        alice = make_user("alice")
        with pytest.raises(ValueError):
            record_feedback(
                alice, SENTIMENT_NEGATIVE,
                reason="api_key=" + "ABCD1234" * 6,
            )
        # Nothing landed in the table.
        assert list_feedback(alice) == []

    def test_rerating_same_message_replaces(self, db_path, make_user):
        alice = make_user("alice")
        record_feedback(alice, SENTIMENT_POSITIVE, message_id=42)
        record_feedback(alice, SENTIMENT_NEGATIVE, message_id=42,
                        reason="changed my mind")
        rows = list_feedback(alice)
        assert len(rows) == 1
        assert rows[0]["sentiment"] == SENTIMENT_NEGATIVE
        assert rows[0]["reason"] == "changed my mind"

    def test_orphan_feedback_does_not_clobber(self, db_path, make_user):
        alice = make_user("alice")
        record_feedback(alice, SENTIMENT_POSITIVE)
        record_feedback(alice, SENTIMENT_NEGATIVE)
        # Two rows because message_id is NULL on both → no dedup.
        assert len(list_feedback(alice)) == 2


class TestUserScoping:
    def test_list_feedback_only_returns_callers_rows(self, db_path, make_user):
        alice = make_user("alice")
        bob = make_user("bob")
        record_feedback(alice, SENTIMENT_POSITIVE, reason="great")
        record_feedback(bob, SENTIMENT_NEGATIVE, reason="meh")
        assert [r["reason"] for r in list_feedback(alice)] == ["great"]
        assert [r["reason"] for r in list_feedback(bob)] == ["meh"]

    def test_delete_only_works_for_owner(self, db_path, make_user):
        alice = make_user("alice")
        bob = make_user("bob")
        fid = record_feedback(alice, SENTIMENT_POSITIVE)
        # Bob's delete must not affect Alice's row.
        assert delete_feedback(fid, bob) is False
        assert len(list_feedback(alice)) == 1
        # Alice can delete her own row.
        assert delete_feedback(fid, alice) is True
        assert list_feedback(alice) == []


# ── Preference block ────────────────────────────────────────────────────────

class TestPreferenceBlock:
    def test_no_feedback_returns_empty_string(self, db_path, make_user):
        alice = make_user("alice")
        assert build_feedback_preferences_block(alice) == ""

    def test_positive_only_mentions_positive(self, db_path, make_user):
        alice = make_user("alice")
        record_feedback(alice, SENTIMENT_POSITIVE)
        record_feedback(alice, SENTIMENT_POSITIVE)
        block = build_feedback_preferences_block(alice)
        assert "USER RESPONSE PREFERENCES" in block
        assert "2 response(s) as helpful" in block

    def test_negative_reasons_are_quoted(self, db_path, make_user):
        alice = make_user("alice")
        record_feedback(
            alice, SENTIMENT_NEGATIVE,
            reason="Stop giving generic corporate advice.",
        )
        block = build_feedback_preferences_block(alice)
        # Reasons are wrapped in quotes so the model reads them as data,
        # not as a free-floating directive.
        assert "\"Stop giving generic corporate advice.\"" in block

    def test_block_is_deterministic(self, db_path, make_user):
        alice = make_user("alice")
        record_feedback(alice, SENTIMENT_NEGATIVE, reason="too generic")
        record_feedback(alice, SENTIMENT_POSITIVE)
        first = build_feedback_preferences_block(alice)
        second = build_feedback_preferences_block(alice)
        assert first == second

    def test_block_does_not_leak_between_users(self, db_path, make_user):
        alice = make_user("alice")
        bob = make_user("bob")
        record_feedback(alice, SENTIMENT_NEGATIVE, reason="too generic")
        record_feedback(bob, SENTIMENT_POSITIVE)
        alice_block = build_feedback_preferences_block(alice)
        bob_block = build_feedback_preferences_block(bob)
        assert "too generic" in alice_block
        assert "too generic" not in bob_block

    def test_block_caps_recent_negative_reasons(self, db_path, make_user):
        alice = make_user("alice")
        for i in range(20):
            record_feedback(
                alice, SENTIMENT_NEGATIVE,
                reason=f"dislike pattern number {i}",
            )
        block = build_feedback_preferences_block(alice)
        # The header explains where the block came from; we only show a
        # bounded number of bullet points to avoid prompt bloat.
        bullet_count = block.count("\n- ")
        assert bullet_count <= 5

    def test_block_dedupes_identical_reasons(self, db_path, make_user):
        alice = make_user("alice")
        for _ in range(4):
            record_feedback(alice, SENTIMENT_NEGATIVE, reason="too generic")
        block = build_feedback_preferences_block(alice)
        assert block.count("\"too generic\"") == 1


# ── Prompt wiring ───────────────────────────────────────────────────────────

@contextlib.contextmanager
def _stub_chat_runtime(reply: str = "ok"):
    """Stub the network bits so chat() exercises only prompt-shaping."""
    fake_client_chat = MagicMock(return_value={"message": {"content": reply}})
    with patch.object(chat_module.client, "chat", fake_client_chat), \
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
        yield fake_client_chat


def _system_prompt(call_args) -> str:
    messages = call_args.kwargs.get("messages") or call_args.args[1]
    assert messages[0]["role"] == "system"
    return messages[0]["content"]


class TestBuildMessagesIntegration:
    def test_no_feedback_block_when_absent(self):
        msgs = build_messages([], "hi", [], None, None, None)
        assert "USER RESPONSE PREFERENCES" not in msgs[0]["content"]

    def test_feedback_block_is_appended(self):
        block = "USER RESPONSE PREFERENCES:\nfoo"
        msgs = build_messages(
            [], "hi", [], None, None, None,
            feedback_preferences=block,
        )
        assert block in msgs[0]["content"]

    def test_safety_contract_still_comes_first(self):
        block = "USER RESPONSE PREFERENCES:\nfoo"
        msgs = build_messages(
            [], "hi", [], None, None, None,
            feedback_preferences=block,
        )
        sys_msg = msgs[0]["content"]
        # The identity contract — which carries the safety framing — must
        # be positioned strictly before the feedback block. Reversing the
        # order would let a crafted "preference" rewrite identity rules.
        assert sys_msg.index(IDENTITY_CONTRACT) < sys_msg.index(block)


class TestChatPullsFeedbackBlock:
    def test_chat_injects_feedback_block_for_user(self, db_path, make_user):
        alice = make_user("alice")
        record_feedback(
            alice, SENTIMENT_NEGATIVE,
            reason="Stop giving generic advice, focus on the project.",
        )
        with _stub_chat_runtime() as fake_client:
            chat([], "salut", [], alice)
        sys_msg = _system_prompt(fake_client.call_args)
        assert "USER RESPONSE PREFERENCES" in sys_msg
        assert "generic advice" in sys_msg

    def test_chat_safe_when_feedback_lookup_fails(self, db_path, make_user):
        alice = make_user("alice")
        with _stub_chat_runtime() as fake_client, \
                patch.object(
                    chat_module, "build_feedback_preferences_block",
                    side_effect=RuntimeError("boom"),
                ):
            reply, _model = chat([], "salut", [], alice)
        # Chat still completes; the system prompt simply lacks the block.
        assert reply == "ok"
        sys_msg = _system_prompt(fake_client.call_args)
        assert "USER RESPONSE PREFERENCES" not in sys_msg

    def test_default_user_pays_no_prompt_cost(self, db_path, make_user):
        alice = make_user("alice")
        with _stub_chat_runtime() as fake_client:
            chat([], "salut", [], alice)
        sys_msg = _system_prompt(fake_client.call_args)
        assert "USER RESPONSE PREFERENCES" not in sys_msg

    def test_safety_framing_text_is_present_in_block(self, db_path, make_user):
        """
        The block must explicitly tell the model that these preferences
        do not override identity or safety rules. This is the textual
        guardrail that backs the structural one (block ordering).
        """
        alice = make_user("alice")
        record_feedback(alice, SENTIMENT_POSITIVE)
        block = build_feedback_preferences_block(alice)
        assert "must not override" in block

    def test_feedback_does_not_leak_into_other_users_prompt(
        self, db_path, make_user,
    ):
        alice = make_user("alice")
        bob = make_user("bob")
        record_feedback(alice, SENTIMENT_NEGATIVE, reason="too generic")
        with _stub_chat_runtime() as fake_client:
            chat([], "salut", [], bob)
        sys_msg = _system_prompt(fake_client.call_args)
        assert "too generic" not in sys_msg
        assert "USER RESPONSE PREFERENCES" not in sys_msg
