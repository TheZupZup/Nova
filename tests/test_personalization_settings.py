"""
Tests for per-user personalization settings.

Personalization (response style, warmth, enthusiasm, emoji level, custom
instructions) lives on top of the same per-user settings infrastructure
introduced in #107: each preference is a row in `user_settings`, scoped
to the caller, validated server-side, and surfaced through the existing
GET /settings and POST /settings endpoints.

Covers:
  * data-layer round-trip and per-user isolation
  * enum validation and custom-instructions length cap
  * /settings GET returns sensible defaults when nothing is saved
  * /settings POST persists the values for the calling user only
  * non-admin callers can write personalization but still cannot mutate
    system-scoped keys (ram_budget) through the same endpoint
"""

from __future__ import annotations

import contextlib
import sqlite3
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core import memory as core_memory, settings as core_settings, users
from memory import store as natural_store


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "nova.db")
    monkeypatch.setattr(core_memory, "DB_PATH", path)
    monkeypatch.setattr(natural_store, "DB_PATH", path)
    core_memory.initialize_db()
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
    resp = client.post(
        "/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _h(token):
    return {"Authorization": f"Bearer {token}"}


# Convenient shorthand used across the HTTP tests.
ALL_FIELDS = {
    "response_style": "concise",
    "warmth_level": "high",
    "enthusiasm_level": "low",
    "emoji_level": "medium",
    "custom_instructions": "Prefer short answers.",
}


# ── Module surface ──────────────────────────────────────────────────────────

class TestPersonalizationConstants:
    def test_keys_are_user_scoped(self):
        # Each personalization key must round-trip through the per-user
        # settings table, never the global settings table.
        for key in core_settings.PERSONALIZATION_KEYS:
            assert core_settings.is_user_setting(key), key

    def test_enums_match_spec(self):
        assert core_settings.PERSONALIZATION_ENUMS["response_style"] == frozenset(
            {"default", "concise", "detailed", "technical"}
        )
        assert core_settings.PERSONALIZATION_ENUMS["warmth_level"] == frozenset(
            {"low", "normal", "high"}
        )
        assert core_settings.PERSONALIZATION_ENUMS["enthusiasm_level"] == frozenset(
            {"low", "normal", "high"}
        )
        assert core_settings.PERSONALIZATION_ENUMS["emoji_level"] == frozenset(
            {"none", "low", "medium"}
        )

    def test_defaults_cover_every_field(self):
        expected = {
            "response_style", "warmth_level", "enthusiasm_level",
            "emoji_level", "custom_instructions",
        }
        assert set(core_settings.PERSONALIZATION_DEFAULTS) == expected


class TestValidatePersonalizationValue:
    def test_accepts_allowed_enum_values(self):
        for key, allowed in core_settings.PERSONALIZATION_ENUMS.items():
            for v in allowed:
                assert core_settings.validate_personalization_value(key, v) == v

    def test_rejects_unknown_enum_value(self):
        with pytest.raises(ValueError):
            core_settings.validate_personalization_value(
                "response_style", "verbose"
            )

    def test_rejects_unknown_key(self):
        with pytest.raises(ValueError):
            core_settings.validate_personalization_value("not_a_key", "default")

    def test_custom_instructions_trimmed(self):
        out = core_settings.validate_personalization_value(
            "custom_instructions", "  hello  "
        )
        assert out == "hello"

    def test_custom_instructions_at_limit_accepted(self):
        s = "x" * core_settings.CUSTOM_INSTRUCTIONS_MAX_LEN
        assert core_settings.validate_personalization_value(
            "custom_instructions", s
        ) == s

    def test_custom_instructions_over_limit_rejected(self):
        s = "x" * (core_settings.CUSTOM_INSTRUCTIONS_MAX_LEN + 1)
        with pytest.raises(ValueError):
            core_settings.validate_personalization_value(
                "custom_instructions", s
            )


# ── Data-layer scoping ──────────────────────────────────────────────────────

class TestPersonalizationStorage:
    def test_get_personalization_returns_defaults_for_new_user(self, db_path):
        a = _make_user(db_path, "alice")
        prefs = core_settings.get_personalization(a)
        assert prefs == core_settings.PERSONALIZATION_DEFAULTS

    def test_save_then_get_round_trip(self, db_path):
        a = _make_user(db_path, "alice")
        for key, value in ALL_FIELDS.items():
            core_settings.save_user_setting(a, key, value)
        assert core_settings.get_personalization(a) == ALL_FIELDS

    def test_user_a_settings_do_not_affect_user_b(self, db_path):
        a = _make_user(db_path, "alice")
        b = _make_user(db_path, "bob")
        for key, value in ALL_FIELDS.items():
            core_settings.save_user_setting(a, key, value)
        # Bob has saved nothing — he should still see the defaults.
        assert core_settings.get_personalization(b) == (
            core_settings.PERSONALIZATION_DEFAULTS
        )
        # Alice's payload is unaffected by Bob existing.
        assert core_settings.get_personalization(a) == ALL_FIELDS


# ── HTTP endpoints ──────────────────────────────────────────────────────────

class TestSettingsGet:
    def test_defaults_returned_for_fresh_user(self, db_path, web_client):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        body = web_client.get("/settings", headers=_h(token)).json()
        for key, default in core_settings.PERSONALIZATION_DEFAULTS.items():
            assert body[key] == default

    def test_returns_saved_values(self, db_path, web_client):
        a = _make_user(db_path, "alice")
        for key, value in ALL_FIELDS.items():
            core_settings.save_user_setting(a, key, value)
        token = _login(web_client, "alice")
        body = web_client.get("/settings", headers=_h(token)).json()
        for key, value in ALL_FIELDS.items():
            assert body[key] == value


class TestSettingsPost:
    def test_user_can_save_personalization_settings(self, db_path, web_client):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        resp = web_client.post(
            "/settings", json=ALL_FIELDS, headers=_h(token)
        )
        assert resp.status_code == 200, resp.text

        body = web_client.get("/settings", headers=_h(token)).json()
        for key, value in ALL_FIELDS.items():
            assert body[key] == value

    def test_settings_survive_reload(self, db_path, web_client):
        """A second login pulls the same payload, simulating a page reload."""
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        web_client.post("/settings", json=ALL_FIELDS, headers=_h(token))

        # Fresh login → fresh token, but the persisted prefs are the same.
        token2 = _login(web_client, "alice")
        body = web_client.get("/settings", headers=_h(token2)).json()
        for key, value in ALL_FIELDS.items():
            assert body[key] == value

    def test_user_a_post_does_not_affect_user_b(self, db_path, web_client):
        a = _make_user(db_path, "alice")
        b = _make_user(db_path, "bob")
        a_token = _login(web_client, "alice")
        b_token = _login(web_client, "bob")

        resp = web_client.post(
            "/settings", json=ALL_FIELDS, headers=_h(a_token)
        )
        assert resp.status_code == 200

        a_body = web_client.get("/settings", headers=_h(a_token)).json()
        b_body = web_client.get("/settings", headers=_h(b_token)).json()
        for key, value in ALL_FIELDS.items():
            assert a_body[key] == value
            assert b_body[key] == core_settings.PERSONALIZATION_DEFAULTS[key]

        # The DB row exists only for Alice.
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT user_id, key FROM user_settings "
                "WHERE key IN (?, ?, ?, ?, ?)",
                tuple(ALL_FIELDS.keys()),
            ).fetchall()
        assert all(r[0] == a for r in rows)
        assert {r[1] for r in rows} == set(ALL_FIELDS.keys())
        assert all(r[0] != b for r in rows)

    def test_partial_update_only_touches_supplied_fields(self, db_path, web_client):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        web_client.post("/settings", json=ALL_FIELDS, headers=_h(token))

        # Only emoji_level changes; everything else is left alone.
        resp = web_client.post(
            "/settings", json={"emoji_level": "none"}, headers=_h(token),
        )
        assert resp.status_code == 200

        body = web_client.get("/settings", headers=_h(token)).json()
        assert body["emoji_level"] == "none"
        assert body["response_style"] == ALL_FIELDS["response_style"]
        assert body["warmth_level"] == ALL_FIELDS["warmth_level"]
        assert body["enthusiasm_level"] == ALL_FIELDS["enthusiasm_level"]
        assert body["custom_instructions"] == ALL_FIELDS["custom_instructions"]

    @pytest.mark.parametrize("key,value", [
        ("response_style", "verbose"),
        ("warmth_level", "extreme"),
        ("enthusiasm_level", ""),
        ("emoji_level", "all"),
    ])
    def test_invalid_enum_value_is_rejected(self, db_path, web_client, key, value):
        a = _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        resp = web_client.post(
            "/settings", json={key: value}, headers=_h(token)
        )
        assert resp.status_code == 422
        # Nothing was written for the rejected field.
        assert core_settings.get_user_setting(a, key, "MISSING") == "MISSING"

    def test_custom_instructions_too_long_is_rejected(self, db_path, web_client):
        a = _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        too_long = "x" * (core_settings.CUSTOM_INSTRUCTIONS_MAX_LEN + 1)
        resp = web_client.post(
            "/settings",
            json={"custom_instructions": too_long},
            headers=_h(token),
        )
        assert resp.status_code == 422
        assert core_settings.get_user_setting(
            a, "custom_instructions", "MISSING"
        ) == "MISSING"

    def test_custom_instructions_at_limit_is_accepted(self, db_path, web_client):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        at_limit = "x" * core_settings.CUSTOM_INSTRUCTIONS_MAX_LEN
        resp = web_client.post(
            "/settings",
            json={"custom_instructions": at_limit},
            headers=_h(token),
        )
        assert resp.status_code == 200
        body = web_client.get("/settings", headers=_h(token)).json()
        assert body["custom_instructions"] == at_limit

    def test_custom_instructions_is_trimmed_server_side(self, db_path, web_client):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        web_client.post(
            "/settings",
            json={"custom_instructions": "  trim me  "},
            headers=_h(token),
        )
        body = web_client.get("/settings", headers=_h(token)).json()
        assert body["custom_instructions"] == "trim me"


