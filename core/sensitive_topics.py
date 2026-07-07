"""
Sensitive-topic detectors for memory privacy.

Nova auto-extracts durable memories from ordinary conversation. Some
turns must never be auto-persisted: who the user is dating or breaking
up with, that they were distressed, grieving, or in crisis, or any
other emotionally heavy disclosure. These deterministic detectors are
the single source of truth for that privacy gate. They are used by
``core.chat`` (to skip auto-extraction for a turn) and by
``memory.policy`` (defence in depth on the durable store).

This module shapes **what Nova remembers**, never how Nova speaks: it
builds no prompt text, defines no tone or "mode", and grants no
capability. The user can always save something deliberately via the
manual memory command ("Retiens ça:" / "Souviens-toi:"), which runs in
the web preflight and bypasses these gates on purpose.

Boundaries enforced here (commitments, not aspirations):

  * **Deterministic.** No LLM in the loop. Same input, same output.
  * **Pure / no I/O.** Only the standard library is imported. Nothing
    here reads the disk, the network, the database, or any setting, so
    it can be imported from any layer (including ``memory.policy``)
    without a cycle.
  * **Never raises.** Non-string input coerces to ``False``.
  * **Conservative detection.** Triggers are topic-specific multi-word
    phrases (EN + FR — Nova is bilingual), mostly anchored to a
    first-person subject or a possessive, so idioms and technical chat
    ("this bug is killing me", "we broke up the monolith", "a lonely
    server in production") do not match. Over-blocking here is the safe
    direction: the only effect is skipping auto-extraction on a
    sensitive turn, which is exactly the desired privacy posture.
"""

from __future__ import annotations

import re

# ── Relationship detail ──────────────────────────────────────────────────────
# Phrases that mark a message as carrying *sensitive relationship
# detail*. Unambiguous substrings: nouns / phrases that are almost never
# anything but a romantic relationship, so they are safe to match in
# *any* phrasing — first / second / third person, including the memory
# extractor's "User's wife…" form. "fiance" (ASCII) is listed before the
# accented forms and subsumes "fiancee"/"fiances".
_SENSITIVE_RELATIONSHIP_PATTERNS: tuple[str, ...] = (
    # English
    "girlfriend", "boyfriend", "wife", "husband", "spouse",
    "fiance", "fiancé", "fiancée",
    "marriage", "married", "divorce", "breakup", "broke up", "break up",
    "cheated on", "cheating on", "infidelity", "in love with",
    # NB: bare conflict phrases ("we argued", "we had a fight/argument")
    # are deliberately NOT here — they are common in work / project
    # contexts, so matching them would make the gate over-block
    # legitimate non-relationship memory. A relationship argument is
    # still caught via a partner noun ("my wife and I had a fight").
    "my relationship", "our relationship", "my marriage", "my couple",
    "we slept together", "relationship problem", "relationship tension",
    # French
    "copine", "copain", "compagne", "compagnon", "petite amie",
    "petit ami", "époux", "épouse", "conjoint", "conjointe",
    "rupture", "infidèle", "infidélité", "amoureux de", "amoureuse de",
    "ma relation", "notre relation", "mon couple", "on a rompu",
    "il m'a trompé", "elle m'a trompé", "on a couché",
    "dispute de couple", "tension dans mon couple",
    "tension dans ma relation",
)

# A few nouns are too ambiguous to match bare: "partner" (a *business*
# partner), "ex" (substring of "example"/"next"), French "mari"
# (substring of "marin"/"marinade") and "femme" ("a woman"). Requiring
# a possessive / pronoun determiner right before the noun keeps them
# precise while still catching every grammatical person — first
# ("my partner"), second ("your partner", "ton mari") and third, the
# form an assistant reply or the memory extractor restates context in
# ("his partner", "User's partner", "son mari", "votre femme", or
# "la femme de l'utilisateur"). \b…\b boundaries stop "your example",
# "next", and "marina" from matching. Enumerating determiners — rather
# than one more first-person string per round — is what actually closes
# the cross-pronoun privacy hole.
_AMBIGUOUS_RELATIONSHIP_RE = re.compile(
    r"\b(?:"
    r"my|your|his|her|their|our|its|user'?s|users|"
    r"mon|ma|mes|ton|ta|tes|son|sa|ses|votre|vos|notre|nos|leur|leurs"
    r")\s+(?:ex[-\s]?)?(?:partner|partenaire|mari|femme|ex)\b"
    r"|\b(?:partner|partenaire|mari|femme)\s+de\s+l['’]utilisateur\b",
    re.IGNORECASE,
)


