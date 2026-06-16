# Nova Memory Pack

A **Nova Memory Pack** is a small, portable `.zip` of *structured JSON*
that lets a user carry their useful Nova context — long-term memories,
conversations, generated summaries, and safe preferences — from one
Nova instance to another so the assistant can "remember them" across
installs and devices.

It is **local-only**, **authenticated** (every signed-in user can
export/import their *own* data), **preview-first**, and
**merge-by-default**. It never leaves the host on its own and it never
contains secrets.

## How it differs from the other export features

Nova ships three related-but-distinct features. Don't confuse them:

| Feature | Module | Scope | Format | Who |
|---|---|---|---|---|
| **Nova Memory Pack** (this doc) | `core/memory_pack.py` | one user's curated data | `.zip` of JSON | any authenticated user |
| Data export / migration | `core/data_export.py` | the whole host (`nova.db` + dirs) | `.tar.gz` raw DB | **admin only** (`/admin/storage/*`) |
| Markdown memory pack import | `core/memory_importer.py` | memories only, import side | Markdown text | any user (programmatic) |

The Memory Pack is the right tool for *"move me to a new laptop"*. The
admin data export is the right tool for *"migrate the whole server to a
new disk"*.

## Using it (Settings → Data)

Open **Settings → Data**. There are two controls, available to every
authenticated user (they are **not** admin/alpha gated):

* **Export Nova Memory Pack** — confirms a privacy warning, then
  downloads `nova-memory-pack-<timestamp>.zip`. A copy is also written
  into `NOVA_DATA_DIR/memory-packs/` so Docker installs keep one on the
  persistent volume.
* **Import Nova Memory Pack** — pick a `.zip`. Nova shows a **preview**
  ("would add N new memories, M new conversations, …") and writes
  nothing until you click **Merge into my account**.

## What the ZIP contains

```
manifest.json        format id + version, counts, file hashes, exclusions
profile.json         { user: {username, role, created_at}, personalization }
memories.json        { classic: [...], natural: [...] }   (no embeddings)
conversations.json   { conversations: [ {title, created, updated, project,
                                         messages: [...] } ] }
summaries.json       { session_continuity: <generated recap> }
settings.json        { user_settings: { key: value } }    (safe keys only)
attachments/         reserved for a future version (unused in v1)
```

### Data sources exported (scoped to the signed-in `user_id`)

| File | Table / source |
|---|---|
| `profile.json` | `users` (no `password_hash`) + `core.settings.get_personalization()` |
| `memories.json` → `classic` | `memories` (`category, content, created, project`) |
| `memories.json` → `natural` | `natural_memories` (`kind, topic, content, confidence, source, timestamps, project`; **embeddings dropped**) |
| `conversations.json` | `conversations` + `messages` (messages nested per conversation) |
| `summaries.json` | `core.session_continuity.build_session_continuity()` — generated at export time, not stored |
| `settings.json` | `user_settings` (secret-filtered) |

Project grouping is preserved by **name**: a memory/conversation tied to
a project records the project name inline, and import get-or-creates a
project of that name for the target user.

## What is intentionally excluded

The pack **never** contains:

* password hashes (`users.password_hash`),
* API keys, GitHub / OAuth tokens and client secrets, the JWT signing
  secret, session cookies — none of which live in the user-scoped tables
  anyway (they are environment configuration),
* the host (global) `settings` table,
* memory embeddings (regenerated locally on use),
* any per-user setting whose **key or value looks secret-shaped**
  (substrings like `token`, `secret`, `password`, `api_key`,
  `oauth`, `client_secret`, … or a 40+ char base64/hex-looking run).
  This is defense-in-depth so a *future* sensitive per-user key cannot
  leak through the export.

The `manifest.json` lists these exclusions and carries a
`privacy_notice` reminding the user the pack may hold personal
conversation history.

## Import: merge & de-duplication

Import is **merge-only** in v1 and **non-destructive**:

