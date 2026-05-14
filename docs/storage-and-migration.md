# Nova Storage & Migration Center

> **Status: Phase 3 — safe guided restore on top of Phase 1 + 2.**
> Nova ships a small, admin-only surface that reports where Nova
> stores its data, builds a portable data export package, lets you
> inspect an existing package, produces a dry-run restore plan, and
> — new in Phase 3 — performs the actual restore after an explicit
> confirmation and an automatic pre-restore backup. Every action is
> opt-in, confirmation-gated, and safe by default. No data is ever
> moved, overwritten, or deleted by Nova without first writing a
> recoverable backup; a failed restore leaves the current data
> bit-for-bit identical to before.

This document is the operator-facing companion to
[`docs/data-directory.md`](data-directory.md) and
[`docs/portable-workspace.md`](portable-workspace.md). Together they
answer three questions:

* **Where does Nova store its data?** — `data-directory.md`.
* **How do I keep code, data, config, and backups under one roof?**
  — `portable-workspace.md`.
* **How do I report on, export, and inspect that data — safely — when
  I need to back it up or move it to another machine?** — this file.

If you are setting up Nova for the first time, start with
`data-directory.md`. Come back here when you want to:

* see at a glance whether `NOVA_DATA_DIR` is on a stable disk,
* produce a portable backup of Nova's memory to move between machines,
* validate an export package before you restore it.

---

## What the Storage & Migration Center covers

Nova exposes five things to the admin:

1. **Storage status** — a read-only report on Nova's path layout:
   `NOVA_DATA_DIR`, the resolved `nova.db` path, the four reserved
   subdirectories (`backups/`, `exports/`, `memory-packs/`, `logs/`),
   whether each path exists, whether it is writable, how much disk
   space remains, and whether the mount looks safe for a 24/7
   service.
2. **Data export** — a portable, allowlisted, data-only `tar.gz`
   archive containing `manifest.json`, a `RESTORE.md` instruction
   sheet, and the canonical Nova data files. Nothing outside the
   data directory is included; secrets, caches, `.git`, `.venv`,
   media libraries, and Ollama models are never bundled.
3. **Export inspection** — a read-only structural check on an
   existing archive. The manifest is parsed, every member is
   validated against path traversal and symlink escape, and the
   result is returned in a structured form the admin UI can render.
4. **Dry-run restore plan** — given an archive and a target data
   directory, Nova lists the files that *would* be restored, flags
   any conflicts (e.g. an existing `nova.db`), and refuses to
   proceed automatically. The plan is purely informational: nothing
   is written, moved, or deleted.
5. **Guided restore (Phase 3)** — given an archive and an explicit
   confirmation, Nova creates an automatic pre-restore backup of
   the current data, stages the archive into a private directory,
   validates every extracted file, and only then replaces files
   inside `NOVA_DATA_DIR`. Failed restores leave the current data
   intact; the pre-restore backup is preserved on success so the
   operator can roll back at any time.

Surfaces:

* **Admin overlay → ⚇ Storage tab** — confirmation-gated UI
  wrapper around all five flows. Shows the status report, builds an
  export package, and (Phase 3) walks the operator through inspect
  → dry-run → confirm → restore.
* **HTTP endpoints** — `/admin/storage/status`,
  `/admin/storage/export`, `/admin/storage/inspect-export`,
  `/admin/storage/restore-dry-run`, `/admin/storage/restore`. All
  five require an admin bearer token.
* **CLI** — `python -m core.data_export {export,inspect,restore-dry-run,restore}`
  so an operator can run the same flows from a shell on either host,
  including the target machine before any data is copied.

Restore is now **safe and reversible**: the dry-run still exists
(default for the cautious operator) and the real restore writes a
pre-restore backup *before* any file is replaced. A failed restore
never corrupts current data.

---

## TL;DR

* Set `NOVA_DATA_DIR=/mnt/fastdata/NovaData` on the active host. Keep
  `/mnt/archive/Backups/Nova` for backups on slower disk.
* Stop Nova before snapshotting `nova.db` from disk; otherwise use
  the export package, which uses SQLite-aware file copying via the
  filesystem.
* Move the export package to the target machine via the channel of
  your choice (rsync, removable disk, encrypted blob). Nova does
  not upload anything.
* On the target machine, inspect the package first. Refuse to
  proceed if the target already has a `nova.db`.

---

## Recommended layout

