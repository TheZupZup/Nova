# Nova Model Providers

> **Status: shipped (Phase: provider abstraction only), local-first.**
> This document describes the seam that lets Nova's model backend be
> replaced without rewriting Nova. It lives inside the boundaries set by
> [`docs/nova-safety-and-trust-contract.md`](nova-safety-and-trust-contract.md).
> Nothing here grants Nova new powers, adds a new model runtime, performs
> model downloads, runs shell, touches a Docker socket, integrates a
> cloud provider, or migrates settings. **Ollama remains the default and
> is fully supported.**

## Why Nova has model providers

Nova is not a chat wrapper around Ollama. Nova owns the parts that make
it *Nova*:

- **identity** and the safety / trust contract,
- **memory** (global and per-project),
- **projects / workspaces**,
- **context construction** (the system prompt, ordered so identity and
  safety always win),
- **tool routing**, **settings**, and **export / restore**.

The component that turns an assembled prompt into tokens — the *model
backend* — is an implementation detail. Before this change Nova called
the Ollama client directly from the chat path, so the backend could not
be swapped, tested without mimicking Ollama's wire shapes, or evolved
toward a Nova-owned runtime. The provider abstraction makes the backend
*replaceable* while everything above stays exactly as it was.

```text
Nova Core  (identity · memory · projects · context · safety · routing · settings · export)
   │
   ▼
Model Provider Interface          core/model_providers/base.py
   ├── OllamaProvider   (default)  core/model_providers/ollama.py
   ├── MockProvider     (tests)    core/model_providers/mock.py
   ├── future LlamaCppProvider
   ├── future TransformersProvider
   └── future NovaModelProvider
```

## The contract

`core/model_providers/base.py` defines small, backend-agnostic objects so
Nova core never imports a concrete client library or its exceptions:

| Object | Role |
| --- | --- |
| `ModelRequest` | `model`, `messages` (already assembled by Nova — system/identity prompt first), `stream`, optional opaque `options`. |
| `ModelResponse` | A complete reply (`content`, `model`). |
| `ModelChunk` | One streamed text fragment. |
| `ProviderHealth` | Result of a cheap, read-only liveness probe. |
| `ModelProviderError` | The **only** failure type callers handle, regardless of backend. |
| `ModelProvider` | ABC: `generate()`, `stream()`, `health()`. |

`core/model_providers/registry.py` is the one place Nova core asks "who
generates text?". Selection precedence: a test override → an explicit
name → `config.MODEL_PROVIDER` (env `NOVA_MODEL_PROVIDER`, default
`"ollama"`). A future runtime registers a factory via
`register_provider("name", factory)` and nothing in Nova core changes.

## Ollama remains supported (and default)

`OllamaProvider` preserves the pre-refactor behaviour exactly:

- the same `client.chat(model=…, messages=…)` call shape,
- the streaming chunk duck-typing (`ollama>=0.4` streams `ChatResponse`
  Pydantic objects — subscriptable but not `dict`; older clients/tests
  yield dicts — both must work),
- the legacy single-shot fallback when an old ollama-python lacks the
  `stream=` kwarg,
- mapping `(ollama.ResponseError, ConnectionError, httpx.HTTPError, …)`
  to `ModelProviderError`, which the chat path still turns into the
  existing "Ollama is unreachable" reply / stream `error` event.

It resolves the shared `core.ollama_client.client` singleton lazily on
every call, so changing `OLLAMA_HOST` needs no process restart and
existing tests that patch that client keep working. Nothing about the
default deployment changes: leave `NOVA_MODEL_PROVIDER` unset and Nova
behaves exactly as before.

## Future providers

New **local** runtimes can be added cleanly in later phases — for
example a `LlamaCppProvider` (GGUF via llama.cpp), a
`TransformersProvider` (Hugging Face Transformers), or a Nova-owned
runtime (`NovaModelProvider`). Each only implements `generate()`,
`stream()`, and `health()` and registers a name. **This phase adds none
of them**, performs no model downloads, and adds no cloud providers and
no API keys — those are explicitly out of scope.

## Nova identity is above provider identity

This is a hard rule, enforced by where the boundary sits:

- A provider only turns `messages` into text. It never builds or reorders
  the system prompt — that ordering is owned by
  `core.chat.build_messages`, where the identity / safety contract,
  personalization, and feedback blocks are layered so **safety and
  identity always win**. A provider cannot move itself above them.
- `provider.name` (`"ollama"`, `"mock"`, …) is a backend label for
  diagnostics. It is **not** Nova's identity and is never surfaced to the
  user as such. Whatever a model emits about "who it is" does not change
  who Nova is.
- Global memory and per-project memory remain owned by Nova. Providers
  receive only the messages Nova chose to send and never read or write
  memory, projects, settings, or export data.
- Backend failures degrade to a calm, controlled message — a provider
  can make Nova briefly unable to answer, never able to override its
  rules.

## Testing

`MockProvider` gives deterministic, offline replies so suites no longer
stub a concrete client or mimic Ollama's event shapes. The provider
suite (`tests/test_model_providers.py`) covers request-shape
preservation, stream duck-typing, the legacy `TypeError` fallback,
failure mapping, clean health-failure handling, registry resolution, and
that `chat` / `chat_stream` route through the interface. The existing
chat / memory / project / storage suites continue to pass against the
real `OllamaProvider` path.