* **keep existing data** — nothing the target already has is changed,
* **import what's missing** — only new items are added,
* **de-duplicate** on content so re-importing the same pack is a no-op:
  * classic memories on `(category, normalized content)`,
  * natural memories on `(kind, topic, normalized content)`,
  * conversations on `(title, created)` (and their messages come with
    them only when the conversation itself is new),
  * settings: a key is applied **only if the user does not already have
    it** — an existing preference is never overwritten,
  * projects: get-or-created by name.

A **preview / dry-run** step (`/memory-pack/import/preview`, and the UI
preview) reports `new` vs. `duplicate` counts plus warnings, and writes
nothing. The real import requires explicit confirmation
(`?confirm=true`).

The entire merge runs inside **one SQLite transaction**. If anything
fails part-way, the transaction is rolled back and the existing database
is left exactly as it was — a failed import never corrupts your data.

## Security & validation

The import path is built for hostile input. Before writing anything it:

* rejects non-zip / empty uploads,
* rejects **path traversal** (`../`), absolute paths, Windows drive
  letters, backslashes, and **symlink** members,
* caps the member count, the per-file size, and the total uncompressed
  size (**zip-bomb** guard),
* rejects a missing / non-JSON / wrong-format `manifest.json`,
* rejects a `format_version` newer than this build understands (with a
  clear "update Nova" message — forward-compatible rather than
  mis-reading a future format),
* parses every data file as JSON with a hard size cap and shape check.

Uploads larger than **50 MB** are rejected with `413` before being
buffered. Importing honours the same per-account memory-save policy gate
as the manual "add memory" action.

## API

All endpoints require a normal Bearer token (any authenticated user);
none are admin-gated. The `.zip` is sent as the **raw request body**
(no multipart), which lets the server enforce a strict size cap.

| Method & path | Purpose |
|---|---|
| `POST /memory-pack/export` | Build + download the pack. Streams `application/zip` with `Content-Disposition` and `X-Nova-Memory-Pack-*` count headers; also writes a copy to `NOVA_DATA_DIR/memory-packs/`. |
| `POST /memory-pack/import/preview` | Validate an uploaded pack and return new/duplicate counts + warnings. Writes nothing. |
| `POST /memory-pack/import?confirm=true` | Merge the uploaded pack. Requires `confirm=true`. |

## Docker

Under the bundled Compose stack `NOVA_DATA_DIR=/data` is a mounted
volume (`nova-data`). Exported packs are written to
`/data/memory-packs/`, so they survive `docker compose down` and image
rebuilds (removed only by `docker compose down -v`). Nothing about this
feature changes the schema or the Docker deployment.

## Compatibility & forward-compatibility

* `manifest.json` pins `format` (`nova-memory-pack`) and an integer
  `format_version` (currently `1`); each data file also carries its own
  `version`.
* Unknown files and unknown JSON keys are ignored on import, and
  optional files may be absent — so a newer pack remains loadable by an
  older reader for the parts it understands, and an older pack always
  loads.
* A pack whose `format_version` is **newer** than the running Nova is
  refused with a clear message instead of being mis-read.
* The existing database schema is unchanged; the feature only reads and
  inserts rows through the established tables.

## Limitations & follow-ups

* **No attachments yet.** `attachments/` is reserved; chat images are
  not packed in v1.
* **Embeddings are not exported.** Imported natural memories start
  without an embedding and rely on keyword similarity until they are
  next saved; this is intentional (embeddings are large and
  host-specific).
* **Merge only.** There is no "replace/overwrite" mode — by design, so
  an import can never destroy existing data. A future version could add
  an explicit, confirmation-gated replace mode.
* **`message_feedback` is not exported** (it references message ids that
  do not survive a cross-instance remap). Personalization preferences in
  `settings.json` capture the user-facing tone choices.
* Conversation summaries are **generated at export time** from recent
  conversation titles; they are a convenience snapshot, not a stored
  artifact.
