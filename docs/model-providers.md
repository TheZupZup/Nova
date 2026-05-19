# Nova Model Providers

> **Status: shipped (Phases: provider abstraction + read-only provider
> settings + admin default-model selection), local-first.**
> This document describes the seam that lets Nova's model backend be
> replaced without rewriting Nova, the admin-only surface for *seeing
> and validating* which backend is active, and the admin-only,
> *validated* choice of which model that backend is asked for by
> default. It lives inside the boundaries set by
> [`docs/nova-safety-and-trust-contract.md`](nova-safety-and-trust-contract.md).
> Nothing here grants Nova new powers, adds a new model runtime, performs
> model downloads, runs shell, touches a Docker socket, integrates a
> cloud provider, or migrates settings. **Provider _selection_ stays
> read-only and env-driven** (`NOVA_MODEL_PROVIDER`); the only thing an
> admin can write is the default *model*, and only after it is validated
> against the active provider's own list. **Ollama remains the default
> provider and is fully supported.**

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

## Seeing & validating the active provider (admin, read-only)

Phase 1 of provider *settings* adds a small admin-only surface so an
operator can answer two questions without reading logs or env files —
**which backend is Nova configured to use** (and which model it asks
for), and **does it actually answer right now**. It changes nothing:
provider selection stays env-driven (`NOVA_MODEL_PROVIDER`), Ollama
stays the default, and nothing is written, migrated, pulled, or
restarted.

`core/provider_status.py` is the calm, read-only foundation, mirroring
`core/storage_status.py`:

| Function | Role |
| --- | --- |
| `get_provider_status()` | Configured provider, the default (always `ollama`), the resolved active backend, the selectable providers, the redacted Ollama host, Nova's default chat model (`config.MODELS["default"]`, host-level and non-secret), whether the backend supports streaming, and warnings. Never reaches the network; never raises — an unknown configured provider is an `error` string, not an exception, and a missing default model degrades to `""`, never a failure. |
| `probe_provider_health(name=None)` | A live but cheap, read-only liveness probe. Delegates to the provider's own `health()` (`client.list()` for Ollama — never a pull, never a generation) and always returns the stable `{ok, provider, detail, models}` shape, even for an unreachable or unknown backend. |

Two admin-only endpoints expose it (both `require_admin`; the provider
name and host are operator-sensitive):

- `GET /admin/provider/status` — the read-only snapshot.
- `POST /admin/provider/test-connection` — runs the liveness probe now.
  It is `POST` so it reads as an explicit "probe now" action and is
  never cached; it needs no confirmation because it cannot modify
  anything (mirrors `/admin/maintenance/fetch`).

Two places render this, both admin-only on the client *and* the server
(the row/tab is hidden for non-admins and the endpoints are
`require_admin`):

- **Settings → Models** shows a calm summary an admin sees where they
  already look for model settings: the active provider and its state
  (default / non-default / not-registered), the current/default model,
  whether streaming is supported, the redacted Ollama host, and a
  **Test connection** button with a clear success/failure message. To
  check provider health, open **Settings → Models** and click **Test
  connection** — it runs the cheap, read-only liveness probe (for
  Ollama: lists installed models; never a pull, never a generation).
- **Admin → Provider** keeps the deeper view: the same snapshot plus
  the registered/selectable providers, the test-only backends, every
  warning, and its own **Test provider connection** button.

Both reuse the same `/admin/provider/*` endpoints, so the success and
failure language is identical wherever an admin runs the probe.

Guardrails baked into this surface:

- **Read-only.** No endpoint mutates the registry, writes settings,
  triggers a download, or restarts anything. An unreachable backend or
  an unknown configured provider is reported as data (HTTP 200 with
  `ok=false` / `error`), never a 500 — the same calm stance as the
  maintenance / storage endpoints.
- **Ollama stays the default.** `DEFAULT_PROVIDER` is `"ollama"`; a
  non-default but registered provider is reported calmly with a "not
  the default" note, never an error.
- **MockProvider stays test-only.** `mock` is never advertised in
  `selectable_providers`. If Nova is *configured* to use it the status
  still reports that truthfully, with a clear warning, so a stray test
  setting can never hide.
- **No secrets.** The only env-derived string surfaced is the Ollama
  host, with any `user:pass@` userinfo redacted before display.

## Choosing Nova's default model (admin, validated)

Phase 1 made the active provider *visible*. Phase 2 lets an admin pick
**which model that provider is asked for by default** — from the models
the provider actually reports, validated before anything is persisted.
It still adds no runtime, pulls nothing, and never changes which
*provider* is used.

### How Nova chooses the default model

There are two layers, and Phase 2 only changes the second:

1. **Per-request routing is unchanged.** A free-chat message is still
   classified by `core.router.route` into `simple` / `normal` / `code`
   / `advanced`; `code` and `advanced` keep their dedicated
   `config.MODELS` entries, and an explicit *Code* / *Deep* UI mode
   still forces those exact models. None of that is configurable here.
2. **The _default_ model is now resolvable, not hard-coded.** Wherever
   Nova previously read `config.MODELS["default"]` as "the general chat
   model" — `simple`/`normal` routing and the router fallback, the
   explicit *Chat* mode, the vision/image path, background memory
   extraction, and the model label on the unreachable-provider reply —
   it now calls `core.model_settings.resolve_default_model()`.

