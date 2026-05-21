"""Tests for the local GGUF model library (Phase 3).

Pinned contracts (see ``core/gguf_settings.py`` and the
``/admin/provider/gguf/models`` + ``/admin/provider/gguf/select``
endpoints):

* :func:`list_local_models` lists ``.gguf`` files **inside**
  ``NOVA_MODEL_DIR`` with safe metadata (name, relative path, size,
  ISO-8601 UTC mtime, selected flag) and:

    - finds models in subdirectories (bounded recursion),
    - skips non-``.gguf`` files, hidden files, and hidden directories,
    - never follows a symlinked directory out of the model dir, and omits
      a symlinked *file* whose target escapes the model dir,
    - reports only relative paths (no unrelated absolute paths leak),
    - degrades to a calm empty list + warning when the dir is missing /
      not a directory, and never raises;

* :func:`select_local_model` resolves a *relative* path against
  ``NOVA_MODEL_DIR`` and persists it through the Phase-2 boundary —
  refusing an absolute path, ``..`` traversal, a missing file, or a
  non-``.gguf`` file with a sanitised :class:`GgufModelPathError` and
  writing nothing;

* both endpoints are admin-only and Ollama stays the default throughout.

No real ``llama-cpp-python`` wheel or real ``.gguf`` weights are needed —
a tiny dummy file stands in for a model.
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

for _mod in ("ddgs", "duckduckgo_search", "ollama", "sgmllib", "feedparser"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from core import gguf_settings as gs  # noqa: E402
from core import memory as core_memory, users  # noqa: E402
from core.gguf_settings import GgufModelPathError  # noqa: E402
from core.model_providers import reset as reset_registry  # noqa: E402
from memory import store as natural_store  # noqa: E402

_ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _write_gguf(path, payload=b"GGUF\x00 not real weights"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


@pytest.fixture
def model_dir(tmp_path):
    d = tmp_path / "nova-models"
    d.mkdir()
    return d


@pytest.fixture
def no_db(monkeypatch, tmp_path):
    """Point the DB at a non-existent file so persisted reads are skipped."""
    monkeypatch.setattr(core_memory, "DB_PATH", str(tmp_path / "absent.db"))


# ── list_local_models ────────────────────────────────────────────────


class TestListLocalModels:
    def test_lists_root_file_with_safe_metadata(
        self, monkeypatch, no_db, model_dir
    ):
        f = _write_gguf(model_dir / "model.gguf", b"GGUF" + b"x" * 100)
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        out = gs.list_local_models()
        assert out["model_dir"] == str(model_dir)
        assert out["model_dir_exists"] is True
        assert out["count"] == 1
        assert out["truncated"] is False
        entry = out["models"][0]
        assert entry["name"] == "model.gguf"
        assert entry["relative_path"] == "model.gguf"
        assert entry["size_bytes"] == f.stat().st_size
        assert _ISO_Z.match(entry["modified_at"]), entry["modified_at"]
        # Parses as a real timestamp.
        datetime.strptime(entry["modified_at"], "%Y-%m-%dT%H:%M:%SZ")
        assert entry["selected"] is False

    def test_finds_models_in_subdirectories(self, monkeypatch, no_db, model_dir):
        _write_gguf(model_dir / "a.gguf")
        _write_gguf(model_dir / "team-a" / "b.gguf")
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        out = gs.list_local_models()
        rels = {m["relative_path"] for m in out["models"]}
        assert rels == {"a.gguf", "team-a/b.gguf"}

    def test_skips_non_gguf_files(self, monkeypatch, no_db, model_dir):
        _write_gguf(model_dir / "real.gguf")
        (model_dir / "notes.txt").write_text("hi")
        (model_dir / "weights.bin").write_bytes(b"nope")
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        out = gs.list_local_models()
        assert [m["name"] for m in out["models"]] == ["real.gguf"]

    def test_skips_hidden_files_and_dirs(self, monkeypatch, no_db, model_dir):
        _write_gguf(model_dir / "visible.gguf")
        _write_gguf(model_dir / ".hidden.gguf")
        _write_gguf(model_dir / ".cache" / "secret.gguf")
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        out = gs.list_local_models()
        assert [m["name"] for m in out["models"]] == ["visible.gguf"]

    def test_uppercase_extension_is_listed(self, monkeypatch, no_db, model_dir):
        _write_gguf(model_dir / "Model.GGUF")
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        out = gs.list_local_models()
        assert [m["name"] for m in out["models"]] == ["Model.GGUF"]

    def test_relative_paths_only_no_absolute_leak(
        self, monkeypatch, no_db, model_dir
    ):
        _write_gguf(model_dir / "sub" / "m.gguf")
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        out = gs.list_local_models()
        for m in out["models"]:
            assert not m["relative_path"].startswith("/")
            assert str(model_dir) not in m["relative_path"]

    def test_selected_flag_tracks_configured_model(
        self, monkeypatch, no_db, model_dir
    ):
        chosen = _write_gguf(model_dir / "chosen.gguf")
        _write_gguf(model_dir / "other.gguf")
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", str(chosen))
        out = gs.list_local_models()
        by_name = {m["name"]: m for m in out["models"]}
        assert by_name["chosen.gguf"]["selected"] is True
        assert by_name["other.gguf"]["selected"] is False

    def test_missing_model_dir_is_calm_empty(self, monkeypatch, no_db, tmp_path):
        monkeypatch.setattr(
            "config.NOVA_MODEL_DIR", str(tmp_path / "does-not-exist")
        )
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        out = gs.list_local_models()
        assert out["models"] == []
        assert out["model_dir_exists"] is False
        assert out["warnings"]

    def test_model_dir_that_is_a_file_is_calm(self, monkeypatch, no_db, tmp_path):
        f = tmp_path / "not-a-dir"
        f.write_text("x")
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(f))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        out = gs.list_local_models()
        assert out["models"] == []
        assert any("not a directory" in w for w in out["warnings"])

    def test_empty_dir_warns_no_models(self, monkeypatch, no_db, model_dir):
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        out = gs.list_local_models()
        assert out["models"] == []
        assert any(".gguf" in w for w in out["warnings"])

    def test_does_not_follow_symlinked_dir_escaping(
        self, monkeypatch, no_db, tmp_path, model_dir
    ):
        outside = tmp_path / "outside"
        outside.mkdir()
        _write_gguf(outside / "escaped.gguf")
        link = model_dir / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform")
        _write_gguf(model_dir / "inside.gguf")
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        out = gs.list_local_models()
        names = {m["name"] for m in out["models"]}
        assert "inside.gguf" in names
        assert "escaped.gguf" not in names

    def test_omits_symlinked_file_escaping(
        self, monkeypatch, no_db, tmp_path, model_dir
    ):
        outside = tmp_path / "outside"
        outside.mkdir()
        target = _write_gguf(outside / "real.gguf")
        link = model_dir / "link.gguf"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform")
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        out = gs.list_local_models()
        assert [m["name"] for m in out["models"]] == []

    def test_depth_bound_excludes_very_deep_file(
        self, monkeypatch, no_db, model_dir
    ):
        # Far below MAX_SCAN_DEPTH levels: must not be listed.
        deep = model_dir
        for i in range(gs.MAX_SCAN_DEPTH + 2):
            deep = deep / f"d{i}"
        _write_gguf(deep / "deep.gguf")
        _write_gguf(model_dir / "shallow.gguf")
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        out = gs.list_local_models()
        names = {m["name"] for m in out["models"]}
        assert "shallow.gguf" in names
        assert "deep.gguf" not in names

    def test_truncates_at_model_cap(self, monkeypatch, no_db, model_dir):
        monkeypatch.setattr(gs, "MAX_LIBRARY_MODELS", 3)
        for i in range(6):
            _write_gguf(model_dir / f"m{i}.gguf")
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        out = gs.list_local_models()
        assert out["count"] == 3
        assert out["truncated"] is True
        assert any("only the first" in w for w in out["warnings"])


# ── select_local_model ───────────────────────────────────────────────


class TestSelectLocalModel:
    @pytest.fixture
    def db(self, monkeypatch, tmp_path):
        path = str(tmp_path / "nova.db")
        monkeypatch.setattr(core_memory, "DB_PATH", path)
        monkeypatch.setattr(natural_store, "DB_PATH", path)
        core_memory.initialize_db()
        reset_registry()
        yield path
        reset_registry()

    def test_valid_relative_path_persists(self, db, monkeypatch, model_dir):
        f = _write_gguf(model_dir / "sub" / "m.gguf")
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.MODEL_PROVIDER", "llamacpp")
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        out = gs.select_local_model("sub/m.gguf")
        assert out["configured_path"] == str(f.resolve())
        assert out["path_source"] == "custom"
        assert out["path_valid"] is True
        assert gs.resolve_gguf_model_path() == str(f.resolve())

    def test_select_then_list_marks_selected(self, db, monkeypatch, model_dir):
        _write_gguf(model_dir / "a.gguf")
        _write_gguf(model_dir / "b.gguf")
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        gs.select_local_model("b.gguf")
        out = gs.list_local_models()
        by_name = {m["name"]: m["selected"] for m in out["models"]}
        assert by_name == {"a.gguf": False, "b.gguf": True}

    def test_absolute_path_refused(self, db, monkeypatch, model_dir):
        f = _write_gguf(model_dir / "m.gguf")
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        with pytest.raises(GgufModelPathError) as exc:
            gs.select_local_model(str(f))
        assert "absolute" in str(exc.value)
        assert gs.resolve_gguf_model_path() == ""

    def test_traversal_refused(self, db, monkeypatch, model_dir):
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        with pytest.raises(GgufModelPathError) as exc:
            gs.select_local_model("../secret.gguf")
        assert ".." in str(exc.value)
        assert gs.resolve_gguf_model_path() == ""

    def test_missing_file_refused(self, db, monkeypatch, model_dir):
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        with pytest.raises(GgufModelPathError) as exc:
            gs.select_local_model("absent.gguf")
        assert "No file exists" in str(exc.value)

    def test_non_gguf_refused(self, db, monkeypatch, model_dir):
        (model_dir / "model.bin").write_bytes(b"nope")
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        with pytest.raises(GgufModelPathError) as exc:
            gs.select_local_model("model.bin")
        assert ".gguf" in str(exc.value)

    def test_empty_refused(self, db, monkeypatch, model_dir):
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        with pytest.raises(GgufModelPathError):
            gs.select_local_model("   ")

    def test_tilde_refused(self, db, monkeypatch, model_dir):
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        with pytest.raises(GgufModelPathError):
            gs.select_local_model("~/m.gguf")


# ── Endpoint wiring ──────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_registry():
    reset_registry()
    yield
    reset_registry()


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "nova.db")
    monkeypatch.setattr(core_memory, "DB_PATH", path)
    monkeypatch.setattr(natural_store, "DB_PATH", path)
    core_memory.initialize_db()
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM users")
    return path


def _make_user(db_path, username, password="pw", role=users.ROLE_USER,
               is_restricted=False):
    with sqlite3.connect(db_path) as conn:
        return users.create_user(
            conn, username, password, role=role, is_restricted=is_restricted,
        )


@pytest.fixture
def web_client(db_path, monkeypatch):
    monkeypatch.setattr(core_memory, "DB_PATH", db_path)
    monkeypatch.setattr(natural_store, "DB_PATH", db_path)
    from core.rate_limiter import _login_limiter
    _login_limiter._store.clear()

    from fastapi.testclient import TestClient
    import web
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("web.initialize_db"))
        stack.enter_context(patch("web.learn_from_feeds"))
        stack.enter_context(patch("web.scheduler", MagicMock()))
        with TestClient(web.app, raise_server_exceptions=True) as client:
            yield client


def _login(client, username, password="pw"):
    resp = client.post("/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _h(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_token(db_path, web_client):
    _make_user(db_path, "alice", role=users.ROLE_ADMIN)
    return _login(web_client, "alice")


@pytest.fixture
def user_token(db_path, web_client):
    _make_user(db_path, "bob")
    return _login(web_client, "bob")


@pytest.fixture
def restricted_token(db_path, web_client):
    _make_user(db_path, "kid", is_restricted=True)
    return _login(web_client, "kid")


_ENDPOINTS = [
    ("GET", "/admin/provider/gguf/models", None),
    ("POST", "/admin/provider/gguf/select", {"relative_path": "m.gguf"}),
]


class TestLibraryEndpointsAuth:
    @pytest.mark.parametrize("method,path,body", _ENDPOINTS)
    def test_non_admin_forbidden(self, web_client, user_token, method, path, body):
        if method == "GET":
            resp = web_client.get(path, headers=_h(user_token))
        else:
            resp = web_client.post(path, headers=_h(user_token), json=body)
        assert resp.status_code == 403

    @pytest.mark.parametrize("method,path,body", _ENDPOINTS)
    def test_restricted_forbidden(self, web_client, restricted_token, method, path, body):
        if method == "GET":
            resp = web_client.get(path, headers=_h(restricted_token))
        else:
            resp = web_client.post(path, headers=_h(restricted_token), json=body)
        assert resp.status_code == 403

    @pytest.mark.parametrize("method,path,body", _ENDPOINTS)
    def test_unauthenticated_blocked(self, web_client, method, path, body):
        if method == "GET":
            resp = web_client.get(path)
        else:
            resp = web_client.post(path, json=body)
        assert resp.status_code in (401, 403)


class TestModelsEndpoint:
    def test_lists_models_with_selected_flag(
        self, web_client, admin_token, monkeypatch, model_dir
    ):
        chosen = _write_gguf(model_dir / "chosen.gguf")
        _write_gguf(model_dir / "team" / "other.gguf")
        monkeypatch.setattr("config.MODEL_PROVIDER", "llamacpp")
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", str(chosen))
        resp = web_client.get("/admin/provider/gguf/models", headers=_h(admin_token))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["model_dir"] == str(model_dir)
        assert body["model_dir_exists"] is True
        by_name = {m["name"]: m for m in body["models"]}
        assert set(by_name) == {"chosen.gguf", "other.gguf"}
        assert by_name["chosen.gguf"]["selected"] is True
        assert by_name["other.gguf"]["relative_path"] == "team/other.gguf"

    def test_missing_dir_is_calm_200(self, web_client, admin_token, monkeypatch, tmp_path):
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(tmp_path / "nope"))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        resp = web_client.get("/admin/provider/gguf/models", headers=_h(admin_token))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["models"] == []
        assert body["warnings"]


class TestSelectEndpoint:
    def test_valid_select_persists(self, web_client, admin_token, monkeypatch, model_dir):
        f = _write_gguf(model_dir / "pick.gguf")
        monkeypatch.setattr("config.MODEL_PROVIDER", "llamacpp")
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        resp = web_client.post(
            "/admin/provider/gguf/select",
            headers=_h(admin_token),
            json={"relative_path": "pick.gguf"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["configured_path"] == str(f.resolve())
        assert body["path_source"] == "custom"
        assert body["path_valid"] is True
        # The library now marks it selected.
        listing = web_client.get(
            "/admin/provider/gguf/models", headers=_h(admin_token)
        ).json()
        assert listing["models"][0]["selected"] is True

    def test_absolute_path_is_400(self, web_client, admin_token, monkeypatch, model_dir):
        f = _write_gguf(model_dir / "m.gguf")
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        resp = web_client.post(
            "/admin/provider/gguf/select",
            headers=_h(admin_token),
            json={"relative_path": str(f)},
        )
        assert resp.status_code == 400, resp.text
        assert "absolute" in resp.json()["detail"]

    def test_traversal_is_400(self, web_client, admin_token, monkeypatch, model_dir):
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        resp = web_client.post(
            "/admin/provider/gguf/select",
            headers=_h(admin_token),
            json={"relative_path": "../secret.gguf"},
        )
        assert resp.status_code == 400, resp.text
        assert ".." in resp.json()["detail"]

    def test_missing_file_is_400(self, web_client, admin_token, monkeypatch, model_dir):
        monkeypatch.setattr("config.NOVA_MODEL_DIR", str(model_dir))
        monkeypatch.setattr("config.NOVA_GGUF_MODEL_PATH", "")
        resp = web_client.post(
            "/admin/provider/gguf/select",
            headers=_h(admin_token),
            json={"relative_path": "absent.gguf"},
        )
        assert resp.status_code == 400, resp.text
        assert "No file exists" in resp.json()["detail"]

    def test_empty_relative_path_is_422(self, web_client, admin_token):
        resp = web_client.post(
            "/admin/provider/gguf/select",
            headers=_h(admin_token),
            json={"relative_path": ""},
        )
        assert resp.status_code == 422

    def test_extra_field_is_422(self, web_client, admin_token):
        resp = web_client.post(
            "/admin/provider/gguf/select",
            headers=_h(admin_token),
            json={"relative_path": "m.gguf", "provider": "evil"},
        )
        assert resp.status_code == 422
