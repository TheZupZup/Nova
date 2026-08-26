"""Local model runtime health — installed, loaded, context, honest errors.

Pins the operational visibility contract:

  * installed vs loaded vs not-installed are distinguished, and an
    unreachable backend yields ``unknown`` rather than a false "missing";
  * context size comes from the runtime when it says one, and is ``None``
    when it does not — never invented;
  * hardware detection degrades gracefully and names no GPU vendor;
  * nothing here downloads, loads, or generates.
"""

from __future__ import annotations

import json

import pytest

from core import model_health as mh
from core import ollama_client


class _Probe:
    """Stand-in for ``probe_provider_health``."""

    def __init__(self, ok=True, models=None, provider="ollama", detail=""):
        self.payload = {
            "ok": ok, "provider": provider, "detail": detail,
            "models": models or [],
        }

    def __call__(self, name=None):
        return dict(self.payload)


@pytest.fixture
def stub_backend(monkeypatch):
    def _apply(*, ok=True, installed=(), loaded=(), provider="ollama",
               detail="", ps_raises=False, show=None):
        monkeypatch.setattr(
            "core.provider_status.probe_provider_health",
            _Probe(ok=ok, models=list(installed), provider=provider,
                   detail=detail),
        )

        class _P:
            name = provider

        monkeypatch.setattr(
            "core.model_providers.get_provider", lambda: _P()
        )

        def _ps():
            if ps_raises:
                raise ollama_client.OllamaUnavailable("down")
            return [dict(row) for row in loaded]

        monkeypatch.setattr(ollama_client, "list_running_models", _ps)
        monkeypatch.setattr(
            ollama_client, "show_model", lambda name, host=None: show or {}
        )
        monkeypatch.setattr(
            "core.model_profiles.configured_role_models",
            lambda: {
                "router": "gemma3:1b", "chat": "gemma4",
                "code": "deepseek-coder-v2", "advanced": "qwen2.5:32b",
            },
        )
    return _apply


def _role(health, name):
    return next(r for r in health["roles"] if r["role"] == name)


class TestStates:
    def test_installed_but_not_loaded(self, stub_backend):
        stub_backend(installed=["deepseek-coder-v2:latest"])
        role = _role(mh.get_model_health(), "code")
        assert role["state"] == mh.STATE_INSTALLED
        assert role["loaded"] is False
        assert "cold start" in role["hint"]

    def test_loaded_model_is_reported_as_loaded(self, stub_backend):
        stub_backend(
            installed=["deepseek-coder-v2:latest"],
            loaded=[{"name": "deepseek-coder-v2:latest"}],
        )
        role = _role(mh.get_model_health(), "code")
        assert role["state"] == mh.STATE_LOADED
        assert role["loaded"] is True
        assert role["hint"] == ""

    def test_missing_model_names_the_manual_fix(self, stub_backend):
        stub_backend(installed=["gemma3:1b"])
        role = _role(mh.get_model_health(), "code")
        assert role["state"] == mh.STATE_NOT_INSTALLED
        assert "ollama pull deepseek-coder-v2" in role["hint"]
        assert "NOVA_CODE_MODEL" in role["hint"]
        assert "never downloads it for you" in role["hint"]

    def test_unreachable_backend_never_claims_a_model_is_missing(
        self, stub_backend
    ):
        stub_backend(ok=False, detail="connection refused")
        health = mh.get_model_health()
        assert health["reachable"] is False
        assert mh.ERROR_BACKEND_UNREACHABLE in health["errors"]
        for role in health["roles"]:
            assert role["state"] == mh.STATE_UNKNOWN
            assert "unreachable" in role["hint"]

    def test_tag_tolerant_matching(self, stub_backend):
        stub_backend(installed=["gemma4:latest"], loaded=[{"name": "gemma4:latest"}])
        assert _role(mh.get_model_health(), "chat")["state"] == mh.STATE_LOADED

    def test_unconfigured_role_points_at_its_env_var(self, stub_backend, monkeypatch):
        stub_backend(installed=["gemma3:1b"])
        monkeypatch.setattr(
            "core.model_profiles.configured_role_models",
            lambda: {"router": "gemma3:1b", "chat": "", "code": "", "advanced": ""},
        )
        role = _role(mh.get_model_health(), "code")
        assert role["state"] == mh.STATE_UNKNOWN
        assert "NOVA_CODE_MODEL" in role["hint"]


class TestContextSizes:
    def test_runtime_context_size_is_used_when_reported(self, stub_backend):
        stub_backend(
            installed=["gemma4"],
            loaded=[{"name": "gemma4", "context_size": 4096}],
        )
        assert _role(mh.get_model_health(), "chat")["runtime_context_size"] == 4096

    def test_show_supplies_the_context_length_when_ps_does_not(self, stub_backend):
        stub_backend(
            installed=["gemma4"],
            loaded=[{"name": "gemma4"}],
            show={"model_info": {"gemma3.context_length": 8192}},
        )
        assert _role(mh.get_model_health(), "chat")["runtime_context_size"] == 8192

    def test_num_ctx_parameter_is_understood(self, stub_backend):
        stub_backend(
            installed=["gemma4"], loaded=[{"name": "gemma4"}],
            show={"parameters": "stop <eos>\nnum_ctx 2048"},
        )
        assert _role(mh.get_model_health(), "chat")["runtime_context_size"] == 2048

    def test_unknown_context_is_none_not_invented(self, stub_backend):
        stub_backend(installed=["gemma4"], loaded=[{"name": "gemma4"}], show={})
        role = _role(mh.get_model_health(), "chat")
        assert role["runtime_context_size"] is None
        # Nova's own recommendation is still reported, clearly separate.
        assert role["profile_context_size"] > 0

    def test_context_lookup_can_be_skipped(self, stub_backend):
        stub_backend(installed=["gemma4"], loaded=[{"name": "gemma4"}])
        health = mh.get_model_health(include_context_sizes=False)
        assert _role(health, "chat")["runtime_context_size"] is None


