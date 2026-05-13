"""
Tests for the optional read-only GitHub triage / recommendation helper.

Scope mirrors the issue's testing checklist:

  * recommendations work with mocked GitHub issues;
  * closed issues are ignored by default;
  * ``good first issue`` / docs / tests issues rank as ``low`` difficulty;
  * security / admin / auth issues include caution / risk notes;
  * vague issues are marked as needing clarification;
  * the recommendations endpoint is admin-only — non-admin /
    restricted users get a 403;
  * the disabled connector returns ``[]`` / a graceful 503;
  * an invalid GitHub token error path is sanitised — the configured
    token never appears in the response or error;
  * the existing GitHub connector tests continue to pass.

The HTTP transport is stubbed via the same ``httpx.Client`` factory
swap the original connector tests use, so no real network call is
ever issued by this suite.
"""

from __future__ import annotations

import ast
import contextlib
import sqlite3
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from core import memory as core_memory, users
from core.integrations import github as gh
from core.integrations import github_triage as triage
from memory import store as natural_store


SECRET_TOKEN = "ghp_TRIAGE_TOKEN_must_never_leak_0987654321"
SECRET_FRAGMENT = "TRIAGE_TOKEN_must_never_leak"


# ── Shared fixtures (mirror tests/test_integrations_github.py) ──────


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "nova.db")
    monkeypatch.setattr(core_memory, "DB_PATH", path)
    monkeypatch.setattr(natural_store, "DB_PATH", path)
    core_memory.initialize_db()
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM users")
    return path


def _make_user(db_path, username, password="pw", role=users.ROLE_USER,
               is_restricted=False):
    with sqlite3.connect(db_path) as conn:
        return users.create_user(
            conn, username, password, role=role, is_restricted=is_restricted,
        )


@pytest.fixture
def web_client(db_path, monkeypatch):
    monkeypatch.setattr(core_memory, "DB_PATH", db_path)
    monkeypatch.setattr(natural_store, "DB_PATH", db_path)
    from core.rate_limiter import _login_limiter
    _login_limiter._store.clear()

    import web
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("web.initialize_db"))
        stack.enter_context(patch("web.learn_from_feeds"))
        stack.enter_context(patch("web.scheduler", MagicMock()))
        with TestClient(web.app, raise_server_exceptions=True) as client:
            yield client


def _login(client, username, password="pw"):
    resp = client.post("/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _h(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_token(db_path, web_client):
    _make_user(db_path, "alice", role=users.ROLE_ADMIN)
    return _login(web_client, "alice")


@pytest.fixture
def user_token(db_path, web_client):
    _make_user(db_path, "bob")
    return _login(web_client, "bob")


@pytest.fixture
def restricted_token(db_path, web_client):
    _make_user(db_path, "kid", is_restricted=True)
    return _login(web_client, "kid")


# ── Fake httpx client (same shape as the connector test suite) ──────


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any = None,
                 headers: dict | None = None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, scripted: dict, calls: list,
                 raise_on: set | None = None, headers: dict | None = None):
        self._scripted = scripted
        self._calls = calls
        self._raise_on = raise_on or set()
        self._headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def _record(self, method: str, path: str, **kwargs):
        self._calls.append(
            {"method": method, "path": path, "kwargs": kwargs,
             "headers": dict(self._headers)},
        )
        if path in self._raise_on:
            raise httpx.ConnectError("boom")
        return self._scripted.get((method, path), _FakeResponse(404))

    def get(self, path: str, **kwargs):
        return self._record("GET", path, **kwargs)


@pytest.fixture
def github_stub(monkeypatch):
    calls: list = []
    state: dict = {"scripted": {}, "raise_on": set()}

    monkeypatch.setattr(gh, "NOVA_GITHUB_ENABLED", True)
    monkeypatch.setattr(gh, "NOVA_GITHUB_TOKEN", SECRET_TOKEN)
    monkeypatch.setattr(gh, "NOVA_GITHUB_DEFAULT_REPO", "octocat/Hello-World")
    monkeypatch.setattr(gh, "NOVA_GITHUB_READ_ONLY", True)

    def factory(*args, **kwargs):
        return _FakeClient(
            state["scripted"], calls, state["raise_on"],
            headers=kwargs.get("headers", {}),
        )

    monkeypatch.setattr(gh.httpx, "Client", factory)

    def install(scripted: dict, raise_on: set | None = None):
        state["scripted"] = scripted
        state["raise_on"] = raise_on or set()

    return install, calls


