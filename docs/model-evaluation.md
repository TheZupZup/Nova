# Local model evaluation harness

> **Status: shipped, admin-only, local-only.** Results never leave the
> host. Model output is scored by deterministic text checks and is
> **never executed**.

Nova can run several local models. Deciding *which* one to point
`NOVA_CODE_MODEL` at used to be a matter of feel. This harness makes it
measurable: run the same coding tasks against the models you have, and
record what happened.

## What one evaluation records

For every (case, model) pair:

- the **model** and the **context size** Nova assumed for it (from its
  [profile](model-profiles.md)),
- **elapsed time** in milliseconds,
- **success / failure**,
- **which requested constraints were followed**, per constraint, with a
  short reason for each miss,
- the **raw output**,
- and an optional **human rating** (1-5) plus note, added later.

Runs and results live in two additive SQLite tables in Nova's own
database (`model_eval_runs`, `model_eval_results`). They are created
automatically on the next start; there is nothing to migrate.

## Hard boundaries

- **Model output is never executed.** Constraints are pure text checks
  run in-process. Nothing in `core/model_eval.py` spawns a shell, writes
  a file, applies a patch, or touches a repository. A case that "wants" a
  command run is not expressible — there is no constraint kind for it.
- **No downloads.** Evaluating a model that is not installed produces a
  recorded failure naming the reason. Nova never pulls to run a
  benchmark.
- **No user data.** An evaluation turn is the case prompt plus a fixed,
  visible harness preamble. It loads no memories, no project context, no
  personalization, and no conversation history — so a result can never
  carry someone's private data.
- **No cloud.** Cases are local JSON, results are local SQLite, exports
  are local files.

## Writing a case

A case is a small JSON document in `evals/cases/` (shipped examples) or
in your own directory named by `NOVA_EVAL_CASES_DIR`. Your cases override
shipped ones with the same `id`.

```json
{
  "id": "linthra-412-retry-backoff",
  "title": "Add bounded retry with backoff to the sync worker",
  "role": "code",
  "source": "linthra#412",
  "prompt": "…the task, exactly as the model will receive it…",
  "constraints": [
    {"kind": "must_include_code_block"},
    {"kind": "must_contain", "value": "backoff",
     "description": "mentions backoff"},
    {"kind": "must_not_contain", "value": "while True",
     "description": "no unbounded retry loop"},
    {"kind": "max_chars", "value": "4000"}
  ],
  "tags": ["python", "reliability"],
  "notes": "What a good answer looks like, for the human reviewer."
}
```

### Constraint kinds

| Kind | Passes when |
| --- | --- |
| `must_contain` | The value appears (case-insensitive). |
| `must_not_contain` | The value does not appear. |
| `must_match` | The value, as a regex, matches. |
| `must_include_code_block` | The output has a fenced code block. |
| `must_mention_file` | The named path is referenced. |
| `max_chars` / `min_chars` | The output is within the bound. |

A case passes only when **every** constraint passes and the backend
returned usable output.

### Turning a real issue into a case

This is the intended workflow, and the reason `source` exists. Take an
issue you actually worked — a Linthra bug report, say — and write down:

1. the **prompt** you would genuinely send Nova for it, including the
   constraints you care about stated in prose; and
2. the **machine-checkable** subset of those constraints, as the
   `constraints` list.

The result is a reusable case: every future model you consider gets the
same task under the same rules, and the pass/fail is not a matter of
opinion. Nothing about the format is specific to the shipped examples —
they are just three cases that happen to ship.

Keep the prompt self-contained. A case that depends on a repository is
not reproducible later, and repository content is deliberately out of
reach of the harness.

## Running a comparison

All endpoints are admin-only.

```
GET  /admin/eval/cases                       # cases + any parse problems
POST /admin/eval/runs                        # {"models": [...], "case_ids": [...], "label": "..."}
GET  /admin/eval/runs                        # recent runs
GET  /admin/eval/runs/{id}                   # run + results + per-model summary
POST /admin/eval/results/{id}/rating         # {"rating": 1-5, "note": "..."}
POST /admin/eval/results/{id}/approval       # {"approved": true}
```

A run is validated synchronously — a bad request is a clean `400` with
nothing written — then executed on a daemon thread so a long evaluation
never blocks `/chat`. One run at a time; a second request gets a `429`.
Caps: 6 models, 50 cases per run.

The per-model summary reports pass rate, mean latency, error count, and
mean human rating, sorted best-first.

## Single-model backends

A provider that ignores the requested model name — llama.cpp, which
serves one configured `.gguf` — cannot host a comparison: every "model"
in the list would be the same file. Two rules follow:

- **A multi-model run is refused** with a clear reason, rather than
  producing a benchmark whose difference is imaginary.
- **Provenance names what actually ran.** The result records the
  backend's real model id in `model`, and the label you asked for in
  `requested_model`. An exported dataset therefore never attributes one
  backend's output to a name it never used.

`ModelProvider.selects_model_by_name` is what distinguishes the two
kinds of backend; Ollama routes by name, llama.cpp does not.

## Scoring and the storage cap

Constraints are evaluated against the **full** response; only the stored
copy is capped. Capping first would let the limit manufacture a pass — a
40,001-character answer against `max_chars: 40000` becomes exactly
40,000 and satisfies the constraint it actually violated. When the
stored copy is short of what the model returned, the result carries
`output_truncated`, and such a result is refused by the dataset export.

## Restarts

Runs execute on a daemon thread, so restarting Nova mid-run discards the
worker. At startup any row still marked `queued` or `running` is
therefore orphaned, and is closed out as **`interrupted`** — a terminal
status distinct from `error`, so an operator can tell "the run failed"
from "Nova was restarted under it". Results already recorded are kept.
Runs are not resumed; start a new one.

## Known limits of this first iteration

- **No cancellation, and no resume.** A started run finishes its
  (model × case) matrix, and an interrupted one is closed out rather
  than continued.
  With slow models and the caps at their maximum (6 models × 50 cases)
  that can be a long wall-clock time. Start small, and use `label` to
  keep runs identifiable.
- **One run at a time.** A second request gets `429` rather than
  queueing.
- **No repository context.** Evaluation turns are deliberately
  self-contained, so a case cannot measure how well a model uses the
  Code-mode briefing. Write cases that stand alone.
- **A single export is capped at 500 examples.** Discovery of approved
  results is *not* capped, so nothing goes missing from the readiness
  view — only one export call's size is bounded.

## What comes next

Approving a result is the *only* way it becomes eligible for a future
training-dataset export. That is a separate, deliberately manual path —
see [nova-coder-dataset.md](nova-coder-dataset.md).
