# Relationship Situation Coach

> **Status: shipped (foundation), local-first.** This document
> describes the non-clinical "situation coach" that helps the user
> answer an emotionally sensitive relationship message calmly and
> respectfully. It lives inside the boundaries set by
> [`docs/nova-safety-and-trust-contract.md`](nova-safety-and-trust-contract.md);
> nothing here grants Nova new powers, contacts the network, or
> changes storage / migration / Ollama behaviour.

## What it does

Some of the hardest things people ask an assistant are not technical:
*"My partner said this — how do I respond without making it worse?"*
Nova should be able to help with that the same calm way it helps with
code or security.

When the user's message is clearly about a sensitive relationship
situation, Nova appends a small, fixed **coach block** to the system
prompt. The block shapes *how* Nova answers this one topic — it does
not add any capability. It frames Nova as a calm, **non-clinical**
situation coach:

- **not a therapist**, not a mediator, not a judge;
- no diagnosis of the user and **no diagnosis of the partner**
  ("narcissist", "toxic", "bipolar" — describe behaviour, not labels);
- if the situation suggests danger, violence, or serious distress,
  Nova calmly points toward appropriate human or professional help
  instead of role-playing a counsellor.

### The method it offers

The block proposes (never imposes) a small, repeatable method:

1. **Summarise** what happened, in facts, without judgement.
2. Surface two or three **possible interpretations** — explicitly as
   hypotheses, never as the truth. No mind-reading of the partner.
3. Help choose a **calm, grounded response**.
4. Avoid **accusatory** ("you always…") or **needy / pleading**
   wording; prefer "I" statements and a clear request.
5. Help set and keep **healthy boundaries** without punishing the
   other person.
6. Help decide whether to **speak now or wait** until calmer.

### Response styles

The user can ask for a tone; the default is *neutral*:

- **soft** — warm and reassuring, gentle on the other person while
  still honest;
- **neutral** — factual and composed, neither cold nor distant;
- **direct but respectful** — clear and firm about the need, with no
  aggression or contempt.

### Hard safety rules

These are stated in the block and are non-negotiable:

- no manipulation or emotional blackmail;
- no coercion (never push to force a reply, presence, or contact);
- no gaslighting (never suggest denying the other person's reality or
  feelings);
- no revenge, retaliation, or punitive power-play advice;
- no diagnosing the partner;
- always steer toward **calm communication, mutual respect, and
  consent**.

## How it is wired

| Concern | Where |
| --- | --- |
| Topic detection | `core/relationship_coach.py` → `is_relationship_coach_query` |
| The prompt block | `core/relationship_coach.py` → `build_relationship_coach_block` / `RELATIONSHIP_COACH_BLOCK` |
| Injection point | `core/chat.py` → `build_messages` (appended last) |
| Auto-save suppression | `core/chat.py` → `_autosave_allowed`; `memory/policy.py` |

Detection is **conservative**: only relationship-specific multi-word
phrases match (EN + FR — Nova is bilingual). A bare word like "reply"
or "elle" never trips the coach, so an unrelated email or support
question is unaffected and pays zero token cost. The block is a fixed
constant — no LLM in the loop, deterministic, byte-identical every
time — and `build_messages` adds it **after** `IDENTITY_CONTRACT` and
the safety / security blocks. Ordering is the guarantee: a coaching
request can never dilute or override Nova's identity or safety rules,
exactly like the personalization and feedback blocks.

The block text is French to match the voice of the identity / safety
contract; Nova still answers in the user's own language because the
response-style contract already enforces language mirroring.

## Privacy: relationship detail is never auto-saved

This is the part that matters most. Relationship situations are
private, so:

- **No automatic memory.** When the user message carries sensitive
  relationship detail (`is_sensitive_relationship_content`), Nova
  **skips automatic memory extraction for that turn entirely** — both
  the LLM-extraction path and the rule-based natural-memory path.
  Nova never silently records who the user is dating, fighting with,
  or breaking up with.
- **Defence in depth.** `memory/policy.py` independently rejects
  sensitive relationship content from the durable natural-memory
  store, using the same single-source predicate, so even a future
  caller that bypasses the chat guard cannot auto-persist it.
- **User-approved only.** The one way a relationship fact is stored is
  the explicit manual memory command (`Retiens ça:` /
  `Souviens-toi:`). That command is handled in the web preflight,
  *before* the chat path, and is intentionally **not** affected by the
  auto-save gate — explicit consent is the whole point.

So: *store only user-approved relationship memories; never save
sensitive relationship details without confirmation.*

## What this foundation does not do

- It does **not** add a new router mode, endpoint, setting, or env
  flag. It is always available and entirely local; an unconfigured
  install behaves identically until the user actually describes a
  relationship situation.
- It does **not** call any model to classify the message, contact the
  network, or read the database.
- It does **not** provide clinical, legal, or crisis services, and it
  says so.
- It does **not** override the Nova Safety and Trust Contract — it
  sits inside it.

## Tests

`tests/test_relationship_coach.py` covers detection (bilingual,
conservative, non-string safe), the sensitive-content gate, the block
content (method, the three styles, every safety rule, the privacy
rule, non-clinical framing), the `memory/policy.py` hardening, and the
`core/chat.py` wiring (block injected only on a coach query, always
below `IDENTITY_CONTRACT`, and the `_autosave_allowed` guard).
