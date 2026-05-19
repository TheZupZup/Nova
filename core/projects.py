"""
Nova Projects / Workspaces — local-first project foundation (Phase 1).

A *project* is a lightweight, user-scoped container that lets Nova
organise conversations and memory by context (e.g. "Nova", "Auryn",
"SilentGuard", "Home Lab", "Personal"). It groups:

  * conversations that opt into ``project_id``
  * project-scoped memory (memory rows that carry the same ``project_id``)
  * an optional free-text description

Conversations and memories that do **not** carry a ``project_id`` remain
"General" / global and behave exactly as before this module existed —
there is no backfill and no reclassification of existing data.

Safety boundary (important): a project is *contextual user data only*.
Project name/description and project memory are surfaced to the model
the same way ordinary user memory is — strictly **below** the identity
and safety contract in the system prompt. Nothing in this module can
raise a project's priority above safety, identity, auth, or admin
rules; it only stores rows and answers ownership questions.

Scope is intentionally narrow (Phase 1):
  * create / list / get / rename+describe / archive
  * everything is scoped to ``user_id`` exactly like conversations
  * no deletes, no autonomous actions, no file/cloud behaviour

Dev Workspace link (Phase 1, read-only): a project may *optionally*
carry a ``local_repo_path``. Storing it only records a validated,
resolved absolute path — it grants Nova no power to modify the repo.
The path is validated by ``core.dev_workspace`` (must exist, contain
``.git``, and resolve inside an operator-configured allowed root); all
git access through that module is strictly read-only. Clearing the
link is always allowed and never touches the repository.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Optional

# Server-side caps. The UI applies matching limits, but the data layer
# re-enforces them so a crafted client cannot smuggle an oversized row.
PROJECT_NAME_MAX_LEN = 80
PROJECT_DESCRIPTION_MAX_LEN = 2_000

_PROJECTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    name        TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    archived_at TEXT
)
"""

_PROJECTS_USER_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_projects_user_id ON projects(user_id)"
)

# Phase 1 Dev Workspace: an optional, nullable column holding the
# validated absolute path of a linked local Git checkout. Added via an
# idempotent ALTER so existing installs gain it with no backfill — a
# project with no link keeps ``NULL`` and behaves exactly as before.
_PROJECTS_REPO_COLUMN = "local_repo_path"


class ProjectError(ValueError):
    """Raised for invalid project input (empty/oversized name, etc.).

    The web layer translates this into a 400 so the client gets a clear
    validation message instead of a 500.
    """


def _now_iso() -> str:
    return datetime.now().isoformat()


def _project_columns(conn: sqlite3.Connection) -> set[str]:
    return {
        row[1]
        for row in conn.execute("PRAGMA table_info(projects)").fetchall()
    }


def migrate(db_path: str) -> None:
    """Create the ``projects`` table + index if missing. Idempotent.

    Purely additive: this never touches conversations, memories, or any
    existing row. Requires the ``users`` table to exist (the chat data
    layer runs this after ``users.migrate()``), but does not depend on
    it having rows — an empty install simply has no projects yet.

    The ``local_repo_path`` column (Phase 1 Dev Workspace) is added by
    an idempotent ALTER guarded on ``PRAGMA table_info`` so re-running
    the migration on an install that already has the column is a no-op
    and never raises.
    """
    with sqlite3.connect(db_path) as conn:
        conn.execute(_PROJECTS_TABLE_SQL)
        conn.execute(_PROJECTS_USER_INDEX_SQL)
        if _PROJECTS_REPO_COLUMN not in _project_columns(conn):
            conn.execute(
                f"ALTER TABLE projects ADD COLUMN {_PROJECTS_REPO_COLUMN} TEXT"
            )


def _validate_name(name: str) -> str:
    if not isinstance(name, str):
        raise ProjectError("project name must be a string")
    name = name.strip()
    if not name:
        raise ProjectError("project name cannot be empty")
    if len(name) > PROJECT_NAME_MAX_LEN:
        raise ProjectError(
            f"project name too long (max {PROJECT_NAME_MAX_LEN} characters)"
        )
    return name


def _validate_description(description: Optional[str]) -> str:
    if description is None:
        return ""
    if not isinstance(description, str):
        raise ProjectError("project description must be a string")
    description = description.strip()
    if len(description) > PROJECT_DESCRIPTION_MAX_LEN:
        raise ProjectError(
            "project description too long "
            f"(max {PROJECT_DESCRIPTION_MAX_LEN} characters)"
        )
    return description


