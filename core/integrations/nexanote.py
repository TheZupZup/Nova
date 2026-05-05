"""
Gated bridge to NexaNote's HTTP API.

NexaNote is an external note service (separate FastAPI backend). Nova
talks to it over HTTP only — no shared database, no shared process. The
integration is per-user opt-in and read-only by default; writes require
a second ``nexanote_write_enabled`` switch.

Boundaries (enforced):
  * HTTP only — never touches NexaNote's database directly.
  * read-only by default; write helpers refuse silently when the
    write switch is off.
  * never raises on network or HTTP errors — returns ``None``/``[]``
    and logs at debug level so a missing or unreachable NexaNote does
    not break Nova.
  * no system-level actions, no subprocess execution.

Configuration (env vars, see ``config.py``):
  * ``NEXANOTE_API_URL``    — base URL, e.g. ``https://nexanote.local``.
                              Empty string means "not deployed";
                              status reports ``not_found`` for everyone.
  * ``NEXANOTE_API_TOKEN``  — optional bearer token.
  * ``NEXANOTE_TIMEOUT_SECONDS`` — per-request timeout (default 3.0s).

The expected REST shape (best-effort; failures are absorbed):
  * ``GET  /health``           → 2xx when reachable.
  * ``GET  /notes?limit=N``    → list of ``{id, title, content, ...}``.
  * ``GET  /notes/{id}``       → one note.
  * ``POST /notes``            → create from ``{title, content}``.
  * ``PUT  /notes/{id}``       → update.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from config import (
    NEXANOTE_API_TOKEN,
    NEXANOTE_API_URL,
    NEXANOTE_TIMEOUT_SECONDS,
)
from core.settings import get_user_setting

logger = logging.getLogger(__name__)

NAME = "nexanote"

STATE_DISABLED = "disabled"
STATE_CONNECTED = "connected"
STATE_NOT_FOUND = "not_found"
STATE_UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class IntegrationStatus:
    """Snapshot of an integration's availability for one user."""

    name: str
    enabled: bool
    state: str
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "state": self.state,
            "detail": self.detail,
        }


# ── Switch helpers ──────────────────────────────────────────────────

def is_enabled(user_id: int) -> bool:
    """True only when the user has explicitly turned the integration on."""
    return get_user_setting(user_id, "nexanote_enabled", "false") == "true"


def is_write_enabled(user_id: int) -> bool:
    """
    True only when the user has explicitly opted into writes *and* has
    the integration enabled. Read-only by default.
    """
    if not is_enabled(user_id):
        return False
    return get_user_setting(user_id, "nexanote_write_enabled", "false") == "true"


def _is_configured() -> bool:
    return bool(NEXANOTE_API_URL)


def _headers() -> dict:
    headers = {"Accept": "application/json"}
    if NEXANOTE_API_TOKEN:
        headers["Authorization"] = f"Bearer {NEXANOTE_API_TOKEN}"
    return headers


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=NEXANOTE_API_URL,
        timeout=NEXANOTE_TIMEOUT_SECONDS,
        headers=_headers(),
    )


# ── Status ──────────────────────────────────────────────────────────

def status(user_id: int) -> IntegrationStatus:
    """
    Report whether the integration is on and reachable. Never raises.

    Off  → ``disabled``.
    On + no URL configured → ``not_found``.
    On + URL configured + ``GET /health`` succeeds → ``connected``.
    On + URL configured + any network/HTTP failure → ``unreachable``.
    """
    enabled = is_enabled(user_id)
    if not enabled:
        return IntegrationStatus(
            name=NAME, enabled=False, state=STATE_DISABLED,
            detail="Turned off in Settings.",
        )
    if not _is_configured():
        return IntegrationStatus(
            name=NAME, enabled=True, state=STATE_NOT_FOUND,
            detail="NEXANOTE_API_URL is not set on this Nova host.",
        )
    try:
        with _client() as client:
            resp = client.get("/health")
        if 200 <= resp.status_code < 300:
            return IntegrationStatus(
                name=NAME, enabled=True, state=STATE_CONNECTED,
                detail=NEXANOTE_API_URL,
            )
        return IntegrationStatus(
            name=NAME, enabled=True, state=STATE_UNREACHABLE,
            detail=f"NexaNote returned HTTP {resp.status_code}.",
        )
    except (httpx.HTTPError, OSError) as e:
        logger.debug("NexaNote health check failed: %s", e)
        return IntegrationStatus(
            name=NAME, enabled=True, state=STATE_UNREACHABLE,
            detail="NexaNote is not reachable.",
        )


