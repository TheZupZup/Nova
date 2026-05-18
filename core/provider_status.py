"""Read-only model-provider status reporter (Phase 1 of provider settings).

This module answers, calmly and read-only, the two questions an operator
asks about Nova's model backend:

* **"Which provider is Nova configured to use, and is that configuration
  coherent?"** — :func:`get_provider_status`. No network I/O, never
  raises into the caller. An unknown / unregistered configured provider
  is reported as an ``error`` string, never as an exception.
* **"Does that backend actually answer right now?"** —
  :func:`probe_provider_health`. Delegates to the provider's own cheap,
  read-only ``health()`` probe (``client.list()`` for Ollama — never a
  model pull, never a generation). ``health()`` never raises by
  contract; an unreachable backend is surfaced as ``ok=False`` with a
  short, non-sensitive detail.

Scope / safety contract (Phase 1):

* **Read-only.** Nothing here writes settings, mutates the registry,
  triggers a model download, or restarts anything. Provider selection
  stays env-driven (``NOVA_MODEL_PROVIDER``); this module only reports
  the resolved state and probes liveness.
* **Ollama stays the default.** :data:`DEFAULT_PROVIDER` is ``"ollama"``
  and the status flags whether the configured provider differs so a
  non-default backend is never silent.
* **MockProvider stays test-only.** ``mock`` is never advertised as a
  selectable production backend (:data:`TEST_ONLY_PROVIDERS`). If Nova
  is *configured* to use it the status still reports that truthfully —
  with a clear warning — so a stray test setting can never hide.
* **No secrets.** The only env-derived string surfaced is the Ollama
  host, with any ``user:pass@`` userinfo redacted before display.

It is the read-only foundation under the admin-only
``/admin/provider/status`` and ``/admin/provider/test-connection``
endpoints.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)


#: Nova's default model provider. Deployments that never set
#: ``NOVA_MODEL_PROVIDER`` keep using this; the status surface flags any
#: deviation so a non-default backend is never silent.
DEFAULT_PROVIDER = "ollama"

#: Providers that exist for tests / offline development only and must
#: never be advertised to operators as a production choice. They are
#: still reported *if explicitly configured* (with a warning) so the
#: state is never hidden — they are only filtered out of the
#: "selectable" list.
TEST_ONLY_PROVIDERS: frozenset[str] = frozenset({"mock"})


# ── Helpers ─────────────────────────────────────────────────────────


def _redact_userinfo(url: str) -> str:
    """Return ``url`` with any ``user:pass@`` userinfo removed.

    ``OLLAMA_HOST`` is normally a plain ``http://localhost:11434`` and
    safe to show, but a host *can* embed credentials in the netloc. We
    strip those before surfacing so the status endpoint never leaks a
    secret. Best-effort and never raises: an unparseable value is
    masked conservatively rather than echoed back.
    """
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        netloc = parts.netloc
        if "@" not in netloc:
            return url
        host = parts.hostname or ""
        try:
            port = parts.port
        except ValueError:
            port = None
        if port is not None:
            host = f"{host}:{port}"
        return urlunsplit(
            (parts.scheme, host, parts.path, parts.query, parts.fragment)
        )
    except ValueError:
        return "<host redacted>"


# ── Dataclass ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProviderStatus:
    """Calm, read-only snapshot of Nova's model-provider configuration.

    Every attribute is JSON-serialisable so the admin endpoint can hand
    it to FastAPI without bespoke encoders.

    * ``configured_provider`` — the provider name Nova is configured to
      use (``config.MODEL_PROVIDER`` / ``NOVA_MODEL_PROVIDER``,
      normalised, default ``"ollama"``).
    * ``default_provider`` — always :data:`DEFAULT_PROVIDER`; surfaced
      so the UI never hard-codes the word "ollama".
    * ``active_provider`` — the resolved provider's backend label, or
      ``None`` when the configured provider cannot be resolved (see
      ``error``).
    * ``is_default`` — whether the configured provider is the default.
    * ``selectable_providers`` — registered providers an operator may
      choose, with test-only backends filtered out.
    * ``test_only_providers`` — registered test-only backends, surfaced
      for transparency so the UI can label them clearly.
    * ``ollama_host`` — the configured Ollama host with any userinfo
      redacted (informational; Ollama is the default backend).
    * ``error`` — a short message when the configured provider is not
      registered, else ``""``.
    * ``warnings`` — human-readable messages the UI renders verbatim.
    """

    configured_provider: str
    default_provider: str
    active_provider: Optional[str]
    is_default: bool
    selectable_providers: tuple[str, ...]
    test_only_providers: tuple[str, ...]
    ollama_host: str
    error: str = ""
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "configured_provider": self.configured_provider,
            "default_provider": self.default_provider,
            "active_provider": self.active_provider,
            "is_default": self.is_default,
            "selectable_providers": list(self.selectable_providers),
            "test_only_providers": list(self.test_only_providers),
            "ollama_host": self.ollama_host,
            "error": self.error,
            "warnings": list(self.warnings),
        }


# ── Public entry points ─────────────────────────────────────────────


def get_provider_status() -> ProviderStatus:
    """Return a calm, read-only snapshot of the provider configuration.

    Safe to call at any time and from any thread; never raises into the
    caller and never reaches the network. An unknown configured provider
    surfaces as a populated ``error`` / ``warnings`` pair with
    ``active_provider=None`` rather than as an exception — the same
    "report it, don't crash" stance as ``core.storage_status``.

    Imports are deferred so the module has no import-time ``config``
    dependency and tests can monkeypatch ``config.MODEL_PROVIDER`` /
    the registry before the first call.
    """
    from config import MODEL_PROVIDER, OLLAMA_HOST
    from core.model_providers import (
        ModelProviderError,
        available_providers,
        get_provider,
    )

    configured = (
        (MODEL_PROVIDER or DEFAULT_PROVIDER).strip().lower()
        or DEFAULT_PROVIDER
    )
    is_default = configured == DEFAULT_PROVIDER

    try:
        names = list(available_providers())
    except Exception as exc:  # pragma: no cover - registry is in-memory
        logger.warning("provider status: available_providers failed: %s", exc)
        names = [DEFAULT_PROVIDER]

    selectable = tuple(n for n in names if n not in TEST_ONLY_PROVIDERS)
    test_only = tuple(n for n in names if n in TEST_ONLY_PROVIDERS)

    error = ""
    active: Optional[str] = None
    try:
        active = get_provider().name
    except ModelProviderError as exc:
        error = str(exc) or f"unknown model provider {configured!r}"
    except Exception as exc:  # never raise into the caller
        logger.warning("provider status: unexpected resolve failure: %s", exc)
        error = "provider could not be resolved"

    warnings: list[str] = []
    if error:
        warnings.append(
            f"Nova is configured to use the {configured!r} model "
            f"provider, but it is not registered. Nova cannot generate "
            f"text until this is fixed — set NOVA_MODEL_PROVIDER="
            f"{DEFAULT_PROVIDER} (the default) or register the provider."
        )
    elif configured in TEST_ONLY_PROVIDERS:
        warnings.append(
            f"Nova is configured to use the test-only {configured!r} "
            f"provider. It returns canned text and never talks to a real "
            f"model — intended for tests / offline development only. Set "
            f"NOVA_MODEL_PROVIDER={DEFAULT_PROVIDER} for normal use."
        )
    elif not is_default:
        warnings.append(
            f"Nova is using the {configured!r} provider instead of the "
            f"default {DEFAULT_PROVIDER!r}. This is supported; Ollama "
            f"remains the default and recommended local backend."
        )

    return ProviderStatus(
        configured_provider=configured,
        default_provider=DEFAULT_PROVIDER,
        active_provider=active,
        is_default=is_default,
        selectable_providers=selectable,
        test_only_providers=test_only,
        ollama_host=_redact_userinfo((OLLAMA_HOST or "").strip()),
        error=error,
        warnings=tuple(warnings),
    )


def probe_provider_health(name: Optional[str] = None) -> dict:
    """Live, read-only liveness probe for the configured provider.

    Resolves the provider (the configured one unless ``name`` is given)
    and delegates to its ``health()`` — a cheap read-only call
    (``client.list()`` for Ollama; never a pull, never a generation).
    ``health()`` never raises by contract, and an unknown / unregistered
    provider is mapped to a clean ``ok=False`` result here instead of an
    exception, so the endpoint always returns one stable JSON shape:

        ``{"ok": bool, "provider": str, "detail": str, "models": [str]}``

    Never raises into the caller — a misbehaving third-party provider
    that breaks the "health never raises" contract is still contained.
    """
    from core.model_providers import ModelProviderError, get_provider

    try:
        provider = get_provider(name)
    except ModelProviderError as exc:
        requested = (name or "").strip().lower()
        if not requested:
            # No explicit name → fall back to the *configured*
            # provider so the result is self-describing ("backend:
            # does-not-exist — not registered") instead of a bare
            # "unknown". Read calmly; never let this raise.
            try:
                from config import MODEL_PROVIDER

                requested = (MODEL_PROVIDER or "").strip().lower()
            except Exception:  # pragma: no cover - config import is stable
                requested = ""
        return {
            "ok": False,
            "provider": requested or "unknown",
            "detail": str(exc) or "unknown model provider",
            "models": [],
        }
    except Exception as exc:  # never raise into the endpoint
        logger.warning("provider health: unexpected resolve failure: %s", exc)
        return {
            "ok": False,
            "provider": (name or "unknown"),
            "detail": "provider could not be resolved",
            "models": [],
        }

    try:
        health = provider.health()
    except Exception as exc:  # base contract says never; stay calm anyway
        logger.warning("provider health: probe raised: %s", exc)
        return {
            "ok": False,
            "provider": getattr(provider, "name", name or "unknown"),
            "detail": "health probe failed unexpectedly",
            "models": [],
        }

    return {
        "ok": bool(health.ok),
        "provider": health.provider,
        "detail": health.detail,
        "models": list(health.models),
    }


__all__ = [
    "DEFAULT_PROVIDER",
    "TEST_ONLY_PROVIDERS",
    "ProviderStatus",
    "get_provider_status",
    "probe_provider_health",
]
