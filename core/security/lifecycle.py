"""
SilentGuard local API service lifecycle helper (Phase 1 — opt-in).

Nova is the cognitive layer; SilentGuard remains the security/network
engine. This helper lets Nova *optionally* start SilentGuard's local
**read-only** API service when the host operator has explicitly opted
in, while keeping every safety boundary the rest of the integration
already commits to:

  * no ``sudo`` / ``pkexec`` / ``doas`` / ``su`` / ``runuser``,
  * no system-level ``systemctl`` (only ``systemctl --user``),
  * no firewall command (``iptables`` / ``nftables`` / ``ufw`` / …),
  * no shell interpretation — argv only, never a string,
  * no command sourced from chat input or remote URLs,
  * no background polling, no retry loops, no notifications.

The helper is the **only** module in ``core.security`` allowed to
import :mod:`subprocess` and :mod:`shutil`. The read-only provider,
the HTTP client, and the prompt context block stay forbidden from
touching either, and the existing AST-based "no forbidden imports"
test continues to assert that.

The orchestration is single-pass and synchronous:

  1. Look up the host-level enabled / auto-start / start-mode / unit
     config.
  2. If integration is disabled → ``state="disabled"``.
  3. Probe :class:`SilentGuardProvider` once. If reachable →
     ``state="connected"``.
  4. If auto-start is off, or start-mode is not ``"systemd-user"``,
     or the unit name fails validation → ``state="unavailable"`` /
     ``state="could_not_start"`` as appropriate, with no spawn.
  5. Otherwise spawn ``systemctl --user start <unit>`` with strict
     argv, ``shell=False``, no inherited stdin, a short timeout, and
     captured stdout/stderr. Wait one bounded delay, re-probe once,
     and return ``connected`` / ``starting`` / ``could_not_start``.

Every error path returns a calm :class:`LifecycleStatus` with a
sanitized, user-safe ``message``. The function never raises into the
chat or web layer.

See ``docs/silentguard-integration-roadmap.md`` for the design
rationale and the exhaustive non-goals.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

from core.security.provider import SecurityProvider
from core.security.silentguard import SilentGuardProvider

logger = logging.getLogger(__name__)

# Lifecycle states surfaced through ``/integrations/silentguard/lifecycle``
# and embedded in ``/integrations/status``. Plain strings so the value
# round-trips through JSON without bespoke encoders. The vocabulary is
# intentionally small — adding a state is a deliberate review.
STATE_DISABLED = "disabled"                # integration switched off
STATE_CONNECTED = "connected"              # API reachable, read-only
STATE_STARTING = "starting"                # spawn accepted; not yet reachable
STATE_COULD_NOT_START = "could_not_start"  # spawn rejected / failed
STATE_UNAVAILABLE = "unavailable"          # not reachable, no auto-start

# Allowed values for ``NOVA_SILENTGUARD_START_MODE``. Anything else
# normalises to ``"disabled"`` so a typo never opens a new spawn path.
START_MODE_SYSTEMD_USER = "systemd-user"
START_MODE_DISABLED = "disabled"
_ALLOWED_START_MODES = frozenset({START_MODE_SYSTEMD_USER, START_MODE_DISABLED})

# The default systemd user unit Nova will try to start when the
# operator has opted in but not customised the unit name. Mirrors the
# documented value in ``docs/silentguard-integration-roadmap.md``.
DEFAULT_SYSTEMD_UNIT = "silentguard-api.service"

# Time budget for the spawned ``systemctl --user start <unit>`` call.
# Short by design — systemd queues the start and returns; the unit
# itself comes up asynchronously. A hung systemctl maps to
# "could_not_start".
DEFAULT_START_TIMEOUT_SECONDS = 3.0
# Bounded sleep between accepting the systemctl start and re-probing
# the API. Single-shot, not a loop.
DEFAULT_POST_START_DELAY_SECONDS = 1.0

# Strict unit-name regex: must start with a lowercase alnum, may
# contain alnum / dot / dash / underscore, must end in ``.service``.
# No path separators, no shell metacharacters, no leading dots.
_UNIT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*\.service$")

# Sequences forbidden anywhere in the unit name (defence in depth on
# top of the regex). Any of these is an immediate validation failure.
_UNIT_FORBIDDEN_SUBSTRINGS = (
    "..",
    "/",
    "\\",
    "\n",
    "\r",
    "\t",
    " ",
    ";",
    "&",
    "|",
    "$",
    "`",
    "<",
    ">",
    "(",
    ")",
    "{",
    "}",
)


@dataclass(frozen=True)
class LifecycleStatus:
    """Read-only snapshot of SilentGuard auto-start lifecycle state.

    Returned by :func:`ensure_running`. Stable, JSON-friendly shape so
    the web layer can surface it without a bespoke renderer. ``state``
    is one of the ``STATE_*`` constants; ``message`` is a calm,
    user-safe sentence (no stack traces, no raw paths, no exception
    text).
    """

    state: str
    enabled: bool
    auto_start: bool
    start_mode: str
    unit: str
    message: str

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "enabled": self.enabled,
            "auto_start": self.auto_start,
            "start_mode": self.start_mode,
            "unit": self.unit,
            "message": self.message,
        }


# ── Config resolution ───────────────────────────────────────────────

def _bool_env(name: str, *, default: bool) -> bool:
    """Read a boolean from the environment with a strict allowlist."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _resolve_enabled() -> bool:
    """Host-level integration switch. Env wins over config import."""
    raw = os.environ.get("NOVA_SILENTGUARD_ENABLED")
    if raw is not None:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    try:
        from config import NOVA_SILENTGUARD_ENABLED  # local import: avoid cycles
    except Exception:  # pragma: no cover — config import is best-effort
        return False
    return bool(NOVA_SILENTGUARD_ENABLED)


