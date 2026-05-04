"""
Local Ollama model registry (issue #110).

This is the foundational layer that lets admins eventually view and manage
the set of local models Nova knows about. The scope of #110 is intentionally
narrow — read-only registry storage + a one-time seed from config.MODELS:

  * Create the `model_registry` table if missing.
  * Seed one row per (purpose, model_name) pair from `config.MODELS`.
  * Provide a helper to reconcile rows with `client.list()` so the
    `installed` flag reflects what Ollama actually has on disk.
  * Provide a list helper that the admin endpoint reads from.

Out of scope here:
  * Pulling / downloading models — that is #111.
  * Per-user / per-role model access controls — that is #112.
  * Frontend model-management UI.
  * Mutating model routing — `core.router.MODEL_MAP` and `web.MODE_MAP`
    keep their current behaviour.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Optional

import httpx
import ollama

from core.ollama_client import client

logger = logging.getLogger(__name__)


_MODEL_REGISTRY_SQL = """
CREATE TABLE IF NOT EXISTS model_registry (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name   TEXT    NOT NULL UNIQUE,
    display_name TEXT    NOT NULL,
    purpose      TEXT    NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1,
    installed    INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL
)
"""

_MODEL_REGISTRY_PURPOSE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_model_registry_purpose "
    "ON model_registry(purpose)"
)


# Friendly labels for the four purposes Nova currently uses. Anything not
# listed falls back to the title-cased purpose string.
_PURPOSE_DISPLAY_NAMES = {
    "router": "Router",
    "default": "Default",
    "code": "Code",
    "advanced": "Advanced",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _purpose_display(purpose: str) -> str:
    return _PURPOSE_DISPLAY_NAMES.get(purpose, purpose.title())


def _open(db_path: Optional[str] = None) -> sqlite3.Connection:
    if db_path is None:
        from core.memory import DB_PATH
        db_path = DB_PATH
    return sqlite3.connect(db_path)


# ── Migration + seed ────────────────────────────────────────────────────────


def migrate(db_path: str) -> None:
    """Create the `model_registry` table and its index. Idempotent."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(_MODEL_REGISTRY_SQL)
        conn.execute(_MODEL_REGISTRY_PURPOSE_INDEX_SQL)


def seed_from_config(
    db_path: str,
    models: Optional[dict] = None,
) -> int:
    """
    Insert one row per (purpose, model_name) pair in `models`.

    Idempotent — collisions on `model_name` are ignored, so a model that is
    already registered keeps its admin-edited flags. Returns the number of
    rows actually inserted.

    `models` defaults to the live `config.MODELS` dict; tests pass an
    explicit mapping so they do not depend on environment.
    """
    if models is None:
        from config import MODELS
        models = MODELS

    inserted = 0
    now = _now_iso()
    with sqlite3.connect(db_path) as conn:
        for purpose, model_name in models.items():
            if not model_name:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO model_registry "
                "(model_name, display_name, purpose, enabled, installed, "
                "created_at, updated_at) VALUES (?, ?, ?, 1, 0, ?, ?)",
                (model_name, _purpose_display(purpose), purpose, now, now),
            )
            inserted += cur.rowcount
    return inserted


# ── Reads ───────────────────────────────────────────────────────────────────


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        "model_name": row["model_name"],
        "display_name": row["display_name"],
        "purpose": row["purpose"],
        "enabled": bool(row["enabled"]),
        "installed": bool(row["installed"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_registered(db_path: Optional[str] = None) -> list[dict]:
    """
    Return every row in `model_registry`, ordered by id.

    Caller is responsible for gating who sees the result — raw `model_name`
    is admin-only per the architecture doc.
    """
    with _open(db_path) as conn:
        prev = conn.row_factory
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, model_name, display_name, purpose, enabled, "
                "installed, created_at, updated_at "
                "FROM model_registry ORDER BY id ASC"
            ).fetchall()
        finally:
            conn.row_factory = prev
    return [_row_to_dict(r) for r in rows]


# ── Reconcile with Ollama ───────────────────────────────────────────────────


def _list_installed_model_names() -> Optional[set[str]]:
    """
    Best-effort read of `client.list()`.

    Returns the set of installed model names, or None if Ollama is
    unreachable. This is a *read-only* call — no pulls, no downloads.
    """
    try:
        payload = client.list()
    except (ConnectionError, OSError, ollama.ResponseError, httpx.HTTPError) as e:
        logger.debug("Ollama list unavailable, skipping reconcile: %s", e)
        return None

    names: set[str] = set()
    for entry in payload.get("models", []) or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("model")
        if name:
            names.add(name)
    return names


def reconcile_installed(db_path: Optional[str] = None) -> Optional[dict[str, bool]]:
    """
    Update each row's `installed` flag based on what Ollama currently has
    locally. Returns a `{model_name: installed}` snapshot, or None if
    Ollama could not be reached (existing flags are left untouched).

    This function never triggers a pull — issue #111 owns that behaviour.
    """
    installed_names = _list_installed_model_names()
    if installed_names is None:
        return None

    snapshot: dict[str, bool] = {}
    now = _now_iso()
    with _open(db_path) as conn:
        rows = conn.execute(
            "SELECT model_name, installed FROM model_registry"
        ).fetchall()
        for model_name, current_installed in rows:
            is_installed = _matches_installed(model_name, installed_names)
            snapshot[model_name] = is_installed
            if bool(current_installed) != is_installed:
                conn.execute(
                    "UPDATE model_registry SET installed = ?, updated_at = ? "
                    "WHERE model_name = ?",
                    (1 if is_installed else 0, now, model_name),
                )
    return snapshot


def _matches_installed(model_name: str, installed_names: Iterable[str]) -> bool:
    """
    Return True if `model_name` matches one of the names Ollama reports.

    Ollama's list often returns `name:tag` pairs, and the registry may
    store either the full tag or just the base name. We accept exact match
    on either side, mirroring `core.updater.get_local_model_digest`.
    """
    for installed in installed_names:
        if installed == model_name:
            return True
        if installed.startswith(model_name + ":"):
            return True
        if model_name.startswith(installed + ":"):
            return True
    return False
