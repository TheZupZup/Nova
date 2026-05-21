"""Tests for the optional local GGUF provider (llama.cpp).

Pinned contracts (see ``core/model_providers/llamacpp.py``):

* the provider implements the :class:`ModelProvider` base interface;
* when the optional ``llama-cpp-python`` wheel is missing, ``health()``
  reports a clean ``ok=False`` and ``generate`` / ``stream`` raise
  :class:`ModelProviderError` — never an ``ImportError`` and never a
  crash;
* a missing model path is a clean health failure (not an exception);
* an invalid model path (wrong extension / not found / unreadable) is
  reported clearly and raised as a sanitised ``ModelProviderError``;
* the model is loaded lazily (construction is cheap) and cached;
* config knobs (context size / threads / gpu layers) reach the backend;
* the happy path returns content and streams fragments;
* operator-facing errors never leak the absolute model path or a raw
  backend exception;
* the registry can select ``llamacpp`` while Ollama stays the default.

No real ``llama-cpp-python`` wheel and no real ``.gguf`` weights are
needed: a fake ``Llama`` class is injected through the constructor, and
the path validator only checks for a readable ``.gguf`` file (it never
parses the model), so a tiny dummy file stands in for one.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

# Match the rest of the suite: stub optional wheels so the package
# imports on a minimal host (``ollama`` is imported by the provider
# package's ``__init__``). ``llama_cpp`` is intentionally NOT stubbed —
# the dependency-missing tests rely on it being absent / forced absent.
for _mod in ("ddgs", "ollama", "sgmllib", "feedparser"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from core import provider_status as ps  # noqa: E402
from core.model_providers import (  # noqa: E402
    GGUFProvider,
    LlamaCppProvider,
    ModelProvider,
    ModelProviderError,
    ModelRequest,
    OllamaProvider,
    available_providers,
    get_provider,
    reset,
)
from core.model_providers import llamacpp as llamacpp_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_registry():
    """No override / cached instance / singleton leaks between tests."""
    reset()
    llamacpp_mod._default = None
    yield
    reset()
    llamacpp_mod._default = None


# ── Fake backend ────────────────────────────────────────────────────


def _make_fake_llama(
    *,
    response: str = "hello from gguf",
    chunks=("Hel", "lo"),
    init_error: Exception | None = None,
    generate_error: Exception | None = None,
):
    """Build a fresh fake ``Llama`` class (isolated per test).

    Tracks constructed instances on ``cls.instances`` so tests can assert
    lazy loading and the kwargs Nova forwarded.
    """

    class _FakeLlama:
        instances: list = []

        def __init__(self, **kwargs):
            if init_error is not None:
                raise init_error
            self.kwargs = kwargs
            type(self).instances.append(self)

        def create_chat_completion(self, messages, stream=False):
            if generate_error is not None:
                raise generate_error
            if stream:
                events = [{"choices": [{"delta": {"role": "assistant"}}]}]
                events += [
                    {"choices": [{"delta": {"content": c}}]} for c in chunks
                ]
                events.append({"choices": [{"delta": {}}]})  # final, empty
                return iter(events)
            return {
                "choices": [
                    {"message": {"role": "assistant", "content": response}}
                ]
            }

    return _FakeLlama


@pytest.fixture
def gguf_file(tmp_path):
    """A readable dummy ``.gguf`` file (contents are never parsed)."""
    path = tmp_path / "model.gguf"
    path.write_bytes(b"GGUF\x00 not real weights")
    return str(path)


# ── Base interface ──────────────────────────────────────────────────


class TestInterface:
    def test_implements_base_interface(self, gguf_file):
        provider = LlamaCppProvider(
            model_path=gguf_file,
            context_size=2048,
            threads=0,
            gpu_layers=0,
            llama_class=_make_fake_llama(),
        )
        assert isinstance(provider, ModelProvider)
        assert provider.name == "llamacpp"
        assert callable(provider.generate)
        assert callable(provider.stream)
        assert callable(provider.health)

    def test_gguf_alias_is_the_same_class(self):
        assert GGUFProvider is LlamaCppProvider


# ── Optional dependency missing ─────────────────────────────────────


class TestDependencyMissing:
    @pytest.fixture(autouse=True)
    def _force_missing(self, monkeypatch):
        # Setting the module to None makes ``import llama_cpp`` raise
        # ImportError deterministically, even on a host that has the
        # wheel installed.
        monkeypatch.setitem(sys.modules, "llama_cpp", None)

    def _provider(self, model_path=""):
        # No llama_class override → the provider imports the real module,
        # which we have forced to be absent.
        return LlamaCppProvider(
            model_path=model_path, context_size=4096, threads=0, gpu_layers=0
        )

    def test_health_reports_missing_dependency(self, gguf_file):
        # Dependency is checked before the path, so a valid path still
        # surfaces the dependency problem (and never raises).
        health = self._provider(model_path=gguf_file).health()
        assert health.ok is False
        assert health.provider == "llamacpp"
        assert "llama-cpp-python" in health.detail
        assert health.models == []

    def test_generate_raises_model_provider_error(self):
        with pytest.raises(ModelProviderError) as exc:
            self._provider().generate(ModelRequest("m", []))
        assert "llama-cpp-python" in str(exc.value)

    def test_stream_raises_model_provider_error(self):
        with pytest.raises(ModelProviderError):
            list(self._provider().stream(ModelRequest("m", [], stream=True)))


# ── Missing / invalid model path (dependency present) ───────────────


class TestModelPathValidation:
    def test_missing_path_is_clean_health_failure(self):
        provider = LlamaCppProvider(
            model_path="",
            context_size=4096,
            threads=0,
            gpu_layers=0,
            llama_class=_make_fake_llama(),
        )
        health = provider.health()
        assert health.ok is False
        assert "NOVA_GGUF_MODEL_PATH" in health.detail
        assert health.models == []

    def test_nonexistent_path_is_clean_health_failure(self, tmp_path):
        missing = str(tmp_path / "absent.gguf")
        provider = LlamaCppProvider(
            model_path=missing,
            context_size=4096,
            threads=0,
            gpu_layers=0,
            llama_class=_make_fake_llama(),
        )
        health = provider.health()
        assert health.ok is False
        assert health.detail
        # The absolute path must not be echoed back.
        assert missing not in health.detail

    def test_wrong_extension_is_rejected(self, tmp_path):
        wrong = tmp_path / "model.bin"
        wrong.write_bytes(b"not gguf")
        provider = LlamaCppProvider(
            model_path=str(wrong),
            context_size=4096,
            threads=0,
            gpu_layers=0,
            llama_class=_make_fake_llama(),
        )
        health = provider.health()
        assert health.ok is False
        assert ".gguf" in health.detail

    def test_generate_invalid_path_raises_sanitised_error(self, tmp_path):
        missing = str(tmp_path / "absent.gguf")
        provider = LlamaCppProvider(
            model_path=missing,
            context_size=4096,
            threads=0,
            gpu_layers=0,
            llama_class=_make_fake_llama(),
        )
        with pytest.raises(ModelProviderError) as exc:
            provider.generate(ModelRequest("m", []))
        # Clean, operator-facing reason — never the raw absolute path.
        assert missing not in str(exc.value)


# ── Happy path ──────────────────────────────────────────────────────


class TestGeneration:
    def _provider(self, gguf_file, **kw):
        return LlamaCppProvider(
            model_path=gguf_file,
            context_size=kw.pop("context_size", 2048),
            threads=kw.pop("threads", 0),
            gpu_layers=kw.pop("gpu_layers", 0),
            llama_class=kw.pop("llama_class", _make_fake_llama()),
        )

    def test_generate_returns_content(self, gguf_file):
        provider = self._provider(
            gguf_file, llama_class=_make_fake_llama(response="bonjour")
        )
        out = provider.generate(
            ModelRequest("ignored-name", [{"role": "user", "content": "hi"}])
        )
        assert out.content == "bonjour"
        # Echoes the requested model label even though llama.cpp ignores it.
        assert out.model == "ignored-name"

    def test_stream_yields_fragments_and_skips_empty_deltas(self, gguf_file):
        provider = self._provider(
            gguf_file, llama_class=_make_fake_llama(chunks=("Hel", "lo", "!"))
        )
        chunks = list(provider.stream(ModelRequest("m", [], stream=True)))
        assert [c.content for c in chunks] == ["Hel", "lo", "!"]

    def test_health_ok_lists_only_basename(self, gguf_file):
        provider = self._provider(gguf_file)
        health = provider.health()
        assert health.ok is True
        assert health.provider == "llamacpp"
        assert health.models == ["model.gguf"]
        # The directory must not leak through the models list.
        assert gguf_file not in health.models

    def test_model_is_loaded_lazily_and_cached(self, gguf_file):
        fake = _make_fake_llama()
        provider = self._provider(gguf_file, llama_class=fake)
        # Construction must not load the model.
        assert fake.instances == []
        provider.generate(ModelRequest("m", []))
        provider.generate(ModelRequest("m", []))
        # Loaded exactly once and reused.
        assert len(fake.instances) == 1

    def test_config_knobs_reach_backend(self, gguf_file):
        fake = _make_fake_llama()
        provider = LlamaCppProvider(
            model_path=gguf_file,
            context_size=8192,
            threads=6,
            gpu_layers=20,
            llama_class=fake,
        )
        provider.generate(ModelRequest("m", []))
        kwargs = fake.instances[0].kwargs
        assert kwargs["model_path"] == gguf_file
        assert kwargs["n_ctx"] == 8192
        assert kwargs["n_threads"] == 6
        assert kwargs["n_gpu_layers"] == 20
        assert kwargs["verbose"] is False

    def test_threads_zero_is_not_forwarded(self, gguf_file):
        fake = _make_fake_llama()
        provider = LlamaCppProvider(
            model_path=gguf_file,
            context_size=4096,
            threads=0,
            gpu_layers=0,
            llama_class=fake,
        )
        provider.generate(ModelRequest("m", []))
        # 0 threads => let llama.cpp decide (kwarg omitted).
        assert "n_threads" not in fake.instances[0].kwargs


# ── Error sanitisation ──────────────────────────────────────────────


class TestErrorSanitisation:
    def test_load_failure_is_sanitised(self, gguf_file):
        secret = "/mnt/secret/path/leak.gguf boom"
        provider = LlamaCppProvider(
            model_path=gguf_file,
            context_size=4096,
            threads=0,
            gpu_layers=0,
            llama_class=_make_fake_llama(init_error=RuntimeError(secret)),
        )
        with pytest.raises(ModelProviderError) as exc:
            provider.generate(ModelRequest("m", []))
        assert "/mnt/secret" not in str(exc.value)
        assert "boom" not in str(exc.value)

    def test_generation_backend_error_is_sanitised(self, gguf_file):
        secret = "internal cuda assert at 0xdeadbeef"
        provider = LlamaCppProvider(
            model_path=gguf_file,
            context_size=4096,
            threads=0,
            gpu_layers=0,
            llama_class=_make_fake_llama(
                generate_error=RuntimeError(secret)
            ),
        )
        with pytest.raises(ModelProviderError) as exc:
            provider.generate(ModelRequest("m", []))
        assert "0xdeadbeef" not in str(exc.value)


# ── Registry integration ────────────────────────────────────────────


class TestRegistry:
    def test_registry_can_select_llamacpp(self):
        assert "llamacpp" in available_providers()
        assert isinstance(get_provider("llamacpp"), LlamaCppProvider)

    def test_ollama_remains_default(self, monkeypatch):
        monkeypatch.setattr("config.MODEL_PROVIDER", "ollama")
        reset()
        assert isinstance(get_provider(), OllamaProvider)

    def test_config_can_select_llamacpp_as_default(self, monkeypatch):
        monkeypatch.setattr("config.MODEL_PROVIDER", "llamacpp")
        reset()
        llamacpp_mod._default = None
        assert isinstance(get_provider(), LlamaCppProvider)

    def test_llamacpp_is_selectable_not_test_only(self, monkeypatch):
        monkeypatch.setattr("config.MODEL_PROVIDER", "ollama")
        status = ps.get_provider_status()
        assert "llamacpp" in status.selectable_providers
        assert "llamacpp" not in status.test_only_providers
