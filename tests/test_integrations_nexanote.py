"""
Tests for the NexaNote integration switch.

NexaNote is reached over HTTP, so the tests stub ``httpx.Client`` rather
than spinning up a real server. They cover:
  * the read-only-by-default contract (writes refused without the
    second switch),
  * the per-user gating (off → no HTTP call at all),
  * the never-crash guarantees on transport / decode errors,
  * the structured status states.
"""

from __future__ import annotations

import ast
import sqlite3
from typing import Any

import httpx
import pytest

from core import memory as core_memory, settings as core_settings, users
from core.integrations import nexanote as nn
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


def _enable_read(user_id):
    core_settings.save_user_setting(user_id, "nexanote_enabled", "true")


def _enable_write(user_id):
    core_settings.save_user_setting(user_id, "nexanote_enabled", "true")
    core_settings.save_user_setting(user_id, "nexanote_write_enabled", "true")


# ── Fake httpx client ───────────────────────────────────────────────
# The integration always opens a client via ``httpx.Client(...)``; we
# stub that constructor so no real network call ever fires. ``calls``
# captures every (method, path, kwargs) so tests can also assert that
# disabled toggles result in zero outgoing requests.

class _FakeResponse:
    def __init__(self, status_code: int, payload: Any = None,
                 raise_decode: bool = False):
        self.status_code = status_code
        self._payload = payload
        self._raise_decode = raise_decode

    def json(self):
        if self._raise_decode:
            raise ValueError("decode error")
        return self._payload


class _FakeClient:
    def __init__(self, scripted: dict, calls: list, raise_on: set | None = None):
        self._scripted = scripted
        self._calls = calls
        self._raise_on = raise_on or set()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def _record(self, method: str, path: str, **kwargs):
        self._calls.append((method, path, kwargs))
        if path in self._raise_on:
            raise httpx.ConnectError("boom")
        return self._scripted.get((method, path), _FakeResponse(404))

    def get(self, path: str, **kwargs):
        return self._record("GET", path, **kwargs)

    def post(self, path: str, **kwargs):
        return self._record("POST", path, **kwargs)

    def put(self, path: str, **kwargs):
        return self._record("PUT", path, **kwargs)


@pytest.fixture
def http_stub(monkeypatch):
    """
    Returns ``(install, calls)``. Calling ``install(scripted, raise_on=...)``
    swaps in a fake httpx.Client; ``calls`` collects every outgoing
    request the integration tries to make.
    """
    calls: list = []
    state: dict = {"scripted": {}, "raise_on": set()}

    def factory(*args, **kwargs):
        return _FakeClient(state["scripted"], calls, state["raise_on"])

    monkeypatch.setattr(nn.httpx, "Client", factory)
    monkeypatch.setattr(nn, "NEXANOTE_API_URL", "http://nexanote.test")

    def install(scripted: dict, raise_on: set | None = None):
        state["scripted"] = scripted
        state["raise_on"] = raise_on or set()

    return install, calls


# ── Default state ───────────────────────────────────────────────────

class TestDisabledByDefault:
    def test_is_enabled_false(self, user_id):
        assert nn.is_enabled(user_id) is False

    def test_is_write_enabled_false(self, user_id):
        assert nn.is_write_enabled(user_id) is False

    def test_status_disabled(self, user_id):
        s = nn.status(user_id)
        assert s.enabled is False
        assert s.state == nn.STATE_DISABLED

    def test_no_http_when_disabled(self, user_id, http_stub):
        install, calls = http_stub
        install({})
        assert nn.list_notes(user_id) == []
        assert nn.read_note(user_id, 1) is None
        assert nn.create_note(user_id, "t", "c") is None
        assert nn.update_note(user_id, 1, title="t") is None
        # Disabled → must never reach out over the wire.
        assert calls == []


# ── Status ──────────────────────────────────────────────────────────

class TestStatus:
    def test_not_found_when_url_unset(self, user_id, monkeypatch):
        _enable_read(user_id)
        monkeypatch.setattr(nn, "NEXANOTE_API_URL", "")
        s = nn.status(user_id)
        assert s.state == nn.STATE_NOT_FOUND

    def test_connected_on_2xx_health(self, user_id, http_stub):
        install, _ = http_stub
        install({("GET", "/health"): _FakeResponse(200, {"ok": True})})
        _enable_read(user_id)
        s = nn.status(user_id)
        assert s.state == nn.STATE_CONNECTED

    def test_unreachable_on_5xx(self, user_id, http_stub):
        install, _ = http_stub
        install({("GET", "/health"): _FakeResponse(503)})
        _enable_read(user_id)
        s = nn.status(user_id)
        assert s.state == nn.STATE_UNREACHABLE

    def test_unreachable_on_network_error(self, user_id, http_stub):
        install, _ = http_stub
        install({}, raise_on={"/health"})
        _enable_read(user_id)
        s = nn.status(user_id)
        assert s.state == nn.STATE_UNREACHABLE


