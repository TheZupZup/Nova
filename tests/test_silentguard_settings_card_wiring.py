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


def _function_body(script: str, name: str) -> str:
    """Return the full source of a top-level JS function in ``script``.

    Walks brace depth from the opening ``{`` so nested object literals
    or try/catch blocks do not confuse the close. Asserts when the
    function is missing — every caller treats absence as a hard fail.
    """
    decl = re.search(
        r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*{",
        script,
    )
    assert decl, f"function {name!r} not found in script"
    start = decl.start()
    open_brace = script.index("{", decl.end() - 1)
    depth = 0
    i = open_brace
    while i < len(script):
        ch = script[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return script[start:i + 1]
        i += 1
    raise AssertionError(f"unterminated function body for {name!r}")


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


# ── Per-user toggle wiring ──────────────────────────────────────────
#
# The bug report's "env says enabled but UI says disabled" failure
# mode hinged on the missing per-user toggle: the summary endpoint
# requires ``silentguard_enabled`` to be true for a given user, the
# /settings endpoint accepts that flag, but the UI never exposed a
# control to flip it. Without this wiring, an operator who set every
# host-level env var correctly would still see "disabled" forever.


class TestPerUserToggleWiring:
    def test_toggle_button_exists_with_documented_id(self, html: str) -> None:
        # The JS queries the toggle by id; without it, the button is
        # invisible to every code path and the bug returns.
        assert 'id="silentguard-toggle-btn"' in html

    def test_toggle_button_does_not_rely_on_inline_onclick(
        self, html: str,
    ) -> None:
        # Same reasoning as the Refresh button: inline onclick is
        # CSP-fragile and untestable.
        toggle_match = re.search(
            r'<button[^>]*id="silentguard-toggle-btn"[^>]*>',
            html,
        )
        assert toggle_match, "Toggle button tag not found"
        assert "onclick=" not in toggle_match.group(0), (
            "Toggle button must use addEventListener, not inline onclick."
        )

    def test_toggle_listener_calls_async_handler(self, script: str) -> None:
        assert 'getElementById("silentguard-toggle-btn")' in script
        listener_block = re.search(
            r'getElementById\("silentguard-toggle-btn"\)[\s\S]{0,400}?'
            r'addEventListener\("click",[\s\S]{0,400}?'
            r'toggleSilentGuardEnabled\(\)',
            script,
        )
        assert listener_block, (
            "Expected an explicit addEventListener('click', ...) that "
            "calls toggleSilentGuardEnabled() on the toggle button."
        )

    def test_toggle_handler_posts_to_settings_with_credentials(
        self, script: str,
    ) -> None:
        # The handler must POST to /settings with silentguard_enabled
        # and ``credentials: "include"`` so the session cookie path
        # works alongside the bearer token. Anything less and a flip
        # silently drops the change.
        body = _function_body(script, "toggleSilentGuardEnabled")
        assert 'fetch("/settings"' in body, (
            "toggleSilentGuardEnabled must POST to /settings."
        )
        assert 'method: "POST"' in body
        assert 'credentials: "include"' in body
        assert "silentguard_enabled" in body, (
            "Toggle must persist the silentguard_enabled key."
        )

    def test_toggle_refreshes_status_after_save(self, script: str) -> None:
        # After a successful flip, the card must re-pull so the user
        # immediately sees the new state — otherwise they have to hunt
        # for the Refresh button to confirm the change took effect.
        body = _function_body(script, "toggleSilentGuardEnabled")
        assert "refreshSilentGuardStatus(" in body, (
            "toggleSilentGuardEnabled must call refreshSilentGuardStatus "
            "after a successful save so the card paints the new state."
        )

    def test_initial_toggle_state_is_loaded_from_settings(
        self, script: str,
    ) -> None:
        # Settings open calls loadSystemSettings which reads /settings
        # and must paint the toggle's initial state. Without this, a
        # user who has flipped the toggle in a previous session sees
        # OFF until they trigger a refresh — a confusing regression.
        body = _function_body(script, "loadSystemSettings")
        assert "applySilentGuardToggleUI" in body, (
            "loadSystemSettings must paint the SilentGuard toggle's "
            "initial state from the saved setting."
        )
        assert "silentguard_enabled" in body, (
            "loadSystemSettings must read silentguard_enabled from the "
            "/settings payload."
        )


# ── Disabled-state UI text differentiation ──────────────────────────
#
# The bug's most visible symptom: the headline always said
# "SilentGuard integration disabled", regardless of whether the
# operator needed to set env vars or just flip a per-user toggle.
# These tests pin the new headline behaviour: the UI renders distinct
# text for the two cases, driven by the new ``host_enabled`` field.


class TestDisabledHeadlineDifferentiation:
    def test_host_enabled_drives_headline_choice(self, script: str) -> None:
        # The render path must consult ``host_enabled`` (or fall back
        # to lifecycle.enabled) to pick between the two distinct
        # disabled headlines.
        assert "host_enabled" in script, (
            "Frontend must consume the new host_enabled field to pick "
            "the right disabled headline."
        )
        # Both headline keys must be present in the render path.
        assert "silentguard_disabled_user_off" in script
        assert "silentguard_disabled_host_off" in script

    def test_disabled_user_off_string_is_translated(self, html: str) -> None:
        # The new keys must exist in both language packs so a French
        # user does not see an English fallback. Spot-check both
        # languages without coupling to the exact wording.
        assert "silentguard_disabled_user_off:" in html, (
            "silentguard_disabled_user_off must be defined in i18n."
        )
        assert "silentguard_disabled_host_off:" in html, (
            "silentguard_disabled_host_off must be defined in i18n."
        )
        # Both languages should have at least one entry per key — the
        # file currently contains EN + FR packs side by side.
        assert html.count("silentguard_disabled_user_off:") >= 2
        assert html.count("silentguard_disabled_host_off:") >= 2


# ── Enable/Disable/Retry endpoint wiring ────────────────────────────
#
# The Settings card's primary action is a state-driven button:
#   * disabled → "Enable SilentGuard"  → POST /enable
#   * connected/unavailable → "Disable" → POST /disable
# A separate Retry button appears only when the integration is enabled
# but unreachable, and POSTs to /retry. Each button surfaces the
# response payload directly so the user sees the new state without a
# follow-up Refresh round-trip.


class TestEnableEndpointWiring:
    def test_enable_label_keys_exist_in_i18n(self, html: str) -> None:
        # The state-driven label needs a calm "Enable" verb in both
        # language packs.
        for key in ("silentguard_enable", "silentguard_disable"):
            assert f"{key}:" in html, f"{key} must be defined in i18n."
            assert html.count(f"{key}:") >= 2, (
                f"{key} must be present in both EN and FR packs."
            )

    def test_toggle_handler_targets_dedicated_endpoint(
        self, script: str,
    ) -> None:
        # The new dedicated path is the primary call; the older
        # /settings POST is allowed as a fallback for forwards-compat.
        body = _function_body(script, "toggleSilentGuardEnabled")
        assert '"/integrations/silentguard/enable"' in body, (
            "toggleSilentGuardEnabled must call the dedicated /enable "
            "endpoint when opting in."
        )
        assert '"/integrations/silentguard/disable"' in body, (
            "toggleSilentGuardEnabled must call the dedicated /disable "
            "endpoint when opting out."
        )

    def test_toggle_handler_uses_credentials_include_on_dedicated_path(
        self, script: str,
    ) -> None:
        # Both dedicated endpoints must be called with credentials so
        # the cookie-only auth path stays attached.
        body = _function_body(script, "toggleSilentGuardEnabled")
        assert body.count('credentials: "include"') >= 1


class TestRetryButtonWiring:
    def test_retry_button_exists_with_documented_id(self, html: str) -> None:
        assert 'id="silentguard-retry-btn"' in html, (
            "Retry button must keep the documented id so the JS can "
            "wire and toggle its visibility."
        )

    def test_retry_button_does_not_rely_on_inline_onclick(
        self, html: str,
    ) -> None:
        retry_match = re.search(
            r'<button[^>]*id="silentguard-retry-btn"[^>]*>',
            html,
        )
        assert retry_match, "Retry button tag not found"
        assert "onclick=" not in retry_match.group(0), (
            "Retry button must use addEventListener, not inline onclick."
        )

    def test_retry_listener_calls_async_handler(self, script: str) -> None:
        assert 'getElementById("silentguard-retry-btn")' in script
        listener_block = re.search(
            r'getElementById\("silentguard-retry-btn"\)[\s\S]{0,400}?'
            r'addEventListener\("click",[\s\S]{0,400}?'
            r'retrySilentGuardStartup\(\)',
            script,
        )
        assert listener_block, (
            "Expected an explicit addEventListener('click', ...) that "
            "calls retrySilentGuardStartup() on the Retry button."
        )

    def test_retry_handler_posts_to_dedicated_endpoint(
        self, script: str,
    ) -> None:
        body = _function_body(script, "retrySilentGuardStartup")
        assert 'fetch("/integrations/silentguard/retry"' in body, (
            "retrySilentGuardStartup must POST to the dedicated /retry "
            "endpoint."
        )
        assert 'method: "POST"' in body
        assert 'credentials: "include"' in body, (
            "Retry fetch must pass credentials: 'include' so the "
            "authenticated session is attached on every retry."
        )

    def test_retry_failure_path_surfaces_visible_card_state(
        self, script: str,
    ) -> None:
        # Errors must land in the card, not in a swallowed promise.
        body = _function_body(script, "retrySilentGuardStartup")
        assert "applySilentGuardCheckFailedUI()" in body, (
            "Retry handler must route errors through "
            "applySilentGuardCheckFailedUI so failures stay visible."
        )

    def test_retry_label_key_exists_in_i18n(self, html: str) -> None:
        assert "silentguard_retry:" in html
        assert html.count("silentguard_retry:") >= 2, (
            "silentguard_retry must be defined in both EN and FR packs."
        )
