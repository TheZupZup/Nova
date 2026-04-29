"""
Per-IP sliding-window rate limiter for sensitive endpoints.

Usage (FastAPI dependency)::

    from core.rate_limiter import check_login_rate_limit

    @app.post("/login")
    def login(request: LoginRequest, _: None = Depends(check_login_rate_limit)):
        ...

Configuration (environment variables):

    LOGIN_RATE_LIMIT_MAX     – max attempts per window (default: 5)
    LOGIN_RATE_LIMIT_WINDOW  – window duration in seconds  (default: 60)

Extension note:
    To back this with Redis, replace ``defaultdict(deque)`` inside
    ``_SlidingWindowLimiter`` with a Redis client. The ``is_allowed``
    interface and the FastAPI dependency are unchanged.
"""

import os
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

_MAX_ATTEMPTS: int = int(os.getenv("LOGIN_RATE_LIMIT_MAX", "5"))
_WINDOW_SECONDS: int = int(os.getenv("LOGIN_RATE_LIMIT_WINDOW", "60"))


def _client_ip(request: Request) -> str:
    """
    Returns the most specific IP available.

    Trusts X-Forwarded-For when present so the limiter works correctly
    behind a reverse proxy (nginx, caddy, etc.). For deployments without
    a proxy this falls back to the direct connection address.
    """
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


class _SlidingWindowLimiter:
    """
    Thread-safe sliding-window counter keyed by an arbitrary string (IP).

    Each key stores a deque of monotonic timestamps for the requests that
    fell inside the current window. Stale entries are pruned on every call
    so memory is bounded to ``max_attempts`` entries per active key.
    """

    def __init__(self, max_attempts: int, window_seconds: int) -> None:
        self._max = max_attempts
        self._window = float(window_seconds)
        self._store: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def is_allowed(self, key: str) -> tuple[bool, int]:
        """
        Record one attempt for ``key`` and decide whether it is allowed.

        Returns:
            (True, 0)                    – request is within limit.
            (False, retry_after_seconds) – limit exceeded; caller should
                                           surface ``retry_after`` in the
                                           ``Retry-After`` response header.
        """
        now = time.monotonic()
        cutoff = now - self._window

        with self._lock:
            bucket = self._store[key]

            # Drop timestamps that have fallen outside the window.
            while bucket and bucket[0] < cutoff:
                bucket.popleft()

            if len(bucket) >= self._max:
                # Oldest timestamp still in the window determines when the
                # client's slot reopens.
                retry_after = int(self._window - (now - bucket[0])) + 1
                return False, retry_after

            bucket.append(now)
            return True, 0


# Module-level singleton — shared across all requests in the process.
_login_limiter = _SlidingWindowLimiter(
    max_attempts=_MAX_ATTEMPTS,
    window_seconds=_WINDOW_SECONDS,
)


def check_login_rate_limit(request: Request) -> None:
    """
    FastAPI dependency that enforces the login rate limit.

    Raises HTTP 429 with a ``Retry-After`` header when the client IP
    has exceeded ``LOGIN_RATE_LIMIT_MAX`` attempts within
    ``LOGIN_RATE_LIMIT_WINDOW`` seconds.
    """
    ip = _client_ip(request)
    allowed, retry_after = _login_limiter.is_allowed(ip)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Too many login attempts. "
                f"Please try again in {retry_after} second(s)."
            ),
            headers={"Retry-After": str(retry_after)},
        )
