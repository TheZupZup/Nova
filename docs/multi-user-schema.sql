-- Multi-user / family-controls schema sketch.
--
-- STATUS: design plan only. None of this is executed by Nova today.
-- This file exists alongside docs/multi-user-architecture.md so that
-- reviewers of issues #103-#112 have a concrete schema to argue against
-- before any migration code is written.
--
-- Conventions:
--   * Times are ISO-8601 strings, matching the existing tables in
--     core/memory.py and memory/store.py.
--   * Booleans are stored as INTEGER 0/1 to match SQLite idiom.
--   * Foreign keys assume `PRAGMA foreign_keys = ON;` at connection time.
--   * `IF NOT EXISTS` everywhere so the migration is idempotent.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- 1. Identity
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT    NOT NULL UNIQUE,
    password_hash   TEXT    NOT NULL,           -- bcrypt
    role            TEXT    NOT NULL CHECK (role IN ('admin', 'user')),
    is_restricted   INTEGER NOT NULL DEFAULT 0, -- child / restricted account
    token_version   INTEGER NOT NULL DEFAULT 1, -- bumped on password/role change
    created_at      TEXT    NOT NULL,
    disabled_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);

-- ---------------------------------------------------------------------------
-- 2. Family controls (only present for restricted users)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS family_controls (
    user_id                 INTEGER PRIMARY KEY
                                    REFERENCES users(id) ON DELETE CASCADE,
    daily_message_limit     INTEGER,            -- NULL = no limit
    allowed_modes           TEXT NOT NULL DEFAULT 'chat',  -- CSV subset of {auto,chat,code,deep}
    web_search_enabled      INTEGER NOT NULL DEFAULT 0,
    weather_enabled         INTEGER NOT NULL DEFAULT 1,
    memory_save_enabled     INTEGER NOT NULL DEFAULT 0,
    memory_import_enabled   INTEGER NOT NULL DEFAULT 0,
    max_prompt_chars        INTEGER NOT NULL DEFAULT 2000,
    quiet_hours_start       TEXT,               -- HH:MM, host local TZ; enforced later
    quiet_hours_end         TEXT
);

-- Daily message accounting. One row per (user, UTC date).
-- A simple counter is enough for v1; we do not need per-message rows here
-- because the `messages` table already records every message.
CREATE TABLE IF NOT EXISTS user_daily_usage (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    usage_date  TEXT    NOT NULL,               -- YYYY-MM-DD, UTC
    message_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, usage_date)
);

-- ---------------------------------------------------------------------------
-- 3. Model registry and per-user allowlist
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS model_registry (
    name            TEXT PRIMARY KEY,           -- e.g. 'gemma3:1b' (admin/technical)
    display_name    TEXT NOT NULL,              -- e.g. 'General chat' (user-facing)
    family          TEXT,                       -- e.g. 'gemma', 'qwen'
    size_bytes      INTEGER,
    is_admin_only   INTEGER NOT NULL DEFAULT 0, -- routing/classifier models
    installed_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_allowed_models (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    model_name  TEXT    NOT NULL REFERENCES model_registry(name) ON DELETE CASCADE,
    PRIMARY KEY (user_id, model_name)
);

-- Background pull jobs. State is one of: queued, running, done, error.
CREATE TABLE IF NOT EXISTS model_pull_jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name      TEXT    NOT NULL,
    requested_by    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    state           TEXT    NOT NULL CHECK (state IN ('queued','running','done','error')),
    bytes_done      INTEGER NOT NULL DEFAULT 0,
    bytes_total     INTEGER,
    error_message   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- 4. Per-user settings
-- ---------------------------------------------------------------------------
-- The existing global `settings` table stays as-is for host-wide config
-- (RAM budget, default routing model, schema_version). Per-user preferences
-- move here.

CREATE TABLE IF NOT EXISTS user_settings (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key         TEXT    NOT NULL,
    value       TEXT    NOT NULL,
    PRIMARY KEY (user_id, key)
);

-- ---------------------------------------------------------------------------
-- 5. Ownership backfill on existing tables
-- ---------------------------------------------------------------------------
-- These ALTERs run after a row in `users` exists for the legacy admin. The
-- migration backfills `user_id` to that user's id, then in a subsequent step
-- enforces NOT NULL by recreating the table (SQLite cannot ALTER NOT NULL
-- in place).
--
-- Shown here as nullable ALTER plus an UPDATE; the recreate-table step is
-- omitted from this sketch for brevity but is part of the migration plan.

ALTER TABLE conversations    ADD COLUMN user_id INTEGER REFERENCES users(id);
ALTER TABLE memories         ADD COLUMN user_id INTEGER REFERENCES users(id);
ALTER TABLE natural_memories ADD COLUMN user_id INTEGER REFERENCES users(id);

-- Backfill: every existing row belongs to the legacy admin (id resolved at
-- migration time).
-- UPDATE conversations    SET user_id = :legacy_admin_id WHERE user_id IS NULL;
-- UPDATE memories         SET user_id = :legacy_admin_id WHERE user_id IS NULL;
-- UPDATE natural_memories SET user_id = :legacy_admin_id WHERE user_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_conversations_user    ON conversations(user_id);
CREATE INDEX IF NOT EXISTS idx_memories_user         ON memories(user_id);
CREATE INDEX IF NOT EXISTS idx_natural_memories_user ON natural_memories(user_id);

-- ---------------------------------------------------------------------------
-- 6. Audit log (issue #112)
-- ---------------------------------------------------------------------------
-- Append-only. Records admin actions only. Never records message content.

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id    INTEGER NOT NULL REFERENCES users(id),
    action      TEXT    NOT NULL,  -- e.g. 'user.create', 'model.pull', 'user.disable'
    target      TEXT,               -- free-form identifier (username, model name, ...)
    detail      TEXT,               -- short human-readable description, no chat content
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_created ON admin_audit_log(created_at);

-- ---------------------------------------------------------------------------
-- Schema version marker
-- ---------------------------------------------------------------------------
-- Stored in the existing `settings` table:
--   INSERT OR REPLACE INTO settings(key, value) VALUES ('schema_version', '2');
