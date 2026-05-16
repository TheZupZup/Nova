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

# Sentinel meaning "do not filter by project at all" (every row for the
# user). ``None`` is a *meaningful* scope value — it means "General /
# global only" — so a distinct object is needed to tell the audit path
# (``/memories``, "what do you remember") apart from a General chat.
ALL_PROJECTS = object()


def _project_scope_clause(project_scope) -> tuple[str, tuple]:
    """Translate a project scope into an SQL fragment + params.

    * ``ALL_PROJECTS``  → no project predicate (every row).
    * ``None``          → ``project_id IS NULL`` (General / global only).
    * an ``int`` (P)    → ``project_id IS NULL OR project_id = P``
                           (global memory stays visible inside a
                           project; other projects never leak in).
    """
    if project_scope is ALL_PROJECTS:
        return "", ()
    if project_scope is None:
        return " AND project_id IS NULL", ()
    return " AND (project_id IS NULL OR project_id = ?)", (project_scope,)


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
    _migrate_natural_memories_project(db_path)


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


def _migrate_natural_memories_project(db_path: str) -> None:
    """
    Add a nullable ``project_id`` column to ``natural_memories`` (Nova
    Projects Phase 1).

    Idempotent. NULL ``project_id`` means the memory is global and
    visible in every project; existing rows keep that behaviour with no
    backfill and no automatic reclassification.
    """
    with sqlite3.connect(db_path) as conn:
        cols = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(natural_memories)"
            ).fetchall()
        }
        if "project_id" not in cols:
            conn.execute(
                "ALTER TABLE natural_memories "
                "ADD COLUMN project_id INTEGER REFERENCES projects(id)"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_natural_memories_project_id "
            "ON natural_memories(project_id)"
        )


def save_memory(
    memory: Memory,
    user_id: int,
    db_path: str | None = None,
    project_id: int | None = None,
):
    """
    Saves a memory for `user_id`, deduplicating only against that user's
    existing memories with the same kind + topic **within the same
    project scope**. If a sufficiently similar memory already exists it
    is updated in place (preserving created_at) rather than inserting a
    duplicate.

    ``project_id`` defaults to ``None`` (global memory, exactly the
    pre-projects behaviour). Dedup is project-aware so a project-scoped
    fact never silently overwrites a global one (and vice-versa).
    """
    db_path = _resolve_db_path(db_path)
    if memory.embedding is None:
        memory = memory.model_copy(update={"embedding": generate_embedding(memory.content)})

    duplicate = _find_duplicate(memory, user_id, db_path, project_id)
    if duplicate:
        to_save = memory.model_copy(update={"id": duplicate.id, "created_at": duplicate.created_at})
        update_memory(to_save, user_id, db_path)
    else:
        _insert_memory(memory, user_id, db_path, project_id)


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


def list_memories(
    user_id: int, db_path: str | None = None, project_scope=ALL_PROJECTS
) -> list[Memory]:
    """Returns memories owned by `user_id`, newest first.

    ``project_scope`` (see :func:`_project_scope_clause`) defaults to
    ``ALL_PROJECTS`` so existing callers (the ``/memories`` view, "what
    do you remember", tests) keep returning every memory. The chat
    retriever passes an explicit scope to apply General / per-project
    visibility.
    """
    db_path = _resolve_db_path(db_path)
    clause, params = _project_scope_clause(project_scope)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM natural_memories WHERE user_id = ?" + clause
            + " ORDER BY created_at DESC",
            (user_id, *params),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_memory(r) for r in rows]


def search_memories(
    query: str,
    user_id: int,
    limit: int = 8,
    db_path: str | None = None,
    project_scope=ALL_PROJECTS,
) -> list[Memory]:
    """
    Returns up to `limit` memories owned by `user_id`, scored by token
    overlap with `query`. Tokens are normalized (lowercased, punctuation
    and underscores stripped).

    ``project_scope`` defaults to ``ALL_PROJECTS`` (every memory). The
    chat retriever passes an explicit scope so a project session only
    sees global + that project's memory.
    """
    db_path = _resolve_db_path(db_path)
    words = _tokenize(query)
    if not words:
        return []

    clause, params = _project_scope_clause(project_scope)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM natural_memories WHERE user_id = ?" + clause
            + " ORDER BY created_at DESC",
            (user_id, *params),
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

def _insert_memory(
    memory: Memory,
    user_id: int,
    db_path: str,
    project_id: int | None = None,
):
    emb_json = json.dumps(memory.embedding) if memory.embedding is not None else None
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO natural_memories
            (id, kind, topic, content, confidence, source,
             created_at, updated_at, last_seen_at, embedding, user_id,
             project_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (memory.id, memory.kind, memory.topic, memory.content,
             memory.confidence, memory.source,
             memory.created_at, memory.updated_at, memory.last_seen_at,
             emb_json, user_id, project_id),
        )


def _find_duplicate(
    memory: Memory,
    user_id: int,
    db_path: str,
    project_id: int | None = None,
) -> Memory | None:
    """
    Returns the most recent memory owned by `user_id` with the same kind +
    topic **and the same project scope** that is similar enough to be
    treated as the same logical memory, or None if no such memory
    exists.

    Cross-user dedup is intentionally avoided so distinct users keep
    distinct memories; cross-project dedup is avoided for the same
    reason — a project fact and a global fact with the same kind/topic
    are different memories and must not overwrite each other.
    """
    candidates = _get_by_kind_topic(
        memory.kind, memory.topic, user_id, db_path, project_id
    )
    for candidate in candidates:
        if memory.embedding and candidate.embedding:
            if cosine_similarity(memory.embedding, candidate.embedding) >= _EMBED_THRESHOLD:
                return candidate
        elif _keyword_similarity(memory.content, candidate.content) >= _KEYWORD_THRESHOLD:
            return candidate
    return None


def _get_by_kind_topic(
    kind: str,
    topic: str,
    user_id: int,
    db_path: str,
    project_id: int | None = None,
) -> list[Memory]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if project_id is None:
            rows = conn.execute(
                "SELECT * FROM natural_memories "
                "WHERE kind=? AND topic=? AND user_id=? "
                "AND project_id IS NULL ORDER BY created_at DESC",
                (kind, topic, user_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM natural_memories "
                "WHERE kind=? AND topic=? AND user_id=? "
                "AND project_id=? ORDER BY created_at DESC",
                (kind, topic, user_id, project_id),
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
