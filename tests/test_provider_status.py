"""Tests for the read-only model-provider status reporter.

Pinned contracts (see ``core/provider_status.py``):

* the reporter is read-only and never raises into the caller — an
  unknown configured provider is an ``error`` string, not an
  exception;
* Ollama is the default and existing (unset) deployments report it
  with ``is_default=True`` and no warnings;
* MockProvider stays test-only: ``mock`` is never in
  ``selectable_providers``, but if it is *configured* the status
  reports it truthfully with a clear warning;
* a non-default but registered provider is reported calmly with a
  "not the default" warning, never an error;
* the Ollama host is surfaced with any ``user:pass@`` userinfo
  redacted;
* ``probe_provider_health`` delegates to the provider's ``health()``
  and always returns the stable ``{ok, provider, detail, models}``
  shape, even for an unknown provider or a provider that breaks the
  "health never raises" contract;
* every response field is JSON-serialisable.

The tests never assume a reachable Ollama daemon — the live-probe
tests drive a registry override / MockProvider.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock

import pytest

for _mod in ("ddgs", "ollama", "sgmllib", "feedparser"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from core import provider_status as ps  # noqa: E402
from core.model_providers import (  # noqa: E402
    MockProvider,
    register_provider,
    reset,
)
from core.model_providers import registry as registry_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_registry():
    """No override / cached instance / extra factory leaks between tests."""
    reset()
    yield
    reset()


# ── _redact_userinfo ────────────────────────────────────────────────


class TestRedactUserinfo:
    def test_plain_host_is_unchanged(self):
        assert (
            ps._redact_userinfo("http://localhost:11434")
            == "http://localhost:11434"
        )

    def test_empty_is_empty(self):
        assert ps._redact_userinfo("") == ""

    def test_userinfo_is_stripped(self):
        out = ps._redact_userinfo("http://user:secret@ollama.internal:11434")
        assert "secret" not in out
        assert "user" not in out
        assert "ollama.internal" in out
        assert "11434" in out

    def test_unparseable_value_with_at_is_masked(self):
        # A bizarre value still must not echo a potential secret back.
        out = ps._redact_userinfo("http://u:p@[bad")
        assert "p@" not in out


# ── get_provider_status: default (Ollama) ───────────────────────────


class TestStatusDefault:
    def test_unset_defaults_to_ollama_no_warnings(self, monkeypatch):
        monkeypatch.setattr("config.MODEL_PROVIDER", "ollama")
        monkeypatch.setattr("config.OLLAMA_HOST", "http://localhost:11434")
        status = ps.get_provider_status()
        assert status.configured_provider == "ollama"
        assert status.default_provider == "ollama"
        assert status.is_default is True
        assert status.active_provider == "ollama"
        assert status.error == ""
        assert status.warnings == ()

    def test_blank_provider_falls_back_to_ollama(self, monkeypatch):
        monkeypatch.setattr("config.MODEL_PROVIDER", "   ")
        status = ps.get_provider_status()
        assert status.configured_provider == "ollama"
        assert status.is_default is True

    def test_mock_is_never_selectable_but_listed_as_test_only(
        self, monkeypatch,
    ):
        monkeypatch.setattr("config.MODEL_PROVIDER", "ollama")
        status = ps.get_provider_status()
        assert "mock" not in status.selectable_providers
        assert "ollama" in status.selectable_providers
        assert "mock" in status.test_only_providers

    def test_ollama_host_is_redacted(self, monkeypatch):
        monkeypatch.setattr("config.MODEL_PROVIDER", "ollama")
        monkeypatch.setattr(
            "config.OLLAMA_HOST", "http://bob:hunter2@10.0.0.5:11434",
        )
        status = ps.get_provider_status()
        assert "hunter2" not in status.ollama_host
        assert "10.0.0.5" in status.ollama_host

    def test_status_is_json_serialisable(self, monkeypatch):
        monkeypatch.setattr("config.MODEL_PROVIDER", "ollama")
        body = ps.get_provider_status().as_dict()
        parsed = json.loads(json.dumps(body))
        assert parsed["default_provider"] == "ollama"
        assert isinstance(parsed["selectable_providers"], list)
        assert isinstance(parsed["warnings"], list)
        assert isinstance(parsed["current_model"], str)
        assert isinstance(parsed["supports_streaming"], bool)


# ── current_model / supports_streaming ──────────────────────────────


class TestStatusModelAndStreaming:
    """The Phase-1 settings UI surfaces the default model and whether
    the backend can stream. Both are read-only, host-level, non-secret
    facts derived without touching the network."""

    def test_current_model_is_the_configured_default(self, monkeypatch):
        monkeypatch.setattr("config.MODEL_PROVIDER", "ollama")
        monkeypatch.setattr(
            "config.MODELS", {"default": "test-model-x", "router": "r"},
        )
        status = ps.get_provider_status()
        assert status.current_model == "test-model-x"

    def test_missing_default_model_is_empty_not_an_error(self, monkeypatch):
        # An absent "default" key must degrade to "" calmly — a model
        # name is informational, never a reason to fail the status read.
        monkeypatch.setattr("config.MODEL_PROVIDER", "ollama")
        monkeypatch.setattr("config.MODELS", {"router": "r"})
        status = ps.get_provider_status()
        assert status.current_model == ""
        assert status.error == ""

    def test_resolvable_provider_reports_streaming_true(self, monkeypatch):
        # Ollama (the default) resolves without network I/O and
        # implements ``stream`` by contract.
        monkeypatch.setattr("config.MODEL_PROVIDER", "ollama")
        status = ps.get_provider_status()
        assert status.active_provider == "ollama"
        assert status.supports_streaming is True

    def test_unknown_provider_does_not_claim_streaming(self, monkeypatch):
        monkeypatch.setattr("config.MODEL_PROVIDER", "does-not-exist")
        status = ps.get_provider_status()
        assert status.active_provider is None
        assert status.supports_streaming is False

    def test_mock_override_supports_streaming(self):
        from core.model_providers import set_override

        set_override(MockProvider(healthy=True))
        status = ps.get_provider_status()
        assert status.supports_streaming is True


# ── get_provider_status: test-only mock configured ──────────────────


class TestStatusMockConfigured:
    def test_configured_mock_is_reported_with_warning(self, monkeypatch):
        # The state is never hidden: if someone sets the test provider
        # in production the status must say so, loudly but calmly.
        monkeypatch.setattr("config.MODEL_PROVIDER", "mock")
        status = ps.get_provider_status()
        assert status.configured_provider == "mock"
        assert status.is_default is False
        assert status.active_provider == "mock"
        assert status.error == ""
        joined = " ".join(status.warnings)
        assert "test-only" in joined
        assert "NOVA_MODEL_PROVIDER=ollama" in joined


# ── get_provider_status: unknown provider ───────────────────────────


class TestStatusUnknownProvider:
    def test_unknown_provider_is_error_not_exception(self, monkeypatch):
        monkeypatch.setattr("config.MODEL_PROVIDER", "does-not-exist")
        status = ps.get_provider_status()
        assert status.active_provider is None
        assert status.error  # populated, non-empty
        assert status.is_default is False
        joined = " ".join(status.warnings)
        assert "not registered" in joined
        assert "NOVA_MODEL_PROVIDER=ollama" in joined


# ── get_provider_status: non-default registered provider ────────────


class _FutureProvider(MockProvider):
    """A registered, non-test, non-default provider for status tests."""

    name = "future"


class TestStatusNonDefaultProvider:
    def test_non_default_registered_provider_warns_calmly(self, monkeypatch):
        register_provider("future", _FutureProvider)
        try:
            monkeypatch.setattr("config.MODEL_PROVIDER", "future")
            status = ps.get_provider_status()
            assert status.configured_provider == "future"
            assert status.active_provider == "future"
            assert status.is_default is False
            assert status.error == ""
            assert "future" in status.selectable_providers
            joined = " ".join(status.warnings)
            assert "default" in joined
            assert "supported" in joined
        finally:
            registry_mod._factories.pop("future", None)
            registry_mod._instances.pop("future", None)


# ── probe_provider_health ───────────────────────────────────────────


class TestProbeProviderHealth:
    def test_healthy_override_reports_ok(self):
        from core.model_providers import set_override

        set_override(MockProvider(healthy=True))
        out = ps.probe_provider_health()
        assert out["ok"] is True
        assert out["provider"] == "mock"
        assert isinstance(out["models"], list)

    def test_unhealthy_override_reports_clean_failure(self):
        from core.model_providers import set_override

        set_override(MockProvider(healthy=False))
        out = ps.probe_provider_health()
        assert out["ok"] is False
        assert out["detail"]  # short, non-empty reason

    def test_unknown_provider_maps_to_clean_failure(self):
        out = ps.probe_provider_health("does-not-exist")
        assert out["ok"] is False
        assert out["provider"] == "does-not-exist"
        assert out["detail"]
        assert out["models"] == []

    def test_health_contract_violation_is_contained(self):
        # A misbehaving third-party provider that *raises* from
        # health() (against the base contract) must still not blow up
        # the endpoint.
        class _Rogue(MockProvider):
            name = "rogue"

            def health(self):
                raise RuntimeError("provider exploded")

        from core.model_providers import set_override

        set_override(_Rogue())
        out = ps.probe_provider_health()
        assert out["ok"] is False
        assert out["models"] == []

    def test_health_models_are_passed_through(self):
        from core.model_providers import set_override
        from core.model_providers import ProviderHealth

        class _WithModels(MockProvider):
            name = "withmodels"

            def health(self):
                return ProviderHealth(
                    ok=True,
                    provider=self.name,
                    models=["gemma4", "qwen2.5:32b"],
                )

        set_override(_WithModels())
        out = ps.probe_provider_health()
        assert out["ok"] is True
        assert set(out["models"]) == {"gemma4", "qwen2.5:32b"}


# ── never raises ────────────────────────────────────────────────────


class TestNeverRaises:
    def test_status_never_raises_even_if_registry_breaks(self, monkeypatch):
        monkeypatch.setattr("config.MODEL_PROVIDER", "ollama")

        def _boom():
            raise RuntimeError("registry on fire")

        monkeypatch.setattr(
            "core.model_providers.available_providers", _boom,
        )
        # Must not propagate — falls back to a sane default list.
        status = ps.get_provider_status()
        assert status.default_provider == "ollama"

    def test_probe_never_raises_on_unexpected_resolve_error(
        self, monkeypatch,
    ):
        def _boom(_name=None):
            raise ValueError("not a ModelProviderError")

        monkeypatch.setattr("core.model_providers.get_provider", _boom)
        out = ps.probe_provider_health()
        assert out["ok"] is False
        assert out["models"] == []
