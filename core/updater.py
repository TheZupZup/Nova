import logging
import sqlite3
import subprocess
import httpx
import ollama
from datetime import datetime
from config import MODELS
from core.ollama_client import client

logger = logging.getLogger(__name__)

# Deduplicated in case two roles share a model
TRACKED_MODELS = list(dict.fromkeys(MODELS.values()))


def get_local_model_digest(model_name: str) -> str:
    """Retourne le digest local d'un modèle."""
    try:
        models = client.list()
        for model in models.get("models", []):
            if not isinstance(model, dict):
                continue
            model_entry_name = model.get("name")
            if not model_entry_name:
                continue
            if model_entry_name.startswith(model_name):
                return model.get("digest", "")
    except (ConnectionError, OSError, ollama.ResponseError, httpx.HTTPError):
        pass
    return ""


def pull_model(model_name: str) -> bool:
    """Télécharge la dernière version d'un modèle."""
    try:
        logger.info("Checking for updates: %s", model_name)
        result = subprocess.run(
            ["ollama", "pull", model_name],
            capture_output=True,
            text=True,
            timeout=3600
        )
        if result.returncode != 0:
            logger.warning("Failed to pull model %s: %s", model_name, result.stderr.strip())
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError) as e:
        logger.warning("Failed to pull model %s: %s", model_name, e)
        return False


def check_and_update_models():
    """Vérifie et met à jour tous les modèles trackés."""
    logger.info("Model update check started at %s", datetime.now().isoformat())
    updated = []
    any_failed = False

    for model in TRACKED_MODELS:
        old_digest = get_local_model_digest(model)
        success = pull_model(model)

        if success:
            new_digest = get_local_model_digest(model)
            if old_digest != new_digest:
                logger.info("Updated: %s", model)
                updated.append(model)
            else:
                logger.debug("Already up to date: %s", model)
        else:
            any_failed = True

    if any_failed:
        logger.warning("Model update completed with partial failures")

    try:
        from core.memory import save_setting
        save_setting("last_model_update", datetime.now().isoformat())
        if updated:
            save_setting("last_updated_models", ", ".join(updated))
            logger.info("Models updated: %s", updated)
        else:
            save_setting("last_updated_models", "All up to date")
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
        logger.warning("Failed to save model update timestamp: %s", e)

    logger.info("Model update check done.")
    return updated
