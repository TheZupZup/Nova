"""
Opt-in dataset export for a future ``nova-coder`` — nothing automatic.

This module prepares the *path* to a Nova-specific fine-tuned coding
model without taking a single step down it. **No model is trained here,
nothing is collected in the background, and nothing leaves the host.**
It only turns evaluation results an operator has explicitly approved,
one by one, into a clean JSONL file they can inspect and then do
whatever they want with.

The gate
--------
Exportable means *all* of:

1. the record is a :mod:`core.model_eval` result — an operator-authored
   task prompt and the model's answer, never a user conversation;
2. an operator set ``approved = 1`` on it deliberately
   (:func:`core.model_eval.set_result_approval`); and
3. the operator names its id in the export request.

Approval is never inferred from a passing constraint check, a thumbs-up,
or anything else. There is no "export everything" call.

What is never exported
----------------------
* **Chat conversations.** No user message, assistant reply, or thread
  from ``/chat`` is reachable from here. The export reads exactly two
  tables' worth of evaluation data.
* **System and safety prompts.** Nova's identity contract, the safety
  contract, personalization blocks, and the harness preamble are all
  omitted — an exported example is a bare ``user`` / ``assistant`` pair.
* **Memory.** Neither structured memories nor natural memories are read.
* **Credentials and personal data.** Every example is scanned with the
  same secret-shaped patterns the feedback module uses, plus an email
  and private-key check. A match **refuses the whole export** rather
  than silently dropping a record, so an operator always learns that
  something sensitive was in their data.
* **Repository file contents.** Code-mode repository briefings are built
  per-turn and are never stored, so they cannot reach an export.

Everything is written under Nova's own exports directory, with a
validated filename, on the local filesystem. There is no upload, no
registry push, and no cloud dependency anywhere in this module.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

MAX_EXAMPLES_PER_EXPORT = 500
MAX_EXAMPLE_CHARS = 40_000
EXPORT_SUBDIR = "nova-coder-datasets"

_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")

# Secret-shaped content. Deliberately the same family of patterns the
# feedback module refuses to store, extended with private-key headers and
# bare email addresses — a training corpus is the last place either
# belongs.
_SECRET_PATTERNS = (
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(
        r"(?i)\b(?:password|passwd|pwd|token|secret|api[_-]?key|bearer)"
        r"\s*[:=]\s*\S{6,}"
    ),
    re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
)

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class DatasetExportError(ValueError):
    """An export was refused. The message is safe to show an operator."""


@dataclass(frozen=True)
class DatasetExample:
    """One training example: a task prompt and an approved answer.

    ``metadata`` carries provenance an operator needs to audit the
    corpus later — which model produced it, which case it came from, how
    it scored. It never carries a user id, a conversation id, or any
    prompt Nova would not show the user.
    """

    prompt: str
    completion: str
    metadata: dict

    def as_jsonl_record(self) -> dict:
        return {
            "messages": [
                {"role": "user", "content": self.prompt},
                {"role": "assistant", "content": self.completion},
            ],
            "metadata": self.metadata,
        }


def _scan_for_secrets(text: str) -> Optional[str]:
    """The name of the first secret-shaped pattern found, else ``None``."""
    for pattern in _SECRET_PATTERNS:
        if pattern.search(text or ""):
            return pattern.pattern[:60]
    return None


def _clean(text: object) -> str:
    if not isinstance(text, str):
        return ""
    return _CONTROL_CHAR_RE.sub("", text)


def build_examples(
    result_ids: Sequence[int],
    db_path: Optional[str] = None,
) -> list[DatasetExample]:
    """Turn explicitly-named, operator-approved results into examples.

    Refuses — with nothing produced — when an id is unknown, is not
    approved, produced no usable output, or when any content looks like a
    credential or a personal identifier. A refusal names the reason
    without echoing the offending text.
    """
    from core import model_eval

    if not result_ids:
        raise DatasetExportError("Select at least one approved result to export.")
    if len(result_ids) > MAX_EXAMPLES_PER_EXPORT:
        raise DatasetExportError(
            f"At most {MAX_EXAMPLES_PER_EXPORT} results can be exported at once."
        )

    seen: set[int] = set()
    examples: list[DatasetExample] = []
    for raw in result_ids:
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise DatasetExportError("Result ids must be integers.")
        if raw in seen:
            continue
        seen.add(raw)

        result = model_eval.get_result(raw, db_path=db_path)
        if result is None:
            raise DatasetExportError(f"Result {raw} does not exist.")
        if not result["approved"]:
            raise DatasetExportError(
                f"Result {raw} has not been approved for export. Approve each "
                f"example explicitly first."
            )
        if result["error"]:
            raise DatasetExportError(
                f"Result {raw} recorded a backend error and has no usable output."
            )

        case = model_eval.get_case(result["case_id"])
        if case is None:
            raise DatasetExportError(
                f"Result {raw} refers to case '{result['case_id']}', which is "
                f"no longer available. Its prompt cannot be reconstructed."
            )

        prompt = _clean(case.prompt).strip()
        completion = _clean(result["output"]).strip()
        if not prompt or not completion:
            raise DatasetExportError(f"Result {raw} has no usable prompt/answer pair.")
        if len(prompt) + len(completion) > MAX_EXAMPLE_CHARS:
            raise DatasetExportError(f"Result {raw} is too large to export.")

        # The system prompt is deliberately absent from what we scan and
        # from what we write — it is never part of an exported example.
        found = _scan_for_secrets(prompt) or _scan_for_secrets(completion)
        if found is not None:
            raise DatasetExportError(
                f"Result {raw} contains content matching a credential or "
                f"personal-data pattern. Nothing was exported. Review and "
                f"edit the case or withdraw its approval."
            )

        examples.append(DatasetExample(
            prompt=prompt,
            completion=completion,
            metadata={
                "result_id": result["id"],
                "run_id": result["run_id"],
                "case_id": result["case_id"],
                "case_source": case.source,
                "model": result["model"],
                "context_size": result["context_size"],
                "elapsed_ms": result["elapsed_ms"],
                "constraints_passed": result["constraints_passed"],
                "constraints_total": result["constraints_total"],
                "human_rating": result["human_rating"],
                "approved_at": result["approved_at"],
            },
        ))
    return examples


def export_dir() -> Path:
    """The local directory dataset files are written into."""
    from core import paths

    return paths.exports_dir() / EXPORT_SUBDIR


def _validate_filename(name: object) -> str:
    if not isinstance(name, str) or not name.strip():
        raise DatasetExportError("A filename is required.")
    candidate = name.strip()
    if not candidate.endswith(".jsonl"):
        candidate = f"{candidate}.jsonl"
    stem = candidate[: -len(".jsonl")]
    if not _FILENAME_RE.match(stem):
        raise DatasetExportError(
            "Filename may only contain letters, digits, '.', '_' and '-'."
        )
    return candidate


def export_jsonl(
    result_ids: Sequence[int],
    filename: Optional[str] = None,
    db_path: Optional[str] = None,
) -> dict:
    """Write approved examples to a local JSONL file. Never overwrites.

    Returns ``{"path", "filename", "example_count", "models", "bytes"}``.
    Nothing is uploaded, registered, or sent anywhere: the operator ends
    up with one file on their own disk.
    """
    examples = build_examples(result_ids, db_path=db_path)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # A blank / whitespace-only name means "you pick one", not an error.
    requested = filename.strip() if isinstance(filename, str) else ""
    target_name = _validate_filename(requested or f"nova-coder-{stamp}.jsonl")

    directory = export_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise DatasetExportError("The export directory could not be created.") from None

    target = directory / target_name
    if target.exists():
        raise DatasetExportError(
            "A file with that name already exists; choose another name."
        )

    body = "".join(
        json.dumps(example.as_jsonl_record(), ensure_ascii=False) + "\n"
        for example in examples
    )
    try:
        # ``x`` so a concurrent writer cannot be clobbered.
        with open(target, "x", encoding="utf-8") as handle:
            handle.write(body)
    except OSError:
        raise DatasetExportError("The dataset file could not be written.") from None

    models = sorted({e.metadata["model"] for e in examples})
    return {
        "path": str(target),
        "filename": target_name,
        "example_count": len(examples),
        "models": models,
        "bytes": len(body.encode("utf-8")),
    }


def describe_export_readiness(db_path: Optional[str] = None) -> dict:
    """How many results are approved and where an export would be written.

    A read-only summary for the admin surface. It reports counts, never
    content, and starts no export.
    """
    from core import model_eval

    approved = [r for r in model_eval.list_results(db_path=db_path) if r["approved"]]
    return {
        "approved_count": len(approved),
        "approved_result_ids": [r["id"] for r in approved],
        "export_dir": str(export_dir()),
        "max_examples_per_export": MAX_EXAMPLES_PER_EXPORT,
        "automatic_export": False,
    }


__all__ = [
    "MAX_EXAMPLES_PER_EXPORT", "EXPORT_SUBDIR",
    "DatasetExportError", "DatasetExample",
    "build_examples", "export_jsonl", "export_dir",
    "describe_export_readiness",
]
