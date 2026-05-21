"""Model-provider abstraction.

Nova core depends on :class:`ModelProvider` and the small data objects in
:mod:`core.model_providers.base` — never on a concrete client library.
Ollama is the default provider; :class:`MockProvider` serves tests and
offline development; future local runtimes register through
:func:`register_provider`.

See ``docs/model-providers.md`` for the rationale and the rule that a
provider's identity is never Nova's identity.
"""

from .base import (
    ModelChunk,
    ModelProvider,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ProviderHealth,
)
from .llamacpp import GGUFProvider, LlamaCppProvider, get_llamacpp_provider
from .mock import MockProvider
from .ollama import OllamaProvider, get_ollama_provider
from .registry import (
    available_providers,
    get_provider,
    register_provider,
    reset,
    set_override,
    use_provider,
)

__all__ = [
    "ModelProvider",
    "ModelProviderError",
    "ModelRequest",
    "ModelResponse",
    "ModelChunk",
    "ProviderHealth",
    "OllamaProvider",
    "get_ollama_provider",
    "LlamaCppProvider",
    "GGUFProvider",
    "get_llamacpp_provider",
    "MockProvider",
    "get_provider",
    "register_provider",
    "available_providers",
    "use_provider",
    "set_override",
    "reset",
]
