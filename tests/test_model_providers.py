"""Tests for the model-provider abstraction (Nova ↔ backend seam).

Covers:
  * OllamaProvider preserves the exact pre-refactor request behaviour
    (same ``client.chat(model=, messages=)`` shape, same response
    mapping, same stream chunk duck-typing + legacy ``TypeError``
    fallback) and maps backend failures to ``ModelProviderError``.
  * OllamaProvider.health() is a clean read-only probe that never raises.
  * MockProvider is deterministic and records requests.
  * The registry resolves Ollama by default, supports overrides, and
    raises a typed error for unknown providers.
  * core.chat.chat / chat_stream route through the provider interface
    rather than a concrete client, and a provider-health/availability
    failure is surfaced cleanly (no regression to the existing
    "Ollama unreachable" reply / stream `error` event).
"""

from __future__ import annotations

import contextlib
import sqlite3
import sys
from unittest.mock import MagicMock, patch

import pytest

# Match the rest of the suite: stub optional wheels if absent so this
# file collects on a minimal host. ``ollama`` is real in CI (the
# transport-error tests monkeypatch ``ollama.ResponseError`` exactly the
# way ``test_chat_stream`` already does).
for _mod in ("ddgs", "ollama", "sgmllib", "feedparser"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

import ollama  # noqa: E402

from core import chat as chat_module  # noqa: E402
from core import memory as core_memory, ollama_client, users  # noqa: E402
from core.chat import chat, chat_stream  # noqa: E402
from core.model_providers import (  # noqa: E402
    MockProvider,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    OllamaProvider,
    ProviderHealth,
    available_providers,
    get_provider,
    register_provider,
    reset,
    use_provider,
)
from core.model_providers import registry as registry_mod  # noqa: E402
from memory import store as natural_store  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_registry():
    """No override / no cached instances leaks between tests."""
    reset()
    yield
    reset()


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "nova.db")
    monkeypatch.setattr(core_memory, "DB_PATH", path)
    monkeypatch.setattr(natural_store, "DB_PATH", path)
    core_memory.initialize_db()
    return path


def _make_user(db_path, username="alice"):
    with sqlite3.connect(db_path) as conn:
        return users.create_user(conn, username, "pw", role=users.ROLE_USER)


class _SubscriptableEvent:
    """Mirror of ``ollama.ChatResponse`` — subscriptable, not a ``dict``."""

    def __init__(self, **fields):
        self._fields = fields

    def get(self, key, default=None):
        return self._fields.get(key, default)

    def __getitem__(self, key):
        return self._fields[key]


# ── OllamaProvider: request behaviour ───────────────────────────────────────


class TestOllamaProviderRequestBehaviour:
    def test_generate_uses_exact_legacy_call_shape(self):
        fake = MagicMock(return_value={"message": {"content": "hi there"}})
        provider = OllamaProvider(client=MagicMock(chat=fake))

        out = provider.generate(
            ModelRequest(model="gemma4", messages=[{"role": "user", "content": "yo"}])
        )

        assert isinstance(out, ModelResponse)
        assert out.content == "hi there"
        assert out.model == "gemma4"
        fake.assert_called_once_with(
            model="gemma4", messages=[{"role": "user", "content": "yo"}]
        )

    def test_generate_handles_subscriptable_chatresponse(self):
        resp = _SubscriptableEvent(
            message=_SubscriptableEvent(content="pong")
        )
        provider = OllamaProvider(client=MagicMock(chat=MagicMock(return_value=resp)))
        assert provider.generate(ModelRequest("m", [])).content == "pong"

    def test_stream_yields_chunks_and_skips_empty_metadata_frames(self):
        events = [
            {"message": {"content": "Hel"}, "done": False},
            {"message": {"content": ""}, "done": False},  # metadata frame
            {"message": {"content": "lo"}, "done": False},
            {"message": {"content": ""}, "done": True},  # synthetic done
        ]
        client = MagicMock()
        client.chat.return_value = iter(events)
        provider = OllamaProvider(client=client)

        chunks = list(provider.stream(ModelRequest("m", [], stream=True)))

        assert [c.content for c in chunks] == ["Hel", "lo"]
        client.chat.assert_called_once_with(model="m", messages=[], stream=True)

    def test_stream_handles_subscriptable_chatresponse_shape(self):
        # Regression: ``ollama>=0.4`` streams Pydantic objects, not dicts.
        events = [
            _SubscriptableEvent(
                message=_SubscriptableEvent(content="bon"), done=False
            ),
            _SubscriptableEvent(
                message=_SubscriptableEvent(content="jour"), done=False
            ),
            _SubscriptableEvent(
                message=_SubscriptableEvent(content=""), done=True
            ),
        ]
        client = MagicMock()
        client.chat.return_value = iter(events)
        provider = OllamaProvider(client=client)

        chunks = list(provider.stream(ModelRequest("m", [], stream=True)))
        assert "".join(c.content for c in chunks) == "bonjour"

    def test_stream_falls_back_when_client_lacks_stream_kwarg(self):
        # An old ollama-python without the stream= kwarg raises TypeError;
        # the provider degrades to a single non-streamed reply.
        def chat(*args, **kwargs):
            if kwargs.get("stream"):
                raise TypeError("unexpected keyword argument 'stream'")
            return {"message": {"content": "single shot"}}

        provider = OllamaProvider(client=MagicMock(chat=chat))
        chunks = list(provider.stream(ModelRequest("m", [], stream=True)))
        assert [c.content for c in chunks] == ["single shot"]

    def test_default_resolves_shared_singleton(self):
        # No injected client → the shared core.ollama_client.client, so a
        # patch on its .chat is seen (this is the test seam the existing
        # chat suites rely on).
        provider = OllamaProvider()
        with patch.object(
            ollama_client.client, "chat",
            return_value={"message": {"content": "shared"}},
        ):
            assert provider.generate(ModelRequest("m", [])).content == "shared"


