"""
Read-only bridge to SilentGuard's local memory file.

SilentGuard is an external monitoring tool that records observed network
connections (IP, process, port, trust status) to a JSON file in the user's
home directory. Nova consumes that file as a passive data source so the
assistant can answer questions like "show me suspicious connections" or
"analyze unknown IPs" without ever changing system state.

Boundaries enforced by this module:
  * read-only file access — no writes, no deletions.
  * no subprocess execution.
  * no network scanning, no socket calls, no DNS lookups.
  * no firewall, kill, or block actions.
  * no root or elevated-privilege operations.

If SilentGuard is not installed or the memory file is missing/corrupt,
every public function returns an empty result rather than raising.
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_FEED_PATH = Path.home() / ".silentguard_memory.json"

TRUST_KNOWN = "Known"
TRUST_UNKNOWN = "Unknown"
TRUST_LOCAL = "Local"
_VALID_TRUST = {TRUST_KNOWN, TRUST_UNKNOWN, TRUST_LOCAL}

_MAX_FEED_BYTES = 5 * 1024 * 1024  # 5 MB cap — silently ignore anything larger.
_DEFAULT_LIMIT = 50

_SECURITY_KEYWORDS = (
    "suspicious connection", "suspicious connections",
    "connexion suspecte", "connexions suspectes",
    "unknown ip", "unknown ips", "ip inconnue", "ips inconnues",
    "silentguard", "security feed", "flux de sécurité",
    "explain this process", "explique ce processus",
    "analyze unknown", "analyse les inconnu", "analyse inconnu",
    "recent security events", "événements de sécurité",
)


@dataclass(frozen=True)
class SecurityEvent:
    """One observation reported by SilentGuard."""

    ip: str
    process: str
    port: Optional[int]
    trust: str
    timestamp: Optional[str] = None
    raw: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def is_unknown(self) -> bool:
        return self.trust == TRUST_UNKNOWN

    @property
    def is_local(self) -> bool:
        return self.trust == TRUST_LOCAL


def _resolve_path(path: Optional[os.PathLike]) -> Path:
    if path is not None:
        return Path(path)
    override = os.environ.get("NOVA_SILENTGUARD_PATH")
    if override:
        return Path(override).expanduser()
    return DEFAULT_FEED_PATH


def _coerce_port(value) -> Optional[int]:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    if port < 0 or port > 65535:
        return None
    return port


def _coerce_trust(value) -> str:
    if not isinstance(value, str):
        return TRUST_UNKNOWN
    normalized = value.strip().capitalize()
    if normalized in _VALID_TRUST:
        return normalized
    return TRUST_UNKNOWN


def _parse_event(entry) -> Optional[SecurityEvent]:
    if not isinstance(entry, dict):
        return None
    ip = entry.get("ip") or entry.get("remote_ip") or entry.get("address")
    process = entry.get("process") or entry.get("proc") or entry.get("command")
    if not isinstance(ip, str) or not ip.strip():
        return None
    if not isinstance(process, str) or not process.strip():
        process = "unknown"
    return SecurityEvent(
        ip=ip.strip(),
        process=process.strip(),
        port=_coerce_port(entry.get("port") or entry.get("remote_port")),
        trust=_coerce_trust(entry.get("trust") or entry.get("status")),
        timestamp=entry.get("timestamp") or entry.get("time"),
        raw=entry,
    )


def _load_raw(path: Path) -> list:
    """Load and decode the SilentGuard JSON file. Returns [] on any failure."""
    try:
        if not path.is_file():
            return []
        if path.stat().st_size > _MAX_FEED_BYTES:
            logger.warning("SilentGuard feed too large (>%d bytes); skipping.", _MAX_FEED_BYTES)
            return []
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.debug("SilentGuard feed unavailable: %s", e)
        return []

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # Common shapes: {"events": [...]} or {"connections": [...]}.
        for key in ("events", "connections", "entries", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def get_recent_security_events(
    limit: int = _DEFAULT_LIMIT,
    path: Optional[os.PathLike] = None,
) -> list[SecurityEvent]:
    """
    Return up to `limit` most recent events from the SilentGuard feed.

    Read-only. Returns [] if SilentGuard is not installed, the file is
    missing, or the payload is malformed. Entries that cannot be parsed
    are skipped silently.
    """
    if limit <= 0:
        return []
    feed_path = _resolve_path(path)
    raw = _load_raw(feed_path)
    events: list[SecurityEvent] = []
    for entry in raw:
        event = _parse_event(entry)
        if event is not None:
            events.append(event)
    # SilentGuard appends; the tail is the newest slice.
    return events[-limit:]


def summarize_events(events: list[SecurityEvent]) -> dict:
    """
    Build a passive summary suitable for inclusion in a chat prompt.

    Groups unknown connections by IP, surfaces the busiest processes, and
    highlights anomalies (repeated unknown IPs, rare ports). No system
    actions are taken or recommended.
    """
    if not events:
        return {
            "total": 0,
            "unknown_count": 0,
            "known_count": 0,
            "local_count": 0,
            "unknown_groups": [],
            "top_processes": [],
            "anomalies": [],
        }

    unknowns = [e for e in events if e.is_unknown]
    knowns = [e for e in events if e.trust == TRUST_KNOWN]
    locals_ = [e for e in events if e.is_local]

    unknown_by_ip: dict[str, list[SecurityEvent]] = {}
    for e in unknowns:
        unknown_by_ip.setdefault(e.ip, []).append(e)

    unknown_groups = [
        {
            "ip": ip,
            "count": len(group),
            "processes": sorted({e.process for e in group}),
            "ports": sorted({e.port for e in group if e.port is not None}),
        }
        for ip, group in sorted(
            unknown_by_ip.items(), key=lambda kv: len(kv[1]), reverse=True
        )
    ]

    top_processes = [
        {"process": p, "count": n}
        for p, n in Counter(e.process for e in events).most_common(5)
    ]

    anomalies: list[str] = []
    for group in unknown_groups:
        if group["count"] >= 5:
            anomalies.append(
                f"IP {group['ip']} has {group['count']} unknown connections."
            )
    rare_ports = [g for g in unknown_groups if any(p and p > 49151 for p in g["ports"])]
    for g in rare_ports:
        anomalies.append(
            f"IP {g['ip']} reached high/ephemeral ports {g['ports']}."
        )

    return {
        "total": len(events),
        "unknown_count": len(unknowns),
        "known_count": len(knowns),
        "local_count": len(locals_),
        "unknown_groups": unknown_groups,
        "top_processes": top_processes,
        "anomalies": anomalies,
    }


def format_security_summary(summary: dict) -> str:
    """Render a `summarize_events` result as a compact, prompt-friendly string."""
    if not summary or summary.get("total", 0) == 0:
        return "Aucun événement SilentGuard disponible."

    lines = [
        f"Événements: {summary['total']} "
        f"(connus={summary['known_count']}, "
        f"inconnus={summary['unknown_count']}, "
        f"locaux={summary['local_count']}).",
    ]
    if summary["unknown_groups"]:
        lines.append("Connexions inconnues groupées par IP:")
        for g in summary["unknown_groups"][:10]:
            ports = ", ".join(str(p) for p in g["ports"]) or "—"
            procs = ", ".join(g["processes"]) or "—"
            lines.append(
                f"  - {g['ip']} ×{g['count']} (ports: {ports}; processus: {procs})"
            )
    if summary["top_processes"]:
        top = ", ".join(
            f"{p['process']} ({p['count']})" for p in summary["top_processes"]
        )
        lines.append(f"Processus les plus actifs: {top}.")
    if summary["anomalies"]:
        lines.append("Anomalies:")
        for a in summary["anomalies"]:
            lines.append(f"  - {a}")
    lines.append(
        "Lecture seule — Nova ne bloque, ne tue, ni ne modifie aucun processus."
    )
    return "\n".join(lines)


def is_security_query(user_input: str) -> bool:
    """Heuristic match for queries that should pull in the SilentGuard feed."""
    if not isinstance(user_input, str):
        return False
    lower = user_input.lower()
    return any(keyword in lower for keyword in _SECURITY_KEYWORDS)


def get_security_context(
    limit: int = _DEFAULT_LIMIT,
    path: Optional[os.PathLike] = None,
) -> Optional[str]:
    """
    Convenience wrapper used by the chat layer.

    Returns the formatted summary string, or `None` when there is nothing
    to report (no feed file or empty feed). Always read-only.
    """
    events = get_recent_security_events(limit=limit, path=path)
    if not events:
        return None
    return format_security_summary(summarize_events(events))
