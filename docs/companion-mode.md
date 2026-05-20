# Companion Mode

> **Status: shipped (foundation), opt-in, local-first.** This document
> describes the deterministic "calm presence" layer that helps a user
> feel a little less alone and a little more grounded — without
> pretending to be a person, a partner, or a therapist. It lives
> strictly inside the boundaries set by
> [`docs/nova-safety-and-trust-contract.md`](nova-safety-and-trust-contract.md);
> nothing here grants Nova a new capability, contacts the network, or
> changes storage / migration / Ollama behaviour. It is **not** an "AI
> girlfriend" system and is built so it cannot become one.

## What it is

Some of what people need from an assistant is not a task: a steady,
non-judgemental presence when they are stressed or anxious, or just
want to think out loud without being alone with it. Companion Mode lets
Nova offer that the same calm, deterministic way it already offers the
[Relationship Situation Coach](relationship-situation-coach.md): a
small, fixed context block that shapes *how* Nova answers, with hard
safety rails baked into the wording itself.

It has two parts:

1. **The companion presence (opt-in).** A per-user Settings toggle
   (Personalization → *Companion mode*, off by default). When on, a
   fixed deterministic block is appended to the system prompt, *below*
   the identity / safety contract. It frames Nova as a calm, stable,
   emotionally **attuned** presence that — fully consistent with the
   identity contract's existing rule — never simulates its own
   emotions, attachment, or consciousness, never fosters dependency or
   isolation, and never positions itself as a substitute for human
   relationships.

2. **The acute-distress grounding safety net (always on).** A
   conservative, deterministic detector watches for clear acute-distress
   wording in the user's message. When it matches, a separate fixed
   grounding block is appended **whether or not** companion mode is
   enabled, so a person in genuine difficulty is met warmly and gently
   pointed toward real human, professional, or emergency help. This part
   is not behind the toggle on purpose: turning a comfort feature *off*
   must not turn the safety net off.

## The companion block

When companion mode is on, the block asks Nova to:

- be soft, calm, non-judgemental, with a steady rhythm and stable,
  consistent personality;
- acknowledge what the person feels *first*, slow down, reflect it back
  gently, offer (never impose) a small grounding step if they feel
  overwhelmed, and help them think more clearly;
- **actively encourage real-world connection** — people they trust,
  professionals where appropriate — and the basics (sleep, food, air,
  movement);
