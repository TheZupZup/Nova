"""
Tests for the Tone Profile selector being removed from the Settings UI.

Nova is now warm, patient, and emotionally aware by default — the
baseline `RESPONSE_STYLE_BLOCK` already carries that warmth — so the
visible "Tone profile / Profil de ton" selector in Personalization is
unnecessary user-facing complexity and has been removed.

This file pins the *visible-UI* contract:

  * the `<select id="pers-tone-profile">` and its option rows are not
    rendered in `static/index.html`;
  * the explanatory paragraph (FR + EN) is gone from the i18n table
    and from the `_setText` re-render path;
  * the `tone_profile` key is no longer in the `PERSONALIZATION_FIELDS`
    JS map, so the load / save path never tries to read or write a
    selector that doesn't exist;
  * the remaining personalization controls (response style, warmth,
    enthusiasm, emoji, custom instructions, companion mode) are
    untouched;

The *backend* surface stays intact for backward compatibility:

  * `tone_profile` is still in `PERSONALIZATION_ENUMS`,
    `PERSONALIZATION_DEFAULTS`, and `USER_SETTING_KEYS`, so a
    previously-saved row continues to load cleanly;
  * `build_tone_profile_block` still returns the right block for any
    saved non-default value, so the chat-side wiring is unchanged;
  * `get_personalization` still returns `"default"` for a fresh user
    and the saved value for a returning user;
  * the warm baseline is still in every default-user system prompt,
    so a user with no settings still gets a kind, emotionally-aware
    Nova — no setting required.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# Heavy network deps the chat module imports at module load. Stub them
# before the import so a missing wheel never blocks this test file.
for _mod in ("ddgs", "ollama", "sgmllib", "feedparser"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from core import memory as core_memory, settings as core_settings, users  # noqa: E402
from core.chat import build_messages  # noqa: E402
from core.identity import IDENTITY_CONTRACT  # noqa: E402
from core.tone_profile import (  # noqa: E402
    TONE_DEEP_COMFORT_BLOCK,
    TONE_PROFILE_VALUES,
    TONE_WARM_COMPANION_BLOCK,
    build_tone_profile_block,
)
from memory import store as natural_store  # noqa: E402


INDEX_HTML = Path(__file__).resolve().parents[1] / "static" / "index.html"


@pytest.fixture(scope="module")
def html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def script(html: str) -> str:
    """Return the largest <script> block, where the app code lives."""
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.S)
    assert blocks, "expected at least one inline <script> in index.html"
    return max(blocks, key=len)


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "nova.db")
    monkeypatch.setattr(core_memory, "DB_PATH", path)
    monkeypatch.setattr(natural_store, "DB_PATH", path)
    core_memory.initialize_db()
    return path


def _make_user(db_path, username, password="pw"):
    with sqlite3.connect(db_path) as conn:
        return users.create_user(conn, username, password)


# ── Visible-UI contract: the selector is gone ───────────────────────────────


class TestToneProfileSelectorIsNotRendered:
    def test_no_tone_profile_select_element(self, html: str) -> None:
        # The dropdown the JS used to query by id must not be in the
        # static HTML at all — not commented out, not hidden, not present.
        assert 'id="pers-tone-profile"' not in html

    def test_no_tone_profile_title_span(self, html: str) -> None:
        # The row's title span is the anchor for the i18n update;
        # removing the select without removing the span would leave an
        # orphan "Tone profile" label visible to the user.
        assert 'id="pers-tone-title"' not in html

    def test_no_tone_profile_hint_span(self, html: str) -> None:
        assert 'id="pers-tone-hint"' not in html

    @pytest.mark.parametrize("opt_id", [
        "pers-tone-opt-default",
        "pers-tone-opt-professional",
        "pers-tone-opt-developer",
        "pers-tone-opt-warm-companion",
        "pers-tone-opt-calm-support",
        "pers-tone-opt-deep-comfort",
    ])
    def test_no_tone_profile_option_ids(self, html: str, opt_id: str) -> None:
        assert f'id="{opt_id}"' not in html

    def test_no_english_tone_profile_label(self, html: str) -> None:
        # The English settings-row title.
        assert "Tone profile" not in html

    def test_no_french_tone_profile_label(self, html: str) -> None:
        # The French settings-row title.
        assert "Profil de ton" not in html

    def test_no_savePersonalizationField_call_for_tone_profile(
        self, html: str,
    ) -> None:
        # The old onchange="savePersonalizationField('tone_profile', …)"
        # call would only be present if the <select> were too. Pin its
        # absence so a regression cannot quietly re-introduce the wiring.
        assert "savePersonalizationField('tone_profile'" not in html
        assert 'savePersonalizationField("tone_profile"' not in html


# ── i18n contract: translation keys are gone ────────────────────────────────


class TestToneProfileTranslationsAreRemoved:
    @pytest.mark.parametrize("key", [
        "pers_tone_title",
        "pers_tone_hint",
        "pers_tone_opt_default",
        "pers_tone_opt_professional",
        "pers_tone_opt_developer",
        "pers_tone_opt_warm_companion",
        "pers_tone_opt_calm_support",
        "pers_tone_opt_deep_comfort",
    ])
    def test_translation_key_not_in_table(self, html: str, key: str) -> None:
        # The FR and EN i18n tables previously carried these keys; the
        # _setText calls that read them have also been removed. If the
        # key reappears, either the dictionary or the render path has
        # regressed.
        assert f"{key}:" not in html
        assert f't("{key}")' not in html
        assert f"t('{key}')" not in html


# ── JS field-map contract: PERSONALIZATION_FIELDS no longer carries tone ──


class TestPersonalizationFieldsMapDoesNotLeakToneProfile:
    def test_tone_profile_key_not_in_personalization_fields_object(
        self, script: str,
    ) -> None:
        # Slice the PERSONALIZATION_FIELDS object literal and assert the
        # tone_profile entry is gone. A naive substring search would also
        # match the unrelated "tone_profile" string in /settings POST
        # bodies elsewhere; scoping to the object is the precise check.
        decl = re.search(
            r"const\s+PERSONALIZATION_FIELDS\s*=\s*{([^}]*)}",
            script,
        )
        assert decl, "PERSONALIZATION_FIELDS object literal not found"
        body = decl.group(1)
        assert "tone_profile" not in body
        assert "pers-tone-profile" not in body

    def test_remaining_personalization_fields_are_intact(
        self, script: str,
    ) -> None:
        # The other rows must keep their JS↔DOM wiring exactly as
        # before — removing tone_profile must not collateral-damage them.
        decl = re.search(
            r"const\s+PERSONALIZATION_FIELDS\s*=\s*{([^}]*)}",
            script,
        )
        assert decl, "PERSONALIZATION_FIELDS object literal not found"
        body = decl.group(1)
        for key, dom_id in [
            ("response_style", "pers-response-style"),
            ("warmth_level", "pers-warmth"),
            ("enthusiasm_level", "pers-enthusiasm"),
            ("emoji_level", "pers-emoji"),
            ("custom_instructions", "pers-custom-instructions"),
        ]:
            assert key in body, key
            assert dom_id in body, dom_id


# ── Default-style contract: warmth still lands without any setting ──────────


class TestWarmthBaselineStillInDefaultPrompt:
    def test_default_prompt_contains_baseline_warmth_directives(self):
        # No personalization, no companion mode, no tone profile — the
        # rendered system prompt must still tell Nova to sound warm and
        # emotionally aware. That is the whole point of removing the
        # selector: the baseline already does the right thing.
        msgs = build_messages([], "hi", [], None, None, None)
        sys_msg = msgs[0]["content"].lower()
        assert "chaleureuse" in sys_msg
        assert "patiente" in sys_msg
        # Anti-cold-and-robotic clause is part of the default warmth.
        assert "froide" in sys_msg or "robotique" in sys_msg
        # Light feeling-validation clause is part of the default warmth.
        assert "valide" in sys_msg

    def test_default_prompt_starts_with_identity_contract(self):
        # The warm baseline must never sit *above* the identity / safety
        # contract — ordering is what keeps the warmth subordinate to
        # safety rules. Pin it on the no-personalization path.
        msgs = build_messages([], "hi", [], None, None, None)
        assert msgs[0]["content"].startswith(IDENTITY_CONTRACT)

    def test_default_prompt_still_pins_no_human_no_partner_no_mother(self):
        # The "warmth is in wording, not in identity" rails must land in
        # every default-user prompt so a "you said you were warm…"
        # follow-up cannot weaken them.
        msgs = build_messages([], "hi", [], None, None, None)
        sys_msg = msgs[0]["content"].lower()
        assert "humain" in sys_msg
        assert "partenaire amoureuse" in sys_msg
        assert "mère" in sys_msg
        assert "thérapeute" in sys_msg

    def test_default_prompt_still_pins_no_dependency_no_isolation(self):
        msgs = build_messages([], "hi", [], None, None, None)
        sys_msg = msgs[0]["content"].lower()
        assert "ne crée jamais de dépendance" in sys_msg
        assert "n'encourage jamais l'isolement" in sys_msg

    def test_default_prompt_still_pins_honesty_over_warmth(self):
        # "Warmth never overrides truth" — the baseline must still tell
        # Nova to say risky / wrong / dangerous things plainly even
        # without any tone profile selected.
        msgs = build_messages([], "hi", [], None, None, None)
        sys_msg = msgs[0]["content"].lower()
        # The baseline carries the honesty clause as
        # "La chaleur ne remplace jamais l'honnêteté."
        assert (
            "ne remplace jamais l'honnêteté" in sys_msg
            or "honnêteté" in sys_msg
        )


# ── Backward-compat contract: old saved values still work ───────────────────


class TestStaleSavedToneProfileDoesNotCrash:
    def test_get_personalization_returns_default_for_fresh_user(
        self, db_path,
    ):
        # A fresh user has no row — `get_personalization` falls back to
        # `"default"` and the chat layer renders no tone block.
        alice = _make_user(db_path, "alice")
        prefs = core_settings.get_personalization(alice)
        assert prefs["tone_profile"] == "default"
        assert build_tone_profile_block(prefs["tone_profile"]) == ""

    def test_old_warm_companion_value_still_loads_cleanly(self, db_path):
        # A user who selected "Warm Companion" before the UI removal
        # keeps that row in `user_settings`. Reading it must not raise
        # and the block must still be applied to their chat — the
        # destructive migration the brief forbids is exactly what
        # would have stripped it.
        alice = _make_user(db_path, "alice")
        core_settings.save_user_setting(alice, "tone_profile", "warm_companion")
        prefs = core_settings.get_personalization(alice)
        assert prefs["tone_profile"] == "warm_companion"
        # The chat-side build path still resolves the block, so the
        # user keeps the wording they had previously chosen.
        assert build_tone_profile_block(prefs["tone_profile"]) == (
            TONE_WARM_COMPANION_BLOCK
        )

    def test_old_deep_comfort_value_still_loads_cleanly(self, db_path):
        alice = _make_user(db_path, "alice")
        core_settings.save_user_setting(alice, "tone_profile", "deep_comfort")
        prefs = core_settings.get_personalization(alice)
        assert prefs["tone_profile"] == "deep_comfort"
        assert build_tone_profile_block(prefs["tone_profile"]) == (
            TONE_DEEP_COMFORT_BLOCK
        )

    def test_every_known_value_round_trips_through_storage(self, db_path):
        # Defensive sweep: every value the UI used to expose must keep
        # round-tripping through `user_settings` so older clients (or
        # raw API calls) never trip the loader.
        alice = _make_user(db_path, "alice")
        for value in TONE_PROFILE_VALUES:
            core_settings.save_user_setting(alice, "tone_profile", value)
            prefs = core_settings.get_personalization(alice)
            assert prefs["tone_profile"] == value

    def test_completely_stale_value_falls_back_silently(self, db_path):
        # A directly-written row whose value is no longer in the enum
        # (e.g. an experimental profile shipped on a side-branch) must
        # not crash the loader. ``get_user_setting`` is a raw read, so
        # the resilience contract is at ``build_tone_profile_block``:
        # unknown values resolve to "" and the chat path skips the
        # block, falling back to the warm baseline.
        alice = _make_user(db_path, "alice")
        core_settings.save_user_setting(
            alice, "tone_profile", "experimental_unknown"
        )
        # Raw read returns the stale string — that is the "don't run a
        # destructive migration" guarantee.
        raw = core_settings.get_user_setting(alice, "tone_profile", "default")
        assert raw == "experimental_unknown"
        # But the prompt builder treats it as no profile, so chat is
        # unaffected: the warm baseline still carries the response.
        assert build_tone_profile_block(raw) == ""

    def test_tone_profile_is_still_a_user_setting(self):
        # The settings router uses `is_user_setting` to decide whether a
        # POST belongs in `user_settings` or in the global table. Even
        # though the UI no longer sends `tone_profile`, the backend must
        # still treat any direct API call as a per-user write — never a
        # host-wide one. Removing it from `USER_SETTING_KEYS` would
        # silently re-route legacy clients to the global table.
        assert core_settings.is_user_setting("tone_profile")

    def test_tone_profile_default_is_registered(self):
        # `get_personalization` is the single read path; if `default`
        # disappears from `PERSONALIZATION_DEFAULTS` the fresh-user
        # payload loses a key and any client that reads `tone_profile`
        # crashes with KeyError. Pin the fallback explicitly.
        assert core_settings.PERSONALIZATION_DEFAULTS["tone_profile"] == "default"


# ── Unrelated-settings contract: nothing else regressed ─────────────────────


class TestOtherPersonalizationSettingsUnchanged:
    def test_other_enums_still_present(self):
        # Removing the tone-profile UI must not collateral-damage the
        # other personalization knobs — response style, warmth,
        # enthusiasm, emoji level all keep their enum surface.
        enums = core_settings.PERSONALIZATION_ENUMS
        assert enums["response_style"] == frozenset(
            {"default", "concise", "detailed", "technical"}
        )
        assert enums["warmth_level"] == frozenset({"low", "normal", "high"})
        assert enums["enthusiasm_level"] == frozenset({"low", "normal", "high"})
        assert enums["emoji_level"] == frozenset(
            {"none", "low", "medium", "expressive"}
        )

    def test_other_defaults_unchanged(self):
        defaults = core_settings.PERSONALIZATION_DEFAULTS
        assert defaults["response_style"] == "default"
        assert defaults["warmth_level"] == "normal"
        assert defaults["enthusiasm_level"] == "normal"
        assert defaults["emoji_level"] == "low"
        assert defaults["custom_instructions"] == ""

    def test_companion_mode_setting_still_registered(self):
        # Companion mode is the *other* opt-in tone layer — independent
        # from tone profile and untouched by this change. Pin that the
        # setting key is still per-user so a regression cannot also
        # remove it by accident.
        assert core_settings.is_user_setting("companion_mode_enabled")
