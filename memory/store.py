import sqlite3
from datetime import datetime
from memory.schema import Memory

DB_PATH = "nova.db"


def initialize_memory_database(db_path: str = DB_PATH):
    """Creates the natural_memories table if it doesn't already exist."""
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


def save_memory(memory: Memory, db_path: str = DB_PATH):
    """Inserts a new memory or replaces an existing one with the same id."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO natural_memories
            (id, kind, topic, content, confidence, source, created_at, updated_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory.id, memory.kind, memory.topic, memory.content,
                memory.confidence, memory.source,
                memory.created_at, memory.updated_at, memory.last_seen_at,
            ),
        )


def update_memory(memory: Memory, db_path: str = DB_PATH):
    """Updates mutable fields of an existing memory and refreshes timestamps."""
    now = datetime.now().isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE natural_memories
            SET kind=?, topic=?, content=?, confidence=?, updated_at=?, last_seen_at=?
            WHERE id=?
            """,
            (memory.kind, memory.topic, memory.content, memory.confidence, now, now, memory.id),
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
    Returns up to `limit` memories scored by keyword overlap with `query`.
    Matches are checked against topic + content (case-insensitive).
    """
    if not query.strip():
        return []
    words = [w.lower() for w in query.split() if len(w) > 2]
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
        haystack = f"{mem.topic} {mem.content}".lower()
        score = sum(1 for w in words if w in haystack)
        if score > 0:
            scored.append((score, mem))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [m for _, m in scored[:limit]]


def delete_memories_matching(query: str, db_path: str = DB_PATH) -> int:
    """Deletes all memories matching the query keywords. Returns the count deleted."""
    matches = search_memories(query, limit=200, db_path=db_path)
    for mem in matches:
        delete_memory(mem.id, db_path=db_path)
    return len(matches)


def _row_to_memory(row: sqlite3.Row) -> Memory:
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
    )
