"""Provider registry — the one place Nova core asks "who generates text?".

``get_provider()`` returns the configured backend (Ollama by default, so
existing deployments are unaffected). New local runtimes register a
factory under a name; nothing in Nova core changes when they do. There is
no network I/O here and no model download — registering or resolving a
provider is cheap and side-effect free.

Selection precedence:

  1. an explicit test override (:func:`use_provider` / :func:`set_override`),
  2. an explicit ``name`` argument to :func:`get_provider`,
  3. ``config.MODEL_PROVIDER`` (env ``NOVA_MODEL_PROVIDER``, default
     ``"ollama"``).

Instances are cached per name so the Ollama provider stays a process-wide
singleton; :func:`reset` clears the cache and override for test isolation.
"""

from __future__ import annotations

import contextlib
import threading
from typing import Callable, Dict, Iterator, Optional

from .base import ModelProvider, ModelProviderError
from .llamacpp import get_llamacpp_provider
from .mock import MockProvider
from .ollama import get_ollama_provider

ProviderFactory = Callable[[], ModelProvider]

_lock = threading.Lock()
_factories: Dict[str, ProviderFactory] = {
    "ollama": get_ollama_provider,
    "llamacpp": get_llamacpp_provider,
    "mock": MockProvider,
}
_instances: Dict[str, ModelProvider] = {}
_override: Optional[ModelProvider] = None


def register_provider(name: str, factory: ProviderFactory) -> None:
    """Register (or replace) a provider factory under ``name``.

    A future ``LlamaCppProvider`` / ``TransformersProvider`` /
    ``NovaModelProvider`` plugs in here without touching call sites. Any
    cached instance for ``name`` is dropped so the new factory takes
    effect on the next :func:`get_provider`.
    """
    key = (name or "").strip().lower()
    if not key:
        raise ValueError("provider name must be a non-empty string")
    with _lock:
        _factories[key] = factory
        _instances.pop(key, None)


def available_providers() -> list[str]:
    """Names that can currently be resolved (sorted, for stable output)."""
    with _lock:
        return sorted(_factories)


def _default_name() -> str:
    # Imported lazily so the registry has no import-time config dependency
    # and tests can monkeypatch config.MODEL_PROVIDER before first use.
    from config import MODEL_PROVIDER

    return (MODEL_PROVIDER or "ollama").strip().lower() or "ollama"


def get_provider(name: Optional[str] = None) -> ModelProvider:
    """Resolve a provider following the precedence in the module docstring.

    Raises :class:`ModelProviderError` for an unknown name rather than a
    bare ``KeyError`` so callers handle one exception type. An unknown
    *configured default* is treated the same way — misconfiguration is
    surfaced loudly instead of silently falling back.
    """
    if _override is not None:
        return _override

    key = (name or "").strip().lower() or _default_name()
    with _lock:
        cached = _instances.get(key)
        if cached is not None:
            return cached
        factory = _factories.get(key)
        if factory is None:
            raise ModelProviderError(
                f"unknown model provider {key!r}; "
                f"known: {sorted(_factories)}"
            )
        instance = factory()
        _instances[key] = instance
        return instance


def evict_provider(name: str) -> None:
    """Drop the cached instance for ``name`` so the next resolve rebuilds it.

    Used when a provider's *resolved configuration* changes at runtime —
    e.g. an admin updates the GGUF model path — so the change takes effect
    on the next :func:`get_provider` without a process restart. Unknown /
    blank names are a no-op; the factory itself is left registered.
    """
    key = (name or "").strip().lower()
    if not key:
        return
    with _lock:
        _instances.pop(key, None)


def set_override(provider: Optional[ModelProvider]) -> None:
    """Force :func:`get_provider` to return ``provider`` (tests only).

    Passing ``None`` clears the override.
    """
    global _override
    _override = provider


@contextlib.contextmanager
def use_provider(provider: ModelProvider) -> Iterator[ModelProvider]:
    """Scope an override to a ``with`` block (tests only)."""
    global _override
    prev = _override
    _override = provider
    try:
        yield provider
    finally:
        _override = prev


def reset() -> None:
    """Drop cached instances and any override (test isolation)."""
    global _override
    with _lock:
        _instances.clear()
    _override = None
