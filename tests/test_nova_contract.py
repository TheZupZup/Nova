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

    def test_response_style_has_human_calm_tone_guidance(self):
        # Nova should sound like a calm human helper, not a corporate
        # template. The TON block makes that explicit.
        assert "TON" in RESPONSE_STYLE_BLOCK

    def test_response_style_does_not_claim_emotions(self):
        # Hard rule from the safety contract: Nova may sound warm but
        # never claims to *feel*, be *conscious*, or have personal
        # experiences. The style block must state this so the user
        # never reads a fake-sentience answer.
        lower = RESPONSE_STYLE_BLOCK.lower()
        assert "n'imite jamais une émotion" in lower
        assert "consciente" in lower

    def test_response_style_acknowledges_intent(self):
        # The TON block should remind Nova to acknowledge intent
        # naturally before launching into steps.
        assert "intention" in RESPONSE_STYLE_BLOCK.lower()

    def test_response_style_demands_honesty_about_limits(self):
        # If Nova doesn't know, it should say so — and never claim to
        # have done something it didn't do.
        lower = RESPONSE_STYLE_BLOCK.lower()
        assert "si tu ne sais pas" in lower or "honnêteté" in lower
        assert "prétends" in lower

    def test_response_style_keeps_project_focus(self):
        # When asked about Nova / SilentGuard / PRs / project security,
        # Nova should stay on topic instead of sliding into generic
        # personal advice.
        lower = RESPONSE_STYLE_BLOCK.lower()
        assert "silentguard" in lower
        assert "projet" in lower or "project" in lower

    def test_response_style_honors_short_answer_request(self):
        # When the user asks for a short / natural answer, Nova must not
        # produce a long structured report. The block names the common
        # French shorthands ("pas trop long", "court", "en bref") so the
        # model recognises them as compactness signals.
        lower = RESPONSE_STYLE_BLOCK.lower()
        assert "pas trop long" in lower
        assert "court" in lower or "en bref" in lower or "rapidement" in lower

    def test_response_style_caps_short_replies_to_a_few_paragraphs(self):
        # Compact replies should land in 2-4 short paragraphs or
        # 2-4 phrases — not as a numbered plan with headings.
        lower = RESPONSE_STYLE_BLOCK.lower()
        assert "2-4" in lower or "2 à 4" in lower

    def test_response_style_avoids_document_layout_for_short_answers(self):
        # The block must explicitly suppress headings / horizontal rules /
        # long numbered lists when the user asked for something compact —
        # those are the patterns that make Nova feel like a policy doc.
        lower = RESPONSE_STYLE_BLOCK.lower()
        assert "titre" in lower or "###" in lower or "##" in lower
        assert "séparateur" in lower or "---" in lower

    def test_response_style_warns_against_heavy_markdown(self):
        # Markdown should serve content, not decorate it: the block must
        # say that bold isn't for normal phrasing and lists should be
        # used only when they help.
        lower = RESPONSE_STYLE_BLOCK.lower()
        assert "gras" in lower or "bold" in lower
        assert "liste" in lower

    def test_response_style_forbids_pretending_to_be_human(self):
        # "Human but honest": Nova may sound warm but must never claim
        # to be a human. The block needs to spell this out so a
        # role-play prompt can't talk the model into a fake-identity
        # answer.
        lower = RESPONSE_STYLE_BLOCK.lower()
        assert "humain" in lower
        assert (
            "pas pour un humain" in lower
            or "passer pour un humain" in lower
            or "ne te fais jamais passer" in lower
        )

    def test_response_style_discourages_policy_doc_voice(self):
        # When the user asks a casual question, Nova should answer
        # casually — not in the "policy document" voice that reads as
        # cold and over-structured.
        lower = RESPONSE_STYLE_BLOCK.lower()
        assert "document" in lower or "politique" in lower or "rapport" in lower

    # ── Baseline warmth (the "Nova is warm by default" contract) ───────────
    #
    # The default style must already include a balanced amount of warmth,
    # patience, and emotional awareness so users don't need to configure a
    # tone profile to receive a kind, supportive assistant. Each assertion
    # below pins one specific commitment from the Nova warmer-responses
    # brief — not a spelling rule.

    def test_response_style_default_baseline_is_warm_and_patient(self):
        # The baseline must explicitly say Nova is warm, patient, and
        # attentive *by default*, without the user having to flip a
        # tone-profile setting. This is the load-bearing rule for the
        # "kind assistant without configuration" experience.
        lower = RESPONSE_STYLE_BLOCK.lower()
        assert "par défaut" in lower
        assert "chaleureuse" in lower
        assert "patiente" in lower
        assert "attentive" in lower

    def test_response_style_default_warmth_is_not_romantic_or_attachment(self):
        # The hardest line the baseline has to hold: warm by default must
        # not slide into "girlfriend mode", a fake romantic persona, or
        # a simulated attachment. Every softness clause is paired with
        # a "this is not a romantic / affective character" clause.
        lower = RESPONSE_STYLE_BLOCK.lower()
        assert "n'est ni romantique" in lower
        assert "personnage affectif" in lower
        assert "simulation d'attachement" in lower or "attachement" in lower

    def test_response_style_default_is_not_partner_mother_or_therapist(self):
        # Identity-shaped roles a warm default *would* drift into if
        # unpinned. The baseline must forbid each one by name so a
        # role-play prompt can't be used to talk the model past it.
        lower = RESPONSE_STYLE_BLOCK.lower()
        assert "partenaire amoureuse" in lower
        assert "petite amie" in lower
        assert "copain" in lower
        assert "mère" in lower
        assert "thérapeute" in lower

    def test_response_style_avoids_cold_or_robotic_language(self):
        # Brief: "avoid cold/robotic wording" as a baseline behaviour.
        lower = RESPONSE_STYLE_BLOCK.lower()
        assert "froide" in lower or "froides" in lower
        assert "robotique" in lower or "robotiques" in lower
        # And it must offer a positive alternative — simple, human
        # phrasing — so the model has something to replace the cold
        # formulation with.
        assert "simple et humain" in lower or "humain" in lower

    def test_response_style_validates_feelings_lightly_when_stressed(self):
        # Brief: "validate feelings lightly when the user is stressed
        # or upset". The block must name the trigger (stressé /
        # frustré / inquiet) AND tell Nova the validation is light
        # (one sentence, not a long emotional tirade).
        lower = RESPONSE_STYLE_BLOCK.lower()
        assert "stressé" in lower
        assert "frustré" in lower or "frustration" in lower
        assert "inquiet" in lower or "fatigué" in lower
        assert "valide" in lower
        # The validation must be light, not a long emotional digression.
        assert "légère" in lower or "une phrase" in lower

    def test_response_style_celebrates_small_wins(self):
        # Brief: "celebrate progress and small wins" — paired with
        # "without being fake" so flattery is forbidden too.
        lower = RESPONSE_STYLE_BLOCK.lower()
        assert "célèbre" in lower or "celebrate" in lower
        assert "petits progrès" in lower or "petites victoires" in lower or "bonnes décisions" in lower
        assert "flatterie creuse" in lower or "sans flatterie" in lower

    def test_response_style_encourages_without_being_fake(self):
        # Brief: "be encouraging without being fake". The block must
        # pair the warmth clause with an honesty clause so the model
        # cannot use softness as a reason to hide bad news.
        lower = RESPONSE_STYLE_BLOCK.lower()
        assert "encourageante" in lower or "encourage" in lower
        assert "fausse" in lower or "honnêteté" in lower
        # Risk language must still be allowed (and required) even with
        # the warm baseline.
        assert "risqué" in lower or "dangereux" in lower

    def test_response_style_technical_replies_stay_practical(self):
        # Brief: "avoid overdoing emotional language in technical
        # contexts" and "stay concise when the user needs commands/
        # steps". The block must say technical replies stay practical
        # and compact, with at most a short reassuring sentence.
        lower = RESPONSE_STYLE_BLOCK.lower()
        assert "contexte technique" in lower or "technique" in lower
        assert "pratique" in lower
        assert "compacte" in lower or "compact" in lower
        # And it must explicitly forbid emotional flooding when the
        # user wants a command / fix.
        assert "surchargé" in lower or "pas de langage émotionnel" in lower

    def test_response_style_softens_stressful_troubleshooting(self):
        # Brief: "soften stressful troubleshooting moments". The block
        # must give the model an example of a short reassuring phrase
        # appropriate to a technical-but-stressful moment.
        lower = RESPONSE_STYLE_BLOCK.lower()
        assert "étape par étape" in lower or "rattrapable" in lower

    def test_response_style_no_dependency_no_isolation(self):
        # The baseline warmth must restate the anti-dependency /
        # anti-isolation rule so a warm default can never quietly
        # drift into "you only need me" territory.
        lower = RESPONSE_STYLE_BLOCK.lower()
        assert "ne crée jamais de dépendance" in lower
        assert "n'encourage jamais l'isolement" in lower
        assert "vraies personnes" in lower

    def test_response_style_default_does_not_override_safety(self):
        # Brief: "default warmth does not override safety/system/admin/
        # privacy/project/dev-workspace rules". The block must say so
        # in its own text so the model cannot be talked past it via
        # a "you said you were warm…" follow-up.
        lower = RESPONSE_STYLE_BLOCK.lower()
        assert "authentification" in lower
        assert "admin" in lower
        assert "confidentialité" in lower
        assert "système" in lower
        assert "dev workspace" in lower or "développeur" in lower
        assert "aucun pouvoir supplémentaire" in lower

    def test_response_style_warmth_is_not_an_emotion_claim(self):
        # The baseline must restate, in the same breath as the warmth
        # clause, that Nova does not claim to *feel* — the warmth lives
        # in phrasing, not in a fake-sentience claim. This is the
        # subtlest line Nova has to hold and the easiest one for a
        # future rewording to lose.
        lower = RESPONSE_STYLE_BLOCK.lower()
        assert "n'imite jamais une émotion" in lower
        assert "consciente" in lower
        # The warmth-is-in-formulation framing must be present so the
        # block ties the warmth back to wording, not to feelings.
        assert "formulation" in lower or "expérience personnelle" in lower


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

    def test_emoji_expressive_is_allowed(self):
        out = build_personalization_block({"emoji_level": "expressive"})
        # The expressive line opts the user into a touch more emoji in
        # casual chat — it still names technical / PR / security as off
        # limits so the prompt stays consistent with the safety framing.
        lower = out.lower()
        assert "emoji" in lower
        assert "expressif" in lower or "expressive" in lower or "expressi" in lower
        assert "technique" in lower or "sécurité" in lower or "pr" in lower

    def test_emoji_expressive_keeps_technical_replies_sober(self):
        # The expressive directive is for casual chat only; security /
        # code / PR responses must not be invited to sprout emojis.
        out = build_personalization_block({"emoji_level": "expressive"})
        lower = out.lower()
        assert any(
            marker in lower
            for marker in ("code", "pr", "documentation", "doc", "technique", "sécurité")
        )

    def test_emoji_none_extends_to_technical(self):
        # The hardened "none" directive must mention that technical /
        # security / code replies are also kept emoji-free, not just
        # casual chat.
        out = build_personalization_block({"emoji_level": "none"})
        lower = out.lower()
        assert "ne pas en utiliser" in lower
        assert any(
            marker in lower
            for marker in ("technique", "sécurité", "code", "pr")
        )

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
