"""
Static-asset wiring tests for the SilentGuard mitigation block.

The Settings card grows a small mitigation surface in this PR:

  * a mode badge that shows the current SilentGuard mitigation mode;
  * three buttons — *Enable temporary mitigation*, *Keep detection
    only*, *Disable mitigation* — that surface based on the mode;
  * an inline confirmation prompt that gates every mutating call;
  * an i18n string set covering the calm wording.

These tests parse ``static/index.html`` and pin the structural
contract so a renaming or accidental drop of an id is caught here
rather than in the browser. They are deliberately structural — the
JS is not executed.

Pinned commitments:

  * every documented element id exists in the markup;
  * the click handlers are attached via explicit listeners (not inline
    ``onclick="..."``), matching the existing SilentGuard wiring tests;
  * the enable / disable handlers send the SilentGuard acknowledgement
    payload (``{"acknowledge": true}``);
  * a single click on Enable / Disable does *not* call SilentGuard:
    it only opens the confirmation prompt;
  * the read endpoint URL is the documented one;
  * "Keep detection only" never calls any endpoint;
  * the calm wording strings exist in both supported locales.
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
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.S)
    assert blocks, "expected at least one inline <script> in index.html"
    return max(blocks, key=len)


def _function_body(script: str, name: str) -> str:
    """Return the source of a top-level JS function in ``script``."""
    decl = re.search(
        r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*{",
        script,
    )
    assert decl, f"function {name!r} not found in script"
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
                return script[decl.start():i + 1]
        i += 1
    raise AssertionError(f"unterminated function body for {name!r}")


# ── Markup ──────────────────────────────────────────────────────────


REQUIRED_IDS = (
    "silentguard-mitigation-block",
    "silentguard-mitigation-mode-label",
    "silentguard-mitigation-mode-badge",
    "silentguard-mitigation-explainer",
    "silentguard-mitigation-actions",
    "silentguard-mitigation-enable-btn",
    "silentguard-mitigation-keep-btn",
    "silentguard-mitigation-disable-btn",
    "silentguard-mitigation-confirm",
    "silentguard-mitigation-confirm-text",
    "silentguard-mitigation-confirm-yes",
    "silentguard-mitigation-confirm-no",
    "silentguard-mitigation-status",
)


class TestMarkup:
    @pytest.mark.parametrize("element_id", REQUIRED_IDS)
    def test_required_id_present(self, html: str, element_id: str) -> None:
        assert f'id="{element_id}"' in html, (
            f"mitigation block must keep the {element_id!r} id; "
            "renaming it silently breaks the JS controller"
        )

    def test_block_starts_hidden(self, html: str) -> None:
        # The block must render hidden until SilentGuard has supplied
        # a real mitigation state. Painting "Enable temporary
        # mitigation" before the integration is connected would be
        # misleading.
        match = re.search(
            r'<div[^>]*id="silentguard-mitigation-block"[^>]*>',
            html,
        )
        assert match, "mitigation block element not found"
        assert "display:none" in match.group(0), (
            "mitigation block must start hidden — only paint after "
            "the read endpoint returns a recognised state"
        )

    def test_buttons_do_not_use_inline_onclick(self, html: str) -> None:
        for btn_id in (
            "silentguard-mitigation-enable-btn",
            "silentguard-mitigation-keep-btn",
            "silentguard-mitigation-disable-btn",
            "silentguard-mitigation-confirm-yes",
            "silentguard-mitigation-confirm-no",
        ):
            match = re.search(
                rf'<button[^>]*id="{btn_id}"[^>]*>',
                html,
            )
            assert match, f"button {btn_id!r} not found"
            assert "onclick=" not in match.group(0), (
                f"{btn_id} must use addEventListener wiring, not inline onclick"
            )


# ── JS controller ───────────────────────────────────────────────────


class TestController:
    def test_refresh_function_calls_documented_endpoint(self, script: str) -> None:
        body = _function_body(script, "refreshSilentGuardMitigation")
        assert "/integrations/silentguard/mitigation" in body
        # Read path must use GET — there's no need to spell GET
        # explicitly because ``fetch`` defaults to it, but a stray
        # ``method: "POST"`` would mean the read silently mutates.
        assert 'method: "POST"' not in body
        # And it must include the bearer token so the auth gate fires.
        assert "Authorization" in body
        assert "Bearer" in body

    def test_execute_action_uses_acknowledge_payload(self, script: str) -> None:
        body = _function_body(script, "executeSilentGuardMitigationAction")
        # Both write paths are referenced.
        assert "/integrations/silentguard/mitigation/enable-temporary" in body
        assert "/integrations/silentguard/mitigation/disable" in body
        # Body literally contains ``acknowledge: true`` so the
        # SilentGuard / Nova acknowledgement contract is honoured.
        assert "acknowledge: true" in body
        # Method must be POST.
        assert 'method: "POST"' in body
        # And the bearer token must travel with the request.
        assert "Authorization" in body

    def test_show_confirm_does_not_make_network_call(self, script: str) -> None:
        # Clicking Enable / Disable opens the confirmation prompt; it
        # must not fetch anything — that would defeat the explicit
        # confirmation step.
        body = _function_body(script, "showSilentGuardMitigationConfirm")
        assert "fetch(" not in body, (
            "showSilentGuardMitigationConfirm must not call fetch — "
            "the confirmation step gates every mutation"
        )

    def test_keep_detection_only_does_not_call_endpoint(self, script: str) -> None:
        # The "Keep detection only" affordance is purely a UI ack —
        # detection-only is the default mode, so there is nothing to
        # call. A future change that turns this into a network call
        # is a *new* mitigation capability and must be reviewed.
        body = _function_body(script, "acknowledgeSilentGuardKeepDetectionOnly")
        assert "fetch(" not in body, (
            "Keep detection only must not call any endpoint"
        )
        assert "/mitigation" not in body

    def test_apply_ui_falls_back_to_hidden_on_unavailable(
        self, script: str,
    ) -> None:
        body = _function_body(script, "applySilentGuardMitigationUI")
        # The function must explicitly hide the block on a
        # non-available payload — never paint a stale active state.
        assert 'block.style.display = "none"' in body

    def test_refresh_runs_after_a_settings_open_or_refresh(
        self, script: str,
    ) -> None:
        # The mitigation refresh piggybacks on the existing Settings
        # refresh trigger set: opening Settings or clicking Refresh.
        body = _function_body(script, "refreshSilentGuardStatus")
        assert "refreshSilentGuardMitigation" in body, (
            "refreshSilentGuardStatus must also pull the mitigation "
            "state so the Settings card stays consistent"
        )


# ── i18n ────────────────────────────────────────────────────────────


class TestI18n:
    REQUIRED_KEYS = (
        "silentguard_mitigation_label",
        "silentguard_mitigation_mode_detection_only",
        "silentguard_mitigation_mode_ask_before_blocking",
        "silentguard_mitigation_mode_temporary_auto_block",
        "silentguard_mitigation_mode_unknown",
        "silentguard_mitigation_explainer_detection_only",
        "silentguard_mitigation_explainer_ask_before_blocking",
        "silentguard_mitigation_explainer_temporary_auto_block",
        "silentguard_mitigation_explainer_unknown",
        "silentguard_mitigation_enable",
        "silentguard_mitigation_keep",
        "silentguard_mitigation_disable",
        "silentguard_mitigation_confirm_yes",
        "silentguard_mitigation_confirm_no",
        "silentguard_mitigation_confirm_enable",
        "silentguard_mitigation_confirm_disable",
        "silentguard_mitigation_keep_ack",
        "silentguard_mitigation_status_unavailable",
        "silentguard_mitigation_status_failed",
        "silentguard_mitigation_status_enabled",
        "silentguard_mitigation_status_disabled",
        "silentguard_mitigation_expires_at",
    )

    @pytest.mark.parametrize("key", REQUIRED_KEYS)
    def test_key_exists_in_both_locales(self, html: str, key: str) -> None:
        # Each translation block must define every key. We assert by
        # counting occurrences — at minimum two definitions (one per
        # locale). A single occurrence usually means a key was
        # added in only one locale, which leaves the other showing
        # the raw key in the UI.
        occurrences = re.findall(rf"\b{re.escape(key)}\s*:", html)
        assert len(occurrences) >= 2, (
            f"i18n key {key!r} must be defined in both fr and en; "
            f"found {len(occurrences)} occurrences"
        )

    def test_calm_wording_avoids_alarming_language(self, html: str) -> None:
        # Calm UX over flashy alerts, per the roadmap §4.5. The
        # mitigation copy must not contain alarm words. We pull only
        # the mitigation translation lines (anchored on the
        # ``silentguard_mitigation_`` prefix) so unrelated CSS or id
        # tokens like ``--danger`` cannot trip the assertion.
        mitigation_lines = re.findall(
            r"silentguard_mitigation_[a-z_]+\s*:\s*\"([^\"]*)\"",
            html,
        )
        assert mitigation_lines, "no mitigation i18n lines found"
        joined = " ".join(mitigation_lines).lower()
        for forbidden in ("under attack", "alert!", "danger", "urgent"):
            assert forbidden not in joined, (
                f"{forbidden!r} appears in mitigation copy; the "
                "wording must stay calm and non-alarming"
            )
