"""Opt-in, background model bootstrap (off by default).

Nova assumes the operator installs their own models and never downloads
one implicitly. This module is the *single* exception, and only when the
operator explicitly turns it on:

    NOVA_AUTO_PULL_MODELS=true
    NOVA_BOOTSTRAP_MODELS=gemma3:1b,...

When (and only when) auto-pull is enabled, :func:`maybe_bootstrap_models`
pulls each named model **in the background** (a daemon thread), **logged**,
and **without blocking** app startup. With auto-pull off — the default —
it returns immediately and downloads nothing, so a fresh install performs
no downloads until the operator asks for one.

It reuses ``core.model_pulls.request_pull``, which already:

  * validates the model name against a strict allowlist (no shell
    metacharacters, no path traversal) *before* any Ollama call,
  * speaks HTTP to the local daemon — never a subprocess, never a shell,
  * streams progress to the ``model_pulls`` table, and
  * runs the actual download on its own daemon thread.

Anything already installed or already pulling is a no-op. Every failure
is caught and logged — a bad bootstrap value can never crash Nova.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)


def _bootstrap_worker(models: tuple[str, ...]) -> None:
    """Request a pull for each bootstrap model, one at a time.

    Runs on a daemon thread. ``request_pull`` itself dispatches the heavy
    download onto another daemon thread and returns quickly, but it also
    does a small pre-pull size probe (a network call), so the whole loop
    lives off the request/startup path. Errors are per-model and never
    propagate.
    """
    from core import model_pulls

    for name in models:
        try:
            model_pulls.request_pull(name)
            logger.info("model bootstrap: requested pull for %r", name)
        except model_pulls.ModelAlreadyInstalled:
            logger.info(
                "model bootstrap: %r already installed — skipping", name
            )
        except model_pulls.PullAlreadyInProgress:
            logger.info(
                "model bootstrap: %r already pulling — skipping", name
            )
        except model_pulls.InvalidModelName as exc:
            logger.warning(
                "model bootstrap: skipping invalid model name %r (%s)",
                name,
                exc,
            )
        except Exception as exc:  # noqa: BLE001 — never crash startup
            logger.warning(
                "model bootstrap: pull request for %r failed: %s", name, exc
            )


def maybe_bootstrap_models() -> bool:
    """Kick off the opt-in bootstrap if enabled. Returns True if started.

    Non-blocking: the work runs on a daemon thread so app startup is never
    delayed, even by the pre-pull size probe. A no-op (returns ``False``)
    when ``NOVA_AUTO_PULL_MODELS`` is off or no bootstrap models are
    configured. Safe to call unconditionally from app startup.
    """
    try:
        from config import NOVA_AUTO_PULL_MODELS, NOVA_BOOTSTRAP_MODELS
    except Exception as exc:  # pragma: no cover - config is stable
        logger.warning("model bootstrap: config import failed: %s", exc)
        return False

    if not NOVA_AUTO_PULL_MODELS:
        logger.debug(
            "model bootstrap: NOVA_AUTO_PULL_MODELS is off — no downloads"
        )
        return False

    models = tuple(NOVA_BOOTSTRAP_MODELS)
    if not models:
        logger.info(
            "model bootstrap: auto-pull is on but NOVA_BOOTSTRAP_MODELS is "
            "empty — nothing to pull"
        )
        return False

    logger.info(
        "model bootstrap: auto-pull enabled; bootstrapping %s in the "
        "background",
        list(models),
    )
    threading.Thread(
        target=_bootstrap_worker,
        args=(models,),
        name="nova-model-bootstrap",
        daemon=True,
    ).start()
    return True


__all__ = ["maybe_bootstrap_models"]
