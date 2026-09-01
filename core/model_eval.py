"""
Local model evaluation harness — compare models on the same coding tasks.

Nova can run several local models. Until now there was no way to answer
*"is the 14B actually better than the 7B on my work?"* except by feel.
This module makes that measurable, locally, with results that stay on
the operator's machine.

What one evaluation records
---------------------------
For every (case, model) pair: the model name, the context size the local
runtime actually reports for that loaded evaluation model when available,
elapsed wall-clock time, whether it succeeded, which of the case's
**requested constraints** it followed, the exact prompt snapshot that
produced the answer, the raw output, and — later, when a human looks at
it — an optional 1-5 rating and note.

Hard boundaries
---------------
* **Model output is never executed.** Constraints are pure, deterministic
  text checks run in this process. Nothing in this module spawns a
  shell, writes a file, applies a patch, or touches a repository. A case
  that "wants" a command run is simply not expressible here — by design.
* **No network beyond the configured local model backend.** Cases are
  local JSON, results are local SQLite.
* **No downloads.** Evaluating a model that is not installed produces a
  recorded failure with a clear error, never a pull.
* **Nothing is exported anywhere.** Results live in Nova's database until
  an operator explicitly asks for them (see :mod:`core.coder_dataset`).
* **No memory, no personalization, no user data.** An evaluation turn is
  built from the case prompt and a fixed evaluation preamble only. It
  never loads a user's memories, project context, or preferences, so a
  result can never carry someone's private data.

Case files
----------
A case is a small JSON document (see ``evals/cases/*.json`` for the
shipped examples). Operators add their own by dropping files into
``NOVA_EVAL_CASES_DIR``. A real issue — a Linthra bug report, say —
becomes a reusable evaluation case by writing down the prompt and the
constraints the fix has to satisfy; nothing about the format is specific
to the shipped examples.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

ENV_CASES_DIR = "NOVA_EVAL_CASES_DIR"

#: Cases shipped with Nova, relative to the repository root.
BUILTIN_CASES_DIRNAME = "evals/cases"

# ── Constraint kinds ────────────────────────────────────────────────
CONSTRAINT_MUST_CONTAIN = "must_contain"
CONSTRAINT_MUST_NOT_CONTAIN = "must_not_contain"
CONSTRAINT_MUST_MATCH = "must_match"
CONSTRAINT_MUST_INCLUDE_CODE_BLOCK = "must_include_code_block"
CONSTRAINT_MUST_MENTION_FILE = "must_mention_file"
CONSTRAINT_MAX_CHARS = "max_chars"
CONSTRAINT_MIN_CHARS = "min_chars"

CONSTRAINT_KINDS: tuple[str, ...] = (
    CONSTRAINT_MUST_CONTAIN,
    CONSTRAINT_MUST_NOT_CONTAIN,
    CONSTRAINT_MUST_MATCH,
    CONSTRAINT_MUST_INCLUDE_CODE_BLOCK,
    CONSTRAINT_MUST_MENTION_FILE,
    CONSTRAINT_MAX_CHARS,
    CONSTRAINT_MIN_CHARS,
)

# ── Run states ──────────────────────────────────────────────────────
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"
#: A run whose worker vanished — the process restarted while it was
#: queued or in flight. Terminal, and distinct from ``error`` so an
#: operator can tell "the run failed" from "Nova was restarted under it".
STATUS_INTERRUPTED = "interrupted"

# ── Caps ────────────────────────────────────────────────────────────
MAX_CASE_ID_LEN = 80
MAX_TITLE_LEN = 200
MAX_PROMPT_CHARS = 20_000
MAX_OUTPUT_CHARS = 40_000
MAX_CONSTRAINTS = 25
MAX_CONSTRAINT_VALUE_LEN = 500
MAX_CASE_FILE_BYTES = 256 * 1024
MAX_CASES = 500
MAX_MODELS_PER_RUN = 6
MAX_CASES_PER_RUN = 50
MAX_HUMAN_NOTE_LEN = 500
_MAX_CONCURRENT_RUNS = 1

_CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")

#: The evaluation preamble. This is a **visible, non-secret** harness
#: instruction, deliberately separate from Nova's product system prompt:
#: it is never Nova's identity or safety block, and it is never written
#: to an exported dataset (see :mod:`core.coder_dataset`).
EVAL_SYSTEM_PROMPT = (
    "You are answering a software engineering evaluation task. "
    "Follow every stated constraint exactly. Reply with text only — "
    "your answer is read by a human, it is never executed. Do not ask "
    "for permission to run commands and do not claim to have run "
    "anything."
)


class EvalError(ValueError):
    """A case, run request, or rating was rejected. Message is safe to show."""


# ── Regex safety ────────────────────────────────────────────────────
#
# ``must_match`` is the only constraint that runs a regex, and Python's
# ``re`` backtracks. On a pattern like ``(a+)+$`` an input that *almost*
# matches takes exponential time, and there is no timeout to interrupt
# it: ``re.search`` is a single C call holding the GIL, so a worker that
# enters one cannot be cancelled, joined, or signalled. The evaluation
# thread is simply gone for the life of the process.
#
# The blowup needs **ambiguity** — a repetition whose body can consume
# the same text in more than one way, so a failure forces the engine to
# retry every division of it. That is a structural property of the
# pattern, so it can be decided before the pattern is ever run, using
# the same parser ``re`` itself uses. Patterns are screened at case-parse
# time (a clear :class:`EvalError`) and again in
# :meth:`Constraint.check`, which fails closed instead of raising.
#
# The screen is deliberately conservative: it refuses constructs it
# cannot prove safe rather than trying to decide each one exactly. These
# patterns are written by the operator, in a local case file, so the cost
# of a refusal is an error message at load time.

try:  # CPython >= 3.11
    from re import _parser as _re_parser
except ImportError:  # pragma: no cover - older interpreters
    try:
        import sre_parse as _re_parser  # type: ignore[no-redef]
    except ImportError:  # pragma: no cover - no parser at all
        _re_parser = None  # type: ignore[assignment]

#: Open-ended repetitions (``*``, ``+``, ``{n,}``) allowed in one
#: pattern. Each one the engine can retry independently adds a degree to
#: the worst-case cost; a handful is far more than a real constraint
#: needs and well short of anything that stalls.
MAX_OPEN_ENDED_REPEATS = 6

_REPEAT_OPS = frozenset({"MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"})
_GROUP_OPS = frozenset({"SUBPATTERN", "ATOMIC_GROUP"})
_ASSERT_OPS = frozenset({"ASSERT", "ASSERT_NOT"})
_BACKREF_OPS = frozenset({"GROUPREF", "GROUPREF_IGNORE", "GROUPREF_EXISTS"})


def _op_name(op: object) -> str:
    return str(getattr(op, "name", None) or op)


def _sub_sequences(name: str, av: object) -> list:
    """The sub-patterns a node contains, whatever its own shape."""
    if name in _REPEAT_OPS:
        return [av[2]]
    if name == "SUBPATTERN":
        return [av[3]]
    if name == "ATOMIC_GROUP":
        return [av]
    if name in _ASSERT_OPS:
        return [av[1]]
    if name == "BRANCH":
        return list(av[1])
    return []


def _is_open_ended(av: object) -> bool:
    """Does this repeat have no upper bound (``*``, ``+``, ``{n,}``)?"""
    high = av[1]
    return high is None or high == getattr(_re_parser, "MAXREPEAT", None)


#: Characters the overlap test probes. Disjointness over ASCII is what
#: separates ``\s+\w+`` (two repetitions that can never compete for the
#: same character) from ``\w+\w*`` (two that always do).
_PROBE_CHARS = tuple(chr(code) for code in range(128))

_CATEGORY_TESTS = {
    "CATEGORY_DIGIT": lambda c: c.isdigit(),
    "CATEGORY_NOT_DIGIT": lambda c: not c.isdigit(),
    "CATEGORY_SPACE": lambda c: c.isspace(),
    "CATEGORY_NOT_SPACE": lambda c: not c.isspace(),
    "CATEGORY_WORD": lambda c: c.isalnum() or c == "_",
    "CATEGORY_NOT_WORD": lambda c: not (c.isalnum() or c == "_"),
}


def _char_test(node):
    """A predicate for a body that matches exactly one character, or ``None``.

    ``None`` means "not a simple single-character body" — the caller
    treats that as *not* provably overlapping, because the shapes that
    actually explode (a repetition inside a repetition, alternation
    under one) are refused by :func:`_ambiguity_in` before this runs.
    """
    op, av = node
    name = _op_name(op)
    if name == "ANY":
        return lambda c: True
    if name == "LITERAL":
        return lambda c: ord(c) == av
    if name == "NOT_LITERAL":
        return lambda c: ord(c) != av
    if name == "CATEGORY":
        return _CATEGORY_TESTS.get(_op_name(av))
    if name == "IN":
        items = list(av)
        negated = bool(items) and _op_name(items[0][0]) == "NEGATE"
        if negated:
            items = items[1:]
        tests = []
        for item in items:
            if _op_name(item[0]) == "RANGE":
                low, high = item[1]
                tests.append(lambda c, low=low, high=high: low <= ord(c) <= high)
            else:
                inner = _char_test(item)
                if inner is None:
                    return None
                tests.append(inner)
        if negated:
            return lambda c: not any(test(c) for test in tests)
        return lambda c: any(test(c) for test in tests)
    return None


def _bodies_can_overlap(first, second) -> bool:
    r"""Can these two repetition bodies compete for the same character?

    Two open-ended repetitions in a row only multiply work when they can
    both consume the same text, so the engine has to try every division
    of it — ``.*.*`` or ``\w+\w*``. When their bodies are disjoint,
    ``\s+\w+`` for instance, each character belongs to exactly one of
    them and there is nothing to redistribute.
    """
    left, right = list(first), list(second)
    if left == right:
        return True
    if len(left) != 1 or len(right) != 1:
        return False
    left_test = _char_test(left[0])
    right_test = _char_test(right[0])
    if left_test is None or right_test is None:
        return False
    try:
        return any(
            left_test(char) and right_test(char) for char in _PROBE_CHARS
        )
    except Exception:  # pragma: no cover - defensive
        return True


def _ambiguity_in(nodes) -> Optional[str]:
    """Why the body of a repetition could match one span two ways."""
    for op, av in nodes:
        name = _op_name(op)
        if name in _REPEAT_OPS:
            return "a repetition nested inside another repetition"
        if name == "BRANCH":
            return "alternation inside a repetition"
        if name in _ASSERT_OPS:
            return "a look-around inside a repetition"
        for body in _sub_sequences(name, av):
            reason = _ambiguity_in(body)
            if reason:
                return reason
    return None


def _open_ended_body(name: str, av: object):
    """The repeated body if this node is an open-ended repeat, else ``None``."""
    if name in _REPEAT_OPS:
        return list(av[2]) if _is_open_ended(av) else None
    if name in _GROUP_OPS:
        for body in _sub_sequences(name, av):
            items = list(body)
            if len(items) == 1:
                inner_name = _op_name(items[0][0])
                if inner_name in _REPEAT_OPS and _is_open_ended(items[0][1]):
                    return list(items[0][1][2])
    return None


def _screen_sequence(nodes, state: dict) -> Optional[str]:
    previous_body = None
    for op, av in nodes:
        name = _op_name(op)
        if name in _BACKREF_OPS:
            # A back-reference makes matching NP-hard in general; there
            # is no cheap structural bound to apply to one.
            return "back-references are not supported"

        if name in _REPEAT_OPS:
            reason = _ambiguity_in(av[2])
            if reason:
                return reason

        body = _open_ended_body(name, av)
        if body is not None:
            state["open_ended"] += 1
            if state["open_ended"] > MAX_OPEN_ENDED_REPEATS:
                return "too many open-ended repetitions"
            if previous_body is not None and _bodies_can_overlap(
                previous_body, body
            ):
                # ``.*.*`` and friends: every split of the same span is a
                # distinct attempt, for no expressive gain.
                return "two open-ended repetitions competing for the same text"

        for sub in _sub_sequences(name, av):
            reason = _screen_sequence(sub, state)
            if reason:
                return reason

        previous_body = body
    return None


@lru_cache(maxsize=256)
def unsafe_regex_reason(pattern: str) -> Optional[str]:
    """Why ``pattern`` is refused for ``must_match``; ``None`` if it is safe.

    Fails closed: an invalid pattern, an interpreter without the parser,
    or any surprise during the walk is a refusal, never an exception and
    never a pass.
    """
    if _re_parser is None:  # pragma: no cover - no stdlib parser
        return "regex constraints are unavailable on this interpreter"
    try:
        parsed = _re_parser.parse(pattern)
    except re.error:
        return "not a valid regex"
    except Exception:  # pragma: no cover - defensive
        return "could not be checked"
    try:
        return _screen_sequence(parsed, {"open_ended": 0})
    except RecursionError:
        return "pattern is nested too deeply"
    except Exception:  # pragma: no cover - defensive
        return "could not be checked"


def _fenced_code_block_present(text: str) -> bool:
    """Is there a *complete* fenced block, rather than a stray marker?

    A single ``` is what a truncated or malformed answer looks like, so
    accepting it scored those as having produced code. A block needs an
    opening fence — optionally carrying a language — and a later closing
    one.
    """
    fences = 0
    for line in text.splitlines():
        if line.strip().startswith("```"):
            fences += 1
            if fences >= 2:
                return True
    return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Constraints ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Constraint:
    """One deterministic, text-only check against a model's output."""

    kind: str
    value: str = ""
    description: str = ""

    def label(self) -> str:
        if self.description:
            return self.description
        if self.kind in (CONSTRAINT_MUST_INCLUDE_CODE_BLOCK,):
            return "output includes a fenced code block"
        return f"{self.kind}: {self.value}"

    def check(self, output: str) -> tuple[bool, str]:
        """``(passed, detail)`` — never raises, never executes anything."""
        text = output or ""
        lowered = text.lower()
        try:
            if self.kind == CONSTRAINT_MUST_CONTAIN:
                ok = self.value.lower() in lowered
                return ok, "" if ok else "expected text not found"
            if self.kind == CONSTRAINT_MUST_NOT_CONTAIN:
                ok = self.value.lower() not in lowered
                return ok, "" if ok else "forbidden text present"
            if self.kind == CONSTRAINT_MUST_MATCH:
                # ``_coerce_constraint`` screens patterns when a case is
                # loaded, but a ``Constraint`` can also be built
                # directly. Re-check here so the dangerous path is the
                # one that is closed, not merely the documented one —
                # and refuse without raising, per this method's contract.
                refusal = unsafe_regex_reason(self.value)
                if refusal is not None:
                    return False, f"pattern refused ({refusal})"
                ok = re.search(self.value, text, re.I | re.M) is not None
                return ok, "" if ok else "pattern did not match"
            if self.kind == CONSTRAINT_MUST_INCLUDE_CODE_BLOCK:
                ok = _fenced_code_block_present(text)
                return ok, "" if ok else "no complete fenced code block"
            if self.kind == CONSTRAINT_MUST_MENTION_FILE:
                ok = self.value.lower() in lowered
                return ok, "" if ok else "file was not referenced"
            if self.kind == CONSTRAINT_MAX_CHARS:
                limit = int(self.value)
                ok = len(text) <= limit
                return ok, "" if ok else f"{len(text)} chars exceeds {limit}"
            if self.kind == CONSTRAINT_MIN_CHARS:
                limit = int(self.value)
                ok = len(text) >= limit
                return ok, "" if ok else f"{len(text)} chars below {limit}"
        except (re.error, TypeError, ValueError) as exc:
            return False, f"constraint could not be evaluated ({type(exc).__name__})"
        return False, "unknown constraint kind"

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "value": self.value,
            "description": self.description,
        }


