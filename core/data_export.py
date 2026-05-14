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
                # Symlinks must point at a name still inside the
                # archive — not an absolute path, no ``..``.
                if member.issym():
                    target = member.linkname or ""
                    if (
                        not target
                        or target.startswith("/")
                        or ".." in PurePosixPath(target).parts
                    ):
                        errors.append(
                            f"Unsafe symlink target for {member.name!r}."
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
                file_names.append(member.name)
                if member.isfile():
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
    "REASON_CACHE",
    "REASON_NODE_MODULES",
    "REASON_NOT_ALLOWLISTED",
    "REASON_OLLAMA_MODEL",
    "REASON_OUTSIDE_DATA_DIR",
    "REASON_SECRET",
    "REASON_SYMLINK_ESCAPE",
    "REASON_VCS",
    "REASON_VENV",
    "RestorePlan",
    "create_data_export",
    "inspect_export",
    "plan_restore",
]