# ── Read API ────────────────────────────────────────────────────────

class TestReadApi:
    def test_list_notes_returns_list(self, user_id, http_stub):
        install, calls = http_stub
        notes = [{"id": 1, "title": "a"}, {"id": 2, "title": "b"}]
        install({("GET", "/notes"): _FakeResponse(200, notes)})
        _enable_read(user_id)
        result = nn.list_notes(user_id, limit=10)
        assert result == notes
        # The integration must pass the limit as a query param.
        assert calls[-1][2].get("params") == {"limit": 10}

    def test_list_notes_unwraps_dict_payload(self, user_id, http_stub):
        install, _ = http_stub
        install({("GET", "/notes"): _FakeResponse(200, {"notes": [{"id": 1}]})})
        _enable_read(user_id)
        assert nn.list_notes(user_id) == [{"id": 1}]

    def test_list_notes_empty_on_decode_error(self, user_id, http_stub):
        install, _ = http_stub
        install({("GET", "/notes"): _FakeResponse(200, raise_decode=True)})
        _enable_read(user_id)
        assert nn.list_notes(user_id) == []

    def test_list_notes_empty_on_network_error(self, user_id, http_stub):
        install, _ = http_stub
        install({}, raise_on={"/notes"})
        _enable_read(user_id)
        assert nn.list_notes(user_id) == []

    def test_read_note_fetches_one(self, user_id, http_stub):
        install, _ = http_stub
        install({("GET", "/notes/42"): _FakeResponse(200, {"id": 42, "title": "x"})})
        _enable_read(user_id)
        assert nn.read_note(user_id, 42) == {"id": 42, "title": "x"}

    def test_read_note_none_on_404(self, user_id, http_stub):
        install, _ = http_stub
        install({("GET", "/notes/99"): _FakeResponse(404)})
        _enable_read(user_id)
        assert nn.read_note(user_id, 99) is None


# ── Read-only-by-default contract ──────────────────────────────────

class TestWriteGating:
    def test_create_refused_when_only_read_enabled(self, user_id, http_stub):
        install, calls = http_stub
        install({("POST", "/notes"): _FakeResponse(201, {"id": 1})})
        _enable_read(user_id)  # writes NOT enabled
        assert nn.create_note(user_id, "t", "c") is None
        # Read-only mode must not even attempt the POST.
        assert all(method != "POST" for method, *_ in calls)

    def test_create_succeeds_when_write_enabled(self, user_id, http_stub):
        install, calls = http_stub
        install({("POST", "/notes"): _FakeResponse(201, {"id": 7})})
        _enable_write(user_id)
        assert nn.create_note(user_id, "t", "c") == {"id": 7}
        assert calls[-1][0] == "POST"
        assert calls[-1][2].get("json") == {"title": "t", "content": "c"}

    def test_update_refused_when_only_read_enabled(self, user_id, http_stub):
        install, calls = http_stub
        install({("PUT", "/notes/1"): _FakeResponse(200, {"id": 1})})
        _enable_read(user_id)
        assert nn.update_note(user_id, 1, title="new") is None
        assert all(method != "PUT" for method, *_ in calls)

    def test_update_succeeds_when_write_enabled(self, user_id, http_stub):
        install, calls = http_stub
        install({("PUT", "/notes/1"): _FakeResponse(200, {"id": 1})})
        _enable_write(user_id)
        assert nn.update_note(user_id, 1, content="new") == {"id": 1}
        assert calls[-1][2].get("json") == {"content": "new"}

    def test_create_swallows_network_error(self, user_id, http_stub):
        install, _ = http_stub
        install({}, raise_on={"/notes"})
        _enable_write(user_id)
        # Must not raise — integration absorbs failure.
        assert nn.create_note(user_id, "t", "c") is None

    def test_create_rejects_blank_title(self, user_id, http_stub):
        install, calls = http_stub
        install({("POST", "/notes"): _FakeResponse(201, {"id": 1})})
        _enable_write(user_id)
        assert nn.create_note(user_id, "   ", "c") is None
        # Validation runs before any HTTP call.
        assert calls == []


# ── Read-only / safety guarantees ───────────────────────────────────

class TestNoSystemActions:
    def test_no_subprocess_or_socket_imports(self):
        with open(nn.__file__, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())

        forbidden = {"subprocess", "socket", "shutil", "ctypes", "signal", "os"}
        # ``os`` is excluded by the integration on purpose — config reads
        # happen in config.py, not here.
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root not in forbidden, (
                        f"nexanote integration must not import {alias.name!r}"
                    )
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                assert root not in forbidden, (
                    f"nexanote integration must not import from {node.module!r}"
                )