def _coerce_constraint(raw: object) -> Constraint:
    if not isinstance(raw, dict):
        raise EvalError("each constraint must be an object")
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in CONSTRAINT_KINDS:
        raise EvalError("constraint has an unknown 'kind'")
    value = raw.get("value", "")
    if isinstance(value, bool):
        value = ""
    elif isinstance(value, (int, float)):
        value = str(int(value))
    elif not isinstance(value, str):
        value = ""
    if len(value) > MAX_CONSTRAINT_VALUE_LEN:
        raise EvalError("constraint value is too long")
    if kind == CONSTRAINT_MUST_MATCH:
        refusal = unsafe_regex_reason(value)
        if refusal is not None:
            raise EvalError(
                f"constraint 'must_match' was refused as an unsafe regex: {refusal}"
            )
    if kind in (CONSTRAINT_MAX_CHARS, CONSTRAINT_MIN_CHARS):
        try:
            int(value)
        except (TypeError, ValueError):
            raise EvalError(f"constraint '{kind}' needs an integer value") from None
    description = raw.get("description", "")
    if not isinstance(description, str):
        description = ""
    return Constraint(kind=kind, value=value, description=description[:MAX_TITLE_LEN])


# ── Cases ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EvalCase:
    """One reusable evaluation task.

    ``source`` is free-form provenance ("linthra#412", "internal") so a
    real issue can be represented as a case and traced back later.
    """

    id: str
    title: str
    prompt: str
    role: str = "code"
    constraints: tuple[Constraint, ...] = field(default_factory=tuple)
    tags: tuple[str, ...] = field(default_factory=tuple)
    source: str = ""
    notes: str = ""

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "prompt": self.prompt,
            "role": self.role,
            "constraints": [c.as_dict() for c in self.constraints],
            "tags": list(self.tags),
            "source": self.source,
            "notes": self.notes,
        }


