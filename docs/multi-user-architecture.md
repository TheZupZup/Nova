# Multi-User and Family Controls Architecture

> **Status: design plan only.** Nova today is a **single-user** application.
> Nothing in this document is implemented yet. This file exists to align
> contributors *before* code is written, and to give reviewers a stable target
> to argue against when issues #103–#112 land.
>
> Do not cite this document as evidence that multi-user support exists.

---

## 1. Current single-user limitations

Nova currently has no concept of an account. The relevant facts as of this
writing:

- `core/auth.py` reads a single `NOVA_USERNAME` / `NOVA_PASSWORD` from the
  environment. Every browser session that authenticates with those credentials
  is the same logical actor.
- The JWT payload is `{"exp": ...}` only — no `sub`, no user id, no role. A
  valid token grants full access to every endpoint.
- The SQLite schema in `core/memory.py` and `memory/store.py` defines
  `settings`, `memories`, `conversations`, `messages`, and `natural_memories`.
  **None of these tables carry an owner column.** Every row is global.
- Model selection, memory saving, web search, weather, and prompt handling are
  governed by global state (DB `settings` rows, env vars) — not per-user
  preferences.
- There is no concept of an admin vs. a regular user. The first credential is
  the only credential.

Practical consequences:

- Any second person who logs in sees the first person's full conversation
  history and memory store.
- Disabling a feature (e.g. web search) disables it for everyone.
- There is no way to give a child or guest a restricted view.

## 2. Data ownership gaps

The following tables need an owner before multi-user makes sense. This is the
gap a migration must close:

| Table              | Current owner    | Needs                                  |
| ------------------ | ---------------- | -------------------------------------- |
| `conversations`    | global           | `user_id` (FK → `users.id`)            |
| `messages`         | via conversation | inherits via `conversation_id`         |
| `memories`         | global           | `user_id`                              |
| `natural_memories` | global           | `user_id`                              |
| `settings`         | global           | split: global vs. per-user             |

`settings` is the trickiest case. Some keys (e.g. RAM budget, default routing
model) are genuinely host-wide. Others (e.g. preferred mode, memory toggles)
should become per-user. Migration must classify each existing key.

## 3. Proposed roles

Three roles, deliberately few:

- **admin** — manages accounts, models, and family controls. Can pull/remove
  Ollama models. Can assign allowed models and modes to users. Can reset a
  user's password.
- **user** — normal adult account. Owns their own conversations, memories,
  and per-user settings. Not subject to family controls by default.
- **child / restricted** — a `user` with `is_restricted=true` and a non-empty
  `family_controls` row. Can only use models the admin has assigned, subject
  to limits.

A single role enum column (`admin` | `user`) plus a boolean `is_restricted`
keeps the model simple without over-fitting to one family shape. A household
with only adults uses `admin` + `user` and never touches restrictions.

## 4. Privacy principle

**Admin controls must not become surveillance.** This is a hard constraint,
not a preference. The default behavior of the system should isolate users
from each other, including from admins.

Specifically:

- Admins **cannot** read another user's conversations or memories through any
  default endpoint or UI surface. Conversations are scoped to their owner.
- Admins **can** manage accounts (create, disable, reset password) and limits
  (daily message cap, allowed models, quiet hours).
- Admins **can** see aggregate, non-content metadata when needed for limit
  enforcement (e.g. "user X sent 47 messages today") but not the message text.
- A child / restricted account **must** be visibly told, in plain language on
  first login, what is and is not visible to the admin. No silent monitoring.
- Any future "view child's chat" feature would require the child's explicit,
  per-session opt-in and would be out of scope for v1.

If a feature can only be implemented by giving admins read access to other
users' content, it does not ship.

## 5. Proposed schema

See `docs/multi-user-schema.sql` for the full SQL sketch. Summary:

- New table `users` (id, username, password_hash, role, is_restricted,
  created_at, disabled_at).
- New table `family_controls` (user_id, daily_message_limit, allowed_modes,
  web_search_enabled, weather_enabled, memory_save_enabled,
  memory_import_enabled, max_prompt_chars, quiet_hours_start, quiet_hours_end).
- New table `model_registry` (name, display_name, family, size_bytes,
  installed_at, is_admin_only).
- New table `user_allowed_models` (user_id, model_name).
- Add `user_id` columns to `conversations`, `memories`, `natural_memories`,
  and a per-user settings table.
- JWT payload gains `sub` (user id) and `role`.

## 6. Migration strategy from existing single-user `nova.db`

The migration must be safe to run against a database that has been in use for
months. Approach:

1. **Backup.** Copy `nova.db` to `nova.db.preupgrade-<timestamp>` before any
   schema change. Refuse to proceed if the backup write fails.
2. **Create new tables** (`users`, `family_controls`, `model_registry`,
   `user_allowed_models`, `user_settings`). Do not drop anything.
