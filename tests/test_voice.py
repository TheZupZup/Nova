"""
Tests for the voice (text-to-speech) foundation.

The foundation ships two providers — the always-available browser
engine and an optional local Piper engine — plus a small HTTP surface
for voice config and per-message preparation. The Piper tests stub out
the subprocess so they never actually exec piper.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core import memory as core_memory, users
from core import voice
from core.voice import providers as voice_providers
from core.voice import piper as voice_piper
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


# ── Piper provider ──────────────────────────────────────────────────


def _make_piper_files(tmp_path, with_config=True):
    """Build a fake piper binary and a fake voice model on disk.

    Returns ``(binary_path, model_path, config_path)``. The binary is
    just an executable shell stub; the synthesis tests replace
    ``subprocess.run`` so it is never actually executed.
    """
    binary = tmp_path / "piper"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    model = tmp_path / "voice.onnx"
    model.write_bytes(b"\x00\x01\x02")
    config = None
    if with_config:
        config = tmp_path / "voice.onnx.json"
        config.write_text("{}")
    return str(binary), str(model), (str(config) if config else "")


class TestPiperProviderAvailability:
    def test_unavailable_when_nothing_configured(self):
        provider = voice_piper.PiperProvider(binary="", model="", config="")
        assert provider.is_available() is False
        status = provider.status()
        assert status.available is False
        assert status.binary_found is False
        assert status.model_found is False
        assert "binary" in status.detail.lower()

    def test_unavailable_when_binary_missing(self, tmp_path):
        # Model exists but binary path is bogus.
        model = tmp_path / "voice.onnx"
        model.write_bytes(b"\x00")
        provider = voice_piper.PiperProvider(
            binary=str(tmp_path / "nope-piper"),
            model=str(model),
            config="",
        )
        assert provider.is_available() is False
        assert "binary" in provider.status().detail.lower()

    def test_unavailable_when_model_missing(self, tmp_path):
        binary, _model, _config = _make_piper_files(tmp_path, with_config=False)
        provider = voice_piper.PiperProvider(
            binary=binary,
            model=str(tmp_path / "missing.onnx"),
            config="",
        )
        assert provider.is_available() is False
        assert "model" in provider.status().detail.lower()

    def test_available_when_both_present(self, tmp_path):
        binary, model, config = _make_piper_files(tmp_path)
        provider = voice_piper.PiperProvider(
            binary=binary, model=model, config=config,
        )
        assert provider.is_available() is True
        status = provider.status()
        assert status.available is True
        assert status.binary_found is True
        assert status.model_found is True
        assert status.config_found is True

    def test_auto_discovers_sibling_config(self, tmp_path):
        # When the user only sets the model path, Piper's CLI auto-finds
        # the sibling .onnx.json — we should report config_found=True so
        # the UI doesn't surface a misleading hint.
        binary, model, _config = _make_piper_files(tmp_path, with_config=True)
        provider = voice_piper.PiperProvider(
            binary=binary, model=model, config="",
        )
        assert provider.is_available() is True
        assert provider.status().config_found is True

    def test_engine_name_is_piper(self, tmp_path):
        binary, model, config = _make_piper_files(tmp_path)
        provider = voice_piper.PiperProvider(
            binary=binary, model=model, config=config,
        )
        assert provider.name == voice_providers.ENGINE_PIPER
        assert provider.voice_config().engine == voice_providers.ENGINE_PIPER

    def test_voice_config_lists_recommended_voices(self, tmp_path):
        binary, model, config = _make_piper_files(tmp_path)
        provider = voice_piper.PiperProvider(
            binary=binary, model=model, config=config,
        )
        cfg = provider.voice_config()
        assert isinstance(cfg.preferred_voice_names, tuple)
        # The curated list should not be empty so docs/UI hints have
        # something to surface.
        assert cfg.preferred_voice_names

    def test_synthesize_raises_when_unavailable(self):
        provider = voice_piper.PiperProvider(binary="", model="", config="")
        with pytest.raises(voice_piper.PiperError):
            provider.synthesize("hello")


class TestPiperSynthesisSubprocess:
    """End-to-end synthesis with a stubbed subprocess.

    These tests verify that the provider builds a safe argv (text on
    stdin, never in argv), reads back the file Piper writes, and turns
    every failure mode into ``PiperError`` rather than leaking
    exceptions to the FastAPI handler.
    """

    def test_synthesize_returns_audio_bytes(self, tmp_path):
        binary, model, config = _make_piper_files(tmp_path)
        provider = voice_piper.PiperProvider(
            binary=binary, model=model, config=config,
        )

        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = list(argv)
            captured["kwargs"] = kwargs
            # Piper always writes via --output_file; mimic that.
            out_index = argv.index("--output_file") + 1
            with open(argv[out_index], "wb") as f:
                f.write(b"RIFF....fake-wav-bytes")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with patch.object(subprocess, "run", side_effect=fake_run):
            audio = provider.synthesize("hello world")

        assert audio == b"RIFF....fake-wav-bytes"
        # Argv must contain the model and config but never the user text.
        assert captured["argv"][0] == binary
        assert "--model" in captured["argv"]
        assert model in captured["argv"]
        assert "--config" in captured["argv"]
        assert config in captured["argv"]
        assert "hello world" not in captured["argv"]
        # Text reaches piper via stdin, not argv.
        assert captured["kwargs"].get("input") == "hello world"

    def test_synthesize_raises_on_nonzero_exit(self, tmp_path):
        binary, model, config = _make_piper_files(tmp_path)
        provider = voice_piper.PiperProvider(
            binary=binary, model=model, config=config,
        )

        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="bad model")

        with patch.object(subprocess, "run", side_effect=fake_run):
            with pytest.raises(voice_piper.PiperError):
                provider.synthesize("hello")

    def test_synthesize_raises_on_timeout(self, tmp_path):
        binary, model, config = _make_piper_files(tmp_path)
        provider = voice_piper.PiperProvider(
            binary=binary, model=model, config=config, timeout_seconds=1.0,
        )

        def fake_run(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=1.0)

        with patch.object(subprocess, "run", side_effect=fake_run):
            with pytest.raises(voice_piper.PiperError):
                provider.synthesize("hello")

    def test_synthesize_raises_when_file_missing(self, tmp_path):
        binary, model, config = _make_piper_files(tmp_path)
        provider = voice_piper.PiperProvider(
            binary=binary, model=model, config=config,
        )

        def fake_run(argv, **kwargs):
            # Process "succeeded" but never actually wrote the file —
            # this is the indirect "model corrupt" failure mode.
            out_index = argv.index("--output_file") + 1
            try:
                os.unlink(argv[out_index])
            except OSError:
                pass
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with patch.object(subprocess, "run", side_effect=fake_run):
            with pytest.raises(voice_piper.PiperError):
                provider.synthesize("hello")

    def test_synthesize_cleans_up_tempfile(self, tmp_path):
        binary, model, config = _make_piper_files(tmp_path)
        provider = voice_piper.PiperProvider(
            binary=binary, model=model, config=config,
        )

        seen_path = {}

        def fake_run(argv, **kwargs):
            out_index = argv.index("--output_file") + 1
            seen_path["path"] = argv[out_index]
            with open(argv[out_index], "wb") as f:
                f.write(b"data")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with patch.object(subprocess, "run", side_effect=fake_run):
            provider.synthesize("hello")

        # The tempfile must be gone whether synthesis succeeded or not.
        assert seen_path["path"]
        assert not os.path.exists(seen_path["path"])


# ── Provider selection helpers ──────────────────────────────────────


class TestEngineSelection:
    def test_default_provider_is_browser(self):
        # The server default never silently switches engines; even on a
        # host with Piper present, browser remains the safe default.
        assert isinstance(voice.get_default_provider(), voice_providers.BrowserProvider)

    def test_list_engines_browser_only_by_default(self, monkeypatch):
        monkeypatch.setattr("config.NOVA_PIPER_BINARY", "")
        monkeypatch.setattr("config.NOVA_PIPER_VOICE_MODEL", "")
        engines = voice.list_available_engines()
        assert engines == [voice_providers.ENGINE_BROWSER]

    def test_list_engines_includes_piper_when_configured(self, tmp_path, monkeypatch):
        binary, model, config = _make_piper_files(tmp_path)
        monkeypatch.setattr("config.NOVA_PIPER_BINARY", binary)
        monkeypatch.setattr("config.NOVA_PIPER_VOICE_MODEL", model)
        monkeypatch.setattr("config.NOVA_PIPER_VOICE_CONFIG", config)
        engines = voice.list_available_engines()
        assert voice_providers.ENGINE_BROWSER in engines
        assert voice_providers.ENGINE_PIPER in engines

    def test_get_provider_returns_none_for_unknown(self):
        assert voice.get_provider("not-a-real-engine") is None

    def test_get_provider_returns_browser(self):
        provider = voice.get_provider(voice_providers.ENGINE_BROWSER)
        assert isinstance(provider, voice_providers.BrowserProvider)

    def test_get_provider_returns_none_when_piper_misconfigured(self, monkeypatch):
        monkeypatch.setattr("config.NOVA_PIPER_BINARY", "")
        monkeypatch.setattr("config.NOVA_PIPER_VOICE_MODEL", "")
        # Asked for piper, but nothing is configured — None signals
        # "fall back to the browser engine" to the caller.
        assert voice.get_provider(voice_providers.ENGINE_PIPER) is None


# ── HTTP: /voice/config with Piper ──────────────────────────────────


class TestVoiceConfigEndpointWithPiper:
    def test_browser_only_payload_lists_browser_engine(self, db_path, web_client):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")

        body = web_client.get("/voice/config", headers=_h(token)).json()
        assert body["engine"] == voice_providers.ENGINE_BROWSER
        assert body["available_engines"] == [voice_providers.ENGINE_BROWSER]
        # No piper block when nothing is configured — the UI hides the
        # row entirely in this case.
        assert "piper" not in body or body["piper"]["available"] is False

    def test_payload_includes_piper_when_configured(
        self, tmp_path, db_path, web_client, monkeypatch,
    ):
        binary, model, config = _make_piper_files(tmp_path)
        monkeypatch.setattr("config.NOVA_PIPER_BINARY", binary)
        monkeypatch.setattr("config.NOVA_PIPER_VOICE_MODEL", model)
        monkeypatch.setattr("config.NOVA_PIPER_VOICE_CONFIG", config)

        _make_user(db_path, "alice")
        token = _login(web_client, "alice")

        body = web_client.get("/voice/config", headers=_h(token)).json()
        # Server default stays browser — Piper is *additionally* offered.
        assert body["engine"] == voice_providers.ENGINE_BROWSER
        assert voice_providers.ENGINE_PIPER in body["available_engines"]
        assert body["piper"]["available"] is True


# ── HTTP: /voice/synthesize with Piper ──────────────────────────────


class TestVoiceSynthesizeWithPiper:
    def test_unknown_engine_rejected(self, db_path, web_client):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        resp = web_client.post(
            "/voice/synthesize",
            json={"text": "hi", "engine": "weird"},
            headers=_h(token),
        )
        assert resp.status_code == 422

    def test_piper_returns_audio_bytes(
        self, tmp_path, db_path, web_client, monkeypatch,
    ):
        binary, model, config = _make_piper_files(tmp_path)
        monkeypatch.setattr("config.NOVA_PIPER_BINARY", binary)
        monkeypatch.setattr("config.NOVA_PIPER_VOICE_MODEL", model)
        monkeypatch.setattr("config.NOVA_PIPER_VOICE_CONFIG", config)

        def fake_run(argv, **kwargs):
            out_index = argv.index("--output_file") + 1
            with open(argv[out_index], "wb") as f:
                f.write(b"RIFFwav-bytes")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        _make_user(db_path, "alice")
        token = _login(web_client, "alice")

        with patch.object(subprocess, "run", side_effect=fake_run):
            resp = web_client.post(
                "/voice/synthesize",
                json={"text": "hello", "engine": "piper"},
                headers=_h(token),
            )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("audio/")
        assert resp.headers.get("x-voice-engine") == voice_providers.ENGINE_PIPER
        assert resp.content == b"RIFFwav-bytes"

    def test_piper_falls_back_to_browser_when_unconfigured(self, db_path, web_client):
        # Asking for piper on a host where it isn't configured must not
        # 5xx — we want a graceful fallback the client can present.
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        resp = web_client.post(
            "/voice/synthesize",
            json={"text": "hello", "engine": "piper"},
            headers=_h(token),
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        body = resp.json()
        assert body["fallback"] is True
        assert body["engine"] == voice_providers.ENGINE_BROWSER
        assert "fallback_reason" in body

    def test_piper_falls_back_when_subprocess_fails(
        self, tmp_path, db_path, web_client, monkeypatch,
    ):
        binary, model, config = _make_piper_files(tmp_path)
        monkeypatch.setattr("config.NOVA_PIPER_BINARY", binary)
        monkeypatch.setattr("config.NOVA_PIPER_VOICE_MODEL", model)
        monkeypatch.setattr("config.NOVA_PIPER_VOICE_CONFIG", config)

        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="bad")

        _make_user(db_path, "alice")
        token = _login(web_client, "alice")

        with patch.object(subprocess, "run", side_effect=fake_run):
            resp = web_client.post(
                "/voice/synthesize",
                json={"text": "hello", "engine": "piper"},
                headers=_h(token),
            )
        # Subprocess failure must surface as a JSON fallback, not 5xx —
        # the user still gets read-aloud through the browser engine.
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        body = resp.json()
        assert body["fallback"] is True
        assert body["engine"] == voice_providers.ENGINE_BROWSER

    def test_browser_engine_request_still_returns_json(self, db_path, web_client):
        # Explicit engine=browser keeps the original JSON envelope so
        # any future client that opts in by name still works.
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        resp = web_client.post(
            "/voice/synthesize",
            json={"text": "hello", "engine": "browser"},
            headers=_h(token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["text"] == "hello"
        assert body["engine"] == voice_providers.ENGINE_BROWSER

    def test_piper_engine_request_validation_runs_first(self, db_path, web_client):
        # The text validator must reject empty input regardless of which
        # engine the client asked for.
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        resp = web_client.post(
            "/voice/synthesize",
            json={"text": "   ", "engine": "piper"},
            headers=_h(token),
        )
        assert resp.status_code == 400


# ── Voice config payload contract ───────────────────────────────────


class TestVoiceConfigPayloadContract:
    """Pins the keys the Settings UI relies on.

    The Settings panel reads ``available_engines`` to decide whether to
    show the engine selector and the active-engine chip, and
    ``piper.detail`` to surface a calm hint when Piper is misconfigured.
    Renaming or dropping either field would silently break the UX even
    if the rest of the read-aloud flow still works, so we lock the
    shape here.
    """

    def test_browser_only_payload_carries_available_engines(self, db_path, web_client):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        body = web_client.get("/voice/config", headers=_h(token)).json()
        assert "available_engines" in body
        assert isinstance(body["available_engines"], list)
        assert voice_providers.ENGINE_BROWSER in body["available_engines"]

    def test_piper_block_exposes_detail_for_ui_hint(
        self, tmp_path, db_path, web_client, monkeypatch,
    ):
        # Half-configured Piper (binary present, model missing) is the
        # case where the UI most wants a hint. The payload must include
        # a ``piper`` block with ``available=False`` and a human-readable
        # ``detail`` string.
        binary, _model, _config = _make_piper_files(tmp_path, with_config=False)
        monkeypatch.setattr("config.NOVA_PIPER_BINARY", binary)
        monkeypatch.setattr("config.NOVA_PIPER_VOICE_MODEL", "")
        monkeypatch.setattr("config.NOVA_PIPER_VOICE_CONFIG", "")

        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        body = web_client.get("/voice/config", headers=_h(token)).json()

        piper = body.get("piper")
        assert isinstance(piper, dict)
        assert piper["available"] is False
        assert isinstance(piper["detail"], str)
        assert piper["detail"].strip(), "detail string must not be empty"

    def test_payload_never_leaks_filesystem_paths(
        self, tmp_path, db_path, web_client, monkeypatch,
    ):
        # The Piper provider holds absolute paths internally. Those
        # paths must never appear in the public payload — a static
        # check today, but the kind of regression worth pinning.
        binary, model, config = _make_piper_files(tmp_path)
        monkeypatch.setattr("config.NOVA_PIPER_BINARY", binary)
        monkeypatch.setattr("config.NOVA_PIPER_VOICE_MODEL", model)
        monkeypatch.setattr("config.NOVA_PIPER_VOICE_CONFIG", config)

        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        body = web_client.get("/voice/config", headers=_h(token)).json()
        raw = repr(body)
        assert binary not in raw
        assert model not in raw
        assert config not in raw
