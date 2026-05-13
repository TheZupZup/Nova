"""
Read-only maintainer triage / recommendation helper (issue #119 follow-up).

This module sits on top of the read-only GitHub connector and turns the
sanitised issue list into a short ranked list of *suggested* things a
maintainer might want to work on. Nova is a local assistant — never an
autonomous bot — so every decision here is deterministic, explainable,
and harmless:

  * pure-Python scoring on the output of
    ``core.integrations.github.list_issues`` (no extra HTTP calls);
  * no LLM call, no background polling, no scheduled work;
  * never mutates GitHub state in any form — no create, close, comment,
    label edit, assignment, merge, approve, or repo-settings change;
  * never persists anything to disk or the database;
  * the configured GitHub token is held only inside the underlying
    connector — this layer never sees it.

The maintainer always picks what to work on. Nova's role is to
surface a short, calmly-ranked list with explanations so the human
can choose; nothing here starts work, claims an issue, or signals an
assignee to GitHub.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable, Optional

from . import github as _gh

logger = logging.getLogger(__name__)

NAME = "github_triage"

DIFFICULTY_LOW = "low"
DIFFICULTY_MEDIUM = "medium"
DIFFICULTY_HIGH = "high"
DIFFICULTY_ALLOWED = (DIFFICULTY_LOW, DIFFICULTY_MEDIUM, DIFFICULTY_HIGH)

CONFIDENCE_LOW = "low"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_HIGH = "high"

# Caps mirror the underlying connector so a single response can never
# balloon Nova's chat context. The pool is intentionally larger than
# the visible top-N so the ranking has a healthy candidate set.
_DEFAULT_LIMIT = 5
_MAX_LIMIT = 25
_FETCH_POOL_DEFAULT = 100


# ── Label dictionaries ──────────────────────────────────────────────
# Entries are stored in the canonical normalised form (lowercase, with
# both spaces and underscores collapsed to ``-``) so every lookup is
# routed through ``_norm_label`` and matches regardless of how the
# upstream GitHub label is punctuated.

LOW_DIFFICULTY_LABELS = frozenset({
    "good-first-issue",
    "beginner",
    "starter",
    "easy",
    "docs",
    "documentation",
    "tests",
    "test",
    "testing",
    "typo",
    "chore",
    "cleanup",
    "ui",
    "ux",
    "polish",
})

HIGH_DIFFICULTY_LABELS = frozenset({
    "architecture",
    "refactor",
    "rewrite",
    "migration",
    "breaking-change",
    "performance",
    "perf",
    "complex",
    "major",
    "epic",
})

# Labels that should always carry an explicit risk / caution note.
# These do not block recommendation — a security fix may be exactly
# what the maintainer wants to do — but the response surfaces the
# heightened stakes so the human reads carefully before starting.
RISK_LABELS = frozenset({
    "security",
    "auth",
    "authentication",
    "admin",
    "github",
    "memory",
    "secrets",
    "crypto",
    "permissions",
    "privacy",
    "data-loss",
})

# Labels that mean "don't ship this without a maintainer call". We
# exclude these from recommendations entirely so they do not sit at
# the top of a "what should I do next?" list.
NON_ACTIONABLE_LABELS = frozenset({
    "wontfix",
    "won't-fix",
    "wont-fix",
    "invalid",
    "duplicate",
    "blocked",
    "stale",
    "spam",
})

# Labels that suggest the issue is still in conversation. We still
# surface these, but tag them as needing clarification rather than
# coding work.
NEEDS_CLARIFICATION_LABELS = frozenset({
    "needs-design",
    "needs-discussion",
    "needs-triage",
    "discussion",
    "question",
    "rfc",
})

# Generic / vague title tokens. If the title is *only* these words we
# mark the issue as needing clarification rather than starting work.
VAGUE_TITLE_TOKENS = frozenset({
    "bug",
    "issue",
    "help",
    "question",
    "fix",
    "fixme",
    "broken",
    "error",
    "problem",
    "todo",
    "wip",
    "tbd",
})

# Acceptance-criteria markers. Their presence in an issue body is a
# strong actionability signal; the maintainer can start without having
# to chase down what "done" means.
_AC_MARKERS = (
    "acceptance criteria",
    "definition of done",
    "ac:",
    "- [ ]",
    "tasks:",
    "expected behaviour",
    "expected behavior",
    "steps to reproduce",
)

_WORD_RE = re.compile(r"[a-z0-9]+")


# ── Normalisation helpers ──────────────────────────────────────────


def _norm_label(label: str) -> str:
    """Lowercase + hyphen-normalise a single label so dictionary lookups match.

    Spaces and underscores both collapse to ``-`` so a GitHub label
    literal of ``good first issue`` matches a query like
    ``?label=good-first-issue`` and the in-module label dictionaries
    (which use the hyphenated canonical form).
    """
    return label.strip().lower().replace("_", "-").replace(" ", "-")


def _norm_labels(labels: Iterable[str]) -> list[str]:
    if not labels:
        return []
    out: list[str] = []
    for label in labels:
        if isinstance(label, str):
            out.append(_norm_label(label))
    return out


def _normalise_difficulty(value: Optional[str]) -> Optional[str]:
    """Coerce a free-form difficulty string to one of the allowed values."""
    if not isinstance(value, str):
        return None
    norm = value.strip().lower()
    if norm in DIFFICULTY_ALLOWED:
        return norm
    return None


def _comments_count(issue: dict) -> int:
    raw = issue.get("comments")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(n, 0)


def _is_vague_title(title: Optional[str]) -> bool:
    if not isinstance(title, str):
        return True
    cleaned = title.strip().lower()
    if not cleaned:
        return True
    words = _WORD_RE.findall(cleaned)
    if not words:
        return True
    if all(word in VAGUE_TITLE_TOKENS for word in words):
        return True
    if len(cleaned) < 12 and len(words) <= 2:
        return True
    return False


def _has_acceptance_criteria(body: Optional[str]) -> bool:
    if not isinstance(body, str) or not body.strip():
        return False
    lowered = body.lower()
    return any(marker in lowered for marker in _AC_MARKERS)


def _body_looks_vague(body: Optional[str]) -> bool:
    """True when the body is missing or too short to be actionable."""
    if not isinstance(body, str):
        return True
    return len(body.strip()) < 50


def _clamp_limit(limit: int) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT
    if n <= 0:
        return _DEFAULT_LIMIT
    return min(n, _MAX_LIMIT)


def _matches_topic(issue: dict, topic: str) -> bool:
    """Case-insensitive substring match against title and labels.

    The body is intentionally NOT inspected here because the list view
    does not carry it; we keep ``recommend_issues`` to a single HTTP
    request and rely on title + labels as the topic signal.
    """
    keyword = topic.strip().lower()
    if not keyword:
        return True
    title = issue.get("title") or ""
    if isinstance(title, str) and keyword in title.lower():
        return True
    for label in _norm_labels(issue.get("labels") or []):
        if keyword in label:
            return True
    return False


def _matches_label(issue: dict, label: str) -> bool:
    """Strict label filter, case-insensitive, hyphen tolerant."""
    target = _norm_label(label)
    if not target:
        return True
    return target in _norm_labels(issue.get("labels") or [])


# ── Per-issue analyser ─────────────────────────────────────────────


def analyze_issue(
    issue: dict, body: Optional[str] = None,
) -> Optional[dict]:
    """Score one sanitised issue and return a recommendation dict.

    ``issue`` is the dict shape produced by ``github.list_issues``.
    ``body`` is the optional issue body string; when omitted, the
    analyser falls back to title + labels only and lowers its
    confidence accordingly.

    Returns ``None`` when the issue is not actionable at all (closed,
    a pull request, or carrying a hard-exclude label like ``wontfix``).
    The caller filters ``None``s out of the recommendation list.
    """
    if not isinstance(issue, dict):
        return None

    # Closed and PR rows never appear in a recommendation list — the
    # underlying ``list_issues`` already drops PRs, but defence in
    # depth is cheap.
    if issue.get("state") != "open":
        return None
    if "pull_request" in issue:
        return None

    number = issue.get("number")
    title = issue.get("title")
    url = issue.get("html_url")
    raw_labels = issue.get("labels") or []
    if not isinstance(raw_labels, list):
        raw_labels = []
    norm_labels = _norm_labels(raw_labels)

    if any(label in NON_ACTIONABLE_LABELS for label in norm_labels):
        return None

    score = 0
    risk_notes: list[str] = []
    priority_reasons: list[str] = []
    next_steps: list[str] = []

    # ── Difficulty from labels (label-driven, deterministic) ──
    low_hits = [label for label in norm_labels if label in LOW_DIFFICULTY_LABELS]
    high_hits = [label for label in norm_labels if label in HIGH_DIFFICULTY_LABELS]
    if low_hits and not high_hits:
        difficulty = DIFFICULTY_LOW
        score += 30
        priority_reasons.append(
            f"labelled {', '.join(sorted(set(low_hits)))} — usually low-effort"
        )
    elif high_hits and not low_hits:
        difficulty = DIFFICULTY_HIGH
        score -= 5
        priority_reasons.append(
            f"labelled {', '.join(sorted(set(high_hits)))} — likely higher-effort"
        )
    elif low_hits and high_hits:
        difficulty = DIFFICULTY_MEDIUM
        score += 5
    else:
        difficulty = DIFFICULTY_MEDIUM

    # ── Risk labels add a caution note and tug difficulty upwards ──
    risk_hits = sorted({label for label in norm_labels if label in RISK_LABELS})
    if risk_hits:
        risk_notes.append(
            f"security-sensitive area ({', '.join(risk_hits)}); review carefully"
        )
        if difficulty == DIFFICULTY_LOW:
            difficulty = DIFFICULTY_MEDIUM
        score -= 5

    # ── Discussion / clarification labels ──
    needs_clarification = any(
        label in NEEDS_CLARIFICATION_LABELS for label in norm_labels
    )
    if needs_clarification:
        risk_notes.append("needs clarification before coding")
        score -= 10
        next_steps.append(
            "Read the thread and ask any open questions before starting."
        )

    # ── Title vagueness ──
    if _is_vague_title(title):
        risk_notes.append("title is vague — confirm scope before starting")
        score -= 15
        if not next_steps:
            next_steps.append(
                "Ask the reporter to clarify the title and expected behaviour."
            )

    # ── Body signals (only when provided; the list view omits body) ──
    body_provided = isinstance(body, str)
    has_ac = _has_acceptance_criteria(body) if body_provided else False
    if has_ac:
        score += 20
        priority_reasons.append("clear acceptance criteria")
    if body_provided and _body_looks_vague(body):
        risk_notes.append("body is short / vague — needs clarification")
        score -= 10

    # ── Comment-count signal ──
    comments = _comments_count(issue)
    if comments == 0:
        score += 5
    elif comments <= 5:
        score += 3
    elif comments >= 20:
        risk_notes.append(
            f"{comments} comments — read the thread before starting"
        )
        score -= 8
    elif comments >= 10:
        score -= 3

    # ── Baseline reason when nothing else fired ──
    if not priority_reasons:
        if difficulty == DIFFICULTY_LOW:
            priority_reasons.append("scoped to a small area")
        elif difficulty == DIFFICULTY_HIGH:
            priority_reasons.append("scoped to a complex area")
        else:
            priority_reasons.append("open and unclaimed")

    # ── Default next step ──
    if not next_steps:
        next_steps.append(
            "Read the issue, scope the work, then open a small draft PR."
        )

    # ── Confidence ──
    signal_strength = (
        (1 if low_hits or high_hits else 0)
        + (1 if has_ac else 0)
        + (1 if risk_hits else 0)
    )
    if _is_vague_title(title) or (body_provided and _body_looks_vague(body)):
        confidence = CONFIDENCE_LOW
    elif signal_strength >= 2:
        confidence = CONFIDENCE_HIGH
    else:
        confidence = CONFIDENCE_MEDIUM

    return {
        "number": number,
        "title": title,
        "url": url,
        "state": issue.get("state"),
        "labels": list(raw_labels) if isinstance(raw_labels, list) else [],
        "difficulty": difficulty,
        "priority_reason": "; ".join(priority_reasons),
        "recommended_next_step": next_steps[0],
        "risk_notes": risk_notes,
        "confidence": confidence,
        "score": score,
    }


# ── Ranker ─────────────────────────────────────────────────────────


def rank_issues(
    issues: Iterable[dict],
    topic: Optional[str] = None,
    label: Optional[str] = None,
    difficulty: Optional[str] = None,
    limit: int = _DEFAULT_LIMIT,
) -> list[dict]:
    """Score and rank a pre-fetched issue list, returning the top N recs.

    ``issues`` must be the sanitised shape from
    ``github.list_issues``. Pure-Python sort; never makes HTTP calls.

    Filters applied (in order):
      1. ``topic``      — case-insensitive substring against title / labels.
      2. ``label``      — strict label match (case- and hyphen-insensitive).
      3. ``difficulty`` — keep only recs at the requested difficulty.

    The output is sorted by descending score; ties break on issue
    number (ascending) so the ordering is stable across runs.
    """
    if not isinstance(issues, list):
        try:
            issues = list(issues)
        except TypeError:
            return []

    pool: list[dict] = []
    for item in issues:
        if not isinstance(item, dict):
            continue
        if topic and not _matches_topic(item, topic):
            continue
        if label and not _matches_label(item, label):
            continue
        rec = analyze_issue(item)
        if rec is None:
            continue
        pool.append(rec)

    wanted = _normalise_difficulty(difficulty)
    if wanted is not None:
        pool = [r for r in pool if r["difficulty"] == wanted]

    pool.sort(key=lambda r: (-int(r["score"]), int(r.get("number") or 0)))
    return pool[:_clamp_limit(limit)]


# ── Top-level entry point ──────────────────────────────────────────


def recommend_issues(
    owner: str,
    repo: str,
    label: Optional[str] = None,
    difficulty: Optional[str] = None,
    topic: Optional[str] = None,
    limit: int = _DEFAULT_LIMIT,
) -> list[dict]:
    """Fetch open issues for ``owner/repo`` and return ranked recommendations.

    Empty list when the connector is disabled / not configured /
    unreachable, or when the slug pair is invalid. Never raises.

    Only **open** issues are considered — closed issues are ignored by
    design. Pull requests are skipped because the underlying connector
    already filters them out.
    """
    if not _gh.is_enabled() or not _gh._has_token():
        return []
    issues = _gh.list_issues(owner, repo, state="open", limit=_FETCH_POOL_DEFAULT)
    if not issues:
        return []
    return rank_issues(
        issues, topic=topic, label=label, difficulty=difficulty, limit=limit,
    )


def is_available() -> bool:
    """True when the underlying connector is on and has a token configured."""
    return _gh.is_enabled() and _gh._has_token()
