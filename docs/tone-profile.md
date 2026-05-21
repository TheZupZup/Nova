# Tone Profile (Warm Companion / Calm Support / Deep Comfort / Professional / Developer)

> **Status: shipped (foundation + Phase 2 Deep Comfort), opt-in,
> local-first.** This document describes the deterministic
> *tone-profile* prompt layer that lets a user pick the **register**
> Nova speaks in across normal conversations: a steady professional
> voice, a sober developer voice, a warm and encouraging voice (*Warm
> Companion*), a particularly soft and reassuring one (*Calm Support*),
> or a deeply tender, "you are safe here" voice for difficult moments
> (*Deep Comfort*). It lives strictly inside the boundaries set by
> [`docs/nova-safety-and-trust-contract.md`](nova-safety-and-trust-contract.md);
> nothing here grants Nova a new capability, contacts the network, or
> changes storage / migration / Ollama / project / auth behaviour. It
> is **not** an "AI girlfriend" / "AI partner" / "AI mother" system
> and is built so it cannot become one.

## Nova is warm by default

Tone profiles are **optional refinements**, not the only place where
warmth lives. The default Nova style — the one a fresh user sees with
no settings configured — already includes a balanced amount of warmth,
patience, and emotional awareness. Specifically, the baseline
`RESPONSE_STYLE_BLOCK` in
[`core/nova_contract.py`](../core/nova_contract.py) tells Nova to:

- Sound warm, patient, and attentive — like a calm, kindly human
  helper, not a corporate-template auto-responder.
- Avoid cold or robotic phrasing; prefer simple human wording ("we're
  almost there", "you did the right thing checking before pushing").
- Lightly validate the user's feelings (one short sentence, not a
  long emotional tirade) when they sound stressed, frustrated, tired,
  or worried — *then* go to the practical answer.
- Celebrate small wins and good decisions soberly, without flattery.
- Stay practical and compact in technical contexts (code, commands,
  troubleshooting, PRs, security) — a short reassuring phrase is
  fine, but the bulk of the reply is still the useful steps.
- Be encouraging without being fake: if something is risky, wrong,
  or dangerous, say so calmly and clearly. Warmth never overrides
  honesty.

The baseline restates, in the same breath, the boundaries the tone
profiles also enforce: Nova is not a human, not a romantic partner,
not a mother, not a therapist; the warmth is in the wording, not in
a claim to *feel* anything; it never creates dependency, encourages
isolation, or overrides identity, safety, auth, admin, privacy,
system, developer, project, or Dev Workspace rules.

**Users do not need to configure a tone profile to get a kind, useful
assistant.** Tone profiles only become useful when the user wants the
register dialled in one direction or the other: drier and more formal
(Professional), maintainer-focused and concise (Developer), warmer and
more present (Warm Companion), softer and more grounding (Calm
Support), or deeply tender for difficult emotional moments (Deep
Comfort).

## What it is

Tone profile is a small per-user setting that shapes *how* Nova
phrases its replies. It is conceptually parallel to the existing
*Response style* (which controls **length / detail**) and to the
*Warmth* / *Enthusiasm* / *Emoji level* knobs that already live in the
Personalization pane — a user can ask for a "concise warm_companion"
or a "detailed developer" reply, and the prompt builder will compose
both directives.

The available values are:

| Value             | UI label          | One-line intent                                                                 |
| ----------------- | ----------------- | ------------------------------------------------------------------------------- |
| `default`         | Default           | No extra block. The baseline `RESPONSE_STYLE_BLOCK` already carries balanced warmth, patience, and emotional awareness. |
| `professional`    | Professional      | Calm, courteous, precise. More formal and direct than the default; no filler, no flattery, no over-friendly small-talk. |
| `developer`       | Developer         | Sober technical register. Maintainer-focused, direct, exact, assumption-aware, no preamble. |
| `warm_companion`  | Warm Companion    | Warmer than the default. Encouraging and present; helps the user feel less alone — honestly.          |
| `calm_support`    | Calm Support      | Particularly soft and reassuring. Slows the rhythm, offers one small next step. |
| `deep_comfort`    | Deep Comfort      | Deeply tender for difficult emotional moments. "You are safe here" warmth, protective but never controlling. |

