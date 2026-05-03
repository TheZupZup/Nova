import sqlite3
import shutil
import os
from datetime import datetime
from typing import Optional
from memory.store import initialize_memory_database as _init_natural_memory
from core import users as _users
from core.settings import migrate_user_settings as _migrate_user_settings

DB_PATH = "nova.db"


def backup_db():
    """Copie nova.db → nova.db.backup avant chaque écriture."""
    backup = DB_PATH + ".backup"
    if os.path.exists(backup):
        try:
            os.replace(backup, backup + ".1")
        except OSError:
            pass
    if os.path.exists(DB_PATH):
        shutil.copy2(DB_PATH, backup)


def _get_connection() -> sqlite3.Connection:
    """Retourne une connexion à la base de données locale."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_setting(key: str, default: str = "") -> str:
    """Récupère un setting depuis la DB."""
    try:
        with _get_connection() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        return default


def save_setting(key: str, value: str):
    """Sauvegarde un setting dans la DB."""
    with _get_connection() as conn:
        conn.execute("""
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, value))


def initialize_db():
    """Crée toutes les tables si elles n'existent pas encore."""
    with _get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
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
                user_id   INTEGER REFERENCES users(id),
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
    _users.migrate(DB_PATH)
    _migrate_conversation_ownership(DB_PATH)
    _migrate_memories_ownership(DB_PATH)
    _migrate_user_settings(DB_PATH)
    _init_natural_memory(DB_PATH)


def save_memory(category: str, content: str, user_id: int):
    """Sauvegarde un nouveau souvenir attribué à `user_id`."""
    backup_db()
    with _get_connection() as conn:
        conn.execute(
            "INSERT INTO memories (category, content, created, user_id) "
            "VALUES (?, ?, ?, ?)",
            (category, content, datetime.now().isoformat(), user_id)
        )


def parse_and_save(result: str, user_id: int) -> bool:
    """
    Parse un résultat de l'LLM et sauvegarde la mémoire si le format est valide.
    Retourne True si une mémoire a été sauvegardée, False sinon.
    Le souvenir est attribué à `user_id`.
    """
    if not result or not isinstance(result, str):
        return False

    result = result.strip()
    if not result.startswith("SAVE:"):
        return False

    parts = result[5:].split(":", 1)
    if len(parts) != 2:
        return False

    category = parts[0].strip()
    content = parts[1].strip()

    if not category or not content:
        return False

    save_memory(category, content, user_id)
    return True


def load_memories(user_id: int) -> list[dict]:
    """Charge les souvenirs appartenant à `user_id`."""
    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT category, content FROM memories "
            "WHERE user_id = ? ORDER BY created ASC",
            (user_id,)
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


def _migrate_memories_ownership(db_path: str) -> None:
    """
    Add a user_id column to the memories table and backfill existing rows
    to the legacy admin (issue #106).

    Idempotent: returns immediately if the column is already present (after
    ensuring the index exists).

    The column is nullable for the migration itself, mirroring the
    conversations migration; all application paths set user_id explicitly
    via save_memory().
    """
    with sqlite3.connect(db_path) as conn:
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(memories)").fetchall()
        }
        if "user_id" in cols:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memories_user_id "
                "ON memories(user_id)"
            )
            return

        row = conn.execute(
            "SELECT id FROM users ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if row is None:
            raise RuntimeError(
                "cannot scope memories: users table is empty; "
                "users.migrate() must run first"
            )
        legacy_owner_id = row[0]

        conn.execute(
            "ALTER TABLE memories "
            "ADD COLUMN user_id INTEGER REFERENCES users(id)"
        )
        conn.execute(
            "UPDATE memories SET user_id = ? WHERE user_id IS NULL",
            (legacy_owner_id,),
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_user_id "
            "ON memories(user_id)"
        )


def _migrate_conversation_ownership(db_path: str) -> None:
    """
    Add a user_id column to the conversations table and backfill existing
    rows to the legacy admin (issue #105).

    Idempotent: returns immediately if the column is already present.

    The architecture doc (#103) specifies a nullable column for the initial
    migration, with NOT NULL enforcement deferred to a follow-up. All
    application paths set user_id explicitly via create_conversation().
    """
    with sqlite3.connect(db_path) as conn:
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(conversations)").fetchall()
        }
        if "user_id" in cols:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversations_user_id "
                "ON conversations(user_id)"
            )
            return

        row = conn.execute(
            "SELECT id FROM users ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if row is None:
            raise RuntimeError(
                "cannot scope conversations: users table is empty; "
                "users.migrate() must run first"
            )
        legacy_owner_id = row[0]

        conn.execute(
            "ALTER TABLE conversations "
            "ADD COLUMN user_id INTEGER REFERENCES users(id)"
        )
        conn.execute(
            "UPDATE conversations SET user_id = ? WHERE user_id IS NULL",
            (legacy_owner_id,),
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_conversations_user_id "
            "ON conversations(user_id)"
        )


def create_conversation(title: str, user_id: int) -> int:
    """Crée une nouvelle conversation pour `user_id` et retourne son ID."""
    now = datetime.now().isoformat()
    with _get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO conversations (user_id, title, created, updated) "
            "VALUES (?, ?, ?, ?)",
            (user_id, title, now, now)
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


def load_conversations(user_id: int) -> list[dict]:
    """Charge les conversations de `user_id` triées par date."""
    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT id, title, updated FROM conversations "
            "WHERE user_id = ? ORDER BY updated DESC",
            (user_id,)
        ).fetchall()
    return [{"id": row["id"], "title": row["title"], "updated": row["updated"]} for row in rows]


