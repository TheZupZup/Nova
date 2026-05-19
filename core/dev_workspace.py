"""
Dev Workspace — read-only local Git context for Nova Projects (Phase 1).

A Nova *project* can optionally be linked to a local Git checkout so
Nova can understand the repository's state (branch, clean/dirty,
recent commits, changed files) when helping the user code. Phase 1 is
**strictly read-only**: this module can *observe* a repo, never modify
one.

What this module is *not*:

  * a command runner or web terminal,
  * a way to commit / push / branch / write files,
  * a GitHub / Codeberg client,
  * a filesystem scanner,
  * a privilege-escalation helper.

Safety contract (enforced here):

  * **Opt-in by the operator.** A repo can only be linked when it
    resolves *inside* one of the absolute directories listed in
    ``NOVA_DEV_WORKSPACE_ROOTS``. Unset / empty → the feature is off
    and no path validates. Nova never invents an allowed root.
  * **Hard path validation.** The path must exist, be a directory,
    contain ``.git``, resolve (symlinks included) within an allowed
    root, and must not be the filesystem root, a top-level directory,
    or any broad system path (``/``, ``/home``, ``/mnt``, ``/etc`` …).
    ``..`` traversal is refused outright.
  * **Allowlisted git only.** Every subprocess is built from a
    hard-coded argv tuple drawn from a frozen allowlist of read-only
    subcommands (``status --short``, ``branch --show-current``,
    ``log --oneline -n 20``, ``diff --stat``, ``status --porcelain``).
    No user / model / chat input is ever concatenated into argv; the
    only varying value is the validated repo path, used solely as the
    process *cwd* — never inside the command.
  * **``shell=False`` everywhere.** No string commands, no shell
    interpretation. ``stdin`` is closed; ``GIT_TERMINAL_PROMPT=0`` and
    ``GIT_OPTIONAL_LOCKS=0`` keep git non-interactive and lock-free so
    a read can never prompt, hang, or write a lock.
  * **No network subcommands.** ``fetch`` / ``pull`` / ``clone`` /
    ``remote`` are not in the allowlist, so Phase 1 makes no network
    calls and performs no background scans.
  * **No ``sudo`` / ``pkexec`` / ``doas`` / ``su`` / ``runuser``.**
  * **Timeouts + caps.** Every call has a small timeout; every list
    field is line- and length-capped so a huge repo cannot wedge a
    request or balloon the JSON payload.
  * **Calm errors.** Snapshots never raise and never embed secrets,
    environment variables, raw stderr, or stack traces — only short,
    fixed, frontend-safe summaries.

This is the single module in ``core`` allowed to import ``subprocess``
for dev-workspace git reads. The web layer must call only the public
functions here and keep them user-scoped.

See ``docs/dev-workspace.md`` for the operator walkthrough, the
explicit non-goals, and the Phase 2-6 roadmap.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

# ── Configuration ───────────────────────────────────────────────────
#
# ``NOVA_DEV_WORKSPACE_ROOTS`` is an OS-path-separator- or
# comma-separated list of absolute directories that are permitted to
# contain linkable repositories. Read fresh on every call so a
# tests-time ``monkeypatch.setenv`` propagates without a reload, and so
# an operator changing the env (then restarting) takes effect cleanly.
ENV_ROOTS = "NOVA_DEV_WORKSPACE_ROOTS"

# Internal limits. All dev-workspace git calls are local and read-only,
# so a single small timeout is enough; a misbehaving repo maps to a
# calm error snapshot rather than a wedged request.
_GIT_TIMEOUT_SECONDS = 5.0

_MAX_LOG_LINES = 20          # mirrors ``git log --oneline -n 20``
_MAX_STATUS_LINES = 200
_MAX_DIFF_LINES = 100
_MAX_CHANGED_FILES = 200
_MAX_LINE_CHARS = 300
_MAX_RAW_PATH_CHARS = 4096

# Resolved absolute paths a repo may *never* be, regardless of the
# configured roots. An operator who points a root at one of these (or
# at ``/``) still cannot link it — the denylist wins. The "top-level
# directory" check below additionally refuses any first-level path
# (parent == filesystem anchor), covering broad roots not named here.
_DENY_EXACT = frozenset(
    {
        "/",
        "/home",
        "/mnt",
        "/media",
        "/root",
        "/etc",
        "/usr",
        "/bin",
        "/sbin",
        "/lib",
        "/lib32",
        "/lib64",
        "/var",
        "/boot",
        "/dev",
        "/proc",
        "/sys",
        "/opt",
        "/srv",
        "/run",
        "/tmp",
    }
)

# The complete set of git subcommands this module may ever spawn. Each
# entry is the *exact* argv tail (everything after the git binary).
# ``_run_git`` refuses anything not in this set, so the module cannot
# be coaxed into a write, a network call, or an arbitrary command even
# by a future code change that forgets the contract.
_ALLOWED_GIT_ARGV: frozenset[tuple[str, ...]] = frozenset(
    {
        ("status", "--short"),
        ("status", "--porcelain"),
        ("branch", "--show-current"),
        ("log", "--oneline", "-n", "20"),
        ("diff", "--stat"),
    }
)

# Calm, frontend-safe ``state`` values.
STATE_DISABLED = "disabled"        # no allowed roots configured
STATE_INVALID_PATH = "invalid_path"  # stored path no longer validates
STATE_GIT_UNAVAILABLE = "git_unavailable"  # git binary missing
STATE_ERROR = "error"              # git ran but a core read failed
STATE_READY = "ready"              # snapshot populated


class RepoPathError(ValueError):
    """Raised when a candidate repo path fails validation.

    The web layer maps this to a 400 with the (short, safe) message so
    the user sees *why* the path was refused instead of a 500.
    """


# ── Allowed-roots resolution ────────────────────────────────────────


def _split_roots(raw: str) -> list[str]:
    """Split the raw env value on the OS path separator and commas.

    Both separators are accepted so the variable is forgiving to copy
    from a shell ``PATH``-style list or a comma list in a ``.env``.
    """
    parts: list[str] = []
    for chunk in raw.split(os.pathsep):
        parts.extend(chunk.split(","))
    return [p.strip() for p in parts if p.strip()]


def configured_roots() -> tuple[Path, ...]:
    """Return the resolved, absolute allowed workspace roots.

    Read from ``NOVA_DEV_WORKSPACE_ROOTS`` on every call. Entries that
    are blank, relative, or unresolvable are dropped silently — Nova
    never treats an ambiguous value as "allow everything". The result
    is de-duplicated while preserving order.
    """
    raw = os.environ.get(ENV_ROOTS, "")
    if not raw or not raw.strip():
        return ()
    seen: set[str] = set()
    roots: list[Path] = []
    for entry in _split_roots(raw):
        try:
            candidate = Path(entry).expanduser()
        except (OSError, RuntimeError, ValueError):
            continue
        if not candidate.is_absolute():
            continue
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        key = str(resolved)
        if key in seen:
            continue
        # A root that is itself a denied/top-level path can never make
        # anything linkable; drop it so it cannot widen the surface.
        if key in _DENY_EXACT or _is_top_level(resolved):
            continue
        seen.add(key)
        roots.append(resolved)
    return tuple(roots)


def feature_enabled() -> bool:
    """True only when at least one usable allowed root is configured."""
    return len(configured_roots()) > 0


# ── Path validation ─────────────────────────────────────────────────


def _is_top_level(path: Path) -> bool:
    """True when ``path`` is a first-level directory (parent is ``/``).

    ``/home``, ``/mnt``, ``/srv`` … all have the filesystem anchor as
    their parent. Refusing these blocks broad roots even when they are
    not in the explicit denylist.
    """
    return path.parent == Path(path.anchor)


def _within_a_root(path: Path, roots: Sequence[Path]) -> bool:
    """True when ``path`` equals or is nested under an allowed root."""
    for root in roots:
        try:
            if path == root or path.is_relative_to(root):
                return True
        except ValueError:
            continue
    return False


def validate_repo_path(
    raw: str | os.PathLike[str],
    *,
    roots: Optional[Sequence[Path]] = None,
) -> Path:
    """Validate a candidate local repo path and return its resolved form.

    Enforces, in order: non-empty clean string → absolute → no ``..``
    traversal → resolves to an existing directory → contains ``.git``
    → not the filesystem root / a top-level dir / a denied system path
    → at least one allowed root is configured → resolves inside an
    allowed root. Raises :class:`RepoPathError` (short, safe message)
    on the first failure; returns the fully resolved absolute path on
    success.

    ``roots`` is overridable for tests; production passes ``None`` and
    the function reads :func:`configured_roots`.
    """
    if raw is None or isinstance(raw, bool):
        raise RepoPathError("repository path is required")
    try:
        text = os.fspath(raw)
    except TypeError:
        raise RepoPathError("repository path must be a string")
    if not isinstance(text, str):
        raise RepoPathError("repository path must be a string")
    text = text.strip()
    if not text:
        raise RepoPathError("repository path cannot be empty")
    if len(text) > _MAX_RAW_PATH_CHARS:
        raise RepoPathError("repository path is too long")
    if "\x00" in text or "\n" in text or "\r" in text:
        raise RepoPathError("repository path contains invalid characters")
    if "~" in text:
        # No home-directory expansion: keep the allowed-root containment
        # check honest and predictable.
        raise RepoPathError("repository path must be an absolute path")

    candidate = Path(text)
    if not candidate.is_absolute():
        raise RepoPathError("repository path must be an absolute path")
    if ".." in candidate.parts:
        raise RepoPathError("repository path must not contain '..'")

    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        raise RepoPathError("repository path could not be resolved")

    if str(resolved) in _DENY_EXACT:
        raise RepoPathError(
            "repository path is a protected system directory"
        )
    if _is_top_level(resolved):
        raise RepoPathError(
            "repository path must not be a top-level directory"
        )

    try:
        if not resolved.is_dir():
            raise RepoPathError("repository path does not exist")
    except OSError:
        raise RepoPathError("repository path could not be read")

    git_marker = resolved / ".git"
    try:
        has_git = git_marker.exists()
    except OSError:
        has_git = False
    if not has_git:
        raise RepoPathError("repository path is not a Git checkout")

    active_roots = (
        tuple(roots) if roots is not None else configured_roots()
    )
    if not active_roots:
        raise RepoPathError(
            "no allowed workspace roots are configured "
            f"(set {ENV_ROOTS})"
        )
    if not _within_a_root(resolved, active_roots):
        raise RepoPathError(
            "repository path is outside the allowed workspace roots"
        )

    return resolved


def is_valid_repo_path(
    raw: str | os.PathLike[str],
    *,
    roots: Optional[Sequence[Path]] = None,
) -> bool:
    """Boolean convenience wrapper around :func:`validate_repo_path`."""
    try:
        validate_repo_path(raw, roots=roots)
        return True
    except RepoPathError:
        return False


# ── Subprocess primitive ────────────────────────────────────────────


def _git_path() -> Optional[str]:
    """Absolute path to the git binary, or ``None`` when unavailable."""
    return shutil.which("git")


def _git_env() -> dict[str, str]:
    """A copy of the environment hardened for non-interactive reads.

    ``GIT_TERMINAL_PROMPT=0`` guarantees git never blocks on a
    credential / passphrase prompt. ``GIT_OPTIONAL_LOCKS=0`` keeps even
    ``status`` from taking a repository lock, so a Nova read can never
    interfere with a concurrent human ``git`` session.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def _run_git(
    argv_tail: Sequence[str],
    *,
    repo_path: str,
    timeout: float = _GIT_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    """Run ``git <argv_tail>`` in ``repo_path`` with a strict allowlist.

    ``argv_tail`` must be exactly one of :data:`_ALLOWED_GIT_ARGV`; any
    other value is a programming error and raises ``ValueError`` (it
    can never originate from user input — callers pass literals). The
    function otherwise never raises: a missing binary or a timeout maps
    to ``(-1, "", "")`` so callers branch on the rc. ``shell=False`` is
    mandatory, stdin is closed, the repo path is only ever the *cwd*.
    """
    key = tuple(argv_tail)
    if key not in _ALLOWED_GIT_ARGV:
        raise ValueError(f"git argv not allowlisted: {key!r}")

    binary = _git_path()
    if binary is None:
        return -1, "", ""
    argv = [binary, *key]
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
            env=_git_env(),
        )
    except subprocess.TimeoutExpired:
        logger.debug("dev-workspace git %s timed out", key)
        return -1, "", ""
    except (OSError, ValueError) as exc:
        logger.debug("dev-workspace git %s failed to spawn: %s", key, exc)
        return -1, "", ""
    stdout = (result.stdout or b"").decode("utf-8", errors="replace")
    stderr = (result.stderr or b"").decode("utf-8", errors="replace")
    return result.returncode, stdout, stderr