# ── OllamaProvider: failure mapping ─────────────────────────────────────────


class TestOllamaProviderFailureMapping:
    @pytest.mark.parametrize(
        "exc",
        [ConnectionError("down"), OSError("refused")],
    )
    def test_generate_maps_transport_errors(self, exc):
        provider = OllamaProvider(client=MagicMock(chat=MagicMock(side_effect=exc)))
        with pytest.raises(ModelProviderError):
            provider.generate(ModelRequest("m", []))

    def test_generate_maps_ollama_response_error(self):
        class _FakeResponseError(Exception):
            pass

        with patch.object(ollama, "ResponseError", _FakeResponseError):
            provider = OllamaProvider(
                client=MagicMock(chat=MagicMock(side_effect=_FakeResponseError("x")))
            )
            with pytest.raises(ModelProviderError):
                provider.generate(ModelRequest("m", []))

    def test_stream_maps_transport_error_during_iteration(self):
        def gen():
            yield {"message": {"content": "partial"}}
            raise ConnectionError("dropped mid-stream")

        provider = OllamaProvider(client=MagicMock(chat=MagicMock(return_value=gen())))
        with pytest.raises(ModelProviderError):
            list(provider.stream(ModelRequest("m", [], stream=True)))


# ── OllamaProvider: health ──────────────────────────────────────────────────


class TestOllamaProviderHealth:
    def test_health_ok_lists_models(self):
        client = MagicMock()
        client.list.return_value = {
            "models": [{"name": "gemma4"}, {"model": "qwen2.5:32b"}]
        }
        health = OllamaProvider(client=client).health()
        assert health.ok is True
        assert health.provider == "ollama"
        assert set(health.models) == {"gemma4", "qwen2.5:32b"}

    def test_health_failure_is_clean_never_raises(self):
        client = MagicMock()
        client.list.side_effect = ConnectionError("no daemon")
        health = OllamaProvider(client=client).health()
        assert isinstance(health, ProviderHealth)
        assert health.ok is False
        assert health.provider == "ollama"
        assert health.detail  # short, non-empty reason


# ── MockProvider ────────────────────────────────────────────────────────────


class TestMockProvider:
    def test_generate_is_deterministic_and_records_requests(self):
        mp = MockProvider(response="always this")
        req = ModelRequest("m", [{"role": "user", "content": "q"}])
        assert mp.generate(req).content == "always this"
        assert mp.generate(req).content == "always this"
        assert mp.requests == [req, req]

    def test_stream_chunks_concat_matches_generate(self):
        mp = MockProvider(chunks=["a", "b", "c"])
        streamed = "".join(c.content for c in mp.stream(ModelRequest("m", [])))
        assert streamed == "abc"
        assert mp.generate(ModelRequest("m", [])).content == "abc"

    def test_error_injection_raises_for_both_paths(self):
        mp = MockProvider(error=ModelProviderError("simulated outage"))
        with pytest.raises(ModelProviderError):
            mp.generate(ModelRequest("m", []))
        with pytest.raises(ModelProviderError):
            list(mp.stream(ModelRequest("m", [])))

    def test_health_reflects_configuration(self):
        assert MockProvider().health().ok is True
        unhealthy = MockProvider(healthy=False).health()
        assert unhealthy.ok is False
        assert unhealthy.detail


