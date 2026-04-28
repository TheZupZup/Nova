import re
from memory.schema import Memory

# Credentials, tokens, financial details, and personal identifiers
_SENSITIVE_PATTERNS = [
    r"\b(password|mot de passe|passwd|passphrase)\b",
    r"\b(token|api[_\s-]?key|access[_\s-]?key|secret[_\s-]?key|private[_\s-]?key)\b",
    r"\b\d{13,19}\b",                    # raw card numbers
    r"\b(credit card|carte de crédit|carte bancaire|cvv|cvc|iban|swift|bic)\b",
    r"\b(social security|numéro de sécurité sociale|ssn|sin number)\b",
    r"[A-Za-z0-9+/]{32,}={0,2}\b",      # base64-encoded secrets / long tokens
]

# Health, politics, religion, relationships — sensitive identity information
_IDENTITY_PATTERNS = [
    r"\b(depression|anxiety|cancer|diabetes|hiv|sida|antidépresseur|medication|médicament|therapist|thérapie)\b",
    r"\b(republican|democrat|liberal|conservative|trump|biden|macron|le pen|political party|parti politique)\b",
    r"\b(vote for|voted for|my religion|my faith|my church|ma religion|ma foi)\b",
]

# Transient, short-lived details that are not worth persisting
_TRANSIENT_PATTERNS = [
    r"\b(right now|en ce moment|tonight|ce soir|this morning|ce matin)\b",
    r"\b(i feel|je me sens|i'm (sad|angry|depressed|happy today)|je suis (triste|en colère))\b",
    r"\b(my ex|mon ex|breakup|rupture|divorce|cheated|trompé|cheating)\b",
    r"\b(\d+\s+(?:rue|avenue|boulevard|street|drive|lane|road))\b",  # private addresses
]


def is_memory_allowed(memory: Memory) -> bool:
    """
    Returns True if the memory is safe and durable enough to store.
    Rejects credentials, sensitive identity info, and transient emotions.
    """
    combined = f"{memory.topic} {memory.content}".lower()

    for pattern in _SENSITIVE_PATTERNS:
        if re.search(pattern, combined, re.IGNORECASE):
            return False

    for pattern in _IDENTITY_PATTERNS:
        if re.search(pattern, combined, re.IGNORECASE):
            return False

    for pattern in _TRANSIENT_PATTERNS:
        if re.search(pattern, combined, re.IGNORECASE):
            return False

    return True