| Path                                     | Purpose                                     |
|------------------------------------------|---------------------------------------------|
| `/mnt/fastdata/NovaData`                 | **Active data** — SSD, mounted via `/etc/fstab`. |
| `/mnt/archive/Backups/Nova`              | **Backups / archives** — HDD or NAS.        |
| `/mnt/archive/Backups/Nova/exports`      | **Export packages** ready to move.          |
| `~/.ollama/models` (or `OLLAMA_MODELS`)  | **Ollama models** — owned by Ollama, **not** Nova. |

The Storage & Migration Center reports on the first three. The Ollama
path is shown for context (so you know where models live and that
they survive a Nova restore) but **Nova never moves Ollama models**.

### Why SSD for active data

SQLite touches the database synchronously on every transaction. On an
HDD, chat feels laggy; on an SSD it stays calm. The active database
should always live on the fastest disk you have.

### Why HDD or NAS for backups

Backups are written rarely (once a day, once a week, before an
upgrade) and read even more rarely (only on restore). They do not
need fast random access and they benefit from cheap, large, durable
storage. NAS volumes work fine as long as the mount is stable.

### Why stable `/mnt/...` mounts

Long-running services need their data path to exist at boot and
survive a desktop logout. `/etc/fstab` mounts under `/mnt/...` or
`/srv/...` are stable; `/run/media/<user>/<disk>` is **transient** —
it only exists while a desktop session is logged in and disappears
on logout, taking the live database with it. That is the easy way to
corrupt `nova.db` mid-write. The status report warns explicitly when
Nova's data lives on a transient mount.

---

## Reading the storage status

```http
GET /admin/storage/status
Authorization: Bearer <admin-token>
```

The response is a calm, read-only JSON snapshot. Example fragment:

```json
{
  "data_dir_configured": true,
  "data_dir": "/mnt/fastdata/NovaData",
  "paths": [
    {
      "name": "data_dir",
      "label": "Nova data directory",
      "path": "/mnt/fastdata/NovaData",
      "configured": true,
      "exists": true,
      "is_dir": true,
      "writable": true,
      "free_bytes": 12345678901,
      "total_bytes": 256000000000,
      "mount_class": "stable",
      "warnings": []
    },
    {
      "name": "database",
      "label": "SQLite database (nova.db)",
      "path": "/mnt/fastdata/NovaData/nova.db",
      "configured": true,
      "exists": true,
      "is_dir": false,
      "writable": true,
      "free_bytes": 12345678901,
      "total_bytes": 256000000000,
      "mount_class": "stable",
      "warnings": []
    }
    // … backups, exports, memory_packs, logs, ollama_models …
  ],
  "warnings": [],
  "recommendations": ["…", "…"]
}
```

### Mount classes

| Class        | Meaning                                                |
|--------------|--------------------------------------------------------|
| `stable`     | `/mnt/...`, `/var/lib/...`, `/srv/...`, `/opt/...`     |
| `transient`  | `/run/media/...`, `/media/...` — **warns**.            |
| `tmp`        | `/tmp/...`, `/var/tmp/...` — **warns** (cleared on reboot). |
| `user_home`  | Under `$HOME` — acceptable for single-user dev hosts. |
| `other`      | Anything else (including relative paths).              |

The classifier is purely lexical — Nova never reads `/proc/mounts`,
never calls `stat -f`, and never follows symlinks. The point is to
surface a clear warning for paths that are obviously unsafe, not to
prove that a path is mounted correctly.

### Top-level warnings

Some warnings apply to the whole deployment:

* `NOVA_DATA_DIR is not set` — Nova is running in legacy mode with
  `nova.db` next to the checkout. The export endpoint still works
  but only the canonical Nova data files are included.
* `Data directory contains a .git directory` — your runtime data is
  sitting inside the Git checkout. This is a leak risk and must be
  fixed by moving the data directory outside the repository.

---

## Building an export package

```http
POST /admin/storage/export
Authorization: Bearer <admin-token>
Content-Type: application/json

{"confirm": true, "mode": "data-only"}
```

The endpoint requires `confirm: true` so a stray click never produces
an archive. The only supported mode in Phase 1 is `data-only`.

The response contains the archive's absolute path, its size, its
SHA-256, a summary of what was included and excluded, and the full
manifest body:

```json
{
  "archive_path": "/mnt/fastdata/NovaData/exports/nova-data-export-20260514T160000Z.tar.gz",
  "archive_size": 1048576,
  "archive_sha256": "…",
  "manifest": { "format": "nova-data-export", "format_version": 1, "…": "…" },
  "included": [ {"path": "nova.db", "size": 1048064, "sha256": "…"} ],
  "excluded": [ {"path": ".env", "reason": "secret"} ],
  "warnings": []
}
```

