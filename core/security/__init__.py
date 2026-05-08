"""
Read-only security provider foundation for Nova (Phase 1).

Nova is the cognitive / conversational layer. It does **not** enforce
network policy, modify firewall rules, run privileged commands, or
take autonomous security actions. SilentGuard (and any future
security tool) remains the security/network engine.

This package provides the smallest abstraction Nova needs to talk to
such tools safely:

  * a ``SecurityStatus`` snapshot describing whether a security
    provider is reachable on this host.
  * a ``SecurityProvider`` Protocol with a single ``get_status``
    method that never raises.
  * a ``NullSecurityProvider`` default so Nova works normally when
    nothing is configured.
  * a ``SilentGuardProvider`` that probes SilentGuard's local
    presence (file-based today; ready for a future local read API).

Higher layers — the existing per-user gate in
``core.integrations.silentguard`` and the chat-side summariser in
``core.security_feed`` — keep their current responsibilities. This
package is a small, additive foundation, not a rewrite.

See ``docs/silentguard-integration-roadmap.md`` for the broader
design and the explicit non-goals (no blocking, no firewall
mutations, no background polling, no autonomous behaviour).
"""

from core.security.provider import (
    NullSecurityProvider,
    SecurityProvider,
    SecurityStatus,
    STATE_AVAILABLE,
    STATE_OFFLINE,
    STATE_UNAVAILABLE,
    now_iso,
)
from core.security.silentguard import SilentGuardProvider

__all__ = [
    "NullSecurityProvider",
    "SecurityProvider",
    "SecurityStatus",
    "SilentGuardProvider",
    "STATE_AVAILABLE",
    "STATE_OFFLINE",
    "STATE_UNAVAILABLE",
    "default_provider",
    "get_security_context_summary",
    "now_iso",
]


def default_provider() -> SecurityProvider:
    """Return the safe default security provider.

    Currently always a ``NullSecurityProvider`` so Nova's behaviour is
    unchanged when no provider has been wired up. Wiring a real
    provider (e.g. ``SilentGuardProvider``) into the chat / web
    paths is intentionally deferred to follow-up work.
    """
    return NullSecurityProvider()


def get_security_context_summary(
    provider: SecurityProvider | None = None,
) -> dict:
    """Return a small, prompt-friendly dict describing provider state.

    The shape mirrors ``SecurityStatus.as_dict``: short scalar
    fields only (``available``, ``service``, ``state``, ``message``,
    ``timestamp``), no raw payloads. Falls back to the safe default
    provider when no argument is supplied. Read-only.
    """
    active = provider if provider is not None else default_provider()
    return active.get_status().as_dict()
