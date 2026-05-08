"""
Read-only security context block for prompt injection (Phase 1).

Nova's chat layer needs a small, deterministic way to tell the model
*"is SilentGuard configured here, is it reachable, and what is its
read-only summary?"* — without bloating every prompt and without
inviting Nova to take action.

This module is the single place that wording lives. It produces a
short, bullet-shaped block:

    Security context:
    - SilentGuard integration: connected in read-only mode.
    - Current summary: 0 alerts, 0 blocked items, 0 trusted items,
      0 active connections.
    - Allowed behavior: explain and summarize only; do not perform
      firewall or rule actions.

The wording is intentionally calm. It never invents numbers it does
not have. It never suggests an action. It always restates that Nova
is read-only.

Boundaries enforced here (commitments, not aspirations):

  * **Read-only.** The builder calls only the provider's read-only
    surface (``get_status`` and the optional ``get_summary_counts``).
    No writes, no shell calls, no firewall actions.
  * **Deterministic.** No LLM in the loop. The sentences are fixed;
    only the four counts vary.
  * **Graceful.** Every error path returns a short, calm block. The
    builder never raises into the chat path.
  * **Minimal by default.** When no provider is configured the block
    is three short lines, so unconfigured Nova installs pay near-zero
    token cost.
  * **No raw payload leakage.** Only counts and a fixed wording set
    enter the block. Process names, IPs, exception messages, and
    timestamps are *not* surfaced here — they belong to the existing
    ``core.security_feed`` summariser when (and only when) the user
    asks a security-shaped question.
"""

from __future__ import annotations

import logging
from typing import Optional

from core.security.provider import (
    NullSecurityProvider,
    STATE_AVAILABLE,
    STATE_OFFLINE,
    STATE_UNAVAILABLE,
    SecurityProvider,
)

logger = logging.getLogger(__name__)

_HEADER = "Security context:"
_BEHAVIOR_LINE = (
    "Allowed behavior: explain and summarize only; do not perform "
    "firewall or rule actions."
)

# The four count keys the block surfaces. Kept as a tuple so an
# implementation that returns a dict with extra keys does not silently
# leak them into the prompt.
_REQUIRED_COUNT_KEYS = ("alerts", "blocked", "trusted", "connections")


def _format_block(*lines: str) -> str:
    """Join ``Security context:`` + bullets into one block."""
    return _HEADER + "\n" + "\n".join(f"- {line}" for line in lines)


def _safe_get_status(provider):
    """Call ``provider.get_status``; return ``None`` if it raises."""
    try:
        return provider.get_status()
    except Exception:  # pragma: no cover — providers must not raise
        logger.debug(
            "security provider get_status raised; treating as unavailable",
            exc_info=True,
        )
        return None


def _safe_counts(provider) -> Optional[dict]:
    """Return read-only counts, or ``None`` when unavailable.

    A provider may optionally expose ``get_summary_counts`` returning a
    dict with ``alerts`` / ``blocked`` / ``trusted`` / ``connections``
    integer fields. Missing method, exception, wrong shape, or
    negative numbers all map to ``None`` — the caller falls back to
    the count-less wording.
    """
    getter = getattr(provider, "get_summary_counts", None)
    if not callable(getter):
        return None
    try:
        counts = getter()
    except Exception:  # pragma: no cover — defensive
        logger.debug(
            "security provider get_summary_counts raised", exc_info=True,
        )
        return None
    if not isinstance(counts, dict):
        return None
    if not all(key in counts for key in _REQUIRED_COUNT_KEYS):
        return None
    if not all(
        isinstance(counts[key], int) and counts[key] >= 0
        for key in _REQUIRED_COUNT_KEYS
    ):
        return None
    return {key: counts[key] for key in _REQUIRED_COUNT_KEYS}


def _is_null_provider(provider, name: str) -> bool:
    """Return True for the safe-default null provider.

    Detected either by isinstance check (preferred) or by the stable
    ``name`` sentinel; both are documented in
    ``core.security.provider``.
    """
    return isinstance(provider, NullSecurityProvider) or name in ("", "none")


def build_security_context_block(
    provider: Optional[SecurityProvider] = None,
) -> str:
    """Return a small, deterministic security-context block.

    Falls back to ``core.security.default_provider()`` (currently the
    safe null provider) when no argument is supplied. Always returns a
    non-empty string — callers append it like
    ``format_time_context()``.

    Outcomes:

      * No provider / null provider     → "SilentGuard integration:
                                          not configured."
      * SilentGuard, ``state=unavailable`` → "SilentGuard integration:
                                              not configured."
      * SilentGuard, ``state=offline``     → "SilentGuard integration:
                                              read-only API is unavailable."
      * SilentGuard, ``state=available``   → "SilentGuard integration:
                                              connected in read-only
                                              mode." (+ counts when the
                                              provider exposes them)

    Read-only. Never raises. Never includes raw payloads, exceptions,
    process names, IPs, or timestamps.
    """
    if provider is None:
        # Local import to avoid a hard cycle with ``core.security.__init__``,
        # which imports this module to re-export the helper.
        from core.security import default_provider
        provider = default_provider()

    name = (getattr(provider, "name", "") or "").strip().lower()

    if _is_null_provider(provider, name):
        return _format_block(
            "SilentGuard integration: not configured.",
            _BEHAVIOR_LINE,
        )

    status = _safe_get_status(provider)
    if status is None:
        # Provider misbehaved; treat as unavailable. We do not surface
        # the underlying error in the prompt.
        return _format_block(
            "SilentGuard integration: read-only API is unavailable.",
            _BEHAVIOR_LINE,
        )

    # ``status.available`` is the source of truth for the available path;
    # the state strings split the unavailable path into "not configured"
    # (``unavailable`` — nothing to talk to) and "read-only API is
    # unavailable" (``offline`` — configured but unreachable).
    if not status.available:
        if status.state == STATE_OFFLINE:
            return _format_block(
                "SilentGuard integration: read-only API is unavailable.",
                _BEHAVIOR_LINE,
            )
        # STATE_UNAVAILABLE or any other unavailable state.
        return _format_block(
            "SilentGuard integration: not configured.",
            _BEHAVIOR_LINE,
        )

    # Available. Try to enrich with counts when the provider can supply
    # them; fall back to the count-less wording otherwise.
    counts = _safe_counts(provider)
    if counts is None:
        return _format_block(
            "SilentGuard integration: connected in read-only mode.",
            _BEHAVIOR_LINE,
        )

    return _format_block(
        "SilentGuard integration: connected in read-only mode.",
        (
            f"Current summary: {counts['alerts']} alerts, "
            f"{counts['blocked']} blocked items, "
            f"{counts['trusted']} trusted items, "
            f"{counts['connections']} active connections."
        ),
        _BEHAVIOR_LINE,
    )


# Re-exports so the unused-import lints stay happy and so callers can
# do ``from core.security.context import STATE_*`` if they need to.
__all__ = [
    "build_security_context_block",
    "STATE_AVAILABLE",
    "STATE_OFFLINE",
    "STATE_UNAVAILABLE",
]