# ── Read API ────────────────────────────────────────────────────────

def list_notes(user_id: int, limit: int = 50) -> list[dict]:
    """
    Return up to ``limit`` notes from NexaNote.

    Empty list when the integration is off, NexaNote is not reachable,
    or the response cannot be decoded. Read-only.
    """
    if not is_enabled(user_id) or not _is_configured() or limit <= 0:
        return []
    try:
        with _client() as client:
            resp = client.get("/notes", params={"limit": limit})
        if not (200 <= resp.status_code < 300):
            return []
        data = resp.json()
    except (httpx.HTTPError, OSError, ValueError) as e:
        logger.debug("NexaNote list_notes failed: %s", e)
        return []
    if isinstance(data, list):
        return [n for n in data if isinstance(n, dict)]
    if isinstance(data, dict):
        for key in ("notes", "items", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return [n for n in value if isinstance(n, dict)]
    return []


def read_note(user_id: int, note_id) -> Optional[dict]:
    """
    Fetch a single note. ``None`` when disabled, missing, or on any
    transport error. Read-only.
    """
    if not is_enabled(user_id) or not _is_configured() or note_id is None:
        return None
    try:
        with _client() as client:
            resp = client.get(f"/notes/{note_id}")
        if not (200 <= resp.status_code < 300):
            return None
        data = resp.json()
    except (httpx.HTTPError, OSError, ValueError) as e:
        logger.debug("NexaNote read_note failed: %s", e)
        return None
    return data if isinstance(data, dict) else None


# ── Write API (gated behind nexanote_write_enabled) ─────────────────

def create_note(user_id: int, title: str, content: str) -> Optional[dict]:
    """
    Create a note. Refuses silently — returns ``None`` — when:
      * the integration is off, or
      * the user has not opted into writes (default), or
      * NexaNote is not configured / not reachable.
    """
    if not is_write_enabled(user_id) or not _is_configured():
        return None
    if not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(content, str):
        return None
    try:
        with _client() as client:
            resp = client.post(
                "/notes",
                json={"title": title.strip(), "content": content},
            )
        if not (200 <= resp.status_code < 300):
            return None
        data = resp.json()
    except (httpx.HTTPError, OSError, ValueError) as e:
        logger.debug("NexaNote create_note failed: %s", e)
        return None
    return data if isinstance(data, dict) else None


def update_note(user_id: int, note_id, title: Optional[str] = None,
                content: Optional[str] = None) -> Optional[dict]:
    """
    Update a note. Same gating as ``create_note``; returns ``None`` when
    the integration is off, writes are off, or the call fails.
    """
    if not is_write_enabled(user_id) or not _is_configured() or note_id is None:
        return None
    payload: dict = {}
    if isinstance(title, str) and title.strip():
        payload["title"] = title.strip()
    if isinstance(content, str):
        payload["content"] = content
    if not payload:
        return None
    try:
        with _client() as client:
            resp = client.put(f"/notes/{note_id}", json=payload)
        if not (200 <= resp.status_code < 300):
            return None
        data = resp.json()
    except (httpx.HTTPError, OSError, ValueError) as e:
        logger.debug("NexaNote update_note failed: %s", e)
        return None
    return data if isinstance(data, dict) else None
