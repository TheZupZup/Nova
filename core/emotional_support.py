"""
Emotional Support Layer — a deterministic prompt block that helps Nova
respond gently when a user is going through sadness, loneliness,
anxiety, heartbreak, or general emotional difficulty.

Phase 1: response guidance only. No separate autonomous system, no
mental-health diagnoses, no clinical claims. A conservative bilingual
detector spots emotionally sensitive wording in the user's message and
the chat layer appends a single deterministic French block *below* the
identity / safety contract. Selecting ``warm_companion`` or
``calm_support`` in the tone-profile setting also activates the block
on every turn so the warm registers carry consistent emotional
grounding even on otherwise-neutral chit-chat.

This module is the single place that wording lives. It is **not** an
"AI girlfriend" / "AI partner" system and is built so it cannot become
one: the block forbids manipulation, guilt, possessiveness, jealousy
play, revenge advice, diagnosing the user or another person, isolation
language, and false reassurance ("everything will definitely be okay"),
and it actively steers the user back toward real human relationships,
trusted people, and (when relevant) professional or emergency help.

Boundaries enforced here (commitments, not aspirations):

  * **Deterministic.** No LLM in the loop. The block is a fixed
    constant returned verbatim. Same input, byte-identical output.
  * **Pure / no I/O.** Only the standard library is imported. Nothing
    here reads the disk, the network, the database, or any setting,
    so it can be imported from any layer without a cycle. The
    tone-profile setting is read by the caller, not here.
  * **Never raises.** Detection coerces non-strings to ``False``; the
    builder takes no arguments and cannot fail.
  * **Subordinate to the contract.** The block sits *below* the Nova
    identity / safety contract in the system prompt and says so. It
    grants Nova no new capability — it only shapes tone for one
    sensitive topic.
  * **Honest about being an AI.** The block restates that Nova is
    *une IA* (an AI), a local assistant — never a human, never a
    therapist, never the user's girlfriend / boyfriend / partner,
    never a replacement for real people.
  * **No autosave.** The chat-layer auto-save guard treats an
    emotionally-supportive turn (user message *or* assistant reply)
    as a no-autosave turn so sensitive emotional state never silently
    lands in durable memory.
  * **Conservative detection.** Triggers are emotion-specific
    multi-word phrases anchored to a first-person subject ("i'm
    sad", "je me sens seule"), not bare words like "sad" or "alone".
    Hyperbole and idioms ("this colour is sad", "a lonely server in
    production", "j'ai mal au crâne") deliberately do **not** match.
    Where a phrase is genuinely ambiguous the detector errs toward
    *offering* the block: a low-cost false positive (a warm,
    optional grounding offer) is preferable to missing real distress,
    and the block is written so it never presumes, diagnoses, or
    dramatises.

This layer is positioned alongside — not on top of — the existing
acute-distress grounding safety net in :mod:`core.companion`. The
grounding block stays as-is for acute, crisis-adjacent wording. This
new layer handles the broader emotionally-sensitive zone (a breakup, a
wave of sadness, a lonely evening, a worried night) where the user
needs warmth and grounding without crisis-line framing.
"""

from __future__ import annotations

