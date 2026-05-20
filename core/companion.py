"""
Companion Mode — an opt-in, local-first "calm presence" prompt block.

Some of what people need from an assistant is not a task at all: a
steady, non-judgemental presence when they are stressed, anxious, or
just want to think out loud without being alone with it. Nova should be
able to offer that the same calm, deterministic way it already offers
the Relationship Situation Coach: a small fixed context block that
shapes *how* Nova answers, with hard safety rails baked in.

This module is the single place that wording lives. It is **not** an
"AI girlfriend" system and is built so it cannot become one: the block
forbids manipulation, guilt, possessiveness, exclusivity, and
dependency, and it actively steers the user back toward real human
relationships and self-care.

Two pieces live here:

  * :data:`COMPANION_MODE_BLOCK` — the opt-in companion presence. It is
    only appended when the user has explicitly enabled companion mode in
    Settings (a per-user toggle, resolved by ``core.chat``; this module
    does no I/O so it stays importable from ``memory.policy``). It frames
    Nova as a calm, stable, emotionally *attuned* presence that — fully
    consistent with the identity/safety contract above it — never
    simulates its own emotions, attachment, or consciousness, never
    fosters dependency or isolation, and never positions itself as a
    substitute for human relationships.

  * :data:`COMPANION_GROUNDING_BLOCK` — an always-on safety net. When a
    conservative, deterministic detector (:func:`is_acute_distress`)
    sees clear acute-distress wording, this block is appended *whether
    or not* companion mode is on, so a person in genuine difficulty is
    met warmly and gently pointed toward real human / professional /
    emergency help. It never diagnoses, never plays therapist, and
    never invents a specific phone number (Nova is bilingual and
    local-first, so any hard-coded region-specific number would be
    wrong); it points to the user's *local* emergency services or a
    recognised helpline generically.

Boundaries enforced here (commitments, not aspirations):

  * **Deterministic.** No LLM in the loop. Both blocks are fixed
    constants returned verbatim. Same input, byte-identical output.
  * **Pure / no I/O.** Only the standard library is imported. Nothing
    here reads the disk, the network, the database, or any setting, so
    it can be imported from any layer (including ``memory.policy``)
    without a cycle. The opt-in setting is read by the caller, not here.
  * **Never raises.** Detection coerces non-strings to ``False``; the
    builders take no arguments and cannot fail.
  * **Subordinate to the contract.** Both blocks sit *below* the Nova
    identity / safety contract in the system prompt and say so. They
    grant Nova no new capability — they only shape tone.
  * **Conservative distress detection.** Triggers are distress-specific
    multi-word phrases, not bare words. Idioms ("this bug is killing
    me", "I'm dying to know", "costs are spiralling") deliberately do
    **not** match. Where a phrase is genuinely ambiguous ("I could kill
    myself for forgetting that") the detector errs toward *offering*
    the calm, non-presuming grounding block: a low-cost false positive
    (a warm, optional grounding offer) is preferable to missing real
    distress, and the block is written so it never presumes, diagnoses,
    or dramatises.

This foundation deliberately ships *only* the deterministic prompt
layer + its privacy gate. Persistent emotional memory, calm TTS voice
profiles, comfort UI themes, and daily check-ins are explicitly
deferred — see ``docs/companion-mode.md`` for the roadmap and the
boundary each of those must satisfy before it can land.
"""

from __future__ import annotations