# ── Registry ────────────────────────────────────────────────────────────────


class TestRegistry:
    def test_default_is_ollama(self):
        assert isinstance(get_provider(), OllamaProvider)

    def test_named_lookup_and_singleton_caching(self):
        a = get_provider("ollama")
        b = get_provider("ollama")
        assert a is b
        assert isinstance(get_provider("mock"), MockProvider)

    def test_unknown_provider_raises_typed_error(self):
        with pytest.raises(ModelProviderError):
            get_provider("does-not-exist")

    def test_config_default_is_honoured(self, monkeypatch):
        monkeypatch.setattr("config.MODEL_PROVIDER", "mock")
        reset()
        assert isinstance(get_provider(), MockProvider)

    def test_register_provider_plugs_in_a_new_backend(self):
        sentinel = MockProvider(response="future-runtime")
        register_provider("future", lambda: sentinel)
        try:
            assert "future" in available_providers()
            assert get_provider("future") is sentinel
        finally:
            registry_mod._factories.pop("future", None)
            registry_mod._instances.pop("future", None)

    def test_use_provider_override_scopes_to_block(self):
        mock = MockProvider(response="overridden")
        with use_provider(mock):
            assert get_provider() is mock
            assert get_provider("ollama") is mock  # override wins
        assert isinstance(get_provider(), OllamaProvider)


# ── core.chat routes through the provider interface ─────────────────────────


@contextlib.contextmanager
def _neutralise_chat_side_effects():
    with patch.object(chat_module, "route", lambda _m: "default"), \
         patch.object(chat_module, "should_search", lambda _m: False), \
         patch.object(chat_module, "is_security_query", lambda _m: False), \
         patch.object(chat_module, "detect_weather_city", lambda _m: None), \
         patch.object(chat_module, "get_relevant_memories", lambda *_a, **_k: []), \
         patch.object(chat_module, "extract_and_save_memory", lambda *_a, **_k: None), \
         patch.object(
             chat_module, "_extract_and_save_natural_memories",
             lambda *_a, **_k: None,
         ):
        yield


class TestChatUsesProviderInterface:
    def test_chat_returns_provider_output(self, db_path):
        alice = _make_user(db_path)
        mock = MockProvider(response="from the provider")
        with use_provider(mock), _neutralise_chat_side_effects():
            reply, model = chat([], "hello", [], alice)
        assert reply == "from the provider"
        assert model == "default"
        # Nova core handed the assembled messages to the provider, with
        # the system/identity prompt first — provider never reorders it.
        assert mock.requests
        sent = mock.requests[-1]
        assert isinstance(sent, ModelRequest)
        assert sent.messages[0]["role"] == "system"

    def test_chat_stream_streams_provider_chunks(self, db_path):
        alice = _make_user(db_path)
        mock = MockProvider(chunks=["Hel", "lo", "!"])
        with use_provider(mock), _neutralise_chat_side_effects():
            events = list(chat_stream([], "hi", [], alice))
        assert events[0]["type"] == "meta"
        deltas = [e["content"] for e in events if e["type"] == "delta"]
        assert deltas == ["Hel", "lo", "!"]
        done = events[-1]
        assert done["type"] == "done"
        assert done["reply"] == "Hello!"

    def test_chat_provider_failure_maps_to_unreachable_reply(self, db_path):
        alice = _make_user(db_path)
        broken = MockProvider(error=ModelProviderError("backend down"))
        with use_provider(broken), _neutralise_chat_side_effects():
            reply, model = chat([], "hi", [], alice)
        assert reply == chat_module.OLLAMA_UNAVAILABLE

    def test_chat_stream_provider_failure_yields_clean_error(self, db_path):
        alice = _make_user(db_path)
        broken = MockProvider(error=ModelProviderError("backend down"))
        with use_provider(broken), _neutralise_chat_side_effects():
            events = list(chat_stream([], "hi", [], alice))
        assert any(e["type"] == "error" for e in events)
        assert not any(e["type"] == "done" for e in events)
        err = next(e for e in events if e["type"] == "error")
        assert err["detail"] == chat_module.OLLAMA_UNAVAILABLE