# ── Issue payload factory ──────────────────────────────────────────


def _issue(
    number: int,
    title: str = "Improve the widget",
    labels: list | None = None,
    comments: int = 0,
    state: str = "open",
    body: str | None = None,
) -> dict:
    return {
        "number": number,
        "title": title,
        "state": state,
        "user": {"login": "alice"},
        "labels": [{"name": label} for label in (labels or [])],
        "comments": comments,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
        "closed_at": None,
        "html_url": f"https://github.com/octocat/Hello-World/issues/{number}",
        "body": body,
    }


def _sanitised(issue: dict) -> dict:
    """Drop ``body`` so the dict matches the shape returned by ``list_issues``."""
    out = dict(issue)
    out.pop("body", None)
    out["labels"] = [label["name"] for label in issue.get("labels", [])]
    return out


# ── Pure analyser tests ────────────────────────────────────────────


class TestAnalyzeIssueDifficulty:
    def test_good_first_issue_is_low(self):
        rec = triage.analyze_issue(_sanitised(_issue(1, labels=["good first issue"])))
        assert rec is not None
        assert rec["difficulty"] == triage.DIFFICULTY_LOW

    def test_docs_label_is_low(self):
        rec = triage.analyze_issue(_sanitised(_issue(2, labels=["docs"])))
        assert rec["difficulty"] == triage.DIFFICULTY_LOW

    def test_tests_label_is_low(self):
        rec = triage.analyze_issue(_sanitised(_issue(3, labels=["tests"])))
        assert rec["difficulty"] == triage.DIFFICULTY_LOW

    def test_ui_label_is_low(self):
        rec = triage.analyze_issue(_sanitised(_issue(4, labels=["ui"])))
        assert rec["difficulty"] == triage.DIFFICULTY_LOW

    def test_architecture_label_is_high(self):
        rec = triage.analyze_issue(_sanitised(_issue(5, labels=["architecture"])))
        assert rec["difficulty"] == triage.DIFFICULTY_HIGH

    def test_refactor_label_is_high(self):
        rec = triage.analyze_issue(_sanitised(_issue(6, labels=["refactor"])))
        assert rec["difficulty"] == triage.DIFFICULTY_HIGH

    def test_no_label_defaults_to_medium(self):
        rec = triage.analyze_issue(
            _sanitised(_issue(7, title="Move retry budget into config")),
        )
        assert rec["difficulty"] == triage.DIFFICULTY_MEDIUM

    def test_label_lookup_is_case_and_separator_insensitive(self):
        rec = triage.analyze_issue(
            _sanitised(_issue(8, labels=["Good_First_Issue"])),
        )
        assert rec["difficulty"] == triage.DIFFICULTY_LOW


class TestAnalyzeIssueRisk:
    def test_security_label_adds_risk_note(self):
        rec = triage.analyze_issue(_sanitised(_issue(11, labels=["security"])))
        assert any("security-sensitive" in note for note in rec["risk_notes"])

    def test_auth_label_adds_risk_note(self):
        rec = triage.analyze_issue(_sanitised(_issue(12, labels=["auth"])))
        assert any("security-sensitive" in note for note in rec["risk_notes"])

    def test_admin_label_adds_risk_note(self):
        rec = triage.analyze_issue(_sanitised(_issue(13, labels=["admin"])))
        assert any("security-sensitive" in note for note in rec["risk_notes"])

    def test_memory_label_adds_risk_note(self):
        rec = triage.analyze_issue(_sanitised(_issue(14, labels=["memory"])))
        assert any("security-sensitive" in note for note in rec["risk_notes"])

    def test_risk_bumps_low_to_medium(self):
        # An issue labelled "good first issue" and "auth" should *not*
        # be sold as a casual starter; bump it to medium so the
        # maintainer reads carefully.
        rec = triage.analyze_issue(
            _sanitised(_issue(15, labels=["good first issue", "auth"])),
        )
        assert rec["difficulty"] == triage.DIFFICULTY_MEDIUM
        assert any("security-sensitive" in note for note in rec["risk_notes"])


