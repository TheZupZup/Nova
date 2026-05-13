"""
Optional local media-assistant bridges (Phase 1: Jellyfin, read-only).

Nova is a **local media assistant**, not an autonomous media manager.
This sub-package collects read-only bridges that help a user *explore*
a locally-hosted media library and surface deterministic suggestions
from the library's own metadata.

What lives here:

  * :mod:`jellyfin`        — read-only HTTP client for a local
                             Jellyfin server (status + music metadata).
  * :mod:`recommendations` — pure-Python heuristics that turn a list
                             of sanitised tracks into short, calmly
                             ranked playlist suggestions. Never makes
                             HTTP calls; never uses ML or embeddings.

Provider / recommendation split (intentional):

  * The provider module knows how to talk to *one* media server and
    returns small, sanitised dicts the rest of Nova can splice into
    chat context safely.
  * The recommendation module never imports a provider directly — it
    operates on the sanitised track dicts. That keeps the heuristics
    deterministic and lets a future Plex provider plug in without
    touching the playlist logic.

Phase 1 boundaries (enforced):

  * Read-only HTTP against the configured local server.
  * No playlist creation, edit, or deletion.
  * No streaming, transcoding, or copying of media files.
  * No cloud music APIs.
  * No background polling / autoplay.
  * No on-disk scanning outside of Jellyfin.
  * API keys live only in env config and the connector's request
    headers — never in any response body, log line, or chat context.

Future direction (NOT in this PR):

  * Plex support behind the same provider interface.
  * Playlist creation, gated by an explicit per-request confirmation
    in the UI and a separate write switch.
  * Auryn-led library population, as a separate project.
"""