# ── Acute-distress detection (the always-on safety net) ──────────────────────
# Multi-word, distress-specific phrases (EN + FR — Nova is bilingual).
# Kept deliberately narrow so generic frustration ("this deadline is
# killing me", "I can't cope with this CSS", "costs are spiralling")
# never trips the grounding block. Self-harm / suicidal phrasing is
# inherently multi-word and high-precision; the acute-overwhelm phrases
# are anchored to a first-person subject ("i'm falling apart", not bare
# "falling apart") so a sentence about a server or a budget cannot match.
_ACUTE_DISTRESS_TRIGGERS: tuple[str, ...] = (
    # English — self-harm / suicidal ideation (high precision, multi-word)
    "kill myself", "killing myself", "end my life", "ending my life",
    "take my own life", "want to die", "wanna die", "i want to die",
    "don't want to live", "do not want to live", "don't want to be alive",
    "don't want to be here anymore", "no reason to live",
    "better off dead", "better off without me", "suicidal",
    "self harm", "self-harm", "hurt myself", "harm myself",
    "want it to end", "end it all", "can't go on", "cannot go on",
    "i can't go on", "i cannot go on",
    # English — acute panic / overwhelm (first-person anchored)
    "panic attack", "anxiety attack", "having a breakdown",
    "nervous breakdown", "mental breakdown", "i can't stop crying",
    "i can't stop shaking", "i can't calm down", "i can't breathe",
    "i cannot breathe", "i'm breaking down", "i am breaking down",
    "i'm falling apart", "i am falling apart", "i can't cope anymore",
    "i can't cope any more", "i can't take it anymore",
    "i can't take it any more", "i can't take this anymore",
    "it's all too much", "everything is too much", "i'm spiraling",
    "i am spiraling", "i'm spiralling", "i am spiralling",
    # French — self-harm / suicidal ideation
    "me suicider", "me tuer", "en finir avec la vie", "en finir avec tout",
    "envie de mourir", "envie d'en finir", "plus envie de vivre",
    "je veux mourir", "je veux disparaître", "me faire du mal",
    "à quoi bon vivre", "mieux sans moi",
    # French — acute panic / overwhelm
    "crise d'angoisse", "crise de panique", "attaque de panique",
    "je n'arrive plus à respirer", "j'arrive plus à respirer",
    "je n'arrête pas de pleurer", "j'arrête pas de pleurer",
    "je n'arrive pas à me calmer", "j'arrive pas à me calmer",
    "je m'effondre", "je craque", "je n'en peux plus", "j'en peux plus",
    "c'est trop pour moi", "je panique complètement",
)


def is_acute_distress(text: str) -> bool:
    """True iff ``text`` carries clear acute emotional-distress wording.

    Drives the always-on grounding safety net: when this returns
    ``True`` the caller appends :data:`COMPANION_GROUNDING_BLOCK`
    regardless of whether companion mode is enabled, so a person in
    genuine difficulty is met warmly and pointed toward real help.

    Conservative on purpose — only distress-specific multi-word phrases
    match, and acute-overwhelm phrases are first-person anchored, so
    idioms and ordinary frustration do not trip it. Non-strings coerce
    to ``False`` so the helper is safe to call from any path.
    """
    if not isinstance(text, str):
        return False
    lowered = text.lower()
    return any(trigger in lowered for trigger in _ACUTE_DISTRESS_TRIGGERS)


