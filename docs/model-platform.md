# Nova as a local model platform — architecture

> **Status: shipped.** Additive throughout. No migration is required to
> start Nova after this change, and every `NOVA_*_MODEL` variable keeps
> working exactly as before.

This is the map of how Nova stopped treating a model as an
interchangeable string and became a platform for **running, evaluating,
and eventually specialising** local models — without becoming an
autonomous agent.

## The shape of it

```
                       ┌──────────────────────────────┐
   NOVA_ROUTER_MODEL   │      core/model_profiles     │
   NOVA_DEFAULT_MODEL ─▶  role → ModelProfile         │◀── model-profiles.json
   NOVA_CODE_MODEL     │  (context, tools, class, …)  │    (optional overlay)
   NOVA_ADVANCED_MODEL └──────────┬───────────────────┘
                                  │ describes
        ┌─────────────────────────┼──────────────────────────┐
        ▼                         ▼                          ▼
 core/model_health        core/chat + code_context     core/model_eval
 installed / loaded /     Code-mode grounding from     same cases across
 runtime context /        the read-only Dev            models; deterministic
 honest errors            Workspace                    text-only scoring
        │                         │                          │
        │                         │                          ▼
        │                         │                 operator approves
        │                         │                 individual results
        │                         │                          │
        ▼                         ▼                          ▼
  /admin/models/health      system prompt, below      core/coder_dataset
                            the safety contract       JSONL, local file only
```

Five small modules, no framework, no new runtime dependency.

## 1. `core/model_profiles.py` — a model is a profile

The four roles (`router`, `chat`, `code`, `advanced`) map one-to-one onto
the existing `config.MODELS` keys and env vars; `chat` is a friendlier
label for `default`. Each resolves to a `ModelProfile` describing the
model: recommended context size, tool-use, coding specialisation,
resource class, parsed parameter size and quantisation, operator notes,
and where the values came from.

Resolution is overlay → builtin family table → conservative unknown. It
performs no I/O beyond reading one optional local JSON file, imports no
HTTP client and no `subprocess`, and never raises.

**A profile grants nothing.** It has no field that could enable an
action, and routing, model access control, family controls, and the
Safety and Trust Contract are unchanged by anything it says.

Details: [model-profiles.md](model-profiles.md).

## 2. `core/code_context.py` — Code mode that can see

Code mode used to mean only "use `NOVA_CODE_MODEL`". Now, when a project
has a linked Dev Workspace repository **and** the user explicitly picks
Code mode, `web._resolve_code_context` builds a bounded read-only
briefing and `core.chat` inserts it below the identity contract.

Two new read-only primitives live in `core/dev_workspace.py`:
`git_tracked_files` (index-only discovery via the allowlisted
`git ls-files`) and `read_text_snippet` (one validated, capped,
non-secret, text-only file excerpt).

The boundaries that make this safe are unchanged Dev Workspace rules
plus three new ones — Nova picks the files deterministically (the model
cannot request one), everything is capped so there is no repository
dump, and the block is framed as untrusted data.

Details: [dev-workspace.md](dev-workspace.md#code-mode-grounding-uses-phase-1-adds-no-new-power).

## 3. `core/model_health.py` — what the runtime is actually doing

Installed vs loaded vs not-installed per role, the context size the
runtime reports (never invented), and machine-readable error codes.
Read-only `/api/tags` + `/api/ps` + `/api/show`; no downloads, no vendor
SDK, no `subprocess`, graceful degradation on non-Ollama providers.

Surfaced to operators as the admin-only **Settings → Models → "Model
roles & health"** card, rendered from one call to
`GET /admin/models/health`, with a Refresh button and no write control
of any kind.

Details: [model-health.md](model-health.md).

## 4. `core/model_eval.py` — measuring instead of guessing

Local JSON cases, deterministic text-only constraint checks, two
additive SQLite tables, a background runner, per-model summaries, and an
optional human rating. **Model output is never executed** — there is no
constraint kind that could run anything, and the module spawns no
process and touches no repository.

Details: [model-evaluation.md](model-evaluation.md).

## 5. `core/coder_dataset.py` — the gate before the model

An explicit, per-example, operator-approved JSONL export of evaluation
results. No bulk path, no background collection, no upload. Chat
conversations, memory, and system/safety prompts are unreachable from
it; secret-shaped content refuses the whole export.

Details: [nova-coder-dataset.md](nova-coder-dataset.md).

## What did **not** change

- Routing. `core.router.MODEL_MAP` and `web.MODE_MAP` are byte-for-byte
  the same, and a test pins both.
- The four environment variables, their defaults, and the admin-selected
  default model.
- The database schema for anything that already existed; the two new
  tables are additive and created idempotently on start.
- Any safety boundary. Nothing here executes a command, writes to a
  repository, downloads a model, weakens authentication or family
  controls, crosses a project boundary, or gives a model filesystem
  access.

## The `nova-coder` end state

The point of all five pieces is that adopting a Nova-specific fine-tuned
coding model is *boring*:

```
NOVA_CODE_MODEL=nova-coder:14b
```

The profile resolves (the family is already known), health reports
whether it is installed and loaded, and the evaluation harness compares
it against whatever it replaced on the same cases. Nova never downloads
it, and Nova stays model-flexible — swapping it back out is the same one
variable.
