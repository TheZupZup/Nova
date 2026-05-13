"""
Tests for the optional read-only Jellyfin music bridge (Phase 1).

Scope of the suite mirrors the design brief's testing requirements:

  * disabled bridge reports ``disabled`` and never reaches the wire,
  * missing API key reports ``not_configured`` without leaking secrets,
  * invalid Jellyfin / HTTP errors are sanitised and surface as
    ``unavailable`` with no key bleed,
  * read-only listing of artists / albums / tracks / genres /
    playlists works against a mocked Jellyfin response,
  * empty libraries return an empty result without raising,
  * the bridge module exposes no write helpers and contains no
    POST / PUT / PATCH / DELETE / playlist-create-style calls,
  * the FastAPI endpoints are admin-only — non-admin and restricted
    users get a 403,
  * the configured API key never appears in any returned JSON body,
  * the recommendation helper is deterministic.

The HTTP transport is stubbed via the same ``httpx.Client`` factory
swap pattern used by the GitHub integration tests, so no real
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
from core.integrations.media import jellyfin as jf
from core.integrations.media import recommendations as rec
from memory import store as natural_store


SECRET_KEY = "jf_SECRET_must_never_leak_abcdef1234567890"
SECRET_FRAGMENT = "SECRET_must_never_leak"


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
                 raise_decode: bool = False):
        self.status_code = status_code
        self._payload = payload
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
def jellyfin_stub(monkeypatch):
    """Replace ``httpx.Client`` in the bridge and enable the integration.

    Returns ``(install, calls)``; ``install(scripted, raise_on=...)``
    sets the scripted responses, and ``calls`` accumulates every
    outbound request (method, path, kwargs, captured headers).
    """
    calls: list = []
    state: dict = {"scripted": {}, "raise_on": set()}

    monkeypatch.setattr(jf, "NOVA_JELLYFIN_ENABLED", True)
    monkeypatch.setattr(jf, "NOVA_JELLYFIN_URL", "http://127.0.0.1:8096")
    monkeypatch.setattr(jf, "NOVA_JELLYFIN_API_KEY", SECRET_KEY)
    monkeypatch.setattr(
        jf, "NOVA_JELLYFIN_USER_ID", "00000000-0000-0000-0000-000000000001",
    )
    monkeypatch.setattr(jf, "NOVA_JELLYFIN_READ_ONLY", True)

    def factory(*args, **kwargs):
        return _FakeClient(
            state["scripted"], calls, state["raise_on"],
            headers=kwargs.get("headers", {}),
        )

    monkeypatch.setattr(jf.httpx, "Client", factory)

    def install(scripted: dict, raise_on: set | None = None):
        state["scripted"] = scripted
        state["raise_on"] = raise_on or set()

    return install, calls


# ── Switches ────────────────────────────────────────────────────────


class TestSwitches:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.setattr(jf, "NOVA_JELLYFIN_ENABLED", False)
        assert jf.is_enabled() is False

    def test_enabled_when_env_true(self, monkeypatch):
        monkeypatch.setattr(jf, "NOVA_JELLYFIN_ENABLED", True)
        assert jf.is_enabled() is True

    def test_read_only_default_true(self, monkeypatch):
        monkeypatch.setattr(jf, "NOVA_JELLYFIN_READ_ONLY", True)
        assert jf.is_read_only() is True


# ── Status states ───────────────────────────────────────────────────


class TestStatus:
    def test_disabled_when_switch_off(self, monkeypatch):
        monkeypatch.setattr(jf, "NOVA_JELLYFIN_ENABLED", False)
        monkeypatch.setattr(jf, "NOVA_JELLYFIN_API_KEY", SECRET_KEY)
        s = jf.status()
        assert s.state == jf.STATE_DISABLED
        assert s.enabled is False

    def test_not_configured_when_key_missing(self, monkeypatch):
        monkeypatch.setattr(jf, "NOVA_JELLYFIN_ENABLED", True)
        monkeypatch.setattr(jf, "NOVA_JELLYFIN_URL", "http://127.0.0.1:8096")
        monkeypatch.setattr(jf, "NOVA_JELLYFIN_API_KEY", "")
        s = jf.status()
        assert s.state == jf.STATE_NOT_CONFIGURED
        assert s.enabled is True
        assert SECRET_FRAGMENT not in s.detail

    def test_not_configured_when_url_missing(self, monkeypatch):
        monkeypatch.setattr(jf, "NOVA_JELLYFIN_ENABLED", True)
        monkeypatch.setattr(jf, "NOVA_JELLYFIN_URL", "")
        monkeypatch.setattr(jf, "NOVA_JELLYFIN_API_KEY", SECRET_KEY)
        s = jf.status()
        assert s.state == jf.STATE_NOT_CONFIGURED
        assert s.enabled is True

    def test_connected_on_2xx(self, jellyfin_stub):
        install, _ = jellyfin_stub
        install({
            ("GET", "/System/Info/Public"): _FakeResponse(
                200, {"ServerName": "JellyBox", "Version": "10.8.13"},
            ),
            ("GET", "/Users/00000000-0000-0000-0000-000000000001/Views"):
                _FakeResponse(200, {"Items": [
                    {"Name": "Music", "CollectionType": "music"},
                ]}),
        })
        s = jf.status()
        assert s.state == jf.STATE_CONNECTED
        assert s.server_name == "JellyBox"
        assert s.server_version == "10.8.13"
        assert s.library_kinds == ("music",)

    def test_unavailable_on_401(self, jellyfin_stub):
        install, _ = jellyfin_stub
        install({("GET", "/System/Info/Public"): _FakeResponse(401)})
        s = jf.status()
        assert s.state == jf.STATE_UNAVAILABLE
        assert SECRET_FRAGMENT not in s.detail

    def test_unavailable_on_5xx(self, jellyfin_stub):
        install, _ = jellyfin_stub
        install({("GET", "/System/Info/Public"): _FakeResponse(503)})
        s = jf.status()
        assert s.state == jf.STATE_UNAVAILABLE

    def test_unavailable_on_network_error(self, jellyfin_stub):
        install, _ = jellyfin_stub
        install({}, raise_on={"/System/Info/Public"})
        s = jf.status()
        assert s.state == jf.STATE_UNAVAILABLE
        assert SECRET_FRAGMENT not in s.detail

    def test_no_http_when_disabled(self, monkeypatch, jellyfin_stub):
        install, calls = jellyfin_stub
        monkeypatch.setattr(jf, "NOVA_JELLYFIN_ENABLED", False)
        install({("GET", "/System/Info/Public"): _FakeResponse(200, {})})
        jf.status()
        assert calls == []

    def test_no_http_when_key_missing(self, monkeypatch, jellyfin_stub):
        install, calls = jellyfin_stub
        monkeypatch.setattr(jf, "NOVA_JELLYFIN_API_KEY", "")
        install({("GET", "/System/Info/Public"): _FakeResponse(200, {})})
        jf.status()
        assert calls == []


# ── Read API ────────────────────────────────────────────────────────


_ARTIST_PAYLOAD = {
    "Id": "artist-1",
    "Name": "Josh A",
    "Genres": ["Hip-Hop", "Emo"],
}

_ALBUM_PAYLOAD = {
    "Id": "album-1",
    "Name": "The Forest",
    "AlbumArtist": "Josh A",
    "ProductionYear": 2020,
    "Genres": ["Hip-Hop"],
}

_TRACK_PAYLOAD = {
    "Id": "track-1",
    "Name": "Save Me",
    "Artists": ["Josh A"],
    "AlbumArtist": "Josh A",
    "Album": "The Forest",
    "ProductionYear": 2020,
    "Genres": ["Hip-Hop"],
    "RunTimeTicks": 200 * 10_000_000,  # 200 seconds
}

_GENRE_PAYLOAD = {
    "Id": "genre-1",
    "Name": "Hip-Hop",
}

_PLAYLIST_PAYLOAD = {
    "Id": "pl-1",
    "Name": "Late Night",
    "ChildCount": 17,
    "RunTimeTicks": 3600 * 10_000_000,
    "MediaType": "Audio",
}

_USER_ITEMS_PATH = "/Users/00000000-0000-0000-0000-000000000001/Items"


class TestListArtists:
    def test_returns_sanitised_artists(self, jellyfin_stub):
        install, calls = jellyfin_stub
        install({
            ("GET", "/Artists"): _FakeResponse(
                200, {"Items": [_ARTIST_PAYLOAD]},
            ),
        })
        result = jf.list_artists()
        assert result == [{
            "id": "artist-1",
            "name": "Josh A",
            "genres": ["Hip-Hop", "Emo"],
        }]
        assert calls[-1]["kwargs"]["params"]["IncludeItemTypes"] == "MusicArtist"

    def test_empty_when_disabled(self, monkeypatch, jellyfin_stub):
        install, calls = jellyfin_stub
        monkeypatch.setattr(jf, "NOVA_JELLYFIN_ENABLED", False)
        install({("GET", "/Artists"): _FakeResponse(200, {"Items": []})})
        assert jf.list_artists() == []
        assert calls == []

    def test_empty_on_network_error(self, jellyfin_stub):
        install, _ = jellyfin_stub
        install({}, raise_on={"/Artists"})
        assert jf.list_artists() == []

    def test_empty_on_malformed_response(self, jellyfin_stub):
        install, _ = jellyfin_stub
        install({("GET", "/Artists"): _FakeResponse(200, "not-a-dict")})
        assert jf.list_artists() == []

    def test_limit_clamped(self, jellyfin_stub):
        install, calls = jellyfin_stub
        install({("GET", "/Artists"): _FakeResponse(200, {"Items": []})})
        jf.list_artists(limit=10_000)
        assert calls[-1]["kwargs"]["params"]["Limit"] == 200


class TestListAlbums:
    def test_returns_sanitised_albums(self, jellyfin_stub):
        install, _ = jellyfin_stub
        install({
            ("GET", _USER_ITEMS_PATH): _FakeResponse(
                200, {"Items": [_ALBUM_PAYLOAD]},
            ),
        })
        result = jf.list_albums()
        assert result == [{
            "id": "album-1",
            "name": "The Forest",
            "artist": "Josh A",
            "year": 2020,
            "genres": ["Hip-Hop"],
        }]

    def test_empty_when_url_missing(self, monkeypatch, jellyfin_stub):
        install, calls = jellyfin_stub
        monkeypatch.setattr(jf, "NOVA_JELLYFIN_URL", "")
        install({
            ("GET", _USER_ITEMS_PATH): _FakeResponse(
                200, {"Items": [_ALBUM_PAYLOAD]},
            ),
        })
        assert jf.list_albums() == []
        assert calls == []


class TestListTracks:
    def test_returns_sanitised_tracks(self, jellyfin_stub):
        install, _ = jellyfin_stub
        install({
            ("GET", _USER_ITEMS_PATH): _FakeResponse(
                200, {"Items": [_TRACK_PAYLOAD]},
            ),
        })
        result = jf.list_tracks()
        assert result == [{
            "id": "track-1",
            "title": "Save Me",
            "artist": "Josh A",
            "artists": ["Josh A"],
            "album": "The Forest",
            "year": 2020,
            "genres": ["Hip-Hop"],
            "duration": 200,
        }]

    def test_duration_none_when_ticks_missing(self, jellyfin_stub):
        install, _ = jellyfin_stub
        item = {**_TRACK_PAYLOAD}
        item.pop("RunTimeTicks")
        install({
            ("GET", _USER_ITEMS_PATH): _FakeResponse(200, {"Items": [item]}),
        })
        result = jf.list_tracks()
        assert result[0]["duration"] is None

    def test_empty_library_returns_empty_list(self, jellyfin_stub):
        install, _ = jellyfin_stub
        install({
            ("GET", _USER_ITEMS_PATH): _FakeResponse(200, {"Items": []}),
        })
        assert jf.list_tracks() == []


class TestListGenres:
    def test_returns_sanitised_genres(self, jellyfin_stub):
        install, _ = jellyfin_stub
        install({
            ("GET", "/MusicGenres"): _FakeResponse(
                200, {"Items": [_GENRE_PAYLOAD]},
            ),
        })
        assert jf.list_genres() == [{"id": "genre-1", "name": "Hip-Hop"}]


class TestListPlaylists:
    def test_returns_sanitised_playlists(self, jellyfin_stub):
        install, _ = jellyfin_stub
        install({
            ("GET", _USER_ITEMS_PATH): _FakeResponse(
                200, {"Items": [_PLAYLIST_PAYLOAD]},
            ),
        })
        result = jf.list_playlists()
        assert result == [{
            "id": "pl-1",
            "name": "Late Night",
            "track_count": 17,
            "duration": 3600,
            "media_kind": "audio",
        }]

    def test_empty_when_unreachable(self, jellyfin_stub):
        install, _ = jellyfin_stub
        install({}, raise_on={_USER_ITEMS_PATH})
        assert jf.list_playlists() == []


class TestLibrarySnapshot:
    def test_aggregates_helpers(self, jellyfin_stub):
        install, _ = jellyfin_stub
        install({
            ("GET", "/Artists"): _FakeResponse(200, {"Items": [_ARTIST_PAYLOAD]}),
            ("GET", _USER_ITEMS_PATH): _FakeResponse(200, {"Items": []}),
            ("GET", "/MusicGenres"): _FakeResponse(200, {"Items": []}),
        })
        snap = jf.library_snapshot()
        assert snap["read_only"] is True
        assert isinstance(snap["artists"], list)
        assert isinstance(snap["tracks"], list)


# ── Key safety ──────────────────────────────────────────────────────


class TestApiKeySafety:
    def test_key_not_in_status_payload(self, jellyfin_stub):
        install, _ = jellyfin_stub
        install({
            ("GET", "/System/Info/Public"): _FakeResponse(
                200, {"ServerName": "JellyBox", "Version": "10.8"},
            ),
            ("GET", "/Users/00000000-0000-0000-0000-000000000001/Views"):
                _FakeResponse(200, {"Items": []}),
        })
        serialised = repr(jf.status().as_dict())
        assert SECRET_FRAGMENT not in serialised

    def test_key_not_in_track_payload(self, jellyfin_stub):
        install, _ = jellyfin_stub
        install({
            ("GET", _USER_ITEMS_PATH): _FakeResponse(
                200, {"Items": [_TRACK_PAYLOAD]},
            ),
        })
        out = jf.list_tracks()
        assert SECRET_FRAGMENT not in repr(out)

    def test_key_only_in_header(self, jellyfin_stub):
        install, calls = jellyfin_stub
        install({
            ("GET", "/System/Info/Public"): _FakeResponse(
                200, {"ServerName": "JellyBox"},
            ),
            ("GET", "/Users/00000000-0000-0000-0000-000000000001/Views"):
                _FakeResponse(200, {"Items": []}),
        })
        jf.status()
        assert calls, "expected at least one outbound request"
        captured = calls[0]["headers"]
        assert captured.get(jf.API_KEY_HEADER) == SECRET_KEY
        for call in calls:
            assert SECRET_FRAGMENT not in call["path"]
            assert SECRET_FRAGMENT not in repr(call["kwargs"])

    def test_logger_messages_omit_key(self, monkeypatch, jellyfin_stub, caplog):
        install, _ = jellyfin_stub
        install({}, raise_on={"/System/Info/Public"})
        with caplog.at_level("DEBUG", logger=jf.logger.name):
            jf.status()
        for record in caplog.records:
            assert SECRET_FRAGMENT not in record.getMessage()
            assert SECRET_FRAGMENT not in str(record.args or "")


# ── Module-level "no write code" enforcement ────────────────────────


class TestNoWriteCode:
    """The Phase-1 module must not call any write verb against Jellyfin.

    A future PR can add playlist-creation helpers, but they must come
    with their own gating + audit + confirmation logic. This test
    fails fast if POST / PUT / PATCH / DELETE leak into the read-only
    module by accident.
    """

    def test_module_has_no_write_helpers(self):
        for name in (
            "create_playlist", "update_playlist", "delete_playlist",
            "add_to_playlist", "remove_from_playlist",
            "play", "queue_track", "start_playback",
        ):
            assert not hasattr(jf, name), (
                f"{name!r} should not exist in the Phase-1 bridge"
            )

    def test_module_never_calls_write_verbs(self):
        with open(jf.__file__, "r", encoding="utf-8") as f:
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

    def test_module_does_not_import_cloud_music_apis(self):
        with open(jf.__file__, "r", encoding="utf-8") as f:
            text = f.read()
        for forbidden in ("spotify", "tidal", "deezer", "youtube", "soundcloud"):
            assert forbidden not in text.lower(), (
                f"bridge must not reference cloud provider {forbidden!r}"
            )


# ── Endpoint: admin-only enforcement ────────────────────────────────


class TestEndpointsAdminOnly:
    @pytest.fixture(autouse=True)
    def _stub(self, jellyfin_stub):
        install, _ = jellyfin_stub
        install({
            ("GET", "/System/Info/Public"): _FakeResponse(
                200, {"ServerName": "JellyBox"},
            ),
            ("GET", "/Users/00000000-0000-0000-0000-000000000001/Views"):
                _FakeResponse(200, {"Items": []}),
            ("GET", "/Artists"): _FakeResponse(200, {"Items": [_ARTIST_PAYLOAD]}),
            ("GET", _USER_ITEMS_PATH): _FakeResponse(200, {"Items": []}),
            ("GET", "/MusicGenres"): _FakeResponse(200, {"Items": []}),
        })

    @pytest.mark.parametrize("path", [
        "/integrations/media/jellyfin/status",
        "/integrations/media/jellyfin/artists",
        "/integrations/media/jellyfin/albums",
        "/integrations/media/jellyfin/tracks",
        "/integrations/media/jellyfin/genres",
        "/integrations/media/jellyfin/playlists",
        "/integrations/media/recommendations",
    ])
    def test_non_admin_user_forbidden(self, web_client, user_token, path):
        resp = web_client.get(path, headers=_h(user_token))
        assert resp.status_code == 403

    @pytest.mark.parametrize("path", [
        "/integrations/media/jellyfin/status",
        "/integrations/media/jellyfin/tracks",
        "/integrations/media/recommendations",
    ])
    def test_restricted_user_forbidden(self, web_client, restricted_token, path):
        resp = web_client.get(path, headers=_h(restricted_token))
        assert resp.status_code == 403

    @pytest.mark.parametrize("path", [
        "/integrations/media/jellyfin/status",
        "/integrations/media/jellyfin/tracks",
        "/integrations/media/recommendations",
    ])
    def test_unauthenticated_blocked(self, web_client, path):
        resp = web_client.get(path)
        assert resp.status_code in (401, 403)


# ── Endpoint: admin happy path ──────────────────────────────────────


class TestEndpointsAdmin:
    @pytest.fixture(autouse=True)
    def _stub(self, jellyfin_stub):
        install, _ = jellyfin_stub
        install({
            ("GET", "/System/Info/Public"): _FakeResponse(
                200, {"ServerName": "JellyBox", "Version": "10.8.13"},
            ),
            ("GET", "/Users/00000000-0000-0000-0000-000000000001/Views"):
                _FakeResponse(200, {"Items": [
                    {"Name": "Music", "CollectionType": "music"},
                ]}),
            ("GET", "/Artists"): _FakeResponse(200, {"Items": [_ARTIST_PAYLOAD]}),
            ("GET", _USER_ITEMS_PATH): _FakeResponse(200, {"Items": [_TRACK_PAYLOAD]}),
            ("GET", "/MusicGenres"): _FakeResponse(200, {"Items": [_GENRE_PAYLOAD]}),
        })

    def test_status_endpoint(self, web_client, admin_token):
        resp = web_client.get(
            "/integrations/media/jellyfin/status", headers=_h(admin_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == jf.STATE_CONNECTED
        assert body["read_only"] is True
        assert body["server_name"] == "JellyBox"
        assert "api_key" not in body
        assert SECRET_FRAGMENT not in resp.text

    def test_status_disabled_when_switch_off(
        self, monkeypatch, web_client, admin_token,
    ):
        monkeypatch.setattr(jf, "NOVA_JELLYFIN_ENABLED", False)
        resp = web_client.get(
            "/integrations/media/jellyfin/status", headers=_h(admin_token),
        )
        assert resp.status_code == 200
        assert resp.json()["state"] == jf.STATE_DISABLED

    def test_list_artists(self, web_client, admin_token):
        resp = web_client.get(
            "/integrations/media/jellyfin/artists", headers=_h(admin_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["read_only"] is True
        assert body["artists"][0]["name"] == "Josh A"
        assert SECRET_FRAGMENT not in resp.text

    def test_list_tracks(self, web_client, admin_token):
        resp = web_client.get(
            "/integrations/media/jellyfin/tracks", headers=_h(admin_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["tracks"][0]["title"] == "Save Me"
        assert body["tracks"][0]["duration"] == 200

    def test_endpoints_503_when_unconfigured(
        self, monkeypatch, web_client, admin_token, jellyfin_stub,
    ):
        install, _ = jellyfin_stub
        monkeypatch.setattr(jf, "NOVA_JELLYFIN_API_KEY", "")
        install({})
        resp = web_client.get(
            "/integrations/media/jellyfin/status", headers=_h(admin_token),
        )
        assert resp.status_code == 200
        assert resp.json()["state"] == jf.STATE_NOT_CONFIGURED
        for path in (
            "/integrations/media/jellyfin/artists",
            "/integrations/media/jellyfin/albums",
            "/integrations/media/jellyfin/tracks",
            "/integrations/media/jellyfin/genres",
            "/integrations/media/jellyfin/playlists",
            "/integrations/media/recommendations",
        ):
            resp = web_client.get(path, headers=_h(admin_token))
            assert resp.status_code == 503, path

    def test_endpoints_404_when_disabled(
        self, monkeypatch, web_client, admin_token,
    ):
        monkeypatch.setattr(jf, "NOVA_JELLYFIN_ENABLED", False)
        for path in (
            "/integrations/media/jellyfin/artists",
            "/integrations/media/jellyfin/tracks",
            "/integrations/media/recommendations",
        ):
            resp = web_client.get(path, headers=_h(admin_token))
            assert resp.status_code == 404, path

    def test_endpoint_errors_do_not_leak_key(
        self, web_client, admin_token, jellyfin_stub,
    ):
        install, _ = jellyfin_stub
        install({}, raise_on={"/System/Info/Public", "/Artists"})
        for path in (
            "/integrations/media/jellyfin/status",
            "/integrations/media/jellyfin/artists",
        ):
            resp = web_client.get(path, headers=_h(admin_token))
            assert SECRET_FRAGMENT not in resp.text, path

    def test_recommendations_endpoint(self, web_client, admin_token, jellyfin_stub):
        # Populate the track pool with enough chill/focus material to
        # produce at least one playlist (min_tracks = 3).
        install, _ = jellyfin_stub
        items = [
            {**_TRACK_PAYLOAD, "Id": f"t{i}", "Name": f"Lo-Fi Study {i}",
             "Genres": ["Lo-Fi", "Ambient"]}
            for i in range(6)
        ]
        install({
            ("GET", "/System/Info/Public"): _FakeResponse(
                200, {"ServerName": "JellyBox"},
            ),
            ("GET", "/Users/00000000-0000-0000-0000-000000000001/Views"):
                _FakeResponse(200, {"Items": []}),
            ("GET", _USER_ITEMS_PATH): _FakeResponse(200, {"Items": items}),
        })
        resp = web_client.get(
            "/integrations/media/recommendations", headers=_h(admin_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["read_only"] is True
        assert isinstance(body["recommendations"], list)
        moods = {p["mood"] for p in body["recommendations"]}
        # The lo-fi tracks lean toward focus/coding/chill.
        assert "focus" in moods or "coding" in moods or "chill" in moods


# ── Aggregate /integrations/status surface ──────────────────────────


class TestAggregateStatus:
    def test_admin_sees_jellyfin_state(self, web_client, admin_token, jellyfin_stub):
        install, _ = jellyfin_stub
        install({
            ("GET", "/System/Info/Public"): _FakeResponse(
                200, {"ServerName": "JellyBox"},
            ),
            ("GET", "/Users/00000000-0000-0000-0000-000000000001/Views"):
                _FakeResponse(200, {"Items": []}),
        })
        resp = web_client.get("/integrations/status", headers=_h(admin_token))
        assert resp.status_code == 200
        jellyfin = resp.json()["jellyfin"]
        assert jellyfin["state"] == jf.STATE_CONNECTED

    def test_non_admin_sees_disabled_jellyfin(
        self, web_client, user_token, jellyfin_stub,
    ):
        install, _ = jellyfin_stub
        install({
            ("GET", "/System/Info/Public"): _FakeResponse(
                200, {"ServerName": "JellyBox"},
            ),
        })
        resp = web_client.get("/integrations/status", headers=_h(user_token))
        assert resp.status_code == 200
        jellyfin = resp.json()["jellyfin"]
        assert jellyfin["state"] == jf.STATE_DISABLED
        assert jellyfin["enabled"] is False


# ── Recommendation heuristics ───────────────────────────────────────


def _track(title, genres=None, duration=200, **extra):
    out = {
        "id": title.lower().replace(" ", "-"),
        "title": title,
        "artist": "Test Artist",
        "artists": ["Test Artist"],
        "album": "Test Album",
        "year": 2020,
        "genres": list(genres) if genres else [],
        "duration": duration,
    }
    out.update(extra)
    return out


class TestRecommendations:
    def test_empty_library_returns_empty_list(self):
        assert rec.recommend_playlists([]) == []

    def test_only_unmatched_moods_returns_empty(self):
        tracks = [_track("Mystery", genres=["Polka"])]
        assert rec.recommend_playlists(tracks) == []

    def test_chill_playlist_from_lofi(self):
        tracks = [
            _track(f"Lo-Fi {i}", genres=["Lo-Fi", "Ambient"])
            for i in range(6)
        ]
        out = rec.recommend_playlists(tracks)
        moods = [p["mood"] for p in out]
        assert "chill" in moods
        chill = next(p for p in out if p["mood"] == "chill")
        assert chill["title"] == "Chill Wind-Down"
        assert chill["description"]
        assert len(chill["tracks"]) >= 3
        for entry in chill["tracks"]:
            assert "reason" in entry and entry["reason"]

    def test_gym_playlist_from_metal(self):
        tracks = [
            _track(f"Heavy {i}", genres=["Metal"]) for i in range(6)
        ]
        out = rec.recommend_playlists(tracks, moods=["gym", "dark"])
        moods = [p["mood"] for p in out]
        assert "gym" in moods
        assert "dark" in moods

    def test_deterministic_output(self):
        tracks = [
            _track(f"Chill {i}", genres=["Ambient"]) for i in range(8)
        ]
        first = rec.recommend_playlists(tracks)
        second = rec.recommend_playlists(tracks)
        assert first == second

    def test_estimated_duration_sums_track_seconds(self):
        tracks = [
            _track(f"Chill {i}", genres=["Ambient"], duration=180)
            for i in range(5)
        ]
        out = rec.recommend_playlists(tracks, moods=["chill"])
        assert out[0]["estimated_duration"] is not None
        assert out[0]["estimated_duration"] >= 180 * len(out[0]["tracks"]) - 5

    def test_estimated_duration_none_when_durations_missing(self):
        tracks = [
            _track(f"Chill {i}", genres=["Ambient"], duration=None)
            for i in range(5)
        ]
        out = rec.recommend_playlists(tracks, moods=["chill"])
        assert out[0]["estimated_duration"] is None

    def test_title_token_signal(self):
        tracks = [
            _track("Night Drive Anthem", genres=["Pop"]),
            _track("Midnight Highway", genres=["Pop"]),
            _track("Drive Home", genres=["Pop"]),
            _track("Late Night Cruise", genres=["Pop"]),
        ]
        out = rec.recommend_playlists(tracks, moods=["night drive"])
        assert out and out[0]["mood"] == "night drive"

    def test_limit_clamps(self):
        tracks = [
            _track(f"Mix {i}", genres=["Lo-Fi", "Metal", "EDM"])
            for i in range(20)
        ]
        out = rec.recommend_playlists(tracks, limit=999)
        # _MAX_MOODS_LIMIT = 12; the mood catalogue currently has 8
        # entries so the upper bound is the catalogue size.
        assert len(out) <= 12

    def test_per_playlist_clamps(self):
        tracks = [
            _track(f"Chill {i}", genres=["Ambient"]) for i in range(50)
        ]
        out = rec.recommend_playlists(
            tracks, moods=["chill"], per_playlist=999,
        )
        assert len(out[0]["tracks"]) <= 25

    def test_unknown_mood_filtered_out(self):
        tracks = [
            _track(f"Chill {i}", genres=["Ambient"]) for i in range(5)
        ]
        # Unknown moods produce an empty list, not an error.
        assert rec.recommend_playlists(tracks, moods=["banana"]) == []

    def test_reason_string_explains_why(self):
        tracks = [
            _track(f"Sad Track {i}", genres=["Blues"]) for i in range(5)
        ]
        out = rec.recommend_playlists(tracks, moods=["sad"])
        for entry in out[0]["tracks"]:
            assert entry["reason"].startswith("matches sad mood")


class TestRecommendationsFromJellyfin:
    def test_disabled_returns_empty(self, monkeypatch):
        monkeypatch.setattr(jf, "NOVA_JELLYFIN_ENABLED", False)
        assert rec.recommend_from_jellyfin() == []

    def test_missing_key_returns_empty(self, monkeypatch):
        monkeypatch.setattr(jf, "NOVA_JELLYFIN_ENABLED", True)
        monkeypatch.setattr(jf, "NOVA_JELLYFIN_URL", "http://x")
        monkeypatch.setattr(jf, "NOVA_JELLYFIN_API_KEY", "")
        assert rec.recommend_from_jellyfin() == []

    def test_pulls_tracks_and_recommends(self, jellyfin_stub):
        install, _ = jellyfin_stub
        items = [
            {**_TRACK_PAYLOAD, "Id": f"t{i}",
             "Name": f"Chill Vibes {i}",
             "Genres": ["Lo-Fi", "Ambient"]}
            for i in range(8)
        ]
        install({
            ("GET", _USER_ITEMS_PATH): _FakeResponse(200, {"Items": items}),
        })
        out = rec.recommend_from_jellyfin()
        assert out, "expected at least one playlist suggestion"
        for playlist in out:
            for entry in playlist["tracks"]:
                assert SECRET_FRAGMENT not in str(entry)