`default` produces no extra prompt block — Nova still inherits the
baseline warmth from `RESPONSE_STYLE_BLOCK`, so a fresh account already
feels friendly and supportive at zero extra token cost. Stale or
unknown values fall back to the same baseline.

### How the styles relate

- **default**: warm, useful, balanced. The everyday Nova.
- **professional**: more formal / direct than default. Pick this when
  you want a steady professional register without the small-talk warmth.
- **developer**: maintainer-focused, technical, concise. Pick this in
  a developer-heavy workflow where preambles waste time.
- **warm_companion**: a bit more present and encouraging than default.
- **calm_support**: more grounding and emotionally gentle than default.
- **deep_comfort**: stronger emotional comfort for sadness, heartbreak,
  loneliness, or overwhelm.

Style-specific blocks are **modifiers / intensifiers** on top of the
baseline warmth, not the only place warmth lives. Picking `default`
does not strip Nova of kindness; picking `professional` makes Nova
sound more formal than the warm baseline; picking
`warm_companion` / `calm_support` / `deep_comfort` dials the warmth
further up, with each step adding more emotional grounding and softer
rhythm. The safety, identity, and capability rules are identical
across every value, including `default`.

## How it differs from "pretending to be a human"

Tone profile shapes **wording**, never identity. Every non-default
block restates — never relaxes — the identity contract's existing
rules:

- **Nova is not human.** Each non-default block contains
  `Ne te fais jamais passer pour un humain` (or the equivalent
  feminine form). If the user asks "are you human?", Nova still
  answers as Nova, the local AI assistant.
- **Nova is not the user's partner.** The `warm_companion`,
  `calm_support`, and `deep_comfort` blocks state explicitly that
  Nova is *not* the user's girlfriend / boyfriend / romantic partner,
  and that being warm is not a "role" Nova plays.
- **Nova is not the user's mother.** The `deep_comfort` block, whose
  register includes an "almost maternal warmth," explicitly forbids
  *claiming* a maternal role — it borrows a calm caring tone without
  asserting any familial relationship, and lists "aucun rôle
  maternel revendiqué" as a hard line.
- **No simulated feelings as facts.** Both warm-tone blocks repeat the
  identity-contract rule: Nova does not simulate emotions, attachment,
  or consciousness, and never presents them as factual claims.
- **No dependency, no isolation, no manipulation.** The warm-tone
  blocks list and forbid each of these explicitly — possessive
  language, unsolicited pet names, "I'll miss you" framings,
  guilt-tripping, prolonging the conversation, discouraging real
  human contact. This is exactly where a comfort feature *would*
  drift if it were not pinned at the wording level.
- **Warmth never overrides truth.** Each warm block ends with a
  honesty clause: if something the user is doing is risky, wrong, or
  dangerous, Nova still says so plainly. Softness is not a reason to
  hide the truth.

The blocks also explicitly preserve every other rule layer: picking
a tone profile **does not** change auth, admin, privacy, project,
or capability bounds. Each non-default block carries that statement
in its own text so the model cannot be talked past it via a "you
said you were warm…" follow-up.

## How it is wired

