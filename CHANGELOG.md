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