def _truncate(line: str) -> str:
    """Clip one output line so a pathological repo cannot bloat JSON."""
    if len(line) <= _MAX_LINE_CHARS:
        return line
    return line[:_MAX_LINE_CHARS] + "…"


def _lines(out: str, cap: int) -> tuple[str, ...]:
    rows = [_truncate(s) for s in out.splitlines() if s.strip()]
    return tuple(rows[:cap])


# ── Read-only git helpers ───────────────────────────────────────────


def git_current_branch(repo_path: str) -> str:
    """Current branch name, or ``""`` (detached HEAD / failure)."""
    rc, out, _ = _run_git(["branch", "--show-current"], repo_path=repo_path)
    return out.strip() if rc == 0 else ""


def git_status_short(repo_path: str) -> tuple[str, ...]:
    """``git status --short`` lines, capped."""
    rc, out, _ = _run_git(["status", "--short"], repo_path=repo_path)
    if rc != 0:
        return ()
    return _lines(out, _MAX_STATUS_LINES)


def git_is_clean(repo_path: str) -> bool:
    """True when the working tree is clean.

    An unreadable status is reported as *dirty* — Nova never claims a
    tree is clean when it could not actually inspect it.
    """
    rc, out, _ = _run_git(["status", "--porcelain"], repo_path=repo_path)
    if rc != 0:
        return False
    return out.strip() == ""


