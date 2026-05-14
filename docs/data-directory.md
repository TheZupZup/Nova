# Nova data directory (`NOVA_DATA_DIR`)

Nova stores its runtime state — the SQLite database, sidecar backups,
and a few reserved subdirectories — in a single, configurable
directory. This document explains the layout, how to move existing
data onto a dedicated disk, and how to back it up and restore it.

> **Looking for a bigger layout?** If you want code, data, config,
> logs, and backups all under one parent folder that you can move as
> a single unit (and that works for both systemd and Docker), see
> [`docs/portable-workspace.md`](portable-workspace.md). That guide
> builds on the `NOVA_DATA_DIR` foundation described here.

This is the **Phase 1 foundation**. Nova does not auto-migrate, does
not auto-back up, and does not run a wizard. Moving data is a
deliberate operator step that you can review before it happens and
revert by hand if anything looks wrong.

## What `NOVA_DATA_DIR` is

`NOVA_DATA_DIR` is an environment variable that selects the parent
directory of every persistent file Nova writes. The default —
empty / unset — preserves Nova's legacy behaviour: `nova.db` is
created in the working directory of the running process (typically
the Nova checkout). Existing installs do not need to set anything for
this PR; nothing changes for them.

When set to an absolute path, Nova:

* uses `<NOVA_DATA_DIR>/nova.db` as its SQLite database,
* creates the standard subdirectories listed below on startup,
* refuses to start if the directory is not writable, with a clear
  error message that names the path.

Nova never silently creates data in a different location when the
configured directory is unavailable. The service fails loudly so an
operator notices and can fix the mount, permission, or path.

## Recommended layout

```
NOVA_DATA_DIR/
├── nova.db                # SQLite database (live)
├── nova.db.backup         # sidecar backup written before destructive ops
├── nova.db.preupgrade-*   # pre-migration backups (timestamped)
├── backups/               # reserved for future explicit backup exports
├── exports/               # reserved for future user-facing exports
├── memory-packs/          # reserved for future memory pack imports
└── logs/                  # reserved for future local log files
```

The four reserved subdirectories are created on startup but not
written to by any current Nova feature. They exist so future PRs can
land cleanly without each one having to re-derive a layout convention.

## Recommended locations

| Path                             | Use                                   |
|----------------------------------|---------------------------------------|
| `/mnt/fastdata/NovaData`         | active database — fast SSD.           |
| `/mnt/archive/NovaData`          | archive / backup copy — slower disk.  |
| `/var/lib/nova` (system-managed) | acceptable for a single dedicated host. |

Use **stable mount paths** declared in `/etc/fstab`. Avoid
`/run/media/<user>/<disk>` — those exist only while a desktop session
is logged in and disappear at logout, which is unsafe for a long-running
service. systemd will not wait for a `/run/media` mount either.

The active database should ideally live on **SSD**: SQLite touches
the file synchronously on every transaction, and an HDD makes chat
feel laggy. Archive copies on HDD are fine.

## Moving an existing `nova.db`

Phase 1 does **not** auto-migrate. The procedure below is the
documented manual step. It is the same on every host shape (bare
metal, systemd, Docker, dev box).

```bash
# 1. Stop Nova so the database is not being written to.
sudo systemctl stop nova

# 2. Create the new data directory and make sure Nova owns it.
sudo mkdir -p /mnt/fastdata/NovaData
sudo chown -R nova:nova /mnt/fastdata/NovaData

# 3. Copy nova.db (and its sidecar files) into the new location.
#    Use rsync -a so timestamps and permissions are preserved.
sudo -u nova rsync -a /path/to/Nova/nova.db /mnt/fastdata/NovaData/
sudo -u nova rsync -a /path/to/Nova/nova.db.backup /mnt/fastdata/NovaData/ 2>/dev/null || true

# 4. Configure systemd to point Nova at the new directory.
#    Edit /etc/systemd/system/nova.service and add:
#        Environment="NOVA_DATA_DIR=/mnt/fastdata/NovaData"
#        ReadWritePaths=/mnt/fastdata/NovaData
#    (See deploy/systemd/nova.service for the documented placeholders.)
sudo systemctl daemon-reload

# 5. Start Nova and confirm it picked up the new path.
sudo systemctl restart nova
journalctl -u nova -n 50 --no-pager

# 6. Verify in the web UI that memories, conversations, and
#    feedback are present. Keep the OLD database file as a backup
#    until you have verified the migration end-to-end.
```

If you change your mind, stop Nova, remove the `NOVA_DATA_DIR`
environment line, and restart. Nova falls back to the legacy path
(the original `nova.db` next to the checkout) automatically.

Nova never deletes the old database on its own. The legacy file
remains in place until you remove it by hand.

### What the startup log says

When `NOVA_DATA_DIR` is set, Nova prints a `WARNING` log line if:

* the legacy `./nova.db` still exists next to the checkout, AND
* the configured `NOVA_DATA_DIR/nova.db` does not yet exist.

That is the "you probably want to copy your database" reminder. Nova
will still start fresh under the new path — copying is up to you, and
the warning intentionally never touches a file.

## Backups

Treat the data directory as the single thing to back up. Everything
Nova writes lives under it.

```bash
# Snapshot the whole directory (compressed, timestamped).
sudo -u nova rsync -a /mnt/fastdata/NovaData/ \
                     /mnt/archive/NovaData-$(date +%Y%m%dT%H%M%SZ)/

# Or a simple SQLite-aware dump for the database alone.
sudo -u nova sqlite3 /mnt/fastdata/NovaData/nova.db \
    ".backup '/mnt/archive/nova-$(date +%Y%m%dT%H%M%SZ).db'"
```

Encrypt offline copies — the database contains conversation history,
user-authored memories, and per-user settings.

## Restore

The restore path is the inverse of the migration above:

```bash
sudo systemctl stop nova
sudo -u nova rsync -a /mnt/archive/NovaData-<stamp>/nova.db \
                     /mnt/fastdata/NovaData/nova.db
sudo systemctl start nova
journalctl -u nova -n 50 --no-pager
```

Always test the restore path on a non-production host first.

## Safety guarantees

These are firm boundaries — they are reasons this PR is small and
deliberate, not future work.

* Nova **never** moves data automatically. The operator copies.
* Nova **never** overwrites an existing `nova.db`.
* Nova **never** deletes the legacy database.
* Nova **never** runs `sudo`, `pkexec`, or any shell command as part
  of path handling. Permission setup is the operator's job.
* Nova fails loudly when `NOVA_DATA_DIR` is unwritable — it does not
  silently fall back to a different path.

If a future Nova release introduces an opt-in migration helper, it
will be additive (a single explicit command), preserve the legacy
file, and require explicit confirmation. Nothing about the current
data is at risk from this PR.
