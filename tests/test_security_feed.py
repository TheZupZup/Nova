import json

import pytest

from core import security_feed
from core.security_feed import (
    SecurityEvent,
    TRUST_KNOWN,
    TRUST_LOCAL,
    TRUST_UNKNOWN,
    format_security_summary,
    get_recent_security_events,
    get_security_context,
    is_security_query,
    summarize_events,
)


def _write_feed(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_missing_file_returns_empty(tmp_path):
    missing = tmp_path / "nope.json"
    assert get_recent_security_events(path=missing) == []
    assert get_security_context(path=missing) is None


def test_invalid_json_returns_empty(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    assert get_recent_security_events(path=path) == []


def test_parses_list_of_events(tmp_path):
    path = tmp_path / "feed.json"
    _write_feed(path, [
        {"ip": "1.2.3.4", "process": "curl", "port": 443, "trust": "Known"},
        {"ip": "10.0.0.5", "process": "node", "port": 8080, "trust": "Local"},
        {"ip": "5.6.7.8", "process": "rogue", "port": 9999, "trust": "Unknown"},
    ])
    events = get_recent_security_events(path=path)
    assert len(events) == 3
    assert {e.trust for e in events} == {TRUST_KNOWN, TRUST_LOCAL, TRUST_UNKNOWN}
    assert events[0].port == 443
    assert events[2].is_unknown


def test_parses_dict_wrapper(tmp_path):
    path = tmp_path / "feed.json"
    _write_feed(path, {"events": [
        {"ip": "1.1.1.1", "process": "ping", "port": 0, "trust": "Known"},
    ]})
    events = get_recent_security_events(path=path)
    assert len(events) == 1
    assert events[0].ip == "1.1.1.1"


def test_alternative_field_names(tmp_path):
    path = tmp_path / "feed.json"
    _write_feed(path, [
        {"remote_ip": "9.9.9.9", "command": "ssh", "remote_port": 22, "status": "known"},
    ])
    events = get_recent_security_events(path=path)
    assert len(events) == 1
    assert events[0].ip == "9.9.9.9"
    assert events[0].process == "ssh"
    assert events[0].port == 22
    assert events[0].trust == TRUST_KNOWN


def test_invalid_entries_are_skipped(tmp_path):
    path = tmp_path / "feed.json"
    _write_feed(path, [
        "not a dict",
        {"process": "no-ip"},
        {"ip": "  ", "process": "blank"},
        {"ip": "8.8.8.8", "process": "dig"},
    ])
    events = get_recent_security_events(path=path)
    assert len(events) == 1
    assert events[0].ip == "8.8.8.8"


def test_unknown_trust_defaults(tmp_path):
    path = tmp_path / "feed.json"
    _write_feed(path, [{"ip": "1.2.3.4", "process": "x", "trust": "weird-value"}])
    events = get_recent_security_events(path=path)
    assert events[0].trust == TRUST_UNKNOWN


def test_limit_returns_tail(tmp_path):
    path = tmp_path / "feed.json"
    payload = [
        {"ip": f"10.0.0.{i}", "process": "p", "trust": "Known"} for i in range(20)
    ]
    _write_feed(path, payload)
    events = get_recent_security_events(limit=5, path=path)
    assert len(events) == 5
    assert [e.ip for e in events] == [f"10.0.0.{i}" for i in range(15, 20)]


def test_oversized_file_is_ignored(tmp_path, monkeypatch):
    path = tmp_path / "feed.json"
    _write_feed(path, [{"ip": "1.2.3.4", "process": "x", "trust": "Known"}])
    monkeypatch.setattr(security_feed, "_MAX_FEED_BYTES", 1)
    assert get_recent_security_events(path=path) == []


def test_summarize_empty():
    summary = summarize_events([])
    assert summary["total"] == 0
    assert summary["unknown_groups"] == []
    assert summary["anomalies"] == []


def test_summarize_groups_unknowns():
    events = [
        SecurityEvent(ip="6.6.6.6", process="rogue", port=4444, trust=TRUST_UNKNOWN),
        SecurityEvent(ip="6.6.6.6", process="rogue", port=4445, trust=TRUST_UNKNOWN),
        SecurityEvent(ip="1.1.1.1", process="curl", port=443, trust=TRUST_KNOWN),
    ]
    summary = summarize_events(events)
    assert summary["total"] == 3
    assert summary["unknown_count"] == 2
    assert summary["known_count"] == 1
    assert summary["unknown_groups"][0]["ip"] == "6.6.6.6"
    assert summary["unknown_groups"][0]["count"] == 2
    assert summary["unknown_groups"][0]["ports"] == [4444, 4445]


def test_summarize_flags_repeated_unknown_anomaly():
    events = [
        SecurityEvent(ip="6.6.6.6", process="rogue", port=80, trust=TRUST_UNKNOWN)
        for _ in range(5)
    ]
    summary = summarize_events(events)
    assert any("6.6.6.6" in a and "5" in a for a in summary["anomalies"])


def test_summarize_flags_high_port_anomaly():
    events = [
        SecurityEvent(ip="7.7.7.7", process="rogue", port=60001, trust=TRUST_UNKNOWN),
    ]
    summary = summarize_events(events)
    assert any("60001" in a for a in summary["anomalies"])


def test_format_summary_includes_readonly_disclaimer():
    events = [
        SecurityEvent(ip="6.6.6.6", process="rogue", port=4444, trust=TRUST_UNKNOWN),
    ]
    rendered = format_security_summary(summarize_events(events))
    assert "Lecture seule" in rendered
    assert "6.6.6.6" in rendered


def test_format_summary_empty_message():
    assert "Aucun" in format_security_summary(summarize_events([]))


@pytest.mark.parametrize("query", [
    "Show me suspicious connections",
    "Analyze unknown IPs",
    "Explain this process",
    "connexions suspectes ?",
    "que dit silentguard",
])
def test_is_security_query_matches(query):
    assert is_security_query(query)


@pytest.mark.parametrize("query", [
    "what's the weather",
    "tell me a joke",
    "qui est le premier ministre",
    "",
])
def test_is_security_query_negatives(query):
    assert not is_security_query(query)


def test_is_security_query_handles_non_string():
    assert not is_security_query(None)
    assert not is_security_query(42)


def test_get_security_context_uses_env_override(tmp_path, monkeypatch):
    path = tmp_path / "feed.json"
    _write_feed(path, [
        {"ip": "5.5.5.5", "process": "x", "port": 22, "trust": "Unknown"},
    ])
    monkeypatch.setenv("NOVA_SILENTGUARD_PATH", str(path))
    rendered = get_security_context()
    assert rendered is not None
    assert "5.5.5.5" in rendered


def test_get_security_context_returns_none_when_empty(tmp_path):
    path = tmp_path / "empty.json"
    _write_feed(path, [])
    assert get_security_context(path=path) is None


def test_no_system_action_surfaces_in_module():
    """The module must not import or call anything that could mutate the system."""
    import ast

    with open(security_feed.__file__, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())

    forbidden_imports = {"subprocess", "socket", "shutil", "ctypes", "signal"}
    forbidden_calls = {"system", "kill", "popen", "remove", "unlink", "rmdir"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root not in forbidden_imports, (
                    f"security_feed must not import {alias.name!r}"
                )
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert root not in forbidden_imports, (
                f"security_feed must not import from {node.module!r}"
            )
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                assert func.attr not in forbidden_calls, (
                    f"security_feed must not call {func.attr!r}"
                )
            elif isinstance(func, ast.Name):
                assert func.id not in forbidden_calls, (
                    f"security_feed must not call {func.id!r}"
                )