class TestAnalyzeIssueClarification:
    def test_vague_title_marked(self):
        rec = triage.analyze_issue(_sanitised(_issue(21, title="Bug")))
        assert any("vague" in note for note in rec["risk_notes"])

    def test_vague_title_lowers_confidence(self):
        rec = triage.analyze_issue(_sanitised(_issue(22, title="Help?")))
        assert rec["confidence"] == triage.CONFIDENCE_LOW

    def test_concrete_title_not_marked_vague(self):
        rec = triage.analyze_issue(
            _sanitised(_issue(23, title="Wire SilentGuard summary card",
                              labels=["ui"])),
        )
        assert not any("vague" in note for note in rec["risk_notes"])

    def test_short_body_flagged_when_provided(self):
        sanitised = _sanitised(_issue(24, title="Improve onboarding"))
        rec = triage.analyze_issue(sanitised, body="see logs")
        assert any("body is short" in note for note in rec["risk_notes"])

    def test_clear_body_does_not_get_flagged(self):
        body = (
            "We should add a /v1/foo endpoint so the dashboard can fetch the "
            "latest counts in one round trip. Acceptance criteria: returns "
            "JSON with a count field; never raises."
        )
        sanitised = _sanitised(_issue(25, title="Add /v1/foo endpoint"))
        rec = triage.analyze_issue(sanitised, body=body)
        assert not any("vague" in note for note in rec["risk_notes"])
        assert "clear acceptance criteria" in rec["priority_reason"]

    def test_needs_clarification_label_adds_note(self):
        rec = triage.analyze_issue(_sanitised(_issue(26, labels=["question"])))
        assert any("clarification" in note.lower() for note in rec["risk_notes"])


class TestAnalyzeIssueExclusions:
    def test_closed_issue_returns_none(self):
        assert triage.analyze_issue(_sanitised(_issue(31, state="closed"))) is None

    def test_wontfix_issue_returns_none(self):
        assert triage.analyze_issue(_sanitised(_issue(32, labels=["wontfix"]))) is None

    def test_blocked_issue_returns_none(self):
        assert triage.analyze_issue(_sanitised(_issue(33, labels=["blocked"]))) is None

    def test_pull_request_returns_none(self):
        sanitised = _sanitised(_issue(34))
        sanitised["pull_request"] = {"url": "..."}
        assert triage.analyze_issue(sanitised) is None

    def test_non_dict_input_returns_none(self):
        assert triage.analyze_issue(None) is None
        assert triage.analyze_issue("not an issue") is None


class TestAnalyzeIssueComments:
    def test_many_comments_add_caution(self):
        rec = triage.analyze_issue(
            _sanitised(_issue(41, title="Refine the retry budget", comments=25)),
        )
        assert any("read the thread" in note for note in rec["risk_notes"])

    def test_zero_comments_does_not_add_caution(self):
        rec = triage.analyze_issue(
            _sanitised(_issue(42, title="Refine the retry budget", comments=0)),
        )
        assert not any("comments" in note for note in rec["risk_notes"])


class TestAnalyzeIssueShape:
    def test_recommendation_shape(self):
        rec = triage.analyze_issue(
            _sanitised(_issue(51, title="Add /v1/foo endpoint",
                              labels=["docs", "good first issue"])),
        )
        # Required keys per the issue spec.
        for key in (
            "number", "title", "url", "labels", "difficulty",
            "priority_reason", "recommended_next_step", "risk_notes",
            "confidence",
        ):
            assert key in rec, f"missing key {key!r}"
        assert rec["number"] == 51
        assert rec["url"] == (
            "https://github.com/octocat/Hello-World/issues/51"
        )
        assert isinstance(rec["risk_notes"], list)
        assert rec["difficulty"] in triage.DIFFICULTY_ALLOWED
        assert rec["confidence"] in (
            triage.CONFIDENCE_LOW,
            triage.CONFIDENCE_MEDIUM,
            triage.CONFIDENCE_HIGH,
        )


# ── Ranker tests ───────────────────────────────────────────────────


