# Nova

A local-first, self-hostable AI assistant built on FastAPI and Ollama.

## What Nova is

Nova is a personal, local-first AI assistant designed to run entirely on
hardware you control — a private assistant runtime and
operator-controlled model platform. It helps with productivity, coding,
homelab work, memory, and local tools. It routes each conversation to the
most appropriate local model, maintains a persistent SQLite memory across
sessions, and serves a calm web interface reachable from any browser on
your network. There is no cloud account, no telemetry, and no required
external service.

Nova is **warm by default but an assistant, not a companion product.** It
is kind, calm, and supportive, but it is **not** an "AI girlfriend", a
romantic partner, a soulmate, or a dependency-forming emotional
companion. Nova is an AI assistant: it never claims to be human, never
role-plays a romantic partner, does not simulate feelings or attachment,
and it encourages real-world support rather than positioning itself as a
substitute for real people. These identity boundaries are spelled out in
the [Nova Safety and Trust Contract](docs/nova-safety-and-trust-contract.md)
and hold on every surface.

Nova is built around four ideas:

- **Local-first.** Inference and memory run on your machine. Outbound
  calls are limited to clearly-scoped optional tools (weather, web
  search) and only when the user triggers them.
- **User control.** Optional integrations are off by default and
  per-user. Nova never auto-installs binaries, never escalates
  privilege, and never performs sensitive actions without a visible,
  explicit confirmation.
- **Modular.** Memory, model providers, and remote integrations sit
  behind small abstractions. None of them is required for Nova to
  work; each can be replaced or left disabled.
- **Privacy-focused.** Conversation history and user-authored memories
  stay in a local SQLite file under your account. No cloud sync, no
  third-party analytics.

