"""
Role-based family controls (issue #108).

This is the server-side enforcement layer for admin / user / restricted
accounts. Every protected feature reads a `Policy` resolved from the
caller's identity, and the web layer surfaces 403 / 429 responses when a
request fails the policy check. The frontend never decides; it only
mirrors what the backend allows.

Resolution rules:
  * `admin`                 → full access (`ADMIN_POLICY`).
  * `user`, not restricted  → permissive defaults (`DEFAULT_USER_POLICY`).
  * `user`, `is_restricted` → defaults from `DEFAULT_RESTRICTED_POLICY`,
                              overridden field-by-field by the row in
                              `family_controls` if one exists.

Storage:
  * `family_controls(user_id PK, …)` — one row per restricted user.
    Absent rows fall back to `DEFAULT_RESTRICTED_POLICY`.
  * `user_daily_usage(user_id, usage_date PK)` — counter table for the
    daily message limit. `record_message` is the single writer.

Out of scope for this issue (#108):
  * Admin endpoints to manage `family_controls` rows (#109).
  * Model registry / per-user model allowlist (#110–#112).
  * Surveillance dashboards / shared family memory.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from core.users import ROLE_ADMIN, ROLE_USER  # noqa: F401

KNOWN_MODES: frozenset[str] = frozenset({"auto", "chat", "code", "deep"})

# A `max_prompt_chars` of 0 means "no cap"; anything else is the strict
# upper bound on the user-supplied message length.
NO_PROMPT_CAP = 0
NO_DAILY_LIMIT: Optional[int] = None


@dataclass(frozen=True)
class Policy:
    """Resolved feature/limit set for one request."""

    role: str
    is_restricted: bool
    allowed_modes: frozenset[str]
    web_search_enabled: bool
    weather_enabled: bool
    memory_save_enabled: bool
    memory_import_enabled: bool
    max_prompt_chars: int = NO_PROMPT_CAP
    daily_message_limit: Optional[int] = NO_DAILY_LIMIT

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    def mode_allowed(self, mode: str) -> bool:
        return mode in self.allowed_modes

    def prompt_too_long(self, text: str) -> bool:
        return self.max_prompt_chars > 0 and len(text) > self.max_prompt_chars


ADMIN_POLICY = Policy(
    role=ROLE_ADMIN,
    is_restricted=False,
    allowed_modes=KNOWN_MODES,
    web_search_enabled=True,
    weather_enabled=True,
    memory_save_enabled=True,
    memory_import_enabled=True,
    max_prompt_chars=NO_PROMPT_CAP,
    daily_message_limit=NO_DAILY_LIMIT,
)

DEFAULT_USER_POLICY = Policy(
    role=ROLE_USER,
    is_restricted=False,
    allowed_modes=KNOWN_MODES,
    web_search_enabled=True,
    weather_enabled=True,
    memory_save_enabled=True,
    memory_import_enabled=True,
    max_prompt_chars=NO_PROMPT_CAP,
    daily_message_limit=NO_DAILY_LIMIT,
)

DEFAULT_RESTRICTED_POLICY = Policy(
    role=ROLE_USER,
    is_restricted=True,
    allowed_modes=frozenset({"chat"}),
    web_search_enabled=False,
    weather_enabled=True,
    memory_save_enabled=False,
    memory_import_enabled=False,
    max_prompt_chars=2000,
    daily_message_limit=200,
)


# ── Schema ──────────────────────────────────────────────────────────────────

_FAMILY_CONTROLS_SQL = """
CREATE TABLE IF NOT EXISTS family_controls (
    user_id               INTEGER PRIMARY KEY
                                  REFERENCES users(id) ON DELETE CASCADE,
    daily_message_limit   INTEGER,
    allowed_modes         TEXT    NOT NULL DEFAULT 'chat',
    web_search_enabled    INTEGER NOT NULL DEFAULT 0,
    weather_enabled       INTEGER NOT NULL DEFAULT 1,
    memory_save_enabled   INTEGER NOT NULL DEFAULT 0,
    memory_import_enabled INTEGER NOT NULL DEFAULT 0,
    max_prompt_chars      INTEGER NOT NULL DEFAULT 2000
)
"""

_USER_DAILY_USAGE_SQL = """
CREATE TABLE IF NOT EXISTS user_daily_usage (
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    usage_date    TEXT    NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, usage_date)
)
"""


def _open(db_path: Optional[str] = None) -> sqlite3.Connection:
    if db_path is None:
        from core.memory import DB_PATH
        db_path = DB_PATH
    return sqlite3.connect(db_path)


def migrate_family_controls(db_path: str) -> None:
    """Create the `family_controls` and `user_daily_usage` tables. Idempotent."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(_FAMILY_CONTROLS_SQL)
        conn.execute(_USER_DAILY_USAGE_SQL)