class TestRanker:
    def test_filters_closed_issues(self):
        issues = [
            _sanitised(_issue(1, title="Add tests", labels=["tests"])),
            _sanitised(_issue(2, title="Old thing", state="closed")),
        ]
        recs = triage.rank_issues(issues)
        assert [r["number"] for r in recs] == [1]

    def test_low_difficulty_outranks_high(self):
        issues = [
            _sanitised(_issue(1, title="Rework storage backend",
                              labels=["architecture"])),
            _sanitised(_issue(2, title="Add tests for retry budget",
                              labels=["tests"])),
        ]
        recs = triage.rank_issues(issues)
        assert recs[0]["number"] == 2
        assert recs[0]["difficulty"] == triage.DIFFICULTY_LOW

    def test_limit_applied(self):
        issues = [
            _sanitised(_issue(i, title=f"Polish step {i}", labels=["ui"]))
            for i in range(1, 11)
        ]
        recs = triage.rank_issues(issues, limit=3)
        assert len(recs) == 3

    def test_limit_clamped(self):
        issues = [_sanitised(_issue(i, labels=["ui"])) for i in range(1, 5)]
        # 0 / negative falls back to default; oversize is clamped.
        assert len(triage.rank_issues(issues, limit=0)) == 4
        assert len(triage.rank_issues(issues, limit=-5)) == 4
        assert len(triage.rank_issues(issues, limit=10_000)) == 4

    def test_label_filter(self):
        issues = [
            _sanitised(_issue(1, labels=["docs"])),
            _sanitised(_issue(2, labels=["memory"])),
            _sanitised(_issue(3, labels=["ui"])),
        ]
        recs = triage.rank_issues(issues, label="memory")
        assert [r["number"] for r in recs] == [2]

    def test_label_filter_case_insensitive(self):
        issues = [_sanitised(_issue(1, labels=["Memory"]))]
        recs = triage.rank_issues(issues, label="memory")
        assert [r["number"] for r in recs] == [1]

    def test_label_filter_space_and_hyphen_interchangeable(self):
        # GitHub labels are commonly space-separated ("good first issue")
        # while query strings prefer hyphens. The matcher must collapse
        # both to the same form so neither side gets surprise misses.
        issues = [_sanitised(_issue(1, labels=["good first issue"]))]
        assert [r["number"] for r in
                triage.rank_issues(issues, label="good-first-issue")] == [1]
        assert [r["number"] for r in
                triage.rank_issues(issues, label="good first issue")] == [1]
        assert [r["number"] for r in
                triage.rank_issues(issues, label="GOOD_FIRST_ISSUE")] == [1]

    def test_low_difficulty_from_space_separated_label(self):
        rec = triage.analyze_issue(
            _sanitised(_issue(2, labels=["good first issue"])),
        )
        assert rec is not None
        assert rec["difficulty"] == triage.DIFFICULTY_LOW

    def test_difficulty_filter(self):
        issues = [
            _sanitised(_issue(1, title="Add tests for retry budget",
                              labels=["tests"])),
            _sanitised(_issue(2, title="Rework storage backend",
                              labels=["architecture"])),
            _sanitised(_issue(3, title="Move retry budget into config")),
        ]
        recs = triage.rank_issues(issues, difficulty="high")
        assert [r["number"] for r in recs] == [2]

    def test_difficulty_filter_unknown_value_keeps_all(self):
        issues = [_sanitised(_issue(1, labels=["docs"]))]
        # Unknown value falls back to "no filter" rather than emptying.
        recs = triage.rank_issues(issues, difficulty="impossible")
        assert [r["number"] for r in recs] == [1]

    def test_topic_filter_matches_title(self):
        issues = [
            _sanitised(_issue(1, title="Memory pack import")),
            _sanitised(_issue(2, title="Voice polish")),
        ]
        recs = triage.rank_issues(issues, topic="memory")
        assert [r["number"] for r in recs] == [1]

    def test_topic_filter_matches_label(self):
        issues = [
            _sanitised(_issue(1, title="Add metric", labels=["silentguard"])),
            _sanitised(_issue(2, title="Add metric", labels=["voice"])),
        ]
        recs = triage.rank_issues(issues, topic="silentguard")
        assert [r["number"] for r in recs] == [1]

    def test_stable_tie_break_on_issue_number(self):
        issues = [_sanitised(_issue(i, labels=["ui"])) for i in (9, 3, 7)]
        recs = triage.rank_issues(issues)
        assert [r["number"] for r in recs] == [3, 7, 9]


