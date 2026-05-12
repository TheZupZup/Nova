"""Local-only memory pack importer (issue #84).

The importer converts a Markdown memory pack into a list of reviewable
candidates and exposes an explicit commit step. It never writes to the
database on its own; persistence is delegated to a caller-supplied
``save_fn``. This keeps parsing, safety scanning, and persistence in
clearly separated layers.

Design contracts:

* Parser logic lives in :func:`parse_markdown_memory_pack`.
* Safety scanning lives in :func:`scan_content_for_flags` and the helpers
  it composes — none of them touch storage or do Markdown parsing.
* Preview generation in :func:`build_memory_import_preview` composes the
  parser, deduper, and scanner but writes nothing.
* Saving requires :func:`commit_memory_import` to be called with
  ``confirm=True``; without that, nothing is persisted.

Safety rules are deterministic regexes/keywords — no ML, no network.
"""

from dataclasses import dataclass, field
import re
from typing import Callable, List

MIN_CONTENT_LENGTH = 10

# A single whitespace-free alphanumeric token this long looks like a secret
# (API keys, OAuth tokens, hex digests, etc.).
_RANDOM_STRING_MIN_LEN = 20

# Keyword tables used by the scanner. They are intentionally small and
# explicit so a contributor can audit them at a glance.
_PASSWORD_KEYWORDS = ("password", "passwd", "passphrase")
_TOKEN_KEYWORDS = (
    "api key",
    "api_key",
    "access token",
    "auth_token",
    "auth token",
    "bearer token",
    "token",
)
_PRIVATE_KEY_KEYWORDS = (
    "private key",
    "private_key",
    "-----begin",
    "begin rsa",
    "begin openssh",
    "ssh-rsa ",
    "ssh-ed25519 ",
)
_GENERIC_SECRET_KEYWORDS = ("secret", "credential")

# Patterns used to spot personal data without an LLM. They are tuned to
# be conservative: false negatives are preferable to false positives in
# a "review-before-save" workflow.
_EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_SSN_PATTERN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CREDIT_CARD_PATTERN = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_PHONE_PATTERN = re.compile(
    r"(?:\+?\d[\s.\-]?){2,}\(?\d{2,4}\)?[\s.\-]?\d{2,4}[\s.\-]?\d{2,4}"
)


@dataclass
class MemoryImportCandidate:
    """A single reviewable memory entry parsed from a memory pack."""

    category: str
    content: str
    source: str = field(default="import")
    priority: str = field(default="normal")
    flags: tuple = field(default_factory=tuple)
    duplicate: bool = False


@dataclass
class MemoryImportPreview:
    """Result of running the parser + safety + dedup pipeline."""

    candidates: List[MemoryImportCandidate]
    total: int
    categories: List[str]
    warnings: List[str]
    rejected_count: int = 0
    duplicate_count: int = 0
    flagged_count: int = 0


@dataclass
class MemoryImportCommitResult:
    """Summary of an explicit, confirmed import commit."""

    saved_count: int = 0
    skipped_flagged: int = 0
    skipped_duplicate: int = 0
    skipped_unconfirmed: int = 0


# ---------------------------------------------------------------------------
# Markdown parser — pure, no DB, no safety scanning.
# ---------------------------------------------------------------------------

def parse_markdown_memory_pack(text: str) -> List[MemoryImportCandidate]:
    """Parse a Markdown memory pack into structured memory candidates.

    Headings (``##``) become categories; bullet points beneath them become
    entries. Content before the first ``##`` heading and empty/too-short
    bullets are ignored. A top-level ``#`` title resets the active
    category so its bullets are not attached to it.
    """
    candidates: List[MemoryImportCandidate] = []
    current_category: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if line.startswith("## "):
            current_category = line[3:].strip()
            continue

        if line.startswith("# "):
            current_category = None
            continue

        if current_category is None:
            continue

        if line.startswith("- "):
            content = line[2:].strip()
            if len(content) >= MIN_CONTENT_LENGTH:
                candidates.append(
                    MemoryImportCandidate(category=current_category, content=content)
                )

    return candidates


# ---------------------------------------------------------------------------
# Safety scanning — pure, no DB, no Markdown parsing.
# ---------------------------------------------------------------------------

def scan_content_for_flags(content: str) -> tuple[str, ...]:
    """Return a tuple of safety flag strings for ``content``.

    Multiple flags can fire for the same entry. The umbrella flag
    ``possible_secret`` is added whenever a more specific
    password/token/private-key flag fires, so callers that only check
    for the umbrella flag still see all credential-like entries.
    """
    flags: list[str] = []
    lower = content.lower()

    matched_specific_secret = False

    if any(k in lower for k in _PASSWORD_KEYWORDS):
        flags.append("possible_password")
        matched_specific_secret = True

    if any(k in lower for k in _TOKEN_KEYWORDS):
        flags.append("possible_token")
        matched_specific_secret = True

    if any(k in lower for k in _PRIVATE_KEY_KEYWORDS):
        flags.append("possible_private_key")
        matched_specific_secret = True

    if matched_specific_secret or any(k in lower for k in _GENERIC_SECRET_KEYWORDS):
        if "possible_secret" not in flags:
            flags.append("possible_secret")

    for word in content.split():
        if len(word) >= _RANDOM_STRING_MIN_LEN and word.isalnum():
            flags.append("suspicious_string")
            if "possible_token" not in flags:
                flags.append("possible_token")
            if "possible_secret" not in flags:
                flags.append("possible_secret")
            break

    if _looks_like_sensitive_personal_data(content):
        flags.append("possible_sensitive_personal_data")

    return tuple(flags)


