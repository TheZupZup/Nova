import sqlite3
from datetime import datetime

DB_PATH = "nexus.db"


def _get_connection() -> sqlite3.Connection:
    """Retourne une connexion à la base de données locale."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_db():
    """Crée la table des souvenirs si elle n'existe pas encore."""
    with _get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                category  TEXT NOT NULL,
                content   TEXT NOT NULL,
                created   TEXT NOT NULL
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
