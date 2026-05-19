# Changelog

## Unreleased
### Fixed
- Streaming chat no longer surfaces "Nova didn't produce a reply." for
  every prompt. The chunk extractor used to filter Ollama events with
  `isinstance(event, dict)`, but `ollama-python>=0.4` streams Pydantic
  `ChatResponse` objects — subscriptable, but not `dict` instances.
  That filter silently dropped every production chunk, leaving the
  accumulator empty and tripping the empty-reply fallback even for a
  trivial "bonjour". The extractor now duck-types on the `.get` API
  both shapes expose, with a `getattr` fallback for unexpected event
  types. Regression tests cover both the dict (legacy) and
  `SubscriptableBaseModel` (production) shapes end-to-end.

### Added
- Relationship Situation Coach (foundation, local-first): a new
  `core/relationship_coach.py` adds a non-clinical "situation coach"
  that helps the user respond calmly and respectfully to an
  emotionally sensitive relationship message. A conservative,
  bilingual (FR/EN) topic detector (`is_relationship_coach_query`,
  multi-word relationship phrases only — "reply"/"elle" alone never
  trips it) gates a fixed, deterministic French prompt block
  (`build_relationship_coach_block`, no LLM, no I/O, never raises)
  that `core/chat.py` appends in `build_messages` *after*
  `IDENTITY_CONTRACT` and the safety/security blocks, so it can never
  override identity or safety rules. The block frames Nova as a
  non-clinical coach (not a therapist, no partner diagnosis), gives a
  light method (summarise; surface possible readings without
  mind-reading; choose a calm response; avoid accusatory/needy
  wording; keep healthy boundaries; speak now or wait), offers three
  styles (soft / neutral / direct but respectful), and states hard
  safety rules (no manipulation, coercion, gaslighting, revenge
  advice, or diagnosing the partner; always toward calm communication
  and consent). Privacy: sensitive relationship detail is never
  auto-persisted — a shared `is_sensitive_relationship_content` gate
  makes `core/chat.py` skip automatic memory extraction for those
  turns (new `_autosave_allowed` helper) and `memory/policy.py` reject
  such content from the durable natural-memory store; the explicit
  manual memory command ("Retiens ça:" / "Souviens-toi:"), handled in
  the web preflight, is the only path that stores a relationship fact
  and is intentionally unaffected. Documented in
  `docs/relationship-situation-coach.md`; covered by
  `tests/test_relationship_coach.py` (detection, sensitive-content
  gate, block content, memory-policy hardening, chat wiring,
  auto-save guard).
- Dev Workspace (Phase 1, read-only): a Nova Project can optionally
  link a local Git checkout so Nova *understands* its state when
  helping the user code — without modifying anything yet. A new
  `core/dev_workspace.py` resolves operator-configured allowed roots
  (`NOVA_DEV_WORKSPACE_ROOTS`, off by default), validates a candidate
  path hard (absolute, no `~`/`..`/control chars, resolves through
  symlinks to a directory containing `.git`, refuses `/`, top-level
  dirs, and broad system paths like `/home` `/mnt` `/etc`, and must
  resolve *inside* an allowed root — a symlink escaping the root is
  refused), and exposes read-only Git facts via a frozen allowlist of
  subcommands only: `status --short`, `branch --show-current`,
  `log --oneline -n 20`, `diff --stat`, `status --porcelain` (changed
  files). Every spawn is `shell=False`, timed out, stdin-closed, with
  `GIT_TERMINAL_PROMPT=0`/`GIT_OPTIONAL_LOCKS=0`; the repo path is the
  cwd, never an argv element. No commit, push, branch, fetch, clone,
  remote, file write, sudo, GitHub/Codeberg call, or background scan
  is reachable, and snapshots never raise or leak secrets/stderr
  (calm `state`: `ready`/`disabled`/`invalid_path`/`git_unavailable`/
  `error`). `core/projects.py` gains an idempotent, additive
  `local_repo_path` column and a user-scoped `set`/`get` (invalid
  path → `ProjectError`/400, foreign project → 404). Two read-only,
  user-scoped endpoints: `PUT /projects/{id}/repo` (link/unlink) and
  `GET /projects/{id}/repo/status`. The project bar gains a `⎇`
  Dev Workspace panel (linked path, branch, clean/dirty, latest
  commits, changed files, diff summary; all dynamic git output is
  rendered via `textContent`, never `innerHTML`). New suite
  `tests/test_dev_workspace.py` covers path validation, the module
  safety contract (no `shell=True`, no privilege escalation, no
  `os.system`, allowlist is read-only and refuses anything else), the
  git helpers against a real throwaway repo, the projects integration,
  and the endpoints. Later phases (patch propose → apply →
  branch/commit → PR draft → optional push) stay behind explicit
  confirmation and are **not** in Phase 1. See
  [`docs/dev-workspace.md`](docs/dev-workspace.md).