class TestSettingsAuthorization:
    def test_non_admin_cannot_change_system_settings_via_personalization_payload(
        self, db_path, web_client,
    ):
        """
        A non-admin posting personalization plus a system key must be
        refused on the system key, and the personalization must NOT be
        partially applied. This is the existing #107 behaviour: the
        system-key check fires first and aborts the whole request.
        """
        a = _make_user(db_path, "alice", role=users.ROLE_USER)
        token = _login(web_client, "alice")
        resp = web_client.post(
            "/settings",
            json={"ram_budget": 4096, "response_style": "technical"},
            headers=_h(token),
        )
        assert resp.status_code == 403
        # System setting unchanged.
        assert core_settings.get_system_setting("ram_budget", "2048") == "2048"
        # Personalization left untouched (no half-applied write).
        assert core_settings.get_user_setting(
            a, "response_style", "MISSING"
        ) == "MISSING"

    def test_restricted_user_can_save_personalization(self, db_path, web_client):
        """
        Restricted accounts retain access to harmless personalization
        settings. The /settings endpoint never gates user-scoped writes
        on role or restriction, so a restricted user's POST goes through.
        """
        a = _make_user(
            db_path, "kid", role=users.ROLE_USER, is_restricted=True,
        )
        token = _login(web_client, "kid")
        resp = web_client.post(
            "/settings", json=ALL_FIELDS, headers=_h(token),
        )
        assert resp.status_code == 200
        for key, value in ALL_FIELDS.items():
            assert core_settings.get_user_setting(a, key) == value

    def test_unknown_field_is_rejected(self, db_path, web_client):
        """`extra="forbid"` covers the personalization endpoint too."""
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        resp = web_client.post(
            "/settings",
            json={"telepathy_level": "max"},
            headers=_h(token),
        )
        assert resp.status_code == 422

    def test_settings_get_does_not_expose_raw_model_names(
        self, db_path, web_client,
    ):
        """
        The personalization payload must not leak the host's MODEL_MAP
        or any raw model identifier. Only the user's own nova_model_name
        (already #107) is allowed through, and personalization is purely
        labels — no model strings.
        """
        a = _make_user(db_path, "alice")
        for key, value in ALL_FIELDS.items():
            core_settings.save_user_setting(a, key, value)
        token = _login(web_client, "alice")
        body = web_client.get("/settings", headers=_h(token)).json()

        from config import MODELS
        raw_names = {v for v in MODELS.values()}
        for key in (
            "response_style", "warmth_level", "enthusiasm_level",
            "emoji_level", "custom_instructions",
        ):
            assert body[key] not in raw_names
