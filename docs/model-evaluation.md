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

- the **model** that actually produced the answer (and, separately, the
  label that was requested — see [Single-model backends](#single-model-backends)),
- the **context size the runtime reported** — observed, never assumed;
  see [below](#what-the-context-field-means),
- **elapsed time** in milliseconds,
- **success / failure**,
- **which requested constraints were followed**, per constraint, with a
  short reason for each miss,
- the **raw output**,
- and an optional **human rating** (1-5) plus note, added later.

Runs and results live in two additive SQLite tables in Nova's own
database (`model_eval_runs`, `model_eval_results`). They are created
automatically on the next start; there is nothing to migrate.

## <a id="what-the-context-field-means"></a>What the context field means

`context_size` on a result is **observed, not assumed**. For Ollama it is
read from the loaded model's own `/api/ps` report after generation; a
provider that cannot report one stores `null`.

It is deliberately *not* taken from the model's
[profile](model-profiles.md). A profile's `context_size` is Nova's
recommendation, and no provider applies it to a request — the Ollama
provider calls `client.chat(model, messages)`, and llama.cpp uses its own
process-wide `n_ctx`. Recording a recommendation as though it were the
window the model was actually given would be a fabricated comparison
field, which is exactly what a benchmark must not have.

So read `null` as *"the runtime did not say"* — **not** as "the profile
size was used".

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
| `must_match` | The value, as a regex, matches. Patterns are screened for catastrophic backtracking first (see below). |
| `must_include_code_block` | The output has a **complete** fenced block. An opening fence may carry a language; a closing fence may not, so two openers (```` ```python ```` then ```` ```javascript ````) do not count, and neither does a lone ```. |
| `must_mention_file` | The named path is referenced. |
| `max_chars` / `min_chars` | The output is within the bound. |

`must_contain`, `must_not_contain`, `must_match` and `must_mention_file`
**require a non-empty value**. An omitted one is a malformed case, not a
permissive one: `"" in text` holds for every response and `re.search("")`
matches at position zero, so the case would report `all_passed`
regardless of what the model said — quietly corrupting model comparisons
and able to promote a worthless answer into an approved training
example. Such a case is refused at load time, naming the constraint.

A case passes only when **every** constraint passes and the backend
returned usable output.

#### Why `must_match` patterns are screened

Python's regex engine backtracks, and a match cannot be interrupted: it
is a single C call holding the GIL, so a runaway one cannot be joined,
cancelled, or signalled. A pattern like `(a+)+$` takes exponential time
against input that *almost* matches — 41 characters is already enough to
outlast a 12-second timeout — and an evaluation worker that entered one
would be gone for the life of the process.

That blowup needs **ambiguity**: somewhere the match boundary can land
in more than one place, so a failure forces the engine to go back and
try the others. Because that is a
structural property, it is decided before the pattern is ever run.

The property that matters is **variable length**, not "open-ended". A
bounded repeat is just as ambiguous when it can give ground: every `a?`
in `^a?a?a?…a{30}$` is an independent match-or-skip decision, so thirty
of them is 2³⁰ assignments to work through before failure can be
reported — and that fits comfortably inside the 500-character value cap.

Repetition is not the only way to get it. A **branch** whose
alternatives end in different places is a choice point too:
`^(?:a|aa)(?:a|aa)…b$` contains no repetition at all, and thirty of
those branches is 2³⁰ combinations — measured at **18 seconds** against
45 characters. So the budget below counts choice points of either kind.

A pattern is refused when it contains:

- a repetition inside another repetition — `(a+)+`, `(a*)*`;
- alternation or a look-around inside a repetition — `(a|aa)+`;
- a back-reference — `(.*)\1`;
- two variable-length repetitions in a row that can compete for the same
  characters — `.*.*`, `\w+\w*`, `a?a?`;
- more than two variable-length repetitions competing for the same
  characters anywhere in a sequence — `.*a.*b.*`, or `^a+aa+aa+a…$`,
  where each `a+` is separated by a literal the repetitions on both
  sides also match, so the boundary between them slides freely and they
  compete despite not being neighbours;
- more than eight ambiguous choices in total, counting both
  variable-length repetitions and branches whose alternatives can match
  at the same place and end in different ones — `a|aa`.

Four things are deliberately **not** refused. Disjoint neighbours:
`\s+\w+` cannot backtrack, because no character belongs to both
classes, and it is the most common shape in a real constraint.
Separators the repetitions cannot match: `\d+\.\d+` is two independent
repetitions, because `.` is not a digit. Fixed repeats: `a{3}` cannot
give ground, so it adds no ambiguity. And unambiguous alternation:
`foo|bar` and `cat|car` always end in the same place, while
`(?:GET|POST|DELETE)` has alternatives of different lengths that cannot
begin at the same character, so at most one can ever match. Ordinary
patterns — `def\s+\w+`, `\d+\.\d+`, `foo|bar`, `.*error.*`,
`(?:GET|POST)\s+/\w+`, `^\s*def\s+\w+\s*\(` — are unaffected.

### What the screen does *not* promise

It bounds the exponent, not the wall clock. Two competing repetitions
are permitted because patterns like `.*error.*` are ordinary and
useful — but two competing repetitions are still quadratic, and against
a maximum-length 40,000-character response that is measurable:
`.*error.*` takes about **5.7 seconds** and `\w+@\w+\.\w+` about
**3.8 seconds** on non-matching input of that size.

That is slow, not unbounded — it terminates, and it is the price of
accepting patterns operators genuinely write. What the screen removes is
the class that does *not* terminate. A hard wall-clock bound is not
available in-process: `re.search` is one C call holding the GIL, so no
watchdog thread runs and `signal.setitimer` works only on the main
thread. Enforcing one would mean matching behind a process boundary,
which this module deliberately does not have — see the note on spawning
in its docstring.

A refused pattern is reported when the case file loads, naming the
reason. Building a `Constraint` directly is screened too, and fails
closed with `passed: false` rather than raising — `check()` never
raises, by contract.

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
