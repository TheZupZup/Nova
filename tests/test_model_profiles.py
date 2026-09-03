"""Model profiles — a model is a profile, not just a name.

Pins the contract that makes Nova a platform for *specialising* local
models rather than swapping interchangeable strings:

  * inference is deterministic, offline, and never raises;
  * the four ``NOVA_*_MODEL`` env vars keep working untouched, and the
    admin-selected default still wins for the chat role;
  * an operator overlay can describe a model Nova has never heard of —
    and a malformed overlay degrades to "no override", never an error;
  * a future ``nova-coder`` derivative already resolves to a
    coding-specialised, large-context profile with no code change;
  * a profile is a *description*, never a permission.
"""

from __future__ import annotations

import importlib
import json

import pytest

from core import model_profiles as mp


class TestInference:
    def test_known_code_family_is_code_specialized(self):
        profile = mp.profile_for("deepseek-coder-v2", mp.ROLE_CODE)
        assert profile.code_specialized is True
        assert profile.source == mp.SOURCE_BUILTIN
        assert profile.context_size >= 8192

    def test_unknown_family_degrades_to_conservative_defaults(self):
        profile = mp.profile_for("some-private-model", mp.ROLE_CHAT)
        assert profile.source == mp.SOURCE_UNKNOWN
        assert profile.supports_tools is False
        assert profile.context_size == 8192

    def test_longest_family_prefix_wins(self):
        coder = mp.profile_for("qwen2.5-coder:14b", mp.ROLE_CODE)
        plain = mp.profile_for("qwen2.5:32b", mp.ROLE_ADVANCED)
        assert coder.code_specialized is True
        assert plain.code_specialized is False

    @pytest.mark.parametrize("name,expected", [
        ("gemma3:1b", 1.0),
        ("qwen2.5:32b", 32.0),
        ("model-1.5b-instruct", 1.5),
        ("deepseek-coder-v2", None),
    ])
    def test_parameter_size_parsing(self, name, expected):
        assert mp.parse_parameter_size(name) == expected

    @pytest.mark.parametrize("size,expected", [
        (None, mp.CLASS_UNKNOWN),
        (1.0, mp.CLASS_TINY),
        (3.0, mp.CLASS_SMALL),
        (14.0, mp.CLASS_MEDIUM),
        (32.0, mp.CLASS_LARGE),
        (70.0, mp.CLASS_XLARGE),
    ])
    def test_resource_classes(self, size, expected):
        assert mp.resource_class_for(size) == expected

    def test_quantization_is_parsed_when_present(self):
        assert mp.profile_for("llama3:8b-instruct-q4_K_M").quantization == "q4"
        assert mp.profile_for("llama3:8b").quantization == ""

    def test_router_role_never_gets_a_huge_context(self):
        # A big family in the router seat is still a one-word classifier.
        profile = mp.profile_for("qwen2.5:32b", mp.ROLE_ROUTER)
        assert profile.context_size <= 2048

    def test_registry_prefix_is_stripped(self):
        profile = mp.profile_for("hf.co/someone/qwen2.5-coder:7b", mp.ROLE_CODE)
        assert profile.code_specialized is True

    def test_blank_model_yields_a_named_unknown_profile(self):
        profile = mp.profile_for("", mp.ROLE_CODE)
        assert profile.name == ""
        assert profile.source == mp.SOURCE_UNKNOWN
        assert "No model is configured" in profile.notes

    def test_oversized_name_is_refused_not_raised(self):
        assert mp.normalize_model_name("x" * 5000) == ""
        assert mp.profile_for("x" * 5000).name == ""

    def test_non_string_model_never_raises(self):
        for value in (None, 5, [], {}, True):
            assert mp.profile_for(value).name == ""


class TestNovaCoder:
    """The future fine-tuned derivative plugs in with no code change."""

    def test_nova_coder_resolves_to_a_coding_profile(self):
        profile = mp.profile_for("nova-coder:14b", mp.ROLE_CODE)
        assert profile.code_specialized is True
        assert profile.supports_tools is True
        assert profile.context_size >= 16384
        assert profile.resource_class == mp.CLASS_MEDIUM
        assert profile.parameter_size_b == 14.0

    def test_nova_code_model_env_var_selects_it(self, monkeypatch):
        monkeypatch.setenv("NOVA_CODE_MODEL", "nova-coder:14b")
        import config

        importlib.reload(config)
        try:
            assert config.MODELS["code"] == "nova-coder:14b"
            models = mp.configured_role_models()
            assert models[mp.ROLE_CODE] == "nova-coder:14b"
        finally:
            monkeypatch.delenv("NOVA_CODE_MODEL", raising=False)
            importlib.reload(config)


