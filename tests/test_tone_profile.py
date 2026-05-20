"""
Tests for the Tone Profile foundation:

  - the deterministic per-profile prompt blocks (warm_companion,
    calm_support, professional, developer) and their safety language
  - ``default`` / unknown / non-string profile values resolve to an
    empty string so a fresh account is byte-identical to no profile
  - ``core.chat.build_messages`` appends the right block when the user
    has picked a non-default profile, always *below* IDENTITY_CONTRACT
  - the per-user setting key is registered, enum-validated, and
    round-trips through the data layer without leaking between users
  - the setting does not change storage/export/restore behaviour and
    does not relax auth, identity, or safety rules

Heavy / optional deps that ``core.chat`` pulls in transitively are
stubbed (mirroring test_relationship_coach.py / test_companion.py) so
the chat-wiring tests collect cleanly on a minimal host.
"""

from __future__ import annotations

import contextlib
import sqlite3
import sys
from unittest.mock import MagicMock, patch

import pytest

# Heavy network deps the chat module imports at module load. Stub them
# before the import so a missing wheel never blocks this test file.
for _mod in ("ddgs", "ollama", "sgmllib", "feedparser"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from core.tone_profile import (  # noqa: E402
    TONE_CALM_SUPPORT_BLOCK,
    TONE_DEVELOPER_BLOCK,
    TONE_PROFESSIONAL_BLOCK,
    TONE_PROFILE_VALUES,
    TONE_WARM_COMPANION_BLOCK,
    build_tone_profile_block,
    is_valid_tone_profile,
)
from core.chat import build_messages  # noqa: E402
from core.identity import IDENTITY_CONTRACT  # noqa: E402
from core import memory as core_memory, settings as core_settings, users  # noqa: E402
from memory import store as natural_store  # noqa: E402


# ── Constants surface ───────────────────────────────────────────────────────

class TestToneProfileConstants:
    def test_known_values(self):
        assert TONE_PROFILE_VALUES == (
            "default",
            "professional",
            "developer",
            "warm_companion",
            "calm_support",
        )

    def test_default_is_present_first(self):
        # The order matters for the UI's <select> rendering: default
        # comes first so a fresh account sees "Default" highlighted.
        assert TONE_PROFILE_VALUES[0] == "default"

    def test_is_registered_in_personalization_enums(self):
        assert "tone_profile" in core_settings.PERSONALIZATION_ENUMS
        assert core_settings.PERSONALIZATION_ENUMS["tone_profile"] == frozenset(
            TONE_PROFILE_VALUES
        )

    def test_default_is_registered(self):
        assert core_settings.PERSONALIZATION_DEFAULTS["tone_profile"] == "default"

    def test_is_a_user_setting(self):
        # Personalization keys must round-trip through user_settings so
        # one account's tone choice never leaks onto another's chat.
        assert core_settings.is_user_setting("tone_profile")


# ── is_valid_tone_profile ───────────────────────────────────────────────────

class TestIsValidToneProfile:
    def test_accepts_every_known_value(self):
        for v in TONE_PROFILE_VALUES:
            assert is_valid_tone_profile(v) is True

    def test_rejects_unknown_value(self):
        assert is_valid_tone_profile("girlfriend") is False
        assert is_valid_tone_profile("therapist") is False
        assert is_valid_tone_profile("") is False

    def test_rejects_non_string(self):
        assert is_valid_tone_profile(None) is False
        assert is_valid_tone_profile(42) is False
        assert is_valid_tone_profile(["warm_companion"]) is False


# ── build_tone_profile_block: deterministic + safe by default ───────────────

class TestBuildToneProfileBlock:
    def test_default_resolves_to_empty_string(self):
        # ``default`` must be indistinguishable from "no profile" so a
        # fresh account pays zero token cost and behaves exactly as
        # before the feature existed.
        assert build_tone_profile_block("default") == ""

    def test_unknown_value_resolves_to_empty_string(self):
        # A stale setting (e.g. an enum value removed in a later
        # release) must not break chat — fall back to the safe baseline.
        assert build_tone_profile_block("verbose") == ""
        assert build_tone_profile_block("") == ""

    def test_non_string_is_safe(self):
        assert build_tone_profile_block(None) == ""
        assert build_tone_profile_block(42) == ""
        assert build_tone_profile_block(["warm_companion"]) == ""

    def test_returns_the_right_block_per_profile(self):
        assert build_tone_profile_block("professional") == TONE_PROFESSIONAL_BLOCK
        assert build_tone_profile_block("developer") == TONE_DEVELOPER_BLOCK
        assert build_tone_profile_block("warm_companion") == TONE_WARM_COMPANION_BLOCK
        assert build_tone_profile_block("calm_support") == TONE_CALM_SUPPORT_BLOCK

    def test_is_byte_identical_on_repeated_calls(self):
        # Determinism is the whole point: no LLM in the loop, same input
        # → byte-identical output. Pin it so a future "personalisation
        # touch-up" cannot accidentally inject the user's id, the time,
        # or a random nonce into the block.
        for name in ("professional", "developer",
                     "warm_companion", "calm_support"):
            assert (build_tone_profile_block(name)
                    == build_tone_profile_block(name))


# ── Per-block safety / tone language ────────────────────────────────────────
#
# These pin the exact wording the README and docs promise. They are not
# spelling tests; each assertion captures a specific safety / tone
# commitment that must survive any future re-wording of the block.

class TestProfessionalBlock:
    def test_is_non_empty(self):
        assert TONE_PROFESSIONAL_BLOCK.strip()

    def test_no_unfilled_placeholders(self):
        import re
        assert not re.search(r"\{[^}]+\}", TONE_PROFESSIONAL_BLOCK)

    def test_is_subordinate_to_the_contract(self):
        lower = TONE_PROFESSIONAL_BLOCK.lower()
        assert "subordonné" in lower
        assert "sécurité" in lower

    def test_restates_no_human_role(self):
        lower = TONE_PROFESSIONAL_BLOCK.lower()
        assert "ne te fais jamais passer pour un humain" in lower
        assert "assistant ia local" in lower

    def test_restates_no_permission_override(self):
        # Every non-default block must reaffirm that picking the style
        # is *not* a way to relax permissions, security, privacy, or
        # project rules.
        lower = TONE_PROFESSIONAL_BLOCK.lower()
        assert "authentification" in lower
        assert "confidentialité" in lower
        assert "permissions" in lower


class TestDeveloperBlock:
    def test_is_non_empty(self):
        assert TONE_DEVELOPER_BLOCK.strip()

    def test_no_unfilled_placeholders(self):
        import re
        assert not re.search(r"\{[^}]+\}", TONE_DEVELOPER_BLOCK)

    def test_is_subordinate_to_the_contract(self):
        lower = TONE_DEVELOPER_BLOCK.lower()
        assert "subordonné" in lower
        assert "sécurité" in lower

    def test_explicitly_blocks_destructive_action(self):
        # Developer tone is exactly where a model might be tempted to
        # "just run it"; the block must reaffirm the read-only posture.
        lower = TONE_DEVELOPER_BLOCK.lower()
        assert "aucune action destructrice" in lower
        assert "sudo" in lower

    def test_restates_no_human_role(self):
        lower = TONE_DEVELOPER_BLOCK.lower()
        assert "ne te fais jamais passer pour un humain" in lower


class TestWarmCompanionBlock:
    def test_is_non_empty(self):
        assert TONE_WARM_COMPANION_BLOCK.strip()

    def test_no_unfilled_placeholders(self):
        import re
        assert not re.search(r"\{[^}]+\}", TONE_WARM_COMPANION_BLOCK)

    def test_is_subordinate_to_the_contract(self):
        lower = TONE_WARM_COMPANION_BLOCK.lower()
        assert "subordonné" in lower
        assert "sécurité" in lower

    def test_explicitly_not_human_not_partner(self):
        # The single hardest line to hold for a warm tone: it must
        # never claim to be human, a romantic partner, or a substitute
        # for real people.
        lower = TONE_WARM_COMPANION_BLOCK.lower()
        assert "tu n'es pas humaine" in lower
        assert "petite amie" in lower or "partenaire" in lower
        assert "ne joues jamais ce rôle" in lower

    def test_never_simulates_feelings_as_facts(self):
        lower = TONE_WARM_COMPANION_BLOCK.lower()
        assert "ne simules pas d'émotions" in lower
        assert "jamais comme des faits" in lower

    def test_emotional_care_before_technical_steps(self):
        lower = TONE_WARM_COMPANION_BLOCK.lower()
        # The block must say emotional acknowledgement comes *before*
        # the technical next step, not instead of it.
        assert "avant" in lower
        assert "étapes techniques" in lower

    def test_celebrates_small_wins_without_flattery(self):
        lower = TONE_WARM_COMPANION_BLOCK.lower()
        assert "petites victoires" in lower
        assert "sans flatterie creuse" in lower

    def test_anti_dependency_anti_isolation_anti_manipulation(self):
        lower = TONE_WARM_COMPANION_BLOCK.lower()
        assert "aucune manipulation" in lower
        assert "aucun chantage affectif" in lower
        assert "aucune culpabilisation" in lower
        assert "ne crée jamais de dépendance" in lower
        assert "n'encourage jamais l'isolement" in lower

    def test_encourages_real_world_connection(self):
        lower = TONE_WARM_COMPANION_BLOCK.lower()
        assert "liens humains réels" in lower
        assert "remplacement des relations humaines" in lower

    def test_no_possessive_or_pet_names(self):
        lower = TONE_WARM_COMPANION_BLOCK.lower()
        assert "ne pars pas" in lower or "tu me manques" in lower
        assert "intimité simulée" in lower
        assert "surnom affectif non demandé" in lower

    def test_warmth_does_not_override_truth(self):
        # If something is risky / wrong / dangerous, the warm block
        # must still say so plainly. Honesty wins over softness.
        lower = TONE_WARM_COMPANION_BLOCK.lower()
        assert "reste honnête" in lower
        assert "risqué" in lower or "dangereux" in lower

    def test_does_not_change_auth_admin_storage_rules(self):
        lower = TONE_WARM_COMPANION_BLOCK.lower()
        assert "authentification" in lower
        assert "admin" in lower
        assert "confidentialité" in lower


class TestCalmSupportBlock:
    def test_is_non_empty(self):
        assert TONE_CALM_SUPPORT_BLOCK.strip()

    def test_no_unfilled_placeholders(self):
        import re
        assert not re.search(r"\{[^}]+\}", TONE_CALM_SUPPORT_BLOCK)

    def test_is_subordinate_to_the_contract(self):
        lower = TONE_CALM_SUPPORT_BLOCK.lower()
        assert "subordonné" in lower
        assert "sécurité" in lower

    def test_not_human_not_partner_not_therapist(self):
        lower = TONE_CALM_SUPPORT_BLOCK.lower()
        assert "tu n'es ni humaine" in lower or "tu n'es pas humaine" in lower
        assert "partenaire" in lower
        assert "thérapeute" in lower

    def test_never_simulates_feelings_as_facts(self):
        lower = TONE_CALM_SUPPORT_BLOCK.lower()
        assert "ne simules pas d'émotions" in lower
        assert "jamais comme des faits" in lower

    def test_emotional_care_before_concrete_next_step(self):
        lower = TONE_CALM_SUPPORT_BLOCK.lower()
        assert "reconnais d'abord" in lower
        # "puis propose" or "*puis* propose" — the emphasis around
        # "puis" can be tightened in a future re-wording, so the test
        # only pins the ordering, not the exact glyphs.
        idx_first = lower.find("reconnais d'abord")
        idx_then = lower.find("propose une suite")
        assert idx_first != -1 and idx_then != -1
        assert idx_first < idx_then

    def test_offers_small_steps_not_long_lists(self):
        lower = TONE_CALM_SUPPORT_BLOCK.lower()
        assert "un seul petit pas" in lower
        assert "pas une liste" in lower

    def test_anti_dependency_anti_isolation_anti_manipulation(self):
        lower = TONE_CALM_SUPPORT_BLOCK.lower()
        assert "aucune manipulation" in lower
        assert "ne crée jamais de dépendance" in lower
        assert "n'encourage jamais l'isolement" in lower

    def test_encourages_real_world_help(self):
        lower = TONE_CALM_SUPPORT_BLOCK.lower()
        assert "personnes de confiance" in lower
        assert "professionnel" in lower
        assert "substitut" in lower

    def test_calmness_does_not_override_truth(self):
        lower = TONE_CALM_SUPPORT_BLOCK.lower()
        assert "reste honnête" in lower
        assert "risqué" in lower or "dangereux" in lower

    def test_does_not_change_auth_admin_storage_rules(self):
        lower = TONE_CALM_SUPPORT_BLOCK.lower()
        assert "authentification" in lower
        assert "admin" in lower
        assert "confidentialité" in lower


# ── core.chat.build_messages wiring ─────────────────────────────────────────

class TestChatWiring:
    def test_default_profile_adds_nothing(self):
        # A fresh account's payload includes ``tone_profile="default"``;
        # the prompt must be byte-identical to the no-profile baseline.
        baseline = build_messages([], "hello", [], None, None, None)
        with_default = build_messages(
            [], "hello", [], None, None, None,
            personalization={"tone_profile": "default"},
        )
        assert baseline[0]["content"] == with_default[0]["content"]

    def test_no_personalization_means_no_tone_block(self):
        msgs = build_messages([], "hello", [], None, None, None)
        sys_msg = msgs[0]["content"]
        for block in (
            TONE_PROFESSIONAL_BLOCK,
            TONE_DEVELOPER_BLOCK,
            TONE_WARM_COMPANION_BLOCK,
            TONE_CALM_SUPPORT_BLOCK,
        ):
            assert block not in sys_msg

    @pytest.mark.parametrize("profile,block", [
        ("professional", TONE_PROFESSIONAL_BLOCK),
        ("developer", TONE_DEVELOPER_BLOCK),
        ("warm_companion", TONE_WARM_COMPANION_BLOCK),
        ("calm_support", TONE_CALM_SUPPORT_BLOCK),
    ])
    def test_non_default_profile_lands_in_system_prompt(self, profile, block):
        msgs = build_messages(
            [], "hello", [], None, None, None,
            personalization={"tone_profile": profile},
        )
        assert block in msgs[0]["content"]

    def test_only_one_tone_block_is_appended(self):
        # Selecting a profile must append *its* block and only its
        # block — no accidental sibling injection.
        msgs = build_messages(
            [], "hello", [], None, None, None,
            personalization={"tone_profile": "warm_companion"},
        )
        sys_msg = msgs[0]["content"]
        assert TONE_WARM_COMPANION_BLOCK in sys_msg
        for other in (
            TONE_PROFESSIONAL_BLOCK,
            TONE_DEVELOPER_BLOCK,
            TONE_CALM_SUPPORT_BLOCK,
        ):
            assert other not in sys_msg

    def test_identity_contract_sits_above_the_tone_block(self):
        # Ordering is the load-bearing guarantee: the tone block can
        # never be placed above the identity / safety contract, so it
        # can never weaken or override it.
        msgs = build_messages(
            [], "hello", [], None, None, None,
            personalization={"tone_profile": "warm_companion"},
        )
        content = msgs[0]["content"]
        assert content.startswith(IDENTITY_CONTRACT)
        assert content.index(IDENTITY_CONTRACT) < content.index(
            TONE_WARM_COMPANION_BLOCK
        )

    def test_tone_block_coexists_with_companion_mode_block(self):
        # Picking a warm tone profile does not change the opt-in
        # Companion Mode toggle, and vice-versa: both blocks may
        # coexist (each with its own safety language) when the user
        # has set both.
        from core.companion import COMPANION_MODE_BLOCK
        msgs = build_messages(
            [], "hello", [], None, None, None,
            personalization={"tone_profile": "warm_companion"},
            companion_mode=True,
        )
        content = msgs[0]["content"]
        assert TONE_WARM_COMPANION_BLOCK in content
        assert COMPANION_MODE_BLOCK in content
        # Both blocks must still sit below the identity contract.
        assert content.index(IDENTITY_CONTRACT) < content.index(
            TONE_WARM_COMPANION_BLOCK
        )
        assert content.index(IDENTITY_CONTRACT) < content.index(
            COMPANION_MODE_BLOCK
        )

    def test_tone_block_does_not_disable_acute_distress_safety_net(self):
        # Even when the user has picked a warm tone, an acute-distress
        # message still appends the grounding safety net — turning a
        # comfort feature *on* must not turn the safety net off.
        from core.companion import COMPANION_GROUNDING_BLOCK
        msgs = build_messages(
            [], "i want to die", [], None, None, None,
            personalization={"tone_profile": "warm_companion"},
        )
        assert COMPANION_GROUNDING_BLOCK in msgs[0]["content"]

    def test_unknown_profile_value_falls_back_silently(self):
        # If a future migration changes the enum, an existing row with
        # a stale value must not break the prompt: it falls back to
        # the empty block (no extra tokens).
        msgs = build_messages(
            [], "hello", [], None, None, None,
            personalization={"tone_profile": "girlfriend"},
        )
        sys_msg = msgs[0]["content"]
        for block in (
            TONE_PROFESSIONAL_BLOCK,
            TONE_DEVELOPER_BLOCK,
            TONE_WARM_COMPANION_BLOCK,
            TONE_CALM_SUPPORT_BLOCK,
        ):
            assert block not in sys_msg


# ── Per-user storage / per-user isolation ───────────────────────────────────

@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "nova.db")
    monkeypatch.setattr(core_memory, "DB_PATH", path)
    monkeypatch.setattr(natural_store, "DB_PATH", path)
    core_memory.initialize_db()
    return path


