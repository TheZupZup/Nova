"""
Admin-only maintenance / update helper.

Nova is a local-first, self-hostable assistant. This module gives an
admin a *narrow*, *opt-in* way to ask the local install three things:

  1. What is the state of the checkout (branch, commit, upstream,
     clean / dirty, fast-forward-able)?
  2. After an explicit confirmation, run ``git pull --ff-only`` once
     so the working tree advances to the upstream tip — but only when
     the working tree is clean and the branch is strictly behind
     upstream.
  3. After an explicit confirmation, ask systemd-user to restart the
     configured Nova unit — when (and only when) the operator has
     enabled the systemd-user restart mode and configured a valid
     unit name.

What this module is *not*:

  * a web terminal,
  * a generic command runner,
  * a chat-driven action,
  * an auto-updater,
  * a privilege-escalation helper.

Safety contract (enforced):

  * **Allowlisted git commands only.** Every subprocess call is
    constructed from a hard-coded argv list — the only varying parts
    are the resolved git binary path and ``HEAD..@{u}`` for the
    upstream comparisons. No user / model / chat input is ever
    concatenated into a command.
  * **``shell=False`` everywhere.** No string commands, no shell
    interpretation by Python or by the OS.
  * **No ``sudo`` / ``pkexec`` / ``doas`` / ``su`` / ``runuser``.** No
    privilege escalation, ever.
  * **Timeouts on every subprocess call.** A hung git or systemctl
    maps to a calm "command_failed" snapshot, never to a wedged
    request.
  * **Fast-forward pulls only.** ``git pull --ff-only`` refuses to
    merge or rebase. A dirty working tree or a diverged branch
    surfaces a refusal *before* the pull is attempted.
  * **Restart is its own switch.** The pull switch does not unlock
    restart, and vice versa. Restart, when enabled, uses
    ``systemctl --user restart <validated-unit>`` exclusively.
  * **Sanitised errors.** The structured response never embeds
    secrets, environment variables, raw stderr, or stack traces.
    Detail strings are short, fixed, frontend-safe summaries.

The helper is the single module in ``core`` allowed to import
``subprocess`` for git operations. The web layer must call only the
public functions in this module and gate them with ``require_admin``.

See ``docs/maintenance-center.md`` for the operator-facing setup
walkthrough and the non-goals.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

logger = logging.getLogger(__name__)


# ── Public state constants ──────────────────────────────────────────

STATE_DISABLED = "disabled"            # NOVA_MAINTENANCE_ENABLED is off
STATE_READY = "ready"                  # repo probed, snapshot is valid
STATE_UNAVAILABLE = "unavailable"      # git missing, repo not a checkout

# Update-availability sub-states returned in ``update_available``.
UPDATE_UP_TO_DATE = "up_to_date"
UPDATE_AVAILABLE = "available"
UPDATE_DIVERGED = "diverged"
UPDATE_NO_UPSTREAM = "no_upstream"
UPDATE_UNKNOWN = "unknown"

# Pull outcomes.
PULL_DISABLED = "disabled"
PULL_NOT_ALLOWED = "pull_not_allowed"
PULL_REPO_UNAVAILABLE = "repo_unavailable"
PULL_DIRTY_WORKING_TREE = "dirty_working_tree"
PULL_DIVERGED = "diverged"
PULL_NO_UPSTREAM = "no_upstream"
PULL_NOT_FAST_FORWARD = "not_fast_forward"
PULL_SUCCESS = "success"
PULL_FAILED = "failed"

# Restart outcomes.
RESTART_DISABLED = "disabled"
RESTART_NOT_ALLOWED = "restart_not_allowed"
RESTART_MODE_DISABLED = "restart_mode_disabled"
RESTART_INVALID_UNIT = "invalid_unit"
RESTART_SYSTEMCTL_MISSING = "systemctl_missing"
RESTART_FAILED = "failed"
RESTART_ACCEPTED = "accepted"

# Restart modes.
RESTART_MODE_SYSTEMD_USER = "systemd-user"
RESTART_MODE_OFF = "disabled"
_ALLOWED_RESTART_MODES = frozenset({RESTART_MODE_SYSTEMD_USER, RESTART_MODE_OFF})


# ── Internal limits ─────────────────────────────────────────────────

# Per-call timeouts. Network-touching calls (``fetch``, ``pull``) get
# a larger budget; local-only calls (``status``, ``rev-parse``) stay
# small so a misbehaving repo cannot wedge a request.
_GIT_LOCAL_TIMEOUT_SECONDS = 5.0
_GIT_FETCH_TIMEOUT_SECONDS = 30.0
_GIT_PULL_TIMEOUT_SECONDS = 60.0
_SYSTEMCTL_TIMEOUT_SECONDS = 5.0

# Caps so a single response cannot balloon the JSON payload. The
# changed-files summary is the only stretchy field — keep it small.
_MAX_LOG_LINES = 50
_MAX_DIFF_LINES = 50
_MAX_LINE_CHARS = 300

# Strict unit-name regex (mirrors the SilentGuard lifecycle helper).
_UNIT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*\.service$")
_UNIT_FORBIDDEN_SUBSTRINGS = (
    "..", "/", "\\", "\n", "\r", "\t", " ",
    ";", "&", "|", "$", "`", "<", ">", "(", ")", "{", "}",
)


# ── Snapshot dataclasses ────────────────────────────────────────────


@dataclass(frozen=True)
class MaintenanceStatus:
    """Calm, frontend-safe snapshot of the maintenance surface.

    ``state`` is one of the module-level ``STATE_*`` values. When the
    feature is disabled, every other field is left at its default so
    the response shape stays stable. The structure deliberately does
    not include env vars, raw stderr, or absolute paths — the only
    paths surfaced are the configured ``repo_path`` (when non-empty)
    and the ``unit`` field.
    """

    state: str
    enabled: bool = False
    allow_pull: bool = False
    allow_restart: bool = False
    restart_mode: str = RESTART_MODE_OFF
    unit: str = ""
    repo_path: str = ""

    # Repo facts (filled when state == STATE_READY).
    branch: str = ""
    commit: str = ""
    upstream: str = ""
    has_upstream: bool = False
    working_tree_clean: bool = True
    update_available: str = UPDATE_UNKNOWN
    behind_count: int = 0
    ahead_count: int = 0
    incoming_commits: tuple[str, ...] = field(default_factory=tuple)
    changed_files: tuple[str, ...] = field(default_factory=tuple)

    # ``detail`` is a short, fixed sentence the UI can render verbatim.
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "enabled": self.enabled,
            "allow_pull": self.allow_pull,
            "allow_restart": self.allow_restart,
            "restart_mode": self.restart_mode,
            "unit": self.unit,
            "repo_path": self.repo_path,
            "branch": self.branch,
            "commit": self.commit,
            "upstream": self.upstream,
            "has_upstream": self.has_upstream,
            "working_tree_clean": self.working_tree_clean,
            "update_available": self.update_available,
            "behind_count": self.behind_count,
            "ahead_count": self.ahead_count,
            "incoming_commits": list(self.incoming_commits),
            "changed_files": list(self.changed_files),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PullResult:
    """Outcome of a single ``git pull --ff-only`` attempt."""

    outcome: str
    detail: str = ""
    previous_commit: str = ""
    new_commit: str = ""

    def as_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "detail": self.detail,
            "previous_commit": self.previous_commit,
            "new_commit": self.new_commit,
        }


@dataclass(frozen=True)
class RestartResult:
    """Outcome of a single restart attempt."""

    outcome: str
    detail: str = ""
    mode: str = RESTART_MODE_OFF
    unit: str = ""

    def as_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "detail": self.detail,
            "mode": self.mode,
            "unit": self.unit,
        }


# ── Config resolution ───────────────────────────────────────────────


def _resolve_enabled() -> bool:
    raw = os.environ.get("NOVA_MAINTENANCE_ENABLED")
    if raw is not None:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    try:
        from config import NOVA_MAINTENANCE_ENABLED
    except Exception:  # pragma: no cover — config import is best-effort
        return False
    return bool(NOVA_MAINTENANCE_ENABLED)


def _resolve_allow_pull() -> bool:
    raw = os.environ.get("NOVA_MAINTENANCE_ALLOW_PULL")
    if raw is not None:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    try:
        from config import NOVA_MAINTENANCE_ALLOW_PULL
    except Exception:  # pragma: no cover
        return False
    return bool(NOVA_MAINTENANCE_ALLOW_PULL)


def _resolve_allow_restart() -> bool:
    raw = os.environ.get("NOVA_MAINTENANCE_ALLOW_RESTART")
    if raw is not None:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    try:
        from config import NOVA_MAINTENANCE_ALLOW_RESTART
    except Exception:  # pragma: no cover
        return False
    return bool(NOVA_MAINTENANCE_ALLOW_RESTART)


def _resolve_restart_mode() -> str:
    raw = os.environ.get("NOVA_MAINTENANCE_RESTART_MODE")
    if raw is None:
        try:
            from config import NOVA_MAINTENANCE_RESTART_MODE
        except Exception:  # pragma: no cover
            return RESTART_MODE_OFF
        raw = NOVA_MAINTENANCE_RESTART_MODE
    value = (raw or "").strip().lower()
    return value if value in _ALLOWED_RESTART_MODES else RESTART_MODE_OFF


def _resolve_unit() -> str:
    raw = os.environ.get("NOVA_MAINTENANCE_SYSTEMD_UNIT")
    if raw is None:
        try:
            from config import NOVA_MAINTENANCE_SYSTEMD_UNIT
        except Exception:  # pragma: no cover
            return "nova.service"
        raw = NOVA_MAINTENANCE_SYSTEMD_UNIT
    return (raw or "").strip()


def _resolve_repo_path() -> str:
    raw = os.environ.get("NOVA_MAINTENANCE_REPO_PATH")
    if raw is None:
        try:
            from config import NOVA_MAINTENANCE_REPO_PATH
        except Exception:  # pragma: no cover
            raw = ""
        else:
            raw = NOVA_MAINTENANCE_REPO_PATH
    value = (raw or "").strip()
    if value:
        return value
    # Fall back to the directory that contains this module's package —
    # i.e. the Nova checkout itself. We never invent a path; we only
    # use the install's own directory when the operator did not pin
    # one explicitly.
    return str(Path(__file__).resolve().parent.parent)


# ── Validation ──────────────────────────────────────────────────────


def validate_unit_name(unit: str) -> bool:
    """Return True only when ``unit`` is a safe systemd user unit name.

    Same defensive rules as ``core/security/lifecycle.py``: the input
    must be a non-empty string under 128 characters, equal to its
    ``.strip()`` form, free of forbidden substrings (``..``, path
    separators, shell metacharacters, whitespace, control characters),
    and matching ``^[a-z0-9][a-z0-9._-]*\\.service$``.
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


