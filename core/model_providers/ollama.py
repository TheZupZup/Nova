"""Ollama-backed model provider.

This is the default provider — it preserves Nova's pre-refactor Ollama
behaviour exactly. All the Ollama-specific knowledge that used to live in
``core.chat`` now lives here behind the :class:`ModelProvider` contract:

  * the exact ``client.chat(model=..., messages=...)`` call shape,
  * the streaming chunk duck-typing (``ollama>=0.4`` streams
    ``ChatResponse`` Pydantic objects — subscriptable but **not** ``dict``
    — older clients/tests yield plain dicts; both must work),
  * the legacy-client ``TypeError`` fallback when the installed
    ollama-python predates the ``stream=`` kwarg,
  * mapping ``(ollama.ResponseError, ConnectionError, httpx.HTTPError)``
    to :class:`ModelProviderError`.

The shared ``core.ollama_client.client`` singleton is resolved lazily on
every call rather than captured at construction, so existing tests that
``patch`` either ``core.ollama_client.client`` or its ``.chat`` attribute
keep working unchanged, and so a host can swap ``OLLAMA_HOST`` without a
process restart.
"""

from __future__ import annotations

from typing import Iterator, Optional

import httpx
import ollama

from .base import (
    ModelChunk,
    ModelProvider,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ProviderHealth,
)

# Backend transport/availability errors. Resolved via the ``ollama``
# module object at raise/except time (not bound at import) so a test that
# monkeypatches ``ollama.ResponseError`` is still matched — this mirrors
# the pre-refactor behaviour in ``core.chat``.
_TRANSPORT_ERRORS = (ConnectionError, OSError, httpx.HTTPError)


def _safe_get(obj, key: str):
    """Read ``key`` from a dict-like or Pydantic ``SubscriptableBaseModel``.

    Both shapes expose ``.get`` with the same contract, but a non-dict,
    non-Pydantic object (e.g. an unexpected string event) would raise on
    subscript. Falling back to ``getattr`` keeps the streaming loop
    robust without re-introducing an ``isinstance(dict)`` filter that
    silently drops valid ``ChatResponse`` events.
    """
    if obj is None:
        return None
    getter = getattr(obj, "get", None)
    if callable(getter):
        try:
            return getter(key)
        except TypeError:
            pass
    return getattr(obj, key, None)


def _iter_content_chunks(stream) -> Iterator[str]:
    """Yield non-empty ``message.content`` strings from an Ollama stream.

    Ollama's chat-stream generator yields events shaped like
    ``{"message": {"content": "..."}, "done": bool}`` — but the concrete
    type depends on the installed ollama-python client. ``ollama>=0.4``
    streams ``ChatResponse`` Pydantic models (subscriptable but **not**
    ``dict`` instances); older releases and our tests yield plain dicts.
    We duck-type on the ``.get`` API both shapes expose so neither one is
    silently dropped — an ``isinstance(event, dict)`` filter here was the
    source of empty-reply regressions in production. Some intermediate
    events have empty content (e.g. metadata frames) and the final
    ``done`` event also carries a synthetic empty content — skipping
    empties keeps the wire format tidy without affecting correctness.
    """
    for event in stream:
        if event is None:
            continue
        msg = _safe_get(event, "message") or {}
        chunk = _safe_get(msg, "content") or ""
        if isinstance(chunk, str) and chunk:
            yield chunk


def _is_response_error(exc: BaseException) -> bool:
    """True if ``exc`` is Ollama's ``ResponseError``.

    Looked up off the live ``ollama`` module so a monkeypatched
    ``ollama.ResponseError`` (used by the unreachable-backend tests) is
    still recognised. ``ResponseError`` may be absent when the optional
    ``ollama`` wheel is stubbed in CI — guard with ``getattr``.
    """
    response_error = getattr(ollama, "ResponseError", None)
    return isinstance(response_error, type) and isinstance(exc, response_error)


class OllamaProvider(ModelProvider):
    """Default provider. Talks to a local Ollama daemon."""

    name = "ollama"

    def __init__(self, client=None):
        # ``None`` => resolve the shared singleton lazily on every call
        # (see module docstring). An explicit client is only injected by
        # tests that want a fully isolated double.
        self._client_override = client

    def _client(self):
        if self._client_override is not None:
            return self._client_override
        # Late import + late attribute read: keeps `patch`-ability and
        # avoids an import cycle (core.ollama_client pulls in config).
        from core import ollama_client

        return ollama_client.client

    def generate(self, request: ModelRequest) -> ModelResponse:
        try:
            response = self._client().chat(
                model=request.model, messages=request.messages
            )
        except _TRANSPORT_ERRORS as exc:
            raise ModelProviderError(str(exc) or "Ollama unreachable") from exc
        except Exception as exc:  # noqa: BLE001 — narrowed below
            if _is_response_error(exc):
                raise ModelProviderError(
                    str(exc) or "Ollama error"
                ) from exc
            raise
        content = _safe_get(_safe_get(response, "message") or {}, "content")
        return ModelResponse(content=content or "", model=request.model)

    def stream(self, request: ModelRequest) -> Iterator[ModelChunk]:
        client = self._client()
        try:
            try:
                upstream = client.chat(
                    model=request.model,
                    messages=request.messages,
                    stream=True,
                )
            except TypeError:
                # Older ollama clients lacked the streaming kwarg. Fall
                # back to a single, non-streaming call so the path still
                # works — surfaced as one chunk.
                response = client.chat(
                    model=request.model, messages=request.messages
                )
                content = _safe_get(
                    _safe_get(response, "message") or {}, "content"
                )
                if content:
                    yield ModelChunk(content=content)
                return
            for chunk in _iter_content_chunks(upstream):
                yield ModelChunk(content=chunk)
        except _TRANSPORT_ERRORS as exc:
            raise ModelProviderError(str(exc) or "Ollama unreachable") from exc
        except Exception as exc:  # noqa: BLE001 — narrowed below
            if _is_response_error(exc):
                raise ModelProviderError(str(exc) or "Ollama error") from exc
            raise

    def health(self) -> ProviderHealth:
        """Read-only ``client.list()`` probe. Never raises, never pulls."""
        try:
            payload = self._client().list()
        except _TRANSPORT_ERRORS as exc:
            return ProviderHealth(
                ok=False, provider=self.name, detail=str(exc) or "unreachable"
            )
        except Exception as exc:  # noqa: BLE001 — narrowed below
            if _is_response_error(exc):
                return ProviderHealth(
                    ok=False,
                    provider=self.name,
                    detail=str(exc) or "error",
                )
            raise
        models: list[str] = []
        raw = _safe_get(payload, "models") or []
        try:
            for entry in raw:
                name = _safe_get(entry, "name") or _safe_get(entry, "model")
                if isinstance(name, str) and name:
                    models.append(name)
        except TypeError:
            # Unexpected payload shape — still healthy (Ollama answered),
            # just no parseable model list.
            models = []
        return ProviderHealth(ok=True, provider=self.name, models=models)


_default: Optional[OllamaProvider] = None


def get_ollama_provider() -> OllamaProvider:
    """Process-wide singleton. Cheap; holds no captured client."""
    global _default
    if _default is None:
        _default = OllamaProvider()
    return _default
