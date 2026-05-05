import os
import pathlib
import zoneinfo
from datetime import datetime, timedelta


def _get_timezone_name() -> str:
    tz_name = os.environ.get("NOVA_TIMEZONE", "").strip()
    if tz_name:
        try:
            zoneinfo.ZoneInfo(tz_name)
            return tz_name
        except (zoneinfo.ZoneInfoNotFoundError, KeyError):
            pass
    try:
        name = pathlib.Path("/etc/timezone").read_text().strip()
        if name:
            return name
    except OSError:
        pass
    return "UTC"


def _get_tz() -> zoneinfo.ZoneInfo:
    try:
        return zoneinfo.ZoneInfo(_get_timezone_name())
    except (zoneinfo.ZoneInfoNotFoundError, KeyError):
        return zoneinfo.ZoneInfo("UTC")


def now() -> datetime:
    return datetime.now(_get_tz())


def today_iso() -> str:
    return now().date().isoformat()


def get_timezone_name() -> str:
    return _get_timezone_name()


def resolve_relative_date(expression: str) -> str | None:
    """Return the ISO date string for a simple relative date expression, or None if unrecognised."""
    expr = expression.lower().strip()
    today = now().date()
    mapping = {
        "today":                        timedelta(0),
        "aujourd'hui":                  timedelta(0),
        "yesterday":                    timedelta(days=-1),
        "hier":                         timedelta(days=-1),
        "tomorrow":                     timedelta(days=1),
        "demain":                       timedelta(days=1),
    }
    if expr in mapping:
        return (today + mapping[expr]).isoformat()

    # Week expressions — always anchored to the Monday of the week
    week_offsets = {
        "this week": 0,
        "cette semaine": 0,
        "last week": -7,
        "la semaine dernière": -7,
        "semaine dernière": -7,
        "next week": 7,
        "la semaine prochaine": 7,
        "semaine prochaine": 7,
    }
    if expr in week_offsets:
        monday = today - timedelta(days=today.weekday()) + timedelta(days=week_offsets[expr])
        return monday.isoformat()

    return None


def get_time_context() -> dict:
    dt = now()
    return {
        "current_date": dt.date().isoformat(),
        "current_time": dt.strftime("%H:%M"),
        "timezone":     _get_timezone_name(),
    }


def format_time_context() -> str:
    ctx = get_time_context()
    return (
        f"[Time context]\n"
        f"current_date: {ctx['current_date']}\n"
        f"current_time: {ctx['current_time']}\n"
        f"timezone: {ctx['timezone']}"
    )