# ── CSV helpers for `allowed_modes` ─────────────────────────────────────────

def _modes_to_csv(modes) -> str:
    cleaned = sorted({m for m in modes if m in KNOWN_MODES})
    if not cleaned:
        cleaned = ["chat"]
    return ",".join(cleaned)


def _csv_to_modes(value: str) -> frozenset[str]:
    parts = {p.strip() for p in (value or "").split(",") if p.strip()}
    return frozenset(parts & KNOWN_MODES) or frozenset({"chat"})


# ── Policy resolution ───────────────────────────────────────────────────────

def _read_family_controls(
    conn: sqlite3.Connection, user_id: int
) -> Optional[sqlite3.Row]:
    prev = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM family_controls WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    finally:
        conn.row_factory = prev


def _policy_from_row(row: sqlite3.Row) -> Policy:
    return Policy(
        role=ROLE_USER,
        is_restricted=True,
        allowed_modes=_csv_to_modes(row["allowed_modes"]),
        web_search_enabled=bool(row["web_search_enabled"]),
        weather_enabled=bool(row["weather_enabled"]),
        memory_save_enabled=bool(row["memory_save_enabled"]),
        memory_import_enabled=bool(row["memory_import_enabled"]),
        max_prompt_chars=int(row["max_prompt_chars"]),
        daily_message_limit=(
            int(row["daily_message_limit"])
            if row["daily_message_limit"] is not None
            else NO_DAILY_LIMIT
        ),
    )


def get_policy(user, db_path: Optional[str] = None) -> Policy:
    """
    Resolve the policy for `user`.

    Accepts any object exposing `role` and `is_restricted`. Admins and
    non-restricted users are resolved without touching the database; only
    a restricted user's row in `family_controls` triggers a read.
    """
    if user is None:
        # No identity → behave as the most permissive user (CLI / learner
        # paths run as the legacy admin and pass `None` rather than a
        # CurrentUser). Family controls do not apply here.
        return ADMIN_POLICY
    if getattr(user, "role", None) == ROLE_ADMIN:
        return ADMIN_POLICY
    if not getattr(user, "is_restricted", False):
        return DEFAULT_USER_POLICY
    user_id = int(getattr(user, "id"))
    try:
        with _open(db_path) as conn:
            row = _read_family_controls(conn, user_id)
    except sqlite3.DatabaseError:
        row = None
    if row is None:
        return DEFAULT_RESTRICTED_POLICY
    return _policy_from_row(row)


def set_family_controls(
    user_id: int,
    *,
    allowed_modes=None,
    web_search_enabled: Optional[bool] = None,
    weather_enabled: Optional[bool] = None,
    memory_save_enabled: Optional[bool] = None,
    memory_import_enabled: Optional[bool] = None,
    max_prompt_chars: Optional[int] = None,
    daily_message_limit: Optional[int] = None,
    db_path: Optional[str] = None,
) -> None:
    """
    Upsert the `family_controls` row for `user_id`.

    Fields left as `None` fall back to the schema defaults on insert and
    are left untouched on update. The admin endpoints that drive this
    helper land in #109; #108 provides the storage path so enforcement
    has something to read.
    """
    with _open(db_path) as conn:
        existing = _read_family_controls(conn, user_id)
        if existing is None:
            conn.execute(
                "INSERT INTO family_controls ("
                "user_id, daily_message_limit, allowed_modes, "
                "web_search_enabled, weather_enabled, "
                "memory_save_enabled, memory_import_enabled, "
                "max_prompt_chars) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    daily_message_limit,
                    _modes_to_csv(
                        allowed_modes
                        if allowed_modes is not None
                        else DEFAULT_RESTRICTED_POLICY.allowed_modes
                    ),
                    int(
                        web_search_enabled
                        if web_search_enabled is not None
                        else DEFAULT_RESTRICTED_POLICY.web_search_enabled
                    ),
                    int(
                        weather_enabled
                        if weather_enabled is not None
                        else DEFAULT_RESTRICTED_POLICY.weather_enabled
                    ),
                    int(
                        memory_save_enabled
                        if memory_save_enabled is not None
                        else DEFAULT_RESTRICTED_POLICY.memory_save_enabled
                    ),
                    int(
                        memory_import_enabled
                        if memory_import_enabled is not None
                        else DEFAULT_RESTRICTED_POLICY.memory_import_enabled
                    ),
                    int(
                        max_prompt_chars
                        if max_prompt_chars is not None
                        else DEFAULT_RESTRICTED_POLICY.max_prompt_chars
                    ),
                ),
            )
            return

        updates: list[tuple[str, object]] = []
        if allowed_modes is not None:
            updates.append(("allowed_modes", _modes_to_csv(allowed_modes)))
        if web_search_enabled is not None:
            updates.append(("web_search_enabled", int(web_search_enabled)))
        if weather_enabled is not None:
            updates.append(("weather_enabled", int(weather_enabled)))
        if memory_save_enabled is not None:
            updates.append(("memory_save_enabled", int(memory_save_enabled)))
        if memory_import_enabled is not None:
            updates.append(("memory_import_enabled", int(memory_import_enabled)))
        if max_prompt_chars is not None:
            updates.append(("max_prompt_chars", int(max_prompt_chars)))
        if daily_message_limit is not None:
            updates.append(("daily_message_limit", int(daily_message_limit)))
        if not updates:
            return
        cols = ", ".join(f"{k} = ?" for k, _ in updates)
        params = [v for _, v in updates] + [user_id]
        conn.execute(
            f"UPDATE family_controls SET {cols} WHERE user_id = ?", params
        )


