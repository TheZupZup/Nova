# Hardened systemd unit for Nova

This directory contains an optional, hardened example unit file for running
Nova as a systemd service. It is a drop-in replacement for the minimal unit
shown in the top-level [README](../../README.md#running-as-a-systemd-service)
and adds sandbox restrictions that reduce the blast radius if Nova, one of
its tools, or a future plugin behaves unsafely.

The application itself is unchanged. None of the directives below alter
Nova's behavior — they only restrict what the process is allowed to do at
the kernel level.

## What this is, and what it is not

This unit is **defense in depth**, not a perfect security boundary. It
makes a number of common host-level attacks harder, but it does not:

- replace authentication or authorization inside Nova
- audit or filter what the model says or what tools do at the application
  layer
- contain a determined attacker who already has code execution as the
  `nova` user with full filesystem access to the checkout
- substitute for keeping Nova, Python, Ollama and the host kernel patched

If you need stronger isolation, run Nova in a container or VM and apply
this hardening to the container/VM host as well.

## Files

- `nova.service` — the hardened example unit. Ships with `USERNAME` and
  `/path/to/Nova` placeholders so it cannot accidentally be used unedited.
- `silentguard-api.service` — an optional user-level unit for running
  SilentGuard's loopback, **read-only** HTTP API as a background
  service. Used only by hosts that want to wire the optional Nova
  ↔ SilentGuard integration end-to-end. The unit binds to
  `127.0.0.1`, runs under `systemctl --user` (no sudo, no root), and
  is **disabled by default**. See
  [`docs/silentguard-background-service.md`](../../docs/silentguard-background-service.md)
  for the install / enable / disable walkthrough and the security
  notes (read-only only, no firewall changes, no telemetry).

## What stays the same

- Nova still needs **read/write access to `nova.db`** in its working
  directory. `ReadWritePaths=` is set to the Nova checkout for exactly that
  reason. Backups (`nova.db.backup`, see `core/memory.py`) are written to
  the same directory.
- Nova still talks to Ollama over HTTP through **`OLLAMA_HOST`** (default
  `http://localhost:11434`). `RestrictAddressFamilies=AF_INET AF_INET6
  AF_UNIX` keeps that path open. If you point `OLLAMA_HOST` at a remote
  host, no further unit changes are needed — the network call uses
  `AF_INET`/`AF_INET6` like any other outbound HTTP request.
- Optional outbound calls (DuckDuckGo web search, Open-Meteo weather,
  RSS learner) continue to work for the same reason.
- The web UI still listens on `0.0.0.0:8080` by default. Adjust the
  `ExecStart=` line if you bind elsewhere.

## What each hardening directive does

| Directive | Effect |
|---|---|
| `NoNewPrivileges=true` | The process and its children can never gain new privileges, even via setuid binaries. |
| `PrivateTmp=true` | `/tmp` and `/var/tmp` are private to the unit and wiped on stop. |
| `ProtectSystem=strict` | The whole filesystem is mounted read-only for the unit, except paths listed in `ReadWritePaths=`. |
| `ProtectHome=read-only` | `/home`, `/root` and `/run/user` are mounted read-only. Nova does not need to write there. |
| `ReadWritePaths=/path/to/Nova` | Restores write access to the Nova checkout so `nova.db` and its backups continue to work. Add extra entries if the database lives elsewhere. |
| `CapabilityBoundingSet=` (empty) | Drops every Linux capability for the unit. Nova is a userspace HTTP app and needs none. |
| `AmbientCapabilities=` (empty) | Belt-and-suspenders companion to the line above; ensures no capability is granted to the started process. |
| `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX` | Only IPv4, IPv6 and Unix domain sockets are usable. Blocks raw sockets, netlink, packet sockets, etc. |
| `LockPersonality=true` | The personality(2) syscall is locked, preventing runtime switches to legacy execution domains. |
| `MemoryDenyWriteExecute=true` | Refuses memory mappings that are both writable and executable — a common class of in-memory code injection. |
| `SystemCallArchitectures=native` | Rejects syscalls from non-native ABIs (e.g. i386/x32 on x86_64). |
| `RestrictSUIDSGID=true` | The unit cannot create files with the SUID/SGID bits or acquire them on exec. |
| `ProtectKernelTunables=true` | `/proc/sys`, `/sys`, `/proc/sysrq-trigger` and similar kernel tunables become read-only or invisible. |
| `ProtectKernelModules=true` | The unit cannot load or unload kernel modules. |
| `ProtectKernelLogs=true` | Blocks access to the kernel log ring buffer (`dmesg` / `/dev/kmsg`). Nova never reads kernel messages. |
| `ProtectControlGroups=true` | The cgroup hierarchy becomes read-only, so the unit cannot rewrite or escape its own resource limits. |
| `PrivateDevices=true` | Hides raw block devices and most of `/dev`; Nova only needs the standard streams and pseudo-terminals. |
| `RestrictNamespaces=true` | Blocks creation of user / mount / ipc / pid / net / uts / cgroup namespaces — a common building block for container-escape and self-sandboxing tricks. |
| `RemoveIPC=true` | Drops SysV IPC objects (shared memory, semaphores, message queues) on stop. Nova has no IPC peers. |
| `ProtectProc=invisible` + `ProcSubset=pid` | Hides other users' processes in `/proc` and exposes only PID directories. Stops Nova (or a spawned tool) from enumerating unrelated host processes. |
| `ProtectClock=true` | Refuses syscalls that would set the system clock. |
| `RestrictRealtime=true` | Blocks acquisition of realtime scheduling priorities — a frequent DoS vector. |
| `ProtectHostname=true` | The hostname becomes read-only for the unit. |
| `UMask=0077` | Files Nova writes (nova.db, backups, logs) are owner-only by default instead of world-readable. |
| `SystemCallFilter=@system-service` (+ denylist) | Allow-list of syscalls Nova needs (`@system-service`), with `@debug @mount @swap @reboot @raw-io @cpu-emulation @obsolete` explicitly removed. Filtered syscalls return `EPERM` rather than killing the process. |

For the authoritative reference, see
[`systemd.exec(5)`](https://www.freedesktop.org/software/systemd/man/systemd.exec.html).

## Installing the hardened unit

1. Edit `deploy/systemd/nova.service` and replace the placeholders:
   - `USERNAME` → the local Linux account that owns the Nova checkout.
   - `/path/to/Nova` → the absolute path of the checkout (the directory
     that contains `web.py` and where `nova.db` lives).

2. Install, reload, and start:

   ```bash
   sudo cp deploy/systemd/nova.service /etc/systemd/system/nova.service
   sudo systemctl daemon-reload
   sudo systemctl restart nova
   systemctl status nova
   ```

   On first install, also run `sudo systemctl enable nova` so the unit
   starts on boot.

3. Tail logs if anything looks off:

   ```bash
   journalctl -u nova -f
   ```

## Testing the hardening

systemd ships an analyzer that scores the sandbox profile of a unit:

```bash
systemd-analyze security nova.service
```

It prints a per-directive table and an overall exposure score (lower is
better). Use it to confirm the directives above are picked up, and to spot
anything you may want to tighten further on your specific host. A non-zero
score is normal — it does not mean the unit is misconfigured.

The unit shipped in this repo scores in the low single digits when
analysed offline:

```
→ Overall exposure level for nova.service: 1.9 OK :-)
```

The remaining points are deliberate trade-offs — Nova needs IPv4/IPv6
networking to reach Ollama, the weather API, and any optional
read-only SilentGuard endpoint, so the families and `PrivateNetwork=`
must stay open. If your host has no need for outbound traffic, you
can add an egress allow-list with `IPAddressAllow=` / `IPAddressDeny=`
on top of this unit.

## Troubleshooting

- **`Read-only file system` on startup.** `nova.db` is being written
  somewhere outside the path listed in `ReadWritePaths=`. Either move the
  database into the checkout or add its directory to `ReadWritePaths=`.
- **Cannot reach Ollama.** Confirm `OLLAMA_HOST` is set correctly and that
  the Ollama service is up (`systemctl status ollama`). Network access
  itself is not blocked by this unit.
- **Service fails immediately with no useful log.** Temporarily comment out
  the `MemoryDenyWriteExecute=`, `SystemCallArchitectures=`, and
  `SystemCallFilter=` lines and restart; some Python extensions built
  with JIT or non-native wheels can trip those. Re-enable one at a time
  once you've identified the culprit. The denylist
  (`~@debug @mount @swap @reboot @raw-io @cpu-emulation @obsolete`)
  rejects syscalls a userspace app should never reach for — if you need
  a tool from one of those groups, audit the dependency before relaxing
  the filter.
- **Files written by Nova look unreadable from another account.** The
  hardened unit ships with `UMask=0077`, so newly created files are
  owner-only. That is intentional — `nova.db` contains conversation
  history. If you actively need group-readable backups, override with
  `UMask=0027` (group-readable) and document the choice.
