"""
Guardrails for removed product surfaces.

Nova used to ship a SilentGuard security integration, a voice / TTS
(read-aloud + Piper) feature, and companion / calm-support /
emotional-support modes. All of them were removed to re-center Nova as
a neutral, adaptive, local-first AI assistant. This file pins the
removal so a future change cannot quietly re-advertise them, and it
pins the migration story: an existing database that still carries the
old per-user settings must keep working with the rows simply ignored.

Covered here:

  * active docs / UI never advertise SilentGuard, voice/TTS, or a
    companion / calm-support mode;
  * the old HTTP endpoints are gone (404), and the settings API no
    longer accepts or returns the removed keys;
  * deprecated per-user settings rows (``silentguard_enabled``,
    ``companion_mode_enabled``, ``tone_profile``) in an existing DB
    never crash the app — /settings, personalization reads, and the
    system-prompt build all keep working;
  * stale ``NOVA_SILENTGUARD_*`` / ``NOVA_PIPER_*`` env vars are
    harmless: config imports cleanly with them set.
"""

from __future__ import annotations

import contextlib
import importlib
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

for _mod in ("ddgs", "ollama", "sgmllib", "feedparser"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from core import memory as core_memory, settings as core_settings, users  # noqa: E402
from core.settings import DEPRECATED_USER_SETTING_KEYS, USER_SETTING_KEYS  # noqa: E402
from memory import store as natural_store  # noqa: E402


REPO = Path(__file__).resolve().parents[1]

# The docs a user actually reads today. Historical records (CHANGELOG)
# and roadmap post-mortems may mention the removed features by name;
# the active product docs must not advertise them.
ACTIVE_DOCS = (
    "README.md",
    "INSTALL.md",
    ".env.example",
    "docs/docker.md",
    "docs/docker-desktop.md",
    "docs/model-providers.md",
    "docs/local-gguf.md",
    "docs/secure-deployment.md",
    "docs/projects.md",
    "docs/dev-workspace.md",
    "docs/nova-safety-and-trust-contract.md",
    "deploy/systemd/README.md",
)


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower())


# ── Docs and UI no longer advertise the removed features ────────────────────


class TestDocsDoNotAdvertiseRemovedFeatures:
    @pytest.mark.parametrize("rel", ACTIVE_DOCS)
    def test_no_silentguard_feature_docs(self, rel):
        low = _norm(_read(rel))
        # A deprecation notice ("has been removed … ignored") is fine;
        # anything that reads as an available feature is not.
        for phrase in (
            "enable silentguard",
            "silentguard integration roadmap",
            "silentguard read-only api",
            "silentguard-background-service",
            "nova_silentguard_api_url=",
        ):
            assert phrase not in low, f"{rel} advertises SilentGuard: {phrase!r}"

    @pytest.mark.parametrize("rel", ACTIVE_DOCS)
    def test_no_voice_feature_docs(self, rel):
        low = _norm(_read(rel))
        for phrase in (
            "read aloud", "read-aloud", "speechsynthesis",
            "voice assistant", "voice model", "piper tts",
            "nova_piper_binary=",
        ):
            assert phrase not in low, f"{rel} advertises voice/TTS: {phrase!r}"

    @pytest.mark.parametrize("rel", ACTIVE_DOCS)
    def test_no_companion_mode_docs(self, rel):
        low = _norm(_read(rel))
        # Disclaimers are allowed ("there is no companion mode"); an
        # advertisement is not. Strip the negated forms before checking.
        low = low.replace("no companion mode", "")
        low = low.replace("no emotional-support mode", "")
        for phrase in (
            "companion mode", "calm support mode", "calm-support",
            "emotional support layer", "supportive tone",
            "relationship situation coach", "tone profile",
        ):
            assert phrase not in low, f"{rel} advertises a support mode: {phrase!r}"

    @pytest.mark.parametrize("rel", ACTIVE_DOCS)
    def test_no_romantic_framing(self, rel):
        low = _norm(_read(rel))
        # "girlfriend"-family words may only appear in an explicit
        # negation / disclaimer; a doc that uses them positively fails
        # the framing check below.
        for phrase in (
            "your ai girlfriend", "ai boyfriend", "your companion",
            "emotional partner app", "misses you", "loves you back",
        ):
            assert phrase not in low, f"{rel} uses romantic framing: {phrase!r}"

    def test_feature_doc_files_are_gone(self):
        for rel in (
            "docs/silentguard-background-service.md",
            "docs/silentguard-integration-roadmap.md",
            "docs/companion-mode.md",
            "docs/emotional-support.md",
            "docs/relationship-situation-coach.md",
            "docs/tone-profile.md",
            "deploy/systemd/silentguard-api.service",
        ):
            assert not (REPO / rel).exists(), f"{rel} should be deleted"


