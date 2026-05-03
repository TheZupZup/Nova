import re

from core.nova_contract import (
    IDENTITY_BLOCK,
    CAPABILITIES_BLOCK,
    CONTEXT_RULES_BLOCK,
    MEMORY_RULES_BLOCK,
    RESPONSE_STYLE_BLOCK,
    IDENTITY_CONTRACT,
    build_contract,
)


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