def git_log_oneline(repo_path: str) -> tuple[str, ...]:
    """Up to the latest 20 commits as ``<sha> <subject>`` lines."""
    rc, out, _ = _run_git(
        ["log", "--oneline", "-n", "20"], repo_path=repo_path
    )
    if rc != 0:
        return ()
    return _lines(out, _MAX_LOG_LINES)


def git_diff_stat(repo_path: str) -> tuple[str, ...]:
    """``git diff --stat`` (unstaged working-tree changes), capped."""
    rc, out, _ = _run_git(["diff", "--stat"], repo_path=repo_path)
    if rc != 0:
        return ()
    return _lines(out, _MAX_DIFF_LINES)


def git_changed_files(repo_path: str) -> tuple[dict, ...]:
    """Changed paths parsed from ``git status --porcelain``.

    Returns ``{"status": <2-char code, trimmed>, "path": <path>}``
    entries (staged, unstaged, and untracked), capped. Rename entries
    keep git's ``old -> new`` text in ``path``. Read-only: this only
    parses ``status`` output, it never touches the index.
    """
    rc, out, _ = _run_git(["status", "--porcelain"], repo_path=repo_path)
    if rc != 0:
        return ()
    entries: list[dict] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        code = line[:2].strip()
        path = _truncate(line[3:].strip()) if len(line) > 3 else ""
        if not path:
            continue
        entries.append({"status": code, "path": path})
        if len(entries) >= _MAX_CHANGED_FILES:
            break
    return tuple(entries)


