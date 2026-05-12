"""
Tests for the optional read-only GitHub connector (issue #119).

Scope of the suite mirrors the issue's testing requirements:

  * disabled connector reports ``disabled`` and never reaches the wire,
  * missing token reports ``not_configured`` without leaking secrets,
  * invalid token / HTTP errors are sanitised and surface as
    ``unavailable`` with no token bleed,
  * read-only issue and pull request listing works against a mocked
    GitHub response,
  * the connector module exposes no write helpers and contains no
    POST / PUT / PATCH / DELETE calls,
  * the FastAPI endpoints are admin-only — non-admin and restricted
    users get a 403,
  * the configured token never appears in any returned JSON body or
    error message.

The HTTP transport is stubbed via the same ``httpx.Client`` factory
swap pattern used by the NexaNote integration tests, so no real
network call is ever issued.
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
from memory import store as natural_store


SECRET_TOKEN = "ghp_TESTTOKEN_must_never_leak_1234567890"
SECRET_FRAGMENT = "TESTTOKEN_must_never_leak"


# ── Fixtures ────────────────────────────────────────────────────────


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


# ── Fake httpx client ───────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any = None,
                 headers: dict | None = None, raise_decode: bool = False):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self._raise_decode = raise_decode

    def json(self):
        if self._raise_decode:
            raise ValueError("decode error")
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
    """Replace ``httpx.Client`` in the connector and enable the integration.

    Returns ``(install, calls)``; ``install(scripted, raise_on=...)``
    sets the scripted responses, and ``calls`` accumulates every
    outbound request (method, path, kwargs, captured headers).
    """
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


# ── Switches ────────────────────────────────────────────────────────


class TestSwitches:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.setattr(gh, "NOVA_GITHUB_ENABLED", False)
        assert gh.is_enabled() is False

    def test_enabled_when_env_true(self, monkeypatch):
        monkeypatch.setattr(gh, "NOVA_GITHUB_ENABLED", True)
        assert gh.is_enabled() is True

    def test_read_only_is_true_by_default(self, monkeypatch):
        monkeypatch.setattr(gh, "NOVA_GITHUB_READ_ONLY", True)
        assert gh.is_read_only() is True


# ── Status states ───────────────────────────────────────────────────


class TestStatus:
    def test_disabled_when_switch_off(self, monkeypatch):
        monkeypatch.setattr(gh, "NOVA_GITHUB_ENABLED", False)
        monkeypatch.setattr(gh, "NOVA_GITHUB_TOKEN", SECRET_TOKEN)
        s = gh.status()
        assert s.state == gh.STATE_DISABLED
        assert s.enabled is False

    def test_not_configured_when_token_missing(self, monkeypatch):
        monkeypatch.setattr(gh, "NOVA_GITHUB_ENABLED", True)
        monkeypatch.setattr(gh, "NOVA_GITHUB_TOKEN", "")
        s = gh.status()
        assert s.state == gh.STATE_NOT_CONFIGURED
        assert s.enabled is True

    def test_connected_on_2xx(self, github_stub):
        install, _ = github_stub
        install({
            ("GET", "/user"): _FakeResponse(
                200, {"login": "octocat"},
                headers={"X-OAuth-Scopes": "repo, read:org"},
            ),
        })
        s = gh.status()
        assert s.state == gh.STATE_CONNECTED
        assert s.authenticated_login == "octocat"
        assert s.scopes == ("repo", "read:org")

    def test_unavailable_on_401(self, github_stub):
        install, _ = github_stub
        install({("GET", "/user"): _FakeResponse(401, {"message": "Bad credentials"})})
        s = gh.status()
        assert s.state == gh.STATE_UNAVAILABLE

    def test_unavailable_on_5xx(self, github_stub):
        install, _ = github_stub
        install({("GET", "/user"): _FakeResponse(503)})
        s = gh.status()
        assert s.state == gh.STATE_UNAVAILABLE

    def test_unavailable_on_network_error(self, github_stub):
        install, _ = github_stub
        install({}, raise_on={"/user"})
        s = gh.status()
        assert s.state == gh.STATE_UNAVAILABLE

    def test_no_http_when_disabled(self, monkeypatch, github_stub):
        install, calls = github_stub
        monkeypatch.setattr(gh, "NOVA_GITHUB_ENABLED", False)
        install({("GET", "/user"): _FakeResponse(200, {"login": "octocat"})})
        gh.status()
        assert calls == []

    def test_no_http_when_token_missing(self, monkeypatch, github_stub):
        install, calls = github_stub
        monkeypatch.setattr(gh, "NOVA_GITHUB_TOKEN", "")
        install({("GET", "/user"): _FakeResponse(200, {"login": "octocat"})})
        gh.status()
        assert calls == []


# ── Read API ────────────────────────────────────────────────────────


_ISSUE_PAYLOAD = {
    "number": 42,
    "title": "Add tests",
    "state": "open",
    "user": {"login": "alice"},
    "labels": [{"name": "bug"}, {"name": "good first issue"}],
    "comments": 3,
    "created_at": "2024-01-01T00:00:00Z",
    "updated_at": "2024-01-02T00:00:00Z",
    "closed_at": None,
    "html_url": "https://github.com/octocat/Hello-World/issues/42",
    "body": "Body of the issue",
}

_PR_PAYLOAD = {
    "number": 7,
    "title": "Add feature",
    "state": "open",
    "draft": False,
    "merged": False,
    "user": {"login": "bob"},
    "labels": [{"name": "enhancement"}],
    "head": {"ref": "feature-branch"},
    "base": {"ref": "main"},
    "created_at": "2024-01-03T00:00:00Z",
    "updated_at": "2024-01-04T00:00:00Z",
    "closed_at": None,
    "merged_at": None,
    "html_url": "https://github.com/octocat/Hello-World/pull/7",
    "body": "Body of the PR",
}


class TestListIssues:
    def test_returns_sanitised_issues(self, github_stub):
        install, calls = github_stub
        install({
            ("GET", "/repos/octocat/Hello-World/issues"):
                _FakeResponse(200, [_ISSUE_PAYLOAD]),
        })
        result = gh.list_issues("octocat", "Hello-World")
        assert result == [{
            "number": 42,
            "title": "Add tests",
            "state": "open",
            "user": "alice",
            "labels": ["bug", "good first issue"],
            "comments": 3,
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-02T00:00:00Z",
            "closed_at": None,
            "html_url": "https://github.com/octocat/Hello-World/issues/42",
        }]
        # The list view never includes the body — it stays small and chat-safe.
        assert "body" not in result[0]
        assert calls[-1]["kwargs"]["params"]["state"] == "open"

    def test_skips_pull_requests_in_issue_listing(self, github_stub):
        install, _ = github_stub
        install({
            ("GET", "/repos/octocat/Hello-World/issues"): _FakeResponse(
                200,
                [
                    _ISSUE_PAYLOAD,
                    {**_ISSUE_PAYLOAD, "number": 99, "pull_request": {"url": "..."}},
                ],
            ),
        })
        result = gh.list_issues("octocat", "Hello-World")
        assert [i["number"] for i in result] == [42]

    def test_empty_when_disabled(self, monkeypatch, github_stub):
        install, calls = github_stub
        monkeypatch.setattr(gh, "NOVA_GITHUB_ENABLED", False)
        install({
            ("GET", "/repos/octocat/Hello-World/issues"):
                _FakeResponse(200, [_ISSUE_PAYLOAD]),
        })
        assert gh.list_issues("octocat", "Hello-World") == []
        assert calls == []

    def test_empty_on_network_error(self, github_stub):
        install, _ = github_stub
        install({}, raise_on={"/repos/octocat/Hello-World/issues"})
        assert gh.list_issues("octocat", "Hello-World") == []

    def test_empty_on_invalid_slug(self, github_stub):
        install, calls = github_stub
        install({})
        assert gh.list_issues("../etc", "passwd") == []
        assert gh.list_issues("octocat", "../etc") == []
        assert calls == []

    def test_state_filter_coerced(self, github_stub):
        install, calls = github_stub
        install({
            ("GET", "/repos/octocat/Hello-World/issues"):
                _FakeResponse(200, [_ISSUE_PAYLOAD]),
        })
        gh.list_issues("octocat", "Hello-World", state="banana")
        assert calls[-1]["kwargs"]["params"]["state"] == "open"

    def test_limit_clamped(self, github_stub):
        install, calls = github_stub
        install({
            ("GET", "/repos/octocat/Hello-World/issues"):
                _FakeResponse(200, [_ISSUE_PAYLOAD]),
        })
        gh.list_issues("octocat", "Hello-World", limit=10_000)
        assert calls[-1]["kwargs"]["params"]["per_page"] == 100


class TestGetIssue:
    def test_returns_issue_with_body(self, github_stub):
        install, _ = github_stub
        install({
            ("GET", "/repos/octocat/Hello-World/issues/42"):
                _FakeResponse(200, _ISSUE_PAYLOAD),
        })
        out = gh.get_issue("octocat", "Hello-World", 42)
        assert out["number"] == 42
        assert out["body"] == "Body of the issue"
        assert out["body_truncated"] is False

    def test_returns_none_for_pull_request(self, github_stub):
        install, _ = github_stub
        install({
            ("GET", "/repos/octocat/Hello-World/issues/77"): _FakeResponse(
                200, {**_ISSUE_PAYLOAD, "number": 77, "pull_request": {"url": "..."}},
            ),
        })
        assert gh.get_issue("octocat", "Hello-World", 77) is None

    def test_returns_none_on_404(self, github_stub):
        install, _ = github_stub
        install({("GET", "/repos/octocat/Hello-World/issues/99"): _FakeResponse(404)})
        assert gh.get_issue("octocat", "Hello-World", 99) is None

    def test_invalid_number_returns_none(self, github_stub):
        install, calls = github_stub
        install({})
        assert gh.get_issue("octocat", "Hello-World", 0) is None
        assert gh.get_issue("octocat", "Hello-World", "../etc") is None
        assert calls == []

    def test_body_truncation(self, github_stub):
        install, _ = github_stub
        big = "x" * 20_000
        install({
            ("GET", "/repos/octocat/Hello-World/issues/42"): _FakeResponse(
                200, {**_ISSUE_PAYLOAD, "body": big},
            ),
        })
        out = gh.get_issue("octocat", "Hello-World", 42)
        assert len(out["body"]) == 16_000
        assert out["body_truncated"] is True


class TestListPulls:
    def test_returns_sanitised_pulls(self, github_stub):
        install, _ = github_stub
        install({
            ("GET", "/repos/octocat/Hello-World/pulls"):
                _FakeResponse(200, [_PR_PAYLOAD]),
        })
        result = gh.list_pull_requests("octocat", "Hello-World")
        assert result == [{
            "number": 7,
            "title": "Add feature",
            "state": "open",
            "draft": False,
            "merged": False,
            "user": "bob",
            "labels": ["enhancement"],
            "head": "feature-branch",
            "base": "main",
            "created_at": "2024-01-03T00:00:00Z",
            "updated_at": "2024-01-04T00:00:00Z",
            "closed_at": None,
            "merged_at": None,
            "html_url": "https://github.com/octocat/Hello-World/pull/7",
        }]

    def test_empty_on_network_error(self, github_stub):
        install, _ = github_stub
        install({}, raise_on={"/repos/octocat/Hello-World/pulls"})
        assert gh.list_pull_requests("octocat", "Hello-World") == []

    def test_empty_when_token_missing(self, monkeypatch, github_stub):
        install, calls = github_stub
        monkeypatch.setattr(gh, "NOVA_GITHUB_TOKEN", "")
        install({
            ("GET", "/repos/octocat/Hello-World/pulls"):
                _FakeResponse(200, [_PR_PAYLOAD]),
        })
        assert gh.list_pull_requests("octocat", "Hello-World") == []
        assert calls == []


class TestGetPull:
    def test_returns_pr_with_body(self, github_stub):
        install, _ = github_stub
        install({
            ("GET", "/repos/octocat/Hello-World/pulls/7"):
                _FakeResponse(200, _PR_PAYLOAD),
        })
        out = gh.get_pull_request("octocat", "Hello-World", 7)
        assert out["number"] == 7
        assert out["body"] == "Body of the PR"

    def test_returns_none_on_404(self, github_stub):
        install, _ = github_stub
        install({("GET", "/repos/octocat/Hello-World/pulls/99"): _FakeResponse(404)})
        assert gh.get_pull_request("octocat", "Hello-World", 99) is None


class TestRepoResolution:
    def test_parses_default_repo(self, monkeypatch):
        monkeypatch.setattr(gh, "NOVA_GITHUB_DEFAULT_REPO", "octocat/Hello-World")
        assert gh.resolve_repo(None) == ("octocat", "Hello-World")

    def test_explicit_repo_wins(self, monkeypatch):
        monkeypatch.setattr(gh, "NOVA_GITHUB_DEFAULT_REPO", "octocat/Hello-World")
        assert gh.resolve_repo("foo/bar") == ("foo", "bar")

    def test_invalid_spec_rejected(self):
        for bad in ("", "no-slash", "two/slashes/here", "../etc/passwd",
                    "owner/", "/repo", "owner/repo;rm -rf /"):
            assert gh.parse_repo_spec(bad) is None


# ── Token safety ────────────────────────────────────────────────────


class TestTokenSafety:
    def test_token_not_in_status_payload(self, github_stub):
        install, _ = github_stub
        install({
            ("GET", "/user"): _FakeResponse(200, {"login": "octocat"}),
        })
        serialised = repr(gh.status().as_dict())
        assert SECRET_FRAGMENT not in serialised

    def test_token_not_in_issue_payload(self, github_stub):
        install, _ = github_stub
        install({
            ("GET", "/repos/octocat/Hello-World/issues/42"):
                _FakeResponse(200, _ISSUE_PAYLOAD),
        })
        out = gh.get_issue("octocat", "Hello-World", 42)
        assert SECRET_FRAGMENT not in repr(out)

    def test_token_only_in_auth_header(self, github_stub):
        install, calls = github_stub
        install({("GET", "/user"): _FakeResponse(200, {"login": "octocat"})})
        gh.status()
        assert calls, "expected at least one outbound request"
        captured = calls[-1]["headers"]
        # Token must be present in the Authorization header...
        assert captured.get("Authorization") == f"Bearer {SECRET_TOKEN}"
        # ...and nowhere in the URL / params / query.
        assert SECRET_FRAGMENT not in captured["path"] if "path" in captured else True
        assert SECRET_FRAGMENT not in repr(calls[-1]["kwargs"])

    def test_logger_messages_omit_token(self, monkeypatch, github_stub, caplog):
        install, _ = github_stub
        install({}, raise_on={"/user"})
        with caplog.at_level("DEBUG", logger=gh.logger.name):
            gh.status()
        for record in caplog.records:
            assert SECRET_FRAGMENT not in record.getMessage()
            assert SECRET_FRAGMENT not in str(record.args or "")


# ── Module-level "no write code" enforcement ────────────────────────


class TestNoWriteCode:
    """The Phase-1 module must not call any write verb against GitHub.

    A future PR can add write helpers, but they must come with their
    own gating + audit + confirmation logic. This test fails fast if
    POST / PUT / PATCH / DELETE ever leak into the read-only module by
    accident.
    """

    def test_module_has_no_write_helpers(self):
        for name in (
            "create_issue", "close_issue", "comment_on_issue",
            "merge_pull_request", "approve_pull_request",
            "comment_on_pull_request", "create_pull_request",
        ):
            assert not hasattr(gh, name), (
                f"{name!r} should not exist in the Phase-1 connector"
            )

    def test_module_never_calls_write_verbs(self):
        with open(gh.__file__, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        forbidden = {"post", "put", "patch", "delete"}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            attr = getattr(func, "attr", None)
            if isinstance(attr, str) and attr.lower() in forbidden:
                raise AssertionError(
                    f"forbidden write verb {attr!r} called at line {node.lineno}"
                )

    def test_module_does_not_import_oauth_flow(self):
        """The maintainer connector must stay independent of the OAuth gate."""
        with open(gh.__file__, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert "github_oauth" not in module, (
                    f"connector must not import from {module!r}"
                )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert "github_oauth" not in alias.name, (
                        f"connector must not import {alias.name!r}"
                    )


# ── Endpoint: admin-only enforcement ────────────────────────────────


class TestEndpointsAdminOnly:
    @pytest.fixture(autouse=True)
    def _stub(self, github_stub):
        install, _ = github_stub
        install({
            ("GET", "/user"): _FakeResponse(200, {"login": "octocat"}),
            ("GET", "/repos/octocat/Hello-World/issues"):
                _FakeResponse(200, [_ISSUE_PAYLOAD]),
            ("GET", "/repos/octocat/Hello-World/issues/42"):
                _FakeResponse(200, _ISSUE_PAYLOAD),
            ("GET", "/repos/octocat/Hello-World/pulls"):
                _FakeResponse(200, [_PR_PAYLOAD]),
            ("GET", "/repos/octocat/Hello-World/pulls/7"):
                _FakeResponse(200, _PR_PAYLOAD),
        })

    @pytest.mark.parametrize("path", [
        "/integrations/github/status",
        "/integrations/github/issues",
        "/integrations/github/pulls",
        "/integrations/github/issues/42",
        "/integrations/github/pulls/7",
    ])
    def test_non_admin_user_forbidden(self, web_client, user_token, path):
        resp = web_client.get(path, headers=_h(user_token))
        assert resp.status_code == 403

    @pytest.mark.parametrize("path", [
        "/integrations/github/status",
        "/integrations/github/issues",
        "/integrations/github/pulls",
        "/integrations/github/issues/42",
        "/integrations/github/pulls/7",
    ])
    def test_restricted_user_forbidden(self, web_client, restricted_token, path):
        resp = web_client.get(path, headers=_h(restricted_token))
        assert resp.status_code == 403

    @pytest.mark.parametrize("path", [
        "/integrations/github/status",
        "/integrations/github/issues",
        "/integrations/github/pulls",
        "/integrations/github/issues/42",
        "/integrations/github/pulls/7",
    ])
    def test_unauthenticated_blocked(self, web_client, path):
        resp = web_client.get(path)
        assert resp.status_code in (401, 403)


# ── Endpoint: admin happy path ──────────────────────────────────────


class TestEndpointsAdmin:
    @pytest.fixture(autouse=True)
    def _stub(self, github_stub):
        install, _ = github_stub
        install({
            ("GET", "/user"): _FakeResponse(200, {"login": "octocat"}),
            ("GET", "/repos/octocat/Hello-World/issues"):
                _FakeResponse(200, [_ISSUE_PAYLOAD]),
            ("GET", "/repos/octocat/Hello-World/issues/42"):
                _FakeResponse(200, _ISSUE_PAYLOAD),
            ("GET", "/repos/octocat/Hello-World/pulls"):
                _FakeResponse(200, [_PR_PAYLOAD]),
            ("GET", "/repos/octocat/Hello-World/pulls/7"):
                _FakeResponse(200, _PR_PAYLOAD),
        })

    def test_status_endpoint(self, web_client, admin_token):
        resp = web_client.get(
            "/integrations/github/status", headers=_h(admin_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == gh.STATE_CONNECTED
        assert body["read_only"] is True
        assert "token" not in body
        assert SECRET_FRAGMENT not in resp.text

    def test_status_disabled_when_switch_off(
        self, monkeypatch, web_client, admin_token,
    ):
        monkeypatch.setattr(gh, "NOVA_GITHUB_ENABLED", False)
        resp = web_client.get(
            "/integrations/github/status", headers=_h(admin_token),
        )
        assert resp.status_code == 200
        assert resp.json()["state"] == gh.STATE_DISABLED

    def test_list_issues_default_repo(self, web_client, admin_token):
        resp = web_client.get(
            "/integrations/github/issues", headers=_h(admin_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["repo"] == "octocat/Hello-World"
        assert body["read_only"] is True
        assert body["issues"][0]["number"] == 42
        assert SECRET_FRAGMENT not in resp.text

    def test_list_issues_explicit_repo(self, web_client, admin_token, github_stub):
        install, _ = github_stub
        install({
            ("GET", "/user"): _FakeResponse(200, {"login": "octocat"}),
            ("GET", "/repos/foo/bar/issues"):
                _FakeResponse(200, [_ISSUE_PAYLOAD]),
        })
        resp = web_client.get(
            "/integrations/github/issues?repo=foo/bar",
            headers=_h(admin_token),
        )
        assert resp.status_code == 200
        assert resp.json()["repo"] == "foo/bar"

    def test_list_issues_400_without_repo(
        self, monkeypatch, web_client, admin_token,
    ):
        monkeypatch.setattr(gh, "NOVA_GITHUB_DEFAULT_REPO", "")
        resp = web_client.get(
            "/integrations/github/issues", headers=_h(admin_token),
        )
        assert resp.status_code == 400

    def test_get_issue(self, web_client, admin_token):
        resp = web_client.get(
            "/integrations/github/issues/42", headers=_h(admin_token),
        )
        assert resp.status_code == 200
        assert resp.json()["issue"]["title"] == "Add tests"
        assert SECRET_FRAGMENT not in resp.text

    def test_get_issue_404_when_missing(
        self, web_client, admin_token, github_stub,
    ):
        install, _ = github_stub
        install({
            ("GET", "/user"): _FakeResponse(200, {"login": "octocat"}),
            ("GET", "/repos/octocat/Hello-World/issues/99"): _FakeResponse(404),
        })
        resp = web_client.get(
            "/integrations/github/issues/99", headers=_h(admin_token),
        )
        assert resp.status_code == 404

    def test_list_pulls(self, web_client, admin_token):
        resp = web_client.get(
            "/integrations/github/pulls", headers=_h(admin_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["pull_requests"][0]["number"] == 7
        assert body["read_only"] is True

    def test_get_pull(self, web_client, admin_token):
        resp = web_client.get(
            "/integrations/github/pulls/7", headers=_h(admin_token),
        )
        assert resp.status_code == 200
        assert resp.json()["pull_request"]["title"] == "Add feature"

    def test_endpoints_503_when_unconfigured(
        self, monkeypatch, web_client, admin_token, github_stub,
    ):
        install, _ = github_stub
        monkeypatch.setattr(gh, "NOVA_GITHUB_TOKEN", "")
        install({})
        # status itself stays 200 with state="not_configured"
        resp = web_client.get(
            "/integrations/github/status", headers=_h(admin_token),
        )
        assert resp.status_code == 200
        assert resp.json()["state"] == gh.STATE_NOT_CONFIGURED
        # listing endpoints translate that into 503 so the caller does
        # not have to peek at the status body to know when to retry.
        for path in (
            "/integrations/github/issues",
            "/integrations/github/pulls",
            "/integrations/github/issues/42",
            "/integrations/github/pulls/7",
        ):
            resp = web_client.get(path, headers=_h(admin_token))
            assert resp.status_code == 503, path

    def test_endpoint_errors_do_not_leak_token(
        self, monkeypatch, web_client, admin_token, github_stub,
    ):
        install, _ = github_stub
        install({}, raise_on={"/user", "/repos/octocat/Hello-World/issues"})
        for path in (
            "/integrations/github/status",
            "/integrations/github/issues",
        ):
            resp = web_client.get(path, headers=_h(admin_token))
            assert SECRET_FRAGMENT not in resp.text, path


# ── Aggregate /integrations/status surface ──────────────────────────


class TestAggregateStatus:
    def test_admin_sees_github_state(self, web_client, admin_token, github_stub):
        install, _ = github_stub
        install({("GET", "/user"): _FakeResponse(200, {"login": "octocat"})})
        resp = web_client.get("/integrations/status", headers=_h(admin_token))
        assert resp.status_code == 200
        github = resp.json()["github"]
        assert github["state"] == gh.STATE_CONNECTED

    def test_non_admin_sees_disabled_github(
        self, web_client, user_token, github_stub,
    ):
        install, _ = github_stub
        install({("GET", "/user"): _FakeResponse(200, {"login": "octocat"})})
        resp = web_client.get("/integrations/status", headers=_h(user_token))
        assert resp.status_code == 200
        github = resp.json()["github"]
        assert github["state"] == gh.STATE_DISABLED
        assert github["enabled"] is False