class TestUiDoesNotAdvertiseRemovedFeatures:
    def _html(self) -> str:
        return _read("static/index.html")

    def test_no_silentguard_ui(self):
        low = self._html().lower()
        assert "silentguard" not in low

    def test_no_voice_ui(self):
        low = self._html().lower()
        for marker in (
            "speechsynthesis", "voice-select", "voice/config",
            "voice/synthesize", "read aloud", "piper",
            "settings-pane-voice",
        ):
            assert marker not in low, marker

    def test_no_companion_ui(self):
        low = self._html().lower()
        for marker in (
            "companion", "calm support", "soutien calme",
            "tone_profile", "tone-profile",
        ):
            assert marker not in low, marker


# ── Backend surface: modules and endpoints are gone ─────────────────────────


class TestBackendSurfaceRemoved:
    def test_removed_modules_do_not_import(self):
        for mod in (
            "core.security", "core.security_feed",
            "core.integrations.silentguard", "core.voice",
            "core.companion", "core.emotional_support",
            "core.relationship_coach", "core.tone_profile",
        ):
            with pytest.raises(ModuleNotFoundError):
                importlib.import_module(mod)

    def test_config_has_no_removed_switches(self):
        import config
        for name in dir(config):
            assert not name.startswith("NOVA_SILENTGUARD"), name
            assert not name.startswith("NOVA_PIPER"), name
        assert "silentguard_enabled" not in config.ALLOWED_SETTINGS

    def test_user_setting_keys_dropped(self):
        for key in ("silentguard_enabled", "companion_mode_enabled",
                    "tone_profile"):
            assert key not in USER_SETTING_KEYS
            assert key in DEPRECATED_USER_SETTING_KEYS

    def test_stale_env_vars_are_ignored(self, monkeypatch):
        # An old .env may still export the removed switches; importing
        # config with them set must not raise and must not resurrect
        # any NOVA_SILENTGUARD_* / NOVA_PIPER_* attribute.
        monkeypatch.setenv("NOVA_SILENTGUARD_ENABLED", "true")
        monkeypatch.setenv("NOVA_SILENTGUARD_API_URL", "http://127.0.0.1:1")
        monkeypatch.setenv("NOVA_PIPER_BINARY", "/usr/bin/piper")
        import config
        importlib.reload(config)
        assert not hasattr(config, "NOVA_SILENTGUARD_ENABLED")
        assert not hasattr(config, "NOVA_PIPER_BINARY")
        # Restore module state for other tests.
        monkeypatch.undo()
        importlib.reload(config)


# ── Migration safety: old DB rows are ignored, never a crash ────────────────


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "nova.db")
    monkeypatch.setattr(core_memory, "DB_PATH", path)
    monkeypatch.setattr(natural_store, "DB_PATH", path)
    core_memory.initialize_db()
    return path


