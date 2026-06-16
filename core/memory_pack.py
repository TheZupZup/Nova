"""Nova Memory Pack — portable, structured, per-user export / import.

A **Nova Memory Pack** is a small ``.zip`` archive of *structured JSON*
that lets a user carry their useful Nova context — long-term memories,
conversations, generated summaries, and safe preferences — from one
Nova instance to another so the assistant can "remember them" across
installs and devices.

This module is deliberately distinct from the two adjacent features:

* :mod:`core.data_export` ships the **raw ``nova.db``** in a ``tar.gz``
  for whole-host migration. It is **admin-only** and is a database
  dump, not a curated per-user pack.
* :mod:`core.memory_importer` ingests a **Markdown** memory pack
  (memories only, no conversations, no export side).

The Memory Pack is **per-user, authenticated (not admin), structured
JSON, and round-trippable** (export ⇄ import). The contract:

* **Local-only.** No network, no cloud, no account linking. Pure
  functions over bytes + the local SQLite database.
* **Secret-free.** Password hashes, JWT secrets, OAuth/GitHub tokens,
  API keys, session cookies, and the host ``settings`` table are
  never written. Per-user settings pass a secret-shaped denylist as
  defense-in-depth so a *future* sensitive key cannot leak.
* **Merge by default.** Import keeps existing data and only adds what
  is missing, de-duplicating on content so re-importing the same pack
  is a no-op.
* **Transactional.** The whole import runs inside one SQLite
  transaction; any failure rolls back and the existing database is
  left exactly as it was.
* **Hostile-input safe.** Import validates the archive structure
  (path traversal, symlinks, member count, zip-bomb size, per-file
  JSON validity, manifest format / version) before writing anything.

The format is forward-compatible: every file carries a ``version``
field, unknown files / keys are ignored, optional files may be
missing, and an archive whose ``format_version`` is *newer* than this
build understands is refused with a clear message rather than
mis-read.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import sqlite3
import stat
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Optional

logger = logging.getLogger(__name__)


# ── Format identity ─────────────────────────────────────────────────

#: Pack format identifier — pinned in every manifest so a future format
#: change is detected explicitly instead of silently mis-read.
FORMAT_ID = "nova-memory-pack"
FORMAT_VERSION = 1

#: File layout inside the archive. Everything is JSON at the archive
#: root except the reserved ``attachments/`` directory (unused in v1
#: but accepted on import so a future pack stays loadable here).
MANIFEST_NAME = "manifest.json"
PROFILE_NAME = "profile.json"
MEMORIES_NAME = "memories.json"
CONVERSATIONS_NAME = "conversations.json"
SUMMARIES_NAME = "summaries.json"
SETTINGS_NAME = "settings.json"
ATTACHMENTS_PREFIX = "attachments/"

#: Every JSON file the exporter writes, in a stable order.
_EXPORT_FILES = (
    MANIFEST_NAME, PROFILE_NAME, MEMORIES_NAME,
    CONVERSATIONS_NAME, SUMMARIES_NAME, SETTINGS_NAME,
)

#: Top-level names an import tolerates without a warning.
_KNOWN_TOP_LEVEL = frozenset(_EXPORT_FILES)

#: Default filename stem for a freshly built pack. The timestamp is
#: appended so packs sort lexicographically by creation time.
DEFAULT_PACK_STEM = "nova-memory-pack"


# ── Import safety caps ──────────────────────────────────────────────

#: Largest upload the API will read into memory (compressed bytes).
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
#: Largest total *uncompressed* size we will accept — zip-bomb guard.
MAX_TOTAL_UNCOMPRESSED_BYTES = 500 * 1024 * 1024
#: Largest single member (uncompressed) we will accept.
MAX_MEMBER_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
#: Largest number of archive members we will enumerate.
MAX_MEMBERS = 10_000
#: Hard cap when actually decompressing one JSON file from the archive.
MAX_JSON_BYTES = 64 * 1024 * 1024

_HASH_CHUNK_BYTES = 64 * 1024


# ── Secret filtering (defense-in-depth) ─────────────────────────────

#: Per-user setting keys are already a safe allowlist today, but a
#: future key could hold a credential. Any key containing one of these
#: substrings is dropped from the export and ignored on import.
_SECRET_KEY_SUBSTRINGS: tuple[str, ...] = (
    "token", "secret", "password", "passwd", "api_key", "apikey",
    "credential", "oauth", "jwt", "client_secret", "private_key",
    "privatekey", "webhook", "cookie", "session", "access_key",
    "refresh_token", "bearer",
)

#: A whitespace-free run this long that looks like base64 / hex is
#: treated as a secret-shaped value and the setting is dropped.
_LONG_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{40,}")

#: Allowed natural-memory ``kind`` values. Mirrors
#: :data:`memory.schema.VALID_KINDS`; imported rows with an unknown
#: kind are coerced to ``general`` rather than rejected.
try:  # pragma: no cover - trivial import guard
    from memory.schema import VALID_KINDS as _VALID_KINDS
except Exception:  # pragma: no cover
    _VALID_KINDS = frozenset({
        "preference", "project", "hardware", "software",
        "workflow", "constraint", "avoid", "general",
    })


# ── Result dataclasses ──────────────────────────────────────────────


@dataclass(frozen=True)
class MemoryPackResult:
    """Outcome of :func:`build_memory_pack`.

    ``data`` is the raw ``.zip`` bytes; ``filename`` is the suggested
    download name; ``manifest`` is the parsed manifest body; ``counts``
    is the per-section item count. ``as_dict`` omits the raw bytes so
    it is safe to log or return as JSON.
    """

    filename: str
    data: bytes
    manifest: dict
    counts: dict

    def as_dict(self) -> dict:
        return {
            "filename": self.filename,
            "size": len(self.data),
            "manifest": self.manifest,
            "counts": self.counts,
        }


@dataclass(frozen=True)
class MemoryPackInspection:
    """Read-only structural report on an uploaded pack."""

    valid: bool
    manifest: Optional[dict]
    counts: dict
    files: tuple[str, ...]
    errors: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "valid": self.valid,
            "manifest": self.manifest,
            "counts": self.counts,
            "files": list(self.files),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class MemoryPackImportPreview:
    """Dry-run preview: what an import *would* do. Writes nothing."""

    valid: bool
    counts: dict
    manifest: Optional[dict]
    warnings: tuple[str, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "valid": self.valid,
            "counts": self.counts,
            "manifest": self.manifest,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


#: Stable wire-format strings for ``MemoryPackImportResult.outcome``.
OUTCOME_IMPORTED = "imported"
OUTCOME_UNCONFIRMED = "unconfirmed"
OUTCOME_REFUSED = "refused"
OUTCOME_FAILED = "failed"


@dataclass(frozen=True)
class MemoryPackImportResult:
    """Outcome of :func:`import_memory_pack`."""

    outcome: str
    imported: dict
    skipped: dict
    manifest: Optional[dict]
    warnings: tuple[str, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "imported": self.imported,
            "skipped": self.skipped,
            "manifest": self.manifest,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


# ── Small helpers ───────────────────────────────────────────────────


def _resolve_db_path(db_path: Optional[str]) -> str:
    """Return ``db_path`` or the live ``core.memory.DB_PATH``.

    Resolved at call time (not import time) so tests that
    ``monkeypatch.setattr(core.memory, "DB_PATH", ...)`` are honoured.
    """
    if db_path is not None:
        return db_path
    from core.memory import DB_PATH
    return DB_PATH


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


def _normalize(text: str) -> str:
    """Lowercase + collapse whitespace for content de-duplication."""
    return " ".join((text or "").lower().split())


def _nova_version() -> str:
    """Best-effort Nova version from the CHANGELOG heading, else ``""``."""
    try:
        repo_root = Path(__file__).resolve().parent.parent
        changelog = repo_root / "CHANGELOG.md"
        if changelog.is_file():
            for line in changelog.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()[:20]:
                m = re.match(r"^##\s*\[?(\d+\.\d+(?:\.\d+)?)\]?", line.strip())
                if m:
                    return m.group(1)
    except OSError:
        return ""
    return ""


def _is_secret_setting(key: str, value: str) -> bool:
    """True when a per-user setting looks like it carries a secret."""
    low = (key or "").lower()
    if any(sub in low for sub in _SECRET_KEY_SUBSTRINGS):
        return True
    if value and _LONG_TOKEN_RE.search(value):
        return True
    return False


def _is_safe_member_name(name: str) -> bool:
    """Reject path traversal / absolute / drive / control-char names."""
    if not name or name.startswith("/") or name.startswith("\\"):
        return False
    if "\x00" in name or "\\" in name:
        return False
    if re.match(r"^[A-Za-z]:", name):
        return False
    for part in PurePosixPath(name).parts:
        if part == "..":
            return False
    return True


def _is_symlink_member(info: zipfile.ZipInfo) -> bool:
    """True when a zip entry encodes a Unix symlink in its mode bits."""
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode)


# ── Export ──────────────────────────────────────────────────────────


def build_memory_pack(
    user_id: int,
    *,
    db_path: Optional[str] = None,
    now: Optional[datetime] = None,
) -> MemoryPackResult:
    """Build a Memory Pack ``.zip`` for ``user_id`` (in memory).

    Reads only data owned by ``user_id``. Never touches the network,
    never writes a file (the caller persists / streams the bytes), and
    never includes a secret. The output is deterministic for a fixed
    ``now`` and database state, which keeps the tests honest.
    """
    db = _resolve_db_path(db_path)
    created = _now(now)
    created_iso = created.isoformat()

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        project_names = _load_project_names(conn, user_id)
        profile = _export_profile(conn, user_id)
        classic, natural = _export_memories(conn, user_id, project_names)
        conversations = _export_conversations(conn, user_id, project_names)
        settings_payload, dropped_secret = _export_settings(conn, user_id)
    finally:
        conn.close()

    # Summaries are generated, not stored — build one from the user's
    # recent conversations at export time.
    summary = _export_summary(user_id, created)

    msg_count = sum(len(c["messages"]) for c in conversations)
    counts = {
        "memories": len(classic),
        "natural_memories": len(natural),
        "conversations": len(conversations),
        "messages": msg_count,
        "settings": len(settings_payload),
        "summaries": 1 if summary.get("has_continuity") else 0,
        "projects": len(project_names),
    }

    profile_doc = {"version": 1, **profile}
    memories_doc = {"version": 1, "classic": classic, "natural": natural}
    conversations_doc = {"version": 1, "conversations": conversations}
    summaries_doc = {"version": 1, "session_continuity": summary}
    settings_doc = {"version": 1, "user_settings": settings_payload}

    # Hash the data files so the manifest can record integrity digests
    # and an importer (or a human) can verify the archive contents.
    file_bodies: dict[str, bytes] = {
        PROFILE_NAME: _dumps(profile_doc),
        MEMORIES_NAME: _dumps(memories_doc),
        CONVERSATIONS_NAME: _dumps(conversations_doc),
        SUMMARIES_NAME: _dumps(summaries_doc),
        SETTINGS_NAME: _dumps(settings_doc),
    }
    files_meta = [
        {"name": name, "size": len(body),
         "sha256": hashlib.sha256(body).hexdigest()}
        for name, body in file_bodies.items()
    ]

    warnings: list[str] = []
    if dropped_secret:
        warnings.append(
            f"{dropped_secret} setting(s) were excluded because they "
            "looked like a secret."
        )

    manifest = {
        "format": FORMAT_ID,
        "format_version": FORMAT_VERSION,
        "app": "Nova",
        "kind": "memory-pack",
        "created_at": created_iso,
        "nova_version": _nova_version(),
        "counts": counts,
        "files": files_meta,
        "excluded": [
            "password hashes",
            "API keys",
            "GitHub / OAuth tokens and client secrets",
            "JWT signing secret",
            "session cookies",
            "the host (global) settings table",
            "memory embeddings (regenerated locally on use)",
            "per-user settings whose key or value looked secret-shaped",
        ],
        "privacy_notice": (
            "This pack may contain personal conversation history and "
            "long-term memories. Treat it like private data: store it "
            "somewhere safe and only import it into a Nova instance you "
            "trust."
        ),
        "warnings": warnings,
    }
    manifest_body = _dumps(manifest)

    bio = io.BytesIO()
    with zipfile.ZipFile(bio, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        _add_member(zf, MANIFEST_NAME, manifest_body, created)
        for name in (PROFILE_NAME, MEMORIES_NAME, CONVERSATIONS_NAME,
                     SUMMARIES_NAME, SETTINGS_NAME):
            _add_member(zf, name, file_bodies[name], created)

    stamp = created.strftime("%Y%m%dT%H%M%SZ")
    filename = f"{DEFAULT_PACK_STEM}-{stamp}.zip"
    return MemoryPackResult(
        filename=filename,
        data=bio.getvalue(),
        manifest=manifest,
        counts=counts,
    )


def _dumps(doc: dict) -> bytes:
    """Serialise ``doc`` deterministically as UTF-8 JSON bytes."""
    return json.dumps(
        doc, indent=2, sort_keys=True, ensure_ascii=False,
    ).encode("utf-8")


def _add_member(
    zf: zipfile.ZipFile, name: str, body: bytes, when: datetime,
) -> None:
    """Write ``body`` to ``name`` with a stable, traversal-safe header."""
    info = zipfile.ZipInfo(
        filename=name,
        date_time=when.timetuple()[:6],
    )
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    zf.writestr(info, body)


def _load_project_names(
    conn: sqlite3.Connection, user_id: int,
) -> dict[int, str]:
    """Map the user's project ids → names (for inline export labels)."""
    try:
        rows = conn.execute(
            "SELECT id, name FROM projects WHERE user_id = ?", (user_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {int(r["id"]): r["name"] for r in rows}


def _export_profile(conn: sqlite3.Connection, user_id: int) -> dict:
    """Return the safe profile block (never the password hash)."""
    user: dict = {}
    try:
        row = conn.execute(
            "SELECT username, role, created_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if row is not None:
            user = {
                "username": row["username"],
                "role": row["role"],
                "created_at": row["created_at"],
            }
    except sqlite3.OperationalError:
        user = {}

    # Personalization preferences via the canonical helper so defaults
    # and key names never drift from the settings module.
    try:
        from core.settings import get_personalization
        personalization = get_personalization(user_id)
    except Exception:  # pragma: no cover - defensive
        personalization = {}

    return {"user": user, "personalization": personalization}


def _export_memories(
    conn: sqlite3.Connection, user_id: int, project_names: dict[int, str],
) -> tuple[list[dict], list[dict]]:
    """Return ``(classic, natural)`` memory rows owned by ``user_id``."""
    classic: list[dict] = []
    try:
        rows = conn.execute(
            "SELECT category, content, created, project_id FROM memories "
            "WHERE user_id = ? ORDER BY created ASC",
            (user_id,),
        ).fetchall()
        for r in rows:
            classic.append({
                "category": r["category"],
                "content": r["content"],
                "created": r["created"],
                "project": project_names.get(r["project_id"]),
            })
    except sqlite3.OperationalError:
        pass

    natural: list[dict] = []
    try:
        rows = conn.execute(
            "SELECT kind, topic, content, confidence, source, created_at, "
            "updated_at, last_seen_at, project_id FROM natural_memories "
            "WHERE user_id = ? ORDER BY created_at ASC",
            (user_id,),
        ).fetchall()
        for r in rows:
            natural.append({
                "kind": r["kind"],
                "topic": r["topic"],
                "content": r["content"],
                "confidence": r["confidence"],
                "source": r["source"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "last_seen_at": r["last_seen_at"],
                "project": project_names.get(r["project_id"]),
            })
    except sqlite3.OperationalError:
        pass

    return classic, natural


def _export_conversations(
    conn: sqlite3.Connection, user_id: int, project_names: dict[int, str],
) -> list[dict]:
    """Return conversations (with nested messages) owned by ``user_id``."""
    out: list[dict] = []
    try:
        convs = conn.execute(
            "SELECT id, title, created, updated, project_id FROM conversations "
            "WHERE user_id = ? ORDER BY created ASC",
            (user_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return out

    for c in convs:
        try:
            msgs = conn.execute(
                "SELECT role, content, model, created FROM messages "
                "WHERE conversation_id = ? ORDER BY created ASC, id ASC",
                (c["id"],),
            ).fetchall()
        except sqlite3.OperationalError:
            msgs = []
        out.append({
            "title": c["title"],
            "created": c["created"],
            "updated": c["updated"],
            "project": project_names.get(c["project_id"]),
            "messages": [
                {
                    "role": m["role"],
                    "content": m["content"],
                    "model": m["model"],
                    "created": m["created"],
                }
                for m in msgs
            ],
        })
    return out


def _export_settings(
    conn: sqlite3.Connection, user_id: int,
) -> tuple[dict, int]:
    """Return ``(safe_settings, dropped_secret_count)`` for ``user_id``."""
    safe: dict[str, str] = {}
    dropped = 0
    try:
        rows = conn.execute(
            "SELECT key, value FROM user_settings WHERE user_id = ? "
            "ORDER BY key ASC",
            (user_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return safe, dropped
    for r in rows:
        key, value = r["key"], r["value"]
        if _is_secret_setting(key, value):
            dropped += 1
            continue
        safe[key] = value
    return safe, dropped


def _export_summary(user_id: int, created: datetime) -> dict:
    """Build a generated session-continuity summary (never stored)."""
    try:
        from core.session_continuity import build_session_continuity
        # ``build_session_continuity`` compares naive timestamps from
        # the store; pass a naive ``now`` to avoid tz subtraction errors.
        naive = created.replace(tzinfo=None)
        return build_session_continuity(user_id, now=naive)
    except Exception:  # pragma: no cover - summary is best-effort
        return {"has_continuity": False}


# ── Inspection / validation ─────────────────────────────────────────


def inspect_memory_pack(zip_bytes: bytes) -> MemoryPackInspection:
    """Validate ``zip_bytes`` structurally. Never writes, never raises.

    Checks (in order): non-empty; real ZIP; member count cap; every
    member name safe (no traversal / absolute / drive / symlink);
    declared sizes within the zip-bomb caps; a parseable
    ``manifest.json`` with the expected format and a ``format_version``
    this build understands; and every present data file is valid JSON
    of the expected shape. Returns counts derived from the parsed
    files so a caller can render a preview without a second parse.
    """
    errors: list[str] = []
    warnings: list[str] = []
    files: list[str] = []
    counts = _empty_counts()

    if not zip_bytes:
        return MemoryPackInspection(
            False, None, counts, (), ("The uploaded file is empty.",),
        )

    bio = io.BytesIO(zip_bytes)
    if not zipfile.is_zipfile(bio):
        return MemoryPackInspection(
            False, None, counts, (),
            ("The uploaded file is not a valid .zip archive.",),
        )

    try:
        with zipfile.ZipFile(bio) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_MEMBERS:
                return MemoryPackInspection(
                    False, None, counts, (),
                    (f"Archive has too many entries (> {MAX_MEMBERS}).",),
                )
            total_uncompressed = 0
            for info in infos:
                name = info.filename
                if name.endswith("/"):
                    continue  # directory entry
                if not _is_safe_member_name(name):
                    errors.append(f"Unsafe archive entry: {name!r}.")
                    continue
                if _is_symlink_member(info):
                    errors.append(f"Archive contains a symlink: {name!r}.")
                    continue
                if info.file_size > MAX_MEMBER_UNCOMPRESSED_BYTES:
                    errors.append(f"Archive entry {name!r} is too large.")
                    continue
                total_uncompressed += int(info.file_size)
                files.append(name)
                top = PurePosixPath(name).parts[0]
                if top not in _KNOWN_TOP_LEVEL \
                        and not name.startswith(ATTACHMENTS_PREFIX):
                    warnings.append(f"Ignoring unexpected entry: {name!r}.")

            if total_uncompressed > MAX_TOTAL_UNCOMPRESSED_BYTES:
                return MemoryPackInspection(
                    False, None, counts, tuple(files),
                    ("Archive is too large when uncompressed "
                     "(possible zip bomb).",),
                )
            if errors:
                # A traversal / symlink / oversize member is fatal — do
                # not go on to parse anything from a hostile archive.
                return MemoryPackInspection(
                    False, None, counts, tuple(files),
                    tuple(errors), tuple(warnings),
                )

            manifest, m_errors = _read_manifest(zf)
            errors.extend(m_errors)

            payload, p_errors, p_warnings = _read_payload(zf)
            errors.extend(p_errors)
            warnings.extend(p_warnings)
            counts = _count_payload(payload)
    except zipfile.BadZipFile:
        return MemoryPackInspection(
            False, None, counts, tuple(files),
            ("Archive could not be read (corrupt zip).",),
        )

    return MemoryPackInspection(
        valid=not errors,
        manifest=manifest,
        counts=counts,
        files=tuple(files),
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def _empty_counts() -> dict:
    return {
        "memories": 0, "natural_memories": 0, "conversations": 0,
        "messages": 0, "settings": 0, "summaries": 0, "projects": 0,
    }


def _read_member_json(
    zf: zipfile.ZipFile, name: str,
) -> tuple[Optional[object], Optional[str]]:
    """Decompress + parse one JSON member with a hard size cap.

    Returns ``(parsed, error)``. ``parsed`` is ``None`` when the member
    is absent (no error) or unreadable (with an error string).
    """
    try:
        info = zf.getinfo(name)
    except KeyError:
        return None, None
    if info.file_size > MAX_JSON_BYTES:
        return None, f"{name} is too large to read."
    try:
        with zf.open(name) as fh:
            raw = fh.read(MAX_JSON_BYTES + 1)
    except (zipfile.BadZipFile, OSError):
        return None, f"{name} could not be read."
    if len(raw) > MAX_JSON_BYTES:
        return None, f"{name} is too large to read."
    try:
        return json.loads(raw.decode("utf-8")), None
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, f"{name} is not valid JSON."


def _read_manifest(
    zf: zipfile.ZipFile,
) -> tuple[Optional[dict], list[str]]:
    """Read + validate the manifest. Returns ``(manifest, errors)``."""
    errors: list[str] = []
    manifest, err = _read_member_json(zf, MANIFEST_NAME)
    if err:
        return None, [err]
    if manifest is None:
        return None, ["Archive is missing manifest.json."]
    if not isinstance(manifest, dict):
        return None, ["manifest.json is not a JSON object."]
    if manifest.get("format") != FORMAT_ID:
        errors.append(
            f"Unexpected pack format {manifest.get('format')!r} "
            f"(expected {FORMAT_ID!r})."
        )
    ver = manifest.get("format_version")
    if not isinstance(ver, int):
        errors.append("manifest.json has no integer format_version.")
    elif ver > FORMAT_VERSION:
        errors.append(
            f"This pack was created by a newer Nova (format_version "
            f"{ver}); this instance supports up to {FORMAT_VERSION}. "
            "Update Nova and try again."
        )
    return manifest, errors


def _read_payload(
    zf: zipfile.ZipFile,
) -> tuple[dict, list[str], list[str]]:
    """Read + shape-check the data files. Returns ``(payload, errs, warns)``."""
    errors: list[str] = []
    warnings: list[str] = []
    payload: dict = {}

    def _grab(name: str, key: str, kind: type):
        parsed, err = _read_member_json(zf, name)
        if err:
            errors.append(err)
            return
        if parsed is None:
            return  # optional file absent
        if not isinstance(parsed, dict):
            errors.append(f"{name} is not a JSON object.")
            return
        value = parsed.get(key)
        if value is None:
            return
        if not isinstance(value, kind):
            errors.append(f"{name} field {key!r} has the wrong type.")
            return
        payload[key] = value

    _grab(PROFILE_NAME, "user", dict)
    _grab(PROFILE_NAME, "personalization", dict)
    _grab(MEMORIES_NAME, "classic", list)
    _grab(MEMORIES_NAME, "natural", list)
    _grab(CONVERSATIONS_NAME, "conversations", list)
    _grab(SETTINGS_NAME, "user_settings", dict)
    _grab(SUMMARIES_NAME, "session_continuity", dict)

    return payload, errors, warnings


def _count_payload(payload: dict) -> dict:
    counts = _empty_counts()
    counts["memories"] = len(payload.get("classic", []) or [])
    counts["natural_memories"] = len(payload.get("natural", []) or [])
    convs = payload.get("conversations", []) or []
    counts["conversations"] = len(convs)
    counts["messages"] = sum(
        len(c.get("messages", []) or [])
        for c in convs if isinstance(c, dict)
    )
    counts["settings"] = len(payload.get("user_settings", {}) or {})
    sc = payload.get("session_continuity", {}) or {}
    counts["summaries"] = 1 if sc.get("has_continuity") else 0
    counts["projects"] = len(_payload_project_names(payload))
    return counts


def _payload_project_names(payload: dict) -> set[str]:
    """Distinct, non-empty project names referenced anywhere in a pack."""
    names: set[str] = set()
    for row in payload.get("classic", []) or []:
        _maybe_add_name(names, row)
    for row in payload.get("natural", []) or []:
        _maybe_add_name(names, row)
    for row in payload.get("conversations", []) or []:
        _maybe_add_name(names, row)
    return names


def _maybe_add_name(names: set[str], row: object) -> None:
    if isinstance(row, dict):
        p = row.get("project")
        if isinstance(p, str) and p.strip():
            names.add(p.strip())


# ── Import preview (dry-run) ────────────────────────────────────────


def build_import_preview(
    zip_bytes: bytes,
    user_id: int,
    *,
    db_path: Optional[str] = None,
) -> MemoryPackImportPreview:
    """Validate the pack and report what a merge would add vs. skip.

    Writes nothing. Counts are split into ``new`` / ``duplicate`` so
    the UI can show "X new memories, Y already known" before the user
    confirms.
    """
    inspection = inspect_memory_pack(zip_bytes)
    if not inspection.valid:
        return MemoryPackImportPreview(
            False, inspection.counts, inspection.manifest,
            inspection.warnings, inspection.errors,
        )

    payload = _parse_payload(zip_bytes)
    db = _resolve_db_path(db_path)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        existing = _load_existing_signatures(conn, user_id)
    finally:
        conn.close()

    counts = _preview_counts(payload, existing)
    return MemoryPackImportPreview(
        valid=True,
        counts=counts,
        manifest=inspection.manifest,
        warnings=inspection.warnings,
        errors=(),
    )


def _preview_counts(payload: dict, existing: dict) -> dict:
    classic = payload.get("classic", []) or []
    natural = payload.get("natural", []) or []
    convs = payload.get("conversations", []) or []
    settings = payload.get("user_settings", {}) or {}

    classic_new = sum(
        1 for r in classic
        if isinstance(r, dict)
        and (str(r.get("category", "")), _normalize(str(r.get("content", ""))))
        not in existing["classic"]
        and str(r.get("content", "")).strip()
    )
    natural_new = sum(
        1 for r in natural
        if isinstance(r, dict)
        and (str(r.get("kind", "")), str(r.get("topic", "")),
             _normalize(str(r.get("content", ""))))
        not in existing["natural"]
        and str(r.get("content", "")).strip()
    )
    conv_new_rows = [
        c for c in convs
        if isinstance(c, dict)
        and (str(c.get("title", "")), str(c.get("created", "")))
        not in existing["conversations"]
    ]
    conv_new = len(conv_new_rows)
    msgs_new = sum(len(c.get("messages", []) or []) for c in conv_new_rows)

    settings_safe = {
        k: v for k, v in settings.items()
        if not _is_secret_setting(str(k), str(v))
    }
    settings_apply = sum(
        1 for k in settings_safe if k not in existing["settings"]
    )
    settings_secret = len(settings) - len(settings_safe)

    pack_projects = _payload_project_names(payload)
    projects_new = len(pack_projects - existing["projects"])

    return {
        "memories": {
            "total": len(classic), "new": classic_new,
            "duplicate": len(classic) - classic_new,
        },
        "natural_memories": {
            "total": len(natural), "new": natural_new,
            "duplicate": len(natural) - natural_new,
        },
        "conversations": {
            "total": len(convs), "new": conv_new,
            "duplicate": len(convs) - conv_new,
        },
        "messages": {"total": msgs_new},
        "settings": {
            "total": len(settings), "applicable": settings_apply,
            "skipped_existing": len(settings_safe) - settings_apply,
            "skipped_secret": settings_secret,
        },
        "summaries": {
            "present": bool(
                (payload.get("session_continuity", {}) or {})
                .get("has_continuity")
            )
        },
        "projects": {
            "total": len(pack_projects), "new": projects_new,
            "existing": len(pack_projects) - projects_new,
        },
    }


# ── Import (merge, transactional) ───────────────────────────────────


def import_memory_pack(
    zip_bytes: bytes,
    user_id: int,
    *,
    confirm: bool = False,
    mode: str = "merge",
    db_path: Optional[str] = None,
    now: Optional[datetime] = None,
) -> MemoryPackImportResult:
    """Merge a Memory Pack into ``user_id``'s data inside one transaction.

    With ``confirm=False`` nothing is written (the caller is expected
    to show a preview first). ``mode`` must be ``"merge"`` in v1 — the
    only mode that exists; it keeps every existing row and adds only
    what is missing. On *any* error the transaction is rolled back so
    the database is left exactly as it was.
    """
    inspection = inspect_memory_pack(zip_bytes)
    if not inspection.valid:
        return MemoryPackImportResult(
            OUTCOME_REFUSED, {}, {}, inspection.manifest,
            inspection.warnings, inspection.errors,
        )
    if mode != "merge":
        return MemoryPackImportResult(
            OUTCOME_REFUSED, {}, {}, inspection.manifest, (),
            (f"Unsupported import mode {mode!r}; only 'merge' is "
             "available.",),
        )
    if not confirm:
        return MemoryPackImportResult(
            OUTCOME_UNCONFIRMED, {}, {}, inspection.manifest,
            ("Import not confirmed — nothing was written.",), (),
        )

    payload = _parse_payload(zip_bytes)
    db = _resolve_db_path(db_path)
    when = _now(now).isoformat()

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        imported, skipped, warnings = _apply_merge(
            conn, user_id, payload, when,
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - we must never leave a half import
        conn.rollback()
        logger.warning("Memory pack import failed and was rolled back: %s", exc)
        return MemoryPackImportResult(
            OUTCOME_FAILED, {}, {}, inspection.manifest, (),
            ("Import failed; no changes were made to your data.",),
        )
    finally:
        conn.close()

    return MemoryPackImportResult(
        outcome=OUTCOME_IMPORTED,
        imported=imported,
        skipped=skipped,
        manifest=inspection.manifest,
        warnings=tuple(warnings),
        errors=(),
    )


def _apply_merge(
    conn: sqlite3.Connection, user_id: int, payload: dict, when: str,
) -> tuple[dict, dict, list[str]]:
    """Insert missing rows on ``conn`` (already inside a transaction)."""
    existing = _load_existing_signatures(conn, user_id)
    project_ids = _load_existing_project_ids(conn, user_id)
    warnings: list[str] = []
    imported = {
        "memories": 0, "natural_memories": 0, "conversations": 0,
        "messages": 0, "settings": 0, "projects": 0,
    }
    skipped = {
        "memories_duplicate": 0, "natural_memories_duplicate": 0,
        "conversations_duplicate": 0, "settings_existing": 0,
        "settings_secret": 0,
    }

    def _project_id(name: object) -> Optional[int]:
        if not isinstance(name, str) or not name.strip():
            return None
        key = name.strip()
        if key in project_ids:
            return project_ids[key]
        cur = conn.execute(
            "INSERT INTO projects (user_id, name, description, "
            "created_at, updated_at) VALUES (?, ?, '', ?, ?)",
            (user_id, key, when, when),
        )
        new_id = int(cur.lastrowid)
        project_ids[key] = new_id
        imported["projects"] += 1
        return new_id

    # ── classic memories ──
    for row in payload.get("classic", []) or []:
        if not isinstance(row, dict):
            continue
        category = str(row.get("category", "") or "general")
        content = str(row.get("content", "") or "")
        if not content.strip():
            continue
        sig = (category, _normalize(content))
        if sig in existing["classic"]:
            skipped["memories_duplicate"] += 1
            continue
        conn.execute(
            "INSERT INTO memories (category, content, created, user_id, "
            "project_id) VALUES (?, ?, ?, ?, ?)",
            (category, content, str(row.get("created") or when), user_id,
             _project_id(row.get("project"))),
        )
        existing["classic"].add(sig)
        imported["memories"] += 1

    # ── natural memories ──
    for row in payload.get("natural", []) or []:
        if not isinstance(row, dict):
            continue
        content = str(row.get("content", "") or "")
        if not content.strip():
            continue
        kind = str(row.get("kind", "") or "general")
        if kind not in _VALID_KINDS:
            kind = "general"
        topic = str(row.get("topic", "") or "")
        sig = (kind, topic, _normalize(content))
        if sig in existing["natural"]:
            skipped["natural_memories_duplicate"] += 1
            continue
        try:
            confidence = float(row.get("confidence", 0.8))
        except (TypeError, ValueError):
            confidence = 0.8
        conn.execute(
            "INSERT INTO natural_memories (id, kind, topic, content, "
            "confidence, source, created_at, updated_at, last_seen_at, "
            "embedding, user_id, project_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (str(uuid.uuid4()), kind, topic, content, confidence,
             str(row.get("source") or "import"),
             str(row.get("created_at") or when),
             str(row.get("updated_at") or when),
             str(row.get("last_seen_at") or when),
             user_id, _project_id(row.get("project"))),
        )
        existing["natural"].add(sig)
        imported["natural_memories"] += 1

    # ── conversations + messages ──
    for conv in payload.get("conversations", []) or []:
        if not isinstance(conv, dict):
            continue
        title = str(conv.get("title", "") or "Imported conversation")
        created = str(conv.get("created") or when)
        sig = (title, created)
        if sig in existing["conversations"]:
            skipped["conversations_duplicate"] += 1
            continue
        cur = conn.execute(
            "INSERT INTO conversations (user_id, title, created, updated, "
            "project_id) VALUES (?, ?, ?, ?, ?)",
            (user_id, title, created, str(conv.get("updated") or created),
             _project_id(conv.get("project"))),
        )
        conv_id = int(cur.lastrowid)
        existing["conversations"].add(sig)
        imported["conversations"] += 1
        for msg in conv.get("messages", []) or []:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role", "") or "user")
            content = str(msg.get("content", "") or "")
            conn.execute(
                "INSERT INTO messages (conversation_id, role, content, "
                "model, created) VALUES (?, ?, ?, ?, ?)",
                (conv_id, role, content,
                 (msg.get("model") if isinstance(msg.get("model"), str)
                  else None),
                 str(msg.get("created") or created)),
            )
            imported["messages"] += 1

    # ── settings (fill missing only; never overwrite) ──
    _merge_settings(conn, user_id, payload, existing, imported, skipped,
                    warnings)

    return imported, skipped, warnings


def _merge_settings(
    conn, user_id, payload, existing, imported, skipped, warnings,
) -> None:
    """Apply safe, non-existing per-user settings inside the transaction."""
    settings = payload.get("user_settings", {}) or {}
    if not settings:
        return
    try:
        from core.settings import (
            PERSONALIZATION_ENUMS, CUSTOM_INSTRUCTIONS_MAX_LEN,
        )
    except Exception:  # pragma: no cover - defensive
        PERSONALIZATION_ENUMS, CUSTOM_INSTRUCTIONS_MAX_LEN = {}, 1000

    for key, value in settings.items():
        key = str(key)
        value = "" if value is None else str(value)
        if _is_secret_setting(key, value):
            skipped["settings_secret"] += 1
            continue
        if key in existing["settings"]:
            skipped["settings_existing"] += 1
            continue
        # Validate known personalization values; skip anything invalid
        # rather than poisoning a column the UI cannot render.
        if key in PERSONALIZATION_ENUMS and value not in PERSONALIZATION_ENUMS[key]:
            warnings.append(f"Skipped setting {key!r}: value not recognised.")
            continue
        if key == "custom_instructions" and len(value) > CUSTOM_INSTRUCTIONS_MAX_LEN:
            value = value[:CUSTOM_INSTRUCTIONS_MAX_LEN]
        conn.execute(
            "INSERT INTO user_settings (user_id, key, value) "
            "VALUES (?, ?, ?) ON CONFLICT(user_id, key) DO NOTHING",
            (user_id, key, value),
        )
        existing["settings"].add(key)
        imported["settings"] += 1


def _load_existing_signatures(
    conn: sqlite3.Connection, user_id: int,
) -> dict:
    """Read content signatures of the user's current data for dedup."""
    out = {
        "classic": set(), "natural": set(), "conversations": set(),
        "settings": set(), "projects": set(),
    }
    try:
        for r in conn.execute(
            "SELECT category, content FROM memories WHERE user_id = ?",
            (user_id,),
        ):
            out["classic"].add((str(r["category"]), _normalize(str(r["content"]))))
    except sqlite3.OperationalError:
        pass
    try:
        for r in conn.execute(
            "SELECT kind, topic, content FROM natural_memories "
            "WHERE user_id = ?", (user_id,),
        ):
            out["natural"].add(
                (str(r["kind"]), str(r["topic"]), _normalize(str(r["content"])))
            )
    except sqlite3.OperationalError:
        pass
    try:
        for r in conn.execute(
            "SELECT title, created FROM conversations WHERE user_id = ?",
            (user_id,),
        ):
            out["conversations"].add((str(r["title"]), str(r["created"])))
    except sqlite3.OperationalError:
        pass
    try:
        for r in conn.execute(
            "SELECT key FROM user_settings WHERE user_id = ?", (user_id,),
        ):
            out["settings"].add(str(r["key"]))
    except sqlite3.OperationalError:
        pass
    try:
        for r in conn.execute(
            "SELECT name FROM projects WHERE user_id = ?", (user_id,),
        ):
            out["projects"].add(str(r["name"]))
    except sqlite3.OperationalError:
        pass
    return out


def _load_existing_project_ids(
    conn: sqlite3.Connection, user_id: int,
) -> dict[str, int]:
    try:
        rows = conn.execute(
            "SELECT id, name FROM projects WHERE user_id = ?", (user_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {str(r["name"]): int(r["id"]) for r in rows}


def _parse_payload(zip_bytes: bytes) -> dict:
    """Re-open a validated pack and return its parsed data files.

    Assumes the archive already passed :func:`inspect_memory_pack`;
    used by preview / import after validation so a hostile archive can
    never reach this point.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        payload, _, _ = _read_payload(zf)
    return payload


# ── Persistence helper (Docker-friendly) ────────────────────────────


def write_pack_to_data_dir(
    result: MemoryPackResult,
    *,
    dest_dir: Optional[str] = None,
) -> Optional[Path]:
    """Write the pack into ``NOVA_DATA_DIR/memory-packs`` (best effort).

    Returns the written path, or ``None`` if persistence failed (the
    caller still streams the bytes to the user, so a read-only data
    directory is non-fatal). Under Docker ``NOVA_DATA_DIR=/data`` so
    the file lands on the mounted ``nova-data`` volume.
    """
    try:
        from core import paths as _paths
        if dest_dir is not None:
            target_dir = Path(dest_dir)
        else:
            target_dir = _paths.memory_packs_dir()
            if not target_dir.is_absolute():
                target_dir = _paths.effective_data_root() / target_dir.name
        target_dir.mkdir(parents=True, exist_ok=True)
        out = target_dir / result.filename
        tmp = target_dir / (result.filename + ".partial")
        tmp.write_bytes(result.data)
        tmp.replace(out)
        return out
    except OSError as exc:
        logger.warning("Could not persist memory pack to data dir: %s", exc)
        return None
