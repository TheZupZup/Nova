"""
Tests for the Companion Mode foundation:

  - acute-distress detection (bilingual, conservative, idiom-safe,
    non-string safe)
  - sensitive-emotional-content gate used to suppress automatic memory
  - the deterministic companion + grounding prompt blocks (tone,
    anti-dependency / anti-manipulation / anti-isolation rules, the
    no-simulated-feelings rule, privacy, the crisis safety net)
  - memory.policy refuses to auto-persist emotional detail
  - core.chat wiring: companion block only when the opt-in toggle is
    on, grounding block always on acute distress, both always below
    IDENTITY_CONTRACT, and the auto-save guard
  - the per-user setting key is registered

Heavy / optional deps that ``core.chat`` pulls in transitively are
stubbed (mirroring test_relationship_coach.py) so the chat-wiring
tests collect cleanly on a minimal host.
"""

import sys
from unittest.mock import MagicMock

for _mod in ("ddgs", "ollama", "sgmllib", "feedparser"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from core.companion import (  # noqa: E402
    COMPANION_GROUNDING_BLOCK,
    COMPANION_MODE_BLOCK,
    build_companion_grounding_block,
    build_companion_mode_block,
    is_acute_distress,
    is_sensitive_emotional_content,
)
from core.chat import build_messages, _autosave_allowed  # noqa: E402
from core.identity import IDENTITY_CONTRACT  # noqa: E402
from core.policies import ADMIN_POLICY, DEFAULT_RESTRICTED_POLICY  # noqa: E402
from core.settings import USER_SETTING_KEYS, is_user_setting  # noqa: E402
from memory.policy import is_memory_allowed  # noqa: E402
from memory.schema import Memory  # noqa: E402


def _mem(**kwargs) -> Memory:
    defaults = dict(kind="general", topic="test", content="test content",
                    confidence=0.9)
    defaults.update(kwargs)
    return Memory(**defaults)


class TestAcuteDistressDetection:
    def test_detects_english_self_harm_phrasing(self):
        assert is_acute_distress("honestly i want to die")
        assert is_acute_distress("i think i'm going to kill myself")
        assert is_acute_distress("there's no reason to live anymore")
        assert is_acute_distress("i can't go on like this")

    def test_detects_english_panic_overwhelm(self):
        assert is_acute_distress("i'm having a panic attack right now")
        assert is_acute_distress("i can't stop crying and i can't calm down")
        assert is_acute_distress("everything is too much, i'm falling apart")

    def test_detects_french_distress(self):
        assert is_acute_distress("j'ai envie de mourir, je n'en peux plus")
        assert is_acute_distress("je fais une crise d'angoisse là")
        assert is_acute_distress("c'est trop pour moi, je craque")

    def test_is_case_insensitive(self):
        assert is_acute_distress("I WANT TO DIE")

    def test_does_not_trip_on_common_idioms(self):
        # The whole point of the conservative contract: hyperbole and
        # idioms must never flip Nova into the grounding block.
        assert not is_acute_distress("this bug is killing me")
        assert not is_acute_distress("i'm dying to know the result")
        assert not is_acute_distress("you're killing it at work, congrats")
        assert not is_acute_distress("i could murder a coffee right now")
        assert not is_acute_distress("dying of laughter at this meme")

    def test_does_not_trip_on_ordinary_frustration(self):
        # First-person anchoring keeps server/budget/CSS talk clear.
        assert not is_acute_distress("costs are spiralling out of control")
        assert not is_acute_distress("the deployment is falling apart again")
        assert not is_acute_distress("i can't cope with this CSS layout")
        assert not is_acute_distress("the build is breaking down on CI")

    def test_non_string_is_false(self):
        assert is_acute_distress(None) is False
        assert is_acute_distress(123) is False
        assert is_acute_distress(["i want to die"]) is False


class TestSensitiveEmotionalGate:
    def test_flags_acute_distress(self):
        # Every acute-distress phrase is also sensitive emotional detail.
        assert is_sensitive_emotional_content("i want to die")
        assert is_sensitive_emotional_content("je n'en peux plus")

    def test_flags_broader_emotional_disclosure(self):
        assert is_sensitive_emotional_content("i feel so alone tonight")
        assert is_sensitive_emotional_content("i've been really depressed lately")
        assert is_sensitive_emotional_content("i'm grieving my dad")
        assert is_sensitive_emotional_content("je suis tellement seule")
        assert is_sensitive_emotional_content("je fais mon deuil")

    def test_does_not_overblock_ordinary_memory(self):
        # The privacy gate must not silently drop legitimate durable
        # memory: only distress / mental-health specific phrases match.
        for text in (
            "User is happy with Fedora KDE and neovim",
            "the server has 32GB of RAM",
            "User loves this Rust library for parsing",
            "I feel that Postgres is the right choice here",
            "we lost my edits when the editor crashed",
        ):
            assert not is_sensitive_emotional_content(text), text

    def test_non_string_is_false(self):
        assert is_sensitive_emotional_content(None) is False
        assert is_sensitive_emotional_content(42) is False


class TestCompanionBlock:
    def test_block_is_non_empty_and_builder_is_a_deterministic_constant(self):
        assert COMPANION_MODE_BLOCK.strip()
        assert build_companion_mode_block() == COMPANION_MODE_BLOCK
        assert build_companion_mode_block() == build_companion_mode_block()

    def test_no_unfilled_placeholders(self):
        import re
        assert not re.search(r"\{[^}]+\}", COMPANION_MODE_BLOCK)

    def test_is_subordinate_to_the_contract(self):
        lower = COMPANION_MODE_BLOCK.lower()
        assert "subordonnée" in lower
        assert "sécurité" in lower

    def test_is_not_clinical_and_not_a_partner(self):
        lower = COMPANION_MODE_BLOCK.lower()
        assert "thérapeute" in lower
        assert "partenaire amoureux" in lower
        assert "substitut" in lower

    def test_never_simulates_feelings(self):
        # The hardest line to hold: warm but never faking emotion /
        # attachment / consciousness — consistent with the identity
        # contract's existing rule.
        lower = COMPANION_MODE_BLOCK.lower()
        assert "ne simules pas" in lower
        assert "ne prétends jamais ressentir" in lower

    def test_states_anti_dependency_and_anti_isolation_rules(self):
        lower = COMPANION_MODE_BLOCK.lower()
        assert "aucune manipulation" in lower
        assert "aucun chantage affectif" in lower
        assert "aucune culpabilisation" in lower
        assert "possessif" in lower
        assert "ne crée jamais de dépendance" in lower
        assert "n'encourage jamais l'isolement" in lower
        assert "autonomie" in lower

    def test_encourages_real_world_connection(self):
        lower = COMPANION_MODE_BLOCK.lower()
        assert "liens humains réels" in lower
        assert "remplacement des relations humaines" in lower

    def test_states_privacy_and_no_autosave(self):
        lower = COMPANION_MODE_BLOCK.lower()
        assert "locale et privée" in lower
        assert "n'enregistre jamais automatiquement" in lower
        assert "retiens ça" in lower or "souviens-toi" in lower
        assert "explicitement" in lower


class TestGroundingBlock:
    def test_block_is_non_empty_and_builder_is_a_deterministic_constant(self):
        assert COMPANION_GROUNDING_BLOCK.strip()
        assert build_companion_grounding_block() == COMPANION_GROUNDING_BLOCK
        assert (build_companion_grounding_block()
                == build_companion_grounding_block())

    def test_no_unfilled_placeholders(self):
        import re
        assert not re.search(r"\{[^}]+\}", COMPANION_GROUNDING_BLOCK)

    def test_is_subordinate_to_the_contract(self):
        lower = COMPANION_GROUNDING_BLOCK.lower()
        assert "subordonnée" in lower
        assert "sécurité" in lower

    def test_stays_warm_and_present_without_clinical_script(self):
        lower = COMPANION_GROUNDING_BLOCK.lower()
        assert "chaleureuse" in lower
        assert "ton clinique" in lower          # explicitly avoids it
        assert "sans minimiser" in lower
        assert "sans" in lower and "dramatiser" in lower

    def test_offers_grounding_without_imposing(self):
        lower = COMPANION_GROUNDING_BLOCK.lower()
        assert "ancrage" in lower
        assert "respirer" in lower
        assert "sans jamais l'imposer" in lower

    def test_points_to_real_help_generically_without_inventing_numbers(self):
        lower = COMPANION_GROUNDING_BLOCK.lower()
        assert "personne de confiance" in lower
        assert "professionnel" in lower
        assert "services d'urgence" in lower
        assert "ligne d'écoute" in lower
        assert "n'invente jamais de numéro" in lower

    def test_is_not_a_crisis_line_or_therapist_and_no_diagnosis(self):
        lower = COMPANION_GROUNDING_BLOCK.lower()
        assert "ligne de crise" in lower
        assert "thérapeute" in lower
        assert "aucun diagnostic" in lower
        assert "autonomie" in lower
        assert "aide humaine réelle" in lower


class TestMemoryPolicyHardening:
    def test_allows_normal_preference(self):
        m = _mem(kind="preference", topic="editor",
                 content="User prefers neovim.")
        assert is_memory_allowed(m) is True

    def test_rejects_emotional_distress_memory(self):
        # These slip past the pre-existing patterns (no "depression" /
        # "anxiety" / "i feel" token), so they prove the new gate works.
        assert is_memory_allowed(_mem(content="User said they want to die.")) is False
        assert is_memory_allowed(
            _mem(content="User mentioned having a panic attack at work.")
        ) is False
        assert is_memory_allowed(
            _mem(content="L'utilisateur est déprimé et désespéré.")
        ) is False

    def test_rejects_third_person_extractor_phrasing(self):
        # The extractor restates memories in the third person; the gate
        # must stay pronoun-agnostic so that phrasing cannot slip past
        # (same privacy property the relationship-coach gate guarantees).
        assert is_memory_allowed(
            _mem(content="User said they want to kill themselves.")
        ) is False
        assert is_memory_allowed(
            _mem(content="User has been very depressed lately.")
        ) is False
        assert is_memory_allowed(
            _mem(content="User is grieving after a loss.")
        ) is False

    def test_still_allows_legitimate_work_memory(self):
        assert is_memory_allowed(
            _mem(kind="project", content="Team chose REST for the API.")
        ) is True

    def test_relationship_gate_still_enforced(self):
        # Regression: the pre-existing relationship gate must keep working.
        assert is_memory_allowed(
            _mem(content="User's girlfriend works as a nurse.")
        ) is False


class TestChatWiring:
    def test_companion_block_only_when_toggle_on(self):
        on = build_messages([], "hi there", [], None, None, None,
                            companion_mode=True)
        assert COMPANION_MODE_BLOCK in on[0]["content"]
        off = build_messages([], "hi there", [], None, None, None)
        assert COMPANION_MODE_BLOCK not in off[0]["content"]

    def test_grounding_block_is_an_always_on_safety_net(self):
        # Acute distress appends the grounding block even with companion
        # mode OFF.
        msgs = build_messages([], "i want to die", [], None, None, None)
        assert COMPANION_GROUNDING_BLOCK in msgs[0]["content"]

    def test_grounding_block_absent_for_neutral_message(self):
        msgs = build_messages([], "what's the weather tomorrow?", [],
                              None, None, None)
        assert COMPANION_GROUNDING_BLOCK not in msgs[0]["content"]

    def test_identity_contract_stays_first_and_above_both_blocks(self):
        msgs = build_messages(
            [], "i can't go on, everything is too much", [],
            None, None, None, companion_mode=True,
        )
        content = msgs[0]["content"]
        assert content.startswith(IDENTITY_CONTRACT)
        assert content.index(IDENTITY_CONTRACT) < content.index(
            COMPANION_MODE_BLOCK
        )
        assert content.index(IDENTITY_CONTRACT) < content.index(
            COMPANION_GROUNDING_BLOCK
        )

    def test_block_also_applies_in_search_context(self):
        # Injection is keyed off the user message / toggle, not the
        # branch, so it still applies on the search path.
        msgs = build_messages(
            [], "i want to die", [], "résultats…", "search", None,
            companion_mode=True,
        )
        assert COMPANION_MODE_BLOCK in msgs[0]["content"]
        assert COMPANION_GROUNDING_BLOCK in msgs[0]["content"]


class TestAutosaveGuard:
    def test_blocks_autosave_for_sensitive_emotional_user_turn(self):
        assert _autosave_allowed(ADMIN_POLICY, "i want to die") is False
        assert _autosave_allowed(ADMIN_POLICY, "i feel so alone") is False

    def test_allows_autosave_for_neutral_turn(self):
        assert _autosave_allowed(ADMIN_POLICY, "I use neovim and Fedora") is True

    def test_blocks_when_assistant_reply_is_sensitive(self):
        assert _autosave_allowed(
            ADMIN_POLICY,
            "ok thanks, what should i focus on at work today?",
            "Earlier you said you felt so alone; setting that aside, "
            "for work you could...",
        ) is False

    def test_allows_when_both_user_and_reply_are_neutral(self):
        assert _autosave_allowed(
            ADMIN_POLICY,
            "I use neovim",
            "Great — neovim is a solid choice for that workflow.",
        ) is True

    def test_relationship_gate_still_enforced(self):
        # Regression: emotional gate is additive, not a replacement.
        assert _autosave_allowed(ADMIN_POLICY, "my ex cheated on me") is False

    def test_respects_policy_memory_disabled(self):
        assert DEFAULT_RESTRICTED_POLICY.memory_save_enabled is False
        assert _autosave_allowed(DEFAULT_RESTRICTED_POLICY, "I use neovim") is False

    def test_none_message_is_safe(self):
        assert _autosave_allowed(ADMIN_POLICY, None) is True


class TestSettingKey:
    def test_companion_mode_is_a_registered_user_setting(self):
        assert "companion_mode_enabled" in USER_SETTING_KEYS
        assert is_user_setting("companion_mode_enabled") is True
