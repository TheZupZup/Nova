"""
Tests for ``core.security.silentguard_mitigation``.

The mitigation client is the *only* file in ``core.security`` allowed
to make POST calls to SilentGuard. These tests pin the safety contract
that lets it do that without breaking the rest of the package's
read-only posture:

  * the existing read-only client (``silentguard_client.py``) is left
    untouched — its forbidden-verb assertion in
    ``test_security_provider`` keeps holding;
  * every POST carries ``{"acknowledge": true}`` and refuses without it;
  * mode strings outside the allow-list normalise to ``"unknown"``;
  * malformed timestamps drop to ``None`` instead of leaking into the
    Nova response;
  * transport / decoding / non-2xx all map to calm fallback values
    (``None`` for read, ``ok=False`` for action) — no exception ever
    reaches the caller;
  * the active flag is mode-derived and never honours an
    ``active=true`` claim from SilentGuard for a mode Nova has not
    vetted.
"""

from __future__ import annotations

import httpx
import pytest

from core.security import silentguard_mitigation as mitigation_module
from core.security.silentguard_mitigation import (
    ACKNOWLEDGE_PAYLOAD,
    DEFAULT_TIMEOUT_SECONDS,
    MODE_ASK_BEFORE_BLOCKING,
    MODE_DETECTION_ONLY,
    MODE_TEMPORARY_AUTO_BLOCK,
    MODE_UNKNOWN,
    MitigationActionResult,
    MitigationState,
    PATH_MITIGATION,
    PATH_MITIGATION_DISABLE,
    PATH_MITIGATION_ENABLE_TEMPORARY,
    SilentGuardMitigationClient,
    _normalise_mode,
    _normalise_timestamp,
    _parse_state,
)


# ── Helpers / doubles ───────────────────────────────────────────────


class _RecordingTransport(httpx.BaseTransport):
    """Stub HTTP transport that records every request and returns scripted replies."""

    def __init__(self, replies):
        # ``replies`` is a list of tuples ``(status_code, json_body)``
        # consumed in order. Extra calls beyond the script raise so
        # tests notice unexpected traffic.
        self._replies = list(replies)
        self.calls: list[dict] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = None
        if request.content:
            try:
                import json
                body = json.loads(request.content.decode("utf-8"))
            except Exception:
                body = request.content
        self.calls.append({
            "method": request.method,
            "url": str(request.url),
            "path": request.url.path,
            "body": body,
            "headers": dict(request.headers),
        })
        if not self._replies:
            raise AssertionError(
                "unexpected extra HTTP call to %s %s" % (request.method, request.url)
            )
        status, payload = self._replies.pop(0)
        if isinstance(payload, str):
            return httpx.Response(status, text=payload)
        return httpx.Response(status, json=payload)


def _make_client_with_transport(transport, *, base_url="http://127.0.0.1:8765"):
    """Wire a ``SilentGuardMitigationClient`` whose calls go through ``transport``."""
    client = SilentGuardMitigationClient(base_url=base_url)

    def _open_with_transport():
        # Build an httpx.Client that uses our recording transport, but
        # keep every other knob (timeout, headers) identical to the
        # production path.
        return httpx.Client(
            base_url=client.base_url,
            timeout=client.timeout_seconds,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            transport=transport,
        )

    client._open = _open_with_transport  # type: ignore[assignment]
    return client


# ── Construction / configuration ────────────────────────────────────


class TestConstruction:
    def test_empty_base_url_is_not_configured(self):
        client = SilentGuardMitigationClient(base_url="")
        assert client.is_configured() is False
        assert client.get_state() is None
        # Action calls never reach the wire when not configured.
        result = client.enable_temporary()
        assert result.ok is False
        assert result.state is None

    def test_base_url_is_normalised(self):
        client = SilentGuardMitigationClient(
            base_url="  http://127.0.0.1:8765/  ",
        )
        assert client.base_url == "http://127.0.0.1:8765"
        assert client.is_configured() is True

    def test_invalid_timeout_falls_back_to_default(self):
        # Negative, zero, and non-numeric timeouts all collapse to the
        # documented default — a hostile config cannot disable the
        # safety bound that keeps SilentGuard from wedging Nova.
        for bad in (-1, 0, None, "not-a-number"):
            c = SilentGuardMitigationClient(
                base_url="http://x", timeout_seconds=bad,
            )
            assert c.timeout_seconds == DEFAULT_TIMEOUT_SECONDS


