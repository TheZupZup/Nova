"""
Detected local Ollama models — admin-facing registry.

This module owns the *detection* side: a snapshot of which models the
local Ollama daemon currently reports as installed. Refreshing hits
Ollama's `GET /api/tags` (via `core.ollama_client.list_local_models`)
and upserts rows into `local_ollama_models`, keyed on
`(provider, model_name)`.

Idempotency: re-running a refresh against the same Ollama state
inserts nothing new — existing rows have their digest / size /
modified_at / last_seen_at refreshed in place.

Out of scope here:
  * Pulling / downloading models — `core.model_pulls` already owns
    that flow and is unchanged by this module.
  * GGUF import.
  * Per-user / per-role model access controls.
  * Auto-removal of models that disappear from Ollama — refresh keeps
    the audit trail and never deletes rows.

The `model_registry` table (config-seeded routing-purpose registry)
is *not* touched by this module; the two registries live side by side.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Optional

from core.ollama_client import list_local_models as _client_list_local_models

logger = logging.getLogger(__name__)

PROVIDER_OLLAMA = "ollama"


_LOCAL_MODELS_SQL = """
CREATE TABLE IF NOT EXISTS local_ollama_models (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    provider       TEXT    NOT NULL,
    model_name     TEXT    NOT NULL,
    digest         TEXT,
    size_bytes     INTEGER,
    modified_at    TEXT,
    first_seen_at  TEXT    NOT NULL,
    last_seen_at   TEXT    NOT NULL,
    UNIQUE (provider, model_name)
)
"""

_LOCAL_MODELS_PROVIDER_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_local_ollama_models_provider "
    "ON local_ollama_models(provider)"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _open(db_path: Optional[str] = None) -> sqlite3.Connection:
    if db_path is None:
        from core.memory import DB_PATH
        db_path = DB_PATH
    return sqlite3.connect(db_path)


def migrate(db_path: str) -> None:
    """Create the `local_ollama_models` table and its index. Idempotent."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(_LOCAL_MODELS_SQL)
        conn.execute(_LOCAL_MODELS_PROVIDER_INDEX_SQL)


def _coerce_int(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = int(value)
        return n if n >= 0 else None
    return None


def _coerce_str(value: object) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def upsert_models(
    detected: Iterable[dict],
    db_path: Optional[str] = None,
    provider: str = PROVIDER_OLLAMA,
) -> dict:
    """
    Upsert a list of detected models into the registry.

    Each entry in `detected` is the raw shape returned by
    `core.ollama_client.list_local_models`: at minimum a `name`, plus
    optional `digest`, `size`, `modified_at`. Entries without a usable
    name are skipped.

    Existing rows for `(provider, model_name)` have their `digest`,
    `size_bytes`, `modified_at`, and `last_seen_at` updated. New pairs
    are inserted with `first_seen_at = last_seen_at = now()`.

    Models present in the registry but absent from `detected` are
    intentionally left in place — this function never deletes.

    Returns a `{"inserted", "updated", "seen"}` summary.
    """
    now = _now_iso()
    inserted = 0
    updated = 0
    seen = 0
    with _open(db_path) as conn:
        for entry in detected:
            if not isinstance(entry, dict):
                continue
            name = _coerce_str(entry.get("name") or entry.get("model"))
            if name is None:
                continue
            seen += 1
            digest = _coerce_str(entry.get("digest"))
            size_bytes = _coerce_int(
                entry.get("size") if entry.get("size") is not None
                else entry.get("size_bytes")
            )
            modified_at = _coerce_str(entry.get("modified_at"))

            row = conn.execute(
                "SELECT id FROM local_ollama_models "
                "WHERE provider = ? AND model_name = ?",
                (provider, name),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO local_ollama_models "
                    "(provider, model_name, digest, size_bytes, "
                    "modified_at, first_seen_at, last_seen_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (provider, name, digest, size_bytes,
                     modified_at, now, now),
                )
                inserted += 1
            else:
                conn.execute(
                    "UPDATE local_ollama_models SET "
                    "digest = ?, size_bytes = ?, modified_at = ?, "
                    "last_seen_at = ? WHERE id = ?",
                    (digest, size_bytes, modified_at, now, row[0]),
                )
                updated += 1
    return {"inserted": inserted, "updated": updated, "seen": seen}


def _row_to_dict(row: sqlite3.Row) -> dict:
    size = row["size_bytes"]
    return {
        "id": int(row["id"]),
        "provider": row["provider"],
        "model_name": row["model_name"],
        "digest": row["digest"],
        "size_bytes": int(size) if size is not None else None,
        "modified_at": row["modified_at"],
        "first_seen_at": row["first_seen_at"],
        "last_seen_at": row["last_seen_at"],
    }


def list_models(db_path: Optional[str] = None) -> list[dict]:
    """Return every row in the local-Ollama registry, ordered by id."""
    with _open(db_path) as conn:
        prev = conn.row_factory
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, provider, model_name, digest, size_bytes, "
                "modified_at, first_seen_at, last_seen_at "
                "FROM local_ollama_models ORDER BY id ASC"
            ).fetchall()
        finally:
            conn.row_factory = prev
    return [_row_to_dict(r) for r in rows]


def refresh_from_ollama(db_path: Optional[str] = None) -> dict:
    """
    Read `GET /api/tags`, upsert into the registry, return stats.

    Bubbles `core.ollama_client.OllamaUnavailable` straight up so the
    caller (an admin endpoint) can surface a clean 503.
    """
    detected = _client_list_local_models()
    return upsert_models(detected, db_path=db_path)
