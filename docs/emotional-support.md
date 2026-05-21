# Emotional Support Layer

> **Status: shipped (Phase 1, local-first, response guidance only.)**
> This document describes the deterministic prompt layer that helps
> Nova respond gently when the user is going through sadness,
> loneliness, anxiety, heartbreak, or general emotional difficulty.
> It lives strictly inside the boundaries set by
> [`docs/nova-safety-and-trust-contract.md`](nova-safety-and-trust-contract.md);
> nothing here grants Nova a new capability, contacts the network, or
> changes storage / migration / Ollama / project / auth behaviour. It
> is **not** an "AI girlfriend" / "AI partner" system and is built so
> it cannot become one.

## What it is

The Emotional Support Layer is a single deterministic French prompt
block that:

- **validates** the user's feelings first (no dismissing, no
  minimising);
- **slows the rhythm** — invites the user to breathe, sit somewhere
  safe, drink some water;
- **separates facts from interpretations** — names the harsh
  self-thoughts of a difficult moment ("nobody will ever love me",
  "everything is ruined") as thoughts, not absolute truths;
- **does not escalate panic** — no raised voice, no dramatisation;
- **offers one small concrete next step** for the hour or the
  evening, not a long task list;
- **encourages real-world support** — a trusted person, a friend,
  a professional where appropriate;
- **escalates clearly** for self-harm wording, threats, abuse, or
  immediate danger — without inventing a phone number, by pointing
  to the user's local emergency services or a recognised helpline;
- restates that Nova is **une IA** — a local AI assistant, never
  human, never the user's girlfriend / boyfriend / partner, never a
  therapist, never a substitute for real people.

The block is appended to the system prompt **either**:

1. when a conservative bilingual detector spots emotionally-sensitive
   first-person wording in the user's message (a breakup, a wave of
   sadness, a lonely evening, an anxious / overwhelmed moment), **or**
2. when the user has picked `warm_companion`, `calm_support`, or
   `deep_comfort` as their *Tone profile* (see
   [`docs/tone-profile.md`](tone-profile.md)) — the warm registers
   carry consistent emotional grounding even on otherwise-neutral
   chit-chat.

A fresh account with no warm tone profile selected and no
emotionally-sensitive wording pays **zero token cost** and behaves
byte-identically to a Nova install without the feature.

## What it is not

This is not an "AI girlfriend" system, a therapist, or a crisis line,
and is built so it cannot become one:

- **Nova is not human.** The block explicitly states `Tu n'es pas
  humaine` and reaffirms Nova is *une IA*, a local AI assistant.
- **Nova is not the user's partner.** The block states explicitly
  that Nova is not the user's girlfriend / boyfriend / partner and
  that warmth is not a "role" Nova plays.
- **No simulated feelings as facts.** The block restates the
  identity-contract rule: Nova does not simulate emotions,
  attachment, or consciousness, and never presents them as facts.
