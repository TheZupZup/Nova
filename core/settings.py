"""
System and per-user settings (issue #107).

Today the `settings` table is a flat key/value store shared across every
user. This module splits ownership so that:

  * Host-wide / admin-level keys (RAM budget, model-update metadata,
    schema_version) stay in the existing `settings` table.
  * Per-user preferences (the user's Nova-model preference) live in a
    new `user_settings(user_id, key)` table.

The split preserves single-user behaviour through the migrated default
admin: a fresh DB starts empty, an upgraded DB has its previously-global
user-scoped keys moved under the legacy admin's id at startup.

Family controls, model registry, and admin UI all live in later issues
(#108–#112). This module only owns settings storage.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

# Keys that belong to a user, not to the host. Anything not in this set
# is treated as a system/global setting.
USER_SETTING_KEYS: frozenset[str] = frozenset({
    "nova_model_enabled",
    "nova_model_name",
})


def is_user_setting(key: str) -> bool:
    return key in USER_SETTING_KEYS


def _db_path() -> str:
    # Late import: core.memory wires up the broader data layer, and importing
    # it at module level would create a cycle with core.memory.initialize_db.
    from core.memory import DB_PATH
    return DB_PATH


def _open(db_path: Optional[str] = None) -> sqlite3.Connection:
    return sqlite3.connect(db_path or _db_path())


# ── System (global) settings ────────────────────────────────────────────────

def get_system_setting(key: str, default: str = "") -> str:
    """Read a host-wide setting from the `settings` table."""
    try:
        with _open() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else default
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        return default


def save_system_setting(key: str, value: str) -> None:
    """Write a host-wide setting to the `settings` table."""
    with _open() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# ── Per-user settings ───────────────────────────────────────────────────────

def get_user_setting(user_id: int, key: str, default: str = "") -> str:
    """Read a per-user setting; returns `default` if the user has no value."""
    try:
        with _open() as conn:
            row = conn.execute(
                "SELECT value FROM user_settings WHERE user_id = ? AND key = ?",
                (user_id, key),
            ).fetchone()
        return row[0] if row else default
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        return default


def save_user_setting(user_id: int, key: str, value: str) -> None:
    """Write a per-user setting. Other users are never affected."""
    with _open() as conn:
        conn.execute(
            "INSERT INTO user_settings (user_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
            (user_id, key, value),
        )


# ── Migration ───────────────────────────────────────────────────────────────

def migrate_user_settings(db_path: str) -> None:
    """
    Create the `user_settings` table and move user-scoped keys out of the
    global `settings` table to the legacy admin's id.

    Idempotent: the existence of the `user_settings` table is the marker;
    a second call returns immediately. Requires the `users` table to exist
    with at least one row (the legacy admin), so it must run after
    `core.users.migrate()`.
    """
    with sqlite3.connect(db_path) as conn:
        already_migrated = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='user_settings'"
        ).fetchone()
        if already_migrated:
            return

        row = conn.execute(
            "SELECT id FROM users ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if row is None:
            raise RuntimeError(
                "cannot migrate user_settings: users table is empty; "
                "users.migrate() must run first"
            )
        legacy_admin_id = int(row[0])

        conn.execute(
            "CREATE TABLE user_settings ("
            "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
            "key     TEXT    NOT NULL, "
            "value   TEXT    NOT NULL, "
            "PRIMARY KEY (user_id, key))"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_settings_user_id "
            "ON user_settings(user_id)"
        )

        for key in USER_SETTING_KEYS:
            existing = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
            if existing is None:
                continue
            conn.execute(
                "INSERT INTO user_settings (user_id, key, value) "
                "VALUES (?, ?, ?)",
                (legacy_admin_id, key, existing[0]),
            )
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