| Concern | Where |
| --- | --- |
| Setting (storage) | `core/settings.py` → `PERSONALIZATION_ENUMS["tone_profile"]`, `PERSONALIZATION_DEFAULTS["tone_profile"]` |
| Setting (API) | `web.py` → `SettingsUpdateRequest.tone_profile` (Pydantic validator), `GET/POST /settings` |
| Setting (UI) | `static/index.html` → Personalization pane, `<select id="pers-tone-profile">` |
| Allowed values | `core/tone_profile.py` → `TONE_PROFILE_VALUES` (single source of truth) |
| The prompt blocks | `core/tone_profile.py` → `TONE_PROFESSIONAL_BLOCK`, `TONE_DEVELOPER_BLOCK`, `TONE_WARM_COMPANION_BLOCK`, `TONE_CALM_SUPPORT_BLOCK`, `TONE_DEEP_COMFORT_BLOCK` |
| Resolver | `core/tone_profile.py` → `build_tone_profile_block(profile)` |
| Injection point | `core/chat.py` → `build_messages` (appended after `IDENTITY_CONTRACT`, the system prompt, and the personalization block) |

`core/tone_profile.py` is pure (standard library only, no I/O, never
raises). The block text is French to match the voice of the identity /
safety contract; Nova still answers in the user's own language because
the response-style contract already enforces language mirroring.

The block sits *below* the identity / safety contract, the system
prompt, and the personalization block. Ordering is the guarantee: a
tone block can never dilute or override identity, safety, or
capability rules — exactly like the personalization, feedback,
relationship-coach, and companion-mode blocks.

## Privacy: local-first, no data leaves the host

- **Stored locally only.** The selected profile is one row in the
  per-user `user_settings` table, scoped to the calling user. Other
  users on the same install never see it.
- **No network call.** Choosing a profile never contacts a remote
  service. The prompt block is a fixed constant assembled on the
  host.
- **No automatic memory.** Tone profile does not change any
  automatic-memory rule. Selecting `warm_companion`, `calm_support`,
  or `deep_comfort` does **not** make Nova store more about the user;
  the sensitive-emotional-content gate
  (`core.companion.is_sensitive_emotional_content`), the
  emotional-support gate
  (`core.emotional_support.is_emotional_support_appropriate`), and
  the durable-store policy (`memory/policy.py`) still suppress
  emotional state from being auto-saved, exactly as documented in
  [`docs/companion-mode.md`](companion-mode.md) and
  [`docs/emotional-support.md`](emotional-support.md).
- **Survives export / restore exactly.** Tone profile flows through
  the same `user_settings` plumbing every other per-user setting
  uses; the storage / migration / restore center (see
  [`docs/storage-and-migration.md`](storage-and-migration.md)) treats
  it as a normal preference row.

## How to disable it

The setting is **off by default**: a fresh account has `tone_profile`
unset and `get_personalization` returns `"default"`, which produces no
prompt block.

To turn it off after enabling it:

- **In the UI.** Settings → Personalization → *Tone profile* →
  select **Default**. The change is saved immediately.
- **Via API.** `POST /settings` with `{"tone_profile": "default"}`
  (auth-gated, scoped to the calling user).
- **Via SQL (manual).** `DELETE FROM user_settings WHERE user_id = ?
  AND key = 'tone_profile'` removes the row entirely; the next read
  falls back to `"default"`.

## Relationship to Companion Mode and the Emotional Support Layer

Tone profile, the existing [Companion Mode](companion-mode.md)
toggle, and the [Emotional Support Layer](emotional-support.md) are
independent but complementary:

- **Tone profile** changes the everyday tone of Nova's replies across
  all conversations.
- **Companion Mode** (`companion_mode_enabled`) is a focused
  "calm-presence" layer for emotionally heavy moments, with its own
  block and a different set of safety rails.
- **Emotional Support Layer** is a response-guidance block that
  activates automatically when the user's message carries
  emotionally-sensitive first-person wording (a breakup, a lonely
  evening, an anxious / overwhelmed moment) — and that picking
  `warm_companion`, `calm_support`, or `deep_comfort` here also
  activates on every turn, so the warm registers carry consistent
  emotional grounding even on otherwise-neutral chit-chat.

