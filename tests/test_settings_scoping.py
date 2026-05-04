"""
Tests for the system/user settings split (issue #107).

Covers:
  * The migration that creates `user_settings` and moves the user-scoped
    keys from the global `settings` table to the legacy admin.
  * The data-layer helpers that keep one user's settings out of another's.
  * The /settings HTTP endpoints: per-user reads, per-user writes, system
    keys staying global, admin-only writes for system keys, and the
    legacy single-user behaviour preserved through the migrated admin.
"""

from __future__ import annotations

import contextlib
import sqlite3
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core import memory as core_memory, settings as core_settings, users
from memory import store as natural_store


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Initialise a fresh nova.db with all tables and migrations run."""
    path = str(tmp_path / "nova.db")
    monkeypatch.setattr(core_memory, "DB_PATH", path)
    monkeypatch.setattr(natural_store, "DB_PATH", path)
    core_memory.initialize_db()
    return path


def _make_user(db_path, username, password="pw", role=users.ROLE_USER):
    with sqlite3.connect(db_path) as conn:
        return users.create_user(conn, username, password, role=role)


# ── Migration ───────────────────────────────────────────────────────────────

class TestMigration:
    def test_fresh_db_creates_user_settings_table(self, db_path):
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='user_settings'"
            ).fetchone()
        assert row is not None

    def test_user_settings_table_has_expected_columns(self, db_path):
        with sqlite3.connect(db_path) as conn:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(user_settings)"
            ).fetchall()}
        assert {"user_id", "key", "value"} <= cols

    def test_user_settings_primary_key_is_user_id_and_key(self, db_path):
        # Storing two values under the same (user, key) must replace, not
        # duplicate. The save helper uses ON CONFLICT, so this also confirms
        # the primary key is wired correctly.
        a = _make_user(db_path, "alice")
        core_settings.save_user_setting(a, "nova_model_name", "first")
        core_settings.save_user_setting(a, "nova_model_name", "second")
        with sqlite3.connect(db_path) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM user_settings "
                "WHERE user_id = ? AND key = 'nova_model_name'", (a,),
            ).fetchone()[0]
        assert n == 1
        assert core_settings.get_user_setting(a, "nova_model_name") == "second"

    def test_migration_is_idempotent(self, db_path):
        # initialize_db ran once via the fixture; running it again must not
        # raise or recreate anything.
        core_memory.initialize_db()
        core_memory.initialize_db()
        with sqlite3.connect(db_path) as conn:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='user_settings'"
            ).fetchall()]
        assert tables == ["user_settings"]

    def test_legacy_user_scoped_keys_move_to_legacy_admin(self, tmp_path, monkeypatch):
        """An upgraded DB carries `nova_model_*` from settings to user_settings."""
        path = str(tmp_path / "nova.db")
        monkeypatch.setattr(core_memory, "DB_PATH", path)
        monkeypatch.setattr(natural_store, "DB_PATH", path)
        monkeypatch.setenv("NOVA_USERNAME", "legacyadmin")
        monkeypatch.setenv("NOVA_PASSWORD", "legacypw")

        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE settings ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO settings(key, value) VALUES "
                "('nova_model_enabled', 'true'), "
                "('nova_model_name', 'legacy-nova'), "
                "('ram_budget', '4096')"
            )

        core_memory.initialize_db()

        with sqlite3.connect(path) as conn:
            admin_id = conn.execute(
                "SELECT id FROM users WHERE username = 'legacyadmin'"
            ).fetchone()[0]
            user_rows = conn.execute(
                "SELECT key, value FROM user_settings WHERE user_id = ?",
                (admin_id,),
            ).fetchall()
            settings_rows = conn.execute(
                "SELECT key, value FROM settings WHERE key IN "
                "('nova_model_enabled','nova_model_name','ram_budget')"
            ).fetchall()

        assert sorted(user_rows) == [
            ("nova_model_enabled", "true"),
            ("nova_model_name", "legacy-nova"),
        ]
        # ram_budget stays in the global settings table.
        assert settings_rows == [("ram_budget", "4096")]

    def test_legacy_admin_reads_existing_settings_after_migration(
        self, tmp_path, monkeypatch,
    ):
        """The migrated admin still sees the same `nova_model_*` values."""
        path = str(tmp_path / "nova.db")
        monkeypatch.setattr(core_memory, "DB_PATH", path)
        monkeypatch.setattr(natural_store, "DB_PATH", path)
        monkeypatch.setenv("NOVA_USERNAME", "legacyadmin")
        monkeypatch.setenv("NOVA_PASSWORD", "legacypw")

        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE settings ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO settings(key, value) VALUES "
                "('nova_model_enabled', 'true'), "
                "('nova_model_name', 'legacy-nova')"
            )

        core_memory.initialize_db()

        admin_id = users.get_legacy_admin_id(path)
        assert admin_id is not None
        assert core_settings.get_user_setting(
            admin_id, "nova_model_enabled"
        ) == "true"
        assert core_settings.get_user_setting(
            admin_id, "nova_model_name"
        ) == "legacy-nova"

    def test_migration_fresh_db_has_no_user_setting_rows(self, db_path):
        with sqlite3.connect(db_path) as conn:
            n = conn.execute("SELECT COUNT(*) FROM user_settings").fetchone()[0]
        assert n == 0


# ── Data-layer scoping ──────────────────────────────────────────────────────

class TestUserSettingsIsolation:
    def test_save_then_get_round_trip(self, db_path):
        a = _make_user(db_path, "alice")
        core_settings.save_user_setting(a, "nova_model_name", "alice-nova")
        assert core_settings.get_user_setting(
            a, "nova_model_name"
        ) == "alice-nova"

    def test_unknown_key_returns_default(self, db_path):
        a = _make_user(db_path, "alice")
        assert core_settings.get_user_setting(a, "missing", "fallback") == "fallback"
        assert core_settings.get_user_setting(a, "missing") == ""

    def test_user_a_update_does_not_affect_user_b(self, db_path):
        a = _make_user(db_path, "alice")
        b = _make_user(db_path, "bob")
        core_settings.save_user_setting(a, "nova_model_enabled", "true")
        core_settings.save_user_setting(a, "nova_model_name", "alice-nova")

        assert core_settings.get_user_setting(b, "nova_model_enabled", "false") == "false"
        assert core_settings.get_user_setting(b, "nova_model_name", "default") == "default"

    def test_user_b_can_have_different_value_for_same_key(self, db_path):
        a = _make_user(db_path, "alice")
        b = _make_user(db_path, "bob")
        core_settings.save_user_setting(a, "nova_model_name", "alice-nova")
        core_settings.save_user_setting(b, "nova_model_name", "bob-nova")
        assert core_settings.get_user_setting(a, "nova_model_name") == "alice-nova"
        assert core_settings.get_user_setting(b, "nova_model_name") == "bob-nova"

    def test_save_user_setting_updates_existing_value(self, db_path):
        a = _make_user(db_path, "alice")
        core_settings.save_user_setting(a, "nova_model_name", "v1")
        core_settings.save_user_setting(a, "nova_model_name", "v2")
        assert core_settings.get_user_setting(a, "nova_model_name") == "v2"


class TestSystemSettingsRemainGlobal:
    def test_system_setting_round_trip(self, db_path):
        core_settings.save_system_setting("ram_budget", "4096")
        assert core_settings.get_system_setting("ram_budget") == "4096"

    def test_system_setting_does_not_leak_into_user_settings(self, db_path):
        a = _make_user(db_path, "alice")
        core_settings.save_system_setting("ram_budget", "4096")
        # A user reading the same key from user_settings sees nothing.
        assert core_settings.get_user_setting(a, "ram_budget", "missing") == "missing"

    def test_user_setting_does_not_leak_into_system_settings(self, db_path):
        a = _make_user(db_path, "alice")
        core_settings.save_user_setting(a, "nova_model_name", "alice-nova")
        # System reads of a user-scoped key see nothing.
        assert core_settings.get_system_setting("nova_model_name", "missing") == "missing"

    def test_user_setting_keys_constant_lists_expected_keys(self):
        assert "nova_model_enabled" in core_settings.USER_SETTING_KEYS
        assert "nova_model_name" in core_settings.USER_SETTING_KEYS
        # System keys are not in the user set.
        assert "ram_budget" not in core_settings.USER_SETTING_KEYS
        assert "last_model_update" not in core_settings.USER_SETTING_KEYS


# ── HTTP endpoint scoping ───────────────────────────────────────────────────

@pytest.fixture
def web_client(db_path, monkeypatch):
    """A TestClient backed by the temp DB, with background jobs suppressed."""
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


class TestSettingsEndpoint:
    def test_get_returns_user_specific_nova_model_values(self, db_path, web_client):
        a = _make_user(db_path, "alice")
        _make_user(db_path, "bob")
        core_settings.save_user_setting(a, "nova_model_enabled", "true")
        core_settings.save_user_setting(a, "nova_model_name", "alice-nova")
        # Bob has nothing set; he should see the defaults.

        a_token = _login(web_client, "alice")
        b_token = _login(web_client, "bob")

        a_body = web_client.get("/settings", headers=_h(a_token)).json()
        b_body = web_client.get("/settings", headers=_h(b_token)).json()

        assert a_body["nova_model_enabled"] is True
        assert a_body["nova_model_name"] == "alice-nova"
        assert b_body["nova_model_enabled"] is False
        # NOVA_MODEL_DEFAULT_NAME is the fallback.
        from config import NOVA_MODEL_DEFAULT_NAME
        assert b_body["nova_model_name"] == NOVA_MODEL_DEFAULT_NAME

    def test_get_returns_system_settings_globally(self, db_path, web_client):
        _make_user(db_path, "alice")
        _make_user(db_path, "bob")
        core_settings.save_system_setting("ram_budget", "8192")

        a_token = _login(web_client, "alice")
        b_token = _login(web_client, "bob")

        a_body = web_client.get("/settings", headers=_h(a_token)).json()
        b_body = web_client.get("/settings", headers=_h(b_token)).json()

        assert a_body["ram_budget"] == "8192"
        assert b_body["ram_budget"] == "8192"

    def test_post_user_setting_does_not_affect_other_user(self, db_path, web_client):
        a = _make_user(db_path, "alice")
        b = _make_user(db_path, "bob")
        a_token = _login(web_client, "alice")
        b_token = _login(web_client, "bob")

        resp = web_client.post(
            "/settings",
            json={"nova_model_name": "alice-nova", "nova_model_enabled": True},
            headers=_h(a_token),
        )
        assert resp.status_code == 200

        # Alice sees her values...
        a_body = web_client.get("/settings", headers=_h(a_token)).json()
        assert a_body["nova_model_enabled"] is True
        assert a_body["nova_model_name"] == "alice-nova"

        # ...Bob does not.
        b_body = web_client.get("/settings", headers=_h(b_token)).json()
        assert b_body["nova_model_enabled"] is False
        from config import NOVA_MODEL_DEFAULT_NAME
        assert b_body["nova_model_name"] == NOVA_MODEL_DEFAULT_NAME

        # The DB row exists only for Alice.
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT user_id, key FROM user_settings ORDER BY user_id, key"
            ).fetchall()
        assert (a, "nova_model_enabled") in rows
        assert (a, "nova_model_name") in rows
        assert all(r[0] != b for r in rows)

    def test_post_system_setting_requires_admin(self, db_path, web_client):
        """Non-admins are 403'd when changing ram_budget; admins are not."""
        _make_user(db_path, "alice", role=users.ROLE_USER)
        _make_user(db_path, "boss", role=users.ROLE_ADMIN)
        a_token = _login(web_client, "alice")
        boss_token = _login(web_client, "boss")

        # Non-admin: refused, value unchanged.
        resp = web_client.post(
            "/settings", json={"ram_budget": 4096}, headers=_h(a_token)
        )
        assert resp.status_code == 403
        assert core_settings.get_system_setting("ram_budget", "2048") == "2048"

        # Admin: accepted.
        resp = web_client.post(
            "/settings", json={"ram_budget": 4096}, headers=_h(boss_token)
        )
        assert resp.status_code == 200
        assert core_settings.get_system_setting("ram_budget") == "4096"

    def test_non_admin_can_still_change_their_own_user_settings(
        self, db_path, web_client,
    ):
        """A 403 on system settings must not block per-user writes."""
        _make_user(db_path, "alice", role=users.ROLE_USER)
        a_token = _login(web_client, "alice")

        resp = web_client.post(
            "/settings",
            json={"nova_model_enabled": True, "nova_model_name": "x"},
            headers=_h(a_token),
        )
        assert resp.status_code == 200

        body = web_client.get("/settings", headers=_h(a_token)).json()
        assert body["nova_model_enabled"] is True
        assert body["nova_model_name"] == "x"

    def test_legacy_admin_single_user_flow_preserved(self, tmp_path, monkeypatch):
        """End-to-end: a pre-#107 single-user DB still works after migration."""
        path = str(tmp_path / "nova.db")
        monkeypatch.setattr(core_memory, "DB_PATH", path)
        monkeypatch.setattr(natural_store, "DB_PATH", path)
        monkeypatch.setenv("NOVA_USERNAME", "nova")
        monkeypatch.setenv("NOVA_PASSWORD", "nova")

        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE settings ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO settings(key, value) VALUES "
                "('nova_model_enabled', 'true'), "
                "('nova_model_name', 'pre-107-nova'), "
                "('ram_budget', '4096')"
            )

        core_memory.initialize_db()
        from core.rate_limiter import _login_limiter
        _login_limiter._store.clear()

        import web
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("web.initialize_db"))
            stack.enter_context(patch("web.learn_from_feeds"))
            stack.enter_context(patch("web.scheduler", MagicMock()))
            with TestClient(web.app, raise_server_exceptions=True) as client:
                token = _login(client, "nova", "nova")
                body = client.get("/settings", headers=_h(token)).json()

        assert body["nova_model_enabled"] is True
        assert body["nova_model_name"] == "pre-107-nova"
        assert body["ram_budget"] == "4096"

    def test_post_extra_unknown_field_is_rejected(self, db_path, web_client):
        """`extra="forbid"` prevents drive-by writes via unrecognised keys."""
        _make_user(db_path, "alice")
        a_token = _login(web_client, "alice")

        resp = web_client.post(
            "/settings", json={"unknown_key": "x"}, headers=_h(a_token)
        )
        assert resp.status_code == 422
