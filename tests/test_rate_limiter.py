"""
Tests for core/rate_limiter.py — _SlidingWindowLimiter and the /login integration.
"""

import contextlib
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import web
from core.rate_limiter import (
    _SlidingWindowLimiter,
    _client_ip,
    _parse_trusted_proxies,
)


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
        if client_host is None:
            req.client = None
        else:
            req.client = MagicMock()
            req.client.host = client_host
        return req

    # ── Direct-connection (untrusted) behaviour ────────────────────────────────

    def test_uses_direct_ip_when_no_proxy_header(self):
        """No XFF, no trusted proxies — direct IP wins."""
        req = self._request()
        assert _client_ip(req, trusted_proxies=frozenset()) == "1.2.3.4"

    def test_returns_unknown_when_no_client(self):
        req = self._request(client_host=None)
        assert _client_ip(req, trusted_proxies=frozenset()) == "unknown"

    # ── Spoofing protection ────────────────────────────────────────────────────

    def test_direct_client_with_spoofed_xff_uses_direct_ip(self):
        """
        Untrusted client sends X-Forwarded-For trying to impersonate another IP.
        The header must be ignored and the direct connection IP used instead.
        """
        req = self._request(
            forwarded_for="1.1.1.1",          # spoofed value
            client_host="203.0.113.50",       # real attacker IP
        )
        result = _client_ip(req, trusted_proxies=frozenset())
        assert result == "203.0.113.50"

    def test_xff_ignored_when_direct_ip_not_in_trusted_set(self):
        """Even with XFF and a populated trusted set, mismatched direct IP → ignore XFF."""
        req = self._request(
            forwarded_for="1.1.1.1",
            client_host="203.0.113.50",
        )
        result = _client_ip(req, trusted_proxies=frozenset({"127.0.0.1", "::1"}))
        assert result == "203.0.113.50"

    # ── Trusted-proxy behaviour ────────────────────────────────────────────────

    def test_trusted_proxy_extracts_first_xff_entry(self):
        """When the direct IP IS in the trusted set, the leftmost XFF entry is used."""
        req = self._request(
            forwarded_for="198.51.100.7, 10.0.0.1, 10.0.0.2",
            client_host="127.0.0.1",
        )
        result = _client_ip(req, trusted_proxies=frozenset({"127.0.0.1"}))
        assert result == "198.51.100.7"

    def test_trusted_proxy_strips_whitespace_in_xff(self):
        req = self._request(
            forwarded_for="   198.51.100.7   ,  10.0.0.1",
            client_host="127.0.0.1",
        )
        result = _client_ip(req, trusted_proxies=frozenset({"127.0.0.1"}))
        assert result == "198.51.100.7"

    def test_trusted_proxy_with_empty_xff_falls_back_to_direct(self):
        """Trusted proxy but no XFF header → direct IP (the proxy itself)."""
        req = self._request(client_host="127.0.0.1")
        result = _client_ip(req, trusted_proxies=frozenset({"127.0.0.1"}))
        assert result == "127.0.0.1"


# ── _parse_trusted_proxies ─────────────────────────────────────────────────────

class TestParseTrustedProxies:
    def test_empty_string_yields_empty_set(self):
        """Empty/missing config must trust no proxy."""
        assert _parse_trusted_proxies("") == frozenset()

    def test_whitespace_only_yields_empty_set(self):
        assert _parse_trusted_proxies("   ,  ,") == frozenset()

    def test_single_value(self):
        assert _parse_trusted_proxies("127.0.0.1") == frozenset({"127.0.0.1"})

    def test_multiple_values_with_whitespace(self):
        """Comma-separated list must be parsed and trimmed."""
        result = _parse_trusted_proxies("127.0.0.1, ::1 ,10.0.0.5")
        assert result == frozenset({"127.0.0.1", "::1", "10.0.0.5"})

    def test_returns_frozenset(self):
        """Result must be immutable so the trusted set cannot be mutated at runtime."""
        result = _parse_trusted_proxies("127.0.0.1")
        assert isinstance(result, frozenset)


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
        fake_user = MagicMock()
        with patch("web.authenticate", return_value=fake_user), \
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

        # TestClient connects from a virtual host called "testclient"; whitelist
        # it so the spoof-protected _client_ip honours the X-Forwarded-For
        # header set by _post_login(). Without this patch, every request would
        # collapse onto the single key "testclient".
        with patch("core.rate_limiter._login_limiter.is_allowed", side_effect=side_effect), \
             patch("core.rate_limiter._TRUSTED_PROXIES", frozenset({"testclient"})), \
             patch("web.authenticate", return_value=None):
            # Exhaust IP A
            _post_login(client, ip="192.168.0.1")
            _post_login(client, ip="192.168.0.1")
            blocked = _post_login(client, ip="192.168.0.1")

            # IP B must still be allowed through (gets a 401, not 429)
            unblocked = _post_login(client, ip="192.168.0.2")

        assert blocked.status_code == 429
        assert unblocked.status_code == 401  # wrong credentials, not rate limited