- Model provider settings (Phase 2): admin-only **default-model
  selection**. Admins can now see the models the active provider
  actually reports and choose which one Nova uses by default, from the
  UI, without adding a runtime or downloading anything. A new
  `core/model_settings.py` resolves the default model — the
  admin-selected one if safely persisted, else `config.MODELS["default"]`
  — network-free and never raising, so the chat hot path and every
  existing install (nothing persisted) behave exactly as before. Two
  admin endpoints expose it: `GET /admin/provider/models` (read-only;
  reuses the Phase-1 `health()` probe — `client.list()` for Ollama,
  never a pull or a generation) and `POST /admin/provider/default-model`
  (validates the chosen model against the active provider's reported
  list *before* persisting a single host-wide `settings` row; an
  unreachable provider, an empty/oversized string, or a not-installed
  model is refused with a sanitised `400` and **nothing is written**).
  **Settings → Models** gains a *Default model* card (current default,
  installed-model picker, *Set as default*) next to the Phase-1
  read-only provider summary. No provider name is ever accepted from
  the client (`extra="forbid"`; the core never takes one) so provider
  *selection* stays env-driven and **Ollama remains the default
  provider**. `code`/`advanced` routing and the *Code*/*Deep* modes are
  unchanged; `MockProvider` stays test-only. New suites
  `tests/test_model_settings.py` /
  `tests/test_provider_default_model_endpoints.py`; see
  [`docs/model-providers.md`](docs/model-providers.md).
- Model provider settings (Phase 1, read-only): a small admin-only
  surface to *see and validate* which model backend Nova is using,
  without adding a runtime. A new `core/provider_status.py` reports the
  configured provider, the default (always Ollama), the resolved active
  backend, the selectable providers, and the redacted Ollama host —
  calmly and read-only, never raising (an unknown configured provider
  is an `error` string, not a 500). Two admin endpoints expose it:
  `GET /admin/provider/status` and `POST /admin/provider/test-connection`
  (a cheap, read-only liveness probe — `client.list()` for Ollama,
  never a pull or a generation). The admin panel gains a **Provider**
  tab that renders the snapshot and a **Test provider connection**
  button surfacing health/errors clearly. **Ollama stays the default**
  and provider selection stays env-driven (`NOVA_MODEL_PROVIDER`) —
  nothing is written, migrated, pulled, or restarted. `MockProvider`
  stays test-only: it is never advertised as selectable, but a
  configured `mock` is reported truthfully with a clear warning so the
  state can never hide. No llama.cpp, no Ollama removal, no
  memory/projects/storage changes. New suites
  `tests/test_provider_status.py` / `tests/test_provider_endpoints.py`;
  see [`docs/model-providers.md`](docs/model-providers.md).
- Model provider in Settings → Models (Phase 1 UI, read-only,
  admin-only): the active model provider is now visible and testable
  where users actually look for model settings, not just in the deep
  Admin → Provider tab. The status snapshot now also reports Nova's
  default chat model (`config.MODELS["default"]` — host-level,
  non-secret; a missing default degrades to `""`, never an error) and
  whether the resolved backend supports streaming. A new admin-only
  card in **Settings → Models** shows the active provider and its
  state, the current/default model, the streaming flag, the redacted
  Ollama host, and a **Test connection** button with a clear
  success/failure message; the row is hidden entirely for non-admins
  and the endpoints stay `require_admin`. Reuses the existing
  `/admin/provider/status` and `/admin/provider/test-connection`
  endpoints — nothing new is written, pulled, restarted, or
  generated, Ollama stays the default, and `MockProvider` stays
  test-only. No new runtime, no model downloads, no cloud provider,
  no API keys; chat/memory/projects/storage behaviour is unchanged.
- Model-provider abstraction (Phase: provider abstraction only): Nova
  is no longer architecturally hardwired to Ollama. A new
  `core/model_providers` package introduces a backend-agnostic
  `ModelProvider` interface (`ModelRequest` / `ModelResponse` /
  `ModelChunk` / `ProviderHealth` / `ModelProviderError`), a registry,
  and an `OllamaProvider` that preserves the existing Ollama request,
  streaming, fallback, and unreachable-error behaviour exactly. The
  `chat` / `chat_stream` paths now talk to the provider interface
  instead of calling the Ollama client directly; the Ollama-specific
  stream duck-typing moved behind the provider. A deterministic
  `MockProvider` replaces ad-hoc client stubs in tests. **Ollama
  remains the default and fully supported** (`NOVA_MODEL_PROVIDER`,
  default `ollama`); future *local* runtimes (llama.cpp, transformers,
  a Nova-owned runtime) can register cleanly. No new runtime, no model
  downloads, no shell/Docker/cloud/API-key, and no settings migration
  in this phase. Nova identity / context / memory / safety stay owned
  by Nova and always above any provider. See
  [`docs/model-providers.md`](docs/model-providers.md).
- Nova Projects / Workspaces (Phase 1): a local-first, per-user
  foundation for organising conversations and memory by project (e.g.
  `Nova`, `Auryn`, `SilentGuard`, `Home Lab`, `Personal`). Adds an
  additive `projects` table and a nullable `project_id` column on
  `conversations`, `memories`, and `natural_memories` — all idempotent,
  with **no backfill and no reclassification**: existing conversations
  and memory stay "General" / global and behave exactly as before.
  Memory is now scoped: a General chat sees global memory only; a
  project chat sees global memory **plus** that project's memory and
  never another project's. Project context is contextual user data and
  is injected **below** the identity/safety contract, so it can never
  override safety, identity, auth, or admin rules. New endpoints
  (`GET/POST /projects`, `PATCH /projects/{id}`,
  `POST /projects/{id}/archive|unarchive`); `/conversations` is
  filterable by `?scope=general` / `?project_id=`; `/conversations`,
  `/chat`, and `/chat/stream` accept an optional `project_id` for new
  conversations. Archiving is a soft, reversible, non-destructive flag —
  there are no destructive project deletes. The sidebar gains a small
  `General + projects` selector; the rest of the UI is unchanged.
  Storage/migration, export/restore, and Ollama behaviour are
  untouched. See [`docs/projects.md`](docs/projects.md).
- Safe guided restore for Nova data export packages (Storage &
  Migration Phase 3). The Storage tab now exposes a four-step flow —
  inspect, dry-run, confirm, restore — backed by a new
  `apply_restore` helper in `core/data_export.py`, two admin
  endpoints (`POST /admin/storage/restore-dry-run` and
  `POST /admin/storage/restore`), and a `python -m core.data_export
  restore <archive> --confirm` CLI subcommand. Every real restore
  writes an automatic pre-restore backup of the current data under
  `NOVA_DATA_DIR/backups/pre-restore/`, refuses to proceed if the
  backup cannot be written, stages the archive into a private
  `.restore-staging/` directory inside the data root, validates
  every extracted member against path traversal / symlink escape,
  and only then replaces files atomically per-file. Failed restores
  leave existing data bit-for-bit identical; the pre-restore backup
  is preserved on success so an operator can roll back. The admin UI
  keeps the restore button disabled until inspection and dry-run
  both succeed and the operator ticks an explicit "I understand"
  checkbox. No cloud sync, no automatic restart, no shell, no model
  files; secrets, `.env`, `.git`, and Ollama models stay out by
  construction. See `docs/storage-and-migration.md` for the full
  walkthrough.
- Smoother streamed chat experience: the streaming bubble now coalesces
  incoming Ollama tokens on a short flush window (~28 ms) and only
  paints once per cycle, so single-character chunks no longer cause
  visible jitter. The final Markdown is still rendered once, on the
  `done` event, so half-formed code fences never flicker into the
  wrong layout. The endpoint still forwards every Ollama chunk as its
  own NDJSON `delta` event; coalescing lives in the renderer.
- `expressive` emoji preference level. A fourth choice in Settings →
  Personalization → Emoji level lets users opt into a slightly warmer
  feel in casual chat (one or two emojis per reply, never in clusters).
  Code, PR, documentation, and security replies stay sober regardless
  — that rule is restated in the prompt, not left to the model.
- Calmer / more human style guidance in the system prompt. The
  RESPONSE_STYLE_BLOCK now includes explicit TON / PERTINENCE /
  HONNÊTETÉ guidance: acknowledge intent briefly, stay project-focused
  on Nova / SilentGuard / PR / security questions, be honest about
  limits, and never claim to feel emotions or be conscious. The Nova
  Safety and Trust Contract still wins — the new lines are a tone
  reminder, not a new capability.
- Edit and delete sent chat messages from the chat UI (issue #94). Two
  new auth-gated endpoints, `PUT /messages/{id}` and
  `DELETE /messages/{id}`, accept content edits and message deletes
  scoped to the caller's conversations. Cross-user requests return 404
  to avoid leaking existence. Deleting a user message can optionally
  remove the paired assistant reply by passing
  `?cascade_assistant=true`; assistant deletes never cascade. Editing
  rewrites the message in place — it does not regenerate Nova's reply
  (regenerate-after-edit is left as an explicit follow-up). Memory
  entries are deliberately untouched: editing or deleting a chat
  message never removes memories already extracted from it. Feedback
  rows attached to a deleted message id are cleaned up so the local
  feedback table never carries dangling references. The chat-stream
  `done` event now also surfaces `user_message_id` so the browser can
  attach edit/delete controls to the just-sent user bubble without a
  conversation reload.
- Read-only GitHub maintainer triage helper (issue #119 follow-up).
  A new admin-only endpoint, `GET /integrations/github/recommendations`,
  surfaces a short ranked list of open issues a maintainer might want
  to work on next, with `difficulty`, `priority_reason`,
  `recommended_next_step`, `risk_notes`, and `confidence` fields per
  entry. Ranking is deterministic and label-driven — there is no LLM
  call, no background polling, and no GitHub mutation. Optional query
  params: `repo`, `label`, `difficulty`, `topic`, `limit`. The
  underlying connector stays strictly read-only; the configured token
  is never echoed back in the response.
- Local response feedback turns thumbs up / thumbs down into a per-user
  preference signal. Ratings are stored locally in SQLite (scoped per
  user, never sent off-host), and a short, deterministic preference
  block is appended to future system prompts below the identity
  contract and the personalization block. Thumbs-down accepts an
  optional short reason; reasons that look like they contain a
  credential are refused at write time. Ratings can be listed and
  deleted via `GET /feedback` and `DELETE /feedback/{id}`.

### Fixed
- Streaming chat: empty model output no longer leaves a stray empty
  Nova bubble in the transcript or persists a blank assistant row.
  The `/chat/stream` endpoint now surfaces an `error` event when the
  reply is empty or whitespace-only, and the frontend renders a calm
  fallback message instead of an unanswered bubble. Reloading a
  conversation no longer shows duplicate or empty assistant rows.

## v0.4.0 - 2026-04-24
### Added
- Manual web search button in interface
- Adaptive response length — shorter and more direct answers
- Expanded RSS learning sources (HN, Reddit, Ars Technica, Wired)
- Increased knowledge memory limit to 500 entries
- Auto-cleanup of old knowledge memories
- Settings panel with memory management (view, edit, add, delete)
- Copy button on Nova responses
- Automatic language detection FR/EN
- Model selection mode toggle (Auto/Chat/Code/Deep)
- Real-time weather via Open-Meteo API
- Web search via DuckDuckGo
- Automatic knowledge learning via RSS feeds every 6 hours

### Fixed
- Router no longer misclassifies conversational requests as code
- Auto-memory no longer saves web search results as user facts
- Search query cleaning for better results

## v0.3.0 - 2026-04-23
### Added
- Settings panel with memory management
- Copy button on responses
- Automatic language detection FR/EN
- Mode selector (Auto/Chat/Code/Deep)
- Real-time weather via Open-Meteo
- Web search via DuckDuckGo
- Automatic knowledge learning via RSS

## v0.2.0 - 2026-04-23
### Added
- Conversation history with sidebar navigation
- JWT authentication with username and password
- Persistent memory via SQLite with auto-extraction
- Intelligent model routing (gemma3:1b router)
- Mobile-friendly responsive web interface
- Cloudflare Tunnel support

## v0.1.0 - 2026-04-22
### Added
- Initial release
- Basic chat interface
- Ollama integration
- AMD ROCm support
- Multi-model support (gemma4, deepseek-coder-v2, qwen2.5:32b)
- Terminal interface
- FastAPI web server
- systemd service
