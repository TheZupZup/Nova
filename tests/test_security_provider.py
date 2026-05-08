"""
Tests for the read-only security provider foundation.

The provider layer (``core.security``) is intentionally narrow: it
reports whether a security tool is reachable, and it never raises.
These tests pin the contract:

  * the dataclass shape is stable (``available`` / ``service`` /
    ``state`` / ``message`` / ``timestamp``);
  * the null provider is the safe default and reports unavailable;
  * ``SilentGuardProvider`` returns the right state for present,
    missing, and unreadable paths;
  * ``get_security_context_summary`` falls back cleanly when no
    provider is wired up;
  * none of the new files import system-mutating modules.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core import security as security_pkg
from core.security import (
    NullSecurityProvider,
    SecurityProvider,
    SecurityStatus,
    SilentGuardProvider,
    STATE_AVAILABLE,
    STATE_OFFLINE,
    STATE_UNAVAILABLE,
    default_provider,
    get_security_context_summary,
)
from core.security import provider as provider_module
from core.security import silentguard as silentguard_module


# ── SecurityStatus dataclass ────────────────────────────────────────

class TestSecurityStatusShape:
    def test_required_fields(self):
        s = SecurityStatus(
            available=True,
            service="silentguard",
            state=STATE_AVAILABLE,
        )
        assert s.available is True
        assert s.service == "silentguard"
        assert s.state == STATE_AVAILABLE
        assert s.message == ""
        assert s.timestamp is None

    def test_as_dict_keys(self):
        s = SecurityStatus(
            available=False,
            service="silentguard",
            state=STATE_UNAVAILABLE,
            message="not found",
            timestamp="2026-05-08T12:00:00Z",
        )
        d = s.as_dict()
        assert set(d.keys()) == {
            "available", "service", "state", "message", "timestamp",
        }
        assert d["available"] is False
        assert d["service"] == "silentguard"
        assert d["state"] == STATE_UNAVAILABLE
        assert d["message"] == "not found"
        assert d["timestamp"] == "2026-05-08T12:00:00Z"

    def test_is_frozen(self):
        s = SecurityStatus(
            available=False, service="x", state=STATE_UNAVAILABLE,
        )
        with pytest.raises(Exception):
            s.available = True  # type: ignore[misc]


# ── NullSecurityProvider ────────────────────────────────────────────

class TestNullProvider:
    def test_status_reports_unavailable(self):
        status = NullSecurityProvider().get_status()
        assert status.available is False
        assert status.state == STATE_UNAVAILABLE
        assert status.service == "none"
        assert "no security provider" in status.message.lower()

    def test_status_has_timestamp(self):
        status = NullSecurityProvider().get_status()
        # ISO-8601 UTC with seconds, ending in Z.
        assert status.timestamp is not None
        assert status.timestamp.endswith("Z")
        assert "T" in status.timestamp

    def test_satisfies_provider_protocol(self):
        assert isinstance(NullSecurityProvider(), SecurityProvider)


# ── SilentGuardProvider ─────────────────────────────────────────────

class TestSilentGuardProviderStatus:
    def test_unavailable_when_path_missing(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NOVA_SILENTGUARD_PATH", raising=False)
        missing = tmp_path / "absent.json"
        provider = SilentGuardProvider(feed_path=missing)
        status = provider.get_status()
        assert status.available is False
        assert status.state == STATE_UNAVAILABLE
        assert status.service == "silentguard"
        assert str(missing) in status.message

    def test_available_when_file_present(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NOVA_SILENTGUARD_PATH", raising=False)
        feed = tmp_path / "feed.json"
        feed.write_text("[]", encoding="utf-8")
        provider = SilentGuardProvider(feed_path=feed)
        status = provider.get_status()
        assert status.available is True
        assert status.state == STATE_AVAILABLE
        assert status.service == "silentguard"
        assert str(feed) in status.message

    def test_explicit_path_takes_precedence_over_env(
        self, tmp_path, monkeypatch,
    ):
        env_path = tmp_path / "env.json"
        explicit = tmp_path / "explicit.json"
        explicit.write_text("[]", encoding="utf-8")
        monkeypatch.setenv("NOVA_SILENTGUARD_PATH", str(env_path))
        provider = SilentGuardProvider(feed_path=explicit)
        status = provider.get_status()
        assert status.available is True
        assert str(explicit) in status.message
        assert str(env_path) not in status.message

    def test_env_override_used_when_no_explicit_path(
        self, tmp_path, monkeypatch,
    ):
        env_feed = tmp_path / "via-env.json"
        env_feed.write_text("[]", encoding="utf-8")
        monkeypatch.setenv("NOVA_SILENTGUARD_PATH", str(env_feed))
        status = SilentGuardProvider().get_status()
        assert status.available is True
        assert str(env_feed) in status.message

    def test_default_path_is_home_relative(self, monkeypatch):
        # Without an explicit path or env override, the provider
        # resolves to a path under the user's home directory. We do
        # not require the file to exist; we only require the resolved
        # path to be the documented default.
        monkeypatch.delenv("NOVA_SILENTGUARD_PATH", raising=False)
        provider = SilentGuardProvider()
        resolved = provider._resolved_path()
        assert resolved == Path.home() / ".silentguard_memory.json"

    def test_offline_when_path_probe_raises(self, monkeypatch):
        monkeypatch.delenv("NOVA_SILENTGUARD_PATH", raising=False)
        provider = SilentGuardProvider(feed_path=Path("/nope/feed.json"))

        def boom(self):
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "is_file", boom)
        status = provider.get_status()
        assert status.available is False
        assert status.state == STATE_OFFLINE
        assert "not accessible" in status.message.lower()

    def test_satisfies_provider_protocol(self):
        assert isinstance(SilentGuardProvider(), SecurityProvider)

    def test_get_status_does_not_raise_on_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NOVA_SILENTGUARD_PATH", raising=False)
        # Should never raise even when the file is absent.
        status = SilentGuardProvider(feed_path=tmp_path / "nope.json").get_status()
        assert isinstance(status, SecurityStatus)

    def test_get_status_does_not_read_file_contents(self, tmp_path, monkeypatch):
        """Provider only stat's the file; it does not open it for reading."""
        monkeypatch.delenv("NOVA_SILENTGUARD_PATH", raising=False)
        feed = tmp_path / "feed.json"
        # An invalid JSON body would explode any parser; the provider
        # must not parse, only probe, so this still returns available.
        feed.write_text("{ definitely not json", encoding="utf-8")
        status = SilentGuardProvider(feed_path=feed).get_status()
        assert status.available is True
        assert status.state == STATE_AVAILABLE


