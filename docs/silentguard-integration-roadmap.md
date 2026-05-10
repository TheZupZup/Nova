# Nova ↔ SilentGuard Integration — Architecture and Phase 1 Plan

> **Status: design plan only.** This document describes how Nova should
> talk to SilentGuard. **Nothing in this document is implemented yet
> beyond what already exists in `core/security_feed.py` and
> `core/integrations/silentguard.py`.** It exists so contributors can
> argue with the direction before more code lands, and so future issues
> have a stable target to reference.
>
> Do not cite this document as evidence that any feature exists. If a
> section describes something Nova does not do today, it does not do it.
>
> **Scope guards.** This is a plan for a **read-only, opt-in, local-host
> integration**. It is explicitly *not* a plan for Nova to act on the
> network, manage rules, push firewall changes, or run autonomous defence
> loops. The one carefully scoped carve-out — described in detail below —
> is that Nova may probe whether the SilentGuard read-only API is
> reachable, and, on an explicit user click, run a *user-configured,
> non-privileged* command to start it. That carve-out never runs sudo,
> never modifies firewall rules, and never happens without a visible
> user action. Nova is the cognitive layer; SilentGuard remains the
> security/network layer. Any proposal that blurs that line should be
> rejected.
>
> **Posture, in one line.** Transparency, safety, local-first design,
> and user control over automatic behaviour beat convenience every time.

---

## Table of contents