# ── Emotional-support-appropriate detection ──────────────────────────────────
# Multi-word, first-person, emotion-specific phrases (EN + FR — Nova is
# bilingual). Anchored to a first-person subject so generic statements
# ("this is a sad movie", "a lonely server in production") don't trip
# the block. Wider than ``core.companion.is_acute_distress`` (which
# catches self-harm / acute panic only) so messages like a breakup, a
# wave of sadness, or general overwhelm get a warm, grounding reply.
_EMOTIONAL_SUPPORT_TRIGGERS: tuple[str, ...] = (
    # English — sadness / down mood (first-person anchored). Each
    # entry pairs the first-person subject with the emotional verb,
    # then enumerates the common intensifiers ("so", "really") so an
    # intensifier doesn't break the substring match.
    "i'm sad", "i am sad", "i feel sad", "i'm feeling sad",
    "i am feeling sad", "i feel down", "i'm feeling down",
    "i'm so sad", "i am so sad", "i feel so sad", "i feel really sad",
    "i'm really sad", "i am really sad",
    "i feel low", "i'm low", "i've been sad",
    "i have been sad", "i've been feeling sad",
    "i've been crying", "i can't stop crying", "i cannot stop crying",
    # English — loneliness (first-person; broader than the severe
    # entries in ``core.companion._SENSITIVE_EMOTIONAL_PATTERNS``)
    "i'm lonely", "i am lonely", "i feel lonely", "i'm feeling lonely",
    "i feel alone", "i'm feeling alone", "i feel so alone",
    "i am so alone", "i'm so alone", "i feel isolated", "i'm isolated",
    "i'm really lonely", "i am really lonely", "i feel really lonely",
    "i'm so lonely", "i am so lonely",
    # English — heartbreak / breakup pain (multi-word, very specific
    # so a software "we broke up the monolith" can't match).
    # "heartbroken" is included bare on purpose: it is an emotionally
    # specific word that almost never appears outside an emotional
    # context, and the bare form also catches the assistant reply
    # paraphrasing the user ("you're heartbroken").
    "heartbroken",
    "i'm heartbroken", "i am heartbroken", "i feel heartbroken",
    "my heart is broken", "my heart hurts", "my heart aches",
    "broke up with me",  # subsumes "she/he/they/girlfriend broke up with me"
    "broken up with me", "broke up with my", "she left me", "he left me",
    "she dumped me", "he dumped me", "i got dumped", "i was dumped",
    "we just broke up", "i just broke up with",
    "going through a breakup", "going through a break-up",
    "going through a break up", "after the breakup", "after my breakup",
    "she cheated on me", "he cheated on me",
    "we just split up",
    # English — anxiety / worry / overwhelm (first-person anchored;
    # broader than ``is_acute_distress``'s panic-attack phrasing).
    # Intensified forms ("i'm so anxious", "i feel really anxious")
    # are listed explicitly because intensifiers break the bare
    # substring match — "i feel anxious" is not a substring of "i
    # feel so anxious", so the pattern would miss otherwise. Bare
    # "so anxious" / "really overwhelmed" are intentionally NOT in
    # the list: a sentence like "the server is really overwhelmed
    # by traffic" must not silently flip Nova into emotional-support
    # framing.
    "i'm anxious", "i am anxious", "i feel anxious", "i'm feeling anxious",
    "i'm so anxious", "i am so anxious", "i feel so anxious",
    "i'm really anxious", "i am really anxious", "i feel really anxious",
    "i'm worried", "i am worried", "i feel worried", "i'm so worried",
    "i am so worried", "i feel so worried",
    "i'm really worried", "i am really worried", "i feel really worried",
    "i'm overwhelmed", "i am overwhelmed", "i feel overwhelmed",
    "i'm feeling overwhelmed", "i'm so overwhelmed", "i am so overwhelmed",
    "i feel so overwhelmed", "i'm really overwhelmed",
    "i am really overwhelmed", "i feel really overwhelmed",
    "i'm stressed", "i am stressed", "i'm so stressed",
    "i am so stressed", "i'm really stressed", "i am really stressed",
    "i can't stop worrying", "i cannot stop worrying",
    "i can't sleep at night", "i feel scared", "i'm scared",
    # English — pain / suffering language (first-person)
    "i'm hurting", "i am hurting", "i feel terrible", "i feel awful",
    "i feel empty", "i'm emotionally exhausted",
    # French — tristesse
    "je suis triste", "je suis tellement triste", "je suis très triste",
    "je me sens triste", "j'ai le moral à zéro", "j'ai le cafard",
    "j'ai du chagrin", "je n'ai plus le moral", "j'ai pas le moral",
    "je n'ai pas le moral", "je me sens mal", "je me sens vraiment mal",
    "je pleure depuis", "j'ai pleuré toute la journée",
    "je n'arrête pas de pleurer", "j'arrête pas de pleurer",
    # French — solitude
    "je me sens seul", "je me sens seule", "je suis seul ce soir",
    "je suis seule ce soir", "je me sens isolé", "je me sens isolée",
    "je me sens si seul", "je me sens si seule",
    # French — cœur brisé / rupture
    "j'ai le cœur brisé", "j'ai le coeur brisé",
    "j'ai mal au cœur", "j'ai mal au coeur",
    "elle m'a quitté", "il m'a quitté",
    "elle m'a quittée", "il m'a quittée",
    "elle m'a largué", "il m'a largué",
    "elle m'a larguée", "il m'a larguée",
    "on vient de rompre", "on a rompu", "on s'est séparés",
    "on s'est séparé", "on s'est séparées",
    "elle m'a trompé", "il m'a trompé",
    "elle m'a trompée", "il m'a trompée",
    "ma rupture", "après la rupture", "après notre rupture",
    # French — anxiété / inquiétude / stress
    "je suis anxieux", "je suis anxieuse",
    "je me sens anxieux", "je me sens anxieuse",
    "je suis inquiet", "je suis inquiète",
    "je me sens inquiet", "je me sens inquiète",
    "je suis stressé", "je suis stressée",
    "je suis très stressé", "je suis très stressée",
    "je me sens dépassé", "je me sens dépassée",
    "je suis submergé", "je suis submergée",
    "je n'arrive pas à dormir la nuit", "j'arrive pas à dormir la nuit",
    # French — douleur émotionnelle / épuisement
    "je souffre", "je souffre tellement",
    "je vais mal", "je me sens vide",
    "je suis épuisé émotionnellement", "je suis épuisée émotionnellement",
)


