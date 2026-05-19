"""
Tests for the Relationship Situation Coach foundation:

  - topic detection (bilingual, conservative, non-string safe)
  - sensitive-content gate used to suppress automatic memory
  - the deterministic prompt block (method, styles, safety, privacy)
  - memory.policy refuses to auto-persist relationship detail
  - core.chat wiring: block injected only on a coach query, always
    below IDENTITY_CONTRACT, and the auto-save guard

Heavy / optional deps that ``core.chat`` pulls in transitively are
stubbed by ``tests/conftest.py`` when the real wheel is absent, so the
chat-wiring tests collect cleanly on a minimal host.
"""

import sys
from unittest.mock import MagicMock

# Match the defensive stubbing used by test_identity.py so importing
# core.chat never fails for a missing optional dep.
for _mod in ("ddgs", "ollama", "sgmllib", "feedparser"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from core.relationship_coach import (  # noqa: E402
    RELATIONSHIP_COACH_BLOCK,
    build_relationship_coach_block,
    is_relationship_coach_query,
    is_sensitive_relationship_content,
)
from core.chat import build_messages, _autosave_allowed  # noqa: E402
from core.identity import IDENTITY_CONTRACT  # noqa: E402
from core.policies import ADMIN_POLICY, DEFAULT_RESTRICTED_POLICY  # noqa: E402
from memory.policy import is_memory_allowed  # noqa: E402
from memory.schema import Memory  # noqa: E402


def _mem(**kwargs) -> Memory:
    defaults = dict(kind="general", topic="test", content="test content",
                    confidence=0.9)
    defaults.update(kwargs)
    return Memory(**defaults)


class TestDetection:
    def test_detects_french_partner_situation(self):
        assert is_relationship_coach_query(
            "On s'est disputé hier soir, comment répondre à ma copine "
            "sans être accusateur ?"
        )

    def test_detects_english_partner_situation(self):
        assert is_relationship_coach_query(
            "my girlfriend got upset with me — how do i respond to her?"
        )

    def test_detects_relationship_advice_phrase(self):
        assert is_relationship_coach_query("I need some relationship advice")
        assert is_relationship_coach_query("j'ai besoin d'un conseil relationnel")

    def test_is_case_insensitive(self):
        assert is_relationship_coach_query("MY PARTNER said something hurtful")

    def test_ignores_generic_message(self):
        assert not is_relationship_coach_query("what's the weather tomorrow?")
        assert not is_relationship_coach_query("écris-moi une fonction python")

    def test_does_not_trip_on_unrelated_reply_question(self):
        # "reply" / "respond" alone must not flip Nova into coach mode —
        # otherwise every email/support question would pay the cost.
        assert not is_relationship_coach_query(
            "how should i reply to this email from a client?"
        )

    def test_does_not_trip_on_non_romantic_conflicts(self):
        # Anchor-less phrases were removed: coworker / client / family
        # conflicts must not be silently reframed as relationship
        # coaching (Codex review P2).
        assert not is_relationship_coach_query(
            "my client got upset with me about the invoice"
        )
        assert not is_relationship_coach_query(
            "my coworker got upset, how do i respond to her?"
        )
        assert not is_relationship_coach_query("should i text him back?")
        assert not is_relationship_coach_query(
            "mon collègue m'en veut après une dispute avec lui au bureau"
        )

    def test_non_string_is_false(self):
        assert is_relationship_coach_query(None) is False
        assert is_relationship_coach_query(123) is False
        assert is_relationship_coach_query(["my partner"]) is False


class TestSensitiveContentGate:
    def test_flags_english_relationship_detail(self):
        assert is_sensitive_relationship_content("my ex cheated on me")
        assert is_sensitive_relationship_content("we broke up last week")
        assert is_sensitive_relationship_content("my wife and I had a fight")

    def test_flags_french_relationship_detail(self):
        assert is_sensitive_relationship_content("mon copain m'a trompé")
        assert is_sensitive_relationship_content(
            "on a rompu, c'est une rupture difficile"
        )

    def test_ignores_non_relationship_text(self):
        assert not is_sensitive_relationship_content(
            "User prefers Fedora KDE and neovim"
        )
        assert not is_sensitive_relationship_content("the server has 32GB RAM")

    def test_non_string_is_false(self):
        assert is_sensitive_relationship_content(None) is False
        assert is_sensitive_relationship_content(42) is False


class TestCoachBlock:
    def test_block_is_non_empty(self):
        assert RELATIONSHIP_COACH_BLOCK.strip()

    def test_builder_returns_constant(self):
        assert build_relationship_coach_block() == RELATIONSHIP_COACH_BLOCK

    def test_builder_is_deterministic(self):
        assert build_relationship_coach_block() == build_relationship_coach_block()

    def test_no_unfilled_placeholders(self):
        import re
        assert not re.search(r"\{[^}]+\}", RELATIONSHIP_COACH_BLOCK)

    def test_declares_non_clinical_and_not_a_therapist(self):
        lower = RELATIONSHIP_COACH_BLOCK.lower()
        assert "non clinique" in lower
        assert "thérapeute" in lower

    def test_states_it_is_subordinate_to_the_contract(self):
        # The header must make clear the block sits below identity /
        # safety rules so a coaching request can't dilute the contract.
        lower = RELATIONSHIP_COACH_BLOCK.lower()
        assert "subordonné" in lower
        assert "sécurité" in lower

    def test_covers_the_six_method_steps(self):
        lower = RELATIONSHIP_COACH_BLOCK.lower()
        assert "résume" in lower                       # summarize
        assert "interprétation" in lower               # interpretations
        assert "ne lis pas dans les pensées" in lower  # no mind-reading
        assert "réponse calme" in lower                # calm response
        assert "accusatrice" in lower                  # avoid accusatory
        assert "besoin" in lower                       # avoid needy wording
        assert "limites saines" in lower               # healthy boundaries
        assert ("attendre" in lower
                and "maintenant" in lower)             # speak now or wait

    def test_offers_the_three_response_styles(self):
        lower = RELATIONSHIP_COACH_BLOCK.lower()
        assert "doux" in lower
        assert "neutre" in lower
        assert "direct mais respectueux" in lower

    def test_states_all_safety_rules(self):
        lower = RELATIONSHIP_COACH_BLOCK.lower()
        assert "aucune manipulation" in lower
        assert "aucune coercition" in lower
        assert "aucun gaslighting" in lower
        assert "aucun conseil de vengeance" in lower
        assert "aucun diagnostic du partenaire" in lower
        assert "consentement" in lower
        assert "communication calme" in lower

    def test_states_local_private_and_no_autosave(self):
        lower = RELATIONSHIP_COACH_BLOCK.lower()
        assert "locale et privée" in lower
        assert "n'enregistre jamais" in lower or "ne mémorise" in lower
        # Only the explicit manual command may persist a relationship fact.
        assert "retiens ça" in lower or "souviens-toi" in lower
        assert "explicitement" in lower


class TestMemoryPolicyHardening:
    def test_allows_normal_preference(self):
        m = _mem(kind="preference", topic="editor",
                 content="User prefers neovim.")
        assert is_memory_allowed(m) is True

    def test_rejects_partner_detail(self):
        m = _mem(content="User's girlfriend works as a nurse.")
        assert is_memory_allowed(m) is False

    def test_rejects_french_relationship_detail(self):
        m = _mem(content="L'utilisateur s'est disputé avec ma copine.")
        assert is_memory_allowed(m) is False

    def test_still_rejects_legacy_ex_drama(self):
        # Pre-existing transient rule must keep working.
        m = _mem(content="User's ex cheated on them.")
        assert is_memory_allowed(m) is False


class TestChatWiring:
    def test_block_injected_for_coach_query(self):
        msgs = build_messages(
            [], "on s'est disputé avec ma copine, comment lui répondre",
            [], None, None, None,
        )
        assert RELATIONSHIP_COACH_BLOCK in msgs[0]["content"]

    def test_block_absent_for_neutral_query(self):
        msgs = build_messages([], "quelle heure est-il ?", [], None, None, None)
        assert RELATIONSHIP_COACH_BLOCK not in msgs[0]["content"]

    def test_identity_contract_still_first(self):
        # The coach block must never displace the identity/safety
        # contract from the front of the system prompt.
        msgs = build_messages(
            [], "my husband and I had a fight, how do i respond to him",
            [], None, None, None,
        )
        assert msgs[0]["content"].startswith(IDENTITY_CONTRACT)
        assert msgs[0]["content"].index(IDENTITY_CONTRACT) < msgs[0][
            "content"
        ].index(RELATIONSHIP_COACH_BLOCK)

    def test_block_also_applies_in_search_context(self):
        # Injection is keyed off the user message, not the branch, so a
        # coach query still gets the framing even on the search path.
        msgs = build_messages(
            [], "relationship advice please", [], "résultats…", "search", None,
        )
        assert RELATIONSHIP_COACH_BLOCK in msgs[0]["content"]


class TestAutosaveGuard:
    def test_blocks_autosave_for_sensitive_relationship_turn(self):
        assert _autosave_allowed(ADMIN_POLICY, "my ex cheated on me") is False

    def test_allows_autosave_for_neutral_turn(self):
        assert _autosave_allowed(ADMIN_POLICY, "I use neovim and Fedora") is True

    def test_respects_policy_memory_disabled(self):
        # A restricted policy with memory saving off must stay off even
        # for a perfectly neutral message.
        assert DEFAULT_RESTRICTED_POLICY.memory_save_enabled is False
        assert _autosave_allowed(DEFAULT_RESTRICTED_POLICY, "I use neovim") is False

    def test_none_message_is_safe(self):
        assert _autosave_allowed(ADMIN_POLICY, None) is True

    def test_blocks_when_assistant_reply_is_sensitive(self):
        # Codex review P1: the LLM autosave path mines BOTH the user
        # message and the assistant reply, so a neutral follow-up whose
        # reply restates relationship context must still be blocked.
        assert _autosave_allowed(
            ADMIN_POLICY,
            "ok thanks, what should i focus on at work today?",
            "Earlier you mentioned your girlfriend was upset; setting "
            "that aside, for work you could...",
        ) is False

    def test_allows_when_both_user_and_reply_are_neutral(self):
        assert _autosave_allowed(
            ADMIN_POLICY,
            "I use neovim",
            "Great — neovim is a solid choice for that workflow.",
        ) is True

    def test_reply_arg_is_optional(self):
        # Backward-compatible default: callers that pass only the user
        # message still work and only gate on it.
        assert _autosave_allowed(ADMIN_POLICY, "I use Fedora") is True
