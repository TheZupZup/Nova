"""Deterministic in-process provider for tests and offline development.

Same input always yields the same output, so suites that exercise the
chat/streaming paths no longer need to stub a concrete client library or
mimic Ollama's event shapes. It is intentionally tiny and never touches
the network, the filesystem, or any model runtime.
"""

from __future__ import annotations

from typing import Iterator, Optional, Sequence

from .base import (
    ModelChunk,
    ModelProvider,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ProviderHealth,
)


class MockProvider(ModelProvider):
    """Canned, deterministic responses.

    * ``response`` — the full text returned by :meth:`generate` and, when
      ``chunks`` is not given, the single fragment streamed.
    * ``chunks`` — explicit ordered fragments for :meth:`stream`; their
      concatenation is also what :meth:`generate` returns when
      ``response`` is left at its default so the two paths stay
      consistent for a given configuration.
    * ``healthy`` — what :meth:`health` reports.
    * ``error`` — if set, every :meth:`generate` / :meth:`stream` call
      raises it (use a :class:`ModelProviderError` to simulate an
      unreachable backend cleanly).

    Every request is recorded on :attr:`requests` so tests can assert
    that Nova core routed through the provider with the expected model
    and messages.
    """

    name = "mock"

    _DEFAULT = "mock response"

    def __init__(
        self,
        response: Optional[str] = None,
        chunks: Optional[Sequence[str]] = None,
        healthy: bool = True,
        error: Optional[Exception] = None,
    ):
        self._chunks: list[str] = list(chunks) if chunks is not None else []
        if response is not None:
            self._response = response
        elif self._chunks:
            self._response = "".join(self._chunks)
        else:
            self._response = self._DEFAULT
        if not self._chunks:
            self._chunks = [self._response] if self._response else []
        self._healthy = healthy
        self._error = error
        self.requests: list[ModelRequest] = []

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return ModelResponse(content=self._response, model=request.model)

    def stream(self, request: ModelRequest) -> Iterator[ModelChunk]:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        for fragment in self._chunks:
            yield ModelChunk(content=fragment)

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            ok=self._healthy,
            provider=self.name,
            detail="" if self._healthy else "mock provider marked unhealthy",
        )


__all__ = ["MockProvider", "ModelProviderError"]
