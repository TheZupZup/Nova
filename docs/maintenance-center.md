# Admin-only Maintenance / Update Center

> **Status: optional, disabled by default.** Nova ships an admin-only
> Maintenance surface that lets a maintainer check for upstream
> changes, fast-forward the local checkout, and (when explicitly
> configured) ask systemd-user to restart Nova — all from the web UI.
> Every switch defaults to off. An unconfigured Nova install never
> executes any maintenance command, even for an admin.

This document describes the safety boundaries the feature commits to
and the setup steps required before any of it does anything. It
sits alongside [`secure-deployment.md`](secure-deployment.md) and
[`nova-safety-and-trust-contract.md`](nova-safety-and-trust-contract.md);
nothing in this guide weakens those documents.

---

## TL;DR

- **Remote updates are optional, opt-in, and admin-only.**
- **No arbitrary shell.** Only a fixed allowlist of `git` commands
  runs, plus a single `systemctl --user restart <validated-unit>`
  when the restart switch is on.
- **No `sudo`, no `pkexec`, no system-level `systemctl`.** Nova never
  asks for elevation, ever.
- **Fast-forward only.** A dirty working tree or a diverged branch
  blocks the pull and prints a clear "manual intervention required"
  message — Nova does not improvise.
- **Confirmation-gated.** Pull and restart each require an explicit
  `{"confirm": true}` body. The UI shows a `confirm()` prompt before
  posting.
- **Back up first.** `nova.db` and any other state in the checkout
  should be backed up before enabling the pull switch.

---

## What this feature does (and does not)

What the Maintenance surface **does** when fully enabled:

- Tells the admin which branch the checkout is on, which commit it
  points at, and which upstream branch is configured.
- Reports whether the working tree is clean.
- Reports whether the local branch is up-to-date, strictly behind
  upstream (fast-forward-able), or diverged.
- Lists the incoming commits (`git log --oneline HEAD..@{u}`) and
  the changed-files summary (`git diff --stat HEAD..@{u}`) when an
  update is available.
- After an explicit confirmation, runs `git pull --ff-only`.
- After an explicit confirmation, asks systemd-user to restart the
  configured Nova unit.

What the Maintenance surface **does not** do:

- It is **not** a web terminal. No free-text commands are accepted.
- It does **not** execute arbitrary shell. Every subprocess call is
  built from a hard-coded argv list with `shell=False`.
- It does **not** accept commands from chat input. The chat surface
  is unchanged; this is a separate admin endpoint with its own
  confirmation.
- It does **not** run as root. It does **not** call `sudo`,
  `pkexec`, `doas`, `su`, or `runuser`.
- It does **not** bypass systemd. The only restart path is
  `systemctl --user restart <validated-unit>`.
- It does **not** auto-update. Nothing happens until an admin clicks
  through the confirmation.
- It does **not** restart without confirmation.
- It is **not** reachable by a non-admin or a restricted user. Every
  endpoint is wrapped in `require_admin`.
- It does **not** modify the GitHub repository. There are no pushes,
  no force-pushes, no branch creation, no comments, and no PR
  actions.
- It does **not** merge or rebase on a diverged branch. The only
  pull verb wired in is `pull --ff-only`.
- It does **not** disable any other Nova safety check. SilentGuard,
  the GitHub read-only connector, the family controls, and the
  identity contract are untouched.

These are firm boundaries. A future change that crosses any of them
is a contract change that must be discussed in its own PR.

---

## Configuration

Every switch is read from the environment and defaults to off / safe.
You can set them in `.env` or in the systemd unit's `Environment=`
lines (the README walks through both).

```ini
# Host-wide opt-in. When false, every maintenance endpoint resolves
# but reports state="disabled" and never touches git or systemctl.
NOVA_MAINTENANCE_ENABLED=false

# Independent switches. Enabling NOVA_MAINTENANCE_ENABLED does not
# auto-enable pull or restart — the admin has to opt into each
# action explicitly so an admin who only wants to *see* the status
# card can do that without unlocking the destructive paths.
NOVA_MAINTENANCE_ALLOW_PULL=false
NOVA_MAINTENANCE_ALLOW_RESTART=false

# Absolute path to the Nova checkout. Empty falls back to the
# directory containing config.py (the install itself), which is
# usually what you want.
NOVA_MAINTENANCE_REPO_PATH=

# Restart backend. `systemd-user` is the only allowed non-disabled
# value, on purpose: it pins Nova to `systemctl --user restart <unit>`,
# which never escalates privilege and never touches firewall config.
# Anything else (typos, "sudo-systemctl", etc.) normalises to
# `disabled`.
NOVA_MAINTENANCE_RESTART_MODE=disabled

# User-level systemd unit name to restart. Validated against
# ^[a-z0-9][a-z0-9._-]*\.service$ before any spawn — path
# traversal, shell metacharacters, whitespace, control characters,
# and system-level targets are all rejected before exec.
NOVA_MAINTENANCE_SYSTEMD_UNIT=nova.service
```