The archive layout is fixed:

```
<archive>.tar.gz
├── manifest.json        — file list, hashes, metadata
├── RESTORE.md           — operator-facing instructions
└── data/
    ├── nova.db
    ├── nova.db.backup           (if present)
    ├── nova.db.preupgrade-*     (if present)
    ├── backups/                 (contents of the backups subdir)
    ├── exports/                 (contents of the exports subdir)
    ├── memory-packs/            (contents of the memory-packs subdir)
    └── logs/                    (contents of the logs subdir)
```

### What is included

Only the **canonical Nova data files** under the configured data
root, plus their canonical subdirectories. Every other entry under
the data root is silently skipped — this is the property that keeps
a legacy data root (which happens to be the Nova checkout) from
accidentally exporting source code, `.git`, or `.venv`.

### What is NOT included — ever

* `.env`, `.envrc`, `.netrc`, `.npmrc` files
* `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.crt`, `*.cer`
* `id_rsa`, `id_dsa`, `id_ecdsa`, `id_ed25519`
* `credentials*`, `secrets*`
* Anything matching `*_token`, `*_secret`, `*_credentials`
* `.ssh/`, `.gnupg/`, `.aws/`, `.gcloud/`, `.kube/`
* `.git/`, `.venv/`, `venv/`, `__pycache__/`, `.pytest_cache/`,
  `.mypy_cache/`, `.ruff_cache/`, `node_modules/`
* Ollama models (`*.gguf`, anything under an `ollama/` directory)
* Media libraries (Jellyfin, Plex, …)
* Symlinks whose target resolves outside the data directory

The exclusion list is **conservative by design**: a false positive is
preferable to silently exporting a credential.

### What about Ollama models?

Ollama models are **separate from Nova data**. The model files are
usually large (gigabytes), they live in `OLLAMA_MODELS` or
`~/.ollama/models`, and they belong to the Ollama service, not to
Nova. Nova export packages do not include them by default. After
restoring on a new machine, re-pull the models you need:

```bash
ollama pull gemma3:1b
ollama pull gemma4
ollama pull deepseek-coder-v2
```

The Nova model registry survives the export (it lives inside
`nova.db`), so Nova will already know which models you had configured;
the model weights themselves are simply re-downloaded on demand.

### Where the archive lives

The archive is written to `NOVA_DATA_DIR/exports/` when configured
and to `./exports/` in legacy mode. The filename is
`nova-data-export-<UTC timestamp>.tar.gz`; multiple exports sort
lexicographically by creation time so the latest is the last entry.

---

## Inspecting an existing package

```http
POST /admin/storage/inspect-export
Authorization: Bearer <admin-token>
Content-Type: application/json

{"name": "nova-data-export-20260514T160000Z.tar.gz"}
```

The `name` field is a **plain filename**, not a path. The server
resolves it against the configured exports directory and refuses any
name that contains a path separator, a `..`, or a leading dot.

The response is a structured inspection report:

```json
{
  "archive_path": "/mnt/fastdata/NovaData/exports/nova-data-export-20260514T160000Z.tar.gz",
  "valid": true,
  "manifest": { "format": "nova-data-export", "…": "…" },
  "files": ["manifest.json", "RESTORE.md", "data/nova.db", "…"],
  "total_uncompressed_size": 12345678,
  "errors": [],
  "warnings": []
}
```

Inspection refuses an archive when:

* the file is not a tarball;
* `manifest.json` is missing, too large, or not JSON;
* the manifest format identifier or version does not match;
* any member name contains `..`, an absolute path, a drive letter,
  or a control character;
* any member is a hardlink, a device, or a fifo;
* any symlink member points at an absolute or `..`-containing target;
* any member lives outside the allowed top-level set
  (`manifest.json`, `RESTORE.md`, `data/`).

---

## Moving the package between machines

Nova never uploads anything. Moving the archive is a manual operator
step that goes through a channel you trust. Common options:

```bash
# rsync over SSH
rsync -aH --progress \
    /mnt/fastdata/NovaData/exports/nova-data-export-<stamp>.tar.gz \
    user@new-host:/mnt/fastdata/NovaData/exports/

# Removable disk
cp /mnt/fastdata/NovaData/exports/nova-data-export-<stamp>.tar.gz \
   /run/media/$USER/<disk>/

# Encrypted offline copy
age -r recipient -o backup.age \
    /mnt/fastdata/NovaData/exports/nova-data-export-<stamp>.tar.gz
```

