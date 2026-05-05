from typing import Optional

import httpx
import ollama
from config import OLLAMA_HOST

# Short connect timeout so unreachable Ollama fails fast.
# No read timeout — large models can take minutes to generate.
client = ollama.Client(
    host=OLLAMA_HOST,
    timeout=httpx.Timeout(5.0, read=None),
)


class OllamaUnavailable(Exception):
    """Raised when Ollama's local API cannot be reached or replies in
    an unexpected shape. Callers map this to a controlled error
    (e.g. an HTTP 503) instead of leaking transport details."""


# `/api/tags` is a small JSON read; keep both timeouts tight so a stalled
# daemon does not block the admin endpoint.
_TAGS_TIMEOUT = httpx.Timeout(5.0, read=10.0)


def list_local_models(host: Optional[str] = None) -> list[dict]:
    """
    Read the list of installed models from Ollama's `GET /api/tags`.

    Returns one dict per model with at least a `name` field. Empty list
    is a valid result (Ollama is reachable but has no models installed).

    Raises `OllamaUnavailable` if Ollama is unreachable, the request
    times out, the HTTP status is non-2xx, or the response is not
    parseable JSON of the expected shape.

    This call is read-only — it never triggers a pull or a download.
    """
    base = (host or OLLAMA_HOST).rstrip("/")
    url = f"{base}/api/tags"
    try:
        resp = httpx.get(url, timeout=_TAGS_TIMEOUT)
        resp.raise_for_status()
    except (httpx.HTTPError, OSError) as exc:
        raise OllamaUnavailable(
            f"could not reach Ollama at {url!r}"
        ) from exc

    try:
        payload = resp.json()
    except ValueError as exc:
        raise OllamaUnavailable("Ollama returned non-JSON response") from exc

    if not isinstance(payload, dict):
        raise OllamaUnavailable("Ollama returned unexpected payload shape")

    raw = payload.get("models", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise OllamaUnavailable("Ollama returned non-list 'models' field")

    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("model")
        if not isinstance(name, str) or not name:
            continue
        out.append({
            "name": name,
            "digest": entry.get("digest"),
            "size": entry.get("size"),
            "modified_at": entry.get("modified_at"),
        })
    return out
