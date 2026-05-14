# Nova

A local-first, self-hostable AI assistant built on FastAPI and Ollama.

## What Nova is

Nova is a personal AI assistant designed to run entirely on hardware you
control. It routes each conversation to the most appropriate local model,
maintains a persistent SQLite memory across sessions, and serves a calm
web interface reachable from any browser on your network. There is no
cloud account, no telemetry, and no required external service.

Nova is built around four ideas:

- **Local-first.** Inference, memory, and audio synthesis all run on
  your machine. Outbound calls are limited to clearly-scoped optional
  tools (weather, web search) and only when the user triggers them.
- **User control.** Optional integrations are off by default and
  per-user. Nova never auto-installs binaries, never escalates
  privilege, and never performs sensitive actions without a visible,
  explicit confirmation.
- **Modular.** Memory, voice, security context, and remote integrations
  sit behind small abstractions. None of them is required for Nova to
  work; each can be replaced or left disabled.
- **Privacy-focused.** Conversation history and user-authored memories
  stay in a local SQLite file under your account. No cloud sync, no
  third-party analytics.

Nova is under active development. Most of what is described in this
README ships today; the **Development status** section calls out what
is still design work or experimental.

## Key features

Shipped today:

- **Multi-model routing.** A lightweight classifier (`gemma3:1b`)
  decides which local model handles each request: general chat,
  code-focused, or advanced reasoning. The router falls back cleanly
  when a model is missing.
- **Streaming replies.** Assistant messages stream into the UI as they
  are generated, with a calm typing indicator while Nova is thinking.
  The browser coalesces incoming tokens on a short flush window
  (~28 ms) so text reveals smoothly instead of jittering on every
  one-character token, and the final Markdown is rendered in a single
  pass once the stream completes. Backend errors surface inline rather
  than leaving an empty bubble.
- **Persistent memory.** A local SQLite database stores conversations,
  user-authored memories, and per-user settings. Manual commands
  (`Retiens ça:`, `Souviens-toi:`) let users save explicit facts;
  automatic extraction adds short, low-confidence facts from chat.
- **Session continuity.** A small, deterministic "continue where we
  left off" summary surfaces recent conversation topics on return.
  Derived from data already in the sidebar, dismissable, never
  emotional or inferential.
- **Web interface.** A futuristic but quiet web UI with conversation
  sidebar, mode selector (Auto / Chat / Code / Deep), copy buttons,
  read-aloud, and a settings panel for memories, model preferences,
  personalization, and optional integrations.
- **Per-user accounts and family controls.** JWT-secured login,
  admin-managed user list, per-user settings, and an optional
  family-controls layer for restricted roles.
- **Personalization preferences.** Response length, warmth,
  enthusiasm, emoji density (none / low / medium / expressive), and
  free-text custom instructions are stored per user and shape Nova's
  tone without leaking into other accounts. Technical / code / PR /
  security replies stay sober regardless of the emoji level — the
  preference shapes casual chat only and never overrides the Nova
  Safety and Trust Contract.
- **Local response feedback.** Thumbs up / thumbs down under each
  assistant message records a per-user preference signal in the local
  SQLite database; a thumbs-down accepts an optional short reason such
  as *"focus on this project, not generic advice"*. The data feeds a
  short, deterministic preference block in future system prompts — it
  never leaves the host, never triggers model training, and cannot
  override the identity contract or the Nova Safety and Trust Contract.
  See [docs/nova-safety-and-trust-contract.md](docs/nova-safety-and-trust-contract.md)
  for the boundaries this layer sits inside.
- **Edit and delete sent messages.** Every chat message can be edited
  (user messages) or deleted (user and assistant messages) from the
  chat UI. Deleting a user message can optionally remove the paired
  assistant reply in the same exchange. Editing rewrites the message
  in place; it does not regenerate Nova's reply — re-send the edited
  text as a new message if you want a fresh answer. Memory cleanup is
  intentionally a separate, explicit action: editing or deleting a
  chat message never erases memories that were already extracted from
  it. Endpoints: `PUT /messages/{id}` and `DELETE /messages/{id}`,
  both auth-gated and scoped to the caller's conversations.