def _row_to_dict(row: sqlite3.Row) -> dict:
    # ``local_repo_path`` is read defensively: every read path runs
    # through ``initialize_db`` which applies the migration, but a
    # missing column must never turn a project list into a 500.
    keys = row.keys()
    repo_path = (
        row[_PROJECTS_REPO_COLUMN]
        if _PROJECTS_REPO_COLUMN in keys
        else None
    )
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "archived_at": row["archived_at"],
        "archived": row["archived_at"] is not None,
        "local_repo_path": repo_path or None,
        "has_local_repo": bool(repo_path),
    }


def create_project(
    name: str,
    user_id: int,
    description: Optional[str] = None,
    db_path: str | None = None,
) -> dict:
    """Create a project owned by ``user_id`` and return it as a dict.

    Raises :class:`ProjectError` for an empty / oversized name or an
    oversized description.
    """
    from core.memory import DB_PATH

    name = _validate_name(name)
    description = _validate_description(description)
    now = _now_iso()
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "INSERT INTO projects "
            "(user_id, name, description, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, name, description, now, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    finally:
        conn.close()
    return _row_to_dict(row)


def list_projects(
    user_id: int,
    include_archived: bool = False,
    db_path: str | None = None,
) -> list[dict]:
    """Return ``user_id``'s projects, newest first.

    Archived projects are hidden unless ``include_archived`` is True so
    the default sidebar list stays uncluttered.
    """
    from core.memory import DB_PATH

    conn = sqlite3.connect(db_path or DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        if include_archived:
            rows = conn.execute(
                "SELECT * FROM projects WHERE user_id = ? "
                "ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM projects "
                "WHERE user_id = ? AND archived_at IS NULL "
                "ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows]


def get_project(
    project_id: int, user_id: int, db_path: str | None = None
) -> Optional[dict]:
    """Return the project if it exists and belongs to ``user_id``, else None."""
    from core.memory import DB_PATH

    conn = sqlite3.connect(db_path or DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ? AND user_id = ?",
            (project_id, user_id),
        ).fetchone()
    finally:
        conn.close()
    return _row_to_dict(row) if row is not None else None


def project_belongs_to(
    project_id: int, user_id: int, db_path: str | None = None
) -> bool:
    """True iff the project exists and is owned by ``user_id``."""
    from core.memory import DB_PATH

    with sqlite3.connect(db_path or DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM projects WHERE id = ? AND user_id = ?",
            (project_id, user_id),
        ).fetchone()
    return row is not None


def is_active_project(
    project_id: int, user_id: int, db_path: str | None = None
) -> bool:
    """True iff the project exists, is owned by ``user_id`` and is not archived.

    Used to decide whether a *new* conversation may be opened inside a
    project. Existing conversations already tied to a now-archived
    project keep working — only the creation path consults this.
    """
    from core.memory import DB_PATH

    with sqlite3.connect(db_path or DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM projects "
            "WHERE id = ? AND user_id = ? AND archived_at IS NULL",
            (project_id, user_id),
        ).fetchone()
    return row is not None


def update_project(
    project_id: int,
    user_id: int,
    name: Optional[str] = None,
    description: Optional[str] = None,
    db_path: str | None = None,
) -> Optional[dict]:
    """Rename and/or re-describe a project the caller owns.

    Only the fields that are not ``None`` are changed. Returns the
    updated project, or ``None`` if it does not exist / belongs to
    another user (the web layer maps that to 404 so cross-user probing
    cannot reveal existence). Raises :class:`ProjectError` for invalid
    input.
    """
    from core.memory import DB_PATH

    resolved = db_path or DB_PATH
    existing = get_project(project_id, user_id, db_path=resolved)
    if existing is None:
        return None

    new_name = existing["name"] if name is None else _validate_name(name)
    new_description = (
        existing["description"]
        if description is None
        else _validate_description(description)
    )

    with sqlite3.connect(resolved) as conn:
        conn.execute(
            "UPDATE projects SET name = ?, description = ?, updated_at = ? "
            "WHERE id = ? AND user_id = ?",
            (new_name, new_description, _now_iso(), project_id, user_id),
        )
    return get_project(project_id, user_id, db_path=resolved)


def archive_project(
    project_id: int, user_id: int, db_path: str | None = None
) -> Optional[dict]:
    """Soft-archive a project (sets ``archived_at``). Non-destructive.

    The project's conversations and memory are left exactly as they are
    — archiving only hides the project from the default list. Returns
    the updated project, or ``None`` if it does not exist / is not owned
    by the caller. Idempotent: re-archiving keeps the first timestamp.
    """
    from core.memory import DB_PATH

    resolved = db_path or DB_PATH
    existing = get_project(project_id, user_id, db_path=resolved)
    if existing is None:
        return None
    if existing["archived_at"] is None:
        with sqlite3.connect(resolved) as conn:
            now = _now_iso()
            conn.execute(
                "UPDATE projects SET archived_at = ?, updated_at = ? "
                "WHERE id = ? AND user_id = ?",
                (now, now, project_id, user_id),
            )
    return get_project(project_id, user_id, db_path=resolved)


def unarchive_project(
    project_id: int, user_id: int, db_path: str | None = None
) -> Optional[dict]:
    """Clear ``archived_at`` so the project re-appears in the default list.

    Returns the updated project, or ``None`` if it does not exist / is
    not owned by the caller.
    """
    from core.memory import DB_PATH

    resolved = db_path or DB_PATH
    existing = get_project(project_id, user_id, db_path=resolved)
    if existing is None:
        return None
    with sqlite3.connect(resolved) as conn:
        conn.execute(
            "UPDATE projects SET archived_at = NULL, updated_at = ? "
            "WHERE id = ? AND user_id = ?",
            (_now_iso(), project_id, user_id),
        )
    return get_project(project_id, user_id, db_path=resolved)


# ── Dev Workspace link (Phase 1, read-only) ─────────────────────────
#
# Storing a ``local_repo_path`` only records *where* a linked checkout
# is. It confers no write power: every git access goes through
# ``core.dev_workspace``, which is allowlisted to read-only
# subcommands. The path is validated there before it is persisted, so
# the DB can never hold a path outside the operator's configured
# workspace roots.


def get_local_repo_path(
    project_id: int, user_id: int, db_path: str | None = None
) -> Optional[str]:
    """Return the linked repo path for a project the caller owns.

    ``None`` when the project does not exist, is not owned by the
    caller, or simply has no repo linked — the web layer maps the
    "not owned" case to 404 so existence is not leaked.
    """
    project = get_project(project_id, user_id, db_path=db_path)
    if project is None:
        return None
    return project.get("local_repo_path")


def set_local_repo_path(
    project_id: int,
    user_id: int,
    path: Optional[str],
    db_path: str | None = None,
) -> Optional[dict]:
    """Link / unlink a local Git checkout to a project the caller owns.

    ``path`` of ``None`` or an empty/whitespace string *unlinks* the
    repo (always safe, never touches the filesystem). A non-empty
    ``path`` is validated by :func:`core.dev_workspace.validate_repo_path`
    — it must exist, contain ``.git``, and resolve inside an
    operator-configured allowed root — and the *resolved* absolute
    path is what gets stored.

    Returns the updated project, or ``None`` if it does not exist /
    belongs to another user (the web layer maps that to 404). Raises
    :class:`ProjectError` with a short, safe message when a non-empty
    path fails validation so the web layer can return 400.
    """
    from core.memory import DB_PATH

    resolved_db = db_path or DB_PATH
    existing = get_project(project_id, user_id, db_path=resolved_db)
    if existing is None:
        return None

    if path is None or not str(path).strip():
        stored: Optional[str] = None
    else:
        # Imported lazily so the projects data layer has no hard
        # dependency on the dev-workspace module (import-cycle safe,
        # and projects keeps working if the feature is unused).
        from core import dev_workspace

        try:
            stored = str(dev_workspace.validate_repo_path(path))
        except dev_workspace.RepoPathError as exc:
            raise ProjectError(str(exc)) from exc

    with sqlite3.connect(resolved_db) as conn:
        conn.execute(
            "UPDATE projects SET local_repo_path = ?, updated_at = ? "
            "WHERE id = ? AND user_id = ?",
            (stored, _now_iso(), project_id, user_id),
        )
    return get_project(project_id, user_id, db_path=resolved_db)
