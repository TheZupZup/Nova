from core.memory_importer import (
    MemoryImportCandidate,
    MemoryImportCommitResult,
    MemoryImportPreview,
    build_memory_import_preview,
    commit_memory_import,
    parse_markdown_memory_pack,
    scan_content_for_flags,
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


# ---------------------------------------------------------------------------
# parse_markdown_memory_pack
# ---------------------------------------------------------------------------

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


def test_candidate_flags_default_to_empty_tuple():
    results = parse_markdown_memory_pack(SAMPLE_PACK)
    for r in results:
        assert hasattr(r, "flags")
        assert r.flags == ()


def test_candidate_duplicate_defaults_to_false():
    results = parse_markdown_memory_pack(SAMPLE_PACK)
    for r in results:
        assert hasattr(r, "duplicate")
        assert r.duplicate is False


# ---------------------------------------------------------------------------
# build_memory_import_preview — basic
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


def test_preview_rejected_count():
    text = "## Info\n- ok\n- hi\n- This entry is long enough.\n"
    preview = build_memory_import_preview(text)
    assert preview.rejected_count == 2


def test_preview_rejected_count_zero_on_clean_input():
    preview = build_memory_import_preview(SAMPLE_PACK)
    assert preview.rejected_count == 0


def test_preview_does_not_save_anything():
    import core.memory_importer as mod
    assert not hasattr(mod, "save_memory")
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


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

def test_duplicate_detection_marks_candidate():
    text = "## Info\n- The user wants main to stay stable.\n"
    existing = ["The user wants main to stay stable."]
    preview = build_memory_import_preview(text, existing_contents=existing)
    assert preview.candidates[0].duplicate is True


def test_duplicate_count_is_correct():
    text = (
        "## Info\n"
        "- The user wants main to stay stable.\n"
        "- Something entirely new here.\n"
    )
    existing = ["The user wants main to stay stable."]
    preview = build_memory_import_preview(text, existing_contents=existing)
    assert preview.duplicate_count == 1
    non_dup = [c for c in preview.candidates if not c.duplicate]
    assert len(non_dup) == 1


def test_duplicate_detection_is_case_insensitive():
    text = "## Info\n- THE USER WANTS MAIN TO STAY STABLE.\n"
    existing = ["The user wants main to stay stable."]
    preview = build_memory_import_preview(text, existing_contents=existing)
    assert preview.candidates[0].duplicate is True
    assert preview.duplicate_count == 1


def test_duplicate_detection_ignores_extra_whitespace():
    text = "## Info\n- The  user   wants main to stay stable.\n"
    existing = ["The user wants main to stay stable."]
    preview = build_memory_import_preview(text, existing_contents=existing)
    assert preview.candidates[0].duplicate is True


def test_no_duplicates_without_existing_contents():
    preview = build_memory_import_preview(SAMPLE_PACK, existing_contents=None)
    assert preview.duplicate_count == 0
    assert all(not c.duplicate for c in preview.candidates)


def test_no_duplicates_with_empty_existing_contents():
    preview = build_memory_import_preview(SAMPLE_PACK, existing_contents=[])
    assert preview.duplicate_count == 0


def test_duplicate_candidates_are_marked_not_dropped():
    text = "## Info\n- The user wants main to stay stable.\n"
    existing = ["The user wants main to stay stable."]
    preview = build_memory_import_preview(text, existing_contents=existing)
    # Duplicate is kept in candidates, just marked.
    assert preview.total == 1
    assert preview.candidates[0].duplicate is True


def test_duplicate_warning_added_when_duplicates_found():
    text = "## Info\n- The user wants main to stay stable.\n"
    existing = ["The user wants main to stay stable."]
    preview = build_memory_import_preview(text, existing_contents=existing)
    assert any("duplicate" in w for w in preview.warnings)


def test_preview_duplicate_count_field_exists():
    preview = build_memory_import_preview(SAMPLE_PACK)
    assert hasattr(preview, "duplicate_count")
    assert preview.duplicate_count == 0


# ---------------------------------------------------------------------------
# Sensitive / secret flagging
# ---------------------------------------------------------------------------

def test_password_entry_is_flagged():
    text = "## Creds\n- My password is hunter2 for the work account.\n"
    preview = build_memory_import_preview(text)
    assert preview.flagged_count == 1
    assert "possible_secret" in preview.candidates[0].flags


def test_token_entry_is_flagged():
    text = "## Access\n- The api key is stored in the config file.\n"
    preview = build_memory_import_preview(text)
    flagged = [c for c in preview.candidates if c.flags]
    assert len(flagged) == 1


def test_private_key_entry_is_flagged():
    text = "## Security\n- The private key is saved in the secrets folder.\n"
    preview = build_memory_import_preview(text)
    assert preview.flagged_count == 1


def test_flagged_count_is_correct():
    text = (
        "## Creds\n"
        "- My password is xyz for the login.\n"
        "- The user prefers dark mode settings.\n"
    )
    preview = build_memory_import_preview(text)
    assert preview.flagged_count == 1


def test_flagged_entries_still_appear_in_preview():
    text = "## Secrets\n- My password is hunter2 for the work account.\n"
    preview = build_memory_import_preview(text)
    assert preview.total == 1
    assert len(preview.candidates) == 1


def test_suspicious_long_string_is_flagged():
    token = "a" * 25  # 25 alphanumeric chars, no spaces
    text = f"## Tokens\n- The access token is {token} and it works.\n"
    preview = build_memory_import_preview(text)
    assert preview.flagged_count == 1
    assert "suspicious_string" in preview.candidates[0].flags


def test_clean_entries_not_flagged():
    preview = build_memory_import_preview(SAMPLE_PACK)
    assert preview.flagged_count == 0
    assert all(c.flags == () for c in preview.candidates)


def test_preview_flagged_count_field_exists():
    preview = build_memory_import_preview(SAMPLE_PACK)
    assert hasattr(preview, "flagged_count")


# ---------------------------------------------------------------------------
# No external dependencies or DB access
# ---------------------------------------------------------------------------

def test_no_external_dependencies():
    import importlib.util
    spec = importlib.util.find_spec("core.memory_importer")
    with open(spec.origin) as f:
        source = f.read()
    for lib in ("requests", "numpy", "pandas", "httpx", "aiohttp", "sqlalchemy"):
        assert lib not in source


def test_no_sqlite_usage_in_importer():
    import importlib.util
    spec = importlib.util.find_spec("core.memory_importer")
    with open(spec.origin) as f:
        source = f.read()
    assert "sqlite3" not in source
    assert "save_memory" not in source


# ---------------------------------------------------------------------------
# Granular safety flags
# ---------------------------------------------------------------------------

def test_scan_returns_empty_tuple_for_clean_content():
    assert scan_content_for_flags("The user prefers dark mode.") == ()


def test_password_emits_possible_password_flag():
    flags = scan_content_for_flags("My password is hunter2 for the work account.")
    assert "possible_password" in flags
    # Umbrella flag still present for callers that only check the generic one.
    assert "possible_secret" in flags


def test_passphrase_emits_possible_password_flag():
    flags = scan_content_for_flags("The passphrase for the disk is on a sticky note.")
    assert "possible_password" in flags


def test_api_key_emits_possible_token_flag():
    flags = scan_content_for_flags("The api key is stored in the config file.")
    assert "possible_token" in flags
    assert "possible_secret" in flags


def test_bearer_token_emits_possible_token_flag():
    flags = scan_content_for_flags("Send the bearer token in the Authorization header.")
    assert "possible_token" in flags


def test_private_key_emits_possible_private_key_flag():
    flags = scan_content_for_flags("The private key lives at ~/.ssh/id_rsa.")
    assert "possible_private_key" in flags
    assert "possible_secret" in flags


def test_pem_header_emits_possible_private_key_flag():
    flags = scan_content_for_flags("-----BEGIN OPENSSH PRIVATE KEY----- pasted here")
    assert "possible_private_key" in flags


def test_email_emits_possible_sensitive_personal_data_flag():
    flags = scan_content_for_flags("Contact me at jane.doe@example.com for questions.")
    assert "possible_sensitive_personal_data" in flags


def test_phone_number_emits_possible_sensitive_personal_data_flag():
    flags = scan_content_for_flags("Reach me at +1 555-123-4567 anytime.")
    assert "possible_sensitive_personal_data" in flags


def test_ssn_emits_possible_sensitive_personal_data_flag():
    flags = scan_content_for_flags("My SSN is 123-45-6789, do not share.")
    assert "possible_sensitive_personal_data" in flags


def test_credit_card_emits_possible_sensitive_personal_data_flag():
    flags = scan_content_for_flags("Card number 4111 1111 1111 1111 expires soon.")
    assert "possible_sensitive_personal_data" in flags


def test_long_alphanumeric_emits_possible_token_flag():
    token = "a" * 25
    flags = scan_content_for_flags(f"The pasted value is {token} here.")
    assert "possible_token" in flags
    # Existing umbrella flag is also kept.
    assert "possible_secret" in flags
    assert "suspicious_string" in flags


def test_preview_personal_data_increments_flagged_count():
    text = "## Contacts\n- Email me at jane.doe@example.com please.\n"
    preview = build_memory_import_preview(text)
    assert preview.flagged_count == 1
    assert "possible_sensitive_personal_data" in preview.candidates[0].flags


def test_preview_emits_specific_password_flag():
    text = "## Creds\n- My password is hunter2 for the work account.\n"
    preview = build_memory_import_preview(text)
    flags = preview.candidates[0].flags
    assert "possible_password" in flags
    assert "possible_secret" in flags


def test_preview_emits_specific_private_key_flag():
    text = "## Security\n- The private key is saved in the secrets folder.\n"
    preview = build_memory_import_preview(text)
    flags = preview.candidates[0].flags
    assert "possible_private_key" in flags


def test_duplicate_flag_appears_on_candidate_flags():
    text = "## Info\n- The user wants main to stay stable.\n"
    existing = ["The user wants main to stay stable."]
    preview = build_memory_import_preview(text, existing_contents=existing)
    assert "duplicate" in preview.candidates[0].flags
    assert preview.candidates[0].duplicate is True


def test_duplicate_only_flag_does_not_count_as_sensitive_flagged():
    # A plain duplicate should not bump flagged_count — only sensitive flags do.
    text = "## Info\n- The user wants main to stay stable.\n"
    existing = ["The user wants main to stay stable."]
    preview = build_memory_import_preview(text, existing_contents=existing)
    assert preview.flagged_count == 0
    assert preview.duplicate_count == 1


def test_clean_entry_has_no_flags():
    flags = scan_content_for_flags("The user prefers concise, direct answers.")
    assert flags == ()


# ---------------------------------------------------------------------------
# commit_memory_import — explicit confirmation
# ---------------------------------------------------------------------------

class _RecorderSave:
    """Stand-in for ``core.memory.save_memory(category, content, user_id)``."""

    def __init__(self):
        self.calls: list[tuple[str, str, int]] = []

    def __call__(self, category: str, content: str, user_id: int) -> None:
        self.calls.append((category, content, user_id))


def test_commit_without_confirmation_saves_nothing():
    preview = build_memory_import_preview(SAMPLE_PACK)
    saver = _RecorderSave()
    result = commit_memory_import(preview, user_id=1, save_fn=saver)
    assert isinstance(result, MemoryImportCommitResult)
    assert result.saved_count == 0
    assert result.skipped_unconfirmed == preview.total
    assert saver.calls == []


def test_commit_with_confirmation_saves_clean_candidates():
    preview = build_memory_import_preview(SAMPLE_PACK)
    saver = _RecorderSave()
    result = commit_memory_import(preview, user_id=7, save_fn=saver, confirm=True)
    assert result.saved_count == preview.total
    assert len(saver.calls) == preview.total
    assert all(call[2] == 7 for call in saver.calls)
    categories = {call[0] for call in saver.calls}
    assert "Git workflow" in categories
    assert "Response style" in categories


def test_commit_skips_flagged_by_default():
    text = (
        "## Notes\n"
        "- The user prefers concise answers always.\n"
        "## Creds\n"
        "- My password is hunter2 for the work account.\n"
    )
    preview = build_memory_import_preview(text)
    saver = _RecorderSave()
    result = commit_memory_import(preview, user_id=1, save_fn=saver, confirm=True)
    assert result.saved_count == 1
    assert result.skipped_flagged == 1
    saved_contents = [c[1] for c in saver.calls]
    assert "My password is hunter2 for the work account." not in saved_contents


def test_commit_skips_duplicates_by_default():
    text = "## Info\n- The user wants main to stay stable.\n"
    existing = ["The user wants main to stay stable."]
    preview = build_memory_import_preview(text, existing_contents=existing)
    saver = _RecorderSave()
    result = commit_memory_import(preview, user_id=1, save_fn=saver, confirm=True)
    assert result.saved_count == 0
    assert result.skipped_duplicate == 1
    assert saver.calls == []


def test_commit_allow_flagged_persists_flagged_entries():
    text = "## Creds\n- My password is hunter2 for the work account.\n"
    preview = build_memory_import_preview(text)
    saver = _RecorderSave()
    result = commit_memory_import(
        preview, user_id=1, save_fn=saver, confirm=True, allow_flagged=True,
    )
    assert result.saved_count == 1
    assert result.skipped_flagged == 0
    assert len(saver.calls) == 1


def test_commit_allow_duplicates_persists_duplicate_entries():
    text = "## Info\n- The user wants main to stay stable.\n"
    existing = ["The user wants main to stay stable."]
    preview = build_memory_import_preview(text, existing_contents=existing)
    saver = _RecorderSave()
    result = commit_memory_import(
        preview, user_id=1, save_fn=saver, confirm=True, allow_duplicates=True,
    )
    assert result.saved_count == 1
    assert result.skipped_duplicate == 0


def test_commit_returns_zero_counts_on_empty_preview():
    preview = build_memory_import_preview("")
    saver = _RecorderSave()
    result = commit_memory_import(preview, user_id=1, save_fn=saver, confirm=True)
    assert result.saved_count == 0
    assert result.skipped_flagged == 0
    assert result.skipped_duplicate == 0
    assert saver.calls == []


def test_commit_passes_category_and_content_to_save_fn():
    text = "## Workflow\n- The user wants warnings before risky Git actions.\n"
    preview = build_memory_import_preview(text)
    saver = _RecorderSave()
    commit_memory_import(preview, user_id=42, save_fn=saver, confirm=True)
    assert saver.calls == [
        ("Workflow", "The user wants warnings before risky Git actions.", 42),
    ]


def test_commit_does_not_save_on_confirm_false_even_with_allow_flags():
    text = "## Creds\n- My password is hunter2 for the work account.\n"
    preview = build_memory_import_preview(text)
    saver = _RecorderSave()
    result = commit_memory_import(
        preview,
        user_id=1,
        save_fn=saver,
        confirm=False,
        allow_flagged=True,
        allow_duplicates=True,
    )
    assert result.saved_count == 0
    assert saver.calls == []


def test_commit_skipped_flagged_counts_each_flagged_candidate():
    text = (
        "## A\n- My password is hunter2 for the work account.\n"
        "## B\n- The api key is stored in config_file_here.\n"
        "## C\n- The user prefers concise replies always.\n"
    )
    preview = build_memory_import_preview(text)
    saver = _RecorderSave()
    result = commit_memory_import(preview, user_id=1, save_fn=saver, confirm=True)
    assert result.saved_count == 1
    assert result.skipped_flagged == 2
