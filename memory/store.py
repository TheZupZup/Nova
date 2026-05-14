import json
import re
import sqlite3
from datetime import datetime

from core.paths import database_path as _resolved_db_path
from memory.embeddings import generate_embedding, cosine_similarity
from memory.schema import Memory

# Resolved at import time from ``core.paths``. With ``NOVA_DATA_DIR``
# set this becomes ``<NOVA_DATA_DIR>/nova.db``; otherwise it remains
# the legacy relative path so existing installs are unaffected. Tests
# still monkeypatch ``memory.store.DB_PATH`` directly, and every call
# path resolves the attribute via :func:`_resolve_db_path` below — so
# the override propagates unchanged.
DB_PATH = str(_resolved_db_path())

# Thresholds for deciding whether a new memory duplicates an existing one.
# Cosine similarity is used when both memories have embeddings; Jaccard token
# overlap is used as a fallback when one or both are missing an embedding.
_EMBED_THRESHOLD = 0.85
_KEYWORD_THRESHOLD = 0.50


def _resolve_db_path(db_path: str | None) -> str:
    """Return the explicit `db_path` if given, else the current module DB_PATH.

    Resolving at call time (rather than as a default arg) lets tests
    monkeypatch `memory.store.DB_PATH` and have every call honour the patch.
    """
    return db_path if db_path is not None else DB_PATH


