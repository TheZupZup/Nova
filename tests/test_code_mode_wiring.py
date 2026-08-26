"""Code mode grounding reaches the prompt — and only when it should.

Covers the last link in the chain: the repository briefing built by
``core.code_context`` is threaded through ``core.chat`` into the system
prompt, sits *below* the identity contract, and is attached by the web
layer only for an explicit Code-mode turn on a project whose owner
linked a repository.

Also pins the compatibility promise: nothing here changes routing, the
``NOVA_*_MODEL`` variables, or the behaviour of a chat with no repo.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

for _mod in ("ddgs", "ollama"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from core.chat import build_messages, chat, chat_stream  # noqa: E402
from core.identity import IDENTITY_CONTRACT  # noqa: E402

BLOCK = "REPOSITORY CONTEXT (read-only, may be partial):\nBranch: main"


def _system(messages):
    return messages[0]["content"]


class TestBuildMessages:
    def test_block_is_appended_to_the_system_prompt(self):
        messages = build_messages([], "hi", [], code_context=BLOCK)
        assert BLOCK in _system(messages)

    def test_block_sits_below_the_identity_contract(self):
        system = _system(build_messages([], "hi", [], code_context=BLOCK))
        assert system.index(IDENTITY_CONTRACT) < system.index(BLOCK)

    def test_block_sits_below_the_feedback_preferences(self):
        system = _system(build_messages(
            [], "hi", [], feedback_preferences="PREFS", code_context=BLOCK,
        ))
        assert system.index("PREFS") < system.index(BLOCK)

    def test_omitted_block_changes_nothing(self):
        assert (
            build_messages([], "hi", [])
            == build_messages([], "hi", [], code_context=None)
            == build_messages([], "hi", [], code_context="")
        )

    def test_block_also_applies_on_the_search_branch(self):
        messages = build_messages(
            [], "hi", [], "results", "search", code_context=BLOCK
        )
        assert BLOCK in _system(messages)


class TestChatThreading:
    def _capture(self, monkeypatch):
        seen = {}

        def _fake_generate(model, messages, **kwargs):
            seen["messages"] = messages
            return "ok"

        monkeypatch.setattr("core.chat._generate", _fake_generate)
        monkeypatch.setattr("core.chat.route", lambda _: "code-model")
        monkeypatch.setattr(
            "core.chat.get_relevant_memories", lambda *a, **k: []
        )
        monkeypatch.setattr("core.chat.should_search", lambda _: False)
        monkeypatch.setattr(
            "core.chat.detect_weather_city", lambda _: "no_match"
        )
        monkeypatch.setattr(
            "core.chat._autosave_allowed", lambda *a, **k: False
        )
        return seen

    def test_chat_forwards_the_block(self, monkeypatch):
        seen = self._capture(monkeypatch)
        reply, model = chat([], "fix it", [], 1, code_context=BLOCK)
        assert reply == "ok"
        assert BLOCK in _system(seen["messages"])

    def test_chat_without_a_block_is_unchanged(self, monkeypatch):
        seen = self._capture(monkeypatch)
        chat([], "fix it", [], 1)
        assert "REPOSITORY CONTEXT" not in _system(seen["messages"])

    def test_chat_stream_forwards_the_block(self, monkeypatch):
        seen = {}

        def _fake_stream(model, messages, **kwargs):
            seen["messages"] = messages
            yield {"type": "delta", "content": "ok"}
            return "ok"

        monkeypatch.setattr("core.chat.route", lambda _: "code-model")
        monkeypatch.setattr(
            "core.chat.get_relevant_memories", lambda *a, **k: []
        )
        monkeypatch.setattr("core.chat.should_search", lambda _: False)
        monkeypatch.setattr(
            "core.chat.detect_weather_city", lambda _: "no_match"
        )
        monkeypatch.setattr(
            "core.chat._autosave_allowed", lambda *a, **k: False
        )
        monkeypatch.setattr(
            "core.chat._stream_and_accumulate", _fake_stream
        )
        list(chat_stream([], "fix it", [], 1, code_context=BLOCK))
        assert BLOCK in _system(seen["messages"])


class TestWebResolution:
    """``web._resolve_code_context`` gates attachment conservatively."""

    @pytest.fixture
    def web(self):
        import web as web_module

        return web_module

    def _request(self, web, mode):
        return web.ChatRequest(message="fix core/router.py", mode=mode)

    def _user(self):
        user = MagicMock()
        user.id = 1
        return user

    @pytest.mark.parametrize("mode", ["chat", "auto", "deep", ""])
    def test_non_code_modes_get_no_context(self, web, mode):
        with patch.object(web._projects, "get_local_repo_path") as get_path:
            assert web._resolve_code_context(
                self._request(web, mode), self._user(), 7
            ) is None
            get_path.assert_not_called()

    def test_general_chat_without_a_project_gets_no_context(self, web):
        with patch.object(web._projects, "get_local_repo_path") as get_path:
            assert web._resolve_code_context(
                self._request(web, "code"), self._user(), None
            ) is None
            get_path.assert_not_called()

    def test_project_without_a_linked_repo_gets_no_context(self, web):
        with patch.object(
            web._projects, "get_local_repo_path", return_value=None
        ):
            assert web._resolve_code_context(
                self._request(web, "code"), self._user(), 7
            ) is None

    def test_linked_repo_in_code_mode_gets_the_block(self, web):
        built = MagicMock()
        built.as_prompt_block.return_value = BLOCK
        with patch.object(
            web._projects, "get_local_repo_path", return_value="/repos/x"
        ), patch.object(
            web._code_context, "build_code_context", return_value=built
        ) as build:
            assert web._resolve_code_context(
                self._request(web, "code"), self._user(), 7
            ) == BLOCK
            # The repo path comes from the *stored project*, never the
            # request body — a client cannot name a directory to read.
            build.assert_called_once_with("/repos/x", "fix core/router.py")

    def test_repo_path_is_scoped_to_the_calling_user(self, web):
        with patch.object(
            web._projects, "get_local_repo_path", return_value=None
        ) as get_path:
            web._resolve_code_context(
                self._request(web, "code"), self._user(), 7
            )
            get_path.assert_called_once_with(7, 1)

    def test_a_failure_degrades_to_no_context(self, web):
        with patch.object(
            web._projects, "get_local_repo_path",
            side_effect=RuntimeError("boom"),
        ):
            assert web._resolve_code_context(
                self._request(web, "code"), self._user(), 7
            ) is None

    def test_empty_block_becomes_none(self, web):
        built = MagicMock()
        built.as_prompt_block.return_value = ""
        with patch.object(
            web._projects, "get_local_repo_path", return_value="/repos/x"
        ), patch.object(
            web._code_context, "build_code_context", return_value=built
        ):
            assert web._resolve_code_context(
                self._request(web, "code"), self._user(), 7
            ) is None


class TestCompatibility:
    def test_mode_map_is_unchanged(self):
        import web
        from config import MODELS

        assert web.MODE_MAP == {
            "chat": MODELS["default"],
            "code": MODELS["code"],
            "deep": MODELS["advanced"],
        }

    def test_router_map_is_unchanged(self):
        from config import MODELS
        from core.router import MODEL_MAP

        assert MODEL_MAP == {
            "simple": MODELS["default"],
            "normal": MODELS["default"],
            "advanced": MODELS["advanced"],
            "code": MODELS["code"],
        }

    def test_every_legacy_model_env_var_still_selects_its_role(
        self, monkeypatch
    ):
        import importlib

        import config

        for var, key, value in (
            ("NOVA_ROUTER_MODEL", "router", "r:1b"),
            ("NOVA_DEFAULT_MODEL", "default", "d:7b"),
            ("NOVA_CODE_MODEL", "code", "nova-coder:14b"),
            ("NOVA_ADVANCED_MODEL", "advanced", "a:32b"),
        ):
            monkeypatch.setenv(var, value)
            importlib.reload(config)
            assert config.MODELS[key] == value
            monkeypatch.delenv(var, raising=False)
        importlib.reload(config)