# ── Sanitisers ──────────────────────────────────────────────────────


class TestNormaliseMode:
    @pytest.mark.parametrize("raw,expected", [
        ("detection_only", MODE_DETECTION_ONLY),
        (" Detection_Only ", MODE_DETECTION_ONLY),
        ("ask_before_blocking", MODE_ASK_BEFORE_BLOCKING),
        ("temporary_auto_block", MODE_TEMPORARY_AUTO_BLOCK),
    ])
    def test_known_modes_normalise(self, raw, expected):
        assert _normalise_mode(raw) == expected

    @pytest.mark.parametrize("raw", [
        None, "", "permanent_block", "off", 0, True, [], {}, "blocking",
    ])
    def test_unknown_modes_collapse_to_unknown(self, raw):
        assert _normalise_mode(raw) == MODE_UNKNOWN


class TestNormaliseTimestamp:
    def test_accepts_iso_8601(self):
        assert _normalise_timestamp("2026-05-10T12:34:56Z") == "2026-05-10T12:34:56Z"
        assert _normalise_timestamp("2026-05-10T12:34:56.789+00:00") == (
            "2026-05-10T12:34:56.789+00:00"
        )

    @pytest.mark.parametrize("raw", [
        None, 0, True, [], {},
        "yesterday",
        "2026-05-10",
        "2026-05-10 12:34:56",  # space, not T
        "x" * 200,              # too long
        # Non-whitespace garbage in the middle of the value must
        # collapse to ``None`` so a hostile log line cannot reach the UI.
        "2026-05-10T12:34:56Z;DROP TABLE",
        "2026-05-10T12:34:56Z<script>",
    ])
    def test_rejects_bad_input(self, raw):
        assert _normalise_timestamp(raw) is None


class TestParseState:
    def test_detection_only_payload(self):
        state = _parse_state({"mode": "detection_only"})
        assert state == MitigationState(
            mode=MODE_DETECTION_ONLY, active=False, expires_at=None,
        )

    def test_temporary_auto_block_is_active(self):
        state = _parse_state({"mode": "temporary_auto_block"})
        assert state.mode == MODE_TEMPORARY_AUTO_BLOCK
        assert state.active is True

    def test_active_flag_does_not_override_unvetted_mode(self):
        # SilentGuard claims active=True on a mode Nova has not vetted.
        # The parser must refuse to surface "active" for an unknown
        # mode — the UI never paints "active" for an unreviewed value.
        state = _parse_state({"mode": "permanent_block", "active": True})
        assert state.mode == MODE_UNKNOWN
        assert state.active is False

    def test_active_flag_does_not_override_detection_only(self):
        # Even an explicit ``active=True`` on detection_only is
        # ignored, because detection_only is a non-active mode by
        # definition.
        state = _parse_state({"mode": "detection_only", "active": True})
        assert state.mode == MODE_DETECTION_ONLY
        assert state.active is False

    def test_expires_at_is_propagated(self):
        state = _parse_state({
            "mode": "temporary_auto_block",
            "expires_at": "2026-05-10T13:00:00Z",
        })
        assert state.expires_at == "2026-05-10T13:00:00Z"

    def test_extra_fields_are_dropped(self):
        # SilentGuard may include richer fields in the future. Until
        # they are reviewed, ``MitigationState.as_dict`` must surface
        # only the documented keys.
        state = _parse_state({
            "mode": "detection_only",
            "internal_debug": "secret",
            "blocked_subnets": ["1.2.3.0/24"],
        })
        assert state is not None
        assert set(state.as_dict().keys()) == {"mode", "active", "expires_at"}

    def test_non_dict_payload_returns_none(self):
        assert _parse_state(None) is None
        assert _parse_state([]) is None
        assert _parse_state("detection_only") is None
        assert _parse_state(42) is None


# ── Read path ───────────────────────────────────────────────────────


class TestGetState:
    def test_returns_parsed_state_on_success(self):
        transport = _RecordingTransport([
            (200, {"mode": "detection_only"}),
        ])
        client = _make_client_with_transport(transport)

        state = client.get_state()
        assert state == MitigationState(
            mode=MODE_DETECTION_ONLY, active=False, expires_at=None,
        )
        assert len(transport.calls) == 1
        assert transport.calls[0]["method"] == "GET"
        assert transport.calls[0]["path"] == PATH_MITIGATION
        # No body on the read path.
        assert transport.calls[0]["body"] in (None, b"")

    def test_non_2xx_returns_none(self):
        transport = _RecordingTransport([(503, {"error": "down"})])
        client = _make_client_with_transport(transport)
        assert client.get_state() is None

    def test_non_json_body_returns_none(self):
        transport = _RecordingTransport([(200, "not json")])
        client = _make_client_with_transport(transport)
        assert client.get_state() is None

    def test_transport_error_returns_none(self):
        class _Boom(httpx.BaseTransport):
            def handle_request(self, request):
                raise httpx.ConnectError("refused", request=request)

        client = _make_client_with_transport(_Boom())
        assert client.get_state() is None