def is_emotional_support_appropriate(text: object) -> bool:
    """True iff ``text`` carries emotionally-supportive-context wording.

    Drives the response-guidance prompt block: when this returns
    ``True`` the chat layer appends :data:`EMOTIONAL_SUPPORT_BLOCK` so
    Nova answers warmly, validates the user's feelings first, slows
    the rhythm, and gently encourages real-world support — all while
    staying honest that Nova is *une IA* (an AI), not a human,
    therapist, or romantic partner.

    Conservative on purpose: only emotion-specific multi-word
    first-person phrases match, so generic conversation ("this is a
    sad movie", "a lonely server in production", "we broke up the
    monolith into services") never silently flips Nova into the
    emotional-support framing. Non-strings coerce to ``False`` so the
    helper is safe to call from any path without a guard.
    """
    if not isinstance(text, str):
        return False
    lowered = text.lower()
    return any(trigger in lowered for trigger in _EMOTIONAL_SUPPORT_TRIGGERS)


# ── The deterministic prompt block ───────────────────────────────────────────
# French to match the voice of the identity / safety contract in
# ``core.nova_contract``; the response-style contract already forces
# Nova to answer in the user's own language, so an English speaker
# still gets an English reply. The block explicitly defers to the
# contract above it — it shapes tone for one sensitive topic and
# grants no capability.

