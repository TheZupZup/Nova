"""
Per-user / per-role model and mode access controls (issue #112).

This is the backend access-control foundation that lets admins eventually
decide which modes (chat / code / deep / auto) and which raw models a
given user or role may use. The scope of #112 is intentionally narrow:

  * Storage for per-role and per-user overrides.
  * A helper that computes the *effective* allowed modes/models for a
    user, layered on top of `core.policies.get_policy(user)`.
  * Enforcement helpers used by `/chat`.
  * A friendly-label view used by `/me` so non-admin clients never see
    raw model names.

Resolution rules:
  * Admins always pass through with full access (all known modes and
    every enabled registry model).
  * Non-admin users start from `policies.get_policy(user).allowed_modes`
    and `model_registry` (enabled rows). Any per-role and per-user
    override only INTERSECTS — overrides cannot grant access that the
    base policy already refuses. This keeps the family-controls
    invariants intact even when an admin assigns a per-user override.

Privacy:
  * Raw `model_name` is admin-only. The helpers in this module return
    raw names only to the enforcement layer (where they are compared
    against the resolved Ollama model). Non-admin views use friendly
    mode labels.

Out of scope here (other issues):
  * Admin endpoints / UI for assigning role or per-user access — #127.
  * Admin model-management UI — #124.
  * Ollama pull flow — #111.
  * Model deletion or marketplace.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Iterable, Optional

from core.policies import KNOWN_MODES, get_policy
from core.users import ROLE_USER


# ── Friendly mode labels (non-admin /me) ────────────────────────────────────

# Stable, user-facing labels. Keys must be a subset of `KNOWN_MODES`.
MODE_LABELS: dict[str, str] = {
    "auto": "Auto",
    "chat": "Chat",
    "code": "Code",
    "deep": "Deep",
}

# Stable presentation order for /me.
_MODE_ORDER: tuple[str, ...] = ("chat", "auto", "code", "deep")


def mode_label(mode: str) -> str:
    """Return the friendly label for `mode`, or the mode itself if unknown."""
    return MODE_LABELS.get(mode, mode)


# ── Schema ──────────────────────────────────────────────────────────────────

_ROLE_MODEL_ACCESS_SQL = """
CREATE TABLE IF NOT EXISTS role_model_access (
    role           TEXT    NOT NULL,
    is_restricted  INTEGER NOT NULL,
    allowed_modes  TEXT,
    allowed_models TEXT,
    PRIMARY KEY (role, is_restricted)
)
"""

_USER_MODEL_ACCESS_SQL = """
CREATE TABLE IF NOT EXISTS user_model_access (
    user_id        INTEGER PRIMARY KEY
                          REFERENCES users(id) ON DELETE CASCADE,
    allowed_modes  TEXT,
    allowed_models TEXT
)
"""


def migrate(db_path: str) -> None:
    """Create the role_model_access and user_model_access tables. Idempotent."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(_ROLE_MODEL_ACCESS_SQL)
        conn.execute(_USER_MODEL_ACCESS_SQL)


def _open(db_path: Optional[str] = None) -> sqlite3.Connection:
    if db_path is None:
        from core.memory import DB_PATH
        db_path = DB_PATH
    return sqlite3.connect(db_path)


# ── CSV helpers ─────────────────────────────────────────────────────────────
#
# `None` (NULL in storage) means "inherit from the previous resolution
# layer" — built-in defaults for role rows, the role row for user rows.
# An empty string ("") means "explicitly deny everything".

def _modes_to_csv(modes: Optional[Iterable[str]]) -> Optional[str]:
    if modes is None:
        return None
    cleaned = sorted({m for m in modes if m in KNOWN_MODES})
    return ",".join(cleaned)


def _csv_to_modes(value: Optional[str]) -> Optional[frozenset[str]]:
    if value is None:
        return None
    parts = {p.strip() for p in value.split(",") if p.strip()}
    return frozenset(parts & KNOWN_MODES)


def _models_to_csv(models: Optional[Iterable[str]]) -> Optional[str]:
    if models is None:
        return None
    cleaned = sorted({m.strip() for m in models if m and m.strip()})
    return ",".join(cleaned)


def _csv_to_models(value: Optional[str]) -> Optional[frozenset[str]]:
    if value is None:
        return None
    parts = {p.strip() for p in value.split(",") if p.strip()}
    return frozenset(parts)


# ── Reads ───────────────────────────────────────────────────────────────────

def _read_role_row(
    conn: sqlite3.Connection, role: str, is_restricted: bool
) -> Optional[sqlite3.Row]:
    prev = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT allowed_modes, allowed_models FROM role_model_access "
            "WHERE role = ? AND is_restricted = ?",
            (role, 1 if is_restricted else 0),
        ).fetchone()
    finally:
        conn.row_factory = prev


def _read_user_row(
    conn: sqlite3.Connection, user_id: int
) -> Optional[sqlite3.Row]:
    prev = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT allowed_modes, allowed_models FROM user_model_access "
            "WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    finally:
        conn.row_factory = prev