def parse_case(payload: object) -> EvalCase:
    """Validate one case document. Raises :class:`EvalError` with a reason."""
    if not isinstance(payload, dict):
        raise EvalError("a case must be a JSON object")
    case_id = payload.get("id")
    if not isinstance(case_id, str) or not _CASE_ID_RE.match(case_id.strip()):
        raise EvalError("case 'id' must be a short alphanumeric identifier")
    case_id = case_id.strip()

    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise EvalError(f"case '{case_id}' needs a non-empty 'prompt'")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise EvalError(f"case '{case_id}' prompt is too long")

    title = payload.get("title")
    title = title.strip()[:MAX_TITLE_LEN] if isinstance(title, str) else case_id

    role = payload.get("role")
    role = role.strip().lower() if isinstance(role, str) else "code"

    raw_constraints = payload.get("constraints", [])
    if raw_constraints is None:
        raw_constraints = []
    if not isinstance(raw_constraints, list):
        raise EvalError(f"case '{case_id}' has a non-list 'constraints'")
    if len(raw_constraints) > MAX_CONSTRAINTS:
        raise EvalError(f"case '{case_id}' has too many constraints")
    constraints = tuple(_coerce_constraint(c) for c in raw_constraints)

    raw_tags = payload.get("tags", [])
    tags = tuple(
        t.strip()[:40] for t in raw_tags
        if isinstance(t, str) and t.strip()
    ) if isinstance(raw_tags, list) else ()

    source = payload.get("source")
    source = source.strip()[:MAX_TITLE_LEN] if isinstance(source, str) else ""
    notes = payload.get("notes")
    notes = notes.strip()[:MAX_PROMPT_CHARS] if isinstance(notes, str) else ""

    return EvalCase(
        id=case_id, title=title or case_id, prompt=prompt.strip(),
        role=role, constraints=constraints, tags=tags,
        source=source, notes=notes,
    )