def initialize_memory_database(db_path: str | None = None):
    """Creates the natural_memories table and runs any pending schema migrations."""
    db_path = _resolve_db_path(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS natural_memories (
                id           TEXT PRIMARY KEY,
                kind         TEXT NOT NULL,
                topic        TEXT NOT NULL,
                content      TEXT NOT NULL,
                confidence   REAL NOT NULL,
                source       TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            )
        """)
        # v2 migration: add embedding column to existing databases
        try:
            conn.execute("ALTER TABLE natural_memories ADD COLUMN embedding TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
    _migrate_natural_memories_ownership(db_path)


def _migrate_natural_memories_ownership(db_path: str) -> None:
    """
    Add a user_id column to natural_memories and backfill existing rows
    to the legacy admin (issue #106).

    Idempotent: if user_id is already present, only the index is ensured.
    Requires the users table to exist with at least one row.
    """
    with sqlite3.connect(db_path) as conn:
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(natural_memories)").fetchall()
        }
        if "user_id" in cols:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_natural_memories_user_id "
                "ON natural_memories(user_id)"
            )
            return

        users_table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone() is not None
        if not users_table_exists:
            raise RuntimeError(
                "cannot scope natural_memories: users table is missing; "
                "users.migrate() must run first"
            )
        row = conn.execute(
            "SELECT id FROM users ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if row is None:
            raise RuntimeError(
                "cannot scope natural_memories: users table is empty; "
                "users.migrate() must run first"
            )
        legacy_owner_id = row[0]

        conn.execute(
            "ALTER TABLE natural_memories "
            "ADD COLUMN user_id INTEGER REFERENCES users(id)"
        )
        conn.execute(
            "UPDATE natural_memories SET user_id = ? WHERE user_id IS NULL",
            (legacy_owner_id,),
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_natural_memories_user_id "
            "ON natural_memories(user_id)"
        )


def save_memory(memory: Memory, user_id: int, db_path: str | None = None):
    """
    Saves a memory for `user_id`, deduplicating only against that user's
    existing memories with the same kind + topic. If a sufficiently similar
    memory already exists it is updated in place (preserving created_at)
    rather than inserting a duplicate.
    """
    db_path = _resolve_db_path(db_path)
    if memory.embedding is None:
        memory = memory.model_copy(update={"embedding": generate_embedding(memory.content)})

    duplicate = _find_duplicate(memory, user_id, db_path)
    if duplicate:
        to_save = memory.model_copy(update={"id": duplicate.id, "created_at": duplicate.created_at})
        update_memory(to_save, user_id, db_path)
    else:
        _insert_memory(memory, user_id, db_path)


def update_memory(memory: Memory, user_id: int, db_path: str | None = None):
    """Updates all mutable fields of an existing memory owned by `user_id`."""
    db_path = _resolve_db_path(db_path)
    now = datetime.now().isoformat()
    emb_json = json.dumps(memory.embedding) if memory.embedding is not None else None
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE natural_memories
            SET kind=?, topic=?, content=?, confidence=?, embedding=?,
                updated_at=?, last_seen_at=?
            WHERE id=? AND user_id=?
            """,
            (memory.kind, memory.topic, memory.content, memory.confidence,
             emb_json, now, now, memory.id, user_id),
        )


def delete_memory(memory_id: str, user_id: int, db_path: str | None = None):
    """Deletes a memory by id, but only if owned by `user_id`."""
    db_path = _resolve_db_path(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "DELETE FROM natural_memories WHERE id = ? AND user_id = ?",
            (memory_id, user_id),
        )


def list_memories(user_id: int, db_path: str | None = None) -> list[Memory]:
    """Returns all memories owned by `user_id`, newest first."""
    db_path = _resolve_db_path(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM natural_memories WHERE user_id = ? "
            "ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_memory(r) for r in rows]


def search_memories(query: str, user_id: int, limit: int = 8, db_path: str | None = None) -> list[Memory]:
    """
    Returns up to `limit` memories owned by `user_id`, scored by token
    overlap with `query`. Tokens are normalized (lowercased, punctuation
    and underscores stripped).
    """
    db_path = _resolve_db_path(db_path)
    words = _tokenize(query)
    if not words:
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM natural_memories WHERE user_id = ? "
            "ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    scored: list[tuple[int, Memory]] = []
    for row in rows:
        mem = _row_to_memory(row)
        haystack = set(_tokenize(f"{mem.topic} {mem.content}"))
        score = sum(1 for w in words if w in haystack)
        if score > 0:
            scored.append((score, mem))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [m for _, m in scored[:limit]]


def delete_memories_matching(query: str, user_id: int, db_path: str | None = None) -> int:
    """
    Deletes ALL of `user_id`'s memories matching the query keywords.
    Returns the count deleted. Memories belonging to other users are
    never touched.

    Uses a single connection/transaction: the read + matching scan + delete
    all happen on the same `sqlite3.Connection` to avoid the N+1 reconnect
    pattern that opened a fresh connection per deleted row.
    """
    db_path = _resolve_db_path(db_path)
    words = _tokenize(query)
    if not words:
        return 0

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, topic, content FROM natural_memories WHERE user_id = ?",
            (user_id,),
        ).fetchall()

        to_delete = [
            r["id"]
            for r in rows
            if any(w in set(_tokenize(f"{r['topic']} {r['content']}")) for w in words)
        ]
        if not to_delete:
            return 0

        # Placeholders are built from a fixed `?` literal, never user input,
        # so this remains a fully parameterized query.
        placeholders = ",".join(["?"] * len(to_delete))
        conn.execute(
            f"DELETE FROM natural_memories WHERE user_id = ? AND id IN ({placeholders})",
            (user_id, *to_delete),
        )
        return len(to_delete)


# ── private helpers ────────────────────────────────────────────────────────────

def _insert_memory(memory: Memory, user_id: int, db_path: str):
    emb_json = json.dumps(memory.embedding) if memory.embedding is not None else None
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO natural_memories
            (id, kind, topic, content, confidence, source,
             created_at, updated_at, last_seen_at, embedding, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (memory.id, memory.kind, memory.topic, memory.content,
             memory.confidence, memory.source,
             memory.created_at, memory.updated_at, memory.last_seen_at,
             emb_json, user_id),
        )


def _find_duplicate(memory: Memory, user_id: int, db_path: str) -> Memory | None:
    """
    Returns the most recent memory owned by `user_id` with the same kind +
    topic that is similar enough to be treated as the same logical memory,
    or None if no such memory exists. Cross-user dedup is intentionally
    avoided so distinct users keep distinct memories.
    """
    candidates = _get_by_kind_topic(memory.kind, memory.topic, user_id, db_path)
    for candidate in candidates:
        if memory.embedding and candidate.embedding:
            if cosine_similarity(memory.embedding, candidate.embedding) >= _EMBED_THRESHOLD:
                return candidate
        elif _keyword_similarity(memory.content, candidate.content) >= _KEYWORD_THRESHOLD:
            return candidate
    return None


def _get_by_kind_topic(kind: str, topic: str, user_id: int, db_path: str) -> list[Memory]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM natural_memories "
            "WHERE kind=? AND topic=? AND user_id=? ORDER BY created_at DESC",
            (kind, topic, user_id),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_memory(r) for r in rows]


def _keyword_similarity(a: str, b: str) -> float:
    """Jaccard similarity between the tokenized representations of two strings."""
    tokens_a = set(_tokenize(a))
    tokens_b = set(_tokenize(b))
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def _tokenize(text: str) -> list[str]:
    """Lowercases and splits on non-alphanumeric characters (incl. underscores)."""
    return [t for t in re.split(r"[\W_]+", text.lower()) if len(t) > 2]


def _row_to_memory(row: sqlite3.Row) -> Memory:
    emb_raw = row["embedding"]
    return Memory(
        id=row["id"],
        kind=row["kind"],
        topic=row["topic"],
        content=row["content"],
        confidence=float(row["confidence"]),
        source=row["source"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_seen_at=row["last_seen_at"],
        embedding=json.loads(emb_raw) if emb_raw else None,
    )
