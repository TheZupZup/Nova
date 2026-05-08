"""
Generic security provider foundation (Phase 1 — read-only).

Nova's cognitive layer can describe security telemetry produced by an
external monitoring tool, but Nova does not enforce or act. To keep
that boundary explicit, the cognitive layer talks to security tools
through a small ``SecurityProvider`` Protocol that exposes one read
method: ``get_status``. Every implementation reports whether the
underlying tool is reachable from this host, and never raises on the
absent path.

Boundaries enforced by this module (and by every implementation in
this package):

  * read-only — no writes, no deletions, no subprocess, no socket,
    no DNS, no firewall action.
  * graceful absence — a missing tool, an unreadable file, or any
    transport error maps to a ``SecurityStatus`` with
    ``available=False`` and an explanatory message.
  * decoupled — providers must not import anything that requires the
    backing tool to be installed. Importing the package on a host
    that has never heard of SilentGuard must remain safe.

This module deliberately ships only the foundation: status snapshots
and the provider Protocol. Higher layers (the existing per-user
``core.integrations.silentguard`` gate and the chat-side
``core.security_feed`` summariser) keep their current
responsibilities. See ``docs/silentguard-integration-roadmap.md`` for
the broader design and the explicit non-goals.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Protocol, runtime_checkable


# Provider availability states. Kept as plain strings so the snapshot
# round-trips through JSON without bespoke encoders.
STATE_AVAILABLE = "available"      # tool is configured and reachable
STATE_UNAVAILABLE = "unavailable"  # tool is not installed / not configured
STATE_OFFLINE = "offline"          # tool is configured but not reachable


@dataclass(frozen=True)
class SecurityStatus:
    """Read-only snapshot of one security provider's availability.

    The shape is intentionally flat (``available`` / ``service`` /
    ``state`` / ``message`` / ``timestamp``) so it can be surfaced as
    JSON, logged, or threaded into a future prompt summary without a
    schema-specific renderer.
    """

    available: bool
    service: str
    state: str
    message: str = ""
    timestamp: Optional[str] = None  # ISO-8601 UTC, when known

    def as_dict(self) -> dict:
        return {
            "available": self.available,
            "service": self.service,
            "state": self.state,
            "message": self.message,
            "timestamp": self.timestamp,
        }


def now_iso() -> str:
    """ISO-8601 UTC timestamp for status snapshots."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@runtime_checkable
class SecurityProvider(Protocol):
    """Read-only contract every security provider must satisfy.

    Implementations expose a stable ``name`` and a ``get_status``
    method. ``get_status`` must never raise: a missing tool, an
    unreadable path, a network failure, or a config error all map to a
    ``SecurityStatus`` with ``available=False`` and a short
    explanatory message.
    """

    name: str

    def get_status(self) -> SecurityStatus:
        """Return a snapshot of provider availability. Never raises."""
        ...


class NullSecurityProvider:
    """Default-safe provider that always reports unavailable.

    Used when no security provider has been configured, so Nova
    continues to work normally — the cognitive layer sees a calm
    ``available=False`` state instead of an exception path.
    """

    name: str = "none"

    def get_status(self) -> SecurityStatus:
        return SecurityStatus(
            available=False,
            service=self.name,
            state=STATE_UNAVAILABLE,
            message="No security provider is configured.",
            timestamp=now_iso(),
        )
