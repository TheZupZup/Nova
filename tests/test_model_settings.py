"""Tests for the Phase-2 admin default-model selection core
(``core/model_settings.py``).

Pinned contracts:

* ``resolve_default_model`` falls back to ``config.MODELS["default"]``
  when nothing is persisted (existing installs behave exactly as
  before) and returns the persisted choice once one is set — without
  any network I/O and without ever raising;
* ``set_default_model`` only persists a model the *active* provider
  currently reports, and refuses (writing nothing) an empty / oversized
  string, a model the provider does not list, or an unreachable
  provider;
* the unreachable refusal is sanitised — no raw transport detail and no
  configured host leak into the message;
* ``list_available_models`` returns the stable JSON shape the admin UI
  renders, including the current vs. compiled-in default.

The provider is always driven by a registry override / MockProvider so
the suite never assumes a reachable Ollama daemon.
"""

from __future__ import annotations

import sqlite3
import sys
from unittest.mock import MagicMock

import pytest

for _mod in ("ddgs", "ollama", "sgmllib", "feedparser"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from config import MODELS  # noqa: E402
from core import memory as core_memory  # noqa: E402
from core import model_settings as ms  # noqa: E402
from core import settings as core_settings  # noqa: E402
from core.model_providers import (  # noqa: E402
    MockProvider,
    ModelProviderError,
    reset as reset_registry,
    set_override,
)
from memory import store as natural_store  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_registry():
    """A registry override / cached instance never leaks between tests."""
    reset_registry()
    yield
    reset_registry()


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """A fresh nova.db with every table / migration applied."""
    path = str(tmp_path / "nova.db")
    monkeypatch.setattr(core_memory, "DB_PATH", path)
    monkeypatch.setattr(natural_store, "DB_PATH", path)
    core_memory.initialize_db()
    return path


# ── resolve_default_model ───────────────────────────────────────────


class TestResolveDefaultModel:
    def test_unset_falls_back_to_config_default(self, db_path):
        # The default for every existing install: no row → config model.
        assert ms.resolve_default_model() == MODELS["default"]
        assert ms.config_default_model() == MODELS["default"]

    def test_persisted_value_is_returned(self, db_path):
        core_settings.save_system_setting(
            ms.DEFAULT_MODEL_SETTING_KEY, "llama3:8b"
        )
        assert ms.resolve_default_model() == "llama3:8b"

    def test_blank_persisted_value_falls_back(self, db_path):
        core_settings.save_system_setting(ms.DEFAULT_MODEL_SETTING_KEY, "   ")
        assert ms.resolve_default_model() == MODELS["default"]

    def test_read_path_never_touches_the_provider(self, db_path):
        # A provider that explodes on every call must not affect the
        # read path — resolving the default is network-free.
        set_override(MockProvider(error=ModelProviderError("boom")))
        assert ms.resolve_default_model() == MODELS["default"]


# ── list_available_models ───────────────────────────────────────────


class TestListAvailableModels:
    def test_reachable_lists_models_and_default(self, db_path):
        set_override(MockProvider(healthy=True, models=["m1", "m2"]))
        out = ms.list_available_models()
        assert out["ok"] is True
        assert out["provider"] == "mock"
        assert out["models"] == ["m1", "m2"]
        assert out["default_model"] == MODELS["default"]
        assert out["config_default_model"] == MODELS["default"]
        assert out["is_custom"] is False

    def test_custom_default_flagged(self, db_path):
        core_settings.save_system_setting(
            ms.DEFAULT_MODEL_SETTING_KEY, "m2"
        )
        set_override(MockProvider(healthy=True, models=["m1", "m2"]))
        out = ms.list_available_models()
        assert out["default_model"] == "m2"
        assert out["is_custom"] is True

    def test_unreachable_is_calm_empty(self, db_path):
        set_override(MockProvider(healthy=False))
        out = ms.list_available_models()
        assert out["ok"] is False
        assert out["models"] == []
        # Still surfaces what Nova *would* use by default.
        assert out["default_model"] == MODELS["default"]


# ── set_default_model ───────────────────────────────────────────────


class TestSetDefaultModel:
    def test_valid_model_is_persisted_and_resolves(self, db_path):
        set_override(MockProvider(healthy=True, models=["keep", "pick"]))
        result = ms.set_default_model("pick")
        assert result["ok"] is True
        assert result["default_model"] == "pick"
        assert result["is_custom"] is True
        # Persisted: a fresh resolve (network-free) returns the choice.
        assert ms.resolve_default_model() == "pick"

    def test_whitespace_is_trimmed_before_validation(self, db_path):
        set_override(MockProvider(healthy=True, models=["pick"]))
        result = ms.set_default_model("  pick  ")
        assert result["default_model"] == "pick"
        assert ms.resolve_default_model() == "pick"

    def test_model_not_in_provider_list_is_rejected(self, db_path):
        set_override(MockProvider(healthy=True, models=["only-this"]))
        with pytest.raises(ms.DefaultModelError):
            ms.set_default_model("not-installed")
        # Nothing was written — the default is still the config model.
        assert ms.resolve_default_model() == MODELS["default"]

    @pytest.mark.parametrize("bad", ["", "   ", "x" * (ms.MAX_MODEL_NAME_LEN + 1)])
    def test_empty_or_oversized_is_rejected(self, db_path, bad):
        set_override(MockProvider(healthy=True, models=["m"]))
        with pytest.raises(ms.DefaultModelError):
            ms.set_default_model(bad)
        assert ms.resolve_default_model() == MODELS["default"]

    def test_non_string_is_rejected(self, db_path):
        set_override(MockProvider(healthy=True, models=["m"]))
        with pytest.raises(ms.DefaultModelError):
            ms.set_default_model(None)  # type: ignore[arg-type]

    def test_unreachable_provider_refuses_with_sanitised_message(
        self, db_path
    ):
        # The mock's health detail names the host-shaped string below;
        # the refusal must NOT echo it (no raw transport / host leak).
        secret_host = "http://bob:hunter2@ollama.internal:11434"
        set_override(
            MockProvider(
                healthy=False,
                error=ModelProviderError(secret_host),
            )
        )
        with pytest.raises(ms.DefaultModelError) as excinfo:
            ms.set_default_model("anything")
        msg = str(excinfo.value)
        assert "hunter2" not in msg
        assert "ollama.internal" not in msg
        assert "unreachable" in msg.lower()
        assert ms.resolve_default_model() == MODELS["default"]

    def test_rejection_does_not_reflect_the_candidate_string(self, db_path):
        set_override(MockProvider(healthy=True, models=["safe"]))
        injected = "<script>evil</script>"
        with pytest.raises(ms.DefaultModelError) as excinfo:
            ms.set_default_model(injected)
        assert injected not in str(excinfo.value)


def test_settings_key_is_not_user_scoped():
    # The default model is host-wide: it must not be treated as a
    # per-user key (that would scope it to one account by mistake).
    assert not core_settings.is_user_setting(ms.DEFAULT_MODEL_SETTING_KEY)


def test_default_model_not_writable_via_generic_allowed_settings():
    # The dedicated, validated endpoint is the only writer — the key
    # must never ride in on the generic settings allowlist.
    from config import ALLOWED_SETTINGS

    assert ms.DEFAULT_MODEL_SETTING_KEY not in ALLOWED_SETTINGS


def test_persisted_default_survives_a_separate_connection(db_path):
    # Sanity: the value really lands in the shared settings table, not
    # a per-test in-memory shim.
    set_override(MockProvider(healthy=True, models=["persisted"]))
    ms.set_default_model("persisted")
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (ms.DEFAULT_MODEL_SETTING_KEY,),
        ).fetchone()
    assert row is not None and row[0] == "persisted"
