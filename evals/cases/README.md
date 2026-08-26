# Nova evaluation cases

Each `*.json` file in this directory is one reusable evaluation case (or
a JSON array of them). Nova loads them read-only; add your own by
pointing `NOVA_EVAL_CASES_DIR` at a directory of your own case files.
Cases there override shipped cases with the same `id`.

```json
{
  "id": "short-identifier",
  "title": "Human-readable title",
  "role": "code",
  "source": "linthra#412",
  "prompt": "The task, exactly as the model will receive it.",
  "constraints": [
    {"kind": "must_include_code_block"},
    {"kind": "must_contain", "value": "timeout"},
    {"kind": "must_not_contain", "value": "sudo"},
    {"kind": "max_chars", "value": "4000"}
  ],
  "tags": ["python", "bugfix"],
  "notes": "Why this case exists and what a good answer looks like."
}
```

Constraint kinds: `must_contain`, `must_not_contain`, `must_match`
(regex), `must_include_code_block`, `must_mention_file`, `max_chars`,
`min_chars`. They are deterministic text checks — **model output is
never executed**, so a case can never ask Nova to run a command.

Representing a real issue (for example a Linthra bug report) as a case
means writing down the prompt you would actually send and the
constraints a correct answer has to satisfy. Nothing about the format is
specific to the examples shipped here.
