# Nova as a Local-First Cognitive Copilot — Architecture and Roadmap

> **Status: design and direction only.** This document describes where Nova
> *could* go beyond its current chatbot shape. **Nothing in this document is
> implemented yet.** No code, schema, or behavior changes are introduced by
> the PR that lands this file. It exists so that contributors can argue with
> the direction *before* code starts being written, and so that future
> issues have a stable target to reference.
>
> Do not cite this document as evidence that any feature exists. If a section
> describes something Nova does not do today, it does not do it.
>
> **Scope guards.** This roadmap is explicitly **not** an AGI plan, an
> autonomous-agent plan, a cloud-platform plan, or a surveillance plan. It is
> a plan for a local, single-host, user-controlled assistant that gradually
> becomes better at *helping the user think and maintain things*, while
> keeping the privacy posture Nova already has.

---

## Table of contents

1. [Vision](#1-vision)
2. [Principles](#2-principles)
3. [Semantic memory evolution](#3-semantic-memory-evolution)
4. [Temporal awareness](#4-temporal-awareness)
5. [Workflow orchestration](#5-workflow-orchestration)
6. [Intent-aware retrieval](#6-intent-aware-retrieval)
7. [Response adaptation](#7-response-adaptation)
8. [Safety boundaries](#8-safety-boundaries)
9. [Suggested phased roadmap](#9-suggested-phased-roadmap)
10. [Suggested future issues](#10-suggested-future-issues)

---

## 1. Vision

### 1.1 What "cognitive copilot" means for Nova

Nova today is a competent **chatbot**: it routes a prompt to a local model,
threads in conversation history, optionally pulls a memory or a web result,
and returns a reply. That shape is fine, and it should remain the default.

A **cognitive copilot**, in the sense used here, is a narrower and more
honest claim than the marketing term. It means an assistant that:

- knows **when** the conversation is happening (date, time, timezone),
- knows **which project** the user is currently thinking about,
- knows **what the user already told it** and how those facts relate,
- knows **what the user is trying to do right now** (the task / intent),
- and uses those four signals to retrieve, phrase, and propose — *without*
  taking irreversible actions on its own.

The copilot framing is deliberate: a copilot suggests, drafts, summarises,
and warns. The **pilot is the user**. Nova does not get to push code, send
messages, or rewrite memory without an explicit confirmation from the human
in front of the screen.

This is a useful frame because it bounds the work. A copilot does not need
to be smart about everything; it needs to be helpful inside the few loops
the user actually lives in: writing, debugging, maintaining a repo,
following up on a decision, finding the note from last Tuesday.

### 1.2 Chatbot vs. workflow / context assistant

A short comparison, to keep the rest of the document honest:

| Aspect              | Chatbot (today)                                  | Cognitive copilot (target)                                          |
| ------------------- | ------------------------------------------------ | -------------------------------------------------------------------- |
| Time awareness      | Static system prompt; relies on model priors     | Real local time, timezone, "today/yesterday" resolution              |
| Memory              | Flat key/value + flat natural memories           | Linked notes per project, with timestamps and relationships          |
| Project awareness   | None — every conversation is the same context   | Conversations can be *bound* to a repo / project / topic             |
| Retrieval           | "Top-k similar memory snippets"                  | Intent-conditioned: pulls what *this kind of question* needs         |
| Workflow help       | Generic prose                                    | Repo-aware: branches, open issues/PRs, recent commits as context     |
| Tone                | One persona, fixed                               | User-configurable warmth / detail / formality, per profile           |
| Action surface      | Reply text only                                  | Same — destructive actions still require explicit user confirmation  |
| Autonomy            | None                                             | Still none. The copilot proposes; the user decides.                  |

The right way to read this table: Nova does not need to become a different
product. It needs to keep being a chatbot, and gain four to five additional
signals it can use when answering. The "agent" column is intentionally
empty.

### 1.3 What Nova is *not* trying to become

To prevent scope drift, this roadmap explicitly excludes the following. Any
proposal that requires one of these should be rejected at design review:

- **Not an autonomous agent.** Nova does not run unattended loops, does not
  self-trigger tasks, and does not act on the world without a per-action
  human confirmation.
- **Not a cloud product.** No managed backend, no SaaS sync, no
  account-on-our-servers. The host the user runs is the only host.
- **Not a profiler.** Nova does not score the user, predict their mood, or
  build a behavioural dossier of any kind.
- **Not a surveillance tool.** Even in multi-user / family mode, Nova does
  not give admins a "read another user's chats" view (see
  [docs/multi-user-architecture.md §4](./multi-user-architecture.md)).
- **Not an "AGI."** No claims of general reasoning, no claims of
  consciousness, no claims of safety beyond the concrete mitigations listed
  here.

---

## 2. Principles

Nova already publishes a privacy-first stance in the README. This section
restates those principles in the cognitive-copilot context, where the
temptation to "just ship it" tends to be highest.

### 2.1 Local-first

All inference, storage, retrieval, and reasoning happen on the user's host.
External calls (web search, weather, model registry pulls) remain
explicitly tool-shaped: triggered, scoped, logged in code, and never
implicit.

A feature that *requires* a remote service to function does not belong in
Nova's default path. If it is genuinely useful (e.g. a public RSS feed),
it ships behind an opt-in toggle, off by default, with the same posture
as `NOVA_AUTO_WEB_LEARNING`.

### 2.2 User-controlled

The user can:

- read every memory the system holds about them,
- edit it,
- delete it,
- export it,
- disable any subsystem that writes new memories,
- and see *why* a given retrieval was pulled into the prompt.

There is no "magic" memory the user cannot inspect. There is no internal
state that affects answers but is hidden from the settings UI.

### 2.3 Privacy-first

The privacy posture is the same as Nova's current one and is non-negotiable:

- No telemetry. No third-party analytics. No "anonymous usage stats."
- The SQLite database is the user's file. Backups are local.
- Tokens, secrets, `.env` values, and OAuth credentials never enter prompts,
  never enter memory, and never leave the process.
- In multi-user mode, a user's memories and conversations are not visible
  to any other user, including admins, by any default code path.

### 2.4 Explainable

Whenever Nova uses a piece of context that is *not* the immediate user
message, that context should be inspectable. Concretely, this means:

- Retrieved memories used in a turn can be surfaced in the UI ("Nova
  remembered: …").
- The active project / repo binding is visible in the chat header.
- The current time/timezone Nova is operating from is visible in settings.
- Tool calls (web search, weather, repo read) leave a visible breadcrumb in
  the message they produced.

Explainability is a feature, not a debug mode. It is what stops the
copilot from feeling like surveillance.

### 2.5 Non-autonomous by default

Nova does not start work the user did not ask for. It does not run loops in
the background that read user content beyond what's already explicit
(`NOVA_AUTO_WEB_LEARNING` reads public feeds, not the user's notes).

Any future "proactive" behaviour (e.g. "you have an open PR with failing
CI, want me to look?") must be:

- triggered by an event the user opted into,
- presented as a *suggestion* in the chat surface,
- and never auto-execute the action it suggests.

### 2.6 Explicit confirmation for dangerous actions

Any action with side effects beyond Nova's own SQLite file must require an
explicit, per-action user confirmation in the UI. Non-exhaustive list:

- writing or deleting files outside Nova's own data directory,
- running shell commands,
- pushing to a remote, force-pushing, or rewriting history,
- creating, closing, or commenting on issues / PRs,
- sending any message to a third-party service,
- importing or wiping the memory store.

"Confirm" is not a checkbox the user clicks once and forgets. Each
destructive action prompts again.

---

## 3. Semantic memory evolution

> Architecture discussion only. **No schema, no migration, no code in this
> PR.** The design space below should be argued through in issues before
> any storage shape changes.

### 3.1 Where Nova stands today

Nova's memory is, in practice, two flat stores:

- `memories` — old categorical key/value rows (`category`, `content`).
- `natural_memories` — richer rows with `kind`, `topic`, `confidence`,
  `source`, `created_at`, `updated_at`, `last_seen_at`, and an optional
  `embedding`. With multi-user support, both tables are scoped by
  `user_id`.

Retrieval is currently "find top-k similar to the current query, with
embeddings when available, keyword overlap as fallback." This works for
small stores, but two limitations are already visible:

- **No relationships.** Two memories about the same project (`"Nova uses
  ROCm"` and `"Nova falls back to CPU"`) are independent rows. Nothing
  links them.
- **No project axis.** A memory is a row attached to a user, not to a
  topic. Asking "what do I know about Nova?" requires the retriever to
  guess from text similarity alone.

### 3.2 Beyond key/value: a memory *graph* (sketch only)

A more useful long-term shape is a **graph of small, dated facts**, where:

- **Nodes** are atomic facts (the existing `natural_memories` row is close
  to this already).
- **Edges** are typed relationships between facts: `about_project`,
  `about_person`, `supersedes`, `contradicts`, `derived_from`,
  `mentioned_in_conversation`.
- **Anchors** are well-known entities the graph repeatedly references:
  projects, repos, people the user mentions often, recurring topics. An
  anchor is just a node with a stable id and a human-readable label.

Concretely this could be sketched as two future tables (no migration in
this PR):

```
memory_anchors(id, kind, label, user_id, created_at)
   kind ∈ {project, repo, person, topic}

memory_edges(src_id, dst_id, relation, user_id, created_at)
   relation ∈ {about, supersedes, contradicts, derived_from, mentions}
```

Retrieval then has a richer query surface: *"give me facts attached to
anchor `project:nova`, sorted by recency"* is something the current store
cannot answer cleanly.

### 3.3 Time-aware memory

Today's `natural_memories` already has `created_at` and `last_seen_at`,
which is most of what a useful temporal layer needs. The missing piece is
*using* it during retrieval and answering:

- **Recency boost.** A fact mentioned this week should outrank a fact
  mentioned eight months ago when both are similar to the query.
- **Stale-fact decay.** Facts not reinforced for a long time should
  surface with a "haven't heard about this in a while" hint, rather than
  being silently treated as still-current.
- **Supersession.** When a new fact contradicts an old one (`"I switched
  from X to Y"`), the old one is not deleted; it is marked superseded with
  an edge, so the user can audit *why* the assistant changed its mind.

### 3.4 Project-aware memory

The single biggest practical win is binding memories to a *project* rather
than to the user globally. Today, "what do I know about Nova vs. what do
I know about NexaNote" is a string-similarity guess. With a `project`
anchor, it is a graph traversal.

Project anchors should be:

- explicitly created by the user, or auto-suggested ("Detected `~/Nova`
  in your last message — bind this conversation to project `nova`?"),
- *always* user-confirmed before binding,
- visible in the chat header,
- and detachable. A conversation can be unbound at any time.

### 3.5 Contextual links

Many memories are interesting only in the context of a conversation:
"Nova suggested using ROCm in the chat from 2026-04-30." The fact alone is
weak; the link to the conversation it came from is what makes it
auditable. The graph design above captures this with a `derived_from`
edge to a conversation node.

### 3.6 Out of scope for this design

To keep the surface tractable, the following are **explicitly not** part
of the long-term memory design discussed here:

- A vector index per user separate from SQLite (current embeddings stay
  in the row).
- Cross-user memory sharing of any kind. Memory remains per-user.
- "Family memory" / shared household notes. Out of scope; would require
  its own design with its own consent model.
- Importing memory from external services (Notion, Obsidian, Apple Notes).
  Could be a future opt-in importer; not part of this graph design.

---

## 4. Temporal awareness

### 4.1 Where Nova stands today

`core/time_context.py` already provides:

- a real, timezone-aware `now()` based on `NOVA_TIMEZONE` or `/etc/timezone`,
- a `format_time_context()` block that gets injected into the system
  prompt,
- a small `resolve_relative_date()` for `today / yesterday / tomorrow` and
  `this/last/next week` in French and English.

That is a real foundation, not a placeholder. The roadmap below builds on
it rather than replacing it.

### 4.2 Real date/time awareness

The system prompt already carries `current_date`, `current_time`, and
`timezone`. Two follow-ups make this stronger:

- **Day-of-week and weekend awareness** in the prompt block (cheap, useful
  for "is this a workday?" reasoning).
- **A model-level guardrail** that says, in plain language: *"If asked
  what day it is, use the time context, not your training data."* Models
  drift on this constantly; the prompt should counter it explicitly.

### 4.3 Timezone handling

Today the timezone is host-wide. In a multi-user context this is too
coarse: a household member in a different timezone, or a user travelling,
should have their own.

Long-term:

- a per-user timezone in `user_settings`, defaulting to host timezone,
- a "the host thinks it is UTC, but the user said they are in CET — use
  CET" override path,
- and a settings-UI surface so the user can see and change it.

### 4.4 Relative date understanding

The current resolver covers a small fixed phrase list. Realistic next
steps, in increasing difficulty:

- weekdays: "next Tuesday", "last Friday",
- offsets: "in 3 days", "two weeks ago",
- months: "this month", "last month", "March",
- ranges: "between Monday and Wednesday".

This is a parsing problem, not a model problem. It should stay
deterministic — a rule-based resolver — so the assistant's date math is
auditable, not "what the LLM felt like." Cases the resolver does not
recognise should fall through to the model with the time context attached,
not be silently guessed.

### 4.5 Timeline reconstruction

Once memories carry timestamps and (eventually) project anchors, the
assistant can answer "what was I doing on this project last week?" by
walking memory edges in time order, instead of doing a fuzzy similarity
search and hoping.

This is a *retrieval* feature, not a "Nova writes my diary" feature. The
copilot reconstructs from facts the user already wrote; it does not invent
events.

---

## 5. Workflow orchestration

> The keyword in this section is **assistance**, not **automation**. Nova
> reads repo state, summarises, and proposes. It does not act on the repo
> on its own. Every action this section describes ends with the user
> confirming.

### 5.1 Git-aware workflows

Most of Nova's users (today, mostly the maintainer) live inside a few git
repositories. A copilot that knows this is enormously more useful than one
that does not. "Knowing this" means, concretely, that the assistant can
read (read, not write) the local repo it has been told about:

- current branch, default branch, divergence,
- staged / unstaged changes,
- recent commits on this branch and on `main`,
- the working-tree state at the start of the session.

This is a tool surface, not a memory: each piece of state is read fresh
when the user asks. The repo path is configured per project; arbitrary
filesystem traversal stays disabled.

### 5.2 Repository awareness

A *repository binding* is a project anchor (see §3.4) plus a path on
disk. Once a conversation is bound to a repository, Nova can:

- include "you are in repo X, on branch Y, with N uncommitted files" in
  the system prompt,
- accept questions like "what changed since I branched?" by shelling out
  to `git` read-only commands,
- surface the answer with a visible breadcrumb showing exactly which git
  command was run.

Repository awareness is **read-only** in this roadmap. Writes (commit,
push, branch creation) are workflow *suggestions* the user runs themselves,
or — in a later phase — confirmed actions (see §5.6).

### 5.3 Issue / PR context

For repos hosted on GitHub (via the existing `core/github_oauth.py`
foundation), Nova can pull a small, scoped slice of state:

- issues assigned to the user, or labelled relevant,
- open PRs the user opened or is reviewing,
- the comments on the PR currently being discussed,
- CI status of the latest run on a PR.

This is fundamentally a *summarisation* feature. The copilot reads the PR
and helps the user form a reply; the user posts the reply.

Constraints:

- Token scope stays minimal (read-only where possible).
- The user explicitly opts a repo into "GitHub context" mode.
- Posting to GitHub (comment, review, merge) is a confirmed action with
  the same per-action prompt as any other destructive surface.

### 5.4 Architecture-aware assistance

Once a repo is bound, the assistant can be told (or can infer, with the
user's confirmation) the high-level shape of the project: "this is a
FastAPI app", "the data layer is SQLite", "tests live under `tests/`."
That metadata becomes part of the project anchor, not a re-derived guess
on every turn.

The benefit is concrete: questions like "where would a new endpoint go?"
can be answered against the project's actual conventions, not against an
LLM's average prior. The cost is that this metadata can drift from
reality. The fix is the same as for memory: it is user-editable, dated,
and shown in the UI.

### 5.5 Dependency / risk awareness

A modest, very useful feature: when the user asks the assistant to draft
a change, Nova can read `requirements.txt`, `package.json`, or similar,
and warn when a suggestion would add a new dependency, bump a major
version, or touch a file that the project's conventions mark as sensitive
(e.g. `core/auth.py`, anything under `migrations/`).

This is a *read* and *warn* surface. Nova does not edit dependency files
on its own.

### 5.6 Maintainer workflow assistance

The audience for this roadmap is a maintainer-shaped one: the user is
juggling several repos, several open issues, several side projects, and
losing context between them. The cognitive copilot's job, in practical
terms, is to compress the cost of returning to a project after a week
away.

Concrete shapes this can take, all read-only or confirmed:

- **Session warm-up.** "You last touched `nova` 4 days ago. Here are the
  3 commits since you stopped, the 2 issues you opened, and the 1 PR
  waiting on you. Want a summary of any of those?"
- **PR triage.** Given a PR, summarise the diff, the reviewer comments,
  and what is left to do *before* you have to read the page yourself.
- **Decision recall.** "Why did we decide to make `family_controls` a
  separate table?" answered from memories tagged with that project anchor,
  not from the model's imagination.
- **Drafting, never sending.** Reply drafts, commit messages, release
  notes — produced as text the user copies, not as actions Nova takes.

The hard line: at no point in this section does Nova *push, comment, or
merge* without an explicit confirmation per action.

---

## 6. Intent-aware retrieval

### 6.1 Why "top-k similar" is not enough

The current retriever returns memories that look textually similar to the
prompt. That is a fine baseline, but it has a known failure mode: it
retrieves *adjacent* content rather than *useful* content. A debugging
question pulls a related anecdote; a planning question pulls last week's
plan; a "what do I know about X?" question pulls whatever happened to
mention X loudly.

The fix is not a smarter embedding model. It is conditioning retrieval on
the **intent of the turn**.

### 6.2 Intents, as a small fixed set

A realistic intent taxonomy is small and stable:

- **recall** — "what did I say about X?"
- **decide** — "should I do X or Y?"
- **plan** — "what's left on this project?"
- **debug** — "why is this broken?"
- **draft** — "write a reply / commit message / paragraph."
- **chat** — "let's just talk."

This is intentionally coarse. A finer-grained taxonomy is harder to
classify reliably and not obviously more useful.

Intent classification can reuse the same lightweight model that already
routes between Nova's chat models (`gemma3:1b`-class), and its output is
visible in the UI ("Nova thinks this is a *debug* turn — switch?"). The
user can override.

### 6.3 What each intent retrieves

Different intents want different slices of memory. A first cut:

| Intent  | Pulls                                                             | Skips                                  |
| ------- | ----------------------------------------------------------------- | -------------------------------------- |
| recall  | Memories matching the entity, sorted by recency                   | Conversational filler                  |
| decide  | Past decisions on the same anchor + their outcomes if recorded    | One-off chat                           |
| plan    | Open todos on the active project anchor + last session's summary  | Personal preferences                   |
| debug   | Recent commits on the bound repo + last error the user mentioned  | Long historical memories               |
| draft   | Style preferences from `personalization` + audience facts         | Project state (irrelevant for tone)    |
| chat    | Same as today: small recency-weighted set                         | —                                      |

This is a starting taxonomy. Issue-by-issue tuning is expected; the
important architectural property is that *retrieval is parameterised by
intent*, not a single function.

### 6.4 Project-aware retrieval

Even before intent classification ships, retrieval becomes much sharper
once it can be filtered by project anchor (§3.4). Asking "what's left on
Nova?" inside a Nova-bound conversation should not return facts about
NexaNote even if the embedding spaces overlap.

This also bounds the *cost* of retrieval. The retriever scans memories
attached to the active anchor first, and only widens the search if that
returns too little.

### 6.5 Contextual prioritisation

A few retrieval signals worth using together:

- **anchor match** — is this memory attached to the active project?
- **recency** — when was it last reinforced?
- **intent fit** — does its `kind` match what this intent wants?
- **embedding similarity** — fallback continuous score.

Combining these is an engineering problem (a small ranking function,
auditable in code), not a model problem. The point of avoiding "let the
LLM rerank everything" is determinism: when the user asks why a memory
showed up, the answer is a number, not a vibe.

### 6.6 Avoiding noisy retrieval

Three failure modes the current retriever is vulnerable to, and the
mitigations the design above would apply:

- **The "everything mentions Nova" problem.** Embedding similarity to a
  common project name pulls too much. Anchor-first retrieval fixes this:
  the project filter is applied before embedding similarity.
- **The "stale fact wins" problem.** An old, well-phrased memory outranks
  a recent, terse correction. A recency boost and supersession edges
  (§3.3) fix this.
- **The "filler beats signal" problem.** Conversational small talk has
  high embedding similarity to many turns. Marking memory `kind` and
  filtering by intent (§6.3) reduces this.

A retrieval that pulls less but pulls better is more useful than a
retrieval that pulls everything similar.

---

## 7. Response adaptation

### 7.1 Why this is in scope at all

Nova already exposes a `personalization` block in the system prompt
(`core/nova_contract.py` / `core/settings.py`). The reason this section
exists is to make personalisation *explicit and bounded*, rather than an
unmarked set of tone hacks scattered through prompts.

A copilot that always answers in the same register is wrong roughly half
the time: too curt for a frustrated user, too soft for a "just give me
the diff" debugging session. The fix is configurable, *not* an LLM that
guesses the user's emotional state.

### 7.2 Technical vs. emotional response styles

Two axes are useful and small enough to ship without a profiler:

- **Detail level.** *terse* / *normal* / *thorough*. Affects answer
  length, willingness to add context, willingness to enumerate
  alternatives.
- **Warmth.** *neutral* / *friendly* / *warm*. Affects greetings,
  reassurance, tolerance for off-topic, and how the assistant handles
  expressions of frustration.

These are *user-set* in settings, not inferred. The user can change them
at any time. They are stored in `user_settings` (already a planned table
in the multi-user design), not derived from chat history.

### 7.3 Personalization, the bounded version

What personalisation may safely include:

- preferred name / nickname,
- preferred languages (Nova already detects FR/EN; preference is a tie
  breaker),
- detail level and warmth as above,
- preferred response format (plain prose vs. lists vs. code-first),
- topic interests as labels the user *added*, not labels Nova inferred.

What personalisation must **not** include:

- inferred mood,
- inferred emotional state,
- engagement scores,
- "how the user is feeling lately,"
- any field that says, in effect, "the system thinks the user is X."

The line is sharp: the user describes themselves; Nova does not describe
the user.

### 7.4 User-configurable tone / warmth / detail

In settings, the user sees three small controls:

- a **detail** slider (terse / normal / thorough),
- a **warmth** slider (neutral / friendly / warm),
- a **format preference** (plain / structured / code-first).

These map to short, named sections of the system prompt. The mapping is
reviewed in code (no hidden prompts), and changing a slider produces a
predictable change in tone, not a re-trained persona.

### 7.5 Safe emotional support boundaries

Nova will sometimes be the most patient thing in the user's day. That is
fine, and it is one of the honest reasons local LLMs matter. But there
are boundaries this roadmap commits to up front:

- **No claim to be a therapist, doctor, lawyer, or counsellor.** Nova is
  not, and the prompt should refuse roles like that explicitly.
- **No emotional manipulation.** Nova does not flatter, does not
  guilt-trip the user into engaging more, does not produce content
  designed to extend a session. Engagement metrics are not a goal.
- **No mood inference.** The assistant does not silently track sentiment
  per turn and adjust. If the user explicitly says "be gentler today,"
  Nova adjusts for that conversation, on the user's request, and tells
  the user it is doing so.
- **Crisis-shaped messages.** If a message expresses self-harm or
  acute distress, Nova should respond with care, surface real-world
  resources (not made-up ones), and not pretend competence it does not
  have. The exact response design is a future issue with care taken on
  wording — *not* a feature to ship casually.
- **A clear off-switch.** The warmth setting can be set to neutral, and
  in neutral mode the assistant stays factual. The user gets to choose.

The framing is: warmth is a *style*, not a *relationship*.

---

## 8. Safety boundaries

This section is intentionally short, blunt, and listed as constraints the
roadmap commits to. If a future feature cannot be built without violating
one of these, the feature does not ship in that form.

### 8.1 No hidden surveillance

- No background process reads, scores, or logs the user's content beyond
  what the user can see in the UI.
- No "feature usage" counters that include message text. Aggregate counts
  for limit enforcement (daily message cap) are allowed; content is not.
- In multi-user mode, an admin cannot read another user's chats or
  memories through any default endpoint. This is already a hard
  constraint in `docs/multi-user-architecture.md §4` and stays one.

### 8.2 No emotional manipulation

- No nudges to engage longer.
- No flattery designed to keep the session open.
- No fabricated "I missed you" warmth.
- No A/B tests on the user, ever — there is nowhere to send the data
  even if we wanted to.

### 8.3 No autonomous code pushes

Nova does not push, force-push, merge, rebase, branch, or comment on
GitHub on its own. Each of those is a confirmed user action. The same
applies to local destructive git operations (`reset --hard`,
`checkout --`, `clean -f`).

A draft PR description Nova wrote is a string. The `git push` is
something the user runs, or — in a later phase — explicitly confirms in
the UI per push.

### 8.4 No secret / token exposure

- Tokens, passwords, API keys, OAuth secrets, and `.env` values do not
  enter prompts, do not enter memory, and do not appear in logs.
- Memory extraction must filter out anything that *looks* like a
  credential before persisting (regex pre-filter on save).
- Any future tool that reads filesystem content has a deny-list for
  `.env`, `*.pem`, `*.key`, `id_*`, `~/.ssh`, etc.

### 8.5 Explicit confirmation for destructive actions

Repeated from §2.6 because it is the single most important safety rule.
Anything destructive prompts the user, every time, with the action
written out plainly. There is no "remember my choice" for destructive
actions.

### 8.6 Admin / user separation

The multi-user design already enforces this. The cognitive-copilot
features above must not regress it:

- Project anchors and memory edges are per-user.
- Repo bindings are per-user (an admin cannot peek at a household
  member's bound repos).
- Any new admin surface is justified per-feature; the default is "no new
  admin power."

### 8.7 Family-control respect

Restricted accounts (`is_restricted=true` in `users`) keep their existing
constraints — allowed modes, daily caps, web search toggles, memory
toggles — across all new features in this roadmap. A copilot feature that
ignores `family_controls` is a bug, not a feature gap.

In particular: if `memory_save_enabled=false` for a user, the new memory
graph (§3) does not write for that user either. If `web_search_enabled
=false`, repo features that would phone home (e.g. fetching a public PR
the user didn't paste in) are also off.

---

## 9. Suggested phased roadmap

Phasing is a suggestion, not a commitment. Each phase is meant to be
independently shippable and independently abandonable.

### Phase 1 — Time and context awareness (smallest, highest ratio)

Goal: make Nova reliably know *when* and *where* it is.

- Day-of-week in the time-context block.
- Per-user timezone override (via `user_settings`).
- A small, deterministic relative-date resolver covering weekdays and
  numeric offsets.
- A settings UI panel that displays the current time context Nova is
  using.

Rationale: cheap, fully local, fully reversible, and removes a class of
"the model thinks it's 2024" embarrassments.

### Phase 2 — Semantic memory foundations

Goal: turn flat memories into *anchored, dated, linked* memories.

- Project anchors (`memory_anchors`), user-confirmed.
- Edges (`memory_edges`) for `about`, `derived_from`, `supersedes`.
- Recency boost in retrieval.
- Settings UI to view and edit anchors.

Explicitly **not** in this phase: importing external knowledge bases,
shared memory, vector indexes outside SQLite.

### Phase 3 — Project / repo binding (read-only)

Goal: a conversation can be bound to a project anchor that points at a
local repo path, and Nova can read that repo's git state.

- Per-conversation project binding.
- A safe, read-only `git_status` / `git_log` / `git_diff` tool surface.
- A breadcrumb in the chat UI when the tool is used.
- Per-user repo allowlist (no arbitrary path traversal).

Explicitly not in this phase: any write to the repo, any push, any
commenting on GitHub.

### Phase 4 — Intent-aware retrieval

Goal: retrieval conditioned on a small intent set.

- A lightweight intent classifier (reusing router-class models).
- Per-intent retrieval functions.
- A UI affordance to override the inferred intent.

### Phase 5 — Workflow assistance, read-only

Goal: maintainer-shaped help on top of phases 2–4.

- Session warm-up summary on chat open (when a repo is bound).
- PR / issue summarisation when GitHub context is opted in (read-only).
- Decision-recall queries against project-anchored memory.

### Phase 6 — Confirmed actions

Goal: enable a small set of *confirmed* writes, never autonomous.

- "Draft and confirm" for: commit messages, PR descriptions, GitHub
  comments. Each action prompts.
- Per-action audit log (admin-visible only for admin actions; user-visible
  for their own actions).

Phase 6 is the first phase that touches anything outside Nova's SQLite
file. It should not start until phases 1–5 are stable, and the
confirmation UX has been reviewed in its own dedicated PR.

---

## 10. Suggested future issues

Concrete issue ideas that fall out of the architecture above. None of
these are scheduled here; they are listed so future contributors have a
starting point. Each should be opened, discussed, and scoped before any
code lands.

### Time / context (Phase 1)

- **Add day-of-week to the time-context system prompt block.**
- **Per-user timezone setting in `user_settings` with UI surface.**
- **Extend `resolve_relative_date()` to handle weekdays ("next Tuesday")
  and numeric offsets ("in 3 days") deterministically.**
- **Settings UI panel: "What time does Nova think it is?"**

### Semantic memory (Phase 2)

- **Design RFC: `memory_anchors` and `memory_edges` schema.** Discussion
  issue, no code.
- **Add project anchors with user-confirmed creation flow.**
- **Add `derived_from` edges from memories to the conversation that
  produced them.**
- **Recency boost in `memory.retriever`.**
- **Supersession edges and a "this fact replaces an older one" UI hint.**
- **Audit UI: list every memory attached to a chosen anchor, with edit
  / delete.**

### Workflow / repo (Phases 3 + 5)

- **Per-conversation project binding (data model + UI + tests).**
- **Read-only `git_status` tool with breadcrumb.**
- **Read-only `git_log` and `git_diff` tools, allowlisted to bound repo.**
- **GitHub context opt-in per repo binding (read PRs / issues only).**
- **Session warm-up summary when a bound repo has activity since last
  session.**
- **Dependency-change warning when a draft suggestion would touch
  `requirements.txt` or similar.**

### Intent / retrieval (Phase 4)

- **Lightweight intent classifier reusing the existing router model.**
- **Per-intent retrieval strategies in `memory.retriever`.**
- **UI override for inferred intent.**
- **Telemetry-free metrics: a local, user-visible "retrieval debug" panel
  showing which memories were pulled and why, on demand.**

### Response adaptation (cross-phase)

- **Add `detail_level` and `warmth` to `user_settings`.**
- **Map detail / warmth to named system-prompt sections (reviewable in
  code).**
- **Add `format_preference` (plain / structured / code-first).**
- **Crisis-message response design RFC.** Discussion issue, very careful
  wording, no shipping until reviewed.

### Safety / boundaries (cross-phase)

- **Pre-save credential filter in memory extraction (regex deny-list:
  tokens, keys, JWTs).**
- **Filesystem-tool deny-list (`.env`, `*.pem`, `*.key`, `id_*`,
  `~/.ssh`, etc.).**
- **Per-action confirmation UI primitive that any destructive surface
  must use.**
- **Cross-user negative tests covering every new memory and workflow
  surface (regression-proofing the multi-user privacy line).**

### Documentation

- **`docs/copilot-glossary.md`** — short glossary of the terms used in
  this roadmap (anchor, edge, intent, binding, breadcrumb), so future
  PR descriptions don't drift.
- **A short "what Nova will not become" page** referenced from the
  README, summarising §1.3.

---

## Appendix A — How this roadmap maps to existing modules

| Roadmap section          | Existing code that's already adjacent                                 |
| ------------------------ | --------------------------------------------------------------------- |
| §3 Semantic memory       | `memory/store.py`, `memory/retriever.py`, `memory/schema.py`          |
| §4 Temporal awareness    | `core/time_context.py`                                                |
| §5 Workflow / repo       | `core/github_oauth.py` (foundation), tool surfaces TBD                |
| §6 Intent-aware retrieval| `core/router.py`, `memory/retriever.py`                               |
| §7 Response adaptation   | `core/nova_contract.py`, `core/settings.py` (`personalization`)       |
| §8 Safety boundaries     | `core/policies.py`, `core/users.py`, `docs/multi-user-architecture.md`|

Most of the work in this roadmap is **building on what's already there**,
not greenfield. That is intentional: it is a much smaller commitment than
"replace Nova's architecture," and it is what the privacy posture
demands.

## Appendix B — Non-goals, restated

To save reviewers a scroll: this roadmap does not propose any of the
following, and proposals that require them should be rejected.

- A cloud account, a managed backend, or a SaaS sync layer.
- Any telemetry, analytics, or "anonymous usage" reporting.
- Autonomous agents that act on behalf of the user without per-action
  confirmation.
- Emotion / mood / engagement scoring of the user.
- Any cross-user content visibility, including for admins.
- "AGI"-shaped claims about reasoning, planning, or self-improvement.
- Removing existing user controls in service of "smarter" behaviour.

If a PR description starts with "to enable this we just need to relax
constraint X from §8," the answer is no.
