from core.memory import save_memory

_MANUAL_PREFIXES = (
    "retiens ça:",
    "souviens-toi de ça:",
    "souviens-toi:",
)


def handle_manual_memory_command(message: str) -> str | None:
    """
    Detects explicit manual memory commands and saves their content.

    Matches "Retiens ça:", "Souviens-toi de ça:", or "Souviens-toi:" (case-insensitive).
    Saves everything after the first colon as-is under the "manual" category.
    Returns a confirmation string if matched, None if the message is not a memory command.
    """
    stripped = message.lstrip()
    lower = stripped.lower()
    for prefix in _MANUAL_PREFIXES:
        if lower.startswith(prefix):
            content = stripped[len(prefix):].strip()
            if not content:
                return "Rien à sauvegarder : le contenu est vide."
            save_memory("manual", content)
            return f"Souvenir sauvegardé : {content}"
    return None