def _resolve_auto_start() -> bool:
    """Whether Nova may spawn the configured start command."""
    raw = os.environ.get("NOVA_SILENTGUARD_AUTO_START")
    if raw is not None:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    try:
        from config import NOVA_SILENTGUARD_AUTO_START
    except Exception:  # pragma: no cover
        return False
    return bool(NOVA_SILENTGUARD_AUTO_START)


def _resolve_start_mode() -> str:
    """Pick the start backend; unknown values normalise to disabled."""
    raw = os.environ.get("NOVA_SILENTGUARD_START_MODE")
    if raw is None:
        try:
            from config import NOVA_SILENTGUARD_START_MODE
        except Exception:  # pragma: no cover
            return START_MODE_DISABLED
        raw = NOVA_SILENTGUARD_START_MODE
    value = (raw or "").strip().lower()
    return value if value in _ALLOWED_START_MODES else START_MODE_DISABLED


def _resolve_unit() -> str:
    """Look up the configured systemd user unit name."""
    raw = os.environ.get("NOVA_SILENTGUARD_SYSTEMD_UNIT")
    if raw is None:
        try:
            from config import NOVA_SILENTGUARD_SYSTEMD_UNIT
        except Exception:  # pragma: no cover
            return DEFAULT_SYSTEMD_UNIT
        raw = NOVA_SILENTGUARD_SYSTEMD_UNIT
    return (raw or "").strip()


# ── Validation ──────────────────────────────────────────────────────