def builtin_cases_dir() -> Path:
    """The shipped case directory (inside the Nova checkout)."""
    return Path(__file__).resolve().parent.parent / BUILTIN_CASES_DIRNAME


def operator_cases_dir() -> Optional[Path]:
    """The operator's own case directory, or ``None`` when unset."""
    raw = (os.getenv(ENV_CASES_DIR, "") or "").strip()
    if not raw:
        return None
    try:
        return Path(raw).expanduser()
    except (OSError, ValueError):
        return None


def _load_dir(directory: Path) -> tuple[list[EvalCase], list[str]]:
    cases: list[EvalCase] = []
    problems: list[str] = []
    try:
        if not directory.is_dir():
            return cases, problems
        entries = sorted(p for p in directory.iterdir() if p.suffix == ".json")
    except OSError:
        return cases, [f"{directory.name}: directory unreadable"]

    for path in entries:
        try:
            if not path.is_file() or path.stat().st_size > MAX_CASE_FILE_BYTES:
                problems.append(f"{path.name}: skipped (not a small JSON file)")
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            problems.append(f"{path.name}: unreadable or invalid JSON")
            continue
        documents = payload if isinstance(payload, list) else [payload]
        for document in documents:
            try:
                cases.append(parse_case(document))
            except EvalError as exc:
                problems.append(f"{path.name}: {exc}")
    return cases, problems


def load_cases(
    directories: Optional[Sequence[Path]] = None,
) -> tuple[list[EvalCase], list[str]]:
    """``(cases, problems)`` from the builtin and operator case directories.

    Never raises: an unreadable file becomes a human-readable problem
    string instead of an exception. A later directory's case wins on an
    id collision, so an operator can override a shipped case.
    """
    if directories is None:
        dirs = [builtin_cases_dir()]
        operator = operator_cases_dir()
        if operator is not None:
            dirs.append(operator)
    else:
        dirs = list(directories)

    # Merge every directory first, then cap. Capping inside the loop let
    # the limit decide *which definition wins*: with the cap already
    # reached, a later operator directory could not override a shipped
    # case of the same id, silently contradicting the documented
    # later-directory-wins rule — and doing so without increasing the
    # total, since an override replaces rather than adds.
    merged: dict[str, EvalCase] = {}
    problems: list[str] = []
    for directory in dirs:
        found, issues = _load_dir(directory)
        problems.extend(issues)
        for case in found:
            merged[case.id] = case

    cases = list(merged.values())
    if len(cases) > MAX_CASES:
        problems.append(
            f"{len(cases)} cases found; only the first {MAX_CASES} are used."
        )
        cases = cases[:MAX_CASES]
    return cases, problems


def get_case(case_id: str) -> Optional[EvalCase]:
    for case in load_cases()[0]:
        if case.id == case_id:
            return case
    return None


# ── Storage ─────────────────────────────────────────────────────────

_RUNS_SQL = """
CREATE TABLE IF NOT EXISTS model_eval_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    label        TEXT    NOT NULL DEFAULT '',
    models       TEXT    NOT NULL,
    case_ids     TEXT    NOT NULL,
    status       TEXT    NOT NULL,
    total        INTEGER NOT NULL DEFAULT 0,
    completed    INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL,
    started_at   TEXT,
    finished_at  TEXT,
    error        TEXT    NOT NULL DEFAULT ''
)
"""

