import sqlite3
from datetime import datetime

DB_PATH = "nova.db"


def _get_connection() -> sqlite3.Connection:
    """Retourne une connexion à la base de données locale."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_db():
    """Crée toutes les tables si elles n'existent pas encore."""
    with _get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                category  TEXT NOT NULL,
                content   TEXT NOT NULL,
                created   TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                title     TEXT NOT NULL,
                created   TEXT NOT NULL,
                updated   TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role            TEXT NOT NULL,
                content         TEXT NOT NULL,
                model           TEXT,
                created         TEXT NOT NULL,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id)
            )
        """)


def save_memory(category: str, content: str):
    """Sauvegarde un nouveau souvenir dans la base."""
    with _get_connection() as conn:
        conn.execute(
            "INSERT INTO memories (category, content, created) VALUES (?, ?, ?)",
            (category, content, datetime.now().isoformat())
        )


def load_memories() -> list[dict]:
    """Charge tous les souvenirs existants."""
    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT category, content FROM memories ORDER BY created ASC"
        ).fetchall()
    return [{"category": row["category"], "content": row["content"]} for row in rows]


def format_memories_for_prompt(memories: list[dict]) -> str:
    """Formate les souvenirs en texte injecté dans le prompt système."""
    if not memories:
        return ""
    lines = ["Ce que tu sais déjà sur l'utilisateur :"]
    for m in memories:
        lines.append(f"- [{m['category']}] {m['content']}")
    return "\n".join(lines)


def create_conversation(title: str) -> int:
    """Crée une nouvelle conversation et retourne son ID."""
    now = datetime.now().isoformat()
    with _get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO conversations (title, created, updated) VALUES (?, ?, ?)",
            (title, now, now)
        )
        return cursor.lastrowid


def update_conversation_title(conversation_id: int, title: str):
    """Met à jour le titre d'une conversation."""
    with _get_connection() as conn:
        conn.execute(
            "UPDATE conversations SET title = ?, updated = ? WHERE id = ?",
            (title, datetime.now().isoformat(), conversation_id)
        )


def update_conversation_timestamp(conversation_id: int):
    """Met à jour le timestamp d'une conversation."""
    with _get_connection() as conn:
        conn.execute(
            "UPDATE conversations SET updated = ? WHERE id = ?",
            (datetime.now().isoformat(), conversation_id)
        )


def save_message(conversation_id: int, role: str, content: str, model: str = None):
    """Sauvegarde un message dans une conversation."""
    with _get_connection() as conn:
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, model, created) VALUES (?, ?, ?, ?, ?)",
            (conversation_id, role, content, model, datetime.now().isoformat())
        )
    update_conversation_timestamp(conversation_id)


def load_conversations() -> list[dict]:
    """Charge toutes les conversations triées par date."""
    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT id, title, updated FROM conversations ORDER BY updated DESC"
        ).fetchall()
    return [{"id": row["id"], "title": row["title"], "updated": row["updated"]} for row in rows]


def load_conversation_messages(conversation_id: int) -> list[dict]:
    """Charge tous les messages d'une conversation."""
    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT role, content, model FROM messages WHERE conversation_id = ? ORDER BY created ASC",
            (conversation_id,)
        ).fetchall()
    return [{"role": row["role"], "content": row["content"], "model": row["model"]} for row in rows]


def delete_conversation(conversation_id: int):
    """Supprime une conversation et ses messages."""
    with _get_connection() as conn:
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
