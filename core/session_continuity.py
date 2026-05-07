"""
Session continuity — a small, deterministic helper that builds a calm
"continue where we left off" summary from the user's recent conversations.

Design notes
------------
This module is intentionally tiny and local-first. It does **not**:

- call any LLM or external service,
- store hidden summaries,
- infer mood, intent, or anything emotional,
- run on a background loop.

It only reads conversation rows the user already owns and produces a
short, human-readable summary the UI can display once and dismiss. If
the local signals are too weak to be useful, it returns
``has_continuity=False`` and the UI shows nothing — silence is the
correct fallback.

The output is fully derived from data the user can already inspect in
the sidebar. There is no information here that the user could not
reconstruct themselves; the module just saves them a glance.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Iterable, Optional

from core.memory import load_conversations


# How far back we look for "recent" activity. Past this horizon the
# summary is too stale to be reassuring; we'd rather show nothing than
# remind the user of something they finished weeks ago.
RECENT_WINDOW_DAYS = 14

# How many distinct titles we surface. Three is enough to feel grounded
# without becoming a wall of text.
MAX_TITLES = 3

# How many topic tokens we extract. Same reasoning: terse beats noisy.
MAX_TOPICS = 3

# Minimum number of distinct, non-trivial conversations needed before we
# show anything. With only one recent conversation the user is already
# looking at it; the banner would just be repeating the obvious.
MIN_CONVERSATIONS = 1

# Generic "Nouvelle conversation" / "New conversation" titles carry no
# information. They are filtered out before we look at content. The
# match is intentionally case-insensitive and language-aware.
_PLACEHOLDER_TITLE_RE = re.compile(
    r"^\s*(nouvelle conversation|new conversation|untitled|sans titre)\s*$",
    re.IGNORECASE,
)

# Pull `#123`-style issue/PR references straight out of titles. The
# leading boundary keeps `pr#1` out — we want a real anchor.
_PR_REF_RE = re.compile(r"(?<![A-Za-z0-9])#(\d{1,5})(?![A-Za-z0-9])")

# Tokens we consider "interesting" for the topic list. We only keep
# words that look like project / capability names, which on this
# project tend to be CamelCase, ALL-CAPS, or names with internal
# punctuation (e.g. `nova-voice`, `Piper`). Plain English filler is
# dropped to keep the summary honest.
_TOPIC_TOKEN_RE = re.compile(
    r"\b("
    r"[A-Z][A-Za-z0-9]+(?:[-_/][A-Za-z0-9]+)+"  # Nova-voice, Foo/Bar
    r"|[A-Z]{2,}[A-Za-z0-9]*"  # ROCm, UI, API
    r"|[A-Z][a-z]+[A-Z][A-Za-z0-9]*"  # CamelCase
    r")\b"
)

# Words we never want to surface as a topic, even if they slip past
# the token regex. Keep short and conservative; the goal is to avoid
# obvious noise, not to exhaustively curate vocabulary.
_TOPIC_STOPLIST = frozenset(
    {
        "I",
        "Im",
        "Ive",
        "OK",
        "TODO",
        "FIXME",
        "PR",
        "PRs",
        "Issue",
        "Issues",
    }
)


def _parse_iso(value: str | None) -> Optional[datetime]:
    """Parse an ISO timestamp; return None if it's missing or malformed.

    The store writes timestamps via ``datetime.now().isoformat()``, but
    we still tolerate junk so a corrupt row never blocks the rest of
    the summary.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _is_meaningful_title(title: str | None) -> bool:
    if not title:
        return False
    stripped = title.strip()
    if len(stripped) < 3:
        return False
    if _PLACEHOLDER_TITLE_RE.match(stripped):
        return False
    return True


def _relative_label(when: datetime, now: datetime) -> str:
    """Render ``when`` as a calm relative phrase like 'yesterday'.

    The output is deliberately coarse: hour-level precision would
    sound clinical, and Nova should sound like a colleague, not a log
    file.
    """
    delta = now - when
    if delta < timedelta(0):
        # Clock skew: treat future timestamps as "just now" rather than
        # surfacing a confusing negative duration.
        return "just now"
    if delta < timedelta(hours=1):
        return "just now"
    if delta < timedelta(hours=12) and when.date() == now.date():
        return "earlier today"
    if when.date() == now.date():
        return "today"
    if when.date() == (now - timedelta(days=1)).date():
        return "yesterday"
    days = delta.days
    if days < 7:
        return f"{days} days ago"
    if days < 14:
        return "last week"
    weeks = days // 7
    return f"{weeks} weeks ago"


