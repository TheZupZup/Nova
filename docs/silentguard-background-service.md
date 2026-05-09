# Running the SilentGuard read-only API as a local background service

This document describes how to run SilentGuard's loopback, read-only
HTTP API as a long-lived background service on the same host as Nova,
so Nova's optional SilentGuard integration can probe it.

It is **for Nova-integration readiness**. SilentGuard remains a
**standalone** project — the API works without Nova, and Nova works
without the API. Nothing here turns Nova into a firewall, a service
supervisor, or a security tool; the integration is a calm, read-only
pull from a service the user has chosen to run.

If you only want to read SilentGuard's on-disk memory file (the
historical default), you do not need any of this. That path keeps
working and is unchanged.

---

## What this is, and what it is not

This guide is about **running an existing read-only service** behind a
stable local URL. It is explicitly:

- **Read-only.** Nova's HTTP client only ever issues `GET` requests
  against a fixed path list (`/status`, `/connections`, `/blocked`,
  `/trusted`, `/alerts`). There is no write helper, no `POST`, no
  `DELETE`.
- **Local-first.** The API binds to `127.0.0.1` by default. There is
  no remote binding, no auto-discovery, and no cloud call. Nothing is
  contacted off-host.
- **Non-privileged.** The API runs in the user's own session under
  `systemctl --user`. No `sudo`, no `pkexec`, no `doas`, no system-
  level `systemctl`, no setuid wrapper.
- **Optional and disabled by default.** The unit is shipped with an
  `[Install]` block so a user can `systemctl --user enable` it
  intentionally; until they do, nothing starts.
- **Independent.** The service does not depend on Nova to function. If
  Nova is not installed, the API still runs; if the API is not running,
  Nova still works (it falls back to the file probe or surfaces a calm
  `unavailable` snapshot).

It is **not**:

- A firewall control surface. The API never modifies firewall rules,
  never blocks or unblocks IPs, and never invokes `iptables`,
  `nftables`, `firewalld`, `ufw`, `pf`, or `ipfw`.
- An autonomous defender. Nothing here polls in the background, retries
  on failure, or pushes notifications.
- A telemetry pipe. No outbound HTTP, no analytics, no remote sync.
- A Nova hard dependency. The unit and the API have no `Requires=` /
  `BindsTo=` on any Nova process.

---

## Prerequisites

