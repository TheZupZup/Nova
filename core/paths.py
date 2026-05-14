"""Nova data directory resolution helpers (Phase 1).

This module centralises the question "where does Nova store its
runtime data?". Today that data is a single SQLite file (``nova.db``)
plus a small set of sidecar backups, written next to the application
checkout. Operators who want to put Nova's data on a dedicated disk
should set::

    NOVA_DATA_DIR=/mnt/fastdata/NovaData

and Nova will use that directory as the parent of ``nova.db`` and any
other persistent runtime files. When the environment variable is unset
(or empty), Nova preserves its legacy behaviour — files are written
relative to the current working directory, exactly as before this
module existed.

Design contracts:

* Path resolution always returns ``pathlib.Path`` objects so callers
  do not have to think about leading slashes or trailing separators.
* :func:`prepare` is the single side-effecting function. It creates
  Nova-owned subdirectories only when ``NOVA_DATA_DIR`` is set, and it
  raises a clear :class:`RuntimeError` if the directory cannot be used.
  When ``NOVA_DATA_DIR`` is unset, :func:`prepare` is a strict no-op
  so existing installs and tests that point Nova at a ``tmp_path`` are
  not affected.
* No data is moved, copied, or deleted by this module. Migration from
  a legacy ``./nova.db`` is documented in ``docs/data-directory.md``
  and remains a manual operator step in Phase 1.
* The module has no Nova-internal imports — it is import-cycle safe.
  ``core.memory`` and ``memory.store`` import from here, never the
  other way around.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Environment variable that selects the Nova data root. Reading the
# variable each call (instead of caching at import time) lets pytest's
# ``monkeypatch.setenv`` propagate without forcing a module reload.
ENV_VAR = "NOVA_DATA_DIR"

# Names of the canonical files / subdirectories under the data root.
# Listed centrally so docs and systemd guidance can point at a single
# place when an operator wants to back the directory up, set
# permissions, or audit what Nova writes.
DB_FILENAME = "nova.db"
BACKUPS_SUBDIR = "backups"
EXPORTS_SUBDIR = "exports"
MEMORY_PACKS_SUBDIR = "memory-packs"
LOGS_SUBDIR = "logs"

_SUBDIRS: tuple[str, ...] = (
    BACKUPS_SUBDIR,
    EXPORTS_SUBDIR,
    MEMORY_PACKS_SUBDIR,
    LOGS_SUBDIR,
)


def configured_data_dir() -> Path | None:
    """Return the configured Nova data directory, or ``None`` when unset.

    ``NOVA_DATA_DIR`` is read fresh on every call so tests can override
    it through ``monkeypatch.setenv``. Empty / whitespace-only values
    normalise to ``None`` so a blank line in ``.env`` behaves the same
    as "not configured" — Nova never silently writes to ``""``.
    """
    raw = os.environ.get(ENV_VAR, "")
    if not raw or not raw.strip():
        return None
    return Path(raw.strip()).expanduser()


def database_path() -> Path:
    """Return the path to ``nova.db`` honouring ``NOVA_DATA_DIR``.

    With ``NOVA_DATA_DIR`` unset the function returns the legacy
    relative path (``nova.db`` in the current working directory) so
    existing installs keep working unchanged. With it set, the path
    is ``<NOVA_DATA_DIR>/nova.db``.
    """
    root = configured_data_dir()
    if root is None:
        return Path(DB_FILENAME)
    return root / DB_FILENAME


def backups_dir() -> Path:
    """Return the path of the ``backups`` subdirectory.

    Falls back to a relative ``backups`` path when ``NOVA_DATA_DIR`` is
    unset. The directory is created by :func:`prepare` only when the
    env var is set; callers that bypass :func:`prepare` should still
    tolerate its absence.
    """
    root = configured_data_dir()
    if root is None:
        return Path(BACKUPS_SUBDIR)
    return root / BACKUPS_SUBDIR


def exports_dir() -> Path:
    """Return the path of the ``exports`` subdirectory."""
    root = configured_data_dir()
    if root is None:
        return Path(EXPORTS_SUBDIR)
    return root / EXPORTS_SUBDIR


def memory_packs_dir() -> Path:
    """Return the path of the ``memory-packs`` subdirectory."""
    root = configured_data_dir()
    if root is None:
        return Path(MEMORY_PACKS_SUBDIR)
    return root / MEMORY_PACKS_SUBDIR


def logs_dir() -> Path:
    """Return the path of the ``logs`` subdirectory."""
    root = configured_data_dir()
    if root is None:
        return Path(LOGS_SUBDIR)
    return root / LOGS_SUBDIR


@dataclass(frozen=True)
class LegacyMigrationStatus:
    """Read-only snapshot of the legacy → ``NOVA_DATA_DIR`` state.

    Returned by :func:`describe_legacy_migration` so operators (and
    future admin endpoints) can see what *would* be migrated without
    performing any move. The dataclass deliberately exposes no methods —
    it is a value object, not a worker.
    """

    legacy_db_path: Path
    configured_db_path: Path
    legacy_exists: bool
    configured_exists: bool
    should_advise_copy: bool


def describe_legacy_migration() -> LegacyMigrationStatus | None:
    """Return a snapshot of the legacy → ``NOVA_DATA_DIR`` migration state.

    Returns ``None`` when ``NOVA_DATA_DIR`` is unset — there is no
    target to migrate to. Otherwise the returned status reports:

    * ``legacy_db_path`` — the absolute legacy location (``./nova.db``).
    * ``configured_db_path`` — the absolute path under ``NOVA_DATA_DIR``.
    * ``legacy_exists`` / ``configured_exists`` — file presence flags.
    * ``should_advise_copy`` — true only when a legacy DB is sitting
      next to the checkout and ``NOVA_DATA_DIR`` is empty.

    The function is **read-only**. It never moves files, never deletes
    anything, and never writes. ``docs/data-directory.md`` documents
    the manual copy procedure an operator should follow.
    """
    root = configured_data_dir()
    if root is None:
        return None
    legacy = Path(DB_FILENAME).resolve()
    configured = (root / DB_FILENAME).resolve()
    legacy_exists = legacy.exists()
    configured_exists = configured.exists()
    should_advise_copy = (
        legacy_exists
        and not configured_exists
        and legacy != configured
    )
    return LegacyMigrationStatus(
        legacy_db_path=legacy,
        configured_db_path=configured,
        legacy_exists=legacy_exists,
        configured_exists=configured_exists,
        should_advise_copy=should_advise_copy,
    )


def prepare() -> Path | None:
    """Create the Nova data directory and its subdirectories.

    When ``NOVA_DATA_DIR`` is unset, this is a strict no-op and returns
    ``None`` — legacy behaviour is preserved, and tests that point Nova
    at a ``tmp_path`` see no extra side effects.

    When ``NOVA_DATA_DIR`` is set, this function:

    * Creates the data directory with ``mkdir -p`` semantics.
    * Creates the standard subdirectories (``backups``, ``exports``,
      ``memory-packs``, ``logs``) so future PRs do not have to
      defensively re-create them.
    * Verifies the directory is writable by the running process.

    Raises :class:`RuntimeError` with a sanitised message when the
    directory is unusable. The error references the configured path
    and the underlying syscall failure — it never leaks internal state.
    """
    root = configured_data_dir()
    if root is None:
        return None

    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"Nova data directory {root!s} could not be created: "
            f"{exc.strerror or exc}"
        ) from exc

    if not root.is_dir():
        raise RuntimeError(
            f"Nova data directory {root!s} exists but is not a directory."
        )

    if not os.access(root, os.W_OK):
        raise RuntimeError(
            f"Nova data directory {root!s} is not writable by the "
            "current user."
        )

    for name in _SUBDIRS:
        sub = root / name
        try:
            sub.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeError(
                f"Nova data subdirectory {sub!s} could not be created: "
                f"{exc.strerror or exc}"
            ) from exc

    return root
