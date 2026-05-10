"""
Tiny, narrowly-scoped mitigation client for SilentGuard's local API.

SilentGuard owns detection and enforcement. Nova is the cognitive layer
and does **not** become the firewall. This module is the smallest
possible bridge that lets Nova:

  * read the current mitigation mode SilentGuard is in
    (``detection_only`` / ``ask_before_blocking`` / ``temporary_auto_block``);
  * after the user has explicitly confirmed, ask SilentGuard to enable
    temporary mitigation;
  * after the user has explicitly confirmed, ask SilentGuard to disable
    mitigation.

Boundaries (commitments, not aspirations):

  * **Local-first.** The client only ever talks to a loopback URL the
    operator has configured for the read-only API
    (``NOVA_SILENTGUARD_API_URL``). There is no auto-discovery, no
    remote fallback, and no second URL just for mitigation.
  * **Acknowledge-or-refuse.** Every POST carries the SilentGuard
    acknowledgement payload (``{"acknowledge": true}``). The client
    refuses to send a POST without it; the Nova endpoint above this
    layer refuses to call without the acknowledgement coming in
    explicitly from the user request.
  * **Strict timeouts.** Every call is bounded by a short timeout.
  * **Defensive parsing.** Non-JSON or unexpected payload shapes map
    to ``None`` (state) or a calm ``MitigationActionResult(ok=False,
    ...)``; raw transport errors never reach the caller.
  * **No shell, no subprocess, no firewall.** This module reads /
    writes nothing on disk, runs no commands, and touches no firewall
    rule directly. SilentGuard does the enforcement; Nova only asks.
  * **Separate from the read-only client.** ``silentguard_client.py``
    stays GET-only and is pinned by ``test_security_provider``'s
    forbidden-verb assertion. Mitigation lives here on purpose so that
    ``GET /status`` / ``GET /alerts`` paths cannot accidentally grow
    a write capability.

Scope is intentionally tiny. The full set of operations:

  * ``GET  /mitigation``               — read the current mode.
  * ``POST /mitigation/enable-temporary`` — opt-in temporary mitigation.
  * ``POST /mitigation/disable``       — turn it off.

Anything else (per-IP unblock, persistent rule writes, schedule
windows) is out of scope for this PR. Add a new method here only via
deliberate review — adding a method is the moment Nova grows a new
mitigation capability.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Short by design — the API is loopback-only.
DEFAULT_TIMEOUT_SECONDS = 3.0

# Mitigation paths Nova knows about. Adding to this list is a
# deliberate review.
PATH_MITIGATION = "/mitigation"
PATH_MITIGATION_ENABLE_TEMPORARY = "/mitigation/enable-temporary"
PATH_MITIGATION_DISABLE = "/mitigation/disable"

# Mitigation modes Nova surfaces. Anything else SilentGuard returns
# normalises to ``MODE_UNKNOWN`` so the UI never paints an unreviewed
# value. Order is *not* significant — these are flat enum values.
MODE_DETECTION_ONLY = "detection_only"
MODE_ASK_BEFORE_BLOCKING = "ask_before_blocking"
MODE_TEMPORARY_AUTO_BLOCK = "temporary_auto_block"
MODE_UNKNOWN = "unknown"

_RECOGNISED_MODES = frozenset({
    MODE_DETECTION_ONLY,
    MODE_ASK_BEFORE_BLOCKING,
    MODE_TEMPORARY_AUTO_BLOCK,
})

# Modes that count as "mitigation is currently active" for surfacing
# purposes. ``ask_before_blocking`` is *not* in this set: it is a
# detection mode that prompts before any block, so the user has not
# yet authorised an active block.
_ACTIVE_MODES = frozenset({MODE_TEMPORARY_AUTO_BLOCK})

# Acknowledgement payload SilentGuard requires on every mitigation
# write. Nova mirrors the same key at its own endpoint so a stray POST
# without the user's explicit acknowledgement is rejected at the Nova
# layer too.
ACKNOWLEDGE_KEY = "acknowledge"
ACKNOWLEDGE_PAYLOAD = {ACKNOWLEDGE_KEY: True}

# ISO-8601 timestamp shape SilentGuard is expected to return for the
# ``expires_at`` field. We only accept strings that match this pattern
# so the UI can render them without re-parsing into a ``datetime``.
_ISO_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(\.[0-9]+)?(Z|[+-][0-9]{2}:?[0-9]{2})?$"
)
_MAX_TIMESTAMP_LENGTH = 40

# Calm, user-safe message shown when SilentGuard is unreachable or
# returns an unexpected shape. The Nova endpoint surfaces this verbatim
# so a hostile log line never leaks into the UI.
_GENERIC_UNAVAILABLE_MESSAGE = "SilentGuard mitigation is currently unavailable."
_GENERIC_REFUSED_MESSAGE = "SilentGuard refused the mitigation request."


def _normalise_base_url(value: Optional[str]) -> str:
    """Trim whitespace and the trailing slash from a base URL."""
    if not value:
        return ""
    return str(value).strip().rstrip("/")


def _normalise_mode(raw: object) -> str:
    """Map an external mode string into Nova's small allow-list.

    Anything that is not one of the three documented modes (including
    ``None``, non-strings, mixed case, surrounding whitespace, or new
    SilentGuard modes Nova has not reviewed yet) maps to
    :data:`MODE_UNKNOWN`. The UI then renders it as a calm
    *"unknown"* state rather than echoing arbitrary text.
    """
    if not isinstance(raw, str):
        return MODE_UNKNOWN
    cleaned = raw.strip().lower()
    if cleaned in _RECOGNISED_MODES:
        return cleaned
    return MODE_UNKNOWN


def _normalise_timestamp(raw: object) -> Optional[str]:
    """Return a short ISO-8601 string, or ``None`` on bad input.

    Defence in depth: the value flows into Nova's response and may be
    rendered in the UI. Only well-formed ISO-8601 timestamps under a
    short length cap are accepted; everything else is dropped silently.
    """
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    if not cleaned or len(cleaned) > _MAX_TIMESTAMP_LENGTH:
        return None
    if not _ISO_TIMESTAMP_RE.match(cleaned):
        return None
    return cleaned


def _normalise_bool(raw: object, *, default: Optional[bool] = None) -> Optional[bool]:
    """Coerce a JSON-safe boolean, dropping numeric / string truthiness."""
    if isinstance(raw, bool):
        return raw
    return default


# ── Result dataclasses ──────────────────────────────────────────────


@dataclass(frozen=True)
class MitigationState:
    """Read-only snapshot of SilentGuard's mitigation mode.

    Fields are intentionally minimal: a normalised ``mode``, a derived
    ``active`` boolean (so the UI does not have to know the mode →
    active mapping), and an optional ``expires_at`` for temporary
    modes. SilentGuard's raw payload may include richer fields; this
    snapshot intentionally drops them so a future field cannot reach
    the UI without a deliberate review.
    """

    mode: str
    active: bool
    expires_at: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "active": self.active,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class MitigationActionResult:
    """Outcome of an enable / disable mitigation call.

    ``ok`` is the single source of truth for whether SilentGuard
    accepted the action. ``state`` carries the post-action mitigation
    snapshot when available, so the UI can repaint in a single
    round-trip. ``message`` is calm, user-safe text — never a raw
    exception or HTTP body — chosen by this module so the Nova
    endpoint above can surface it verbatim.
    """

    ok: bool
    state: Optional[MitigationState]
    message: str

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "state": self.state.as_dict() if self.state else None,
            "message": self.message,
        }


def _parse_state(payload: object) -> Optional[MitigationState]:
    """Coerce a SilentGuard ``/mitigation`` payload into a ``MitigationState``.

    Returns ``None`` only when the payload is so malformed that even a
    mode field cannot be extracted. An unrecognised mode coerces to
    :data:`MODE_UNKNOWN` so the caller still gets a snapshot it can
    render. Callers must therefore type-check the return value rather
    than treat ``None`` as "no mitigation."
    """
    if not isinstance(payload, dict):
        return None
    mode = _normalise_mode(payload.get("mode"))
    # ``active`` may be supplied explicitly by SilentGuard or derived.
    derived_active = mode in _ACTIVE_MODES
    explicit_active = _normalise_bool(payload.get("active"), default=None)
    if explicit_active is None:
        active = derived_active
    else:
        # Defence in depth: even if SilentGuard claims ``active=true``
        # we only honour it when paired with an active mode. This keeps
        # the UI from ever showing "active" for a mode Nova has not
        # vetted.
        active = explicit_active and derived_active
    expires_at = _normalise_timestamp(payload.get("expires_at"))
    return MitigationState(mode=mode, active=active, expires_at=expires_at)


# ── Client ──────────────────────────────────────────────────────────


class SilentGuardMitigationClient:
    """Narrow HTTP client for SilentGuard's mitigation endpoints.

    Construction is cheap; ``httpx.Client`` is created per request so a
    transient hang cannot wedge long-lived state. The client never
    raises into the caller — failures map to ``None`` (read) or a calm
    :class:`MitigationActionResult` with ``ok=False`` (write).

    The client refuses to make a POST without the SilentGuard
    acknowledgement payload. Callers cannot smuggle in a different
    body shape: the body is constructed inside this class.
    """

    def __init__(
        self,
        base_url: str,
        timeout_seconds: Optional[float] = None,
    ) -> None:
        self.base_url: str = _normalise_base_url(base_url)
        try:
            timeout = (
                float(timeout_seconds)
                if timeout_seconds is not None
                else DEFAULT_TIMEOUT_SECONDS
            )
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT_SECONDS
        if timeout <= 0:
            timeout = DEFAULT_TIMEOUT_SECONDS
        self.timeout_seconds: float = timeout

    def is_configured(self) -> bool:
        return bool(self.base_url)

    # ── Internal helpers ────────────────────────────────────────────

    def _open(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

    def _request_state(
        self, method: str, path: str,
    ) -> Optional[MitigationState]:
        """Issue one request that is expected to return a state payload."""
        if not self.is_configured():
            return None
        try:
            with self._open() as client:
                if method == "GET":
                    resp = client.get(path)
                elif method == "POST":
                    resp = client.post(path, json=ACKNOWLEDGE_PAYLOAD)
                else:  # pragma: no cover — defensive
                    return None
        except (httpx.HTTPError, OSError) as exc:
            logger.debug(
                "SilentGuard mitigation %s %s failed: %s", method, path, exc,
            )
            return None
        if not (200 <= resp.status_code < 300):
            logger.debug(
                "SilentGuard mitigation %s %s returned HTTP %s",
                method, path, resp.status_code,
            )
            return None
        try:
            payload = resp.json()
        except (ValueError, TypeError) as exc:
            logger.debug(
                "SilentGuard mitigation %s %s returned invalid JSON: %s",
                method, path, exc,
            )
            return None
        return _parse_state(payload)

    # ── Public surface (three methods, on purpose) ──────────────────

    def get_state(self) -> Optional[MitigationState]:
        """Return SilentGuard's current mitigation snapshot, or ``None``.

        ``None`` means the read-only API is not configured, the call
        failed, or SilentGuard returned a payload Nova could not parse
        at all. The provider above this layer turns ``None`` into a
        calm "unavailable" snapshot — callers must not treat ``None``
        as "no mitigation is configured."
        """
        return self._request_state("GET", PATH_MITIGATION)

    def enable_temporary(self) -> MitigationActionResult:
        """Ask SilentGuard to enable temporary mitigation.

        Sends the required acknowledgement payload. Never raises.
        Returns ``MitigationActionResult(ok=True, ...)`` on success
        with a fresh state snapshot, or ``ok=False`` with a calm,
        user-safe message on any failure.
        """
        state = self._request_state(
            "POST", PATH_MITIGATION_ENABLE_TEMPORARY,
        )
        if state is None:
            return MitigationActionResult(
                ok=False, state=None,
                message=_GENERIC_UNAVAILABLE_MESSAGE,
            )
        return MitigationActionResult(
            ok=True, state=state,
            message="Temporary mitigation enabled.",
        )

    def disable(self) -> MitigationActionResult:
        """Ask SilentGuard to disable mitigation.

        Sends the required acknowledgement payload. Never raises.
        Returns ``MitigationActionResult(ok=True, ...)`` on success
        with a fresh state snapshot, or ``ok=False`` with a calm,
        user-safe message on any failure.
        """
        state = self._request_state("POST", PATH_MITIGATION_DISABLE)
        if state is None:
            return MitigationActionResult(
                ok=False, state=None,
                message=_GENERIC_UNAVAILABLE_MESSAGE,
            )
        return MitigationActionResult(
            ok=True, state=state,
            message="Mitigation disabled.",
        )


__all__ = [
    "ACKNOWLEDGE_KEY",
    "ACKNOWLEDGE_PAYLOAD",
    "DEFAULT_TIMEOUT_SECONDS",
    "MODE_ASK_BEFORE_BLOCKING",
    "MODE_DETECTION_ONLY",
    "MODE_TEMPORARY_AUTO_BLOCK",
    "MODE_UNKNOWN",
    "MitigationActionResult",
    "MitigationState",
    "PATH_MITIGATION",
    "PATH_MITIGATION_DISABLE",
    "PATH_MITIGATION_ENABLE_TEMPORARY",
    "SilentGuardMitigationClient",
]
