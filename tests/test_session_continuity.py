"""
Tests for ``core.session_continuity``.

The summariser is intentionally deterministic and local, so the tests
pin its behaviour rather than its phrasing in detail. The shape of
the output, the topic extraction, the recency window, and the
"silence beats hallucination" rule each get their own case.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from core import memory as core_memory, users, session_continuity
from memory import store as natural_store


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "nova.db")
    monkeypatch.setattr(core_memory, "DB_PATH", path)
    monkeypatch.setattr(natural_store, "DB_PATH", path)
    core_memory.initialize_db()
    return path


def _make_user(username: str = "alice") -> int:
    with sqlite3.connect(core_memory.DB_PATH) as conn:
        return users.create_user(conn, username, "pw", role=users.ROLE_USER)


def _set_updated(conv_id: int, when: datetime) -> None:
    """Force a conversation's ``updated`` timestamp to ``when``.

    ``create_conversation`` always stamps ``now()``; tests need to
    place rows at controlled points in the past.
    """
    with sqlite3.connect(core_memory.DB_PATH) as conn:
        conn.execute(
            "UPDATE conversations SET updated = ? WHERE id = ?",
            (when.isoformat(), conv_id),
        )


class TestEmptyAndStale:
    def test_no_conversations_returns_no_continuity(self, db_path):
        uid = _make_user()
        result = session_continuity.build_session_continuity(uid)
        assert result == {"has_continuity": False}

    def test_only_placeholder_titles_returns_no_continuity(self, db_path):
        uid = _make_user()
        cid = core_memory.create_conversation("Nouvelle conversation", uid)
        _set_updated(cid, datetime.now())
        result = session_continuity.build_session_continuity(uid)
        assert result == {"has_continuity": False}

    def test_only_stale_conversations_returns_no_continuity(self, db_path):
        """Conversations older than the window must be ignored.

        Showing "you were doing X two months ago" is the wrong shape
        for a continue-where-we-left-off card; silence is correct.
        """
        uid = _make_user()
        cid = core_memory.create_conversation("Nova voice tuning", uid)
        _set_updated(cid, datetime.now() - timedelta(days=60))
        result = session_continuity.build_session_continuity(uid)
        assert result == {"has_continuity": False}


class TestBasicSummary:
    def test_recent_conversation_produces_summary(self, db_path):
        uid = _make_user()
        cid = core_memory.create_conversation("Nova-voice improvements", uid)
        now = datetime.now()
        _set_updated(cid, now - timedelta(hours=2))
        result = session_continuity.build_session_continuity(uid, now=now)
        assert result["has_continuity"] is True
        assert "Nova-voice" in result["summary"]
        assert result["recent_titles"] == ["Nova-voice improvements"]
        assert "Nova-voice" in result["topics"]
        assert isinstance(result["fingerprint"], str) and len(result["fingerprint"]) >= 6

    def test_pr_reference_surfaces_as_topic(self, db_path):
        uid = _make_user()
        cid = core_memory.create_conversation("Reviewing PR #154 piper integration", uid)
        now = datetime.now()
        _set_updated(cid, now - timedelta(hours=3))
        result = session_continuity.build_session_continuity(uid, now=now)
        assert result["has_continuity"] is True
        assert "PR #154" in result["topics"]
        assert "PR #154" in result["summary"]

    def test_summary_uses_relative_label(self, db_path):
        uid = _make_user()
        cid = core_memory.create_conversation("UI refinements", uid)
        now = datetime(2026, 5, 7, 10, 0, 0)
        _set_updated(cid, now - timedelta(days=1))
        result = session_continuity.build_session_continuity(uid, now=now)
        assert result["last_active_label"] == "yesterday"
        assert "yesterday" in result["summary"]


class TestExclusionAndOrdering:
    def test_excluded_conversation_is_skipped(self, db_path):
        """Don't quote the conversation the user is already looking at."""
        uid = _make_user()
        a = core_memory.create_conversation("UI refinements", uid)
        b = core_memory.create_conversation("Piper integration", uid)
        now = datetime.now()
        _set_updated(a, now - timedelta(hours=10))
        _set_updated(b, now - timedelta(hours=2))
        result = session_continuity.build_session_continuity(
            uid, now=now, exclude_conversation_id=b
        )
        assert result["recent_titles"] == ["UI refinements"]

    def test_titles_sorted_most_recent_first(self, db_path):
        uid = _make_user()
        old = core_memory.create_conversation("Older topic Foo", uid)
        mid = core_memory.create_conversation("Mid topic Bar", uid)
        new = core_memory.create_conversation("Newest topic Baz", uid)
        now = datetime.now()
        _set_updated(old, now - timedelta(days=5))
        _set_updated(mid, now - timedelta(days=2))
        _set_updated(new, now - timedelta(hours=1))
        result = session_continuity.build_session_continuity(uid, now=now)
        assert result["recent_titles"][0] == "Newest topic Baz"
        # MAX_TITLES is 3 — all three should be present, newest first.
        assert result["recent_titles"][:3] == [
            "Newest topic Baz",
            "Mid topic Bar",
            "Older topic Foo",
        ]


class TestUserScoping:
    def test_other_users_conversations_are_invisible(self, db_path):
        """Continuity respects per-user scoping; no cross-user leakage."""
        alice = _make_user("alice")
        bob = _make_user("bob")
        cid = core_memory.create_conversation("Bob's secret project", bob)
        _set_updated(cid, datetime.now() - timedelta(hours=1))
        result = session_continuity.build_session_continuity(alice)
        assert result == {"has_continuity": False}


class TestDeterminism:
    def test_fingerprint_stable_for_same_inputs(self, db_path):
        uid = _make_user()
        cid = core_memory.create_conversation("Stable Fingerprint check", uid)
        now = datetime(2026, 5, 7, 10, 0, 0)
        _set_updated(cid, now - timedelta(hours=2))
        a = session_continuity.build_session_continuity(uid, now=now)
        b = session_continuity.build_session_continuity(uid, now=now)
        assert a["fingerprint"] == b["fingerprint"]

    def test_fingerprint_changes_when_titles_change(self, db_path):
        uid = _make_user()
        cid = core_memory.create_conversation("First topic Alpha", uid)
        now = datetime(2026, 5, 7, 10, 0, 0)
        _set_updated(cid, now - timedelta(hours=2))
        before = session_continuity.build_session_continuity(uid, now=now)

        cid2 = core_memory.create_conversation("Second topic Beta", uid)
        _set_updated(cid2, now - timedelta(hours=1))
        after = session_continuity.build_session_continuity(uid, now=now)

        assert before["fingerprint"] != after["fingerprint"]
