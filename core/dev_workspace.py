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

Phase 2 — **patch proposal mode** — is also implemented here (see the
"Patch proposal" section near the bottom). It is a *pure* transform: it
turns a structured, model-produced change description into a validated,
review-only :class:`PatchProposal` (title, summary, plan, likely files,
a unified-diff preview, suggested tests, a risk/warning checklist, plus
a transient ``id`` + UTC ``created_at`` stamp). It performs **no** git
calls, **no** file writes, and **no** subprocess work — it never
applies, stages, commits, pushes, or branches anything. Every proposed
path is validated to be repo-relative, traversal-free, non-secret, and
contained inside the linked repo, and any file whose content carries a
NUL byte is refused as binary (no readable text diff possible).
Applying a reviewed patch is a deliberately separate, later phase.

See ``docs/dev-workspace.md`` for the operator walkthrough, the
explicit non-goals, and the Phase 2-6 roadmap.
"""

from __future__ import annotations

import difflib
import logging
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
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


# ════════════════════════════════════════════════════════════════════
# Patch proposal mode (Phase 2)
# ════════════════════════════════════════════════════════════════════
#
# Phase 2 lets Nova *propose* code changes for a linked repo. The model
# describes the change (a plan, the files it would touch with
# before/after content, suggested tests, risks); this module turns that
# into a calm, validated, **review-only** :class:`PatchProposal`:
#
#   * It is a pure transform — no git, no subprocess, no file writes,
#     no network. Nothing is applied, staged, committed, pushed, or
#     branched. The on-disk repo is never touched (``resolve()`` only
#     reads path metadata, exactly as Phase 1 already does).
#   * Every proposed path is validated to be **repo-relative**
#     (absolute paths refused), free of ``..`` traversal, not a
#     secret/private file (``.env``, ``*.db``/``nova.db``, SSH keys,
#     tokens, credentials, logs, backups, exports, ``.git`` internals,
#     …), and contained inside the validated linked repo.
#   * Binary blobs are refused outright (a NUL byte in either
#     ``old_content`` or ``new_content`` means the file is not text and
#     has no useful unified-diff preview); reviewing binary changes is a
#     deliberately later phase.
#   * The diff is produced locally with :mod:`difflib` from the
#     model-supplied before/after text — Nova does not read the working
#     tree to build it, so a proposal cannot leak file contents the
#     model was not already given. Verifying a patch against disk is a
#     deliberately separate, later (apply) phase.
#   * Every field is capped so a runaway model reply cannot balloon the
#     payload, and the result restates that nothing was applied.
#   * Each built proposal carries a transient ``id`` (random UUID) and a
#     UTC ``created_at`` ISO timestamp so the review UI can pin a
#     preview to the exact build it is looking at. No proposal is
#     persisted in this phase — the values are recomputed on every
#     call.


class PatchProposalError(ValueError):
    """Raised when a model-produced patch proposal fails validation.

    The web layer maps this to a 400 with the short, safe message so the
    user learns *why* the proposal was refused instead of getting a 500.
    Never carries secrets, stack traces, or raw model output.
    """


# Caps. A patch proposal is model-produced text; every list and blob is
# bounded so a runaway reply cannot wedge a request or bloat the JSON.
_MAX_PROPOSAL_TITLE_CHARS = 120
_MAX_PROPOSAL_SUMMARY_CHARS = 300
_MAX_PROPOSAL_PLAN_STEPS = 40
_MAX_PROPOSAL_FILES = 50
_MAX_PROPOSAL_FILE_DIFF_LINES = 400
_MAX_PROPOSAL_DIFF_LINES = 800
_MAX_PROPOSAL_TESTS = 40
_MAX_PROPOSAL_RISKS = 40
_MAX_PROPOSAL_CONTENT_CHARS = 200_000

_PATCH_ACTIONS = frozenset({"modify", "add", "delete"})

# Path *segments* (any directory component, or a final component) that a
# proposal may never target: VCS / key / credential stores and the
# private Nova runtime directories (data, db sidecars, backups, exports,
# memory packs, logs). Matched case-insensitively.
_SECRET_SEGMENTS = frozenset(
    {
        ".git",
        ".ssh",
        ".gnupg",
        ".aws",
        ".azure",
        ".kube",
        ".docker",
        "data",
        "novadata",
        "novaportable",
        "backups",
        "exports",
        "memory-packs",
        "logs",
        "__pycache__",
        ".venv",
        "node_modules",
    }
)

# Exact (case-insensitive) basenames that are always secret/private.
_SECRET_BASENAMES = frozenset(
    {
        ".env",
        "nova.env",
        "nova.db",
        "nexus.db",
        ".netrc",
        ".pgpass",
        ".htpasswd",
        ".npmrc",
        ".pypirc",
        ".dockercfg",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "credentials",
        "credentials.json",
        "secrets",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
        "token",
        "tokens",
    }
)

# Basename suffixes that mark data / key / log / backup material. Source
# code (``.py``/``.md``/``.sql``/…) is intentionally *not* here so a
# normal code change is never blocked; only sensitive blobs are.
_SECRET_SUFFIXES = (
    ".db",
    ".sqlite",
    ".sqlite3",
    ".pem",
    ".key",
    ".pfx",
    ".p12",
    ".keystore",
    ".jks",
    ".log",
    ".backup",
    ".bak",
    ".save",
)

# Documented, secret-free ``.env`` companions an operator may legitimately
# want a proposal to touch (samples committed to the repo).
_ENV_SAMPLE_SUFFIXES = (".example", ".sample", ".template", ".dist")

# Fixed, frontend-safe reminder attached to every proposal so the review
# UI (and the model, when this is echoed back) cannot lose the contract.
_PROPOSAL_SAFETY_NOTES = (
    "This is a proposal only — Nova has not modified any file.",
    "Nothing was written to disk, staged, committed, pushed, or branched.",
    "No command was run; the working tree is unchanged.",
    "Review it, then apply it yourself. A later phase will add an "
    "explicit, per-patch approval step before anything is written.",
)


def _looks_binary(content: str) -> bool:
    """True when ``content`` is not safe to preview as a text patch.

    A real text source file never contains a NUL byte, so a NUL is the
    cheapest, most reliable binary signal — and the same heuristic git
    itself uses to flag a file as "Binary". The diff preview is pure
    text, so a binary blob is refused outright in this phase rather than
    surfacing a broken or huge diff. Empty content is trivially text.
    """
    return "\x00" in content


def _is_secret_path(rel_posix: str) -> bool:
    """True when a repo-relative path targets a protected/secret file.

    Conservative on purpose: a false *positive* only refuses one
    proposed edit (the user can still edit that file by hand); a false
    *negative* could surface a secret. Source code is never matched.
    """
    parts = PurePosixPath(rel_posix).parts
    if not parts:
        return True
    for seg in parts:
        if seg.lower() in _SECRET_SEGMENTS:
            return True
    base = parts[-1].lower()
    if base in _SECRET_BASENAMES:
        return True
    # ``.env`` and ``.env.<env>`` are secret; ``.env.example`` and the
    # other documented sample suffixes are explicitly allowed.
    if base == ".env" or base.startswith(".env."):
        if not any(base.endswith(s) for s in _ENV_SAMPLE_SUFFIXES):
            return True
    if any(base.endswith(suff) for suff in _SECRET_SUFFIXES):
        return True
    return False


def validate_proposed_path(repo_root: Path, raw: object) -> str:
    """Validate one model-proposed, repo-relative file path.

    Enforces, in order: present → string → non-empty (trimmed) →
    length-capped → no NUL/newline → no ``~`` → ``/``-separated (no
    ``\\``) → **repo-relative, not absolute** → no ``..`` traversal →
    collapses to a real target → not a secret/private file → resolves
    **inside** ``repo_root``. A leading ``./`` (and other ``.``/empty
    segments) is normalised away; a path that is *only* ``.`` has no
    target and is refused. Returns the normalised POSIX repo-relative
    path on success; raises
    :class:`PatchProposalError` (short, safe message) on the first
    failure. ``repo_root`` must already be the resolved absolute repo
    path from :func:`validate_repo_path`.
    """
    if raw is None or isinstance(raw, bool):
        raise PatchProposalError("proposed file path is required")
    if not isinstance(raw, str):
        raise PatchProposalError("proposed file path must be a string")
    text = raw.strip()
    if not text:
        raise PatchProposalError("proposed file path cannot be empty")
    if len(text) > _MAX_RAW_PATH_CHARS:
        raise PatchProposalError("proposed file path is too long")
    if "\x00" in text or "\n" in text or "\r" in text:
        raise PatchProposalError(
            "proposed file path contains invalid characters"
        )
    if "~" in text:
        raise PatchProposalError(
            "proposed file path must be repo-relative (no '~')"
        )
    if "\\" in text:
        raise PatchProposalError(
            "proposed file path must use '/' separators"
        )

    candidate = PurePosixPath(text)
    if candidate.is_absolute():
        raise PatchProposalError(
            "proposed file path must be repo-relative, not absolute"
        )
    parts = candidate.parts
    if any(p == ".." for p in parts):
        raise PatchProposalError(
            "proposed file path must not contain '..'"
        )
    # ``PurePosixPath`` normalises away ``.`` and empty segments, so a
    # path that is *only* ``.`` (or otherwise collapses to nothing) has
    # no real target.
    if not parts:
        raise PatchProposalError(
            "proposed file path is not a normalised relative path"
        )

    rel = candidate.as_posix()
    if _is_secret_path(rel):
        raise PatchProposalError(
            "proposed file path targets a protected or secret file"
        )

    # Defence in depth: even though ``..``/absolute are already refused,
    # resolve the join so a symlinked directory *inside* the repo that
    # points outside it cannot smuggle an out-of-tree write target into
    # a later apply phase. ``resolve()`` only reads path metadata.
    try:
        joined = (repo_root / rel).resolve()
    except (OSError, RuntimeError, ValueError):
        raise PatchProposalError(
            "proposed file path could not be resolved"
        )
    try:
        inside = joined == repo_root or joined.is_relative_to(repo_root)
    except ValueError:
        inside = False
    if not inside:
        raise PatchProposalError(
            "proposed file path resolves outside the linked repository"
        )
    return rel


@dataclass(frozen=True)
class ProposedFileChange:
    """One reviewed-only change to a single repo-relative file.

    ``diff`` is a unified diff built locally from the model-supplied
    before/after text — it is *not* applied anywhere. ``added`` /
    ``removed`` count the changed body lines (diff headers excluded).
    """

    path: str
    action: str
    diff: str = ""
    added: int = 0
    removed: int = 0

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "action": self.action,
            "diff": self.diff,
            "added": self.added,
            "removed": self.removed,
        }


@dataclass(frozen=True)
class PatchProposal:
    """Calm, frontend-safe, **review-only** patch proposal.

    Holds the implementation plan, the files Nova would change (with a
    capped unified-diff preview each), suggested tests, and a risk
    checklist. Nothing here has been applied; ``as_dict`` always reports
    ``review_only`` / ``applied`` and the standing safety notes so the
    contract travels with the data.

    ``id`` and ``created_at`` are transient metadata stamped when the
    proposal is built (random UUID + UTC ISO timestamp). No proposal is
    persisted in this phase — these fields exist so the UI can keep the
    preview pinned to a specific build while the user reviews it. The
    ``risks`` data is additionally surfaced as ``warnings`` in
    :meth:`as_dict` so the JSON contract matches both the spec wording
    and the existing Phase 2 endpoint.
    """

    repo_path: str
    summary: str = ""
    title: str = ""
    plan: tuple[str, ...] = field(default_factory=tuple)
    files: tuple[ProposedFileChange, ...] = field(default_factory=tuple)
    suggested_tests: tuple[str, ...] = field(default_factory=tuple)
    risks: tuple[str, ...] = field(default_factory=tuple)
    diff_preview: str = ""
    id: str = ""
    created_at: str = ""

    def as_dict(self) -> dict:
        risks = list(self.risks)
        return {
            "review_only": True,
            "applied": False,
            "id": self.id,
            "created_at": self.created_at,
            "repo_path": self.repo_path,
            "title": self.title,
            "summary": self.summary,
            "plan": list(self.plan),
            "files": [f.as_dict() for f in self.files],
            "suggested_tests": list(self.suggested_tests),
            "risks": risks,
            "warnings": list(risks),
            "diff_preview": self.diff_preview,
            "safety": list(_PROPOSAL_SAFETY_NOTES),
        }


def _string_list(value: object, cap: int) -> tuple[str, ...]:
    """Coerce model output into a capped tuple of short, clean strings.

    ``None`` → ``()``; a bare string → a one-item list. Blank entries
    are dropped and each survivor is length-clipped. Any non-string item
    is a hard error so a malformed proposal fails loudly, not silently.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise PatchProposalError("expected a list of strings")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise PatchProposalError("list items must be strings")
        cleaned = item.strip()
        if not cleaned:
            continue
        out.append(_truncate(cleaned))
        if len(out) >= cap:
            break
    return tuple(out)