- **Voice / read-aloud.** Every assistant message has a "Read aloud"
  button. The default engine is the browser's local
  `speechSynthesis` API; an optional local [Piper](https://github.com/rhasspy/piper)
  neural voice is available for users who want richer audio.
- **Optional weather and web search.** Open-Meteo (no API key) and
  DuckDuckGo, both opt-in and triggered explicitly by the user.
- **Optional SilentGuard read-only integration.** See the
  [SilentGuard integration](#silentguard-integration) section.
- **Optional background RSS learning.** Off by default; opt in via
  `NOVA_AUTO_WEB_LEARNING=true`.
- **Login rate limiting.** Per-IP sliding-window limiter on the login
  endpoint, configurable via environment variables.
- **Identity contract.** Nova presents itself as a named assistant and
  does not reveal the underlying model name unless asked a technical
  implementation question.
- **AMD GPU acceleration via ROCm.** Falls back to CPU automatically.
- **Systemd and Docker deployment.** A hardened systemd unit and a
  `docker-compose.yml` ship with the repo.

Experimental / partial:

- Natural-language memory store and retriever (`memory/`). The pipeline
  is present and used in some paths, but not yet validated for
  production use.

## Architecture overview

```
web.py                FastAPI application, REST and SSE endpoints
main.py               Terminal interface (no web server)
config.py             Central configuration loaded from .env

core/
  router.py           Model selection via gemma3:1b classifier
  chat.py             Conversation logic, streaming, system prompt
  memory.py           SQLite memory: facts, conversations, settings
  memory_command.py   Manual memory command parser
  memory_importer.py  Local-only Markdown memory pack importer
  nova_contract.py    Nova identity + personalization prompt blocks
  feedback.py         Local response feedback (thumbs up/down) → preference block
  identity.py         Identity contract constant
  auth.py             JWT creation and verification
  github_oauth.py     Optional GitHub OAuth gate (alpha channel)
  rate_limiter.py     Per-IP sliding-window login rate limiter
  users.py            Users table, default-admin migration
  policies.py         Role-based family controls
  settings.py         System and per-user settings storage
  session_continuity.py  Deterministic "continue where we left off"
  learner.py          Background RSS ingestion (opt-in)
  weather.py          Open-Meteo integration
  search.py           DuckDuckGo integration
  time_context.py     Calendar/timezone context for prompts
  updater.py          Model version management
  ollama_client.py    Thin Ollama HTTP client
  local_models.py     Local model discovery / readiness
  model_registry.py   Allow-list of installable models
  model_access.py     Per-user model access checks
  model_pulls.py      Background model pull progress
  security_feed.py    Read-only SilentGuard JSON parser
  security/           Read-only security provider package
  integrations/       Per-user gates for optional integrations
    media/            Local-first media bridges (Jellyfin, read-only)
  voice/              TTS provider abstraction (browser + Piper)

memory/
  store.py            Natural-language memory store
  retriever.py        Semantic memory retrieval
  extractor.py        Memory extraction pipeline
  schema.py           Memory data schema
  policy.py           Retention and cleanup policy
  embeddings.py       Embedding helper

static/
  index.html          Web interface

deploy/systemd/       Hardened nova.service + walkthrough
deploy/docker/        Portable-workspace docker-compose example
docker/               Docker entrypoint
docs/                 Roadmaps and deployment guides
scripts/              Operator helper scripts
tests/                Pytest test suite
```

## Security and local-first philosophy

Nova is a **powerful local service**, not a trusted root-level agent.
The deployment guide in [docs/secure-deployment.md](docs/secure-deployment.md)
covers recommended setups, VPN / Zero-Trust gateways, least privilege,
backups, and the systemd hardening that ships in
[deploy/systemd/nova.service](deploy/systemd/nova.service).

The broader safety boundaries — human-control rules, honesty
requirements, defensive-security-only posture, prompt-injection
resistance, and the strict limits any future quarantine or honeypot
feature must respect — live in
[docs/nova-safety-and-trust-contract.md](docs/nova-safety-and-trust-contract.md).
That document is the checklist new features are reviewed against; a
PR that would violate it is the wrong PR for Nova.

The boundaries below are firm. They are commitments, not future work:

- Nova does **not** run as root.
- Nova does **not** call `sudo`, `pkexec`, `doas`, `su`, or `runuser`.
- Nova does **not** execute model-generated shell commands.
- Nova does **not** modify firewall rules or block / unblock IPs by
  itself. SilentGuard owns enforcement.
- Nova does **not** act as a firewall.
- Nova does **not** perform autonomous security actions. Anything
  sensitive requires an explicit user confirmation in the UI.
- Nova does **not** auto-install Piper, voice models, or any other
  binary.
- Nova does **not** send prompts, audio, or conversation history to a
  third-party cloud service.
- Nova does **not** require SilentGuard to function. SilentGuard is an
  optional, off-by-default integration.
- Nova does **not** stream, transcode, or copy media files. The
  optional Jellyfin bridge reads library metadata only and never
  contacts a cloud music API.

The hardened systemd unit drops capabilities, enables
`ProtectSystem=strict`, restricts namespaces, applies a syscall
denylist, and confines writes to the Nova checkout. See
[deploy/systemd/README.md](deploy/systemd/README.md) for the
per-directive walkthrough.

## SilentGuard integration

SilentGuard is a **separate project** and remains the security and
network monitoring engine. Nova does not re-implement it.

SilentGuard's role:

- Observing local connections.
- Classifying trust (known / trusted / blocked).
- Persisting rules and emitting alerts / events.
- Mitigation and enforcement when the operator explicitly enables it.
- Exposing an optional, loopback-only read API.

Nova's role:

- Reading SilentGuard's status and recent context, when configured.
- Explaining network and security activity in plain language.
- Summarising alerts, connections, blocked items, and trusted items.
- Asking for an explicit user confirmation before forwarding any
  sensitive request (for example, enabling a temporary mitigation
  window) to SilentGuard.

The integration is **optional and off by default**:

- A per-user toggle in Settings opts each account in. Without it,
  SilentGuard data is never surfaced to that user.
- The default transport is a read-only file probe of
  `~/.silentguard_memory.json`. If `NOVA_SILENTGUARD_API_URL` is set
  (typically `http://127.0.0.1:<port>`), Nova switches to SilentGuard's
  loopback read-only HTTP API. Both transports are GET-only against a
  fixed path list.
- A small lifecycle helper can optionally start SilentGuard's user-level
  service (`systemctl --user start <unit>`) after a failed probe. Every
  gate defaults off; the helper never uses `sudo`, never spawns a
  shell, never touches firewall config, and never polls.
- Mitigation actions (enable / disable temporary mitigation) are
  confirmation-gated. Nova only POSTs to SilentGuard after an explicit
  acknowledgement from the user in the UI; SilentGuard itself requires
  the same acknowledgement payload.

When SilentGuard is reachable and the user has opted in, Nova's chat
layer appends a short read-only "Security context:" block to the
system prompt: it states the connection state and, when available,
summarises four counts (alerts, blocked items, trusted items, active
connections). Every variant of the block reminds the model that Nova
may **explain and summarise only** — it must not perform firewall or
rule actions.

For the broader design — connector abstraction, JSON contract, and
phased scope — see
[docs/silentguard-integration-roadmap.md](docs/silentguard-integration-roadmap.md).
For the operator-facing walkthrough of running SilentGuard's
read-only API as a user service, see
[docs/silentguard-background-service.md](docs/silentguard-background-service.md);
an example unit lives at
[`deploy/systemd/silentguard-api.service`](deploy/systemd/silentguard-api.service).

## Optional GitHub maintainer connector

Nova ships an **optional**, **admin-only**, **read-only** GitHub
connector (issue #119). The connector lets a maintainer ask Nova
calm questions about a repository — open issues, open pull
requests, basic metadata — without turning Nova into an autonomous
bot.

Important: this is **not** the alpha-channel GitHub OAuth login
gate (`GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` /
`/auth/github`). The OAuth flow is about *signing users into Nova*
on the alpha channel. The connector below is about *reading a
repo's state on the maintainer's behalf*. They share neither code
paths nor config keys.

The connector is disabled by default. To enable it on a local
Nova install, add the following to your `.env`:

```ini
NOVA_GITHUB_ENABLED=true
NOVA_GITHUB_TOKEN=ghp_your_local_token
NOVA_GITHUB_DEFAULT_REPO=owner/name      # optional fallback
NOVA_GITHUB_READ_ONLY=true               # default; v1 has no writes
NOVA_GITHUB_TIMEOUT_SECONDS=5.0
```

The token only needs **read** scopes (`repo:read` is enough for v1)
because Nova never performs write operations against GitHub in this
phase. Use a fine-grained personal access token scoped to the
repositories you want Nova to read; do not give the token write,
admin, or organisation-management scopes.

Once configured, Nova exposes six admin-only endpoints:

- `GET /integrations/github/status` — calm snapshot of the
  connector. The `state` field is one of `disabled`,
  `not_configured`, `unavailable`, or `connected_read_only`.
- `GET /integrations/github/issues` — list open issues for
  `?repo=owner/name` (or the default repo).
- `GET /integrations/github/pulls` — list open pull requests.
- `GET /integrations/github/issues/{number}` — single issue.
- `GET /integrations/github/pulls/{number}` — single pull request.
- `GET /integrations/github/recommendations` — short ranked list
  of issues a maintainer might want to work on next (see below).

All endpoints are auth-gated and admin-only. Non-admin and
restricted users receive a 403; the aggregate
`/integrations/status` response surfaces `state: "disabled"` for
the GitHub entry to non-admin callers so the UI can hide the card
without leaking the configured state.

### Maintainer triage / recommendations

`GET /integrations/github/recommendations` turns the open-issues
list into a short ranked list of suggestions. Nova answers
questions like *"find issues I could work on"*, *"which issues
look easy"*, *"which issues are important"*, *"find
beginner-friendly issues"*, or *"find issues related to memory /
SilentGuard / security / UI"* by scoring each open issue with
deterministic heuristics:

- `good first issue`, `docs`, `tests`, `ui` → lower difficulty;
- `architecture`, `refactor`, `migration`, `performance` → higher
  difficulty;
- `security`, `auth`, `admin`, `memory`, `github` → carry an
  explicit caution / risk note even when the issue otherwise
  looks easy;
- vague titles (`Bug`, `Help`, `Fix`, etc.) and `wontfix` /
  `duplicate` / `blocked` issues are flagged or excluded so the
  list stays actionable;
- many comments → "read the thread first" note;
- recommendations are **open issues only**; closed issues are
  ignored.

Each entry carries: `number`, `title`, `url`, `labels`, `state`,
`difficulty` (`low` / `medium` / `high`), `priority_reason`,
`recommended_next_step`, `risk_notes`, and `confidence`.

Optional query params:

- `repo=owner/name` — override the default repo;
- `label=memory` — filter to issues carrying that label;
- `difficulty=low|medium|high` — filter to that difficulty;
- `topic=memory` — case-insensitive title / label keyword match;
- `limit=5` — clamp to 1..25 (default 5).

This endpoint is **read-only**. Nova never:

- creates, closes, or comments on issues,
- edits labels, assigns users, or modifies repository settings,
- approves or merges pull requests,
- decides for the maintainer what to work on — it returns
  suggestions; the maintainer picks.

There is no background polling, no autonomous behaviour, and no
LLM-only "magic" ranking — the score is computed from labels,
title shape, and comment counts so the output is reproducible
and explainable.

Token safety contract:

- The token is read from `NOVA_GITHUB_TOKEN` and **never** returned
  in any HTTP response body, chat context, log line, or error
  message.
- The token only ever appears inside the connector's private
  request `Authorization` header — never in URLs, query params, or
  JSON bodies.
- The connector stores the token in environment-local config only.
  This PR does not persist it to the database; future revisions
  may add encrypted storage, but the v1 contract is local-first.
- Sanitised error responses (e.g. invalid token, unreachable API)
  surface a short, hard-coded summary like *"GitHub rejected the
  configured token."* — never the raw exception, never the
  response body.

What this connector is **not** allowed to do (now or via this PR):

- create, close, or comment on issues,
- comment on, approve, reject, or merge pull requests,
- change repository settings, labels, or permissions,
- push, force-push, or run any git command,
- run any background polling or scheduled maintenance.

Any future write actions will be introduced behind their own
opt-in switch, will require explicit user confirmation in the
UI, and will carry audit logging. There is no autonomous
maintainer behaviour planned.

## Optional local media assistant (Jellyfin, read-only)

Nova ships an **optional**, **admin-only**, **read-only** bridge to a
local Jellyfin music library, plus a small deterministic helper that
turns library metadata into playlist suggestions. Nova is a *local
media assistant*, not an autonomous media manager — Phase 1 is
strictly read-only and works entirely against your local server.

This is **not** a cloud music client. Nova does **not** send your
library data to any third-party music service. Nova does **not**
stream, transcode, or copy media files. Nova **only** reads metadata
(artists, albums, tracks, genres, playlists) and computes playlist
*ideas* you can choose to use.

The bridge is disabled by default. To enable it on a local Nova
install, add the following to your `.env`:

```ini
NOVA_JELLYFIN_ENABLED=true
NOVA_JELLYFIN_URL=http://127.0.0.1:8096
NOVA_JELLYFIN_API_KEY=your_local_jellyfin_api_key
NOVA_JELLYFIN_USER_ID=                   # optional; scopes reads to one user
NOVA_JELLYFIN_READ_ONLY=true             # default; Phase 1 has no writes
NOVA_JELLYFIN_TIMEOUT_SECONDS=5.0
```

The API key only needs **read** scopes because Nova never performs
write operations against Jellyfin in this phase. Generate the key
from Jellyfin's *Dashboard → API Keys* page; do not give the key
admin or playback-control scopes.

Once configured, Nova exposes six admin-only endpoints:

- `GET /integrations/media/jellyfin/status` — calm snapshot of the
  bridge. The `state` field is one of `disabled`, `not_configured`,
  `unavailable`, or `connected_read_only`.
- `GET /integrations/media/jellyfin/artists` — list music artists.
- `GET /integrations/media/jellyfin/albums` — list music albums.
- `GET /integrations/media/jellyfin/tracks` — list music tracks
  with title, artist, album, year, genres, and whole-second duration.
- `GET /integrations/media/jellyfin/genres` — list music genres.
- `GET /integrations/media/jellyfin/playlists` — list playlists
  (read-only; this endpoint never creates or edits playlists).
- `GET /integrations/media/recommendations` — deterministic playlist
  suggestions computed from the local library (see below).

All endpoints are auth-gated and admin-only. Non-admin and
restricted users receive a 403; the aggregate `/integrations/status`
response surfaces `state: "disabled"` for the Jellyfin entry to
non-admin callers so the UI can hide the card without leaking the
configured state.

### Playlist recommendations

`GET /integrations/media/recommendations` answers questions like
*"give me some chill playlist ideas"* or *"what could I play for a
late-night coding session?"* by scoring each track in your local
library against a small mood catalogue:

- `chill`, `focus`, `gym`, `dark`, `upbeat`, `sad`, `night drive`,
  `coding`.

Each playlist suggestion carries: `title`, `mood`, `description`,
`estimated_duration` (when track durations are available), a list
of `tracks` (each with `title`, `artist`, `album`, `duration`, and
a short `reason`), and a `confidence` label.

Optional query params:

- `mood=chill,focus` — comma-separated filter; entries not in the
  catalogue are dropped.
- `limit=8` — clamp to 1..12 (default 8).
- `per_playlist=12` — clamp to 3..25 (default 12).

The heuristics are deterministic — identical libraries produce
identical suggestions. There is **no** LLM call, **no** embedding
model, and **no** cloud lookup involved. The score is computed from
genre dictionaries, title-token signals, and track-duration nudges
so the output is reproducible and explainable.

This endpoint is **read-only**. Nova never:

- creates, edits, or deletes playlists on Jellyfin,
- streams, transcodes, or copies any media file,
- starts playback, queues tracks, or autoplays anything,
- decides for the user what to play — it surfaces ideas; the
  user picks.

There is no background polling, no autonomous behaviour, no autoplay.

### API-key safety contract

- The API key is read from `NOVA_JELLYFIN_API_KEY` and **never**
  returned in any HTTP response body, chat context, log line, or
  error message.
- The key only ever appears inside the bridge's private request
  `X-Emby-Token` header — never in URLs, query params, or JSON
  bodies.
- The bridge stores the key in environment-local config only.
  This PR does not persist it to the database; future revisions
  may add encrypted storage, but the Phase-1 contract is
  local-first.
- Sanitised error responses (e.g. invalid key, unreachable server)
  surface a short, hard-coded summary like *"Jellyfin rejected the
  configured API key."* — never the raw exception, never the
  response body.

### What this bridge is **not** allowed to do (now or via this PR)

- create, edit, or delete playlists on Jellyfin,
- stream, transcode, or copy any media file,
- start playback or queue tracks for playback,
- change Jellyfin server settings,
- talk to any cloud music API (Spotify, Tidal, Deezer, etc.),
- scan local disk outside of Jellyfin's own metadata,
- poll Jellyfin in the background or run scheduled work.

### Future direction (NOT in this PR)

- **Plex** support behind the same provider interface. The
  recommendation module operates on a sanitised-track dict shape,
  so a future Plex provider can plug in without touching playlist
  logic.
- **Playlist creation** behind an explicit per-request confirmation
  in the UI and a separate write switch. Nova will never create a
  playlist without an explicit "yes" from the user.
- **Auryn-led library population** as a separate project. Auryn
  remains independent; this bridge does not change Auryn's
  behaviour.

The full walkthrough lives in
[`docs/jellyfin-integration.md`](docs/jellyfin-integration.md).

## Optional admin Maintenance / Update Center

Nova ships an **optional**, **admin-only**, **opt-in** Maintenance
surface that lets an administrator inspect the local checkout's git
state, fast-forward to upstream after an explicit confirmation, and
(when explicitly configured) ask systemd-user to restart Nova — all
from the web UI. Every switch defaults to off so an unconfigured
Nova install never executes any maintenance command.

The full walkthrough lives in
[`docs/maintenance-center.md`](docs/maintenance-center.md). Setup is
deliberately granular:

```ini
NOVA_MAINTENANCE_ENABLED=false          # default; flip to true to opt in
NOVA_MAINTENANCE_ALLOW_PULL=false       # gates POST /admin/maintenance/pull
NOVA_MAINTENANCE_ALLOW_RESTART=false    # gates POST /admin/maintenance/restart
NOVA_MAINTENANCE_REPO_PATH=             # empty → use the install directory
NOVA_MAINTENANCE_RESTART_MODE=disabled  # or systemd-user
NOVA_MAINTENANCE_SYSTEMD_UNIT=nova.service
```

When enabled, the **Settings → Admin → Maintenance & Updates** card
shows the configured branch, current commit, upstream tracking
branch, working-tree cleanliness, incoming commits, and a diff-stat
summary. Pull and restart actions each require a separate switch
and a visible `confirm()` before the request is sent. The server
re-checks every condition on the actual call — the UI is a
convenience layer, not a security boundary.

Safety boundaries (enforced in
[`core/maintenance.py`](core/maintenance.py) and pinned by
[`tests/test_maintenance.py`](tests/test_maintenance.py)):

- **No web terminal.** Only a fixed allowlist of `git` subcommands
  ever runs: `fetch`, `status --porcelain`, `rev-parse`,
  `log --oneline`, `diff --stat`, and `pull --ff-only`.
- **No arbitrary shell.** Every subprocess call uses an argv list
  with `shell=False`. No string commands, no `os.system`.
- **No `sudo` / `pkexec` / `doas` / `su` / `runuser`.** Nova never
  asks for elevation.
- **No system-level `systemctl`.** Restart, when enabled, is locked
  to `systemctl --user restart <validated-unit>` with a strict
  unit-name regex.
- **Fast-forward only.** A dirty working tree or a diverged branch
  blocks the pull with a calm "manual intervention required"
  message — Nova never improvises a merge or reset.
- **Admin-only.** Non-admin and restricted users receive a `403`.
- **Confirmation-gated.** Pull and restart endpoints reject requests
  that do not carry `{"confirm": true}`.
- **No GitHub writes.** This feature reads the upstream tip via
  `git fetch` and the local working tree. It never pushes, merges
  PRs, or interacts with GitHub beyond `git fetch`.
- **No auto-update.** Nothing happens until an admin clicks
  through the confirmation. There is no background polling, no
  scheduled update.

`docs/maintenance-center.md` documents the recommended starting
point (status-only, no pull, no restart) and explains how to
optionally set up a user-level Nova unit if you want the restart
button to work without leaving the secure-deployment guide's
boundaries.

## Voice and TTS

Every assistant message has a "Read aloud" button. By default Nova uses
the browser's built-in `speechSynthesis` API — zero install, fully
local, and pleasant on most platforms (Apple Samantha, Microsoft Aria,
Google Female, …).

While Nova is reading, a soft cyan orb and waveform appears beside the
message with an inline Stop control. The animation respects
`prefers-reduced-motion` and disappears the moment playback stops or
the user switches conversations.

On Fedora and other Linux desktops the platform voices sometimes fall
back to a robotic engine. Nova detects this and surfaces a gentle hint
in Settings → Voice recommending the optional local Piper path. It
never auto-installs anything. Settings → Voice also shows an **Active
engine** chip next to the engine selector and a **Test voice** button
for side-by-side comparisons.

Privacy notes:

- Piper, when installed, runs offline. No audio bytes leave the host.
- Nova never auto-downloads a Piper binary or voice model.
- Nova does not use any cloud TTS service — neither for the
  "Read aloud" button nor for the Settings preview.
- No microphone capture, no always-on listening. Read-aloud is the only
  voice surface today.

### Optional: enable Piper

1. Install the Piper binary. The simplest path is the official release:

   ```bash
   # Pick the build for your CPU; this example is x86_64 Linux.
   curl -L -o piper.tar.gz \
     https://github.com/rhasspy/piper/releases/latest/download/piper_linux_x86_64.tar.gz
   tar -xzf piper.tar.gz
   sudo mv piper /usr/local/bin/   # or anywhere on PATH
   ```

2. Download a calm voice model. Nova suggests these soft, natural
   voices (`.onnx` model + `.onnx.json` config, side by side):

   - `en_US-amy-medium`
   - `en_GB-jenny_dioco-medium`
   - `en_US-lessac-medium`
   - `fr_FR-siwis-medium` (French)

   Models live in the
   [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices)
   repository. Place the `.onnx` and `.onnx.json` together in a folder
   you control (for example `~/.local/share/piper/voices/`).

3. Configure Nova by adding to your `.env`:

   ```ini
   NOVA_PIPER_BINARY=/usr/local/bin/piper
   NOVA_PIPER_VOICE_MODEL=/home/you/.local/share/piper/voices/en_US-amy-medium.onnx
   ```

   Both variables must be set. Leave `NOVA_PIPER_BINARY` blank if
   `piper` is already on the system `PATH`. `NOVA_PIPER_VOICE_CONFIG`
   is optional — Piper auto-discovers the sibling `.onnx.json`.

4. Restart Nova. Open Settings → Voice and select Piper. Click
   "Test voice" to confirm it works.

If Piper is missing, the model is unreadable, the subprocess fails, or
synthesis times out, Nova silently falls back to the browser engine —
the read-aloud experience is never lost.

## Development status and roadmap

Nova is under active development. The features in **Key features**
above are shipped and exercised by the test suite. The items below are
the directions of active interest — they are not commitments and
nothing in the linked documents should be cited as evidence that a
feature exists.

- **Natural-language memory pipeline.** Hardening the `memory/`
  package for production use.
- **Memory pack import (v1 backend).** A local-only Markdown memory
  pack parser, safety scanner, and confirmation-gated commit step
  lives in [`core/memory_importer.py`](core/memory_importer.py).
  Format and safety rules are documented in
  [docs/memory-pack-import.md](docs/memory-pack-import.md). UI / API
  wiring is intentionally a follow-up.
- **Multi-user UX polish.** The data model and admin endpoints exist;
  the design document
  [docs/multi-user-architecture.md](docs/multi-user-architecture.md)
  tracks the broader plan.
- **Cognitive copilot direction.** A longer-term design for semantic
  memory, temporal awareness, and Git-aware workflows lives in
  [docs/cognitive-copilot-roadmap.md](docs/cognitive-copilot-roadmap.md).
  Design only — nothing in it is implemented yet.
- **SilentGuard integration phases.** The current Phase 1 scope and
  the explicit non-goals (no autonomous blocking, no firewall
  mutations, no background polling) are tracked in
  [docs/silentguard-integration-roadmap.md](docs/silentguard-integration-roadmap.md).
- **Broader test coverage and improved model-fallback UX.**

## Running locally

### Requirements

- Linux (tested on Fedora).
- Python 3.11+.
- [Ollama](https://ollama.com) installed and running.
- AMD GPU with ROCm (optional — falls back to CPU).

### 1. Clone the repository

```bash
git clone https://github.com/TheZupZup/Nova.git
cd Nova
```

### 2. Create a virtual environment and install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Pull the required Ollama models

```bash
ollama pull gemma3:1b
ollama pull gemma4
ollama pull deepseek-coder-v2
ollama pull qwen2.5:32b
```

`qwen2.5:32b` requires significant disk space and RAM. If your hardware
is constrained, skip it; the router falls back to `gemma4` for advanced
requests.

### 4. Configure credentials

```bash
cp .env.example .env
```

Edit `.env` and set at minimum:

```
NOVA_USERNAME=your_username
NOVA_PASSWORD=your_password
NOVA_SECRET_KEY=a-long-random-string
```

The defaults in `.env.example` are intentionally weak placeholders.
Change them before any deployment, especially if Nova is exposed
beyond localhost.

### 5. Start Nova

```bash
python web.py
```

Nova is available at `http://localhost:8080`.

### Running as a systemd service

A hardened example unit lives at
[`deploy/systemd/nova.service`](deploy/systemd/nova.service); the
per-directive walkthrough is in
[`deploy/systemd/README.md`](deploy/systemd/README.md). The unit
enforces `NoNewPrivileges`, an empty capability bounding set,
`ProtectSystem=strict`, `ProtectHome=read-only`, restricted address
families, a syscall denylist, and `UMask=0077` — it does not change
Nova's behaviour, only its blast radius if something goes wrong.

For the broader deployment story (LAN-only, VPN / Zero-Trust gateway,
backups, and the explicit list of things Nova will never do) see
[docs/secure-deployment.md](docs/secure-deployment.md).

### Docker

A `Dockerfile` and `docker-compose.yml` are included. The compose
stack persists `nova.db` in a Docker volume, keeps Ollama external
(reachable via `OLLAMA_HOST`), and supports clean updates via
`docker compose pull`. See [docs/docker.md](docs/docker.md) for the
full guide.

### Portable workspace (systemd or Docker)

If you'd rather keep the Git checkout, the database, the config, the
logs, and the backups all under one parent folder that can be moved
between disks or machines as a single unit, scaffold a **Nova
Portable Workspace**:

```bash
python -m core.paths init-workspace /mnt/fastdata/NovaPortable
# or:
scripts/init-portable-workspace.sh /mnt/fastdata/NovaPortable
```

The helper creates `data/`, `logs/`, `backups/`, `config/`,
`scripts/`, and `app/` under the parent and writes a single
`config/nova.env.example` pointing `NOVA_DATA_DIR` at the workspace.
It is safe to re-run; existing files are never overwritten.

See [`docs/portable-workspace.md`](docs/portable-workspace.md) for
the full walkthrough including systemd wiring, the Docker / compose
shape ([`deploy/docker/docker-compose.portable.yml`](deploy/docker/docker-compose.portable.yml)),
and the move / backup / restore procedures.

### Configuration

All configuration is read from `.env` at startup. Key variables:

| Variable | Default | Description |
|---|---|---|
| `NOVA_USERNAME` | — | Login username for the seeded admin |
| `NOVA_PASSWORD` | — | Login password for the seeded admin |
| `NOVA_SECRET_KEY` | — | JWT signing secret |
| `NOVA_DATA_DIR` | — | Optional absolute path that holds `nova.db`, backups, and reserved subdirectories. Blank = legacy layout (DB next to the checkout). See [`docs/data-directory.md`](docs/data-directory.md), or [`docs/portable-workspace.md`](docs/portable-workspace.md) for a self-contained parent layout that works for both systemd and Docker. |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama API base URL |
| `NOVA_AUTO_WEB_LEARNING` | `false` | Enable background RSS/web learning |
| `LOGIN_RATE_LIMIT_MAX` | `5` | Max login attempts per window |
| `LOGIN_RATE_LIMIT_WINDOW` | `60` | Rate limit window in seconds (sliding) |
| `LOGIN_RATE_LIMIT_TRUSTED_PROXIES` | — | Comma-separated proxy IPs to trust for `X-Forwarded-For` |
| `NOVA_SILENTGUARD_API_URL` | — | Loopback URL of SilentGuard's read-only API (blank = file probe) |
| `NOVA_SILENTGUARD_API_TIMEOUT_SECONDS` | `2.0` | Timeout for SilentGuard probes |
| `NOVA_SILENTGUARD_ENABLED` | `false` | Host-level switch for the lifecycle helper |
| `NOVA_SILENTGUARD_AUTO_START` | `false` | Allow one-shot `systemctl --user start` after a failed probe |
| `NOVA_SILENTGUARD_START_MODE` | `disabled` | `systemd-user` is the only enabled value |
| `NOVA_SILENTGUARD_SYSTEMD_UNIT` | `silentguard-api.service` | User-level unit name to start |
| `NOVA_PIPER_BINARY` | — | Path to the optional Piper TTS binary (blank = auto-detect on `PATH`) |
| `NOVA_PIPER_VOICE_MODEL` | — | Path to a Piper `.onnx` voice model. Blank disables Piper. |
| `NOVA_PIPER_VOICE_CONFIG` | — | Path to the `.onnx.json` config (blank = auto-discover sibling) |
| `NOVA_PIPER_TIMEOUT_SECONDS` | `20` | Piper synthesis timeout, in seconds |

Model assignments are defined in `config.py` in the `MODELS`
dictionary. To swap a model, update that dictionary and restart Nova.

A note on language: the default system prompt and Nova's persona are
written in French. Nova auto-detects the language of each message and
replies in kind, so English conversations work without any
configuration change.

### Development workflow

```bash
# Run the full test suite
pytest

# Run a specific test file with verbose output
pytest tests/test_router.py -v
```

The test suite covers model routing, memory storage and parsing,
manual memory commands, rate limiting, the identity contract,
personalization, session continuity, SilentGuard provider behaviour
(including the read-only and mitigation paths), voice providers, and
the systemd unit shape.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for branch and pull request
rules.

Short version:

- Branch from `main` with a descriptive name (`feature/…`, `fix/…`,
  `refactor/…`).
- One change per PR.
- Avoid modifying unrelated files.
- Keep changes small and readable.

Check the [open issues](https://github.com/TheZupZup/Nova/issues),
particularly those labelled `good first issue`, for a way in.

## License

[Mozilla Public License 2.0](LICENSE)