class TestOverrides:
    def test_override_file_wins_over_inference(self, tmp_path, monkeypatch):
        path = tmp_path / "model-profiles.json"
        path.write_text(json.dumps({"profiles": [{
            "name": "private-model:8b",
            "role": "code",
            "context_size": 65536,
            "supports_tools": True,
            "code_specialized": True,
            "resource_class": "medium",
            "notes": "internal build",
        }]}), encoding="utf-8")
        monkeypatch.setenv(mp.ENV_PROFILES_PATH, str(path))

        profile = mp.profile_for("private-model:8b", mp.ROLE_CODE)
        assert profile.source == mp.SOURCE_OVERRIDE
        assert profile.context_size == 65536
        assert profile.code_specialized is True
        assert profile.notes == "internal build"

    def test_override_accepts_a_bare_list(self, tmp_path, monkeypatch):
        path = tmp_path / "profiles.json"
        path.write_text(
            json.dumps([{"name": "x:1b", "notes": "n"}]), encoding="utf-8"
        )
        monkeypatch.setenv(mp.ENV_PROFILES_PATH, str(path))
        assert mp.load_overrides()["x:1b"].notes == "n"

    def test_missing_override_file_is_normal(self, tmp_path, monkeypatch):
        monkeypatch.setenv(mp.ENV_PROFILES_PATH, str(tmp_path / "absent.json"))
        assert mp.load_overrides() == {}
        assert mp.describe_profiles()["overrides_present"] is False

    def test_malformed_override_degrades_to_no_override(self, tmp_path, monkeypatch):
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        monkeypatch.setenv(mp.ENV_PROFILES_PATH, str(path))
        assert mp.load_overrides() == {}
        # Inference still answers.
        assert mp.profile_for("gemma3:1b", mp.ROLE_ROUTER).name == "gemma3:1b"

    def test_override_entries_without_a_name_are_skipped(self, tmp_path, monkeypatch):
        path = tmp_path / "p.json"
        path.write_text(json.dumps([
            {"role": "code"},
            {"name": "", "role": "code"},
            {"name": "ok:1b"},
        ]), encoding="utf-8")
        monkeypatch.setenv(mp.ENV_PROFILES_PATH, str(path))
        assert list(mp.load_overrides()) == ["ok:1b"]

    def test_oversized_override_file_is_ignored(self, tmp_path, monkeypatch):
        path = tmp_path / "huge.json"
        path.write_text(
            json.dumps([{"name": "a:1b", "notes": "x" * 400_000}]),
            encoding="utf-8",
        )
        monkeypatch.setenv(mp.ENV_PROFILES_PATH, str(path))
        assert mp.load_overrides() == {}

    def test_override_context_size_is_clamped(self, tmp_path, monkeypatch):
        path = tmp_path / "p.json"
        path.write_text(json.dumps([
            {"name": "a:1b", "context_size": -5},
            {"name": "b:1b", "context_size": 10 ** 12},
        ]), encoding="utf-8")
        monkeypatch.setenv(mp.ENV_PROFILES_PATH, str(path))
        overrides = mp.load_overrides()
        assert overrides["a:1b"].context_size >= 512
        assert overrides["b:1b"].context_size <= 1_048_576


class TestRoleResolution:
    def test_every_role_resolves_to_a_profile(self):
        profiles = mp.role_profiles()
        assert set(profiles) == set(mp.ROLES)
        for role, profile in profiles.items():
            assert profile.role == role

    def test_config_keys_preserve_the_existing_env_contract(self):
        assert mp.ROLE_CONFIG_KEYS[mp.ROLE_CHAT] == "default"
        assert mp.ROLE_ENV_VARS == {
            "router": "NOVA_ROUTER_MODEL",
            "chat": "NOVA_DEFAULT_MODEL",
            "code": "NOVA_CODE_MODEL",
            "advanced": "NOVA_ADVANCED_MODEL",
        }

    def test_admin_selected_default_wins_for_the_chat_role(self, monkeypatch):
        monkeypatch.setattr(
            "core.model_settings.resolve_default_model",
            lambda: "admin-picked:7b",
        )
        assert mp.configured_role_models()[mp.ROLE_CHAT] == "admin-picked:7b"

    def test_a_failing_settings_read_falls_back_to_config(self, monkeypatch):
        def boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(
            "core.model_settings.resolve_default_model", boom
        )
        from config import MODELS

        assert mp.configured_role_models()[mp.ROLE_CHAT] == MODELS["default"]

    def test_describe_profiles_is_json_serialisable(self):
        payload = mp.describe_profiles()
        json.dumps(payload)
        assert [r["role"] for r in payload["roles"]] == list(mp.ROLES)
        assert payload["resource_classes"]


class TestProfilesGrantNothing:
    def test_profile_fields_are_descriptive_only(self):
        """A profile has no field that could enable an action."""
        fields = set(mp.ModelProfile("x").as_dict())
        forbidden = {
            "allow_shell", "allow_exec", "tools", "enabled_tools",
            "permissions", "capabilities", "auto_pull", "download",
        }
        assert not (fields & forbidden)

    def test_module_never_reaches_a_backend(self):
        source = (
            importlib.import_module("core.model_profiles").__file__
        )
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        for banned in ("httpx", "subprocess", "requests", "ollama.Client"):
            assert banned not in text
