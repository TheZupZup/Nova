# UX study: Open WebUI-inspired improvements for Nova

Status: report only. No code changes proposed in this document beyond the
PR breakdown at the end. Nothing here is implemented yet.

This study answers the brief in the parent issue:

> Identify which UX patterns from mature local AI interfaces (Open WebUI in
> particular) would make Nova easier to use **without turning it into an
> Open WebUI clone** and **without weakening Nova's identity, memory model,
> local-first stance, or behavior contract**.

The investigation is grounded in the current code at the time of writing:
`static/index.html`, `web.py`, `core/chat.py`, `core/router.py`,
`core/nova_contract.py`, `core/memory.py`, `core/memory_importer.py`,
`memory/store.py`, and `config.py`.

Open issues already in flight are referenced rather than re-proposed.

---

## 1. Current Nova UX gaps

The list is what an attentive new user actually hits today, mapped to the
files that produce the behavior.

### a. Conversation sidebar caps at 15 entries with no search and no rename
- `static/index.html` (the `renderConversations` function) sorts by id DESC
  and `slice(0, 15)`. Conversations 16+ are unreachable from the UI even
  though `GET /conversations` returns the full list.
- There is no conversation rename UI. Titles are auto-derived from the
  first 40 chars of the first user message in `web.py:362`
  (`update_conversation_title(conversation_id, request.message[:40])`).
  A user who starts a thread with "hi" lives with the title "hi" forever.
- There is no search box. With more than ~10 active threads the sidebar is
  already hard to navigate.
- Conversations are not grouped (Today / Yesterday / Older).

### b. Mode selector and per-message badge leak underlying model identities
- `core/nova_contract.py` (the IDENTITY_BLOCK) explicitly tells the model
  *never* to mention the underlying model names: "Ne mentionne jamais le
  nom du modèle sous-jacent (ex: gemma4, gemma3, deepseek, qwen)…".
- The UI does the opposite. The mode dropdown in `static/index.html` has
  the descriptions:
  - Chat → "Forces gemma4"
  - Code → "Forces deepseek-coder-v2"
  - Deep → "Forces qwen2.5:32b"
- Each assistant message is labelled `Nova · {model_name}` (the
  `appendMessage` function), and the chat header `#model-badge` shows the
  raw Ollama model id on every reply.
- This is a UI-side contract violation. The model is told to hide the
  identity that the chrome around it puts on screen.

### c. No tool-call visibility (weather / search / memory)
- `core/chat.py` runs three "tools" silently: `detect_weather_city` →
  Open-Meteo, `should_search` / `force_search` → DuckDuckGo, and
  `get_relevant_memories` → natural memory store.
- The reply just appears. There is no chip, badge, citation, or
  "weather called" indicator. Users cannot tell which tool fired or why.
- This also makes debugging hard: when the wrong tool fires, the only
  evidence is the response text itself.

### d. Chat input is a single-line `<input type="text">`
- `static/index.html` declares `<input type="text" id="user-input">`.
  Multi-line input is impossible. Pasted code or prose collapses
  visually.
- The keydown handler already guards `if (e.key === "Enter" && !e.shiftKey)`
  but Shift+Enter is dead on a real `<input>` element — the browser cannot
  insert a newline there.
- This makes "Code" mode — supposedly the codepath worth the heaviest
  router decision — awkward to actually use.

### e. Settings panel is one long scroll with no sections
- `#settings-body` stacks RAM Budget, Auto-update, Nova Model override,
  and the entire memory list (which can be hundreds of items) in a single
  vertical scroll.
- There are no tabs, no grouping, no per-category memory filter, no
  search inside memories, no count. With ~50+ memories the panel becomes
  a wall.

### f. Memories are a binary present/absent: no disable/mute
- `core/memory.py` exposes only insert / update / delete on the `memories`
  table. There is no `enabled` flag.
- A user who wants to temporarily ignore a memory must delete and later
  retype it. Open WebUI's "toggle" pattern fits here without breaking the
  schema beyond a single column.

### g. No memory or conversation export/import surface
- `core/memory_importer.py` parses Markdown memory packs and produces a
  preview, but no UI is wired up. (Tracked in issues #84, #97, #98, #93.)
- There is no "export" path at all. The local-first claim is true at the
  filesystem layer — `nova.db` is on disk — but a user who wants their
  data in a portable format has to read SQLite themselves.

### h. No edit / regenerate / delete on chat messages
- Tracked in issue #94. Worth flagging here because it interacts with
  issue #64 (Ollama error strings persisted as assistant messages
  poisoning future context). Both have the same root: messages are
  immutable today.