def _git_path() -> Optional[str]:
    """Return the absolute path to git, or ``None`` if missing."""
    return shutil.which("git")


def _systemctl_path() -> Optional[str]:
    return shutil.which("systemctl")


def _is_git_checkout(repo_path: str) -> bool:
    """Cheap structural check: ``repo_path/.git`` must exist.

    The directory form is the common case; a worktree gitfile lands as
    a regular file pointing to the real ``.git`` dir, so we accept
    either. We deliberately do *not* fall back to walking parents —
    the maintenance surface operates on the configured checkout, not
    on an ancestor.
    """
    try:
        p = Path(repo_path)
        if not p.is_dir():
            return False
        gitdir = p / ".git"
        return gitdir.exists()
    except OSError:
        return False


# ── Subprocess primitives ───────────────────────────────────────────


def _run_git(
    argv_tail: Sequence[str],
    *,
    repo_path: str,
    timeout: float,
) -> tuple[int, str, str]:
    """Run ``git <argv_tail>`` in ``repo_path`` with strict argv.

    Never raises. Returns ``(returncode, stdout, stderr)``. A missing
    git binary maps to ``(-1, "", "")`` so callers can branch on the
    rc cleanly. stdout / stderr are decoded as UTF-8 with replacement,
    *not* surfaced to the frontend — they exist for the helper's own
    parsing only.

    The first argv element is always the *resolved* git path; the
    rest is the literal argv_tail. The CWD is the configured repo
    path, ``shell=False`` is mandatory, stdin is closed, env is left
    untouched (we never set HOME or GIT_DIR from user input).
    """
    binary = _git_path()
    if binary is None:
        return -1, "", ""
    argv = [binary, *argv_tail]
    try:
        result = subprocess.run(
            argv,
            cwd=repo_path,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        logger.debug("git %s timed out in %s", argv_tail, repo_path)
        return -1, "", ""
    except (OSError, ValueError) as exc:
        logger.debug("git %s failed to spawn: %s", argv_tail, exc)
        return -1, "", ""
    stdout = (result.stdout or b"").decode("utf-8", errors="replace")
    stderr = (result.stderr or b"").decode("utf-8", errors="replace")
    return result.returncode, stdout, stderr


def _git_branch(repo_path: str) -> str:
    rc, out, _ = _run_git(
        ["rev-parse", "--abbrev-ref", "HEAD"],
        repo_path=repo_path, timeout=_GIT_LOCAL_TIMEOUT_SECONDS,
    )
    return out.strip() if rc == 0 else ""


def _git_head(repo_path: str) -> str:
    rc, out, _ = _run_git(
        ["rev-parse", "HEAD"],
        repo_path=repo_path, timeout=_GIT_LOCAL_TIMEOUT_SECONDS,
    )
    return out.strip() if rc == 0 else ""


def _git_upstream(repo_path: str) -> str:
    rc, out, _ = _run_git(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        repo_path=repo_path, timeout=_GIT_LOCAL_TIMEOUT_SECONDS,
    )
    return out.strip() if rc == 0 else ""


def _git_status_clean(repo_path: str) -> bool:
    """True when the working tree is clean (porcelain output empty)."""
    rc, out, _ = _run_git(
        ["status", "--porcelain"],
        repo_path=repo_path, timeout=_GIT_LOCAL_TIMEOUT_SECONDS,
    )
    if rc != 0:
        # An unreadable status is not "clean" — surface it as dirty so
        # we never pull on top of a state we couldn't inspect.
        return False
    return out.strip() == ""


def _git_revlist_count(repo_path: str, spec: str) -> int:
    """Count commits on one side of a symmetric difference.

    ``spec`` is one of ``HEAD..@{u}`` (commits Nova is behind) or
    ``@{u}..HEAD`` (commits Nova is ahead by). The function is
    defensive: a non-zero rc or an unparseable count returns 0.
    """
    rc, out, _ = _run_git(
        ["rev-list", "--count", spec],
        repo_path=repo_path, timeout=_GIT_LOCAL_TIMEOUT_SECONDS,
    )
    if rc != 0:
        return 0
    try:
        return max(0, int(out.strip()))
    except ValueError:
        return 0


def _truncate(line: str) -> str:
    """Clip a single log / diff line so it cannot bloat the response."""
    if len(line) <= _MAX_LINE_CHARS:
        return line
    return line[:_MAX_LINE_CHARS] + "…"


def _git_incoming_log(repo_path: str) -> tuple[str, ...]:
    rc, out, _ = _run_git(
        ["log", "--oneline", "HEAD..@{u}"],
        repo_path=repo_path, timeout=_GIT_LOCAL_TIMEOUT_SECONDS,
    )
    if rc != 0:
        return ()
    lines = [_truncate(s) for s in out.splitlines() if s.strip()]
    return tuple(lines[:_MAX_LOG_LINES])


def _git_incoming_diffstat(repo_path: str) -> tuple[str, ...]:
    rc, out, _ = _run_git(
        ["diff", "--stat", "HEAD..@{u}"],
        repo_path=repo_path, timeout=_GIT_LOCAL_TIMEOUT_SECONDS,
    )
    if rc != 0:
        return ()
    lines = [_truncate(s) for s in out.splitlines() if s.strip()]
    return tuple(lines[:_MAX_DIFF_LINES])


# ── Public helpers ──────────────────────────────────────────────────


def is_enabled() -> bool:
    """Public read of ``NOVA_MAINTENANCE_ENABLED``.

    Re-evaluated on every call so a tests-time ``monkeypatch.setenv``
    and a systemd-provided env both resolve correctly.
    """
    return _resolve_enabled()


def disabled_status(message: str = "Maintenance disabled.") -> MaintenanceStatus:
    """Return a calm ``state="disabled"`` snapshot.

    The web layer calls this when the host operator has not opted in,
    so the response shape is stable even with the feature off.
    """
    return MaintenanceStatus(
        state=STATE_DISABLED,
        enabled=False,
        allow_pull=False,
        allow_restart=False,
        restart_mode=_resolve_restart_mode(),
        unit=_resolve_unit(),
        repo_path=_resolve_repo_path(),
        detail=message,
    )


def get_status(*, do_fetch: bool = False) -> MaintenanceStatus:
    """Read-only snapshot of the maintenance surface.

    When ``do_fetch`` is True, the helper runs ``git fetch`` once
    before reading branch / status / ahead-behind state so the
    response reflects the freshest upstream. ``fetch`` is the only
    network-touching call here, and it never modifies the working
    tree.

    Never raises. A missing git binary, a non-checkout path, or a
    transient failure each map to a calm ``state="unavailable"``
    snapshot with a fixed message.
    """
    enabled = _resolve_enabled()
    allow_pull = _resolve_allow_pull()
    allow_restart = _resolve_allow_restart()
    restart_mode = _resolve_restart_mode()
    unit = _resolve_unit()
    repo_path = _resolve_repo_path()

    if not enabled:
        return MaintenanceStatus(
            state=STATE_DISABLED,
            enabled=False,
            allow_pull=allow_pull,
            allow_restart=allow_restart,
            restart_mode=restart_mode,
            unit=unit,
            repo_path=repo_path,
            detail="Maintenance disabled.",
        )

    if _git_path() is None:
        return MaintenanceStatus(
            state=STATE_UNAVAILABLE,
            enabled=True,
            allow_pull=allow_pull,
            allow_restart=allow_restart,
            restart_mode=restart_mode,
            unit=unit,
            repo_path=repo_path,
            detail="git is not available on this host.",
        )

    if not _is_git_checkout(repo_path):
        return MaintenanceStatus(
            state=STATE_UNAVAILABLE,
            enabled=True,
            allow_pull=allow_pull,
            allow_restart=allow_restart,
            restart_mode=restart_mode,
            unit=unit,
            repo_path=repo_path,
            detail="Configured path is not a git checkout.",
        )

    if do_fetch:
        # ``git fetch`` is read-only against the working tree. It can
        # be slow on a misconfigured remote — the timeout above is
        # already short. We tolerate a non-zero rc (offline host, no
        # remote, auth refused) by continuing with the existing
        # remote-tracking refs.
        _run_git(
            ["fetch"],
            repo_path=repo_path, timeout=_GIT_FETCH_TIMEOUT_SECONDS,
        )

    branch = _git_branch(repo_path)
    commit = _git_head(repo_path)
    upstream = _git_upstream(repo_path)
    has_upstream = bool(upstream)
    clean = _git_status_clean(repo_path)

    if not has_upstream:
        return MaintenanceStatus(
            state=STATE_READY,
            enabled=True,
            allow_pull=allow_pull,
            allow_restart=allow_restart,
            restart_mode=restart_mode,
            unit=unit,
            repo_path=repo_path,
            branch=branch,
            commit=commit,
            upstream="",
            has_upstream=False,
            working_tree_clean=clean,
            update_available=UPDATE_NO_UPSTREAM,
            detail="No upstream branch configured.",
        )

    behind = _git_revlist_count(repo_path, "HEAD..@{u}")
    ahead = _git_revlist_count(repo_path, "@{u}..HEAD")

    if behind == 0 and ahead == 0:
        availability = UPDATE_UP_TO_DATE
        detail = "Already up to date."
        incoming = ()
        changed = ()
    elif behind > 0 and ahead == 0:
        availability = UPDATE_AVAILABLE
        detail = f"{behind} new commit{'s' if behind != 1 else ''} available."
        incoming = _git_incoming_log(repo_path)
        changed = _git_incoming_diffstat(repo_path)
    elif behind == 0 and ahead > 0:
        # Local branch is ahead of upstream. From the update-center
        # perspective there is nothing to pull, but we surface
        # ``up_to_date`` with the ahead count exposed so the UI can
        # show a soft note if it likes.
        availability = UPDATE_UP_TO_DATE
        detail = "Already up to date."
        incoming = ()
        changed = ()
    else:
        availability = UPDATE_DIVERGED
        detail = "Branch diverged from upstream. Manual intervention required."
        # We still surface the incoming commits / diffstat so the
        # admin can see what's on the other side without us touching
        # the working tree.
        incoming = _git_incoming_log(repo_path)
        changed = _git_incoming_diffstat(repo_path)

    return MaintenanceStatus(
        state=STATE_READY,
        enabled=True,
        allow_pull=allow_pull,
        allow_restart=allow_restart,
        restart_mode=restart_mode,
        unit=unit,
        repo_path=repo_path,
        branch=branch,
        commit=commit,
        upstream=upstream,
        has_upstream=True,
        working_tree_clean=clean,
        update_available=availability,
        behind_count=behind,
        ahead_count=ahead,
        incoming_commits=incoming,
        changed_files=changed,
        detail=detail,
    )


def fetch() -> MaintenanceStatus:
    """Run ``git fetch`` and return a refreshed status snapshot.

    Convenience wrapper for the ``POST /admin/maintenance/fetch``
    endpoint. Disabled maintenance short-circuits to the calm
    disabled snapshot — no network call is issued.
    """
    if not _resolve_enabled():
        return disabled_status()
    return get_status(do_fetch=True)


def pull() -> PullResult:
    """Run ``git pull --ff-only`` after the strict safety checks.

    Returns a :class:`PullResult` describing the outcome. The function
    never raises and never falls back to a non-fast-forward pull,
    even when the underlying git call would accept one.

    Refusal reasons (no spawn happens for any of these):
      * maintenance is disabled  → ``disabled``
      * pull is not allowed      → ``pull_not_allowed``
      * git is missing / no repo → ``repo_unavailable``
      * no upstream configured   → ``no_upstream``
      * working tree is dirty    → ``dirty_working_tree``
      * branch is diverged       → ``diverged``
      * branch is ahead-only or already up-to-date → ``not_fast_forward``
    """
    if not _resolve_enabled():
        return PullResult(
            outcome=PULL_DISABLED, detail="Maintenance disabled.",
        )
    if not _resolve_allow_pull():
        return PullResult(
            outcome=PULL_NOT_ALLOWED,
            detail="Pull is disabled in the host configuration.",
        )

    repo_path = _resolve_repo_path()
    if _git_path() is None or not _is_git_checkout(repo_path):
        return PullResult(
            outcome=PULL_REPO_UNAVAILABLE,
            detail="Configured repository is not available.",
        )

    upstream = _git_upstream(repo_path)
    if not upstream:
        return PullResult(
            outcome=PULL_NO_UPSTREAM,
            detail="No upstream branch configured.",
        )

    if not _git_status_clean(repo_path):
        return PullResult(
            outcome=PULL_DIRTY_WORKING_TREE,
            detail=(
                "Local changes detected. Manual intervention required."
            ),
        )

    behind = _git_revlist_count(repo_path, "HEAD..@{u}")
    ahead = _git_revlist_count(repo_path, "@{u}..HEAD")
    if behind > 0 and ahead > 0:
        return PullResult(
            outcome=PULL_DIVERGED,
            detail="Branch diverged. Manual intervention required.",
        )
    if behind == 0:
        # Already up to date (or local-only ahead): there is nothing
        # to fast-forward. Surface this as a structured refusal rather
        # than spawning a no-op pull.
        return PullResult(
            outcome=PULL_NOT_FAST_FORWARD,
            detail="No fast-forward update available.",
        )

    previous_commit = _git_head(repo_path)
    rc, _out, _err = _run_git(
        ["pull", "--ff-only"],
        repo_path=repo_path, timeout=_GIT_PULL_TIMEOUT_SECONDS,
    )
    new_commit = _git_head(repo_path) if rc == 0 else previous_commit
    if rc != 0:
        return PullResult(
            outcome=PULL_FAILED,
            detail="git pull --ff-only failed.",
            previous_commit=previous_commit,
            new_commit=previous_commit,
        )
    return PullResult(
        outcome=PULL_SUCCESS,
        detail="Updated.",
        previous_commit=previous_commit,
        new_commit=new_commit,
    )


def _start_systemctl_user_restart(unit: str) -> tuple[bool, str]:
    """Run ``systemctl --user restart <unit>`` with strict argv.

    Never raises. Returns ``(success, sanitized_detail)``. The detail
    is a short fixed string, never raw stderr.

    Safety properties:
      * argv list, ``shell=False`` — no shell interpretation.
      * absolute systemctl path resolved via ``shutil.which``; we
        never spawn when it is missing.
      * literal ``--user`` flag — no system-level systemctl path.
      * literal ``restart`` verb — no ``stop`` / ``start`` /
        ``daemon-reload`` reachable from this code path.
      * stdin closed; stdout / stderr captured into bounded buffers.
      * short timeout — a hung systemctl maps to failure, not to a
        wedged request.
    """
    binary = _systemctl_path()
    if binary is None:
        return False, "systemctl_not_found"
    if not validate_unit_name(unit):
        return False, "unit_validation_failed"

    argv = [binary, "--user", "restart", unit]
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_SYSTEMCTL_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        logger.debug("systemctl --user restart %s timed out", unit)
        return False, "spawn_timeout"
    except (OSError, ValueError) as exc:
        logger.debug("systemctl --user restart %s failed to spawn: %s", unit, exc)
        return False, "spawn_error"

    if result.returncode != 0:
        logger.debug(
            "systemctl --user restart %s exited with code %s",
            unit, result.returncode,
        )
        return False, "non_zero_exit"
    return True, "spawn_accepted"


def restart() -> RestartResult:
    """Ask systemd-user to restart the configured Nova unit.

    Refusal reasons (no spawn happens for any of these):
      * maintenance is disabled    → ``disabled``
      * restart is not allowed     → ``restart_not_allowed``
      * restart mode is not systemd-user → ``restart_mode_disabled``
      * unit name fails validation → ``invalid_unit``
      * systemctl is not on PATH   → ``systemctl_missing``

    On a successful spawn the result is ``accepted`` — Nova will be
    restarted by systemd-user a moment later. The caller should warn
    the user that the connection will drop while the unit cycles.
    """
    mode = _resolve_restart_mode()
    unit = _resolve_unit()

    if not _resolve_enabled():
        return RestartResult(
            outcome=RESTART_DISABLED, detail="Maintenance disabled.",
            mode=mode, unit=unit,
        )
    if not _resolve_allow_restart():
        return RestartResult(
            outcome=RESTART_NOT_ALLOWED,
            detail="Restart is disabled in the host configuration.",
            mode=mode, unit=unit,
        )
    if mode != RESTART_MODE_SYSTEMD_USER:
        return RestartResult(
            outcome=RESTART_MODE_DISABLED,
            detail="Restart mode is disabled.",
            mode=mode, unit=unit,
        )
    if not validate_unit_name(unit):
        return RestartResult(
            outcome=RESTART_INVALID_UNIT,
            detail="Configured systemd unit name is invalid.",
            mode=mode, unit=unit,
        )
    if _systemctl_path() is None:
        return RestartResult(
            outcome=RESTART_SYSTEMCTL_MISSING,
            detail="systemctl is not available on this host.",
            mode=mode, unit=unit,
        )

    accepted, _detail = _start_systemctl_user_restart(unit)
    if not accepted:
        return RestartResult(
            outcome=RESTART_FAILED,
            detail="Restart could not be started.",
            mode=mode, unit=unit,
        )
    return RestartResult(
        outcome=RESTART_ACCEPTED,
        detail="Restart accepted by systemd.",
        mode=mode, unit=unit,
    )


__all__ = [
    "MaintenanceStatus",
    "PullResult",
    "RestartResult",
    "PULL_DIRTY_WORKING_TREE",
    "PULL_DISABLED",
    "PULL_DIVERGED",
    "PULL_FAILED",
    "PULL_NOT_ALLOWED",
    "PULL_NOT_FAST_FORWARD",
    "PULL_NO_UPSTREAM",
    "PULL_REPO_UNAVAILABLE",
    "PULL_SUCCESS",
    "RESTART_ACCEPTED",
    "RESTART_DISABLED",
    "RESTART_FAILED",
    "RESTART_INVALID_UNIT",
    "RESTART_MODE_DISABLED",
    "RESTART_MODE_OFF",
    "RESTART_MODE_SYSTEMD_USER",
    "RESTART_NOT_ALLOWED",
    "RESTART_SYSTEMCTL_MISSING",
    "STATE_DISABLED",
    "STATE_READY",
    "STATE_UNAVAILABLE",
    "UPDATE_AVAILABLE",
    "UPDATE_DIVERGED",
    "UPDATE_NO_UPSTREAM",
    "UPDATE_UNKNOWN",
    "UPDATE_UP_TO_DATE",
    "disabled_status",
    "fetch",
    "get_status",
    "is_enabled",
    "pull",
    "restart",
    "validate_unit_name",
]
