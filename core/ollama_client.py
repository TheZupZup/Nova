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


# ``/api/ps`` and ``/api/show`` are small JSON reads used by the local
# model-health surface. Both are strictly read-only: ``ps`` reports what
# the daemon currently holds in memory, ``show`` reports a model's own
# metadata. **Neither downloads anything** — ``show`` fails with a 404
# for a model that is not installed rather than fetching it.
_PS_TIMEOUT = httpx.Timeout(5.0, read=10.0)
_SHOW_TIMEOUT = httpx.Timeout(5.0, read=15.0)


def list_running_models(host: Optional[str] = None) -> list[dict]:
    """
    Read the models Ollama currently has **loaded** (`GET /api/ps`).

    Returns one dict per loaded model with `name` plus whatever the
    daemon reports: `size` (total resident bytes), `size_vram`
    (accelerator-resident bytes, when the daemon knows), `context_size`,
    and `expires_at` (when the model will be unloaded). Fields the
    daemon omits are simply absent — Nova makes **no assumption about
    which accelerator, vendor, or driver** is in use, and a CPU-only
    host is a normal, fully-supported answer.

    An empty list is a valid result (Ollama is reachable, nothing
    loaded). Raises `OllamaUnavailable` on transport / status / shape
    failures, exactly like :func:`list_local_models`.

    Read-only — it never triggers a pull, a load, or a generation.
    """
    base = (host or OLLAMA_HOST).rstrip("/")
    url = f"{base}/api/ps"
    try:
        resp = httpx.get(url, timeout=_PS_TIMEOUT)
        resp.raise_for_status()
    except (httpx.HTTPError, OSError) as exc:
        raise OllamaUnavailable(f"could not reach Ollama at {url!r}") from exc

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
        row: dict = {"name": name}
        for key in ("size", "size_vram", "expires_at", "digest"):
            if entry.get(key) is not None:
                row[key] = entry[key]
        details = entry.get("details")
        if isinstance(details, dict):
            for key in ("parameter_size", "quantization_level", "family"):
                if isinstance(details.get(key), str) and details[key]:
                    row[key] = details[key]
        ctx = entry.get("context_length") or entry.get("context_size")
        if isinstance(ctx, int) and ctx > 0:
            row["context_size"] = ctx
        out.append(row)
    return out


def show_model(name: str, host: Optional[str] = None) -> dict:
    """
    Read one installed model's metadata (`POST /api/show`).

    Despite the verb this is a **read**: Ollama returns metadata for a
    model it already has and answers 404 for one it does not — it never
    pulls. Returns the raw JSON dict (callers pick the fields they
    need). Raises `OllamaUnavailable` when the daemon is unreachable,
    the model is unknown, or the response is not a JSON object.
    """
    model = (name or "").strip()
    if not model:
        raise OllamaUnavailable("a model name is required")
    base = (host or OLLAMA_HOST).rstrip("/")
    url = f"{base}/api/show"
    try:
        resp = httpx.post(url, json={"model": model}, timeout=_SHOW_TIMEOUT)
        resp.raise_for_status()
    except (httpx.HTTPError, OSError) as exc:
        raise OllamaUnavailable(f"could not read model info from {url!r}") from exc

    try:
        payload = resp.json()
    except ValueError as exc:
        raise OllamaUnavailable("Ollama returned non-JSON response") from exc
    if not isinstance(payload, dict):
        raise OllamaUnavailable("Ollama returned unexpected payload shape")
    return payload
