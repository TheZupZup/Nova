from dataclasses import dataclass, field
from typing import List

MIN_CONTENT_LENGTH = 10


@dataclass
class MemoryImportCandidate:
    category: str
    content: str
    source: str = field(default="import")
    priority: str = field(default="normal")


@dataclass
class MemoryImportPreview:
    candidates: List[MemoryImportCandidate]
    total: int
    categories: List[str]
    warnings: List[str]


def build_memory_import_preview(text: str) -> MemoryImportPreview:
    """Return a preview of what would be imported without writing to the database."""
    warnings: List[str] = []

    if not text or not text.strip():
        warnings.append("empty input")
        return MemoryImportPreview(candidates=[], total=0, categories=[], warnings=warnings)

    rejected = _count_short_entries(text)
    candidates = parse_markdown_memory_pack(text)

    if not candidates:
        warnings.append("no valid memory candidates found")

    if rejected:
        warnings.append(f"{rejected} entr{'y' if rejected == 1 else 'ies'} rejected for being too short")

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
            # Top-level heading — not a category, reset
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