def _extract_topics(titles: Iterable[str]) -> list[str]:
    """Pull a small, ordered list of topic tokens from recent titles.

    Order is preserved by first appearance so the most recent
    conversation's topics surface first. Duplicates (case-insensitive)
    are dropped.
    """
    seen: dict[str, str] = {}
    for title in titles:
        # PR / issue references first — they're the most concrete signal.
        for match in _PR_REF_RE.finditer(title):
            label = f"PR #{match.group(1)}"
            key = label.lower()
            if key not in seen:
                seen[key] = label
            if len(seen) >= MAX_TOPICS:
                return list(seen.values())
        for match in _TOPIC_TOKEN_RE.finditer(title):
            token = match.group(1)
            if token in _TOPIC_STOPLIST:
                continue
            key = token.lower()
            if key in seen:
                continue
            seen[key] = token
            if len(seen) >= MAX_TOPICS:
                return list(seen.values())
    return list(seen.values())


def _compose_summary(
    titles: list[str],
    topics: list[str],
    last_label: str,
) -> str:
    """Build the one-line, plain-English banner string.

    The phrasing is intentionally hedged ("focused on", "you were
    working on") because we are summarising titles, not reading minds.
    """
    if topics:
        if len(topics) == 1:
            joined = topics[0]
        elif len(topics) == 2:
            joined = f"{topics[0]} and {topics[1]}"
        else:
            joined = ", ".join(topics[:-1]) + f", and {topics[-1]}"
        return f"Last session ({last_label}) focused on {joined}."
    # No structured topics — fall back to the most recent title verbatim.
    # Quoting it makes clear we're echoing, not paraphrasing.
    return f'Last session ({last_label}): "{titles[0]}".'


def _fingerprint(titles: list[str], last_active_iso: str) -> str:
    """A short stable hash of the summary inputs.

    The UI uses this to remember which summary the user has already
    dismissed. If new activity changes the fingerprint, the banner is
    eligible to reappear; otherwise it stays hidden.
    """
    payload = "|".join([last_active_iso, *titles])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def build_session_continuity(
    user_id: int,
    *,
    now: Optional[datetime] = None,
    exclude_conversation_id: int | None = None,
) -> dict:
    """Return a small structured summary for the user's recent activity.

    The returned dict always contains ``has_continuity``. When the
    signal is too thin (no recent conversations, only placeholders,
    only the conversation the user is already looking at), the rest of
    the fields are omitted and the UI shows nothing.

    Parameters
    ----------
    user_id:
        Owner of the conversations being summarised. Memory scoping
        is enforced by ``load_conversations`` upstream.
    now:
        Optional override for the current time (used by tests so the
        relative-label logic is deterministic).
    exclude_conversation_id:
        If set, the conversation with this id is excluded from the
        summary. Useful when the user already has that conversation
        open — repeating its title back at them is noise.
    """
    now = now or datetime.now()
    cutoff = now - timedelta(days=RECENT_WINDOW_DAYS)

    rows = load_conversations(user_id) or []

    recent: list[tuple[datetime, str]] = []
    for row in rows:
        if exclude_conversation_id is not None and row.get("id") == exclude_conversation_id:
            continue
        title = row.get("title")
        if not _is_meaningful_title(title):
            continue
        updated = _parse_iso(row.get("updated"))
        if updated is None or updated < cutoff:
            continue
        recent.append((updated, title.strip()))

    if len(recent) < MIN_CONVERSATIONS:
        return {"has_continuity": False}

    # ``load_conversations`` already returns rows updated-DESC, but we
    # re-sort defensively so callers can pass a list in any order.
    recent.sort(key=lambda pair: pair[0], reverse=True)

    titles = [title for _, title in recent[:MAX_TITLES]]
    last_active_dt, _ = recent[0]
    last_active_iso = last_active_dt.isoformat()
    last_label = _relative_label(last_active_dt, now)
    topics = _extract_topics(titles)
    summary = _compose_summary(titles, topics, last_label)

    return {
        "has_continuity": True,
        "summary": summary,
        "last_active": last_active_iso,
        "last_active_label": last_label,
        "recent_titles": titles,
        "topics": topics,
        "fingerprint": _fingerprint(titles, last_active_iso),
    }
