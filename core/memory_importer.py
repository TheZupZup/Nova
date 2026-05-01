from dataclasses import dataclass, field
from typing import List

MIN_CONTENT_LENGTH = 10

_SENSITIVE_KEYWORDS = (
    "password",
    "passwd",
    "token",
    "secret",
    "api key",
    "api_key",
    "private key",
    "private_key",
    "credential",
    "auth_token",
)

# A single whitespace-free alphanumeric token this long looks like a secret.
_RANDOM_STRING_MIN_LEN = 20


@dataclass
class MemoryImportCandidate:
    category: str
    content: str
    source: str = field(default="import")
    priority: str = field(default="normal")
    flags: tuple = field(default_factory=tuple)
    duplicate: bool = False


@dataclass
class MemoryImportPreview:
    candidates: List[MemoryImportCandidate]
    total: int
    categories: List[str]
    warnings: List[str]
    rejected_count: int = 0
    duplicate_count: int = 0
    flagged_count: int = 0


def _normalize(text: str) -> str:
    """Lowercase and collapse whitespace for comparison."""
    return " ".join(text.lower().split())


def _flag_sensitive(content: str) -> tuple:
    """Return flag strings when content looks like it might contain secrets."""
    flags = []
    lower = content.lower()

    for keyword in _SENSITIVE_KEYWORDS:
        if keyword in lower:
            flags.append("possible_secret")
            break

    # Long alphanumeric token with no spaces is suspicious (e.g. an API key value).
    for word in content.split():
        if len(word) >= _RANDOM_STRING_MIN_LEN and word.isalnum():
            flags.append("suspicious_string")
            break

    return tuple(flags)


def build_memory_import_preview(
    text: str,
    existing_contents: list[str] | None = None,
) -> MemoryImportPreview:
    """Return a preview of what would be imported without writing to the database."""
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

    # Mark duplicates against caller-supplied existing content.
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

    # Flag candidates that look like they contain sensitive data.
    flagged_count = 0
    for candidate in candidates:
        flags = _flag_sensitive(candidate.content)
        if flags:
            candidate.flags = flags
            flagged_count += 1

    seen: dict[str, int] = {}
    for c in candidates:
        seen.setdefault(c.category, 0)
        seen[c.category] += 1
    categories = list(seen.keys())

    return MemoryImportPreview(
        candidates=candidates,
        total=len(candidates),
        categories=categories,
        warnings=warnings,
        rejected_count=rejected,
        duplicate_count=duplicate_count,
        flagged_count=flagged_count,
    )


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


def parse_markdown_memory_pack(text: str) -> List[MemoryImportCandidate]:
    """Parse a Markdown memory pack into structured memory candidates.

    Headings (##) become categories; bullet points beneath them become entries.
    Content before the first heading and empty/too-short bullets are ignored.
    """
    candidates: List[MemoryImportCandidate] = []
    current_category: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if line.startswith("## "):
            current_category = line[3:].strip()
            continue

        if line.startswith("# "):
            # Top-level title — not a category; ignore content until next ##.
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
