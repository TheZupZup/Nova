import math
import logging
from core.ollama_client import client

EMBED_MODEL = "nomic-embed-text"

logger = logging.getLogger(__name__)


def generate_embedding(text: str) -> list[float] | None:
    """
    Generates a vector embedding for `text` using the local Ollama instance.
    Returns None on any failure so callers can fall back to keyword search.
    """
    try:
        response = client.embeddings(model=EMBED_MODEL, prompt=text)
        return response["embedding"]
    except Exception as exc:
        logger.debug("Embedding generation skipped: %s", exc)
        return None


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Returns cosine similarity in [-1, 1]. Returns 0.0 if either vector is zero."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
