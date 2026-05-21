"""Admin-configurable local GGUF model path (GGUF provider, Phase 2).

Phase 1 shipped the optional ``llamacpp`` provider — Nova can run a local
``.gguf`` model without Ollama — but the *only* way to point it at a model
file was editing ``NOVA_GGUF_MODEL_PATH`` in the environment and
restarting. This module adds a small, safe, admin-only configuration
surface so the model path can be set, validated, and tested from
**Settings → Models** without touching ``.env``.

Scope / safety contract (Phase 2):

* **Directory-confined, never arbitrary.** A model path is accepted only
  when it resolves (symlinks included) **inside** the configured model
  directory (``config.NOVA_MODEL_DIR``, default ``/mnt/archive/nova-models``),
  is an existing readable regular ``.gguf`` file, and contains no ``..``
  traversal. Anything else is refused with a short, sanitised reason and
  **nothing is written**. There is no filesystem browsing, no globbing,
  no directory walk, no shell — Nova validates exactly the one path an
  admin pastes.
* **Persisted like the Phase-2 default model.** The chosen path is a
  single host-wide row in the ``settings`` table via
  :func:`core.settings.save_system_setting` — an operator decision, not a
  per-user preference. It is deliberately **not** in
  ``core.settings.USER_SETTING_KEYS`` or ``config.ALLOWED_SETTINGS`` so it
  can never be written through the generic ``/settings`` path that would
  bypass the validation here.
* **Read path is cheap and never raises.**
  :func:`resolve_gguf_model_path` returns the persisted path if one is set
  (and a DB exists), else ``config.NOVA_GGUF_MODEL_PATH``. It performs no
  network I/O, never loads a model, and swallows every error so provider
  construction stays cheap and offline-safe.
* **Takes effect without a restart.** A successful write invalidates the
  cached ``llamacpp`` provider so the next generation rebuilds it against
  the new path — but nothing is loaded eagerly and nothing else changes.
* **No downloads, no deletion, no overwrite.** Nova never fetches,
  removes, or replaces a model. The operator supplies the file; this
  module only records which one to use.
* **Ollama is untouched and stays the default.** Configuring a GGUF path
  is harmless when ``NOVA_MODEL_PROVIDER`` is still ``ollama`` — it simply
  takes effect if/when the operator selects ``llamacpp``.
* **Sanitised errors.** Operator-facing messages name the relevant
  environment variable and the problem; they never echo a raw backend
  exception. Full detail is logged server-side only.

It is the foundation under the admin-only ``GET /admin/provider/gguf``,
``POST /admin/provider/gguf/model-path`` and ``POST
/admin/provider/gguf/test`` endpoints.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

#: Host-wide ``settings`` key holding the admin-selected GGUF model path.
#: Kept out of ``USER_SETTING_KEYS`` / ``ALLOWED_SETTINGS`` on purpose so
#: it can only ever be written through :func:`set_gguf_model_path`, which
#: validates the path against the allowed model directory first.
GGUF_MODEL_PATH_SETTING_KEY = "gguf_model_path"

#: Defensive cap on a pasted path. PATH_MAX on Linux is 4096; we accept up
#: to that so a crafted client can't smuggle a huge blob into the settings
#: row even though the value is also validated as a real file.
MAX_PATH_LEN = 4096

#: The backend label this surface configures. The GGUF provider is always
#: ``llamacpp`` regardless of which provider is currently the default.
PROVIDER_NAME = "llamacpp"

#: Bounds on the read-only model-library scan of ``NOVA_MODEL_DIR`` (Phase
#: 3). The scan is recursive but *strictly* bounded so it can never become
#: a filesystem-wide walk: it descends at most ``MAX_SCAN_DEPTH`` levels
#: below the model directory, visits at most ``MAX_DIRS_SCANNED``
#: directories, and returns at most ``MAX_LIBRARY_MODELS`` files. Hidden /
#: dot directories are pruned and symlinked directories are never followed
#: (see :func:`list_local_models`).
MAX_SCAN_DEPTH = 5
MAX_DIRS_SCANNED = 2000
MAX_LIBRARY_MODELS = 500

# Fixed, non-sensitive operator-facing message reused by status + test so
# the wording never drifts.
_NO_PATH_MSG = (
    "No GGUF model is configured. Paste the path to a .gguf file inside "
    "the model directory, or set NOVA_GGUF_MODEL_PATH."
)


class GgufModelPathError(Exception):
    """A requested GGUF model path was refused.

    Carries a short, **sanitised** reason safe to surface to the admin UI:
    it never echoes a raw backend exception or a path outside the one the
    caller supplied.
    """


# ── Config / persisted reads ─────────────────────────────────────────


def resolve_model_dir() -> str:
    """The directory GGUF model files must live inside (``NOVA_MODEL_DIR``).

    Best-effort and never raises: a missing / oddly-shaped config degrades
    to the recommended default rather than turning a read into a failure.
    """
    try:
        from config import NOVA_MODEL_DIR

        value = (NOVA_MODEL_DIR or "").strip()
        return value or "/mnt/archive/nova-models"
    except Exception as exc:  # pragma: no cover - config is stable
        logger.warning("gguf: model-dir lookup failed: %s", exc)
        return "/mnt/archive/nova-models"


def _config_model_path() -> str:
    """The env-provided GGUF model path (``NOVA_GGUF_MODEL_PATH``)."""
    try:
        from config import NOVA_GGUF_MODEL_PATH

        return (NOVA_GGUF_MODEL_PATH or "").strip()
    except Exception as exc:  # pragma: no cover - config is stable
        logger.warning("gguf: config path lookup failed: %s", exc)
        return ""


def _persisted_model_path() -> str:
    """The admin-persisted GGUF model path, or ``""`` if none / unavailable.

    Guards on the DB file existing before connecting so a read on a host
    that has not initialised its database yet (or a test that never set
    one up) neither raises nor creates a stray empty database file. Any
    error degrades to ``""``.
    """
    try:
        from core.memory import DB_PATH

        if not os.path.exists(DB_PATH):
            return ""
        from core.settings import get_system_setting

        return (get_system_setting(GGUF_MODEL_PATH_SETTING_KEY, "") or "").strip()
    except Exception as exc:  # never block construction on a settings read
        logger.debug("gguf: persisted path read failed: %s", exc)
        return ""


def resolve_gguf_model_path() -> str:
    """The GGUF model path Nova uses: persisted admin choice, else env.

    Safe to call from provider construction and from any thread: it
    performs **no** network I/O, never loads a model, and never raises. A
    blank persisted setting (the default for every existing install)
    transparently falls back to ``config.NOVA_GGUF_MODEL_PATH`` so Phase-1
    deployments behave exactly as before.
    """
    return _persisted_model_path() or _config_model_path()


# ── Path validation (the safety boundary) ────────────────────────────


def validate_gguf_model_path(raw, model_dir: str) -> str:
    """Validate a candidate model path, returning its resolved form.

    Enforces, in order: a non-empty clean string → no NUL/newlines → no
    ``~`` expansion → absolute → no ``..`` traversal → a ``.gguf``
    extension → resolves (symlinks included) inside ``model_dir`` → exists
    → is a regular file → is readable. Raises :class:`GgufModelPathError`
    with a short, safe message on the first failure; returns the fully
    resolved absolute path string on success.

    The containment check uses :meth:`pathlib.Path.resolve` on both the
    candidate and ``model_dir`` so a symlink that points outside the
    allowed directory is refused — not just literal ``..`` traversal.
    """
    if raw is None or isinstance(raw, bool):
        raise GgufModelPathError("A model path is required.")
    try:
        text = os.fspath(raw)
    except TypeError:
        raise GgufModelPathError("The model path must be a string.")
    if not isinstance(text, str):
        raise GgufModelPathError("The model path must be a string.")
    text = text.strip()
    if not text:
        raise GgufModelPathError("A model path is required.")
    if len(text) > MAX_PATH_LEN:
        raise GgufModelPathError("That model path is too long.")
    if "\x00" in text or "\n" in text or "\r" in text:
        raise GgufModelPathError("That model path contains invalid characters.")
    if "~" in text:
        # No home-directory expansion: keep the containment check honest.
        raise GgufModelPathError("The model path must be an absolute path.")

    candidate = Path(text)
    if not candidate.is_absolute():
        raise GgufModelPathError("The model path must be an absolute path.")
    if ".." in candidate.parts:
        raise GgufModelPathError("The model path must not contain '..'.")
    if candidate.suffix.lower() != ".gguf":
        raise GgufModelPathError("The model file must be a .gguf file.")

    allowed_raw = (model_dir or "").strip()
    if not allowed_raw:
        raise GgufModelPathError(
            "No model directory is configured. Set NOVA_MODEL_DIR."
        )
    try:
        allowed = Path(allowed_raw).resolve()
    except (OSError, RuntimeError, ValueError):
        raise GgufModelPathError(
            "The configured model directory could not be resolved."
        )
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        raise GgufModelPathError("That model path could not be resolved.")

    try:
        within = resolved == allowed or resolved.is_relative_to(allowed)
    except ValueError:
        within = False
    if not within:
        raise GgufModelPathError(
            "The model file must be inside the configured model directory."
        )

    try:
        if not resolved.exists():
            raise GgufModelPathError(
                "No file exists at that path inside the model directory."
            )
        if not resolved.is_file():
            raise GgufModelPathError("That path is not a file.")
    except OSError:
        raise GgufModelPathError("That model path could not be read.")
    if not os.access(str(resolved), os.R_OK):
        raise GgufModelPathError(
            "That model file is not readable. Check its permissions."
        )

    return str(resolved)


# ── Status (read-only) ───────────────────────────────────────────────


def _configured_provider() -> str:
    """The provider Nova is configured to use (``NOVA_MODEL_PROVIDER``)."""
    try:
        from config import MODEL_PROVIDER

        return (MODEL_PROVIDER or "ollama").strip().lower() or "ollama"
    except Exception as exc:  # pragma: no cover - config is stable
        logger.warning("gguf: provider lookup failed: %s", exc)
        return "ollama"


def _dir_state(model_dir: str) -> tuple[bool, bool]:
    """``(exists, is_dir)`` for ``model_dir``. Never raises."""
    try:
        p = Path(model_dir)
        return p.exists(), p.is_dir()
    except OSError:
        return False, False


def gguf_status() -> dict:
    """Calm, read-only snapshot of the local-GGUF configuration.

    Returns a JSON-serialisable dict the admin UI renders verbatim::

        {
          "provider": str,            # configured provider (NOVA_MODEL_PROVIDER)
          "is_llamacpp": bool,        # is the GGUF provider the active one?
          "default_provider": str,    # always "ollama"
          "model_dir": str,           # NOVA_MODEL_DIR
          "model_dir_exists": bool,
          "model_dir_is_dir": bool,
          "configured_path": str,     # resolved model path ("" if unset)
          "path_source": str,         # "custom" | "env" | "unset"
          "path_valid": bool,         # passes validation against model_dir
          "path_detail": str,         # sanitised reason when not valid
          "filename": str,            # basename of the configured path
        }

    Never raises and never loads a model. ``configured_path`` is the one
    operator-set value (admin-only endpoint); no other filesystem paths
    are surfaced.
    """
    provider = _configured_provider()
    model_dir = resolve_model_dir()
    exists, is_dir = _dir_state(model_dir)

    persisted = _persisted_model_path()
    env_path = _config_model_path()
    resolved = persisted or env_path
    source = "custom" if persisted else ("env" if env_path else "unset")

    path_valid = False
    path_detail = ""
    filename = ""
    if resolved:
        filename = os.path.basename(resolved)
        try:
            validate_gguf_model_path(resolved, model_dir)
            path_valid = True
        except GgufModelPathError as exc:
            path_detail = str(exc)

    return {
        "provider": provider,
        "is_llamacpp": provider == PROVIDER_NAME,
        "default_provider": "ollama",
        "model_dir": model_dir,
        "model_dir_exists": exists,
        "model_dir_is_dir": is_dir,
        "configured_path": resolved,
        "path_source": source,
        "path_valid": path_valid,
        "path_detail": path_detail,
        "filename": filename,
    }


# ── Write (validated) ────────────────────────────────────────────────


def _invalidate_llamacpp_cache() -> None:
    """Drop the cached ``llamacpp`` provider so the new path takes effect.

    Best-effort: a failure to evict only means the change waits for the
    next restart, so it is logged and swallowed rather than raised after a
    successful persist.
    """
    try:
        from core.model_providers import evict_provider
        from core.model_providers.llamacpp import reset_llamacpp_provider

        reset_llamacpp_provider()
        evict_provider(PROVIDER_NAME)
    except Exception as exc:  # never fail the write on a cache miss
        logger.debug("gguf: provider cache invalidation failed: %s", exc)


def set_gguf_model_path(path) -> dict:
    """Validate ``path`` against the model directory, then persist it.

    On success the resolved path is written to the global ``settings``
    table, the cached ``llamacpp`` provider is dropped so the change takes
    effect on the next generation, and the new :func:`gguf_status` is
    returned. Any validation failure raises :class:`GgufModelPathError`
    with a sanitised message and **writes nothing**.
    """
    model_dir = resolve_model_dir()
    validated = validate_gguf_model_path(path, model_dir)

    from core.settings import save_system_setting

    save_system_setting(GGUF_MODEL_PATH_SETTING_KEY, validated)
    _invalidate_llamacpp_cache()
    return gguf_status()


# ── Model library: read-only listing + select (Phase 3) ──────────────


def _iso_utc(timestamp: float) -> str:
    """Format a POSIX mtime as an ISO-8601 UTC string (second precision).

    Returns ``""`` for an unrepresentable timestamp rather than raising,
    so one odd file never breaks the whole listing.
    """
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (OverflowError, OSError, ValueError):
        return ""


def _selected_resolved_path(model_dir: str) -> str:
    """The resolved path of the currently-configured model, or ``""``.

    Resolves the configured GGUF path the same way a listed entry is
    resolved (through :func:`validate_gguf_model_path`) so the ``selected``
    flag is a like-for-like comparison of canonical paths. A missing /
    invalid configured path simply means "nothing selected" — never an
    error.
    """
    configured = resolve_gguf_model_path()
    if not configured:
        return ""
    try:
        return validate_gguf_model_path(configured, model_dir)
    except GgufModelPathError:
        return ""


def list_local_models() -> dict:
    """Read-only listing of local ``.gguf`` models inside ``NOVA_MODEL_DIR``.

    Returns a JSON-serialisable dict the admin UI renders verbatim::

        {
          "model_dir": str,          # NOVA_MODEL_DIR (the one allowed dir)
          "model_dir_exists": bool,
          "models": [
            {
              "name": str,           # file basename, e.g. "model.gguf"
              "relative_path": str,  # path relative to model_dir
              "size_bytes": int,
              "modified_at": str,    # ISO-8601 UTC, e.g. "2026-05-21T12:00:00Z"
              "selected": bool,      # is this the configured model?
            },
            ...
          ],
          "count": int,
          "truncated": bool,         # a bound was hit; not every file shown
          "warnings": [str],         # calm, non-sensitive notes
        }

    Safety (the whole point of this surface):

    * **Confined to the one allowed directory.** Only ``NOVA_MODEL_DIR``
      is scanned — never the wider filesystem, never a caller-supplied
      path. The directory itself is the only absolute path returned;
      every model is reported by *relative* path + basename so no
      unrelated filesystem layout leaks.
    * **Bounded recursion.** The walk descends at most
      :data:`MAX_SCAN_DEPTH` levels, visits at most
      :data:`MAX_DIRS_SCANNED` directories, and returns at most
      :data:`MAX_LIBRARY_MODELS` files; hitting any bound sets
      ``truncated`` and stops — it can never become a filesystem-wide
      scan.
    * **No hidden/system entries, no symlink escape.** Dot directories
      and dot files are skipped, symlinked directories are never
      descended (``followlinks=False``), and every candidate file is
      re-validated with :func:`validate_gguf_model_path` so a symlinked
      file whose target escapes the model directory (or is unreadable, or
      is not a regular ``.gguf`` file) is silently omitted — the listed
      set is exactly the selectable set.
    * **Read-only.** Nothing is created, downloaded, moved, deleted, or
      overwritten and no shell is invoked. Never raises — any problem
      degrades to an empty list with a sanitised warning.
    """
    model_dir = resolve_model_dir()
    warnings: list[str] = []

    def _result(exists: bool, models: list[dict], truncated: bool) -> dict:
        return {
            "model_dir": model_dir,
            "model_dir_exists": exists,
            "models": models,
            "count": len(models),
            "truncated": truncated,
            "warnings": warnings,
        }

    raw = (model_dir or "").strip()
    if not raw:
        warnings.append("No model directory is configured. Set NOVA_MODEL_DIR.")
        return _result(False, [], False)

    try:
        base = Path(raw).resolve()
    except (OSError, RuntimeError, ValueError):
        warnings.append("The configured model directory could not be resolved.")
        return _result(False, [], False)

    try:
        if not base.exists():
            warnings.append(
                "The model directory does not exist yet. Create it (or set "
                "NOVA_MODEL_DIR) and place your .gguf files inside it."
            )
            return _result(False, [], False)
        if not base.is_dir():
            warnings.append("The configured model directory is not a directory.")
            return _result(True, [], False)
    except OSError:
        warnings.append("The model directory could not be read.")
        return _result(False, [], False)

    selected = _selected_resolved_path(model_dir)
    base_str = str(base)

    models: list[dict] = []
    truncated = False
    dirs_scanned = 0

    for dirpath, dirnames, filenames in os.walk(base_str, followlinks=False):
        dirs_scanned += 1
        if dirs_scanned > MAX_DIRS_SCANNED:
            truncated = True
            break

        rel_dir = os.path.relpath(dirpath, base_str)
        depth = 0 if rel_dir in (".", "") else len(rel_dir.split(os.sep))
        # Prune hidden/system dirs in-place and stop descending past the
        # depth cap. Sorting makes truncation deterministic.
        if depth >= MAX_SCAN_DEPTH:
            dirnames[:] = []
        else:
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))

        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            if not filename.lower().endswith(".gguf"):
                continue
            abs_path = os.path.join(dirpath, filename)
            try:
                resolved = validate_gguf_model_path(abs_path, model_dir)
            except GgufModelPathError:
                # Symlink escaping the dir, unreadable, not a regular file,
                # etc. — not selectable, so deliberately not listed.
                continue
            try:
                st = os.stat(resolved)
            except OSError:
                continue
            try:
                rel = str(Path(resolved).relative_to(base))
            except ValueError:
                rel = os.path.basename(resolved)
            models.append(
                {
                    "name": os.path.basename(resolved),
                    "relative_path": rel,
                    "size_bytes": int(st.st_size),
                    "modified_at": _iso_utc(st.st_mtime),
                    "selected": bool(selected) and resolved == selected,
                }
            )
            if len(models) >= MAX_LIBRARY_MODELS:
                truncated = True
                break
        if truncated:
            break

    models.sort(key=lambda m: m["relative_path"].lower())

    if truncated:
        warnings.append(
            "The model directory has more files than can be listed at once; "
            "only the first results are shown."
        )
    elif not models:
        warnings.append("No .gguf model files were found in the model directory.")

    return _result(True, models, truncated)


def select_local_model(relative_path) -> dict:
    """Select a listed local model (by its ``relative_path``) as the GGUF model.

    Resolves ``relative_path`` against ``NOVA_MODEL_DIR`` and hands the
    joined path to :func:`set_gguf_model_path`, which performs the full
    Phase-2 validation (``.gguf`` extension, containment inside the model
    directory with symlinks resolved, existing readable regular file, no
    ``..``) before persisting it as the single host-wide setting and
    invalidating the cached provider. Returns the new :func:`gguf_status`.

    ``relative_path`` must be a clean *relative* path (exactly as returned
    by :func:`list_local_models`): an absolute path, a ``..`` segment, a
    ``~``, NUL/newline bytes, or an over-long value is refused up front
    with a sanitised :class:`GgufModelPathError` and **nothing is written**
    — the containment guarantee never depends on the caller being honest.
    """
    if relative_path is None or isinstance(relative_path, bool):
        raise GgufModelPathError("A model is required.")
    try:
        text = os.fspath(relative_path)
    except TypeError:
        raise GgufModelPathError("The model selection must be a string.")
    if not isinstance(text, str):
        raise GgufModelPathError("The model selection must be a string.")
    text = text.strip()
    if not text:
        raise GgufModelPathError("A model is required.")
    if len(text) > MAX_PATH_LEN:
        raise GgufModelPathError("That model selection is too long.")
    if "\x00" in text or "\n" in text or "\r" in text:
        raise GgufModelPathError(
            "That model selection contains invalid characters."
        )
    if "~" in text:
        raise GgufModelPathError("Choose a model from the listed local models.")

    candidate = Path(text)
    if candidate.is_absolute():
        raise GgufModelPathError(
            "Choose a model from the listed local models (use its relative "
            "path), not an absolute path."
        )
    if ".." in candidate.parts:
        raise GgufModelPathError("The model path must not contain '..'.")

    model_dir = resolve_model_dir()
    raw_dir = (model_dir or "").strip()
    if not raw_dir:
        raise GgufModelPathError(
            "No model directory is configured. Set NOVA_MODEL_DIR."
        )

    # Join, then re-validate-and-persist through the Phase-2 boundary:
    # set_gguf_model_path resolves symlinks and re-checks containment, so
    # selection inherits exactly the same safety guarantees as a pasted
    # path — there is no second, weaker code path.
    full = os.path.join(raw_dir, text)
    return set_gguf_model_path(full)


# ── Test / health (read-only, never loads the model) ─────────────────


def test_gguf_provider() -> dict:
    """Check the configured GGUF model is *valid enough to attempt loading*.

    Read-only and cheap: it confirms a path is configured, that it passes
    the directory-confined validation (exists, readable, ``.gguf``, inside
    ``NOVA_MODEL_DIR``), and that the ``llama-cpp-python`` backend is
    importable — but it never loads the (potentially multi-GB) weights.
    Always returns a stable, JSON-serialisable shape and never raises::

        {"ok": bool, "provider": "llamacpp", "detail": str,
         "filename": str, "path_valid": bool}

    ``ok`` is ``True`` only when the path is valid *and* the backend is
    installed; otherwise ``detail`` carries a short, sanitised reason.
    """
    model_dir = resolve_model_dir()
    resolved = resolve_gguf_model_path()
    if not resolved:
        return {
            "ok": False,
            "provider": PROVIDER_NAME,
            "detail": _NO_PATH_MSG,
            "filename": "",
            "path_valid": False,
        }

    try:
        validated = validate_gguf_model_path(resolved, model_dir)
    except GgufModelPathError as exc:
        return {
            "ok": False,
            "provider": PROVIDER_NAME,
            "detail": str(exc),
            "filename": os.path.basename(resolved),
            "path_valid": False,
        }

    # Cheap, read-only probe: the provider's own health() checks the
    # dependency and re-validates the path as a readable .gguf — it never
    # loads the model. Construct a fresh provider against the validated
    # path so the probe reflects exactly what was configured.
    try:
        from core.model_providers.llamacpp import LlamaCppProvider

        health = LlamaCppProvider(model_path=validated).health()
        ok = bool(health.ok)
        detail = health.detail or ""
    except Exception as exc:  # never raise into the endpoint
        logger.warning("gguf test: provider probe failed: %s", exc)
        return {
            "ok": False,
            "provider": PROVIDER_NAME,
            "detail": "The GGUF provider could not be probed.",
            "filename": os.path.basename(validated),
            "path_valid": True,
        }

    if ok and not detail:
        detail = (
            "The GGUF model path is valid and llama-cpp-python is "
            "installed. Nova will load the model on first use."
        )
    return {
        "ok": ok,
        "provider": PROVIDER_NAME,
        "detail": detail,
        "filename": os.path.basename(validated),
        "path_valid": True,
    }


__all__ = [
    "GGUF_MODEL_PATH_SETTING_KEY",
    "MAX_PATH_LEN",
    "PROVIDER_NAME",
    "MAX_SCAN_DEPTH",
    "MAX_DIRS_SCANNED",
    "MAX_LIBRARY_MODELS",
    "GgufModelPathError",
    "resolve_model_dir",
    "resolve_gguf_model_path",
    "validate_gguf_model_path",
    "gguf_status",
    "set_gguf_model_path",
    "list_local_models",
    "select_local_model",
    "test_gguf_provider",
]
