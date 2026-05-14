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

The module also exposes :func:`init_workspace` and a small CLI
entry point (``python -m core.paths init-workspace <parent>``) that
scaffolds the optional "Nova Portable Workspace" layout — a
self-contained parent folder that bundles the Git checkout, data
directory, logs, backups, config, and helper scripts. See
``docs/portable-workspace.md`` for the full walkthrough.

Design contracts:

* Path resolution always returns ``pathlib.Path`` objects so callers
  do not have to think about leading slashes or trailing separators.
* :func:`prepare` is the single side-effecting function for runtime
  path setup. It creates Nova-owned subdirectories only when
  ``NOVA_DATA_DIR`` is set, and it raises a clear
  :class:`RuntimeError` if the directory cannot be used. When
  ``NOVA_DATA_DIR`` is unset, :func:`prepare` is a strict no-op so
  existing installs and tests that point Nova at a ``tmp_path`` are
  not affected.
* :func:`init_workspace` is the side-effecting helper for the
  portable layout. It is **purely additive**: it creates missing
  directories with ``mkdir -p`` semantics, writes a single example
  env file when it does not already exist, and never overwrites,
  moves, or deletes anything.
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


def effective_data_root() -> Path:
    """Return the absolute parent directory of every Nova-owned file.

    With ``NOVA_DATA_DIR`` set this is identical to
    :func:`configured_data_dir`. Without it, Nova runs in legacy mode
    and the data root is the parent of the resolved
    :func:`database_path` — i.e. the current working directory. The
    helper exists so downstream modules (status reporter, export
    builder, admin endpoints) all agree on a single "where does Nova
    live?" answer instead of re-deriving the fallback.
    """
    root = configured_data_dir()
    if root is not None:
        return root
    return database_path().resolve().parent


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


# ── Nova Portable Workspace ─────────────────────────────────────────
#
# The "portable workspace" is an optional layout that bundles Nova's
# Git checkout, runtime data, logs, backups, config, and helper
# scripts in a single parent folder so the whole thing can be moved
# between disks or machines as one unit. The layout is purely a
# convention enforced by :func:`init_workspace`:
#
#     <parent>/
#       app/                # Git checkout goes here (operator clones)
#       data/               # NOVA_DATA_DIR points here
#       logs/               # local log files (future use)
#       backups/            # explicit backup packs
#       config/             # nova.env, nova.env.example
#       scripts/            # operator-owned helper scripts
#
# Private data lives outside ``app/`` so it cannot accidentally be
# committed to Git. Nothing in this module assumes the operator
# placed the checkout under ``app/``; if they put it elsewhere, the
# rest of the layout still works.

WORKSPACE_APP_SUBDIR = "app"
WORKSPACE_DATA_SUBDIR = "data"
WORKSPACE_LOGS_SUBDIR = "logs"
WORKSPACE_BACKUPS_SUBDIR = "backups"
WORKSPACE_CONFIG_SUBDIR = "config"
WORKSPACE_SCRIPTS_SUBDIR = "scripts"

# ``app/`` is intentionally created so the layout is visible after
# init, but ``init_workspace`` never clones anything into it.
_WORKSPACE_SUBDIRS: tuple[str, ...] = (
    WORKSPACE_APP_SUBDIR,
    WORKSPACE_DATA_SUBDIR,
    WORKSPACE_LOGS_SUBDIR,
    WORKSPACE_BACKUPS_SUBDIR,
    WORKSPACE_CONFIG_SUBDIR,
    WORKSPACE_SCRIPTS_SUBDIR,
)

WORKSPACE_ENV_EXAMPLE_NAME = "nova.env.example"


def _workspace_env_example_body(data_dir: Path) -> str:
    """Return the contents of ``config/nova.env.example``.

    The body is deliberately minimal: a single ``NOVA_DATA_DIR=`` line
    that points at the workspace's ``data/`` directory, plus comments
    explaining how systemd consumes the file via ``EnvironmentFile=``.
    Operators copy the example to ``nova.env`` and edit from there.
    """
    return (
        "# Nova Portable Workspace — example environment file.\n"
        "#\n"
        "# Copy this file to nova.env in the same directory, then edit\n"
        "# values as needed. systemd can read nova.env directly via:\n"
        "#\n"
        "#     [Service]\n"
        f"#     EnvironmentFile={data_dir.parent / WORKSPACE_CONFIG_SUBDIR / 'nova.env'}\n"
        "#\n"
        "# NOVA_DATA_DIR points at this workspace's data/ subdirectory\n"
        "# so nova.db, backups, exports, memory-packs, and logs all\n"
        "# live inside the portable workspace.\n"
        f"NOVA_DATA_DIR={data_dir}\n"
    )


@dataclass(frozen=True)
class WorkspaceInitResult:
    """Read-only snapshot of what :func:`init_workspace` did.

    The dataclass is returned so callers (the CLI, tests, future
    admin endpoints) can render an honest summary without re-stating
    the filesystem. ``created_*`` lists the entries this call created;
    ``existing_*`` lists the entries that were already present and
    therefore left untouched. The function never overwrites, so an
    entry can only appear in one list.
    """

    root: Path
    created_dirs: tuple[Path, ...]
    existing_dirs: tuple[Path, ...]
    created_files: tuple[Path, ...]
    existing_files: tuple[Path, ...]

    @property
    def data_dir(self) -> Path:
        """Absolute path of the workspace ``data/`` subdirectory."""
        return self.root / WORKSPACE_DATA_SUBDIR

    @property
    def env_example_path(self) -> Path:
        """Absolute path of the generated ``config/nova.env.example``."""
        return (
            self.root / WORKSPACE_CONFIG_SUBDIR / WORKSPACE_ENV_EXAMPLE_NAME
        )


