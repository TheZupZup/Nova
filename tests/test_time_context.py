import os
import zoneinfo
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from core.time_context import (
    format_time_context,
    get_time_context,
    get_timezone_name,
    now,
    resolve_relative_date,
    today_iso,
)


# ── Timezone handling ────────────────────────────────────────────────────────


def test_nova_timezone_env_is_respected():
    with patch.dict(os.environ, {"NOVA_TIMEZONE": "America/Montreal"}):
        name = get_timezone_name()
    assert name == "America/Montreal"


def test_nova_timezone_invalid_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVA_TIMEZONE", "Not/AReal_Zone")
    # Should not raise; falls back gracefully (system tz or UTC)
    name = get_timezone_name()
    assert name != "Not/AReal_Zone"


def test_nova_timezone_empty_uses_system_or_utc(monkeypatch):
    monkeypatch.delenv("NOVA_TIMEZONE", raising=False)
    name = get_timezone_name()
    assert isinstance(name, str)
    assert len(name) > 0


def test_now_returns_timezone_aware_datetime():
    with patch.dict(os.environ, {"NOVA_TIMEZONE": "UTC"}):
        dt = now()
    assert dt.tzinfo is not None


def test_now_uses_configured_timezone():
    with patch.dict(os.environ, {"NOVA_TIMEZONE": "America/Montreal"}):
        dt = now()
    assert dt.tzinfo is not None
    assert dt.tzname() in ("EST", "EDT")  # Montreal observes both


# ── Current date retrieval ───────────────────────────────────────────────────


def test_today_iso_format():
    with patch.dict(os.environ, {"NOVA_TIMEZONE": "UTC"}):
        iso = today_iso()
    parts = iso.split("-")
    assert len(parts) == 3
    assert len(parts[0]) == 4  # year
    assert len(parts[1]) == 2  # month
    assert len(parts[2]) == 2  # day


def test_today_iso_matches_now():
    with patch.dict(os.environ, {"NOVA_TIMEZONE": "UTC"}):
        iso = today_iso()
        dt = now()
    assert iso == dt.date().isoformat()


def test_get_time_context_keys():
    ctx = get_time_context()
    assert "current_date" in ctx
    assert "current_time" in ctx
    assert "timezone" in ctx


def test_get_time_context_date_is_iso():
    ctx = get_time_context()
    d = date.fromisoformat(ctx["current_date"])  # raises if invalid
    assert isinstance(d, date)


def test_get_time_context_time_format():
    ctx = get_time_context()
    parts = ctx["current_time"].split(":")
    assert len(parts) == 2
    assert parts[0].isdigit() and parts[1].isdigit()


def test_format_time_context_contains_fields():
    text = format_time_context()
    assert "current_date" in text
    assert "current_time" in text
    assert "timezone" in text


# ── Relative date resolution ─────────────────────────────────────────────────


def _today_in_tz(tz_name: str = "UTC") -> date:
    import datetime as _dt
    return _dt.datetime.now(zoneinfo.ZoneInfo(tz_name)).date()


@pytest.fixture(autouse=True)
def force_utc(monkeypatch):
    monkeypatch.setenv("NOVA_TIMEZONE", "UTC")


def test_resolve_today():
    assert resolve_relative_date("today") == _today_in_tz().isoformat()


def test_resolve_today_fr():
    assert resolve_relative_date("aujourd'hui") == _today_in_tz().isoformat()


def test_resolve_yesterday():
    expected = (_today_in_tz() - timedelta(days=1)).isoformat()
    assert resolve_relative_date("yesterday") == expected


def test_resolve_hier():
    expected = (_today_in_tz() - timedelta(days=1)).isoformat()
    assert resolve_relative_date("hier") == expected


def test_resolve_tomorrow():
    expected = (_today_in_tz() + timedelta(days=1)).isoformat()
    assert resolve_relative_date("tomorrow") == expected


def test_resolve_demain():
    expected = (_today_in_tz() + timedelta(days=1)).isoformat()
    assert resolve_relative_date("demain") == expected


def test_resolve_this_week_is_monday():
    result = resolve_relative_date("this week")
    d = date.fromisoformat(result)
    assert d.weekday() == 0  # Monday


def test_resolve_cette_semaine_is_monday():
    result = resolve_relative_date("cette semaine")
    d = date.fromisoformat(result)
    assert d.weekday() == 0


def test_resolve_last_week_is_monday_before_this_week():
    this_monday = date.fromisoformat(resolve_relative_date("this week"))
    last_monday = date.fromisoformat(resolve_relative_date("last week"))
    assert this_monday - last_monday == timedelta(days=7)


def test_resolve_next_week_is_monday_after_this_week():
    this_monday = date.fromisoformat(resolve_relative_date("this week"))
    next_monday = date.fromisoformat(resolve_relative_date("next week"))
    assert next_monday - this_monday == timedelta(days=7)


def test_resolve_unknown_returns_none():
    assert resolve_relative_date("last year") is None
    assert resolve_relative_date("in three days") is None
    assert resolve_relative_date("") is None


def test_resolve_case_insensitive():
    assert resolve_relative_date("TODAY") == resolve_relative_date("today")
    assert resolve_relative_date("YESTERDAY") == resolve_relative_date("yesterday")


def test_resolve_strips_whitespace():
    assert resolve_relative_date("  tomorrow  ") == resolve_relative_date("tomorrow")
