"""Read-only model-map status for operators.

Answers the question an operator asks about Nova's model configuration:
*which model fills each role, is the backend reachable, which models are
installed, and which configured ones are missing?* — all without a single
write, download, or generation.

It composes two existing read-only pieces:

  * ``config.MODELS`` — the operator-configured role→model map, and
  * ``core.provider_status.probe_provider_health`` — the backend's own
    cheap ``health()`` probe (``client.list()`` for Ollama; never a pull,
    never a generation).

Never raises into the caller and never reaches a model runtime — the same
calm "report it, don't crash" stance as ``core.provider_status`` /
``core.storage_status``. An unreachable backend is reported as data
(``reachable=false``), not an exception, and ``missing_models`` is left
empty because an unreachable backend cannot tell us what is installed.

It is the read-only foundation under the admin-only
``GET /admin/models/status`` endpoint.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: The four routing roles Nova fills from the operator-configured map.
ROLES = ("router", "default", "code", "advanced")


def get_model_status() -> dict:
    """Return a JSON-serialisable snapshot of the model map + backend state.

    Shape::

        {
          "provider": str,             # active backend label
          "reachable": bool,           # backend answered a read-only probe
          "detail": str,               # backend's short health detail
          "model_map": {role: name},   # configured role -> model
          "configured_models": [str],  # distinct configured model names
          "installed_models": [str],   # models the backend reports
          "missing_models": [str],     # configured but not installed
                                       #   (empty when unreachable)
          "auto_pull_models": bool,    # NOVA_AUTO_PULL_MODELS
          "auto_update_models": bool,  # NOVA_AUTO_UPDATE_MODELS
          "bootstrap_models": [str],   # NOVA_BOOTSTRAP_MODELS
        }
    """
    from core.provider_status import probe_provider_health

    try:
        from config import (
            MODELS,
            NOVA_AUTO_PULL_MODELS,
            NOVA_AUTO_UPDATE_MODELS,
            NOVA_BOOTSTRAP_MODELS,
        )
    except Exception as exc:  # pragma: no cover - config is stable
        logger.warning("model status: config import failed: %s", exc)
        MODELS = {}
        NOVA_AUTO_PULL_MODELS = False
        NOVA_AUTO_UPDATE_MODELS = False
        NOVA_BOOTSTRAP_MODELS = ()

    model_map = {
        role: str((MODELS or {}).get(role, "") or "") for role in ROLES
    }

    # Read-only liveness + installed-model list. Never raises; stable
    # ``{ok, provider, detail, models}`` shape even when the backend is
    # down or the configured provider is unknown.
    health = probe_provider_health()
    reachable = bool(health.get("ok"))
    installed = sorted(
        {m for m in (health.get("models") or []) if isinstance(m, str) and m}
    )
    installed_set = set(installed)

    # Distinct, sorted configured model names (a role may reuse a model).
    configured = sorted({name for name in model_map.values() if name})

    # "Missing" is only meaningful when the backend answered — an
    # unreachable backend cannot report what is installed, so claiming a
    # model is missing would be misleading.
    missing = (
        [name for name in configured if name not in installed_set]
        if reachable
        else []
    )

    return {
        "provider": str(health.get("provider") or "unknown"),
        "reachable": reachable,
        "detail": str(health.get("detail") or ""),
        "model_map": model_map,
        "configured_models": configured,
        "installed_models": installed,
        "missing_models": missing,
        "auto_pull_models": bool(NOVA_AUTO_PULL_MODELS),
        "auto_update_models": bool(NOVA_AUTO_UPDATE_MODELS),
        "bootstrap_models": list(NOVA_BOOTSTRAP_MODELS),
    }


__all__ = ["ROLES", "get_model_status"]