### Recommended starting point

For an admin who wants to *try* the surface without unlocking the
destructive actions yet:

```ini
NOVA_MAINTENANCE_ENABLED=true
NOVA_MAINTENANCE_ALLOW_PULL=false
NOVA_MAINTENANCE_ALLOW_RESTART=false
```

The card will now appear in **Settings → Admin** for admin users.
It shows the branch, commit, upstream, clean / dirty state, and
update availability — but the **Pull update** and **Restart Nova**
buttons stay hidden because the corresponding switches are off.

Once you are comfortable with the surface, flip `ALLOW_PULL=true`
to enable fast-forward updates. Flip `ALLOW_RESTART=true` (and pick
a `RESTART_MODE` / `SYSTEMD_UNIT`) to enable the restart path.

---

## HTTP endpoints

All four endpoints are auth-gated, admin-only, and disabled unless
configured. Non-admin and restricted users receive a `403`.

- `GET  /admin/maintenance/status`

  Read-only snapshot. Never reaches the network. Returns the
  disabled snapshot when the feature is off so the admin UI can
  render a stable shape either way. Fields include `state` (`disabled`
  / `ready` / `unavailable`), `branch`, `commit`, `upstream`,
  `has_upstream`, `working_tree_clean`, `update_available`
  (`up_to_date` / `available` / `diverged` / `no_upstream` /
  `unknown`), `behind_count`, `ahead_count`, `incoming_commits`,
  `changed_files`, `allow_pull`, `allow_restart`, `restart_mode`,
  and `unit`.

- `POST /admin/maintenance/fetch`

  Runs `git fetch` once and returns a refreshed status snapshot.
  The only network-touching maintenance endpoint that does not
  require a confirmation: fetching never modifies the working tree.

- `POST /admin/maintenance/pull` (requires `{"confirm": true}`)

  Refusal outcomes (no spawn happens for any of these):
  - `disabled` — maintenance is off.
  - `pull_not_allowed` — `NOVA_MAINTENANCE_ALLOW_PULL` is false.
  - `repo_unavailable` — git missing or path is not a checkout.
  - `no_upstream` — no upstream is configured for the current branch.
  - `dirty_working_tree` — `git status --porcelain` had output.
  - `diverged` — both `behind` and `ahead` are non-zero.
  - `not_fast_forward` — already up-to-date, nothing to pull.

  Success outcome: `success`, with `previous_commit` and `new_commit`
  exposed so the admin can confirm the advance.

- `POST /admin/maintenance/restart` (requires `{"confirm": true}`)

  Refusal outcomes (no spawn happens for any of these):
  - `disabled` — maintenance is off.
  - `restart_not_allowed` — `NOVA_MAINTENANCE_ALLOW_RESTART` is false.
  - `restart_mode_disabled` — `NOVA_MAINTENANCE_RESTART_MODE` is not
    `systemd-user`.
  - `invalid_unit` — the configured unit name fails validation.
  - `systemctl_missing` — `systemctl` is not on PATH.
  - `failed` — the spawn returned non-zero or timed out.

  Success outcome: `accepted`. systemd will restart the unit
  asynchronously; the admin should refresh the page after a moment.

Pull and restart bodies that omit `confirm`, send `confirm: false`,
or send any other value, are rejected with `400` before any helper
code runs.

---

## Behaviour by state

| Repo state | Status response | Pull behaviour |
|---|---|---|
| Maintenance disabled | `state="disabled"` | `outcome="disabled"` |
| Pull switch off | `state="ready"` (pull button hidden in UI) | `outcome="pull_not_allowed"` |
| No upstream configured | `update_available="no_upstream"` | `outcome="no_upstream"` |
| Clean + behind | `update_available="available"` with `incoming_commits` / `changed_files` | `outcome="success"` (runs `git pull --ff-only`) |
| Clean + up-to-date | `update_available="up_to_date"` | `outcome="not_fast_forward"` |
| Dirty working tree | `working_tree_clean=false` | `outcome="dirty_working_tree"` |
| Diverged | `update_available="diverged"` | `outcome="diverged"` |

The UI hides the **Pull update** button unless the helper reports
`allow_pull=true`, `has_upstream=true`, `working_tree_clean=true`,
and `update_available="available"`. The server still re-checks each
condition on the actual pull call — the UI is a convenience layer,
not a security boundary.

---

## Restart safety

The restart path is firmly opt-in and locked down to one verb:

```text
systemctl --user restart <validated-unit>
```

Everything about that call is fixed:

- **Argv list, `shell=False`.** Python builds the argv array
  manually; nothing is concatenated as a shell string.
- **Absolute `systemctl` path.** Resolved with `shutil.which`. If
  systemctl is missing, the call is refused with
  `outcome="systemctl_missing"` — Nova never invents a path.
