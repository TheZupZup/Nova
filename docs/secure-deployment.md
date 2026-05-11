# Secure local deployment

Nova is a **local-first** assistant. Its safest deployment is the one
the rest of this repo already documents: one long-lived service, owned
by an unprivileged user, reachable only over your trusted local
network. This guide collects the boundaries that hold that promise
together and the few extra knobs you should turn before running Nova
on a host that is also reachable from elsewhere.

Treat Nova as a **powerful local service**, not as a trusted
root-level agent. Everything below is defence in depth on top of the
application-level controls Nova already ships (per-user auth, JWT
sessions, rate-limited login, scoped settings).

## Threat model — what this guide does and does not cover

In scope:

- A misbehaving, compromised or malicious dependency running inside the
  Nova process (Python, FastAPI, a model adapter, etc.).
- An attacker who reaches the HTTP listener and looks for ways to pivot
  beyond the Nova checkout — escape the user account, read files in
  other users' home directories, load a kernel module, change firewall
  rules, fiddle with the system clock.
- A tool / plugin that Nova spawns and that itself misbehaves.

Out of scope:

- A determined attacker who already has code execution **as the
  Nova user** with full read/write access to the checkout. The
  hardening reduces blast radius, it does not erase it.
- Patching Nova, Python, Ollama, Piper, your reverse proxy or your
  kernel. Keep them up to date.
- Authentication / authorization inside Nova. Those live in
  `core/auth.py` and the admin surface — they are unrelated to the
  systemd unit.

If you need stronger isolation than this guide provides, run Nova in
a container or VM and apply the same hardening to the
container / VM host.

## Recommended deployment shapes

Pick the option closest to where Nova actually lives on your network.
**Never expose Nova directly to the public Internet.**

### Local-only (laptop, home server)

This is the default. Nova binds to `0.0.0.0:8080`, but only your local
machine — or the LAN it sits on — can reach it. No proxy is required.

- Confirm the listener does **not** punch through your home router.
  Most home routers do not port-forward by default; if yours does,
  remove the rule.
- Optional: change the bind address to `127.0.0.1` in the unit's
  `ExecStart=` line to keep Nova loopback-only on a multi-user host:
  ```ini
  ExecStart=/path/to/Nova/.venv/bin/uvicorn web:app --host 127.0.0.1 --port 8080
  ```

### LAN-only

If Nova should be reachable from other devices on your network (a
tablet, a second laptop), keep the bind address on the LAN interface
but stop there. Do not add a public DNS record.

- Treat every device on the LAN as a user. Nova's per-user auth and
  per-user settings already model that.
- If your LAN includes guest devices, segregate them onto a guest
  SSID / VLAN so they cannot reach the Nova port.

### Remote access through a trusted gateway

If you need to reach Nova from outside your LAN, place an
authenticated gateway in front of it. Nova should still be **only**
reachable through the gateway — never bind it directly to a public
interface.

Choose one:

- **VPN.** WireGuard or Tailscale are simple, secure, and keep Nova
  inside the same trust boundary as the rest of your home network.
- **Cloudflare Access / Tailscale Funnel / similar Zero Trust
  gateway.** The gateway terminates TLS and applies its own
  identity / SSO check before forwarding requests to Nova on
  `127.0.0.1:8080`.
- **Reverse proxy on a hardened host (nginx, Caddy) with TLS and
  client-certificate / SSO authentication enabled.** The proxy is the
  only thing on a public IP; Nova listens on loopback or a private
  interface.

In every case:

- Terminate TLS at the gateway, not in Nova. Uvicorn's TLS support is
  fine but a hardened proxy is easier to keep patched.
- Require authentication **at the gateway as well**, not only inside
  Nova. Two layers of identity is the point.
- Keep an allow-list of source IPs / identities on the gateway. Public
  exposure with no allow-list is equivalent to "directly on the
  Internet".

## Systemd hardening