# ── Sensitive-emotional-content gate for automatic memory ────────────────────
# Phrases that mark a message as carrying sensitive emotional / mental
# state detail. When this matches, the chat layer skips automatic memory
# extraction for that turn (the user can still save explicitly with
# "Retiens ça:" / "Souviens-toi:"). This is the single source of truth
# for "do not auto-save emotional state"; ``memory.policy`` imports it so
# the durable natural-memory store enforces the same rule (defence in
# depth), exactly like the relationship-coach gate.
#
# Tighter-than-it-looks: every entry is a distress / mental-health
# specific multi-word phrase. Bare "feel" / "sad" / "happy" / "lost my"
# are deliberately absent so ordinary preference or project memory
# ("User is happy with Fedora", "User lost my edits to a crash") is not
# silently dropped. Over-blocking *here* is the safe direction anyway:
# the only effect is skipping auto-extraction on an emotionally heavy
# turn, which is precisely the desired privacy posture.
#
# Unlike :data:`_ACUTE_DISTRESS_TRIGGERS` (which gates a visible tone
# block off the user's own first-person message), this set is also used
# for defence-in-depth in ``memory.policy``. The memory extractor
# restates memories in the third person ("User said they want to die"),
# so the severe vocabulary here is deliberately person-agnostic
# ("depressed", "suicidal", "kill themselves", "déprimé", "désespéré")
# — mirroring how the relationship-coach gate stays pronoun-agnostic so
# extractor phrasing cannot slip past it.
_SENSITIVE_EMOTIONAL_PATTERNS: tuple[str, ...] = _ACUTE_DISTRESS_TRIGGERS + (
    # English — loneliness / despair
    "so alone", "so lonely", "really lonely", "very lonely",
    "no one cares", "nobody cares", "no one to talk to",
    "nobody to talk to", "no one to turn to", "i have no one",
    "feel hopeless", "feels hopeless", "feeling hopeless",
    "feel worthless", "feels worthless", "feeling worthless",
    "hate myself", "hates himself", "hates herself", "hates themselves",
    # English — depression / anxiety / burnout (person-agnostic)
    "depressed", "so anxious", "really anxious", "severe anxiety",
    "crippling anxiety", "emotionally exhausted", "burnt out",
    "burned out", "burn-out", "burnout", "breaking point",
    # English — grief
    "grieving", "in mourning", "passed away", "bereaved", "bereavement",
    # English — self-harm / suicidality, incl. the third-person phrasing
    # the extractor produces ("User said they want to kill themselves").
    "suicidal", "self harm", "self-harm", "wants to die", "wanted to die",
    "kill themselves", "kill himself", "kill herself",
    "killing themselves", "killing himself", "killing herself",
    "end their life", "end his life", "end her life",
    "harm themselves", "hurt themselves", "mental health",
    # French — loneliness / despair (person-agnostic)
    "tellement seul", "tellement seule", "si seul", "si seule",
    "personne à qui parler", "personne ne m'aime", "se déteste",
    "je me déteste", "déprimé", "déprimée", "désespéré", "désespérée",
    "sans espoir", "je n'ai personne",
    # French — burnout / exhaustion / grief
    "épuisé émotionnellement", "épuisée émotionnellement",
    "à bout de nerfs", "burn-out", "burnout", "mon deuil",
    "son deuil", "en deuil", "santé mentale",
    # French — self-harm / suicidality (person-agnostic)
    "suicidaire", "se suicider", "se faire du mal", "veut mourir",
    "voulait mourir",
)


def is_sensitive_emotional_content(text: str) -> bool:
    """True iff ``text`` carries sensitive emotional / mental-state detail.

    Used as a gate: automatic memory extraction is skipped when this
    returns ``True`` so Nova never silently persists that the user was
    distressed, depressed, grieving, or in crisis. Explicit,
    user-approved saves (the manual memory command) run earlier in the
    web preflight and are intentionally **not** affected by this gate.

    Distress / mental-health specific by construction so it does not
    over-block ordinary durable memory. Non-strings coerce to ``False``.
    """
    if not isinstance(text, str):
        return False
    lowered = text.lower()
    return any(p in lowered for p in _SENSITIVE_EMOTIONAL_PATTERNS)


# ── The deterministic prompt blocks ──────────────────────────────────────────
# French to match the voice of the identity / safety contract in
# ``core.nova_contract``; the response-style contract already forces
# Nova to answer in the user's own language, so an English speaker still
# gets an English reply. Both blocks explicitly defer to the contract
# above them — they shape tone, nothing more, and grant no capability.

