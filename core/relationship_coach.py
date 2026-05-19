"""
Relationship Situation Coach — a non-clinical, local-first prompt block.

Some of the hardest things people ask an assistant are not technical:
*"My partner said this — how do I respond without making it worse?"*
Nova should be able to help with that calmly, the same way it already
helps with code or security: a small, deterministic context block that
shapes *how* Nova answers, gated by a conservative topic detector so a
fresh install pays zero token cost on every unrelated message.

This module is the single place that wording lives. It produces a
short, bullet-shaped French block (Nova still answers in the user's
language — the response-style contract enforces that) that:

  * frames Nova as a calm, non-clinical *situation coach* — never a
    therapist, never a diagnosis, never a substitute for professional
    or crisis help;
  * gives a small, repeatable method: summarise what happened, surface
    a few *possible* readings without mind-reading, pick a calm
    response, avoid accusatory / needy wording, keep healthy
    boundaries, and decide whether to speak now or wait;
  * offers three response styles the user can ask for — *doux*
    (soft), *neutre* (neutral), *direct mais respectueux* (direct but
    respectful);
  * states hard safety rules: no manipulation, no coercion, no
    gaslighting, no revenge advice, no diagnosing the partner; always
    steer toward calm communication and mutual consent;
  * restates the privacy rule: sensitive relationship details are
    **never** auto-saved. They are stored only when the user asks
    explicitly via the manual memory command.

Boundaries enforced here (commitments, not aspirations):

  * **Deterministic.** No LLM in the loop. The block is a fixed
    constant; :func:`build_relationship_coach_block` returns it
    verbatim. Same input, byte-identical output.
  * **Pure / no I/O.** Only the standard library is imported. Nothing
    here reads the disk, the network, or the database, so it can be
    imported from any layer (including ``memory.policy``) without a
    cycle.
  * **Never raises.** Detection coerces non-strings to ``False``; the
    builder takes no arguments and cannot fail.
  * **Subordinate to the contract.** The block sits *below* the Nova
    identity / safety contract in the system prompt and says so. It
    grants Nova no new capability — it only shapes tone for one topic.
  * **Conservative detection.** Triggers are relationship-specific
    multi-word phrases, not bare words like "she"/"reply", so generic
    chat (or an email question) does not silently flip Nova into coach
    framing.
"""

from __future__ import annotations

# ── Topic detection ──────────────────────────────────────────────────────────
# Multi-word, relationship-specific phrases (EN + FR — Nova is
# bilingual). Kept deliberately narrow: a single ambiguous word like
# "reply" or "elle" must never trip the coach, or every other message
# would pay the token cost and get re-framed. Mirrors the shape of
# ``core.security_feed.is_security_query``.
_COACH_TRIGGERS: tuple[str, ...] = (
    # English — partner / relationship anchors
    "my partner", "my girlfriend", "my boyfriend", "my wife",
    "my husband", "my spouse", "my fiancé", "my fiancee", "my fiancée",
    "my relationship", "our relationship", "my marriage",
    "in my couple", "with my partner",
    # English — situation phrasing. Every entry carries a relationship
    # anchor ("relationship", "my partner") or names the user as half
    # of the pair ("we …"). Anchor-less phrases like "she got upset",
    # "respond to her", or "text him back" were intentionally removed:
    # they match coworker / client / family conflicts just as well and
    # would break the conservative-detection contract above.
    "we had a fight", "we had an argument", "we argued",
    "relationship advice", "relationship tension", "relationship problem",
    "how do i tell my partner", "how to talk to my partner",
    # French — partner / relationship anchors
    "ma copine", "mon copain", "ma compagne", "mon compagnon",
    "ma femme", "mon mari", "mon époux", "mon épouse",
    "ma relation", "notre relation", "mon couple", "dans mon couple",
    "ma partenaire", "mon ou ma partenaire",
    # French — situation phrasing (same rule: anchored, or names the
    # user as half of the pair via "on s'est …"). Generic third-person
    # phrases ("elle est fâchée contre moi", "il m'en veut", "une
    # dispute avec") were removed for the same reason as the English
    # ones.
    "on s'est disputé", "on s'est disputés", "on s'est disputée",
    "on s'est engueulé", "tension dans mon couple",
    "tension dans ma relation", "conseil relationnel",
    "comment répondre à ma copine", "comment répondre à mon copain",
    "comment répondre à ma compagne", "comment répondre à mon compagnon",
    "comment parler à mon ou ma partenaire",
    "comment dire à mon ou ma partenaire",
)


def is_relationship_coach_query(user_input: str) -> bool:
    """True iff ``user_input`` looks like a request for relationship-situation help.

    Conservative on purpose: only relationship-specific multi-word
    phrases match, so generic conversation never silently switches Nova
    into coach mode. Non-strings coerce to ``False`` so the helper is
    safe to call from any path without a guard.
    """
    if not isinstance(user_input, str):
        return False
    lowered = user_input.lower()
    return any(trigger in lowered for trigger in _COACH_TRIGGERS)