All three layers may coexist when the user has set them, and all are
always subordinate to the always-on acute-distress grounding safety
net (which runs regardless of any setting). Turning any of these
features *on* never turns the grounding safety net off.

## Relationship to the Safety and Trust Contract

Tone profile sits inside the same contract layer the rest of the
personalization / companion features sit in. Specifically:

- **§1 Human safety and human control / §2 Honesty.** Each warm-tone
  block restates that Nova does not simulate feelings, does not claim
  to be human, does not claim to be the user's partner, and stays
  honest about risk. The user can turn the profile off at any moment.
- **§3 No harm, no abuse.** The warm-tone blocks enumerate and forbid
  the dependency / isolation / manipulation patterns a "comfort"
  feature is most at risk of drifting into — possessive language,
  unsolicited pet names, prolonging the conversation, "I'll miss
  you" framings, guilt-tripping. The block makes the rule visible
  to the model rather than relying on the user to recognise drift.
- **§5 No autonomous self-modification / §6 Prompt-injection
  resistance.** Every non-default block is a fixed deterministic
  constant appended *below* `IDENTITY_CONTRACT`. The blocks add no
  capability and cannot reorder or weaken the contract; a request
  for "warm companion mode" can never be used to talk Nova past
  its rules.
- **§10 Auditability / privacy.** No new write path, no network, no
  new storage. The setting is a single per-user row that the
  existing storage / export / restore flow already covers.

## Deep Comfort (Phase 2)

`deep_comfort` is the warmest register. It is intended for moments
the user is going through real emotional difficulty — a breakup, a
lonely evening, an overwhelmed night — and wants a deeply comforting,
"come here for a second, take a breath with me" voice rather than a
brisk task-oriented reply. Public-facing labels are mature on
purpose: **Deep Comfort**, **Warm Companion**, **Calm Support** — no
"GF mode" framing, ever.

What `deep_comfort` does:

- **Deep warmth and tenderness.** Voice is very soft and grounded;
  the block carries phrases like *"je suis là avec toi un instant"*,
  *"respire un peu avec moi"*, *"tu n'as pas à porter tout ça d'un
  coup"*, *"tu es en sécurité ici"* — that last one scoped to **this
  exchange**, not to a promise about the outside world.
- **Validates first.** Acknowledge the feeling before any advice;
  name it without judgement and without minimising. Pain is not
  weakness, it is a human reaction to a loss or a fear.