1. [Vision and roles](#1-vision-and-roles)
2. [What exists today](#2-what-exists-today)
3. [What SilentGuard exposes today](#3-what-silentguard-exposes-today)
4. [Principles](#4-principles)
5. [Architecture](#5-architecture)
6. [Module structure](#6-module-structure)
7. [Data contract](#7-data-contract)
8. [Nova-side HTTP surface](#8-nova-side-http-surface)
9. [Conversational shapes](#9-conversational-shapes)
10. [Security considerations](#10-security-considerations)
11. [Phase 1 scope (and what is *not* in it)](#11-phase-1-scope-and-what-is-not-in-it)
12. [Future phases (sketches only)](#12-future-phases-sketches-only)
13. [Recommended implementation order](#13-recommended-implementation-order)
14. [Non-goals, restated](#14-non-goals-restated)

---

## 1. Vision and roles

The two projects answer different questions and should keep doing so.

| Project       | Responsibility                                                                                       | Posture                |
| ------------- | ----------------------------------------------------------------------------------------------------- | ---------------------- |
| SilentGuard   | Observes connections, classifies trust, persists rules (known/trusted/blocked), enforces local rules | The **security tool**  |
| Nova          | Explains, summarises, contextualises that data in plain language at the user's request               | The **cognitive layer** |

The user's mental model should be: *SilentGuard sees the network; Nova
helps me understand what SilentGuard saw*. Nova does **not** become the
firewall. Nova does **not** decide what to block. Nova does **not** push
rules into SilentGuard. SilentGuard remains independently usable
(GTK/TUI today) whether Nova is running or not. Nova remains
independently usable whether SilentGuard is installed or not.

The integration also has a small **presence** responsibility. When the
user has flipped the "Enable SilentGuard integration" switch, Nova
should make it obvious whether SilentGuard is running, and — only if
the user has configured a start command — offer a visible button to
start it. Nova never starts SilentGuard silently, never starts it with
elevated privileges, and never keeps trying to start it in the
background. Presence is *shown*; presence is not *enforced*.

Phase 1 cashes that vision in for a small, honest set of capabilities:
read SilentGuard's existing on-disk state, surface a calm read-only
slice of it in Nova's UI and chat, and offer the user a transparent,
opt-in way to keep SilentGuard available — and stop there.

---

## 2. What exists today

Nova already has a working — but narrow — first slice of this
integration. The Phase 1 plan below extends it; it does not start from
zero.

### 2.1 `core/security_feed.py`

A passive parser for SilentGuard's connection memory file.

- Reads `~/.silentguard_memory.json` (path overridable via
  `NOVA_SILENTGUARD_PATH`).
- Hard-coded read-only: no writes, no subprocess, no socket, no DNS, no
  privileged calls. The test suite asserts the module does not even
  *import* `subprocess`, `socket`, `shutil`, `ctypes`, or `signal`.
- Tolerant of missing files, malformed JSON, oversized files (5 MB cap),
  and unknown trust strings — every public function returns an empty
  result rather than raising.
- Produces `SecurityEvent` records, a `summarize_events` aggregate, and
  a prompt-friendly summary string.
- Has a small `is_security_query` heuristic that decides when to inject
  the summary into the chat system prompt.

### 2.2 `core/integrations/silentguard.py`

A per-user gate over `security_feed`.

- `is_enabled(user_id)` reads `silentguard_enabled` from
  `user_settings`; default is `false`.
- `status(user_id)` today returns `disabled` / `connected` /
  `not_found`. Phase 1 splits `not_found` into the more honest
  `offline` (configured but unreachable) and `not_configured`
  (nothing to reach), see §7.1.
- `recent_events(...)` and `recent_events_summary(...)` short-circuit
  to `[]` / `None` when the user has not opted in.

### 2.3 `core/chat.py`

When a user asks a security-shaped question (`is_security_query`
matches), Nova injects the SilentGuard summary into the system prompt
under `SECURITY_SYSTEM_PROMPT`. The prompt explicitly tells the model
that Nova has *no* way to act and must not suggest destructive
commands.

### 2.4 `web.py`

`GET /integrations/status` returns the per-user availability snapshot
(state, detail, enabled flag). The settings panel surfaces a single
on/off toggle for SilentGuard.

### 2.4.bis `core/security/lifecycle.py` (opt-in non-privileged starter)

A small, narrowly scoped helper that lets Nova *optionally* start
SilentGuard's local read-only API service. Every gate defaults off,
so unconfigured Nova installs never try to spawn anything.

The helper is the **only** module in the security package allowed to
import :mod:`subprocess` and :mod:`shutil`. The forbidden-imports
test continues to pin every other file in the package to read-only.

Configuration (env vars, all defaulting to safe values):

  * ``NOVA_SILENTGUARD_ENABLED`` — host-level switch for the
    lifecycle helper. Per-user ``silentguard_enabled`` still gates
    whether SilentGuard data is surfaced to a given user.
  * ``NOVA_SILENTGUARD_AUTO_START`` — when both this and the host
    switch are true, the helper may spawn the configured start
    command after a failed reachability probe. Defaults to off.
  * ``NOVA_SILENTGUARD_API_BASE_URL`` — accepted as a synonym of the
    pre-existing ``NOVA_SILENTGUARD_API_URL``. Loopback-only, e.g.
    ``http://127.0.0.1:8767``.
  * ``NOVA_SILENTGUARD_START_MODE`` — selects the start backend.
    ``"systemd-user"`` is the only allowed non-disabled value, on
    purpose: it pins Nova to ``systemctl --user start <unit>``.
    Anything else (including typos) normalises to ``"disabled"``.
  * ``NOVA_SILENTGUARD_SYSTEMD_UNIT`` — the user-level unit name to
    start. Validated against ``^[a-z0-9][a-z0-9._-]*\.service$`` and
    a forbidden-substring list (``..``, path separators, shell
    metacharacters, whitespace, control characters); rejected
    configurations surface as ``state="could_not_start"`` *before*
    any spawn.

Behaviour, in order:

  1. If integration is disabled → ``state="disabled"``.
  2. Probe :class:`SilentGuardProvider` once. If reachable →
     ``state="connected"``.
  3. If auto-start is off, or start-mode is not ``"systemd-user"``,
     or the unit name fails validation → ``state="unavailable"`` /
     ``state="could_not_start"`` as appropriate, with no spawn.
  4. Otherwise spawn ``systemctl --user start <unit>`` with strict
     argv (``shell=False``, no inherited stdin, captured stderr,
     short timeout). If the spawn itself fails →
     ``state="could_not_start"``. If it succeeds, wait one bounded
     delay and re-probe once. Connected → ``state="connected"``;
     still unreachable → ``state="starting"`` (the unit was
     accepted but has not bound its socket yet).

Hard guarantees, asserted in code and tests:

  * No ``sudo`` / ``pkexec`` / ``doas`` / ``su`` / ``runuser``.
  * No system-level ``systemctl`` — only ``systemctl --user``.
  * No firewall command (``iptables`` / ``nftables`` / ``ufw`` …).
  * No shell interpretation; argv list only.
  * No command sourced from chat input or remote URLs.
  * No background polling, no retry loop, no notifications.
  * Single-pass and synchronous; never raises into chat or web.
  * If SilentGuard is not installed / not configured, Nova continues
    working normally — every gate falls back to a calm
    ``state="disabled"`` / ``state="unavailable"`` snapshot.

The helper is wired into the web layer via
``GET /integrations/silentguard/lifecycle``, gated by the per-user
``silentguard_enabled`` setting, and surfaced in the same calm
``state`` vocabulary the Settings card renders. There is no
auto-start on Nova boot, on session resume, or on tab focus — the
helper runs only when the user (or operator-driven UI) explicitly
asks for the lifecycle status.

### 2.4.ter Settings status surface (visibility only)

The Settings panel grows a small calm SilentGuard status row in the
General pane. It is **not** a security dashboard, **not** an alert
surface, and **not** a control surface — it is a single read-only
card that tells the user, in one sentence, what the lifecycle helper
believes the integration's current state is.

The row is fed by ``GET /integrations/silentguard/summary``, a small
endpoint that combines two existing read-only paths:

  * the per-user gate + lifecycle state already returned by
    ``/integrations/silentguard/lifecycle``, and
  * the optional summary counts already produced by
    :meth:`SilentGuardProvider.get_summary_counts` (alerts, blocked,
    trusted, connections — only when the read-only HTTP transport is
    configured *and* reachable).

Stable response shape::

    {
      "lifecycle": {state, enabled, auto_start, start_mode, unit, message},
      "counts": {"alerts", "blocked", "trusted", "connections"} | null,
      "connection_summary": {                       # all keys optional
          "total":   int,
          "local":   int,
          "known":   int,
          "unknown": int,
          "top_processes":    [{"name", "count"}, ...],
          "top_remote_hosts": [{"host", "count"}, ...],
      } | null,
      "host_enabled": bool
    }

``connection_summary`` carries the richer read-only aggregate
SilentGuard's optional ``GET /connections/summary`` endpoint
returns. Every key inside it is independently optional — Nova omits
values it does not have rather than inventing them, and a missing
endpoint, malformed payload, or older SilentGuard build all map to
``connection_summary: null``. The Settings card uses it to render an
optional, compact ``"N local · N known · N unknown"`` subtitle when
all three of those fields parse cleanly; partial breakdowns degrade
gracefully to the basic four counts above. This is **visibility
only** — no blocking, no firewall control, no dashboard.

The existing ``/integrations/silentguard/lifecycle`` and
``/integrations/status`` endpoint shapes are unchanged — the summary
endpoint is purely additive so the prompt-context, chat, and admin
flows that already depend on those payloads keep working byte for byte.

Headline mapping (state → calm sentence):

  * ``disabled`` (per-user opt-in off, or host switch off) →
    *"SilentGuard integration disabled"* (or *"SilentGuard not
    configured"* when the user has opted in but the host has not).
  * ``connected`` → *"SilentGuard connected in read-only mode"* with
    a *"Read-only"* badge and, when counts are available, the line
    *"N alerts · N blocked · N trusted · N connections"*.
  * ``starting`` → *"Starting local SilentGuard API…"*.
  * ``could_not_start`` → *"Could not start SilentGuard"*.
  * ``unavailable`` → *"SilentGuard unavailable"*. When the operator
    has integration on but auto-start off, the calm subtext
    *"Auto-start disabled"* surfaces alongside it.

The card carries one explicit *Refresh* button. There is **no**
background polling, no ``setInterval``, no scheduler, no auto-refresh
on tab focus — the only triggers are (1) opening the Settings
overlay and (2) clicking *Refresh*. This honours the
"reachability probe triggers" exhaust-list in §10.11: the summary
endpoint runs ``ensure_running`` once per call, never on a timer.

What the surface deliberately does **not** do:

  * No firewall control, no block / unblock buttons, no rule editor.
  * No notifications, toasts, or "you have alerts" badges.
  * No remote / cloud / telemetry calls.
  * No raw exception text in the UI; provider failures map to the
    same calm "unavailable" wording the lifecycle helper already
    uses.
  * No Alpha-only state — the surface follows the same auth /
    per-user / channel gates as every other ``/integrations/*``
    endpoint.

### 2.4.quater Confirmation-gated SilentGuard mitigation surface

Nova's Settings card grew a small **mitigation block** in this PR.
SilentGuard owns detection and enforcement; Nova is the cognitive
layer. The block lets the user *ask SilentGuard* — explicitly, after
confirming — to enable temporary mitigation, or to disable it again.
Nova never blocks IPs directly, never runs firewall commands, never
runs `sudo`, never executes shell, never silently auto-blocks.

The mitigation surface lives in two new files:

  * `core/security/silentguard_mitigation.py` — a tiny POST-capable
    HTTP client for SilentGuard's mitigation endpoints, plus the
    sanitiser helpers (`_normalise_mode`, `_normalise_timestamp`,
    `_parse_state`) that protect Nova's UI from any unreviewed field
    SilentGuard might one day return. The client ships exactly three
    methods (`get_state`, `enable_temporary`, `disable`), and every
    POST carries the SilentGuard acknowledgement payload
    (`{"acknowledge": true}`). Errors collapse to `None` (read) or
    `MitigationActionResult(ok=False, ...)` (action) — no exception
    ever reaches the caller.
  * `core/security/silentguard.py::SilentGuardProvider` grew three
    matching methods (`get_mitigation_state`,
    `enable_temporary_mitigation`, `disable_mitigation`) that delegate
    to the new client. The provider refuses to issue a write when its
    own status probe says SilentGuard is unavailable, so a stray POST
    cannot fire into a void. The existing read-only `get_status`,
    `get_summary_counts`, and `get_connection_summary` paths stay
    byte-identical; pinned tests assert they never invoke any
    mitigation method.

The read-only `core/security/silentguard_client.py` stays read-only.
The forbidden-verb assertion in `tests/test_security_provider.py`
still passes (no `.post(`/`.put(`/`.delete(`/`.patch(` calls in that
file). Splitting the POST capability into its own module on purpose
keeps the existing read paths' safety contract intact.

#### Modes Nova knows about

The mitigation client recognises exactly three modes; anything else
SilentGuard returns coerces to `"unknown"` so the UI never paints an
unreviewed value:

  * **`detection_only`** — the default. SilentGuard observes and logs
    but takes no enforcement action. The Settings card surfaces this
    as *"Detection only"* with the calm explainer *"SilentGuard
    detected activity that may resemble a flood pattern. Temporary
    mitigation is available, but it is not active yet."*
  * **`ask_before_blocking`** — SilentGuard prompts before any block.
    Surfaced as *"Ask before blocking"*; no action is taken until the
    user confirms.
  * **`temporary_auto_block`** — SilentGuard's temporary mitigation is
    active. Surfaced as *"Temporary auto-block active"*. The Settings
    card shows the optional `expires_at` timestamp verbatim when
    SilentGuard provides one (sanitiser-validated, length-capped, no
    re-parsing).

#### Confirmation flow (two clicks per mutation)

  1. The user opens Settings or clicks Refresh on the SilentGuard
     status card → Nova issues `GET /integrations/silentguard/mitigation`
     once. The block hides itself when SilentGuard returns no parsed
     state, so a non-mitigation install never sees a misleading half-
     painted control.
  2. The user clicks *Enable temporary mitigation* (or *Disable
     mitigation* when a temporary block is already active) → Nova
     shows an inline confirmation prompt. **No HTTP call is issued
     yet.**
  3. The user clicks *Confirm* → Nova issues
     `POST /integrations/silentguard/mitigation/enable-temporary`
     (or `/disable`) with the body `{"acknowledge": true}`. SilentGuard
     applies the change and returns the new state; Nova repaints the
     mode badge and shows a calm *"Temporary mitigation enabled."* /
     *"Mitigation disabled."* status line.

The third button — *Keep detection only* — is purely a UX
acknowledgement. It does **not** call any endpoint, because
detection-only is the default mode. A pinned test (`UI controller
tests`) asserts this remains true: turning *Keep detection only* into
a network call would be a *new* mitigation capability that needs its
own review.

#### Nova-side endpoint contract

| Method | Path                                                         | Effect                                                     |
| ------ | ------------------------------------------------------------ | ---------------------------------------------------------- |
| GET    | `/integrations/silentguard/mitigation`                       | Read-only mitigation snapshot for the caller.              |
| POST   | `/integrations/silentguard/mitigation/enable-temporary`      | Acknowledged enable; relays to SilentGuard's `/mitigation/enable-temporary`. |
| POST   | `/integrations/silentguard/mitigation/disable`               | Acknowledged disable; relays to SilentGuard's `/mitigation/disable`. |

Stable response shape (all four endpoints):

```json
{
  "ok": true,
  "available": true,
  "state": {
    "mode":       "detection_only" | "ask_before_blocking"
                  | "temporary_auto_block" | "unknown",
    "active":     true,
    "expires_at": "2026-05-10T13:00:00Z" | null
  },
  "message": "Temporary mitigation enabled."
}
```

`available` is `false` whenever SilentGuard's mitigation API is not
reachable / not configured / returned an unparseable payload. The
`message` field is calm, user-safe wording — never a raw exception or
HTTP body — chosen by the provider layer so the endpoint can surface
it verbatim.

Safety contract:

  * Every endpoint requires an authenticated session.
  * Both POST endpoints require the body `{"acknowledge": true}`. Without
    it, the endpoint returns **400** and never issues any call to
    SilentGuard. This is a hard server-side checkpoint independent of
    the UI confirmation step.
  * Both endpoints honour the per-user `silentguard_enabled` gate. A
    user who has not opted in sees a calm
    `{"ok": false, "available": false, "state": null, ...}` payload
    and the provider is **not instantiated**.
  * The `_SilentGuardMitigationRequest` body model declares only
    `acknowledge` and ignores extra fields — there is nothing for an
    attacker to smuggle in (no IP list, no command string, no mode
    selector).
  * Disable does **not** mean Nova writes to SilentGuard's data files.
    Nova never touches `~/.silentguard_rules.json` or
    `~/.silentguard_memory.json`. Mitigation mode lives inside
    SilentGuard.
  * The read-only `/integrations/silentguard/summary` endpoint is
    unchanged. A pinned test (`TestReadOnlyEndpointsDoNotCallMitigation`)
    asserts that hitting `/summary` never triggers any mitigation
    read or write. The mitigation block on the Settings card pulls
    from its own URL on the same trigger set as the existing summary
    refresh (Settings open + Refresh click) — there is **no
    background polling** added by this PR.

#### What this PR explicitly does **not** ship

  * No notifications, toasts, or "you have alerts" badges. The
    mitigation block is rendered inside the existing Settings card
    only when the user opens it.
  * No permanent blocking control. The SilentGuard
    `POST /blocked/{ip}/unblock` path mentioned in the original brief
    is intentionally out of scope; it would only land in a later PR
    after its own review.
  * No autonomous behaviour — mitigation never enables itself, never
    auto-disables, never re-arms after a failure.
  * No new aggressive language. The copy stays calm; a forbidden-
    word assertion in `test_silentguard_mitigation_card_wiring`
    rejects strings like *"under attack"*, *"alert!"*, *"danger"*,
    *"urgent"* in the mitigation i18n set.
  * No subprocess, no shell, no firewall command. The mitigation
    module imports neither `subprocess` nor `shutil`; a forbidden-
    imports test pins this.

The default behaviour Nova ships in this PR is therefore *unchanged
from before*: a fresh install starts with detection-only and never
issues a mitigation POST until the user explicitly clicks *Enable
temporary mitigation* and then confirms.

### 2.5 `core/security/context.py` (read-only chat context)

A small builder that produces the per-turn "Security context:" block
appended to Nova's chat system prompt. The block is short,
deterministic, read-only, and intentionally calm:

- when nothing is configured → *"SilentGuard integration: not
  configured."* + the read-only behaviour line.
- when configured but unreachable → *"SilentGuard integration:
  read-only API is unavailable."* + the same read-only line.
- when reachable → *"SilentGuard integration: connected in read-only
  mode."*, optionally followed by *"Current summary: N alerts, N
  blocked items, N trusted items, N active connections."* (counts
  appear only when the optional HTTP transport is configured and
  responsive; the file fallback omits them).
- when reachable **and** SilentGuard supports the optional
  ``GET /connections/summary`` endpoint, two further bullets may be
  appended: *"Connection summary: N active connections, N local, N
  known, N unknown."* and *"Top processes: firefox 8, python 4,
  steam 3."* (and, if SilentGuard supplies them, *"Top remote hosts:
  …"*). Each field inside the summary is independently optional —
  Nova omits a value it does not have rather than inventing it, and
  a missing endpoint or malformed payload simply degrades to the
  count-less wording above. This is *visibility only*: it does not
  add firewall control, blocking, or autonomous behaviour — Nova
  still only summarises and explains.

Every variant ends with the same fixed clause:
*"Allowed behavior: explain and summarize only; do not perform
firewall or rule actions."* So Nova always knows what state the
provider is in **and** that it must not act on it.

The block is built on every turn from the existing
`core/security/SilentGuardProvider`. There is no background polling,
no scheduled refresh, no notification. It is a pull, on the same
trigger that already builds the time context. Failures inside the
provider map to the calm "unavailable" wording; raw exception text,
IPs, process names, and timestamps never enter the prompt.

This is what *"surface SilentGuard read-only context"* ships as in
this PR. It is **not** notification behaviour, **not** a firewall
control surface, **not** an autonomous alerting layer. The
"Capabilities" line in §11.1 above stays accurate: actions are still
explicitly out of scope.

### 2.6 What is missing today

- **Trusted IPs and blocked IPs** are not exposed. SilentGuard persists
  them in `~/.silentguard_rules.json` (see §3); Nova does not read that
  file at all yet.
- **Connection vs. alert separation.** Today everything is a flat
  `SecurityEvent`. The chat path treats the whole feed as one blob;
  there is no first-class notion of "alert" (an unknown / suspicious
  event worth surfacing) distinct from "connection" (any observation).
- **No Nova-side HTTP surface for the data**. The web UI can only see
  the on/off status. There is no `/integrations/silentguard/connections`
  endpoint the chat UI could render as a calm panel.
- **No connector abstraction.** All paths read the file directly.
  Swapping in an HTTP transport later (if SilentGuard ever ships a
  loopback REST API) would require touching every call site.
- **No reachability / lifecycle surface.** Today the integration has
  two states from the user's point of view (`enabled` and the data
  Nova managed to read), and no concept of "SilentGuard is
  installed but not currently running." The Settings card cannot
  distinguish *offline* from *not configured*, and there is no
  user-visible affordance to start SilentGuard from Nova when the
  user is the one who decides to.

The chat-prompt presence gap (Nova not knowing whether SilentGuard
exists at all) is closed by the read-only context block in §2.5.
The remaining gaps below — rules, alerts, connector abstraction,
HTTP surface, lifecycle — are still the Phase 1 scope.

Phase 1 is exactly the set of changes that closes those gaps.

---

## 3. What SilentGuard exposes today

SilentGuard (the project Nova integrates with) is a Python 3 / GTK / TUI
network monitor. Its persisted state, as of the current public
repository, is two JSON files in the user's home directory:

### 3.1 `~/.silentguard_memory.json`

The connection observation log Nova already consumes. Each entry
includes (with some shape variance Nova's parser already absorbs):

- `ip` (string, required)
- `process` (string)
- `port` (int 0–65535)
- `trust` ∈ `Known | Unknown | Local | Blocked`
- `timestamp` (string, optional, format-tolerant)

### 3.2 `~/.silentguard_rules.json`

The user's trust classifications. The documented top-level keys are:

- `known_processes` — processes the user has marked as expected.
- `trusted_ips` — IPs the user has marked as benign.
- `blocked_ips` — IPs the user has marked as unwanted.

Internal shape (as observed publicly): list-of-strings or list-of-dicts
per key, depending on SilentGuard version. The Nova-side parser must
tolerate both, and treat anything it cannot decode as "absent" rather
than raising.

### 3.3 Recommended SilentGuard service / daemon model

Going forward, the integration *prefers* the **service / daemon
shape** of SilentGuard: SilentGuard runs as a long-lived, user-level
process and exposes a *loopback-only, read-only* JSON API on a port
the operator chooses (`http://127.0.0.1:<port>`). On Linux, the
recommended deployment is a `systemd --user` unit
(e.g. `silentguard.service`) that the user enables and starts in
their own session — never a system-level unit, never a setuid
wrapper. The file-based contract in §3.1 / §3.2 remains a supported
fallback for legacy installs that do not run the daemon, but the
API is the contract Nova's reachability probe, Settings status
card, and "Start SilentGuard" action are designed around.

When `NOVA_SILENTGUARD_API_URL` is set, Nova's `SilentGuardProvider`
probes that endpoint via the small `SilentGuardClient` in
`core/security/silentguard_client.py` instead of stat'ing the on-disk
file. The client only ever issues `GET` requests against a fixed path
list (`/status`, `/connections`, `/connections/summary`, `/blocked`,
`/trusted`, `/alerts`); there are no write helpers, no shell calls,
and no firewall actions. Any transport, decode, or HTTP failure maps
to the same calm `available=False` snapshot the file probe produces,
so Nova's design still works without the API.

The optional `/connections/summary` path is a recent addition — older
SilentGuard builds simply do not serve it, and the client treats a
missing endpoint exactly like any other failure (returns ``None``).
The expected payload is a JSON object with any subset of the
following fields:

```jsonc
{
  "total":   55,                 // total active connections in the window
  "local":   38,                 // connections classified Local
  "known":   12,                 // connections classified Known
  "unknown": 5,                  // connections classified Unknown
  "top_processes":    [{"name": "firefox", "count": 8}, ...],
  "top_remote_hosts": [{"host": "1.2.3.4", "count": 12}, ...]
}
```

Every field is independently optional. Nova's provider drops fields
it cannot validate (non-int counts, non-string names, oversized
strings, anything that fails the `[A-Za-z0-9._:/-]` whitelist) and
caps the `top_*` lists at five entries before they leave the
provider layer — the prompt context block and the Settings card
trust the post-normalisation shape.

Nova surfaces the recommended setup (the `systemd --user` unit, the
loopback port, the read-only flag) via a "View setup instructions"
link in the Settings card. Nova does **not** install the unit, write
to `~/.config/systemd/user/`, or invoke any system-level
`systemctl`. The user is the one who installs SilentGuard; Nova just
explains what good looks like and offers to run a start command if
the user has explicitly configured one.

---

## 4. Principles

These restate Nova's existing principles in the SilentGuard context.

### 4.1 Local-first

Every byte read or written by this integration stays on the user's
host. No outbound calls. No telemetry. No "anonymous usage" reporting.
If SilentGuard is on this machine, Nova talks to it; if not, the
integration reports `offline` or `not_configured` and Nova continues
working.

### 4.2 Read-only by default — and probably forever

Phase 1 is read-only with no opt-in path to writes. Future write
support — if it ever happens — would not arrive as a setting toggle,
but as a separate, deliberately scoped phase with its own design
review (see §12). The default remains: Nova reads, SilentGuard
enforces.

### 4.3 Opt-in per user

Per-user setting `silentguard_enabled` already gates the integration.
A second person on the same Nova install does not see SilentGuard data
unless they have flipped their own switch. Nothing in this plan
changes that.

### 4.4 Graceful absence

Every public read returns an empty result on the "tool absent" path
and never raises. A missing file, a malformed file, an oversize file,
a permission error, or a shape mismatch all map to "no data," not to
an exception escaping into the chat layer.

### 4.5 Calm UX over flashy AI

The voice of this integration is meant to be *calm*. Nova explains.
Nova summarises. Nova does not invent threats. Nova does not push the
user toward action. When the data is empty, Nova says so plainly.
Any tone work in the prompts should bias toward "informative and
unhurried," not "alert and urgent."

### 4.6 Determinism over inference

The summarisation logic (counts, groupings, anomaly heuristics) lives
in code, not in the LLM. The LLM phrases the result; the numbers come
from the parser. This makes the surface reviewable and keeps the
"why does Nova think IP X is suspicious?" answer auditable.

### 4.7 No autonomous behaviour

Nova does not poll SilentGuard in the background. The data is read
*on demand* — when the user asks, when the chat layer has matched a
security-shaped question, or when the UI panel is open. There are no
loops, no schedulers, no notifications pushed at the user. This is a
pull model on purpose.

### 4.8 Transparent presence

When the integration is enabled, Nova always shows the user whether
SilentGuard is reachable. There is no hidden retry, no quiet
auto-start, no "we'll keep trying in the background." The Settings
card has three honest states — *connected*, *offline*, *not
configured* — and they always reflect the most recent on-demand
probe. If the user has not configured a start command, the "Start
SilentGuard" button is *absent*, not greyed-out and unexplained;
"View setup instructions" takes its place. The user always knows
what Nova is about to do before Nova does it.

### 4.9 No privilege escalation, ever

Nova never elevates privilege on the user's behalf. The "Start
SilentGuard" action runs the configured command as the *Nova process
user*, with no `sudo`, no `pkexec`, no `doas`, no `su`, no `runuser`,
no setuid wrapper, and no system-level `systemctl`. If running
SilentGuard as a system service is the operator's preferred
deployment, the in-Nova start action is simply not offered, and the
setup instructions point at the operator's own `systemctl` workflow.
Nova does not try to do it for them. The same rule applies to
firewall managers (`iptables`, `nftables`, `firewalld`, `ufw`, `pf`,
`ipfw`): Nova never invokes them, and the start-command validator
rejects argv lists that look like they would.

---

## 5. Architecture

### 5.1 Layered shape

```
┌────────────────────────────────────────────────────────────┐
│  Nova chat / web UI                                        │
│  - "Show recent suspicious connections" → renders panel    │
│  - "Why was 5.6.7.8 blocked?" → chat turn with context     │
└────────────────────────────────────────────────────────────┘
              │                                    │
              ▼                                    ▼
┌──────────────────────────┐   ┌──────────────────────────────┐
│ web.py                   │   │ core/chat.py                 │
│ /integrations/silentguard│   │ injects summaries into the   │
│ /{status,connections,    │   │ system prompt on demand      │
│  blocked,trusted,alerts} │   │                              │
└──────────────────────────┘   └──────────────────────────────┘
              │                                    │
              └──────────────────┬─────────────────┘
                                 ▼
              ┌──────────────────────────────────────┐
              │ core/integrations/silentguard.py     │
              │ - per-user gate (silentguard_enabled)│
              │ - normalises connector output        │
              │ - returns dataclasses, not raw JSON  │
              └──────────────────────────────────────┘
                                 │
                                 ▼
              ┌──────────────────────────────────────┐
              │ core/integrations/silentguard/       │
              │   connector.py    (Protocol)         │
              │   file_connector.py (default impl)   │
              │   http_connector.py (stub, future)   │
              └──────────────────────────────────────┘
                                 │
                                 ▼
                    ┌─────────────────────────┐
                    │  SilentGuard on disk    │
                    │  ~/.silentguard_*.json  │
                    └─────────────────────────┘
```

### 5.2 The connector abstraction

The single new piece this design introduces is a **connector
interface**: a small `Protocol` describing the read-only operations
Nova needs from "something that knows the SilentGuard state." The
default implementation reads the JSON files. A future implementation
could speak HTTP to a hypothetical SilentGuard loopback API, or could
be a `FakeConnector` for tests, without any change above the
connector boundary.

The Protocol is intentionally tiny:

```python
# Sketch only. Not yet code in this PR. See §6 for the proposed file.
class SilentGuardConnector(Protocol):
    def get_status(self) -> ConnectorStatus: ...
    def get_connections(self, limit: int) -> list[ConnectionRecord]: ...
    def get_blocked_ips(self) -> list[IPRule]: ...
    def get_trusted_ips(self) -> list[IPRule]: ...
    def get_alerts(self, limit: int) -> list[AlertRecord]: ...
```

Five methods, one for each of the five resources Phase 1 surfaces.
Each returns a tame dataclass; none raise on the absent path.

### 5.3 Why a connector and not just "more file reads"

Three reasons, in order of weight:

1. **Future-proofing without speculation.** If SilentGuard *ever* gains
   a local HTTP API, Nova swaps connectors. If it never does, the
   `FileConnector` is the only one we ship and the abstraction has cost
   nothing — five methods on a Protocol.
2. **Testability.** `FakeConnector` lets the chat / web tests assert
   over a known data shape without writing JSON files to `tmp_path`
   for every case.
3. **Boundary clarity.** Today's `security_feed` mixes "open the file"
   and "summarise the events." The connector splits those: connectors
   produce typed records, and `silentguard.py` does the summarising.
   That separation is what lets Phase 1 add `/blocked` and `/trusted`
   without `security_feed` growing a second JSON-file-reading path.

The Protocol is not a framework. It is a one-file interface with one
default implementation. We resist the temptation to add base classes,
plugin loaders, or registry patterns; those are §12 problems at best.

### 5.4 Failure modes and what they map to

| Condition                                                       | `get_status` reports | Read methods return    |
| --------------------------------------------------------------- | -------------------- | ---------------------- |
| User has not opted in                                           | `disabled`           | `[]`                   |
| Opted in, no API URL set, no SilentGuard files on disk          | `not_configured`     | `[]`                   |
| Opted in, API URL set but probe fails (refused / timeout)       | `offline`            | `[]`                   |
| Opted in, file path expected but file missing                   | `offline`            | `[]`                   |
| Opted in, source reachable, file empty/malformed                | `connected`          | `[]` (logged at debug) |
| Opted in, source reachable, valid records                       | `connected`          | parsed records         |
| Opted in, file present, file too large (>5 MB)                  | `connected`          | `[]` (logged at warn)  |

Nothing in this column is an exception. The chat surface, the web
surface, and any future caller can rely on "this never throws on the
absence path."

### 5.5 Reachability and lifecycle

Reachability is a **separate concern** from the connector. The
connector answers "given that SilentGuard is here, what does it
say?". The reachability probe answers "is SilentGuard here at all,
right now?". Mixing them was tempting and we resisted it: the
connector stays pure, and lifecycle gets its own narrowly scoped
file.

A new module, `core/integrations/silentguard/lifecycle.py`, owns
exactly two operations:

- `probe_reachable(timeout: float) -> ReachabilityResult` — performs
  one `GET /status` against the configured loopback URL (or one
  `os.stat` against the file fallback) with a short timeout (default
  1.5 s) and returns the result as a calm dataclass. **No retry. No
  fallback re-probe in a loop.** The probe runs when the Settings
  card mounts, when the user clicks "Refresh," or as a single
  post-spawn check after `start_silentguard`. That is the entire
  trigger set.
- `start_silentguard(user_id: str) -> StartResult` — only callable
  when the user has explicitly configured `silentguard_start_command`
  in their settings *and* clicked the "Start SilentGuard" button.
  Spawns the configured command via `subprocess.Popen` with
  `shell=False`, `start_new_session=True`, no inherited stdin, and
  detached stdout/stderr piped to a small ring buffer. After the
  spawn it runs one post-start `probe_reachable` and returns a
  `StartResult` summarising whether the spawn succeeded *and*
  whether SilentGuard is now reachable. A spawn that "succeeds" but
  whose API never comes up is reported honestly, not papered over.

`lifecycle.py` is the only file in this integration permitted to
import `subprocess`. Every other file (`connector.py`,
`file_connector.py`, `http_connector.py`, `summaries.py`,
`silentguard.py`) keeps the existing forbidden-imports assertion.
That keeps the privilege of "spawning a process" pinned to a single
50-line file the reviewer can read end-to-end.

### 5.6 What lifecycle is not

- It is **not** a service supervisor. There is no restart policy,
  no health-check loop, no "if the probe fails three times, try
  again." A failed probe surfaces as `offline` and stays that way
  until the user takes action.
- It is **not** an installer. Nova does not deploy
  `silentguard.service`, write to `~/.config/systemd/user/`, or
  invoke `systemctl daemon-reload`. The "View setup instructions"
  link is text, not action.
- It is **not** a privileged helper. There is no companion daemon
  Nova ships, no `nova-helper` setuid binary, no DBus polkit prompt.
  If running SilentGuard requires root in your environment, the
  in-Nova start button is simply not the right tool — use your
  system's own service manager.

---

## 6. Module structure

### 6.1 Files (proposed)

```
core/
  security_feed.py                  (existing — keep, narrow over time)
  integrations/
    silentguard.py                  (existing — keep, becomes the gate +
                                     summariser + lifecycle facade)
    silentguard/                    (NEW package, Phase 1)
      __init__.py
      connector.py                  (Protocol + dataclasses)
      file_connector.py             (default impl, reads JSON files)
      http_connector.py             (loopback-only, read-only API client)
      lifecycle.py                  (reachability probe + opt-in start;
                                     ONLY file allowed to import subprocess)
      summaries.py                  (pure functions: events → summary,
                                     rules → counts, alerts heuristic)
```

The existing `core/integrations/silentguard.py` becomes a thin facade:
it picks the connector, applies the per-user gate, calls the
summariser, and returns the same `IntegrationStatus` shape it returns
today (so existing call sites keep working). New helpers
(`recent_connections`, `blocked_ips`, `trusted_ips`, `recent_alerts`)
are added on top — they do not replace `recent_events` /
`recent_events_summary`, which the chat layer already uses.

### 6.2 Why a sub-package and not a flat split

Today `core/integrations/silentguard.py` is one file. Phase 1 adds
maybe 200 lines of code across connector + dataclasses + parser. That
is the size at which a folder starts to be honest. The folder also
gives `http_connector.py` a natural home for whoever picks up Phase 2.

If the folder turns out to be premature, collapsing it back to one
file is a refactor, not a redesign.

### 6.3 What does *not* change

- `core/security_feed.py` keeps doing what it does today. The plan
  does not delete or rewrite it. Over time, parts of it may move into
  `silentguard/summaries.py`, but that is a *cleanup* later, not a
  Phase 1 deliverable.
- The chat path continues to call
  `silentguard_integration.recent_events_summary(...)`. The same
  function exists; it is just routed through the connector internally.
- The settings table, the `silentguard_enabled` key, and the
  `/integrations/status` endpoint keep their current shape.

Backwards compatibility is not a "future concern" here — the existing
chat flow uses these helpers in production-shaped tests today, and
breaking them is what Phase 1 must not do.

---

## 7. Data contract

The contract below is what Nova *expects* a connector to produce. It
intentionally normalises away SilentGuard's internal shape variations
so the rest of Nova does not have to care which version of
SilentGuard is on disk.

### 7.1 `ConnectorStatus`

```jsonc
{
  "state": "connected",               // "connected" | "offline" | "not_configured"
  "source": "http",                   // "file" | "http"
  "detail": "http://127.0.0.1:8765",  // path or loopback URL probed
  "version": "0.4.2",                 // SilentGuard self-reported version, if any
  "probed_at": "2026-05-08T14:19:02Z" // ISO-8601 of the most recent probe
}
```

`state` semantics (these are the *only* three values, by design):

- `connected` — the API is reachable (or the file is present and
  parseable for the file fallback). The Settings card shows
  *"SilentGuard connected in read-only mode."*
- `offline` — the integration is configured (an API URL is set, or
  the default file path applies on this OS) but the probe failed:
  the socket refused, the HTTP call timed out, or the file is
  missing. The Settings card shows *"SilentGuard is not running"*
  and offers the "Start SilentGuard" button **only if** a start
  command is configured.
- `not_configured` — neither an API URL nor a recognisable file path
  is configured for this user / host. The Settings card shows
  *"SilentGuard not configured"* with a "View setup instructions"
  link, and no Start button.

The per-user `enabled` flag is layered on top by `silentguard.py`,
yielding the user-facing `IntegrationStatus` shape with values
`disabled` / `connected` / `offline` / `not_configured`. The
connector itself does not know about users — that gate lives one
layer up.

### 7.2 `ConnectionRecord`

```jsonc
{
  "ip": "1.2.3.4",
  "process": "curl",
  "port": 443,                        // null if absent / out of range
  "trust": "Known",                   // "Known" | "Unknown" | "Local" | "Blocked"
  "timestamp": "2026-05-08T14:19:02Z" // ISO-8601 string, or null
}
```

A `ConnectionRecord` is the normalised form of one entry in the
SilentGuard memory file. Unknown trust values map to `Unknown`. Bad
ports map to `null`. Records that are not parseable at all are
dropped (not surfaced as errors).

### 7.3 `IPRule`

```jsonc
{
  "ip": "9.9.9.9",
  "label": "DNS",                     // user-supplied label, may be ""
  "added_at": "2026-04-30T10:00:00Z", // optional, may be null
  "source": "user"                    // "user" | "imported" | "unknown"
}
```

Used for both `/blocked` and `/trusted`. The `source` field is
forward-looking; today it is always `"user"` or `"unknown"` because
that is all SilentGuard records.

### 7.4 `AlertRecord`

An alert is **not** a separate file in SilentGuard — it is a
*derived* record Nova computes from the connection log. Phase 1
surfaces a small, deterministic alert set:

- An alert per `Unknown` IP with **≥5 distinct connections** in the
  current memory window.
- An alert per `Unknown` IP that has reached an **ephemeral or
  high-numbered port** (>49151).
- An alert per process that has connected to **≥3 distinct Unknown
  IPs** in the window.

```jsonc
{
  "kind": "repeated_unknown_ip",      // see "kinds" below
  "ip": "5.6.7.8",                    // when applicable
  "process": "rogue",                 // when applicable
  "count": 7,                         // observation count behind the alert
  "first_seen": "2026-05-08T13:50:00Z",
  "last_seen":  "2026-05-08T14:18:11Z",
  "summary": "5.6.7.8 connected to 7 times by `rogue`."
}
```

Alert `kind`s, fixed and small:

- `repeated_unknown_ip` — the ≥5 rule above.
- `high_port_unknown` — the >49151 rule above.
- `process_fans_out` — the ≥3 distinct-IP rule above.

Adding a kind is a follow-up issue, not an in-place change. Each new
kind carries its own threshold and its own test.

The alert heuristics are deliberately *boring*. Phase 1 is not the
place for ML-based anomaly detection. The numbers in the rules are
visible in the code, the user can read them, and they can be tuned
without retraining anything.

### 7.5 `ReachabilityResult`

```jsonc
{
  "reachable": false,
  "state": "offline",                 // matches ConnectorStatus.state
  "source": "http",
  "detail": "Connection refused on 127.0.0.1:8765",
  "elapsed_ms": 14,
  "probed_at": "2026-05-08T14:19:02Z"
}
```

Returned by `lifecycle.probe_reachable`. Always returns a value;
never raises. `detail` is a short human-readable string (no stack
trace, no full URL beyond the loopback host:port the user already
configured) and is safe to display in the Settings card verbatim.

### 7.6 `StartResult`

```jsonc
{
  "spawned": true,
  "post_probe_state": "connected",    // re-uses the ConnectorStatus state vocabulary
  "exit_code": null,                  // null while the process is alive
  "tail": ["listening on 127.0.0.1:8765"],
  "started_at": "2026-05-08T14:19:02Z",
  "summary": "SilentGuard started; API reachable."
}
```

Returned by `lifecycle.start_silentguard`. `tail` carries at most
the last 20 lines of the spawned process's stderr/stdout, so a
configuration error can be shown to the user without sending them to
syslog. `tail` entries are character-set sanitised
(`[A-Za-z0-9._:/ -]` + length cap per line) before being returned to
the UI, so a hostile log line cannot smuggle markup or escape
sequences into the Settings card.

A `StartResult` with `spawned: false` is the normal return when:

- The user has not configured a start command.
- The configured command failed validation (see §10.10).
- The current state is already `connected` (the call is a no-op).
- The spawn itself raised (binary missing, permission denied, etc.).

In every one of those cases the UI receives a calm `StartResult`
and surfaces the `summary`. `start_silentguard` itself never raises.

### 7.7 What the contract does not include

- No raw process command lines, environment variables, or arguments —
  even if SilentGuard later exposes them — until a separate review
  approves it. Process names are enough for the explanatory surface.
- No DNS resolution. Nova does not look up `5.6.7.8` to "enrich" it.
  An IP is an IP.
- No GeoIP lookups, no AS-org lookups, no third-party reputation
  feeds. All of those are remote calls and are explicitly out of
  scope.
- No user-visible IDs or hashes that could be used to correlate
  across machines. Each install is its own world.

---

## 8. Nova-side HTTP surface

The endpoints below are **Nova's own** HTTP surface — what the chat
UI calls in this user's browser. They are not endpoints SilentGuard
exposes (it has none). All endpoints require a logged-in user and
respect the per-user `silentguard_enabled` gate.

### 8.1 Endpoints

| Method | Path                                       | Returns                              |
| ------ | ------------------------------------------ | ------------------------------------ |
| GET    | `/integrations/silentguard/status`         | `IntegrationStatus` JSON             |
| GET    | `/integrations/silentguard/connections`    | `{items: ConnectionRecord[]}`        |
| GET    | `/integrations/silentguard/blocked`        | `{items: IPRule[]}`                  |
| GET    | `/integrations/silentguard/trusted`        | `{items: IPRule[]}`                  |
| GET    | `/integrations/silentguard/alerts`         | `{items: AlertRecord[]}`             |
| POST   | `/integrations/silentguard/start`          | `StartResult` (only when configured) |

(`/integrations/status` stays as the single-call snapshot for the
settings panel.)

### 8.2 Behaviour

- **Disabled user → empty list, 200 OK.** Returning 200 with `items:
  []` and a `state: "disabled"` field is calmer than a 403 here. The
  user has not done anything wrong; they have just not opted in.
- **Offline → empty list, 200 OK, `state: "offline"`.** The
  integration is enabled and configured, but SilentGuard is not
  reachable. The Settings card will surface "SilentGuard is not
  running" and (if configured) the Start button.
- **Not configured → empty list, 200 OK, `state: "not_configured"`.**
- **Limit query param.** `?limit=N` on `/connections` and `/alerts`,
  defaulting to 50, capped at e.g. 500. `/blocked` and `/trusted` do
  not paginate in Phase 1; the SilentGuard rules file is small.
- **No data-write endpoints.** `POST` / `PUT` / `DELETE` against any
  of the read paths returns 405. There is no
  `/integrations/silentguard/block` in Phase 1, by design.
  `POST /integrations/silentguard/start` is the only mutating
  endpoint, and it does not write to SilentGuard's data files
  (see §8.4).
- **No streaming, no websockets, no SSE.** A polled GET is enough for
  a panel that the user opens occasionally. A push channel would
  invite the "background tab pings forever" failure mode.

### 8.3 What the UI does with these

A modest first cut, mirroring the existing settings panel rather than
inventing a dashboard:

- The settings panel grows a "SilentGuard" tab visible when the
  integration is on. Tab body is four read-only lists: recent
  connections, blocked IPs, trusted IPs, recent alerts. Each list has
  a "explain this" affordance that drops a templated question into
  the chat composer (e.g. "Explain this connection: …"); the user
  presses send.
- The chat composer gains nothing new. Existing `is_security_query`
  heuristics keep deciding when to inject the summary into the
  prompt. All "explain" buttons just *seed* the composer.
- The chat answer keeps using the existing `SECURITY_SYSTEM_PROMPT`,
  optionally extended with the relevant slice (just the alerts, just
  the blocked rules, etc.) when the seeded message names one.

There is no "live tail," no auto-refresh on a timer, no notifications
out of the chat tab. The user opens the panel; the panel renders
once.

### 8.4 The `/start` endpoint

`POST /integrations/silentguard/start` is the **only** mutating
endpoint in the integration. Its mutation is exactly: spawn the
user's configured `silentguard_start_command`, wait for a short
post-spawn probe (default 2 s) to give the process time to bind its
socket, and return the `StartResult`. Gating, in order:

1. The request must be from an authenticated user.
2. That user's `silentguard_enabled` must be `true`.
3. That user's `silentguard_start_command` must be set and pass the
   validator in §10.10 (no `sudo` / `pkexec` / `doas` / `su`, no
   shell metacharacters, no firewall paths, argv list only).
4. The current `ConnectorStatus.state` must be `offline`. Calling
   `/start` when SilentGuard is already `connected` is a no-op that
   re-probes and returns the current status. Calling `/start` from
   `not_configured` returns a `StartResult` with `spawned: false`
   and an instructive `summary`.

If any gate fails, the endpoint returns **200 OK** with a
`StartResult` shape carrying `spawned: false` and a human-readable
`summary` ("Start command not configured", "Start command rejected:
contains sudo", etc.). It does not return 4xx on the absence path,
for the same reason the read endpoints don't: the user has not done
anything wrong.

There is **no** `/stop` endpoint, no `/restart`, no `/reload`.
Stopping SilentGuard is the user's responsibility via SilentGuard's
own UI or `systemctl --user stop silentguard.service`. Nova does
not offer to stop a security tool. There is also no auto-call to
`/start` on Nova boot, on session resume, on tab focus, or on any
other event — only the user clicking the button triggers it.

### 8.5 The Settings status card

The Settings card's contract with the user is exactly:

| `state`           | Headline                                  | Action                                            |
| ----------------- | ----------------------------------------- | ------------------------------------------------- |
| `disabled`        | "SilentGuard integration is off."         | Show the *Enable SilentGuard* button.             |
| `connected`       | "SilentGuard connected in read-only mode." | Refresh button + optional *Disable*. (Read-only is stated explicitly.) |
| `offline`         | "SilentGuard is not running."             | *Retry* button; *Disable* button; setup link otherwise. |
| `not_configured`  | "SilentGuard is not configured here."     | "View setup instructions" link.                   |

The card carries one line of subtext under each headline making the
read-only nature explicit (e.g. *"Nova reads from SilentGuard. Nova
does not enforce rules."*) so a user who scrolls into the card
cannot mistake it for a control surface.

### 8.6 The Enable / Disable / Retry endpoints

The Settings card's primary action is a state-driven button. Each
state has a dedicated, auth-gated endpoint so the UI never has to
guess what the next call should do, and so that an audit reading the
HTTP log can tell *exactly* why a particular request happened.

| Method | Path                                       | Effect                                                                                |
| ------ | ------------------------------------------ | ------------------------------------------------------------------------------------- |
| POST   | `/integrations/silentguard/enable`         | Persists per-user `silentguard_enabled = true`; runs the lifecycle helper once.       |
| POST   | `/integrations/silentguard/disable`        | Persists per-user `silentguard_enabled = false`. Does **not** stop the SilentGuard service. |
| POST   | `/integrations/silentguard/retry`          | Re-runs the lifecycle helper (probe, and — only when the operator opted into the safe `systemd-user` start mode — the same single `systemctl --user start <unit>` documented elsewhere). No setting is mutated. |

All three endpoints return the same payload shape as
`GET /integrations/silentguard/summary`:

```json
{
  "lifecycle": { "state": "...", "enabled": true,
                 "auto_start": false, "start_mode": "...",
                 "unit": "...", "message": "..." },
  "counts":    { "alerts": 0, "blocked": 0, "trusted": 0, "connections": 0 } | null,
  "host_enabled": true
}
```

So the UI can paint the new state in a single round-trip after the
user clicks Enable / Disable / Retry — no follow-up `/summary` call
is needed.

Safety contract (unchanged):

* Every endpoint requires an authenticated session.
* Only the per-user `silentguard_enabled` setting is mutated, and
  only by `/enable` and `/disable`. `/retry` mutates nothing.
* The lifecycle helper is the only code path allowed to spawn a
  process. It runs `systemctl --user start <validated-unit>` only
  when the host operator opted in via `NOVA_SILENTGUARD_AUTO_START`
  *and* `NOVA_SILENTGUARD_START_MODE=systemd-user`. No `sudo`, no
  `pkexec`, no firewall command, no shell interpretation, no command
  sourced from the request body.
* The endpoints take no body. The frontend sends none, and any body
  that is sent is silently ignored — there is nothing for an
  attacker to smuggle in.
* Disable does **not** stop the SilentGuard service. Stopping a
  security tool is the user's responsibility through SilentGuard's
  own UI or `systemctl --user stop <unit>`. Nova never offers that
  affordance.

The Settings card surfaces the state-driven actions like this:

| `state`           | Headline                                  | Primary action            | Secondary actions       |
| ----------------- | ----------------------------------------- | ------------------------- | ----------------------- |
| `disabled`        | "SilentGuard integration disabled."       | *Enable SilentGuard*      | —                       |
| `starting`        | "Starting SilentGuard…"                   | *Disable*                 | (button briefly busy)   |
| `connected`       | "SilentGuard connected in read-only mode." | *Disable*                 | *Refresh*               |
| `unavailable`     | "SilentGuard unavailable."                | *Disable*                 | *Retry*                 |
| `could_not_start` | "Could not start SilentGuard."            | *Disable*                 | *Retry*                 |

There is still no background polling. The fetch trigger set is
exactly: opening Settings, clicking Enable/Disable/Retry, clicking
Refresh. Nothing else.

---

## 9. Conversational shapes

The user-facing target is **calm explanation**, not alarm. Examples
below are illustrative; phrasing belongs in a follow-up issue with
review on tone.

### 9.1 "What is using the most network right now?"

→ Nova reads `/connections` (last N), groups by process, returns a
short prose answer:

> "In the last 50 observations, `firefox` accounts for 32 connections,
> mostly to known IPs (CDNs, your usual sites). `node` made 6
> connections, all `Local`. Nothing in the recent window is marked
> Unknown. Want the full list?"

If the integration is `disabled`, `offline`, or `not_configured`,
Nova says so plainly: *"SilentGuard isn't connected here, so I don't
have visibility into network activity."* No fabrication, and no
suggestion that Nova will try to bring it up on its own.

### 9.2 "Explain this suspicious connection."

Triggered by the panel's "explain" button, which seeds a message
naming the IP/process. Nova reads `/connections` and `/alerts`,
filters to the named record, and answers descriptively:

> "`rogue` reached `5.6.7.8` on port 4444 seven times in the recent
> window. SilentGuard flagged this as Unknown. Port 4444 is
> non-standard and not a port your other processes use. I can't tell
> you what `rogue` is doing — only what was observed. If you don't
> recognise it, the next step is to look at the process in
> SilentGuard's UI and decide whether to mark the IP."

Note what the answer does not do: it does not suggest a `kill`
command, a `pkill`, an `iptables` rule, or any other action. Nova
points the user back to SilentGuard for the action.

### 9.3 "Why was this IP blocked?"

Nova reads `/blocked`, looks up the IP, and reports what the rules
file says (label, when added, whether SilentGuard recorded a reason).
If the rule has no metadata beyond "blocked," Nova says so:

> "The rule was added by you (or by an earlier session), labelled
> `'spam'`, with no other notes. SilentGuard's rules file doesn't
> record why a block was created — just that one exists."

### 9.4 "Summarise recent suspicious activity."

Nova reads `/alerts`, groups by `kind`, returns the existing
deterministic summary plus a one-paragraph framing:

> "Two patterns stand out in the recent window: `5.6.7.8` was reached
> seven times by `rogue`, and `8.8.8.8` was reached on port 53234,
> which is unusual for it. Three other Unknown IPs appeared once each
> and don't currently rise to alert level."

### 9.5 "Show unknown outbound connections."

Nova reads `/connections` filtered to `trust=Unknown`. The list is
the answer; Nova adds a one-liner of context but does not pad the
reply with reassurance.

### 9.6 The off-switch

The user can ask "stop including SilentGuard data in my chats" and
Nova answers by linking to the settings toggle. Nova does **not**
flip the switch on the user's behalf, even though the API would let
it: settings changes belong to the user, not to the assistant.
(See [cognitive copilot roadmap §2.6](./cognitive-copilot-roadmap.md#26-explicit-confirmation-for-dangerous-actions).)

---

## 10. Security considerations

This integration reads from a file under the user's home directory.
That is a small surface, but it carries real privacy weight. The
constraints below are commitments.

### 10.1 No write paths to SilentGuard data

Phase 1 ships zero `os.open(..., 'w')` calls against any SilentGuard
file. The connector exposes no write methods. The HTTP surface
exposes no mutating verbs against SilentGuard's *data*
(`/connections`, `/blocked`, `/trusted`, `/alerts` are GET-only).

The forbidden-imports test continues to assert that
`silentguard.py`, `connector.py`, `file_connector.py`,
`http_connector.py`, and `summaries.py` do not import
`subprocess`, `socket` (beyond what `urllib` pulls in for HTTP),
`shutil`, `ctypes`, or `signal`. **The single, narrow exception** is
`lifecycle.py`, which is permitted to import `subprocess` and only
that — it is forbidden from importing `os.system`, `pty`, `ctypes`,
or `signal`, and the test enforces the import allowlist explicitly
(*"if a new import lands in lifecycle.py, this test must be updated
on purpose"*).

### 10.2 Path safety

The connector resolves the SilentGuard file paths in exactly two
ways:

- `NOVA_SILENTGUARD_PATH` env var (if set), expanded with `~`.
- The default `~/.silentguard_memory.json` /
  `~/.silentguard_rules.json`.

No path comes from user input. No path comes from the chat composer.
No path comes from a request body. The user cannot ask Nova to
"please read `/etc/shadow`."

### 10.3 Size and parse caps

- 5 MB hard cap on each JSON file (the existing
  `_MAX_FEED_BYTES`). Larger files are skipped, not partially parsed.
- Standard library `json.load` only. No streaming JSON parser, no
  YAML, no pickle, no custom deserialisers.
- Decode errors are logged at `debug`, never raised, and never
  surfaced into chat replies. A noisy log on disk is fine; a 500
  error blowing up a chat turn is not.

### 10.4 Per-user isolation

The per-user gate is not advisory. Helpers all take a `user_id`
first, look up `silentguard_enabled` for that user, and short-circuit
on `false`. The HTTP layer always passes the authenticated user's
id; there is no admin override that says "show me everyone's
SilentGuard data" — none of the endpoints accept a `?user_id=`
parameter, and adding one would be a regression against
[multi-user-architecture §4](./multi-user-architecture.md#4-cross-user-privacy).

### 10.5 No credentials or secrets in records

`ConnectionRecord` and friends only carry IP, port, process name, and
trust. No environment variables, no command-line arguments, no
filesystem paths beyond the process name SilentGuard already
records. If SilentGuard later starts recording richer fields, the
connector strips them at parse time rather than passing them
through. The
[cognitive-copilot roadmap §8.4](./cognitive-copilot-roadmap.md#84-no-secret--token-exposure)
deny-list applies in spirit: if a future field looks like a
credential, it does not enter Nova's pipeline.

### 10.6 No prompt-injection vector

The chat layer formats SilentGuard data through the existing
`format_security_summary` (counts, IPs, port numbers) — not by
splatting raw JSON into the prompt. Process names and IPs are
sanitised to a small character set before going into the prompt
(`[A-Za-z0-9._:/-]` and a length cap), so a hostile process named
`"\n\nIgnore previous instructions"` cannot redirect the model.

### 10.7 No background polling

The integration runs only when the user pulls. There is no scheduler.
There is no `learner.py`-like background loop. The user closing the
SilentGuard tab is the off-switch; nothing keeps reading.

### 10.8 Logging hygiene

`logger.debug` for absence-path messages; `logger.warning` for the
size-cap case; nothing at `info` or above on the happy path. Logs
never include the *contents* of the parsed file — only the path and
the failure mode. (A user pasting their log into a bug report
should not also be pasting their connection history.)

### 10.9 Family controls

The integration respects existing family controls. A restricted user
with `web_search_enabled=false` is not blocked from SilentGuard data
(the data is fully local; that flag is about external network
calls). However, the per-user `silentguard_enabled` switch is the
authoritative gate for restricted users too — an admin cannot flip
it on behalf of a household member.

The `/start` endpoint and the `silentguard_start_command` setting
inherit the same gating: a restricted user with
`silentguard_enabled=false` cannot trigger a spawn even if a start
command happens to be configured for them.

### 10.10 No privileged or firewall actions

Nova never invokes `iptables`, `nftables`, `firewalld`, `ufw`, `pf`,
or `ipfw`. Nova never invokes `sudo`, `pkexec`, `doas`, `su`, or
`runuser`. Nova never calls system-level `systemctl` (only,
optionally, `systemctl --user`, and only if that is exactly what the
user's configured start command literally is).

The `silentguard_start_command` validator enforces this in code
*before* any spawn:

- Argv must be a non-empty list of strings, not a single string. A
  string-form command is rejected outright (no shell interpretation
  by Python or by the OS).
- The first element is rejected if it is, or its basename is, one
  of: `sudo`, `pkexec`, `doas`, `su`, `runuser`, `setpriv`,
  `chroot`, `unshare`, `nsenter`. Aliases via path component
  (`/usr/bin/sudo`, etc.) are caught by basename comparison.
- Any element containing a shell metacharacter is rejected:
  `;`, `&`, `|`, `` ` ``, `$`, `<`, `>`, `(`, `)`, `{`, `}`,
  `\\`, `\n`, `\r`. Configured commands must be plain argv, not a
  shell pipeline. The validator does **not** try to "quote" or
  "escape" them; it rejects.
- Any element whose path resolves under a firewall configuration
  directory (`/etc/iptables`, `/etc/nftables`, `/etc/firewalld`,
  `/etc/ufw`, `/etc/pf*`) is rejected.
- The first element must be either an absolute path that exists and
  is executable by the Nova process user, **or** the literal
  invocation `systemctl --user <verb> <unit>` where `<verb>` is one
  of `start` / `restart` and `<unit>` matches `[a-z0-9._-]+\.service`.

A rejected command surfaces in the Settings card as
`state: "not_configured"` with a `detail` naming the rule that
failed. Rejection is never silent.

### 10.11 Reachability probe triggers, exhaustively listed

The reachability probe runs:

- Once when the Settings card mounts.
- Once when the user clicks "Refresh" on the card.
- Once after a `POST /start` call, ~2 s later, to give the spawned
  process time to bind its socket.

That is the full list. There is no timer. There is no
`setInterval`. There is no server-side scheduler. There is no
"check every minute" in the chat process. The card displays a
*"checked N seconds ago"* label rather than re-probing on its own;
if the user wants a fresh probe they click Refresh.

### 10.12 No cloud calls and no telemetry

The lifecycle module performs exactly one outbound network call: an
HTTP `GET` against `127.0.0.1` (or `::1`) on the user-configured
port. The validator rejects any `NOVA_SILENTGUARD_API_URL` whose
host is not a loopback address. There is **no** usage reporting, no
error reporting, no "anonymous start success rate" counter, no
Sentry, no analytics, no crash reporter. A failed start stays on
the user's machine.

### 10.13 Graceful fallback if start fails

A failed start surfaces as a `StartResult` with `spawned: false`
(when validation, exec, or fork fails) or `spawned: true` paired
with a post-probe `state: "offline"` (when the process started but
the API never came up). The Settings card shows the failure mode
and the captured `tail`. Nova continues to function: the chat layer
keeps using the file fallback if it is available, or simply tells
the user "SilentGuard isn't connected here" if it is not. A failed
start never raises into the chat path, never disables the
integration toggle, never blocks unrelated requests, and never
auto-retries. The user retries by clicking the button again.

---

## 11. Phase 1 scope (and what is *not* in it)

### 11.1 In scope

- The user-facing **Enable / Disable / Retry flow in Nova Settings**
  (see §8.6). The Settings card carries a calm, state-driven primary
  action button — *Enable SilentGuard* when the user hasn't opted in,
  *Disable* once they have — plus an optional *Retry* button when the
  integration is enabled but unreachable. Each button posts to a
  dedicated, auth-gated endpoint (`/enable`, `/disable`, `/retry`)
  that returns the same `{lifecycle, counts, host_enabled}` payload
  the existing `/summary` endpoint serves, so the UI repaints in a
  single round-trip. Persistence rides the existing per-user
  `silentguard_enabled` setting in `user_settings`, so the toggle
  survives restarts. Disable does **not** stop the SilentGuard
  service — Nova still does not offer a stop affordance for an
  external security tool.
- The `SilentGuardConnector` Protocol and dataclasses.
- A `FileConnector` reading both `~/.silentguard_memory.json` and
  `~/.silentguard_rules.json`.
- An `HttpConnector` that performs read-only `GET`s against the
  loopback API (`/status`, `/connections`, `/blocked`, `/trusted`,
  `/alerts`) when `NOVA_SILENTGUARD_API_URL` is set.
- `silentguard.py` facade additions: `recent_connections`,
  `blocked_ips`, `trusted_ips`, `recent_alerts`. Existing helpers
  preserved.
- The "Enable SilentGuard integration" per-user setting
  (`silentguard_enabled`, default `false`) — already exists; Phase 1
  re-affirms it as the single authoritative gate.
- A new per-user setting `silentguard_start_command` (argv list,
  optional, default empty). When empty, the Start button is absent.
- The lifecycle module: `probe_reachable` and `start_silentguard`,
  with the validator described in §10.10.
- The six Nova-side HTTP endpoints listed in §8 (five GETs plus
  `POST /start`).
- A Settings status card with the four states described in §8.5,
  the read-only subtext, the optional Start button, and the "View
  setup instructions" link. The existing read-only data tab (four
  lists with "explain" buttons) is added under the same panel.
- The **confirmation-gated mitigation flow** described in §2.4.quater:
  three new endpoints
  (`GET /integrations/silentguard/mitigation` and the two
  acknowledged POSTs), three new provider methods
  (`get_mitigation_state`, `enable_temporary_mitigation`,
  `disable_mitigation`), a new POST-capable client module
  (`core/security/silentguard_mitigation.py`) — with the read-only
  client kept POST-free so its forbidden-verb assertion still holds —
  and a Settings card mitigation block whose default copy is calm,
  whose actions require explicit confirmation, and which surfaces no
  notifications. Detection-only remains the default: Nova never
  enables temporary mitigation without an explicit user click +
  confirm.
- Test coverage:
  - Setting disabled means Nova issues *zero* calls to SilentGuard
    (asserted with a fake connector that records call counts and a
    fake lifecycle that records spawn attempts).
  - Setting enabled triggers exactly one probe per card mount and
    per refresh click — no extra background calls.
  - Missing SilentGuard surfaces `offline` calmly through both file
    and HTTP transports.
  - Start action is hidden when `silentguard_start_command` is
    unset; the `/start` endpoint returns `spawned: false` for any
    request that bypasses the UI gating.
  - Start command validator rejects each forbidden shape (`sudo`,
    pipe, firewall path, string-form command, missing binary,
    system-level `systemctl`).
  - Failed start does not crash Nova: the chat path, the read
    endpoints, and the rest of the Settings page keep working.
  - Existing chat / prompt behaviour (`is_security_query` injection,
    `SECURITY_SYSTEM_PROMPT`) is byte-identical when the
    integration is disabled.
  - Forbidden-imports test grows to cover all new files in the
    `silentguard/` sub-package, with `lifecycle.py` allowlisted
    only for `subprocess`.

### 11.2 Explicitly *not* in Phase 1

- Any write to SilentGuard's files. (No "block this IP" button.)
- Any privileged subprocess (`sudo`, `pkexec`, `doas`, `su`,
  `runuser`), or any subprocess against `iptables` / `nftables` /
  `firewalld` / `ufw`. The lifecycle module runs *one* configured
  non-privileged user-level command on explicit user click, and
  only that.
- Any system-level (non-`--user`) `systemctl` invocation.
- Any "auto-start on Nova boot" behaviour. Nova does not start
  SilentGuard at process startup, on session resume, on tab focus,
  or on any event other than the user pressing the Start button.
- Any retry loop after a failed start. The user retries by clicking
  the button again.
- Any `/stop`, `/restart`, `/reload` endpoint or button.
- Any background polling, watcher, inotify, or scheduler. The probe
  triggers in §10.11 are the complete list.
- Any modification of `~/.config/systemd/user/*.service` units, or
  any other system or user unit file. Nova explains; Nova does not
  install.
- Any cross-user / admin "see other users' SilentGuard data or
  start commands" surface.
- Any GeoIP, reputation lookup, or other remote enrichment.
- Any LLM-driven anomaly classification. The alert kinds in §7.4
  are the entire heuristic set.
- Any push notifications, toast popups, or "you have alerts" badges
  outside the integration's tab.
- Any cloud calls or telemetry. The probe is loopback-only.

---

## 12. Future phases (sketches only)

Phasing is a suggestion. Each phase is independently shippable and
independently abandonable. None of these are committed.

### Phase 2 — HTTP connector (only if SilentGuard ships an API)

If — *and only if* — SilentGuard adds a loopback-only, read-only
JSON API on the SilentGuard side, Nova ships an `HttpConnector` that
hits it. Same Protocol, same dataclasses, same callers. The benefits
would be: live data without re-parsing the file, version negotiation
via a `/version` endpoint, and explicit ETags for caching.

This phase requires a corresponding SilentGuard PR. It does not
start until that PR exists and has its own design review on the
SilentGuard side. Nova does not lobby SilentGuard to ship this; it
is simply ready if it does.

### Phase 3 — Confirmed actions (deliberately further out)

A *long* way down the road, and only after Phase 6 of the cognitive
copilot roadmap has shipped (per-action confirmation UX), Nova might
gain a "draft a SilentGuard rule" surface where:

- The user clicks "block this IP" in the Nova panel.
- Nova produces a *draft* rule and shows it.
- The user explicitly confirms.
- Nova writes through SilentGuard's documented write path (file or
  API), or — more likely — opens SilentGuard's UI focused on the
  rule editor, and the user finishes the action there.

This is included only to anchor the long-term boundary: even in the
write phase, Nova does not act without a per-action confirmation,
and SilentGuard remains the system of record for rules.

### Phase 4 — Cross-tool context (very far out)

A future where Nova's project anchors (cognitive-copilot roadmap §3)
can be linked to SilentGuard observations: *"this connection was
made while you were working on `nova`."* The link is an inferred
timestamp join, not a new field in SilentGuard. This is interesting
and probably useful, but not before the project-anchor feature
exists.

---

## 13. Recommended implementation order

This is the sequence in which an implementation PR series would land,
if Phase 1 starts. Each step is intended to be a small PR, mergeable
on its own.

1. **Doc and Protocol stub.** This document plus a
   `core/integrations/silentguard/connector.py` containing only the
   `Protocol` and the dataclasses (`ConnectorStatus`,
   `ConnectionRecord`, `IPRule`, `AlertRecord`). No parser, no
   facade changes. Tests assert the Protocol shape via static checks.
2. **`FileConnector` for connections.** Move (or wrap) the existing
   `security_feed.get_recent_security_events` parsing into
   `FileConnector.get_connections`. Behaviour preserved: existing
   tests pass unchanged; new test asserts the connector returns
   `ConnectionRecord` instances.
3. **`FileConnector` for rules.** Add `get_blocked_ips` /
   `get_trusted_ips`, reading `~/.silentguard_rules.json`. Tolerant
   of both list-of-strings and list-of-dicts. Tests cover empty file,
   missing file, malformed file, oversize file.
4. **Alerts derivation.** Add `get_alerts` with the three kinds in
   §7.4. Pure function over `get_connections` output, fully tested
   with fixtures.
5. **Facade additions.** Extend
   `core/integrations/silentguard.py` with `recent_connections`,
   `blocked_ips`, `trusted_ips`, `recent_alerts`. Each respects the
   per-user gate. The existing `recent_events` /
   `recent_events_summary` helpers stay; they are now thin wrappers
   over the new connector, but their public signature does not
   change. The chat path is untouched.
6. **HTTP endpoints.** Add the four new GETs in §8. Each returns
   `{items: [...], state: "..."}`. Existing
   `/integrations/status` is untouched.
7. **Settings panel UI.** Add the SilentGuard tab. Read-only. Four
   lists. Four "explain" buttons that seed the composer.
8. **Prompt sanitiser.** Tighten the formatter that builds the
   prompt-injected summary so process names and IPs are
   character-set capped before they reach the model.
9. **AST assertions.** Extend the existing forbidden-imports test in
   `test_integrations_silentguard.py` to cover all new files in the
   `silentguard/` sub-package.
10. **HTTP connector.** Add `http_connector.py` (loopback-only,
    `GET`-only, fixed path list) and route the facade through it
    when `NOVA_SILENTGUARD_API_URL` is set. Same dataclasses, same
    callers; the file connector remains the default.
11. **Reachability probe.** Add `lifecycle.py` with
    `probe_reachable` only. Update the facade and
    `/integrations/silentguard/status` to return
    `state ∈ {connected, offline, not_configured}` based on the
    probe result. The Settings card grows the four-state surface
    described in §8.5.
12. **Start command validator.** Add the argv validator in §10.10
    as a pure function in `lifecycle.py`. Test every rejection
    case explicitly: `sudo`, `pkexec`, `doas`, pipe / redirect /
    `$(...)`, string-form command, firewall path, system-level
    `systemctl`, missing binary. Validator success returns the
    normalised argv list; everything else returns a typed reason.
13. **Start action and button.** Add `start_silentguard` to
    `lifecycle.py` and `POST /integrations/silentguard/start`. The
    Settings card grows the *Start SilentGuard* button, visible
    **only** when `silentguard_start_command` is configured *and*
    the current state is `offline`. Failed starts surface the
    captured `tail`. The forbidden-imports test now allowlists
    `subprocess` for `lifecycle.py` only — and asserts that no
    other file in the package imports it.
14. **No-polling assertion.** Add a UI test asserting that the
    SilentGuard settings panel registers no `setInterval`,
    `setTimeout`-loop, or `requestAnimationFrame` polling, and a
    server-side test asserting that `lifecycle.probe_reachable` is
    never called from a thread, scheduler, or background task.
15. **Disabled-means-silent test.** A regression test that, with
    `silentguard_enabled=false`, exercises every public Nova path
    (chat turn, settings page load, `/integrations/status` poll,
    explicit hits against every `/integrations/silentguard/*`
    endpoint) and asserts the connector and lifecycle layers
    receive *zero* calls.

A reasonable ratio: each step ≤ ~150 net lines of new code, each
with focused tests, none of them touching `core/chat.py` or the
prompt structure of an existing reply path.

---

## 14. Non-goals, restated

To save reviewers a scroll: this integration does not propose any of
the following, and proposals that require them should be rejected.

- Nova writing to SilentGuard's files.
- Nova running `iptables` / `nftables` / `firewalld` / `ufw` / `pf`,
  or any system-level `systemctl`, or any privileged subprocess
  (`sudo` / `pkexec` / `doas` / `su` / `runuser`), under any setting,
  ever. The lifecycle module's narrow exception runs *one* configured
  non-privileged user-level command on explicit user click, validated
  against §10.10, and only that.
- Nova auto-starting SilentGuard. Not on Nova boot, not on session
  resume, not on tab focus, not on `/integrations/status` poll, not
  on any event other than the user pressing the *Start SilentGuard*
  button. Auto-start is the whole thing this design is set up to
  prevent.
- Nova retrying a failed start in the background. A failed start
  shows the failure; the user retries by clicking again.
- Nova modifying firewall rules, network routes, DNS settings,
  `/etc/hosts`, or any other host configuration as a side effect of
  any setting toggle in this integration.
- Nova installing or modifying systemd unit files. Setup
  instructions are *text*. The user runs `systemctl --user enable`
  themselves.
- A background polling loop that reads SilentGuard data without the
  user asking. The probe triggers in §10.11 are exhaustive.
- Any remote API call (GeoIP, reputation, threat-intel feeds, crash
  reporters, telemetry) for "enrichment" or for monitoring this
  feature's adoption. Loopback only.
- Any cross-user visibility into SilentGuard data or start commands,
  including for admins.
- An LLM-driven anomaly engine. The alert kinds are explicit and
  numeric.
- An on-by-default integration. The per-user toggle stays the gate
  and the default stays `false`.
- A new permission tier in `family_controls` specifically for
  SilentGuard. The existing per-user switch is the entire
  authorisation surface.
- Nova becoming the firewall engine. The mitigation surface in
  §2.4.quater **only** asks SilentGuard, after the user has
  explicitly confirmed, to enable or disable temporary mitigation.
  Nova does not block IPs directly, does not run firewall commands,
  does not auto-block, does not silently enable mitigation, and does
  not keep mitigation enabled when the user has not asked for it.
  The SilentGuard `POST /blocked/{ip}/unblock` path is intentionally
  out of scope for this PR; even when added, it would only land
  behind the same explicit-confirmation contract as
  `/mitigation/enable-temporary` and would never be invoked
  autonomously.

If a PR description starts with "to enable this we just need to
relax constraint X from §10," the answer is no.

---

## Appendix A — Mapping to existing modules

| Plan section            | Existing code that's already adjacent                          |
| ----------------------- | -------------------------------------------------------------- |
| §2 What exists today    | `core/security_feed.py`, `core/integrations/silentguard.py`    |
| §5 Architecture         | `core/integrations/__init__.py` (gating pattern)               |
| §7 Data contract        | `core/security_feed.py::SecurityEvent` (becomes one of many)   |
| §8 HTTP surface         | `web.py::integrations_status` (the pattern, not the data)      |
| §9 Conversational shapes| `core/chat.py::SECURITY_SYSTEM_PROMPT`                         |
| §10 Security            | tests/test_integrations_silentguard.py (forbidden-imports test)|

Most of the work in this plan is **building on what's already
there**, not greenfield. That is intentional: the privacy posture
demands it, and the existing read-only parser is the correct
foundation to extend rather than replace.

## Appendix B — Open questions

The following questions are deliberately left open for the
implementation PRs to resolve, not for this design doc:

- **Rule file shape variance.** The `silentguard_rules.json` shape
  has been observed as both list-of-strings and list-of-dicts.
  `FileConnector` should tolerate both; the parser's exact branching
  is an implementation detail, not an architectural choice.
- **Alert thresholds.** The numbers in §7.4 (≥5, >49151, ≥3) are
  starting values. Tuning them is an issue, not a redesign.
- **Settings-tab affordances.** Whether "explain" seeds the composer
  with French or English depends on the user's detected language;
  the existing tone helpers in `core/nova_contract.py` already cover
  this and should be reused.
- **i18n of the security prompt.** The current
  `SECURITY_SYSTEM_PROMPT` is French; whether to keep it French and
  let the model code-switch (current behaviour) or split it into
  per-language variants is a follow-up, not a blocker.

These are listed here so they do not surprise a reviewer, not as
items this document is committing to resolve.