3. **Seed the legacy admin.** Insert one row into `users` using the existing
   `NOVA_USERNAME` and re-hashing `NOVA_PASSWORD` into `password_hash`. Role
   = `admin`. Call this user the *legacy owner*.
4. **Backfill ownership.** Add nullable `user_id` columns to `conversations`,
   `memories`, `natural_memories`. Set every existing row's `user_id` to the
   legacy owner. Then make the column `NOT NULL` in a follow-up step once the
   backfill is verified.
5. **Reclassify `settings`.** Walk known keys. Host-wide keys stay in
   `settings`. Per-user keys move to `user_settings` under the legacy owner's
   id. Unknown keys stay in `settings` to avoid data loss.
6. **Idempotency.** Each step checks whether it has already run (presence of
   the new column, presence of the legacy user row). Re-running the migration
   is a no-op.
7. **Down-migration.** Out of scope. We do not promise to undo this.

Migration runs on startup, behind a version marker stored in `settings`
(`schema_version`). v1 of the multi-user release is `schema_version = 2`.

## 7. Family controls

All controls live in the `family_controls` row tied to a restricted user.
Absence of a row = no restrictions.

| Control                | Type     | Default        | Notes                                         |
| ---------------------- | -------- | -------------- | --------------------------------------------- |
| `daily_message_limit`  | int      | NULL (none)    | Counted in UTC day of the host                |
| `allowed_modes`        | CSV      | `chat`         | Subset of {auto, chat, code, deep}            |
| `web_search_enabled`   | bool     | false          | Disables the web search tool entirely         |
| `weather_enabled`      | bool     | true           | Disables weather tool                         |
| `memory_save_enabled`  | bool     | false          | Disables auto + manual memory writes          |
| `memory_import_enabled`| bool     | false          | Disables `memory_importer` for this user      |
| `max_prompt_chars`     | int      | 2000           | Server-side cap; UI also enforces it          |
| `quiet_hours_start`    | HH:MM    | NULL           | Local TZ of host. Implemented later (#?).     |
| `quiet_hours_end`      | HH:MM    | NULL           | Window during which chat is disabled.         |

Quiet hours are explicitly deferred — the column exists in the schema so the
later PR doesn't reshape the table, but enforcement is a follow-up issue.

Enforcement happens in the request path, not the UI alone. UI hides controls
the user is not allowed to use; the server independently rejects forbidden
requests with a 403 carrying a machine-readable reason code
(`mode_not_allowed`, `daily_limit_reached`, etc.).

## 8. Model registry and per-user model assignment

Today, model names like `gemma3:1b` leak into the UI through the mode
selector and settings. For a child account this is wrong on two axes: it
exposes implementation detail, and it lets the user pick a model the admin
hasn't approved.

Proposal:

- **`model_registry`** is the source of truth for "what models exist on this
  host". Populated by the admin (manual add / Ollama pull). Each row carries
  a human-readable `display_name` ("General chat", "Code assistant") that
  child / restricted users see instead of the raw name.
- **`user_allowed_models`** is the per-user allowlist. For an unrestricted
  user this table is empty and the user sees everything in the registry. For
  a restricted user, only listed models appear.
- **Mode → model resolution** stays server-side. The router picks a concrete
  model from the user's allowed set; if the routing decision lands on a
  forbidden model, the router falls back to the user's allowed default.
- **Raw model names are admin-only.** The `/admin/models` view shows
  `gemma3:1b`. A child's UI shows "General chat". The API never returns the
  raw model name to a restricted user.

## 9. Ollama model pull design

Pulling a model from the Ollama registry is the most dangerous new surface
in this whole design. It is the one place where a string from an HTTP request
becomes an argument to a long-running subprocess. Constraints:

- **Admin-only.** Endpoint requires `role == admin`. Non-admin requests are
  404, not 403, to avoid advertising the surface.
- **Validated names.** The model name is matched against a strict regex
  (`^[a-z0-9._-]+(:[a-z0-9._-]+)?$`) before being passed anywhere. Names that
  fail validation are rejected with 400 and never reach the subprocess layer.
- **No shell.** The pull is invoked via the Ollama HTTP API
  (`POST /api/pull`) or via `subprocess.run([...], shell=False)` with an
  argv list. The validated name is the only user-controlled element.
- **No arbitrary commands.** There is no "run any ollama command" endpoint.
  Only `pull`, `list`, and `delete` are exposed, each with their own
  validated handler.
- **Background execution.** Pulls can take minutes. The handler enqueues a
  task (in-process task queue is fine for v1; do not block the event loop)
  and returns a job id immediately.
- **Progress / status.** A `/admin/models/jobs/{id}` endpoint returns
  `{state, bytes_done, bytes_total, error}`. The UI polls. Streaming via
  SSE/WebSocket is a possible follow-up but not required for v1.
- **Concurrency.** At most one pull runs at a time. Additional pulls queue.
  This keeps disk and network behavior predictable.
- **Cancellation.** Out of scope for v1. Pulls run to completion or fail.

## 10. Security and privacy risks, and mitigations

| Risk                                         | Mitigation                                                                 |
| -------------------------------------------- | -------------------------------------------------------------------------- |
| Admin endpoints reachable by non-admins      | Single auth dependency that checks `role == admin`; default-deny           |
| Cross-user data leak via missing WHERE       | Every query that touches owned tables must filter by `user_id`; add tests |
| JWT replay across users after role change    | Include `role` in JWT; on role change, rotate `token_version` per user     |
| Command injection via model name             | Strict regex + argv-only subprocess + admin-only endpoint                  |
| Resource exhaustion via parallel pulls       | Single-job queue; reject concurrent pull requests                          |
| Surveillance creep (admin reading chats)     | No code path returns another user's messages; tested with negative tests   |
| Restricted user sees raw model names         | Display name resolution at the API layer, not the UI                       |
| Migration corrupts existing single-user data | Pre-migration backup; idempotent steps; schema_version gate                |
| Password reset abused by admin to read chats | Reset rotates password but does not grant access to existing data; audit  |
| Memory import bypassing per-user toggles     | Importer reads `family_controls.memory_import_enabled` before running      |

## 11. PR breakdown matching issues #103–#112

The mapping below is the intended decomposition. Each PR should be reviewable
in isolation and shippable without the next one.

- **#103 — Schema and migration foundation.** Add `users`, `family_controls`,
  `model_registry`, `user_allowed_models`, `user_settings`. Add nullable
  `user_id` to owned tables. Backfill to legacy owner. No behavior change.
- **#104 — Identity in auth.** JWT carries `sub` and `role`. Login resolves
  against `users` instead of env-only credentials. Env credentials become the
  bootstrap admin only when `users` is empty.
- **#105 — Per-user data scoping.** Every query that reads owned tables
  filters by `user_id`. Add cross-user negative tests.
- **#106 — Admin user management UI/API.** Create / disable / reset users.
  Admin-only.
- **#107 — Family controls schema enforcement.** Server enforces allowed
  modes, daily limits, prompt length, web search, weather, memory toggles.
- **#108 — Model registry.** Admin can list / add / remove models. Display
  names. Per-user allowlist.
- **#109 — Ollama pull (background, validated, admin-only).**
- **#110 — Restricted-user UX.** Display names instead of raw model names;
  hidden controls; clear error messages.
- **#111 — Quiet hours enforcement.**
- **#112 — Audit log for admin actions** (account creation, role change,
  model pull, password reset). Append-only. Visible to admins only.