def is_sensitive_relationship_content(text: str) -> bool:
    """True iff ``text`` carries sensitive relationship detail.

    Used as a gate: automatic memory extraction is skipped when this
    returns ``True`` so Nova never silently persists who the user is
    dating, fighting with, or breaking up with. Explicit, user-approved
    saves (the manual memory command) are handled elsewhere and are
    intentionally **not** affected by this gate.

    Matching is two-tier: a fast substring pass over the unambiguous
    vocabulary, then a determiner-anchored regex for the handful of
    nouns ("partner", "ex", "mari", "femme") that are only sensitive
    when owned by someone. The regex is pronoun-agnostic on purpose so
    a reply that restates context in the second or third person is
    still blocked.

    Non-strings coerce to ``False``.
    """
    if not isinstance(text, str):
        return False
    lowered = text.lower()
    if any(p in lowered for p in _SENSITIVE_RELATIONSHIP_PATTERNS):
        return True
    return _AMBIGUOUS_RELATIONSHIP_RE.search(lowered) is not None


# ── Distress / mental-state detail ───────────────────────────────────────────
# Distress-specific multi-word phrases. Self-harm / suicidal phrasing is
# inherently multi-word and high-precision; the acute-overwhelm phrases
# are anchored to a first-person subject ("i'm falling apart", not bare
# "falling apart") so a sentence about a server or a budget cannot match.
_DISTRESS_TRIGGERS: tuple[str, ...] = (
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

# Phrases that mark a message as carrying sensitive emotional / mental
# state detail. Tighter-than-it-looks: every entry is a distress /
# mental-health specific multi-word phrase. Bare "feel" / "sad" /
# "happy" / "lost my" are deliberately absent so ordinary preference or
# project memory ("User is happy with Fedora", "User lost my edits to a
# crash") is not silently dropped.
#
# The memory extractor restates memories in the third person ("User
# said they want to die"), so the severe vocabulary here is
# deliberately person-agnostic ("depressed", "suicidal", "kill
# themselves", "déprimé", "désespéré") — mirroring how the relationship
# gate stays pronoun-agnostic so extractor phrasing cannot slip past it.
_SENSITIVE_EMOTIONAL_PATTERNS: tuple[str, ...] = _DISTRESS_TRIGGERS + (
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


# ── Broader emotional disclosure ─────────────────────────────────────────────
# First-person, emotion-specific phrases wider than the severe set above
# (a breakup, a wave of sadness, a lonely evening, a worried night).
# Used only by the chat-layer autosave gate: these turns are personal
# enough that nothing from them should silently land in durable memory,
# even when no crisis vocabulary appears.
_EMOTIONAL_DISCLOSURE_TRIGGERS: tuple[str, ...] = (
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
    # English — loneliness (first-person)
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
    "broke up with me",  # subsumes "she/he/they broke up with me"
    "broken up with me", "broke up with my", "she left me", "he left me",
    "she dumped me", "he dumped me", "i got dumped", "i was dumped",
    "we just broke up", "i just broke up with",
    "going through a breakup", "going through a break-up",
    "going through a break up", "after the breakup", "after my breakup",
    "she cheated on me", "he cheated on me",
    "we just split up",
    # English — anxiety / worry / overwhelm (first-person anchored).
    # Intensified forms ("i'm so anxious", "i feel really anxious")
    # are listed explicitly because intensifiers break the bare
    # substring match — "i feel anxious" is not a substring of "i
    # feel so anxious", so the pattern would miss otherwise. Bare
    # "so anxious" / "really overwhelmed" are intentionally NOT in
    # the list: a sentence like "the server is really overwhelmed
    # by traffic" must not trip the gate.
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


def is_emotional_disclosure(text: object) -> bool:
    """True iff ``text`` carries a first-person emotional disclosure.

    Wider than :func:`is_sensitive_emotional_content` (sadness,
    loneliness, a breakup, worry — not only crisis vocabulary). Used by
    the chat-layer autosave gate so nothing from an emotionally personal
    turn silently lands in durable memory.

    Conservative on purpose: only emotion-specific multi-word
    first-person phrases match, so generic conversation ("this is a
    sad movie", "a lonely server in production", "we broke up the
    monolith into services") never trips the gate. Non-strings coerce
    to ``False`` so the helper is safe to call from any path without a
    guard.
    """
    if not isinstance(text, str):
        return False
    lowered = text.lower()
    return any(trigger in lowered for trigger in _EMOTIONAL_DISCLOSURE_TRIGGERS)


__all__ = [
    "is_sensitive_relationship_content",
    "is_sensitive_emotional_content",
    "is_emotional_disclosure",
]