def validate_unit_name(unit: str) -> bool:
    """Return True only when ``unit`` is a safe systemd user unit name.

    Defensive rules, in order:
      * input is a string;
      * length between 1 and 128 chars (real units are ≪ this);
      * input equals its ``.strip()`` form — any leading/trailing
        whitespace, including embedded newlines, is rejected;
      * no forbidden substring (``..``, path separators, shell
        metacharacters, whitespace, control characters);
      * matches ``^[a-z0-9][a-z0-9._-]*\\.service$``.

    The validator deliberately does **not** silently strip input: a
    config with an embedded newline is suspicious, and surfacing that
    as a rejection (``state="could_not_start"``) is more honest than
    quietly normalising it. ``ensure_running`` resolves env vars via
    its own ``str.strip()`` before calling this validator, so a
    well-formed config still validates.

    A rejected unit causes :func:`ensure_running` to surface
    ``state="could_not_start"`` *before* any spawn is attempted.
    """
    if not isinstance(unit, str):
        return False
    if not unit or len(unit) > 128:
        return False
    if unit != unit.strip():
        return False
    if any(bad in unit for bad in _UNIT_FORBIDDEN_SUBSTRINGS):
        return False
    return bool(_UNIT_RE.match(unit))


def _systemctl_path() -> Optional[str]:
    """Return the absolute path to systemctl, or ``None`` if missing."""
    return shutil.which("systemctl")


# ── Spawner ─────────────────────────────────────────────────────────

def _start_systemd_user_unit(unit: str) -> tuple[bool, str]:
    """Run ``systemctl --user start <unit>`` with strict argv.

    Never raises. Returns ``(success, sanitized_detail)``.

    Safety properties (assertion-grade, not aspirational):
      * argv list, ``shell=False`` — no shell interpretation by
        Python or by the OS.
      * absolute path to ``systemctl`` resolved via ``shutil.which``;
        if that fails we never spawn.
      * literal ``--user`` flag — no system-level systemctl, ever.
      * literal ``start`` verb — no ``stop`` / ``restart`` /
        ``reload`` / ``enable`` reachable from this code path.
      * stdin redirected from /dev/null; stdout / stderr captured to
        bounded buffers.
      * short timeout — a hung systemctl is treated as failure, not
        as something to wait on indefinitely.

    The returned ``detail`` is a short, sanitized string suitable for
    debug logging; it is **not** surfaced to the user verbatim so that
    a hostile log line cannot leak into the UI.
    """
    binary = _systemctl_path()
    if binary is None:
        return False, "systemctl_not_found"

    argv = [binary, "--user", "start", unit]
    # Belt-and-braces: re-validate just before exec, in case a caller
    # somehow reached this path without going through ensure_running.
    if not validate_unit_name(unit):
        return False, "unit_validation_failed"

    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=DEFAULT_START_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        logger.debug("systemctl --user start %s timed out", unit)
        return False, "spawn_timeout"
    except (OSError, ValueError) as exc:
        logger.debug("systemctl --user start %s failed to spawn: %s", unit, exc)
        return False, "spawn_error"

    if result.returncode != 0:
        logger.debug(
            "systemctl --user start %s exited with code %s",
            unit, result.returncode,
        )
        return False, "non_zero_exit"
    return True, "spawn_accepted"


# ── Public orchestration ────────────────────────────────────────────

def _provider_status(provider: SecurityProvider):
    """Call ``provider.get_status``; return ``None`` if it raises."""
    try:
        return provider.get_status()
    except Exception:  # pragma: no cover — providers must not raise
        logger.debug(
            "security provider get_status raised; treating as unavailable",
            exc_info=True,
        )
        return None


