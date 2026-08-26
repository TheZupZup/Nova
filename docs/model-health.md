# Local model runtime health

> **Status: shipped, admin-only, read-only.** Never downloads, loads, or
> generates. Makes no assumption about any GPU vendor.

[`docs/model-profiles.md`](model-profiles.md) answers *what* each role
runs. This surface answers what an operator asks next when a coding turn
is slow or fails: **is it installed, is it loaded, what context is it
actually running with, and if something is wrong, what is the useful
error?**

```
GET /admin/models/health
```

## Where an operator sees this

**Settings → Models → "Model roles & health"** (admin-only) renders the
whole thing from a single call to this endpoint: which model fills each
role, what Nova assumes about it (context, code-specialisation,
tool-capability, resource class, and whether the profile came from the
builtin table or an operator overlay), whether the backend has it
installed, whether it is loaded right now, and what context size the
runtime reports.

The card is deliberately read-only. It offers a **Refresh** button and
nothing else — no pull, no load, no restart, no default-model change.
Those live on their own existing surfaces, behind their own
confirmations. The row is hidden entirely for non-admins on the client,
and the endpoint is `require_admin` on the server.

## Per-role states

| State | Meaning |
| --- | --- |
| `loaded` | Installed and currently resident in the runtime. |
| `installed` | Installed but not loaded — the next turn pays a cold start. |
| `not_installed` | Backend is up and does not have this model. |
| `unknown` | Backend is unreachable, or no model is configured for the role. |

The distinction matters: while the backend is unreachable Nova reports
`unknown`, **never** `not_installed`. A backend that cannot answer cannot
tell you a model is missing, and claiming otherwise would send an
operator chasing the wrong problem.

Each role also carries a `hint` naming the concrete next step — install
this model, point `NOVA_CODE_MODEL` somewhere else, start the backend —
and the missing-model hint states plainly that Nova will not download it.

## Context sizes

Two numbers are reported side by side and never conflated:

- `profile_context_size` — what Nova *recommends* for that model, from
  its profile.
- `runtime_context_size` — what the runtime *says* it is configured
  with, read from `/api/ps` or `/api/show`.

`runtime_context_size` is `null` when the runtime did not report one.
Nova does not invent a number to fill the field.

## Single-model backends

When the active provider serves one configured model for every role and
ignores the role's model *name* (llama.cpp), tag-matching those names
against the backend's file would mark every working role
`not_installed` and advise an `ollama pull` on a host that does not run
Ollama. Instead each role reports the configured backend model and says
plainly that the role's name is not a selector there.

**Reachable is not resident.** Such a backend's health probe
deliberately does not load the model, so "configured and reachable"
says nothing about whether the next request pays a cold start.
Residency comes from `ModelProvider.is_model_resident()`, which returns
True/False when the backend can answer and `None` when it cannot —
rendered as unknown, never as "not loaded".

## Errors

`errors` carries stable machine-readable codes rather than prose:

| Code | Meaning |
| --- | --- |
| `backend_unreachable` | The model backend did not answer its health probe. |
| `runtime_details_unsupported` | The active provider has no loaded-model view (any non-Ollama backend today). |
| `runtime_details_unavailable` | The backend is up but the runtime read failed. |

An unreachable backend is a calm `200` with `reachable: false` — never a
`500`.

## Hardware

Nova reports **only what the runtime volunteers**: total resident bytes,
and an accelerator-resident byte count when the daemon supplies one.

- No vendor SDK is imported — no `pynvml`, no `torch`, no vendor Python
  package.
- No vendor tool is spawned — there is no `subprocess` import in
  `core/model_health.py` at all.
- A CPU-only host is a normal, fully-supported answer, not a degraded
  one.
- When the runtime says nothing about placement, Nova says nothing about
  placement.

## Read-only by construction

The three calls behind this surface are `/api/tags` (installed),
`/api/ps` (loaded) and `/api/show` (metadata). None of them download.
`/api/show` answers `404` for a model the backend does not have rather
than fetching it — so a status question can never become a multi-gigabyte
download. Providers other than Ollama still report installed models via
their own `health()` probe and degrade gracefully on the rest.
