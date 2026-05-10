"""
Tests for the mitigation methods on ``SilentGuardProvider``.

The provider exposes three opt-in mitigation methods on top of the
existing read-only surface:

  * ``get_mitigation_state``         — read SilentGuard's current mode.
  * ``enable_temporary_mitigation``  — relay an enable request.
  * ``disable_mitigation``           — relay a disable request.

These tests pin the safety contract:

  * the methods never raise, even when SilentGuard is unreachable or
    misbehaving;
  * the provider refuses to issue a write when its own status probe
    says SilentGuard is unavailable;
  * the read-only ``get_status`` / ``get_summary_counts`` /
    ``get_connection_summary`` paths never instantiate a mitigation
    client and never issue a POST;
  * a custom mitigation client can be injected for tests so production
    code paths stay isolated;
  * the existing read-only client (``silentguard_client.py``) still
    has zero ``.post(`` / ``.put(`` / ``.delete(`` / ``.patch(`` calls
    so the forbidden-verb assertion in ``test_security_provider``
    keeps holding.
"""

from __future__ import annotations

import ast
from pathlib import Path

from core.security import (
    MitigationActionResult,
    MitigationState,
    SilentGuardClient,
    SilentGuardProvider,
)
from core.security.silentguard_mitigation import (
    MODE_DETECTION_ONLY,
    MODE_TEMPORARY_AUTO_BLOCK,
)


# ── Test doubles ────────────────────────────────────────────────────


class _RecordingMitigationClient:
    """Records every call and returns scripted responses."""

    def __init__(
        self,
        state=None,
        enable_result=None,
        disable_result=None,
    ):
        self.state = state
        self.enable_result = enable_result or MitigationActionResult(
            ok=True,
            state=MitigationState(
                mode=MODE_TEMPORARY_AUTO_BLOCK, active=True,
            ),
            message="enabled",
        )
        self.disable_result = disable_result or MitigationActionResult(
            ok=True,
            state=MitigationState(
                mode=MODE_DETECTION_ONLY, active=False,
            ),
            message="disabled",
        )
        self.calls: list[str] = []

    def get_state(self):
        self.calls.append("get_state")
        return self.state

    def enable_temporary(self):
        self.calls.append("enable_temporary")
        return self.enable_result

    def disable(self):
        self.calls.append("disable")
        return self.disable_result


# ── Read path ───────────────────────────────────────────────────────


class TestGetMitigationState:
    def test_returns_state_when_provider_available(self, tmp_path):
        # ``feed_path`` exists so the file-based status probe reports
        # ``available=True`` without an HTTP transport.
        feed = tmp_path / "feed.json"
        feed.write_text("[]", encoding="utf-8")
        client = _RecordingMitigationClient(
            state=MitigationState(mode=MODE_DETECTION_ONLY, active=False),
        )
        provider = SilentGuardProvider(feed_path=feed, mitigation_client=client)

        state = provider.get_mitigation_state()
        assert state is not None
        assert state.mode == MODE_DETECTION_ONLY
        assert client.calls == ["get_state"]

    def test_returns_none_when_provider_unavailable(self, tmp_path):
        # Missing feed file → provider unavailable → mitigation read
        # short-circuits before issuing any HTTP call.
        client = _RecordingMitigationClient(
            state=MitigationState(mode=MODE_DETECTION_ONLY, active=False),
        )
        provider = SilentGuardProvider(
            feed_path=tmp_path / "absent.json",
            mitigation_client=client,
        )
        assert provider.get_mitigation_state() is None
        assert client.calls == []

    def test_returns_none_when_no_mitigation_client_configured(self, tmp_path):
        # No mitigation_client argument and no API URL configured →
        # provider has no way to reach SilentGuard's mitigation
        # endpoints. The read returns ``None`` calmly.
        feed = tmp_path / "feed.json"
        feed.write_text("[]", encoding="utf-8")
        provider = SilentGuardProvider(feed_path=feed, api_url="")
        assert provider.get_mitigation_state() is None

    def test_swallows_misbehaving_client(self, tmp_path):
        # A custom client that raises must not propagate the error.
        feed = tmp_path / "feed.json"
        feed.write_text("[]", encoding="utf-8")

        class _Boom:
            def get_state(self):
                raise RuntimeError("boom")

            def enable_temporary(self):  # pragma: no cover — not invoked
                raise RuntimeError("boom")

            def disable(self):  # pragma: no cover — not invoked
                raise RuntimeError("boom")

        provider = SilentGuardProvider(feed_path=feed, mitigation_client=_Boom())
        assert provider.get_mitigation_state() is None


