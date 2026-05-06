import os
import pathlib
import re
import zoneinfo
from datetime import datetime, timedelta

# Locale-independent day names so the prompt is deterministic regardless of
# the host's LC_TIME setting.
_DAY_NAMES = (
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
)

# Weekday lookup for relative-date parsing. Keys are lowercase EN/FR names;
# values are the Python `weekday()` index (Monday=0).
_WEEKDAY_INDEX = {
    "monday": 0,    "lundi":    0,
    "tuesday": 1,   "mardi":    1,
    "wednesday": 2, "mercredi": 2,
    "thursday": 3,  "jeudi":    3,
    "friday": 4,    "vendredi": 4,
    "saturday": 5,  "samedi":   5,
    "sunday": 6,    "dimanche": 6,
}

_WEEKDAY_NAMES_RE = "|".join(_WEEKDAY_INDEX.keys())

# "next monday" / "last friday"
_EN_NEXT_LAST_WEEKDAY = re.compile(
    rf"^(next|last)\s+({_WEEKDAY_NAMES_RE})$"
)
# "lundi prochain" / "vendredi dernier"
_FR_WEEKDAY_NEXT_LAST = re.compile(
    rf"^({_WEEKDAY_NAMES_RE})\s+(prochain|dernier)$"
)
# "in 3 days", "in 2 weeks"
_EN_IN_N = re.compile(r"^in\s+(\d+)\s+(day|days|week|weeks)$")
# "3 days ago", "2 weeks ago"
_EN_N_AGO = re.compile(r"^(\d+)\s+(day|days|week|weeks)\s+ago$")
# "dans 3 jours", "dans 2 semaines"
_FR_DANS_N = re.compile(r"^dans\s+(\d+)\s+(jour|jours|semaine|semaines)$")
# "il y a 3 jours", "il y a 2 semaines"
_FR_IL_Y_A_N = re.compile(r"^il y a\s+(\d+)\s+(jour|jours|semaine|semaines)$")


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
    """Return the ISO date string for a simple relative date expression, or None if unrecognised.

    Supports a small, deterministic set of EN/FR phrases:

    - ``today / yesterday / tomorrow`` (and ``aujourd'hui / hier / demain``)
    - ``this / last / next week`` (and ``cette / la semaine dernière / prochaine``),
      always anchored to the Monday of the target week
    - ``next <weekday>`` / ``last <weekday>`` (and ``<weekday> prochain / dernier``),
      where "next" is the first occurrence strictly after today and "last"
      the most recent occurrence strictly before today
    - ``in N days / weeks`` and ``N days / weeks ago``
      (and ``dans N jours / semaines``, ``il y a N jours / semaines``)

    Anything outside this set returns ``None`` and is expected to fall through
    to the model with the time context already attached, rather than be
    silently guessed here.
    """
    expr = expression.lower().strip()
    if not expr:
        return None
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

    # "next monday" / "last friday" / "lundi prochain" / "vendredi dernier".
    # "next" is the first occurrence strictly after today; "last" the most
    # recent strictly before today (today itself never matches).
    weekday_match = _parse_weekday_expr(expr)
    if weekday_match is not None:
        direction, target = weekday_match
        if direction == "next":
            delta = (target - today.weekday()) % 7 or 7
            return (today + timedelta(days=delta)).isoformat()
        back = (today.weekday() - target) % 7 or 7
        return (today - timedelta(days=back)).isoformat()

    # Numeric offsets: "in N days/weeks" and "N days/weeks ago", EN + FR
    offset = _parse_numeric_offset(expr)
    if offset is not None:
        return (today + timedelta(days=offset)).isoformat()

    return None


def _parse_weekday_expr(expr: str) -> tuple[str, int] | None:
    """Return (direction, weekday_index) for "next/last <weekday>" forms."""
    m = _EN_NEXT_LAST_WEEKDAY.match(expr)
    if m is not None:
        return (m.group(1), _WEEKDAY_INDEX[m.group(2)])
    m = _FR_WEEKDAY_NEXT_LAST.match(expr)
    if m is not None:
        direction = "next" if m.group(2) == "prochain" else "last"
        return (direction, _WEEKDAY_INDEX[m.group(1)])
    return None


def _parse_numeric_offset(expr: str) -> int | None:
    """Return a signed day offset for "in N days/weeks" and "N days/weeks ago"."""
    for pattern, sign in (
        (_EN_IN_N,    +1),
        (_FR_DANS_N,  +1),
        (_EN_N_AGO,   -1),
        (_FR_IL_Y_A_N, -1),
    ):
        m = pattern.match(expr)
        if m is None:
            continue
        n = int(m.group(1))
        unit = m.group(2)
        days = n * 7 if unit.startswith(("week", "semaine")) else n
        return sign * days
    return None


def get_time_context() -> dict:
    dt = now()
    return {
        "current_date": dt.date().isoformat(),
        "current_time": dt.strftime("%H:%M"),
        "day_of_week":  _DAY_NAMES[dt.weekday()],
        "timezone":     _get_timezone_name(),
    }


def format_time_context() -> str:
    ctx = get_time_context()
    return (
        f"[Time context]\n"
        f"current_date: {ctx['current_date']}\n"
        f"day_of_week: {ctx['day_of_week']}\n"
        f"current_time: {ctx['current_time']}\n"
        f"timezone: {ctx['timezone']}"
    )
