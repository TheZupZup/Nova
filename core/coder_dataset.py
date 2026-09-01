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
   task prompt snapshot and the model's answer, never a user conversation;
2. an operator set ``approved = 1`` on it deliberately
   (:func:`core.model_eval.set_result_approval`); and
3. the operator names its id in the export request.

Approval is never inferred from a passing constraint check, a thumbs-up,
or anything else. There is no "export everything" call.

What is never exported
----------------------
* **Chat conversations.** No user message, assistant reply, or thread
  from ``/chat`` is reachable from here. The export reads evaluation
  results only through :mod:`core.model_eval`.
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

The prompt paired with an answer is the immutable snapshot stored on the
evaluation result at generation time. Editing, overriding, or deleting a
case later can therefore never silently pair an approved answer with a
different task.

Everything is written under Nova's own exports directory, with a
validated filename, on the local filesystem. There is no upload, no
registry push, and no cloud dependency anywhere in this module.
"""

from __future__ import annotations

import json
import logging
import os
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
    approved, lacks the immutable prompt snapshot, produced no usable
    output, or when any content looks like a credential or a personal
    identifier. A refusal names the reason without echoing offending
    content.
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
        if result.get("output_truncated"):
            raise DatasetExportError(
                f"Result {raw} stored only a truncated copy of the model's "
                f"answer, so exporting it would teach an answer that stops "
                f"mid-thought. Re-run the case if you need this example."
            )

        # Never reconstruct from the current case definition. Cases are
        # operator-editable and may disappear; only the prompt stored with
        # the result is guaranteed to be the prompt that produced this
        # completion. Empty snapshots identify legacy rows from an earlier
        # branch revision and are refused rather than guessed.
        prompt = _clean(result.get("prompt_snapshot")).strip()
        if not prompt:
            raise DatasetExportError(
                f"Result {raw} predates prompt snapshots and cannot be exported "
                f"safely. Re-run that evaluation case and approve the new result."
            )
        completion = _clean(result["output"]).strip()
        if not completion:
            raise DatasetExportError(f"Result {raw} has no usable prompt/answer pair.")
        if len(prompt) + len(completion) > MAX_EXAMPLE_CHARS:
            raise DatasetExportError(f"Result {raw} is too large to export.")

        # Provenance is stored on the result row (never re-read from a
        # case file that may since have changed).
        case_source = result.get("case_source") or ""
        metadata = {
            "result_id": result["id"],
            "run_id": result["run_id"],
            "case_id": result["case_id"],
            "case_source": case_source,
            "model": result["model"],
            "context_size": result["context_size"],
            "elapsed_ms": result["elapsed_ms"],
            "constraints_passed": result["constraints_passed"],
            "constraints_total": result["constraints_total"],
            "human_rating": result["human_rating"],
            "approved_at": result["approved_at"],
        }

        # Scan everything that will actually be written — not just the
        # two big fields. Provenance is free-form operator text: a case
        # ``source`` naming a reporter's email would otherwise ride into
        # the corpus untouched, straight past this gate. Scanning the
        # metadata generically (rather than naming one field) keeps that
        # true for any field added later.
        #
        # The system prompt is deliberately absent from what we scan and
        # from what we write — it is never part of an exported example.
        found = _scan_for_secrets(prompt) or _scan_for_secrets(completion)
        if found is None:
            for value in metadata.values():
                if isinstance(value, str):
                    found = _scan_for_secrets(value)
                    if found is not None:
                        break
        if found is not None:
            raise DatasetExportError(
                f"Result {raw} contains content matching a credential or "
                f"personal-data pattern. Nothing was exported. Review the "
                f"evaluation result or withdraw its approval."
            )

        examples.append(DatasetExample(
            prompt=prompt,
            completion=completion,
            metadata=metadata,
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
        # ``O_EXCL`` so a concurrent writer cannot be clobbered, and mode
        # ``0600`` at *creation* rather than a chmod afterwards — a chmod
        # leaves a window in which the file is already on disk and still
        # world-readable. The default 0644 an ``open()`` would produce
        # under the usual 022 umask hands every local account on a shared
        # host the prompts and completions an operator approved. The
        # credential scan does not make that safe: it looks for
        # secret-shaped strings, while the export's whole purpose is to
        # carry proprietary task and code content. Matches how the rest
        # of Nova writes sensitive exports (``core/data_export.py``).
        fd = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
    except FileExistsError:
        raise DatasetExportError(
            "A file with that name already exists; choose another name."
        ) from None
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

    # Uncapped on purpose: filtering the newest-N browsing list would
    # drop older approved rows from the only endpoint that discovers
    # them, so an operator could not find an id that is still exportable.
    approved = model_eval.list_approved_results(db_path=db_path)
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