_RESULTS_SQL = """
CREATE TABLE IF NOT EXISTS model_eval_results (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id             INTEGER NOT NULL,
    case_id            TEXT    NOT NULL,
    case_title         TEXT    NOT NULL DEFAULT '',
    prompt_snapshot    TEXT    NOT NULL DEFAULT '',
    case_source        TEXT    NOT NULL DEFAULT '',
    requested_model    TEXT    NOT NULL DEFAULT '',
    model              TEXT    NOT NULL,
    context_size       INTEGER,
    elapsed_ms         INTEGER NOT NULL DEFAULT 0,
    success            INTEGER NOT NULL DEFAULT 0,
    constraints_passed INTEGER NOT NULL DEFAULT 0,
    constraints_total  INTEGER NOT NULL DEFAULT 0,
    constraints_json   TEXT    NOT NULL DEFAULT '[]',
    output             TEXT    NOT NULL DEFAULT '',
    output_truncated   INTEGER NOT NULL DEFAULT 0,
    error              TEXT    NOT NULL DEFAULT '',
    human_rating       INTEGER,
    human_note         TEXT    NOT NULL DEFAULT '',
    approved           INTEGER NOT NULL DEFAULT 0,
    approved_at        TEXT,
    created_at         TEXT    NOT NULL
)
"""

_RESULTS_RUN_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_model_eval_results_run "
    "ON model_eval_results(run_id)"
)


def _ensure_result_snapshot_columns(conn: sqlite3.Connection) -> None:
    """Upgrade pre-snapshot evaluation tables in place, idempotently.

    This branch has not shipped yet, but Nova's migration contract is
    deliberately restart-only: an operator may have created evaluation
    rows while testing an earlier revision of the branch. SQLite's
    additive ``ALTER TABLE ... ADD COLUMN`` keeps those databases usable.
    Legacy rows receive empty snapshots and are deliberately refused by
    the dataset exporter rather than reconstructing a possibly changed
    prompt later.
    """
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(model_eval_results)").fetchall()
    }
    if "prompt_snapshot" not in columns:
        conn.execute(
            "ALTER TABLE model_eval_results ADD COLUMN "
            "prompt_snapshot TEXT NOT NULL DEFAULT ''"
        )
    if "requested_model" not in columns:
        conn.execute(
            "ALTER TABLE model_eval_results ADD COLUMN "
            "requested_model TEXT NOT NULL DEFAULT ''"
        )
    if "case_source" not in columns:
        conn.execute(
            "ALTER TABLE model_eval_results ADD COLUMN "
            "case_source TEXT NOT NULL DEFAULT ''"
        )
    if "output_truncated" not in columns:
        conn.execute(
            "ALTER TABLE model_eval_results ADD COLUMN "
            "output_truncated INTEGER NOT NULL DEFAULT 0"
        )


