"""
Optional read-only GitHub connector (issue #119).

Nova is a *local maintainer assistant*, not an autonomous GitHub bot.
This v1 module is intentionally narrow:

  * read-only HTTP calls against the GitHub REST API,
  * admin-gated at the endpoint layer (this module is a pure library
    and is never invoked from chat),
  * a single status snapshot the UI / chat can show calmly,
  * helpers to list / fetch issues and pull requests for a repo.

What this module deliberately does NOT do:

  * create / close issues or PRs,
  * comment, approve, merge, or auto-merge,
  * change repository settings, labels, or permissions,
  * push, force-push, or run any git command,
  * background polling or scheduled maintenance.

Token safety contract (enforced):

  * the token is read from ``config.NOVA_GITHUB_TOKEN`` and never
    returned by any public function;
  * the token is sent only as a request ``Authorization`` header — it
    never appears in URLs, query params, or JSON request bodies;
  * exceptions are logged at debug level by *type* only, never with the
    raw message, so a misbehaving stack trace cannot leak the header;
  * detail strings surfaced to the frontend are short, hard-coded
    summaries — they never include the token, the response body, or the
    exception's repr.

Configuration (env vars, see ``config.py``):

  * ``NOVA_GITHUB_ENABLED``        — host-wide on/off switch.
  * ``NOVA_GITHUB_TOKEN``          — personal access token. Read-only
                                     scopes are enough; v1 never writes.
  * ``NOVA_GITHUB_DEFAULT_REPO``   — optional ``owner/name`` fallback.
  * ``NOVA_GITHUB_READ_ONLY``      — belt-and-braces; defaults True.
  * ``NOVA_GITHUB_TIMEOUT_SECONDS`` — per-request timeout (default 5s).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import httpx

from config import (
    NOVA_GITHUB_DEFAULT_REPO,
    NOVA_GITHUB_ENABLED,
    NOVA_GITHUB_READ_ONLY,
    NOVA_GITHUB_TIMEOUT_SECONDS,
    NOVA_GITHUB_TOKEN,
)

logger = logging.getLogger(__name__)

NAME = "github"

API_BASE_URL = "https://api.github.com"
USER_AGENT = "Nova-GitHub-Connector/1.0"

STATE_DISABLED = "disabled"
STATE_NOT_CONFIGURED = "not_configured"
STATE_UNAVAILABLE = "unavailable"
STATE_CONNECTED = "connected_read_only"

# Caps so a single response can never balloon Nova's memory or chat
# context. GitHub already pages at 30 by default; we enforce a hard
# upper bound of 100 (the GitHub API's own per_page cap).
_MAX_LIMIT = 100
_DEFAULT_LIMIT = 30
# Single-issue / single-PR bodies can be large markdown; truncating
# protects callers that splice the value into a chat prompt. The cap
# is generous (~16 KB) and never silently rewrites — callers see an
# explicit ``"body_truncated": True`` marker when it fires.
_BODY_MAX_CHARS = 16_000

# GitHub permits these characters in owner / repo path segments. The
# regex is anchored on both ends so any rejected slug aborts before
# the HTTP call is built — defence in depth against path injection.
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_REPO_SPEC_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]{0,99})/([A-Za-z0-9][A-Za-z0-9._-]{0,99})$"
)


# ── Status type ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class GitHubStatus:
    """Calm, frontend-safe snapshot of the connector's availability.

    Never includes the token, never includes raw exception text. The
    ``state`` field is one of the four module-level ``STATE_*`` values
    so the UI can branch on a stable enum.
    """

    name: str
    enabled: bool
    state: str
    detail: str = ""
    default_repo: str = ""
    read_only: bool = True
    authenticated_login: Optional[str] = None
    scopes: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "state": self.state,
            "detail": self.detail,
            "default_repo": self.default_repo,
            "read_only": self.read_only,
            "authenticated_login": self.authenticated_login,
            "scopes": list(self.scopes),
        }


# ── Switch helpers ──────────────────────────────────────────────────


def is_enabled() -> bool:
    """True when the host operator has flipped the env switch on."""
    return bool(NOVA_GITHUB_ENABLED)


def is_read_only() -> bool:
    """Always True in v1; reflects ``NOVA_GITHUB_READ_ONLY``."""
    return bool(NOVA_GITHUB_READ_ONLY)


def _has_token() -> bool:
    return bool(NOVA_GITHUB_TOKEN)


def _headers() -> dict:
    """Build the request headers. Never returned to callers."""
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {NOVA_GITHUB_TOKEN}",
    }


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=API_BASE_URL,
        timeout=NOVA_GITHUB_TIMEOUT_SECONDS,
        headers=_headers(),
    )


def _log_error(where: str, exc: BaseException) -> None:
    """Log an exception by type only so the token cannot leak via repr."""
    logger.debug("github %s failed: %s", where, type(exc).__name__)


# ── Repo helpers ────────────────────────────────────────────────────


def parse_repo_spec(spec: Optional[str]) -> Optional[tuple[str, str]]:
    """Validate ``owner/name`` and return the pair, or ``None`` if invalid.

    Used by the endpoint layer so a caller cannot smuggle a path
    fragment via the ``repo`` query param.
    """
    if not spec or not isinstance(spec, str):
        return None
    match = _REPO_SPEC_RE.match(spec.strip())
    if match is None:
        return None
    return match.group(1), match.group(2)


def resolve_repo(spec: Optional[str]) -> Optional[tuple[str, str]]:
    """Return the validated ``(owner, name)`` for the requested spec.

    Falls back to ``NOVA_GITHUB_DEFAULT_REPO`` when ``spec`` is empty.
    ``None`` means neither the request nor the host config provided a
    usable repo — the caller surfaces that as a 400.
    """
    if spec:
        return parse_repo_spec(spec)
    return parse_repo_spec(NOVA_GITHUB_DEFAULT_REPO)


def _valid_slug(slug: str) -> bool:
    return isinstance(slug, str) and _SLUG_RE.match(slug) is not None


def _valid_number(number) -> bool:
    try:
        return int(number) > 0
    except (TypeError, ValueError):
        return False


def _clamp_limit(limit: int) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT
    if n <= 0:
        return _DEFAULT_LIMIT
    return min(n, _MAX_LIMIT)


def _valid_state(state: str) -> str:
    """Coerce the optional GitHub ``state`` filter to a known value."""
    if isinstance(state, str) and state.lower() in ("open", "closed", "all"):
        return state.lower()
    return "open"


# ── Status ──────────────────────────────────────────────────────────


def status() -> GitHubStatus:
    """Report whether the connector is on and reachable. Never raises.

    Off                                  → ``disabled``.
    On + no token                        → ``not_configured``.
    On + token + ``GET /user`` 2xx       → ``connected_read_only``.
    On + token + any HTTP / network err  → ``unavailable`` with a
                                           short, sanitised detail.
    """
    if not is_enabled():
        return GitHubStatus(
            name=NAME, enabled=False, state=STATE_DISABLED,
            detail="Set NOVA_GITHUB_ENABLED=true to turn the connector on.",
            default_repo=NOVA_GITHUB_DEFAULT_REPO,
            read_only=is_read_only(),
        )
    if not _has_token():
        return GitHubStatus(
            name=NAME, enabled=True, state=STATE_NOT_CONFIGURED,
            detail="NOVA_GITHUB_TOKEN is not set on this Nova host.",
            default_repo=NOVA_GITHUB_DEFAULT_REPO,
            read_only=is_read_only(),
        )
    try:
        with _client() as client:
            resp = client.get("/user")
    except (httpx.HTTPError, OSError) as exc:
        _log_error("status", exc)
        return GitHubStatus(
            name=NAME, enabled=True, state=STATE_UNAVAILABLE,
            detail="GitHub API is not reachable.",
            default_repo=NOVA_GITHUB_DEFAULT_REPO,
            read_only=is_read_only(),
        )
    if resp.status_code == 401:
        return GitHubStatus(
            name=NAME, enabled=True, state=STATE_UNAVAILABLE,
            detail="GitHub rejected the configured token.",
            default_repo=NOVA_GITHUB_DEFAULT_REPO,
            read_only=is_read_only(),
        )
    if not (200 <= resp.status_code < 300):
        return GitHubStatus(
            name=NAME, enabled=True, state=STATE_UNAVAILABLE,
            detail=f"GitHub returned HTTP {resp.status_code}.",
            default_repo=NOVA_GITHUB_DEFAULT_REPO,
            read_only=is_read_only(),
        )
    login: Optional[str] = None
    try:
        body = resp.json()
        if isinstance(body, dict):
            raw_login = body.get("login")
            if isinstance(raw_login, str):
                login = raw_login
    except ValueError as exc:
        _log_error("status decode", exc)
    scopes = _parse_scopes(resp.headers.get("X-OAuth-Scopes"))
    return GitHubStatus(
        name=NAME, enabled=True, state=STATE_CONNECTED,
        detail=(
            f"Authenticated as {login}." if login
            else "Authenticated read-only access."
        ),
        default_repo=NOVA_GITHUB_DEFAULT_REPO,
        read_only=is_read_only(),
        authenticated_login=login,
        scopes=scopes,
    )


def _parse_scopes(header: Optional[str]) -> tuple[str, ...]:
    if not isinstance(header, str) or not header.strip():
        return ()
    return tuple(s.strip() for s in header.split(",") if s.strip())


# ── Read API ────────────────────────────────────────────────────────


def list_issues(
    owner: str,
    repo: str,
    state: str = "open",
    limit: int = _DEFAULT_LIMIT,
) -> list[dict]:
    """Return sanitised issues for ``owner/repo``.

    Empty list when the connector is off / not configured / unreachable,
    when the slugs fail validation, or when GitHub returns a non-2xx
    response. ``state`` accepts ``open`` / ``closed`` / ``all``.

    Note: GitHub's ``/issues`` endpoint also returns pull requests; we
    drop those from this list and let callers use :func:`list_pull_requests`
    for PR data instead.
    """
    if not is_enabled() or not _has_token():
        return []
    if not (_valid_slug(owner) and _valid_slug(repo)):
        return []
    params = {
        "state": _valid_state(state),
        "per_page": _clamp_limit(limit),
    }
    try:
        with _client() as client:
            resp = client.get(f"/repos/{owner}/{repo}/issues", params=params)
        if not (200 <= resp.status_code < 300):
            return []
        data = resp.json()
    except (httpx.HTTPError, OSError, ValueError) as exc:
        _log_error("list_issues", exc)
        return []
    if not isinstance(data, list):
        return []
    issues = []
    for item in data:
        if not isinstance(item, dict):
            continue
        # GitHub flags PRs by including a ``pull_request`` key; skip them.
        if "pull_request" in item:
            continue
        issues.append(_sanitize_issue(item))
    return issues


def get_issue(owner: str, repo: str, number) -> Optional[dict]:
    """Return one sanitised issue, or ``None`` on any failure path."""
    if not is_enabled() or not _has_token():
        return None
    if not (_valid_slug(owner) and _valid_slug(repo) and _valid_number(number)):
        return None
    try:
        with _client() as client:
            resp = client.get(f"/repos/{owner}/{repo}/issues/{int(number)}")
        if not (200 <= resp.status_code < 300):
            return None
        data = resp.json()
    except (httpx.HTTPError, OSError, ValueError) as exc:
        _log_error("get_issue", exc)
        return None
    if not isinstance(data, dict):
        return None
    # If GitHub hands back a PR via the issues endpoint, surface it as
    # such; the caller can decide whether to redirect.
    if "pull_request" in data:
        return None
    return _sanitize_issue(data, include_body=True)


def list_pull_requests(
    owner: str,
    repo: str,
    state: str = "open",
    limit: int = _DEFAULT_LIMIT,
) -> list[dict]:
    """Return sanitised pull requests for ``owner/repo``. Empty on failure."""
    if not is_enabled() or not _has_token():
        return []
    if not (_valid_slug(owner) and _valid_slug(repo)):
        return []
    params = {
        "state": _valid_state(state),
        "per_page": _clamp_limit(limit),
    }
    try:
        with _client() as client:
            resp = client.get(f"/repos/{owner}/{repo}/pulls", params=params)
        if not (200 <= resp.status_code < 300):
            return []
        data = resp.json()
    except (httpx.HTTPError, OSError, ValueError) as exc:
        _log_error("list_pull_requests", exc)
        return []
    if not isinstance(data, list):
        return []
    return [_sanitize_pr(item) for item in data if isinstance(item, dict)]


def get_pull_request(owner: str, repo: str, number) -> Optional[dict]:
    """Return one sanitised pull request, or ``None`` on any failure."""
    if not is_enabled() or not _has_token():
        return None
    if not (_valid_slug(owner) and _valid_slug(repo) and _valid_number(number)):
        return None
    try:
        with _client() as client:
            resp = client.get(f"/repos/{owner}/{repo}/pulls/{int(number)}")
        if not (200 <= resp.status_code < 300):
            return None
        data = resp.json()
    except (httpx.HTTPError, OSError, ValueError) as exc:
        _log_error("get_pull_request", exc)
        return None
    if not isinstance(data, dict):
        return None
    return _sanitize_pr(data, include_body=True)


# ── Summary helper ──────────────────────────────────────────────────


def summarize_repo_activity(
    owner: str, repo: str, limit: int = _DEFAULT_LIMIT
) -> dict:
    """Read-only roll-up of open issues + PRs for ``owner/repo``.

    Returns a plain dict the chat layer or UI can surface without
    further processing. Never raises; failure paths yield zeroed
    counts and empty lists so the caller can render a calm summary.
    """
    issues = list_issues(owner, repo, state="open", limit=limit)
    pulls = list_pull_requests(owner, repo, state="open", limit=limit)
    return {
        "repo": f"{owner}/{repo}",
        "open_issues": len(issues),
        "open_pull_requests": len(pulls),
        "issues": issues,
        "pull_requests": pulls,
        "read_only": True,
    }


# ── Sanitisers ──────────────────────────────────────────────────────


def _truncate_body(body) -> tuple[Optional[str], bool]:
    if not isinstance(body, str):
        return None, False
    if len(body) <= _BODY_MAX_CHARS:
        return body, False
    return body[:_BODY_MAX_CHARS], True


def _labels(item: dict) -> list[str]:
    labels = item.get("labels") or []
    if not isinstance(labels, list):
        return []
    out = []
    for label in labels:
        if isinstance(label, dict):
            name = label.get("name")
            if isinstance(name, str):
                out.append(name)
        elif isinstance(label, str):
            out.append(label)
    return out


def _login(item: dict, key: str = "user") -> Optional[str]:
    sub = item.get(key)
    if isinstance(sub, dict):
        login = sub.get("login")
        if isinstance(login, str):
            return login
    return None


def _sanitize_issue(item: dict, include_body: bool = False) -> dict:
    out: dict = {
        "number": item.get("number"),
        "title": item.get("title"),
        "state": item.get("state"),
        "user": _login(item),
        "labels": _labels(item),
        "comments": item.get("comments"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "closed_at": item.get("closed_at"),
        "html_url": item.get("html_url"),
    }
    if include_body:
        body, truncated = _truncate_body(item.get("body"))
        out["body"] = body
        out["body_truncated"] = truncated
    return out


def _sanitize_pr(item: dict, include_body: bool = False) -> dict:
    head = item.get("head") if isinstance(item.get("head"), dict) else {}
    base = item.get("base") if isinstance(item.get("base"), dict) else {}
    out: dict = {
        "number": item.get("number"),
        "title": item.get("title"),
        "state": item.get("state"),
        "draft": item.get("draft"),
        "merged": item.get("merged"),
        "user": _login(item),
        "labels": _labels(item),
        "head": head.get("ref"),
        "base": base.get("ref"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "closed_at": item.get("closed_at"),
        "merged_at": item.get("merged_at"),
        "html_url": item.get("html_url"),
    }
    if include_body:
        body, truncated = _truncate_body(item.get("body"))
        out["body"] = body
        out["body_truncated"] = truncated
    return out
