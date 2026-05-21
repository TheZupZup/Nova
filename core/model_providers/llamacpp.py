"""Direct local GGUF model provider (llama.cpp via ``llama-cpp-python``).

This is the first provider that lets Nova generate text **without
Ollama** — it loads a single local ``.gguf`` model file through
``llama-cpp-python`` and serves it behind the same
:class:`~core.model_providers.base.ModelProvider` contract every other
backend implements. Ollama remains Nova's default; this is an opt-in
alternative selected with ``NOVA_MODEL_PROVIDER=llamacpp``.

Design rules baked in here (all flow from Nova's safety/trust contract):

  * **Optional dependency, graceful absence.** ``llama_cpp`` is imported
    lazily inside the methods that need it — never at module import — so
    the registry can import this file on a host that does not have the
    wheel installed. When the dependency is missing, :meth:`health`
    reports ``ok=False`` with a clear, fixed message and
    :meth:`generate` / :meth:`stream` raise :class:`ModelProviderError`.
  * **No downloads, ever.** Nova never fetches a model. The operator
    points ``NOVA_GGUF_MODEL_PATH`` at a file they already have.
  * **Safe path validation.** The configured path is accepted only if it
    is a readable regular ``.gguf`` file. No globbing, no directory walk,
    no filesystem scan, no shell.
  * **Cheap construction, lazy load.** Constructing the provider only
    validates config and the path; the (potentially multi-GB) model is
    loaded on first use and cached. This honours the base contract that
    providers are "safe to construct cheaply and repeatedly" and that
    construction performs no heavy I/O.
  * **Sanitised errors.** User/operator-facing messages name the
    relevant env var and the problem; they never echo a raw backend
    exception or the absolute model path. The full detail is logged
    server-side only.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Iterator, Optional

from .base import (
    ModelChunk,
    ModelProvider,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ProviderHealth,
)

logger = logging.getLogger(__name__)

# Fixed, non-sensitive operator-facing messages. Kept as constants so the
# same wording is reported by ``health()`` and raised by generation, and
# so tests can pin the contract without matching a backend's raw error.
_DEP_MISSING_MSG = (
    "llama-cpp-python is not installed. Install it to use the local GGUF "
    "provider, e.g. `pip install llama-cpp-python`."
)
_NO_MODEL_MSG = (
    "No GGUF model configured. Set NOVA_GGUF_MODEL_PATH to a local .gguf "
    "model file."
)
_LOAD_FAILED_MSG = (
    "Failed to load the GGUF model. Check NOVA_GGUF_MODEL_PATH and that the "
    "host has enough memory for this model."
)
_GENERATE_FAILED_MSG = "GGUF model generation failed."
_STREAM_FAILED_MSG = "GGUF model streaming failed."

# Sensible context window when the operator does not set one. llama.cpp's
# own default (512) is too small for Nova's assembled prompts.
_DEFAULT_CONTEXT_SIZE = 4096


def _validate_model_path(configured: str) -> tuple[Optional[str], str]:
    """Resolve ``configured`` to a usable model path, or explain why not.

    Returns ``(path, "")`` when the value points at a readable regular
    ``.gguf`` file, else ``(None, reason)`` with a short, non-sensitive
    reason. An empty ``configured`` returns ``(None, "")`` — "not
    configured" is a distinct, caller-handled state, not a validation
    failure. Best-effort and never raises: a path that trips an ``OSError``
    on ``stat`` is treated as unusable, not propagated.
    """
    value = (configured or "").strip()
    if not value:
        return None, ""
    try:
        path = Path(value)
        if path.suffix.lower() != ".gguf":
            return None, (
                "NOVA_GGUF_MODEL_PATH must point at a .gguf model file."
            )
        if not path.is_file():
            return None, (
                "GGUF model file not found. Check NOVA_GGUF_MODEL_PATH."
            )
        if not os.access(value, os.R_OK):
            return None, (
                "GGUF model file is not readable. Check its permissions."
            )
        return str(path), ""
    except OSError:
        return None, "GGUF model path could not be read."


class LlamaCppProvider(ModelProvider):
    """Run a local ``.gguf`` model directly via ``llama-cpp-python``.

    Constructed cheaply: it reads config (or explicit overrides) and
    validates the model path, but does **not** import ``llama_cpp`` or
    load the model until the first :meth:`generate` / :meth:`stream`.
    Access to the loaded model is serialised with a lock because a single
    ``llama_cpp.Llama`` handle is not safe to call concurrently.
    """

    name = "llamacpp"

    def __init__(
        self,
        model_path: Optional[str] = None,
        context_size: Optional[int] = None,
        threads: Optional[int] = None,
        gpu_layers: Optional[int] = None,
        llama_class: Optional[type] = None,
    ) -> None:
        # Defer config import so this module has no import-time config
        # dependency and tests can monkeypatch config before constructing.
        if (
            model_path is None
            or context_size is None
            or threads is None
            or gpu_layers is None
        ):
            from config import (
                NOVA_GGUF_CONTEXT_SIZE,
                NOVA_GGUF_GPU_LAYERS,
                NOVA_GGUF_MODEL_PATH,
                NOVA_GGUF_THREADS,
            )

            if model_path is None:
                model_path = NOVA_GGUF_MODEL_PATH
            if context_size is None:
                context_size = NOVA_GGUF_CONTEXT_SIZE
            if threads is None:
                threads = NOVA_GGUF_THREADS
            if gpu_layers is None:
                gpu_layers = NOVA_GGUF_GPU_LAYERS

        self._configured_path = (model_path or "").strip()
        self._model_path, self._path_problem = _validate_model_path(
            self._configured_path
        )

        ctx = int(context_size) if context_size else 0
        self._context_size = ctx if ctx > 0 else _DEFAULT_CONTEXT_SIZE
        thr = int(threads) if threads else 0
        self._threads = thr if thr > 0 else 0
        gpu = int(gpu_layers) if gpu_layers else 0
        self._gpu_layers = gpu if gpu > 0 else 0

        # Injected only by tests that want a fake ``Llama`` without the
        # real wheel; ``None`` => import the genuine class on first use.
        self._llama_class_override = llama_class
        self._llama = None
        self._lock = threading.Lock()

    # ── internals ───────────────────────────────────────────────────

    def _get_llama_class(self) -> type:
        """Return ``llama_cpp.Llama`` (or an injected double).

        Raises :class:`ImportError` when the optional wheel is absent so
        callers can translate it into the fixed dependency message.
        """
        if self._llama_class_override is not None:
            return self._llama_class_override
        from llama_cpp import Llama  # optional dep; may raise ImportError

        return Llama

    def _prepare(self):
        """Return a loaded model handle. Caller must hold ``self._lock``.

        Maps every failure mode — missing dependency, missing/invalid
        path, load failure — to :class:`ModelProviderError` with a fixed,
        non-sensitive message.
        """
        if self._llama is not None:
            return self._llama
        try:
            llama_class = self._get_llama_class()
        except ImportError as exc:
            raise ModelProviderError(_DEP_MISSING_MSG) from exc
        if self._model_path is None:
            raise ModelProviderError(self._path_problem or _NO_MODEL_MSG)

        kwargs: dict = {
            "model_path": self._model_path,
            "n_ctx": self._context_size,
            "n_gpu_layers": self._gpu_layers,
            "verbose": False,
        }
        if self._threads:
            kwargs["n_threads"] = self._threads
        try:
            self._llama = llama_class(**kwargs)
        except Exception as exc:  # noqa: BLE001 — sanitised below
            logger.warning("GGUF model load failed: %s", exc)
            raise ModelProviderError(_LOAD_FAILED_MSG) from exc
        return self._llama

    # ── generation ──────────────────────────────────────────────────

    def generate(self, request: ModelRequest) -> ModelResponse:
        with self._lock:
            llama = self._prepare()
            try:
                result = llama.create_chat_completion(messages=request.messages)
            except Exception as exc:  # noqa: BLE001 — sanitised below
                logger.warning("GGUF generation failed: %s", exc)
                raise ModelProviderError(_GENERATE_FAILED_MSG) from exc
        content = _message_content(result)
        return ModelResponse(content=content or "", model=request.model)

    def stream(self, request: ModelRequest) -> Iterator[ModelChunk]:
        # The whole body runs while iterated, so the lock is held for the
        # duration of the stream — serialising access to the single,
        # non-concurrent llama.cpp handle. Nova's chat path consumes the
        # generator fully, so the lock is always released.
        with self._lock:
            llama = self._prepare()
            try:
                upstream = llama.create_chat_completion(
                    messages=request.messages, stream=True
                )
                for chunk in upstream:
                    fragment = _delta_content(chunk)
                    if fragment:
                        yield ModelChunk(content=fragment)
            except ModelProviderError:
                raise
            except Exception as exc:  # noqa: BLE001 — sanitised below
                logger.warning("GGUF streaming failed: %s", exc)
                raise ModelProviderError(_STREAM_FAILED_MSG) from exc

    # ── health ──────────────────────────────────────────────────────

    def health(self) -> ProviderHealth:
        """Cheap, read-only probe. Never raises; never loads the model.

        Reports ``ok=False`` with a clear reason when the dependency is
        missing or the configured path is absent/invalid, and ``ok=True``
        with the model's filename (basename only — never the full path)
        when everything needed to load is in place.
        """
        try:
            self._get_llama_class()
        except ImportError:
            return ProviderHealth(
                ok=False, provider=self.name, detail=_DEP_MISSING_MSG
            )
        except Exception as exc:  # never raise from health()
            logger.warning("GGUF health: backend import failed: %s", exc)
            return ProviderHealth(
                ok=False,
                provider=self.name,
                detail="llama-cpp backend could not be loaded.",
            )

        if not self._configured_path:
            return ProviderHealth(
                ok=False, provider=self.name, detail=_NO_MODEL_MSG
            )
        if self._model_path is None:
            return ProviderHealth(
                ok=False,
                provider=self.name,
                detail=self._path_problem or _NO_MODEL_MSG,
            )

        # Surface only the basename so the model is selectable in the
        # default-model UI without leaking the directory layout.
        return ProviderHealth(
            ok=True,
            provider=self.name,
            models=[os.path.basename(self._model_path)],
        )


# ── response shape helpers ──────────────────────────────────────────
# ``create_chat_completion`` returns OpenAI-style dicts. We read them
# defensively (``.get`` with isinstance guards) so an unexpected shape
# degrades to empty text rather than raising.


def _first_choice(payload) -> dict:
    if not isinstance(payload, dict):
        return {}
    choices = payload.get("choices")
    if not isinstance(choices, (list, tuple)) or not choices:
        return {}
    first = choices[0]
    return first if isinstance(first, dict) else {}


def _message_content(payload) -> str:
    message = _first_choice(payload).get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _delta_content(chunk) -> str:
    delta = _first_choice(chunk).get("delta")
    if not isinstance(delta, dict):
        return ""
    content = delta.get("content")
    return content if isinstance(content, str) else ""


_default: Optional[LlamaCppProvider] = None
_default_lock = threading.Lock()


def _resolve_configured_path() -> Optional[str]:
    """The model path to build the singleton with: persisted choice, else env.

    The admin-configurable path (Phase 2) lives in the settings DB and
    falls back to ``NOVA_GGUF_MODEL_PATH``. Resolved here — never raises,
    never loads a model — so construction stays cheap. ``None`` lets
    :class:`LlamaCppProvider` read ``config`` itself (the Phase-1
    behaviour) if the resolver is somehow unavailable.
    """
    try:
        from core.gguf_settings import resolve_gguf_model_path

        return resolve_gguf_model_path()
    except Exception as exc:  # pragma: no cover - resolver is defensive
        logger.debug("GGUF path resolve failed; using config: %s", exc)
        return None


def get_llamacpp_provider() -> LlamaCppProvider:
    """Process-wide singleton factory used by the registry.

    Cheap: construction only validates config and the model path — the
    model itself is loaded lazily on first generation. The model path is
    resolved from the admin-persisted setting (falling back to
    ``NOVA_GGUF_MODEL_PATH``), so an admin change takes effect once the
    cached instance is dropped (see :func:`reset_llamacpp_provider`).
    """
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                _default = LlamaCppProvider(model_path=_resolve_configured_path())
    return _default


def reset_llamacpp_provider() -> None:
    """Drop the cached singleton so the next factory call rebuilds it.

    Called after the admin updates the configured model path so the new
    file is picked up (and the previously-loaded model released) on the
    next generation, without a process restart.
    """
    global _default
    with _default_lock:
        _default = None


# Friendly alias: the backend is "llama.cpp", the model format is "GGUF".
GGUFProvider = LlamaCppProvider


__all__ = [
    "LlamaCppProvider",
    "GGUFProvider",
    "get_llamacpp_provider",
]