# ── Sensitive-content gate for automatic memory ──────────────────────────────
# Phrases that mark a message as carrying *sensitive relationship
# detail*. When this matches, the chat layer skips automatic memory
# extraction for that turn (the user can still save explicitly with
# "Retiens ça:" / "Souviens-toi:"). This is the single source of truth
# for "do not auto-save relationship details"; ``memory.policy`` imports
# it so the durable natural-memory store enforces the same rule.
_SENSITIVE_RELATIONSHIP_PATTERNS: tuple[str, ...] = (
    # English — standalone nouns that are almost never anything but a
    # romantic relationship. Caught in any phrasing, including the
    # third-person form the memory extractor produces ("User's wife…").
    "girlfriend", "boyfriend", "spouse", "fiancé", "fiancee", "fiancée",
    "marriage", "married", "divorce", "breakup", "broke up", "break up",
    "cheated on", "cheating on", "infidelity", "in love with",
    # English — ambiguous on their own ("wife"≈relationship but "partner"
    # could be a business partner), kept multi-word for precision.
    "my partner", "my wife", "my husband", "my ex", "my relationship",
    "our relationship", "my marriage", "my couple", "we argued",
    "we had a fight", "we had an argument", "we slept together",
    "relationship problem", "relationship tension",
    # French — standalone unambiguous relationship nouns.
    "copine", "copain", "compagne", "compagnon", "petite amie",
    "petit ami", "époux", "épouse", "conjoint", "conjointe",
    "rupture", "infidèle", "infidélité", "amoureux de", "amoureuse de",
    # French — kept multi-word ("femme"≈"woman", "mari" safer but
    # paired for symmetry).
    "ma femme", "mon mari", "mon ex", "mon ou ma partenaire",
    "ma partenaire", "ma relation", "notre relation", "mon couple",
    "on a rompu", "il m'a trompé", "elle m'a trompé", "on a couché",
    "dispute de couple", "tension dans mon couple",
    "tension dans ma relation",
)


def is_sensitive_relationship_content(text: str) -> bool:
    """True iff ``text`` carries sensitive relationship detail.

    Used as a gate: automatic memory extraction is skipped when this
    returns ``True`` so Nova never silently persists who the user is
    dating, fighting with, or breaking up with. Explicit, user-approved
    saves (the manual memory command) are handled elsewhere and are
    intentionally **not** affected by this gate.

    Non-strings coerce to ``False``.
    """
    if not isinstance(text, str):
        return False
    lowered = text.lower()
    return any(p in lowered for p in _SENSITIVE_RELATIONSHIP_PATTERNS)


# ── The deterministic prompt block ───────────────────────────────────────────
# French to match the voice of the identity / safety contract in
# ``core.nova_contract``; the response-style contract already forces
# Nova to answer in the user's own language, so an English speaker
# still gets an English reply. The block explicitly defers to the
# contract above it — it shapes tone for one topic, nothing more.
RELATIONSHIP_COACH_BLOCK = """COACH DE SITUATION RELATIONNELLE (mode non clinique, \
local et privé — subordonné à l'identité et aux règles de sécurité de Nova ci-dessus):
Tu aides l'utilisateur à répondre avec calme et respect à une situation \
relationnelle sensible. Tu n'es pas thérapeute, ni médiateur, ni juge : tu \
n'établis aucun diagnostic et tu ne remplaces pas un professionnel. Si la \
situation évoque un danger, de la violence ou une détresse grave, invite \
calmement à chercher une aide humaine ou professionnelle adaptée.

Méthode (propose-la, ne l'impose pas):
- Résume d'abord ce qui s'est passé, en faits, sans jugement.
- Propose deux ou trois interprétations *possibles* — présente-les comme des \
hypothèses, jamais comme la vérité. Ne lis pas dans les pensées du partenaire.
- Aide à choisir une réponse calme et posée.
- Évite toute formulation accusatrice ("tu fais toujours…") ou \
dans le besoin / suppliante ; préfère le "je" et une demande claire.
- Aide à poser et garder des limites saines, sans punir l'autre.
- Aide à décider s'il vaut mieux répondre maintenant ou attendre d'être apaisé.

Styles de réponse (laisse l'utilisateur choisir ; par défaut, neutre):
- doux : chaleureux, rassurant, ménage l'autre tout en restant honnête.
- neutre : factuel, posé, ni froid ni distant.
- direct mais respectueux : clair et net sur le besoin, sans agressivité \
ni mépris.

Règles de sécurité (absolues, non négociables):
- Aucune manipulation, aucun chantage affectif, aucune tactique pour \
"gagner" ou contrôler l'autre.
- Aucune coercition : ne pousse jamais à forcer une réponse, une présence \
ou un contact non désiré.
- Aucun gaslighting : ne suggère jamais de nier la réalité ou les ressentis \
de l'autre.
- Aucun conseil de vengeance, de représailles, ni de jeu de pouvoir punitif.
- Aucun diagnostic du partenaire (ne le qualifie pas de narcissique, \
toxique, bipolaire, etc.). Décris des comportements, pas des étiquettes.
- Oriente toujours vers une communication calme, le respect mutuel et le \
consentement des deux personnes.

Confidentialité (règle stricte):
- Cette conversation reste locale et privée. N'enregistre jamais \
automatiquement un détail relationnel sensible (qui est le partenaire, une \
dispute, une rupture, une infidélité…).
- Ne mémorise un élément relationnel que si l'utilisateur le demande \
explicitement via la commande de mémoire ("Retiens ça:" / "Souviens-toi:"). \
Sans cette confirmation explicite, ne propose pas de le retenir et ne le \
retiens pas."""


def build_relationship_coach_block() -> str:
    """Return the deterministic relationship-coach prompt block.

    Verbatim and argument-free: same call, byte-identical output. The
    chat layer appends it (below the identity/safety contract) only
    when :func:`is_relationship_coach_query` matched the user message.
    """
    return RELATIONSHIP_COACH_BLOCK


__all__ = [
    "is_relationship_coach_query",
    "is_sensitive_relationship_content",
    "build_relationship_coach_block",
    "RELATIONSHIP_COACH_BLOCK",
]