# ── Action paths ────────────────────────────────────────────────────


class TestEnableTemporaryMitigation:
    def test_enables_when_provider_available(self, tmp_path):
        feed = tmp_path / "feed.json"
        feed.write_text("[]", encoding="utf-8")
        client = _RecordingMitigationClient()
        provider = SilentGuardProvider(feed_path=feed, mitigation_client=client)

        result = provider.enable_temporary_mitigation()
        assert result.ok is True
        assert result.state is not None
        assert result.state.mode == MODE_TEMPORARY_AUTO_BLOCK
        assert client.calls == ["enable_temporary"]

    def test_refuses_when_provider_unavailable(self, tmp_path):
        # No feed file → the provider's own status probe returns
        # unavailable. The mitigation method must refuse without
        # touching the client (no point posting into a void).
        client = _RecordingMitigationClient()
        provider = SilentGuardProvider(
            feed_path=tmp_path / "absent.json",
            mitigation_client=client,
        )
        result = provider.enable_temporary_mitigation()
        assert result.ok is False
        assert result.state is None
        assert client.calls == []

    def test_refuses_when_no_client_configured(self, tmp_path):
        # No mitigation client and no API URL → calm refusal.
        feed = tmp_path / "feed.json"
        feed.write_text("[]", encoding="utf-8")
        provider = SilentGuardProvider(feed_path=feed, api_url="")
        result = provider.enable_temporary_mitigation()
        assert result.ok is False
        assert result.state is None
        # Message must be a non-empty user-safe string.
        assert isinstance(result.message, str) and result.message

    def test_swallows_misbehaving_client(self, tmp_path):
        feed = tmp_path / "feed.json"
        feed.write_text("[]", encoding="utf-8")

        class _Boom:
            def get_state(self):  # pragma: no cover — not invoked
                raise RuntimeError("boom")

            def enable_temporary(self):
                raise RuntimeError("boom")

            def disable(self):  # pragma: no cover — not invoked
                raise RuntimeError("boom")

        provider = SilentGuardProvider(feed_path=feed, mitigation_client=_Boom())
        result = provider.enable_temporary_mitigation()
        assert result.ok is False
        assert result.state is None


class TestDisableMitigation:
    def test_disables_when_provider_available(self, tmp_path):
        feed = tmp_path / "feed.json"
        feed.write_text("[]", encoding="utf-8")
        client = _RecordingMitigationClient()
        provider = SilentGuardProvider(feed_path=feed, mitigation_client=client)

        result = provider.disable_mitigation()
        assert result.ok is True
        assert result.state is not None
        assert result.state.mode == MODE_DETECTION_ONLY
        assert client.calls == ["disable"]

    def test_refuses_when_provider_unavailable(self, tmp_path):
        client = _RecordingMitigationClient()
        provider = SilentGuardProvider(
            feed_path=tmp_path / "absent.json",
            mitigation_client=client,
        )
        result = provider.disable_mitigation()
        assert result.ok is False
        assert client.calls == []


# ── Read-only paths must not invoke mitigation ──────────────────────


