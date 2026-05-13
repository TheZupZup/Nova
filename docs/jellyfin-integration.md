# Local media assistant — Jellyfin (Phase 1)

Nova is a **local media assistant**, not an autonomous media manager.
The Phase-1 Jellyfin bridge lets a Nova admin help users *explore*
their locally-hosted Jellyfin music library and surface deterministic
playlist suggestions — without sending library data to any cloud
service and without modifying Jellyfin's state.

This document describes the Phase-1 scope, the configuration surface,
the endpoints, and the privacy / safety contract. It is the
companion reference for the section in [README.md](../README.md).

## Scope (Phase 1)

What the bridge does:

- check the Jellyfin connection status,
- list music artists, albums, tracks, genres, and playlists,
- generate deterministic playlist suggestions from local metadata.

What the bridge does **not** do (now or via this PR):

- create, edit, or delete playlists on Jellyfin,
- stream, transcode, or copy any media file,
- start playback, queue tracks, or autoplay anything,
- change Jellyfin server settings,
- talk to any cloud music API (Spotify, Tidal, Deezer, YouTube,
  SoundCloud, etc.),
- scan local disk outside of Jellyfin's own metadata,
- poll Jellyfin in the background or run scheduled work,
- modify Auryn behaviour or be involved in Auryn's library
  population — those remain a separate project.

## Configuration

All switches default to OFF. An unconfigured Nova install **never**
contacts Jellyfin, even for an admin.

```ini
NOVA_JELLYFIN_ENABLED=true
NOVA_JELLYFIN_URL=http://127.0.0.1:8096
NOVA_JELLYFIN_API_KEY=your_local_jellyfin_api_key
NOVA_JELLYFIN_USER_ID=                   # optional Jellyfin user GUID
NOVA_JELLYFIN_READ_ONLY=true             # default; Phase 1 has no writes
NOVA_JELLYFIN_TIMEOUT_SECONDS=5.0
```

### Generating a Jellyfin API key

1. Open Jellyfin's web UI and sign in as an admin.
2. Go to *Dashboard → API Keys*.
3. Click *+* and provide a label (e.g. `nova-local-bridge`).
4. Copy the generated key into `NOVA_JELLYFIN_API_KEY`.

The key only needs **read** scopes. Nova never performs write
operations against Jellyfin in Phase 1, so there is no need to give
the key admin or playback-control permissions.

### Choosing a user ID (optional)

`NOVA_JELLYFIN_USER_ID` is optional. When set to a valid Jellyfin
user GUID, library reads are scoped to that user's view (so per-user
library visibility in Jellyfin is honoured). When empty, the bridge
falls back to server-wide read endpoints.

## Endpoints

All endpoints are admin-only. Non-admin and restricted users receive
a 403; the configured key, URL, and user ID are never serialised
into any response body.

| Endpoint | Description |
| --- | --- |
| `GET /integrations/media/jellyfin/status`     | Calm snapshot of the bridge. |
| `GET /integrations/media/jellyfin/artists`    | List music artists. |
| `GET /integrations/media/jellyfin/albums`     | List music albums. |
| `GET /integrations/media/jellyfin/tracks`     | List music tracks. |
| `GET /integrations/media/jellyfin/genres`     | List music genres. |
| `GET /integrations/media/jellyfin/playlists`  | List playlists (read-only). |
| `GET /integrations/media/recommendations`     | Playlist suggestions. |

### Status states

- `disabled` — host operator has not set `NOVA_JELLYFIN_ENABLED=true`.
- `not_configured` — the URL or the API key is missing.
- `unavailable` — Jellyfin returned an error or could not be reached.
  The detail field is a short, hard-coded summary; it never echoes
  the raw exception or the response body.
- `connected_read_only` — the bridge is reachable and authenticated
  in read-only mode.

### Recommendation payload

`GET /integrations/media/recommendations` returns a list of playlist
ideas. Each entry has the shape:

```json
{
  "title": "Night Coding",
  "mood": "coding",
  "description": "Calm tracks for late-night development.",
  "estimated_duration": 3600,
  "confidence": "high",
  "tracks": [
    {
      "id": "track-1",
      "title": "Lo-Fi Study",
      "artist": "Example Artist",
      "album": "Example Album",
      "duration": 200,
      "reason": "matches coding mood: genre lo-fi; title hints 'study'."
    }
  ]
}
```

- `estimated_duration` is the sum of the picked tracks' durations in
  whole seconds, or `null` when no track in the playlist reports one.
- `confidence` is `low` / `medium` / `high` based on the number of
  strong-signal tracks.
- `reason` is a short, sanitised explanation of why the track was
  picked — never an LLM call, never a cloud lookup.

Available moods: `chill`, `focus`, `gym`, `dark`, `upbeat`, `sad`,
`night drive`, `coding`.

Query params:

- `mood=chill,focus` — comma-separated filter (entries not in the
  catalogue are dropped),
- `limit=8` — clamp to 1..12 (default 8),
- `per_playlist=12` — clamp to 3..25 (default 12).

The output is deterministic: identical libraries produce identical
suggestions.

## Privacy and safety contract

- **Local-first.** The bridge talks to your configured Jellyfin
  server only. No request ever leaves your network on Nova's behalf.
- **No cloud music APIs.** Nova does not contact Spotify, Tidal,
  Deezer, YouTube, SoundCloud, or any similar service.
- **No telemetry.** The bridge does not phone home and does not
  emit usage analytics anywhere.
- **No streaming or file copying.** The bridge reads metadata via
  Jellyfin's REST API. It does not download, stream, transcode, or
  cache audio data.
- **No autoplay.** The bridge does not start playback, queue tracks,
  or instruct Jellyfin to play anything.
- **No background polling.** Every request is initiated by an admin
  call to the corresponding endpoint.
- **Admin-only.** Both the configuration and the endpoints are
  gated to the `admin` role. The aggregate `/integrations/status`
  payload reports `disabled` for non-admin users so a non-admin UI
  cannot infer the configured state.
- **Sanitised errors.** Every error response is a short, hard-coded
  summary. Raw exception text, response bodies, and the configured
  key are never surfaced.

### API-key handling

- The key lives in env-local config only.
- The key is sent as the `X-Emby-Token` request header — never in
  URLs, query params, or JSON request bodies.
- Logger messages identify exceptions by type only (e.g.
  `"jellyfin status failed: ConnectError"`); the raw exception
  repr is never logged because it could leak the header.
- The key is not stored in the database in this PR.

## Architecture

The Phase-1 bridge intentionally splits into two small modules:

- `core/integrations/media/jellyfin.py` — read-only HTTP client. It
  knows how to talk to *one* media server (Jellyfin) and returns
  sanitised dicts.
- `core/integrations/media/recommendations.py` — pure-Python
  heuristics. It operates on the sanitised track shape and is
  provider-agnostic so a future Plex provider can plug in without
  touching playlist logic.

This split keeps the heuristics deterministic and explainable, and
lets contributors add a new provider in a single small module.

## Future direction (NOT in this PR)

- **Plex support** behind the same provider interface.
- **Playlist creation** behind a per-request confirmation in the UI
  and a separate write switch. Nova will never create a playlist
  without an explicit "yes" from the user, and the action will carry
  audit logging when it is introduced.
- **Auryn integration** can eventually help populate a local library,
  but Auryn remains a separate project. This bridge does not change
  Auryn's behaviour, and Auryn does not run as part of Nova.
