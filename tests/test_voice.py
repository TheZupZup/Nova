"""
Tests for the voice (text-to-speech) foundation.

The foundation ships with one provider — the browser engine — and a
small HTTP surface for voice config and per-message preparation.
Anything beyond that (server-rendered audio, voice picker UI, etc.)
is out of scope for this module and intentionally not tested here.
"""

from __future__ import annotations

import contextlib
import sqlite3
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core import memory as core_memory, users
from core import voice
from core.voice import providers as voice_providers
from memory import store as natural_store


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "nova.db")
    monkeypatch.setattr(core_memory, "DB_PATH", path)
    monkeypatch.setattr(natural_store, "DB_PATH", path)
    core_memory.initialize_db()
    return path


@pytest.fixture
def web_client(db_path, monkeypatch):
    """A TestClient backed by the temp DB, with background jobs suppressed."""
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


def _make_user(db_path, username, password="pw", role=users.ROLE_USER):
    with sqlite3.connect(db_path) as conn:
        return users.create_user(conn, username, password, role=role)


def _login(client, username, password="pw"):
    resp = client.post("/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _h(token):
    return {"Authorization": f"Bearer {token}"}


# ── prepare_text ────────────────────────────────────────────────────


class TestPrepareText:
    def test_strips_surrounding_whitespace(self):
        assert voice.prepare_text("  hello  ") == "hello"

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError):
            voice.prepare_text("")

    def test_rejects_whitespace_only(self):
        with pytest.raises(ValueError):
            voice.prepare_text("   \n\t  ")

    def test_rejects_non_string(self):
        with pytest.raises(ValueError):
            voice.prepare_text(None)  # type: ignore[arg-type]

    def test_caps_overly_long_input(self):
        too_long = "x" * (voice.MAX_TTS_INPUT_CHARS + 1)
        with pytest.raises(ValueError):
            voice.prepare_text(too_long)

    def test_accepts_input_at_cap(self):
        at_cap = "x" * voice.MAX_TTS_INPUT_CHARS
        assert voice.prepare_text(at_cap) == at_cap


# ── Provider abstraction ────────────────────────────────────────────


class TestBrowserProvider:
    def test_is_available(self):
        assert voice_providers.BrowserProvider().is_available() is True

    def test_engine_name(self):
        provider = voice_providers.BrowserProvider()
        assert provider.name == voice_providers.ENGINE_BROWSER
        assert provider.voice_config().engine == voice_providers.ENGINE_BROWSER

    def test_voice_config_carries_preferred_voices(self):
        config = voice_providers.BrowserProvider().voice_config()
        assert isinstance(config.preferred_voice_names, tuple)
        assert config.preferred_voice_names, "default voice list must not be empty"
        # Sanity-check at least one voice from each major platform so a
        # silent rename of the constant doesn't ship without a test fail.
        names = set(config.preferred_voice_names)
        assert "Samantha" in names              # Apple
        assert any("Microsoft" in n for n in names)
        assert any("Google" in n for n in names)

    def test_voice_config_uses_calm_defaults(self):
        config = voice_providers.BrowserProvider().voice_config()
        # Slightly slower than default, neutral pitch — the calm profile.
        assert 0.85 <= config.rate <= 1.0
        assert 0.9 <= config.pitch <= 1.1
        assert config.fade_ms >= 0

    def test_voice_config_as_dict_is_json_safe(self):
        payload = voice_providers.BrowserProvider().voice_config().as_dict()
        assert payload["engine"] == voice_providers.ENGINE_BROWSER
        assert isinstance(payload["preferred_voice_names"], list)
        assert isinstance(payload["rate"], float)
        assert isinstance(payload["pitch"], float)


class TestGetDefaultProvider:
    def test_returns_browser_provider(self):
        provider = voice.get_default_provider()
        assert isinstance(provider, voice_providers.BrowserProvider)


# ── HTTP endpoints ──────────────────────────────────────────────────


class TestVoiceConfigEndpoint:
    def test_requires_authentication(self, web_client):
        resp = web_client.get("/voice/config")
        assert resp.status_code in (401, 403)

    def test_returns_browser_engine_payload(self, db_path, web_client):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")

        resp = web_client.get("/voice/config", headers=_h(token))
        assert resp.status_code == 200
        body = resp.json()
        assert body["available"] is True
        assert body["engine"] == voice_providers.ENGINE_BROWSER
        assert isinstance(body["preferred_voice_names"], list)
        assert body["preferred_voice_names"], "non-empty voice list"
        assert "rate" in body and "pitch" in body and "volume" in body

    def test_payload_is_user_independent(self, db_path, web_client):
        # Voice preferences are not yet user-scoped; both users must see
        # the same calm default profile.
        _make_user(db_path, "alice")
        _make_user(db_path, "bob")
        a_token = _login(web_client, "alice")
        b_token = _login(web_client, "bob")

        a_body = web_client.get("/voice/config", headers=_h(a_token)).json()
        b_body = web_client.get("/voice/config", headers=_h(b_token)).json()
        assert a_body == b_body


class TestVoiceSynthesizeEndpoint:
    def test_requires_authentication(self, web_client):
        resp = web_client.post("/voice/synthesize", json={"text": "hello"})
        assert resp.status_code in (401, 403)

    def test_echoes_text_and_voice_profile(self, db_path, web_client):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")

        resp = web_client.post(
            "/voice/synthesize",
            json={"text": "  Hello, Nova.  "},
            headers=_h(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # prepare_text() must have stripped the input.
        assert body["text"] == "Hello, Nova."
        assert body["engine"] == voice_providers.ENGINE_BROWSER
        assert body["preferred_voice_names"]

    def test_rejects_empty_text(self, db_path, web_client):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        # Pydantic's min_length=1 short-circuits before reaching prepare_text;
        # whitespace-only is what exercises the server-side trim/empty path.
        resp = web_client.post(
            "/voice/synthesize",
            json={"text": "   "},
            headers=_h(token),
        )
        assert resp.status_code == 400

    def test_rejects_overly_long_text(self, db_path, web_client):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        resp = web_client.post(
            "/voice/synthesize",
            json={"text": "x" * (voice.MAX_TTS_INPUT_CHARS + 1)},
            headers=_h(token),
        )
        # Pydantic's max_length rejects with 422 before our handler runs.
        assert resp.status_code in (400, 422)

    def test_rejects_unknown_fields(self, db_path, web_client):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        resp = web_client.post(
            "/voice/synthesize",
            json={"text": "ok", "voice": "stranger"},
            headers=_h(token),
        )
        # `extra="forbid"` keeps the surface tight; future fields must be
        # added explicitly and tested.
        assert resp.status_code == 422

    def test_independent_users_share_provider(self, db_path, web_client):
        _make_user(db_path, "alice")
        _make_user(db_path, "bob")
        a_token = _login(web_client, "alice")
        b_token = _login(web_client, "bob")

        payload = {"text": "hello"}
        a_body = web_client.post(
            "/voice/synthesize", json=payload, headers=_h(a_token)
        ).json()
        b_body = web_client.post(
            "/voice/synthesize", json=payload, headers=_h(b_token)
        ).json()
        assert a_body == b_body
