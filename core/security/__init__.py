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
    presence — the on-disk memory file by default, or, when
    ``NOVA_SILENTGUARD_API_URL`` is configured, SilentGuard's optional
    loopback ``/status`` endpoint via :class:`SilentGuardClient`.

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
from core.security.silentguard_client import SilentGuardClient
from core.security.context import build_security_context_block

__all__ = [
    "NullSecurityProvider",
    "SecurityProvider",
    "SecurityStatus",
    "SilentGuardClient",
    "SilentGuardProvider",
    "STATE_AVAILABLE",
    "STATE_OFFLINE",
    "STATE_UNAVAILABLE",
    "build_security_context_block",
    "default_provider",
    "get_security_context_summary",
    "get_security_context_text",
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


def get_security_context_text(
    provider: SecurityProvider | None = None,
) -> str:
    """Return a one-line, deterministic text summary of provider state.

    Examples::

        "No security provider is configured."
        "SilentGuard is unavailable."
        "SilentGuard read-only API is available."
        "SilentGuard reports 0 alerts and 0 blocked items."

    Falls back to the safe default provider when no argument is
    supplied. The text is **not** auto-injected anywhere; this helper
    exists so a future, explicit prompt site can opt in without
    having to re-derive the wording. Read-only.
    """
    active = provider if provider is not None else default_provider()
    summariser = getattr(active, "get_summary_text", None)
    if callable(summariser):
        try:
            text = summariser()
        except Exception:  # pragma: no cover — defensive
            text = ""
        if isinstance(text, str) and text:
            return text
    status = active.get_status()
    label = (status.service or "security provider").strip() or "security provider"
    label_pretty = label if label != "none" else "Security provider"
    if not status.available:
        if status.service == "none":
            return "No security provider is configured."
        return f"{label_pretty.capitalize()} is unavailable."
    return f"{label_pretty.capitalize()} is available."