**Model-flexible and operator-controlled.** Nova core is a local
assistant *runtime*, not a fixed model or a fixed personality. You
install the local models you want and choose which one Nova uses for each
role — router, general chat, code, and advanced — through
`NOVA_ROUTER_MODEL` / `NOVA_DEFAULT_MODEL` / `NOVA_CODE_MODEL` /
`NOVA_ADVANCED_MODEL` (see [Configuring models](#configuring-models) and,
for constrained hosts, the
[Low-RAM profile](docs/docker.md#low-ram-profile-small-machines)). Nova
assumes you install your own models and never downloads one without your
say-so. It runs as an **assistant, not an autonomous agent**: it does not
act on its own and never executes model-generated shell commands. Nova's
default tone is warm and adaptive, but there is no special persona layer —
never a hardcoded AI-girlfriend, romantic, or dependency-forming persona
(see the [Safety and Trust Contract](docs/nova-safety-and-trust-contract.md)).

Nova is under active development. Most of what is described in this
README ships today; the **Development status** section calls out what
is still design work or experimental.

## Key features

Shipped today:

- **Multi-model routing.** A lightweight classifier (`gemma3:1b` by
  default) decides which local model handles each request: general chat,
  code-focused, or advanced reasoning. Every role is operator-configurable
  (see [Configuring models](#configuring-models)), and the router falls
  back cleanly when a model is missing.
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
- **Projects / workspaces (Phase 1).** A local-first, per-user way to
  organise conversations and memory by project (e.g. `Nova`, `Auryn`,
  `NexaNote`, `Home Lab`, `Personal`). Global memory stays available
  everywhere; project memory is visible only inside its project and
  never leaks across projects. There is always an implicit *General*
  bucket — existing conversations and memory stay unscoped with no
  backfill. Project context is contextual user data injected *below*
  the safety/identity contract and can never override it. The sidebar
  gains a small project selector; nothing else in the UI changes. See
  [docs/projects.md](docs/projects.md).
- **Dev Workspace (read-only, Phase 1).** A project can optionally
  link a local Git checkout so Nova *understands* its state — branch,
  clean/dirty, recent commits, changed files — while helping you code.
  Opt-in via `NOVA_DEV_WORKSPACE_ROOTS` and strictly read-only: no
  commit, push, branch, fetch, file writes, sudo, or GitHub/Codeberg
  calls. Hard path validation keeps links inside an operator-allowed
  root. See [docs/dev-workspace.md](docs/dev-workspace.md).
- **Grounded Code mode.** When a project has a linked repository and you
  pick **Code** mode, Nova attaches a small read-only briefing to that
  turn — branch, dirty state, changed files, recent commits, and a few
  bounded excerpts of the files you named or are editing. It is capped
  on every axis (no repository dumps), reads git's index rather than
  scanning the filesystem, can never excerpt a secret file, and is
  inserted *below* the safety contract as explicitly untrusted data.
  Nova still cannot write, commit, or run anything.
- **Model profiles.** Each routing role resolves to a profile — role,
  recommended context size, tool-use, coding specialisation, resource
  class, operator notes — instead of a bare model name. Inferred
  locally, overridable with an optional JSON file, and purely
  descriptive: a profile grants nothing. See
  [docs/model-profiles.md](docs/model-profiles.md).
- **Local model evaluation.** An admin-only harness that runs the same
  coding cases against the models you have and records model, context
  size, elapsed time, success, which requested constraints were
  followed, the output, and an optional human rating. Model output is
  scored as text and **never executed**; results stay local. See
  [docs/model-evaluation.md](docs/model-evaluation.md).
- **Local model health.** Installed vs loaded vs not-installed per role,
  the context size the runtime actually reports, and useful errors when
  the backend or a model is unavailable — read-only, no downloads, and
  no assumption about any GPU vendor. Surfaced as an admin-only
  **Settings → Models → "Model roles & health"** card with a Refresh
  button and no write control. See
  [docs/model-health.md](docs/model-health.md).
- **Session continuity.** A small, deterministic "continue where we
  left off" summary surfaces recent conversation topics on return.
  Derived from data already in the sidebar, dismissable, never
  emotional or inferential.
- **Web interface.** A futuristic but quiet web UI with conversation
  sidebar, mode selector (Auto / Chat / Code / Deep), copy buttons,
  and a settings panel for memories, model preferences,
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
- **Neutral, adaptive response style.** The baseline Nova style — what
  a fresh user gets with no settings configured — is kind, calm, and
  adaptive to the situation: concise for simple questions, detailed
  when the user needs real help, technical when debugging or coding,
  and steady when the user is stressed. Nova is a neutral AI
  assistant, not a character: it has no gender, no romantic identity,
  never claims human feelings, never uses dependency-forming language,
  and never overrides identity, safety, auth, admin, privacy, system,
  developer, project, or Dev Workspace rules. Users do not need to
  configure anything to receive a kind, useful assistant.
- **Local response feedback.** Thumbs up / thumbs down under each
  assistant message records a per-user preference signal in the local
  SQLite database; a thumbs-down accepts an optional short reason such
  as *"focus on this project, not generic advice"*. The data feeds a
  short, deterministic preference block in future system prompts — it
  never leaves the host, never triggers model training, and cannot
  override the identity contract or the Nova Safety and Trust Contract.
  See [docs/nova-safety-and-trust-contract.md](docs/nova-safety-and-trust-contract.md)
  for the boundaries this layer sits inside.
- **Sensitive-topic memory privacy.** Emotionally personal turns
  (distress, grief, relationship detail, first-person emotional
  disclosures) are excluded from automatic memory extraction by
  deterministic gates (`core/sensitive_topics.py`); a sensitive fact is
  stored only when the user asks explicitly via the manual memory
  command (`Retiens ça :` / `Souviens-toi :`).
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
- **Optional weather and web search.** Open-Meteo (no API key) and
  DuckDuckGo, both opt-in and triggered explicitly by the user.
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
  sensitive_topics.py Deterministic sensitive-topic detectors for memory privacy
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
  integrations/       Per-user gates for optional integrations
    media/            Local-first media bridges (Jellyfin, read-only)

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
- Nova does **not** modify firewall rules or block / unblock IPs.
- Nova does **not** act as a firewall or a security suite.
- Nova does **not** perform autonomous security actions. Anything
  sensitive requires an explicit user confirmation in the UI.
- Nova does **not** auto-install binaries.
- Nova does **not** send prompts or conversation history to a
  third-party cloud service.
- Nova does **not** stream, transcode, or copy media files. The
  optional Jellyfin bridge reads library metadata only and never
  contacts a cloud music API.

The hardened systemd unit drops capabilities, enables
`ProtectSystem=strict`, restricts namespaces, applies a syscall
denylist, and confines writes to the Nova checkout. See
[deploy/systemd/README.md](deploy/systemd/README.md) for the
per-directive walkthrough.

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
memory / security / UI"* by scoring each open issue with
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

## Optional admin Storage & Migration Center

Nova ships an **optional**, **admin-only** Storage & Migration
surface that reports where Nova stores its data, builds portable
data export packages, and lets an administrator inspect an existing
package before restoring it on another machine. Every endpoint is
read-only or confirmation-gated; nothing in this feature moves,
overwrites, or deletes Nova data on its own.

The full walkthrough lives in
[`docs/storage-and-migration.md`](docs/storage-and-migration.md).
The three endpoints are:

* `GET /admin/storage/status` — calm read-only snapshot of the
  data directory, database file, reserved subdirectories, free
  disk space, mount-class warnings, and (informational) Ollama
  models path. Warns when `NOVA_DATA_DIR` is unset or on a
  transient mount such as `/run/media/...`.
* `POST /admin/storage/export` — builds a `nova-data-export-<UTC
  stamp>.tar.gz` package containing `manifest.json`, `RESTORE.md`,
  and an **allowlisted** copy of Nova's runtime data. Requires
  `{"confirm": true}`. The export never includes `.env`, `.git`,
  `.venv`, caches, Ollama models, media libraries, SSH keys, or
  anything outside the data directory; symlinks whose target
  resolves outside the data root are skipped.
* `POST /admin/storage/inspect-export` — validates an existing
  archive in the configured exports directory. Parses the
  manifest, refuses path traversal or symlink-escape members, and
  returns a structured report. Read-only.

Restore in Phase 1 is the documented **manual** procedure in
`docs/storage-and-migration.md`. The dry-run plan (no file writes)
refuses to proceed when the target already contains a `nova.db`.

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
- **Projects / workspaces follow-ups.** Phase 1 (project-scoped
  conversations + memory) is shipped; planned follow-ups — automatic
  global-vs-project memory classification, linked local repo path,
  project import/export, per-project model settings, project files —
  and their explicit non-goals are tracked in
  [docs/projects.md](docs/projects.md).
- **Broader test coverage and improved model-fallback UX.**

## Running locally

> **Just want to use Nova?** The recommended install is Docker — see
> **[INSTALL.md](INSTALL.md)** (and [docs/docker-desktop.md](docs/docker-desktop.md)
> if you manage containers with Docker Desktop). The steps below are the
> from-source path, aimed at contributors and developers.

### Requirements

- Linux (tested on Fedora).
- Python 3.11+.
- [Ollama](https://ollama.com) installed and running. (Optional
  alternative: run a local `.gguf` model directly via llama.cpp with no
  Ollama — see [`docs/local-gguf.md`](docs/local-gguf.md).)
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

### 3. Pull the Ollama models you want

These are the **default** models for Nova's four roles. Pull the ones you
plan to use — Nova never downloads them for you:

```bash
ollama pull gemma3:1b
ollama pull gemma4
ollama pull deepseek-coder-v2
ollama pull qwen2.5:32b
```

`qwen2.5:32b` (and to a lesser degree `gemma4` / `deepseek-coder-v2`)
need significant disk and RAM. **On a constrained, CPU-only host, don't
pull them** — instead point every role at one small model and pull just
that (see [Configuring models](#configuring-models)):

```bash
# .env
NOVA_ROUTER_MODEL=gemma3:1b
NOVA_DEFAULT_MODEL=gemma3:1b
NOVA_CODE_MODEL=gemma3:1b
NOVA_ADVANCED_MODEL=gemma3:1b
```

```bash
ollama pull gemma3:1b
```

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

### Docker (recommended)

The fastest way to run Nova — nothing to install on the host but Docker
itself. The included `Dockerfile` and `docker-compose.yml` bring up the
whole stack: the Nova web app **and** a bundled Ollama model server. All
runtime data (database, memory, settings, logs, exports) is persisted on
a `nova-data` volume and models on an `ollama-models` volume, so rebuilds
and updates never lose data.

```bash
docker compose up -d
# pull at least one model, then open http://localhost:8000 (admin / changeme)
docker compose exec ollama ollama pull gemma3:1b
```

No `.env` is required — the stack starts with safe defaults. Copy
`.env.example` to `.env` only when you want to change the admin login,
ports, or enable integrations (and **do** change the admin password before
exposing Nova beyond localhost — note that `NOVA_USERNAME` /
`NOVA_PASSWORD` seed the account on the very first start only; on a
running install, reset the password from the in-app admin Users panel).

**Don't want to build?** A prebuilt image is published to the GitHub
Container Registry on every push to `main` and every release tag, so you
can deploy without cloning or building the repo. Use the bundled
`docker-compose.ghcr.yml`, which pulls `ghcr.io/thezupzup/nova:latest`:

```bash
docker compose -f docker-compose.ghcr.yml up -d
docker compose -f docker-compose.ghcr.yml exec ollama ollama pull gemma3:1b
# update later: docker compose -f docker-compose.ghcr.yml pull && \
#               docker compose -f docker-compose.ghcr.yml up -d
```

Pin a specific release with `NOVA_IMAGE_TAG=1.2.3` in `.env`. The same
`nova-data` / `ollama-models` volumes are used either way, so switching
between build and prebuilt keeps your data.

Nova is reachable at `http://localhost:8000`, and from other LAN machines
(including Windows browsers) at `http://<host-ip>:8000`. See
[docs/docker.md](docs/docker.md) for first-run, starting/stopping, logs,
updates (incl. optional auto-update), production vs development, backups,
resetting, pulling models, optional NVIDIA GPU, and using Nova from a
Windows browser.

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
| `NOVA_DEV_WORKSPACE_ROOTS` | — | OS-path-separator- or comma-separated absolute directories that may contain repos a Project can link (read-only Phase 1). Blank = the Dev Workspace feature is off. Never set to `/`, `/home`, or a broad system path. See [`docs/dev-workspace.md`](docs/dev-workspace.md). |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama API base URL |
| `NOVA_ROUTER_MODEL` | `gemma3:1b` | Model for the request-routing classifier. See [Configuring models](#configuring-models). |
| `NOVA_DEFAULT_MODEL` | `gemma4` | Model for general chat, vision, and memory extraction. |
| `NOVA_CODE_MODEL` | `deepseek-coder-v2` | Model for code-focused requests. |
| `NOVA_ADVANCED_MODEL` | `qwen2.5:32b` | Model for complex / long-context reasoning. |
| `NOVA_AUTO_UPDATE_MODELS` | `false` | When `true`, a weekly background job runs `ollama pull` for the configured model map. Off by default — Nova never re-downloads models unattended. |
| `NOVA_AUTO_PULL_MODELS` | `false` | When `true`, pull `NOVA_BOOTSTRAP_MODELS` once on first start (opt-in, background, non-blocking). |
| `NOVA_BOOTSTRAP_MODELS` | — | Comma-separated models the opt-in bootstrap pulls (e.g. `gemma3:1b`). Ignored unless `NOVA_AUTO_PULL_MODELS=true`. |
| `NOVA_MODEL_PROFILES_PATH` | — | Optional JSON file overriding/adding [model profiles](docs/model-profiles.md). Blank looks for `model-profiles.json` beside Nova's data; an absent file is normal. Read-only, never written by Nova. |
| `NOVA_CODE_CONTEXT_ENABLED` | `true` | Attach the read-only repository briefing on explicit Code-mode turns for a project with a linked repo. Inert without `NOVA_DEV_WORKSPACE_ROOTS`. |
| `NOVA_CODE_CONTEXT_FILES` | `4` | Files excerpted per Code-mode turn (ceiling 8). |
| `NOVA_CODE_CONTEXT_BUDGET` | `12000` | Total character budget for those excerpts (ceiling 40000). |
| `NOVA_EVAL_CASES_DIR` | — | Directory of your own `*.json` [evaluation cases](docs/model-evaluation.md); overrides shipped cases by id. Blank uses only `evals/cases/`. |
| `NOVA_MODEL_PROVIDER` | `ollama` | Model backend: `ollama` (default) or `llamacpp` (local GGUF, no Ollama). See [`docs/local-gguf.md`](docs/local-gguf.md). |
| `NOVA_MODEL_DIR` | `/mnt/archive/nova-models` | Directory local `.gguf` files must live inside. Admins can set the model path from Settings → Models, or pick from a read-only, bounded listing of the `.gguf` files in this directory (the model library); only paths inside this directory are accepted (no traversal, no arbitrary files, no symlink escape). |
| `NOVA_GGUF_MODEL_PATH` | — | Absolute path to a local `.gguf` model file (only used when `NOVA_MODEL_PROVIDER=llamacpp`). Nova never downloads it. Blank = provider unconfigured. An admin-set path (Settings → Models) takes precedence. |
| `NOVA_GGUF_CONTEXT_SIZE` | `4096` | GGUF context window (`n_ctx`) |
| `NOVA_GGUF_THREADS` | `0` | GGUF CPU threads (`n_threads`); `0` = auto |
| `NOVA_GGUF_GPU_LAYERS` | `0` | GGUF layers offloaded to GPU (`n_gpu_layers`); `0` = CPU only |
| `NOVA_AUTO_WEB_LEARNING` | `false` | Enable background RSS/web learning |
| `LOGIN_RATE_LIMIT_MAX` | `5` | Max login attempts per window |
| `LOGIN_RATE_LIMIT_WINDOW` | `60` | Rate limit window in seconds (sliding) |
| `LOGIN_RATE_LIMIT_TRUSTED_PROXIES` | — | Comma-separated proxy IPs to trust for `X-Forwarded-For` |

### Configuring models

Nova routes each turn to one of four **roles**; you choose which model
fills each role. The defaults are unchanged from earlier releases
(backward compatible), and each is overridable per host via an env var:

| Role | Env var | Default | Used for |
|---|---|---|---|
| Router | `NOVA_ROUTER_MODEL` | `gemma3:1b` | lightweight request classifier |
| Default | `NOVA_DEFAULT_MODEL` | `gemma4` | general chat, vision, memory |
| Code | `NOVA_CODE_MODEL` | `deepseek-coder-v2` | code-focused requests |
| Advanced | `NOVA_ADVANCED_MODEL` | `qwen2.5:32b` | complex / long-context reasoning |

Set any of these in `.env` (or the container environment) and restart
Nova. Leave one unset to keep its default. On a low-RAM host, point all
four at `gemma3:1b` so Nova never tries to load a model the host can't run
(see the [Low-RAM profile](docs/docker.md#low-ram-profile-small-machines)).
`config.py` still holds these defaults if you'd rather edit them there.

**Nova installs no models for you.** Downloads stay explicit: chat only
*uses* models you've installed (a missing one produces a clear "model not
installed" message naming it), the weekly auto-update job is off unless
you set `NOVA_AUTO_UPDATE_MODELS=true`, and an optional first-run
bootstrap (`NOVA_AUTO_PULL_MODELS` + `NOVA_BOOTSTRAP_MODELS`) is
opt-in, background, and non-blocking. Admins can review the configured
map, reachability, and installed-vs-missing models read-only via
`GET /admin/models/status`, and the richer installed / loaded /
context-size view via `GET /admin/models/health`.

The full picture of how these pieces fit together is
[docs/model-platform.md](docs/model-platform.md).

**A model is a profile, not just a name.** Each role resolves to a
[model profile](docs/model-profiles.md) — recommended context size,
tool-use, coding specialisation, resource class, operator notes —
inferred locally from the model name and overridable with an optional
JSON file. Profiles are descriptive: none of it grants a capability.
Compare the models you have on the same coding tasks with the
[evaluation harness](docs/model-evaluation.md).

**Running a future `nova-coder`.** A Nova-specific fine-tuned coding
model needs no code change to adopt: install it yourself, then set
`NOVA_CODE_MODEL=nova-coder:14b`. It resolves to a coding-specialised,
large-context profile out of the box, and Nova stays model-flexible —
swapping it back out is one variable. Nova will never download it for
you. The safe, opt-in path for producing training data is described in
[docs/nova-coder-dataset.md](docs/nova-coder-dataset.md); **no model is
trained by Nova and nothing is ever exported automatically.**

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

The test suite covers model routing, model profiles, Code-mode
repository grounding, the local evaluation harness and dataset-export
gate, model runtime health, memory storage and parsing, manual memory
commands, rate limiting, the identity contract, personalization,
session continuity, and the systemd unit shape.

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