# ── Snapshot ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RepoStatus:
    """Calm, frontend-safe snapshot of a linked repo's Git state.

    ``state`` is one of the module ``STATE_*`` values. Only when it is
    :data:`STATE_READY` are the repo fields meaningful; otherwise they
    stay at their defaults so the response shape is stable. No env
    vars, raw stderr, or absolute paths beyond the validated
    ``repo_path`` are ever included.
    """

    state: str
    repo_path: str = ""
    branch: str = ""
    clean: bool = True
    status_short: tuple[str, ...] = field(default_factory=tuple)
    diff_stat: tuple[str, ...] = field(default_factory=tuple)
    changed_files: tuple[dict, ...] = field(default_factory=tuple)
    recent_commits: tuple[str, ...] = field(default_factory=tuple)
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "repo_path": self.repo_path,
            "branch": self.branch,
            "clean": self.clean,
            "status_short": list(self.status_short),
            "diff_stat": list(self.diff_stat),
            "changed_files": [dict(e) for e in self.changed_files],
            "recent_commits": list(self.recent_commits),
            "detail": self.detail,
        }


def read_status(
    raw_path: str | os.PathLike[str],
    *,
    roots: Optional[Sequence[Path]] = None,
) -> RepoStatus:
    """Validate ``raw_path`` and return a read-only :class:`RepoStatus`.

    Never raises. The stored path is re-validated here (defence in
    depth) so a repo that was moved/removed, or a workspace-root config
    that changed since the link was created, degrades to a calm
    ``invalid_path`` snapshot instead of an error or stale data.
    """
    if not feature_enabled() and roots is None:
        return RepoStatus(
            state=STATE_DISABLED,
            detail="Dev Workspace is not enabled on this host.",
        )
    try:
        resolved = validate_repo_path(raw_path, roots=roots)
    except RepoPathError as exc:
        return RepoStatus(state=STATE_INVALID_PATH, detail=str(exc))

    repo = str(resolved)
    if _git_path() is None:
        return RepoStatus(
            state=STATE_GIT_UNAVAILABLE,
            repo_path=repo,
            detail="git is not available on this host.",
        )

    branch = git_current_branch(repo)
    clean = git_is_clean(repo)
    status_short = git_status_short(repo)
    diff_stat = git_diff_stat(repo)
    changed_files = git_changed_files(repo)
    recent_commits = git_log_oneline(repo)

    # ``log`` failing on a brand-new repo with no commits is normal and
    # not an error; a failing ``status`` (clean forced False with no
    # detail) means we could not inspect the tree at all.
    rc, _, _ = _run_git(["status", "--porcelain"], repo_path=repo)
    if rc != 0:
        return RepoStatus(
            state=STATE_ERROR,
            repo_path=repo,
            branch=branch,
            detail="Could not read the repository's Git status.",
        )

    return RepoStatus(
        state=STATE_READY,
        repo_path=repo,
        branch=branch,
        clean=clean,
        status_short=status_short,
        diff_stat=diff_stat,
        changed_files=changed_files,
        recent_commits=recent_commits,
        detail="" if clean else "Working tree has uncommitted changes.",
    )