### i. Stop button cancels the frontend only
- Tracked in issue #96. The stop UX is implemented (`activeChatController`
  in `static/index.html`, plus the Send→Stop toggle), but the backend
  keeps generating. Worth keeping in scope as a UX fix because the user
  *thinks* they stopped.

### j. Mobile keyboard handling is not safe-area-aware
- The `@media (max-width: 640px)` block in `static/index.html` collapses
  the sidebar correctly, but `#input-area` has no
  `padding-bottom: env(safe-area-inset-bottom)`. On iOS Safari the input
  bar drifts under the home indicator and behind the keyboard.
- The `<meta viewport>` lacks `viewport-fit=cover`, which compounds the
  problem.

### k. Empty state is one static line
- `<div id="empty-state">` shows just `⬡ NOVA` and "Discute avec Nova".
  No suggested prompts, no quick-starts, no help. Users with a blank
  Nova have nothing to click.

### l. Mode descriptions read like developer notes, not user copy
- "Router decides automatically", "Forces gemma4" — both technically
  accurate, neither user-friendly. A persona-aligned description ("for
  everyday questions" / "for code work" / "for long, careful answers")
  would fit the rest of the Nova voice.

### m. Channel switcher is misleading
- `static/index.html` renders a Stable / Beta dropdown that just opens
  external domains. There is no real "switch channel" action; the chrome
  implies one.
- Alpha is not rendered into the dropdown at all, so admins land there
  via direct URL.

### n. No version / build identifier in the UI
- The CHANGELOG says `v0.4.0`. The UI shows nothing. Bug reports from
  users cannot reference a version they can see.

### o. `NOVA_ADMIN_UI` is dead code
- Defined in `config.py:11` but read nowhere — confirmed via grep across
  `web.py`, `static/index.html`, and `core/`. The flag exists in the
  schema with no effect.

### p. `/conversations` returns everything on every navigation
- The endpoint is unpaged. As the conversation count grows, the cost of
  each sidebar refresh grows linearly.

---

## 2. Open WebUI-style patterns worth adopting

Adopt the *idea*, write the *code*. None of the items below would copy
Open WebUI source.

| Pattern | What Nova adopts |
|---|---|
| Time-grouped conversation list with a search box | Group by Today / Yesterday / Past 7 days / Older. Persist a small `<input>` search above the list that filters by title. Drop the 15-item slice. |
| Inline conversation rename | Pencil icon → contenteditable title → Enter to save. Backed by a new `PATCH /conversations/{id}` endpoint with a `Field(max_length=200)`. |
| Tool-call chips inline with the assistant message | Small chips: `☀ weather · Sherbrooke`, `🔍 web search · 5 results`, `🧠 used 3 memories`. Click to expand details. |
| Tabbed settings | General · Models · Memories · About. Even the simplest split removes the wall-of-controls feeling. |
| Memory enable/disable toggle | Third action next to edit/delete. New `enabled` column on the legacy `memories` table; the prompt builder skips disabled rows. |
| Edit / regenerate / delete on messages | Already issue #94. Reference, do not duplicate. |
| Auto-resizing multiline `<textarea>` with Shift+Enter for newline | Replace the input element. The keydown handler already does the right thing if the element supports it. |
| Empty-state suggestions | 3–4 chips that prefill the input. Persona-safe phrasing. |
| Mobile safe-area + autosize fixes | `padding-bottom: env(safe-area-inset-bottom)`, `viewport-fit=cover`, autosize textarea, focus into view on mobile. |
| Local-only data export | Settings → General → "Export my data": JSON file with conversations + memories. No upload, no cloud. |
| Backend-aware Stop | Already issue #96. Reference, do not duplicate. |
| Memory import preview UI | Already issues #97 and #98. Reference, do not duplicate. |

---

## 3. Patterns to avoid

Open WebUI has plenty that does not belong in Nova. Explicitly skip:

- **Cloud LLM connectors and API-key fields.** No OpenAI / Anthropic /
  OpenRouter / Together panes. Nova is local-first; adding these slots
  changes the threat model.
- **Function/plugin/pipeline registries.** A user-extensible tool-call
  surface widens the attack surface and breaks the single-binary feel.
- **Workspaces, folders, drag-and-drop org.** Nova is single-user. The
  data structure does not even have a workspace concept.
- **Multi-tenant admin pages with role-based access.** No multi-user;
  no roles to enforce.
- **Document upload + RAG ingestion UI.** Different scope, different
  privacy posture, different memory model. Touching it would weaken
  the "memory is small, curated, local" property.
- **Replacing the Auto/Chat/Code/Deep mode names with raw model picks.**
  The mode names are persona-correct. Only their descriptions and the
  per-message badges leak. Fix the leak; do not invert the design.
- **A public channel switcher that drops users into Alpha.** Alpha is
  GitHub-OAuth gated. The dropdown should not pretend it is a free
  switch.
- **Showing the underlying Ollama model name on each message bubble.**
  This is the most direct violation of the identity contract today.
- **Voice / STT / TTS UI.** Tracked separately as issue #4. Out of
  scope for this study.
- **Rating thumbs / fine-tune feedback.** Cosmetic noise; no training
  loop in Nova.

---

## 4. Small PR breakdown

Nine independent PRs. Each is small, reviewable in one sitting, and lands
without blocking the others. None of them duplicate work already covered
by issues #84, #93, #94, #96, #97, or #98.

### PR 1 — Hide underlying model names in the UI
**Goal:** Restore the identity contract on the UI side.
**Changes:**
- Mode dropdown descriptions: replace "Forces gemma4 / deepseek-coder-v2
  / qwen2.5:32b" with persona copy ("for everyday questions", "for code
  work", "for long, careful answers").
- Per-message label: drop `· {model}` from the visible label. Stash the
  raw model on `dataset.model` for admin/debug only.
- `#model-badge` in the header: render the *mode* (Auto/Chat/Code/Deep),
  not the raw Ollama id.
- `i18n` strings updated in both FR and EN.
**Files:** `static/index.html`.
**Risk:** Low. UI-only, no API change.

### PR 2 — Time-grouped conversation list with search
**Goal:** Make the sidebar useful past 15 conversations.
**Changes:**
- Add a small search `<input>` above `#conversations-list` that
  case-insensitively filters titles.
- Render conversations under group headers: Today / Yesterday / Past 7
  days / Older. Use `updated` from the existing payload.
- Remove `slice(0, 15)`.
**Files:** `static/index.html`.
**Risk:** Low. Frontend-only. `GET /conversations` already returns all.

### PR 3 — Conversation rename
**Goal:** Let the user fix auto-derived titles.
**Changes:**
- New endpoint `PATCH /conversations/{id}` accepting
  `{title: str = Field(max_length=200)}`. Auth-protected via
  `get_current_user`.
- Reuse `update_conversation_title` from `core/memory.py`. No DB
  migration needed.
- Pencil icon next to each conv item → contenteditable → Enter saves,
  Escape cancels. Sidebar refreshes after save.
- Tests: `tests/test_conversation_rename.py` covering happy path, max
  length, missing auth, missing conversation.
**Files:** `web.py`, `static/index.html`, new `tests/test_conversation_rename.py`.
**Risk:** Low–Medium. New mutation endpoint; thin and validated.

### PR 4 — Multi-line textarea + Shift+Enter newline
**Goal:** Make the input usable for code and prose.
**Changes:**
- Replace `<input id="user-input">` with `<textarea id="user-input">`.
- Autosize JS that grows from 1 line to ~6 lines, then scrolls.
- Keep Enter → send, Shift+Enter → newline (the handler already
  branches on `!e.shiftKey`).
- `padding-bottom: env(safe-area-inset-bottom)` on `#input-area`.
**Files:** `static/index.html`.
**Risk:** Low. No backend change.

### PR 5 — Tool indicators (weather / search / memory)
**Goal:** Make tool calls visible to the user.
**Changes:**
- `core/chat.chat()` returns a third element: a list of tool traces. Each
  trace is `{kind: "weather"|"search"|"memory", detail: dict}`.
- `web.py` `/chat` response gains a `tools` array. v1 keeps it
  ephemeral — no DB migration. (A persisted version can come in a
  follow-up if needed; that would be a small `messages.tools_json`
  column.)
- Frontend renders chips above the assistant message bubble:
  `☀ weather · Sherbrooke`, `🔍 web search · 5 results`,
  `🧠 used 3 memories`. Click to toggle a small detail panel.
- Tests: update `tests/test_router.py` and add `tests/test_tool_traces.py`
  to lock the response shape.
**Files:** `core/chat.py`, `web.py`, `static/index.html`,
  new `tests/test_tool_traces.py`.
**Risk:** Medium. Changes the chat response contract. Existing tests on
  `chat()` need updating because the return tuple grows. Keep ephemeral
  in v1 to avoid a DB migration.

### PR 6 — Tabbed settings panel
**Goal:** Stop forcing a single scroll for unrelated controls.
**Changes:**
- Add a tab strip inside `#settings-panel`: General · Models · Memories
  · About.
  - General: RAM Budget, model auto-update.
  - Models: Nova model override (the existing block).
  - Memories: list, add form, search across memories.
  - About: channel + branch, build version (string from
    `core/version.py`, see PR 9), GitHub link.
- No new endpoints in this PR; only restructuring.
**Files:** `static/index.html`.
**Risk:** Low. Pure restructure.

### PR 7 — Mobile keyboard / safe-area fixes
**Goal:** Stop the input bar disappearing under iOS keyboard / home
  indicator.
**Changes:**
- `<meta name="viewport" content="..., viewport-fit=cover">`.
- `padding-bottom: env(safe-area-inset-bottom)` on `#input-area` and
  `#mode-bar`.
- On focus of the textarea on mobile, scroll the input area into view.
**Files:** `static/index.html`.
**Risk:** Low. CSS + a small focus handler.

### PR 8 — Empty-state suggestions
**Goal:** Give a new user something to click.
**Changes:**
- Replace the static empty-state with 3–4 suggestion chips that prefill
  the input. Persona-safe wording in FR + EN — no model names, no
  capability the contract does not actually provide.
- Suggestions: "What's the weather in Sherbrooke?", "Aide-moi à écrire
  un email", "Souviens-toi: …", "Find me a recipe with what's in my
  fridge".
**Files:** `static/index.html`.
**Risk:** Low.

### PR 9 — Admin/About clarity (read-only)
**Goal:** Make the build self-describing and the dead `NOVA_ADMIN_UI`
  flag actually do something.
**Changes:**
- New `core/version.py` with `__version__ = "0.4.0"` (matched to
  CHANGELOG; bumped on release).
- New `GET /admin/info` endpoint, gated by `NOVA_ADMIN_UI=true` AND auth.
  Returns channel, branch, version, ollama base URL, model registry —
  *no* secrets, no `.env` values.
- About tab in Settings (PR 6) shows version + channel + branch. If
  admin UI is off, About still shows version; the admin block hides.
- Tests: `tests/test_admin_info.py` covers gate-off → 404, gate-on +
  no auth → 401, gate-on + auth → 200 with redacted shape.
**Files:** `config.py`, `web.py`, new `core/version.py`,
  `static/index.html`, new `tests/test_admin_info.py`.
**Risk:** Low–Medium. New endpoint; carefully bounded to read-only
  metadata.

### Deliberately out of scope (already tracked)
- Memory import preview UI — issues #97, #98.
- Memory pack documentation — issue #93.
- Edit / delete on chat messages — issue #94.
- Backend-aware Stop — issue #96.
- Capabilities block in Nova contract — issue #99.
- SearXNG support — issue #8.
- Voice — issue #4.

These remain on their own issues. This study does not duplicate them.

---

## 5. Files most likely to change

Consolidated across the nine PRs:

| File | Touched by |
|---|---|
| `static/index.html` | Every PR |
| `web.py` | PR 3, PR 5, PR 9 |
| `core/chat.py` | PR 5 |
| `core/memory.py` | None directly (PR 3 reuses existing `update_conversation_title`) |
| `config.py` | PR 9 |
| `core/version.py` (new) | PR 9 |
| `tests/test_conversation_rename.py` (new) | PR 3 |
| `tests/test_tool_traces.py` (new) | PR 5 |
| `tests/test_admin_info.py` (new) | PR 9 |
| `tests/test_router.py` | PR 5 (response-shape update) |

No DB migrations are required for any of the nine PRs.
PR 5 *can* persist tool traces in a follow-up, which would add a
`messages.tools_json` column; the v1 plan above avoids that.

---

## 6. Risk per change

| PR | Title | Risk | Why |
|---|---|---|---|
| 1 | Hide model names in UI | Low | UI-only; restores identity contract |
| 2 | Grouped conversation list + search | Low | Frontend-only; no API change |
| 3 | Conversation rename | Low–Medium | New auth-protected mutation; small surface |
| 4 | Multi-line textarea + safe-area | Low | UI-only; replaces one element |
| 5 | Tool indicators | Medium | Changes `chat()` return shape; tests break without update |
| 6 | Tabbed settings | Low | Pure restructure |
| 7 | Mobile keyboard / safe-area fixes | Low | CSS + small JS |
| 8 | Empty-state suggestions | Low | UI-only |
| 9 | Admin / About read-only | Low–Medium | New gated endpoint; bounded payload |

---

## Constraints respected (from the parent issue)

- No Open WebUI source code is copied. All adopted patterns are described
  abstractly and re-implemented from scratch within Nova's existing
  conventions.
- No clone of the Open WebUI product. Nine small, additive PRs, none of
  which approach feature parity with Open WebUI.
- Nova's identity contract is preserved and, in PR 1, *strengthened*:
  the UI stops leaking the model identities the contract already
  forbids.
- Nova's local-first / privacy-first direction is preserved. No cloud
  connectors, no telemetry, no document upload. The export PR (called
  out under §2) writes a local JSON file; it does not transmit.
- Memory import work is referenced but not touched. Issues #84, #93, #97,
  #98 keep their scope.
- This is a report. No code changes have been made.
