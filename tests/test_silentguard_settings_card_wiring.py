"""
Static-asset wiring tests for the SilentGuard Settings status card.

The Settings UI lives in a single ``static/index.html`` file with no
build step and no JS test harness. Several real bugs in the card's
Refresh path (silent click, in-flight guard masking errors, fetch
missing the authenticated session) would have shipped quietly because
nothing pinned the wiring. These tests parse ``index.html`` and assert
the structural contract the bug report calls out:

  * the Refresh button keeps the id the JS queries;
  * the click handler is attached *after* the element exists, instead
    of relying on an inline ``onclick=`` that disappears under stricter
    CSP and is awkward to test;
  * ``refreshSilentGuardStatus`` exists and calls the documented summary
    endpoint with the authenticated session attached
    (``credentials: "include"``);
  * a failure UI helper exists so errors surface in the card instead of
    being swallowed silently;
  * opening Settings refreshes the status card on every open.

The tests are deliberately structural — they do not try to execute the
script. They protect the wiring from regressing without requiring a
frontend toolchain.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


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


# ── Button + listener wiring ────────────────────────────────────────


class TestRefreshButtonWiring:
    def test_refresh_button_keeps_documented_id(self, html: str) -> None:
        # The JS queries the button by id; renaming or dropping the id
        # would silently break the click path.
        assert 'id="silentguard-refresh-btn"' in html

    def test_refresh_button_does_not_rely_on_inline_onclick(
        self, html: str,
    ) -> None:
        # Inline ``onclick="refreshSilentGuardStatus()"`` is what the
        # bug report's "does nothing visibly" scenario hinged on: a
        # stricter CSP, a script-load error elsewhere on the page, or a
        # test fixture that does not execute inline attributes leaves
        # the button mute. The explicit listener below must own the
        # wiring instead.
        button_match = re.search(
            r'<button[^>]*id="silentguard-refresh-btn"[^>]*>',
            html,
        )
        assert button_match, "Refresh button tag not found"
        assert "onclick=" not in button_match.group(0), (
            "Refresh button must not rely on inline onclick; use "
            "addEventListener so the wiring is testable and CSP-safe."
        )

    def test_refresh_listener_is_attached_after_dom_exists(
        self, script: str,
    ) -> None:
        # The listener must be attached against the real button element
        # and must call ``refreshSilentGuardStatus`` so a click triggers
        # the documented refresh path. We assert the structural shape
        # rather than the exact source so cosmetic edits do not break
        # the test.
        assert 'getElementById("silentguard-refresh-btn")' in script
        listener_block = re.search(
            r'getElementById\("silentguard-refresh-btn"\)[\s\S]{0,400}?'
            r'addEventListener\("click",[\s\S]{0,400}?'
            r'refreshSilentGuardStatus\(\)',
            script,
        )
        assert listener_block, (
            "Expected an explicit addEventListener('click', ...) that "
            "calls refreshSilentGuardStatus() on the Refresh button."
        )


# ── Fetch contract ──────────────────────────────────────────────────


class TestRefreshFetchContract:
    def test_refresh_function_exists(self, script: str) -> None:
        assert re.search(
            r"async function refreshSilentGuardStatus\s*\(\s*\)\s*{",
            script,
        ), "refreshSilentGuardStatus must remain an async function"

    def test_refresh_calls_summary_endpoint_with_credentials(
        self, script: str,
    ) -> None:
        # The fetch must hit the documented summary endpoint and must
        # send the authenticated session. ``credentials: "include"``
        # keeps the cookie-only auth path working alongside the Bearer
        # header; either alone is fragile.
        fetch_call = re.search(
            r'fetch\(\s*"/integrations/silentguard/summary"\s*,'
            r'(?P<opts>[\s\S]{0,400}?)\)',
            script,
        )
        assert fetch_call, (
            "Refresh path must fetch /integrations/silentguard/summary."
        )
        opts = fetch_call.group("opts")
        assert 'credentials: "include"' in opts, (
            "Refresh fetch must pass credentials: 'include' so the "
            "authenticated session is attached on every refresh."
        )

    def test_failure_path_surfaces_visible_card_state(
        self, script: str,
    ) -> None:
        # Errors must land in the card, not in a swallowed promise.
        # ``applySilentGuardCheckFailedUI`` is the helper that paints
        # the calm but visible failure state.
        assert "function applySilentGuardCheckFailedUI" in script
        # Both the !res.ok branch and the catch must route to it so a
        # 401/network error never disappears.
        assert script.count("applySilentGuardCheckFailedUI()") >= 2, (
            "applySilentGuardCheckFailedUI must run on both !res.ok "
            "and on a thrown fetch so failures stay visible."
        )

    def test_settings_open_refreshes_card(self, script: str) -> None:
        # Opening Settings must trigger a refresh — that is the second
        # documented refresh trigger besides the button.
        open_settings = re.search(
            r"async function openSettings\s*\(\s*\)\s*{[\s\S]+?\n\s*}",
            script,
        )
        assert open_settings, "openSettings function not found"
        assert "refreshSilentGuardStatus(" in open_settings.group(0), (
            "openSettings must call refreshSilentGuardStatus on every "
            "Settings open."
        )
