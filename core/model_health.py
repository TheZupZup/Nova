"""
Local model runtime health — what is installed, what is loaded, why not.

``core.model_status`` already answers *"which model fills each role and
is the backend up?"*. This module answers the operational questions that
come next when a coding turn is slow or fails:

  * is this model **installed** on the local runtime?
  * is it **loaded** in memory right now, or will the next turn pay a
    cold-start?
  * what **context size** is it actually configured for?
  * when the backend or a model is unavailable, **what is the useful
    error** — not a generic "unreachable"?

Everything here is read-only. It calls ``/api/tags`` (installed),
``/api/ps`` (loaded) and ``/api/show`` (metadata) — none of which
download, load, or generate. A model that is not installed stays not
installed; Nova never pulls to answer a status question.

Hardware
--------
Nova makes **no assumption about any GPU vendor**. It reports only what
the runtime itself volunteers: total resident bytes and, when the daemon
provides it, an accelerator-resident byte count. No vendor SDK is
imported, no ``nvidia-smi``/``rocm-smi`` is spawned, and a CPU-only host
is a normal answer rather than a degraded one. When the runtime says
nothing about placement, Nova says nothing about placement.

Providers other than Ollama degrade gracefully: the installed list still
comes from the provider's own ``health()`` probe, and the loaded/context
details are reported as unsupported rather than guessed.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

#: Per-model states surfaced to the operator. ``STATE_INSTALLED`` means
#: installation is known; consult the separate ``loaded`` field to tell
#: whether residency is true, false, or unknown (``None``).
STATE_LOADED = "loaded"              # installed and resident in memory
STATE_INSTALLED = "installed"        # installed; residency may be known or unknown
STATE_NOT_INSTALLED = "not_installed"  # backend up, model absent
STATE_UNKNOWN = "unknown"            # installation itself cannot be claimed

#: Why the runtime could not answer, in stable machine-readable form.
ERROR_BACKEND_UNREACHABLE = "backend_unreachable"
ERROR_RUNTIME_DETAILS_UNSUPPORTED = "runtime_details_unsupported"
ERROR_RUNTIME_DETAILS_UNAVAILABLE = "runtime_details_unavailable"

_MAX_DETAIL_CHARS = 300


def _short(detail: object) -> str:
    text = str(detail or "").strip().replace("\n", " ")
    return text[:_MAX_DETAIL_CHARS]


def _load_runtime_details() -> tuple[dict[str, dict], Optional[str]]:
    """``({model_name: runtime_row}, error_code)`` for the loaded models.

    Only Ollama exposes a loaded-model view today. Any other provider —
    or an Ollama daemon that cannot be reached — yields an empty map plus
    an error code, never an exception and never a guess.
    """
    try:
        from core.model_providers import get_provider

        provider_name = getattr(get_provider(), "name", "")
    except Exception as exc:  # pragma: no cover - registry is stable
        logger.debug("model health: provider lookup failed: %s", exc)
        return {}, ERROR_RUNTIME_DETAILS_UNAVAILABLE

    if provider_name != "ollama":
        return {}, ERROR_RUNTIME_DETAILS_UNSUPPORTED

    try:
        from core.ollama_client import list_running_models

        rows = list_running_models()
    except Exception as exc:
        # ``OllamaUnavailable`` is the expected case; anything else is a
        # surprise we still refuse to raise into a status endpoint.
        logger.debug(
            "model health: /api/ps unavailable (%s)", type(exc).__name__
        )
        return {}, ERROR_RUNTIME_DETAILS_UNAVAILABLE

    loaded: dict[str, dict] = {}
    for row in rows:
        name = row.get("name")
        if isinstance(name, str) and name:
            loaded[name] = row
    return loaded, None


def _configured_context_from_show(payload: dict) -> Optional[int]:
    """The context the runtime is *configured* to use, from ``/api/show``.

    Only ``num_ctx`` answers this. The architecture key inside
    ``model_info`` (e.g. ``llama.context_length``) is the model's
    **maximum capability**, not the window the runtime will allocate: a
    Modelfile carrying ``num_ctx 8192`` on a model whose architecture
    supports 131072 runs at 8192. Reading the larger number as the
    runtime configuration would overstate the context by 16x, so this
    deliberately does not fall back to it — see
    :func:`_context_capacity_from_show`, which reports it under its own
    name. ``None`` means "the runtime did not say".
    """
    params = payload.get("parameters")
    if isinstance(params, str):
        for line in params.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "num_ctx":
                try:
                    value = int(parts[1])
                except ValueError:
                    continue
                if value > 0:
                    return value
    return None


def _context_capacity_from_show(payload: dict) -> Optional[int]:
    """The architecture's maximum context, when ``/api/show`` reports it.

    A capability, not a configuration — kept separate so it can be shown
    without being mistaken for the window actually in use.
    """
    info = payload.get("model_info")
    if isinstance(info, dict):
        for key, value in info.items():
            if isinstance(key, str) and key.endswith(".context_length"):
                if isinstance(value, int) and value > 0:
                    return value
    return None


def _runtime_context_size(model: str) -> tuple[Optional[int], Optional[int]]:
    """``(configured, capacity)`` for ``model``. Never raises.

    ``configured`` is the window the runtime will actually use, and is
    ``None`` unless the runtime states it. ``capacity`` is the
    architecture maximum, reported separately so it can never be mistaken
    for the configured value.
    """
    try:
        from core.ollama_client import show_model

        payload = show_model(model)
    except Exception as exc:
        logger.debug(
            "model health: /api/show unavailable for a model (%s)",
            type(exc).__name__,
        )
        return None, None
    return (
        _configured_context_from_show(payload),
        _context_capacity_from_show(payload),
    )


def _match_installed(model: str, installed: set[str]) -> bool:
    """Tag-tolerant installed check (``x`` matches ``x:latest``).

    Mirrors ``core.model_registry._matches_installed`` so the two
    surfaces never disagree about whether a model is present.
    """
    if model in installed:
        return True
    for name in installed:
        if name.startswith(model + ":") or model.startswith(name + ":"):
            return True
    return False


def _match_loaded(model: str, loaded: dict[str, dict]) -> Optional[dict]:
    row = loaded.get(model)
    if row is not None:
        return row
    for name, candidate in loaded.items():
        if name.startswith(model + ":") or model.startswith(name + ":"):
            return candidate
    return None


def get_model_health(*, include_context_sizes: bool = True) -> dict:
    """A read-only snapshot of the local model runtime. Never raises.

    Shape::

        {
          "provider": str,
          "reachable": bool,
          "detail": str,                 # backend's short health detail
          "errors": [str],               # stable machine-readable codes
          "installed_models": [str],
          "loaded_models": [ {...} ],    # only what the runtime reported
          "runtime_details_available": bool,
          "roles": [                     # one entry per routing role
            {
              "role": str,
              "model": str,
              "state": str,              # STATE_* above
              "loaded": bool|None,       # None => residency unavailable
              "profile_context_size": int,   # Nova's recommendation
              "runtime_context_size": int|None,  # configured window
              "context_capacity": int|None,      # architecture maximum
              "resource_class": str,
              "code_specialized": bool,
              "supports_tools": bool,    # profile claim, grants nothing
              "profile_source": str,     # builtin | override | unknown
              "hint": str,               # what an operator should do
            }
          ]
        }

    ``runtime_context_size`` is ``None`` whenever the runtime did not
    report one. Likewise ``loaded`` is ``None`` when the loaded-model
    view is unavailable: Nova never turns missing runtime evidence into a
    false cold-start claim.
    """
    from core import model_profiles
    from core.provider_status import probe_provider_health

    errors: list[str] = []
    health = probe_provider_health()
    reachable = bool(health.get("ok"))
    provider = str(health.get("provider") or "unknown")
    installed = sorted(
        {m for m in (health.get("models") or []) if isinstance(m, str) and m}
    )
    installed_set = set(installed)
    if not reachable:
        errors.append(ERROR_BACKEND_UNREACHABLE)

    loaded_map: dict[str, dict] = {}
    runtime_error: Optional[str] = None
    if reachable:
        loaded_map, runtime_error = _load_runtime_details()
        if runtime_error:
            errors.append(runtime_error)

    # A single-model backend (llama.cpp) serves one configured file for
    # every role and ignores the role's model *name*. Detect it once so
    # the per-role branch below can describe that honestly instead of
    # tag-matching names the backend never looks at.
    single_model_backend = False
    backend_model = ""
    backend_resident: Optional[bool] = None
    try:
        from core.model_providers import get_provider

        active = get_provider()
        single_model_backend = not getattr(active, "selects_model_by_name", True)
        if single_model_backend:
            backend_model = active.backend_model_id() or ""
            # Reachable != resident. A single-model backend's health
            # probe deliberately does not load the model, so ask it
            # directly; ``None`` stays unknown rather than becoming a
            # claim in either direction.
            try:
                backend_resident = active.is_model_resident()
            except Exception:  # pragma: no cover - defensive
                backend_resident = None
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("model health: provider capability read failed: %s", exc)

    profiles = model_profiles.role_profiles()
    roles: list[dict] = []
    context_cache: dict[str, Optional[int]] = {}

    for role in model_profiles.ROLES:
        profile = profiles[role]
        model = profile.name
        if not model:
            roles.append({
                "role": role,
                "model": "",
                "state": STATE_UNKNOWN,
                "loaded": None,
                "profile_context_size": profile.context_size,
                "runtime_context_size": None,
                "resource_class": profile.resource_class,
                "code_specialized": profile.code_specialized,
                "supports_tools": profile.supports_tools,
                "profile_source": profile.source,
                "hint": (
                    f"No model is configured for the {role} role. Set "
                    f"{model_profiles.ROLE_ENV_VARS[role]}."
                ),
            })
            continue

        loaded_state: Optional[bool]
        if single_model_backend:
            # This backend ignores the role's model *name* and serves one
            # configured file. Tag-matching the role name against that
            # file's basename would mark every working role
            # "not installed" and advise an `ollama pull` on a host that
            # does not run Ollama.
            served = backend_model or "its configured model"
            if reachable:
                loaded_state = backend_resident
                if backend_resident is True:
                    state = STATE_LOADED
                    residency = "It is loaded and resident."
                elif backend_resident is False:
                    state = STATE_INSTALLED
                    residency = (
                        "It is configured but not yet loaded — the next "
                        "request pays a cold start."
                    )
                else:
                    state = STATE_INSTALLED
                    residency = (
                        "Whether it is currently resident could not be "
                        "determined."
                    )
                hint = (
                    f"This provider serves one configured model "
                    f"({served}) for every role and ignores the role's "
                    f"model name, so '{model}' is not selected here. "
                    f"{residency} Change the provider's configured model "
                    f"to change what runs."
                )
            else:
                state = STATE_UNKNOWN
                loaded_state = None
                hint = (
                    "The model backend is unreachable, so Nova cannot "
                    "say what it would serve. Start the backend and "
                    "refresh."
                )
        elif not reachable:
            state = STATE_UNKNOWN
            loaded_state = None
            hint = (
                "The model backend is unreachable, so Nova cannot say "
                "whether this model is installed or loaded. Start the "
                "backend and refresh."
            )
        elif not _match_installed(model, installed_set):
            state = STATE_NOT_INSTALLED
            loaded_state = False
            hint = (
                f"'{model}' is not installed. Install it on the backend "
                f"(for Ollama: `ollama pull {model}`), or point "
                f"{model_profiles.ROLE_ENV_VARS[role]} at a model you "
                f"already have. Nova never downloads it for you."
            )
        elif runtime_error is not None:
            # Installation is known from the provider health probe, but
            # the loaded-model view failed or is unsupported. Do not turn
            # an empty loaded_map into the false statement "not loaded".
            state = STATE_INSTALLED
            loaded_state = None
            hint = (
                "Installed, but the runtime did not provide loaded-state "
                "details. Nova cannot tell whether this model is already "
                "resident or whether the next request will be a cold start."
            )
        elif _match_loaded(model, loaded_map) is not None:
            state = STATE_LOADED
            loaded_state = True
            hint = ""
        else:
            state = STATE_INSTALLED
            loaded_state = False
            hint = (
                "Installed but not currently loaded — the next request "
                "pays a cold start."
            )

        runtime_ctx: Optional[int] = None
        context_capacity: Optional[int] = None
        if include_context_sizes and state in (STATE_LOADED, STATE_INSTALLED):
            row = _match_loaded(model, loaded_map) or {}
            candidate = row.get("context_size")
            if isinstance(candidate, int) and candidate > 0:
                # A loaded model's own report is the ground truth.
                runtime_ctx = candidate
            # ``/api/ps`` and ``/api/show`` answer different questions and
            # fail independently: ps says what is *resident*, show says
            # what a model is *configured* with. Gating show on ps having
            # worked threw away a perfectly good num_ctx and architecture
            # capacity whenever the loaded-model view was unavailable —
            # residency stayed unknown (correctly) but two unrelated
            # facts went missing with it.
            #
            # It must still not run for a single-model backend: those
            # ignore the role's model *name*, so asking Ollama about that
            # name would publish an unrelated model's numbers for a
            # llama.cpp role whenever an Ollama daemon happens to be
            # reachable on the same host.
            if (runtime_ctx is None or context_capacity is None) and not single_model_backend:
                if model not in context_cache:
                    context_cache[model] = _runtime_context_size(model)
                configured, capacity = context_cache[model]
                if runtime_ctx is None:
                    runtime_ctx = configured
                context_capacity = capacity

        roles.append({
            "role": role,
            "model": model,
            "state": state,
            "loaded": loaded_state,
            "profile_context_size": profile.context_size,
            "runtime_context_size": runtime_ctx,
            # Architecture maximum, never the configured window.
            "context_capacity": context_capacity,
            "resource_class": profile.resource_class,
            "code_specialized": profile.code_specialized,
            "supports_tools": profile.supports_tools,
            "profile_source": profile.source,
            "hint": hint,
        })

    return {
        "provider": provider,
        "reachable": reachable,
        "detail": _short(health.get("detail")),
        "errors": errors,
        "installed_models": installed,
        "loaded_models": [dict(r) for r in loaded_map.values()],
        "runtime_details_available": reachable and runtime_error is None,
        "roles": roles,
    }


__all__ = [
    "STATE_LOADED", "STATE_INSTALLED", "STATE_NOT_INSTALLED", "STATE_UNKNOWN",
    "ERROR_BACKEND_UNREACHABLE", "ERROR_RUNTIME_DETAILS_UNSUPPORTED",
    "ERROR_RUNTIME_DETAILS_UNAVAILABLE",
    "get_model_health",
]
