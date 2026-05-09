"""
Tests for the read-only security context block.

The block is what Nova injects into the chat system prompt so the
model can accurately know whether SilentGuard is configured,
reachable, or reporting basic state. These tests pin the contract:

  * the block is short, deterministic, and read-only;
  * it states "not configured" for the null provider, for SilentGuard
    with no file / no API, and when the provider misbehaves;
  * it states "read-only API is unavailable" when configured but
    offline;
  * it states "connected in read-only mode" when reachable, and
    surfaces zero-or-more counts when the optional ``get_summary_counts``
    method is available;
  * it never includes raw payloads, exception text, IPs, process
    names, or timestamps;
  * it always restates that Nova may explain but must not perform
    firewall or rule actions;
  * malformed counts dicts degrade to the count-less wording rather
    than crashing or leaking the bad shape;
  * the chat ``build_messages`` path appends the block after the time
    context and never duplicates it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# Heavy network deps the chat module imports at module load. Stub them
# before the import so a missing wheel never blocks this test file.
for _mod in ("ddgs", "ollama"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


from core.security import (  # noqa: E402
    NullSecurityProvider,
    SecurityStatus,
    SilentGuardProvider,
    STATE_AVAILABLE,
    STATE_OFFLINE,
    STATE_UNAVAILABLE,
    build_security_context_block,
)
from core.security.context import _BEHAVIOR_LINE, _HEADER  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_silentguard_env(monkeypatch):
    """Keep host env vars from leaking into provider behaviour."""
    monkeypatch.delenv("NOVA_SILENTGUARD_PATH", raising=False)
    monkeypatch.delenv("NOVA_SILENTGUARD_API_URL", raising=False)
    monkeypatch.delenv("NOVA_SILENTGUARD_API_TIMEOUT_SECONDS", raising=False)


class _FakeStatusClient:
    """Stand-in client used by provider-with-counts tests."""

    def __init__(
        self,
        status_payload=None,
        alerts=None,
        blocked=None,
        trusted=None,
        connections=None,
        connection_summary=None,
        base_url="http://stub",
    ):
        self._status = status_payload
        self._alerts = alerts or []
        self._blocked = blocked or []
        self._trusted = trusted or []
        self._connections = connections or []
        self._connection_summary = connection_summary
        self.base_url = base_url

    def get_status(self):
        return self._status

    def get_alerts(self):
        return list(self._alerts)

    def get_blocked(self):
        return list(self._blocked)

    def get_trusted(self):
        return list(self._trusted)

    def get_connections(self):
        return list(self._connections)

    def get_connections_summary(self):
        return self._connection_summary


# ── No provider / null provider ─────────────────────────────────────

class TestNullProvider:
    def test_default_argument_yields_not_configured(self):
        """No argument falls through to the null default provider."""
        block = build_security_context_block()
        assert block.startswith(_HEADER)
        assert "not configured" in block
        assert _BEHAVIOR_LINE in block

    def test_null_provider_says_silentguard_not_configured(self):
        block = build_security_context_block(NullSecurityProvider())
        # Must reference SilentGuard explicitly so a user reading the
        # prompt understands what is missing.
        assert "SilentGuard integration: not configured." in block
        assert _BEHAVIOR_LINE in block

    def test_block_is_short_when_unconfigured(self):
        block = build_security_context_block(NullSecurityProvider())
        # Header + two bullets: should be at most a small handful of
        # lines, so unconfigured installs pay near-zero token cost.
        assert len(block.splitlines()) <= 4


# ── SilentGuardProvider — file transport (no API URL) ────────────────

class TestFileTransportContext:
    def test_missing_file_says_not_configured(self, tmp_path):
        provider = SilentGuardProvider(feed_path=tmp_path / "nope.json")
        block = build_security_context_block(provider)
        assert "SilentGuard integration: not configured." in block
        assert _BEHAVIOR_LINE in block
        # No leak of the resolved path or any other detail.
        assert str(tmp_path) not in block

    def test_present_file_says_connected(self, tmp_path):
        feed = tmp_path / "feed.json"
        feed.write_text("[]", encoding="utf-8")
        provider = SilentGuardProvider(feed_path=feed)
        block = build_security_context_block(provider)
        assert "connected in read-only mode" in block
        assert _BEHAVIOR_LINE in block
        # File transport has no counts surface; the block must not
        # invent a "Current summary:" line.
        assert "Current summary" not in block
        # And must not leak the file path into the prompt.
        assert str(feed) not in block

    def test_offline_says_unavailable(self, tmp_path, monkeypatch):
        provider = SilentGuardProvider(feed_path=Path("/no/such/path.json"))

        def boom(_self):
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "is_file", boom)
        block = build_security_context_block(provider)
        assert "read-only API is unavailable" in block
        assert _BEHAVIOR_LINE in block

    def test_block_never_includes_exception_text(self, tmp_path, monkeypatch):
        provider = SilentGuardProvider(feed_path=Path("/no/such/path.json"))

        def boom(_self):
            raise OSError("super-secret host detail")

        monkeypatch.setattr(Path, "is_file", boom)
        block = build_security_context_block(provider)
        assert "super-secret" not in block
        assert "OSError" not in block
        assert "Traceback" not in block


# ── SilentGuardProvider — HTTP transport with counts ────────────────

class TestHttpTransportContext:
    def test_connected_with_zero_counts(self, tmp_path):
        provider = SilentGuardProvider(
            feed_path=tmp_path / "missing.json",
            client=_FakeStatusClient(
                status_payload={"ok": True},
                base_url="http://127.0.0.1:8765",
            ),
        )
        block = build_security_context_block(provider)
        assert "connected in read-only mode" in block
        assert (
            "Current summary: 0 alerts, 0 blocked items, "
            "0 trusted items, 0 active connections."
        ) in block
        assert _BEHAVIOR_LINE in block
        # API URL must not leak into the prompt.
        assert "127.0.0.1" not in block

    def test_connected_with_nonzero_counts(self, tmp_path):
        provider = SilentGuardProvider(
            feed_path=tmp_path / "missing.json",
            client=_FakeStatusClient(
                status_payload={"ok": True},
                alerts=[{"id": 1}, {"id": 2}],
                blocked=[{"ip": "1.1.1.1"}],
                trusted=[{"ip": "9.9.9.9"}, {"ip": "8.8.8.8"}, {"ip": "1.0.0.1"}],
                connections=[{"ip": "5.6.7.8"}, {"ip": "1.2.3.4"}],
                base_url="http://127.0.0.1:8765",
            ),
        )
        block = build_security_context_block(provider)
        assert (
            "Current summary: 2 alerts, 1 blocked items, "
            "3 trusted items, 2 active connections."
        ) in block
        # Raw payload must not enter the prompt — only the counts do.
        for leak in ("1.1.1.1", "9.9.9.9", "8.8.8.8", "5.6.7.8", "1.2.3.4"):
            assert leak not in block

    def test_offline_api_says_unavailable(self, tmp_path):
        # API client where /status returns None → STATE_OFFLINE.
        provider = SilentGuardProvider(
            feed_path=tmp_path / "missing.json",
            client=_FakeStatusClient(
                status_payload=None,
                base_url="http://127.0.0.1:8765",
            ),
        )
        block = build_security_context_block(provider)
        assert "read-only API is unavailable" in block
        assert _BEHAVIOR_LINE in block
        assert "Current summary" not in block
        # The probed URL must not leak to the prompt.
        assert "127.0.0.1" not in block


# ── Rich /connections/summary in the prompt ─────────────────────────


class TestRichConnectionSummaryContext:
    """Tests pinning how the prompt context absorbs the optional
    ``/connections/summary`` payload SilentGuard may expose.

    The block must surface counts and short top-N lists when the
    payload is well-formed, omit fields it does not have rather than
    invent them, and degrade gracefully (no rich lines, no exception)
    when the endpoint is missing or malformed.
    """

    def test_connected_with_full_rich_summary(self, tmp_path):
        # The example wording from the integration brief — a fully
        # populated payload should render two extra bullets after the
        # basic counts line.
        provider = SilentGuardProvider(
            feed_path=tmp_path / "missing.json",
            client=_FakeStatusClient(
                status_payload={"ok": True},
                connection_summary={
                    "total": 55,
                    "local": 38,
                    "known": 12,
                    "unknown": 5,
                    "top_processes": [
                        {"name": "firefox", "count": 8},
                        {"name": "python", "count": 4},
                        {"name": "steam", "count": 3},
                    ],
                },
                base_url="http://127.0.0.1:8765",
            ),
        )
        block = build_security_context_block(provider)
        assert "connected in read-only mode" in block
        # The basic counts line is unchanged.
        assert (
            "Current summary: 0 alerts, 0 blocked items, "
            "0 trusted items, 0 active connections."
        ) in block
        # The richer summary appears as separate bullets.
        assert (
            "Connection summary: 55 active connections, 38 local, "
            "12 known, 5 unknown."
        ) in block
        assert "Top processes: firefox 8, python 4, steam 3." in block
        assert _BEHAVIOR_LINE in block

    def test_partial_summary_omits_missing_fields(self, tmp_path):
        # When SilentGuard supplies only some of the breakdown fields,
        # Nova lists the ones it has and omits the rest — never
        # filling missing values with zero or "unknown".
        provider = SilentGuardProvider(
            feed_path=tmp_path / "missing.json",
            client=_FakeStatusClient(
                status_payload={"ok": True},
                connection_summary={
                    "total": 42,
                    "local": 30,
                    # known/unknown missing on purpose
                },
                base_url="http://127.0.0.1:8765",
            ),
        )
        block = build_security_context_block(provider)
        assert "Connection summary: 42 active connections, 30 local." in block
        # Missing fields must not appear; the line never says
        # "0 known" when SilentGuard never reported a known count.
        assert "0 known" not in block
        assert "0 unknown" not in block

    def test_summary_with_only_top_processes(self, tmp_path):
        # No counts breakdown at all, only a top-processes list.
        provider = SilentGuardProvider(
            feed_path=tmp_path / "missing.json",
            client=_FakeStatusClient(
                status_payload={"ok": True},
                connection_summary={
                    "top_processes": [{"name": "firefox", "count": 8}],
                },
                base_url="http://127.0.0.1:8765",
            ),
        )
        block = build_security_context_block(provider)
        assert "Connection summary:" not in block
        assert "Top processes: firefox 8." in block

    def test_summary_renders_top_remote_hosts(self, tmp_path):
        provider = SilentGuardProvider(
            feed_path=tmp_path / "missing.json",
            client=_FakeStatusClient(
                status_payload={"ok": True},
                connection_summary={
                    "top_remote_hosts": [
                        {"host": "github.com", "count": 4},
                    ],
                },
                base_url="http://127.0.0.1:8765",
            ),
        )
        block = build_security_context_block(provider)
        assert "Top remote hosts: github.com 4." in block

    def test_endpoint_missing_falls_back_to_basic_counts(self, tmp_path):
        # The optional endpoint returns ``None`` (older SilentGuard
        # build, transport error, malformed payload). The basic counts
        # line still appears; no rich lines are added.
        provider = SilentGuardProvider(
            feed_path=tmp_path / "missing.json",
            client=_FakeStatusClient(
                status_payload={"ok": True},
                connection_summary=None,
                base_url="http://127.0.0.1:8765",
            ),
        )
        block = build_security_context_block(provider)
        assert "connected in read-only mode" in block
        assert (
            "Current summary: 0 alerts, 0 blocked items, "
            "0 trusted items, 0 active connections."
        ) in block
        assert "Connection summary:" not in block
        assert "Top processes:" not in block
        assert "Top remote hosts:" not in block

    def test_malformed_summary_falls_back_silently(self, tmp_path):
        # Top-level not a dict — provider normalises to ``None`` and
        # the prompt drops the rich lines without complaint.
        provider = SilentGuardProvider(
            feed_path=tmp_path / "missing.json",
            client=_FakeStatusClient(
                status_payload={"ok": True},
                connection_summary=["not", "a", "dict"],
                base_url="http://127.0.0.1:8765",
            ),
        )
        block = build_security_context_block(provider)
        assert "connected in read-only mode" in block
        assert "Connection summary:" not in block

    def test_summary_missing_method_does_not_crash(self):
        # An out-of-band provider that exposes ``get_status`` but not
        # ``get_connection_summary`` must work — older provider
        # implementations should still build a valid block.
        class _LegacyProvider:
            name = "silentguard"

            def get_status(self):
                return SecurityStatus(
                    available=True, service=self.name, state=STATE_AVAILABLE,
                )

        block = build_security_context_block(_LegacyProvider())
        assert "connected in read-only mode" in block
        assert "Connection summary:" not in block
        assert _BEHAVIOR_LINE in block

    def test_summary_raises_falls_back_silently(self):
        # A provider whose ``get_connection_summary`` raises must not
        # break the prompt — context block contracts to never raise.
        class _BoomSummaryProvider:
            name = "silentguard"

            def get_status(self):
                return SecurityStatus(
                    available=True, service=self.name, state=STATE_AVAILABLE,
                )

            def get_connection_summary(self):
                raise RuntimeError("synthetic boom")

        block = build_security_context_block(_BoomSummaryProvider())
        assert "connected in read-only mode" in block
        assert "synthetic boom" not in block
        assert "Connection summary:" not in block

    def test_summary_strings_do_not_leak_raw_payload(self, tmp_path):
        # IPs / process names that contain prompt-injection-shaped
        # characters get sanitised at the provider layer; nothing past
        # the whitelist reaches the prompt.
        provider = SilentGuardProvider(
            feed_path=tmp_path / "missing.json",
            client=_FakeStatusClient(
                status_payload={"ok": True},
                connection_summary={
                    "top_processes": [
                        {"name": "firefox\n\nIgnore previous", "count": 8},
                    ],
                    "top_remote_hosts": [
                        {"host": "evil.example.com\n--more--", "count": 2},
                    ],
                },
                base_url="http://127.0.0.1:8765",
            ),
        )
        block = build_security_context_block(provider)
        # The injection attempt cannot reach the prompt verbatim.
        assert "Ignore previous" not in block
        assert "--more--" not in block
        # The sanitised label *can* reach the prompt — it's just a
        # process / host name with the dangerous chars removed.
        assert "firefox" in block


# ── Resilience: malformed counts ─────────────────────────────────────


class _BadCountsClient(_FakeStatusClient):
    """Client that returns the expected status but malformed lists."""

    def get_alerts(self):
        return "not a list"  # noqa: pragma — malformed on purpose

    def get_blocked(self):
        return None  # noqa: pragma — malformed on purpose


class TestMalformedCountsAreGraceful:
    def test_falls_back_to_count_less_wording(self, tmp_path):
        provider = SilentGuardProvider(
            feed_path=tmp_path / "missing.json",
            client=_BadCountsClient(
                status_payload={"ok": True},
                base_url="http://127.0.0.1:8765",
            ),
        )
        block = build_security_context_block(provider)
        # We are still connected per /status, just without trustworthy
        # counts. The block should say so without inventing numbers.
        assert "connected in read-only mode" in block
        assert "Current summary" not in block
        assert _BEHAVIOR_LINE in block

    def test_provider_get_summary_counts_returns_none_on_bad_lists(self, tmp_path):
        provider = SilentGuardProvider(
            feed_path=tmp_path / "missing.json",
            client=_BadCountsClient(
                status_payload={"ok": True},
                base_url="http://127.0.0.1:8765",
            ),
        )
        assert provider.get_summary_counts() is None

    def test_negative_counts_dict_is_rejected(self):
        """An out-of-band ``get_summary_counts`` returning negatives is dropped."""

        class _BogusProvider:
            name = "silentguard"

            def get_status(self):
                return SecurityStatus(
                    available=True, service=self.name, state=STATE_AVAILABLE,
                )

            def get_summary_counts(self):
                return {
                    "alerts": -1, "blocked": 0, "trusted": 0, "connections": 0,
                }

        block = build_security_context_block(_BogusProvider())
        assert "connected in read-only mode" in block
        assert "Current summary" not in block

    def test_missing_keys_in_counts_dict_is_rejected(self):
        class _PartialProvider:
            name = "silentguard"

            def get_status(self):
                return SecurityStatus(
                    available=True, service=self.name, state=STATE_AVAILABLE,
                )

            def get_summary_counts(self):
                # Missing "trusted" and "connections".
                return {"alerts": 1, "blocked": 1}

        block = build_security_context_block(_PartialProvider())
        assert "connected in read-only mode" in block
        assert "Current summary" not in block

    def test_non_dict_counts_is_rejected(self):
        class _NonDictProvider:
            name = "silentguard"

            def get_status(self):
                return SecurityStatus(
                    available=True, service=self.name, state=STATE_AVAILABLE,
                )

            def get_summary_counts(self):
                return [1, 2, 3]  # type: ignore[return-value]

        block = build_security_context_block(_NonDictProvider())
        assert "connected in read-only mode" in block
        assert "Current summary" not in block


# ── Resilience: provider misbehaves ─────────────────────────────────

class TestProviderRaisesIsHandled:
    def test_status_raise_maps_to_unavailable(self):
        class _RaisingProvider:
            name = "silentguard"

            def get_status(self):
                raise RuntimeError("explosive provider")

        block = build_security_context_block(_RaisingProvider())
        assert "read-only API is unavailable" in block
        assert "explosive provider" not in block
        assert _BEHAVIOR_LINE in block

    def test_summary_counts_raise_falls_back_silently(self, tmp_path):
        class _CountsRaisingProvider:
            name = "silentguard"

            def get_status(self):
                return SecurityStatus(
                    available=True, service=self.name, state=STATE_AVAILABLE,
                )

            def get_summary_counts(self):
                raise RuntimeError("countdown")

        block = build_security_context_block(_CountsRaisingProvider())
        assert "connected in read-only mode" in block
        assert "Current summary" not in block
        assert "countdown" not in block


# ── State mapping table ─────────────────────────────────────────────

class TestStateMapping:
    @pytest.mark.parametrize(
        "state,expected_phrase",
        [
            (STATE_UNAVAILABLE, "not configured"),
            (STATE_OFFLINE, "read-only API is unavailable"),
            (STATE_AVAILABLE, "connected in read-only mode"),
        ],
    )
    def test_each_state_maps_to_documented_phrase(self, state, expected_phrase):
        class _StubProvider:
            name = "silentguard"

            def __init__(self, state):
                self._state = state

            def get_status(self):
                return SecurityStatus(
                    available=(self._state == STATE_AVAILABLE),
                    service=self.name,
                    state=self._state,
                )

        block = build_security_context_block(_StubProvider(state))
        assert expected_phrase in block
        assert _BEHAVIOR_LINE in block


# ── Wording invariants ──────────────────────────────────────────────

class TestWordingInvariants:
    def test_block_always_starts_with_header(self):
        # Across all three states.
        for provider in (
            NullSecurityProvider(),
            SilentGuardProvider(feed_path=Path("/tmp/__nope__sg.json")),
        ):
            block = build_security_context_block(provider)
            assert block.startswith(_HEADER)

    def test_block_always_includes_read_only_behavior_clause(self):
        for provider in (
            NullSecurityProvider(),
            SilentGuardProvider(feed_path=Path("/tmp/__nope__sg.json")),
        ):
            block = build_security_context_block(provider)
            assert _BEHAVIOR_LINE in block
            # Sanity check that the wording is not scary.
            assert "URGENT" not in block
            assert "ALERT" not in block.upper().replace("ALERTS", "")

    def test_block_does_not_promise_actions(self):
        for provider in (
            NullSecurityProvider(),
            SilentGuardProvider(feed_path=Path("/tmp/__nope__sg.json")),
        ):
            block = build_security_context_block(provider).lower()
            # No verbs that imply Nova will act.
            for forbidden in ("block ", "unblock", "kill ", "firewall rule",
                              "iptables", "nftables", "sudo "):
                assert forbidden not in block, (
                    f"security context must not promise the action {forbidden!r}"
                )


# ── Integration with chat.build_messages ────────────────────────────

# Bring in chat module *after* the heavy-import stubs at the top of
# the file. ``ddgs`` and ``ollama`` are stubbed; the chat module is
# safe to import.
from core import chat as chat_module  # noqa: E402
from core.chat import build_messages  # noqa: E402


class TestChatBuildMessagesAppendsBlock:
    def test_block_lands_in_system_prompt(self, tmp_path, monkeypatch):
        # Force the SilentGuard file probe to miss so the block reports
        # "not configured." We patch the env var rather than the
        # default path so the test stays independent of host state.
        monkeypatch.setenv(
            "NOVA_SILENTGUARD_PATH", str(tmp_path / "absent.json"),
        )
        msgs = build_messages([], "hi", [])
        assert msgs[0]["role"] == "system"
        sys_prompt = msgs[0]["content"]
        assert "Security context:" in sys_prompt
        assert "SilentGuard integration: not configured." in sys_prompt
        assert _BEHAVIOR_LINE in sys_prompt

    def test_block_lands_after_time_context(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "NOVA_SILENTGUARD_PATH", str(tmp_path / "absent.json"),
        )
        msgs = build_messages([], "hi", [])
        sys_prompt = msgs[0]["content"]
        # Time context block precedes the security block in the
        # assembled prompt — the security block is the last thing.
        assert sys_prompt.index("[Time context]") < sys_prompt.index(
            "Security context:"
        )

    def test_block_appears_only_once(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "NOVA_SILENTGUARD_PATH", str(tmp_path / "absent.json"),
        )
        msgs = build_messages([], "hi", [])
        # No duplicate injection across context_type branches either.
        sys_prompt = msgs[0]["content"]
        assert sys_prompt.count("Security context:") == 1

    def test_block_present_for_security_context_branch(self, tmp_path, monkeypatch):
        # Even when the chat path passes a SilentGuard summary as
        # `extra_context`, the read-only context block is still
        # appended — the two are different surfaces.
        monkeypatch.setenv(
            "NOVA_SILENTGUARD_PATH", str(tmp_path / "absent.json"),
        )
        msgs = build_messages(
            [], "any", [], extra_context="some summary text",
            context_type="security",
        )
        sys_prompt = msgs[0]["content"]
        assert "Security context:" in sys_prompt

    def test_chat_path_swallows_provider_failure(self, tmp_path, monkeypatch):
        """A misbehaving provider must not break chat prompt assembly."""

        class _Boom:
            name = "silentguard"

            def __init__(self, *_, **__):
                pass

            def get_status(self):
                raise RuntimeError("not today")

        # Patch the chat module's reference, not the security package's,
        # so the patch lands on the call site.
        monkeypatch.setattr(chat_module, "SilentGuardProvider", _Boom)
        msgs = build_messages([], "hi", [])
        sys_prompt = msgs[0]["content"]
        # The block ends up "read-only API is unavailable" via the
        # provider's safe-fail path. Either that or the block is
        # omitted entirely. Both are acceptable; the test asserts the
        # chat path never bubbled the exception.
        assert msgs[0]["role"] == "system"
        assert "[Time context]" in sys_prompt
