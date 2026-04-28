from memory.store import search_memories, DB_PATH
from memory.schema import Memory


def get_relevant_memories(message: str, limit: int = 8, db_path: str = DB_PATH) -> list[Memory]:
    """
    Returns up to `limit` memories relevant to the given user message.
    Uses keyword matching against memory topic and content.
    """
    return search_memories(message, limit=limit, db_path=db_path)


def format_for_prompt(memories: list[Memory]) -> str:
    """Formats a list of memories into the context block injected into the system prompt."""
    if not memories:
        return ""
    lines = ["Relevant user memory:"]
    for m in memories:
        lines.append(f"- [{m.kind}/{m.topic}] {m.content}")
    return "\n".join(lines)
