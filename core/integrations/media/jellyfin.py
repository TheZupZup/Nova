"""
Optional read-only Jellyfin music bridge (Phase 1).

Nova is a *local media assistant*, not an autonomous media manager.
This Phase-1 module is intentionally narrow:

  * read-only HTTP calls against a locally-hosted Jellyfin server,
  * admin-gated at the endpoint layer (this module is a pure library
    and is never invoked from chat),
  * a single status snapshot the UI / chat can show calmly,
  * helpers to list music artists, albums, tracks, genres, and
    playlists for the configured library.

What this module deliberately does NOT do:

  * create / edit / delete playlists on Jellyfin,
  * stream, transcode, or copy any media file,
  * start playback or queue tracks for playback,
  * change Jellyfin server settings,
  * talk to any cloud music API,
  * poll Jellyfin in the background or run any scheduled work,
  * scan the local disk outside of Jellyfin's own metadata.

API-key safety contract (enforced):

  * the key is read from ``config.NOVA_JELLYFIN_API_KEY`` and is
    never returned by any public function;
  * the key is sent only as the ``X-Emby-Token`` request header — it
    never appears in URLs, query params, or JSON request bodies;
  * exceptions are logged at debug level by *type* only, never with
    the raw message, so a misbehaving stack trace cannot leak the
    header;
  * detail strings surfaced to the frontend are short, hard-coded
    summaries — they never include the key, the response body, or
    the exception's repr.

Configuration (env vars, see ``config.py``):

  * ``NOVA_JELLYFIN_ENABLED``         — host-wide on/off switch.
  * ``NOVA_JELLYFIN_URL``             — base URL of the Jellyfin server.
  * ``NOVA_JELLYFIN_API_KEY``         — Jellyfin API key (read scopes).
  * ``NOVA_JELLYFIN_USER_ID``         — optional Jellyfin user GUID.
  * ``NOVA_JELLYFIN_READ_ONLY``       — belt-and-braces; defaults True.
  * ``NOVA_JELLYFIN_TIMEOUT_SECONDS`` — per-request timeout (default 5s).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import httpx

from config import (
    NOVA_JELLYFIN_API_KEY,
    NOVA_JELLYFIN_ENABLED,
    NOVA_JELLYFIN_READ_ONLY,
    NOVA_JELLYFIN_TIMEOUT_SECONDS,
    NOVA_JELLYFIN_URL,
    NOVA_JELLYFIN_USER_ID,
)

logger = logging.getLogger(__name__)

NAME = "jellyfin"

USER_AGENT = "Nova-Jellyfin-Bridge/1.0"
API_KEY_HEADER = "X-Emby-Token"

STATE_DISABLED = "disabled"
STATE_NOT_CONFIGURED = "not_configured"
STATE_UNAVAILABLE = "unavailable"
STATE_CONNECTED = "connected_read_only"

# Caps so a single response can never balloon Nova's chat context.
# Jellyfin's ``Limit`` query param accepts large values; we clamp
# to a sane upper bound and a smaller default. Callers asking for
# more get exactly the cap — never an unbounded list.
_MAX_LIMIT = 200
_DEFAULT_LIMIT = 50

# Jellyfin returns ``RunTimeTicks`` in 100-nanosecond units. Dividing
# by 10_000_000 yields whole seconds.
_TICKS_PER_SECOND = 10_000_000

# The Jellyfin GUID format is hex characters and hyphens. Anything
# else is rejected before any HTTP call so a caller cannot smuggle a
# path fragment via the ``id`` field.
_GUID_RE = re.compile(r"^[A-Fa-f0-9-]{1,64}$")


# ── Status type ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class JellyfinStatus:
    """Calm, frontend-safe snapshot of the bridge's availability.

    Never includes the API key, never includes raw exception text.
    The ``state`` field is one of the four module-level ``STATE_*``
    values so the UI can branch on a stable enum.
    """

    name: str
    enabled: bool
    state: str
    detail: str = ""
    server_name: Optional[str] = None
    server_version: Optional[str] = None
    read_only: bool = True
    user_id_configured: bool = False
    base_url_configured: bool = False
    library_kinds: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "state": self.state,
            "detail": self.detail,
            "server_name": self.server_name,
            "server_version": self.server_version,
            "read_only": self.read_only,
            "user_id_configured": self.user_id_configured,
            "base_url_configured": self.base_url_configured,
            "library_kinds": list(self.library_kinds),
        }


# ── Switch helpers ──────────────────────────────────────────────────


def is_enabled() -> bool:
    """True when the host operator has flipped the env switch on."""
    return bool(NOVA_JELLYFIN_ENABLED)


def is_read_only() -> bool:
    """Always True in Phase 1; reflects ``NOVA_JELLYFIN_READ_ONLY``."""
    return bool(NOVA_JELLYFIN_READ_ONLY)


def _has_api_key() -> bool:
    return bool(NOVA_JELLYFIN_API_KEY)


def _has_base_url() -> bool:
    return bool(NOVA_JELLYFIN_URL)


def _has_user_id() -> bool:
    return bool(NOVA_JELLYFIN_USER_ID)


def _valid_user_id(user_id: str) -> bool:
    return isinstance(user_id, str) and _GUID_RE.match(user_id) is not None


def _headers() -> dict:
    """Build the request headers. Never returned to callers."""
    return {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        API_KEY_HEADER: NOVA_JELLYFIN_API_KEY,
    }


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=NOVA_JELLYFIN_URL,
        timeout=NOVA_JELLYFIN_TIMEOUT_SECONDS,
        headers=_headers(),
    )


def _log_error(where: str, exc: BaseException) -> None:
    """Log an exception by type only so the API key cannot leak via repr."""
    logger.debug("jellyfin %s failed: %s", where, type(exc).__name__)


def _clamp_limit(limit) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT
    if n <= 0:
        return _DEFAULT_LIMIT
    return min(n, _MAX_LIMIT)


def _ticks_to_seconds(ticks) -> Optional[int]:
    """Convert Jellyfin RunTimeTicks (100-ns units) to whole seconds."""
    try:
        n = int(ticks)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return n // _TICKS_PER_SECOND


# ── Status ──────────────────────────────────────────────────────────


def status() -> JellyfinStatus:
    """Report whether the bridge is on and reachable. Never raises.

    Off                                     → ``disabled``.
    On + no URL or API key                  → ``not_configured``.
    On + URL + key + ``GET /System/Info``   → ``connected_read_only``.
    On + URL + key + HTTP / network failure → ``unavailable`` with a
                                              short, sanitised detail.
    """
    if not is_enabled():
        return JellyfinStatus(
            name=NAME, enabled=False, state=STATE_DISABLED,
            detail="Set NOVA_JELLYFIN_ENABLED=true to turn the bridge on.",
            read_only=is_read_only(),
            user_id_configured=_has_user_id(),
            base_url_configured=_has_base_url(),
        )
    if not _has_base_url() or not _has_api_key():
        missing = []
        if not _has_base_url():
            missing.append("NOVA_JELLYFIN_URL")
        if not _has_api_key():
            missing.append("NOVA_JELLYFIN_API_KEY")
        return JellyfinStatus(
            name=NAME, enabled=True, state=STATE_NOT_CONFIGURED,
            detail=(
                f"Jellyfin bridge is missing config: {', '.join(missing)}."
            ),
            read_only=is_read_only(),
            user_id_configured=_has_user_id(),
            base_url_configured=_has_base_url(),
        )
    try:
        with _client() as client:
            resp = client.get("/System/Info/Public")
    except (httpx.HTTPError, OSError) as exc:
        _log_error("status", exc)
        return JellyfinStatus(
            name=NAME, enabled=True, state=STATE_UNAVAILABLE,
            detail="Jellyfin server is not reachable.",
            read_only=is_read_only(),
            user_id_configured=_has_user_id(),
            base_url_configured=_has_base_url(),
        )
    if resp.status_code == 401 or resp.status_code == 403:
        return JellyfinStatus(
            name=NAME, enabled=True, state=STATE_UNAVAILABLE,
            detail="Jellyfin rejected the configured API key.",
            read_only=is_read_only(),
            user_id_configured=_has_user_id(),
            base_url_configured=_has_base_url(),
        )
    if not (200 <= resp.status_code < 300):
        return JellyfinStatus(
            name=NAME, enabled=True, state=STATE_UNAVAILABLE,
            detail=f"Jellyfin returned HTTP {resp.status_code}.",
            read_only=is_read_only(),
            user_id_configured=_has_user_id(),
            base_url_configured=_has_base_url(),
        )
    server_name: Optional[str] = None
    server_version: Optional[str] = None
    try:
        body = resp.json()
        if isinstance(body, dict):
            raw_name = body.get("ServerName")
            if isinstance(raw_name, str):
                server_name = raw_name
            raw_version = body.get("Version")
            if isinstance(raw_version, str):
                server_version = raw_version
    except ValueError as exc:
        _log_error("status decode", exc)
    library_kinds = _probe_library_kinds()
    return JellyfinStatus(
        name=NAME, enabled=True, state=STATE_CONNECTED,
        detail=(
            f"Connected to {server_name}." if server_name
            else "Authenticated read-only access."
        ),
        server_name=server_name,
        server_version=server_version,
        read_only=is_read_only(),
        user_id_configured=_has_user_id(),
        base_url_configured=_has_base_url(),
        library_kinds=library_kinds,
    )


def _probe_library_kinds() -> tuple[str, ...]:
    """Best-effort library-kind probe. Never raises; returns ``()`` on failure.

    Surfaces a small set of strings (e.g. ``("music",)``) so the UI
    can tell users "your library has music" without sending Nova back
    to the server for full metadata on every status load.
    """
    if not _has_user_id() or not _valid_user_id(NOVA_JELLYFIN_USER_ID):
        return ()
    try:
        with _client() as client:
            resp = client.get(f"/Users/{NOVA_JELLYFIN_USER_ID}/Views")
        if not (200 <= resp.status_code < 300):
            return ()
        body = resp.json()
    except (httpx.HTTPError, OSError, ValueError) as exc:
        _log_error("library kinds probe", exc)
        return ()
    if not isinstance(body, dict):
        return ()
    items = body.get("Items") or []
    if not isinstance(items, list):
        return ()
    kinds: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = item.get("CollectionType")
        if isinstance(kind, str) and kind.strip():
            kinds.add(kind.strip().lower())
    return tuple(sorted(kinds))


# ── Read API ────────────────────────────────────────────────────────


def _user_scoped_path(suffix: str) -> str:
    """Return a path scoped to the configured user when one is set.

    Jellyfin exposes both ``/Items`` (server-wide) and
    ``/Users/<id>/Items`` (per-user) endpoints. Phase 1 prefers the
    per-user variant when a valid GUID is configured because it lets
    Jellyfin enforce per-user library visibility.
    """
    if _has_user_id() and _valid_user_id(NOVA_JELLYFIN_USER_ID):
        return f"/Users/{NOVA_JELLYFIN_USER_ID}/{suffix.lstrip('/')}"
    return f"/{suffix.lstrip('/')}"


def _is_ready() -> bool:
    return is_enabled() and _has_base_url() and _has_api_key()


def list_artists(limit: int = _DEFAULT_LIMIT) -> list[dict]:
    """Return sanitised music artists from the configured Jellyfin library.

    Empty list when the bridge is off / not configured / unreachable.
    """
    if not _is_ready():
        return []
    params = {
        "IncludeItemTypes": "MusicArtist",
        "Recursive": "true",
        "SortBy": "SortName",
        "SortOrder": "Ascending",
        "Limit": _clamp_limit(limit),
    }
    try:
        with _client() as client:
            resp = client.get("/Artists", params=params)
        if not (200 <= resp.status_code < 300):
            return []
        data = resp.json()
    except (httpx.HTTPError, OSError, ValueError) as exc:
        _log_error("list_artists", exc)
        return []
    if not isinstance(data, dict):
        return []
    items = data.get("Items") or []
    if not isinstance(items, list):
        return []
    return [
        _sanitize_artist(item) for item in items
        if isinstance(item, dict)
    ]


def list_albums(limit: int = _DEFAULT_LIMIT) -> list[dict]:
    """Return sanitised music albums from the configured Jellyfin library."""
    if not _is_ready():
        return []
    params = {
        "IncludeItemTypes": "MusicAlbum",
        "Recursive": "true",
        "SortBy": "SortName",
        "SortOrder": "Ascending",
        "Limit": _clamp_limit(limit),
        "Fields": "Genres,ProductionYear,AlbumArtist",
    }
    try:
        with _client() as client:
            resp = client.get(_user_scoped_path("Items"), params=params)
        if not (200 <= resp.status_code < 300):
            return []
        data = resp.json()
    except (httpx.HTTPError, OSError, ValueError) as exc:
        _log_error("list_albums", exc)
        return []
    return _extract_items(data, _sanitize_album)


def list_tracks(limit: int = _DEFAULT_LIMIT) -> list[dict]:
    """Return sanitised music tracks (Audio items) from the library.

    Each track carries title, artist(s), album, genres, year, and a
    whole-second ``duration`` derived from Jellyfin's ``RunTimeTicks``
    (or ``None`` when the server did not provide one).
    """
    if not _is_ready():
        return []
    params = {
        "IncludeItemTypes": "Audio",
        "Recursive": "true",
        "SortBy": "SortName",
        "SortOrder": "Ascending",
        "Limit": _clamp_limit(limit),
        "Fields": "Genres,ProductionYear,AlbumArtist,RunTimeTicks",
    }
    try:
        with _client() as client:
            resp = client.get(_user_scoped_path("Items"), params=params)
        if not (200 <= resp.status_code < 300):
            return []
        data = resp.json()
    except (httpx.HTTPError, OSError, ValueError) as exc:
        _log_error("list_tracks", exc)
        return []
    return _extract_items(data, _sanitize_track)


def list_genres(limit: int = _DEFAULT_LIMIT) -> list[dict]:
    """Return sanitised music genres available in the library."""
    if not _is_ready():
        return []
    params = {
        "SortBy": "SortName",
        "SortOrder": "Ascending",
        "Limit": _clamp_limit(limit),
    }
    try:
        with _client() as client:
            resp = client.get("/MusicGenres", params=params)
        if not (200 <= resp.status_code < 300):
            return []
        data = resp.json()
    except (httpx.HTTPError, OSError, ValueError) as exc:
        _log_error("list_genres", exc)
        return []
    return _extract_items(data, _sanitize_genre)


def list_playlists(limit: int = _DEFAULT_LIMIT) -> list[dict]:
    """Return sanitised playlists from the library. Empty on failure.

    Phase 1 *reads* playlists only — it never creates, edits, or
    deletes them. The returned dicts carry id, name, track count
    (when reported), and a media-kind hint so the caller can filter
    to music-only playlists.
    """
    if not _is_ready():
        return []
    params = {
        "IncludeItemTypes": "Playlist",
        "Recursive": "true",
        "SortBy": "SortName",
        "SortOrder": "Ascending",
        "Limit": _clamp_limit(limit),
        "Fields": "ChildCount,RunTimeTicks",
    }
    try:
        with _client() as client:
            resp = client.get(_user_scoped_path("Items"), params=params)
        if not (200 <= resp.status_code < 300):
            return []
        data = resp.json()
    except (httpx.HTTPError, OSError, ValueError) as exc:
        _log_error("list_playlists", exc)
        return []
    return _extract_items(data, _sanitize_playlist)


# ── Sanitisers ──────────────────────────────────────────────────────


def _extract_items(data, sanitiser) -> list[dict]:
    if not isinstance(data, dict):
        return []
    items = data.get("Items") or []
    if not isinstance(items, list):
        return []
    return [sanitiser(item) for item in items if isinstance(item, dict)]


def _safe_str(value) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _safe_int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _genres(item: dict) -> list[str]:
    genres = item.get("Genres") or []
    if not isinstance(genres, list):
        return []
    return [g for g in genres if isinstance(g, str) and g]


def _artists(item: dict) -> list[str]:
    raw = item.get("Artists") or []
    if isinstance(raw, list):
        out = [a for a in raw if isinstance(a, str) and a]
        if out:
            return out
    album_artist = item.get("AlbumArtist")
    if isinstance(album_artist, str) and album_artist:
        return [album_artist]
    return []


def _sanitize_artist(item: dict) -> dict:
    return {
        "id": _safe_str(item.get("Id")),
        "name": _safe_str(item.get("Name")),
        "genres": _genres(item),
    }


def _sanitize_album(item: dict) -> dict:
    return {
        "id": _safe_str(item.get("Id")),
        "name": _safe_str(item.get("Name")),
        "artist": _safe_str(item.get("AlbumArtist")),
        "year": _safe_int(item.get("ProductionYear")),
        "genres": _genres(item),
    }


def _sanitize_track(item: dict) -> dict:
    return {
        "id": _safe_str(item.get("Id")),
        "title": _safe_str(item.get("Name")),
        "artist": (_artists(item)[0] if _artists(item) else None),
        "artists": _artists(item),
        "album": _safe_str(item.get("Album")),
        "year": _safe_int(item.get("ProductionYear")),
        "genres": _genres(item),
        "duration": _ticks_to_seconds(item.get("RunTimeTicks")),
    }


def _sanitize_genre(item: dict) -> dict:
    return {
        "id": _safe_str(item.get("Id")),
        "name": _safe_str(item.get("Name")),
    }


def _sanitize_playlist(item: dict) -> dict:
    kind = item.get("MediaType")
    return {
        "id": _safe_str(item.get("Id")),
        "name": _safe_str(item.get("Name")),
        "track_count": _safe_int(item.get("ChildCount")),
        "duration": _ticks_to_seconds(item.get("RunTimeTicks")),
        "media_kind": kind.lower() if isinstance(kind, str) and kind else None,
    }


# ── Library snapshot helper ─────────────────────────────────────────


def library_snapshot(limit: int = _DEFAULT_LIMIT) -> dict:
    """Roll-up of artists / albums / tracks / genres / playlists.

    Used by the recommendation layer so it can ask for "the music
    library" once instead of issuing five separate requests itself.
    Never raises; failure paths yield empty lists so the caller can
    still render a calm "library is empty / unreachable" message.
    """
    return {
        "artists": list_artists(limit=limit),
        "albums": list_albums(limit=limit),
        "tracks": list_tracks(limit=limit),
        "genres": list_genres(limit=limit),
        "playlists": list_playlists(limit=limit),
        "read_only": True,
    }
