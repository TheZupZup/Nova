"""
SilentGuardProvider — read-only foundation (Phase 1).

This is the smallest possible adapter for SilentGuard, the external
network-monitoring tool Nova integrates with. Phase 1 only answers
one question: *is SilentGuard reachable on this host?* Reading
events, listing connections, surfacing alerts, or any other slice of
SilentGuard's state is intentionally **out of scope** for this PR
and lives in follow-up work. See
``docs/silentguard-integration-roadmap.md`` for the full plan and
its non-goals.

Boundaries (commitments, not aspirations):

  * read-only — never writes, never spawns processes, never opens
    sockets, never modifies firewall rules.
  * graceful — every ``get_status`` call returns a ``SecurityStatus``,
    even on error. A missing file, an unreadable path, or an
    OS-level probe failure all map to ``available=False``.
  * decoupled — does not import the per-user integration or the file
    parser. Safe to import on hosts that have never installed
    SilentGuard.

The provider is structured so a future phase can swap the on-disk
probe for a loopback HTTP probe (if SilentGuard ever ships a local
read API) without touching callers above this layer.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from core.security.provider import (
    STATE_AVAILABLE,
    STATE_OFFLINE,
    STATE_UNAVAILABLE,
    SecurityStatus,
    now_iso,
)

logger = logging.getLogger(__name__)

NAME = "silentguard"

# Default location SilentGuard writes to, mirroring
# ``core.security_feed.DEFAULT_FEED_PATH``. Kept as a module-level
# constant so the path is easy to override in tests via the
# ``feed_path`` constructor argument or the
# ``NOVA_SILENTGUARD_PATH`` env var.
DEFAULT_SILENTGUARD_PATH = Path.home() / ".silentguard_memory.json"


class SilentGuardProvider:
    """Read-only security provider that probes SilentGuard's presence.

    Phase 1 checks only that the on-disk memory file is reachable.
    The provider returns ``available=True`` when the file exists and
    is statable, and a calm ``available=False`` snapshot otherwise.
    No file content is read here; parsing stays in
    ``core.security_feed`` so this provider does not regress the
    existing behaviour.
    """

    name: str = NAME

    def __init__(self, feed_path: Optional[os.PathLike] = None) -> None:
        self._explicit_path: Optional[Path] = (
            Path(feed_path) if feed_path is not None else None
        )

    def _resolved_path(self) -> Path:
        """Resolve the SilentGuard feed path, honouring the env override."""
        if self._explicit_path is not None:
            return self._explicit_path
        override = os.environ.get("NOVA_SILENTGUARD_PATH")
        if override:
            return Path(override).expanduser()
        return DEFAULT_SILENTGUARD_PATH

    def get_status(self) -> SecurityStatus:
        """Probe whether SilentGuard's local state is reachable.

        Outcomes:
          * file present and statable → ``available=True``,
            ``state=available``.
          * file simply missing       → ``available=False``,
            ``state=unavailable``.
          * probe raised ``OSError``  → ``available=False``,
            ``state=offline``.

        Never raises.
        """
        path = self._resolved_path()
        timestamp = now_iso()
        try:
            present = path.is_file()
        except OSError as exc:
            logger.debug("SilentGuard path probe failed: %s", exc)
            return SecurityStatus(
                available=False,
                service=self.name,
                state=STATE_OFFLINE,
                message="SilentGuard memory file is not accessible.",
                timestamp=timestamp,
            )
        if not present:
            return SecurityStatus(
                available=False,
                service=self.name,
                state=STATE_UNAVAILABLE,
                message=f"SilentGuard memory file not found at {path}.",
                timestamp=timestamp,
            )
        return SecurityStatus(
            available=True,
            service=self.name,
            state=STATE_AVAILABLE,
            message=f"SilentGuard memory file detected at {path}.",
            timestamp=timestamp,
        )
