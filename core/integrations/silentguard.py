"""
Gated bridge to SilentGuard's local memory file.

SilentGuard is an external network-monitoring tool. It writes observed
connections to ``~/.silentguard_memory.json`` (path overridable via the
``NOVA_SILENTGUARD_PATH`` env var). This module exposes a per-user
opt-in switch on top of the existing read-only parser in
``core.security_feed``.

Boundaries (enforced):
  * read-only file access — no writes, no deletions.
  * no subprocess, no socket, no DNS lookup, no firewall action.
  * no root or elevated-privilege call.
  * if the user has not enabled the integration, every public function
    short-circuits to "disabled" without touching the disk.

If SilentGuard is enabled but the file is missing or malformed, the
status reports ``"not_found"`` and the read helpers return empty
results — they never raise.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core import security_feed
from core.settings import get_user_setting

logger = logging.getLogger(__name__)

NAME = "silentguard"

STATE_DISABLED = "disabled"
STATE_CONNECTED = "connected"
STATE_NOT_FOUND = "not_found"


@dataclass(frozen=True)
class IntegrationStatus:
    """Snapshot of an integration's availability for one user."""

    name: str
    enabled: bool
    state: str
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "state": self.state,
            "detail": self.detail,
        }


def _resolved_path() -> Path:
    """Resolve the SilentGuard feed path the same way ``security_feed`` does."""
    override = os.environ.get("NOVA_SILENTGUARD_PATH")
    if override:
        return Path(override).expanduser()
    return security_feed.DEFAULT_FEED_PATH


def is_enabled(user_id: int) -> bool:
    """True only when the user has explicitly turned the integration on."""
    return get_user_setting(user_id, "silentguard_enabled", "false") == "true"


def status(user_id: int) -> IntegrationStatus:
    """
    Report whether the integration is on, and whether the feed file is
    present. Never raises: a missing or unreadable file maps to
    ``not_found``, not an exception.
    """
    enabled = is_enabled(user_id)
    if not enabled:
        return IntegrationStatus(
            name=NAME, enabled=False, state=STATE_DISABLED,
            detail="Turned off in Settings.",
        )
    path = _resolved_path()
    try:
        if not path.is_file():
            return IntegrationStatus(
                name=NAME, enabled=True, state=STATE_NOT_FOUND,
                detail=f"No file at {path}",
            )
    except OSError as e:
        logger.debug("SilentGuard path probe failed: %s", e)
        return IntegrationStatus(
            name=NAME, enabled=True, state=STATE_NOT_FOUND,
            detail="Feed path is not accessible.",
        )
    return IntegrationStatus(
        name=NAME, enabled=True, state=STATE_CONNECTED,
        detail=str(path),
    )


def recent_events(user_id: int, limit: int = 50) -> list[security_feed.SecurityEvent]:
    """
    Return up to ``limit`` recent events from the SilentGuard feed.

    Empty list when the integration is off, the file is missing, or the
    payload cannot be parsed. Read-only.
    """
    if not is_enabled(user_id):
        return []
    return security_feed.get_recent_security_events(limit=limit)


def recent_events_summary(user_id: int, limit: int = 50) -> Optional[str]:
    """
    Convenience wrapper used by the chat layer.

    Returns a formatted summary string suitable for prompt injection, or
    ``None`` when the integration is off / there is nothing to report.
    Always read-only.
    """
    if not is_enabled(user_id):
        return None
    events = security_feed.get_recent_security_events(limit=limit)
    if not events:
        return None
    return security_feed.format_security_summary(
        security_feed.summarize_events(events)
    )
