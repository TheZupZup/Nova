"""
Tests that the personalization values stored under each user actually
reach the system prompt that Ollama sees.

The persistence + HTTP layer is covered by `test_personalization_settings`.
This file covers the next link in the chain:

  * `core.chat.build_messages` accepts a `personalization` payload and
    appends the resulting block after the identity contract.
  * `core.chat.chat` pulls the caller's personalization from the
    per-user settings table and threads it through `build_messages`,
    so two users in the same SQLite DB get different system prompts.

The Ollama client and weather/search tools are stubbed out so the test
exercises only the prompt-shaping behaviour, never the network.
"""

from __future__ import annotations

import contextlib
import sqlite3
import sys
from unittest.mock import MagicMock, patch

import pytest

# Heavy network deps the chat module imports at module load. Stub them
# before the import so a missing wheel never blocks this test file.
for _mod in ("ddgs", "ollama"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from core import chat as chat_module  # noqa: E402
from core import memory as core_memory, settings as core_settings, users  # noqa: E402
from core.chat import build_messages, chat  # noqa: E402
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


@contextlib.contextmanager
def stub_chat_runtime(reply: str = "ok"):
    """
    Mock the bits of `core.chat` that talk to the outside world so the
    function under test exercises only the prompt-shaping path. Returns
    a MagicMock for `client.chat` so tests can inspect what was sent.

    `extract_and_save_memory` and `_extract_and_save_natural_memories`
    are also stubbed out — both fire after the main reply and would
    otherwise enqueue extra `client.chat` calls that confuse
    `call_args` (which only ever holds the *last* call).
    """
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
    """Pull the system message out of a recorded `client.chat(...)` call."""
    messages = call_args.kwargs.get("messages") or call_args.args[1]
    assert messages[0]["role"] == "system"
    return messages[0]["content"]


# ── build_messages: direct personalization injection ────────────────────────

class TestBuildMessagesPersonalization:
    def test_no_personalization_keeps_existing_layout(self):
        msgs = build_messages([], "hi", [], None, None, None)
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"].startswith(IDENTITY_CONTRACT)
        # No PRÉFÉRENCES block when personalization is absent.
        assert "PRÉFÉRENCES UTILISATEUR" not in msgs[0]["content"]

    def test_default_personalization_keeps_existing_layout(self):
        # Calling with the default payload must be indistinguishable from
        # not passing personalization — same prompt, same token cost.
        msgs = build_messages(
            [], "hi", [], None, None, None,
            personalization=dict(core_settings.PERSONALIZATION_DEFAULTS),
        )
        assert "PRÉFÉRENCES UTILISATEUR" not in msgs[0]["content"]

    def test_concise_style_appears_in_system_prompt(self):
        msgs = build_messages(
            [], "hi", [], None, None, None,
            personalization={"response_style": "concise"},
        )
        sys_msg = msgs[0]["content"]
        assert "PRÉFÉRENCES UTILISATEUR" in sys_msg
        # The contract must still be present and come first.
        assert sys_msg.index(IDENTITY_CONTRACT) < sys_msg.index(
            "PRÉFÉRENCES UTILISATEUR"
        )

    def test_emoji_none_lands_in_system_prompt(self):
        msgs = build_messages(
            [], "hi", [], None, None, None,
            personalization={"emoji_level": "none"},
        )
        sys_msg = msgs[0]["content"].lower()
        assert "emoji" in sys_msg and "ne pas" in sys_msg

    def test_emoji_expressive_lands_in_system_prompt(self):
        # The expressive level is opt-in: the user wants a slightly
        # warmer feel in casual chat, with the same sober behaviour on
        # code / PR / security still spelled out.
        msgs = build_messages(
            [], "hi", [], None, None, None,
            personalization={"emoji_level": "expressive"},
        )
        sys_msg = msgs[0]["content"].lower()
        assert "emoji" in sys_msg
        # The expressive line must mention the sober domains so the
        # directive cannot leak into technical replies.
        assert any(
            marker in sys_msg
            for marker in ("technique", "code", "pr", "sécurité", "doc")
        )

    def test_high_warmth_lands_in_system_prompt(self):
        msgs = build_messages(
            [], "hi", [], None, None, None,
            personalization={"warmth_level": "high"},
        )
        sys_msg = msgs[0]["content"].lower()
        assert "chaleureu" in sys_msg or "attentionné" in sys_msg

    def test_custom_instructions_land_in_system_prompt(self):
        msgs = build_messages(
            [], "hi", [], None, None, None,
            personalization={"custom_instructions": "Toujours signer 'Nova'."},
        )
        assert "Toujours signer 'Nova'." in msgs[0]["content"]


# ── chat(): pulls personalization from settings, scoped per-user ────────────

class TestChatPullsPerUserPersonalization:
    def test_chat_injects_users_saved_personalization(self, db_path, make_user):
        alice = make_user("alice")
        core_settings.save_user_setting(alice, "response_style", "technical")
        core_settings.save_user_setting(alice, "emoji_level", "none")

        with stub_chat_runtime() as fake_client:
            chat([], "expliquer fork()", [], alice)

        sys_msg = _system_prompt(fake_client.call_args).lower()
        assert "technique" in sys_msg
        assert "emoji" in sys_msg and "ne pas" in sys_msg

    def test_chat_does_not_leak_personalization_between_users(
        self, db_path, make_user,
    ):
        """
        Per-user isolation: Alice's "no emojis" preference must not show
        up in Bob's system prompt, and Bob's defaults must produce a
        clean prompt with no PRÉFÉRENCES block.
        """
        alice = make_user("alice")
        bob = make_user("bob")
        core_settings.save_user_setting(alice, "emoji_level", "none")
        core_settings.save_user_setting(alice, "warmth_level", "high")

        # Alice: gets her settings.
        with stub_chat_runtime() as fake_client:
            chat([], "salut", [], alice)
        alice_sys = _system_prompt(fake_client.call_args)
        assert "PRÉFÉRENCES UTILISATEUR" in alice_sys

        # Bob: same DB, same run — no preferences saved.
        with stub_chat_runtime() as fake_client:
            chat([], "salut", [], bob)
        bob_sys = _system_prompt(fake_client.call_args)
        assert "PRÉFÉRENCES UTILISATEUR" not in bob_sys

    def test_default_user_chat_prompt_is_unchanged(self, db_path, make_user):
        """
        A fresh account that never opened the panel must produce the
        same system prompt as before personalization existed. Guards
        against accidental token-bloat for the no-config case.
        """
        a = make_user("alice")
        with stub_chat_runtime() as fake_client:
            chat([], "salut", [], a)
        sys_msg = _system_prompt(fake_client.call_args)
        assert "PRÉFÉRENCES UTILISATEUR" not in sys_msg

    def test_chat_is_resilient_to_settings_failure(self, db_path, make_user):
        """
        If `get_personalization` raises (e.g. transient DB lock), the
        chat flow must still complete, just without the personalization
        block. The user's reply matters more than their tone preference.
        """
        a = make_user("alice")
        with stub_chat_runtime() as fake_client, \
                patch.object(
                    chat_module, "get_personalization",
                    side_effect=RuntimeError("boom"),
                ):
            reply, _model = chat([], "salut", [], a)
        assert reply == "ok"
        sys_msg = _system_prompt(fake_client.call_args)
        # No block, but the contract is still there.
        assert "PRÉFÉRENCES UTILISATEUR" not in sys_msg
        assert IDENTITY_CONTRACT in sys_msg