def _make_user(db_path, username, password="pw", role=users.ROLE_USER):
    with sqlite3.connect(db_path) as conn:
        return users.create_user(conn, username, password, role=role)


class TestPersonalizationStorage:
    def test_fresh_user_sees_default_profile(self, db_path):
        a = _make_user(db_path, "alice")
        prefs = core_settings.get_personalization(a)
        assert prefs["tone_profile"] == "default"

    def test_save_then_get_round_trip(self, db_path):
        a = _make_user(db_path, "alice")
        for profile in TONE_PROFILE_VALUES:
            core_settings.save_user_setting(a, "tone_profile", profile)
            assert core_settings.get_personalization(a)["tone_profile"] == profile

    def test_one_users_choice_does_not_leak_to_another(self, db_path):
        a = _make_user(db_path, "alice")
        b = _make_user(db_path, "bob")
        core_settings.save_user_setting(a, "tone_profile", "warm_companion")
        # Bob has saved nothing — he still sees the default.
        assert (
            core_settings.get_personalization(b)["tone_profile"] == "default"
        )
        # Alice's choice survives Bob existing.
        assert (
            core_settings.get_personalization(a)["tone_profile"]
            == "warm_companion"
        )


class TestValidatePersonalizationValue:
    @pytest.mark.parametrize("profile", TONE_PROFILE_VALUES)
    def test_accepts_every_known_value(self, profile):
        assert (
            core_settings.validate_personalization_value("tone_profile", profile)
            == profile
        )

    def test_rejects_unknown_value(self):
        with pytest.raises(ValueError):
            core_settings.validate_personalization_value(
                "tone_profile", "girlfriend"
            )