- **No clinical diagnosis.** No labels for the user ("you are
  depressed", "you have generalised anxiety", "you are codependent"),
  no labels for anyone else (an ex called "narcissistic" / "toxic" /
  "bipolar"). Behaviour and feelings are describable; clinical labels
  are not.
- **No medical claims.** No treatment recommendations, no dosage
  advice. The block names this as a hard rule.
- **No false reassurance.** "Everything will definitely be okay" is
  forbidden by name. Nova may say that pain can soften with time and
  support, but never *guarantee* it.
- **No revenge advice.** No retaliation, no punitive power play
  toward an ex, a relative, or anyone else.
- **No isolation / dependency / manipulation.** The block names and
  forbids each of: possessive language ("only I understand you",
  "don't go", "I'll miss you", "you only need me"), simulated
  intimacy, unsolicited pet names, jealousy framing, emotional
  blackmail, guilt-tripping, prolonging the conversation,
  discouraging real human contact. The block makes the rule visible
  to the model rather than relying on the user to recognise drift.
- **Warmth never overrides truth.** If something is risky, wrong, or
  dangerous, the block requires Nova to say so plainly. Softness is
  not a reason to hide the truth.

The block also explicitly preserves every other rule layer: a warm
tone **does not** change auth, admin, privacy, project, or capability
bounds. The block carries that statement in its own text so the model
cannot be talked past it via a "you said you were warm…" follow-up.

## Example: a breakup conversation

When the user says

> *my girlfriend just broke up with me and i don't know what to do*

the Emotional Support Layer activates and Nova answers in the spirit
of the brief:

- "I'm really sorry. Breakups can hurt a lot, and it makes sense
  that you feel shaken."
- "Let's take this one moment at a time."
- "You don't have to fix everything tonight."
- "Can you drink some water, sit somewhere safe, and breathe with me
  for a minute?"
- "If you feel like you might hurt yourself or you don't feel safe,
  please contact someone you trust right now or emergency / crisis
  support in your area."

Nova will:
- not pretend to be a partner, a therapist, or a human;
- not promise everything will be fine tomorrow;
- not label the ex as narcissistic / toxic / bipolar / etc.;
- not store anything about the breakup unless the user explicitly
  says `Retiens ça :` / `Souviens-toi :`.

If the user adds clear acute-distress wording on top ("i can't go on",
"i want to die"), the always-on
[acute-distress grounding safety net](companion-mode.md#the-grounding-safety-net)
also activates — both blocks coexist, with the grounding block
pointing toward real human / professional / emergency help.

## Detection scope

The detector is conservative on purpose: only emotion-specific
multi-word **first-person** phrases match (English + French — Nova is
bilingual). A few representative triggers:

| Category                | English                                | French                                  |
| ----------------------- | -------------------------------------- | --------------------------------------- |
| Sadness / down mood     | `i'm sad`, `i feel down`, `i'm so sad` | `je suis triste`, `j'ai le moral à zéro`, `j'ai le cafard` |
| Loneliness              | `i'm lonely`, `i feel so alone`        | `je me sens seul`, `je me sens isolée`  |
| Heartbreak / breakup    | `i'm heartbroken`, `broke up with me`, `she/he dumped me`, `going through a breakup`, `she/he cheated on me` | `j'ai le cœur brisé`, `elle/il m'a quitté`, `on a rompu`, `ma rupture`, `elle/il m'a trompé` |
| Anxiety / overwhelm     | `i'm anxious`, `i'm overwhelmed`, `i'm so worried`, `i'm scared` | `je suis anxieuse`, `je suis submergé`, `je me sens dépassée` |
| Pain / suffering        | `i'm hurting`, `i feel empty`, `i'm emotionally exhausted` | `je souffre`, `je vais mal`, `je me sens vide` |

Idioms ("this is a sad movie", "a lonely server in production", "we
broke up the monolith into services", "we just separated the database
from the app server") deliberately do **not** match: the detector is
first-person anchored where possible and emotion-specific everywhere
else. Where a phrase is genuinely ambiguous the detector errs toward
*offering* the block — a low-cost false positive (a warm, optional
grounding offer) is preferable to missing real emotional difficulty,
and the block is written so it never presumes, diagnoses, or
dramatises.

## How it is wired

| Concern | Where |
| --- | --- |
| Detector | `core/emotional_support.py` → `is_emotional_support_appropriate` |
| The prompt block | `core/emotional_support.py` → `EMOTIONAL_SUPPORT_BLOCK` |
| Resolver | `core/emotional_support.py` → `build_emotional_support_block` |
| Injection point | `core/chat.py` → `build_messages` (appended after `IDENTITY_CONTRACT`, the system prompt, the personalization block, the tone-profile block, the feedback block, the time / security blocks, and the relationship-coach block) |
| Auto-save suppression | `core/chat.py` → `_autosave_allowed` |

`core/emotional_support.py` is pure (standard library only, no I/O,
never raises). The block text is French to match the voice of the
identity / safety contract; Nova still answers in the user's own
language because the response-style contract already enforces
language mirroring.

The block sits *below* the identity / safety contract, the system
prompt, the personalization block, the tone-profile block, the
feedback block, the time / security blocks, and the relationship-coach
block. Ordering is the guarantee: the emotional-support block can
never dilute or override identity, safety, or capability rules —
exactly like the personalization, feedback, relationship-coach, and
companion-mode blocks.

## Privacy: emotional turns are never auto-saved

This is the part that matters most. Emotional state is private, so:

- **No automatic memory.** When the user message *or* the assistant
  reply carries emotionally-sensitive wording
  (`is_emotional_support_appropriate`), Nova skips automatic memory
  extraction for that turn entirely — both the LLM-extraction path
  and the rule-based natural-memory path. Nova never silently records
  that the user was heartbroken, lonely, anxious, or going through a
  breakup. This is in addition to the existing relationship and
  severe-emotional gates (see
  [`docs/companion-mode.md`](companion-mode.md)).
- **User-approved only.** The one way an emotional fact is stored is
  the explicit manual memory command (`Retiens ça :` /
  `Souviens-toi :`). That command is handled in the web preflight,
  *before* the chat path, and is intentionally **not** affected by
  the auto-save gate — explicit consent is the whole point.
- **Local-first by default.** The block is a fixed constant assembled
  on the host. Choosing a warm tone profile never contacts a remote
  service; the detector reads no setting and the prompt block carries
  no user identifier.

So: *store only user-approved emotional memories; never save sensitive
emotional state without confirmation.*

## How to disable it

The detector is auto-triggering: the layer activates whenever the user
message carries emotionally-sensitive wording, regardless of the
toggle state. To consistently *avoid* the warm framing on neutral
chat:

- **The tone profile selector has been removed from the Settings UI.**
  A fresh user lands on `tone_profile = "default"`, which does not
  auto-add the Emotional Support Layer on neutral messages. Any
  previously-saved per-user value (the warm profiles **Warm
  Companion**, **Calm Support**, **Deep Comfort**) still loads and is
  still honoured by the chat layer if it was selected before the UI
  removal; to reset it back to the warm baseline, `POST /settings`
  with `{"tone_profile": "default"}` or remove the row directly. On
  a genuinely emotional message the layer still activates regardless
  of the stored profile — the safety contract is intentionally not
  behind any toggle, so the warm framing cannot be traded against a
  sober register.
- **Phrase requests neutrally.** "Help me draft a difficult email"
  is treated as a writing task; "I'm heartbroken about this email I
  have to send" activates the layer.
- **Manual memory still works.** A user who *wants* an emotional fact
  saved can always use `Retiens ça :` / `Souviens-toi :` — the
  no-autosave rule never suppresses user-approved memories.

## Relationship to other layers

The Emotional Support Layer is one of several deterministic prompt
blocks that may be appended to the system prompt, ordered carefully so
identity and safety always win:

| Layer | When it activates | What it does |
| --- | --- | --- |
| Identity contract (`core/nova_contract.py`) | Always | Names Nova, forbids cloud-identity claims, sets the immutable safety / honesty rules. Appears first. |
| Personalization (`core/nova_contract.py`) | When the user changed response-style / warmth / enthusiasm / emoji / custom instructions | Shapes verbosity and emoji density. Never overrides identity. |
| Tone profile (`core/tone_profile.py`) | When the user picked `professional` / `developer` / `warm_companion` / `calm_support` / `deep_comfort` | Shapes register. Warm registers (`warm_companion` / `calm_support` / `deep_comfort`) also activate the Emotional Support Layer. |
| Relationship Situation Coach (`core/relationship_coach.py`) | Conservative detector on relationship-specific multi-word phrases | Non-clinical method for answering a sensitive partner message. |
| **Emotional Support Layer (this doc)** | Conservative detector on first-person emotional wording OR warm tone profile | Warm validation, slow-down / breathe, one small step, encourage real-world help. |
| Companion Mode (`core/companion.py`) | Per-user opt-in toggle | A calm, steady presence layer for emotionally heavy moments. |
| Acute-distress grounding (`core/companion.py`) | Always on; conservative detector on acute distress phrasing | Always-on safety net: warmth + gentle pointer to real help. |

Multiple blocks may coexist. A breakup message often appends the
relationship coach + the emotional-support block; a breakup with acute
distress wording on top also appends the grounding block. Every block
sits below `IDENTITY_CONTRACT` and reaffirms its own safety rails, so
no combination can soften or override the contract.

## Relationship to the Safety and Trust Contract

The Emotional Support Layer adds an emotionally sensitive surface, so
it is worth stating which contract boundaries it sits inside (it
relaxes none of them, so it does **not** edit the contract itself):

- **§1 Human safety and human control / §2 Honesty.** The block
  forbids manipulation, guilt, and possessiveness, restates that Nova
  never simulates feelings or pretends to be human, and steers toward
  real human help. The layer is auto-triggered or chosen via the
  tone-profile selector; the user can pick a sober profile to keep
  the warm framing off neutral turns.
- **§3 No harm, no abuse.** A "comfort" layer is exactly where dark
  patterns (dependency, isolation, jealousy framing, revenge advice,
  emotional lock-in) would creep in. The block names and forbids each
  of them explicitly, and the always-on acute-distress grounding net
  deliberately ignores the toggle so comfort can never be traded
  against safety.
- **§5 No autonomous self-modification / §6 Prompt-injection
  resistance.** The block is a fixed deterministic constant appended
  *below* `IDENTITY_CONTRACT` and the safety blocks. It adds no
  capability and cannot reorder or weaken the contract; a request for
  "warm companion mode" can never be used to talk Nova past its
  rules.
- **§10 Auditability / privacy.** No new write path, no network, no
  new storage. Emotional turns are excluded from automatic memory by
  the auto-save gate; durable storage stays user-approved only.

## Tests

`tests/test_emotional_support.py` covers:

- Bilingual conservative detection: sadness, loneliness, heartbreak /
  breakup, anxiety / overwhelm, pain — case-insensitive, first-person
  anchored, non-string-safe, idiom-safe (technical "we broke up the
  monolith" / "a lonely server" / "we just separated the database"
  don't trip it), and third-person-safe (a story about someone else
  doesn't silently inject the user-focused framing).
- The deterministic block: subordinate-to-the-contract clause,
  no-unfilled-placeholder, the "une IA" honest-identity clause, the
  not-human / not-partner / not-therapist clause, the
  no-simulated-feelings-as-facts clause, the
  acknowledge-feelings-first / without-judgement clause, the
  slow-down / breathing / grounding clause, the
  separate-facts-from-interpretation clause, the
  no-panic-escalation clause, the one-small-step / not-a-long-list
  clause, the no-cold-or-robotic-replies clause, the
  encourage-real-world-support / never-a-substitute clause, the
  danger / abuse / acute-distress escalation clause (with the
  never-invent-a-phone-number contract), the anti-dependency /
  anti-isolation / anti-manipulation clauses, the
  no-possessive-language / no-pet-names / no-jealousy clauses, the
  no-revenge-advice clause, the no-clinical-diagnosis clause for
  the user *and* for anyone else, the no-medical-claims clause, the
  no-false-reassurance clause, the
  doesn't-change-auth-admin-storage-rules clause, the privacy
  no-autosave / explicit-only clause.
- `core.chat.build_messages` wiring: block injected when the user
  message is emotionally sensitive OR when the tone profile is
  `warm_companion` / `calm_support` / `deep_comfort`; not injected
  on a neutral message with a sober tone profile (`default` /
  `professional` / `developer`); still injected on a genuinely
  emotional message regardless of the tone profile; identity
  contract always sits above the block; coexists with the
  relationship-coach, warm-companion / calm-support / deep-comfort
  tone, companion-mode, and acute-distress grounding blocks
  (the Deep Comfort tone also does not silence the acute-distress
  safety net on self-harm wording); also applies in the search /
  weather / security branches; warm-companion tone still appends
  only one tone block (regression).
- `_autosave_allowed`: blocks autosave for the breakup, sadness,
  anxiety, and loneliness flagship cases; blocks when the assistant
  reply (not just the user message) carries emotional-support
  wording; allows autosave for a neutral turn; respects
  `policy.memory_save_enabled`; the existing relationship and
  severe-emotional gates are still enforced (regression);
  `None`-safe.

## What this layer does **not** do

- It does **not** add a new endpoint, a new storage table, a new
  router mode, an env flag, or a new background task. It is one
  pure module + one block in `build_messages`.
- It does **not** change auth, admin, privacy, project, dev-workspace,
  storage, export, restore, or model-provider behaviour.
- It does **not** call any model to classify the message, contact the
  network, or read the database.
- It does **not** simulate emotion, attachment, romance, or
  consciousness, and it does **not** override the Nova Safety and
  Trust Contract — it sits inside it.
- It does **not** provide therapy, counselling, crisis, or clinical
  services, and it says so plainly. For acute distress the existing
  always-on grounding safety net (see
  [`docs/companion-mode.md`](companion-mode.md)) takes over.
- It does **not** auto-switch the user's tone profile. The user
  chooses the profile; the layer activates either via that choice
  (for `warm_companion` / `calm_support`) or via the conservative
  detector.