class TestGracefulDegradation:
    def test_ps_failure_is_reported_not_raised(self, stub_backend):
        stub_backend(installed=["gemma4"], ps_raises=True)
        health = mh.get_model_health()
        assert mh.ERROR_RUNTIME_DETAILS_UNAVAILABLE in health["errors"]
        assert health["runtime_details_available"] is False
        # Installed state is still known — only the loaded view is lost.
        assert _role(health, "chat")["state"] == mh.STATE_INSTALLED

    def test_non_ollama_provider_reports_unsupported(self, stub_backend):
        stub_backend(installed=["gemma4"], provider="llamacpp")
        health = mh.get_model_health()
        assert mh.ERROR_RUNTIME_DETAILS_UNSUPPORTED in health["errors"]
        assert health["loaded_models"] == []

    def test_no_vendor_sdk_or_probe_is_used(self):
        """Hardware facts come from the runtime, never a vendor tool."""
        with open(mh.__file__, encoding="utf-8") as handle:
            source = handle.read()
        assert "subprocess" not in source
        for token in ("import pynvml", "import torch", "pyamdgpuinfo",
                      "GPUtil", "nvidia_smi", "nvmlInit"):
            assert token not in source
        # Vendor names appear only in the module docstring, explaining
        # what Nova deliberately does *not* do.
        body = source.split('"""', 2)[-1].lower()
        for vendor in ("nvidia", "cuda", "rocm", "metal"):
            assert vendor not in body

    def test_accelerator_bytes_pass_through_only_when_reported(
        self, stub_backend
    ):
        stub_backend(
            installed=["gemma4"],
            loaded=[{"name": "gemma4", "size": 100, "size_vram": 80}],
        )
        row = mh.get_model_health()["loaded_models"][0]
        assert row["size_vram"] == 80
        stub_backend(installed=["gemma4"], loaded=[{"name": "gemma4"}])
        assert "size_vram" not in mh.get_model_health()["loaded_models"][0]

    def test_snapshot_is_json_serialisable(self, stub_backend):
        stub_backend(installed=["gemma4"], loaded=[{"name": "gemma4"}])
        json.dumps(mh.get_model_health())


class TestProfileFieldsAreCarried:
    """One fetch renders the whole operator card — no second round trip."""

    def test_role_rows_carry_the_profile_traits(self, stub_backend):
        stub_backend(installed=["deepseek-coder-v2"])
        role = _role(mh.get_model_health(), "code")
        assert role["code_specialized"] is True
        assert role["supports_tools"] is False
        assert role["profile_source"] in ("builtin", "override", "unknown")
        assert role["resource_class"] in (
            "tiny", "small", "medium", "large", "xlarge", "unknown"
        )

    def test_unconfigured_role_still_carries_the_fields(
        self, stub_backend, monkeypatch
    ):
        stub_backend(installed=[])
        monkeypatch.setattr(
            "core.model_profiles.configured_role_models",
            lambda: {"router": "", "chat": "", "code": "", "advanced": ""},
        )
        for role in mh.get_model_health()["roles"]:
            assert "supports_tools" in role
            assert "profile_source" in role

    def test_profile_traits_are_never_permissions(self, stub_backend):
        """supports_tools is a claim about a model, not a grant."""
        stub_backend(installed=["qwen2.5-coder:7b"])
        role = _role(mh.get_model_health(), "code")
        forbidden = {
            "allow_shell", "allow_exec", "permissions", "capabilities",
            "auto_pull", "can_write", "tools_enabled",
        }
        assert not (set(role) & forbidden)


class TestOllamaReadHelpers:
    def test_running_models_parses_a_ps_payload(self, monkeypatch):
        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"models": [
                    {"name": "a:1b", "size": 10, "size_vram": 5,
                     "details": {"parameter_size": "1B",
                                 "quantization_level": "Q4_0"}},
                    {"no_name": True},
                    "junk",
                ]}

        monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp())
        rows = ollama_client.list_running_models()
        assert len(rows) == 1
        assert rows[0]["parameter_size"] == "1B"

    def test_unreachable_ps_raises_ollama_unavailable(self, monkeypatch):
        import httpx

        def _boom(*a, **k):
            raise httpx.ConnectError("nope")

        monkeypatch.setattr("httpx.get", _boom)
        with pytest.raises(ollama_client.OllamaUnavailable):
            ollama_client.list_running_models()

    def test_show_requires_a_model_name(self):
        with pytest.raises(ollama_client.OllamaUnavailable):
            ollama_client.show_model("  ")

    def test_read_helpers_never_call_a_pull_endpoint(self):
        with open(ollama_client.__file__, encoding="utf-8") as handle:
            source = handle.read()
        for token in ("/api/pull", "client.pull", ".pull(", "api/create",
                      "api/delete", "api/copy", "api/push"):
            assert token not in source