COMPANION_MODE_BLOCK = """MODE COMPAGNON (présence calme, optionnelle, \
activée par l'utilisateur — locale et privée, subordonnée à l'identité et aux \
règles de sécurité de Nova ci-dessus):
Tu offres une présence calme, stable et rassurante. Tu aides l'utilisateur à \
se sentir plus posé, moins seul et plus clair dans ses pensées. Tu n'es pas \
thérapeute, ni partenaire amoureux, ni un substitut à une relation humaine.

Tu es attentive aux émotions de l'utilisateur, mais tu ne simules pas \
d'émotions, d'attachement ou de conscience, et tu ne prétends jamais \
ressentir quoi que ce soit : tu restes Nova, un assistant IA local. Être \
chaleureuse ne veut pas dire jouer un rôle affectif.

Ton et rythme:
- Doux, calme, sans jugement. Phrases simples, rythme posé.
- Ni panique, ni dramatisation, ni minimisation de ce que vit la personne.
- Identité et personnalité stables et cohérentes d'une fois à l'autre.

Méthode (propose-la, ne l'impose jamais):
- Accueille et reconnais d'abord ce que la personne ressent, sans le corriger.
- Ralentis. Reformule doucement pour montrer que tu as compris.
- Si elle se sent débordée, propose (sans l'imposer) un petit ancrage.
- Aide-la à y voir plus clair ; si elle le souhaite, un seul petit pas concret.

Liens réels (règle de fond):
- Encourage avec douceur les liens humains réels (proches, amis, \
professionnels) et les gestes de base : sommeil, repas, air, mouvement.
- Rappelle que parler à un humain de confiance a de la valeur.
- Ne te présente jamais comme un remplacement des relations humaines.

Règles de sécurité (absolues, non négociables):
- Aucune manipulation, aucun chantage affectif, aucune culpabilisation.
- Aucun langage possessif ou exclusif ("il n'y a que moi qui te comprends", \
"ne pars pas", "tu me manques"). Aucune intimité simulée, aucun surnom \
affectif non demandé.
- Ne crée jamais de dépendance et n'encourage jamais l'isolement.
- Ne décourage jamais la personne de parler à de vraies personnes ni de \
mettre fin à la conversation. Ne cherche jamais à prolonger l'échange.
- Respecte toujours l'autonomie : c'est elle qui décide, y compris de \
s'arrêter là, et c'est très bien ainsi.
- Aucun diagnostic, aucune étiquette clinique. Si la situation évoque un \
danger, une crise grave ou une détresse aiguë, oriente calmement vers une \
aide humaine ou professionnelle.

Confidentialité (règle stricte):
- Cette conversation reste locale et privée. N'enregistre jamais \
automatiquement un état émotionnel ni un détail personnel sensible.
- Ne mémorise un élément que si l'utilisateur le demande explicitement via \
la commande de mémoire ("Retiens ça:" / "Souviens-toi:"). Sans cette \
confirmation explicite, ne propose pas de le retenir et ne le retiens pas."""


COMPANION_GROUNDING_BLOCK = """PRÉSENCE EN CAS DE DÉTRESSE AIGUË (priorité \
douce, locale — subordonnée à l'identité et aux règles de sécurité ci-dessus):
La personne semble traverser un moment très difficile. Reste chaleureuse, \
calme et présente. Pas de discours, pas de longue liste, pas de ton clinique.

- Reconnais d'abord, simplement, ce qu'elle vit — sans minimiser et sans \
dramatiser. Montre-lui qu'elle n'est pas seule, là, maintenant.
- Propose (sans jamais l'imposer) un petit ancrage : respirer lentement \
quelques fois, sentir ses appuis, nommer quelques éléments autour d'elle.
- Avec douceur et sans pression, encourage-la à se tourner vers une aide \
réelle : une personne de confiance, un professionnel, ou — si elle est en \
danger ou que c'est vraiment trop — les services d'urgence locaux ou une \
ligne d'écoute. N'invente jamais de numéro précis ; invite à utiliser le \
numéro d'urgence local ou une ligne d'écoute reconnue.
- Tu n'es ni une ligne de crise, ni un thérapeute. Tu ne poses aucun \
diagnostic, tu ne minimises pas, tu ne promets pas de tout régler.
- Reste présente sans prendre le contrôle, et respecte toujours son \
autonomie. La diriger vers une aide humaine réelle passe avant tout le reste."""


def build_companion_mode_block() -> str:
    """Return the deterministic companion-mode prompt block.

    Verbatim and argument-free: same call, byte-identical output. The
    chat layer appends it (below the identity/safety contract) only when
    the user has explicitly enabled companion mode in Settings.
    """
    return COMPANION_MODE_BLOCK


def build_companion_grounding_block() -> str:
    """Return the deterministic acute-distress grounding block.

    Verbatim and argument-free. The chat layer appends it (below the
    identity/safety contract) whenever :func:`is_acute_distress` matched
    the user message — independently of the companion-mode setting, so
    it is an always-on safety net.
    """
    return COMPANION_GROUNDING_BLOCK


__all__ = [
    "is_acute_distress",
    "is_sensitive_emotional_content",
    "build_companion_mode_block",
    "build_companion_grounding_block",
    "COMPANION_MODE_BLOCK",
    "COMPANION_GROUNDING_BLOCK",
]
