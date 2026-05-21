"""Tests for the admin-configurable local GGUF model path (Phase 2).

Pinned contracts (see ``core/gguf_settings.py``):

* :func:`validate_gguf_model_path` accepts a readable ``.gguf`` file that
  resolves inside the allowed model directory, and refuses everything
  else with a sanitised :class:`GgufModelPathError`:

    - a non-``.gguf`` file,
    - a missing file,
    - a directory,
    - a path outside the model directory,
    - ``..`` traversal,
    - a symlink that escapes the model directory,
    - a relative path / ``~`` expansion / empty / NUL-bearing input;

* :func:`resolve_gguf_model_path` prefers the persisted setting over the
  env value and never raises (no DB → env fallback);
* :func:`gguf_status` reports the configured path + directory state
  without loading a model;
* :func:`set_gguf_model_path` validates before persisting and writes
  nothing on failure;
* :func:`test_gguf_provider` reports a missing path / invalid path as a
  calm ``ok=False`` with a sanitised detail (no real wheel needed).

No real ``llama-cpp-python`` wheel and no real ``.gguf`` weights are
needed — the validator only checks for a readable ``.gguf`` file, so a
tiny dummy file stands in for one.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

# Stub optional wheels so importing the provider package (pulled in by
# the cache-invalidation / probe helpers) works on a minimal host.
for _mod in ("ddgs", "duckduckgo_search", "ollama", "sgmllib", "feedparser"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from core import gguf_settings as gs  # noqa: E402
from core import memory as core_memory  # noqa: E402
from core.gguf_settings import GgufModelPathError  # noqa: E402
from core.model_providers import reset as reset_registry  # noqa: E402
from memory import store as natural_store  # noqa: E402


@pytest.fixture
def model_dir(tmp_path):
    d = tmp_path / "nova-models"
    d.mkdir()
    return d


@pytest.fixture
def gguf_file(model_dir):
    """A readable dummy ``.gguf`` file inside the model directory."""
    path = model_dir / "model.gguf"
    path.write_bytes(b"GGUF\x00 not real weights")
    return path


# ── validate_gguf_model_path ─────────────────────────────────────────


class TestValidatePathAccepts:
    def test_accepts_gguf_inside_model_dir(self, model_dir, gguf_file):
        result = gs.validate_gguf_model_path(str(gguf_file), str(model_dir))
        # Returns the fully resolved absolute path (canonical form).
        assert result == str(gguf_file.resolve())

    def test_accepts_gguf_in_subdir_of_model_dir(self, model_dir):
        sub = model_dir / "team-a"
        sub.mkdir()
        f = sub / "m.gguf"
        f.write_bytes(b"GGUF\x00")
        result = gs.validate_gguf_model_path(str(f), str(model_dir))
        assert result == str(f.resolve())

    def test_uppercase_extension_is_accepted(self, model_dir):
        f = model_dir / "Model.GGUF"
        f.write_bytes(b"GGUF\x00")
        result = gs.validate_gguf_model_path(str(f), str(model_dir))
        assert result == str(f.resolve())


class TestValidatePathRejects:
    def test_rejects_non_gguf_file(self, model_dir):
        bad = model_dir / "model.bin"
        bad.write_bytes(b"not gguf")
        with pytest.raises(GgufModelPathError) as exc:
            gs.validate_gguf_model_path(str(bad), str(model_dir))
        assert ".gguf" in str(exc.value)

    def test_rejects_missing_file(self, model_dir):
        missing = model_dir / "absent.gguf"
        with pytest.raises(GgufModelPathError) as exc:
            gs.validate_gguf_model_path(str(missing), str(model_dir))
        assert "No file exists" in str(exc.value)
        # The absolute path must not be echoed back.
        assert str(missing) not in str(exc.value)

    def test_rejects_directory(self, model_dir):
        # A directory named with a .gguf suffix passes the extension gate
        # but must be refused by the is-file check.
        d = model_dir / "weights.gguf"
        d.mkdir()
        with pytest.raises(GgufModelPathError) as exc:
            gs.validate_gguf_model_path(str(d), str(model_dir))
        assert "not a file" in str(exc.value)

    def test_rejects_path_outside_model_dir(self, tmp_path, model_dir):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        f = outside / "model.gguf"
        f.write_bytes(b"GGUF\x00")
        with pytest.raises(GgufModelPathError) as exc:
            gs.validate_gguf_model_path(str(f), str(model_dir))
        assert "inside the configured model directory" in str(exc.value)

    def test_rejects_sibling_prefix_directory(self, tmp_path):
        # "/x/nova-models-evil" must not be treated as inside
        # "/x/nova-models" just because of the shared string prefix.
        allowed = tmp_path / "nova-models"
        allowed.mkdir()
        evil = tmp_path / "nova-models-evil"
        evil.mkdir()
        f = evil / "model.gguf"
        f.write_bytes(b"GGUF\x00")
        with pytest.raises(GgufModelPathError) as exc:
            gs.validate_gguf_model_path(str(f), str(allowed))
        assert "inside the configured model directory" in str(exc.value)

    def test_rejects_dotdot_traversal(self, model_dir):
        traversal = str(model_dir / ".." / "secret.gguf")
        with pytest.raises(GgufModelPathError) as exc:
            gs.validate_gguf_model_path(traversal, str(model_dir))
        assert ".." in str(exc.value)

    def test_rejects_symlink_escaping_model_dir(self, tmp_path, model_dir):
        # A symlink *inside* the model dir whose target is *outside* it
        # must be refused — resolve() follows the link before the
        # containment check.
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "real.gguf"
        target.write_bytes(b"GGUF\x00")
        link = model_dir / "link.gguf"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform")
        with pytest.raises(GgufModelPathError) as exc:
            gs.validate_gguf_model_path(str(link), str(model_dir))
        assert "inside the configured model directory" in str(exc.value)

    def test_rejects_relative_path(self, model_dir):
        with pytest.raises(GgufModelPathError) as exc:
            gs.validate_gguf_model_path("model.gguf", str(model_dir))
        assert "absolute" in str(exc.value)

    def test_rejects_tilde_path(self, model_dir):
        with pytest.raises(GgufModelPathError) as exc:
            gs.validate_gguf_model_path("~/model.gguf", str(model_dir))
        assert "absolute" in str(exc.value)

    def test_rejects_empty(self, model_dir):
        with pytest.raises(GgufModelPathError):
            gs.validate_gguf_model_path("   ", str(model_dir))

    def test_rejects_nul_byte(self, model_dir):
        with pytest.raises(GgufModelPathError) as exc:
            gs.validate_gguf_model_path("/x/m\x00.gguf", str(model_dir))
        assert "invalid characters" in str(exc.value)

    def test_rejects_when_no_model_dir_configured(self, gguf_file):
        with pytest.raises(GgufModelPathError) as exc:
            gs.validate_gguf_model_path(str(gguf_file), "")
        assert "NOVA_MODEL_DIR" in str(exc.value)

    def test_rejects_non_string(self, model_dir):
        with pytest.raises(GgufModelPathError):
            gs.validate_gguf_model_path(1234, str(model_dir))


# ── resolve_model_dir / resolve_gguf_model_path ──────────────────────


class TestResolution:
    def test_model_dir_defaults_to_recommended(self, monkeypatch):
        monkeypatch.setattr("config.NOVA_MODEL_DIR", "")
        assert gs.resolve_model_dir() == "/mnt/archive/nova-models"

    def test_model_dir_honours_config(self, monkeypatch):
        monkeypatch.setattr("config.NOVA_MODEL_DIR", "/srv/models")
        assert gs.resolve_model_dir() == "/srv/models"

    def test_resolve_falls_back_to_env_without_db(self, monkeypatch, tmp_path):
        # No DB on disk → no persisted value → env path is used and no
        # stray database file is created.
        monkeypatch.setattr(core_memory, "DB_PATH", str(tmp_path / "absent.db"))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "/env/model.gguf")
        assert gs.resolve_gguf_model_path() == "/env/model.gguf"
        assert not (tmp_path / "absent.db").exists()

    def test_persisted_path_overrides_env(self, monkeypatch, tmp_path):
        db = str(tmp_path / "nova.db")
        monkeypatch.setattr(core_memory, "DB_PATH", db)
        monkeypatch.setattr(natural_store, "DB_PATH", db)
        core_memory.initialize_db()
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "/env/model.gguf")
        from core.settings import save_system_setting

        save_system_setting(gs.GGUF_MODEL_PATH_SETTING_KEY, "/custom/m.gguf")
        assert gs.resolve_gguf_model_path() == "/custom/m.gguf"


# ── gguf_status ──────────────────────────────────────────────────────


class TestStatus:
    def test_shape_and_unset_state(self, monkeypatch, tmp_path):
        monkeypatch.setattr(core_memory, "DB_PATH", str(tmp_path / "none.db"))
        monkeypatch.setattr("config.MODEL_PROVIDER", "ollama")
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(tmp_path / "nova-models"))
        status = gs.gguf_status()
        assert status["provider"] == "ollama"
        assert status["is_llamacpp"] is False
        assert status["default_provider"] == "ollama"
        assert status["model_dir"] == str(tmp_path / "nova-models")
        assert status["model_dir_exists"] is False
        assert status["configured_path"] == ""
        assert status["path_source"] == "unset"
        assert status["path_valid"] is False
        assert status["filename"] == ""

    def test_reports_env_path_validity(self, monkeypatch, tmp_path, gguf_file, model_dir):
        monkeypatch.setattr(core_memory, "DB_PATH", str(tmp_path / "none.db"))
        monkeypatch.setattr("config.MODEL_PROVIDER", "llamacpp")
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", str(gguf_file))
        status = gs.gguf_status()
        assert status["is_llamacpp"] is True
        assert status["path_source"] == "env"
        assert status["path_valid"] is True
        assert status["filename"] == "model.gguf"
        assert status["configured_path"] == str(gguf_file.resolve())

    def test_reports_invalid_env_path_with_detail(self, monkeypatch, tmp_path, model_dir):
        # An env path outside the model dir is surfaced as invalid with a
        # sanitised detail rather than silently accepted.
        monkeypatch.setattr(core_memory, "DB_PATH", str(tmp_path / "none.db"))
        monkeypatch.setattr("config.MODEL_PROVIDER", "llamacpp")
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        outside = tmp_path / "outside.gguf"
        outside.write_bytes(b"GGUF\x00")
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", str(outside))
        status = gs.gguf_status()
        assert status["path_valid"] is False
        assert status["path_detail"]


# ── set_gguf_model_path ──────────────────────────────────────────────


class TestSetPath:
    @pytest.fixture
    def db(self, monkeypatch, tmp_path):
        path = str(tmp_path / "nova.db")
        monkeypatch.setattr(core_memory, "DB_PATH", path)
        monkeypatch.setattr(natural_store, "DB_PATH", path)
        core_memory.initialize_db()
        reset_registry()
        yield path
        reset_registry()

    def test_valid_path_persists(self, db, monkeypatch, model_dir, gguf_file):
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.MODEL_PROVIDER", "llamacpp")
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        out = gs.set_gguf_model_path(str(gguf_file))
        assert out["configured_path"] == str(gguf_file.resolve())
        assert out["path_source"] == "custom"
        assert out["path_valid"] is True
        # A follow-up resolve reflects the persisted choice.
        assert gs.resolve_gguf_model_path() == str(gguf_file.resolve())

    def test_invalid_path_writes_nothing(self, db, monkeypatch, model_dir, tmp_path):
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        outside = tmp_path / "evil.gguf"
        outside.write_bytes(b"GGUF\x00")
        with pytest.raises(GgufModelPathError):
            gs.set_gguf_model_path(str(outside))
        # Nothing persisted → resolve falls back to the (empty) env value.
        assert gs.resolve_gguf_model_path() == ""


# ── test_gguf_provider ───────────────────────────────────────────────


class TestProviderTest:
    def test_no_path_is_calm_failure(self, monkeypatch, tmp_path):
        monkeypatch.setattr(core_memory, "DB_PATH", str(tmp_path / "none.db"))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        result = gs.test_gguf_provider()
        assert result["ok"] is False
        assert result["provider"] == "llamacpp"
        assert result["path_valid"] is False
        assert "No GGUF model" in result["detail"]

    def test_invalid_path_is_calm_failure(self, monkeypatch, tmp_path, model_dir):
        monkeypatch.setattr(core_memory, "DB_PATH", str(tmp_path / "none.db"))
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        outside = tmp_path / "outside.gguf"
        outside.write_bytes(b"GGUF\x00")
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", str(outside))
        result = gs.test_gguf_provider()
        assert result["ok"] is False
        assert result["path_valid"] is False
        assert "inside the configured model directory" in result["detail"]

    def test_valid_path_reports_path_valid(self, monkeypatch, tmp_path, model_dir, gguf_file):
        # The dependency may be absent in CI; either way the path is valid
        # and the detail is a clean, non-sensitive string.
        monkeypatch.setattr(core_memory, "DB_PATH", str(tmp_path / "none.db"))
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", str(gguf_file))
        result = gs.test_gguf_provider()
        assert result["path_valid"] is True
        assert result["filename"] == "model.gguf"
        assert isinstance(result["detail"], str) and result["detail"]
