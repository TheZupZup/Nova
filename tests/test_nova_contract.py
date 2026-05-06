import re

from core.nova_contract import (
    IDENTITY_BLOCK,
    CAPABILITIES_BLOCK,
    CONTEXT_RULES_BLOCK,
    MEMORY_RULES_BLOCK,
    RESPONSE_STYLE_BLOCK,
    IDENTITY_CONTRACT,
    build_contract,
    build_personalization_block,
)
from core.settings import PERSONALIZATION_DEFAULTS


class TestBlocks:
    def test_all_blocks_non_empty(self):
        for block in [
            IDENTITY_BLOCK,
            CAPABILITIES_BLOCK,
            CONTEXT_RULES_BLOCK,
            MEMORY_RULES_BLOCK,
            RESPONSE_STYLE_BLOCK,
        ]:
            assert block.strip()

    def test_identity_block_names_nova(self):
        assert "Nova" in IDENTITY_BLOCK

    def test_identity_block_hides_model_names(self):
        assert "gemma4" in IDENTITY_BLOCK

    def test_identity_block_covers_identity_question(self):
        assert "Nova c'est qui" in IDENTITY_BLOCK

    def test_context_rules_prohibit_self_search(self):
        assert "Nova" in CONTEXT_RULES_BLOCK

    def test_memory_block_mentions_manual_commands(self):
        assert "Retiens ça" in MEMORY_RULES_BLOCK

    def test_response_style_forbids_filler_openers(self):
        assert "Bien sûr" in RESPONSE_STYLE_BLOCK


class TestCapabilitiesBlock:
    def test_has_capabilities_label(self):
        assert "CAPACITÉS" in CAPABILITIES_BLOCK

    def test_mentions_local_ollama_chat(self):
        assert "Ollama" in CAPABILITIES_BLOCK

    def test_mentions_persistent_memory(self):
        assert "Mémoire persistante" in CAPABILITIES_BLOCK

    def test_mentions_manual_memory_commands(self):
        assert "Retiens ça" in CAPABILITIES_BLOCK
        assert "Souviens-toi" in CAPABILITIES_BLOCK

    def test_mentions_local_web_ui(self):
        text = CAPABILITIES_BLOCK.lower()
        assert "interface web" in text
        assert "navigateur" in text

    def test_mentions_weather_tool(self):
        assert "Météo" in CAPABILITIES_BLOCK

    def test_mentions_manual_web_search(self):
        text = CAPABILITIES_BLOCK.lower()
        assert "recherche web" in text
        assert "manuelle" in text

    def test_mentions_coding_help(self):
        assert "code" in CAPABILITIES_BLOCK.lower()

    def test_marks_memory_import_experimental(self):
        text = CAPABILITIES_BLOCK.lower()
        assert "import de mémoire" in text
        assert "expérimental" in text

    def test_lists_things_nova_does_not_do(self):
        assert "Nova ne fait pas" in CAPABILITIES_BLOCK
        assert "cloud" in CAPABILITIES_BLOCK.lower()

    def test_does_not_expose_raw_model_names(self):
        text = CAPABILITIES_BLOCK.lower()
        for name in ("gemma4", "gemma3", "deepseek", "qwen"):
            assert name not in text

    def test_block_stays_short(self):
        # Issue #99 targets ~25 lines of contract text for this block.
        assert CAPABILITIES_BLOCK.count("\n") < 25


class TestBuildContract:
    def test_contains_all_blocks(self):
        contract = build_contract()
        for block in [
            IDENTITY_BLOCK,
            CAPABILITIES_BLOCK,
            CONTEXT_RULES_BLOCK,
            MEMORY_RULES_BLOCK,
            RESPONSE_STYLE_BLOCK,
        ]:
            assert block in contract

    def test_capabilities_block_appears_after_identity(self):
        contract = build_contract()
        assert contract.index(IDENTITY_BLOCK) < contract.index(CAPABILITIES_BLOCK)

    def test_is_deterministic(self):
        assert build_contract() == build_contract()

    def test_no_unfilled_placeholders(self):
        assert not re.search(r'\{[^}]+\}', build_contract())

    def test_module_constant_equals_build_contract(self):
        assert IDENTITY_CONTRACT == build_contract()


