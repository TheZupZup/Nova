"""
Read-only local playlist recommendation helper (Phase 1).

This module sits on top of the read-only media provider(s) and turns
a sanitised list of tracks into a short list of *suggested* playlists.
Nova is a local media *assistant* — never an autonomous media
manager — so every decision here is deterministic, explainable, and
harmless:

  * pure-Python scoring on the sanitised dicts produced by
    ``core.integrations.media.jellyfin.list_tracks`` (no extra HTTP
    calls, no provider-specific code paths);
  * no LLM call, no embeddings, no ML — only label / genre / title /
    duration heuristics;
  * never mutates anything on the media server (no playlist create,
    update, delete; no play / queue / shuffle);
  * never persists anything to disk or the database;
  * never sends library data to any cloud service.

The user always picks what to play. Nova's role is to surface a short
list of *ideas* with reasons; nothing here starts playback, edits a
playlist, or talks to a third-party service.

Future direction (NOT in this PR):

  * Playlist creation behind an explicit per-request confirmation
    and a separate write switch.
  * Plex provider plugged in behind the same sanitised-track shape.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

NAME = "media_recommendations"

CONFIDENCE_LOW = "low"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_HIGH = "high"

# Caps mirror the underlying provider so a single response can never
# balloon Nova's chat context. The visible top-N defaults are small;
# the per-playlist track cap is small enough to fit comfortably into
# a chat reply.
_DEFAULT_MOODS_LIMIT = 8
_MAX_MOODS_LIMIT = 12
_DEFAULT_TRACKS_PER_PLAYLIST = 12
_MAX_TRACKS_PER_PLAYLIST = 25
_MIN_TRACKS_PER_PLAYLIST = 3

# Genre dictionaries are stored lowercase so every lookup is routed
# through ``_norm``. Words longer than the literal label are matched
# as substrings ("hard rock" matches "rock"), which keeps the
# heuristics robust to small label variations across libraries.

# Genre → mood weights. A positive weight nudges a track toward the
# mood; a negative weight discourages it. Zero weights are omitted
# from the dict so the lookup stays fast.
_GENRE_MOOD_WEIGHTS: dict[str, dict[str, int]] = {
    "ambient":      {"chill": 4, "focus": 4, "coding": 3, "night drive": 2},
    "classical":    {"chill": 3, "focus": 4, "coding": 3, "sad": 2},
    "lo-fi":        {"chill": 4, "focus": 4, "coding": 4, "night drive": 2},
    "lofi":         {"chill": 4, "focus": 4, "coding": 4, "night drive": 2},
    "acoustic":     {"chill": 3, "focus": 2, "sad": 2},
    "folk":         {"chill": 3, "sad": 2, "focus": 2},
    "indie":        {"chill": 2, "focus": 2, "night drive": 2},
    "jazz":         {"chill": 3, "focus": 3, "coding": 2, "night drive": 2},
    "blues":        {"chill": 2, "sad": 3},
    "soundtrack":   {"focus": 3, "coding": 3, "chill": 2, "night drive": 2},
    "instrumental": {"focus": 4, "coding": 4, "chill": 3},
    "electronic":   {"focus": 2, "coding": 2, "upbeat": 3, "night drive": 3},
    "synthwave":    {"night drive": 4, "coding": 3, "focus": 2, "dark": 2},
    "house":        {"upbeat": 3, "gym": 3, "night drive": 2},
    "techno":       {"upbeat": 3, "gym": 3, "dark": 2, "night drive": 2},
    "trance":       {"upbeat": 3, "focus": 2, "night drive": 2},
    "edm":          {"upbeat": 4, "gym": 4, "night drive": 2},
    "dance":        {"upbeat": 4, "gym": 3, "night drive": 2},
    "pop":          {"upbeat": 3, "gym": 2},
    "rock":         {"upbeat": 3, "gym": 3},
    "hard rock":    {"gym": 4, "upbeat": 3, "dark": 2},
    "metal":        {"gym": 4, "dark": 4, "upbeat": 2},
    "punk":         {"gym": 3, "upbeat": 3, "dark": 2},
    "hip hop":      {"gym": 3, "upbeat": 3, "coding": 2, "night drive": 2},
    "hip-hop":      {"gym": 3, "upbeat": 3, "coding": 2, "night drive": 2},
    "rap":          {"gym": 3, "upbeat": 3, "night drive": 2},
    "trap":         {"gym": 3, "dark": 2, "night drive": 2},
    "drill":        {"dark": 3, "gym": 2},
    "r&b":          {"chill": 3, "night drive": 2, "sad": 2},
    "rnb":          {"chill": 3, "night drive": 2, "sad": 2},
    "soul":         {"chill": 3, "sad": 2, "night drive": 2},
    "country":      {"chill": 2, "sad": 2, "upbeat": 1},
    "reggae":       {"chill": 4, "upbeat": 2},
    "world":        {"chill": 2, "focus": 1},
    "post-rock":    {"focus": 3, "chill": 3, "coding": 3},
    "shoegaze":     {"chill": 3, "sad": 3, "dark": 2},
    "darkwave":     {"dark": 4, "night drive": 3, "sad": 2},
    "industrial":   {"dark": 4, "gym": 2, "upbeat": 1},
    "goth":         {"dark": 4, "sad": 3, "night drive": 2},
    "emo":          {"sad": 4, "dark": 2},
    "screamo":      {"dark": 3, "sad": 2},
}

# Title-token signals. These are coarse — a track called "Save Me"
# is not always sad — but they let the heuristic move a track toward
# a mood when the genre is missing or ambiguous. Tokens are matched
# as lowercase whole-word substrings against the title.
_TITLE_MOOD_TOKENS: dict[str, dict[str, int]] = {
    "chill":     {"chill": 3, "focus": 1},
    "relax":     {"chill": 3},
    "calm":      {"chill": 2, "focus": 1},
    "study":     {"focus": 3, "coding": 2},
    "focus":     {"focus": 4, "coding": 2},
    "work":      {"focus": 2, "coding": 2},
    "code":      {"coding": 4, "focus": 2},
    "coding":    {"coding": 4, "focus": 2},
    "night":     {"night drive": 3, "dark": 1},
    "midnight":  {"night drive": 3, "dark": 2},
    "drive":     {"night drive": 3, "upbeat": 1},
    "highway":   {"night drive": 2, "upbeat": 1},
    "run":       {"gym": 2, "upbeat": 2},
    "fire":      {"upbeat": 2, "gym": 1},
    "energy":    {"upbeat": 3, "gym": 2},
    "party":     {"upbeat": 3},
    "happy":     {"upbeat": 3},
    "joy":       {"upbeat": 2},
    "sun":       {"upbeat": 2, "chill": 1},
    "summer":    {"upbeat": 2, "chill": 1},
    "sad":       {"sad": 4, "dark": 1},
    "cry":       {"sad": 3},
    "tears":     {"sad": 3},
    "alone":     {"sad": 2, "dark": 1},
    "lonely":    {"sad": 3, "dark": 1},
    "lost":      {"sad": 2, "dark": 1},
    "save":      {"sad": 2, "dark": 1},
    "dark":      {"dark": 4, "night drive": 1},
    "shadow":    {"dark": 3, "night drive": 1},
    "demons":    {"dark": 3},
    "death":     {"dark": 2, "sad": 1},
    "broken":    {"sad": 2, "dark": 1},
    "pain":      {"sad": 2, "dark": 1},
    "winter":    {"chill": 2, "sad": 1},
    "rain":      {"chill": 2, "sad": 1},
    "sleep":     {"chill": 3, "focus": 1},
    "dream":     {"chill": 2, "focus": 1, "night drive": 1},
}

# Mood catalogue: each entry is the public face of a recommendation.
# ``description`` is a calm, neutral one-liner; ``min_tracks`` lets
# us drop moods that did not match anything reasonable.
_MOOD_CATALOGUE: dict[str, dict] = {
    "chill":       {
        "title": "Chill Wind-Down",
        "description": "Calm tracks for unwinding without going to sleep.",
        "min_tracks": _MIN_TRACKS_PER_PLAYLIST,
    },
    "focus":       {
        "title": "Deep Focus",
        "description": "Low-distraction tracks for sustained concentration.",
        "min_tracks": _MIN_TRACKS_PER_PLAYLIST,
    },
    "gym":         {
        "title": "Gym Push",
        "description": "High-energy tracks for a workout block.",
        "min_tracks": _MIN_TRACKS_PER_PLAYLIST,
    },
    "dark":        {
        "title": "Dark and Heavy",
        "description": "Heavier, moodier tracks for a darker session.",
        "min_tracks": _MIN_TRACKS_PER_PLAYLIST,
    },
    "upbeat":      {
        "title": "Upbeat Pickup",
        "description": "Bright, energetic tracks for a quick mood lift.",
        "min_tracks": _MIN_TRACKS_PER_PLAYLIST,
    },
    "sad":         {
        "title": "Quiet Reflection",
        "description": "Slower, more contemplative tracks for a quiet hour.",
        "min_tracks": _MIN_TRACKS_PER_PLAYLIST,
    },
    "night drive": {
        "title": "Night Drive",
        "description": "Late-night driving tracks with steady forward motion.",
        "min_tracks": _MIN_TRACKS_PER_PLAYLIST,
    },
    "coding":      {
        "title": "Night Coding",
        "description": "Calm tracks for late-night development.",
        "min_tracks": _MIN_TRACKS_PER_PLAYLIST,
    },
}

# Stable ordering for the mood catalogue. The output preserves this
# order so two runs against the same library produce the same list.
MOOD_ORDER: tuple[str, ...] = tuple(_MOOD_CATALOGUE.keys())


# ── Normalisation helpers ──────────────────────────────────────────


def _norm(value) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def _clamp(value, lo: int, hi: int, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if n < lo:
        return lo
    if n > hi:
        return hi
    return n


def _title_tokens(title) -> set[str]:
    if not isinstance(title, str):
        return set()
    out: set[str] = set()
    cleaned = title.lower()
    for token in _TITLE_MOOD_TOKENS:
        if token in cleaned:
            out.add(token)
    return out


# ── Per-track scoring ──────────────────────────────────────────────


def score_track(track: dict) -> dict[str, int]:
    """Return ``{mood: score}`` for one sanitised track dict.

    Empty when nothing matched. Pure-Python, deterministic; identical
    input yields identical output.
    """
    if not isinstance(track, dict):
        return {}
    scores: dict[str, int] = {}

    # Genre signals — exact and substring match. Substring matching
    # makes "post-rock" pick up the "rock" entry as a weaker hint.
    raw_genres = track.get("genres") or []
    if isinstance(raw_genres, list):
        for genre in raw_genres:
            normalised = _norm(genre)
            if not normalised:
                continue
            weights = _GENRE_MOOD_WEIGHTS.get(normalised)
            if weights:
                for mood, weight in weights.items():
                    scores[mood] = scores.get(mood, 0) + weight
                continue
            # Substring fallback: pick the strongest match so a
            # multi-word genre like "alternative rock" still maps
            # to the "rock" entry.
            for key, weights in _GENRE_MOOD_WEIGHTS.items():
                if key in normalised:
                    for mood, weight in weights.items():
                        scores[mood] = scores.get(mood, 0) + max(weight - 1, 1)
                    break

    # Title-token signals — additive but capped per token so a flashy
    # title cannot dominate a track with no matching genre.
    for token in _title_tokens(track.get("title")):
        for mood, weight in _TITLE_MOOD_TOKENS[token].items():
            scores[mood] = scores.get(mood, 0) + weight

    # Duration nudge: very short tracks are unlikely to be a sustained
    # focus / coding pick; very long tracks lean toward chill / focus.
    duration = track.get("duration")
    if isinstance(duration, int) and duration > 0:
        if duration < 90:
            for mood in ("focus", "coding", "chill", "night drive"):
                if mood in scores:
                    scores[mood] = max(scores[mood] - 1, 0)
        elif duration >= 300:
            for mood in ("focus", "coding", "chill"):
                scores[mood] = scores.get(mood, 0) + 1

    return {mood: score for mood, score in scores.items() if score > 0}


def _reason_for(track: dict, mood: str) -> str:
    """Short, calm explanation of why this track was picked for ``mood``."""
    raw_genres = track.get("genres") or []
    genre_hits: list[str] = []
    if isinstance(raw_genres, list):
        for genre in raw_genres:
            normalised = _norm(genre)
            if not normalised:
                continue
            weights = _GENRE_MOOD_WEIGHTS.get(normalised)
            if weights and weights.get(mood):
                genre_hits.append(genre)
                continue
            for key, weights in _GENRE_MOOD_WEIGHTS.items():
                if key in normalised and weights.get(mood):
                    genre_hits.append(genre)
                    break

    title_hits = sorted(
        token for token in _title_tokens(track.get("title"))
        if _TITLE_MOOD_TOKENS[token].get(mood)
    )

    parts: list[str] = []
    if genre_hits:
        parts.append(f"genre {', '.join(sorted(set(genre_hits)))}")
    if title_hits:
        parts.append(f"title hints '{', '.join(title_hits)}'")
    if not parts:
        parts.append("matched on duration / fallback heuristics")
    return f"matches {mood} mood: " + "; ".join(parts) + "."


# ── Playlist builder ───────────────────────────────────────────────


def _pick_tracks_for_mood(
    tracks: Iterable[dict],
    mood: str,
    per_playlist: int,
) -> list[dict]:
    """Return up to ``per_playlist`` track entries scored for ``mood``.

    Tie-break: highest mood score first, then track title ascending so
    the order is stable across runs against the same library.
    """
    scored: list[tuple[int, str, dict]] = []
    for track in tracks:
        if not isinstance(track, dict):
            continue
        score = score_track(track).get(mood, 0)
        if score <= 0:
            continue
        title = track.get("title") or ""
        scored.append((score, title.lower() if isinstance(title, str) else "", track))
    scored.sort(key=lambda r: (-r[0], r[1]))
    out: list[dict] = []
    for _, _, track in scored[:per_playlist]:
        out.append({
            "id": track.get("id"),
            "title": track.get("title"),
            "artist": track.get("artist"),
            "album": track.get("album"),
            "duration": track.get("duration"),
            "reason": _reason_for(track, mood),
        })
    return out


def _confidence(track_count: int, library_size: int) -> str:
    """Confidence label for a playlist based on signal strength."""
    if track_count >= 8:
        return CONFIDENCE_HIGH
    if track_count >= 5:
        return CONFIDENCE_MEDIUM
    return CONFIDENCE_LOW


def _estimated_duration(tracks: list[dict]) -> Optional[int]:
    total = 0
    counted = 0
    for track in tracks:
        if not isinstance(track, dict):
            continue
        duration = track.get("duration")
        if isinstance(duration, int) and duration > 0:
            total += duration
            counted += 1
    if counted == 0:
        return None
    return total


def build_playlist(
    tracks: Iterable[dict],
    mood: str,
    per_playlist: int = _DEFAULT_TRACKS_PER_PLAYLIST,
    library_size: int = 0,
) -> Optional[dict]:
    """Build a single playlist suggestion for ``mood``, or ``None``.

    Returns ``None`` when the mood is unknown or the library yields
    fewer than the mood's ``min_tracks`` matches. The caller filters
    ``None``s out of the recommendation list.
    """
    catalogue_entry = _MOOD_CATALOGUE.get(mood)
    if catalogue_entry is None:
        return None
    if not isinstance(tracks, list):
        tracks = list(tracks)
    picked = _pick_tracks_for_mood(
        tracks,
        mood,
        _clamp(per_playlist, _MIN_TRACKS_PER_PLAYLIST,
               _MAX_TRACKS_PER_PLAYLIST, _DEFAULT_TRACKS_PER_PLAYLIST),
    )
    if len(picked) < catalogue_entry["min_tracks"]:
        return None
    estimated_duration = _estimated_duration(picked)
    confidence = _confidence(len(picked), library_size or len(tracks))
    return {
        "title": catalogue_entry["title"],
        "mood": mood,
        "description": catalogue_entry["description"],
        "estimated_duration": estimated_duration,
        "tracks": picked,
        "confidence": confidence,
    }


def recommend_playlists(
    tracks: Iterable[dict],
    moods: Optional[Iterable[str]] = None,
    limit: int = _DEFAULT_MOODS_LIMIT,
    per_playlist: int = _DEFAULT_TRACKS_PER_PLAYLIST,
) -> list[dict]:
    """Return a list of playlist suggestions for the given tracks.

    ``moods`` is an optional caller-supplied filter; entries not in
    the mood catalogue are dropped. ``limit`` clamps to a maximum of
    :data:`_MAX_MOODS_LIMIT`; ``per_playlist`` clamps to a maximum of
    :data:`_MAX_TRACKS_PER_PLAYLIST`.

    Empty list on empty input or when every mood fails to meet its
    minimum-track threshold. The output is deterministic: identical
    inputs yield identical outputs (insertion order matches the
    :data:`MOOD_ORDER` constant).
    """
    if not isinstance(tracks, list):
        tracks = list(tracks)
    if not tracks:
        return []

    if moods is None:
        wanted_order = MOOD_ORDER
    else:
        normalised = {_norm(m) for m in moods if isinstance(m, str)}
        wanted_order = tuple(
            mood for mood in MOOD_ORDER if mood in normalised
        )
        if not wanted_order:
            return []

    limit_n = _clamp(limit, 1, _MAX_MOODS_LIMIT, _DEFAULT_MOODS_LIMIT)
    per_playlist_n = _clamp(
        per_playlist, _MIN_TRACKS_PER_PLAYLIST,
        _MAX_TRACKS_PER_PLAYLIST, _DEFAULT_TRACKS_PER_PLAYLIST,
    )

    out: list[dict] = []
    library_size = len(tracks)
    for mood in wanted_order:
        playlist = build_playlist(
            tracks, mood,
            per_playlist=per_playlist_n,
            library_size=library_size,
        )
        if playlist is None:
            continue
        out.append(playlist)
        if len(out) >= limit_n:
            break
    return out


# ── Provider-backed convenience entry point ────────────────────────


def recommend_from_jellyfin(
    moods: Optional[Iterable[str]] = None,
    limit: int = _DEFAULT_MOODS_LIMIT,
    per_playlist: int = _DEFAULT_TRACKS_PER_PLAYLIST,
    track_pool: int = 200,
) -> list[dict]:
    """Fetch tracks from the Jellyfin bridge and run :func:`recommend_playlists`.

    Empty list when the bridge is disabled / not configured /
    unreachable. Never raises. ``track_pool`` clamps to the
    provider's per-request cap so a single recommendation request
    cannot overwhelm Jellyfin.

    This convenience wrapper keeps the recommendation module free of
    direct HTTP code; the heuristics themselves operate on the
    sanitised track shape and never know which provider produced it.
    """
    from . import jellyfin as _jellyfin

    if not _jellyfin.is_enabled() or not _jellyfin._has_api_key():
        return []
    if not _jellyfin._has_base_url():
        return []
    tracks = _jellyfin.list_tracks(limit=track_pool)
    return recommend_playlists(
        tracks, moods=moods, limit=limit, per_playlist=per_playlist,
    )


def is_available() -> bool:
    """True when the recommendation pipeline has at least one provider on."""
    from . import jellyfin as _jellyfin
    return (
        _jellyfin.is_enabled()
        and _jellyfin._has_api_key()
        and _jellyfin._has_base_url()
    )