This ordering is a suggestion, not a contract. #103 and #104 must land
before anything else. #109 must not land before #108.

## 12. Test plan

Each PR carries its own tests; the architectural-level cases that must exist
by the end of the series:

- **Auth / identity**
  - Login with valid creds for each role yields a JWT whose `sub` and `role`
    match.
  - JWT signed with a different secret is rejected.
  - JWT for a disabled user is rejected even if not yet expired.
- **Data scoping (cross-user negative tests)**
  - User A cannot read User B's conversations, messages, memories, or
    natural memories via any documented endpoint.
  - Admin gets the same 403/404 as anyone else when reading another user's
    conversation content.
- **Family controls**
  - Restricted user hitting a disallowed mode gets a 403 with reason code.
  - Daily message limit blocks the (N+1)th message and resets at host UTC
    midnight.
  - Prompt longer than `max_prompt_chars` is rejected by the server even if
    the UI is bypassed.
  - Memory save toggle off → no rows written to `memories` or
    `natural_memories` for that user.
- **Model registry**
  - Restricted user listing models sees only display names from their
    allowlist.
  - Restricted user requesting a model outside their allowlist gets 403.
- **Ollama pull**
  - Non-admin gets 404.
  - Admin with an invalid model name gets 400 and no subprocess is spawned.
  - Admin with a valid name gets a job id; polling reports progress; second
    concurrent pull queues rather than runs.
- **Migration**
  - Running the migration twice on the same DB is a no-op the second time.
  - After migration, all pre-existing rows are owned by the legacy admin.
  - `schema_version` is set to 2.

## 13. Non-goals for v1

Stated explicitly so reviewers can push back if a PR drifts:

- No SSO / OIDC / external identity providers.
- No multi-tenant isolation across hosts. One Nova instance, one household.
- No admin read access to other users' chat content. Not now, not via a
  hidden flag.
- No realtime push of family-control changes — a user's next request picks up
  the new limits, that's enough.
- No model fine-tuning or training UI.
- No quotas beyond daily message count (no token quotas, no GPU-time quotas).
- No mobile app. Web UI only, as today.
- No down-migration path.
- No audit log UI beyond a flat list (search/filter is later).
- No "supervised mode" where an admin watches a child's session live.

---

*This document is the design contract for issues #103–#112. If an
implementation PR contradicts this file, either the PR or this file is
wrong — reconcile them in review before merging.*