# ── Default-provider helper / context summary ───────────────────────

class TestDefaults:
    def test_default_provider_is_null(self):
        assert isinstance(default_provider(), NullSecurityProvider)

    def test_summary_falls_back_to_null_provider(self):
        summary = get_security_context_summary()
        assert summary["available"] is False
        assert summary["state"] == STATE_UNAVAILABLE
        assert summary["service"] == "none"
        # Stable shape — same keys as SecurityStatus.as_dict().
        assert set(summary.keys()) == {
            "available", "service", "state", "message", "timestamp",
        }

    def test_summary_uses_supplied_provider(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NOVA_SILENTGUARD_PATH", raising=False)
        feed = tmp_path / "feed.json"
        feed.write_text("[]", encoding="utf-8")
        summary = get_security_context_summary(
            SilentGuardProvider(feed_path=feed),
        )
        assert summary["available"] is True
        assert summary["service"] == "silentguard"
        assert summary["state"] == STATE_AVAILABLE


# ── Read-only / safety guarantees ───────────────────────────────────

_FORBIDDEN_IMPORTS = {
    "subprocess", "socket", "shutil", "ctypes", "signal",
}


def _assert_module_is_read_only(module):
    """Match the convention used by ``test_integrations_silentguard``."""
    with open(module.__file__, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root not in _FORBIDDEN_IMPORTS, (
                    f"{module.__name__} must not import {alias.name!r}"
                )
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert root not in _FORBIDDEN_IMPORTS, (
                f"{module.__name__} must not import from {node.module!r}"
            )


class TestReadOnlyGuarantees:
    def test_provider_module_has_no_forbidden_imports(self):
        _assert_module_is_read_only(provider_module)

    def test_silentguard_module_has_no_forbidden_imports(self):
        _assert_module_is_read_only(silentguard_module)

    def test_package_init_has_no_forbidden_imports(self):
        _assert_module_is_read_only(security_pkg)

    def test_provider_does_not_mutate_feed_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NOVA_SILENTGUARD_PATH", raising=False)
        feed = tmp_path / "feed.json"
        feed.write_text("[]", encoding="utf-8")
        original_bytes = feed.read_bytes()
        original_mtime = feed.stat().st_mtime

        SilentGuardProvider(feed_path=feed).get_status()
        # And again to make sure repeated probes do not write either.
        SilentGuardProvider(feed_path=feed).get_status()

        assert feed.read_bytes() == original_bytes
        assert feed.stat().st_mtime == original_mtime

    def test_module_exports_match_dunder_all(self):
        # Sanity check: anything advertised in __all__ resolves on the
        # package, so callers that do `from core.security import X` get
        # what the docstring promises.
        for name in security_pkg.__all__:
            assert hasattr(security_pkg, name), (
                f"core.security exports {name!r} but it is missing"
            )
