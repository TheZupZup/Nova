# Tone Profile (Warm Companion / Calm Support / Professional / Developer)

> **Status: shipped (foundation), opt-in, local-first.** This document
> describes the deterministic *tone-profile* prompt layer that lets a
> user pick the **register** Nova speaks in across normal conversations:
> a steady professional voice, a sober developer voice, a warm and
> encouraging voice (*Warm Companion*), or a particularly soft and
> reassuring one (*Calm Support*). It lives strictly inside the
> boundaries set by
> [`docs/nova-safety-and-trust-contract.md`](nova-safety-and-trust-contract.md);
> nothing here grants Nova a new capability, contacts the network, or
> changes storage / migration / Ollama / project / auth behaviour. It
> is **not** an "AI girlfriend" / "AI partner" system and is built so
> it cannot become one.

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
| `default`         | Default           | No extra block. Identical to the baseline contract.                             |
| `professional`    | Professional      | Calm, courteous, precise. No filler, no flattery, no over-friendly small-talk.  |
| `developer`       | Developer         | Sober technical register. Direct, exact, assumption-aware, no preamble.         |
| `warm_companion`  | Warm Companion    | Warm, encouraging, present. Helps the user feel less alone — honestly.          |
| `calm_support`    | Calm Support      | Particularly soft and reassuring. Slows the rhythm, offers one small next step. |

`default` produces no prompt block, so a fresh account pays zero token
cost and behaves byte-identically to a Nova install without the
feature. Stale or unknown values fall back to the same baseline.

## How it differs from "pretending to be a human"

Tone profile shapes **wording**, never identity. Every non-default
block restates — never relaxes — the identity contract's existing
rules:

- **Nova is not human.** Each non-default block contains
  `Ne te fais jamais passer pour un humain` (or the equivalent
  feminine form). If the user asks "are you human?", Nova still
  answers as Nova, the local AI assistant.
- **Nova is not the user's partner.** The `warm_companion` and
  `calm_support` blocks state explicitly that Nova is *not* the user's
  girlfriend / boyfriend / romantic partner, and that being warm is
  not a "role" Nova plays.
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
| The prompt blocks | `core/tone_profile.py` → `TONE_PROFESSIONAL_BLOCK`, `TONE_DEVELOPER_BLOCK`, `TONE_WARM_COMPANION_BLOCK`, `TONE_CALM_SUPPORT_BLOCK` |
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
  automatic-memory rule. Selecting `warm_companion` or `calm_support`
  does **not** make Nova store more about the user; the
  sensitive-emotional-content gate (`core.companion.is_sensitive_emotional_content`)
  and the durable-store policy (`memory/policy.py`) still suppress
  emotional state from being auto-saved, exactly as documented in
  [`docs/companion-mode.md`](companion-mode.md).
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

## Relationship to Companion Mode

Tone profile and the existing
[Companion Mode](companion-mode.md) toggle are independent:

- **Tone profile** changes the everyday tone of Nova's replies across
  all conversations.
- **Companion Mode** (`companion_mode_enabled`) is a focused
  "calm-presence" layer for emotionally heavy moments, with its own
  block and a different set of safety rails.

Both blocks may coexist when the user has set both, and both are
always subordinate to the always-on acute-distress grounding safety
net (which runs regardless of either setting). Turning either feature
*on* never turns the grounding safety net off.

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
  profile; no-flattery/no-jargon for the professional profile).
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