The archive contains your conversation history, user memories, and
per-user settings. Encrypt offline copies. Treat the file as
sensitive even though Nova has already filtered out tokens and
secrets.

---

## Using the CLI

The admin endpoints are also exposed through a small command-line
wrapper so an operator can drive the same flows from a shell on
either host. The CLI is part of the Nova checkout — no extra install
required.

```bash
# Build an export package. By default it lands in NOVA_DATA_DIR/exports.
python -m core.data_export export

# Build into a specific output directory (must be writable).
python -m core.data_export export --output /mnt/archive/Backups/Nova

# Read-only inspection of an existing package.
python -m core.data_export inspect \
    /mnt/archive/Backups/Nova/nova-data-export-20260514T160000Z.tar.gz

# Dry-run restore plan against an explicit target data directory.
# Never writes anything; refuses if the target already has a nova.db.
python -m core.data_export restore-dry-run \
    /mnt/archive/Backups/Nova/nova-data-export-20260514T160000Z.tar.gz \
    --data-dir /mnt/fastdata/NovaData
```

Exit codes:

* `0` — the command succeeded (including a dry-run that returned
  `allowed: false` — that is a *result*, not an error).
* `1` — a user-visible failure (bad archive, unwritable destination,
  workspace mode requested, malformed stem).
* `2` — argparse usage error.

The CLI never modifies anything outside the chosen output directory
(for `export`), never extracts archives, and never restores data.

## Using the admin UI

When you sign in as an admin and open the admin overlay, a third
tab — **⚇ Storage** — sits next to Users and Models. It renders the
storage status report (path-by-path, with mount-class and free-space
hints), and exposes a single **Create export package** button.

