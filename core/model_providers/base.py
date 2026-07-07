"""Model-provider interface — the seam between Nova core and a backend.

Nova owns identity, memory, projects, context construction, safety/trust
rules, tool routing, settings, and export/restore. The thing that turns a
prompt into tokens is *replaceable*. This module defines the small,
backend-agnostic contract every provider implements so Ollama becomes one
provider among others (mock/test today; llama.cpp, transformers, or a
Nova-owned runtime later) without Nova core depending on any concrete
client library or its exception types.

Hard rules baked into this boundary:

  * A provider only turns ``messages`` into text. It never builds the
    system prompt, so it can never reorder or override Nova's
    identity/safety contract — that ordering is owned upstream in
    :func:`core.chat.build_messages` and stays there.
  * ``provider.name`` is a backend label (``"ollama"``, ``"mock"``). It
    is **not** Nova's identity and must never be surfaced to the user as
    such.
  * Transport/availability failures surface as :class:`ModelProviderError`
    only, so callers handle one exception type regardless of backend.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterator, Mapping, Optional


# ── Error-kind taxonomy ──────────────────────────────────────────────
# A provider classifies *why* a request failed so callers can render a
# distinct, honest message instead of one catch-all. The chat layer maps
# these to user-facing text: a missing model names the model and how to
# install it; a runtime failure (crash / OOM-kill / load failure) is
# surfaced as such — never as a false "backend unreachable"; a genuine
# transport failure stays "unreachable". Unknown / legacy errors default
# to ``ERROR_BACKEND`` so existing ``ModelProviderError("msg")`` callers
# behave exactly as before.
ERROR_UNREACHABLE = "unreachable"      # backend not reachable at all
ERROR_MODEL_MISSING = "model_missing"  # backend up, model not installed
ERROR_MODEL_RUNTIME = "model_runtime"  # model failed to load / crashed / killed
ERROR_BACKEND = "backend_error"        # any other backend failure


class ModelProviderError(Exception):
    """A model provider could not fulfil a request.

    Providers wrap their backend-specific transport/availability errors
    (e.g. ``ollama.ResponseError``, ``ConnectionError``, ``httpx.HTTPError``)
    in this type so Nova core never imports or catches a concrete client
    library's exceptions. Callers map it to a controlled, user-facing
    message.

    ``kind`` is one of the ``ERROR_*`` constants above so the chat layer
    can tell "backend unreachable" apart from "model not installed" and
    "model crashed at runtime". ``model`` is the model name the request
    targeted, when known, so a missing-model message can name it. Both
    are optional and default to the backward-compatible "generic backend
    error, model unknown" — every existing raise site keeps working.
    """

    def __init__(
        self,
        message: str = "",
        *,
        kind: str = ERROR_BACKEND,
        model: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.model = model


@dataclass(frozen=True)
class ModelRequest:
    """One generation request.

    ``messages`` is the already-assembled chat history (system prompt
    first, built by Nova core — providers never touch its ordering).
    Vision requests carry their image inline on a message via the
    backend's own ``images`` key, exactly as before; providers pass
    ``messages`` through untouched. ``options`` is an opaque, optional
    per-request hint bag for future backends; the Ollama path ignores it
    today so current behaviour is unchanged.
    """

    model: str
    messages: list[dict]
    stream: bool = False
    options: Optional[Mapping[str, object]] = None


@dataclass(frozen=True)
class ModelResponse:
    """A complete, non-streamed reply."""

    content: str
    model: str


@dataclass(frozen=True)
class ModelChunk:
    """One incremental text fragment from a streaming generation.

    Empty fragments are legal on the wire (metadata/done frames) but the
    chat layer already skips empties when accumulating, so providers may
    forward or drop them as convenient.
    """

    content: str


@dataclass(frozen=True)
class ProviderHealth:
    """Result of a cheap, read-only liveness probe.

    ``health()`` never raises — an unreachable backend is reported as
    ``ok=False`` with a short, non-sensitive ``detail`` so status surfaces
    can render it without leaking transport internals.
    """

    ok: bool
    provider: str
    detail: str = ""
    models: list[str] = field(default_factory=list)


class ModelProvider(ABC):
    """The contract Nova core depends on instead of a concrete client.

    Implementations must be safe to construct cheaply and repeatedly, and
    must not perform any network I/O at construction time (no model
    downloads, ever — that is out of scope for this layer).
    """

    #: Stable backend label. NOT Nova's identity.
    name: str = "base"

    @abstractmethod
    def generate(self, request: ModelRequest) -> ModelResponse:
        """Return the full reply, or raise :class:`ModelProviderError`."""

    @abstractmethod
    def stream(self, request: ModelRequest) -> Iterator[ModelChunk]:
        """Yield :class:`ModelChunk` fragments in order.

        Raises :class:`ModelProviderError` if the backend is unreachable —
        either when starting the stream or partway through iteration, so
        callers wrap the whole consumption in one ``except``.
        """

    @abstractmethod
    def health(self) -> ProviderHealth:
        """Cheap read-only probe. Never raises; never triggers a pull."""