@pytest.fixture
def legacy_user(db_path):
    """A user whose settings rows still carry the removed keys."""
    with sqlite3.connect(db_path) as conn:
        uid = users.create_user(conn, "legacy", "pw", role=users.ROLE_ADMIN)
        for key, value in (
            ("silentguard_enabled", "true"),
            ("companion_mode_enabled", "true"),
            ("tone_profile", "deep_comfort"),
            ("warmth_level", "high"),
        ):
            conn.execute(
                "INSERT INTO user_settings (user_id, key, value) "
                "VALUES (?, ?, ?)",
                (uid, key, value),
            )
    return uid


@pytest.fixture
def web_client(db_path, monkeypatch):
    from core.rate_limiter import _login_limiter
    _login_limiter._store.clear()

    import web
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("web.initialize_db"))
        stack.enter_context(patch("web.learn_from_feeds"))
        stack.enter_context(patch("web.scheduler", MagicMock()))
        with TestClient(web.app, raise_server_exceptions=True) as client:
            yield client


def _login(client, username="legacy", password="pw"):
    resp = client.post("/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


class TestDeprecatedSettingsAreIgnoredSafely:
    def test_settings_endpoint_works_and_omits_removed_keys(
        self, legacy_user, web_client
    ):
        token = _login(web_client)
        resp = web_client.get(
            "/settings", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        body = resp.json()
        for key in ("silentguard_enabled", "companion_mode_enabled",
                    "tone_profile"):
            assert key not in body
        # Kept personalization still round-trips.
        assert body["warmth_level"] == "high"

    def test_settings_update_still_works(self, legacy_user, web_client):
        token = _login(web_client)
        resp = web_client.post(
            "/settings",
            json={"warmth_level": "low"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = web_client.get(
            "/settings", headers={"Authorization": f"Bearer {token}"}
        ).json()
        assert body["warmth_level"] == "low"

    def test_personalization_read_ignores_stale_tone_profile(self, legacy_user):
        prefs = core_settings.get_personalization(legacy_user)
        assert "tone_profile" not in prefs
        assert prefs["warmth_level"] == "high"

    def test_system_prompt_builds_with_stale_rows(self, legacy_user):
        # The chat prompt path must not read (or crash on) the stale rows.
        from core.chat import build_messages
        prefs = core_settings.get_personalization(legacy_user)
        messages = build_messages([], "bonjour", [], personalization=prefs)
        assert messages[0]["role"] == "system"
        content = messages[0]["content"].lower()
        assert "nova" in content
        for banned in ("silentguard", "compagnon", "companion",
                       "deep_comfort", "soutien calme"):
            assert banned not in content

    def test_old_endpoints_are_gone(self, legacy_user, web_client):
        token = _login(web_client)
        headers = {"Authorization": f"Bearer {token}"}
        for method, path in (
            ("get", "/integrations/silentguard/summary"),
            ("get", "/integrations/silentguard/lifecycle"),
            ("post", "/integrations/silentguard/enable"),
            ("get", "/voice/config"),
            ("post", "/voice/synthesize"),
        ):
            resp = getattr(web_client, method)(path, headers=headers)
            assert resp.status_code in (404, 405), (method, path, resp.status_code)

    def test_integrations_status_has_no_silentguard(self, legacy_user, web_client):
        token = _login(web_client)
        resp = web_client.get(
            "/integrations/status", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        assert "silentguard" not in resp.json()


# ── No dead references linger in the runtime code ───────────────────────────


class TestNoDanglingReferences:
    def test_no_silentguard_references_in_python(self):
        # core/, memory/, web.py, main.py, config.py must not reference
        # the removed integration outside the explicit deprecation
        # notes in config.py / core/settings.py.
        out = subprocess.run(
            ["grep", "-ril", "--include=*.py", "silentguard",
             "core", "memory", "web.py", "main.py"],
            cwd=REPO, capture_output=True, text=True,
        )
        hits = [h for h in out.stdout.splitlines()
                if h not in ("core/settings.py",)]
        assert not hits, f"silentguard referenced in: {hits}"