- A Linux host with `systemd` and a user-level `systemd` instance
  available (`systemctl --user` works in the user's session).
- SilentGuard installed locally per its own documentation.
- Nova running on the same host *only if* you want to wire the
  integration end-to-end. The unit itself does not need Nova.

If you are running headlessly (no graphical session, no `loginctl
enable-linger`), see "Running without a graphical session" below.

---

## 1. Install SilentGuard locally

Install SilentGuard from its own project per the upstream
instructions. Confirm two things before continuing:

1. SilentGuard ships a **read-only API command**. Your version may
   call it `silentguard-api`, expose it as a `--read-only` flag on the
   main binary, or run it as a Python module — whatever the upstream
   project documents.
2. The command supports binding to a specific host and port. You will
   pin it to `127.0.0.1` and a free local port.

Nova does not install SilentGuard. Nova does not try to reach upstream
to fetch the binary, the wheel, or the source. That is your call and
your install.

---

## 2. Start the API manually (smoke test)

Before wiring up systemd, confirm the API works in the foreground:

```bash
# Replace with the exact command and flags your SilentGuard ships.
silentguard-api --host 127.0.0.1 --port 8767 --read-only
```

In a second terminal, probe `/status`:

```bash
curl --silent --show-error --fail http://127.0.0.1:8767/status
```

A `200` response with a JSON body confirms two things at once:

- the service is listening on loopback only, and
- the read-only endpoint contract Nova expects is honoured.

Stop the foreground process with `Ctrl-C` once the smoke test passes.

---

## 3. Enable the API as a `systemctl --user` service

Nova ships an example user unit at
[`deploy/systemd/silentguard-api.service`](../deploy/systemd/silentguard-api.service).
It is an example — review and edit it before installing.

### 3.1 Edit the example unit

Open `deploy/systemd/silentguard-api.service` and replace the
`ExecStart=` line with the real read-only API command from your
SilentGuard install. Keep the bind host at `127.0.0.1` and the
read-only flag set.

The example uses port `8767`; pick any free local port and use the
same value when configuring Nova in step 6.

### 3.2 Install the unit (per user, no sudo)

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/silentguard-api.service ~/.config/systemd/user/silentguard-api.service
systemctl --user daemon-reload
```

`daemon-reload` runs against the user's own systemd instance — it
does not require root and does not touch `/etc/systemd/`.

### 3.3 Start (and optionally enable) the service

Start the service for this session:

```bash
systemctl --user start silentguard-api.service
```

Enable it so it starts automatically on the next login (still
non-privileged, still scoped to your user):

```bash
systemctl --user enable silentguard-api.service
```

The unit ships with `WantedBy=default.target` so `enable` works, but
nothing happens automatically until you run that command — the unit is
**disabled by default**.

---

## 4. Check service status

```bash
systemctl --user status silentguard-api.service
```

Expected output: `active (running)`, with the configured `ExecStart=`
line and the bind on `127.0.0.1`.

For live logs:

```bash
journalctl --user -u silentguard-api.service -f
```

Re-run the loopback probe to confirm Nova will see the same surface:

```bash
curl --silent --show-error --fail http://127.0.0.1:8767/status
```

---

## 5. Stop or disable the service

Stop the running instance for this session (the unit stays installed):

```bash
systemctl --user stop silentguard-api.service
```

Disable autostart on next login (the unit stays installed and can still
be started manually):

```bash
systemctl --user disable silentguard-api.service
```

Remove the unit entirely:

```bash
systemctl --user disable silentguard-api.service
rm ~/.config/systemd/user/silentguard-api.service
systemctl --user daemon-reload
```

Nova does not run any of these commands for you and never offers a
"stop SilentGuard" button. Stopping a security tool is the user's
decision.

---

## 6. Configure Nova to connect to it

Nova's connection to the SilentGuard API is **opt-in** at two levels:

- a **per-user** toggle in Settings (`silentguard_enabled`), and
- a small set of **host-level** environment variables read at startup.

Even with the host-level variables set, a user who has not flipped
their per-user toggle still sees the integration as `disabled` and
Nova never probes the API on their behalf.

Add the following to Nova's `.env` (or the equivalent environment for
your deployment):

```
NOVA_SILENTGUARD_ENABLED=true
NOVA_SILENTGUARD_AUTO_START=true
NOVA_SILENTGUARD_API_BASE_URL=http://127.0.0.1:8767
NOVA_SILENTGUARD_START_MODE=systemd-user
NOVA_SILENTGUARD_SYSTEMD_UNIT=silentguard-api.service
```

What each variable does:

| Variable | Effect |
|---|---|
| `NOVA_SILENTGUARD_ENABLED` | Host-level switch for the lifecycle helper. With it off, Nova never probes and never spawns. |
| `NOVA_SILENTGUARD_AUTO_START` | When also true *and* the integration is enabled, Nova may run `systemctl --user start <unit>` once after a failed reachability probe. Single-pass; no retry loop. |
| `NOVA_SILENTGUARD_API_BASE_URL` | Loopback API base URL Nova probes. Use the same port you set in `ExecStart=`. (Synonym: `NOVA_SILENTGUARD_API_URL`.) |
| `NOVA_SILENTGUARD_START_MODE` | Pinned to `systemd-user`. Any other value normalises to `disabled`, so a typo never opens a new spawn path. |
| `NOVA_SILENTGUARD_SYSTEMD_UNIT` | The user-level unit name to start. Validated against `^[a-z0-9][a-z0-9._-]*\.service$` and a forbidden-substring list before any spawn. |

Restart Nova so it picks up the new variables, sign in, and turn on
the per-user toggle in *Settings → Integrations → SilentGuard*. Nova
will probe `127.0.0.1:8767`, surface a calm "connected in read-only
mode" snapshot when it succeeds, and fall back to a calm "unavailable"
snapshot otherwise.

---

## What Nova does and does not do

What Nova does:

- Reads `GET /status`, `GET /connections`, `GET /blocked`,
  `GET /trusted`, and `GET /alerts` from the configured loopback URL.
- Surfaces the integration's state as a small read-only block in
  Settings, in the chat system prompt, and in
  `GET /integrations/silentguard/{lifecycle,summary}`.
- *Optionally*, when both the host-level and per-user switches are on
  and `NOVA_SILENTGUARD_AUTO_START=true`, runs `systemctl --user start
  <unit>` **once** after a failed probe. Single-pass, synchronous,
  bounded timeout, captured stderr, no retries.

What Nova does not do, by design:

- No write requests against any SilentGuard endpoint. The HTTP client
  has no `POST` / `PUT` / `PATCH` / `DELETE` helper.
- No firewall changes. Nova never invokes `iptables`, `nftables`,
  `firewalld`, `ufw`, `pf`, or `ipfw`, and the lifecycle helper's
  start argv is pinned to `systemctl --user start <unit>` — there is
  no `stop`, no `restart`, no `enable`, no `daemon-reload`.
- No privilege escalation. No `sudo`, `pkexec`, `doas`, `su`, or
  `runuser`. No system-level `systemctl`. No setuid wrapper.
- No background polling. The probe runs only when a Settings card
  mounts, when the user clicks *Refresh*, or when the chat layer
  builds a per-turn security context block. There is no scheduler,
  no `setInterval`, no inotify watcher, no thread.
- No telemetry. No outbound HTTP. No cloud call. No remote enrichment
  (no GeoIP, no reputation lookup).

---

## Security notes

### Localhost only by default

The example unit binds to `127.0.0.1`. Do not change this without
understanding the consequences. SilentGuard's read-only API exposes
network observations, alert kinds, blocked/trusted lists, and live
connection records — useful locally, sensitive on a network. Nova
itself probes loopback only.

If, despite the above, you need to expose the API on a non-loopback
interface for an *advanced* deployment (separate audit host, etc.),
treat that as a deliberate, documented choice:

- Bind explicitly (e.g. `--host 192.0.2.4`) — never `0.0.0.0`.
- Front it with mutual-TLS or an SSH tunnel; the read-only API has
  no built-in auth.
- Update Nova's `NOVA_SILENTGUARD_API_BASE_URL` accordingly and
  understand that the integration is no longer "local-first".

This document does not describe a remote-binding deployment. It is
deliberately out of scope.

### Read-only endpoints only

The API contract Nova expects is `GET`-only. If your version of
SilentGuard exposes write or control verbs as well, ensure the
read-only flag in `ExecStart=` disables them, or run a separate
build that lacks them. Nova will never call them, but defense in
depth means the surface should not exist on the loopback port.

### No privileged commands

Neither the unit nor Nova's lifecycle helper runs anything that
requires root. The unit lives under `~/.config/systemd/user/`. The
lifecycle helper invokes `systemctl --user start <validated-unit>`
with `shell=False`, no inherited stdin, a short timeout, and a strict
unit-name allowlist (`^[a-z0-9][a-z0-9._-]*\.service$` with a
forbidden-substring list). Anything that fails validation surfaces as
`could_not_start` *before* a spawn is attempted.

### No firewall changes

Neither the unit nor Nova ever invokes a firewall manager. The
lifecycle helper's argv is pinned to `systemctl --user start <unit>`;
there is no code path that constructs an `iptables` / `nftables` /
`ufw` / `firewalld` / `pf` / `ipfw` argv.

### No telemetry, no cloud calls

Nothing in this guide requires an outbound connection. The probe is
loopback. The lifecycle helper's only network surface is the local
URL you configured. There is no analytics, no version-check ping, no
"phone home".

---

## Troubleshooting

- **`systemctl --user status` reports `inactive (dead)` and `enable`
  has no effect on next boot.** You probably do not have lingering
  enabled. By default a user manager only runs while the user is
  logged in. If you want the unit running headlessly (no graphical
  session), enable lingering once: `loginctl enable-linger $USER`.
  This is a one-time, non-privileged setup step on most modern
  distros — confirm your local policy.

- **Nova reports `unavailable` even though the API is running.** Make
  sure `NOVA_SILENTGUARD_API_BASE_URL` matches the host *and* port the
  unit binds to, and that no firewall on the host blocks loopback.
  Run `curl http://127.0.0.1:<port>/status` from the same shell Nova
  runs in to confirm.

- **Nova reports `could_not_start`.** The unit name failed validation,
  the configured start backend is not `systemd-user`, or
  `systemctl --user start <unit>` exited non-zero / timed out. Check
  `journalctl --user -u silentguard-api.service` for the underlying
  reason; Nova deliberately does not surface raw stderr in the UI.

- **`systemctl --user daemon-reload` fails.** Confirm the user
  manager is running (`systemctl --user status` should respond). On
  hosts without a user manager, prefer running the API manually
  (step 2) and skip the systemd integration entirely — Nova will still
  probe whatever URL you point it at.

---

## Related documents

- [`docs/silentguard-integration-roadmap.md`](silentguard-integration-roadmap.md)
  — the full design plan for the Nova ↔ SilentGuard integration,
  including non-goals and explicit boundaries.
- [`deploy/systemd/README.md`](../deploy/systemd/README.md) — the
  hardened example unit for running Nova itself under systemd.
- [`deploy/systemd/silentguard-api.service`](../deploy/systemd/silentguard-api.service)
  — the example user unit referenced in this guide.
