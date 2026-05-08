"""
SilentGuardProvider — read-only foundation (Phase 1).

This is the smallest possible adapter for SilentGuard, the external
network-monitoring tool Nova integrates with. Phase 1 only answers
one question: *is SilentGuard reachable on this host?* Reading
events, listing connections, surfacing alerts, or any other slice of
SilentGuard's state is intentionally **out of scope** for this
foundation and lives in follow-up work. See
``docs/silentguard-integration-roadmap.md`` for the full plan and
its non-goals.

The provider supports two transports:

  * **File** (default). Probe the on-disk memory file at
    ``~/.silentguard_memory.json`` (overridable via
    ``NOVA_SILENTGUARD_PATH``).
  * **HTTP** (opt-in). When ``NOVA_SILENTGUARD_API_URL`` is set — or
    an explicit ``api_url`` is passed — probe the SilentGuard
    loopback ``/status`` endpoint via :class:`SilentGuardClient`.

Both transports share the same ``SecurityStatus`` shape, so callers
above this layer never have to care which one ran. If both are
configured, the HTTP probe wins; if it fails, the provider reports
``offline`` rather than silently re-falling-back to the file path —
operators who configure an API URL want to know when it goes away.

Boundaries (commitments, not aspirations):

  * read-only — never writes, never spawns processes, never opens
    raw sockets, never modifies firewall rules. The HTTP transport
    only issues ``GET`` requests against a fixed path list.
  * graceful — every ``get_status`` call returns a ``SecurityStatus``,
    even on error. A missing file, an unreadable path, an HTTP timeout,
    or a malformed JSON body all map to ``available=False``.
  * decoupled — does not import the per-user integration or the file
    parser. Safe to import on hosts that have never installed
    SilentGuard.
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
from core.security.silentguard_client import (
    DEFAULT_TIMEOUT_SECONDS,
    SilentGuardClient,
)

logger = logging.getLogger(__name__)

NAME = "silentguard"

# Default location SilentGuard writes to, mirroring
# ``core.security_feed.DEFAULT_FEED_PATH``. Kept as a module-level
# constant so the path is easy to override in tests via the
# ``feed_path`` constructor argument or the
# ``NOVA_SILENTGUARD_PATH`` env var.
DEFAULT_SILENTGUARD_PATH = Path.home() / ".silentguard_memory.json"

# Env vars / config keys the provider honours for the optional HTTP
# transport. Both default to "off" — until an operator sets the URL,
# the provider behaves exactly like the previous file-only foundation.
ENV_API_URL = "NOVA_SILENTGUARD_API_URL"
ENV_API_TIMEOUT = "NOVA_SILENTGUARD_API_TIMEOUT_SECONDS"


def _config_default_api_url() -> str:
    """Read the configured base URL lazily to keep imports cheap."""
    try:
        from config import NOVA_SILENTGUARD_API_URL  # local import: avoid cycles
    except Exception:  # pragma: no cover — config import is best-effort
        return ""
    return (NOVA_SILENTGUARD_API_URL or "").strip().rstrip("/")


def _config_default_timeout() -> float:
    try:
        from config import NOVA_SILENTGUARD_API_TIMEOUT_SECONDS
    except Exception:  # pragma: no cover
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(NOVA_SILENTGUARD_API_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_TIMEOUT_SECONDS


class SilentGuardProvider:
    """Read-only security provider that probes SilentGuard's presence.

    The provider supports two transports, picked at call time:

      * If a ``SilentGuardClient`` is supplied (or an API base URL is
        configured), the provider probes SilentGuard's loopback
        ``/status`` endpoint over HTTP.
      * Otherwise, it falls back to the historical file probe — checks
        only that the on-disk memory file is reachable, with no
        content read.

    Either transport returns a ``SecurityStatus`` with ``available``,
    ``state``, and a short ``message``. Errors map to calm
    ``available=False`` snapshots, never to exceptions.
    """

    name: str = NAME

    def __init__(
        self,
        feed_path: Optional[os.PathLike] = None,
        *,
        api_url: Optional[str] = None,
        api_timeout_seconds: Optional[float] = None,
        client: Optional[SilentGuardClient] = None,
    ) -> None:
        self._explicit_path: Optional[Path] = (
            Path(feed_path) if feed_path is not None else None
        )
        # `api_url=None` → look up env / config; ``""`` → explicitly off.
        self._explicit_api_url: Optional[str] = api_url
        self._explicit_timeout: Optional[float] = api_timeout_seconds
        self._explicit_client: Optional[SilentGuardClient] = client

    # ── Path / API resolution ───────────────────────────────────────

    def _resolved_path(self) -> Path:
        """Resolve the SilentGuard feed path, honouring the env override."""
        if self._explicit_path is not None:
            return self._explicit_path
        override = os.environ.get("NOVA_SILENTGUARD_PATH")
        if override:
            return Path(override).expanduser()
        return DEFAULT_SILENTGUARD_PATH

    def _resolved_api_url(self) -> str:
        """Resolve the API base URL: explicit > env > config."""
        if self._explicit_api_url is not None:
            return _normalise_url(self._explicit_api_url)
        env_value = os.environ.get(ENV_API_URL)
        if env_value is not None:
            return _normalise_url(env_value)
        return _config_default_api_url()

    def _resolved_timeout(self) -> float:
        if self._explicit_timeout is not None:
            try:
                value = float(self._explicit_timeout)
            except (TypeError, ValueError):
                return DEFAULT_TIMEOUT_SECONDS
            return value if value > 0 else DEFAULT_TIMEOUT_SECONDS
        env_value = os.environ.get(ENV_API_TIMEOUT)
        if env_value is not None:
            try:
                value = float(env_value)
            except ValueError:
                return DEFAULT_TIMEOUT_SECONDS
            return value if value > 0 else DEFAULT_TIMEOUT_SECONDS
        return _config_default_timeout()

    def _resolved_client(self) -> Optional[SilentGuardClient]:
        """Return a ready client, or ``None`` if HTTP probing is off."""
        if self._explicit_client is not None:
            return self._explicit_client
        url = self._resolved_api_url()
        if not url:
            return None
        return SilentGuardClient(
            base_url=url,
            timeout_seconds=self._resolved_timeout(),
        )

    # ── Status probing ──────────────────────────────────────────────

    def _api_status_snapshot(
        self, client: SilentGuardClient,
    ) -> SecurityStatus:
        """Probe ``/status`` over HTTP. Never raises."""
        timestamp = now_iso()
        payload = client.get_status()
        if payload is None:
            return SecurityStatus(
                available=False,
                service=self.name,
                state=STATE_OFFLINE,
                message=(
                    f"SilentGuard read-only API at {client.base_url} "
                    "is not reachable."
                ),
                timestamp=timestamp,
            )
        return SecurityStatus(
            available=True,
            service=self.name,
            state=STATE_AVAILABLE,
            message=(
                f"SilentGuard read-only API is available at {client.base_url}."
            ),
            timestamp=timestamp,
        )

    def _file_status_snapshot(self) -> SecurityStatus:
        """Probe the on-disk memory file. Never raises."""
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

    def get_status(self) -> SecurityStatus:
        """Probe whether SilentGuard's local state is reachable.

        Picks the configured transport:
          * HTTP if a client / api_url is set,
          * file otherwise.

        Outcomes:
          * file present / API reachable     → ``available=True``,
                                               ``state=available``.
          * file simply missing              → ``available=False``,
                                               ``state=unavailable``.
          * file probe raised ``OSError``    → ``available=False``,
                                               ``state=offline``.
          * API unreachable / malformed JSON → ``available=False``,
                                               ``state=offline``.

        Never raises.
        """
        client = self._resolved_client()
        if client is not None:
            return self._api_status_snapshot(client)
        return self._file_status_snapshot()

    # ── Optional summary text ───────────────────────────────────────

    def get_summary_text(self) -> str:
        """Return a one-line, deterministic text summary.

        Examples::

            "SilentGuard is unavailable."
            "SilentGuard read-only API is available."
            "SilentGuard reports 0 alerts and 0 blocked items."

        Read-only. The summary is **not** auto-injected anywhere; this
        helper exists so a future, explicit prompt site can opt in
        without having to re-derive the wording.
        """
        status = self.get_status()
        if not status.available:
            return "SilentGuard is unavailable."
        client = self._resolved_client()
        if client is None:
            return "SilentGuard read-only state is available."
        try:
            alerts = client.get_alerts()
            blocked = client.get_blocked()
        except Exception:  # pragma: no cover — defensive belt-and-braces
            logger.debug("SilentGuard summary enrichment failed", exc_info=True)
            return "SilentGuard read-only API is available."
        if isinstance(alerts, list) and isinstance(blocked, list):
            return (
                f"SilentGuard reports {len(alerts)} alerts "
                f"and {len(blocked)} blocked items."
            )
        return "SilentGuard read-only API is available."

    # ── Optional structured counts ──────────────────────────────────

    def get_summary_counts(self) -> Optional[dict]:
        """Return read-only counts, or ``None`` when unavailable.

        When the HTTP transport is configured *and* SilentGuard is
        reachable, returns::

            {"alerts": int, "blocked": int, "trusted": int, "connections": int}

        Returns ``None`` in every other case:

          * provider unavailable (file missing, API offline, …),
          * no API client configured (file-only fallback),
          * any read returned a non-list payload,
          * any read raised (defensive — the client itself does not
            raise, but a misbehaving substitute might).

        Used by the prompt-injected security context block in
        ``core.security.context``. Read-only — only ``GET`` calls are
        ever issued, only against the fixed read-only path list.
        """
        status = self.get_status()
        if not status.available:
            return None
        client = self._resolved_client()
        if client is None:
            return None
        try:
            alerts = client.get_alerts()
            blocked = client.get_blocked()
            trusted = client.get_trusted()
            connections = client.get_connections()
        except Exception:  # pragma: no cover — defensive belt-and-braces
            logger.debug(
                "SilentGuard summary count enrichment failed", exc_info=True,
            )
            return None
        if not all(
            isinstance(value, list)
            for value in (alerts, blocked, trusted, connections)
        ):
            return None
        return {
            "alerts": len(alerts),
            "blocked": len(blocked),
            "trusted": len(trusted),
            "connections": len(connections),
        }


def _normalise_url(value: str) -> str:
    """Trim whitespace and a trailing slash from an API base URL."""
    if not value:
        return ""
    return str(value).strip().rstrip("/")