def conversation_belongs_to(conversation_id: int, user_id: int) -> bool:
    """True si la conversation existe et appartient à `user_id`."""
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        ).fetchone()
    return row is not None


def load_conversation_messages(
    conversation_id: int, user_id: int
) -> Optional[list[dict]]:
    """
    Charge les messages d'une conversation appartenant à `user_id`.

    Retourne None si la conversation n'existe pas ou n'appartient pas à
    l'utilisateur — l'appelant doit traduire en 404 pour ne pas révéler
    l'existence d'une conversation d'un autre utilisateur.
    """
    if not conversation_belongs_to(conversation_id, user_id):
        return None
    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT role, content, model FROM messages WHERE conversation_id = ? ORDER BY created ASC",
            (conversation_id,)
        ).fetchall()
    return [{"role": row["role"], "content": row["content"], "model": row["model"]} for row in rows]


def delete_conversation(conversation_id: int, user_id: int) -> bool:
    """
    Supprime une conversation appartenant à `user_id` et ses messages.

    Retourne True si la suppression a eu lieu, False si la conversation
    n'existe pas ou appartient à un autre utilisateur.
    """
    with _get_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        )
        if cursor.rowcount == 0:
            return False
        conn.execute(
            "DELETE FROM messages WHERE conversation_id = ?", (conversation_id,)
        )
    return True


def list_memories(user_id: int) -> list[dict]:
    """Liste les souvenirs de `user_id` avec id et created — pour l'interface web."""
    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT id, category, content, created FROM memories "
            "WHERE user_id = ? ORDER BY created DESC",
            (user_id,)
        ).fetchall()
    return [{"id": r["id"], "category": r["category"], "content": r["content"], "created": r["created"]} for r in rows]


def update_memory(memory_id: int, category: str, content: str, user_id: int) -> bool:
    """
    Met à jour un souvenir s'il appartient à `user_id`.
    Retourne True si une ligne a été modifiée, False sinon.
    """
    backup_db()
    with _get_connection() as conn:
        cursor = conn.execute(
            "UPDATE memories SET category = ?, content = ? "
            "WHERE id = ? AND user_id = ?",
            (category, content, memory_id, user_id)
        )
    return cursor.rowcount > 0


def delete_memory(memory_id: int, user_id: int) -> bool:
    """
    Supprime un souvenir s'il appartient à `user_id`.
    Retourne True si une ligne a été supprimée, False sinon.
    """
    backup_db()
    with _get_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM memories WHERE id = ? AND user_id = ?",
            (memory_id, user_id)
        )
    return cursor.rowcount > 0


def cleanup_old_knowledge(user_id: int, max_count: int = 500):
    """Garde seulement les max_count dernières mémoires de type knowledge pour `user_id`."""
    backup_db()
    with _get_connection() as conn:
        conn.execute("""
            DELETE FROM memories
            WHERE category = 'knowledge' AND user_id = ?
            AND id NOT IN (
                SELECT id FROM memories
                WHERE category = 'knowledge' AND user_id = ?
                ORDER BY created DESC
                LIMIT ?
            )
        """, (user_id, user_id, max_count))
