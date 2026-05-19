"""Admin-selected default model (Phase 2 of provider settings).

Phase 1 made the active provider *visible* and *probeable* (see
``core/provider_status.py``). Phase 2 lets an admin choose **which model
that provider is asked for by default**, picked from the models the
provider actually reports — validated before it is ever persisted, never
a free-text field, never a download trigger.

Scope / safety contract (Phase 2):

* **Validated, never arbitrary.** :func:`set_default_model` only persists
  a model the *active* provider currently lists via its read-only
  ``health()`` probe. An unreachable provider, an empty / oversized
  string, or a model the provider does not report is refused with a
  short, sanitised reason — nothing is written.
* **The active provider only.** The provider is always the configured
  one (``core.model_providers.get_provider()``); callers never pass a
  provider name, so a stray/unknown backend can never be selected here.
* **Host-level, not per-user.** The choice is a single row in the global
  ``settings`` table via :func:`core.settings.save_system_setting` — the
  same scope as ``config.MODELS``. It is an operator decision, not a
  per-account preference (``nova_model_name`` stays a separate per-user
  concern and is untouched).
* **Read path is network-free and never raises.**
  :func:`resolve_default_model` returns the persisted choice if one is
  set, else ``config.MODELS["default"]``. It performs no network I/O and
  swallows every error, so the chat hot path stays exactly as fast and
  offline-safe as before — a missing/oddly-shaped config or DB degrades
  to the config default, never an exception.
* **No secrets, no downloads, no new runtime.** Model listing reuses the
  Phase-1 ``probe_provider_health`` (``client.list()`` for Ollama —
  never a pull or a generation); the only strings surfaced are model
  names and the provider's own short, non-sensitive health detail.

It is the foundation under the admin-only ``GET
/admin/provider/models`` and ``POST /admin/provider/default-model``
endpoints.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Host-wide ``settings`` key holding the admin-selected default model.
#: Not in ``core.settings.USER_SETTING_KEYS`` on purpose — it is global,
#: not per-user — and not in ``config.ALLOWED_SETTINGS`` so it can never
#: be written through the generic ``/settings`` path that would bypass
#: the provider-list validation below.
DEFAULT_MODEL_SETTING_KEY = "default_model"

#: Defensive cap on the persisted model id. Mirrors the admin model-pull
#: request bound so a crafted client can never smuggle a large blob into
#: the settings row even though the value is also validated against the
#: provider's reported list.
MAX_MODEL_NAME_LEN = 200


def config_default_model() -> str:
    """Nova's compiled-in default chat model (``config.MODELS["default"]``).

    Best-effort and never raises: a missing / oddly-shaped ``MODELS``
    degrades to ``""`` rather than turning a read into a failure.
    """
    try:
        from config import MODELS

        return str((MODELS or {}).get("default", "") or "")
    except Exception as exc:  # pragma: no cover - config is stable
        logger.warning("default model: config lookup failed: %s", exc)
        return ""


def resolve_default_model() -> str:
    """The model Nova uses by default: persisted admin choice, else config.

    Safe to call from the chat hot path and from any thread: it performs
    **no network I/O** and never raises. The persisted value is only ever
    written by :func:`set_default_model` after it has been validated
    against the active provider's reported model list, so a non-empty
    stored value is one the provider listed at the time it was chosen.
    A missing / blank setting (the default for every existing install)
    transparently falls back to ``config.MODELS["default"]`` — existing
    deployments behave exactly as before.
    """
    config_default = config_default_model()
    try:
        from core.settings import get_system_setting

        saved = (get_system_setting(DEFAULT_MODEL_SETTING_KEY, "") or "").strip()
    except Exception as exc:  # never block chat on a settings read
        logger.debug("default model: settings read failed: %s", exc)
        return config_default
    return saved or config_default


def _provider_health() -> dict:
    """Read-only liveness + model list for the active provider.

    Delegates to the Phase-1 ``probe_provider_health`` so listing and
    validation reuse exactly one read-only path (``client.list()`` for
    Ollama — never a pull, never a generation) and one stable shape:
    ``{"ok": bool, "provider": str, "detail": str, "models": [str]}``.
    Never raises.
    """
    from core.provider_status import probe_provider_health

    health = probe_provider_health()
    models = health.get("models") or []
    return {
        "ok": bool(health.get("ok")),
        "provider": str(health.get("provider") or "unknown"),
        "detail": str(health.get("detail") or ""),
        "models": [m for m in models if isinstance(m, str) and m],
    }


def list_available_models() -> dict:
    """Read-only view: what models the active provider reports + the default.

    Returns a JSON-serialisable dict the admin UI renders verbatim::

        {
          "ok": bool,                # provider reachable
          "provider": str,           # active backend label
          "detail": str,             # provider's short health detail
          "models": [str],           # installed models (empty if down)
          "default_model": str,      # what Nova uses by default now
          "config_default_model": str,  # the compiled-in default
          "is_custom": bool,         # default differs from config
        }

    Never raises and never reaches a model runtime — an unreachable
    provider is reported as ``ok=False`` with an empty model list, not
    an exception, the same calm stance as the Phase-1 status surface.
    """
    health = _provider_health()
    default_model = resolve_default_model()
    config_default = config_default_model()
    return {
        "ok": health["ok"],
        "provider": health["provider"],
        "detail": health["detail"],
        "models": health["models"],
        "default_model": default_model,
        "config_default_model": config_default,
        "is_custom": bool(default_model) and default_model != config_default,
    }


class DefaultModelError(Exception):
    """A requested default-model change was refused.

    Carries a short, **sanitised** reason safe to surface to the admin
    UI: it never echoes back a raw provider exception, the configured
    host, or the caller-supplied string verbatim.
    """


def set_default_model(model: str) -> dict:
    """Validate ``model`` against the active provider, then persist it.

    The model must be a non-empty, reasonably short string **and** appear
    in the active provider's currently-reported model list. The provider
    is always the configured backend — no provider name is accepted from
    the caller. On success the choice is written to the global
    ``settings`` table and the new state is returned; any failure raises
    :class:`DefaultModelError` with a sanitised message and **writes
    nothing**:

    * empty / non-string / over-:data:`MAX_MODEL_NAME_LEN` → refused;
    * provider unreachable (its model list can't be verified) → refused
      with a fixed, non-sensitive message (no raw transport detail, no
      host);
    * model not in the provider's reported list → refused.

    Returns ``{"ok": True, "default_model", "config_default_model",
    "is_custom", "provider", "models"}`` on success.
    """
    if not isinstance(model, str):
        raise DefaultModelError("A model name is required.")
    candidate = model.strip()
    if not candidate:
        raise DefaultModelError("A model name is required.")
    if len(candidate) > MAX_MODEL_NAME_LEN:
        raise DefaultModelError("Model name is too long.")

    health = _provider_health()
    if not health["ok"]:
        # Deliberately do NOT echo health["detail"] here: a raw transport
        # error can carry the configured host. The Phase-1 status surface
        # already shows a redacted host; the *write* path stays maximally
        # conservative and says nothing about why the backend is down.
        raise DefaultModelError(
            "The active model provider is unreachable, so the requested "
            "model could not be verified against its installed models. "
            "No change was made."
        )

    models = health["models"]
    if candidate not in models:
        # Don't reflect the rejected string back; name the provider
        # (already operator-visible via Phase 1) and point at the list.
        raise DefaultModelError(
            f"That model is not installed on the active provider "
            f"({health['provider']}). Choose one of the models the "
            f"provider reports."
        )

    from core.settings import save_system_setting

    save_system_setting(DEFAULT_MODEL_SETTING_KEY, candidate)

    config_default = config_default_model()
    return {
        "ok": True,
        "default_model": candidate,
        "config_default_model": config_default,
        "is_custom": candidate != config_default,
        "provider": health["provider"],
        "models": models,
    }


__all__ = [
    "DEFAULT_MODEL_SETTING_KEY",
    "MAX_MODEL_NAME_LEN",
    "DefaultModelError",
    "config_default_model",
    "resolve_default_model",
    "list_available_models",
    "set_default_model",
]
