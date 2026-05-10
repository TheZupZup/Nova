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
import re
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
from core.security.silentguard_mitigation import (
    MitigationActionResult,
    MitigationState,
    SilentGuardMitigationClient,
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


# ── Connection summary normalisation helpers ────────────────────────
#
# The richer ``/connections/summary`` endpoint yields strings (process
# names, remote hosts) on top of the simple integer counts already
# surfaced via :meth:`SilentGuardProvider.get_summary_counts`. Strings
# from an external source must be sanitised before they enter Nova's
# prompt or UI: the §10.6 deny-list rule in
# ``docs/silentguard-integration-roadmap.md`` calls out exactly this
# concern. The helpers below are the single place that work happens —
# the context builder and the web layer just trust the provider's
# return shape.

# Keep the rendered summary line short and reviewable. SilentGuard may
# legitimately return more entries; Nova never wants to splat an
# unbounded list into a prompt or a Settings card.
_MAX_TOP_ENTRIES = 5

# Cap on the length of one process / host string. Real SilentGuard
# values are well under this; the cap is belt-and-braces against a
# hostile log line being smuggled through.
_MAX_LABEL_LENGTH = 32

# Whitelist of characters allowed in a sanitised label. Mirrors the
# §10.6 character set: alnum + a small punctuation set that covers
# realistic process names (``my-tool``, ``python3.11``) and IP /
# hostname forms (``1.2.3.4``, ``api.example.com``, ``[::1]``).
_LABEL_CHARS = re.compile(r"[^A-Za-z0-9._:/\-]")


def _sanitise_label(value: object) -> Optional[str]:
    """Return a short, prompt-safe string, or ``None`` on bad input.

    Strips every character outside the §10.6 whitelist, trims leading
    / trailing punctuation, caps length, and rejects anything that
    reduces to empty. Non-strings always map to ``None``.
    """
    if not isinstance(value, str):
        return None
    cleaned = _LABEL_CHARS.sub("", value).strip("._:/-")
    if not cleaned:
        return None
    return cleaned[:_MAX_LABEL_LENGTH]


def _coerce_count(value: object) -> Optional[int]:
    """Return a non-negative ``int``, or ``None`` if not coercible.

    ``bool`` is a subclass of ``int`` in Python; we exclude it
    explicitly so a payload claiming ``"unknown": true`` does not
    silently render as ``1 unknown``.
    """
    if isinstance(value, bool):
        return None
    if not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


def _normalise_top_list(value: object, label_key: str) -> list[dict]:
    """Validate one of the ``top_*`` lists from ``/connections/summary``.

    Accepts a list of dicts shaped like ``{label_key: str, "count": int}``.
    Entries whose label fails sanitisation or whose count is not a
    non-negative int are dropped silently. The result is capped at
    :data:`_MAX_TOP_ENTRIES`.

    Anything that is not a list, or yields zero valid entries, returns
    an empty list — the caller drops the field rather than emitting
    ``"Top processes: ."`` into a prompt.
    """
    if not isinstance(value, list):
        return []
    result: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        label = _sanitise_label(item.get(label_key))
        count = _coerce_count(item.get("count"))
        if label is None or count is None:
            continue
        result.append({label_key: label, "count": count})
        if len(result) >= _MAX_TOP_ENTRIES:
            break
    return result


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
        mitigation_client: Optional[SilentGuardMitigationClient] = None,
    ) -> None:
        self._explicit_path: Optional[Path] = (
            Path(feed_path) if feed_path is not None else None
        )
        # `api_url=None` → look up env / config; ``""`` → explicitly off.
        self._explicit_api_url: Optional[str] = api_url
        self._explicit_timeout: Optional[float] = api_timeout_seconds
        self._explicit_client: Optional[SilentGuardClient] = client
        # Opt-in: only constructed when a caller asks for mitigation
        # state or an action. Keeping it ``None`` by default ensures
        # the read-only status / counts / connection-summary code paths
        # cannot accidentally instantiate a POST-capable client.
        self._explicit_mitigation_client: Optional[SilentGuardMitigationClient] = (
            mitigation_client
        )

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

    def get_connection_summary(self) -> Optional[dict]:
        """Return a richer read-only connection summary, or ``None``.

        Newer SilentGuard builds expose a compact aggregated view of
        the live connection set at ``GET /connections/summary``. When
        the HTTP transport is configured, SilentGuard is reachable,
        and the endpoint returns a recognisable payload, this method
        normalises it into a stable shape::

            {
                "total":    int,                       # optional
                "local":    int,                       # optional
                "known":    int,                       # optional
                "unknown":  int,                       # optional
                "top_processes":     [{"name": str, "count": int}, ...],   # optional
                "top_remote_hosts":  [{"host": str, "count": int}, ...],   # optional
            }

        Each field is included only when SilentGuard supplied it *and*
        it parses cleanly. Unknown extra keys in SilentGuard's payload
        are dropped, not surfaced — Nova never invents data, and never
        leaks raw payload shapes that have not been reviewed.

        Returns ``None`` whenever:

          * the provider is unavailable,
          * the HTTP transport is not configured (file-only fallback),
          * SilentGuard does not support the endpoint (older builds),
          * the response is not a JSON object,
          * the response is well-formed but contains no recognised
            fields after normalisation (so callers can treat
            "summary unavailable" and "summary present but empty"
            identically).

        Read-only — only a single ``GET /connections/summary`` call
        is issued, against the fixed read-only path list. Strings are
        character-set sanitised and length-capped before they leave
        this layer; the caller (prompt context, Settings card) can
        render them verbatim.
        """
        status = self.get_status()
        if not status.available:
            return None
        client = self._resolved_client()
        if client is None:
            return None
        getter = getattr(client, "get_connections_summary", None)
        if not callable(getter):
            return None
        try:
            payload = getter()
        except Exception:  # pragma: no cover — defensive belt-and-braces
            logger.debug(
                "SilentGuard connection summary fetch failed", exc_info=True,
            )
            return None
        if not isinstance(payload, dict):
            return None

        result: dict = {}
        for key in ("total", "local", "known", "unknown"):
            value = _coerce_count(payload.get(key))
            if value is not None:
                result[key] = value

        top_processes = _normalise_top_list(
            payload.get("top_processes"), "name",
        )
        if top_processes:
            result["top_processes"] = top_processes

        top_remote_hosts = _normalise_top_list(
            payload.get("top_remote_hosts"), "host",
        )
        if top_remote_hosts:
            result["top_remote_hosts"] = top_remote_hosts

        return result or None

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

    # ── Optional mitigation surface ─────────────────────────────────
    #
    # SilentGuard owns enforcement. These methods are the *only* place
    # the provider exposes write capability, and every one of them is
    # behind an explicit user-confirmation flow at the Nova endpoint
    # layer. They are intentionally small:
    #
    #   * ``get_mitigation_state``     — read-only snapshot.
    #   * ``enable_temporary_mitigation`` — opt-in, requires SilentGuard's
    #                                       acknowledgement payload.
    #   * ``disable_mitigation``       — opt-in, same acknowledgement.
    #
    # The methods never raise; failures map to ``None`` (state read)
    # or a calm :class:`MitigationActionResult` (action). The read-only
    # ``get_status`` / ``get_summary_counts`` / ``get_connection_summary``
    # paths above are unchanged and never invoke any of these.

    def _resolved_mitigation_client(self) -> Optional[SilentGuardMitigationClient]:
        """Return a ready mitigation client, or ``None`` if HTTP is off.

        The mitigation client speaks to the same loopback API base URL
        as the read-only client, but lives in its own module so the
        forbidden-verb assertion on ``silentguard_client.py`` can stay
        in place.
        """
        if self._explicit_mitigation_client is not None:
            return self._explicit_mitigation_client
        url = self._resolved_api_url()
        if not url:
            return None
        return SilentGuardMitigationClient(
            base_url=url,
            timeout_seconds=self._resolved_timeout(),
        )

    def get_mitigation_state(self) -> Optional[MitigationState]:
        """Return SilentGuard's current mitigation snapshot, or ``None``.

        ``None`` means the read-only API is not configured, the
        provider is currently unavailable, or SilentGuard returned a
        payload Nova could not parse at all. The Nova endpoint above
        translates ``None`` into a calm "mitigation status unavailable"
        response — callers must not treat ``None`` as "no mitigation
        is configured".

        Read-only. Issues at most one ``GET /mitigation`` against the
        loopback API.
        """
        status = self.get_status()
        if not status.available:
            return None
        client = self._resolved_mitigation_client()
        if client is None:
            return None
        try:
            return client.get_state()
        except Exception:  # pragma: no cover — defensive belt-and-braces
            logger.debug(
                "SilentGuard mitigation state fetch failed", exc_info=True,
            )
            return None

    def enable_temporary_mitigation(self) -> MitigationActionResult:
        """Ask SilentGuard to enable temporary mitigation.

        Sends SilentGuard's required acknowledgement payload. This
        method must only be called after the user has explicitly
        confirmed the action — that gating lives at the Nova endpoint
        layer (``POST /integrations/silentguard/mitigation/enable-
        temporary``). The provider does not record consent and does
        not re-derive it.

        Never raises. Returns a calm
        :class:`MitigationActionResult` describing the outcome — the
        endpoint above this layer surfaces the result as JSON without
        further translation.
        """
        client = self._resolved_mitigation_client()
        if client is None:
            return MitigationActionResult(
                ok=False, state=None,
                message="SilentGuard mitigation API is not configured.",
            )
        # Defence in depth: refuse to attempt the action when the
        # underlying read-only probe says SilentGuard is unreachable.
        # Saves us from posting into the void and confusing the user
        # with a SilentGuard-side error message.
        status = self.get_status()
        if not status.available:
            return MitigationActionResult(
                ok=False, state=None,
                message="SilentGuard is not reachable right now.",
            )
        try:
            return client.enable_temporary()
        except Exception:  # pragma: no cover — defensive belt-and-braces
            logger.debug(
                "SilentGuard mitigation enable-temporary failed", exc_info=True,
            )
            return MitigationActionResult(
                ok=False, state=None,
                message="SilentGuard mitigation request failed.",
            )

    def disable_mitigation(self) -> MitigationActionResult:
        """Ask SilentGuard to disable mitigation.

        Same gating contract as :meth:`enable_temporary_mitigation`:
        the user-confirmation step lives at the Nova endpoint layer.
        Never raises.
        """
        client = self._resolved_mitigation_client()
        if client is None:
            return MitigationActionResult(
                ok=False, state=None,
                message="SilentGuard mitigation API is not configured.",
            )
        status = self.get_status()
        if not status.available:
            return MitigationActionResult(
                ok=False, state=None,
                message="SilentGuard is not reachable right now.",
            )
        try:
            return client.disable()
        except Exception:  # pragma: no cover — defensive belt-and-braces
            logger.debug(
                "SilentGuard mitigation disable failed", exc_info=True,
            )
            return MitigationActionResult(
                ok=False, state=None,
                message="SilentGuard mitigation request failed.",
            )


def _normalise_url(value: str) -> str:
    """Trim whitespace and a trailing slash from an API base URL."""
    if not value:
        return ""
    return str(value).strip().rstrip("/")
