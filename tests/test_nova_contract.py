import re

from core.nova_contract import (
    IDENTITY_BLOCK,
    CONTEXT_RULES_BLOCK,
    MEMORY_RULES_BLOCK,
    RESPONSE_STYLE_BLOCK,
    IDENTITY_CONTRACT,
    build_contract,
)


class TestBlocks:
    def test_all_blocks_non_empty(self):
        for block in [IDENTITY_BLOCK, CONTEXT_RULES_BLOCK, MEMORY_RULES_BLOCK, RESPONSE_STYLE_BLOCK]:
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


class TestBuildContract:
    def test_contains_all_blocks(self):
        contract = build_contract()
        for block in [IDENTITY_BLOCK, CONTEXT_RULES_BLOCK, MEMORY_RULES_BLOCK, RESPONSE_STYLE_BLOCK]:
            assert block in contract

    def test_is_deterministic(self):
        assert build_contract() == build_contract()

    def test_no_unfilled_placeholders(self):
        assert not re.search(r'\{[^}]+\}', build_contract())

    def test_module_constant_equals_build_contract(self):
        assert IDENTITY_CONTRACT == build_contract()
