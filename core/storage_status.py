"""Nova storage status reporter (Phase 1 of the Storage & Migration Center).

This module answers the question "where is Nova storing its data,
and is that location healthy?" — calmly, read-only, with no side
effects on disk.

What the module reports for each known Nova path
(``NOVA_DATA_DIR``, the database file, ``backups/``, ``exports/``,
``memory-packs/``, ``logs/``, optional Ollama models path):

* the configured / resolved absolute path,
* whether the path exists, is a directory, and is writable,
* a best-effort free-disk-space figure (``None`` when the call fails),
* a coarse mount classification (``stable`` / ``transient`` /
  ``user_home`` / ``tmp`` / ``other``) used to warn operators when
  Nova is pointed at a path that is unsafe for a 24/7 service
  (``/run/media/...``, ``/tmp/...``, …),
* free-form warnings the UI can render verbatim.

Safety contract:

* The module **never writes, moves, or deletes** anything. The
  status snapshot is computed from ``os.stat`` / ``os.access`` /
  ``shutil.disk_usage`` and a small handful of string heuristics.
* It **never follows symlinks**: the mount classification reads the
  lexical path, the existence / writability probes use ``os.access``
  / ``os.path.isdir`` without resolving symlinks. We rely on
  ``Path.is_dir`` (which does follow symlinks) only for explicit
  "is this path a directory?" probes — the result is a flag, not an
  action.
* It **never imports ``subprocess``** and never reaches the network.
* It **never imports ``core.memory``**: the path layer lives in
  ``core.paths`` and this module imports only from there. No import
  cycles.
* It **never reveals secrets**. The Ollama path is the only env-derived
  value surfaced, and only the directory itself — never its contents.

The module is the read-only foundation under the admin-only
``/admin/storage/status`` endpoint. ``core/data_export.py`` reuses
the path resolution but performs its own (also explicit) safety
checks before reading any file content.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from core import paths as _paths

logger = logging.getLogger(__name__)


# ── Constants ───────────────────────────────────────────────────────

#: Coarse mount classifications. Each value is a fixed string the UI
#: can switch on without re-deriving the rules.
MOUNT_STABLE = "stable"           # /mnt/..., /var/lib/..., /srv/..., /opt/...
MOUNT_TRANSIENT = "transient"     # /run/media/..., /media/...
MOUNT_USER_HOME = "user_home"     # under $HOME
MOUNT_TMP = "tmp"                 # /tmp/..., /var/tmp/...
MOUNT_OTHER = "other"             # anything else (incl. relative paths)

#: Mount classes that should raise a warning for a long-running service.
_WARN_MOUNT_CLASSES: frozenset[str] = frozenset({MOUNT_TRANSIENT, MOUNT_TMP})

#: Path prefixes that signal each mount class. Order matters: the
#: first prefix that matches wins. The lists are intentionally
#: conservative — Nova warns only on paths it is confident about.
_STABLE_PREFIXES: tuple[str, ...] = (
    "/mnt/", "/var/lib/", "/srv/", "/opt/", "/data/",
)
_TRANSIENT_PREFIXES: tuple[str, ...] = (
    "/run/media/", "/media/",
)
_TMP_PREFIXES: tuple[str, ...] = (
    "/tmp/", "/var/tmp/",
)

#: Optional Ollama models environment variable. Empty / unset means
#: "Ollama uses its default model location" — usually ``~/.ollama/models``
#: on Linux. Nova never moves Ollama models; this status is purely
#: informational.
OLLAMA_MODELS_ENV = "OLLAMA_MODELS"
OLLAMA_DEFAULT_REL = ".ollama/models"


# ── Dataclasses ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class PathStatus:
    """Health snapshot for a single Nova path.

    Every attribute is safe to serialise to JSON. Paths are surfaced
    as strings (their absolute form) so the admin UI does not have to
    deal with ``Path`` objects. ``None`` values are kept as ``None``
    so the UI can render "unknown" rather than guessing.

    The fields are deliberately read-only:

    * ``name`` — short identifier the UI can switch on (``data_dir``,
      ``database``, ``backups``, …).
    * ``label`` — human-readable label for display.
    * ``path`` — absolute path string, or ``""`` when unconfigured.
    * ``configured`` — true when the underlying env var is set or
      the path is otherwise an explicit user choice.
    * ``exists`` — ``os.path.exists`` on the path.
    * ``is_dir`` — ``Path.is_dir()`` on the path (False for files,
      missing entries, or anything else).
    * ``writable`` — best-effort ``os.access(..., os.W_OK)`` on the
      path (or its first existing parent when the path itself is
      missing) — ``None`` when we cannot probe.
    * ``free_bytes`` — ``shutil.disk_usage(path).free`` against the
      first existing component, or ``None`` on failure.
    * ``total_bytes`` — ``shutil.disk_usage(path).total`` likewise.
    * ``mount_class`` — coarse classification (``stable`` /
      ``transient`` / ``user_home`` / ``tmp`` / ``other``).
    * ``warnings`` — short messages the UI can render verbatim.
    """

    name: str
    label: str
    path: str
    configured: bool
    exists: bool = False
    is_dir: bool = False
    writable: Optional[bool] = None
    free_bytes: Optional[int] = None
    total_bytes: Optional[int] = None
    mount_class: str = MOUNT_OTHER
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "path": self.path,
            "configured": self.configured,
            "exists": self.exists,
            "is_dir": self.is_dir,
            "writable": self.writable,
            "free_bytes": self.free_bytes,
            "total_bytes": self.total_bytes,
            "mount_class": self.mount_class,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class StorageStatus:
    """Top-level storage report for the admin UI / API.

    The structure is intentionally flat: a list of :class:`PathStatus`
    entries (in display order), plus a small set of top-level
    warnings that apply to the deployment as a whole (for example:
    ``NOVA_DATA_DIR`` is unset, or the data dir lives inside a Git
    checkout). The recommendations field is a static list the UI can
    render under a "Recommended layout" header — it never changes
    based on what is on disk so the wording stays stable.
    """

    data_dir_configured: bool
    data_dir: str
    paths: tuple[PathStatus, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)
    recommendations: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "data_dir_configured": self.data_dir_configured,
            "data_dir": self.data_dir,
            "paths": [p.as_dict() for p in self.paths],
            "warnings": list(self.warnings),
            "recommendations": list(self.recommendations),
        }


# ── Helpers ─────────────────────────────────────────────────────────


def classify_mount(path: str | os.PathLike[str]) -> str:
    """Return a coarse mount classification for ``path``.

    The classification is **lexical only** — we never call ``stat``,
    never read ``/proc/mounts``, and never follow symlinks. The goal
    is to surface a clear warning for paths that are known-unsafe for
    a long-running service (``/run/media/...``, ``/tmp/...``), not to
    prove that a path is mounted correctly.

    Returns one of ``MOUNT_STABLE`` / ``MOUNT_TRANSIENT`` /
    ``MOUNT_USER_HOME`` / ``MOUNT_TMP`` / ``MOUNT_OTHER``. Empty or
    relative paths normalise to ``MOUNT_OTHER`` — Nova never assumes
    a relative path is on a stable disk.
    """
    if not path:
        return MOUNT_OTHER
    s = str(path)
    if not s.startswith("/"):
        return MOUNT_OTHER
    # Normalise trailing slashes so "/run/media" classifies the same
    # as "/run/media/" without re-deriving the boundary in each rule.
    probe = s if s.endswith("/") else s + "/"
    for prefix in _TRANSIENT_PREFIXES:
        if probe.startswith(prefix):
            return MOUNT_TRANSIENT
    for prefix in _TMP_PREFIXES:
        if probe.startswith(prefix):
            return MOUNT_TMP
    for prefix in _STABLE_PREFIXES:
        if probe.startswith(prefix):
            return MOUNT_STABLE
    home = os.environ.get("HOME", "")
    if home and home.startswith("/"):
        home_prefix = home if home.endswith("/") else home + "/"
        if probe.startswith(home_prefix) or s == home:
            return MOUNT_USER_HOME
    return MOUNT_OTHER


def _first_existing_ancestor(path: Path) -> Optional[Path]:
    """Return the first existing ancestor of ``path`` (including itself).

    Used by ``disk_usage`` and writability probes when the target path
    does not yet exist — we still want to report the disk it would
    land on. Returns ``None`` if no ancestor exists (only possible on
    a bizarre filesystem).
    """
    current: Optional[Path] = path
    while current is not None:
        try:
            if current.exists():
                return current
        except OSError:
            return None
        if current.parent == current:
            return None
        current = current.parent
    return None


def _safe_disk_usage(path: Path) -> tuple[Optional[int], Optional[int]]:
    """Return ``(free, total)`` bytes for the disk hosting ``path``.

    Falls back to the first existing ancestor when ``path`` itself is
    missing — this lets the UI report disk space for a configured but
    not-yet-created data directory. Returns ``(None, None)`` on any
    OS-level failure so callers can render "unknown" without crashing.
    """
    target = path if path.exists() else _first_existing_ancestor(path)
    if target is None:
        return None, None
    try:
        usage = shutil.disk_usage(str(target))
    except (OSError, ValueError):
        return None, None
    return int(usage.free), int(usage.total)


def _safe_writable(path: Path) -> Optional[bool]:
    """Return whether the running user can write to ``path``.

    When the path itself exists, ``os.access(path, W_OK)`` is the
    direct answer. When it does not, we probe its first existing
    ancestor — that is the directory ``prepare()`` would try to
    ``mkdir`` into. Returns ``None`` on probe failure.
    """
    try:
        if path.exists():
            return bool(os.access(str(path), os.W_OK))
    except OSError:
        return None
    parent = _first_existing_ancestor(path)
    if parent is None:
        return None
    try:
        return bool(os.access(str(parent), os.W_OK))
    except OSError:
        return None


def _is_dir(path: Path) -> bool:
    """Return ``True`` if ``path`` is a directory, ``False`` otherwise."""
    try:
        return path.is_dir()
    except OSError:
        return False


def _exists(path: Path) -> bool:
    """Return ``True`` if ``path`` exists, ``False`` otherwise."""
    try:
        return path.exists()
    except OSError:
        return False


def _looks_like_repo_root(path: Path) -> bool:
    """Return True when ``path`` itself contains a ``.git`` directory.

    Used to detect "Nova data is sitting inside the Git checkout" —
    the layout the safety contract calls out as a leak risk. We do
    not walk parents: the legacy mode lands ``nova.db`` at the
    checkout root, which is the case we care about.
    """
    try:
        return (path / ".git").exists()
    except OSError:
        return False


# ── Per-path builders ───────────────────────────────────────────────


def _build_path_status(
    name: str,
    label: str,
    path: Path,
    *,
    configured: bool,
    require_dir: bool,
    extra_warnings: Iterable[str] = (),
    disk_usage: Optional[tuple[Optional[int], Optional[int]]] = None,
) -> PathStatus:
    """Compute a :class:`PathStatus` for a single Nova path.

    The function deliberately avoids any side effects: no ``mkdir``,
    no ``touch``, no symlink resolution. ``require_dir`` controls
    whether the absence of the path counts as a problem worth
    surfacing as a warning (``True`` for directories, ``False`` for
    the database file which is allowed to be missing before first
    run). ``disk_usage`` lets callers pass a pre-computed
    ``(free, total)`` so we don't statvfs every row when all paths
    share a filesystem.
    """
    abs_path = str(path)
    exists = _exists(path)
    is_dir = _is_dir(path) if exists else False
    writable = _safe_writable(path)
    if disk_usage is None:
        free_bytes, total_bytes = _safe_disk_usage(path)
    else:
        free_bytes, total_bytes = disk_usage
    mount_class = classify_mount(abs_path)

    warnings: list[str] = list(extra_warnings)
    if mount_class == MOUNT_TRANSIENT:
        warnings.append(
            "Path is on a transient mount (e.g. /run/media/...). "
            "systemd will not wait for it and the disk disappears on "
            "logout — unsafe for a 24/7 service. Move data to a stable "
            "mount declared in /etc/fstab."
        )
    elif mount_class == MOUNT_TMP:
        warnings.append(
            "Path is under /tmp. /tmp is cleared on reboot — data "
            "stored here will not survive a restart."
        )
    if exists and require_dir and not is_dir:
        warnings.append(
            "Path exists but is not a directory."
        )
    if exists and writable is False:
        warnings.append(
            "Path is not writable by the running user."
        )

    return PathStatus(
        name=name,
        label=label,
        path=abs_path,
        configured=configured,
        exists=exists,
        is_dir=is_dir,
        writable=writable,
        free_bytes=free_bytes,
        total_bytes=total_bytes,
        mount_class=mount_class,
        warnings=tuple(warnings),
    )


def _ollama_models_path() -> tuple[Path, bool]:
    """Return ``(path, configured)`` for the Ollama models directory.

    ``OLLAMA_MODELS`` selects the directory; when unset, Nova falls
    back to the documented Linux default ``~/.ollama/models`` (purely
    informational — Nova never moves Ollama models). ``configured``
    is ``True`` when the env var is set.
    """
    raw = os.environ.get(OLLAMA_MODELS_ENV, "").strip()
    if raw:
        return Path(raw).expanduser(), True
    home = os.environ.get("HOME", "").strip()
    if home:
        return Path(home).expanduser() / OLLAMA_DEFAULT_REL, False
    # No HOME, no OLLAMA_MODELS — return a clearly-unconfigured
    # marker that the UI can render as "unknown".
    return Path(""), False


# ── Public entry point ─────────────────────────────────────────────


def _recommendations() -> tuple[str, ...]:
    """Return the static recommendations rendered under the report.

    These are intentionally fixed strings — the recommendation set
    does not depend on what is on disk, so the wording stays stable
    across calls and is easy to test. Keep this aligned with
    ``docs/data-directory.md`` and ``docs/storage-and-migration.md``.
    """
    return (
        "Active database belongs on SSD — for example "
        "/mnt/fastdata/NovaData. SQLite writes are synchronous and "
        "HDDs make chat feel laggy.",
        "Backups and archives belong on HDD or NAS — for example "
        "/mnt/archive/Backups/Nova. Backups are written rarely and "
        "do not need fast random access.",
        "Use stable mount paths declared in /etc/fstab. Avoid "
        "/run/media/<user>/<disk> for a long-running service; it "
        "disappears on logout and corrupts the database mid-write.",
        "Do not store Nova's runtime data inside the Git checkout — "
        "private data must never be committed.",
        "Ollama models are owned by Ollama, not by Nova. Nova export "
        "packages do not include model files; re-pull on the target "
        "machine after restore.",
    )


def get_storage_status() -> StorageStatus:
    """Return a calm, read-only snapshot of Nova's storage layout.

    Safe to call at any time and from any thread; never raises into
    the caller. A probe failure (a permission error, a stat failure,
    a missing parent) surfaces as ``None`` in the affected field
    rather than as an exception.
    """
    data_dir = _paths.configured_data_dir()
    data_dir_configured = data_dir is not None
    effective_root = _paths.effective_data_root()

    paths: list[PathStatus] = []

    # Memoise disk-usage calls by filesystem device: every Nova path
    # is usually on the same disk, so we shouldn't call statvfs once
    # per row.
    disk_cache: dict[int, tuple[Optional[int], Optional[int]]] = {}

    def _disk_usage(path: Path) -> tuple[Optional[int], Optional[int]]:
        target = path if path.exists() else _first_existing_ancestor(path)
        if target is None:
            return None, None
        try:
            dev = os.stat(str(target)).st_dev
        except OSError:
            return _safe_disk_usage(path)
        if dev in disk_cache:
            return disk_cache[dev]
        result = _safe_disk_usage(path)
        disk_cache[dev] = result
        return result

    data_warnings: list[str] = []
    if not data_dir_configured:
        data_warnings.append(
            "NOVA_DATA_DIR is not set. Nova stores nova.db next to "
            "the running checkout — set NOVA_DATA_DIR=/mnt/fastdata/"
            "NovaData (or similar) so private data lives on a "
            "dedicated disk."
        )
    if _looks_like_repo_root(effective_root):
        data_warnings.append(
            "Data directory contains a .git directory — private "
            "runtime data is sitting inside a Git checkout. Move "
            "the data directory outside the repository."
        )
    paths.append(_build_path_status(
        name="data_dir",
        label="Nova data directory",
        path=effective_root,
        configured=data_dir_configured,
        require_dir=True,
        extra_warnings=data_warnings,
        disk_usage=_disk_usage(effective_root),
    ))

    # ``require_dir=False`` for the database: it is a file, and a
    # pre-first-run install may not have it yet.
    db_path = _paths.database_path()
    db_abs = db_path if db_path.is_absolute() else (effective_root / db_path.name)
    db_warnings: list[str] = []
    if _exists(db_abs) and not db_abs.is_file():
        db_warnings.append("Database path exists but is not a regular file.")
    paths.append(_build_path_status(
        name="database",
        label="SQLite database (nova.db)",
        path=db_abs,
        configured=data_dir_configured,
        require_dir=False,
        extra_warnings=db_warnings,
        disk_usage=_disk_usage(db_abs),
    ))

    for name, label, sub in (
        ("backups", "Backups directory", _paths.BACKUPS_SUBDIR),
        ("exports", "Exports directory", _paths.EXPORTS_SUBDIR),
        ("memory_packs", "Memory packs directory", _paths.MEMORY_PACKS_SUBDIR),
        ("logs", "Logs directory", _paths.LOGS_SUBDIR),
    ):
        sub_abs = effective_root / sub
        paths.append(_build_path_status(
            name=name,
            label=label,
            path=sub_abs,
            configured=data_dir_configured,
            require_dir=True,
            disk_usage=_disk_usage(sub_abs),
        ))

    ollama_path, ollama_configured = _ollama_models_path()
    ollama_warnings: list[str] = []
    if not ollama_configured:
        ollama_warnings.append(
            "OLLAMA_MODELS is not set; falling back to the documented "
            "default. This is informational only — Nova never moves "
            "Ollama models and never includes them in export packages."
        )
    paths.append(_build_path_status(
        name="ollama_models",
        label="Ollama models directory (informational)",
        path=ollama_path,
        configured=ollama_configured,
        require_dir=True,
        extra_warnings=ollama_warnings,
        disk_usage=_disk_usage(ollama_path) if str(ollama_path) else (None, None),
    ))

    # Promote any per-path warning that should also be visible at the
    # top level. Today this is just the "data directory inside a
    # checkout" rule — the mount-class warnings stay per-path so the
    # UI can highlight the specific row.
    top_warnings: list[str] = []
    if not data_dir_configured:
        top_warnings.append(
            "NOVA_DATA_DIR is not set. Nova is running in legacy mode; "
            "see docs/storage-and-migration.md for the recommended "
            "configuration."
        )
    for entry in paths:
        if entry.name == "data_dir":
            for warning in entry.warnings:
                # Forward the "inside git checkout" warning specifically.
                if "Git checkout" in warning:
                    top_warnings.append(warning)

    return StorageStatus(
        data_dir_configured=data_dir_configured,
        data_dir=str(effective_root),
        paths=tuple(paths),
        warnings=tuple(top_warnings),
        recommendations=_recommendations(),
    )


__all__ = [
    "MOUNT_OTHER",
    "MOUNT_STABLE",
    "MOUNT_TMP",
    "MOUNT_TRANSIENT",
    "MOUNT_USER_HOME",
    "OLLAMA_DEFAULT_REL",
    "OLLAMA_MODELS_ENV",
    "PathStatus",
    "StorageStatus",
    "classify_mount",
    "get_storage_status",
]
