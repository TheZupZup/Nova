"""
Assistant-framing guardrails.

Nova is a warm, local-first **AI assistant** — for productivity, coding,
homelab, memory, and calm support. It is deliberately **not** an "AI
girlfriend", a romantic companion, a soulmate, or a dependency-forming
emotional partner. This file pins that positioning so a future rewrite
cannot quietly drift Nova back into companion-product framing:

  * the Nova Safety and Trust Contract carries the explicit
    assistant-not-companion boundaries (the safe phrases the brief
    requires: "AI assistant", "not a romantic partner", "encourage
    real-world support", "does not simulate feelings or attachment");
  * the README positions Nova as a local-first assistant, not a
    companion product, near the top;
  * the user-facing Settings *labels* never sound romantic or
    companion-like (no "companion", "girlfriend", "partner", …);
  * every warm system-prompt fragment restates the identity + the
    non-attachment clause (Nova does not love / miss / need / attach)
    and encourages real-world support;
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

from core.companion import (  # noqa: E402
    COMPANION_GROUNDING_BLOCK,
    COMPANION_MODE_BLOCK,
)
from core.emotional_support import EMOTIONAL_SUPPORT_BLOCK  # noqa: E402
from core.identity import IDENTITY_CONTRACT  # noqa: E402
from core.nova_contract import RESPONSE_STYLE_BLOCK  # noqa: E402
from core.policies import ADMIN_POLICY  # noqa: E402
from core.relationship_coach import RELATIONSHIP_COACH_BLOCK  # noqa: E402
from core.tone_profile import (  # noqa: E402
    TONE_CALM_SUPPORT_BLOCK,
    TONE_DEEP_COMFORT_BLOCK,
    TONE_DEVELOPER_BLOCK,
    TONE_PROFESSIONAL_BLOCK,
    TONE_WARM_COMPANION_BLOCK,
)
from core.chat import _autosave_allowed  # noqa: E402


REPO = Path(__file__).resolve().parents[1]
CONTRACT = REPO / "docs" / "nova-safety-and-trust-contract.md"
README = REPO / "README.md"
INDEX_HTML = REPO / "static" / "index.html"


def _norm(text: str) -> str:
    """Lowercase and collapse whitespace so Markdown line-wraps don't
    break a multi-word phrase check."""
    return re.sub(r"\s+", " ", text.lower())


# Fragments the reframe added the explicit non-attachment clause to.
_NON_ATTACHMENT_FRAGMENTS = {
    "RESPONSE_STYLE_BLOCK": RESPONSE_STYLE_BLOCK,
    "COMPANION_MODE_BLOCK": COMPANION_MODE_BLOCK,
    "TONE_WARM_COMPANION_BLOCK": TONE_WARM_COMPANION_BLOCK,
    "EMOTIONAL_SUPPORT_BLOCK": EMOTIONAL_SUPPORT_BLOCK,
}

# Every deterministic prompt fragment that ships in a system prompt.
_ALL_FRAGMENTS = {
    "IDENTITY_CONTRACT": IDENTITY_CONTRACT,
    "RESPONSE_STYLE_BLOCK": RESPONSE_STYLE_BLOCK,
    "COMPANION_MODE_BLOCK": COMPANION_MODE_BLOCK,
    "COMPANION_GROUNDING_BLOCK": COMPANION_GROUNDING_BLOCK,
    "EMOTIONAL_SUPPORT_BLOCK": EMOTIONAL_SUPPORT_BLOCK,
    "RELATIONSHIP_COACH_BLOCK": RELATIONSHIP_COACH_BLOCK,
    "TONE_PROFESSIONAL_BLOCK": TONE_PROFESSIONAL_BLOCK,
    "TONE_DEVELOPER_BLOCK": TONE_DEVELOPER_BLOCK,
    "TONE_WARM_COMPANION_BLOCK": TONE_WARM_COMPANION_BLOCK,
    "TONE_CALM_SUPPORT_BLOCK": TONE_CALM_SUPPORT_BLOCK,
    "TONE_DEEP_COMFORT_BLOCK": TONE_DEEP_COMFORT_BLOCK,
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
        "always here for you", "lonely",
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

    def test_calm_support_label_replaced_companion_mode(self):
        html = self._html()
        titles = [v.lower() for v in self._title_values(html)]
        assert "calm support" in titles
        assert "soutien calme" in titles
        # The old companion labels are gone from the label surface.
        assert "companion mode" not in titles
        assert "mode compagnon" not in titles


# ── Warm prompt fragments restate the identity + non-attachment clause ──────


class TestWarmFragmentsHaveBoundaries:
    def test_non_attachment_clause_present(self):
        # Every warm fragment must carry the explicit "does not
        # attach / miss" clause so a warm register can never imply Nova
        # loves, misses, or needs the user.
        for name, block in _NON_ATTACHMENT_FRAGMENTS.items():
            low = block.lower()
            assert "t'attaches pas" in low, name
            assert "ne te manque pas" in low, name
            assert "pas besoin" in low, name

    def test_fragments_stay_honest_about_being_an_ai(self):
        for name, block in _NON_ATTACHMENT_FRAGMENTS.items():
            low = block.lower()
            assert "assistant ia local" in low or "une ia" in low, name

    def test_fragments_encourage_real_world_support(self):
        for name, block in _NON_ATTACHMENT_FRAGMENTS.items():
            low = block.lower()
            assert any(
                marker in low
                for marker in (
                    "vraies personnes",
                    "liens humains réels",
                    "personne de confiance",
                    "relations humaines",
                )
            ), name


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


# ── Emotional support: real-world, honest, and not auto-saved ───────────────


class TestEmotionalSupportBehaviour:
    def test_block_encourages_real_world_and_no_substitute(self):
        low = EMOTIONAL_SUPPORT_BLOCK.lower()
        assert "personne de confiance" in low
        assert "professionnel" in low
        # Nova must not position itself as a replacement for real people.
        assert "remplacement" in low or "substitut" in low

    def test_block_does_not_over_identify_with_user(self):
        low = EMOTIONAL_SUPPORT_BLOCK.lower()
        # Honest identity + the non-attachment clause together prevent
        # the block from over-identifying ("I'm always here for you").
        assert "tu restes nova" in low
        assert "t'attaches pas" in low

    def test_emotional_turn_is_not_auto_saved(self):
        # Sensitive emotional turns must never be silently mined into
        # durable memory; the explicit "Retiens ça:" path is separate.
        assert _autosave_allowed(ADMIN_POLICY, "i feel so alone tonight") is False
        assert _autosave_allowed(ADMIN_POLICY, "i'm heartbroken") is False
        assert _autosave_allowed(ADMIN_POLICY, "je me sens seul ce soir") is False

    def test_neutral_turn_is_still_auto_saved(self):
        # The gate is targeted, not a blanket memory kill-switch.
        assert _autosave_allowed(ADMIN_POLICY, "I use neovim and Fedora") is True