class TestReadOnlyPathsStaySafe:
    def test_get_status_does_not_invoke_mitigation_client(self, tmp_path):
        # The existing status probe is the cheapest read path; it must
        # not touch the mitigation surface. We inject a client that
        # records every call and assert it stays silent.
        feed = tmp_path / "feed.json"
        feed.write_text("[]", encoding="utf-8")
        client = _RecordingMitigationClient()
        provider = SilentGuardProvider(feed_path=feed, mitigation_client=client)

        provider.get_status()
        provider.get_status()  # second call too — defence in depth
        assert client.calls == []

    def test_get_summary_text_does_not_invoke_mitigation_client(self, tmp_path):
        feed = tmp_path / "feed.json"
        feed.write_text("[]", encoding="utf-8")
        client = _RecordingMitigationClient()
        provider = SilentGuardProvider(feed_path=feed, mitigation_client=client)

        provider.get_summary_text()
        assert client.calls == []

    def test_get_summary_counts_does_not_invoke_mitigation_client(self, tmp_path):
        feed = tmp_path / "feed.json"
        feed.write_text("[]", encoding="utf-8")
        client = _RecordingMitigationClient()
        provider = SilentGuardProvider(feed_path=feed, mitigation_client=client)

        provider.get_summary_counts()
        assert client.calls == []


# ── The read-only client stays read-only ────────────────────────────


def _read_module_source(module) -> str:
    return Path(module.__file__).read_text(encoding="utf-8")


class TestReadOnlyClientForbidsWriteVerbs:
    """Pin the same forbidden-verb assertion as test_security_provider."""

    def test_no_post_put_delete_patch_calls(self):
        from core.security import silentguard_client as client_module
        source = _read_module_source(client_module)
        for verb in (
            "client.post", "client.put", "client.delete", "client.patch",
            ".post(", ".put(", ".delete(", ".patch(",
        ):
            assert verb not in source, (
                f"silentguard_client must not call {verb!r}; "
                "POST capability lives in silentguard_mitigation only."
            )


class TestMitigationModuleSurfaceIsTiny:
    """Pin the small public surface so adding to it is a deliberate review."""

    def test_module_only_defines_three_paths(self):
        # The mitigation surface stays tiny by design. Adding a
        # ``PATH_X`` constant means Nova grew a new write capability —
        # this assertion makes the addition a deliberate, reviewed
        # change rather than a quiet drive-by edit.
        from core.security import silentguard_mitigation as mitigation_module
        with open(mitigation_module.__file__, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        path_constants = []
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.startswith("PATH_"):
                    path_constants.append(target.id)
        assert sorted(path_constants) == [
            "PATH_MITIGATION",
            "PATH_MITIGATION_DISABLE",
            "PATH_MITIGATION_ENABLE_TEMPORARY",
        ], (
            "mitigation module must define exactly three writable paths; "
            "adding a fourth needs roadmap + test updates"
        )

    def test_module_does_not_import_subprocess_or_shell(self):
        # The mitigation module is allowed POST capability but must
        # never run shell commands, spawn processes, or touch the
        # firewall directly. This mirrors the package-wide ban for
        # everything except the lifecycle helper.
        from core.security import silentguard_mitigation as mitigation_module
        with open(mitigation_module.__file__, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        forbidden = {"subprocess", "shutil", "ctypes", "signal", "os.system"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root not in forbidden, (
                        f"silentguard_mitigation must not import {alias.name!r}"
                    )
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                assert root not in forbidden, (
                    f"silentguard_mitigation must not import from {node.module!r}"
                )


# ── Sanity: read-only client construction is unchanged ──────────────


class TestReadOnlyClientUnchanged:
    def test_construction_signature_unchanged(self):
        # The existing client still constructs the same way it did
        # before — the mitigation work must not have leaked write
        # capability through the read-only client's public API.
        client = SilentGuardClient(base_url="http://127.0.0.1:8765")
        # ``is_configured`` and read methods must still exist.
        assert client.is_configured() is True
        assert callable(client.get_status)
        assert callable(client.get_alerts)
        # And no enable / disable / mitigation methods snuck in.
        for forbidden in (
            "enable_temporary", "disable", "get_mitigation_state",
            "post", "put", "delete", "patch",
        ):
            assert not hasattr(client, forbidden), (
                f"read-only client unexpectedly grew {forbidden!r}"
            )