# ── recommend_issues (end-to-end against the stub) ─────────────────


class TestRecommendIssues:
    def test_returns_empty_when_disabled(self, monkeypatch, github_stub):
        install, calls = github_stub
        monkeypatch.setattr(gh, "NOVA_GITHUB_ENABLED", False)
        install({
            ("GET", "/repos/octocat/Hello-World/issues"): _FakeResponse(
                200, [_issue(1, labels=["tests"])],
            ),
        })
        assert triage.recommend_issues("octocat", "Hello-World") == []
        # Disabled means we never call out — keep that contract.
        assert calls == []

    def test_returns_empty_when_no_token(self, monkeypatch, github_stub):
        install, calls = github_stub
        monkeypatch.setattr(gh, "NOVA_GITHUB_TOKEN", "")
        install({})
        assert triage.recommend_issues("octocat", "Hello-World") == []
        assert calls == []

    def test_returns_ranked_recommendations(self, github_stub):
        install, _ = github_stub
        install({
            ("GET", "/repos/octocat/Hello-World/issues"): _FakeResponse(
                200,
                [
                    _issue(11, title="Rework storage backend",
                           labels=["architecture"]),
                    _issue(12, title="Add tests for retry budget",
                           labels=["tests"]),
                    _issue(13, title="Refine onboarding copy",
                           labels=["docs"]),
                ],
            ),
        })
        recs = triage.recommend_issues("octocat", "Hello-World", limit=3)
        # Both low-difficulty recs should outrank the architecture one.
        assert len(recs) == 3
        assert recs[0]["difficulty"] == triage.DIFFICULTY_LOW
        assert recs[-1]["difficulty"] == triage.DIFFICULTY_HIGH

    def test_closed_issues_ignored_by_underlying_filter(self, github_stub):
        install, _ = github_stub
        install({
            ("GET", "/repos/octocat/Hello-World/issues"): _FakeResponse(
                200,
                [
                    _issue(21, title="Add tests for retry budget",
                           labels=["tests"], state="open"),
                    _issue(22, title="Move retry budget into config",
                           state="closed"),
                ],
            ),
        })
        recs = triage.recommend_issues("octocat", "Hello-World")
        assert [r["number"] for r in recs] == [21]

    def test_network_error_returns_empty(self, github_stub):
        install, _ = github_stub
        install({}, raise_on={"/repos/octocat/Hello-World/issues"})
        assert triage.recommend_issues("octocat", "Hello-World") == []

    def test_topic_filter_passed_through(self, github_stub):
        install, _ = github_stub
        install({
            ("GET", "/repos/octocat/Hello-World/issues"): _FakeResponse(
                200,
                [
                    _issue(31, title="Memory pack import"),
                    _issue(32, title="Voice polish", labels=["ui"]),
                ],
            ),
        })
        recs = triage.recommend_issues(
            "octocat", "Hello-World", topic="memory",
        )
        assert [r["number"] for r in recs] == [31]


# ── Module-level "no write code" enforcement ───────────────────────