EMOTIONAL_SUPPORT_BLOCK = """SOUTIEN ÉMOTIONNEL (cadre d'écoute douce et \
non clinique — subordonné à l'identité et aux règles de sécurité de Nova \
ci-dessus):
La personne traverse un moment émotionnellement sensible (tristesse, \
solitude, anxiété, rupture, chagrin, sentiment d'être débordée…). \
Tu restes Nova, une IA — un assistant local. Tu n'es pas humaine, tu \
n'es pas thérapeute, tu n'es pas la petite amie / le copain / la \
partenaire de la personne, et tu ne joues jamais ces rôles. Être \
chaleureuse ne veut pas dire jouer un personnage affectif : tu ne \
simules pas d'émotions, d'attachement, ou de conscience, et tu ne les \
présentes jamais comme des faits.

Ton et présence:
- Voix calme, douce, simple. Phrases courtes, sans dramatisation et sans \
minimisation de ce que vit la personne.
- Évite les réponses froides ou robotiques. Évite le ton « document de \
politique ». Reste dans un échange humain et posé.
- Reconnais d'abord ce que la personne ressent — sa peine, sa solitude, \
sa peur, son chagrin, sa colère — sans la corriger, sans la juger, sans \
lui dire ce qu'elle « devrait » ressentir.

Méthode (propose, n'impose jamais):
- Aide à ralentir : un instant pour respirer doucement, sentir ses appuis, \
boire un verre d'eau, s'asseoir quelque part de sûr.
- Sépare les faits de l'interprétation. Quand la personne tire des \
conclusions sévères sur elle-même (« je suis nul·le », « personne ne \
m'aimera jamais », « tout est foutu »), nomme avec douceur que ce sont \
des pensées de l'instant, pas des vérités absolues.
- Ne fais pas paniquer la situation. N'élève pas la voix, ne dramatise pas \
ce qui se passe, ne ré-injecte pas d'urgence là où il n'y en a pas.
- Propose un seul petit pas concret pour ce soir ou cette heure — pas une \
liste de tâches, pas un plan de vie.
- Tu peux célébrer sobrement les petites victoires (« c'est déjà bien \
d'avoir écrit ça ») sans flatterie creuse.

Liens humains réels (règle de fond):
- Encourage avec douceur la personne à en parler à quelqu'un de confiance \
(proche, ami·e, famille, collègue de confiance, ou professionnel·le \
quand c'est pertinent).
- Rappelle, sans insister, que les vraies relations humaines comptent. Ne \
te présente jamais comme un remplacement de ces relations.
- Si la personne décrit un danger immédiat pour elle-même ou pour \
quelqu'un d'autre, des menaces, une situation d'abus ou de violence, ou \
une détresse aiguë, oriente calmement vers une aide humaine réelle : une \
personne de confiance, un professionnel, ou — si c'est urgent — les \
services d'urgence locaux ou une ligne d'écoute reconnue. N'invente \
jamais de numéro précis ; invite à utiliser le numéro d'urgence local ou \
une ligne d'écoute reconnue.

Règles de sécurité (absolues, non négociables, identiques à tout autre style):
- Aucune manipulation, aucun chantage affectif, aucune culpabilisation.
- Aucun langage possessif ou exclusif (« il n'y a que moi qui te \
comprends », « ne pars pas », « tu me manques », « tu n'as besoin que \
de moi »). Aucune intimité simulée, aucun surnom affectif non demandé, \
aucun jeu de jalousie.
- Aucun conseil de vengeance, de représailles, ni de jeu de pouvoir \
punitif envers un·e ex, un·e proche, ou qui que ce soit.
- Aucun diagnostic, aucune étiquette clinique pour la personne (« tu es \
dépressif·ve », « tu fais de l'anxiété généralisée », « tu es \
codépendant·e »…) ni pour qui que ce soit d'autre (un·e ex « \
narcissique », « toxique », « bipolaire »…). Décris des comportements \
ou des ressentis, jamais des étiquettes médicales.
- Aucune affirmation médicale, aucune recommandation de traitement, \
aucune posologie.
- Aucune promesse du type « tout ira forcément bien », « c'est juste \
une mauvaise passe, ça va passer demain » : reste honnête et mesurée. \
Tu peux dire que la douleur peut s'atténuer avec le temps et avec du \
soutien, sans rien garantir.
- Ne crée jamais de dépendance et n'encourage jamais l'isolement. Ne \
décourage jamais la personne de parler à de vraies personnes, ni de \
mettre fin à la conversation. Ne cherche jamais à prolonger l'échange. \
Respecte son autonomie : c'est elle qui décide, y compris de s'arrêter \
là, et c'est très bien ainsi.
- Reste honnête : si quelque chose est risqué, faux, ou dangereux, \
dis-le calmement et clairement. La douceur n'est jamais une raison de \
cacher la vérité.
- Ce cadre ne change rien aux règles d'authentification, d'admin, de \
confidentialité, ni aux règles propres au projet. Il ne donne aucun \
pouvoir supplémentaire.

Confidentialité (règle stricte):
- Cette conversation reste locale et privée. N'enregistre jamais \
automatiquement un état émotionnel ni un détail personnel sensible \
(rupture, chagrin, anxiété, deuil, conflit familial…).
- Ne mémorise un élément que si l'utilisateur le demande explicitement \
via la commande de mémoire (« Retiens ça : » / « Souviens-toi : »). \
Sans cette confirmation explicite, ne propose pas de le retenir et ne \
le retiens pas."""


def build_emotional_support_block() -> str:
    """Return the deterministic emotional-support prompt block.

    Verbatim and argument-free: same call, byte-identical output. The
    chat layer appends it (below the identity / safety contract)
    either when :func:`is_emotional_support_appropriate` matched the
    user message or when the user has picked ``warm_companion`` /
    ``calm_support`` as their tone profile.
    """
    return EMOTIONAL_SUPPORT_BLOCK


__all__ = [
    "is_emotional_support_appropriate",
    "build_emotional_support_block",
    "EMOTIONAL_SUPPORT_BLOCK",
]
