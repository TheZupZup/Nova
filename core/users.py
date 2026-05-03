"""
Users table and default-admin migration.

This is the schema-and-seed foundation for multi-user support
(see docs/multi-user-architecture.md, issue #103).

Scope of this module is intentionally narrow:
  * Create the `users` table if missing.
  * Seed a single admin row from NOVA_USERNAME / NOVA_PASSWORD when the
    table is empty.
  * Mark schema_version=2 in the existing global `settings` table.

Anything else from the architecture doc — JWT identity, per-user data
scoping, family controls, model registry, admin endpoints, UI — is
deferred to issues #104 and beyond. Login behavior is unchanged: this
module only adds rows; it does not alter `core/auth.py`.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import bcrypt

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2
SCHEMA_VERSION_KEY = "schema_version"
ROLE_ADMIN = "admin"
ROLE_USER = "user"


_USERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT    NOT NULL UNIQUE,
    password_hash   TEXT    NOT NULL,
    role            TEXT    NOT NULL CHECK (role IN ('admin', 'user')),
    is_restricted   INTEGER NOT NULL DEFAULT 0,
    token_version   INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL,
    disabled_at     TEXT
)
"""

_USERS_USERNAME_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _ensure_settings_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS settings ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )


def _read_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (SCHEMA_VERSION_KEY,)
    ).fetchone()
    if row is None:
        return 1
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 1


def _write_schema_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SCHEMA_VERSION_KEY, str(version)),
    )


def create_users_table(conn: sqlite3.Connection) -> None:
    """Create the users table and its index. Idempotent."""
    conn.execute(_USERS_TABLE_SQL)
    conn.execute(_USERS_USERNAME_INDEX_SQL)


def count_users(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def get_user_by_username(
    conn: sqlite3.Connection, username: str
) -> Optional[sqlite3.Row]:
    prev_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
    finally:
        conn.row_factory = prev_factory


def create_user(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    role: str = ROLE_USER,
    is_restricted: bool = False,
) -> int:
    if role not in (ROLE_ADMIN, ROLE_USER):
        raise ValueError(f"invalid role: {role!r}")
    if not username:
        raise ValueError("username must be non-empty")
    if not password:
        raise ValueError("password must be non-empty")

    cur = conn.execute(
        "INSERT INTO users (username, password_hash, role, is_restricted, "
        "token_version, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            username,
            _hash_password(password),
            role,
            1 if is_restricted else 0,
            1,
            _now_iso(),
        ),
    )
    return cur.lastrowid


def get_legacy_admin_id(db_path: str) -> Optional[int]:
    """
    Return the user_id of the first user in the table — the seeded admin.

    Used by code paths that have no authenticated user (CLI in main.py,
    background learner) to attribute writes to the migrated default admin,
    preserving single-user behaviour after the multi-user migration.

    Returns None if the users table is empty or missing.
    """
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT id FROM users ORDER BY id ASC LIMIT 1"
            ).fetchone()
    except sqlite3.DatabaseError:
        return None
    return int(row[0]) if row else None


def seed_default_admin(
    conn: sqlite3.Connection,
    username: str,
    password: str,
) -> Optional[int]:
    """
    Insert the legacy admin row when the users table is empty.

    Returns the new user id, or None if the table already had at least one
    row (in which case this is a no-op — the seed has already run).
    """
    if count_users(conn) > 0:
        return None
    user_id = create_user(conn, username, password, role=ROLE_ADMIN)
    logger.info("Seeded default admin user '%s' (id=%d)", username, user_id)
    return user_id


def _backup_db(db_path: str) -> Optional[str]:
    """
    Copy nova.db to nova.db.preupgrade-<UTC timestamp> before a schema bump.

    Returns the backup path, or None if the source DB does not exist yet
    (fresh install — nothing to back up). Raises OSError on failure so the
    caller can refuse to proceed.
    """
    if not os.path.exists(db_path):
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = f"{db_path}.preupgrade-{stamp}"
    shutil.copy2(db_path, backup_path)
    return backup_path


def migrate(
    db_path: str,
    admin_username: Optional[str] = None,
    admin_password: Optional[str] = None,
) -> None:
    """
    Run the users-table + default-admin migration.

    Idempotent:
      * If schema_version >= 2 and the admin row already exists, this is a
        no-op (no backup, no writes).
      * If the table exists but is empty, the admin is re-seeded.
      * If schema_version is missing it is treated as 1.

    Backup:
      * A timestamped copy of the DB is taken before any schema change,
        only when the DB file already exists. A failure to back up aborts
        the migration before any write.

    Login behavior is not affected — `core/auth.py` continues to read
    NOVA_USERNAME / NOVA_PASSWORD from the environment.
    """
    if admin_username is None:
        admin_username = os.getenv("NOVA_USERNAME", "nova")
    if admin_password is None:
        admin_password = os.getenv("NOVA_PASSWORD", "nova")

    needs_backup = os.path.exists(db_path)
    with sqlite3.connect(db_path) as probe:
        _ensure_settings_table(probe)
        current_version = _read_schema_version(probe)
        users_table_exists = probe.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone() is not None
        admin_row_exists = False
        if users_table_exists:
            admin_row_exists = probe.execute(
                "SELECT 1 FROM users LIMIT 1"
            ).fetchone() is not None

    if (
        current_version >= SCHEMA_VERSION
        and users_table_exists
        and admin_row_exists
    ):
        return

    if needs_backup:
        try:
            backup_path = _backup_db(db_path)
        except OSError as exc:
            raise RuntimeError(
                f"refusing to migrate: backup of {db_path} failed: {exc}"
            ) from exc
        if backup_path is not None:
            logger.info("Pre-migration backup written to %s", backup_path)

    with sqlite3.connect(db_path) as conn:
        _ensure_settings_table(conn)
        create_users_table(conn)
        seed_default_admin(conn, admin_username, admin_password)
        _write_schema_version(conn, SCHEMA_VERSION)
