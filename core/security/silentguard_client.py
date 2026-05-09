"""
Tiny read-only HTTP client for SilentGuard's local API.

SilentGuard, the external network-monitoring tool Nova integrates with,
historically persisted state to a JSON file under the user's home
directory. Recent SilentGuard builds *may* additionally expose a
loopback-only read API (``GET /status`` and friends). This module is
the minimum-viable adapter to that surface.

Boundaries (commitments, not aspirations):

  * **Read-only.** The client only ever issues ``GET`` requests to a
    fixed, hard-coded list of paths. There is no write helper, no PUT,
    no POST, no DELETE.
  * **Local-first.** The base URL must come from configuration; there
    is no auto-discovery and no remote fallback. Nothing is contacted
    until an operator sets ``NOVA_SILENTGUARD_API_URL``.
  * **Strict timeouts.** Every call is bounded by a short timeout so
    a hung SilentGuard cannot wedge Nova.
  * **Defensive parsing.** Non-JSON or unexpected payload shapes are
    discarded silently — callers see ``None`` (status) or ``[]``
    (list endpoints) rather than an exception.
  * **Calm fallback.** Any transport, decoding, or HTTP-status error
    maps to the absent path. The provider layer above this client
    presents that as a calm ``available=False`` ``SecurityStatus``.

The client is intentionally small. Nothing in it imports the
SilentGuard project, and nothing in it depends on SilentGuard being
installed. If the API is missing, the client just returns ``None`` /
``[]`` and the rest of Nova carries on.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Short by design: the API is loopback-only, so anything taking longer
# than a couple of seconds is almost certainly a hang.
DEFAULT_TIMEOUT_SECONDS = 2.0

# The fixed read-only path set. Adding a path here is a deliberate
# review — callers cannot supply arbitrary endpoints.
PATH_STATUS = "/status"
PATH_CONNECTIONS = "/connections"
PATH_CONNECTIONS_SUMMARY = "/connections/summary"
PATH_BLOCKED = "/blocked"
PATH_TRUSTED = "/trusted"
PATH_ALERTS = "/alerts"


def _normalise_base_url(value: Optional[str]) -> str:
    """Trim whitespace and the trailing slash from a base URL."""
    if not value:
        return ""
    return str(value).strip().rstrip("/")


class SilentGuardClient:
    """Read-only HTTP client for SilentGuard's optional local API.

    The client speaks JSON over HTTP to a base URL the operator
    configures (typically ``http://127.0.0.1:<port>``). It exposes one
    method per safe endpoint and never raises into the caller — failures
    map to ``None`` (for ``/status``) or ``[]`` (for list endpoints).

    Construction is cheap; the underlying ``httpx.Client`` is created
    per request so transient hangs cannot poison long-lived state. The
    request volume is low (status probes, manual queries) so the
    per-call cost is fine.
    """

    def __init__(
        self,
        base_url: str,
        timeout_seconds: Optional[float] = None,
    ) -> None:
        self.base_url: str = _normalise_base_url(base_url)
        try:
            timeout = float(timeout_seconds) if timeout_seconds is not None else DEFAULT_TIMEOUT_SECONDS
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT_SECONDS
        if timeout <= 0:
            timeout = DEFAULT_TIMEOUT_SECONDS
        self.timeout_seconds: float = timeout

    def is_configured(self) -> bool:
        """``True`` only when a non-empty base URL was supplied."""
        return bool(self.base_url)

    # ── Internal HTTP helper ────────────────────────────────────────

    def _open(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            headers={"Accept": "application/json"},
        )

    def _get_json(self, path: str) -> Optional[object]:
        """Issue one ``GET`` and decode JSON. Never raises.

        Returns the decoded payload, or ``None`` on any failure
        (transport error, non-2xx status, malformed JSON). Callers must
        type-check the return value before use.
        """
        if not self.is_configured():
            return None
        try:
            with self._open() as client:
                resp = client.get(path)
        except (httpx.HTTPError, OSError) as exc:
            logger.debug("SilentGuard API GET %s failed: %s", path, exc)
            return None
        if not (200 <= resp.status_code < 300):
            logger.debug(
                "SilentGuard API GET %s returned HTTP %s",
                path, resp.status_code,
            )
            return None
        try:
            return resp.json()
        except (ValueError, TypeError) as exc:
            logger.debug(
                "SilentGuard API GET %s returned invalid JSON: %s",
                path, exc,
            )
            return None

    def _list_endpoint(self, path: str) -> list[dict]:
        """Coerce a list-shaped endpoint into ``list[dict]``.

        Tolerates either a top-level list or a wrapper object with one
        of the standard keys (``items`` / ``entries`` / ``results``).
        Anything else maps to ``[]``.
        """
        data = self._get_json(path)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("items", "entries", "results"):
                value = data.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        return []

    # ── Public read-only surface ────────────────────────────────────

    def get_status(self) -> Optional[dict]:
        """Return the SilentGuard ``/status`` payload, or ``None``."""
        data = self._get_json(PATH_STATUS)
        return data if isinstance(data, dict) else None

    def get_connections(self) -> list[dict]:
        """Return the SilentGuard ``/connections`` list (may be empty)."""
        return self._list_endpoint(PATH_CONNECTIONS)

    def get_connections_summary(self) -> Optional[dict]:
        """Return the SilentGuard ``/connections/summary`` payload, or ``None``.

        Newer SilentGuard builds expose a compact aggregated view of
        the live connection set (totals, local/known/unknown breakdown,
        top processes / remote hosts). Older builds simply do not
        serve this path; the client treats a missing endpoint exactly
        like any other failure and returns ``None`` rather than
        raising. Callers must type-check the return value before use.
        """
        data = self._get_json(PATH_CONNECTIONS_SUMMARY)
        return data if isinstance(data, dict) else None

    def get_blocked(self) -> list[dict]:
        """Return the SilentGuard ``/blocked`` list (may be empty)."""
        return self._list_endpoint(PATH_BLOCKED)

    def get_trusted(self) -> list[dict]:
        """Return the SilentGuard ``/trusted`` list (may be empty)."""
        return self._list_endpoint(PATH_TRUSTED)

    def get_alerts(self) -> list[dict]:
        """Return the SilentGuard ``/alerts`` list (may be empty)."""
        return self._list_endpoint(PATH_ALERTS)
