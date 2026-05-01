from core.memory_importer import (
    MemoryImportCandidate,
    MemoryImportPreview,
    build_memory_import_preview,
    parse_markdown_memory_pack,
)


SAMPLE_PACK = """
# Nova Memory Pack

## Git workflow
- The user wants main to stay stable.
- The user uses feature/*, fix/*, and hotfix/* branches for PRs.

## Response style
- The user prefers clear, direct, step-by-step answers.
- The user wants warnings before risky Git actions.
"""


def test_headings_become_categories():
    results = parse_markdown_memory_pack(SAMPLE_PACK)
    categories = {r.category for r in results}
    assert "Git workflow" in categories
    assert "Response style" in categories


def test_bullets_become_candidates():
    results = parse_markdown_memory_pack(SAMPLE_PACK)
    contents = [r.content for r in results]
    assert "The user wants main to stay stable." in contents
    assert "The user uses feature/*, fix/*, and hotfix/* branches for PRs." in contents


def test_multiple_categories():
    results = parse_markdown_memory_pack(SAMPLE_PACK)
    git = [r for r in results if r.category == "Git workflow"]
    style = [r for r in results if r.category == "Response style"]
    assert len(git) == 2
    assert len(style) == 2


def test_empty_bullets_ignored():
    text = "## Tools\n- \n-   \n- A valid entry here.\n"
    results = parse_markdown_memory_pack(text)
    assert len(results) == 1
    assert results[0].content == "A valid entry here."


def test_too_short_entries_rejected():
    text = "## Info\n- ok\n- hi\n- This entry is long enough.\n"
    results = parse_markdown_memory_pack(text)
    assert all(len(r.content) >= 10 for r in results)
    assert len(results) == 1
    assert results[0].content == "This entry is long enough."


def test_content_before_first_heading_ignored():
    text = "Some preamble text\n- ignored bullet\n## Category\n- Valid entry here.\n"
    results = parse_markdown_memory_pack(text)
    assert len(results) == 1
    assert results[0].category == "Category"


def test_source_is_import():
    results = parse_markdown_memory_pack(SAMPLE_PACK)
    assert all(r.source == "import" for r in results)


def test_priority_defaults_to_normal():
    results = parse_markdown_memory_pack(SAMPLE_PACK)
    assert all(r.priority == "normal" for r in results)


def test_empty_input_returns_empty_list():
    assert parse_markdown_memory_pack("") == []
    assert parse_markdown_memory_pack("   \n\n  ") == []


def test_preserves_punctuation_and_content():
    text = "## Style\n- Use commas, semicolons; and dashes — carefully.\n"
    results = parse_markdown_memory_pack(text)
    assert len(results) == 1
    assert results[0].content == "Use commas, semicolons; and dashes — carefully."


def test_returns_dataclass_instances():
    results = parse_markdown_memory_pack(SAMPLE_PACK)
    for r in results:
        assert isinstance(r, MemoryImportCandidate)


def test_top_level_heading_does_not_become_category():
    text = "# Top Level\n- ignored\n## Real Category\n- Valid entry here.\n"
    results = parse_markdown_memory_pack(text)
    assert all(r.category != "Top Level" for r in results)
    assert len(results) == 1


# ---------------------------------------------------------------------------
# Phase 2: build_memory_import_preview
# ---------------------------------------------------------------------------

def test_preview_returns_candidates_from_valid_markdown():
    preview = build_memory_import_preview(SAMPLE_PACK)
    assert isinstance(preview, MemoryImportPreview)
    assert len(preview.candidates) == 4
    assert all(isinstance(c, MemoryImportCandidate) for c in preview.candidates)


def test_preview_total_count():
    preview = build_memory_import_preview(SAMPLE_PACK)
    assert preview.total == len(preview.candidates)
    assert preview.total == 4


def test_preview_lists_categories():
    preview = build_memory_import_preview(SAMPLE_PACK)
    assert "Git workflow" in preview.categories
    assert "Response style" in preview.categories
    assert len(preview.categories) == 2


def test_preview_empty_input_warning():
    for empty in ("", "   ", "\n\n"):
        preview = build_memory_import_preview(empty)
        assert "empty input" in preview.warnings
        assert preview.total == 0
        assert preview.candidates == []
        assert preview.categories == []


def test_preview_no_valid_candidates_warning():
    # Every bullet is too short → no valid candidates
    text_no_valid = "## Info\n- ok\n- hi\n"
    preview = build_memory_import_preview(text_no_valid)
    assert "no valid memory candidates found" in preview.warnings
    assert preview.total == 0


def test_preview_short_entries_warning():
    text = "## Info\n- ok\n- hi\n- This entry is long enough.\n"
    preview = build_memory_import_preview(text)
    short_warnings = [w for w in preview.warnings if "rejected for being too short" in w]
    assert len(short_warnings) == 1
    assert "2" in short_warnings[0]


def test_preview_does_not_save_anything():
    import core.memory_importer as mod
    # The module must not expose a save_memory callable — confirming no DB writes are wired in.
    assert not hasattr(mod, "save_memory")
    # Calling preview must not raise and must return data without side effects.
    preview = build_memory_import_preview(SAMPLE_PACK)
    assert preview.total > 0


def test_preview_is_deterministic():
    preview_a = build_memory_import_preview(SAMPLE_PACK)
    preview_b = build_memory_import_preview(SAMPLE_PACK)
    assert preview_a.total == preview_b.total
    assert preview_a.categories == preview_b.categories
    assert preview_a.warnings == preview_b.warnings
    assert [c.content for c in preview_a.candidates] == [c.content for c in preview_b.candidates]


def test_preview_no_warnings_on_clean_input():
    preview = build_memory_import_preview(SAMPLE_PACK)
    assert preview.warnings == []
