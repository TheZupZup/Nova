"""
Tests for the Alpha channel GitHub OAuth guard.

Automated tests cover the middleware logic; the manual steps below are for
verifying a live deployment end-to-end.

Manual test steps
-----------------
1. Alpha without login
   - Visit https://alpha.nova.borealnode.ca
   - Expected: browser is redirected to GitHub OAuth, Nova UI is never shown

2. Alpha with a non-allowed GitHub account
   - Complete the GitHub OAuth flow as any account that is NOT in NOVA_ALPHA_ALLOWED_USERS
   - Expected: 403 "ACCESS DENIED" page with the GitHub username shown

3. Alpha with TheZupZup
   - Complete the GitHub OAuth flow as TheZupZup
   - Expected: Nova login screen appears normally

4. Stable / Beta (no GitHub gate)
   - Visit https://nova.borealnode.ca or https://beta.nova.borealnode.ca
   - Expected: Nova login screen appears with no GitHub redirect

Limitation: sessions are stored in process memory. They are lost on server
restart and are not shared across multiple workers or containers. For a
single-process Alpha instance this is acceptable. A persistent session
store (e.g. Redis) would be needed for multi-worker deployments.
"""

import contextlib
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

import web


# ── Helpers ────────────────────────────────────────────────────────────────────

def _alpha_stack(stack: contextlib.ExitStack) -> None:
    """Enter patches that simulate NOVA_CHANNEL=alpha with stub OAuth credentials."""
    stack.enter_context(patch("web.NOVA_CHANNEL", "alpha"))
    stack.enter_context(patch("web.GITHUB_CLIENT_ID", "test-client-id"))
    stack.enter_context(patch("web.GITHUB_CLIENT_SECRET", "test-client-secret"))
    _suppress_startup(stack)


def _suppress_startup(stack: contextlib.ExitStack) -> None:
    """Suppress DB init, background learning, and the APScheduler."""
    stack.enter_context(patch("web.initialize_db"))
    stack.enter_context(patch("web.learn_from_feeds"))
    stack.enter_context(patch("web.scheduler", MagicMock()))


def _inject_session(github_user: str) -> str:
    """Insert an authenticated GitHub session directly into the store, return SID."""
    sid = f"test-sid-{github_user}"
    web._sessions[sid] = {
        "data": {"github_user": github_user},
        "exp": time.time() + 3600,
    }
    return sid


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def alpha_client():
    """TestClient with NOVA_CHANNEL=alpha and stub OAuth credentials."""
    with contextlib.ExitStack() as stack:
        _alpha_stack(stack)
        web._sessions.clear()
        with TestClient(web.app, raise_server_exceptions=True) as client:
            yield client


@pytest.fixture()
def stable_client():
    """TestClient with NOVA_CHANNEL=stable — guard must not activate."""
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("web.NOVA_CHANNEL", "stable"))
        _suppress_startup(stack)
        web._sessions.clear()
        with TestClient(web.app, raise_server_exceptions=True) as client:
            yield client


# ── is_allowed() ───────────────────────────────────────────────────────────────

class TestIsAllowed:
    def test_listed_user_is_allowed(self):
        with patch("core.github_oauth.NOVA_ALPHA_ALLOWED_USERS", frozenset({"thezupzup"})):
            from core.github_oauth import is_allowed
            assert is_allowed("TheZupZup") is True

    def test_match_is_case_insensitive(self):
        with patch("core.github_oauth.NOVA_ALPHA_ALLOWED_USERS", frozenset({"thezupzup"})):
            from core.github_oauth import is_allowed
            assert is_allowed("THEZUPZUP") is True
            assert is_allowed("thezupzup") is True

    def test_unlisted_user_is_denied(self):
        with patch("core.github_oauth.NOVA_ALPHA_ALLOWED_USERS", frozenset({"thezupzup"})):
            from core.github_oauth import is_allowed
            assert is_allowed("attacker") is False
            assert is_allowed("") is False

    def test_empty_allowlist_denies_everyone(self):
        with patch("core.github_oauth.NOVA_ALPHA_ALLOWED_USERS", frozenset()):
            from core.github_oauth import is_allowed
            assert is_allowed("TheZupZup") is False


# ── Alpha channel guard ─────────────────────────────────────────────────────────

class TestAlphaGuard:
    def test_unauthenticated_browser_redirects_to_oauth(self, alpha_client):
        """Alpha without any session → redirect to the GitHub OAuth initiation route."""
        resp = alpha_client.get("/", follow_redirects=False)
        assert resp.status_code in (302, 307)
        assert "/auth/github" in resp.headers["location"]

    def test_unauthenticated_api_call_returns_401_json(self, alpha_client):
        """Bearer-token API calls without a session get a JSON 401, not an HTML redirect."""
        resp = alpha_client.get(
            "/health",
            headers={"Authorization": "Bearer some.jwt.token"},
            follow_redirects=False,
        )
        assert resp.status_code == 401
        body = resp.json()
        assert body["detail"] == "GitHub authentication required."

    def test_non_allowed_github_user_gets_403(self, alpha_client):
        """A GitHub user not in NOVA_ALPHA_ALLOWED_USERS receives an access-denied page."""
        sid = _inject_session("notallowed")
        with patch("web.is_allowed", return_value=False):
            resp = alpha_client.get(
                "/",
                cookies={web._SESSION_COOKIE: sid},
                follow_redirects=False,
            )
        assert resp.status_code == 403
        assert "ACCESS DENIED" in resp.text
        assert "@notallowed" in resp.text

    def test_allowed_user_reaches_app(self, alpha_client):
        """An allowed GitHub user passes the guard and the underlying handler responds."""
        sid = _inject_session("thezupzup")
        with patch("web.is_allowed", return_value=True):
            resp = alpha_client.get(
                "/health",
                cookies={web._SESSION_COOKIE: sid},
                follow_redirects=False,
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_auth_paths_bypass_guard(self, alpha_client):
        """/auth/* routes are always reachable so the OAuth flow can complete."""
        resp = alpha_client.get("/auth/github", follow_redirects=False)
        # Must redirect toward GitHub, not loop back to itself
        assert resp.status_code in (302, 307)
        assert "github.com" in resp.headers.get("location", "")

    def test_missing_oauth_config_returns_503(self):
        """Alpha with empty GITHUB_CLIENT_ID/SECRET returns 503 instead of crashing."""
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("web.NOVA_CHANNEL", "alpha"))
            stack.enter_context(patch("web.GITHUB_CLIENT_ID", ""))
            stack.enter_context(patch("web.GITHUB_CLIENT_SECRET", ""))
            _suppress_startup(stack)
            web._sessions.clear()
            with TestClient(web.app, raise_server_exceptions=True) as client:
                resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 503

    def test_expired_session_redirects_to_oauth(self, alpha_client):
        """A session whose TTL has passed is treated as unauthenticated."""
        sid = "expired-session"
        web._sessions[sid] = {
            "data": {"github_user": "thezupzup"},
            "exp": time.time() - 1,  # already expired
        }
        resp = alpha_client.get(
            "/",
            cookies={web._SESSION_COOKIE: sid},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 307)
        assert "/auth/github" in resp.headers["location"]


