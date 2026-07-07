"""Model flexibility & operator control.

Nova is a local assistant *runtime*, not a fixed model or a fixed
personality. This suite pins the behaviour that makes that true and keeps
it from regressing:

  * the four routing roles are configurable through ``NOVA_*_MODEL`` env
    vars, and the shipped defaults stay backward-compatible;
  * a missing model and a crashed/OOM-killed model surface as distinct,
    honest chat replies — never a false "Ollama is unreachable";
  * the read-only ``core.model_status`` reports the configured map,
    reachability, and installed-vs-missing models without a write/pull;
  * the optional model bootstrap is opt-in, background, and a strict
    no-op by default (no surprise downloads);
  * the product docs frame Nova as an assistant with operator-controlled,
    lightweight-configurable models — never as an AI girlfriend / romantic
    partner — and keep the low-RAM guidance discoverable.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Defensive stubs for optional wheels the chat import chain pulls in on a
# minimal host (mirrors the rest of the suite). ``ollama`` is intentionally
# left real — the classification tests monkeypatch ``ollama.ResponseError``.
for _mod in ("ddgs", "duckduckgo_search", "sgmllib", "feedparser"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

import config  # noqa: E402
import ollama  # noqa: E402

from core import chat as chat_module  # noqa: E402
from core import model_bootstrap, model_status  # noqa: E402
from core.model_providers import (  # noqa: E402
    ERROR_BACKEND,
    ERROR_MODEL_MISSING,
    ERROR_MODEL_RUNTIME,
    ERROR_UNREACHABLE,
    ModelProviderError,
    ModelRequest,
    OllamaProvider,
)
from core.model_providers import ollama as ollama_provider  # noqa: E402


REPO = Path(__file__).resolve().parents[1]

_MODEL_ENV_KEYS = (
    "NOVA_ROUTER_MODEL",
    "NOVA_DEFAULT_MODEL",
    "NOVA_CODE_MODEL",
    "NOVA_ADVANCED_MODEL",
    "NOVA_AUTO_PULL_MODELS",
    "NOVA_AUTO_UPDATE_MODELS",
    "NOVA_BOOTSTRAP_MODELS",
)

_DEFAULT_MODELS = {
    "router": "gemma3:1b",
    "default": "gemma4",
    "code": "deepseek-coder-v2",
    "advanced": "qwen2.5:32b",
}


# ── Configurable model map ──────────────────────────────────────────────────


class TestModelMapConfig:
    @pytest.fixture(autouse=True)
    def _restore_config(self):
        """Snapshot the model-related env, then restore + reload config.

        Each test mutates ``os.environ`` and reloads ``config`` to observe
        the resolved map; this fixture guarantees the module is returned to
        its pristine (env-clean) state afterwards so other suites are
        unaffected.
        """
        saved = {k: os.environ.get(k) for k in _MODEL_ENV_KEYS}
        yield
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(config)

    def _reload_clean(self):
        for k in _MODEL_ENV_KEYS:
            os.environ.pop(k, None)
        return importlib.reload(config)

    def test_model_from_env_helper(self):
        # Set → the value; blank/whitespace → the default; unset → default.
        os.environ["NOVA_DEFAULT_MODEL"] = "custom:7b"
        assert config._model_from_env("NOVA_DEFAULT_MODEL", "d") == "custom:7b"
        os.environ["NOVA_DEFAULT_MODEL"] = "   "
        assert config._model_from_env("NOVA_DEFAULT_MODEL", "d") == "d"
        os.environ.pop("NOVA_DEFAULT_MODEL", None)
        assert config._model_from_env("NOVA_DEFAULT_MODEL", "d") == "d"

    def test_defaults_are_backward_compatible(self):
        cfg = self._reload_clean()
        assert cfg.MODELS == _DEFAULT_MODELS

    def test_env_overrides_every_role(self):
        self._reload_clean()
        os.environ["NOVA_ROUTER_MODEL"] = "r:1b"
        os.environ["NOVA_DEFAULT_MODEL"] = "d:1b"
        os.environ["NOVA_CODE_MODEL"] = "c:1b"
        os.environ["NOVA_ADVANCED_MODEL"] = "a:1b"
        cfg = importlib.reload(config)
        assert cfg.MODELS == {
            "router": "r:1b",
            "default": "d:1b",
            "code": "c:1b",
            "advanced": "a:1b",
        }

    def test_low_ram_profile_points_all_roles_at_one_small_model(self):
        self._reload_clean()
        for k in ("NOVA_ROUTER_MODEL", "NOVA_DEFAULT_MODEL",
                  "NOVA_CODE_MODEL", "NOVA_ADVANCED_MODEL"):
            os.environ[k] = "gemma3:1b"
        cfg = importlib.reload(config)
        assert set(cfg.MODELS.values()) == {"gemma3:1b"}

    def test_unset_role_keeps_its_default(self):
        self._reload_clean()
        os.environ["NOVA_CODE_MODEL"] = "my-coder:7b"
        cfg = importlib.reload(config)
        assert cfg.MODELS["code"] == "my-coder:7b"
        assert cfg.MODELS["default"] == "gemma4"  # untouched default
        assert cfg.MODELS["router"] == "gemma3:1b"

    def test_blank_override_falls_back_to_default(self):
        self._reload_clean()
        os.environ["NOVA_ADVANCED_MODEL"] = "   "
        cfg = importlib.reload(config)
        assert cfg.MODELS["advanced"] == "qwen2.5:32b"

    def test_operator_install_switches_default_off(self):
        cfg = self._reload_clean()
        assert cfg.NOVA_AUTO_PULL_MODELS is False
        assert cfg.NOVA_AUTO_UPDATE_MODELS is False
        assert cfg.NOVA_BOOTSTRAP_MODELS == ()

    def test_bootstrap_models_parse_from_csv(self):
        self._reload_clean()
        os.environ["NOVA_BOOTSTRAP_MODELS"] = " gemma3:1b , tiny:1b ,, "
        cfg = importlib.reload(config)
        assert cfg.NOVA_BOOTSTRAP_MODELS == ("gemma3:1b", "tiny:1b")


# ── Ollama error classification ─────────────────────────────────────────────


class _FakeResponseError(Exception):
    """Stand-in for ``ollama.ResponseError`` — carries ``error`` / status."""

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.error = message
        if status_code is not None:
            self.status_code = status_code


class TestOllamaErrorClassification:
    def test_404_is_model_missing(self):
        exc = _FakeResponseError("boom", status_code=404)
        assert ollama_provider._classify_response_error(exc) == ERROR_MODEL_MISSING

    def test_not_found_message_is_model_missing(self):
        exc = _FakeResponseError("model 'gemma9' not found, try pulling it first")
        assert ollama_provider._classify_response_error(exc) == ERROR_MODEL_MISSING

    @pytest.mark.parametrize(
        "message",
        [
            "llama runner process has terminated: signal: killed",
            "model runner has unexpectedly stopped",
            "failed to load model: out of memory",
            "cannot allocate memory",
        ],
    )
    def test_runtime_markers_are_model_runtime(self, message):
        exc = _FakeResponseError(message, status_code=500)
        assert ollama_provider._classify_response_error(exc) == ERROR_MODEL_RUNTIME

    def test_other_error_is_backend(self):
        exc = _FakeResponseError("some unexpected backend hiccup", status_code=500)
        assert ollama_provider._classify_response_error(exc) == ERROR_BACKEND

    def test_generate_missing_model_carries_kind_and_model(self):
        err = _FakeResponseError("model 'foo:1b' not found", status_code=404)
        client = MagicMock(chat=MagicMock(side_effect=err))
        with patch.object(ollama, "ResponseError", _FakeResponseError):
            provider = OllamaProvider(client=client)
            with pytest.raises(ModelProviderError) as ei:
                provider.generate(ModelRequest(model="foo:1b", messages=[]))
        assert ei.value.kind == ERROR_MODEL_MISSING
        assert ei.value.model == "foo:1b"

    def test_generate_runtime_error_carries_kind_and_model(self):
        err = _FakeResponseError("llama runner terminated: signal: killed", status_code=500)
        client = MagicMock(chat=MagicMock(side_effect=err))
        with patch.object(ollama, "ResponseError", _FakeResponseError):
            provider = OllamaProvider(client=client)
            with pytest.raises(ModelProviderError) as ei:
                provider.generate(ModelRequest(model="qwen2.5:32b", messages=[]))
        assert ei.value.kind == ERROR_MODEL_RUNTIME
        assert ei.value.model == "qwen2.5:32b"

    def test_transport_error_is_unreachable(self):
        client = MagicMock(chat=MagicMock(side_effect=ConnectionError("down")))
        provider = OllamaProvider(client=client)
        with pytest.raises(ModelProviderError) as ei:
            provider.generate(ModelRequest(model="gemma4", messages=[]))
        assert ei.value.kind == ERROR_UNREACHABLE
        assert ei.value.model == "gemma4"


# ── Distinct, honest chat error messages ────────────────────────────────────


class TestChatErrorMessages:
    def test_missing_model_message_names_the_model(self):
        reply = chat_module._provider_error_reply(
            ModelProviderError("x", kind=ERROR_MODEL_MISSING, model="gemma9:1b")
        )
        assert reply != chat_module.OLLAMA_UNAVAILABLE
        assert "gemma9:1b" in reply
        assert "not installed" in reply.lower()
        # Points the operator at the configurable knobs, not a silent pull.
        assert "NOVA_" in reply

    def test_missing_model_message_without_name_is_still_useful(self):
        reply = chat_module._provider_error_reply(
            ModelProviderError("x", kind=ERROR_MODEL_MISSING, model=None)
        )
        assert reply != chat_module.OLLAMA_UNAVAILABLE
        assert "not installed" in reply.lower()

    def test_runtime_message_is_runtime_not_unreachable(self):
        reply = chat_module._provider_error_reply(
            ModelProviderError("x", kind=ERROR_MODEL_RUNTIME, model="qwen2.5:32b")
        )
        assert reply != chat_module.OLLAMA_UNAVAILABLE
        assert "qwen2.5:32b" in reply
        # A crash/OOM must never be mislabelled as "unreachable".
        assert "unreachable" not in reply.lower()

    def test_unreachable_and_backend_keep_unreachable_reply(self):
        # Backward compatible: a real transport failure and an unclassified
        # backend error both keep the calm existing message.
        assert (
            chat_module._provider_error_reply(
                ModelProviderError("x", kind=ERROR_UNREACHABLE)
            )
            == chat_module.OLLAMA_UNAVAILABLE
        )
        assert (
            chat_module._provider_error_reply(ModelProviderError("plain"))
            == chat_module.OLLAMA_UNAVAILABLE
        )


# ── Read-only model status ──────────────────────────────────────────────────


class TestModelStatus:
    _FAKE_MAP = {
        "router": "gemma3:1b",
        "default": "gemma4",
        "code": "missing-coder",
        "advanced": "gemma4",
    }

    def _patch_health(self, ok, models):
        return patch(
            "core.provider_status.probe_provider_health",
            return_value={
                "ok": ok,
                "provider": "ollama",
                "detail": "" if ok else "unreachable",
                "models": models,
            },
        )

    def test_reachable_reports_installed_and_missing(self, monkeypatch):
        monkeypatch.setattr(config, "MODELS", dict(self._FAKE_MAP))
        with self._patch_health(True, ["gemma3:1b", "gemma4"]):
            status = model_status.get_model_status()
        assert status["reachable"] is True
        assert status["provider"] == "ollama"
        assert status["model_map"] == self._FAKE_MAP
        assert set(status["model_map"]) == {"router", "default", "code", "advanced"}
        # Distinct configured names; the one absent model is reported missing.
        assert status["configured_models"] == ["gemma3:1b", "gemma4", "missing-coder"]
        assert status["installed_models"] == ["gemma3:1b", "gemma4"]
        assert status["missing_models"] == ["missing-coder"]

    def test_unreachable_leaves_missing_empty(self, monkeypatch):
        monkeypatch.setattr(config, "MODELS", dict(self._FAKE_MAP))
        with self._patch_health(False, []):
            status = model_status.get_model_status()
        assert status["reachable"] is False
        # Can't know what's installed when the backend is down — don't guess.
        assert status["missing_models"] == []
        assert status["installed_models"] == []

    def test_reports_operator_install_switches(self, monkeypatch):
        monkeypatch.setattr(config, "MODELS", dict(self._FAKE_MAP))
        monkeypatch.setattr(config, "NOVA_AUTO_PULL_MODELS", True)
        monkeypatch.setattr(config, "NOVA_AUTO_UPDATE_MODELS", False)
        monkeypatch.setattr(config, "NOVA_BOOTSTRAP_MODELS", ("gemma3:1b",))
        with self._patch_health(True, ["gemma3:1b"]):
            status = model_status.get_model_status()
        assert status["auto_pull_models"] is True
        assert status["auto_update_models"] is False
        assert status["bootstrap_models"] == ["gemma3:1b"]

    def test_never_raises_when_probe_is_unusual(self, monkeypatch):
        monkeypatch.setattr(config, "MODELS", dict(self._FAKE_MAP))
        # A malformed probe payload must still degrade calmly.
        with patch(
            "core.provider_status.probe_provider_health",
            return_value={"ok": False, "provider": None, "detail": None, "models": None},
        ):
            status = model_status.get_model_status()
        assert status["reachable"] is False
        assert status["installed_models"] == []
        assert status["missing_models"] == []


# ── Opt-in, background model bootstrap ───────────────────────────────────────


class TestModelBootstrap:
    def test_disabled_by_default_is_a_no_op(self, monkeypatch):
        monkeypatch.setattr(config, "NOVA_AUTO_PULL_MODELS", False)
        monkeypatch.setattr(config, "NOVA_BOOTSTRAP_MODELS", ("gemma3:1b",))
        with patch("core.model_pulls.request_pull") as req, \
             patch.object(model_bootstrap.threading, "Thread") as thread:
            started = model_bootstrap.maybe_bootstrap_models()
        assert started is False
        req.assert_not_called()
        thread.assert_not_called()

    def test_enabled_but_empty_list_is_a_no_op(self, monkeypatch):
        monkeypatch.setattr(config, "NOVA_AUTO_PULL_MODELS", True)
        monkeypatch.setattr(config, "NOVA_BOOTSTRAP_MODELS", ())
        with patch.object(model_bootstrap.threading, "Thread") as thread:
            started = model_bootstrap.maybe_bootstrap_models()
        assert started is False
        thread.assert_not_called()

    def test_enabled_starts_background_thread(self, monkeypatch):
        monkeypatch.setattr(config, "NOVA_AUTO_PULL_MODELS", True)
        monkeypatch.setattr(config, "NOVA_BOOTSTRAP_MODELS", ("gemma3:1b", "tiny:1b"))
        with patch.object(model_bootstrap.threading, "Thread") as Thread:
            started = model_bootstrap.maybe_bootstrap_models()
        assert started is True
        # Non-blocking: dispatched on a daemon thread, not run inline.
        Thread.assert_called_once()
        kwargs = Thread.call_args.kwargs
        assert kwargs["daemon"] is True
        assert kwargs["target"] is model_bootstrap._bootstrap_worker
        Thread.return_value.start.assert_called_once()

    def test_worker_requests_pull_for_each_model(self):
        with patch("core.model_pulls.request_pull") as req:
            model_bootstrap._bootstrap_worker(("a:1b", "b:1b"))
        assert [c.args[0] for c in req.call_args_list] == ["a:1b", "b:1b"]

    def test_worker_swallows_all_errors(self):
        from core import model_pulls

        side_effects = [
            model_pulls.ModelAlreadyInstalled("a:1b"),
            model_pulls.InvalidModelName("bad name"),
            RuntimeError("unexpected"),
        ]
        with patch("core.model_pulls.request_pull", side_effect=side_effects):
            # Must not raise despite every call failing differently.
            model_bootstrap._bootstrap_worker(("a:1b", "b:1b", "c:1b"))


# ── Product docs: model flexibility + assistant framing ─────────────────────


def _read(path: str) -> str:
    return (REPO / path).read_text(encoding="utf-8")


class TestModelDocs:
    def test_docker_low_ram_profile_mentions_lightweight_config(self):
        text = _read("docs/docker.md").lower()
        assert "low-ram profile" in text
        # The exact hardware the brief calls out.
        assert "ryzen 5 3600" in text
        assert "16 gb" in text
        assert "cpu-only" in text
        # The lightweight config it recommends, verbatim.
        for line in (
            "nova_router_model=gemma3:1b",
            "nova_default_model=gemma3:1b",
            "nova_code_model=gemma3:1b",
            "nova_advanced_model=gemma3:1b",
        ):
            assert line in text, line
        # Explains which large models it avoids loading on weak hosts.
        for big in ("gemma4", "deepseek-coder-v2", "qwen2.5:32b"):
            assert big in text, big

    def test_env_example_documents_model_map_and_switches(self):
        text = _read(".env.example")
        for var in (
            "NOVA_ROUTER_MODEL",
            "NOVA_DEFAULT_MODEL",
            "NOVA_CODE_MODEL",
            "NOVA_ADVANCED_MODEL",
            "NOVA_AUTO_UPDATE_MODELS",
            "NOVA_AUTO_PULL_MODELS",
            "NOVA_BOOTSTRAP_MODELS",
        ):
            assert var in text, var

    def test_readme_documents_operator_controlled_models(self):
        text = _read("README.md")
        low = text.lower()
        assert "configuring models" in low
        assert "operator-controlled" in low
        for var in ("NOVA_ROUTER_MODEL", "NOVA_DEFAULT_MODEL",
                    "NOVA_CODE_MODEL", "NOVA_ADVANCED_MODEL"):
            assert var in text, var
        # Read-only status surface is discoverable.
        assert "/admin/models/status" in text
        # Assistant, not an autonomous agent.
        assert "not an autonomous agent" in low

    def test_model_providers_doc_covers_operator_control_and_errors(self):
        low = _read("docs/model-providers.md").lower()
        assert "operator-controlled" in low
        assert "model_missing" in low
        assert "model_runtime" in low
        assert "/admin/models/status" in low

    def test_safety_contract_adds_operator_and_agent_boundaries(self):
        low = _read("docs/nova-safety-and-trust-contract.md").lower()
        assert "operator-controlled" in low
        assert "not an autonomous agent" in low
        assert "execute model-generated shell commands" in low

    def test_product_docs_do_not_market_nova_as_a_girlfriend(self):
        # Marketing phrases that could only appear if Nova were being
        # *positioned* as a girlfriend/partner — none can occur inside a
        # disclaimer like "not an 'AI girlfriend'", so they are safe to
        # forbid outright across the operator-facing docs.
        forbidden = (
            "girlfriend mode",
            "gf mode",
            "boyfriend mode",
            "your ai girlfriend",
            "your virtual girlfriend",
            "your ai boyfriend",
            "ai girlfriend experience",
            "virtual girlfriend experience",
            "dating simulator",
        )
        for path in (
            "README.md",
            "INSTALL.md",
            "docs/docker.md",
            "docs/model-providers.md",
        ):
            low = _read(path).lower()
            for phrase in forbidden:
                assert phrase not in low, f"{path} contains {phrase!r}"

    def test_readme_keeps_assistant_framing(self):
        # Safe assistant framing remains present alongside the model
        # flexibility additions (guards against a reframe stripping it).
        low = _read("README.md").lower()
        assert "local-first" in low
        assert "ai assistant" in low
        assert "not a companion product" in low
