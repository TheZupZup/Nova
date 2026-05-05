"""
Tests for the SilentGuard integration switch.

The integration is a thin wrapper over ``core.security_feed`` whose only
job is to gate the existing parser behind a per-user setting and report
a structured status. These tests cover the gating, the status states,
and the read-only / never-crash guarantees demanded by the integration
contract.
"""

from __future__ import annotations

import ast
import json
import sqlite3

import pytest

from core import memory as core_memory, settings as core_settings, users
from core.integrations import silentguard as sg
from memory import store as natural_store


# ── Fixtures ────────────────────────────────────────────────────────

@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "nova.db")
    monkeypatch.setattr(core_memory, "DB_PATH", path)
    monkeypatch.setattr(natural_store, "DB_PATH", path)
    core_memory.initialize_db()
    return path


@pytest.fixture
def user_id(db_path):
    with sqlite3.connect(db_path) as conn:
        return users.create_user(conn, "alice", "pw")


def _enable(user_id):
    core_settings.save_user_setting(user_id, "silentguard_enabled", "true")


def _write_feed(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


# ── Default state ───────────────────────────────────────────────────

class TestDisabledByDefault:
    def test_is_enabled_false_for_new_user(self, user_id):
        assert sg.is_enabled(user_id) is False

    def test_status_disabled_by_default(self, user_id):
        s = sg.status(user_id)
        assert s.enabled is False
        assert s.state == sg.STATE_DISABLED

    def test_recent_events_empty_when_disabled(self, user_id, tmp_path, monkeypatch):
        path = tmp_path / "feed.json"
        _write_feed(path, [
            {"ip": "1.2.3.4", "process": "x", "trust": "Unknown"},
        ])
        monkeypatch.setenv("NOVA_SILENTGUARD_PATH", str(path))
        assert sg.recent_events(user_id) == []

    def test_summary_none_when_disabled(self, user_id, tmp_path, monkeypatch):
        path = tmp_path / "feed.json"
        _write_feed(path, [
            {"ip": "5.6.7.8", "process": "x", "trust": "Unknown"},
        ])
        monkeypatch.setenv("NOVA_SILENTGUARD_PATH", str(path))
        assert sg.recent_events_summary(user_id) is None


# ── Enabled state ───────────────────────────────────────────────────

class TestEnabledStatus:
    def test_status_not_found_when_file_missing(self, user_id, tmp_path, monkeypatch):
        missing = tmp_path / "nope.json"
        monkeypatch.setenv("NOVA_SILENTGUARD_PATH", str(missing))
        _enable(user_id)
        s = sg.status(user_id)
        assert s.enabled is True
        assert s.state == sg.STATE_NOT_FOUND
        assert str(missing) in s.detail

    def test_status_connected_when_file_present(self, user_id, tmp_path, monkeypatch):
        path = tmp_path / "feed.json"
        _write_feed(path, [])
        monkeypatch.setenv("NOVA_SILENTGUARD_PATH", str(path))
        _enable(user_id)
        s = sg.status(user_id)
        assert s.enabled is True
        assert s.state == sg.STATE_CONNECTED
        assert s.detail == str(path)

    def test_status_as_dict_shape(self, user_id):
        s = sg.status(user_id).as_dict()
        assert set(s.keys()) == {"name", "enabled", "state", "detail"}
        assert s["name"] == "silentguard"


class TestEnabledReads:
    def test_recent_events_returns_parsed_entries(
        self, user_id, tmp_path, monkeypatch,
    ):
        path = tmp_path / "feed.json"
        _write_feed(path, [
            {"ip": "1.2.3.4", "process": "curl", "port": 443, "trust": "Known"},
            {"ip": "5.6.7.8", "process": "rogue", "port": 9999, "trust": "Unknown"},
        ])
        monkeypatch.setenv("NOVA_SILENTGUARD_PATH", str(path))
        _enable(user_id)
        events = sg.recent_events(user_id)
        assert len(events) == 2
        assert {e.ip for e in events} == {"1.2.3.4", "5.6.7.8"}

    def test_summary_includes_unknowns(self, user_id, tmp_path, monkeypatch):
        path = tmp_path / "feed.json"
        _write_feed(path, [
            {"ip": "9.9.9.9", "process": "rogue", "port": 4444, "trust": "Unknown"},
        ])
        monkeypatch.setenv("NOVA_SILENTGUARD_PATH", str(path))
        _enable(user_id)
        summary = sg.recent_events_summary(user_id)
        assert summary is not None
        assert "9.9.9.9" in summary

    def test_recent_events_empty_when_file_missing(
        self, user_id, tmp_path, monkeypatch,
    ):
        missing = tmp_path / "nope.json"
        monkeypatch.setenv("NOVA_SILENTGUARD_PATH", str(missing))
        _enable(user_id)
        # Enabled but no file → safe empty result, no exception.
        assert sg.recent_events(user_id) == []
        assert sg.recent_events_summary(user_id) is None

    def test_recent_events_empty_on_invalid_json(
        self, user_id, tmp_path, monkeypatch,
    ):
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        monkeypatch.setenv("NOVA_SILENTGUARD_PATH", str(path))
        _enable(user_id)
        assert sg.recent_events(user_id) == []


# ── Read-only / safety guarantees ───────────────────────────────────

class TestReadOnlyGuarantees:
    def test_no_subprocess_or_socket_imports(self):
        """The integration module must not import system-mutating modules."""
        with open(sg.__file__, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())

        forbidden = {"subprocess", "socket", "shutil", "ctypes", "signal"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root not in forbidden, (
                        f"silentguard integration must not import {alias.name!r}"
                    )
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                assert root not in forbidden, (
                    f"silentguard integration must not import from {node.module!r}"
                )

    def test_feed_file_is_not_mutated(
        self, user_id, tmp_path, monkeypatch,
    ):
        path = tmp_path / "feed.json"
        original = [
            {"ip": "1.2.3.4", "process": "curl", "port": 443, "trust": "Known"},
        ]
        _write_feed(path, original)
        original_bytes = path.read_bytes()
        monkeypatch.setenv("NOVA_SILENTGUARD_PATH", str(path))
        _enable(user_id)

        sg.recent_events(user_id)
        sg.recent_events_summary(user_id)
        sg.status(user_id)

        assert path.read_bytes() == original_bytes