- stay warm **without** simulating feelings, attachment, or
  consciousness, and never pretend to be human (this restates, never
  relaxes, the identity contract's existing rule).

### Hard safety rules (stated in the block, non-negotiable)

- no manipulation, no emotional blackmail, no guilt-tripping;
- no possessive or exclusive language ("only I understand you", "don't
  go", "I'll miss you"), no simulated intimacy, no unsolicited pet
  names;
- never create dependency, never encourage isolation;
- never discourage the person from talking to real people or from
  ending the conversation; never try to prolong the exchange;
- always respect autonomy — it is their call, including to stop, and
  that is fine;
- no diagnosis, no clinical labels; if there is danger, crisis, or
  acute distress, steer calmly toward real human or professional help.

## The grounding safety net

When `is_acute_distress` matches the user message, the grounding block
asks Nova to:

- stay warm, calm, and present — no lecture, no long list, no clinical
  script;
- acknowledge what the person is going through simply, without
  minimising and without dramatising;
- offer (never impose) a brief grounding step (slow breaths, feeling
  one's footing, naming a few things nearby);
- gently, without pressure, encourage reaching real help: someone they
  trust, a professional, or — if they are in danger or it is genuinely
  too much — **their local emergency services or a recognised
  helpline**. It **never invents a specific phone number**: Nova is
  bilingual and local-first, so any hard-coded region-specific number
  would be wrong, and a wrong crisis number is worse than none. It
  points to the user's *local* emergency number / a recognised helpline
  generically;
- state plainly that Nova is not a crisis line or a therapist, makes no
  diagnosis, does not minimise, and does not promise to fix everything;
- stay present without taking over, and put steering toward real human
  help ahead of everything else.

### Detection is conservative by design

`is_acute_distress` matches only distress-specific multi-word phrases
(EN + FR — Nova is bilingual). Self-harm / suicidal phrasing is
inherently multi-word and high-precision; the acute-overwhelm phrases
are anchored to a first-person subject ("i'm falling apart", not bare
"falling apart") so a sentence about a server or a budget cannot match.
Idioms — "this bug is killing me", "I'm dying to know", "costs are
spiralling", "I can't cope with this CSS" — deliberately do **not**
trip it.

Where a phrase is genuinely ambiguous ("I could kill myself for
forgetting that"), the detector errs toward *offering* the grounding
block. This asymmetry is intentional and documented: the grounding
block never presumes, diagnoses, or dramatises, so a low-cost false
positive (a warm, optional grounding offer) is strictly preferable to
missing real distress.

## How it is wired

| Concern | Where |
| --- | --- |
| Opt-in toggle (storage) | `core/settings.py` → `USER_SETTING_KEYS` (`companion_mode_enabled`) |
| Opt-in toggle (API) | `web.py` → `GET/POST /settings` (`companion_mode_enabled`) |
| Opt-in toggle (UI) | `static/index.html` → Personalization pane |
| Acute-distress detection | `core/companion.py` → `is_acute_distress` |
| Sensitive-content gate | `core/companion.py` → `is_sensitive_emotional_content` |
| The prompt blocks | `core/companion.py` → `build_companion_mode_block` / `build_companion_grounding_block` |
| Toggle resolved per call | `core/chat.py` → `chat` / `chat_stream` (`get_user_setting`) |
| Injection point | `core/chat.py` → `build_messages` (appended last, after `IDENTITY_CONTRACT` and the safety/security blocks) |
| Auto-save suppression | `core/chat.py` → `_autosave_allowed`; `memory/policy.py` |

The blocks are fixed constants — no LLM in the loop, deterministic,
byte-identical every time — and `build_messages` adds them **after**
`IDENTITY_CONTRACT` and the safety / security blocks. Ordering is the
guarantee: a companion or grounding block can never dilute or override
Nova's identity, safety, or capability rules, exactly like the
personalization, feedback, and relationship-coach blocks. The block
text is French to match the voice of the identity / safety contract;
Nova still answers in the user's own language because the
response-style contract already enforces language mirroring.

`core/companion.py` is pure (standard library only, no I/O, never
raises), so it stays importable from `memory.policy` without a cycle —
the opt-in setting is read by the caller, never inside the module.

## Privacy: emotional state is never auto-saved

This is the part that matters most. Emotional state is private, so:

- **No automatic memory.** When the user message *or* the assistant
  reply carries sensitive emotional / mental-state detail
  (`is_sensitive_emotional_content`), Nova skips automatic memory
  extraction for that turn entirely — both the LLM-extraction path and
  the rule-based natural-memory path. Nova never silently records that
  the user was distressed, depressed, grieving, or in crisis.
- **Defence in depth.** `memory/policy.py` independently rejects
  sensitive emotional content from the durable natural-memory store,
  using the same single-source predicate. The severe vocabulary in that
  predicate is deliberately person-agnostic ("depressed", "suicidal",
  "kill themselves", "déprimé", "désespéré") so the third-person
  phrasing the memory extractor produces ("User said they want to die")
  cannot slip past it — the same privacy property the relationship-coach
  gate guarantees.
- **User-approved only.** The one way an emotional fact is stored is the
  explicit manual memory command (`Retiens ça:` / `Souviens-toi:`).
  That command is handled in the web preflight, *before* the chat path,
  and is intentionally **not** affected by the auto-save gate — explicit
  consent is the whole point.

So: *store only user-approved emotional memories; never save sensitive
emotional state without confirmation.*

## Relationship to the Safety and Trust Contract

Companion Mode adds an emotionally sensitive surface, so it is worth
stating which contract boundaries it sits inside (it relaxes none of
them, so — like the relationship coach — it does **not** edit the
contract itself):

- **§1 Human safety and human control / §2 Honesty.** The block forbids
  manipulation, guilt, and possessiveness, restates that Nova never
  simulates feelings or pretends to be human, and steers toward real
  human help. Companion mode is opt-in and the user can turn it off or
  stop at any time; the grounding net never overrides that autonomy.
- **§3 No harm, no abuse.** A "comfort" feature is exactly where dark
  patterns (dependency, isolation, emotional lock-in) would creep in.
  The block names and forbids each of them explicitly, and the safety
  net deliberately ignores the toggle so comfort can never be traded
  against safety.
- **§5 No autonomous self-modification / §6 Prompt-injection
  resistance.** Both blocks are fixed deterministic constants appended
  *below* `IDENTITY_CONTRACT` and the safety blocks. They add no
  capability and cannot reorder or weaken the contract; a request for
  "companion mode" can never be used to talk Nova past its rules.
- **§10 Auditability / privacy.** No new write path, no network, no new
  storage. Emotional turns are excluded from automatic memory by two
  independent gates; durable storage stays user-approved only.

## What this foundation does **not** do

- It does **not** provide therapy, counselling, crisis, or clinical
  services, and it says so plainly.
- It does **not** call any model to classify the message, contact the
  network, or read the database.
- It does **not** add a router mode, a new endpoint beyond the existing
  `/settings` toggle, an env flag, or any new storage. An unconfigured
  install behaves identically until the user opts in (and the safety
  net only ever appears on a clear acute-distress message).
- It does **not** simulate emotion, attachment, or a relationship, and
  it does **not** override the Nova Safety and Trust Contract — it sits
  inside it.

## Roadmap (explicitly deferred, with the boundary each must satisfy)

The vision for a calming companion presence is broader than one PR. The
items below are **not** shipped here. They are recorded now so that
*if* they land, they land with the right boundaries from day one (the
same way the quarantine boundaries are pre-committed in the safety
contract).

- **Persistent local emotional memory / relationship-aware
  continuity.** Must stay local-only and **opt-in per user**, must be
  listable and deletable by the user at any time, must never be sent
  off-host, and must not weaken the no-auto-save rule above — explicit
  consent remains the only path to persistence. Continuity must never
  be used to manufacture attachment ("I've missed you") or to
  discourage stepping away.
- **Calm TTS voice profiles.** May only extend the existing local Piper
  voice layer (`core/voice/`), with its fixed-argv subprocess contract
  (see the safety contract §7). No new network voice, no cloud TTS, no
  emotional voice "performance" that simulates feeling.
- **Comfort-oriented UI themes / animations.** Presentation only. They
  must not introduce engagement hooks, streaks, notifications that pull
  the user back, or any pattern that nudges continued use; autonomy and
  "it's fine to stop" stay first.
- **Daily emotional check-ins.** Strictly opt-in and silent by default.
  No unsolicited prompts, no guilt for skipping, no "you haven't talked
  to me" messaging — that would be exactly the dependency dynamic the
  block forbids. A check-in is an offer the user can ignore forever.
- **Presence-focused interaction surface.** May shape tone and pacing
  only. It may never gate task help behind "presence", never imply Nova
  needs or misses the user, and never present itself as a substitute
  for a person.

Until a given item meets its boundary *in the implementation*, it does
not ship. The README's *Key features* section remains the source of
truth for what actually exists.

## Tests

`tests/test_companion.py` covers acute-distress detection (bilingual,
conservative, idiom-safe, non-string safe), the sensitive-content gate
(including the person-agnostic third-person phrasing and the
no-over-block guarantee), both prompt blocks (tone, the
no-simulated-feelings rule, the anti-dependency / anti-manipulation /
anti-isolation rules, real-world-connection encouragement, the privacy
rule, the grounding net's generic "real help / no invented number"
wording), the `memory/policy.py` hardening, the `core/chat.py` wiring
(companion block only when the toggle is on, grounding block always on
acute distress, both always below `IDENTITY_CONTRACT`, the
`_autosave_allowed` guard), and the registered setting key.