def _list_enabled_models(conn: sqlite3.Connection) -> frozenset[str]:
    """
    Return the set of registry model_names where `enabled = 1`.

    Disabled registry rows are excluded from non-admin allowed_models so
    a model the admin has turned off cannot be reached by a normal user.
    """
    try:
        rows = conn.execute(
            "SELECT model_name FROM model_registry WHERE enabled = 1"
        ).fetchall()
    except sqlite3.DatabaseError:
        return frozenset()
    return frozenset(r[0] for r in rows)


# ── Effective access ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EffectiveAccess:
    """Resolved per-user view of allowed modes and models."""

    is_admin: bool
    allowed_modes: frozenset[str] = field(default_factory=frozenset)
    allowed_models: frozenset[str] = field(default_factory=frozenset)

    def mode_allowed(self, mode: str) -> bool:
        if self.is_admin:
            # Admin keeps full access regardless of stored rows.
            return mode in KNOWN_MODES
        return mode in self.allowed_modes

    def model_allowed(self, model: str) -> bool:
        if not model:
            return False
        if self.is_admin:
            return True
        return model in self.allowed_models


def get_effective_access(
    user, db_path: Optional[str] = None
) -> EffectiveAccess:
    """
    Resolve the effective allowed modes and models for `user`.

    Admins return an `EffectiveAccess(is_admin=True, …)` and bypass every
    override layer. Non-admins start from the base policy and intersect
    each layer (role row, then user row) — no override can grant access
    the base policy already refuses.
    """
    policy = get_policy(user, db_path=db_path)
    if policy.is_admin:
        with _open(db_path) as conn:
            enabled_models = _list_enabled_models(conn)
        return EffectiveAccess(
            is_admin=True,
            allowed_modes=KNOWN_MODES,
            allowed_models=enabled_models,
        )

    role = getattr(user, "role", ROLE_USER)
    is_restricted = bool(getattr(user, "is_restricted", False))
    user_id = int(getattr(user, "id"))

    # Base modes from policy (covers family controls for restricted users
    # and KNOWN_MODES for normal users).
    modes: set[str] = set(policy.allowed_modes)

    with _open(db_path) as conn:
        enabled_models = _list_enabled_models(conn)

        # `models` starts as None so we can distinguish "no constraint
        # applied yet" (= all enabled registry models) from "empty set"
        # (= explicitly deny everything). We intersect with the enabled
        # registry once at the end.
        models: Optional[set[str]] = None

        role_row = _read_role_row(conn, role, is_restricted)
        if role_row is not None:
            r_modes = _csv_to_modes(role_row["allowed_modes"])
            if r_modes is not None:
                modes &= set(r_modes)
            r_models = _csv_to_models(role_row["allowed_models"])
            if r_models is not None:
                models = set(r_models)

        user_row = _read_user_row(conn, user_id)
        if user_row is not None:
            u_modes = _csv_to_modes(user_row["allowed_modes"])
            if u_modes is not None:
                modes &= set(u_modes)
            u_models = _csv_to_models(user_row["allowed_models"])
            if u_models is not None:
                models = (
                    set(u_models)
                    if models is None
                    else (models & set(u_models))
                )

    if models is None:
        # No explicit model restriction → all enabled registry models.
        effective_models = set(enabled_models)
    else:
        # Always intersect with what the registry actually has enabled, so
        # a disabled model can never be reached by a non-admin caller.
        effective_models = models & enabled_models

    return EffectiveAccess(
        is_admin=False,
        allowed_modes=frozenset(modes),
        allowed_models=frozenset(effective_models),
    )


# ── Friendly /me view ───────────────────────────────────────────────────────

def available_modes_for(user, db_path: Optional[str] = None) -> list[dict]:
    """
    Return the modes `user` may request, as friendly entries.

    Each entry is `{"mode": str, "label": str}`. Raw model names are
    intentionally not included — admin clients should read the registry
    via `/admin/models` instead. The returned list preserves a stable,
    user-facing order so the UI does not have to sort it.
    """
    access = get_effective_access(user, db_path=db_path)
    seen: set[str] = set()
    out: list[dict] = []
    for mode in _MODE_ORDER:
        if mode in access.allowed_modes and mode not in seen:
            out.append({"mode": mode, "label": mode_label(mode)})
            seen.add(mode)
    # Anything else known (future modes added to KNOWN_MODES) — keep
    # sorted for deterministic output.
    for mode in sorted(access.allowed_modes):
        if mode not in seen:
            out.append({"mode": mode, "label": mode_label(mode)})
            seen.add(mode)
    return out


# ── Enforcement helpers ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class AccessDenial:
    """Why a request was refused. The web layer maps to HTTPException."""

    status_code: int
    detail: str