# ── Stable and Beta are unaffected ─────────────────────────────────────────────

class TestPublicChannels:
    def test_stable_health_is_public(self, stable_client):
        """Stable channel: /health is reachable with no GitHub session."""
        resp = stable_client.get("/health")
        assert resp.status_code == 200

    def test_beta_health_is_public(self):
        """Beta channel: /health is reachable with no GitHub session."""
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("web.NOVA_CHANNEL", "beta"))
            _suppress_startup(stack)
            web._sessions.clear()
            with TestClient(web.app, raise_server_exceptions=True) as client:
                resp = client.get("/health")
        assert resp.status_code == 200

    def test_stable_index_is_not_gated(self, stable_client):
        """Stable channel: the root path must not redirect to /auth/github."""
        resp = stable_client.get("/", follow_redirects=False)
        location = resp.headers.get("location", "")
        assert "/auth/github" not in location


# ── Session cookie security properties ─────────────────────────────────────────

class TestSessionCookieProperties:
    def test_cookie_is_httponly(self, alpha_client):
        """/auth/github must set a cookie with the HttpOnly flag."""
        resp = alpha_client.get("/auth/github", follow_redirects=False)
        assert "httponly" in resp.headers.get("set-cookie", "").lower()

    def test_cookie_has_samesite_lax(self, alpha_client):
        """/auth/github must set a cookie with SameSite=Lax."""
        resp = alpha_client.get("/auth/github", follow_redirects=False)
        assert "samesite=lax" in resp.headers.get("set-cookie", "").lower()


# ── Session store: TTL purge ────────────────────────────────────────────────────

class TestSessionPurge:
    def test_expired_entries_are_removed_on_next_create(self):
        """_session_create must evict all expired entries before inserting a new one."""
        web._sessions.clear()
        for i in range(3):
            web._sessions[f"stale-{i}"] = {"data": {}, "exp": time.time() - 1}

        web._session_create({"oauth_state": "x"})

        # Purge removed the 3 stale entries; _session_create added exactly 1 new one
        assert len(web._sessions) == 1
        assert not any(k.startswith("stale-") for k in web._sessions)

    def test_valid_entries_are_not_purged(self):
        """_session_purge must leave sessions that have not yet expired."""
        web._sessions.clear()
        sid = web._session_create({"github_user": "thezupzup"})
        web._session_purge()
        assert sid in web._sessions


# ── github_oauth: hardened network calls ───────────────────────────────────────

class TestOAuthNetworkHardening:
    @pytest.mark.anyio
    async def test_exchange_code_returns_none_on_timeout(self):
        with patch("core.github_oauth.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__.return_value.post.side_effect = (
                httpx.TimeoutException("timed out")
            )
            from core.github_oauth import exchange_code
            result = await exchange_code("any-code")
        assert result is None

    @pytest.mark.anyio
    async def test_exchange_code_returns_none_on_transport_error(self):
        with patch("core.github_oauth.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__.return_value.post.side_effect = (
                httpx.ConnectError("refused")
            )
            from core.github_oauth import exchange_code
            result = await exchange_code("any-code")
        assert result is None

    @pytest.mark.anyio
    async def test_exchange_code_returns_none_on_invalid_json(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("not json")
        with patch("core.github_oauth.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__.return_value.post.return_value = mock_resp
            from core.github_oauth import exchange_code
            result = await exchange_code("any-code")
        assert result is None

    @pytest.mark.anyio
    async def test_fetch_username_returns_none_on_timeout(self):
        with patch("core.github_oauth.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__.return_value.get.side_effect = (
                httpx.TimeoutException("timed out")
            )
            from core.github_oauth import fetch_username
            result = await fetch_username("some-token")
        assert result is None

    @pytest.mark.anyio
    async def test_fetch_username_returns_none_on_transport_error(self):
        with patch("core.github_oauth.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__.return_value.get.side_effect = (
                httpx.ConnectError("refused")
            )
            from core.github_oauth import fetch_username
            result = await fetch_username("some-token")
        assert result is None

    @pytest.mark.anyio
    async def test_fetch_username_returns_none_on_invalid_json(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("not json")
        with patch("core.github_oauth.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__.return_value.get.return_value = mock_resp
            from core.github_oauth import fetch_username
            result = await fetch_username("some-token")
        assert result is None
