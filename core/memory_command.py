from core.memory import save_memory

_MANUAL_PREFIXES = (
    "retiens ça:",
    "souviens-toi de ça:",
    "souviens-toi:",
)


def handle_manual_memory_command(
    message: str, user_id: int, project_id: int | None = None
) -> str | None:
    """
    Detects explicit manual memory commands and saves their content for `user_id`.

    Matches "Retiens ça:", "Souviens-toi de ça:", or "Souviens-toi:" (case-insensitive).
    Saves everything after the first colon as-is under the "manual" category,
    attributed to `user_id`.

    ``project_id`` is the active project for the chat (or ``None`` for a
    General chat): an explicit "remember this" issued while a project is
    active is stored as that project's memory, mirroring how
    auto-extracted memory is scoped. ``None`` keeps the pre-projects
    behaviour (global memory).

    Returns a confirmation string if matched, None if the message is not a memory command.
    """
    stripped = message.lstrip()
    lower = stripped.lower()
    for prefix in _MANUAL_PREFIXES:
        if lower.startswith(prefix):
            content = stripped[len(prefix):].strip()
            if not content:
                return "Rien à sauvegarder : le contenu est vide."
            # Keep the General path's call shape identical to the
            # pre-projects one (3 args) — only thread project_id when a
            # project is actually active.
            if project_id is None:
                save_memory("manual", content, user_id)
            else:
                save_memory("manual", content, user_id, project_id)
            return f"Souvenir sauvegardé : {content}"
    return None