# ── HTTP layer (smoke tests) ────────────────────────────────────────────────
#
# Confirms the SettingsUpdateRequest validator and the /settings
# round-trip behave exactly like the existing personalization keys,
# so a new tone profile never opens a side-channel.

@pytest.fixture
def web_client(db_path, monkeypatch):
    monkeypatch.setattr(core_memory, "DB_PATH", db_path)
    monkeypatch.setattr(natural_store, "DB_PATH", db_path)
    from core.rate_limiter import _login_limiter
    _login_limiter._store.clear()

    from fastapi.testclient import TestClient
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


class TestSettingsHttpEndpoint:
    @pytest.mark.parametrize("profile", TONE_PROFILE_VALUES)
    def test_user_can_save_and_read_back_each_profile(
        self, db_path, web_client, profile,
    ):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        resp = web_client.post(
            "/settings", json={"tone_profile": profile}, headers=_h(token)
        )
        assert resp.status_code == 200, resp.text
        body = web_client.get("/settings", headers=_h(token)).json()
        assert body["tone_profile"] == profile

    def test_invalid_profile_is_rejected_with_422(self, db_path, web_client):
        a = _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        resp = web_client.post(
            "/settings",
            json={"tone_profile": "girlfriend"},
            headers=_h(token),
        )
        assert resp.status_code == 422
        # Nothing was persisted for the rejected value.
        assert (
            core_settings.get_user_setting(a, "tone_profile", "MISSING")
            == "MISSING"
        )

    def test_partial_update_does_not_disturb_other_personalization(
        self, db_path, web_client,
    ):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        # Pre-populate other personalization keys.
        web_client.post(
            "/settings",
            json={"response_style": "concise", "emoji_level": "medium"},
            headers=_h(token),
        )
        # Now flip only tone_profile; the other fields must survive.
        web_client.post(
            "/settings",
            json={"tone_profile": "calm_support"},
            headers=_h(token),
        )
        body = web_client.get("/settings", headers=_h(token)).json()
        assert body["tone_profile"] == "calm_support"
        assert body["response_style"] == "concise"
        assert body["emoji_level"] == "medium"

    def test_default_user_payload_includes_default_tone_profile(
        self, db_path, web_client,
    ):
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        body = web_client.get("/settings", headers=_h(token)).json()
        # A brand-new account exposes the default value via /settings
        # so the UI can paint the <select> without a special case.
        assert body["tone_profile"] == "default"

    def test_unknown_field_is_still_rejected(self, db_path, web_client):
        """`extra="forbid"` continues to cover the personalization endpoint."""
        _make_user(db_path, "alice")
        token = _login(web_client, "alice")
        resp = web_client.post(
            "/settings",
            json={"tone_profile_v2": "warm_companion"},
            headers=_h(token),
        )
        assert resp.status_code == 422