class TestBuildPersonalizationBlock:
    """The block that turns the user's saved preferences into prompt text."""

    def test_none_returns_empty(self):
        assert build_personalization_block(None) == ""

    def test_empty_dict_returns_empty(self):
        assert build_personalization_block({}) == ""

    def test_defaults_return_empty(self):
        # A user who never opened the panel must pay zero token cost: the
        # default payload contributes nothing to the system prompt.
        assert build_personalization_block(dict(PERSONALIZATION_DEFAULTS)) == ""

    def test_concise_response_style_emits_short_directive(self):
        out = build_personalization_block({"response_style": "concise"})
        assert out
        assert "court" in out.lower() or "essentiel" in out.lower()

    def test_technical_response_style_mentions_precision(self):
        out = build_personalization_block({"response_style": "technical"})
        assert "technique" in out.lower()

    def test_detailed_response_style_mentions_detail(self):
        out = build_personalization_block({"response_style": "detailed"})
        assert "détail" in out.lower()

    def test_high_warmth_emits_warm_directive(self):
        out = build_personalization_block({"warmth_level": "high"})
        assert "chaleureu" in out.lower() or "attentionné" in out.lower()

    def test_low_warmth_emits_neutral_directive(self):
        out = build_personalization_block({"warmth_level": "low"})
        assert "neutre" in out.lower() or "factuel" in out.lower()

    def test_high_enthusiasm_emits_dynamic_directive(self):
        out = build_personalization_block({"enthusiasm_level": "high"})
        assert "dynamique" in out.lower() or "engagée" in out.lower()

    def test_low_enthusiasm_emits_calm_directive(self):
        out = build_personalization_block({"enthusiasm_level": "low"})
        assert "posée" in out.lower() or "calme" in out.lower()

    def test_emoji_none_forbids_emojis(self):
        out = build_personalization_block({"emoji_level": "none"})
        assert "ne pas" in out.lower() and "emoji" in out.lower()

    def test_emoji_medium_allows_emojis(self):
        out = build_personalization_block({"emoji_level": "medium"})
        assert "emoji" in out.lower()
        # The "medium" line explicitly *allows* emojis; the "no emoji" wording
        # of the "none" preset must not appear.
        assert "ne pas en utiliser" not in out.lower()

    def test_custom_instructions_are_quoted_into_block(self):
        out = build_personalization_block(
            {"custom_instructions": "Toujours commencer par un résumé."}
        )
        assert "Toujours commencer par un résumé." in out

    def test_blank_custom_instructions_are_skipped(self):
        out = build_personalization_block({"custom_instructions": "   "})
        assert out == ""

    def test_block_carries_priority_header(self):
        # The header signals to the model that these are user preferences,
        # not new identity rules. That distinction matters: the contract
        # above must remain authoritative on identity questions.
        out = build_personalization_block({"emoji_level": "none"})
        assert "PRÉFÉRENCES UTILISATEUR" in out

    def test_block_does_not_override_identity(self):
        # The wording must explicitly preserve the contract's authority.
        out = build_personalization_block({"warmth_level": "high"})
        assert "identité" in out.lower() or "règles" in out.lower()

    def test_unknown_enum_values_are_ignored(self):
        # Defence in depth: even if a row somehow holds a value the
        # validator would have rejected, the block must not crash and
        # must not leak the value into the prompt.
        out = build_personalization_block({"response_style": "verbose"})
        assert out == ""

    def test_full_payload_emits_one_line_per_setting(self):
        prefs = {
            "response_style": "technical",
            "warmth_level": "high",
            "enthusiasm_level": "low",
            "emoji_level": "none",
            "custom_instructions": "Pas de salutation.",
        }
        out = build_personalization_block(prefs)
        # 5 bulleted lines + 1 header line.
        assert out.count("\n") >= 5
        assert out.count("- ") >= 5