def _looks_like_sensitive_personal_data(content: str) -> bool:
    """Conservative regex check for emails, SSN, phones, or credit cards."""
    if _EMAIL_PATTERN.search(content):
        return True
    if _SSN_PATTERN.search(content):
        return True
    if _looks_like_credit_card(content):
        return True
    if _looks_like_phone(content):
        return True
    return False


def _looks_like_credit_card(content: str) -> bool:
    """Detect a 13–19 digit sequence after stripping separators."""
    for match in _CREDIT_CARD_PATTERN.finditer(content):
        digits = re.sub(r"[ -]", "", match.group(0))
        if 13 <= len(digits) <= 19 and digits.isdigit():
            return True
    return False


def _looks_like_phone(content: str) -> bool:
    """Heuristic phone-number detection (>= 10 digits, with separators)."""
    for match in _PHONE_PATTERN.finditer(content):
        digits = re.sub(r"\D", "", match.group(0))
        if 10 <= len(digits) <= 15:
            return True
    return False


# ---------------------------------------------------------------------------
# Preview pipeline — composes parser + dedup + scanner.
# ---------------------------------------------------------------------------

def build_memory_import_preview(
    text: str,
    existing_contents: list[str] | None = None,
) -> MemoryImportPreview:
    """Return a preview of what would be imported, without writing anything."""
    warnings: List[str] = []

    if not text or not text.strip():
        warnings.append("empty input")
        return MemoryImportPreview(
            candidates=[],
            total=0,
            categories=[],
            warnings=warnings,
            rejected_count=0,
            duplicate_count=0,
            flagged_count=0,
        )

    rejected = _count_short_entries(text)
    candidates = parse_markdown_memory_pack(text)

    if not candidates:
        warnings.append("no valid memory candidates found")

    if rejected:
        warnings.append(
            f"{rejected} entr{'y' if rejected == 1 else 'ies'} rejected for being too short"
        )

    normalized_existing: set[str] = set()
    if existing_contents:
        for entry in existing_contents:
            normalized_existing.add(_normalize(entry))

    duplicate_count = 0
    for candidate in candidates:
        if _normalize(candidate.content) in normalized_existing:
            candidate.duplicate = True
            duplicate_count += 1

    if duplicate_count:
        warnings.append(
            f"{duplicate_count} duplicate{'s' if duplicate_count != 1 else ''} found"
        )

    flagged_count = 0
    for candidate in candidates:
        flags = list(scan_content_for_flags(candidate.content))
        if candidate.duplicate and "duplicate" not in flags:
            flags.append("duplicate")
        if flags:
            candidate.flags = tuple(flags)
            if any(f != "duplicate" for f in flags):
                flagged_count += 1

    categories = list(dict.fromkeys(c.category for c in candidates))

    return MemoryImportPreview(
        candidates=candidates,
        total=len(candidates),
        categories=categories,
        warnings=warnings,
        rejected_count=rejected,
        duplicate_count=duplicate_count,
        flagged_count=flagged_count,
    )


# ---------------------------------------------------------------------------
# Explicit confirmation step — persistence is delegated to ``save_fn``.
# ---------------------------------------------------------------------------

def commit_memory_import(
    preview: MemoryImportPreview,
    user_id: int,
    save_fn: Callable[[str, str, int], None],
    confirm: bool = False,
    allow_flagged: bool = False,
    allow_duplicates: bool = False,
) -> MemoryImportCommitResult:
    """Persist approved candidates from ``preview`` via ``save_fn``.

    Nothing is saved unless ``confirm=True``. Flagged and duplicate
    candidates are skipped by default and only included when the caller
    explicitly opts in via ``allow_flagged`` / ``allow_duplicates``.
    ``save_fn`` is invoked as ``save_fn(category, content, user_id)``.
    The function is injected so this module stays free of database
    coupling — see :mod:`core.memory` for the standard implementation.
    """
    result = MemoryImportCommitResult()

    if not confirm:
        result.skipped_unconfirmed = preview.total
        return result

    for candidate in preview.candidates:
        if candidate.duplicate and not allow_duplicates:
            result.skipped_duplicate += 1
            continue
        if _is_flagged(candidate) and not allow_flagged:
            result.skipped_flagged += 1
            continue
        save_fn(candidate.category, candidate.content, user_id)
        result.saved_count += 1

    return result


# ---------------------------------------------------------------------------
# Internal helpers.
# ---------------------------------------------------------------------------

def _is_flagged(candidate: MemoryImportCandidate) -> bool:
    """True when a candidate has at least one non-``duplicate`` safety flag."""
    return any(f != "duplicate" for f in candidate.flags)


def _normalize(text: str) -> str:
    """Lowercase and collapse whitespace for comparison."""
    return " ".join(text.lower().split())


def _count_short_entries(text: str) -> int:
    """Count bullet entries that exist but are rejected for being too short."""
    count = 0
    in_category = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            in_category = True
            continue
        if line.startswith("# "):
            in_category = False
            continue
        if in_category and line.startswith("- "):
            content = line[2:].strip()
            if content and len(content) < MIN_CONTENT_LENGTH:
                count += 1
    return count