def init_workspace(parent: str | os.PathLike[str]) -> WorkspaceInitResult:
    """Scaffold a Nova Portable Workspace layout under ``parent``.

    Creates the directory structure documented in
    ``docs/portable-workspace.md`` (``app/``, ``data/``, ``logs/``,
    ``backups/``, ``config/``, ``scripts/``) and writes a single
    example env file at ``config/nova.env.example`` pointing at the
    workspace's ``data/`` directory.

    The helper is **safe to run repeatedly**:

    * Existing directories are left in place; their contents are not
      touched.
    * The example env file is only written when it does not already
      exist. An operator who customised it keeps their copy.
    * Nothing else is created, copied, moved, or deleted.

    Raises :class:`RuntimeError` if the parent path exists but is not
    a directory, or if any required subdirectory cannot be created.
    The error message names the offending path so the operator can
    fix the underlying mount or permission issue.
    """
    if parent is None or (isinstance(parent, str) and not parent.strip()):
        raise RuntimeError(
            "init_workspace requires a non-empty parent path."
        )

    root = Path(os.fspath(parent)).expanduser()

    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"Nova workspace parent {root!s} could not be created: "
            f"{exc.strerror or exc}"
        ) from exc

    if not root.is_dir():
        raise RuntimeError(
            f"Nova workspace parent {root!s} exists but is not a directory."
        )

    created_dirs: list[Path] = []
    existing_dirs: list[Path] = []
    for name in _WORKSPACE_SUBDIRS:
        sub = root / name
        if sub.exists():
            if not sub.is_dir():
                raise RuntimeError(
                    f"Nova workspace entry {sub!s} exists but is not a "
                    "directory."
                )
            existing_dirs.append(sub)
            continue
        try:
            sub.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            raise RuntimeError(
                f"Nova workspace subdirectory {sub!s} could not be "
                f"created: {exc.strerror or exc}"
            ) from exc
        created_dirs.append(sub)

    created_files: list[Path] = []
    existing_files: list[Path] = []
    env_example = root / WORKSPACE_CONFIG_SUBDIR / WORKSPACE_ENV_EXAMPLE_NAME
    if env_example.exists():
        existing_files.append(env_example)
    else:
        body = _workspace_env_example_body(root / WORKSPACE_DATA_SUBDIR)
        try:
            env_example.write_text(body, encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(
                f"Nova workspace env example {env_example!s} could not "
                f"be written: {exc.strerror or exc}"
            ) from exc
        created_files.append(env_example)

    return WorkspaceInitResult(
        root=root,
        created_dirs=tuple(created_dirs),
        existing_dirs=tuple(existing_dirs),
        created_files=tuple(created_files),
        existing_files=tuple(existing_files),
    )


# ── CLI entry point ─────────────────────────────────────────────────
#
# ``python -m core.paths init-workspace <parent>`` runs
# :func:`init_workspace` and prints a summary. Kept tiny on purpose —
# the heavy lifting lives in the function above so it can be unit
# tested without spawning a subprocess.


def _format_workspace_summary(result: WorkspaceInitResult) -> str:
    """Render a human-readable summary of an :class:`WorkspaceInitResult`."""
    lines = [f"Nova Portable Workspace ready at {result.root}"]
    if result.created_dirs:
        lines.append("  created directories:")
        for path in result.created_dirs:
            lines.append(f"    + {path.relative_to(result.root)}/")
    if result.existing_dirs:
        lines.append("  existing directories (left untouched):")
        for path in result.existing_dirs:
            lines.append(f"    = {path.relative_to(result.root)}/")
    if result.created_files:
        lines.append("  created files:")
        for path in result.created_files:
            lines.append(f"    + {path.relative_to(result.root)}")
    if result.existing_files:
        lines.append("  existing files (left untouched):")
        for path in result.existing_files:
            lines.append(f"    = {path.relative_to(result.root)}")
    lines.append("")
    lines.append(
        f"Next: clone Nova into {result.root / WORKSPACE_APP_SUBDIR}/Nova,"
    )
    lines.append(
        f"      copy {result.env_example_path.relative_to(result.root)} "
        f"to {(result.root / WORKSPACE_CONFIG_SUBDIR / 'nova.env').relative_to(result.root)},"
    )
    lines.append(
        "      and point your service at the workspace's config/nova.env."
    )
    lines.append("See docs/portable-workspace.md for the full walkthrough.")
    return "\n".join(lines)


def _cli(argv: list[str]) -> int:
    """Parse ``argv`` and dispatch the workspace-init command.

    Returns a POSIX-style exit code. The function is split out so
    tests can drive the CLI without ``subprocess``.
    """
    if len(argv) >= 2 and argv[0] == "init-workspace":
        parent = argv[1]
        try:
            result = init_workspace(parent)
        except RuntimeError as exc:
            print(f"error: {exc}", file=_stderr())
            return 1
        print(_format_workspace_summary(result))
        return 0

    print(
        "usage: python -m core.paths init-workspace <parent>\n\n"
        "Scaffold a Nova Portable Workspace under <parent>. Safe to "
        "run repeatedly;\nexisting files are never overwritten. See "
        "docs/portable-workspace.md.",
        file=_stderr(),
    )
    return 2


def _stderr():
    # Indirection so tests can monkeypatch the stderr target.
    import sys

    return sys.stderr


if __name__ == "__main__":  # pragma: no cover - exercised via tests
    import sys

    sys.exit(_cli(sys.argv[1:]))
