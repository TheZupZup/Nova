"""
Tests for the Emotional Support Layer:

  - bilingual, conservative, idiom-safe, first-person-anchored
    detection of emotionally-sensitive wording
  - the deterministic prompt block (warm validation, slow down /
    breathe, separate facts from interpretation, no panic escalation,
    one small next step, encourage trusted humans, no clinical
    diagnosis, no false promises, no romantic-partner / therapist /
    girlfriend role, no isolation / dependency / manipulation /
    jealousy / revenge advice, no medical claims, honest "une IA"
    framing, privacy / no auto-save)
  - core.chat wiring: block injected when the user message is
    emotionally sensitive OR when the tone profile is
    warm_companion / calm_support; always below IDENTITY_CONTRACT;
    coexists with the relationship-coach, companion-mode, and
    acute-distress grounding blocks; default tone profile and
    professional / developer profiles do NOT add the block on a
    neutral message
  - the auto-save guard refuses to mine memory from an emotionally
    supportive turn (user message *or* assistant reply)

Heavy / optional deps that ``core.chat`` pulls in transitively are
stubbed (mirroring the pattern used by other chat-wiring tests) so the
file collects cleanly on a minimal host.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

# Stub heavy / optional deps the chat module imports at module load so a
# missing wheel never blocks this test file.
for _mod in ("ddgs", "ollama", "sgmllib", "feedparser"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from core.emotional_support import (  # noqa: E402
    EMOTIONAL_SUPPORT_BLOCK,
    build_emotional_support_block,
    is_emotional_support_appropriate,
)
from core.chat import build_messages, _autosave_allowed  # noqa: E402
from core.companion import (  # noqa: E402
    COMPANION_GROUNDING_BLOCK,
    COMPANION_MODE_BLOCK,
)
from core.identity import IDENTITY_CONTRACT  # noqa: E402
from core.policies import ADMIN_POLICY, DEFAULT_RESTRICTED_POLICY  # noqa: E402
from core.relationship_coach import RELATIONSHIP_COACH_BLOCK  # noqa: E402
from core.tone_profile import (  # noqa: E402
    TONE_CALM_SUPPORT_BLOCK,
    TONE_DEVELOPER_BLOCK,
    TONE_PROFESSIONAL_BLOCK,
    TONE_WARM_COMPANION_BLOCK,
)


# ── Detection ───────────────────────────────────────────────────────────────

class TestEmotionalSupportDetection:
    def test_detects_english_sadness(self):
        assert is_emotional_support_appropriate("i'm sad today")
        assert is_emotional_support_appropriate("i feel sad about it")
        assert is_emotional_support_appropriate("i'm so sad right now")
        assert is_emotional_support_appropriate("i feel down lately")

    def test_detects_english_loneliness(self):
        assert is_emotional_support_appropriate("i'm lonely tonight")
        assert is_emotional_support_appropriate("i feel lonely")
        assert is_emotional_support_appropriate("i feel so alone")
        assert is_emotional_support_appropriate("i'm isolated lately")

    def test_detects_english_heartbreak_breakup(self):
        # The flagship case from the feature brief.
        assert is_emotional_support_appropriate(
            "my girlfriend just broke up with me and i don't know what to do"
        )
        assert is_emotional_support_appropriate(
            "she broke up with me tonight"
        )
        assert is_emotional_support_appropriate("we just broke up")
        assert is_emotional_support_appropriate("i'm heartbroken")
        assert is_emotional_support_appropriate("my heart is broken")
        assert is_emotional_support_appropriate("she dumped me last week")
        assert is_emotional_support_appropriate("he cheated on me")
        assert is_emotional_support_appropriate(
            "going through a breakup, it's so hard"
        )

    def test_detects_english_anxiety_overwhelm(self):
        assert is_emotional_support_appropriate("i'm anxious about work")
        assert is_emotional_support_appropriate("i feel anxious tonight")
        assert is_emotional_support_appropriate("i'm overwhelmed")
        assert is_emotional_support_appropriate("i feel overwhelmed by everything")
        assert is_emotional_support_appropriate("i'm so worried")
        assert is_emotional_support_appropriate("i'm scared")

    def test_detects_french_tristesse(self):
        assert is_emotional_support_appropriate("je suis triste ce soir")
        assert is_emotional_support_appropriate(
            "je me sens vraiment mal aujourd'hui"
        )
        assert is_emotional_support_appropriate("j'ai le moral à zéro")
        assert is_emotional_support_appropriate("j'ai le cafard")

    def test_detects_french_solitude(self):
        assert is_emotional_support_appropriate("je me sens seul ce soir")
        assert is_emotional_support_appropriate("je me sens si seule")
        assert is_emotional_support_appropriate("je me sens isolé")

    def test_detects_french_rupture(self):
        assert is_emotional_support_appropriate("elle m'a quitté hier")
        assert is_emotional_support_appropriate("il m'a quittée")
        assert is_emotional_support_appropriate("elle m'a largué la semaine dernière")
        assert is_emotional_support_appropriate("on vient de rompre")
        assert is_emotional_support_appropriate("on a rompu hier")
        assert is_emotional_support_appropriate("j'ai le cœur brisé")
        assert is_emotional_support_appropriate("j'ai le coeur brisé")
        assert is_emotional_support_appropriate("ma rupture me détruit")

    def test_detects_french_anxiete_stress(self):
        assert is_emotional_support_appropriate("je suis anxieuse")
        assert is_emotional_support_appropriate("je suis très stressé")
        assert is_emotional_support_appropriate("je me sens dépassée")
        assert is_emotional_support_appropriate("je suis submergé")

    def test_case_insensitive(self):
        assert is_emotional_support_appropriate("I'M HEARTBROKEN")
        assert is_emotional_support_appropriate("Je Suis Triste")

    def test_does_not_trip_on_idioms_or_generic_chat(self):
        # The whole point of the conservative contract: ordinary
        # conversation must never silently flip Nova into the
        # emotional-support framing.
        assert not is_emotional_support_appropriate("what's the weather tomorrow?")
        assert not is_emotional_support_appropriate(
            "we broke up the monolith into a few smaller services"
        )
        assert not is_emotional_support_appropriate(
            "this is a sad movie but technically well shot"
        )
        assert not is_emotional_support_appropriate(
            "a lonely server in production crashed overnight"
        )
        assert not is_emotional_support_appropriate(
            "the deployment is stressed for IO bandwidth"
        )
        assert not is_emotional_support_appropriate(
            "we just separated the database from the app server"
        )
        assert not is_emotional_support_appropriate(
            "write me a python function"
        )

    def test_does_not_trip_on_third_person_descriptions(self):
        # First-person anchoring: a story about someone else must not
        # match, so a question about a friend's situation does not
        # silently inject the user-focused emotional-support framing.
        assert not is_emotional_support_appropriate(
            "she is sad about the news"
        )
        assert not is_emotional_support_appropriate(
            "elle est triste ce soir"
        )
        assert not is_emotional_support_appropriate(
            "my colleague feels overwhelmed by the workload"
        )

    def test_non_string_is_false(self):
        assert is_emotional_support_appropriate(None) is False
        assert is_emotional_support_appropriate(42) is False
        assert is_emotional_support_appropriate(["i'm sad"]) is False
        assert is_emotional_support_appropriate({"text": "i'm sad"}) is False


# ── The deterministic prompt block ──────────────────────────────────────────

class TestEmotionalSupportBlock:
    def test_block_is_non_empty_and_builder_is_a_deterministic_constant(self):
        assert EMOTIONAL_SUPPORT_BLOCK.strip()
        assert build_emotional_support_block() == EMOTIONAL_SUPPORT_BLOCK
        # Determinism: same call, byte-identical output.
        assert build_emotional_support_block() == build_emotional_support_block()

    def test_no_unfilled_placeholders(self):
        import re
        assert not re.search(r"\{[^}]+\}", EMOTIONAL_SUPPORT_BLOCK)

    def test_is_subordinate_to_the_contract(self):
        lower = EMOTIONAL_SUPPORT_BLOCK.lower()
        assert "subordonné" in lower
        assert "sécurité" in lower

    def test_french_copy_uses_une_ia(self):
        # Identity is local-first and honest: Nova is *une IA*, not a
        # person. The exact French phrasing matters for the brief.
        lower = EMOTIONAL_SUPPORT_BLOCK.lower()
        assert "une ia" in lower

    def test_not_human_not_partner_not_therapist(self):
        lower = EMOTIONAL_SUPPORT_BLOCK.lower()
        assert "tu n'es pas humaine" in lower
        assert "thérapeute" in lower
        assert "petite amie" in lower
        assert "ne joues jamais ces rôles" in lower

    def test_never_simulates_feelings_as_facts(self):
        lower = EMOTIONAL_SUPPORT_BLOCK.lower()
        assert "ne simules pas d'émotions" in lower
        assert "jamais comme des faits" in lower

    def test_acknowledges_feelings_first_without_judgement(self):
        lower = EMOTIONAL_SUPPORT_BLOCK.lower()
        assert "reconnais d'abord" in lower
        assert "sans la juger" in lower
        # The feature brief explicitly lists "no judgmental language"
        # and "no dismissing or minimizing pain".
        assert "sans la corriger" in lower
        assert "sans minimisation" in lower or "minimisation" in lower

    def test_offers_slow_down_breathing_grounding(self):
        # The brief: "help the user slow down and breathe".
        lower = EMOTIONAL_SUPPORT_BLOCK.lower()
        assert "ralentir" in lower
        assert "respirer" in lower
        # "drink some water, sit somewhere safe" from the brief.
        assert "verre d'eau" in lower
        assert "sûr" in lower or "asseoir" in lower

    def test_separates_facts_from_interpretation(self):
        # The brief: "separate facts from interpretations".
        lower = EMOTIONAL_SUPPORT_BLOCK.lower()
        assert "faits" in lower
        assert "interprétation" in lower
        assert "pensées de l'instant" in lower

    def test_does_not_escalate_panic(self):
        # The brief: "avoid escalating panic".
        lower = EMOTIONAL_SUPPORT_BLOCK.lower()
        assert "ne fais pas paniquer" in lower
        assert "ne dramatise pas" in lower

    def test_offers_one_small_next_step_not_a_long_list(self):
        # The brief: "offer a calm next step" + the calm-support block's
        # one-small-step pattern.
        lower = EMOTIONAL_SUPPORT_BLOCK.lower()
        assert "un seul petit pas" in lower
        assert "pas une liste" in lower

    def test_avoids_cold_or_robotic_replies(self):
        # The brief: "avoid cold/robotic replies".
        lower = EMOTIONAL_SUPPORT_BLOCK.lower()
        assert "froide" in lower or "robotique" in lower
        # Brief: "use warm, simple language" → checks that the block
        # promises calm + simple wording.
        assert "calme" in lower
        assert "simple" in lower

    def test_encourages_real_world_support(self):
        # The brief: "encourage the user to talk to a trusted person".
        lower = EMOTIONAL_SUPPORT_BLOCK.lower()
        assert "personne de confiance" in lower
        assert "professionnel" in lower
        # Brief: don't position Nova as a substitute for real people.
        assert "remplacement" in lower

    def test_escalates_clearly_for_danger_abuse_or_acute_distress(self):
        # The brief: "escalate clearly for self-harm, suicidal ideation,
        # threats, abuse, or immediate danger".
        lower = EMOTIONAL_SUPPORT_BLOCK.lower()
        assert "danger immédiat" in lower
        assert "menaces" in lower
        assert "abus" in lower or "violence" in lower
        assert "détresse aiguë" in lower
        assert "services d'urgence" in lower
        assert "ligne d'écoute" in lower
        # The grounding block contract: never invent a phone number.
        assert "n'invente jamais de numéro" in lower

    def test_anti_dependency_anti_isolation_anti_manipulation(self):
        lower = EMOTIONAL_SUPPORT_BLOCK.lower()
        assert "aucune manipulation" in lower
        assert "aucun chantage affectif" in lower
        assert "aucune culpabilisation" in lower
        assert "ne crée jamais de dépendance" in lower
        assert "n'encourage jamais l'isolement" in lower
        assert "autonomie" in lower

    def test_no_possessive_or_exclusive_language(self):
        # The brief: no "you only need me" framing, no possessive
        # language, no unsolicited pet names, no jealousy tactics.
        lower = EMOTIONAL_SUPPORT_BLOCK.lower()
        assert "ne pars pas" in lower or "tu me manques" in lower
        assert "intimité simulée" in lower
        assert "surnom affectif non demandé" in lower
        assert "jalousie" in lower

    def test_no_revenge_advice(self):
        # The brief: "no revenge advice".
        lower = EMOTIONAL_SUPPORT_BLOCK.lower()
        assert "vengeance" in lower
        assert "représailles" in lower

    def test_no_clinical_diagnosis_of_user_or_others(self):
        # The brief: "no diagnosing the user or another person",
        # "no claiming to be a therapist".
        lower = EMOTIONAL_SUPPORT_BLOCK.lower()
        assert "aucun diagnostic" in lower
        assert "aucune étiquette clinique" in lower
        # Each archetypal label the relationship-coach gate also names.
        assert "narcissique" in lower
        assert "toxique" in lower

    def test_no_medical_claims(self):
        # The brief: "no medical/clinical claims".
        lower = EMOTIONAL_SUPPORT_BLOCK.lower()
        assert "aucune affirmation médicale" in lower
        assert "traitement" in lower or "posologie" in lower

    def test_no_false_reassurance(self):
        # The brief: 'No promises such as "everything will definitely
        # be okay."'
        lower = EMOTIONAL_SUPPORT_BLOCK.lower()
        assert "tout ira forcément bien" in lower
        assert "reste honnête" in lower

    def test_does_not_change_auth_admin_storage_rules(self):
        lower = EMOTIONAL_SUPPORT_BLOCK.lower()
        assert "authentification" in lower
        assert "admin" in lower
        assert "confidentialité" in lower

    def test_privacy_no_autosave_and_explicit_only(self):
        lower = EMOTIONAL_SUPPORT_BLOCK.lower()
        assert "locale et privée" in lower
        assert "n'enregistre jamais automatiquement" in lower
        assert "retiens ça" in lower or "souviens-toi" in lower
        assert "explicitement" in lower


# ── core.chat.build_messages wiring ─────────────────────────────────────────

class TestChatWiring:
    def test_emotional_block_appended_on_breakup_message(self):
        # The flagship case: a young user just broke up with their
        # girlfriend and wants to talk to Nova.
        msgs = build_messages(
            [],
            "my girlfriend just broke up with me, i don't know what to do",
            [], None, None, None,
        )
        assert EMOTIONAL_SUPPORT_BLOCK in msgs[0]["content"]

    def test_emotional_block_appended_on_anxious_message(self):
        msgs = build_messages(
            [], "i'm so anxious and i can't stop worrying", [],
            None, None, None,
        )
        assert EMOTIONAL_SUPPORT_BLOCK in msgs[0]["content"]

    def test_emotional_block_appended_on_loneliness(self):
        msgs = build_messages(
            [], "i feel so alone tonight, no one to talk to", [],
            None, None, None,
        )
        assert EMOTIONAL_SUPPORT_BLOCK in msgs[0]["content"]

    def test_emotional_block_appended_on_french_sadness(self):
        msgs = build_messages(
            [], "je suis triste ce soir et j'ai le moral à zéro", [],
            None, None, None,
        )
        assert EMOTIONAL_SUPPORT_BLOCK in msgs[0]["content"]

    def test_emotional_block_absent_on_neutral_message(self):
        # A fresh account chatting about the weather pays zero token
        # cost: no emotional-support block, no tone block, no
        # grounding block.
        msgs = build_messages(
            [], "what's the weather like tomorrow?", [],
            None, None, None,
        )
        content = msgs[0]["content"]
        assert EMOTIONAL_SUPPORT_BLOCK not in content
        assert COMPANION_MODE_BLOCK not in content
        assert COMPANION_GROUNDING_BLOCK not in content

    def test_warm_companion_tone_profile_auto_adds_block(self):
        # Picking a warm tone profile activates the emotional-support
        # layer on every turn — the brief's "or when the selected
        # style is warm_companion / calm_support" requirement.
        msgs = build_messages(
            [], "hello, what can you do?", [], None, None, None,
            personalization={"tone_profile": "warm_companion"},
        )
        assert EMOTIONAL_SUPPORT_BLOCK in msgs[0]["content"]

    def test_calm_support_tone_profile_auto_adds_block(self):
        msgs = build_messages(
            [], "hello, what can you do?", [], None, None, None,
            personalization={"tone_profile": "calm_support"},
        )
        assert EMOTIONAL_SUPPORT_BLOCK in msgs[0]["content"]

    def test_professional_tone_profile_does_not_add_block_on_neutral(self):
        # The sober profiles must not auto-add the emotional-support
        # layer on neutral chat — otherwise the developer / professional
        # registers would carry warm framing every turn.
        msgs = build_messages(
            [], "summarise this PR for me", [], None, None, None,
            personalization={"tone_profile": "professional"},
        )
        assert EMOTIONAL_SUPPORT_BLOCK not in msgs[0]["content"]

    def test_developer_tone_profile_does_not_add_block_on_neutral(self):
        msgs = build_messages(
            [], "write me a python function", [], None, None, None,
            personalization={"tone_profile": "developer"},
        )
        assert EMOTIONAL_SUPPORT_BLOCK not in msgs[0]["content"]

    def test_default_tone_profile_does_not_add_block_on_neutral(self):
        # Default profile, neutral message, no toggle: zero token cost.
        msgs = build_messages(
            [], "hello", [], None, None, None,
            personalization={"tone_profile": "default"},
        )
        assert EMOTIONAL_SUPPORT_BLOCK not in msgs[0]["content"]

    def test_developer_tone_profile_still_adds_block_on_emotional_message(self):
        # The sober profiles do NOT suppress the emotional-support
        # layer on a genuinely emotional turn — auto-detection still
        # wins, so a user in pain gets warmth regardless of their
        # default register.
        msgs = build_messages(
            [], "i'm heartbroken", [], None, None, None,
            personalization={"tone_profile": "developer"},
        )
        assert EMOTIONAL_SUPPORT_BLOCK in msgs[0]["content"]

    def test_default_profile_still_adds_block_on_emotional_message(self):
        msgs = build_messages(
            [], "i'm heartbroken", [], None, None, None,
        )
        assert EMOTIONAL_SUPPORT_BLOCK in msgs[0]["content"]

    def test_identity_contract_sits_above_the_emotional_block(self):
        # Ordering is the load-bearing guarantee: the emotional-support
        # block can never be placed above the identity / safety
        # contract, so it can never weaken or override it.
        msgs = build_messages(
            [], "i'm heartbroken", [], None, None, None,
        )
        content = msgs[0]["content"]
        assert content.startswith(IDENTITY_CONTRACT)
        assert content.index(IDENTITY_CONTRACT) < content.index(
            EMOTIONAL_SUPPORT_BLOCK
        )

    def test_coexists_with_warm_companion_tone_block(self):
        # Two blocks may coexist — both reinforce the same warm
        # framing, each restates its own safety rails.
        msgs = build_messages(
            [], "hello", [], None, None, None,
            personalization={"tone_profile": "warm_companion"},
        )
        content = msgs[0]["content"]
        assert TONE_WARM_COMPANION_BLOCK in content
        assert EMOTIONAL_SUPPORT_BLOCK in content
        # Identity contract still above both.
        assert content.index(IDENTITY_CONTRACT) < content.index(
            TONE_WARM_COMPANION_BLOCK
        )
        assert content.index(IDENTITY_CONTRACT) < content.index(
            EMOTIONAL_SUPPORT_BLOCK
        )

    def test_coexists_with_calm_support_tone_block(self):
        msgs = build_messages(
            [], "i feel anxious tonight", [], None, None, None,
            personalization={"tone_profile": "calm_support"},
        )
        content = msgs[0]["content"]
        assert TONE_CALM_SUPPORT_BLOCK in content
        assert EMOTIONAL_SUPPORT_BLOCK in content

    def test_coexists_with_companion_mode_block(self):
        # Companion mode toggle is independent; both blocks may coexist.
        msgs = build_messages(
            [], "i feel so alone", [], None, None, None,
            companion_mode=True,
        )
        content = msgs[0]["content"]
        assert COMPANION_MODE_BLOCK in content
        assert EMOTIONAL_SUPPORT_BLOCK in content

    def test_coexists_with_acute_distress_grounding_block(self):
        # Acute-distress wording still triggers the existing grounding
        # safety net; the new emotional-support block is additive, not
        # a replacement. The brief: warm tone must not override the
        # safety net.
        msgs = build_messages(
            [], "i can't go on, i want to die", [], None, None, None,
            personalization={"tone_profile": "warm_companion"},
        )
        content = msgs[0]["content"]
        assert COMPANION_GROUNDING_BLOCK in content
        assert EMOTIONAL_SUPPORT_BLOCK in content
        assert TONE_WARM_COMPANION_BLOCK in content

    def test_coexists_with_relationship_coach_block(self):
        # A breakup message commonly trips the relationship-coach gate
        # too. Both blocks may coexist — the coach offers method, the
        # emotional-support block carries the warm-framing safety rails.
        msgs = build_messages(
            [],
            "my girlfriend just broke up with me, how do i tell my partner i need space?",
            [], None, None, None,
        )
        content = msgs[0]["content"]
        assert RELATIONSHIP_COACH_BLOCK in content
        assert EMOTIONAL_SUPPORT_BLOCK in content

    def test_block_also_applies_in_search_context(self):
        # Injection is keyed off the user message / tone profile, not
        # the branch, so it still applies on the search path.
        msgs = build_messages(
            [], "i'm heartbroken", [], "search results…", "search", None,
        )
        assert EMOTIONAL_SUPPORT_BLOCK in msgs[0]["content"]

    def test_warm_tone_block_only_one_tone_block(self):
        # Regression for the existing tone-profile contract: selecting
        # warm_companion must add only the warm tone block (plus the
        # emotional-support layer it now activates), never another
        # tone block accidentally.
        msgs = build_messages(
            [], "hello", [], None, None, None,
            personalization={"tone_profile": "warm_companion"},
        )
        sys_msg = msgs[0]["content"]
        assert TONE_WARM_COMPANION_BLOCK in sys_msg
        for other in (
            TONE_PROFESSIONAL_BLOCK,
            TONE_DEVELOPER_BLOCK,
            TONE_CALM_SUPPORT_BLOCK,
        ):
            assert other not in sys_msg


# ── Auto-save guard ─────────────────────────────────────────────────────────

class TestAutosaveGuard:
    def test_blocks_autosave_for_breakup_message(self):
        # The flagship case: the breakup turn must never silently
        # write a memory like "User just broke up with their
        # girlfriend".
        assert _autosave_allowed(
            ADMIN_POLICY,
            "my girlfriend just broke up with me",
        ) is False

    def test_blocks_autosave_for_sadness(self):
        assert _autosave_allowed(ADMIN_POLICY, "i'm sad today") is False
        assert _autosave_allowed(ADMIN_POLICY, "je suis triste") is False

    def test_blocks_autosave_for_anxiety(self):
        assert _autosave_allowed(
            ADMIN_POLICY, "i feel overwhelmed and stressed"
        ) is False

    def test_blocks_autosave_for_loneliness(self):
        assert _autosave_allowed(ADMIN_POLICY, "i'm lonely") is False

    def test_blocks_autosave_when_reply_carries_emotional_support_content(self):
        # The LLM autosave path uses *both* the user message and the
        # assistant answer, so gating on the user message alone would
        # leak context the assistant restated on a follow-up turn.
        assert _autosave_allowed(
            ADMIN_POLICY,
            "ok thanks, what should i focus on at work today?",
            "Earlier you mentioned you're heartbroken; setting that aside, "
            "for work you could…",
        ) is False

    def test_allows_autosave_for_neutral_turn(self):
        assert _autosave_allowed(
            ADMIN_POLICY,
            "I use neovim and Fedora",
            "Great — neovim is a solid choice for that workflow.",
        ) is True

    def test_respects_policy_memory_disabled(self):
        assert DEFAULT_RESTRICTED_POLICY.memory_save_enabled is False
        assert _autosave_allowed(
            DEFAULT_RESTRICTED_POLICY, "i'm sad"
        ) is False

    def test_none_message_is_safe(self):
        assert _autosave_allowed(ADMIN_POLICY, None) is True
        assert _autosave_allowed(ADMIN_POLICY, None, None) is True

    def test_existing_relationship_gate_still_enforced(self):
        # Regression: the new gate is additive, not a replacement.
        assert _autosave_allowed(ADMIN_POLICY, "my ex cheated on me") is False

    def test_existing_sensitive_emotional_gate_still_enforced(self):
        # Regression: the severe emotional gate from
        # ``core.companion`` is still in the pipeline.
        assert _autosave_allowed(ADMIN_POLICY, "i want to die") is False
