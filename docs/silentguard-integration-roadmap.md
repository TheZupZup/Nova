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
> loops. Nova is the cognitive layer; SilentGuard remains the
> security/network layer. Any proposal that blurs that line should be
> rejected.

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

Phase 1 cashes that vision in for a small, honest set of capabilities:
read SilentGuard's existing on-disk state, expose it as a calm read-only
slice in Nova's UI and chat, and stop there.

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
- `status(user_id)` returns `disabled` / `connected` / `not_found`.
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

### 2.5 What is missing today

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

Phase 1 is exactly the set of changes that closes those four gaps.

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

### 3.3 No local HTTP API

SilentGuard does **not** expose a local REST server. That is fine for
Phase 1: the file-based contract is enough, and not requiring
SilentGuard to ship a server keeps the two projects independent.

A future phase *may* propose a tiny loopback-only read API on the
SilentGuard side (see §12), but Nova's design must work without it.

---

## 4. Principles

These restate Nova's existing principles in the SilentGuard context.

### 4.1 Local-first

Every byte read or written by this integration stays on the user's
host. No outbound calls. No telemetry. No "anonymous usage" reporting.
If SilentGuard is on this machine, Nova talks to it; if not, the
integration reports `not_found` and Nova continues working.

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

| Condition                                         | `get_status` reports | Read methods return |
| ------------------------------------------------- | -------------------- | ------------------- |
| User has not opted in                             | `disabled`           | `[]`                |
| Opted in, but no SilentGuard files on disk        | `not_found`          | `[]`                |
| Opted in, file present, file empty/malformed      | `connected`          | `[]` (logged at debug) |
| Opted in, file present, valid                     | `connected`          | parsed records      |
| Opted in, file too large (>5 MB)                  | `connected`          | `[]` (logged at warn)  |

Nothing in this column is an exception. The chat surface, the web
surface, and any future caller can rely on "this never throws on the
absence path."

---

## 6. Module structure

### 6.1 Files (proposed)

```
core/
  security_feed.py                  (existing — keep, narrow over time)
  integrations/
    silentguard.py                  (existing — keep, becomes the gate +
                                     summariser facade)
    silentguard/                    (NEW package, Phase 1)
      __init__.py
      connector.py                  (Protocol + dataclasses)
      file_connector.py             (default impl, reads JSON files)
      summaries.py                  (pure functions: events → summary,
                                     rules → counts, alerts heuristic)
      # http_connector.py           (NOT in Phase 1; placeholder reserved)
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
  "available": true,                  // file readable / API reachable
  "source": "file",                   // "file" | "http" (future)
  "detail": "/home/user/.silentguard_memory.json",
  "version": null                     // SilentGuard self-reported version, if any
}
```

`available=false` means "the underlying tool is not present here."
The per-user `enabled` flag is layered on top by `silentguard.py`,
yielding the existing `IntegrationStatus` shape (`disabled` /
`connected` / `not_found`). The connector itself does not know about
users — that gate lives one layer up.

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

### 7.5 What the contract does not include

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

| Method | Path                                       | Returns                            |
| ------ | ------------------------------------------ | ---------------------------------- |
| GET    | `/integrations/silentguard/status`         | `IntegrationStatus` JSON           |
| GET    | `/integrations/silentguard/connections`    | `{items: ConnectionRecord[]}`      |
| GET    | `/integrations/silentguard/blocked`        | `{items: IPRule[]}`                |
| GET    | `/integrations/silentguard/trusted`        | `{items: IPRule[]}`                |
| GET    | `/integrations/silentguard/alerts`         | `{items: AlertRecord[]}`           |

(`/integrations/status` stays as the single-call snapshot for the
settings panel.)

### 8.2 Behaviour

- **Disabled user → empty list, 200 OK.** Returning 200 with `items:
  []` and a `state: "disabled"` field is calmer than a 403 here. The
  user has not done anything wrong; they have just not opted in.
- **Not found → empty list, 200 OK, `state: "not_found"`.**
- **Limit query param.** `?limit=N` on `/connections` and `/alerts`,
  defaulting to 50, capped at e.g. 500. `/blocked` and `/trusted` do
  not paginate in Phase 1; the SilentGuard rules file is small.
- **No write endpoints.** `POST`/`PUT`/`DELETE` against any of these
  paths returns 405. There is no `/integrations/silentguard/block`
  in Phase 1, by design.
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

If the integration is `disabled` or `not_found`, Nova says so
plainly: *"SilentGuard isn't connected here, so I don't have visibility
into network activity."* No fabrication.

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

### 10.1 No write paths

Phase 1 ships zero `os.open(..., 'w')` calls against any SilentGuard
file. The connector exposes no write methods. The HTTP surface
exposes no mutating verbs. The test suite continues to assert that
`silentguard.py` does not import `subprocess`, `socket`, `shutil`,
`ctypes`, or `signal`, and the new connector files inherit the same
assertion.

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

---

## 11. Phase 1 scope (and what is *not* in it)

### 11.1 In scope

- The `SilentGuardConnector` Protocol and dataclasses.
- A `FileConnector` reading both `~/.silentguard_memory.json` and
  `~/.silentguard_rules.json`.
- `silentguard.py` facade additions: `recent_connections`,
  `blocked_ips`, `trusted_ips`, `recent_alerts`. Existing helpers
  preserved.
- The five Nova-side HTTP endpoints listed in §8.
- A settings-panel "SilentGuard" tab, read-only, four lists, four
  "explain" buttons that seed the chat composer.
- Test coverage: connector parses both files, gate honours the user
  switch, HTTP endpoints return correct shapes for
  disabled/not-found/connected, prompt-injection sanitisation works,
  the read-only ast assertion grows to cover the new files.

### 11.2 Explicitly *not* in Phase 1

- Any write to SilentGuard's files. (No "block this IP" button.)
- Any subprocess execution against SilentGuard or `iptables` /
  `nftables` / `firewalld`.
- Any background polling, watcher, inotify, or scheduler.
- Any HTTP client to SilentGuard. (SilentGuard has no server.)
- Any cross-user / admin "see other users' SilentGuard data" surface.
- Any GeoIP, reputation lookup, or other remote enrichment.
- Any LLM-driven anomaly classification. The alert kinds in §7.4 are
  the entire heuristic set.
- Any push notifications, toast popups, or "you have alerts" badges
  outside the integration's tab.

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

A reasonable ratio: each step ≤ ~150 net lines of new code, each
with focused tests, none of them touching `core/chat.py` or the
prompt structure of an existing reply path.

---

## 14. Non-goals, restated

To save reviewers a scroll: this integration does not propose any of
the following, and proposals that require them should be rejected.

- Nova writing to SilentGuard's files.
- Nova running `iptables` / `nftables` / `firewalld` / `systemctl` /
  any subprocess against the host.
- A background polling loop that reads SilentGuard data without the
  user asking.
- Any remote API call (GeoIP, reputation, threat-intel feeds) for
  "enrichment."
- Any cross-user visibility into SilentGuard data, including for
  admins.
- An LLM-driven anomaly engine. The alert kinds are explicit and
  numeric.
- An on-by-default integration. The per-user toggle stays the gate
  and the default stays `false`.
- A new permission tier in `family_controls` specifically for
  SilentGuard. The existing per-user switch is the entire
  authorisation surface.

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
