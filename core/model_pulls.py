"""
Admin-only Ollama model pull (issue #111).

This module owns the *pulling* side of the local model registry:

  * `model_pulls` table — one row per pull attempt, tracking status,
    progress, and timestamps.
  * `validate_model_name` — strict allowlist that rejects whitespace,
    shell metacharacters, path traversal, and oversized inputs *before*
    any Ollama call.
  * `request_pull` — admin-driven entry point. It validates the name,
    skips the work when the model is already installed, returns the
    in-progress job when one already exists for the same model, and
    otherwise inserts a new row + dispatches a background worker.
  * `_run_pull` — the worker. It uses the Ollama Python client (no
    subprocess, no shell) to stream progress, persists snapshots, and
    flips `model_registry.installed = 1` on success.

Out of scope (see the issue body for the full list):
  * Per-user / per-role model access control (#112).
  * Frontend admin model-management UI (a tiny status surface only).
  * Auto-switching the chat router to a freshly pulled model.
  * Model deletion or removal.

Concurrency model:
  * A module-level `threading.Lock` guards the in-memory active-set,
    which prevents two concurrent pulls of the *same* model.
  * `_MAX_CONCURRENT_PULLS` caps the total number of in-flight pull
    workers so a flurry of admin clicks cannot saturate the host.
  * The DB row is the source of truth; the in-memory set is a fast
    short-circuit for the common case.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

import httpx
import ollama

from core.ollama_client import client

logger = logging.getLogger(__name__)


# ── Status constants ────────────────────────────────────────────────────────

STATUS_QUEUED = "queued"
STATUS_PULLING = "pulling"
STATUS_DONE = "done"
STATUS_ERROR = "error"

_ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_PULLING)
_TERMINAL_STATUSES = (STATUS_DONE, STATUS_ERROR)

# Hard upper bound on the number of pull workers that may be running at
# the same time. Pulls are large downloads — a single one is usually the
# right answer, but we leave a small head-room so an admin can queue one
# more without waiting for the first to finish.
_MAX_CONCURRENT_PULLS = 2


# ── Resource-warning thresholds (issue #126) ────────────────────────────────
#
# These thresholds drive *informational* warnings only — they never block a
# pull. The admin remains in charge of the decision; we just surface the
# resource impact so they can make it with eyes open.
#
# `_LARGE_MODEL_BYTES` is the soft "this is a big download" line. It is
# deliberately conservative so a routine 7B model doesn't trigger it but a
# 27B / 70B does.
_LARGE_MODEL_BYTES = 8 * 1024 ** 3  # 8 GiB


# ── Validation ──────────────────────────────────────────────────────────────

# Ollama model names look like `[host/][namespace/]name[:tag]`. A safe
# allowlist is more than enough — refusing anything outside this charset
# blocks every form of shell injection up-front, regardless of how the
# name is passed downstream.
#
# Components ::= [A-Za-z0-9] [A-Za-z0-9._-]{0,62}
# Names      ::= Components ("/" Components){0,3}
# Tag        ::= ":" Components
_NAME_PART = r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}"
_MODEL_NAME_RE = re.compile(
    rf"^{_NAME_PART}(?:/{_NAME_PART}){{0,3}}(?::{_NAME_PART})?$"
)
_MAX_MODEL_NAME_LENGTH = 200


class InvalidModelName(ValueError):
    """Raised when a caller-supplied model name fails the strict allowlist."""


class PullAlreadyInProgress(Exception):
    """Raised when a pull for the same model is already queued or running."""

    def __init__(self, job: dict):
        super().__init__(f"pull already in progress for {job['model_name']!r}")
        self.job = job


class ModelAlreadyInstalled(Exception):
    """Raised when the registry already reports the model as installed."""

    def __init__(self, model_name: str):
        super().__init__(f"model {model_name!r} is already installed")
        self.model_name = model_name


class TooManyPullsInProgress(Exception):
    """Raised when the global concurrent-pull cap has been reached."""

    def __init__(self, in_flight: int, cap: int):
        super().__init__(
            f"already {in_flight} pulls in progress (cap={cap})"
        )
        self.in_flight = in_flight
        self.cap = cap


def validate_model_name(name: object) -> str:
    """
    Return the canonicalised model name, or raise `InvalidModelName`.

    The check is intentionally strict — anything outside the allowlist
    is rejected, including leading whitespace, shell metacharacters
    (`;`, `|`, `&`, backticks, `$`, `(`, `)`), path-traversal segments
    (`.`, `..`), embedded NUL bytes, and names exceeding 200 chars.
    Validation runs *before* any Ollama call so a malicious input never
    reaches a network or process boundary.
    """
    if not isinstance(name, str):
        raise InvalidModelName("model name must be a string")
    stripped = name.strip()
    if stripped != name:
        raise InvalidModelName("model name must not contain leading/trailing whitespace")
    if not stripped:
        raise InvalidModelName("model name must not be empty")
    if len(stripped) > _MAX_MODEL_NAME_LENGTH:
        raise InvalidModelName(
            f"model name must be at most {_MAX_MODEL_NAME_LENGTH} characters"
        )
    if "\x00" in stripped:
        raise InvalidModelName("model name must not contain NUL bytes")
    # Reject path-traversal segments anywhere in the name. The regex below
    # would also reject these, but checking explicitly gives a clearer error.
    for segment in stripped.split("/"):
        if segment in ("", ".", ".."):
            raise InvalidModelName("model name must not contain path traversal")
    if not _MODEL_NAME_RE.match(stripped):
        raise InvalidModelName("model name contains disallowed characters")
    return stripped


# ── Resource warnings (issue #126) ──────────────────────────────────────────
#
# Warnings are informational metadata returned alongside a pull request so
# the admin sees disk / RAM / slowdown impact before (or with) the pull
# response. Nothing here blocks a pull — large and unknown-size models are
# both allowed. The model-size estimate is best-effort: if Ollama can give
# us a number we surface it; otherwise we mark the pull as "unknown size"
# and the admin proceeds anyway.
#
# Warning shape:
#   {"code": "<stable id>", "level": "info"|"warning", "message": "..."}
#
# Codes are stable so the UI (or a future CLI) can localise / restyle.
WARNING_DISK_USAGE = "disk_usage"
WARNING_RAM_VRAM_IMPACT = "ram_vram_impact"
WARNING_POSSIBLE_SLOWDOWN = "possible_slowdown"
WARNING_UNKNOWN_SIZE = "unknown_size"
WARNING_LARGE_MODEL = "large_model"


def _coerce_size(value: object) -> Optional[int]:
    """Return value as a positive int, or None if it is not a usable size."""
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    n = int(value)
    return n if n > 0 else None


def estimate_model_size(model_name: str) -> Optional[int]:
    """
    Best-effort pre-pull size estimate, in bytes.

    Uses `client.show()` to read whatever size metadata Ollama exposes for
    the model. Most uninstalled models will return *no* size info — the
    Ollama API does not publish a manifest size for un-pulled tags — and
    in that case this function returns None and the caller falls back to
    the "unknown size" warning. A None return is the expected common case
    and never an error.

    The function never raises: any Ollama / network failure becomes None.
    """
    try:
        payload = client.show(model_name)
    except (ollama.ResponseError, httpx.HTTPError, ConnectionError, OSError):
        return None
    except Exception:  # noqa: BLE001 — third-party clients vary; never let this raise
        logger.debug("estimate_model_size: unexpected error for %s", model_name, exc_info=True)
        return None

    if payload is None:
        return None
    # Normalise the payload — the ollama python client may return either a
    # dict (older builds) or a typed object with `.model_dump()` (newer).
    if hasattr(payload, "model_dump"):
        try:
            payload = payload.model_dump()
        except Exception:  # noqa: BLE001
            payload = None
    if not isinstance(payload, dict):
        return None

    # Try the most likely shapes first. Different Ollama versions surface
    # the figure in different places; check them all and take the first
    # positive integer.
    for key in ("size", "total_size"):
        size = _coerce_size(payload.get(key))
        if size is not None:
            return size
    details = payload.get("details")
    if isinstance(details, dict):
        for key in ("size", "parameter_size_bytes"):
            size = _coerce_size(details.get(key))
            if size is not None:
                return size
    return None


def _format_gib(num_bytes: int) -> str:
    return f"{num_bytes / (1024 ** 3):.1f} GiB"


def build_pull_warnings(
    model_name: str,
    estimated_bytes: Optional[int] = None,
) -> dict:
    """
    Build the resource-warning payload for `model_name`.

    The output is informational metadata only — see module docstring. The
    caller may pass an explicit `estimated_bytes` (e.g. from a previous
    `estimate_model_size` call) to avoid re-querying Ollama.

    Returns:
        {
          "model": <name>,
          "estimated_size_bytes": int | None,
          "unknown_size": bool,
          "is_large": bool,
          "warnings": [ {"code": str, "level": str, "message": str}, ... ],
        }
    """
    size = _coerce_size(estimated_bytes)
    unknown = size is None
    is_large = size is not None and size >= _LARGE_MODEL_BYTES

    warnings: list[dict] = []
    if unknown:
        warnings.append({
            "code": WARNING_UNKNOWN_SIZE,
            "level": "warning",
            "message": (
                "Ollama did not report a size for this model. The download "
                "may be larger than expected — proceed only if you have "
                "ample free disk space."
            ),
        })
    if is_large:
        warnings.append({
            "code": WARNING_LARGE_MODEL,
            "level": "warning",
            "message": (
                f"Estimated download size is {_format_gib(size)}. Large "
                "models take significantly longer to pull and consume more "
                "disk space."
            ),
        })
    warnings.append({
        "code": WARNING_DISK_USAGE,
        "level": "info",
        "message": (
            "Pulled models are stored on disk under the Ollama data "
            "directory. Make sure you have enough free space before "
            "starting the pull."
        ),
    })
    warnings.append({
        "code": WARNING_RAM_VRAM_IMPACT,
        "level": "info",
        "message": (
            "Running this model will load weights into RAM (and VRAM if a "
            "GPU is used). Larger models may exceed the resources "
            "available on this host."
        ),
    })
    warnings.append({
        "code": WARNING_POSSIBLE_SLOWDOWN,
        "level": "info",
        "message": (
            "While the pull is running other Nova requests may slow down "
            "due to network and disk contention."
        ),
    })

    return {
        "model": model_name,
        "estimated_size_bytes": size,
        "unknown_size": unknown,
        "is_large": is_large,
        "warnings": warnings,
    }


def preview_pull(model_name: str) -> dict:
    """
    Validate `model_name` and return the resource-warning payload only.

    Lets an admin client fetch warnings ahead of (or independently from)
    triggering a pull. Never starts a download; never inserts a row.
    """
    canonical = validate_model_name(model_name)
    return build_pull_warnings(canonical, estimate_model_size(canonical))


# ── Schema ──────────────────────────────────────────────────────────────────

_MODEL_PULLS_SQL = """
CREATE TABLE IF NOT EXISTS model_pulls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name      TEXT    NOT NULL,
    status          TEXT    NOT NULL CHECK (status IN
                        ('queued', 'pulling', 'done', 'error')),
    total_bytes     INTEGER,
    completed_bytes INTEGER,
    error_message   TEXT,
    started_at      TEXT,
    finished_at     TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
)
"""

_MODEL_PULLS_NAME_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_model_pulls_model_name "
    "ON model_pulls(model_name)"
)

_MODEL_PULLS_STATUS_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_model_pulls_status "
    "ON model_pulls(status)"
)


def migrate(db_path: str) -> None:
    """Create the `model_pulls` table and its indexes. Idempotent."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(_MODEL_PULLS_SQL)
        conn.execute(_MODEL_PULLS_NAME_INDEX_SQL)
        conn.execute(_MODEL_PULLS_STATUS_INDEX_SQL)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _open(db_path: Optional[str] = None) -> sqlite3.Connection:
    if db_path is None:
        from core.memory import DB_PATH
        db_path = DB_PATH
    return sqlite3.connect(db_path)