class TestNoWriteCode:
    """The triage helper must never call any GitHub write verb."""

    def test_module_has_no_write_helpers(self):
        for name in (
            "create_issue", "close_issue", "comment_on_issue",
            "merge_pull_request", "approve_pull_request",
            "set_labels", "assign_user", "auto_assign",
            "start_work", "claim_issue",
        ):
            assert not hasattr(triage, name), (
                f"{name!r} must not exist in the triage helper"
            )

    def test_module_never_calls_write_verbs(self):
        with open(triage.__file__, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        forbidden = {"post", "put", "patch", "delete"}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            attr = getattr(func, "attr", None)
            if isinstance(attr, str) and attr.lower() in forbidden:
                raise AssertionError(
                    f"forbidden write verb {attr!r} at line {node.lineno}"
                )

    def test_module_does_not_call_httpx_directly(self):
        """The helper must route through the connector, not ``httpx``."""
        with open(triage.__file__, "r", encoding="utf-8") as f:
            source = f.read()
        assert "httpx" not in source, (
            "triage helper must use the connector, never httpx directly"
        )


# ── Endpoint: admin-only enforcement ───────────────────────────────


class TestEndpointAdminOnly:
    @pytest.fixture(autouse=True)
    def _stub(self, github_stub):
        install, _ = github_stub
        install({
            ("GET", "/user"): _FakeResponse(200, {"login": "octocat"}),
            ("GET", "/repos/octocat/Hello-World/issues"): _FakeResponse(
                200,
                [
                    _issue(1, title="Add tests", labels=["tests"]),
                    _issue(2, title="Refactor router", labels=["architecture"]),
                ],
            ),
        })

    def test_non_admin_user_forbidden(self, web_client, user_token):
        resp = web_client.get(
            "/integrations/github/recommendations", headers=_h(user_token),
        )
        assert resp.status_code == 403

    def test_restricted_user_forbidden(self, web_client, restricted_token):
        resp = web_client.get(
            "/integrations/github/recommendations", headers=_h(restricted_token),
        )
        assert resp.status_code == 403

    def test_unauthenticated_blocked(self, web_client):
        resp = web_client.get("/integrations/github/recommendations")
        assert resp.status_code in (401, 403)


# ── Endpoint: admin happy path + filters ───────────────────────────


class TestEndpointAdmin:
    @pytest.fixture(autouse=True)
    def _stub(self, github_stub):
        install, _ = github_stub
        install({
            ("GET", "/user"): _FakeResponse(200, {"login": "octocat"}),
            ("GET", "/repos/octocat/Hello-World/issues"): _FakeResponse(
                200,
                [
                    _issue(1, title="Add tests for retry budget",
                           labels=["tests"]),
                    _issue(2, title="Rework storage backend",
                           labels=["architecture"]),
                    _issue(3, title="Bug", labels=["bug"]),
                    _issue(4, title="Add memory metric", labels=["memory"]),
                ],
            ),
        })

    def test_default_recommendations(self, web_client, admin_token):
        resp = web_client.get(
            "/integrations/github/recommendations", headers=_h(admin_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["repo"] == "octocat/Hello-World"
        assert body["read_only"] is True
        recs = body["recommendations"]
        assert isinstance(recs, list)
        assert len(recs) >= 1
        # The "tests" labelled issue should sit at the top — it's the
        # only low-difficulty candidate.
        assert recs[0]["difficulty"] == triage.DIFFICULTY_LOW
        # The "Bug" title is vague and should ship with a risk note.
        vague_rec = next(r for r in recs if r["number"] == 3)
        assert any("vague" in note for note in vague_rec["risk_notes"])
        # The "memory" label always carries a caution note.
        memory_rec = next(r for r in recs if r["number"] == 4)
        assert any("security-sensitive" in note for note in memory_rec["risk_notes"])

    def test_label_filter(self, web_client, admin_token):
        resp = web_client.get(
            "/integrations/github/recommendations?label=memory",
            headers=_h(admin_token),
        )
        assert resp.status_code == 200
        recs = resp.json()["recommendations"]
        assert [r["number"] for r in recs] == [4]

    def test_difficulty_filter(self, web_client, admin_token):
        resp = web_client.get(
            "/integrations/github/recommendations?difficulty=high",
            headers=_h(admin_token),
        )
        assert resp.status_code == 200
        recs = resp.json()["recommendations"]
        assert [r["number"] for r in recs] == [2]

    def test_topic_filter(self, web_client, admin_token):
        resp = web_client.get(
            "/integrations/github/recommendations?topic=memory",
            headers=_h(admin_token),
        )
        assert resp.status_code == 200
        recs = resp.json()["recommendations"]
        assert [r["number"] for r in recs] == [4]

    def test_limit(self, web_client, admin_token):
        resp = web_client.get(
            "/integrations/github/recommendations?limit=2",
            headers=_h(admin_token),
        )
        assert resp.status_code == 200
        assert len(resp.json()["recommendations"]) == 2

    def test_explicit_repo(self, web_client, admin_token, github_stub):
        install, _ = github_stub
        install({
            ("GET", "/user"): _FakeResponse(200, {"login": "octocat"}),
            ("GET", "/repos/foo/bar/issues"): _FakeResponse(
                200, [_issue(7, title="Add docs", labels=["docs"])],
            ),
        })
        resp = web_client.get(
            "/integrations/github/recommendations?repo=foo/bar",
            headers=_h(admin_token),
        )
        assert resp.status_code == 200
        assert resp.json()["repo"] == "foo/bar"


class TestEndpointGracefulFailures:
    def test_400_without_repo(self, monkeypatch, web_client, admin_token,
                              github_stub):
        install, _ = github_stub
        monkeypatch.setattr(gh, "NOVA_GITHUB_DEFAULT_REPO", "")
        install({("GET", "/user"): _FakeResponse(200, {"login": "octocat"})})
        resp = web_client.get(
            "/integrations/github/recommendations", headers=_h(admin_token),
        )
        assert resp.status_code == 400

    def test_503_when_disabled(self, monkeypatch, web_client, admin_token,
                               github_stub):
        install, _ = github_stub
        monkeypatch.setattr(gh, "NOVA_GITHUB_ENABLED", False)
        install({})
        resp = web_client.get(
            "/integrations/github/recommendations", headers=_h(admin_token),
        )
        # disabled → 404 (matches the rest of the connector)
        assert resp.status_code == 404

    def test_503_when_not_configured(self, monkeypatch, web_client, admin_token,
                                     github_stub):
        install, _ = github_stub
        monkeypatch.setattr(gh, "NOVA_GITHUB_TOKEN", "")
        install({})
        resp = web_client.get(
            "/integrations/github/recommendations", headers=_h(admin_token),
        )
        assert resp.status_code == 503

    def test_503_when_unreachable(self, web_client, admin_token, github_stub):
        install, _ = github_stub
        install({}, raise_on={"/user"})
        resp = web_client.get(
            "/integrations/github/recommendations", headers=_h(admin_token),
        )
        assert resp.status_code == 503

    def test_invalid_token_path_sanitised(self, web_client, admin_token,
                                          github_stub):
        install, _ = github_stub
        # A 401 on /user makes the connector report "unavailable" with
        # a short canned detail — the configured token must not leak.
        install({
            ("GET", "/user"): _FakeResponse(401, {"message": "Bad credentials"}),
        })
        resp = web_client.get(
            "/integrations/github/recommendations", headers=_h(admin_token),
        )
        assert resp.status_code == 503
        assert SECRET_FRAGMENT not in resp.text


# ── Token safety ───────────────────────────────────────────────────


class TestTokenSafety:
    def test_token_never_in_response(self, web_client, admin_token, github_stub):
        install, _ = github_stub
        install({
            ("GET", "/user"): _FakeResponse(200, {"login": "octocat"}),
            ("GET", "/repos/octocat/Hello-World/issues"): _FakeResponse(
                200, [_issue(1, title="Add tests", labels=["tests"])],
            ),
        })
        resp = web_client.get(
            "/integrations/github/recommendations", headers=_h(admin_token),
        )
        assert resp.status_code == 200
        assert SECRET_FRAGMENT not in resp.text

    def test_token_never_in_error_response(self, monkeypatch, web_client,
                                           admin_token, github_stub):
        install, _ = github_stub
        install({}, raise_on={"/user", "/repos/octocat/Hello-World/issues"})
        resp = web_client.get(
            "/integrations/github/recommendations", headers=_h(admin_token),
        )
        assert SECRET_FRAGMENT not in resp.text

    def test_token_omitted_from_recommend_issues_output(self, github_stub):
        install, _ = github_stub
        install({
            ("GET", "/repos/octocat/Hello-World/issues"): _FakeResponse(
                200, [_issue(1, title="Add tests", labels=["tests"])],
            ),
        })
        recs = triage.recommend_issues("octocat", "Hello-World")
        assert SECRET_FRAGMENT not in repr(recs)

    def test_logger_messages_omit_token(self, github_stub, caplog):
        install, _ = github_stub
        install({}, raise_on={"/repos/octocat/Hello-World/issues"})
        with caplog.at_level("DEBUG", logger=gh.logger.name):
            triage.recommend_issues("octocat", "Hello-World")
        for record in caplog.records:
            assert SECRET_FRAGMENT not in record.getMessage()
            assert SECRET_FRAGMENT not in str(record.args or "")
