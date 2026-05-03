from memory.embeddings import generate_embedding, cosine_similarity
from memory import store as _store
from memory.store import search_memories, list_memories
from memory.schema import Memory

# Memories with a cosine score below this threshold are considered irrelevant.
_COSINE_THRESHOLD = 0.40


def get_relevant_memories(message: str, user_id: int, limit: int = 8, db_path: str | None = None) -> list[Memory]:
    """
    Returns up to `limit` memories owned by `user_id` and relevant to `message`.

    Cross-user retrieval is impossible by construction — every read goes
    through the user-scoped store layer.

    Strategy:
    - If Ollama is reachable, use cosine similarity for memories that have
      embeddings, and keyword search for older memories that don't.
    - If Ollama is unreachable (generate_embedding returns None), fall back
      entirely to keyword search (v1 behaviour).
    """
    if db_path is None:
        db_path = _store.DB_PATH
    query_emb = generate_embedding(message)
    if query_emb is None:
        return search_memories(message, user_id, limit=limit, db_path=db_path)

    all_mems = list_memories(user_id, db_path=db_path)

    scored: list[tuple[float, Memory]] = []
    without_embedding: list[Memory] = []

    for mem in all_mems:
        if mem.embedding:
            scored.append((cosine_similarity(query_emb, mem.embedding), mem))
        else:
            without_embedding.append(mem)

    scored.sort(key=lambda x: x[0], reverse=True)
    results = [m for score, m in scored if score >= _COSINE_THRESHOLD][:limit]

    # Fill remaining slots with keyword matches for legacy memories (no embedding).
    if without_embedding and len(results) < limit:
        kw = search_memories(message, user_id, limit=limit - len(results), db_path=db_path)
        seen = {m.id for m in results}
        results += [m for m in kw if m.id not in seen]

    return results[:limit]


def format_for_prompt(memories: list[Memory]) -> str:
    """Formats a list of memories into the context block injected into the system prompt."""
    if not memories:
        return ""
    lines = ["Relevant user memory:"]
    for m in memories:
        lines.append(f"- [{m.kind}/{m.topic}] {m.content}")
    return "\n".join(lines)
