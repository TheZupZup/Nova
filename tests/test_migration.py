"""Tests for the users-table + default-admin migration (issue #103)."""

import glob
import os
import sqlite3

import bcrypt
import pytest

from core import users


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "nova.db")


def _table_exists(path: str, name: str) -> bool:
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None


def _schema_version(path: str) -> int:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'schema_version'"
        ).fetchone()
    return int(row[0]) if row else 0


def test_migrate_on_fresh_db_creates_table_and_seeds_admin(db_path):
    users.migrate(db_path, admin_username="admin", admin_password="pw")

    assert _table_exists(db_path, "users")
    assert _schema_version(db_path) == users.SCHEMA_VERSION

    with sqlite3.connect(db_path) as conn:
        row = users.get_user_by_username(conn, "admin")
    assert row is not None
    assert row["role"] == "admin"
    assert bcrypt.checkpw(b"pw", row["password_hash"].encode())


def test_migrate_is_idempotent(db_path):
    users.migrate(db_path, admin_username="admin", admin_password="pw")
    users.migrate(db_path, admin_username="admin", admin_password="pw")
    users.migrate(db_path, admin_username="admin", admin_password="pw")

    with sqlite3.connect(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    assert n == 1


def test_migrate_creates_pre_upgrade_backup_when_db_exists(tmp_path):
    db_path = str(tmp_path / "nova.db")

    # Pre-existing DB with some legacy content.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('legacy_key', 'legacy_value')"
        )

    users.migrate(db_path, admin_username="admin", admin_password="pw")

    backups = glob.glob(str(tmp_path / "nova.db.preupgrade-*"))
    assert len(backups) == 1, f"expected one backup, found {backups!r}"

    # Backup preserves the legacy row exactly as it was pre-migration.
    with sqlite3.connect(backups[0]) as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key='legacy_key'"
        ).fetchone()
    assert row is not None and row[0] == "legacy_value"


def test_migrate_skips_backup_when_already_at_target_version(tmp_path):
    db_path = str(tmp_path / "nova.db")
    users.migrate(db_path, admin_username="admin", admin_password="pw")

    # Remove any backup created on the first run.
    for b in glob.glob(str(tmp_path / "nova.db.preupgrade-*")):
        os.remove(b)

    users.migrate(db_path, admin_username="admin", admin_password="pw")

    backups = glob.glob(str(tmp_path / "nova.db.preupgrade-*"))
    assert backups == []


def test_migrate_does_not_overwrite_existing_admin_password(db_path):
    users.migrate(db_path, admin_username="admin", admin_password="original")
    # Re-run with a different password — should NOT overwrite.
    users.migrate(db_path, admin_username="admin", admin_password="changed")

    with sqlite3.connect(db_path) as conn:
        row = users.get_user_by_username(conn, "admin")
    assert bcrypt.checkpw(b"original", row["password_hash"].encode())
    assert not bcrypt.checkpw(b"changed", row["password_hash"].encode())


def test_migrate_preserves_existing_legacy_tables(tmp_path):
    """The migration must not touch existing single-user data."""
    db_path = str(tmp_path / "nova.db")

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE memories ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "category TEXT NOT NULL, content TEXT NOT NULL, "
            "created TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO memories(category, content, created) "
            "VALUES (?, ?, ?)",
            ("knowledge", "the sky is blue", "2024-01-01T00:00:00Z"),
        )

    users.migrate(db_path, admin_username="admin", admin_password="pw")

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT category, content FROM memories"
        ).fetchall()
    assert rows == [("knowledge", "the sky is blue")]


def test_initialize_db_runs_migration(tmp_path, monkeypatch):
    """core.memory.initialize_db should pick up the users migration."""
    db_path = str(tmp_path / "nova.db")
    monkeypatch.setenv("NOVA_USERNAME", "envadmin")
    monkeypatch.setenv("NOVA_PASSWORD", "envpw")

    from core import memory as core_memory
    from memory import store as natural_store

    monkeypatch.setattr(core_memory, "DB_PATH", db_path)
    monkeypatch.setattr(natural_store, "DB_PATH", db_path)

    core_memory.initialize_db()

    assert _table_exists(db_path, "users")
    with sqlite3.connect(db_path) as conn:
        row = users.get_user_by_username(conn, "envadmin")
    assert row is not None
    assert row["role"] == "admin"