def check_mode_access(
    user, mode: str, db_path: Optional[str] = None
) -> Optional[AccessDenial]:
    """
    Validate that `user` can request `mode`. Returns None when allowed.

    The denial detail is generic and never echoes raw model information.
    """
    access = get_effective_access(user, db_path=db_path)
    if access.mode_allowed(mode):
        return None
    return AccessDenial(
        status_code=403,
        detail=f"Mode '{mode}' is not allowed for this account.",
    )


def check_model_access(
    user, model: str, db_path: Optional[str] = None
) -> Optional[AccessDenial]:
    """
    Validate that `user` can target the raw model `model`. Returns None
    when allowed. The denial detail does not echo the model name back —
    raw names stay admin-only even in error paths.
    """
    access = get_effective_access(user, db_path=db_path)
    if access.model_allowed(model):
        return None
    return AccessDenial(
        status_code=403,
        detail="The requested model is not allowed for this account.",
    )


# ── Setters (admin-managed; used by tests and future admin endpoints) ───────

def set_role_access(
    role: str,
    is_restricted: bool,
    *,
    allowed_modes: Optional[Iterable[str]] = None,
    allowed_models: Optional[Iterable[str]] = None,
    db_path: Optional[str] = None,
) -> None:
    """
    Upsert the role override row for `(role, is_restricted)`.

    Pass `None` to leave a column NULL (= inherit from the built-in
    defaults). Pass an explicit iterable (including `[]` / `frozenset()`)
    to deny everything for that column.
    """
    modes_csv = _modes_to_csv(allowed_modes)
    models_csv = _models_to_csv(allowed_models)
    with _open(db_path) as conn:
        existing = _read_role_row(conn, role, is_restricted)
        if existing is None:
            conn.execute(
                "INSERT INTO role_model_access "
                "(role, is_restricted, allowed_modes, allowed_models) "
                "VALUES (?, ?, ?, ?)",
                (role, 1 if is_restricted else 0, modes_csv, models_csv),
            )
            return
        conn.execute(
            "UPDATE role_model_access SET allowed_modes = ?, "
            "allowed_models = ? WHERE role = ? AND is_restricted = ?",
            (modes_csv, models_csv, role, 1 if is_restricted else 0),
        )


def clear_role_access(
    role: str, is_restricted: bool, db_path: Optional[str] = None
) -> None:
    """Remove the role override row, restoring built-in defaults."""
    with _open(db_path) as conn:
        conn.execute(
            "DELETE FROM role_model_access "
            "WHERE role = ? AND is_restricted = ?",
            (role, 1 if is_restricted else 0),
        )


def get_role_access(
    role: str, is_restricted: bool, db_path: Optional[str] = None
) -> Optional[dict]:
    """Return the role override row as a dict (or None if absent)."""
    with _open(db_path) as conn:
        row = _read_role_row(conn, role, is_restricted)
    if row is None:
        return None
    modes = _csv_to_modes(row["allowed_modes"])
    models = _csv_to_models(row["allowed_models"])
    return {
        "role": role,
        "is_restricted": bool(is_restricted),
        "allowed_modes": sorted(modes) if modes is not None else None,
        "allowed_models": sorted(models) if models is not None else None,
    }


def set_user_access(
    user_id: int,
    *,
    allowed_modes: Optional[Iterable[str]] = None,
    allowed_models: Optional[Iterable[str]] = None,
    db_path: Optional[str] = None,
) -> None:
    """
    Upsert the per-user override row for `user_id`.

    `None` columns inherit from the role row (or built-in defaults).
    """
    modes_csv = _modes_to_csv(allowed_modes)
    models_csv = _models_to_csv(allowed_models)
    with _open(db_path) as conn:
        existing = _read_user_row(conn, user_id)
        if existing is None:
            conn.execute(
                "INSERT INTO user_model_access "
                "(user_id, allowed_modes, allowed_models) "
                "VALUES (?, ?, ?)",
                (user_id, modes_csv, models_csv),
            )
            return
        conn.execute(
            "UPDATE user_model_access SET allowed_modes = ?, "
            "allowed_models = ? WHERE user_id = ?",
            (modes_csv, models_csv, user_id),
        )


def clear_user_access(user_id: int, db_path: Optional[str] = None) -> None:
    """Remove the per-user override row, falling back to role defaults."""
    with _open(db_path) as conn:
        conn.execute(
            "DELETE FROM user_model_access WHERE user_id = ?", (user_id,)
        )


def get_user_access(
    user_id: int, db_path: Optional[str] = None
) -> Optional[dict]:
    """Return the per-user override row as a dict (or None if absent)."""
    with _open(db_path) as conn:
        row = _read_user_row(conn, user_id)
    if row is None:
        return None
    modes = _csv_to_modes(row["allowed_modes"])
    models = _csv_to_models(row["allowed_models"])
    return {
        "user_id": user_id,
        "allowed_modes": sorted(modes) if modes is not None else None,
        "allowed_models": sorted(models) if models is not None else None,
    }
