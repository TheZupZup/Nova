# The `nova-coder` dataset path — opt-in, per-example, local

> **Status: export mechanism shipped. No model is trained, and no data
> is collected.** Nothing is ever exported automatically. There is no
> bulk export, no background collection, and no upload.

A Nova-specific fine-tuned coding model is a plausible future. Getting
there needs training data, and training data is exactly where a
local-first assistant can quietly betray its users. So the mechanism
ships before the model, with the gate built in first.

## The gate

A record is exportable only when **all** of these hold:

1. it is a [model evaluation](model-evaluation.md) result — an
   operator-authored task prompt and a model's answer to it, **never a
   user conversation**;
2. an operator explicitly set `approved` on that result, one result at a
   time, after reading the output; and
3. the operator names its id in the export request.

Approval is never inferred. A passing constraint check does not approve
anything. A thumbs-up does not approve anything. There is no
"export everything" call — `result_ids` is mandatory and the API rejects
an empty list.

## What can never be exported

- **Chat conversations.** No user message, assistant reply, or thread
  from `/chat` is reachable from the export path.
  `core/coder_dataset.py` imports no `sqlite3` and issues no SQL; it can
  only read what the evaluation module exposes.
- **System and safety prompts.** Nova's identity contract, the Safety
  and Trust Contract text, personalization blocks, and the evaluation
  harness preamble are all omitted. An exported example is a bare
  `user` / `assistant` pair.
- **Memory.** Neither structured nor natural memories are read.
- **Truncated answers.** A result whose stored output was cut by the
  storage cap is refused: an incomplete completion would teach an answer
  that stops mid-thought.
- **Credentials and personal data.** Every example is scanned for
  secret-shaped content — long hex strings, JWTs, GitHub/GitLab/AWS/
  OpenAI-style keys, `password=`-style assignments, PEM private-key
  headers, and bare email addresses. A match **refuses the whole
  export** rather than silently dropping the record, so an operator
  always finds out that something sensitive was in their data.
  The scan covers **everything written to the file**, including the
  provenance metadata — a case `source` naming a reporter's email
  would otherwise ride into the corpus straight past this gate. It
  is applied generically to every string field, so a field added
  later is covered without a code change.
- **Repository contents.** Code-mode repository briefings are built
  per-turn and never stored, so they cannot reach an export.

## The format

One JSON object per line:

```json
{
  "messages": [
    {"role": "user", "content": "…the case prompt…"},
    {"role": "assistant", "content": "…the approved answer…"}
  ],
  "metadata": {
    "result_id": 12, "run_id": 3, "case_id": "linthra-412-retry-backoff",
    "case_source": "linthra#412", "model": "qwen2.5-coder:14b",
    "context_size": 32768, "elapsed_ms": 8421,
    "constraints_passed": 4, "constraints_total": 4,
    "human_rating": 5, "approved_at": "2026-08-26T09:12:44+00:00"
  }
}
```

`metadata` carries provenance so the corpus stays auditable: which model
produced each answer, which case it came from, how it scored, and who
rated it. It carries no user id, no conversation id, and no prompt Nova
would not show the user.

Two provenance guarantees are worth stating explicitly:

- **`model` is what actually ran.** On a backend that ignores the
  requested model name (llama.cpp serves one configured `.gguf`), this
  records the backend's own model id rather than the label the run asked
  for — so the corpus can never carry invented provenance.
- **The prompt is the one stored with the result**, captured when the
  answer was produced, never re-read from the case file at export time.
  Editing a case afterwards would otherwise pair an approved answer with
  a task it never answered, and deleting a case would strand a valid
  approved result. Both are prevented; a legacy result with no stored
  snapshot fails closed with a clear message rather than guessing.

## Producing a file

```
GET  /admin/eval/dataset            # how many results are approved, and where a file would land
POST /admin/eval/dataset/export     # {"result_ids": [12, 15, 19], "filename": "batch-01"}
```

The file is written under Nova's own exports directory
(`<data root>/exports/nova-coder-datasets/`), with a validated filename,
and an existing file is never overwritten. Nothing is uploaded,
registered, or pushed anywhere — the operator ends up with one file on
their own disk and decides what happens next.

## What is deliberately *not* here

- **No training.** No trainer, no LoRA config, no GPU code, no
  dependency on a training framework.
- **No collection.** Nothing accumulates in the background waiting to
  become a dataset. The only records that exist are evaluation results
  the operator asked for.
- **No chat mining.** Turning real conversations into training data is a
  different feature with different consent requirements, and it is not
  this one.

## If `nova-coder` is eventually built

The remaining work is outside Nova, on hardware the operator controls:
curate enough approved examples, run the fine-tune with the tooling of
their choice, package the result for the local runtime, and then point
the existing variable at it —

```
NOVA_CODE_MODEL=nova-coder:14b
```

— which needs no Nova code change at all. See
[model-profiles.md](model-profiles.md#nova-coder).