def _build_file_diff(
    path: str, action: str, old: str, new: str
) -> tuple[str, int, int]:
    """Local unified diff + (added, removed) body-line counts.

    Pure :mod:`difflib`; never reads the working tree. The per-file diff
    is line-capped so one giant file cannot dominate the preview.
    """
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    if action == "add":
        fromfile, tofile = "/dev/null", f"b/{path}"
    elif action == "delete":
        fromfile, tofile = f"a/{path}", "/dev/null"
    else:
        fromfile, tofile = f"a/{path}", f"b/{path}"
    raw = list(
        difflib.unified_diff(
            old_lines, new_lines,
            fromfile=fromfile, tofile=tofile, lineterm="",
        )
    )
    added = removed = 0
    for line in raw:
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    if len(raw) > _MAX_PROPOSAL_FILE_DIFF_LINES:
        raw = raw[:_MAX_PROPOSAL_FILE_DIFF_LINES]
        raw.append("… (file diff truncated for preview)")
    return "\n".join(raw), added, removed


def build_patch_proposal(
    repo_path: str | os.PathLike[str],
    proposal: object,
    *,
    roots: Optional[Sequence[Path]] = None,
) -> PatchProposal:
    """Turn model output into a validated, review-only patch proposal.

    ``repo_path`` is the project's linked checkout; it is re-validated
    here with :func:`validate_repo_path` (same hard rules as Phase 1) so
    a proposal can only ever be scoped to a real, allowed linked repo.
    ``proposal`` is the model's structured description::

        {
          "title": "<short label>",
          "summary": "<one line>",
          "plan": ["step", ...],
          "changes": [
            {"path": "core/foo.py", "action": "modify",
             "old_content": "...", "new_content": "..."},
            ...
          ],
          "tests": ["pytest ...", ...],
          "risks": ["touches auth", ...],     # alias: "warnings"
        }

    Every change's path is validated by :func:`validate_proposed_path`
    (repo-relative, no traversal, non-secret, inside the repo), a binary
    blob (NUL-bearing content) is refused, and the diff is computed
    locally. ``title`` is optional and collapsed to a single safe line;
    ``warnings`` is accepted as a synonym for ``risks``. The built
    proposal is stamped with a transient random ``id`` and UTC
    ``created_at``. **Nothing is applied**; this never writes, spawns a
    process, or touches git. Raises :class:`PatchProposalError` (short,
    safe message) on any validation failure.
    """
    try:
        resolved = validate_repo_path(repo_path, roots=roots)
    except RepoPathError as exc:
        raise PatchProposalError(
            f"linked repository is not usable: {exc}"
        ) from exc

    if not isinstance(proposal, dict):
        raise PatchProposalError("patch proposal must be an object")

    title_raw = proposal.get("title") or ""
    if not isinstance(title_raw, str):
        raise PatchProposalError("title must be a string")
    # Collapse whitespace so the title stays a single safe line.
    title = " ".join(title_raw.split())[:_MAX_PROPOSAL_TITLE_CHARS]

    summary_raw = proposal.get("summary") or ""
    if not isinstance(summary_raw, str):
        raise PatchProposalError("summary must be a string")
    # Collapse whitespace so the summary stays a single safe line.
    summary = " ".join(summary_raw.split())[:_MAX_PROPOSAL_SUMMARY_CHARS]

    plan = _string_list(proposal.get("plan"), _MAX_PROPOSAL_PLAN_STEPS)
    tests = _string_list(proposal.get("tests"), _MAX_PROPOSAL_TESTS)
    # Accept ``risks`` and ``warnings`` interchangeably so a model that
    # follows the spec wording lands in the same field as one that
    # follows the Phase 2 endpoint shape; both are surfaced on output.
    risks_raw = proposal.get("risks")
    if risks_raw is None:
        risks_raw = proposal.get("warnings")
    risks = _string_list(risks_raw, _MAX_PROPOSAL_RISKS)

    raw_changes = proposal.get("changes")
    if not isinstance(raw_changes, (list, tuple)) or not raw_changes:
        raise PatchProposalError(
            "patch proposal must include at least one file change"
        )

    files: list[ProposedFileChange] = []
    seen: set[str] = set()
    for change in raw_changes:
        if not isinstance(change, dict):
            raise PatchProposalError("each change must be an object")
        action = change.get("action") or "modify"
        if not isinstance(action, str) or action not in _PATCH_ACTIONS:
            raise PatchProposalError(
                "change action must be one of: modify, add, delete"
            )
        rel = validate_proposed_path(resolved, change.get("path"))
        if rel in seen:
            raise PatchProposalError(f"duplicate change for path: {rel}")
        seen.add(rel)

        old = change.get("old_content")
        new = change.get("new_content")
        old = "" if (old is None or action == "add") else old
        new = "" if (new is None or action == "delete") else new
        if not isinstance(old, str) or not isinstance(new, str):
            raise PatchProposalError("file content must be a string")
        if _looks_binary(old) or _looks_binary(new):
            # Binary patches are out of scope for the text preview: their
            # diff would be unreadable or huge, and reviewing a binary
            # change as text is unsafe. Refuse outright in this phase.
            raise PatchProposalError(
                f"binary content is not supported for {rel}"
            )
        if (
            len(old) > _MAX_PROPOSAL_CONTENT_CHARS
            or len(new) > _MAX_PROPOSAL_CONTENT_CHARS
        ):
            raise PatchProposalError(
                "proposed file content is too large to preview"
            )
        if action == "modify" and old == new:
            raise PatchProposalError(
                f"modify change for {rel} has no difference"
            )
        if action == "add" and not new:
            raise PatchProposalError(
                f"add change for {rel} has empty content"
            )
        if action == "delete" and not old:
            raise PatchProposalError(
                f"delete change for {rel} has no original content"
            )

        diff, added, removed = _build_file_diff(rel, action, old, new)
        files.append(
            ProposedFileChange(
                path=rel, action=action,
                diff=diff, added=added, removed=removed,
            )
        )
        if len(files) >= _MAX_PROPOSAL_FILES:
            break

    # Combined, capped preview across all files.
    preview_parts: list[str] = []
    preview_lines = 0
    for f in files:
        if not f.diff:
            continue
        block = f.diff.splitlines()
        if preview_lines + len(block) > _MAX_PROPOSAL_DIFF_LINES:
            room = _MAX_PROPOSAL_DIFF_LINES - preview_lines
            if room > 0:
                preview_parts.append("\n".join(block[:room]))
            preview_parts.append("… (combined preview truncated)")
            break
        preview_parts.append(f.diff)
        preview_lines += len(block)

    return PatchProposal(
        repo_path=str(resolved),
        title=title,
        summary=summary,
        plan=plan,
        files=tuple(files),
        suggested_tests=tests,
        risks=risks,
        diff_preview="\n".join(preview_parts),
        id=uuid.uuid4().hex,
        created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
