"""
Tests for core/rate_limiter.py — _SlidingWindowLimiter and the /login integration.
"""

import contextlib
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import web
from core.rate_limiter import _SlidingWindowLimiter, _client_ip


# ── _SlidingWindowLimiter unit tests ──────────────────────────────────────────

class TestSlidingWindowLimiter:
    def _limiter(self, max_attempts=3, window=60):
        return _SlidingWindowLimiter(max_attempts=max_attempts, window_seconds=window)

    def test_first_attempts_are_allowed(self):
        lim = self._limiter(max_attempts=3)
        for _ in range(3):
            allowed, _ = lim.is_allowed("ip1")
            assert allowed is True

    def test_exceeding_limit_is_denied(self):
        lim = self._limiter(max_attempts=3)
        for _ in range(3):
            lim.is_allowed("ip1")
        allowed, retry_after = lim.is_allowed("ip1")
        assert allowed is False
        assert retry_after > 0

    def test_retry_after_is_positive_integer(self):
        lim = self._limiter(max_attempts=1, window=60)
        lim.is_allowed("ip1")
        _, retry_after = lim.is_allowed("ip1")
        assert isinstance(retry_after, int)
        assert 0 < retry_after <= 61

    def test_different_keys_are_independent(self):
        lim = self._limiter(max_attempts=1)
        lim.is_allowed("ip1")
        allowed, _ = lim.is_allowed("ip2")
        assert allowed is True

    def test_window_expiry_resets_counter(self):
        lim = self._limiter(max_attempts=1, window=1)
        lim.is_allowed("ip1")  # fills the slot

        # Manually backdate the stored timestamp so the window has elapsed.
        with lim._lock:
            bucket = lim._store["ip1"]
            bucket[0] = time.monotonic() - 2  # 2 s ago, window is 1 s

        allowed, _ = lim.is_allowed("ip1")
        assert allowed is True

    def test_is_thread_safe(self):
        """Concurrent access must not corrupt the counter."""
        import threading

        lim = self._limiter(max_attempts=100, window=60)
        results = []
        lock = threading.Lock()

        def hit():
            ok, _ = lim.is_allowed("shared")
            with lock:
                results.append(ok)

        threads = [threading.Thread(target=hit) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 50
        assert all(r is True for r in results)


# ── _client_ip extraction ──────────────────────────────────────────────────────

class TestClientIp:
    def _request(self, forwarded_for=None, client_host="1.2.3.4"):
        req = MagicMock()
        req.headers = {}
        if forwarded_for is not None:
            req.headers = {"x-forwarded-for": forwarded_for}
        req.client = MagicMock()
        req.client.host = client_host
        return req

    def test_uses_direct_ip_when_no_proxy_header(self):
        req = self._request()
        assert _client_ip(req) == "1.2.3.4"

    def test_uses_first_entry_of_forwarded_for(self):
        req = self._request(forwarded_for="10.0.0.1, 10.0.0.2, 10.0.0.3")
        assert _client_ip(req) == "10.0.0.1"

    def test_strips_whitespace_from_forwarded_for(self):
        req = self._request(forwarded_for="  192.168.1.1  , 10.0.0.1")
        assert _client_ip(req) == "192.168.1.1"

    def test_returns_unknown_when_no_client(self):
        req = MagicMock()
        req.headers = {}
        req.client = None
        assert _client_ip(req) == "unknown"


# ── /login integration tests ───────────────────────────────────────────────────

@pytest.fixture()
def client():
    """TestClient with DB, scheduler, and background jobs suppressed."""
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("web.initialize_db"))
        stack.enter_context(patch("web.learn_from_feeds"))
        stack.enter_context(patch("web.scheduler", MagicMock()))
        with TestClient(web.app, raise_server_exceptions=True) as c:
            yield c


def _post_login(client, username="nova", password="nova", ip="1.2.3.4"):
    return client.post(
        "/login",
        json={"username": username, "password": password},
        headers={"X-Forwarded-For": ip},
    )


class TestLoginRateLimiting:
    def test_successful_login_within_limit(self, client):
        with patch("web.verify_credentials", return_value=True), \
             patch("web.create_token", return_value="tok"), \
             patch("core.rate_limiter._login_limiter.is_allowed", return_value=(True, 0)):
            resp = _post_login(client, ip="10.0.0.1")
        assert resp.status_code == 200
        assert resp.json()["token"] == "tok"

    def test_returns_429_when_limit_exceeded(self, client):
        with patch("core.rate_limiter._login_limiter.is_allowed", return_value=(False, 42)):
            resp = _post_login(client, ip="10.0.0.2")
        assert resp.status_code == 429
        assert "42" in resp.json()["detail"]

    def test_retry_after_header_is_present_on_429(self, client):
        with patch("core.rate_limiter._login_limiter.is_allowed", return_value=(False, 30)):
            resp = _post_login(client, ip="10.0.0.3")
        assert resp.headers.get("retry-after") == "30"

    def test_error_message_is_user_friendly(self, client):
        with patch("core.rate_limiter._login_limiter.is_allowed", return_value=(False, 15)):
            resp = _post_login(client, ip="10.0.0.4")
        detail = resp.json()["detail"]
        assert "Too many login attempts" in detail
        assert "15" in detail

    def test_different_ips_are_not_blocked_together(self, client):
        """Exhausting one IP must not affect another IP."""
        real_limiter = _SlidingWindowLimiter(max_attempts=2, window_seconds=60)

        def side_effect(key):
            return real_limiter.is_allowed(key)

        with patch("core.rate_limiter._login_limiter.is_allowed", side_effect=side_effect), \
             patch("web.verify_credentials", return_value=False):
            # Exhaust IP A
            _post_login(client, ip="192.168.0.1")
            _post_login(client, ip="192.168.0.1")
            blocked = _post_login(client, ip="192.168.0.1")

            # IP B must still be allowed through (gets a 401, not 429)
            unblocked = _post_login(client, ip="192.168.0.2")

        assert blocked.status_code == 429
        assert unblocked.status_code == 401  # wrong credentials, not rate limited