# ── Write paths ─────────────────────────────────────────────────────


class TestEnableTemporary:
    def test_sends_acknowledge_payload(self):
        transport = _RecordingTransport([
            (200, {"mode": "temporary_auto_block"}),
        ])
        client = _make_client_with_transport(transport)

        result = client.enable_temporary()
        assert isinstance(result, MitigationActionResult)
        assert result.ok is True
        assert result.state is not None
        assert result.state.mode == MODE_TEMPORARY_AUTO_BLOCK
        # The single call carried the acknowledgement payload exactly.
        assert len(transport.calls) == 1
        call = transport.calls[0]
        assert call["method"] == "POST"
        assert call["path"] == PATH_MITIGATION_ENABLE_TEMPORARY
        assert call["body"] == ACKNOWLEDGE_PAYLOAD

    def test_failure_returns_calm_result(self):
        transport = _RecordingTransport([(500, {"error": "boom"})])
        client = _make_client_with_transport(transport)

        result = client.enable_temporary()
        assert result.ok is False
        assert result.state is None
        # The message must be user-safe — no raw HTTP body or
        # exception text. We only assert it is a non-empty string.
        assert isinstance(result.message, str) and result.message

    def test_unconfigured_client_returns_calm_failure(self):
        client = SilentGuardMitigationClient(base_url="")
        result = client.enable_temporary()
        assert result.ok is False
        assert result.state is None

    def test_transport_error_returns_calm_result(self):
        class _Boom(httpx.BaseTransport):
            def handle_request(self, request):
                raise httpx.ReadTimeout("too slow", request=request)

        client = _make_client_with_transport(_Boom())
        result = client.enable_temporary()
        assert result.ok is False
        assert result.state is None


class TestDisable:
    def test_sends_acknowledge_payload(self):
        transport = _RecordingTransport([
            (200, {"mode": "detection_only"}),
        ])
        client = _make_client_with_transport(transport)

        result = client.disable()
        assert result.ok is True
        assert result.state is not None
        assert result.state.mode == MODE_DETECTION_ONLY
        assert len(transport.calls) == 1
        call = transport.calls[0]
        assert call["method"] == "POST"
        assert call["path"] == PATH_MITIGATION_DISABLE
        assert call["body"] == ACKNOWLEDGE_PAYLOAD

    def test_failure_returns_calm_result(self):
        transport = _RecordingTransport([(503, {"error": "down"})])
        client = _make_client_with_transport(transport)

        result = client.disable()
        assert result.ok is False
        assert result.state is None
        assert isinstance(result.message, str) and result.message


# ── Module surface ──────────────────────────────────────────────────


class TestModuleSurface:
    def test_acknowledge_payload_is_immutable_shape(self):
        # The acknowledgement payload is fixed: exactly
        # ``{"acknowledge": True}``. The client must not be able to
        # smuggle in an alternate shape.
        assert ACKNOWLEDGE_PAYLOAD == {"acknowledge": True}

    def test_only_three_paths_are_exposed(self):
        # The mitigation surface stays tiny by design. Adding a new
        # path is a deliberate review — this test fails loudly so
        # whoever adds one knows to update the docs and tests.
        assert PATH_MITIGATION == "/mitigation"
        assert PATH_MITIGATION_ENABLE_TEMPORARY == "/mitigation/enable-temporary"
        assert PATH_MITIGATION_DISABLE == "/mitigation/disable"

    def test_module_does_not_define_an_unblock_helper(self):
        # The roadmap mentions ``POST /blocked/{ip}/unblock`` as a
        # later-only addition. This PR must not ship a helper for it;
        # adding one needs its own review.
        assert not hasattr(
            mitigation_module, "unblock_ip",
        ), "unblock helper is intentionally out of scope for this PR"
        assert not hasattr(
            mitigation_module.SilentGuardMitigationClient, "unblock_ip",
        )