def ensure_running(
    provider: Optional[SecurityProvider] = None,
    *,
    sleep: Optional[callable] = None,
) -> LifecycleStatus:
    """Return the current SilentGuard lifecycle state, optionally
    starting the local read-only API service if the operator has
    opted in.

    Parameters
    ----------
    provider:
        An optional :class:`SecurityProvider` to use for reachability
        probes. Defaults to :class:`SilentGuardProvider`, which reads
        the configured API URL / file path itself.
    sleep:
        Optional sleep hook (defaults to :func:`time.sleep`). Tests
        use this to keep the suite fast without changing the
        production code path.

    The function is single-pass and synchronous; there is no retry
    loop, no thread, and no scheduler. It never raises.
    """
    enabled = _resolve_enabled()
    auto_start = _resolve_auto_start()
    start_mode = _resolve_start_mode()
    unit = _resolve_unit()

    if not enabled:
        return LifecycleStatus(
            state=STATE_DISABLED,
            enabled=False,
            auto_start=auto_start,
            start_mode=start_mode,
            unit=unit,
            message="SilentGuard integration disabled.",
        )

    active = provider if provider is not None else SilentGuardProvider()

    # ── First probe ────────────────────────────────────────────────
    status = _provider_status(active)
    if status is not None and status.available:
        return LifecycleStatus(
            state=STATE_CONNECTED,
            enabled=True,
            auto_start=auto_start,
            start_mode=start_mode,
            unit=unit,
            message="SilentGuard connected in read-only mode.",
        )

    # ── Auto-start gating ──────────────────────────────────────────
    if not auto_start or start_mode != START_MODE_SYSTEMD_USER:
        return LifecycleStatus(
            state=STATE_UNAVAILABLE,
            enabled=True,
            auto_start=auto_start,
            start_mode=start_mode,
            unit=unit,
            message="SilentGuard unavailable.",
        )

    if not validate_unit_name(unit):
        # Configured to auto-start, but the unit fails validation —
        # surface this as "could not be started" (no spawn happened)
        # rather than silently degrading to "unavailable", so the
        # operator sees their config is wrong.
        return LifecycleStatus(
            state=STATE_COULD_NOT_START,
            enabled=True,
            auto_start=auto_start,
            start_mode=start_mode,
            unit=unit,
            message="SilentGuard could not be started.",
        )

    # ── Spawn ──────────────────────────────────────────────────────
    spawned, _detail = _start_systemd_user_unit(unit)
    if not spawned:
        return LifecycleStatus(
            state=STATE_COULD_NOT_START,
            enabled=True,
            auto_start=auto_start,
            start_mode=start_mode,
            unit=unit,
            message="SilentGuard could not be started.",
        )

    # ── Bounded re-probe ──────────────────────────────────────────
    sleeper = sleep if callable(sleep) else time.sleep
    try:
        sleeper(DEFAULT_POST_START_DELAY_SECONDS)
    except Exception:  # pragma: no cover — defensive
        pass

    status_after = _provider_status(active)
    if status_after is not None and status_after.available:
        return LifecycleStatus(
            state=STATE_CONNECTED,
            enabled=True,
            auto_start=auto_start,
            start_mode=start_mode,
            unit=unit,
            message="SilentGuard connected in read-only mode.",
        )

    # The systemctl call was accepted, but the API has not bound its
    # socket yet. Honest middle state: "starting". The next status
    # check will resolve to connected (or unavailable if the unit
    # never came up).
    return LifecycleStatus(
        state=STATE_STARTING,
        enabled=True,
        auto_start=auto_start,
        start_mode=start_mode,
        unit=unit,
        message="Starting SilentGuard…",
    )


def disabled_status(message: str = "SilentGuard integration disabled.") -> LifecycleStatus:
    """Return a calm ``state="disabled"`` snapshot.

    Useful when the per-user gate is off (so the host-level config is
    not relevant to *this* user) and the web layer wants to short-
    circuit before invoking :func:`ensure_running`. The returned
    ``auto_start`` is hard-coded to ``False`` so the UI never claims
    a disabled user can trigger a spawn; the unit / start_mode fields
    still surface the host config so operators can debug.
    """
    return LifecycleStatus(
        state=STATE_DISABLED,
        enabled=False,
        auto_start=False,
        start_mode=_resolve_start_mode(),
        unit=_resolve_unit(),
        message=message,
    )


__all__ = [
    "DEFAULT_SYSTEMD_UNIT",
    "LifecycleStatus",
    "STATE_CONNECTED",
    "STATE_COULD_NOT_START",
    "STATE_DISABLED",
    "STATE_STARTING",
    "STATE_UNAVAILABLE",
    "START_MODE_DISABLED",
    "START_MODE_SYSTEMD_USER",
    "disabled_status",
    "ensure_running",
    "validate_unit_name",
]
