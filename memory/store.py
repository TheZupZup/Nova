import json
import re
import sqlite3
from datetime import datetime

from memory.embeddings import generate_embedding, cosine_similarity
from memory.schema import Memory

DB_PATH = "nova.db"

# Thresholds for deciding whether a new memory duplicates an existing one.
# Cosine similarity is used when both memories have embeddings; Jaccard token
# overlap is used as a fallback when one or both are missing an embedding.
_EMBED_THRESHOLD = 0.85
_KEYWORD_THRESHOLD = 0.50


def initialize_memory_database(db_path: str = DB_PATH):
    """Creates the natural_memories table and runs any pending schema migrations."""
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


def save_memory(memory: Memory, db_path: str = DB_PATH):
    """
    Saves a memory, deduplicating against existing memories with the same
    kind + topic. If a sufficiently similar memory already exists it is updated
    in place (preserving created_at) rather than inserting a duplicate.
    """
    if memory.embedding is None:
        memory = memory.model_copy(update={"embedding": generate_embedding(memory.content)})

    duplicate = _find_duplicate(memory, db_path)
    if duplicate:
        to_save = memory.model_copy(update={"id": duplicate.id, "created_at": duplicate.created_at})
        update_memory(to_save, db_path)
    else:
        _insert_memory(memory, db_path)


def update_memory(memory: Memory, db_path: str = DB_PATH):
    """Updates all mutable fields of an existing memory and refreshes timestamps."""
    now = datetime.now().isoformat()
    emb_json = json.dumps(memory.embedding) if memory.embedding is not None else None
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE natural_memories
            SET kind=?, topic=?, content=?, confidence=?, embedding=?,
                updated_at=?, last_seen_at=?
            WHERE id=?
            """,
            (memory.kind, memory.topic, memory.content, memory.confidence,
             emb_json, now, now, memory.id),
        )


def delete_memory(memory_id: str, db_path: str = DB_PATH):
    """Deletes a single memory by its id."""
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM natural_memories WHERE id = ?", (memory_id,))


def list_memories(db_path: str = DB_PATH) -> list[Memory]:
    """Returns all stored memories, newest first."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM natural_memories ORDER BY created_at DESC"
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_memory(r) for r in rows]


def search_memories(query: str, limit: int = 8, db_path: str = DB_PATH) -> list[Memory]:
    """
    Returns up to `limit` memories scored by token overlap with `query`.
    Tokens are normalized (lowercased, punctuation and underscores stripped).
    """
    words = _tokenize(query)
    if not words:
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM natural_memories ORDER BY created_at DESC"
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


def delete_memories_matching(query: str, db_path: str = DB_PATH) -> int:
    """Deletes ALL memories matching the query keywords. Returns the count deleted."""
    words = _tokenize(query)
    if not words:
        return 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM natural_memories").fetchall()
    finally:
        conn.close()

    to_delete = [
        _row_to_memory(r).id
        for r in rows
        if any(w in set(_tokenize(f"{r['topic']} {r['content']}")) for w in words)
    ]
    for memory_id in to_delete:
        delete_memory(memory_id, db_path=db_path)
    return len(to_delete)


# ── private helpers ────────────────────────────────────────────────────────────

def _insert_memory(memory: Memory, db_path: str = DB_PATH):
    emb_json = json.dumps(memory.embedding) if memory.embedding is not None else None
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO natural_memories
            (id, kind, topic, content, confidence, source,
             created_at, updated_at, last_seen_at, embedding)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (memory.id, memory.kind, memory.topic, memory.content,
             memory.confidence, memory.source,
             memory.created_at, memory.updated_at, memory.last_seen_at,
             emb_json),
        )


def _find_duplicate(memory: Memory, db_path: str) -> Memory | None:
    """
    Returns the most recent existing memory with the same kind + topic that is
    similar enough to be treated as the same logical memory, or None if no
    such memory exists.
    """
    candidates = _get_by_kind_topic(memory.kind, memory.topic, db_path)
    for candidate in candidates:
        if memory.embedding and candidate.embedding:
            if cosine_similarity(memory.embedding, candidate.embedding) >= _EMBED_THRESHOLD:
                return candidate
        elif _keyword_similarity(memory.content, candidate.content) >= _KEYWORD_THRESHOLD:
            return candidate
    return None


def _get_by_kind_topic(kind: str, topic: str, db_path: str) -> list[Memory]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM natural_memories WHERE kind=? AND topic=? ORDER BY created_at DESC",
            (kind, topic),
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