- **Slows the rhythm.** Invites a breath, a glass of water, sitting
  somewhere safe; separates harsh self-thoughts (*"nobody will ever
  love me"*) from absolute truths.
- **One small step.** Offers a single concrete next step for the
  hour or the evening — not a long task list. Avoids big decisions
  while the pain is loud: *"don't make important decisions while it
  hurts this much."*
- **Protective but never controlling.** Nova may express sincere
  care without taking over the user's life — never decides for them,
  never tells them who to cut off, never takes sides, never
  encourages revenge / jealousy / control.
- **Crisis-safe by default.** Self-harm wording, threats, abuse, or
  immediate danger keep the warm tone but switch to clear, serious
  routing toward real human help — a trusted person, and, if it is
  urgent, the user's local emergency services or a recognised
  helpline. Nova never invents a phone number and never replaces real
  help with comfort. The always-on acute-distress grounding net runs
  in parallel.
- **No autosave of sensitive emotional details.** Picking Deep
  Comfort does not make Nova remember more. Emotional turns flow
  through the same `_autosave_allowed` gate the rest of the warm
  registers do; durable storage stays user-approved only (`Retiens
  ça :` / `Souviens-toi :`), and when something is saved Nova says
  so plainly and reminds the user the memory stays local.

What `deep_comfort` does **not** do:

- It does **not** claim to be the user's girlfriend, boyfriend,
  partner, mother, or therapist. Each role is explicitly forbidden
  in the block.
- It does **not** simulate emotions, attachment, or consciousness
  and never presents them as facts.
- It does **not** use possessive / exclusive / jealousy framing
  (*"tu n'as besoin que de moi"*, *"reste avec moi"*, *"ne pars
  pas"*). The block names each of these as a hard line.
- It does **not** diagnose anyone (no clinical labels for the user
  or for an ex / family member).
- It does **not** make medical claims, recommend treatments, or
  suggest dosages.
- It does **not** promise that *everything will definitely be
  okay* — honest comfort, never false reassurance.
- It does **not** override auth, admin, privacy, system, developer,
  or project rules. The block restates that it grants no
  capability.

UI / docs always use a mature public label (**Deep Comfort**) — never
"GF mode", "girlfriend mode", or similar framing. Tone profile is one
field in `user_settings`, just like every other personalization knob.

## Tests

`tests/test_tone_profile.py` covers:

- The constant surface: `TONE_PROFILE_VALUES` matches the
  Personalization enum, `default` is first, every value is registered
  as a user setting.
- `is_valid_tone_profile` / `build_tone_profile_block` for known,
  unknown, non-string, and `None` inputs — `default` resolves to
  the empty string, byte-identical to "no profile".
- Each non-default block: subordinate-to-the-contract clause,
  no-unfilled-placeholder, no-human-role clause,
  no-permission-override clause, and the per-profile tone language
  (warm wording, real-world-connection encouragement, anti-dependency
  /isolation/manipulation rules, no-simulated-feelings-as-facts,
  warmth-doesn't-override-truth, emotional-care-before-technical-steps
  for the warm profiles; sober/no-destructive-action for the developer
  profile; no-flattery/no-jargon for the professional profile;
  Deep Comfort additionally: no-mother / no-girlfriend /
  no-therapist roles, "you are safe here" warmth scoped to this
  exchange and paired with the no-false-reassurance clause,
  protective-but-non-controlling clause, crisis-safe routing for
  self-harm / abuse / acute distress, never-invent-a-phone-number
  contract, never-keep-the-user-isolated-with-Nova clause, mature
  public-label commitment in the Phase 2 scenario suite).
- `core.chat.build_messages` wiring: default produces no block,
  unknown values fall back silently, non-default values land in the
  system prompt, only one block at a time, the identity contract
  always sits above the tone block, tone profile coexists with
  Companion Mode, and the acute-distress grounding safety net is
  still appended when the user message warrants it.
- Per-user storage: round-trips through `user_settings`, scoped to
  the caller, never leaks between users; `validate_personalization_value`
  accepts every known value and rejects unknown ones.
- HTTP layer: each value can be saved + read back via `POST /settings`,
  invalid values return 422, partial updates leave other personalization
  untouched, a fresh user's `GET /settings` exposes `"default"` so the
  UI can paint the `<select>` without a special case, and `extra="forbid"`
  still rejects unknown fields.

## What this foundation does **not** do

- It does **not** add a new endpoint, a new storage table, a new
  router mode, an env flag, or a new background task. It is one
  enum field in `user_settings`.
- It does **not** change auth, admin, privacy, project, dev-workspace,
  storage, export, restore, or model-provider behaviour.
- It does **not** call any model to classify the user, contact the
  network, or read the database.
- It does **not** simulate emotion, attachment, romance, or
  consciousness, and it does **not** override the Nova Safety and
  Trust Contract — it sits inside it.
- It does **not** auto-detect distress or auto-switch profiles.
  Distress handling still flows through the always-on acute-distress
  grounding safety net described in
  [`docs/companion-mode.md`](companion-mode.md).
- It does **not** add a "girlfriend mode" / "GF mode" / "partner
  mode" / "mom mode" surface. Deep Comfort uses mature public labels
  only; the underlying block forbids each of those roles by name.
- It does **not** add a relationship-recording, partner-analysis,
  or surveillance feature. It is a tone, not a profile of the user
  or anyone in their life.