- **`--user` is non-negotiable.** There is no system-level
  `systemctl` code path in the maintenance module.
- **`restart` is the only verb.** `start` / `stop` / `enable` /
  `daemon-reload` are not reachable through this surface.
- **Unit name is validated.** A strict
  `^[a-z0-9][a-z0-9._-]*\.service$` regex plus a forbidden-substring
  list (`..`, path separators, shell metacharacters, whitespace,
  control characters) reject anything that could escape the argv.
- **Bounded timeout.** A hung `systemctl` returns `outcome="failed"`,
  never a wedged request.

If your install does not have a systemd-user unit, leave
`NOVA_MAINTENANCE_RESTART_MODE=disabled` and use the documented
`sudo systemctl restart nova` from
[`docs/secure-deployment.md`](secure-deployment.md). The UI will
hide the Restart button when the restart mode is disabled — it
will not pretend a restart succeeded when no path is configured.

### Setting up a user-level Nova unit (optional)

If you run Nova as a system unit (the default in
[`deploy/systemd/nova.service`](../deploy/systemd/nova.service))
and want the in-app restart to work, you have two options:

1. **Keep Nova as a system unit and leave the restart switch off.**
   This is the recommended default. Use `sudo systemctl restart nova`
   from a shell when you want to restart, exactly as the security
   guide documents. The Maintenance card still shows status / pull
   actions; only the Restart button is hidden.

2. **Run Nova under `systemctl --user`** in a user-level unit
   (place a copy of `nova.service` under `~/.config/systemd/user/`,
   adapted for your account). Then set
   `NOVA_MAINTENANCE_RESTART_MODE=systemd-user` and point
   `NOVA_MAINTENANCE_SYSTEMD_UNIT` at that unit name. Nova runs in
   the same unprivileged account in both cases; this option just
   changes where the unit lives so that `systemctl --user` can
   manage it without elevation.

Either option is consistent with the rest of the secure-deployment
guide. The Maintenance surface does not require option 2.

---

## Audit logging

Today, sensitive maintenance actions are visible through:

- The systemd journal for the Nova process (`journalctl -u nova`).
- The Maintenance response payloads themselves, which echo the
  resolved branch / commit / outcome so the action is reviewable in
  the admin's browser history.

A dedicated audit log table (write actions to a `maintenance_audit`
table with `who / when / outcome / previous_commit / new_commit`) is
deliberately deferred so this PR stays small. Adding it later does
not require changing the safety contract — it is a strict extension
of the existing surface.

---

## Backups (strongly recommended)

Before enabling `NOVA_MAINTENANCE_ALLOW_PULL=true`:

```bash
# Back up nova.db and the rest of the checkout. The whole checkout
# is the simplest unit because nova.db, nova.db.backup, and any
# user-authored memories all live alongside the code.
tar czf nova-backup-$(date +%F).tar.gz /path/to/Nova
```

Even though `git pull --ff-only` is a strict advance, the database
file is the part of the install that holds your data. Backing it up
before a code update is cheap insurance.

---

## Non-goals

These are explicitly **out of scope** for this PR and will not be
added without their own review:

- **No auto-update scheduling.** Nova does not poll for updates,
  does not check on a timer, and does not run a background updater.
- **No GitHub write actions.** The Maintenance surface is local-only:
  it reads the upstream tip via `git fetch` and reads the working
  tree. It never pushes, force-pushes, or interacts with GitHub
  beyond `git fetch`'s normal behaviour.
- **No rollback.** A future "roll back to the previous commit"
  button is plausible but is deliberately not part of this PR.
- **No remote terminal.** Period.
- **No SilentGuard behaviour change.** The SilentGuard read-only
  surface and the mitigation flow are unaffected by this feature.

---

## Tests

The contract is pinned by [`tests/test_maintenance.py`](../tests/test_maintenance.py):

- Disabled feature short-circuits every endpoint.
- Pull / restart switches default to off and gate independently.
- Pull refuses on dirty / diverged / no-upstream / not-fast-forward
  states *before* any git command runs.
- Pull uses the exact argv `["git", "pull", "--ff-only"]`.
- Restart uses the exact argv
  `[<systemctl>, "--user", "restart", <unit>]`.
- Unit-name validation rejects path traversal, shell metacharacters,
  system-level targets, and any non-`.service` suffix.
- `subprocess.run` is always called with `shell=False`, a bounded
  timeout, `stdin=DEVNULL`, and a list argv.
- The maintenance module contains no `shell=True` and no string
  reference to `sudo` / `pkexec` / `doas` / `runuser`.
- The web endpoints are admin-only — non-admin and restricted users
  receive `403`; missing bearer returns `401` / `403`.
- Pull and restart require `{"confirm": true}` and return `400`
  otherwise.

The existing auth / admin / SilentGuard / GitHub-connector test
suites continue to pass — this feature is additive.
