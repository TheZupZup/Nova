from dataclasses import dataclass, field
from typing import List

MIN_CONTENT_LENGTH = 10


@dataclass
class MemoryImportCandidate:
    category: str
    content: str
    source: str = field(default="import")
    priority: str = field(default="normal")


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
