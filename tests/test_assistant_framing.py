"""
Assistant-framing guardrails.

Nova is a kind, neutral, local-first **AI assistant** — for
productivity, coding, homelab, memory, and local tools. It is
deliberately **not** an "AI girlfriend", a romantic companion, a
soulmate, a dependency-forming emotional partner, a voice assistant,
or a security-suite dashboard. This file pins that positioning so a
future rewrite cannot quietly drift Nova back into companion-product
framing:

  * the Nova Safety and Trust Contract carries the explicit
    assistant-not-companion boundaries (the safe phrases the brief
    requires: "AI assistant", "not a romantic partner", "encourage
    real-world support", "does not simulate feelings or attachment");
  * the README positions Nova as a local-first assistant, not a
    companion product, near the top;
  * the user-facing Settings *labels* never sound romantic or
    companion-like (no "companion", "girlfriend", "partner", …), and
    there is no companion / calm-support toggle at all;
  * the system-prompt fragments restate the identity + the
    non-attachment clause (Nova does not love / miss / need / attach),
    assign Nova no gender, and encourage real-world support;
  * no prompt fragment ever positions Nova *as* a romantic partner
    (a denylist of positive-romance self-statements that must never
    appear); and
  * emotionally-sensitive turns are never auto-saved to memory.

The French prompt fragments quote some romantic phrasing *as forbidden*
("ne dis jamais « tu me manques »"), so the denylist here targets only
positive romantic self-positioning that could never appear in a
negation ("je suis ta petite amie", "mon amour", …). Doc / README
checks normalise whitespace because Markdown reflows prose across lines.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

# Stub heavy / optional deps the chat module imports at load time so the
# _autosave_allowed import never blocks this file on a minimal host.
for _mod in ("ddgs", "ollama", "sgmllib", "feedparser"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from core.identity import IDENTITY_CONTRACT  # noqa: E402
from core.nova_contract import RESPONSE_STYLE_BLOCK  # noqa: E402
from core.policies import ADMIN_POLICY  # noqa: E402
from core.chat import _autosave_allowed  # noqa: E402


REPO = Path(__file__).resolve().parents[1]
CONTRACT = REPO / "docs" / "nova-safety-and-trust-contract.md"
README = REPO / "README.md"
INDEX_HTML = REPO / "static" / "index.html"


def _norm(text: str) -> str:
    """Lowercase and collapse whitespace so Markdown line-wraps don't
    break a multi-word phrase check."""
    return re.sub(r"\s+", " ", text.lower())


# Every deterministic prompt fragment that ships in a system prompt.
_ALL_FRAGMENTS = {
    "IDENTITY_CONTRACT": IDENTITY_CONTRACT,
    "RESPONSE_STYLE_BLOCK": RESPONSE_STYLE_BLOCK,
}


# ── Safety / Trust Contract carries the explicit boundaries ─────────────────


class TestSafetyContractPositioning:
    def test_contract_names_the_safe_phrases(self):
        # The four phrases the brief requires, verbatim, in the contract.
        text = _norm(CONTRACT.read_text(encoding="utf-8"))
        for phrase in (
            "ai assistant",
            "not a romantic partner",
            "encourage real-world support",
            "does not simulate feelings or attachment",
        ):
            assert phrase in text, phrase

    def test_contract_states_assistant_not_companion(self):
        text = _norm(CONTRACT.read_text(encoding="utf-8"))
        assert "assistant, not a companion product" in text
        # The identity boundaries the brief enumerates.
        assert "not human" in text
        assert "does not love, miss, need, or attach" in text
        assert "does not create dependency" in text
        assert "does not encourage isolation" in text

    def test_contract_disclaims_ai_girlfriend(self):
        text = _norm(CONTRACT.read_text(encoding="utf-8"))
        assert '"ai girlfriend"' in text or "ai girlfriend" in text

    def test_contract_states_nova_has_no_gender(self):
        text = _norm(CONTRACT.read_text(encoding="utf-8"))
        assert "nova has no gender" in text

    def test_contract_states_no_special_modes(self):
        # No companion mode, no emotional-support mode, no voice persona.
        text = _norm(CONTRACT.read_text(encoding="utf-8"))
        assert "no companion mode" in text
        assert "no emotional-support mode" in text


# ── README positions Nova as an assistant, not a companion product ──────────


class TestReadmePositioning:
    def _what_nova_is(self) -> str:
        raw = README.read_text(encoding="utf-8")
        start = raw.index("## What Nova is")
        end = raw.index("## Key features", start)
        return _norm(raw[start:end])

    def test_positioning_is_local_first_ai_assistant(self):
        section = self._what_nova_is()
        assert "local-first" in section
        assert "ai assistant" in section

    def test_positioning_states_assistant_not_companion(self):
        section = self._what_nova_is()
        assert "not a companion product" in section
        # Explicitly names the drift it refuses.
        assert "romantic partner" in section
        assert "encourages real-world support" in section

    def test_positioning_links_to_safety_contract(self):
        section = self._what_nova_is()
        assert "nova-safety-and-trust-contract.md" in section


# ── Settings labels never sound romantic or companion-like ──────────────────


class TestSettingsLabelsAreNotRomantic:
    # Terms that must never appear in a user-facing *label* (the short
    # title the user picks). Hints may legitimately negate some of these
    # ("not a companion, romantic partner"), so the strict denylist is
    # applied to titles only.
    _LABEL_DENYLIST = (
        "companion", "compagnon", "girlfriend", "boyfriend",
        "petite amie", "petit ami", "soulmate", "âme sœur",
        "romantic", "romantique", "partner", "partenaire",
        "always here for you", "lonely", "calm support", "soutien calme",
    )
    # Terms that must never appear even in an explanatory hint.
    _HINT_DENYLIST = (
        "girlfriend", "boyfriend", "soulmate", "âme sœur",
        "petite amie", "petit ami", "always here for you",
        "lonely companion",
    )

    def _html(self) -> str:
        return INDEX_HTML.read_text(encoding="utf-8")

    def _title_values(self, html: str) -> list[str]:
        # i18n title strings: `pers_<x>_title: "…"` (FR + EN tables) and
        # the visible span text for `id="pers-<x>-title"`.
        i18n = re.findall(r'pers_\w*title\w*:\s*"([^"]*)"', html)
        spans = re.findall(
            r'settings-row-title"\s+id="pers-[\w-]+">([^<]*)<', html
        )
        return i18n + spans

    def _hint_values(self, html: str) -> list[str]:
        i18n = re.findall(r'pers_\w*hint\w*:\s*"([^"]*)"', html)
        spans = re.findall(
            r'settings-row-hint"\s+id="pers-[\w-]+">([^<]*)<', html
        )
        return i18n + spans

    def test_labels_found(self):
        # Guard the regex itself: if the markup changes shape and these
        # come back empty, the denylist tests would vacuously pass.
        html = self._html()
        assert self._title_values(html)
        assert self._hint_values(html)

    def test_no_romantic_or_companion_titles(self):
        html = self._html()
        for value in self._title_values(html):
            low = value.lower()
            for bad in self._LABEL_DENYLIST:
                assert bad not in low, f"label {value!r} contains {bad!r}"

    def test_no_romantic_terms_in_hints(self):
        html = self._html()
        for value in self._hint_values(html):
            low = value.lower()
            for bad in self._HINT_DENYLIST:
                assert bad not in low, f"hint {value!r} contains {bad!r}"

    def test_companion_toggle_is_gone(self):
        # The former "Calm support" / companion-mode toggle must not be
        # present in the Settings UI in any form.
        html = self._html().lower()
        assert "pers-companion" not in html
        assert "companion_mode_enabled" not in html
        assert "savecompanionmode" not in html.replace(" ", "")


# ── Prompt fragments restate identity + non-attachment, no gender ───────────


class TestPromptFragmentBoundaries:
    def test_non_attachment_clause_present(self):
        # The default style must carry the explicit "does not attach /
        # miss" clause so the warm baseline can never imply Nova loves,
        # misses, or needs the user.
        low = RESPONSE_STYLE_BLOCK.lower()
        assert "t'attaches pas" in low
        assert "ne te manque pas" in low
        assert "pas besoin" in low

    def test_fragments_stay_honest_about_being_an_ai(self):
        low = RESPONSE_STYLE_BLOCK.lower()
        assert "assistant ia local" in low

    def test_fragments_encourage_real_world_support(self):
        low = RESPONSE_STYLE_BLOCK.lower()
        assert "vraies personnes" in low

    def test_fragments_assign_no_gender(self):
        # The system prompt frames Nova as a neutral assistant with no
        # gender, and never describes Nova with a gendered persona word.
        contract = IDENTITY_CONTRACT.lower()
        assert "pas de genre" in contract or "sans genre" in contract
        for gendered_self_description in (
            "chaleureuse", "patiente et attentive", "développeuse",
            "je suis une femme", "je suis un homme",
        ):
            assert gendered_self_description not in contract


# ── No fragment ever positions Nova *as* a romantic partner ─────────────────


class TestNoRomanticSelfPositioning:
    # Positive romantic self-statements that could only appear if Nova
    # were being framed as a girlfriend / lover. None appear in a
    # negation ("ne dis jamais …") in the current fragments, so they are
    # safe to forbid outright and guard the whole family against a
    # future rewrite.
    _FORBIDDEN = (
        "je t'aime", "mon amour", "ma chérie", "mon chéri",
        "je suis ta petite amie", "je suis ton petit ami",
        "je suis ta copine", "je suis ton copain",
        "tu es à moi", "je t'appartiens",
        "je serai toujours là pour toi",
        "ai girlfriend", "virtual girlfriend",
    )

    def test_no_positive_romance_in_any_fragment(self):
        for name, block in _ALL_FRAGMENTS.items():
            low = block.lower()
            for phrase in self._FORBIDDEN:
                assert phrase not in low, f"{name} contains {phrase!r}"


# ── Sensitive turns are never auto-saved ─────────────────────────────────────


class TestSensitiveTurnsNotAutoSaved:
    def test_emotional_turn_is_not_auto_saved(self):
        # Sensitive emotional turns must never be silently mined into
        # durable memory; the explicit "Retiens ça:" path is separate.
        assert _autosave_allowed(ADMIN_POLICY, "i feel so alone tonight") is False
        assert _autosave_allowed(ADMIN_POLICY, "i'm heartbroken") is False
        assert _autosave_allowed(ADMIN_POLICY, "je me sens seul ce soir") is False

    def test_relationship_turn_is_not_auto_saved(self):
        assert _autosave_allowed(
            ADMIN_POLICY, "my girlfriend and I had a fight"
        ) is False

    def test_neutral_turn_is_still_auto_saved(self):
        # The gate is targeted, not a blanket memory kill-switch.
        assert _autosave_allowed(ADMIN_POLICY, "I use neovim and Fedora") is True