The hardened example unit lives at
[`deploy/systemd/nova.service`](../deploy/systemd/nova.service) and
the [`deploy/systemd/README.md`](../deploy/systemd/README.md) explains
each directive line by line. The unit shipped in this repo already
enforces:

- `NoNewPrivileges=true`, `CapabilityBoundingSet=` (empty),
  `AmbientCapabilities=` (empty) — Nova cannot gain new privileges and
  holds no Linux capabilities.
- `ProtectSystem=strict` + `ProtectHome=read-only` +
  `ReadWritePaths=/path/to/Nova` — the filesystem is read-only except
  for Nova's checkout (where `nova.db` lives). Home directories are
  read-only; other users' home directories are inaccessible.
- `PrivateTmp=true` — a private `/tmp`, wiped on stop.
- `PrivateDevices=true` — raw devices and most of `/dev` are hidden.
- `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX` — only the
  socket families Nova actually uses.
- `RestrictNamespaces=true`, `RestrictRealtime=true`,
  `RestrictSUIDSGID=true`, `LockPersonality=true`,
  `MemoryDenyWriteExecute=true`,
  `SystemCallArchitectures=native` — closes off namespace creation,
  realtime priority abuse, setuid creation, ABI swaps, and W^X
  bypasses.
- `ProtectKernelTunables=true`, `ProtectKernelModules=true`,
  `ProtectKernelLogs=true`, `ProtectControlGroups=true`,
  `ProtectClock=true`, `ProtectHostname=true`,
  `ProtectProc=invisible`, `ProcSubset=pid` — kernel surfaces,
  dmesg, and `/proc` cross-user visibility are locked down.
- `SystemCallFilter=@system-service` with a `~@debug @mount @swap
  @reboot @raw-io @cpu-emulation @obsolete` denylist — a userspace
  HTTP service does not need ptrace, mount, swap, reboot, raw I/O, or
  CPU emulation syscalls.
- `UMask=0077` — files Nova writes (including `nova.db` and its
  backups) are owner-only.
- `RemoveIPC=true` — SysV IPC objects are removed when the unit
  stops.

Verify the profile after installing:

```bash
systemd-analyze security nova.service
```

`systemd-analyze` prints a per-directive score and an overall exposure
number (lower is better). A non-zero score is normal — it does not
mean the unit is misconfigured.

### What stays writable

`ReadWritePaths=` is intentionally narrow:

- The Nova checkout (so `nova.db`, `nova.db.backup`, and any data
  Nova writes alongside the code continue to work).
- Anything you explicitly add, such as a database directory outside
  the checkout, or a log directory.

If you keep `nova.db` somewhere else, **add only that directory** to
`ReadWritePaths=`. Avoid giving Nova write access to the whole home
directory.

### What stays reachable

- **Ollama.** Nova talks to Ollama over HTTP through `OLLAMA_HOST`.
  `AF_INET` is allowed; if Ollama runs on the same host you can keep
  it on `127.0.0.1` and rely on the loopback path.
- **SilentGuard read-only API.** When configured (see
  [`docs/silentguard-background-service.md`](silentguard-background-service.md)),
  Nova GETs a fixed read-only path list on `127.0.0.1`. Read-only is
  the contract — the systemd unit does not need any extra grants
  beyond `AF_INET`.
- **Piper / local TTS.** Synthesis runs as a subprocess of Nova, in
  the same systemd unit. No network is involved.
- **Outbound calls.** DuckDuckGo search, Open-Meteo weather, the
  GitHub OAuth flow on the alpha channel, and the RSS learner all use
  `AF_INET` / `AF_INET6` like any other outbound HTTPS request.

If you want to harden further on a host with no need for outbound
traffic, you can add an egress allow-list with `IPAddressAllow=` /
`IPAddressDeny=`. Start permissive and tighten — a too-aggressive
allow-list will silently break the weather tool, the web search and
the GitHub OAuth gate.

## Least privilege

Hard rules:

- **Never run Nova as `root`.** The unit ships with `User=` /
  `Group=` so the process belongs to an unprivileged local account.