def _row_to_dict(row: sqlite3.Row) -> dict:
    completed = row["completed_bytes"]
    total = row["total_bytes"]
    progress: Optional[float] = None
    if total and total > 0 and completed is not None:
        # Clamp to [0.0, 1.0] in case the upstream reports a slightly
        # over-estimated `total` mid-stream.
        progress = max(0.0, min(1.0, completed / total))
    return {
        "id": int(row["id"]),
        "model_name": row["model_name"],
        "status": row["status"],
        "total_bytes": int(total) if total is not None else None,
        "completed_bytes": int(completed) if completed is not None else None,
        "progress": progress,
        "error_message": row["error_message"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _fetch_one(conn: sqlite3.Connection, sql: str, params: tuple) -> Optional[dict]:
    prev = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(sql, params).fetchone()
    finally:
        conn.row_factory = prev
    return _row_to_dict(row) if row else None


def _fetch_all(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    prev = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.row_factory = prev
    return [_row_to_dict(r) for r in rows]


# ── In-memory active-set (thread-safe) ──────────────────────────────────────

_state_lock = threading.Lock()
_active_models: set[str] = set()


def _try_reserve(model_name: str) -> bool:
    """
    Atomically reserve a slot for `model_name`. Returns True if reserved,
    False if the model is already in flight or the global cap is reached.
    """
    with _state_lock:
        if model_name in _active_models:
            return False
        if len(_active_models) >= _MAX_CONCURRENT_PULLS:
            return False
        _active_models.add(model_name)
        return True


def _release(model_name: str) -> None:
    with _state_lock:
        _active_models.discard(model_name)


def _is_active(model_name: str) -> bool:
    with _state_lock:
        return model_name in _active_models


# ── Reads ───────────────────────────────────────────────────────────────────

def list_pulls(db_path: Optional[str] = None, limit: int = 100) -> list[dict]:
    """Return pull jobs ordered most-recent first."""
    with _open(db_path) as conn:
        return _fetch_all(
            conn,
            "SELECT * FROM model_pulls ORDER BY id DESC LIMIT ?",
            (int(limit),),
        )


def get_pull(pull_id: int, db_path: Optional[str] = None) -> Optional[dict]:
    with _open(db_path) as conn:
        return _fetch_one(
            conn, "SELECT * FROM model_pulls WHERE id = ?", (int(pull_id),)
        )


def _get_active_pull_for(
    conn: sqlite3.Connection, model_name: str
) -> Optional[dict]:
    return _fetch_one(
        conn,
        "SELECT * FROM model_pulls WHERE model_name = ? "
        "AND status IN ('queued', 'pulling') ORDER BY id DESC LIMIT 1",
        (model_name,),
    )


def _model_is_installed(conn: sqlite3.Connection, model_name: str) -> bool:
    row = conn.execute(
        "SELECT installed FROM model_registry WHERE model_name = ?",
        (model_name,),
    ).fetchone()
    return bool(row[0]) if row else False


# ── Public entry point: request_pull ────────────────────────────────────────

def request_pull(
    model_name: str,
    db_path: Optional[str] = None,
    *,
    runner: Optional[Callable[[int, str, str], None]] = None,
) -> dict:
    """
    Validate the input, decide whether work is needed, and start the
    background worker. Returns the job row dict that the caller should
    surface to the user.

    Raises:
        InvalidModelName       — name fails the allowlist.
        ModelAlreadyInstalled  — registry already reports installed.
        PullAlreadyInProgress  — same model has a queued/pulling row;
                                 the existing job is attached.

    `runner` is the function used to invoke the worker. It defaults to
    `_dispatch_in_thread`, which runs `_run_pull` in a daemon thread.
    Tests inject a synchronous runner so the worker executes inline.
    """
    canonical = validate_model_name(model_name)

    if db_path is None:
        from core.memory import DB_PATH
        db_path = DB_PATH

    with _open(db_path) as conn:
        if _model_is_installed(conn, canonical):
            raise ModelAlreadyInstalled(canonical)

        existing = _get_active_pull_for(conn, canonical)
        if existing is not None:
            raise PullAlreadyInProgress(existing)

        # Reserve a worker slot before inserting the row. If the cap is
        # reached the request is rejected outright so the admin gets a
        # clear "too many pulls" signal instead of a row that sits in
        # `queued` indefinitely with nothing servicing it.
        if not _try_reserve(canonical):
            with _state_lock:
                in_flight = len(_active_models)
            raise TooManyPullsInProgress(in_flight, _MAX_CONCURRENT_PULLS)

        now = _now_iso()
        cur = conn.execute(
            "INSERT INTO model_pulls "
            "(model_name, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (canonical, STATUS_QUEUED, now, now),
        )
        pull_id = int(cur.lastrowid)
        job = _fetch_one(
            conn, "SELECT * FROM model_pulls WHERE id = ?", (pull_id,)
        )

    dispatch = runner or _dispatch_in_thread
    try:
        dispatch(pull_id, canonical, db_path)
    except Exception:
        # If the worker could not even be scheduled, release the slot
        # and mark the row as errored so the admin sees a failure
        # rather than a forever-queued job.
        _release(canonical)
        _mark_error(db_path, pull_id, "failed to start pull worker")
        raise

    # Attach #126 resource warnings to the returned job so the admin
    # client receives disk/RAM/slowdown guidance with the pull response.
    # The warnings are not persisted — they are derived metadata and the
    # `model_pulls` row stays unchanged.
    warnings = build_pull_warnings(canonical, estimate_model_size(canonical))
    job["warnings"] = warnings["warnings"]
    job["estimated_size_bytes"] = warnings["estimated_size_bytes"]
    job["unknown_size"] = warnings["unknown_size"]
    job["is_large"] = warnings["is_large"]

    return job  # type: ignore[return-value]


# ── Worker dispatch and execution ───────────────────────────────────────────

def _dispatch_in_thread(pull_id: int, model_name: str, db_path: str) -> None:
    """Default runner — start `_run_pull` on a daemon thread."""
    t = threading.Thread(
        target=_run_pull,
        args=(pull_id, model_name, db_path),
        name=f"ollama-pull-{pull_id}",
        daemon=True,
    )
    t.start()


def _mark_error(db_path: str, pull_id: int, message: str) -> None:
    now = _now_iso()
    with _open(db_path) as conn:
        conn.execute(
            "UPDATE model_pulls SET status = ?, error_message = ?, "
            "finished_at = COALESCE(finished_at, ?), updated_at = ? "
            "WHERE id = ?",
            (STATUS_ERROR, message, now, now, pull_id),
        )


def _safe_error_message(exc: BaseException) -> str:
    """
    Map a raw exception to a short, deterministic string suitable for
    surfacing to an admin client. Avoids echoing full tracebacks, hostnames,
    file paths, or anything that could leak environment data.
    """
    cls = type(exc).__name__
    if isinstance(exc, ollama.ResponseError):
        return f"ollama refused the pull ({cls})"
    if isinstance(exc, httpx.HTTPError):
        return f"network error talking to ollama ({cls})"
    if isinstance(exc, (ConnectionError, OSError)):
        return f"could not reach ollama ({cls})"
    return f"pull failed ({cls})"


def _update_progress(
    db_path: str,
    pull_id: int,
    *,
    completed: Optional[int] = None,
    total: Optional[int] = None,
) -> None:
    now = _now_iso()
    sets = ["updated_at = ?"]
    params: list[object] = [now]
    if completed is not None:
        sets.append("completed_bytes = ?")
        params.append(int(completed))
    if total is not None:
        sets.append("total_bytes = ?")
        params.append(int(total))
    params.append(pull_id)
    with _open(db_path) as conn:
        conn.execute(
            f"UPDATE model_pulls SET {', '.join(sets)} WHERE id = ?",
            params,
        )


def _consume_pull_stream(stream: Iterable, db_path: str, pull_id: int) -> None:
    """
    Walk the generator returned by `client.pull(stream=True)` and persist
    progress snapshots. Each event is a dict with a `status` field and,
    for download events, `completed` / `total` byte counters.
    """
    last_completed: Optional[int] = None
    last_total: Optional[int] = None
    for event in stream:
        if not isinstance(event, dict):
            continue
        completed = event.get("completed")
        total = event.get("total")
        # Only write to the DB when something actually changed, to avoid
        # hammering SQLite during the per-chunk progress callbacks.
        if (
            completed is not None and completed != last_completed
        ) or (
            total is not None and total != last_total
        ):
            _update_progress(
                db_path, pull_id, completed=completed, total=total
            )
            if completed is not None:
                last_completed = completed
            if total is not None:
                last_total = total


def _mark_started(db_path: str, pull_id: int) -> None:
    now = _now_iso()
    with _open(db_path) as conn:
        conn.execute(
            "UPDATE model_pulls SET status = ?, "
            "started_at = COALESCE(started_at, ?), updated_at = ? "
            "WHERE id = ?",
            (STATUS_PULLING, now, now, pull_id),
        )


def _mark_done(db_path: str, pull_id: int, model_name: str) -> None:
    now = _now_iso()
    with _open(db_path) as conn:
        conn.execute(
            "UPDATE model_pulls SET status = ?, finished_at = ?, "
            "updated_at = ? WHERE id = ?",
            (STATUS_DONE, now, now, pull_id),
        )
        # Reflect the successful pull in the registry so the admin
        # /admin/models view immediately shows installed=True without
        # requiring a reconcile pass.
        conn.execute(
            "UPDATE model_registry SET installed = 1, updated_at = ? "
            "WHERE model_name = ?",
            (now, model_name),
        )


def _run_pull(pull_id: int, model_name: str, db_path: str) -> None:
    """
    Background worker. Calls `client.pull(model_name, stream=True)` —
    the Ollama Python client speaks HTTP to the local daemon, no
    subprocess, no shell. Updates the DB row as the stream progresses,
    flips the registry on success, and persists a safe error string on
    failure.
    """
    try:
        _mark_started(db_path, pull_id)
        try:
            stream = client.pull(model_name, stream=True)
        except TypeError:
            # Older client versions may not accept the `stream` kwarg —
            # fall back to a single-shot call so the worker still makes
            # progress even on a stripped-down Ollama install.
            stream = client.pull(model_name)
            stream = [stream] if stream is not None else []
        _consume_pull_stream(stream, db_path, pull_id)
        _mark_done(db_path, pull_id, model_name)
        logger.info("Ollama pull completed for %s", model_name)
    except (
        ollama.ResponseError,
        httpx.HTTPError,
        ConnectionError,
        OSError,
    ) as exc:
        logger.warning(
            "Ollama pull failed for %s: %s", model_name, type(exc).__name__
        )
        _mark_error(db_path, pull_id, _safe_error_message(exc))
    except Exception as exc:  # noqa: BLE001
        # Last-resort guard: a programmer error inside the worker must
        # not leave a job stuck in `pulling` forever, but we also must
        # not echo the raw exception text back to the admin.
        logger.exception("Unexpected error during Ollama pull for %s", model_name)
        _mark_error(db_path, pull_id, _safe_error_message(exc))
    finally:
        _release(model_name)