`resolve_default_model()` returns the **admin-selected model if one has
been safely persisted, otherwise `config.MODELS["default"]`**. It reads
a single host-wide row from the `settings` table, performs **no network
I/O**, and never raises — so the chat hot path is exactly as fast and
offline-safe as before, and **every existing install (no row set)
behaves identically to before this phase.** `core.router.MODEL_MAP` and
`MODEL_MAP`-equivalent constants stay pinned to `config.MODELS` (the
compiled-in contract is unchanged); the admin choice is an *overlay*
applied at call time, not a rewrite of routing.

The selection is **host-wide and admin-owned**, the same scope as
`config.MODELS` — it is deliberately *not* the per-user `nova_model_name`
preference, which is a separate, untouched feature.

### The admin surface

`core/model_settings.py` is the foundation; two admin-only endpoints
(both `require_admin`) expose it:

| Endpoint | Role |
| --- | --- |
| `GET /admin/provider/models` | Read-only. Reuses the Phase-1 `health()` probe (`client.list()` for Ollama — never a pull or a generation) and returns `{ok, provider, detail, models, default_model, config_default_model, is_custom}`. An unreachable provider is a calm `ok=false` with an empty list, never a 500. |
| `POST /admin/provider/default-model` | Validates the chosen model against the active provider's reported list, then persists it. Body is `{ "model": "<name>" }` only (`extra="forbid"`). Returns the new state on success; a refusal is a sanitised `400`. |

**Settings → Models** gains a *Default model* card next to the existing
read-only provider summary: it shows the current default (tagged
*custom* or *config default*), lists the installed models, and offers a
**Set as default** action. Because the choice is host-wide, the action
goes through an explicit `confirm()` first (the same stray-click guard
the maintenance pull/restart buttons use) before the validated write.
It is hidden for non-admins on the client and the endpoints are
admin-only on the server.

### Guardrails

- **Validated, never arbitrary.** A model is persisted only if the
  *active* provider currently lists it. An empty / over-long string, a
  model the provider does not report, or an unreachable provider is
  refused and **nothing is written**.
- **The active provider only.** No provider name is ever accepted from
  the client (`extra="forbid"`, and the core never takes one) — the
  provider is always the configured backend, so a stray / test-only /
  unknown backend can never be selected through this surface. Provider
  selection stays env-driven.
- **No secrets.** The unreachable-provider refusal is a fixed,
  non-sensitive message — it never echoes a raw transport error or the
  configured host (the Phase-1 status surface already shows a redacted
  host; the *write* path says nothing about why the backend is down).
  The candidate string is not reflected back either.
- **Read path stays safe.** `resolve_default_model()` does no network
  I/O and swallows every error to the config default, so a bad DB can
  never wedge chat. If a persisted model is later removed from the
  provider, chat degrades through the existing "provider unreachable /
  error" handling exactly as an unknown model always has — no new
  failure mode, no auto-repull.
- **No downloads, no new runtime.** Listing is read-only; nothing here
  pulls, imports, or runs a model. `MockProvider` stays test-only.

## Future providers

New **local** runtimes can be added cleanly in later phases — for
example a `LlamaCppProvider` (GGUF via llama.cpp), a
`TransformersProvider` (Hugging Face Transformers), or a Nova-owned
runtime (`NovaModelProvider`). Each only implements `generate()`,
`stream()`, and `health()` and registers a name. **This phase adds none
of them**, performs no model downloads, and adds no cloud providers and
no API keys — those are explicitly out of scope.

The default-model surface needs **no per-provider code** to support a
future backend: model listing flows through the same `health()` the
provider already implements (`ProviderHealth.models`), so a new
provider's installed models appear in `GET /admin/provider/models` —
and become validly selectable defaults — automatically once it returns
them from its own read-only probe. A provider that cannot enumerate
models simply returns an empty list; the surface degrades to the calm
"no models reported" state instead of offering an unvalidated choice.

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

The read-only settings surface has its own suites:
`tests/test_provider_status.py` pins the reporter contract (Ollama is
the default and unset deployments warn-free; `mock` is never selectable
but is reported truthfully with a warning if configured; an unknown
provider is an `error`, not an exception; the host is redacted; the
liveness probe always returns the stable shape, even when the provider
breaks the "health never raises" contract). `tests/test_provider_endpoints.py`
pins the wire contract (`require_admin` gating, the status / probe JSON
shapes, and that an unreachable or unknown backend stays a calm 200).

The Phase-2 default-model selection adds two more suites.
`tests/test_model_settings.py` pins the core: `resolve_default_model()`
falls back to `config.MODELS["default"]` when unset and is network-free
even when the provider explodes; `set_default_model()` only persists a
model the active provider lists, refuses empty / oversized / not-listed
/ unreachable with **nothing written**, and the unreachable refusal
leaks neither host nor transport detail nor the candidate string; the
key is host-wide (not a user setting) and unreachable through the
generic `/settings` allowlist. `tests/test_provider_default_model_endpoints.py`
pins the wire contract (`require_admin` gating on both endpoints,
listing stays a calm 200 when the backend is down, a not-installed
model is a `400`, an empty / extra-field body is a schema `422`, an
unreachable provider is a sanitised `400`, and a successful set really
persists). The existing chat / streaming / routing / model-access /
registry / pull suites continue to pass unchanged — with nothing
persisted, every default-model lookup returns the same
`config.MODELS["default"]` as before.
