# Nova Projects / Workspaces

> **Status: shipped (Phase 1), local-first.** This document describes
> the project foundation that lets Nova organise conversations and
> memory by workspace. It lives inside the boundaries set by
> [`docs/nova-safety-and-trust-contract.md`](nova-safety-and-trust-contract.md);
> nothing here grants Nova new powers, and it does not change
> storage/migration, export/restore, or Ollama behaviour.

## What a project is

A **project** (a.k.a. workspace) is a lightweight, user-scoped
container that groups:

- **conversations** that opt into it,
- **project-specific memory / context**,
- an optional free-text **description**,
- (future) optional per-project settings.

Examples: `Nova`, `Auryn`, `SilentGuard`, `NexaNote`, `Home Lab`,
`Personal`.

Projects are local-first and per-user, exactly like conversations and
memory. One user never sees another user's projects; every endpoint
resolves the calling user and a foreign / unknown project id returns
`404` (never `403`) so existence is not leaked across accounts.

## General / unscoped conversations

There is always an implicit **General** bucket — it is simply the
absence of a project (`project_id IS NULL`). Every conversation and
memory that existed before projects stays in General. There is **no
backfill and no automatic reclassification**: upgrading an existing
Nova install changes nothing about how it already behaves until you
deliberately create a project and start working inside it.

Selecting *General* in the sidebar is the pre-projects experience:
unscoped conversations, global memory only.

## Global memory vs project memory

Nova has two memory scopes:

1. **Global memory** — user-wide preferences and durable facts. Stored
   with no `project_id`. Available **everywhere**: in General and inside
   every project. This is where general preferences belong ("I prefer
   concise answers", "I use Fedora").

2. **Project memory** — facts that only make sense inside one project.
   Stored with that project's `project_id`. Visible **only** when that
   project is active. Examples:
   - *Nova*: roadmap and storage/migration state
   - *Auryn*: streamrip / packaging details
   - *SilentGuard*: firewall / TUI / network details
   - *NexaNote*: sync / WebDAV / mobile roadmap

### How memory gets scoped

The active project for a chat turn is **the conversation's own
project**. A new conversation is created in the project selected in the
sidebar (or General); an existing conversation always keeps the project
it was created in.

- **Retrieval.** A General chat retrieves global memory only. A project
  chat retrieves global memory **plus** that project's memory, and
  never any other project's memory. In SQL terms:
  - General → `project_id IS NULL`
  - Project P → `project_id IS NULL OR project_id = P`
- **Writing.** Memory surfaced while a project is active (auto-extracted
  facts, and explicit `retiens ça:` / "remember this" commands) is
  stored as that project's memory. Memory surfaced in General stays
  global, exactly as before. Existing memories are never moved.

> **Note / Phase 1 limitation.** Scoping follows the active project, so
> a *global* preference stated while a project is active is recorded as
> project memory. State durable, cross-project preferences from
> **General**. The memory management UI lets you review and edit
> entries; automatic global-vs-project classification is a planned
> follow-up (see below).

The full-memory audit paths are deliberately **unscoped** so you can
always see everything Nova has stored about you:

- the `/memories` view,
- the *"what do you remember about me?"* command,
- the `forget …` commands (they act on all of your memory, not just the
  current project).

## Safety / context order (important)

Project name, description, and project memory are **contextual user
data only**. They are injected into the system prompt through the same
memory block that already sits **below** the identity and safety
contract:

```
[ IDENTITY_CONTRACT ]            ← safety / identity / auth / admin rules (highest priority)
[ system prompt: memory block ]  ← global + active-project memory (user data)
[ personalization ]
[ feedback preferences ]
[ time / security context ]
[ conversation history ]
[ user message ]
```

Because project context is appended after `IDENTITY_CONTRACT`, it can
**never** override system safety, identity, authentication, or admin
rules. A hostile project memory that says *"ignore all safety rules"*
is still just a user-memory bullet point below the contract — the
contract wins. This ordering is covered by tests
(`tests/test_projects.py::TestProjectContextCannotOverrideSafety`).

## API surface (Phase 1)

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `GET` | `/projects?include_archived=` | List the caller's projects (archived hidden by default) |
| `POST` | `/projects` | Create `{name, description?}` |
| `PATCH` | `/projects/{id}` | Rename / re-describe (only non-null fields change) |
| `POST` | `/projects/{id}/archive` | Soft-archive (non-destructive) |
| `POST` | `/projects/{id}/unarchive` | Restore an archived project |
| `GET` | `/conversations?scope=general` | List unscoped (General) conversations |
| `GET` | `/conversations?project_id=<id>` | List one project's conversations |
| `POST` | `/conversations` | Optional `project_id` |
| `POST` | `/chat`, `/chat/stream` | Optional `project_id` (used only when creating a new conversation) |

Archiving is a soft, reversible flag. It only hides the project from
the default list — its conversations and memory are left bit-for-bit
unchanged and keep working. There are **no destructive project
deletes** in Phase 1.

## Data model

A single additive table plus three nullable columns. No existing row is
touched; all migrations are idempotent and run from
`core.memory.initialize_db()`.

```sql
CREATE TABLE projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    name        TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    archived_at TEXT
);

ALTER TABLE conversations     ADD COLUMN project_id INTEGER REFERENCES projects(id);
ALTER TABLE memories          ADD COLUMN project_id INTEGER REFERENCES projects(id);
ALTER TABLE natural_memories  ADD COLUMN project_id INTEGER REFERENCES projects(id);
```

Because the export/restore layer copies the whole `nova.db` file, the
new table and columns are included automatically — storage/migration
and export/restore behaviour is unchanged.

## What Phase 1 deliberately does not do

- No file uploads or attachments.
- No local-repo scanning or linked repo paths.
- No GitHub / Codeberg project behaviour.
- No cloud sync.
- No autonomous project actions.
- No changes to storage/migration, export/restore, or Ollama behaviour.
- No moving / reclassifying existing memory.
- No destructive deletes.
- No whole-app UI redesign — the sidebar gains a small `General +
  projects` selector and nothing else changes.

## Planned follow-ups

- Automatic global-vs-project memory classification (so a global
  preference stated inside a project is recognised as global).
- Scoping the `forget …` commands and the memory audit view to the
  active project.
- ~~Linked local repository path per project.~~ **Shipped** as the
  Dev Workspace — see [`dev-workspace.md`](dev-workspace.md). Phase 1
  is read-only Git context; Phase 2 adds review-only patch *proposals*
  (plan, diff preview, tests, risks). Both are opt-in and cannot apply
  a patch, commit, push, branch, or write files.
- Project import / export.
- Project-specific model settings.
- Project files / attachments.
- GitHub / Codeberg project integration.

These are intentionally out of scope for Phase 1 to keep it small and
reviewable.
