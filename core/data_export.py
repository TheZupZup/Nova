"""Nova data export package builder + inspector (Phase 1).

This module is the export half of the Storage & Migration Center.
It builds a **portable, local, data-only** export package the
operator can move to another machine and inspect / dry-run-restore
on the target. Phase 1 ships:

* :func:`create_data_export` — builds a gzipped tar archive
  containing ``manifest.json``, ``RESTORE.md``, and a strict
  allowlist of Nova data files (the SQLite database, its sidecar
  backups, and the four reserved subdirectories).
* :func:`inspect_export` — validates an existing archive in
  read-only mode: parses the manifest, checks every member for path
  traversal / symlink escape, surfaces warnings, and returns a
  structured report.
* :func:`plan_restore` — dry-run only. Returns the files that would
  be restored, refuses overwrite by default if the target already
  has a ``nova.db``, and validates archive paths against path
  traversal. **No file is written** by this function.

Safety contract (enforced):

* **Allowlist only.** Only ``nova.db``, files matching
  ``nova.db.*`` (sidecar / pre-upgrade backups), and the four
  reserved subdirectories (``backups/``, ``exports/``,
  ``memory-packs/``, ``logs/``) are eligible for inclusion. The
  Nova checkout, ``.git``, ``.venv``, caches, media libraries,
  Ollama models, ``.env`` files, ``.ssh`` / ``.gnupg`` / token-
  shaped files are never included — even when the export runs
  against a legacy data root that happens to overlap with the
  checkout.
* **No symlink escape.** Walking the allowlist uses
  ``os.walk(followlinks=False)``. Each file encountered is checked
  for symlink-target escape outside the data root before being
  added; offending entries are recorded in ``excluded`` with reason
  ``symlink_escape``.
* **No path traversal on inspect / restore.** Every archive member
  name is validated lexically (no ``..``, no leading ``/``, no
  drive letters) and re-checked after resolution against the
  intended root.
* **No automatic restore.** Phase 1 only inspects and plans. No
  file is written, moved, or deleted by this module. Restore
  itself remains a documented manual operator step (see
  ``docs/storage-and-migration.md``).
* **No shell, no subprocess, no network.** The module relies on
  ``tarfile``, ``hashlib``, ``os.walk``, and ``shutil``. Nothing
  else.
* **No secret leakage.** Manifests never contain absolute paths
  outside the data root, never include env vars, and never echo
  ``.env`` / token-shaped file contents.

The module is admin-only at the API layer — the web endpoints that
expose it are wrapped with ``require_admin`` and require an
explicit ``confirm`` payload.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Optional

from core import paths as _paths

logger = logging.getLogger(__name__)


# ── Public constants ────────────────────────────────────────────────

#: Export package format identifier — pinned in every manifest so a
#: future format change can be detected explicitly rather than
#: silently misread.
FORMAT_ID = "nova-data-export"
FORMAT_VERSION = 1

#: Allowed export modes. Phase 1 ships the safe ``data-only`` default;
#: ``workspace`` is reserved for a follow-up PR and currently rejected.
MODE_DATA_ONLY = "data-only"
MODE_WORKSPACE = "workspace"
_ALLOWED_MODES: frozenset[str] = frozenset({MODE_DATA_ONLY})

#: Filename layout inside the archive. Everything Nova-owned lives
#: under ``data/`` so the archive root is reserved for the manifest
#: and the operator-facing restore instructions.
ARCHIVE_DATA_PREFIX = "data"
ARCHIVE_MANIFEST_NAME = "manifest.json"
ARCHIVE_RESTORE_DOC_NAME = "RESTORE.md"
ARCHIVE_ALLOWED_TOP_LEVEL: frozenset[str] = frozenset({
    ARCHIVE_MANIFEST_NAME, ARCHIVE_RESTORE_DOC_NAME, ARCHIVE_DATA_PREFIX,
})

#: Default filename stem for new export packages. The timestamp is
#: UTC ISO 8601 compact form so packages sort lexicographically by
#: creation time.
DEFAULT_EXPORT_STEM = "nova-data-export"

#: Hash chunk size used when summing files into the manifest.
_HASH_CHUNK_BYTES = 64 * 1024

#: Maximum manifest read size during inspection. The manifest is a
#: small JSON file in practice; a runaway / hostile manifest is
#: rejected so we never read a multi-gigabyte blob into memory.
_MAX_MANIFEST_BYTES = 1 * 1024 * 1024

#: Filenames that look like secrets and must never appear inside an
#: export. We match by the lowercased basename so a hostile path is
#: rejected regardless of the parent directory. The list is
#: deliberately conservative — false positives are preferable to
#: silently exporting a credential.
_SECRET_BASENAMES: frozenset[str] = frozenset({
    ".env", ".envrc", ".netrc", ".npmrc", ".pypirc",
    "credentials", "credentials.json", "credentials.yaml",
    "secrets", "secrets.json", "secrets.yaml",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
})

#: Filename suffixes / patterns that signal a secret. Matched case-
#: insensitively against the basename.
_SECRET_SUFFIX_RE = re.compile(
    r"(?:\.env|\.envrc|\.pem|\.key|\.p12|\.pfx|\.crt|\.cer|"
    r"\.gpg|\.asc|_token|_secret|_credentials)$",
    re.IGNORECASE,
)

#: Directory names that should never be walked into during an
#: export. They are never created by Nova under the data root, but
#: defense-in-depth: a misconfigured legacy data dir overlapping the
#: checkout would have these.
_FORBIDDEN_DIR_BASENAMES: frozenset[str] = frozenset({
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "node_modules",
    ".ssh", ".gnupg", ".aws", ".gcloud", ".kube",
})

#: Top-level entries that are eligible for inclusion. The matcher is
#: lexical: ``nova.db`` and any ``nova.db.*`` sidecar / backup file
#: are matched explicitly; the reserved subdirectories are matched
#: by exact name. Anything else under the data root is **silently
#: ignored** so a legacy install with files next to ``nova.db``
#: never accidentally exports them.
_ALLOWED_TOP_FILES: frozenset[str] = frozenset({
    _paths.DB_FILENAME,  # nova.db
})
_ALLOWED_TOP_FILE_PREFIXES: tuple[str, ...] = (
    _paths.DB_FILENAME + ".",  # nova.db.backup, nova.db.preupgrade-*, …
)
_ALLOWED_TOP_DIRS: frozenset[str] = frozenset({
    _paths.BACKUPS_SUBDIR,
    _paths.EXPORTS_SUBDIR,
    _paths.MEMORY_PACKS_SUBDIR,
    _paths.LOGS_SUBDIR,
})


# ── Exclusion reasons (stable wire-format strings) ──────────────────

REASON_SECRET = "secret"
REASON_VCS = "vcs"
REASON_VENV = "venv"
REASON_CACHE = "cache"
REASON_NODE_MODULES = "node_modules"
REASON_OLLAMA_MODEL = "ollama_model"
REASON_OUTSIDE_DATA_DIR = "outside_data_dir"
REASON_SYMLINK_ESCAPE = "symlink_escape"
REASON_NOT_ALLOWLISTED = "not_allowlisted"
REASON_UNREADABLE = "unreadable"
REASON_PATH_TRAVERSAL = "path_traversal"
REASON_DEVICE_OR_OTHER = "device_or_other"


# ── Dataclasses ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExcludedEntry:
    """A path that was considered but deliberately not included.

    The wire shape is ``{"path": <relative>, "reason": <reason>}`` —
    short, stable strings the UI can switch on. Paths are recorded
    relative to the source data root so they are safe to render.
    """

    path: str
    reason: str

    def as_dict(self) -> dict:
        return {"path": self.path, "reason": self.reason}


@dataclass(frozen=True)
class IncludedEntry:
    """A file that was included in the export, with its integrity hash.

    Sizes are in bytes; hashes are SHA-256 hex digests. Paths are
    POSIX-style (forward slashes) relative to the archive's
    ``data/`` prefix so the restore instructions can reference them
    verbatim.
    """

    path: str
    size: int
    sha256: str

    def as_dict(self) -> dict:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True)
class ExportResult:
    """Outcome of a single :func:`create_data_export` call.

    ``archive_path`` is the absolute path of the produced tar.gz
    file. ``manifest`` is the serialised manifest body so the caller
    does not need to re-open the archive to display the summary.
    ``excluded`` lists every path that was considered but skipped
    (with a reason); ``warnings`` carries any soft issues that did
    not prevent the export.
    """

    archive_path: str
    archive_size: int
    archive_sha256: str
    manifest: dict
    included: tuple[IncludedEntry, ...]
    excluded: tuple[ExcludedEntry, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "archive_path": self.archive_path,
            "archive_size": self.archive_size,
            "archive_sha256": self.archive_sha256,
            "manifest": self.manifest,
            "included": [e.as_dict() for e in self.included],
            "excluded": [e.as_dict() for e in self.excluded],
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class InspectionResult:
    """Read-only structural report on an existing archive.

    Returned by :func:`inspect_export`. ``valid`` is the bottom-line
    flag the UI can switch on; ``errors`` lists any structural
    problems (missing manifest, path traversal, version mismatch).
    The list of files reflects what the archive *contains*, not what
    a restore would *do* — see :func:`plan_restore` for the restore
    dry-run.
    """

    archive_path: str
    valid: bool
    manifest: Optional[dict]
    files: tuple[str, ...]
    total_uncompressed_size: int
    errors: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "archive_path": self.archive_path,
            "valid": self.valid,
            "manifest": self.manifest,
            "files": list(self.files),
            "total_uncompressed_size": self.total_uncompressed_size,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class RestorePlan:
    """Dry-run plan for restoring an archive into the configured data dir.

    Phase 1 returns the plan only — no file is written. ``allowed``
    is ``True`` when the target is safe to restore into (no existing
    ``nova.db`` collision, no path traversal in the archive); when
    ``False``, ``refuse_reason`` and ``conflicts`` explain why and
    the UI must refuse to proceed.
    """

    archive_path: str
    target_data_dir: str
    allowed: bool
    refuse_reason: str
    conflicts: tuple[str, ...]
    would_restore: tuple[str, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)
    manifest: Optional[dict] = None

    def as_dict(self) -> dict:
        return {
            "archive_path": self.archive_path,
            "target_data_dir": self.target_data_dir,
            "allowed": self.allowed,
            "refuse_reason": self.refuse_reason,
            "conflicts": list(self.conflicts),
            "would_restore": list(self.would_restore),
            "warnings": list(self.warnings),
            "manifest": self.manifest,
        }


# ── Helpers ─────────────────────────────────────────────────────────


def _now_iso_utc() -> str:
    """Return the current UTC timestamp in ISO 8601 compact form.

    Centralised so tests can monkeypatch this single function. The
    format ``YYYYMMDDTHHMMSSZ`` is filesystem-safe (no colons) and
    sorts lexicographically.
    """
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _nova_version() -> str:
    """Return the Nova version string, or ``""`` if unknown.

    Nova does not currently ship a ``__version__`` constant. We try
    the ``CHANGELOG.md`` heading as a fallback so the manifest can
    record *something* useful; the empty-string case is acceptable
    when even that is missing (the manifest field is informational).
    """
    try:
        repo_root = Path(__file__).resolve().parent.parent
        changelog = repo_root / "CHANGELOG.md"
        if changelog.is_file():
            for line in changelog.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()[:20]:
                stripped = line.strip()
                # Match either "## 0.4.0" or "## [0.4.0]" headings.
                m = re.match(r"^##\s*\[?(\d+\.\d+(?:\.\d+)?)\]?", stripped)
                if m:
                    return m.group(1)
    except OSError:
        return ""
    return ""


def _sha256_file(path: Path) -> str:
    """Return a SHA-256 hex digest of ``path``'s content.

    Chunked read so a multi-gigabyte SQLite database does not blow
    up the process memory.
    """
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _is_secret_name(basename: str) -> bool:
    """Return True when ``basename`` looks like a secret file.

    Case-insensitive: ``.env``, ``credentials.json``, ``id_rsa``,
    ``*.pem``, ``*.key``, ``*_token``, etc. We never read the file
    content — name-based detection is enough to refuse inclusion.
    """
    if not basename:
        return False
    lower = basename.lower()
    if lower in _SECRET_BASENAMES:
        return True
    if _SECRET_SUFFIX_RE.search(lower):
        return True
    return False


def _classify_exclusion(rel_parts: tuple[str, ...]) -> Optional[str]:
    """Return an exclusion reason for ``rel_parts``, or ``None``.

    ``rel_parts`` is the path relative to the data root, broken into
    components. The function checks each component against the
    forbidden-dir set and the secret-name set; the first match wins.
    Returning ``None`` means the entry is provisionally eligible —
    further checks (symlink escape, top-level allowlist) still apply.
    """
    if not rel_parts:
        return None
    for part in rel_parts:
        lower = part.lower()
        if part in _FORBIDDEN_DIR_BASENAMES:
            if part == ".git":
                return REASON_VCS
            if part in {".venv", "venv"}:
                return REASON_VENV
            if part == "node_modules":
                return REASON_NODE_MODULES
            if part in {"__pycache__", ".pytest_cache",
                        ".mypy_cache", ".ruff_cache"}:
                return REASON_CACHE
            if part in {".ssh", ".gnupg", ".aws", ".gcloud", ".kube"}:
                return REASON_SECRET
        if _is_secret_name(part):
            return REASON_SECRET
        if lower == "ollama" or lower.endswith(".gguf"):
            return REASON_OLLAMA_MODEL
    return None


def _safe_under(root: Path, candidate: Path) -> bool:
    """Return True when ``candidate`` resolves under ``root``.

    Both arguments are resolved before comparison so a path like
    ``root/sub/../sub`` is treated correctly. We never raise: an
    OS error during resolution returns ``False`` so the caller
    treats the candidate as unsafe.
    """
    try:
        root_resolved = root.resolve()
        candidate_resolved = candidate.resolve()
    except OSError:
        return False
    try:
        candidate_resolved.relative_to(root_resolved)
    except ValueError:
        return False
    return True


def _is_safe_member_name(name: str) -> bool:
    """Return True when ``name`` is a safe in-archive path.

    A safe name:
      * is non-empty,
      * does not start with ``/``,
      * does not contain a Windows drive letter,
      * does not contain a ``..`` component,
      * does not contain backslashes (we render POSIX archive paths),
      * does not contain control characters.
    The check is purely lexical — the structural validation in
    :func:`inspect_export` re-runs it before each member is inspected.
    """
    if not name or name.startswith("/") or name.startswith("\\"):
        return False
    if "\x00" in name or "\\" in name:
        return False
    if re.match(r"^[A-Za-z]:", name):
        return False
    parts = PurePosixPath(name).parts
    for part in parts:
        if part in ("..",):
            return False
    return True


# ── Export builder ─────────────────────────────────────────────────


def _walk_allowlisted(
    root: Path,
) -> tuple[list[tuple[Path, tuple[str, ...]]], list[ExcludedEntry]]:
    """Walk the data root and collect (file, rel_parts) for inclusion.

    Returns ``(included, excluded)``. The walk is **strict allowlist**:
    only the documented top-level files (``nova.db`` and ``nova.db.*``)
    and the four reserved subdirectories are descended into. Anything
    else under ``root`` is silently skipped — this is the property
    that keeps a legacy data root pointing at a Nova checkout from
    accidentally including source code.

    Within an allowed subdirectory the walk uses
    ``os.walk(followlinks=False)`` and rejects:

    * any path component matching a forbidden directory name,
    * any basename matching the secret pattern,
    * any symlink whose resolved target is not under ``root``.
    """
    included: list[tuple[Path, tuple[str, ...]]] = []
    excluded: list[ExcludedEntry] = []

    # Top-level enumeration — we do NOT use ``os.walk`` here so we
    # can apply the allowlist before recursing.
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return included, excluded

    for entry in entries:
        name = entry.name
        if entry.is_symlink():
            # A top-level symlink is fine if it lands inside the data
            # root; we still resolve and check. Unsafe links are
            # recorded.
            if not _safe_under(root, entry):
                excluded.append(ExcludedEntry(name, REASON_SYMLINK_ESCAPE))
                continue
        if entry.is_dir():
            if name in _ALLOWED_TOP_DIRS:
                _walk_subdir(root, entry, name, included, excluded)
            else:
                # Silently skip — we never document or echo files
                # outside the allowlist, so a legacy install with the
                # repo as data root doesn't leak source paths.
                excluded.append(ExcludedEntry(name + "/", REASON_NOT_ALLOWLISTED))
            continue
        if entry.is_file():
            if name in _ALLOWED_TOP_FILES or any(
                name.startswith(p) for p in _ALLOWED_TOP_FILE_PREFIXES
            ):
                if _is_secret_name(name):
                    excluded.append(ExcludedEntry(name, REASON_SECRET))
                    continue
                included.append((entry, (name,)))
            else:
                excluded.append(ExcludedEntry(name, REASON_NOT_ALLOWLISTED))
            continue
        # Sockets, fifos, block devices, etc. — never included.
        excluded.append(ExcludedEntry(name, REASON_DEVICE_OR_OTHER))

    return included, excluded


def _walk_subdir(
    root: Path,
    subdir: Path,
    subdir_name: str,
    included: list[tuple[Path, tuple[str, ...]]],
    excluded: list[ExcludedEntry],
) -> None:
    """Recurse into ``subdir`` and collect eligible files.

    Mutates ``included`` and ``excluded`` in place. ``followlinks``
    is disabled so a hostile symlink cannot pull a directory tree
    from outside the data root into the export.
    """
    for dirpath, dirnames, filenames in os.walk(
        subdir, followlinks=False
    ):
        dirpath_p = Path(dirpath)
        # Filter forbidden subdirectories in place so os.walk does
        # not descend into them.
        keep_dirs = []
        for d in list(dirnames):
            if d in _FORBIDDEN_DIR_BASENAMES:
                rel = dirpath_p.relative_to(root) / d
                # Pick the most specific reason we can.
                rel_parts = rel.parts
                reason = _classify_exclusion(rel_parts) or REASON_NOT_ALLOWLISTED
                excluded.append(ExcludedEntry(rel.as_posix() + "/", reason))
                continue
            keep_dirs.append(d)
        dirnames[:] = keep_dirs

        for fname in sorted(filenames):
            file_p = dirpath_p / fname
            try:
                rel = file_p.relative_to(root)
            except ValueError:
                excluded.append(
                    ExcludedEntry(fname, REASON_OUTSIDE_DATA_DIR)
                )
                continue
            rel_parts = rel.parts
            # The top component must be the original subdir name.
            if not rel_parts or rel_parts[0] != subdir_name:
                excluded.append(
                    ExcludedEntry(rel.as_posix(), REASON_OUTSIDE_DATA_DIR)
                )
                continue
            reason = _classify_exclusion(rel_parts)
            if reason is not None:
                excluded.append(ExcludedEntry(rel.as_posix(), reason))
                continue
            if file_p.is_symlink():
                if not _safe_under(root, file_p):
                    excluded.append(
                        ExcludedEntry(rel.as_posix(), REASON_SYMLINK_ESCAPE)
                    )
                    continue
            if not file_p.is_file():
                # Sockets / fifos / etc.
                excluded.append(
                    ExcludedEntry(rel.as_posix(), REASON_DEVICE_OR_OTHER)
                )
                continue
            included.append((file_p, rel_parts))


def create_data_export(
    *,
    dest_dir: Optional[str | os.PathLike[str]] = None,
    mode: str = MODE_DATA_ONLY,
    stem: Optional[str] = None,
) -> ExportResult:
    """Build a portable, allowlisted data-only export package.

    ``dest_dir`` selects the directory the archive is written to.
    The default is :func:`core.paths.exports_dir`, which lives under
    ``NOVA_DATA_DIR/exports`` when configured. The directory is
    created (``mkdir -p`` semantics) if it does not exist; the
    archive itself is written atomically: first to a temporary
    ``.partial`` file, then renamed.

    ``mode`` must be ``"data-only"`` in Phase 1. The ``"workspace"``
    mode is reserved for a future PR and currently raises a
    :class:`ValueError`.

    ``stem`` overrides the default filename stem
    (``nova-data-export``). The final filename is
    ``<stem>-<timestamp>.tar.gz``.

    Raises:
        :class:`ValueError` for an unknown mode.
        :class:`RuntimeError` when the source data root cannot be
            located or the destination cannot be written. Error
            messages are short and frontend-safe — they never
            include env vars or absolute paths outside the data
            root.
    """
    if mode not in _ALLOWED_MODES:
        raise ValueError(
            f"Export mode {mode!r} is not supported. "
            "Phase 1 supports 'data-only' only."
        )

    # Validate ``stem`` before touching the filesystem so a bad
    # value never leaves a freshly-created destination behind.
    final_stem = (stem or DEFAULT_EXPORT_STEM).strip() or DEFAULT_EXPORT_STEM
    if not re.match(r"^[A-Za-z0-9._-]+$", final_stem):
        raise ValueError(
            "Export stem must contain only letters, digits, '.', '_' or '-'."
        )

    # Resolve the source data root. In legacy mode this is the CWD;
    # the allowlist below ensures we still only pick up canonical
    # Nova files in that case.
    data_dir = _paths.configured_data_dir()
    source_root = _paths.effective_data_root()
    try:
        source_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"Source data directory {source_root!s} is not accessible: "
            f"{exc.strerror or 'OS error'}"
        ) from exc

    # Resolve the destination directory.
    if dest_dir is None:
        dest_path = _paths.exports_dir()
        if not dest_path.is_absolute():
            dest_path = source_root / dest_path.name
    else:
        dest_path = Path(os.fspath(dest_dir)).expanduser()
    try:
        dest_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"Export destination {dest_path!s} could not be created: "
            f"{exc.strerror or 'OS error'}"
        ) from exc

    # Build the archive filename. ``final_stem`` was validated above.
    timestamp = _now_iso_utc()
    archive_name = f"{final_stem}-{timestamp}.tar.gz"
    archive_path = dest_path / archive_name
    partial_path = dest_path / (archive_name + ".partial")

    # Walk the data root and collect the included / excluded sets.
    included_pairs, excluded_entries = _walk_allowlisted(source_root)

    # Build the manifest and the included entry list. We hash each
    # file once, while the export builder iterates them, so the
    # disk only has to be touched once per file.
    included_entries: list[IncludedEntry] = []
    archive_files: list[tuple[Path, str]] = []
    for file_p, rel_parts in included_pairs:
        try:
            size = int(file_p.stat().st_size)
        except OSError:
            excluded_entries.append(ExcludedEntry(
                "/".join(rel_parts), REASON_UNREADABLE,
            ))
            continue
        try:
            digest = _sha256_file(file_p)
        except OSError:
            excluded_entries.append(ExcludedEntry(
                "/".join(rel_parts), REASON_UNREADABLE,
            ))
            continue
        rel_posix = "/".join(rel_parts)
        included_entries.append(IncludedEntry(
            path=rel_posix, size=size, sha256=digest,
        ))
        archive_files.append((file_p, rel_posix))

    manifest = _build_manifest(
        mode=mode,
        timestamp=timestamp,
        source_root=source_root,
        included=included_entries,
        excluded=excluded_entries,
    )

    # Write the archive atomically: build under .partial, rename.
    # ``dereference=True`` ensures every symlink that survived the
    # safety check is stored as the *target file's* content, so the
    # restored copy on another machine never depends on a symlink
    # being recreatable on that host. Unsafe symlinks have already
    # been removed during the walk above.
    try:
        with tarfile.open(partial_path, mode="w:gz", dereference=True) as tar:
            manifest_bytes = json.dumps(
                manifest, indent=2, sort_keys=True,
            ).encode("utf-8")
            _add_bytes_to_tar(
                tar, ARCHIVE_MANIFEST_NAME, manifest_bytes,
            )
            restore_doc_bytes = _build_restore_doc(manifest).encode("utf-8")
            _add_bytes_to_tar(
                tar, ARCHIVE_RESTORE_DOC_NAME, restore_doc_bytes,
            )
            for file_p, rel_posix in archive_files:
                arcname = f"{ARCHIVE_DATA_PREFIX}/{rel_posix}"
                if not _is_safe_member_name(arcname):
                    # Should not happen with the allowlist, but
                    # defense-in-depth: skip rather than write a
                    # bogus member.
                    excluded_entries.append(
                        ExcludedEntry(rel_posix, REASON_PATH_TRAVERSAL)
                    )
                    continue
                # ``dereference=True`` ensures symlink targets that
                # passed the safety check are stored as real files
                # — the restored copy never depends on a symlink
                # being present on the target machine.
                tar.add(
                    str(file_p),
                    arcname=arcname,
                    recursive=False,
                    filter=_tar_filter,
                )
        partial_path.replace(archive_path)
    except OSError as exc:
        # Clean up the partial file on failure so we never leave a
        # half-written ``.partial`` next to a real archive.
        try:
            partial_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(
            f"Export archive could not be written: "
            f"{exc.strerror or 'OS error'}"
        ) from exc

    archive_size = archive_path.stat().st_size
    archive_sha = _sha256_file(archive_path)

    warnings: list[str] = []
    if data_dir is None:
        warnings.append(
            "NOVA_DATA_DIR is not configured. The export was built "
            "against the legacy data root; only the canonical Nova "
            "data files were included. See "
            "docs/storage-and-migration.md."
        )
    if not included_entries:
        warnings.append(
            "Export contains no Nova data files. Has Nova ever run on "
            "this host?"
        )

    return ExportResult(
        archive_path=str(archive_path),
        archive_size=int(archive_size),
        archive_sha256=archive_sha,
        manifest=manifest,
        included=tuple(included_entries),
        excluded=tuple(excluded_entries),
        warnings=tuple(warnings),
    )


def _build_manifest(
    *,
    mode: str,
    timestamp: str,
    source_root: Path,
    included: list[IncludedEntry],
    excluded: list[ExcludedEntry],
) -> dict:
    """Return the manifest dict written into the archive.

    The manifest pins:
      * the format identifier and version (so future readers can
        refuse a v2 archive cleanly),
      * the export mode,
      * the creation timestamp,
      * the Nova version, when known,
      * the source data directory (absolute path — already inside
        the operator's trust boundary),
      * the included entries (path / size / sha256),
      * the excluded entries (path / reason),
      * a static list of warnings the operator should keep in mind
        when restoring (e.g. "Ollama models are not included").
    """
    return {
        "format": FORMAT_ID,
        "format_version": FORMAT_VERSION,
        "mode": mode,
        "created_at": timestamp,
        "nova_version": _nova_version(),
        "source_data_dir": str(source_root),
        "files": [e.as_dict() for e in included],
        "excluded": [e.as_dict() for e in excluded],
        "warnings": [
            "Ollama models are NOT included in this package. Re-pull "
            "models on the target machine with `ollama pull <name>` "
            "after restoring.",
            "Media libraries (Jellyfin, Plex, …) are NOT included. "
            "Those services own their own data directories.",
            "Secrets (.env, credentials, SSH keys) are NEVER included. "
            "Configure them on the target machine, not in the export.",
        ],
    }


def _build_restore_doc(manifest: dict) -> str:
    """Return the body of the in-archive ``RESTORE.md`` file.

    The text is informational only — Phase 1 does not perform an
    automated restore. The instructions mirror the manual procedure
    documented in ``docs/storage-and-migration.md``.
    """
    return (
        "# Nova Data Export — Restore\n"
        "\n"
        f"Created at: {manifest.get('created_at', '')}\n"
        f"Source data directory: {manifest.get('source_data_dir', '')}\n"
        f"Format: {manifest.get('format', '')} "
        f"v{manifest.get('format_version', '')}\n"
        f"Mode: {manifest.get('mode', '')}\n"
        "\n"
        "## What this archive contains\n"
        "\n"
        "* `manifest.json` — file list, hashes, and metadata.\n"
        "* `data/` — Nova's runtime data (nova.db, sidecar backups,\n"
        "  and the four reserved subdirectories under NOVA_DATA_DIR).\n"
        "\n"
        "## What this archive does NOT contain\n"
        "\n"
        "* Ollama model files (re-pull on the target machine).\n"
        "* Media libraries (Jellyfin / Plex own their own data).\n"
        "* `.env` files, credentials, SSH keys, OAuth tokens.\n"
        "* The Nova Git checkout, `.venv`, caches, or node_modules.\n"
        "\n"
        "## Restoring (manual, Phase 1)\n"
        "\n"
        "1. Stop Nova on the target machine: `sudo systemctl stop nova`.\n"
        "2. Inspect this archive with Nova's admin UI or with `tar tvf`.\n"
        "3. Make sure NOVA_DATA_DIR is set on the target.\n"
        "4. Back up any existing nova.db on the target.\n"
        "5. Extract: `tar -xzf <archive> -C /tmp/nova-restore` and copy\n"
        "   `/tmp/nova-restore/data/*` into NOVA_DATA_DIR.\n"
        "6. Start Nova: `sudo systemctl start nova` and verify the\n"
        "   web UI shows your memories and conversations.\n"
        "\n"
        "Nova never restores automatically. See "
        "docs/storage-and-migration.md for the full procedure.\n"
    )


def _add_bytes_to_tar(tar: tarfile.TarFile, name: str, body: bytes) -> None:
    """Add a synthetic file with ``body`` content under ``name`` in ``tar``.

    Used for the manifest and the RESTORE.md doc. The tar entry is
    given a stable mode (0o644), uid/gid 0, and the current archive
    timestamp so the produced archive is reasonably deterministic
    across runs at the same UTC second.
    """
    info = tarfile.TarInfo(name=name)
    info.size = len(body)
    info.mode = 0o644
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = int(time.time())
    info.type = tarfile.REGTYPE
    tar.addfile(info, io.BytesIO(body))


def _tar_filter(info: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
    """Normalise tar headers before they reach the archive.

    Strips owner / group names and resets mode bits so the archive
    is reproducible enough for a manual diff. Returns ``None`` for
    any non-regular file that slipped through (defence in depth —
    the walk above already rejects them).
    """
    if not info.isfile():
        return None
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    # Force a stable, owner-only mode on the contents. The operator
    # can re-chmod after restore if a wider mode is required.
    info.mode = 0o600
    return info


# ── Inspection ─────────────────────────────────────────────────────


def inspect_export(archive_path: str | os.PathLike[str]) -> InspectionResult:
    """Validate ``archive_path`` and return a structured report.

    The check is read-only: the archive is opened in stream mode,
    every member is examined, but no file is written to disk. The
    function never raises into the caller — every failure mode
    surfaces as ``valid=False`` with a short, frontend-safe error
    string.

    Validation pins:
      * the file exists and is a tar archive,
      * a single ``manifest.json`` is present at the root and
        parses as JSON within ``_MAX_MANIFEST_BYTES``,
      * the manifest declares the expected format ID and version,
      * every member name is safe (no path traversal, no absolute
        path, no Windows drive letter),
      * every member sits under ``data/`` (except the manifest and
        the optional RESTORE.md),
      * no member is a symlink whose target escapes ``data/``,
      * no member is a hardlink, device, or other non-file type.
    """
    archive_path = str(archive_path)
    errors: list[str] = []
    warnings: list[str] = []
    file_names: list[str] = []
    total_size = 0
    manifest: Optional[dict] = None
    # Track file vs directory members by their normalised (no
    # trailing slash) path so we can refuse archives that list the
    # same path as both a file and a directory. Such archives
    # deterministically fail at extraction (``open(dest, "wb")``
    # on a directory raises ``IsADirectoryError``); refusing at
    # inspection keeps dry-run and real-restore consistent.
    file_member_paths: set[str] = set()
    dir_member_paths: set[str] = set()

    if not os.path.isfile(archive_path):
        return InspectionResult(
            archive_path=archive_path,
            valid=False,
            manifest=None,
            files=(),
            total_uncompressed_size=0,
            errors=("Archive does not exist or is not a regular file.",),
        )

    if not tarfile.is_tarfile(archive_path):
        return InspectionResult(
            archive_path=archive_path,
            valid=False,
            manifest=None,
            files=(),
            total_uncompressed_size=0,
            errors=("File is not a tar archive.",),
        )

    try:
        with tarfile.open(archive_path, mode="r:*") as tar:
            for member in tar:
                if not _is_safe_member_name(member.name):
                    errors.append(
                        f"Unsafe archive member name: {member.name!r}."
                    )
                    continue
                # Forbid hardlinks, devices, fifos.
                if member.islnk() or member.isdev() or member.isfifo():
                    errors.append(
                        f"Disallowed member type for {member.name!r}."
                    )
                    continue
                # Symlinks are refused outright. Nova exports use
                # ``tarfile.open(..., dereference=True)`` so a
                # Nova-built archive never contains a symlink entry
                # — every link is resolved at build time. An archive
                # with a symlink is therefore anomalous, and
                # ``_extract_to_staging`` would silently skip it,
                # which would make the dry-run plan (built from
                # ``inspection.files``) disagree with the actual
                # restore. Refuse it at inspection time so all three
                # surfaces (inspect, dry-run, real restore) agree.
                if member.issym():
                    errors.append(
                        f"Archive contains a symlink member "
                        f"({member.name!r}). Nova exports never "
                        "contain symlinks."
                    )
                    continue
                parts = PurePosixPath(member.name).parts
                if not parts:
                    continue
                top = parts[0]
                if top not in ARCHIVE_ALLOWED_TOP_LEVEL:
                    errors.append(
                        f"Disallowed archive entry at root: {top!r}."
                    )
                    continue
                if (
                    top == ARCHIVE_DATA_PREFIX
                    and len(parts) == 1
                    and not member.isdir()
                ):
                    # A bare ``data`` regular file is not how we
                    # construct exports; reject it.
                    errors.append(
                        "Archive contains a 'data' entry that is not "
                        "a directory."
                    )
                    continue
                # Only regular files become entries in
                # ``InspectionResult.files``. Directory members are
                # structural — they validate (no traversal, no
                # disallowed top-level) but they are not restorable
                # in their own right and ``_extract_to_staging``
                # never adds them to ``extracted``. Including them
                # here used to let the dry-run preview report an
                # ``outcome=dry_run`` for a directory-only archive
                # while the real restore refused with "no
                # extractable Nova data files", breaking the
                # inspect → dry-run → confirm contract.
                #
                # We also track each member's normalised path on the
                # side so we can refuse an archive that lists the
                # same path as both a file and a directory (e.g.
                # ``data/backups/a`` and ``data/backups/a/``). That
                # combination is deterministically unrestorable —
                # extraction creates the directory first and then
                # ``open(file_dst, "wb")`` raises
                # ``IsADirectoryError`` — so refusing here keeps
                # inspect / dry-run / real restore in lockstep.
                normalised_name = member.name.rstrip("/")
                if member.isdir():
                    if normalised_name in file_member_paths:
                        errors.append(
                            f"Archive contains {normalised_name!r} "
                            "as both a file and a directory member."
                        )
                        continue
                    dir_member_paths.add(normalised_name)
                elif member.isfile():
                    if normalised_name in dir_member_paths:
                        errors.append(
                            f"Archive contains {normalised_name!r} "
                            "as both a file and a directory member."
                        )
                        continue
                    file_member_paths.add(normalised_name)
                    file_names.append(member.name)
                    total_size += int(member.size)

                if member.name == ARCHIVE_MANIFEST_NAME and member.isfile():
                    if member.size > _MAX_MANIFEST_BYTES:
                        errors.append(
                            "manifest.json is larger than the allowed "
                            "size — refusing to read."
                        )
                        continue
                    extracted = tar.extractfile(member)
                    if extracted is None:
                        errors.append(
                            "manifest.json could not be read."
                        )
                        continue
                    try:
                        body = extracted.read()
                    finally:
                        extracted.close()
                    try:
                        manifest = json.loads(body.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        errors.append("manifest.json is not valid JSON.")
                        manifest = None
    except tarfile.TarError as exc:
        return InspectionResult(
            archive_path=archive_path,
            valid=False,
            manifest=None,
            files=(),
            total_uncompressed_size=0,
            errors=(f"Archive could not be parsed: {exc.__class__.__name__}.",),
        )
    except OSError as exc:
        return InspectionResult(
            archive_path=archive_path,
            valid=False,
            manifest=None,
            files=(),
            total_uncompressed_size=0,
            errors=(f"Archive could not be opened: "
                    f"{exc.strerror or 'OS error'}.",),
        )

    if manifest is None and not any(
        n.endswith(ARCHIVE_MANIFEST_NAME) for n in file_names
    ):
        errors.append("Archive is missing manifest.json.")

    if isinstance(manifest, dict):
        fmt = manifest.get("format")
        ver = manifest.get("format_version")
        if fmt != FORMAT_ID:
            errors.append(
                f"Manifest format is {fmt!r}, expected {FORMAT_ID!r}."
            )
        if ver != FORMAT_VERSION:
            errors.append(
                f"Manifest format_version is {ver!r}, expected "
                f"{FORMAT_VERSION}."
            )
        if not isinstance(manifest.get("files", []), list):
            errors.append("Manifest 'files' is not a list.")
    elif manifest is not None:
        errors.append("Manifest is not a JSON object.")
        manifest = None

    if not any(
        n == ARCHIVE_MANIFEST_NAME or n.startswith(f"{ARCHIVE_DATA_PREFIX}/")
        for n in file_names
    ):
        warnings.append(
            "Archive does not appear to contain any Nova data files."
        )

    return InspectionResult(
        archive_path=archive_path,
        valid=not errors,
        manifest=manifest,
        files=tuple(file_names),
        total_uncompressed_size=int(total_size),
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


# ── Restore plan (dry-run only) ─────────────────────────────────────


def plan_restore(
    archive_path: str | os.PathLike[str],
    *,
    target_data_dir: Optional[str | os.PathLike[str]] = None,
) -> RestorePlan:
    """Return a dry-run plan for restoring ``archive_path``.

    The function does **not** write, move, or delete anything. It
    inspects the archive, resolves the target data directory (from
    the argument or from ``NOVA_DATA_DIR``), checks whether the
    target already contains ``nova.db``, and reports the result.

    Refusal reasons (``allowed == False``):
      * the archive is invalid (see :func:`inspect_export`);
      * the target data directory cannot be resolved;
      * the target already contains ``nova.db`` (overwrite is
        explicitly refused — Nova never overwrites memory without
        an out-of-band confirmation step);
      * any archive member would resolve outside the target data
        directory after extraction.
    """
    inspection = inspect_export(archive_path)
    archive_path_s = str(archive_path)
    if not inspection.valid:
        return RestorePlan(
            archive_path=archive_path_s,
            target_data_dir="",
            allowed=False,
            refuse_reason="Archive is not valid: " + "; ".join(inspection.errors),
            conflicts=(),
            would_restore=(),
            warnings=inspection.warnings,
            manifest=inspection.manifest,
        )

    if target_data_dir is None:
        configured = _paths.configured_data_dir()
        if configured is None:
            return RestorePlan(
                archive_path=archive_path_s,
                target_data_dir="",
                allowed=False,
                refuse_reason=(
                    "NOVA_DATA_DIR is not set. Configure a target data "
                    "directory before planning a restore."
                ),
                conflicts=(),
                would_restore=(),
                manifest=inspection.manifest,
            )
        target_root = configured
    else:
        target_root = Path(os.fspath(target_data_dir)).expanduser()
    target_root_resolved = target_root.resolve() if target_root.exists() \
        else target_root.absolute()

    # Translate archive member names into would-be target paths and
    # check each one for path traversal after resolution.
    would_restore: list[str] = []
    conflicts: list[str] = []
    refuse_reason = ""

    for member_name in inspection.files:
        if member_name in (ARCHIVE_MANIFEST_NAME, ARCHIVE_RESTORE_DOC_NAME):
            continue
        parts = PurePosixPath(member_name).parts
        if not parts or parts[0] != ARCHIVE_DATA_PREFIX:
            # Already filtered by inspect_export, but be defensive.
            continue
        relative = PurePosixPath(*parts[1:])
        if not str(relative):
            continue
        candidate = target_root_resolved / Path(*relative.parts)
        # Lexical traversal check (no ``..``, no absolute relative).
        if ".." in relative.parts or relative.is_absolute():
            refuse_reason = (
                "Archive contains an unsafe path "
                f"({member_name!r}). Refusing to plan a restore."
            )
            would_restore = []
            break
        # Post-resolution containment check. We do not require the
        # candidate to exist (it usually doesn't) but its absolute
        # form must sit under the target root.
        try:
            candidate_abs = candidate.resolve(strict=False)
        except OSError:
            refuse_reason = (
                f"Archive member {member_name!r} could not be resolved."
            )
            would_restore = []
            break
        try:
            candidate_abs.relative_to(target_root_resolved)
        except ValueError:
            refuse_reason = (
                f"Archive member {member_name!r} would land outside "
                "the target data directory."
            )
            would_restore = []
            break
        would_restore.append(str(relative))
        if candidate.exists():
            conflicts.append(str(relative))

    if refuse_reason:
        return RestorePlan(
            archive_path=archive_path_s,
            target_data_dir=str(target_root_resolved),
            allowed=False,
            refuse_reason=refuse_reason,
            conflicts=tuple(conflicts),
            would_restore=(),
            warnings=inspection.warnings,
            manifest=inspection.manifest,
        )

    # Overwrite refusal: nova.db on the target is sacred.
    nova_db_target = target_root_resolved / _paths.DB_FILENAME
    if nova_db_target.exists():
        return RestorePlan(
            archive_path=archive_path_s,
            target_data_dir=str(target_root_resolved),
            allowed=False,
            refuse_reason=(
                "Target data directory already contains nova.db. "
                "Refusing to overwrite an existing database. Move or "
                "rename the existing file by hand before restoring."
            ),
            conflicts=tuple(conflicts),
            would_restore=tuple(would_restore),
            warnings=inspection.warnings,
            manifest=inspection.manifest,
        )

    warnings: list[str] = list(inspection.warnings)
    if conflicts:
        warnings.append(
            f"{len(conflicts)} file(s) already exist at the target — "
            "they would be overwritten by a restore."
        )

    return RestorePlan(
        archive_path=archive_path_s,
        target_data_dir=str(target_root_resolved),
        allowed=True,
        refuse_reason="",
        conflicts=tuple(conflicts),
        would_restore=tuple(would_restore),
        warnings=tuple(warnings),
        manifest=inspection.manifest,
    )


# ── Safe guided restore (Phase 3) ───────────────────────────────────
#
# Phase 3 layers an opt-in "actually copy the data into the target"
# step on top of Phase 2's dry-run plan. The restore is **only ever**
# performed when:
#
#   1. The archive inspects clean (manifest, format, no path
#      traversal, no symlink escape, no disallowed entry types).
#   2. The dry-run plan returns ``allowed=True`` *or* the caller
#      passed an explicit confirmation that they understand a
#      ``nova.db`` will be replaced.
#   3. A pre-restore backup of the current target data directory was
#      created successfully and lives under
#      ``<target>/backups/pre-restore/`` — refusing the restore if
#      the backup step fails.
#   4. Every archive member was extracted into a private staging
#      directory under ``<target>/.restore-staging/`` and re-validated
#      against path traversal post-extraction.
#
# Only after all of the above does the engine copy the staged files
# into the target. Existing target files that would be replaced were
# already copied into the pre-restore backup, so the operator can roll
# back at any time.

#: Subdirectory under the target ``NOVA_DATA_DIR/backups/`` that holds
#: automatic pre-restore backups. Kept stable so an operator can find
#: previous backups via the filesystem alone.
PRE_RESTORE_BACKUP_SUBDIR = "pre-restore"

#: Directory under the target data root that stages extracted archive
#: contents during a restore. Lives **inside** the data root so the
#: extraction never lands on a different filesystem (an `os.replace`
#: across filesystems would otherwise fail or fall back to a copy +
#: delete that is not atomic).
RESTORE_STAGING_DIRNAME = ".restore-staging"


#: Wire-format strings for ``RestoreResult.outcome``. Stable enough
#: for the UI to switch on and for tests to pin.
RESTORE_OUTCOME_DRY_RUN = "dry_run"
RESTORE_OUTCOME_RESTORED = "restored"
RESTORE_OUTCOME_REFUSED = "refused"
RESTORE_OUTCOME_BACKUP_FAILED = "backup_failed"
RESTORE_OUTCOME_EXTRACT_FAILED = "extract_failed"
RESTORE_OUTCOME_FAILED = "failed"


@dataclass(frozen=True)
class RestoreResult:
    """Outcome of a real (or dry-run) restore call.

    The structure mirrors :class:`RestorePlan` so the UI can render
    a single result panel for either flow.

    * ``outcome`` — short stable identifier (``dry_run``, ``restored``,
      ``refused``, ``backup_failed``, ``extract_failed``, ``failed``).
    * ``refuse_reason`` — non-empty when ``outcome`` is anything other
      than ``restored`` or ``dry_run``; short, frontend-safe.
    * ``restored_files`` — list of POSIX-relative paths actually copied
      into the target on a successful restore. Empty for refusals and
      dry-runs.
    * ``skipped_files`` — files present in the archive that were not
      copied (for example, a stray entry that did not exist in the
      manifest's allowlist).
    * ``conflicts`` — files at the target that were overwritten by
      the restore. Each one is mirrored inside the pre-restore backup.
    * ``backup_path`` — absolute path of the pre-restore backup
      archive, or ``""`` when no backup was created (e.g. dry-run or
      pre-validation refusal).
    * ``restart_recommended`` — ``True`` when restoring a ``nova.db``
      (the database is open by the running process; a calm restart
      ensures the new file is observed).
    * ``warnings`` — soft issues the UI should surface but that did
      not abort the restore.
    """

    archive_path: str
    target_data_dir: str
    outcome: str
    refuse_reason: str
    confirmed: bool
    restored_files: tuple[str, ...]
    skipped_files: tuple[ExcludedEntry, ...]
    conflicts: tuple[str, ...]
    backup_path: str
    backup_size: int
    restart_recommended: bool
    warnings: tuple[str, ...] = field(default_factory=tuple)
    manifest: Optional[dict] = None

    def as_dict(self) -> dict:
        return {
            "archive_path": self.archive_path,
            "target_data_dir": self.target_data_dir,
            "outcome": self.outcome,
            "refuse_reason": self.refuse_reason,
            "confirmed": self.confirmed,
            "restored_files": list(self.restored_files),
            "skipped_files": [e.as_dict() for e in self.skipped_files],
            "conflicts": list(self.conflicts),
            "backup_path": self.backup_path,
            "backup_size": self.backup_size,
            "restart_recommended": self.restart_recommended,
            "warnings": list(self.warnings),
            "manifest": self.manifest,
        }


def _manifest_id(manifest: Optional[dict]) -> str:
    """Return a short identifier for ``manifest`` (best effort).

    Phase 3 uses the manifest's ``created_at`` timestamp as a
    lightweight "confirm you really mean this archive" token. The CLI
    and API accept an optional ``confirmed_manifest_id`` field; when
    present it must match this value.
    """
    if not isinstance(manifest, dict):
        return ""
    val = manifest.get("created_at")
    if isinstance(val, str):
        return val
    return ""


def _resolve_target_root(
    target_data_dir: Optional[str | os.PathLike[str]],
) -> Optional[Path]:
    """Resolve the target data root from arg / env, or ``None``.

    Mirrors :func:`plan_restore`: the explicit argument wins; without
    one we fall back to ``NOVA_DATA_DIR``. Returns ``None`` when
    nothing usable is configured — the caller surfaces that as a
    refusal so the operator gets a clear error.
    """
    if target_data_dir is not None:
        return Path(os.fspath(target_data_dir)).expanduser()
    configured = _paths.configured_data_dir()
    if configured is None:
        return None
    return configured


def _create_pre_restore_backup(
    target_root: Path,
) -> tuple[Optional[Path], int, list[str]]:
    """Build a pre-restore backup archive of the current target dir.

    Returns ``(archive_path, size, warnings)``. ``archive_path`` is
    ``None`` when no backup was needed (the target was empty / had
    no canonical Nova data files) or when the backup failed. The
    caller refuses the restore if the backup was *required* (a
    ``nova.db`` was present) and ``archive_path`` is ``None``.

    The archive is written under
    ``<target_root>/backups/pre-restore/`` using the same data export
    format as the manual export, so an operator can reuse the inspect
    / restore-dry-run flow to verify it. The backup filename is
    namespaced (``nova-pre-restore-<UTC timestamp>.tar.gz``) so it
    can't collide with normal exports.

    The function never overwrites an existing backup file: a name
    clash falls back to appending ``-N`` to the stem until a free
    name is found, or fails with a returned warning if it cannot.
    """
    warnings: list[str] = []
    backup_dir = (
        target_root / _paths.BACKUPS_SUBDIR / PRE_RESTORE_BACKUP_SUBDIR
    )
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        warnings.append(
            f"Could not create pre-restore backup directory: "
            f"{exc.strerror or 'OS error'}"
        )
        return None, 0, warnings

    # Compose a unique stem so the new backup never collides with an
    # existing file. Phase 3 explicitly forbids overwriting backups.
    timestamp = _now_iso_utc()
    stem = f"nova-pre-restore-{timestamp}"
    candidate = backup_dir / f"{stem}.tar.gz"
    counter = 1
    while candidate.exists():
        candidate = backup_dir / f"{stem}-{counter}.tar.gz"
        counter += 1
        if counter > 50:  # pragma: no cover - extreme paranoia
            warnings.append(
                "Could not find a free filename for the pre-restore "
                "backup after 50 attempts."
            )
            return None, 0, warnings

    # Run the export builder against the *target* data root. We
    # cannot rely on the running process's NOVA_DATA_DIR here because
    # the operator may be restoring into a target that is **not** the
    # configured data dir. Temporarily redirect by passing the
    # explicit destination directory and walking the target_root
    # directly.
    included_pairs, excluded_entries = _walk_allowlisted(target_root)
    # Filter out any previously-written pre-restore archives. They
    # live under ``backups/pre-restore/`` (the destination of this
    # very function), so without this filter every restore would
    # pack the previous restore's backup into the next backup —
    # producing "backups of backups" that grow on each restore and
    # eventually trigger spurious ``backup_failed`` outcomes from
    # storage pressure. The operator-facing rollback flow still
    # works: previous pre-restore archives remain on disk
    # untouched; they're just not duplicated into the new one.
    filtered_pairs: list[tuple[Path, tuple[str, ...]]] = []
    for file_p, rel_parts in included_pairs:
        if (
            len(rel_parts) >= 2
            and rel_parts[0] == _paths.BACKUPS_SUBDIR
            and rel_parts[1] == PRE_RESTORE_BACKUP_SUBDIR
        ):
            excluded_entries.append(ExcludedEntry(
                "/".join(rel_parts), REASON_NOT_ALLOWLISTED,
            ))
            continue
        filtered_pairs.append((file_p, rel_parts))
    included_pairs = filtered_pairs
    if not included_pairs:
        # Nothing to back up — return cleanly. The caller can decide
        # whether that is acceptable.
        return None, 0, warnings

    included_entries: list[IncludedEntry] = []
    archive_files: list[tuple[Path, str]] = []
    for file_p, rel_parts in included_pairs:
        rel_posix = "/".join(rel_parts)
        # Read failures here are not a "skip and continue" — the
        # whole point of the pre-restore backup is to give the
        # operator a recoverable copy of every canonical file
        # before any file is replaced. If we cannot stat or hash a
        # file, we cannot include it in the backup, and the
        # subsequent restore could overwrite it without a fallback.
        # Abort the backup so ``apply_restore`` refuses the restore
        # with ``outcome=backup_failed`` and the operator can
        # investigate the unreadable file.
        try:
            size = int(file_p.stat().st_size)
        except OSError as exc:
            warnings.append(
                f"Could not stat {rel_posix!r} for pre-restore "
                f"backup: {exc.strerror or 'OS error'}"
            )
            return None, 0, warnings
        try:
            digest = _sha256_file(file_p)
        except OSError as exc:
            warnings.append(
                f"Could not read {rel_posix!r} for pre-restore "
                f"backup: {exc.strerror or 'OS error'}"
            )
            return None, 0, warnings
        included_entries.append(IncludedEntry(
            path=rel_posix, size=size, sha256=digest,
        ))
        archive_files.append((file_p, rel_posix))

    manifest = _build_manifest(
        mode=MODE_DATA_ONLY,
        timestamp=timestamp,
        source_root=target_root,
        included=included_entries,
        excluded=excluded_entries,
    )
    manifest["pre_restore_backup"] = True

    partial = backup_dir / (candidate.name + ".partial")
    try:
        with tarfile.open(partial, mode="w:gz", dereference=True) as tar:
            manifest_bytes = json.dumps(
                manifest, indent=2, sort_keys=True,
            ).encode("utf-8")
            _add_bytes_to_tar(
                tar, ARCHIVE_MANIFEST_NAME, manifest_bytes,
            )
            restore_doc = _build_restore_doc(manifest).encode("utf-8")
            _add_bytes_to_tar(
                tar, ARCHIVE_RESTORE_DOC_NAME, restore_doc,
            )
            for file_p, rel_posix in archive_files:
                arcname = f"{ARCHIVE_DATA_PREFIX}/{rel_posix}"
                if not _is_safe_member_name(arcname):
                    continue
                tar.add(
                    str(file_p),
                    arcname=arcname,
                    recursive=False,
                    filter=_tar_filter,
                )
        partial.replace(candidate)
    except OSError as exc:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
        warnings.append(
            f"Pre-restore backup could not be written: "
            f"{exc.strerror or 'OS error'}"
        )
        return None, 0, warnings

    try:
        size = int(candidate.stat().st_size)
    except OSError:
        size = 0
    return candidate, size, warnings


def _restore_allowlist_reason(
    rel_parts: tuple[str, ...],
) -> Optional[str]:
    """Return an exclusion reason for an archive-relative path, or ``None``.

    Mirrors the export builder's allowlist so a crafted package
    cannot restore files the exporter would never have packed in
    the first place. The rejection vocabulary uses the existing
    ``REASON_*`` wire strings so the UI can render mixed export /
    restore exclusion lists uniformly.

    A relative path is **allowed** when every component clears the
    secret / VCS / cache / venv / Ollama checks (see
    :func:`_classify_exclusion`) **and** the top-level component is
    one of:

      * ``nova.db`` (single-component path), or
      * a name starting with ``nova.db.`` (sidecar / preupgrade
        backup, single-component path), or
      * one of the four reserved subdirectories (``backups``,
        ``exports``, ``memory-packs``, ``logs``) when the path has
        more than one component.

    Anything else — a bare ``.env`` under ``data/``, a hostile
    ``data/.ssh/id_rsa``, a stray ``data/README.txt``, even
    ``data/notabackup/file`` — is refused with the most specific
    reason available.
    """
    if not rel_parts:
        return REASON_NOT_ALLOWLISTED
    # First pass: per-component secret / forbidden-dir / cache /
    # Ollama checks. This catches ``data/.env``, ``data/.ssh/...``,
    # ``data/backups/.git/...``, ``data/logs/leaked.pem``, etc.
    reason = _classify_exclusion(rel_parts)
    if reason is not None:
        return reason
    top = rel_parts[0]
    if len(rel_parts) == 1:
        if top in _ALLOWED_TOP_FILES:
            return None
        if any(top.startswith(p) for p in _ALLOWED_TOP_FILE_PREFIXES):
            return None
        return REASON_NOT_ALLOWLISTED
    if top in _ALLOWED_TOP_DIRS:
        return None
    return REASON_NOT_ALLOWLISTED


def _safe_extract_member(
    tar: tarfile.TarFile,
    member: tarfile.TarInfo,
    staging: Path,
) -> Optional[Path]:
    """Extract a single archive member into ``staging`` safely.

    Returns the resolved destination path on success, or ``None``
    when the member must be skipped (a non-regular file or a path
    that resolves outside the staging tree). The function uses
    explicit byte-stream copying instead of ``tar.extract`` so we
    fully control the destination path; tarfile's own filter
    (``data_filter``) is also applied where available to refuse the
    historical CVE-2007-4559-style escapes.

    Symlinks within the archive are deliberately **not** materialised
    on disk: the export builder dereferences them at build time, so a
    well-formed Nova archive never contains a symlink in the first
    place. A symlink in a hostile archive is therefore rejected.
    """
    name = member.name
    if not _is_safe_member_name(name):
        return None
    if member.isdir():
        # Just create the directory; tar entries are recreated
        # implicitly by writing files below.
        rel = PurePosixPath(name)
        if not rel.parts or rel.parts[0] != ARCHIVE_DATA_PREFIX:
            return None
        dest = staging / Path(*rel.parts[1:]) if len(rel.parts) > 1 else None
        if dest is None:
            return None
        dest.mkdir(parents=True, exist_ok=True)
        return dest
    if not member.isfile():
        # Symlinks, devices, hardlinks, fifos — all forbidden.
        return None

    rel = PurePosixPath(name)
    if not rel.parts or rel.parts[0] != ARCHIVE_DATA_PREFIX:
        return None
    if len(rel.parts) == 1:
        return None
    dest = staging / Path(*rel.parts[1:])
    # Post-resolution containment check.
    try:
        dest_resolved = dest.resolve(strict=False)
        staging_resolved = staging.resolve(strict=False)
        dest_resolved.relative_to(staging_resolved)
    except (OSError, ValueError):
        return None

    dest.parent.mkdir(parents=True, exist_ok=True)
    extracted = tar.extractfile(member)
    if extracted is None:
        return None
    try:
        with open(dest, "wb") as out:
            shutil.copyfileobj(extracted, out, length=_HASH_CHUNK_BYTES)
    finally:
        extracted.close()
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    return dest


def _extract_to_staging(
    archive_path: Path,
    staging: Path,
) -> tuple[list[str], list[ExcludedEntry], Optional[str]]:
    """Extract ``archive_path`` into ``staging``.

    Returns ``(extracted_files, skipped, error)``. ``extracted_files``
    is the list of POSIX-relative file paths under ``staging/data``
    that were materialised. ``skipped`` records members that were
    intentionally not extracted (symlinks, hardlinks, devices,
    names that would resolve outside the staging tree, **or
    entries that fail the export-side allowlist** — e.g. a hostile
    ``data/.env`` or ``data/.ssh/id_rsa``). ``error`` is ``None`` on
    success or a short, frontend-safe string describing a fatal
    extraction error.
    """
    extracted: list[str] = []
    seen_extracted: set[str] = set()
    skipped: list[ExcludedEntry] = []
    try:
        with tarfile.open(archive_path, mode="r:*") as tar:
            for member in tar:
                if member.name in (
                    ARCHIVE_MANIFEST_NAME, ARCHIVE_RESTORE_DOC_NAME
                ):
                    continue
                if not _is_safe_member_name(member.name):
                    skipped.append(ExcludedEntry(
                        member.name, REASON_PATH_TRAVERSAL,
                    ))
                    continue
                if member.islnk() or member.isdev() or member.isfifo():
                    skipped.append(ExcludedEntry(
                        member.name, REASON_DEVICE_OR_OTHER,
                    ))
                    continue
                if member.issym():
                    skipped.append(ExcludedEntry(
                        member.name, REASON_SYMLINK_ESCAPE,
                    ))
                    continue
                # Restore-side allowlist gate. A crafted archive may
                # ship entries the exporter would have refused (the
                # ``.env`` family, ``.ssh/...``, ``.git/...``,
                # arbitrary non-canonical names). They are never
                # extracted into staging.
                parts = PurePosixPath(member.name).parts
                if (
                    parts
                    and parts[0] == ARCHIVE_DATA_PREFIX
                    and len(parts) > 1
                ):
                    allow_reason = _restore_allowlist_reason(parts[1:])
                    if allow_reason is not None:
                        skipped.append(ExcludedEntry(
                            member.name, allow_reason,
                        ))
                        continue
                dest = _safe_extract_member(tar, member, staging)
                if dest is None:
                    if member.isfile():
                        skipped.append(ExcludedEntry(
                            member.name, REASON_PATH_TRAVERSAL,
                        ))
                    continue
                if member.isfile():
                    rel = PurePosixPath(member.name)
                    posix_rel = "/".join(rel.parts[1:])
                    # Duplicate archive members would queue the same
                    # staging path more than once. The first copy in
                    # ``_copy_into_target`` succeeds — the second one
                    # then hits ENOENT because the staged source has
                    # already been moved into the target. Tar
                    # semantics already make the last write win at
                    # extraction time, so we record one entry per
                    # POSIX-relative path. A well-formed Nova export
                    # never produces duplicates; the dedup is
                    # defence-in-depth against externally-generated
                    # tarballs.
                    if posix_rel in seen_extracted:
                        continue
                    seen_extracted.add(posix_rel)
                    extracted.append(posix_rel)
    except tarfile.TarError as exc:
        return [], skipped, f"Archive could not be extracted: {exc.__class__.__name__}."
    except OSError as exc:
        return [], skipped, (
            f"Archive could not be extracted: "
            f"{exc.strerror or 'OS error'}."
        )
    return extracted, skipped, None


def _newly_created_parents(
    dst: Path, target_root: Path,
) -> list[Path]:
    """Return the parent directories that would be newly created.

    Walks from ``dst.parent`` upward, collecting every component
    that does not yet exist on disk, and stops at ``target_root``
    (or at the filesystem root). The returned list is in
    outer-most-first order, which is the order
    ``Path.mkdir(parents=True)`` would create them. Callers reverse
    the list for deepest-first removal during rollback.

    The function never raises; OS errors during stat short-circuit
    and return whatever was collected so far.
    """
    target_resolved = target_root.resolve()
    needed: list[Path] = []
    p = dst.parent
    while True:
        try:
            if p.exists():
                break
        except OSError:
            break
        try:
            p_resolved = p.resolve(strict=False)
        except OSError:
            break
        if p_resolved == target_resolved:
            break
        # Defence in depth: stop if we have walked above the target
        # root somehow (only possible with symlink trickery).
        try:
            p_resolved.relative_to(target_resolved)
        except ValueError:
            break
        needed.append(p)
        new_parent = p.parent
        if new_parent == p:
            break
        p = new_parent
    return list(reversed(needed))


def _copy_into_target(
    staging: Path,
    target_root: Path,
    relative_files: list[str],
) -> tuple[list[str], list[str], Optional[str]]:
    """Copy staged files into ``target_root`` with rollback on failure.

    Returns ``(restored, conflicts, error)``. ``restored`` is the
    list of POSIX-relative paths actually copied; ``conflicts``
    lists target paths that already existed and were replaced
    (those files were also captured by the pre-restore backup, plus
    stashed in-staging for per-restore rollback). ``error`` is
    ``None`` on success.

    Each per-file copy is staged in two steps so a mid-run failure
    can be rolled back atomically:

      1. If the target file already exists, move it to
         ``<staging>/.replaced-originals/<rel>``.
      2. Move the staged source file into the target via
         ``os.replace`` (atomic on the same filesystem).

    On any failure the function walks back over the successful
    moves: each ``.replaced-originals`` file is moved back to its
    original target path; files that did not exist before are
    deleted. Newly-created parent directories (created by
    ``dst.parent.mkdir(parents=True)``) are also rmdir'd in
    deepest-first order so the target tree returns bit-for-bit to
    its pre-restore shape. The pre-restore backup (created earlier
    by :func:`_create_pre_restore_backup`) remains as the second
    line of defence if even the rollback fails.

    The returned ``restored`` / ``conflicts`` lists are intentionally
    empty on a failure path so the caller does not report files as
    "restored" when the rollback returned them to their previous
    state.
    """
    restored: list[str] = []
    conflicts: list[str] = []
    target_resolved = target_root.resolve()
    stash_dir = staging / ".replaced-originals"
    try:
        stash_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return [], [], (
            f"Could not create rollback stash: "
            f"{exc.strerror or 'OS error'}."
        )

    # Successfully-committed moves so a later failure can roll back.
    # Each entry is ``(dst, stash_path | None, created_dirs)``.
    # ``stash_path is None`` means "the file did not exist before;
    # rollback should delete it". ``created_dirs`` is the (possibly
    # empty) list of parent directories ``dst.parent.mkdir`` brought
    # into existence for this commit; rollback rmdirs them in
    # deepest-first order.
    committed: list[tuple[Path, Optional[Path], list[Path]]] = []
    # Parents that the *in-flight* iteration brought into existence
    # but haven't yet been committed. The rollback path consumes
    # this list too so a failure between mkdir and the final
    # ``os.replace`` doesn't leave empty directories behind.
    in_flight_dirs: list[Path] = []

    def _rollback() -> None:
        # Walk in reverse so a newly-created directory tree unwinds
        # in the right order. Best-effort: a failure here leaves
        # the pre-restore backup as the operator-facing recovery.
        for dst_path, stash, created_dirs in reversed(committed):
            if stash is None:
                try:
                    dst_path.unlink(missing_ok=True)
                except OSError:
                    pass
            else:
                try:
                    os.replace(str(stash), str(dst_path))
                except OSError:
                    pass
            # Remove parent directories we created for this commit,
            # deepest first. ``rmdir`` only succeeds on empty
            # directories so a parent that picked up another
            # committed file in the same run is left alone.
            for new_dir in reversed(created_dirs):
                try:
                    new_dir.rmdir()
                except OSError:
                    pass
        # Mop up any in-flight directories that were created for
        # the failing iteration but never made it into
        # ``committed``.
        for new_dir in reversed(in_flight_dirs):
            try:
                new_dir.rmdir()
            except OSError:
                pass

    for rel in relative_files:
        src = staging / Path(*rel.split("/"))
        dst = target_root / Path(*rel.split("/"))
        # Post-resolution containment check on the destination.
        try:
            dst_resolved = dst.resolve(strict=False)
            dst_resolved.relative_to(target_resolved)
        except (OSError, ValueError):
            _rollback()
            return [], [], (
                f"Refusing to write {rel!r}: it would land outside "
                "the target data directory."
            )
        # Snapshot which parent directories don't yet exist so
        # rollback can rmdir them. Must be computed *before* the
        # mkdir call below — afterwards every parent exists. The
        # in-flight tracker holds them until either the iteration
        # commits (they move to ``committed``) or fails (rollback
        # rmdirs them).
        in_flight_dirs = _newly_created_parents(dst, target_root)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _rollback()
            return [], [], (
                f"Could not prepare {rel!r}: "
                f"{exc.strerror or 'OS error'}."
            )

        # Type-gate the destination before any move. A directory,
        # symlink, fifo, or other special node at ``dst`` would be
        # accepted by ``os.replace`` for the stash step, but the
        # rollback path can't put it back over a regular file —
        # ``os.replace(dir, file)`` errors out, the staging cleanup
        # then deletes the stash, and the original target data is
        # permanently lost. Refuse those upfront and unwind whatever
        # we have committed so far.
        dst_is_symlink = dst.is_symlink()
        existed = dst.exists() or dst_is_symlink
        if existed:
            if dst_is_symlink:
                _rollback()
                return [], [], (
                    f"Refusing to replace {rel!r}: existing target "
                    "is a symlink."
                )
            if not dst.is_file():
                _rollback()
                return [], [], (
                    f"Refusing to replace {rel!r}: existing target "
                    "is not a regular file (directory or special "
                    "node)."
                )
        stash_path: Optional[Path] = None
        if existed:
            stash_path = stash_dir / Path(*rel.split("/"))
            try:
                stash_path.parent.mkdir(parents=True, exist_ok=True)
                # Move existing file aside. ``os.replace`` is atomic
                # on the same filesystem; staging lives inside the
                # data root by construction.
                os.replace(str(dst), str(stash_path))
            except OSError as exc:
                _rollback()
                return [], [], (
                    f"Could not stash existing {rel!r}: "
                    f"{exc.strerror or 'OS error'}."
                )

        try:
            # ``os.replace`` is atomic on the same filesystem. The
            # staging directory lives **inside** the target root by
            # construction so this never falls back to a cross-FS
            # copy.
            os.replace(str(src), str(dst))
        except OSError as exc:
            # Best-effort recovery for *this* file before unwinding
            # the rest: move the stash back into place if we just
            # bumped a pre-existing file out of the way.
            if stash_path is not None and stash_path.exists():
                try:
                    os.replace(str(stash_path), str(dst))
                except OSError:
                    pass
            _rollback()
            return [], [], (
                f"Could not write {rel!r}: "
                f"{exc.strerror or 'OS error'}."
            )
        try:
            os.chmod(dst, 0o600)
        except OSError:
            pass
        restored.append(rel)
        if existed:
            conflicts.append(rel)
        # Promote the in-flight parents into the committed record so
        # they roll back together with the file move. Reset the
        # in-flight tracker for the next iteration.
        committed.append((dst, stash_path, in_flight_dirs))
        in_flight_dirs = []
    return restored, conflicts, None


def _cleanup_staging(staging: Path) -> None:
    """Remove ``staging`` recursively, swallowing OS errors.

    The staging directory always lives inside the target data root,
    so the recursive delete cannot escape it even if something
    upstream had failed.
    """
    if not staging.exists():
        return
    try:
        shutil.rmtree(str(staging), ignore_errors=True)
    except OSError:
        pass


def apply_restore(
    archive_path: str | os.PathLike[str],
    *,
    target_data_dir: Optional[str | os.PathLike[str]] = None,
    confirm: bool = False,
    confirmed_manifest_id: Optional[str] = None,
    dry_run: bool = False,
) -> RestoreResult:
    """Restore ``archive_path`` into the target data directory.

    The function never writes anything unless **all** of the following
    are true:

    * ``dry_run`` is ``False``,
    * ``confirm`` is ``True``,
    * the archive inspects clean,
    * a pre-restore backup of the current target was created
      successfully (or none was needed — empty target).

    Any failure short-circuits to a :class:`RestoreResult` with an
    explanatory ``refuse_reason``, leaves the target data directory
    bit-for-bit identical, and cleans up staging on a best-effort
    basis. Failed restores **must not** corrupt current data — this
    contract is exercised by tests.

    ``confirmed_manifest_id`` lets the caller pin the archive they
    inspected. When provided, it must match the manifest's
    ``created_at`` field — a mismatch refuses the restore so a
    different archive cannot slip in between the inspect step and
    the restore step.
    """
    archive_path_s = str(archive_path)
    archive_p = Path(archive_path_s)
    if not archive_p.is_file():
        return RestoreResult(
            archive_path=archive_path_s,
            target_data_dir="",
            outcome=RESTORE_OUTCOME_REFUSED,
            refuse_reason="Archive does not exist or is not a regular file.",
            confirmed=bool(confirm),
            restored_files=(),
            skipped_files=(),
            conflicts=(),
            backup_path="",
            backup_size=0,
            restart_recommended=False,
            warnings=(),
            manifest=None,
        )

    # Phase 1: inspection (no writes, no staging).
    inspection = inspect_export(archive_path_s)
    if not inspection.valid:
        return RestoreResult(
            archive_path=archive_path_s,
            target_data_dir="",
            outcome=RESTORE_OUTCOME_REFUSED,
            refuse_reason=(
                "Archive is not valid: " + "; ".join(inspection.errors)
            ),
            confirmed=bool(confirm),
            restored_files=(),
            skipped_files=(),
            conflicts=(),
            backup_path="",
            backup_size=0,
            restart_recommended=False,
            warnings=inspection.warnings,
            manifest=inspection.manifest,
        )

    # Optional pinning of the archive identity. A non-empty
    # ``confirmed_manifest_id`` is a deliberate "I inspected
    # exactly this archive" assertion — it must match the
    # archive's manifest id exactly, including when the archive
    # has no usable id of its own. ``None`` and ``""`` both mean
    # "no pin requested" (the CLI omits the flag, the UI sends
    # ``null`` when it has no id to pin against).
    if confirmed_manifest_id:
        actual = _manifest_id(inspection.manifest)
        if actual != confirmed_manifest_id:
            return RestoreResult(
                archive_path=archive_path_s,
                target_data_dir="",
                outcome=RESTORE_OUTCOME_REFUSED,
                refuse_reason=(
                    "Confirmed manifest id does not match the archive's "
                    "manifest. Re-inspect the package before confirming."
                ),
                confirmed=bool(confirm),
                restored_files=(),
                skipped_files=(),
                conflicts=(),
                backup_path="",
                backup_size=0,
                restart_recommended=False,
                warnings=inspection.warnings,
                manifest=inspection.manifest,
            )

    # Resolve the target root.
    target_root = _resolve_target_root(target_data_dir)
    if target_root is None:
        return RestoreResult(
            archive_path=archive_path_s,
            target_data_dir="",
            outcome=RESTORE_OUTCOME_REFUSED,
            refuse_reason=(
                "NOVA_DATA_DIR is not set. Configure a target data "
                "directory before restoring."
            ),
            confirmed=bool(confirm),
            restored_files=(),
            skipped_files=(),
            conflicts=(),
            backup_path="",
            backup_size=0,
            restart_recommended=False,
            warnings=inspection.warnings,
            manifest=inspection.manifest,
        )

    # Resolve the target lexically. ``Path.resolve(strict=False)``
    # does not require the directory to exist, so the dry-run path
    # below stays strictly read-only — no mkdir, no statvfs, no
    # touch. The actual ``mkdir`` is deferred until after the
    # dry-run early return so a dry-run against a missing target
    # never creates a directory on disk.
    target_resolved = target_root.resolve()

    # Phase 2: compute the "would restore" file list (also checks
    # post-resolution containment of each archive member against the
    # target root — we do **not** rely on the dry-run plan refusing
    # an existing nova.db because Phase 3's real restore is allowed
    # to replace it under explicit confirmation).
    #
    # The same export-side allowlist that prevents secrets / VCS /
    # caches / Ollama blobs / arbitrary non-canonical files from
    # being *exported* is applied here on the *restore* side too,
    # so a crafted archive cannot smuggle in a ``data/.env`` or a
    # ``data/.ssh/id_rsa`` that the exporter would have refused.
    would_restore: list[str] = []
    would_restore_seen: set[str] = set()
    # Path-type collision detection. ``file_paths`` lists every
    # would-be file destination as a POSIX string; ``dir_paths``
    # lists every ancestor directory implied by those files. A
    # crafted archive that contains both ``data/foo`` (file) and
    # ``data/foo/bar`` (file) would deterministically fail at
    # extraction (``mkdir`` cannot create ``foo/`` when ``foo`` is
    # already a file). Detecting the collision here keeps inspect
    # / dry-run / real restore consistent: any ancestor-vs-descendant
    # clash refuses the whole restore upfront.
    file_paths_seen: set[str] = set()
    dir_paths_seen: set[str] = set()
    conflicts_seen: list[str] = []
    skipped_in_archive: list[ExcludedEntry] = []
    has_nova_db_in_archive = False
    for member_name in inspection.files:
        if member_name in (ARCHIVE_MANIFEST_NAME, ARCHIVE_RESTORE_DOC_NAME):
            continue
        parts = PurePosixPath(member_name).parts
        if not parts or parts[0] != ARCHIVE_DATA_PREFIX:
            continue
        relative = PurePosixPath(*parts[1:])
        if not str(relative):
            continue
        if ".." in relative.parts or relative.is_absolute():
            return RestoreResult(
                archive_path=archive_path_s,
                target_data_dir=str(target_resolved),
                outcome=RESTORE_OUTCOME_REFUSED,
                refuse_reason=(
                    "Archive contains an unsafe path "
                    f"({member_name!r}). Refusing to restore."
                ),
                confirmed=bool(confirm),
                restored_files=(),
                skipped_files=(),
                conflicts=(),
                backup_path="",
                backup_size=0,
                restart_recommended=False,
                warnings=inspection.warnings,
                manifest=inspection.manifest,
            )
        allow_reason = _restore_allowlist_reason(relative.parts)
        if allow_reason is not None:
            skipped_in_archive.append(
                ExcludedEntry(str(relative), allow_reason)
            )
            continue
        candidate = target_resolved / Path(*relative.parts)
        try:
            candidate_abs = candidate.resolve(strict=False)
            candidate_abs.relative_to(target_resolved)
        except (OSError, ValueError):
            return RestoreResult(
                archive_path=archive_path_s,
                target_data_dir=str(target_resolved),
                outcome=RESTORE_OUTCOME_REFUSED,
                refuse_reason=(
                    f"Archive member {member_name!r} would land outside "
                    "the target data directory."
                ),
                confirmed=bool(confirm),
                restored_files=(),
                skipped_files=(),
                conflicts=(),
                backup_path="",
                backup_size=0,
                restart_recommended=False,
                warnings=inspection.warnings,
                manifest=inspection.manifest,
            )
        rel_str = str(relative)
        # Deduplicate so a tarball with duplicate members (an
        # externally-generated archive, defence in depth) reports a
        # single entry per POSIX path in the dry-run preview. This
        # matches the extraction-time dedup in
        # ``_extract_to_staging`` so dry-run and real-restore stay
        # consistent.
        if rel_str in would_restore_seen:
            continue
        would_restore_seen.add(rel_str)
        # Path-type collision: refuse if this file's path is
        # already in use as someone else's ancestor directory
        # (a previously-added file would require it to be a
        # directory), or if any ancestor of this path was already
        # added as a file (we'd have to ``mkdir`` over an
        # existing file). Either way the real restore would
        # deterministically fail at extraction time, so the
        # preflight refuses upfront.
        if rel_str in dir_paths_seen:
            return RestoreResult(
                archive_path=archive_path_s,
                target_data_dir=str(target_resolved),
                outcome=RESTORE_OUTCOME_REFUSED,
                refuse_reason=(
                    f"Archive contains a path-type collision: "
                    f"{rel_str!r} is a file in this archive but also "
                    "the parent of another archive entry. Restore "
                    "cannot resolve both."
                ),
                confirmed=bool(confirm),
                restored_files=(),
                skipped_files=(),
                conflicts=(),
                backup_path="",
                backup_size=0,
                restart_recommended=False,
                warnings=inspection.warnings,
                manifest=inspection.manifest,
            )
        ancestor_components: list[str] = []
        for part in relative.parts[:-1]:
            ancestor_components.append(part)
            anc = "/".join(ancestor_components)
            if anc in file_paths_seen:
                return RestoreResult(
                    archive_path=archive_path_s,
                    target_data_dir=str(target_resolved),
                    outcome=RESTORE_OUTCOME_REFUSED,
                    refuse_reason=(
                        f"Archive contains a path-type collision: "
                        f"{anc!r} is both a file and a parent "
                        f"directory of {rel_str!r}. Restore cannot "
                        "resolve both."
                    ),
                    confirmed=bool(confirm),
                    restored_files=(),
                    skipped_files=(),
                    conflicts=(),
                    backup_path="",
                    backup_size=0,
                    restart_recommended=False,
                    warnings=inspection.warnings,
                    manifest=inspection.manifest,
                )
            dir_paths_seen.add(anc)
        file_paths_seen.add(rel_str)
        # Preflight type-gate on the existing destination so the
        # dry-run reports the same outcome the real restore would
        # produce. ``_copy_into_target`` refuses to stash a symlink
        # or a non-regular file at ``dst``; mirror that decision
        # here so a target whose ``nova.db`` is a symlink doesn't
        # show ``outcome=dry_run`` and then deterministically fail
        # at execution time. Refusal is structural, not per-file:
        # any non-regular destination invalidates the whole
        # restore.
        if candidate.is_symlink():
            return RestoreResult(
                archive_path=archive_path_s,
                target_data_dir=str(target_resolved),
                outcome=RESTORE_OUTCOME_REFUSED,
                refuse_reason=(
                    f"Existing target {rel_str!r} is a symlink. "
                    "Restore never follows symlinks at the destination."
                ),
                confirmed=bool(confirm),
                restored_files=(),
                skipped_files=(),
                conflicts=(),
                backup_path="",
                backup_size=0,
                restart_recommended=False,
                warnings=inspection.warnings,
                manifest=inspection.manifest,
            )
        if candidate.exists() and not candidate.is_file():
            return RestoreResult(
                archive_path=archive_path_s,
                target_data_dir=str(target_resolved),
                outcome=RESTORE_OUTCOME_REFUSED,
                refuse_reason=(
                    f"Existing target {rel_str!r} is not a regular "
                    "file (directory or special node). Restore would "
                    "be unable to replace it safely."
                ),
                confirmed=bool(confirm),
                restored_files=(),
                skipped_files=(),
                conflicts=(),
                backup_path="",
                backup_size=0,
                restart_recommended=False,
                warnings=inspection.warnings,
                manifest=inspection.manifest,
            )
        would_restore.append(rel_str)
        if relative.parts and relative.parts[0] == _paths.DB_FILENAME:
            has_nova_db_in_archive = True
        if candidate.exists():
            conflicts_seen.append(rel_str)

    # Existing target nova.db is the most-sensitive overwrite. Phase
    # 2's plan_restore refuses unconditionally; Phase 3's real
    # restore allows it iff the caller explicitly confirms.
    target_nova_db = target_resolved / _paths.DB_FILENAME
    target_has_db = target_nova_db.exists()

    # Refuse upfront when there is nothing to restore. This keeps
    # dry-run and real-restore in lockstep: the real-restore path
    # would later hit ``if not extracted:`` inside
    # ``_extract_to_staging`` and refuse with the same reason;
    # surfacing the refusal here means the dry-run preview never
    # says "would proceed" for an archive that holds only
    # directory members or whose only files were filtered by the
    # restore allowlist.
    if not would_restore:
        return RestoreResult(
            archive_path=archive_path_s,
            target_data_dir=str(target_resolved),
            outcome=RESTORE_OUTCOME_REFUSED,
            refuse_reason=(
                "Archive contains no extractable Nova data files."
            ),
            confirmed=bool(confirm),
            restored_files=(),
            skipped_files=tuple(skipped_in_archive),
            conflicts=(),
            backup_path="",
            backup_size=0,
            restart_recommended=False,
            warnings=inspection.warnings,
            manifest=inspection.manifest,
        )

    if dry_run:
        return RestoreResult(
            archive_path=archive_path_s,
            target_data_dir=str(target_resolved),
            outcome=RESTORE_OUTCOME_DRY_RUN,
            refuse_reason="",
            confirmed=bool(confirm),
            restored_files=tuple(would_restore),
            skipped_files=tuple(skipped_in_archive),
            conflicts=tuple(conflicts_seen),
            backup_path="",
            backup_size=0,
            restart_recommended=has_nova_db_in_archive,
            warnings=tuple(_dry_run_warnings(
                target_has_db, has_nova_db_in_archive, conflicts_seen,
                inspection.warnings,
            )),
            manifest=inspection.manifest,
        )

    # Real restore: every gate from here on must succeed.
    if not confirm:
        return RestoreResult(
            archive_path=archive_path_s,
            target_data_dir=str(target_resolved),
            outcome=RESTORE_OUTCOME_REFUSED,
            refuse_reason=(
                "Restore requires explicit confirmation. Re-run with "
                "confirm=true after reviewing the dry-run plan."
            ),
            confirmed=False,
            restored_files=(),
            skipped_files=(),
            conflicts=tuple(conflicts_seen),
            backup_path="",
            backup_size=0,
            restart_recommended=has_nova_db_in_archive,
            warnings=inspection.warnings,
            manifest=inspection.manifest,
        )

    # Confirmation has cleared. Now (and only now) is it safe to
    # create the target data directory on disk — the dry-run path
    # never reaches this point so a dry-run against a missing
    # target leaves the filesystem untouched.
    try:
        target_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return RestoreResult(
            archive_path=archive_path_s,
            target_data_dir=str(target_resolved),
            outcome=RESTORE_OUTCOME_FAILED,
            refuse_reason=(
                f"Target data directory could not be prepared: "
                f"{exc.strerror or 'OS error'}"
            ),
            confirmed=bool(confirm),
            restored_files=(),
            skipped_files=(),
            conflicts=tuple(conflicts_seen),
            backup_path="",
            backup_size=0,
            restart_recommended=has_nova_db_in_archive,
            warnings=inspection.warnings,
            manifest=inspection.manifest,
        )

    # Pre-restore backup. Required when the target has any canonical
    # Nova data on disk; skipped (with a warning) when the target is
    # empty — there is literally nothing to back up.
    backup_archive_path = ""
    backup_size = 0
    backup_warnings: list[str] = []
    target_has_data = _target_has_canonical_data(target_resolved)
    if target_has_data:
        backup, size, backup_warnings = _create_pre_restore_backup(
            target_resolved,
        )
        if backup is None:
            return RestoreResult(
                archive_path=archive_path_s,
                target_data_dir=str(target_resolved),
                outcome=RESTORE_OUTCOME_BACKUP_FAILED,
                refuse_reason=(
                    "Pre-restore backup could not be created — "
                    "refusing to overwrite current data. See warnings."
                ),
                confirmed=bool(confirm),
                restored_files=(),
                skipped_files=(),
                conflicts=tuple(conflicts_seen),
                backup_path="",
                backup_size=0,
                restart_recommended=has_nova_db_in_archive,
                warnings=tuple(
                    list(inspection.warnings) + backup_warnings
                ),
                manifest=inspection.manifest,
            )
        backup_archive_path = str(backup)
        backup_size = size

    # Extract into staging. The staging directory holds (a) the
    # extracted archive contents and (b) the rollback stash of any
    # files we displaced during the copy phase. It is **never
    # blindly cleaned up at startup**: doing so used to silently
    # delete a concurrent restore's in-flight stash, breaking the
    # "failed restore leaves data intact" guarantee for the other
    # caller. ``mkdir(exist_ok=False)`` atomically creates the
    # directory, so two concurrent restores deterministically lose
    # one: the loser sees ``FileExistsError`` and returns
    # ``outcome=refused`` with a clear message. A staging directory
    # left behind by a crashed restore also triggers the same
    # refusal; the operator can investigate and remove it by hand
    # once they have confirmed no other restore is running.
    staging = target_resolved / RESTORE_STAGING_DIRNAME
    try:
        staging.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        return RestoreResult(
            archive_path=archive_path_s,
            target_data_dir=str(target_resolved),
            outcome=RESTORE_OUTCOME_REFUSED,
            refuse_reason=(
                f"Staging directory {RESTORE_STAGING_DIRNAME!r} "
                "already exists in the target. Another restore may "
                "be in progress, or a previous restore was "
                "interrupted. Confirm no other restore is running, "
                "then remove the staging directory by hand and "
                "retry."
            ),
            confirmed=bool(confirm),
            restored_files=(),
            skipped_files=(),
            conflicts=tuple(conflicts_seen),
            backup_path=backup_archive_path,
            backup_size=backup_size,
            restart_recommended=has_nova_db_in_archive,
            warnings=tuple(
                list(inspection.warnings) + backup_warnings
            ),
            manifest=inspection.manifest,
        )
    except OSError as exc:
        return RestoreResult(
            archive_path=archive_path_s,
            target_data_dir=str(target_resolved),
            outcome=RESTORE_OUTCOME_FAILED,
            refuse_reason=(
                f"Could not create staging directory: "
                f"{exc.strerror or 'OS error'}"
            ),
            confirmed=bool(confirm),
            restored_files=(),
            skipped_files=(),
            conflicts=tuple(conflicts_seen),
            backup_path=backup_archive_path,
            backup_size=backup_size,
            restart_recommended=has_nova_db_in_archive,
            warnings=tuple(
                list(inspection.warnings) + backup_warnings
            ),
            manifest=inspection.manifest,
        )

    extracted, _extract_skipped, extract_err = _extract_to_staging(
        archive_p, staging,
    )
    # ``_extract_skipped`` is a defence-in-depth sibling of
    # ``skipped_in_archive`` — both gates run the same allowlist
    # check on each member, so they should agree in well-formed
    # cases. We surface ``skipped_in_archive`` as the wire-format
    # ``skipped_files`` so dry-run and real-restore consumers see
    # the same path shape (POSIX-relative, no ``data/`` prefix).
    if extract_err is not None:
        _cleanup_staging(staging)
        return RestoreResult(
            archive_path=archive_path_s,
            target_data_dir=str(target_resolved),
            outcome=RESTORE_OUTCOME_EXTRACT_FAILED,
            refuse_reason=extract_err,
            confirmed=bool(confirm),
            restored_files=(),
            skipped_files=tuple(skipped_in_archive),
            conflicts=tuple(conflicts_seen),
            backup_path=backup_archive_path,
            backup_size=backup_size,
            restart_recommended=has_nova_db_in_archive,
            warnings=tuple(
                list(inspection.warnings) + backup_warnings
            ),
            manifest=inspection.manifest,
        )

    if not extracted:
        _cleanup_staging(staging)
        return RestoreResult(
            archive_path=archive_path_s,
            target_data_dir=str(target_resolved),
            outcome=RESTORE_OUTCOME_REFUSED,
            refuse_reason=(
                "Archive contained no extractable Nova data files."
            ),
            confirmed=bool(confirm),
            restored_files=(),
            skipped_files=tuple(skipped_in_archive),
            conflicts=tuple(conflicts_seen),
            backup_path=backup_archive_path,
            backup_size=backup_size,
            restart_recommended=False,
            warnings=tuple(
                list(inspection.warnings) + backup_warnings
            ),
            manifest=inspection.manifest,
        )

    restored, conflicts, copy_err = _copy_into_target(
        staging, target_resolved, extracted,
    )
    if copy_err is not None:
        _cleanup_staging(staging)
        return RestoreResult(
            archive_path=archive_path_s,
            target_data_dir=str(target_resolved),
            outcome=RESTORE_OUTCOME_FAILED,
            refuse_reason=copy_err,
            confirmed=bool(confirm),
            restored_files=tuple(restored),
            skipped_files=tuple(skipped_in_archive),
            conflicts=tuple(conflicts),
            backup_path=backup_archive_path,
            backup_size=backup_size,
            restart_recommended=has_nova_db_in_archive,
            warnings=tuple(
                list(inspection.warnings) + backup_warnings
            ),
            manifest=inspection.manifest,
        )
    _cleanup_staging(staging)

    final_warnings: list[str] = list(inspection.warnings) + backup_warnings
    if has_nova_db_in_archive:
        final_warnings.append(
            "Restart Nova so the new nova.db is picked up by the "
            "running process. The previous database is preserved in "
            "the pre-restore backup."
        )
    if not target_has_data:
        final_warnings.append(
            "Target data directory contained no canonical Nova data; "
            "no pre-restore backup was needed."
        )

    return RestoreResult(
        archive_path=archive_path_s,
        target_data_dir=str(target_resolved),
        outcome=RESTORE_OUTCOME_RESTORED,
        refuse_reason="",
        confirmed=True,
        restored_files=tuple(restored),
        skipped_files=tuple(skipped_in_archive),
        conflicts=tuple(conflicts),
        backup_path=backup_archive_path,
        backup_size=backup_size,
        restart_recommended=has_nova_db_in_archive,
        warnings=tuple(final_warnings),
        manifest=inspection.manifest,
    )


def _target_has_canonical_data(target_root: Path) -> bool:
    """Return True when the target contains a file the backup would pack.

    The decision must match :func:`_create_pre_restore_backup`'s
    allowlist — otherwise ``apply_restore`` will demand a backup,
    the backup builder will skip every entry, and the restore is
    refused with a spurious ``outcome=backup_failed``. That false
    positive shows up on installations where a reserved
    subdirectory holds only excluded files (a stray
    ``backups/.env``, a ``logs/__pycache__/foo.pyc`` left behind by
    tooling, …): the directory is non-empty but contains nothing
    the backup builder would actually preserve.

    The check also excludes the pre-restore backup subtree so a
    target whose only "canonical" content is previous pre-restore
    archives doesn't trigger a backup-of-backups loop on
    follow-up restores.

    The walk is cheap for typical Nova installs and never raises
    into the caller.
    """
    if not target_root.exists():
        return False
    included_pairs, _ = _walk_allowlisted(target_root)
    # Mirror the pre-restore-archive filter in
    # :func:`_create_pre_restore_backup`: a target that contains
    # *only* previous pre-restore archives is treated as having no
    # canonical data, so we skip the backup step (there's nothing
    # the operator hasn't already preserved via the previous
    # pre-restore backup).
    for _file_p, rel_parts in included_pairs:
        if (
            len(rel_parts) >= 2
            and rel_parts[0] == _paths.BACKUPS_SUBDIR
            and rel_parts[1] == PRE_RESTORE_BACKUP_SUBDIR
        ):
            continue
        return True
    return False


def _dry_run_warnings(
    target_has_db: bool,
    has_nova_db_in_archive: bool,
    conflicts: list[str],
    inspection_warnings: tuple[str, ...],
) -> list[str]:
    """Compose the warning list rendered by a dry-run restore.

    The dry-run is informational — Phase 3 surfaces every "would
    happen" caveat the operator should see before flipping
    ``confirm=true``.
    """
    warnings: list[str] = list(inspection_warnings)
    if target_has_db and has_nova_db_in_archive:
        warnings.append(
            "Target already contains nova.db. The real restore will "
            "create an automatic pre-restore backup, then replace the "
            "database. The previous nova.db will live inside the "
            "pre-restore backup so you can roll back."
        )
    if conflicts:
        warnings.append(
            f"{len(conflicts)} file(s) at the target would be replaced. "
            "Each replaced file is preserved inside the pre-restore "
            "backup."
        )
    warnings.append(
        "Stop Nova before running the real restore. The dry-run is "
        "safe to run while Nova is running."
    )
    return warnings


# ── CLI ─────────────────────────────────────────────────────────────
#
# ``python -m core.data_export <command> [...]`` exposes the three
# public helpers in a tiny argparse wrapper so an operator can build,
# inspect, and dry-run a restore without going through the admin UI.
# The CLI is the only surface here that talks to ``stdout`` / ``stderr``
# — the library functions never print.
#
# Commands:
#
#   export                 Build a portable archive in the exports
#                          directory (or under ``--output``).
#   inspect <path>         Read-only structural check on an existing
#                          archive — never writes anywhere.
#   restore-dry-run <path> Dry-run plan only — never writes anywhere.
#
# Every command exits 0 on success, 1 on a user-visible failure, and
# 2 for argparse usage errors. Exit codes are intentional so the CLI
# composes cleanly with shell pipelines and CI checks.
#
# The CLI deliberately stays tiny: the heavy lifting lives in the
# functions above so the unit tests can drive them without spawning a
# subprocess (mirroring ``core/paths.py``'s ``_cli`` pattern).


def _format_bytes(size: int) -> str:
    """Render ``size`` as a short, human-readable string.

    Used by the CLI summaries; tests pin the wire format only at the
    program-output level, so the rendering is free to change as long
    as the keywords ("MB", "GB") stay recognisable.
    """
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{size / (1024 * 1024 * 1024):.2f} GB"


def _format_export_summary(result: ExportResult) -> str:
    """Render a short human summary of an :class:`ExportResult`."""
    manifest = result.manifest or {}
    lines = [
        f"Nova export package created at {result.archive_path}",
        (
            f"  format        : {manifest.get('format', '')} "
            f"v{manifest.get('format_version', '')}"
        ),
        f"  mode          : {manifest.get('mode', '')}",
        f"  created (UTC) : {manifest.get('created_at', '')}",
        f"  source        : {manifest.get('source_data_dir', '')}",
        f"  archive size  : {_format_bytes(result.archive_size)}",
        f"  sha256        : {result.archive_sha256}",
        f"  files included: {len(result.included)}",
        f"  files excluded: {len(result.excluded)}",
    ]
    if result.warnings:
        lines.append("  warnings:")
        for warning in result.warnings:
            lines.append(f"    - {warning}")
    lines.append("")
    lines.append(
        "Move this file to your backup target (e.g. "
        "/mnt/archive/Backups/Nova) via rsync or a removable disk."
    )
    lines.append(
        "Inspect before restoring on the target machine with:"
    )
    lines.append(
        f"    python -m core.data_export inspect {result.archive_path}"
    )
    return "\n".join(lines)


def _format_inspect_summary(result: InspectionResult) -> str:
    """Render a short human summary of an :class:`InspectionResult`."""
    lines = [f"Inspecting {result.archive_path}"]
    lines.append(f"  valid                  : {result.valid}")
    if result.manifest:
        lines.append(
            f"  format                 : {result.manifest.get('format', '')} "
            f"v{result.manifest.get('format_version', '')}"
        )
        lines.append(
            f"  mode                   : {result.manifest.get('mode', '')}"
        )
        lines.append(
            f"  created (UTC)          : "
            f"{result.manifest.get('created_at', '')}"
        )
        lines.append(
            f"  source data dir        : "
            f"{result.manifest.get('source_data_dir', '')}"
        )
    has_nova_db = any(
        f == f"{ARCHIVE_DATA_PREFIX}/{_paths.DB_FILENAME}"
        for f in result.files
    )
    lines.append(f"  nova.db present        : {has_nova_db}")
    lines.append(f"  total uncompressed size: "
                 f"{_format_bytes(result.total_uncompressed_size)}")
    lines.append(f"  member count           : {len(result.files)}")
    if result.errors:
        lines.append("  errors:")
        for err in result.errors:
            lines.append(f"    - {err}")
    if result.warnings:
        lines.append("  warnings:")
        for warning in result.warnings:
            lines.append(f"    - {warning}")
    return "\n".join(lines)


def _format_restore_plan(plan: RestorePlan) -> str:
    """Render a short human summary of a :class:`RestorePlan`."""
    lines = [f"Restore dry-run for {plan.archive_path}"]
    lines.append(f"  target data dir : {plan.target_data_dir}")
    lines.append(f"  allowed         : {plan.allowed}")
    if plan.refuse_reason:
        lines.append(f"  refuse reason   : {plan.refuse_reason}")
    lines.append(f"  would restore   : {len(plan.would_restore)} file(s)")
    if plan.conflicts:
        lines.append(
            f"  conflicts       : {len(plan.conflicts)} "
            "existing file(s) would be overwritten"
        )
        for path in plan.conflicts[:10]:
            lines.append(f"    ! {path}")
        if len(plan.conflicts) > 10:
            lines.append(
                f"    ... and {len(plan.conflicts) - 10} more"
            )
    if plan.warnings:
        lines.append("  warnings:")
        for warning in plan.warnings:
            lines.append(f"    - {warning}")
    lines.append("")
    lines.append(
        "This is a dry-run plan only. Nothing was written. Phase 2 "
        "does not perform an automated restore — follow the manual "
        "steps in docs/storage-and-migration.md when you are ready."
    )
    return "\n".join(lines)


def _format_restore_result(result: RestoreResult) -> str:
    """Render a short human summary of a :class:`RestoreResult`.

    Used by both the ``restore`` and ``restore-dry-run`` (Phase 3)
    subcommands so the CLI text-format stays consistent between
    flows. The dry-run form omits the backup line because no backup
    was written.
    """
    lines = [f"Restore {result.outcome} for {result.archive_path}"]
    lines.append(f"  target data dir   : {result.target_data_dir}")
    lines.append(f"  confirmed         : {result.confirmed}")
    if result.refuse_reason:
        lines.append(f"  refuse reason     : {result.refuse_reason}")
    lines.append(
        f"  restored files    : {len(result.restored_files)}"
    )
    if result.conflicts:
        lines.append(
            f"  replaced existing : {len(result.conflicts)} file(s)"
        )
        for path in result.conflicts[:10]:
            lines.append(f"    ~ {path}")
        if len(result.conflicts) > 10:
            lines.append(
                f"    ... and {len(result.conflicts) - 10} more"
            )
    if result.skipped_files:
        lines.append(
            f"  skipped from archive: {len(result.skipped_files)} entry(ies)"
        )
        for entry in result.skipped_files[:10]:
            lines.append(f"    ! {entry.path} ({entry.reason})")
    if result.backup_path:
        lines.append(
            f"  pre-restore backup: {result.backup_path}"
        )
        lines.append(
            f"  backup size       : {_format_bytes(result.backup_size)}"
        )
    if result.restart_recommended:
        lines.append(
            "  restart Nova so the new nova.db is picked up."
        )
    if result.warnings:
        lines.append("  warnings:")
        for warning in result.warnings:
            lines.append(f"    - {warning}")
    return "\n".join(lines)


def _stderr():  # pragma: no cover - trivial helper for monkeypatching
    """Return ``sys.stderr`` indirectly so tests can swap it out."""
    import sys

    return sys.stderr


def _cli(argv: list[str]) -> int:
    """Parse ``argv`` and dispatch a data-export subcommand.

    Returns a POSIX-style exit code:

    * ``0`` — command succeeded.
    * ``1`` — command failed for a user-visible reason (bad archive,
      unwritable destination, refused restore, …). The CLI prints a
      short error line to stderr; nothing else is touched on disk.
    * ``2`` — argparse-style usage error. The CLI prints the usage
      banner to stderr.

    The function is split out so tests can drive the CLI without
    spawning a subprocess, matching the pattern used by
    ``core.paths._cli``.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m core.data_export",
        description=(
            "Build, inspect, and dry-run-restore Nova data export "
            "packages. Read-only by default — only the export "
            "subcommand writes anything, and only inside the "
            "configured exports directory or an explicit --output."
        ),
    )
    subparsers = parser.add_subparsers(dest="command")

    export_parser = subparsers.add_parser(
        "export",
        help="Build a portable Nova data export package.",
    )
    export_parser.add_argument(
        "--output", "-o",
        default=None,
        help=(
            "Directory to write the archive into. Defaults to "
            "NOVA_DATA_DIR/exports (or ./exports in legacy mode)."
        ),
    )
    export_parser.add_argument(
        "--mode",
        default=MODE_DATA_ONLY,
        help=(
            "Export mode. Phase 2 supports 'data-only' only; "
            "'workspace' is reserved for future work."
        ),
    )
    export_parser.add_argument(
        "--stem",
        default=None,
        help=(
            "Override the default filename stem "
            f"({DEFAULT_EXPORT_STEM!r}). Letters, digits, '.', '_', "
            "'-' only."
        ),
    )

    inspect_parser = subparsers.add_parser(
        "inspect",
        help="Read-only structural check on an existing archive.",
    )
    inspect_parser.add_argument(
        "archive",
        help="Path to a Nova data export archive (.tar.gz).",
    )

    restore_parser = subparsers.add_parser(
        "restore-dry-run",
        help=(
            "Dry-run plan for restoring an archive. Never writes a "
            "file."
        ),
    )
    restore_parser.add_argument(
        "archive",
        help="Path to a Nova data export archive (.tar.gz).",
    )
    restore_parser.add_argument(
        "--data-dir",
        default=None,
        help=(
            "Target data directory. Defaults to the configured "
            "NOVA_DATA_DIR; failing that, the command refuses."
        ),
    )

    apply_parser = subparsers.add_parser(
        "restore",
        help=(
            "Restore an archive into the target data directory. "
            "Requires --confirm. Creates an automatic pre-restore "
            "backup before replacing any file."
        ),
    )
    apply_parser.add_argument(
        "archive",
        help="Path to a Nova data export archive (.tar.gz).",
    )
    apply_parser.add_argument(
        "--data-dir",
        default=None,
        help=(
            "Target data directory. Defaults to the configured "
            "NOVA_DATA_DIR; failing that, the command refuses."
        ),
    )
    apply_parser.add_argument(
        "--confirm",
        action="store_true",
        help=(
            "Confirm the restore explicitly. Without this flag the "
            "command refuses. Pair with --confirmed-manifest-id to "
            "pin the archive identity to the one you inspected."
        ),
    )
    apply_parser.add_argument(
        "--confirmed-manifest-id",
        default=None,
        help=(
            "Optional manifest identifier (the archive's 'created_at' "
            "timestamp from inspect output). When set, the restore "
            "refuses unless the archive's manifest matches."
        ),
    )

    if not argv:
        parser.print_help(file=_stderr())
        return 2

    args = parser.parse_args(argv)

    if args.command == "export":
        try:
            result = create_data_export(
                dest_dir=args.output,
                mode=args.mode,
                stem=args.stem,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=_stderr())
            return 1
        except RuntimeError as exc:
            print(f"error: {exc}", file=_stderr())
            return 1
        print(_format_export_summary(result))
        return 0

    if args.command == "inspect":
        result = inspect_export(args.archive)
        print(_format_inspect_summary(result))
        return 0 if result.valid else 1

    if args.command == "restore-dry-run":
        plan = plan_restore(
            args.archive,
            target_data_dir=args.data_dir,
        )
        print(_format_restore_plan(plan))
        # A refused plan is a *result*, not a CLI failure — the
        # operator asked us to plan, we did, the plan said "no".
        # Return 0 so shell scripts can branch on the structured
        # exit-1 reserved for unexpected errors. Tests pin this.
        return 0

    if args.command == "restore":
        if not args.confirm:
            print(
                "error: restore requires --confirm. Re-run with "
                "--confirm after reviewing the dry-run plan with "
                "`python -m core.data_export restore-dry-run`.",
                file=_stderr(),
            )
            return 1
        result = apply_restore(
            args.archive,
            target_data_dir=args.data_dir,
            confirm=True,
            confirmed_manifest_id=args.confirmed_manifest_id,
            dry_run=False,
        )
        print(_format_restore_result(result))
        if result.outcome == RESTORE_OUTCOME_RESTORED:
            return 0
        # A refused / failed restore is a CLI-level failure: the
        # operator typed `restore` and the data did not move. Tests
        # pin this so shell scripts can branch on it.
        return 1

    parser.print_help(file=_stderr())
    return 2


if __name__ == "__main__":  # pragma: no cover - exercised via tests
    import sys

    sys.exit(_cli(sys.argv[1:]))


__all__ = [
    "ARCHIVE_DATA_PREFIX",
    "ARCHIVE_MANIFEST_NAME",
    "ARCHIVE_RESTORE_DOC_NAME",
    "DEFAULT_EXPORT_STEM",
    "ExcludedEntry",
    "ExportResult",
    "FORMAT_ID",
    "FORMAT_VERSION",
    "IncludedEntry",
    "InspectionResult",
    "MODE_DATA_ONLY",
    "MODE_WORKSPACE",
    "PRE_RESTORE_BACKUP_SUBDIR",
    "REASON_CACHE",
    "REASON_NODE_MODULES",
    "REASON_NOT_ALLOWLISTED",
    "REASON_OLLAMA_MODEL",
    "REASON_OUTSIDE_DATA_DIR",
    "REASON_SECRET",
    "REASON_SYMLINK_ESCAPE",
    "REASON_VCS",
    "REASON_VENV",
    "RESTORE_OUTCOME_BACKUP_FAILED",
    "RESTORE_OUTCOME_DRY_RUN",
    "RESTORE_OUTCOME_EXTRACT_FAILED",
    "RESTORE_OUTCOME_FAILED",
    "RESTORE_OUTCOME_REFUSED",
    "RESTORE_OUTCOME_RESTORED",
    "RESTORE_STAGING_DIRNAME",
    "RestorePlan",
    "RestoreResult",
    "apply_restore",
    "create_data_export",
    "inspect_export",
    "plan_restore",
]