- **Do not give the Nova user `sudo` rights.** No part of Nova calls
  `sudo`, `pkexec`, `doas`, `su`, or `runuser`, and no part of Nova
  ever should.
- **Do not give the Nova user write access to `/etc`, system unit
  files, or `/usr`.** `ProtectSystem=strict` already enforces this at
  the kernel level; leave the filesystem permissions matching.
- **Do not let Nova execute model-generated shell commands.** Nova
  reads context from narrow local APIs (SilentGuard's read-only
  endpoint, Piper as a subprocess with a fixed argv, the LLM via
  Ollama). Anything that would let model output reach `subprocess`
  is a regression.

A useful exercise on a fresh host:

```bash
sudo -u nova sudo -l   # should print "Sorry, user nova may not run sudo"
```

## Secret material

- The `.env` file is the only place secrets live. Keep it
  mode `0600` and owned by the Nova user. The hardened unit's
  `ProtectHome=read-only` keeps anything outside `ReadWritePaths=`
  read-only from inside Nova; combine that with normal POSIX
  permissions so other local accounts cannot read the file.
- Rotate `NOVA_SECRET_KEY` if it ever lands in a backup that leaves
  the host.
- `GITHUB_CLIENT_SECRET` only matters when `NOVA_CHANNEL=alpha`. If
  you do not use the alpha channel, leave it empty — the OAuth gate
  is wired so a missing secret returns `503` rather than silently
  permitting open access.

## Backups

- `nova.db` and `nova.db.backup` live in the Nova checkout. Back the
  whole directory up — there are no out-of-band state files.
- Treat backups as **sensitive**: they contain conversation history,
  per-user settings, and (if you enabled the natural memory layer)
  user-authored memories. Encrypt at rest.
- Test the restore path before you need it. The simplest restore is
  `sudo systemctl stop nova && cp nova.db.backup nova.db && sudo
  systemctl start nova`.

## Audit and logs

- `journalctl -u nova` is the canonical log surface for the systemd
  unit. Rotate the system journal per your distribution's
  defaults — the hardened unit does not write a separate log file.
- Nova logs at INFO level by default. Avoid raising verbosity in
  production — DEBUG can include parsed memory blocks.
- The SilentGuard provider is intentionally **read-only** and never
  emits "Nova blocked X" / "Nova unblocked X" log lines, because Nova
  is not the enforcement layer. If you see such a line, treat it as
  a regression and file an issue.

## What Nova will NOT do

These are firm boundaries, not future work:

- Nova does not change firewall rules. Ever.
- Nova does not call `sudo`, `pkexec`, `doas`, `su`, or `runuser`.
- Nova does not execute model-generated shell commands.
- Nova does not run anything as root.
- Nova does not auto-download Piper, voice models, or any other
  binary.
- Nova does not block / unblock IPs or hostnames. SilentGuard owns
  enforcement.
- Nova does not send audio, prompts, or conversation history to a
  third-party cloud service.

If a future feature would cross any of these lines, it is the wrong
feature for Nova.

## Emergency stop (future work)

There is no dedicated "panic button" yet. Today the supported way to
take Nova fully offline on a host is:

```bash
sudo systemctl stop nova
```

A future "safe mode" might disable optional integrations and pause
background work without killing the HTTP listener, so an
administrator can keep talking to Nova while the rest of the system
is being investigated. That work is tracked alongside the broader
SilentGuard integration roadmap; nothing in this guide depends on it.

## Further reading

- [`deploy/systemd/README.md`](../deploy/systemd/README.md) — the
  per-directive walkthrough for the hardened unit.
- [`docs/silentguard-integration-roadmap.md`](silentguard-integration-roadmap.md)
  — the architectural rationale for keeping enforcement out of Nova.
- [`docs/silentguard-background-service.md`](silentguard-background-service.md)
  — running SilentGuard's loopback API as a user service.
- `systemd-analyze security nova.service` — your single best source of
  truth after a unit edit.