The button calls `/admin/storage/export` with `confirm: true`. The
response is rendered as a short summary: archive path, size, SHA-256,
manifest format / version / timestamp, included vs excluded file
counts, and any warnings (for example: "NOVA_DATA_DIR is not
configured"). The UI does **not** offer a destructive restore in
this phase. Inspection and dry-run restore are CLI-only on
purpose — they are the steps you take on the target machine, often
before Nova is even running there.

## Guided restore (Phase 3)

Nova can now restore a valid data export package into the active
`NOVA_DATA_DIR` directly — safely, with an automatic pre-restore
backup and explicit confirmation. The flow is the same whether you
use the CLI or the admin UI, and the safety contract is identical:

1. **Inspect** the package.
2. **Dry-run** the restore. Read the file list and warnings.
3. **Confirm** explicitly.
4. Nova **backs up** the current data automatically.
5. Nova **stages** the archive into a private directory inside
   `NOVA_DATA_DIR`.
6. Nova **replaces** matched files atomically per-file.
7. Nova **cleans up** staging on success or failure.

The pre-restore backup lives under
`NOVA_DATA_DIR/backups/pre-restore/` as a Nova export package of
its own (same `tar.gz` format, same manifest). You can inspect it
or use it as the source for a follow-up restore — that is how you
roll back.

### Restoring from the CLI

```bash
# Stop Nova so the running process does not hold the database open.
sudo systemctl stop nova

# Inspect first. The CLI prints the manifest, the file count, the
# uncompressed size, and any structural issues.
python -m core.data_export inspect \
    /mnt/archive/Backups/Nova/nova-data-export-<stamp>.tar.gz

# Dry-run the restore against the active data directory. Never
# writes a file. The output lists the would-be files and conflicts.
python -m core.data_export restore-dry-run \
    /mnt/archive/Backups/Nova/nova-data-export-<stamp>.tar.gz \
    --data-dir /mnt/fastdata/NovaData

# Real restore. The --confirm flag is required; without it the
# command refuses. The optional --confirmed-manifest-id pins the
# archive identity so another archive cannot slip in between the
# inspect and restore calls.
python -m core.data_export restore \
    /mnt/archive/Backups/Nova/nova-data-export-<stamp>.tar.gz \
    --data-dir /mnt/fastdata/NovaData \
    --confirm

# Start Nova back up.
sudo systemctl start nova
```

The restore CLI exits `0` on success and `1` on a refusal or
failure — so shell scripts can branch on it. The output includes:

* the restored file count,
* the absolute path of the pre-restore backup,
* any warnings (e.g. "restart Nova so the new nova.db is picked
  up"),
* and the manifest summary.

### Restoring from the admin UI

The **⚇ Storage** tab gains a **Restore from export package**
section in Phase 3. The flow:

1. Type the filename of the package as it sits under
   `NOVA_DATA_DIR/exports/` (the admin UI never reads arbitrary
   filesystem paths — the file must already be under the configured
   exports directory). Move the archive there with `rsync` or `cp`
   first if it is not.
2. Click **Inspect**. The UI renders the manifest, the format /
   version, the member count, the uncompressed size, and whether
   `nova.db` is in the package. Errors and warnings are listed
   verbatim.
3. Click **Dry-run**. The UI renders what *would* be restored and
   surfaces a `would proceed` / `refused` tag. The restore button
   appears below.
4. Tick the confirmation checkbox:
   *"I understand this will replace Nova's current data after
   creating a backup."*
5. Click **Restore package and backup current data first**. The UI
   reports the restored files count, the pre-restore backup path,
   any warnings, and a clear "restart Nova" hint when `nova.db`
   was in the package.

The restore button stays **disabled** until inspection and dry-run
have both succeeded *and* the confirmation checkbox is ticked. Each
gate is enforced on both sides: client-side (so the UI never offers
the destructive action before the operator has reviewed) and
server-side (so a misbehaving client cannot bypass the gates).

### Verifying after a restore

1. **Restart Nova** if `nova.db` was in the package: `sudo
   systemctl restart nova`.
2. **Open Nova** and confirm:
   - your conversations are present,
   - your memories are restored,
   - your per-user preferences look correct,
   - `/admin/storage/status` reports the data directory as healthy.
3. **Re-pull Ollama models** you had configured on the source host
   (`ollama pull <name>`). Ollama models are owned by Ollama, not
   by Nova, and never live inside an export package.

### Rolling back using the pre-restore backup

The pre-restore backup is a Nova export package. To roll back:

```bash
# The pre-restore backup lives under NOVA_DATA_DIR/backups/pre-restore/.
ls -lh /mnt/fastdata/NovaData/backups/pre-restore/

# Inspect it as you would any other export.
python -m core.data_export inspect \
    /mnt/fastdata/NovaData/backups/pre-restore/nova-pre-restore-<stamp>.tar.gz

# Restore from the backup. The current data is itself backed up
# *again* automatically before the rollback proceeds, so multiple
# rollbacks are possible.
sudo systemctl stop nova
python -m core.data_export restore \
    /mnt/fastdata/NovaData/backups/pre-restore/nova-pre-restore-<stamp>.tar.gz \
    --data-dir /mnt/fastdata/NovaData \
    --confirm
sudo systemctl start nova
```

Pre-restore backup files are **never overwritten**: a name clash
falls back to `-<n>` until a free name is found. Operators are
expected to clean up old pre-restore backups by hand when they no
longer need them.

### What stays out of scope (still)

* **Ollama models.** Re-pull on the target machine.
* **`.env` and other secrets.** Configure them on the target
  machine, not in the export.
* **Media libraries (Jellyfin, Plex, …).** Those services own
  their own data directories.
* **Automatic restart of Nova.** Nova never restarts itself after
  a restore — even though the maintenance center has a calm
  systemd-user restart facility, the restore flow surfaces a
  "restart Nova" *hint* rather than performing it, so the operator
  remains in charge of when the new database starts being served.

---

## Why GitHub should only contain source code

Nova's GitHub repository is for **public source code**. It must
never contain:

* `nova.db`,
* conversation history,
* user memories,
* per-user settings,
* `.env`, secrets, tokens,
* Ollama models,
* anything an export package would otherwise exclude.

The `.gitignore` shipped in the repo already ignores `nova.db` and
`.env`. The portable workspace layout (`docs/portable-workspace.md`)
puts private data **outside** the Git checkout so a misconfigured
`.gitignore` cannot accidentally stage it. The Storage & Migration
Center reinforces this: a status report whose data directory sits
inside a Git checkout raises a top-level warning, and the export
package never includes `.git` even if the data root happens to
contain one.

---

## Safety guarantees

These are firm boundaries of the Storage & Migration Center:

* **Local-only.** No outbound calls, no cloud sync, no scheduled
  background restore.
* **Read-only by default.** `/admin/storage/status`,
  `/admin/storage/inspect-export`, and `/admin/storage/restore-dry-run`
  never write to disk.
* **Admin-only.** Every endpoint is wrapped with `require_admin`;
  non-admin and restricted users see a 403.
* **Confirmation-gated export.** `/admin/storage/export` requires
  `{"confirm": true}` in the request body. A missing or false
  `confirm` returns 400 with no side effects.
* **Allowlist-only export.** Only Nova's canonical data files
  (`nova.db`, `nova.db.*`, the four reserved subdirectories) are
  eligible for inclusion. Source code, `.git`, `.venv`, caches,
  media libraries, Ollama models, `.env` files, and any
  token-shaped file are excluded by name.
* **No symlink escape.** The export walk uses
  `os.walk(followlinks=False)`. Any symlink whose target resolves
  outside the data directory is recorded with reason
  `symlink_escape` and skipped.
* **No path traversal on inspect / restore.** Every archive member
  name is validated lexically (no `..`, no leading `/`, no drive
  letter) and re-checked against the intended root after
  resolution. Hostile archives are refused with a clear error,
  never extracted.
* **Same allowlist on restore as on export.** A crafted archive
  cannot smuggle in files the exporter would never have produced.
  The restore path applies the same secret / VCS / cache / venv /
  Ollama / non-canonical filters as the export builder, with
  identical wire-format reasons. ``data/.env``, ``data/.ssh/...``,
  ``data/backups/.git/...``, ``data/*.gguf``, and bare
  ``data/README.txt``-style stray files are skipped during restore
  with a reason in ``skipped_files`` even when the rest of the
  archive inspects clean. Only the canonical Nova entries
  (``nova.db``, ``nova.db.*`` sidecars, and contents of the four
  reserved subdirectories) actually land on disk.
* **No restore without explicit confirmation.** Phase 3's real
  restore endpoint refuses unless the request body carries
  `confirm: true`. The CLI's `restore` subcommand refuses without
  `--confirm`. The admin UI keeps the restore button disabled until
  inspection succeeds, the dry-run plan reports "would proceed", and
  the operator ticks the "I understand" checkbox.
* **Automatic pre-restore backup.** The real restore writes a Nova
  export package of the *current* data directory under
  `NOVA_DATA_DIR/backups/pre-restore/` **before** any file is
  replaced. The restore refuses if that backup cannot be created.
  Pre-restore backups are never overwritten — a name clash falls
  back to `-<n>` until a free name is found.
* **Staging-first extraction.** The archive is extracted into a
  private staging directory under
  `NOVA_DATA_DIR/.restore-staging/`. Files are validated against
  path traversal post-resolution and only then moved into the
  target tree, one file at a time. The staging directory is
  cleaned up after success or failure.
* **Atomic per-file replace.** Each restored file lands via
  `os.replace` inside the same filesystem — no half-written
  partials, no torn writes. The staging directory lives **inside**
  the target data root by construction so the rename never falls
  back to a cross-filesystem copy.
* **Failed restores leave current data intact.** A failure after
  the backup step but before all files have been copied keeps the
  remaining target files untouched; rolling back via the
  pre-restore backup recovers the prior state in one step.
* **No automatic data move.** Nova never copies its data root to a
  different disk for you. Use `rsync` or the operator's preferred
  tool, then update `NOVA_DATA_DIR`.
* **No deletion of old data.** Whatever was there before stays
  there until you remove it by hand.
* **No cloud sync.** No outbound calls. No background export.
* **No shell, no subprocess, no privilege escalation.** The export
  builder relies on `tarfile`, `hashlib`, `os.walk`, and `shutil`.
  Nothing else.
* **No secret leakage in responses.** Manifests never echo the
  contents of `.env`-shaped files. The inspection / status
  responses never include env vars. Errors are short and
  frontend-safe.

If a future Nova release adds an automated restore, it lands behind
its own opt-in switch, takes its own explicit confirmation, never
overwrites an existing `nova.db`, and preserves the legacy file. The
boundaries above are firm.

---

## Future work

These are explicitly **not** in Phase 2:

* A full admin-UI wizard for migration between disks.
* A `workspace` export mode that snapshots the entire Nova Portable
  Workspace (code, data, config, scripts) as one archive.
* Automatic restore with progress reporting.
* Encrypted-at-rest export packages.
* Background scheduled exports.
* Editing `NOVA_DATA_DIR` from the UI (with the matching `systemd`
  / Docker config updates).
* Moving Ollama models.

Each of those is a separate PR with its own review. The current
boundary keeps the surface area small, the safety contract clear,
and the existing Nova install untouched.