def migrate(db_path: str) -> None:
    """Create/upgrade the evaluation tables. Idempotent on every start."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(_RUNS_SQL)
        conn.execute(_RESULTS_SQL)
        _ensure_result_snapshot_columns(conn)
        conn.execute(_RESULTS_RUN_INDEX_SQL)


def recover_interrupted_runs(db_path: str) -> int:
    """Close out runs whose worker died with the process. Returns the count.

    Runs execute on daemon threads, so a restart discards the worker
    while its row stays ``queued``/``running`` forever — surfacing a job
    that can never finish, sometimes with partial results attached. Call
    this once at startup, when by definition no worker of this process is
    live, so any active row is orphaned.

    Deliberately separate from :func:`migrate`: that is a schema
    operation and may run at other times, while this rewrites *state* and
    is only sound at startup. Never raises.
    """
    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute(
                "UPDATE model_eval_runs SET status = ?, finished_at = ?, "
                "error = ? WHERE status IN (?, ?)",
                (
                    STATUS_INTERRUPTED, _now_iso(),
                    "Nova restarted while this run was in progress; it was "
                    "not resumed. Any results already recorded are kept.",
                    STATUS_QUEUED, STATUS_RUNNING,
                ),
            )
            return int(cur.rowcount or 0)
    except sqlite3.Error as exc:
        logger.warning(
            "evaluation: interrupted-run recovery failed (%s)",
            type(exc).__name__,
        )
        return 0


def _open(db_path: Optional[str] = None) -> sqlite3.Connection:
    if db_path is None:
        from core.memory import DB_PATH

        db_path = DB_PATH
    return sqlite3.connect(db_path)


def _db_path(db_path: Optional[str] = None) -> str:
    if db_path is not None:
        return db_path
    from core.memory import DB_PATH

    return DB_PATH


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    prev = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.row_factory = prev


def _result_to_dict(row: dict) -> dict:
    try:
        constraints = json.loads(row.get("constraints_json") or "[]")
    except ValueError:
        constraints = []
    return {
        "id": int(row["id"]),
        "run_id": int(row["run_id"]),
        "case_id": row["case_id"],
        "case_title": row["case_title"],
        "prompt_snapshot": row.get("prompt_snapshot") or "",
        "case_source": row.get("case_source") or "",
        # The label the operator asked for. Equal to ``model`` on a
        # name-routing backend; on a single-model one it records what was
        # requested while ``model`` records what actually ran.
        "requested_model": row.get("requested_model") or "",
        "model": row["model"],
        "context_size": row["context_size"],
        "elapsed_ms": int(row["elapsed_ms"]),
        "success": bool(row["success"]),
        "constraints_passed": int(row["constraints_passed"]),
        "constraints_total": int(row["constraints_total"]),
        "constraints": constraints,
        "output": row["output"],
        "output_truncated": bool(row["output_truncated"]),
        "error": row["error"],
        "human_rating": row["human_rating"],
        "human_note": row["human_note"],
        "approved": bool(row["approved"]),
        "approved_at": row["approved_at"],
        "created_at": row["created_at"],
    }


def _run_to_dict(row: dict) -> dict:
    return {
        "id": int(row["id"]),
        "label": row["label"],
        "models": [m for m in (row["models"] or "").split(",") if m],
        "case_ids": [c for c in (row["case_ids"] or "").split(",") if c],
        "status": row["status"],
        "total": int(row["total"]),
        "completed": int(row["completed"]),
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "error": row["error"],
    }


def list_runs(db_path: Optional[str] = None, limit: int = 50) -> list[dict]:
    with _open(db_path) as conn:
        rows = _rows(
            conn,
            "SELECT * FROM model_eval_runs ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 200)),),
        )
    return [_run_to_dict(r) for r in rows]


def get_run(run_id: int, db_path: Optional[str] = None) -> Optional[dict]:
    with _open(db_path) as conn:
        rows = _rows(
            conn, "SELECT * FROM model_eval_runs WHERE id = ?", (int(run_id),)
        )
    return _run_to_dict(rows[0]) if rows else None


def list_results(
    run_id: Optional[int] = None,
    db_path: Optional[str] = None,
    limit: int = 500,
) -> list[dict]:
    capped = max(1, min(int(limit), 1000))
    with _open(db_path) as conn:
        if run_id is None:
            rows = _rows(
                conn,
                "SELECT * FROM model_eval_results ORDER BY id DESC LIMIT ?",
                (capped,),
            )
        else:
            rows = _rows(
                conn,
                "SELECT * FROM model_eval_results WHERE run_id = ? "
                "ORDER BY id ASC LIMIT ?",
                (int(run_id), capped),
            )
    return [_result_to_dict(r) for r in rows]


def list_approved_results(db_path: Optional[str] = None) -> list[dict]:
    """Every approved result, oldest first — deliberately uncapped.

    :func:`list_results` returns the newest N rows, which is right for a
    browsing view and wrong for "what is approved?": once a host has run
    more than that many evaluations, an older approved row would silently
    vanish from the only surface that discovers it, under-reporting the
    count and hiding an id that is still perfectly exportable when named
    directly. Approved rows are a small, operator-curated subset, so
    listing all of them is cheap and honest.
    """
    with _open(db_path) as conn:
        rows = _rows(
            conn,
            "SELECT * FROM model_eval_results WHERE approved = 1 "
            "ORDER BY id ASC",
        )
    return [_result_to_dict(r) for r in rows]


def get_result(result_id: int, db_path: Optional[str] = None) -> Optional[dict]:
    with _open(db_path) as conn:
        rows = _rows(
            conn, "SELECT * FROM model_eval_results WHERE id = ?", (int(result_id),)
        )
    return _result_to_dict(rows[0]) if rows else None


# ── Executing one case ──────────────────────────────────────────────


def evaluate_output(case: EvalCase, output: str) -> dict:
    """Run every constraint against ``output``. Pure, deterministic, safe.

    The output is only ever *inspected as text*. Nothing in this function
    (or anywhere in this module) executes, writes, or interprets it as a
    command.
    """
    checks: list[dict] = []
    passed = 0
    for constraint in case.constraints:
        ok, detail = constraint.check(output)
        if ok:
            passed += 1
        checks.append({
            "kind": constraint.kind,
            "label": constraint.label(),
            "passed": ok,
            "detail": detail,
        })
    return {
        "constraints": checks,
        "constraints_passed": passed,
        "constraints_total": len(case.constraints),
        "all_passed": passed == len(case.constraints),
    }


def _runtime_context_size(model: str, provider: object) -> Optional[int]:
    """Context size the active runtime reports for this loaded model.

    Today only Ollama exposes that fact through its read-only ``/api/ps``
    surface. Providers without a loaded-model view return ``None`` rather
    than borrowing a profile recommendation and presenting it as an
    observed benchmark parameter.
    """
    if getattr(provider, "name", "") != "ollama":
        return None
    try:
        from core.ollama_client import list_running_models

        rows = list_running_models()
    except Exception as exc:
        logger.debug("eval: runtime context unavailable (%s)", type(exc).__name__)
        return None

    exact: Optional[dict] = None
    tolerant: Optional[dict] = None
    for row in rows:
        name = row.get("name")
        if not isinstance(name, str):
            continue
        if name == model:
            exact = row
            break
        if name.startswith(model + ":") or model.startswith(name + ":"):
            tolerant = row
    row = exact or tolerant or {}
    value = row.get("context_size")
    return value if isinstance(value, int) and value > 0 else None


def run_case(case: EvalCase, model: str) -> dict:
    """Generate one answer for ``case`` with ``model`` and score it.

    Returns a plain dict (not persisted here). A backend failure — an
    unreachable daemon, a model that is not installed, a crashed runner —
    is recorded as a failed result with a short reason, never raised and
    never a download. ``context_size`` is an observed runtime value when
    the provider can report it; otherwise it is ``None`` rather than a
    guessed/profile value.
    """
    from core.model_providers import (
        ModelProviderError,
        ModelRequest,
        get_provider,
    )

    messages = [
        {"role": "system", "content": EVAL_SYSTEM_PROMPT},
        {"role": "user", "content": case.prompt},
    ]

    provider = get_provider()

    # Provenance must name what actually ran. A single-model backend
    # (llama.cpp) ignores ``request.model`` and serves its one configured
    # ``.gguf``, so recording the requested label would attribute one
    # backend's output to whatever name the caller happened to pass —
    # silently faking a two-model comparison and poisoning any dataset
    # exported from it. Record the backend's real id instead.
    recorded_model = model
    if not getattr(provider, "selects_model_by_name", True):
        backend_id = ""
        try:
            backend_id = provider.backend_model_id() or ""
        except Exception:  # pragma: no cover - defensive
            backend_id = ""
        recorded_model = backend_id or f"{getattr(provider, 'name', 'provider')}:configured-model"

    started = time.monotonic()
    output = ""
    full_output = ""
    output_truncated = False
    error = ""
    context_size: Optional[int] = None
    try:
        response = provider.generate(
            ModelRequest(model=model, messages=messages)
        )
        # Keep the full response for scoring and cap only what is
        # stored. Capping first lets the storage limit manufacture a
        # pass: a 40,001-character answer against ``max_chars: 40000``
        # becomes exactly 40,000 and satisfies the constraint it
        # actually violated.
        full_output = response.content or ""
        output_truncated = len(full_output) > MAX_OUTPUT_CHARS
        output = full_output[:MAX_OUTPUT_CHARS]
        context_size = _runtime_context_size(recorded_model, provider)
    except ModelProviderError as exc:
        kind = getattr(exc, "kind", "") or "backend_error"
        error = f"{kind}: {str(exc)[:200]}"
    except Exception as exc:  # pragma: no cover - defensive
        error = f"unexpected_error: {type(exc).__name__}"
    elapsed_ms = int((time.monotonic() - started) * 1000)

    scored = evaluate_output(case, full_output)
    return {
        "case_id": case.id,
        "case_title": case.title,
        "prompt_snapshot": case.prompt,
        "case_source": case.source,
        # What actually produced this answer (see above): the requested
        # label on a name-routing backend, the backend's own model id on
        # a single-model one.
        "model": recorded_model,
        "requested_model": model,
        "context_size": context_size,
        "elapsed_ms": elapsed_ms,
        "success": bool(
            not error and full_output.strip() and scored["all_passed"]
        ),
        "constraints": scored["constraints"],
        "constraints_passed": scored["constraints_passed"],
        "constraints_total": scored["constraints_total"],
        "output": output,
        # True when the stored text is shorter than what the model
        # returned. The score above reflects the *full* response; this
        # flags that the stored copy is incomplete, which blocks dataset
        # export — an incomplete completion must not become a training
        # example.
        "output_truncated": output_truncated,
        "error": error,
    }


# ── Runs ────────────────────────────────────────────────────────────

_run_lock = threading.Lock()
_active_runs: set[int] = set()


class TooManyRunsInProgress(Exception):
    """A second evaluation run was refused while one is already active."""

    def __init__(self, cap: int) -> None:
        super().__init__(f"an evaluation run is already in progress (cap={cap})")
        self.cap = cap


def _validate_models(models: Iterable[object]) -> list[str]:
    from core import model_profiles

    out: list[str] = []
    for raw in models:
        name = model_profiles.normalize_model_name(raw)
        if not name:
            raise EvalError("each model must be a short, non-empty name.")
        if name not in out:
            out.append(name)
    if not out:
        raise EvalError("at least one model is required.")
    if len(out) > MAX_MODELS_PER_RUN:
        raise EvalError(f"at most {MAX_MODELS_PER_RUN} models per run.")

    # A backend that ignores the requested name cannot run a comparison:
    # every "model" in the list would be the same configured file, and the
    # run would report a difference that does not exist. Refuse it up
    # front rather than producing a meaningless benchmark.
    try:
        from core.model_providers import get_provider

        provider = get_provider()
        selects = getattr(provider, "selects_model_by_name", True)
    except Exception:  # never block a run on a registry hiccup
        selects = True
    if not selects and len(out) > 1:
        raise EvalError(
            "The active model provider serves a single configured model "
            "and ignores the requested model name, so comparing several "
            "names would evaluate the same model repeatedly. Run one "
            "model at a time on this provider."
        )
    return out


def _resolve_cases(case_ids: Optional[Sequence[str]]) -> list[EvalCase]:
    available = {c.id: c for c in load_cases()[0]}
    if not available:
        raise EvalError("no evaluation cases are available.")
    if not case_ids:
        selected = list(available.values())
    else:
        selected = []
        for raw in case_ids:
            if not isinstance(raw, str) or raw not in available:
                raise EvalError("unknown evaluation case requested.")
            if available[raw] not in selected:
                selected.append(available[raw])
    if len(selected) > MAX_CASES_PER_RUN:
        raise EvalError(f"at most {MAX_CASES_PER_RUN} cases per run.")
    return selected


def start_run(
    models: Sequence[object],
    case_ids: Optional[Sequence[str]] = None,
    label: str = "",
    db_path: Optional[str] = None,
    *,
    background: bool = True,
) -> dict:
    """Queue an evaluation run and (by default) execute it on a thread.

    Validation happens synchronously so a bad request is a clean error
    with nothing written. Generation happens off the request thread so a
    long evaluation never blocks ``/chat``.
    """
    resolved_models = _validate_models(models)
    cases = _resolve_cases(case_ids)
    clean_label = label.strip()[:MAX_TITLE_LEN] if isinstance(label, str) else ""
    path = _db_path(db_path)
    total = len(resolved_models) * len(cases)

    with _run_lock:
        if len(_active_runs) >= _MAX_CONCURRENT_RUNS:
            raise TooManyRunsInProgress(_MAX_CONCURRENT_RUNS)
        with sqlite3.connect(path) as conn:
            cur = conn.execute(
                "INSERT INTO model_eval_runs "
                "(label, models, case_ids, status, total, completed, created_at) "
                "VALUES (?, ?, ?, ?, ?, 0, ?)",
                (clean_label, ",".join(resolved_models),
                 ",".join(c.id for c in cases), STATUS_QUEUED, total, _now_iso()),
            )
            run_id = int(cur.lastrowid)
        _active_runs.add(run_id)

    if background:
        thread = threading.Thread(
            target=_execute_run,
            args=(run_id, resolved_models, cases, path),
            name=f"nova-eval-{run_id}",
            daemon=True,
        )
        thread.start()
    else:
        _execute_run(run_id, resolved_models, cases, path)
    return get_run(run_id, db_path=path) or {"id": run_id, "status": STATUS_QUEUED}


def _execute_run(
    run_id: int,
    models: Sequence[str],
    cases: Sequence[EvalCase],
    db_path: str,
) -> None:
    """Execute every (case, model) pair, recording each result as it lands."""
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE model_eval_runs SET status = ?, started_at = ? WHERE id = ?",
                (STATUS_RUNNING, _now_iso(), run_id),
            )
        completed = 0
        for model in models:
            for case in cases:
                result = run_case(case, model)
                with sqlite3.connect(db_path) as conn:
                    conn.execute(
                        "INSERT INTO model_eval_results "
                        "(run_id, case_id, case_title, prompt_snapshot, case_source, "
                        "model, requested_model, context_size, elapsed_ms, "
                        "success, constraints_passed, "
                        "constraints_total, constraints_json, output, "
                        "output_truncated, error, created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            run_id, result["case_id"], result["case_title"],
                            result["prompt_snapshot"], result["case_source"],
                            result["model"], result["requested_model"],
                            result["context_size"],
                            result["elapsed_ms"], 1 if result["success"] else 0,
                            result["constraints_passed"],
                            result["constraints_total"],
                            json.dumps(result["constraints"]),
                            result["output"],
                            1 if result["output_truncated"] else 0,
                            result["error"], _now_iso(),
                        ),
                    )
                    completed += 1
                    conn.execute(
                        "UPDATE model_eval_runs SET completed = ? WHERE id = ?",
                        (completed, run_id),
                    )
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE model_eval_runs SET status = ?, finished_at = ? WHERE id = ?",
                (STATUS_DONE, _now_iso(), run_id),
            )
    except Exception as exc:  # never let a worker thread die silently
        logger.warning("evaluation run %s failed: %s", run_id, type(exc).__name__)
        try:
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "UPDATE model_eval_runs SET status = ?, finished_at = ?, "
                    "error = ? WHERE id = ?",
                    (STATUS_ERROR, _now_iso(),
                     f"run failed ({type(exc).__name__})", run_id),
                )
        except sqlite3.Error:
            pass
    finally:
        with _run_lock:
            _active_runs.discard(run_id)


# ── Human review ────────────────────────────────────────────────────


def rate_result(
    result_id: int,
    rating: Optional[int],
    note: str = "",
    db_path: Optional[str] = None,
) -> dict:
    """Attach a human 1-5 rating (or clear it) and an optional note.

    A rating is a *human* judgement recorded next to the deterministic
    constraint checks; it never changes ``success``.
    """
    if rating is not None:
        if isinstance(rating, bool) or not isinstance(rating, int):
            raise EvalError("rating must be an integer between 1 and 5.")
        if not (1 <= rating <= 5):
            raise EvalError("rating must be between 1 and 5.")
    clean_note = (note or "").strip()[:MAX_HUMAN_NOTE_LEN] if isinstance(note, str) else ""

    path = _db_path(db_path)
    with sqlite3.connect(path) as conn:
        cur = conn.execute(
            "UPDATE model_eval_results SET human_rating = ?, human_note = ? "
            "WHERE id = ?",
            (rating, clean_note, int(result_id)),
        )
        if cur.rowcount == 0:
            raise EvalError("evaluation result not found.")
    result = get_result(result_id, db_path=path)
    assert result is not None
    return result


def set_result_approval(
    result_id: int,
    approved: bool,
    db_path: Optional[str] = None,
) -> dict:
    """Mark a result as operator-approved (or withdraw approval).

    Approval is the **only** gate that makes a result eligible for a
    training-dataset export (see :mod:`core.coder_dataset`). It is never
    set automatically — an operator sets it, per result, after reading
    the output.
    """
    path = _db_path(db_path)
    with sqlite3.connect(path) as conn:
        cur = conn.execute(
            "UPDATE model_eval_results SET approved = ?, approved_at = ? "
            "WHERE id = ?",
            (1 if approved else 0, _now_iso() if approved else None, int(result_id)),
        )
        if cur.rowcount == 0:
            raise EvalError("evaluation result not found.")
    result = get_result(result_id, db_path=path)
    assert result is not None
    return result


def summarize_run(run_id: int, db_path: Optional[str] = None) -> dict:
    """Per-model totals for one run: pass rate, mean latency, ratings."""
    results = list_results(run_id, db_path=db_path)
    by_model: dict[str, dict] = {}
    for row in results:
        bucket = by_model.setdefault(row["model"], {
            "model": row["model"],
            "context_size": row["context_size"],
            "cases": 0, "passed": 0, "errors": 0,
            "total_elapsed_ms": 0, "ratings": [],
        })
        bucket["cases"] += 1
        bucket["passed"] += 1 if row["success"] else 0
        bucket["errors"] += 1 if row["error"] else 0
        bucket["total_elapsed_ms"] += row["elapsed_ms"]
        if row["human_rating"] is not None:
            bucket["ratings"].append(row["human_rating"])

    models = []
    for bucket in by_model.values():
        cases = bucket["cases"] or 1
        ratings = bucket.pop("ratings")
        models.append({
            **bucket,
            "pass_rate": round(bucket["passed"] / cases, 3),
            "mean_elapsed_ms": int(bucket["total_elapsed_ms"] / cases),
            "mean_human_rating": (
                round(sum(ratings) / len(ratings), 2) if ratings else None
            ),
        })
    models.sort(key=lambda m: (-m["pass_rate"], m["mean_elapsed_ms"]))
    return {"run_id": run_id, "models": models, "result_count": len(results)}


__all__ = [
    "ENV_CASES_DIR", "EVAL_SYSTEM_PROMPT", "CONSTRAINT_KINDS",
    "STATUS_QUEUED", "STATUS_RUNNING", "STATUS_DONE", "STATUS_ERROR",
    "STATUS_INTERRUPTED", "recover_interrupted_runs",
    "EvalError", "TooManyRunsInProgress", "Constraint", "EvalCase",
    "unsafe_regex_reason", "MAX_OPEN_ENDED_REPEATS",
    "parse_case", "load_cases", "get_case",
    "builtin_cases_dir", "operator_cases_dir",
    "migrate", "list_runs", "get_run", "list_results", "get_result",
    "evaluate_output", "run_case", "start_run",
    "list_approved_results",
    "rate_result", "set_result_approval", "summarize_run",
]
