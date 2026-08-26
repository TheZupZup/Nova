# Model profiles — Nova as a model platform

> **Status: shipped.** Purely additive. An existing install upgrades with
> identical behaviour: no migration, no new file, no new required
> configuration. Every `NOVA_*_MODEL` variable keeps working exactly as
> before.

Nova is model-flexible by design — the operator installs the models they
want and points each routing role at one of them. Until now a model was
only a *name*: a string handed to Ollama. That is enough to run a model
and not enough to reason about one. Nova could not size a code-context
block, could not tell a 1B classifier from a 32B reasoner, and could not
describe what it was running.

A **model profile** is the small missing piece.

## What a profile describes

| Field | Meaning |
| --- | --- |
| `name` | The model as the backend knows it (`qwen2.5-coder:14b`). |
| `role` | Which of Nova's four roles it is filling. |
| `context_size` | The context window Nova is willing to *recommend* for it. |
| `supports_tools` | Whether the model was trained for tool syntax. |
| `code_specialized` | Whether it is a coding model. |
| `resource_class` | `tiny` / `small` / `medium` / `large` / `xlarge` / `unknown`. |
| `parameter_size_b`, `quantization` | Parsed from the tag when present. |
| `notes` | Free-form operator note. |
| `source` | `builtin`, `override`, or `unknown`. |

A profile is a **description, never a permission.** `supports_tools`
records a claim about a model; it enables nothing. Routing, per-user
model access, family controls, and the
[Safety and Trust Contract](nova-safety-and-trust-contract.md) are
unchanged by anything a profile says. There is no field a profile could
carry that would widen what Nova will do.

## The four roles

Role names map onto the existing configuration one-to-one:

| Role | `config.MODELS` key | Environment variable |
| --- | --- | --- |
| `router` | `router` | `NOVA_ROUTER_MODEL` |
| `chat` | `default` | `NOVA_DEFAULT_MODEL` |
| `code` | `code` | `NOVA_CODE_MODEL` |
| `advanced` | `advanced` | `NOVA_ADVANCED_MODEL` |

`chat` is simply a friendlier label for the `default` key. The chat role
also honours the admin-selected default model persisted in
Settings → Models, exactly as `core.router` and the chat endpoints
already do.

## How a profile is resolved

1. **Operator overlay** — an entry in the optional JSON file wins.
2. **Builtin family table** — a short prefix table (`nova-coder`,
   `qwen2.5-coder`, `deepseek-coder`, `codellama`, `qwen2.5`, `llama3`,
   `gemma3`, …) plus parameter-size parsing from the tag.
3. **Conservative unknown** — anything unrecognised gets safe defaults
   and is labelled `source: "unknown"` so the admin surface can say so.

This is deliberately **not** a model database. It is a heuristic that
covers what people actually run locally and degrades calmly rather than
growing without bound.

Resolution is offline and never raises: `core/model_profiles.py` imports
no HTTP client, no `subprocess`, and no backend library. It cannot
trigger a download.

## The optional operator overlay

Set `NOVA_MODEL_PROFILES_PATH`, or drop a `model-profiles.json` beside
Nova's other data:

```json
{
  "profiles": [
    {
      "name": "nova-coder:14b",
      "role": "code",
      "context_size": 32768,
      "supports_tools": true,
      "code_specialized": true,
      "resource_class": "medium",
      "notes": "Nova coding derivative, internal build 2026-06"
    }
  ]
}
```

The file is read-only, size-capped (256 KiB), entry-capped, and
field-validated. A missing file, malformed JSON, or an invalid entry
degrades to "no override for that model" — never an error, never a
partial write. Nova never creates or modifies this file.

## Where profiles are used

- `GET /admin/models/profiles` (admin-only) renders the resolved profile
  per role, the overlay path, and whether the overlay was found.
- `GET /admin/models/health` reports Nova's recommended context size
  alongside the one the runtime actually reports — see
  [model-health.md](model-health.md).
- The evaluation harness records the context size assumed for each model
  so a comparison between two models is honest about what each was given
  — see [model-evaluation.md](model-evaluation.md).

## <a id="nova-coder"></a>How a future `nova-coder` plugs in

This is the point of the abstraction. A Nova-specific fine-tuned coding
model — a LoRA or full fine-tune of an existing open model, packaged for
the local runtime — needs **no Nova code change** to be adopted:

1. The operator builds and installs the model themselves, under a name
   like `nova-coder:14b`. Nova never downloads it.
2. They point the existing variable at it:

   ```
   NOVA_CODE_MODEL=nova-coder:14b
   ```

3. The `nova-coder` family is already in the builtin table, so it
   resolves to a coding-specialised, tool-capable, large-context profile.
   Anything they want to say differently goes in the overlay file.
4. `GET /admin/models/health` immediately reports whether it is
   installed and loaded; the evaluation harness can immediately compare
   it against whatever they were using before, on the same cases.

Nothing about that path is special-cased. `nova-coder` is one model
among others, and Nova stays model-flexible: swapping it out is one
environment variable.

The dataset side of that story — how the *training data* for such a
model would be produced, safely and only with explicit approval — is
[nova-coder-dataset.md](nova-coder-dataset.md).
