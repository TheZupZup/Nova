"""Tests for the Nova Memory Pack (portable per-user export / import).

Covers the contract from the feature request:

* manifest generation (format id / version, counts, file hashes),
* export excludes secrets (password hash, secret-shaped settings,
  embeddings, the host settings table),
* import rejects invalid ZIPs (not a zip, empty, missing/!json
  manifest, newer format_version, zip-bomb size),
* import rejects path traversal / absolute / symlink members,
* import merge behaviour + duplicate handling (re-import is a no-op),
* settings merge never overwrites existing preferences,
* the import runs in a transaction (a mid-import failure rolls back),
* the export lands under NOVA_DATA_DIR/memory-packs (Docker path),
* the authenticated HTTP endpoints (export / preview / import).
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

for _mod in ("ddgs", "ollama", "sgmllib", "feedparser"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from fastapi.testclient import TestClient  # noqa: E402

from core import memory as core_memory  # noqa: E402
from core import memory_pack as mp  # noqa: E402
from core import paths as core_paths  # noqa: E402
from core import settings as core_settings  # noqa: E402
from core import users  # noqa: E402
from memory import store as natural_store  # noqa: E402


# ── fixtures / helpers ──────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    monkeypatch.setenv(core_paths.ENV_VAR, str(tmp_path))
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


def _add_natural_memory(db_path, user_id, kind, topic, content,
                        project_id=None):
    """Insert a natural_memories row directly (no embedding / ollama)."""
    now = datetime.now().isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO natural_memories (id, kind, topic, content, "
            "confidence, source, created_at, updated_at, last_seen_at, "
            "embedding, user_id, project_id) "
            "VALUES (?, ?, ?, ?, 0.9, 'extractor', ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), kind, topic, content, now, now, now,
             json.dumps([0.1, 0.2, 0.3]), user_id, project_id),
        )


def _seed_user_with_data(db_path, username="alice"):
    """Create a user with memories, a conversation, and settings."""
    uid = _make_user(db_path, username)
    core_memory.save_memory("preferences", "Alice prefers tea over coffee", uid)
    core_memory.save_memory("facts", "Alice lives in Berlin", uid)
    _add_natural_memory(db_path, uid, "preference", "editor", "Uses Neovim daily")
    cid = core_memory.create_conversation("Trip planning", uid)
    core_memory.save_message(cid, "user", "Help me plan a trip to Japan")
    core_memory.save_message(cid, "assistant", "Of course!", "test-model")
    core_settings.save_user_setting(uid, "response_style", "concise")
    # A secret-shaped per-user setting that must never be exported.
    core_settings.save_user_setting(
        uid, "github_token", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab",
    )
    return uid


def _read_zip(data: bytes) -> dict:
    out = {}
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            out[name] = zf.read(name)
    return out


def _manifest_bytes(version=mp.FORMAT_VERSION, fmt=mp.FORMAT_ID):
    return json.dumps(
        {"format": fmt, "format_version": version, "counts": {}}
    ).encode("utf-8")


def _make_zip(members: dict, symlinks: dict | None = None) -> bytes:
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w") as zf:
        for name, body in members.items():
            zf.writestr(name, body)
        for name, target in (symlinks or {}).items():
            info = zipfile.ZipInfo(name)
            info.external_attr = 0o120777 << 16  # S_IFLNK | 0777
            zf.writestr(info, target)
    return bio.getvalue()


# ── export: manifest + structure ────────────────────────────────────


class TestExportManifest:
    def test_build_produces_zip_with_expected_files(self, db_path):
        uid = _seed_user_with_data(db_path)
        result = mp.build_memory_pack(uid)
        assert result.filename.endswith(".zip")
        assert result.filename.startswith("nova-memory-pack-")
        files = _read_zip(result.data)
        for name in (mp.MANIFEST_NAME, mp.PROFILE_NAME, mp.MEMORIES_NAME,
                     mp.CONVERSATIONS_NAME, mp.SUMMARIES_NAME,
                     mp.SETTINGS_NAME):
            assert name in files

    def test_manifest_format_and_counts(self, db_path):
        uid = _seed_user_with_data(db_path)
        result = mp.build_memory_pack(uid)
        manifest = result.manifest
        assert manifest["format"] == mp.FORMAT_ID
        assert manifest["format_version"] == mp.FORMAT_VERSION
        assert manifest["counts"]["memories"] == 2
        assert manifest["counts"]["natural_memories"] == 1
        assert manifest["counts"]["conversations"] == 1
        assert manifest["counts"]["messages"] == 2
        assert "privacy_notice" in manifest
        assert "personal conversation history" in manifest["privacy_notice"]

    def test_manifest_files_have_hashes(self, db_path):
        uid = _seed_user_with_data(db_path)
        result = mp.build_memory_pack(uid)
        files = _read_zip(result.data)
        import hashlib
        for entry in result.manifest["files"]:
            body = files[entry["name"]]
            assert entry["size"] == len(body)
            assert entry["sha256"] == hashlib.sha256(body).hexdigest()

    def test_export_is_deterministic_for_fixed_now(self, db_path):
        uid = _seed_user_with_data(db_path)
        when = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        a = mp.build_memory_pack(uid, now=when)
        b = mp.build_memory_pack(uid, now=when)
        # The data files are byte-identical (zip mtimes are pinned to now).
        assert _read_zip(a.data)[mp.MEMORIES_NAME] == \
            _read_zip(b.data)[mp.MEMORIES_NAME]


# ── export: secret exclusion ────────────────────────────────────────


class TestExportExcludesSecrets:
    def test_password_hash_never_appears(self, db_path):
        uid = _seed_user_with_data(db_path)
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT password_hash FROM users WHERE id = ?", (uid,),
            ).fetchone()
        password_hash = row[0].encode("utf-8")
        result = mp.build_memory_pack(uid)
        assert password_hash not in result.data
        profile = json.loads(_read_zip(result.data)[mp.PROFILE_NAME])
        assert "password_hash" not in profile["user"]

    def test_secret_shaped_setting_excluded(self, db_path):
        uid = _seed_user_with_data(db_path)
        result = mp.build_memory_pack(uid)
        settings = json.loads(_read_zip(result.data)[mp.SETTINGS_NAME])
        assert "response_style" in settings["user_settings"]
        assert "github_token" not in settings["user_settings"]
        assert b"ghp_ABCDEFG" not in result.data

    def test_embeddings_excluded(self, db_path):
        uid = _seed_user_with_data(db_path)
        result = mp.build_memory_pack(uid)
        memories = json.loads(_read_zip(result.data)[mp.MEMORIES_NAME])
        for row in memories["natural"]:
            assert "embedding" not in row

    def test_profile_contains_safe_fields(self, db_path):
        uid = _seed_user_with_data(db_path)
        result = mp.build_memory_pack(uid)
        profile = json.loads(_read_zip(result.data)[mp.PROFILE_NAME])
        assert profile["user"]["username"] == "alice"
        assert "personalization" in profile
        assert profile["personalization"]["response_style"] == "concise"


# ── inspect / validation ────────────────────────────────────────────


class TestInspectValid:
    def test_valid_pack_inspects_clean(self, db_path):
        uid = _seed_user_with_data(db_path)
        result = mp.build_memory_pack(uid)
        report = mp.inspect_memory_pack(result.data)
        assert report.valid is True
        assert report.errors == ()
        assert report.counts["memories"] == 2
        assert report.counts["conversations"] == 1


class TestInspectRejectsInvalid:
    def test_empty_bytes(self):
        report = mp.inspect_memory_pack(b"")
        assert report.valid is False

    def test_not_a_zip(self):
        report = mp.inspect_memory_pack(b"this is not a zip file")
        assert report.valid is False
        assert "not a valid .zip" in report.errors[0]

    def test_missing_manifest(self):
        data = _make_zip({mp.PROFILE_NAME: b'{"version": 1}'})
        report = mp.inspect_memory_pack(data)
        assert report.valid is False
        assert any("manifest" in e for e in report.errors)

    def test_manifest_not_json(self):
        data = _make_zip({mp.MANIFEST_NAME: b"{ this is not json"})
        report = mp.inspect_memory_pack(data)
        assert report.valid is False
        assert any("not valid JSON" in e for e in report.errors)

    def test_wrong_format(self):
        data = _make_zip({mp.MANIFEST_NAME: _manifest_bytes(fmt="something-else")})
        report = mp.inspect_memory_pack(data)
        assert report.valid is False
        assert any("format" in e for e in report.errors)

    def test_newer_format_version_refused(self):
        data = _make_zip({mp.MANIFEST_NAME: _manifest_bytes(version=999)})
        report = mp.inspect_memory_pack(data)
        assert report.valid is False
        assert any("newer Nova" in e for e in report.errors)

    def test_data_file_not_json(self):
        data = _make_zip({
            mp.MANIFEST_NAME: _manifest_bytes(),
            mp.MEMORIES_NAME: b"{ broken",
        })
        report = mp.inspect_memory_pack(data)
        assert report.valid is False

    def test_zip_bomb_size_rejected(self, monkeypatch):
        monkeypatch.setattr(mp, "MAX_TOTAL_UNCOMPRESSED_BYTES", 5)
        data = _make_zip({mp.MANIFEST_NAME: _manifest_bytes()})
        report = mp.inspect_memory_pack(data)
        assert report.valid is False
        assert any("zip bomb" in e.lower() or "too large" in e.lower()
                   for e in report.errors)

    def test_too_many_members_rejected(self, monkeypatch):
        monkeypatch.setattr(mp, "MAX_MEMBERS", 1)
        data = _make_zip({
            mp.MANIFEST_NAME: _manifest_bytes(),
            mp.PROFILE_NAME: b'{"version": 1}',
        })
        report = mp.inspect_memory_pack(data)
        assert report.valid is False


class TestInspectRejectsTraversal:
    def test_parent_traversal(self):
        data = _make_zip({
            mp.MANIFEST_NAME: _manifest_bytes(),
            "../evil.json": b"{}",
        })
        report = mp.inspect_memory_pack(data)
        assert report.valid is False
        assert any("Unsafe" in e for e in report.errors)

    def test_absolute_path(self):
        data = _make_zip({
            mp.MANIFEST_NAME: _manifest_bytes(),
            "/etc/passwd": b"x",
        })
        report = mp.inspect_memory_pack(data)
        assert report.valid is False

    def test_symlink_member(self):
        data = _make_zip(
            {mp.MANIFEST_NAME: _manifest_bytes()},
            symlinks={"link.json": "/etc/passwd"},
        )
        report = mp.inspect_memory_pack(data)
        assert report.valid is False
        assert any("symlink" in e.lower() for e in report.errors)


# ── import: merge + dedup ───────────────────────────────────────────


class TestImportMerge:
    def test_round_trip_into_fresh_instance(self, tmp_path, monkeypatch):
        db1 = str(tmp_path / "a.db")
        db2 = str(tmp_path / "b.db")

        monkeypatch.setattr(core_memory, "DB_PATH", db1)
        monkeypatch.setattr(natural_store, "DB_PATH", db1)
        core_memory.initialize_db()
        uid_a = _seed_user_with_data(db1, "alice")
        pack = mp.build_memory_pack(uid_a)

        monkeypatch.setattr(core_memory, "DB_PATH", db2)
        monkeypatch.setattr(natural_store, "DB_PATH", db2)
        core_memory.initialize_db()
        uid_b = _make_user(db2, "bob")

        result = mp.import_memory_pack(pack.data, uid_b, confirm=True)
        assert result.outcome == mp.OUTCOME_IMPORTED
        assert result.imported["memories"] == 2
        assert result.imported["natural_memories"] == 1
        assert result.imported["conversations"] == 1
        assert result.imported["messages"] == 2

        mems = core_memory.list_memories(uid_b)
        assert any("tea" in m["content"] for m in mems)
        convs = core_memory.load_conversations(uid_b)
        assert any(c["title"] == "Trip planning" for c in convs)
        nat = natural_store.list_memories(uid_b, db_path=db2)
        assert any("Neovim" in m.content for m in nat)

    def test_unconfirmed_writes_nothing(self, db_path):
        uid = _seed_user_with_data(db_path)
        pack = mp.build_memory_pack(uid)
        target = _make_user(db_path, "bob")
        result = mp.import_memory_pack(pack.data, target, confirm=False)
        assert result.outcome == mp.OUTCOME_UNCONFIRMED
        assert core_memory.list_memories(target) == []

    def test_reimport_is_deduplicated(self, db_path):
        uid = _seed_user_with_data(db_path)
        pack = mp.build_memory_pack(uid)
        target = _make_user(db_path, "bob")

        first = mp.import_memory_pack(pack.data, target, confirm=True)
        assert first.imported["memories"] == 2
        assert first.imported["conversations"] == 1

        second = mp.import_memory_pack(pack.data, target, confirm=True)
        assert second.imported["memories"] == 0
        assert second.imported["conversations"] == 0
        assert second.skipped["memories_duplicate"] == 2
        assert second.skipped["conversations_duplicate"] == 1
        # No duplicate rows landed in the database.
        assert len(core_memory.list_memories(target)) == 2
        assert len(core_memory.load_conversations(target)) == 1

    def test_partial_overlap_imports_only_missing(self, db_path):
        uid = _seed_user_with_data(db_path)
        pack = mp.build_memory_pack(uid)
        target = _make_user(db_path, "bob")
        # Bob already knows one of the facts verbatim.
        core_memory.save_memory("preferences", "Alice prefers tea over coffee",
                                target)
        result = mp.import_memory_pack(pack.data, target, confirm=True)
        assert result.imported["memories"] == 1
        assert result.skipped["memories_duplicate"] == 1

    def test_settings_merge_never_overwrites(self, db_path):
        uid = _seed_user_with_data(db_path)
        pack = mp.build_memory_pack(uid)  # exports response_style=concise
        target = _make_user(db_path, "bob")
        core_settings.save_user_setting(target, "response_style", "technical")

        result = mp.import_memory_pack(pack.data, target, confirm=True)
        # Existing preference preserved; never clobbered by the pack.
        assert core_settings.get_user_setting(target, "response_style") == \
            "technical"
        assert result.skipped["settings_existing"] >= 1

    def test_import_drops_secret_settings(self, db_path):
        # Defense-in-depth: even a hand-crafted pack carrying a secret
        # setting must never land that value in the database.
        target = _make_user(db_path, "bob")
        data = _make_zip({
            mp.MANIFEST_NAME: _manifest_bytes(),
            mp.SETTINGS_NAME: json.dumps({"version": 1, "user_settings": {
                "warmth_level": "high",
                "api_key": "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdef",
            }}).encode("utf-8"),
        })
        result = mp.import_memory_pack(data, target, confirm=True)
        assert result.outcome == mp.OUTCOME_IMPORTED
        assert result.imported["settings"] == 1
        assert result.skipped["settings_secret"] >= 1
        assert core_settings.get_user_setting(target, "warmth_level") == "high"
        assert core_settings.get_user_setting(
            target, "api_key", "MISSING") == "MISSING"

    def test_projects_recreated_by_name(self, db_path):
        uid = _make_user(db_path, "alice")
        from core import projects as projects_mod
        pid = projects_mod.create_project("Nova", uid)["id"]
        core_memory.save_memory("facts", "Project fact about Nova", uid,
                                project_id=pid)
        pack = mp.build_memory_pack(uid)

        target = _make_user(db_path, "bob")
        result = mp.import_memory_pack(pack.data, target, confirm=True)
        assert result.imported["projects"] == 1
        bob_projects = projects_mod.list_projects(target)
        assert any(p["name"] == "Nova" for p in bob_projects)


class TestImportTransaction:
    def test_failure_rolls_back(self, db_path, monkeypatch):
        uid = _seed_user_with_data(db_path)
        pack = mp.build_memory_pack(uid)
        target = _make_user(db_path, "bob")

        # Force a failure *after* memories/conversations are inserted but
        # before commit. The whole transaction must roll back.
        def _boom(*args, **kwargs):
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(mp, "_merge_settings", _boom)
        result = mp.import_memory_pack(pack.data, target, confirm=True)
        assert result.outcome == mp.OUTCOME_FAILED
        # Database is untouched: no memories, no conversations imported.
        assert core_memory.list_memories(target) == []
        assert core_memory.load_conversations(target) == []


# ── preview ─────────────────────────────────────────────────────────


class TestImportPreview:
    def test_preview_reports_new_counts(self, db_path):
        uid = _seed_user_with_data(db_path)
        pack = mp.build_memory_pack(uid)
        target = _make_user(db_path, "bob")
        preview = mp.build_import_preview(pack.data, target)
        assert preview.valid is True
        assert preview.counts["memories"]["new"] == 2
        assert preview.counts["conversations"]["new"] == 1
        # The secret-shaped setting was already stripped at export time, so
        # the safe preference is the only applicable setting here.
        assert preview.counts["settings"]["applicable"] >= 1

    def test_preview_marks_duplicates(self, db_path):
        uid = _seed_user_with_data(db_path)
        pack = mp.build_memory_pack(uid)
        target = _make_user(db_path, "bob")
        mp.import_memory_pack(pack.data, target, confirm=True)
        preview = mp.build_import_preview(pack.data, target)
        assert preview.counts["memories"]["new"] == 0
        assert preview.counts["memories"]["duplicate"] == 2

    def test_preview_invalid_pack(self):
        preview = mp.build_import_preview(b"not a zip", 1)
        assert preview.valid is False


# ── Docker data path ────────────────────────────────────────────────


class TestDockerDataPath:
    def test_pack_written_under_memory_packs(self, db_path, tmp_path,
                                             monkeypatch):
        root = tmp_path / "NovaData"
        root.mkdir()
        monkeypatch.setenv(core_paths.ENV_VAR, str(root))
        uid = _seed_user_with_data(db_path)
        result = mp.build_memory_pack(uid)
        out = mp.write_pack_to_data_dir(result)
        assert out is not None
        assert out.parent == root / core_paths.MEMORY_PACKS_SUBDIR
        assert out.is_file()
        assert out.name == result.filename


# ── HTTP endpoints ──────────────────────────────────────────────────


@pytest.fixture
def web_client(db_path, monkeypatch):
    monkeypatch.setattr(core_memory, "DB_PATH", db_path)
    monkeypatch.setattr(natural_store, "DB_PATH", db_path)
    from core.rate_limiter import _login_limiter
    _login_limiter._store.clear()

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


class TestEndpointsAuth:
    def test_export_requires_auth(self, web_client):
        resp = web_client.post("/memory-pack/export")
        assert resp.status_code in (401, 403)

    def test_import_requires_auth(self, web_client):
        resp = web_client.post("/memory-pack/import?confirm=true", content=b"x")
        assert resp.status_code in (401, 403)


class TestExportEndpoint:
    def test_export_downloads_zip(self, db_path, web_client):
        _seed_user_with_data(db_path)
        token = _login(web_client, "alice")
        resp = web_client.post("/memory-pack/export", headers=_h(token))
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == "application/zip"
        assert "attachment" in resp.headers["content-disposition"]
        assert resp.headers["x-nova-memory-pack-conversations"] == "1"
        # Body is a real pack.
        report = mp.inspect_memory_pack(resp.content)
        assert report.valid is True


class TestImportEndpoints:
    def test_preview_then_import(self, db_path, web_client, tmp_path,
                                 monkeypatch):
        # Build a pack as alice via the export endpoint.
        _seed_user_with_data(db_path)
        alice = _login(web_client, "alice")
        pack = web_client.post(
            "/memory-pack/export", headers=_h(alice),
        ).content

        # Import it into a second user.
        _make_user(db_path, "bob")
        bob = _login(web_client, "bob")

        preview = web_client.post(
            "/memory-pack/import/preview", headers=_h(bob), content=pack,
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["counts"]["memories"]["new"] == 2

        done = web_client.post(
            "/memory-pack/import?confirm=true", headers=_h(bob), content=pack,
        )
        assert done.status_code == 200, done.text
        assert done.json()["outcome"] == "imported"
        assert done.json()["imported"]["conversations"] == 1

    def test_import_requires_confirm(self, db_path, web_client):
        _make_user(db_path, "bob")
        bob = _login(web_client, "bob")
        resp = web_client.post(
            "/memory-pack/import", headers=_h(bob), content=b"PK\x03\x04",
        )
        assert resp.status_code == 400

    def test_import_rejects_invalid_zip(self, db_path, web_client):
        _make_user(db_path, "bob")
        bob = _login(web_client, "bob")
        resp = web_client.post(
            "/memory-pack/import?confirm=true", headers=_h(bob),
            content=b"definitely not a zip",
        )
        assert resp.status_code == 400

    def test_import_rejects_oversize(self, db_path, web_client, monkeypatch):
        monkeypatch.setattr(mp, "MAX_UPLOAD_BYTES", 16)
        _make_user(db_path, "bob")
        bob = _login(web_client, "bob")
        resp = web_client.post(
            "/memory-pack/import?confirm=true", headers=_h(bob),
            content=b"x" * 64,
        )
        assert resp.status_code == 413
