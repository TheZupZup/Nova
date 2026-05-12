# Memory pack import (v1)

Nova can ingest a curated **Markdown memory pack** so a user can bring
useful long-term context with them from another assistant without
re-teaching everything by hand. The import flow is **local-only**,
**preview-first**, and **never saves anything without explicit user
confirmation**.

This document describes the v1 backend contract implemented in
[`core/memory_importer.py`](../core/memory_importer.py). UI / API
wiring is intentionally out of scope for v1; the helpers below can be
exercised programmatically or from a future Settings panel.

## What a memory pack is

A memory pack is a small, human-readable Markdown file. The top-level
heading (`#`) is a title; each second-level heading (`##`) is a memory
**category**; bullet points beneath a category become individual
**memory entries**.

```markdown
# Nova Memory Pack

## Git workflow
- The user wants main to stay stable.
- The user uses feature/*, fix/*, and hotfix/* branches for PRs.

## Response style
- The user prefers clear, direct, step-by-step answers.
- The user wants warnings before risky Git actions.

## Projects
- The user works on Nova, Auryn, SilentGuard, and NexaNote.
```

Parsing rules:

- `## Heading` opens a category. The text after `## ` is used verbatim.
- `# Title` resets the current category — bullets immediately after a
  top-level heading are ignored until the next `## Heading`.
- Bullets must start with `- ` (dash, space). Anything else is ignored.
- Entries shorter than 10 characters are rejected as too short.
- Empty bullets, content before the first `##`, and free prose are
  ignored.

## Local-only behaviour

The importer never reaches the network. It accepts Markdown text in
memory and returns Python data structures. There is **no** cloud
connector, **no** OAuth, **no** ChatGPT/Claude account integration,
**no** background import, and **no** raw conversation ingest in v1.

The parser, safety scanner, and preview pipeline are pure functions
with no dependency on SQLite or the file system. Persistence is
delegated to a caller-supplied `save_fn`, so callers control where —
and whether — data lands.

## Preview before save

`build_memory_import_preview(text, existing_contents=None)` returns a
`MemoryImportPreview` containing:

- `candidates: list[MemoryImportCandidate]` — every reviewable entry,
  with `category`, `content`, `source="import"`, `priority="normal"`,
  a `flags` tuple, and a `duplicate` boolean.
- `total`, `categories`, and counts for rejected / duplicate / flagged
  entries.
- `warnings` — short, human-readable strings describing the rejected
  count, duplicate count, empty input, or "no valid candidates found".

The preview is deterministic: the same input produces the same output.
**Nothing is written to the database at this stage.** A caller can
display the preview, let the user toggle entries, and only then call
the commit helper below.

## Safety flags

Each candidate carries a `flags: tuple[str, ...]` produced by the
deterministic scanner in `scan_content_for_flags()`. Multiple flags
can fire on the same entry. Possible values:

| Flag | Meaning |
|---|---|
| `possible_password` | Content mentions `password`, `passwd`, or `passphrase`. |
| `possible_token` | Content mentions an API key, access token, bearer token, or contains a long alphanumeric run. |
| `possible_private_key` | Content mentions `private key`, `private_key`, or a PEM/OpenSSH header. |
| `possible_secret` | Umbrella flag — set whenever any of the three above fires, or when `secret` / `credential` appear. |
| `possible_sensitive_personal_data` | Content matches an email, phone number, SSN, or credit-card-shaped pattern. |
| `suspicious_string` | A whitespace-free alphanumeric run of 20+ characters was found. |
| `duplicate` | Content matches a memory already in `existing_contents` (case- and whitespace-insensitive). |

The detection is intentionally keyword- and regex-based — **no ML or
LLM is involved**. False negatives are preferred over false positives
in a review-before-save workflow; users still see the entry and
decide.

The importer never imports passwords, tokens, API keys, private keys,
or credentials automatically. Any candidate with a non-`duplicate`
safety flag is skipped at commit time unless the caller explicitly
opts in via `allow_flagged=True`.

## Explicit confirmation step

`commit_memory_import(preview, user_id, save_fn, confirm=False,
allow_flagged=False, allow_duplicates=False)` is the only function in
this module that can cause data to be written. It returns a
`MemoryImportCommitResult` summarising:

- `saved_count` — entries actually persisted via `save_fn`.
- `skipped_flagged` — flagged entries skipped because
  `allow_flagged=False`.
- `skipped_duplicate` — duplicates skipped because
  `allow_duplicates=False`.
- `skipped_unconfirmed` — set to `preview.total` when `confirm=False`,
  signalling that no save was attempted.

Rules:

1. With `confirm=False` the function is a no-op. `allow_flagged` /
   `allow_duplicates` have no effect on their own — confirmation is
   always required.
2. With `confirm=True`, clean candidates are passed to `save_fn` as
   `save_fn(category, content, user_id)`.
3. Flagged and duplicate candidates are skipped by default. The caller
   must enable each opt-in explicitly to import them.

A typical wiring looks like this:

```python
from core.memory import save_memory, list_memories
from core.memory_importer import build_memory_import_preview, commit_memory_import

existing = [m["content"] for m in list_memories(user_id)]
preview = build_memory_import_preview(markdown_text, existing_contents=existing)

# Show preview.candidates / preview.warnings to the user, let them confirm.

result = commit_memory_import(
    preview,
    user_id=user_id,
    save_fn=save_memory,
    confirm=True,                # set only after a real user confirms
    allow_flagged=False,         # never silently save sensitive entries
    allow_duplicates=False,
)
```

## Non-goals for v1

The following are **explicitly out of scope** for this iteration and
should not be added to the importer module:

- Direct connection to ChatGPT, Claude, or any cloud account.
- Importing whole raw conversation transcripts.
- Background / scheduled imports.
- Automatic saving of any entry without a confirmation step.
- Storing passwords, tokens, API keys, or private keys.
- ML- or LLM-based safety classification.

Future work — a Settings UI, a paste-text endpoint, multi-pack import
history — should build **on top of** the contract above, not replace
it.