# ── Daily message accounting ────────────────────────────────────────────────

def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _seconds_until_midnight_utc() -> int:
    now = datetime.now(timezone.utc)
    end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
    return max(1, int((end - now).total_seconds()) + 1)


def today_usage(user_id: int, db_path: Optional[str] = None) -> int:
    with _open(db_path) as conn:
        row = conn.execute(
            "SELECT message_count FROM user_daily_usage "
            "WHERE user_id = ? AND usage_date = ?",
            (user_id, _today_utc()),
        ).fetchone()
    return int(row[0]) if row else 0


def record_message(user_id: int, db_path: Optional[str] = None) -> int:
    """Increment today's counter for `user_id` and return the new total."""
    today = _today_utc()
    with _open(db_path) as conn:
        conn.execute(
            "INSERT INTO user_daily_usage (user_id, usage_date, message_count) "
            "VALUES (?, ?, 1) "
            "ON CONFLICT(user_id, usage_date) DO UPDATE SET "
            "message_count = message_count + 1",
            (user_id, today),
        )
        row = conn.execute(
            "SELECT message_count FROM user_daily_usage "
            "WHERE user_id = ? AND usage_date = ?",
            (user_id, today),
        ).fetchone()
    return int(row[0]) if row else 0


# ── Enforcement helpers ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class PolicyDenial:
    """Describes why a request was refused. Mapped to HTTP at the web edge."""

    status_code: int
    detail: str
    headers: dict = field(default_factory=dict)


def check_chat_request(
    policy: Policy,
    *,
    mode: str,
    message: str,
    requested_search: bool,
) -> Optional[PolicyDenial]:
    """
    Validate a /chat payload against `policy`.

    Returns `None` when the request is allowed. Returns a `PolicyDenial`
    describing the first violation found. Daily-limit accounting is
    handled separately by `enforce_daily_limit` so that the counter is
    only touched once enforcement has otherwise succeeded.
    """
    if not policy.mode_allowed(mode):
        return PolicyDenial(
            status_code=403,
            detail=f"Mode '{mode}' is not allowed for this account.",
        )
    if requested_search and not policy.web_search_enabled:
        return PolicyDenial(
            status_code=403,
            detail="Web search is disabled for this account.",
        )
    if policy.prompt_too_long(message):
        return PolicyDenial(
            status_code=403,
            detail=(
                f"Message exceeds the {policy.max_prompt_chars}-character "
                "limit for this account."
            ),
        )
    return None


def enforce_daily_limit(
    policy: Policy, user_id: int, db_path: Optional[str] = None
) -> Optional[PolicyDenial]:
    """
    Check today's usage against `policy.daily_message_limit` and, if the
    request is allowed, record one message.

    Returns a 429 `PolicyDenial` with a `Retry-After` header (in seconds
    until UTC midnight) when the limit has already been reached.
    """
    if policy.daily_message_limit is None:
        return None
    used = today_usage(user_id, db_path=db_path)
    if used >= policy.daily_message_limit:
        retry = _seconds_until_midnight_utc()
        return PolicyDenial(
            status_code=429,
            detail=(
                "Daily message limit reached "
                f"({policy.daily_message_limit}/day)."
            ),
            headers={"Retry-After": str(retry)},
        )
    record_message(user_id, db_path=db_path)
    return None
