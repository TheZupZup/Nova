import subprocess
import json
from datetime import datetime
from config import MODELS
from core.ollama_client import client

# Deduplicated in case two roles share a model
TRACKED_MODELS = list(dict.fromkeys(MODELS.values()))


def get_local_model_digest(model_name: str) -> str:
    """Retourne le digest local d'un modèle."""
    try:
        models = client.list()
        for model in models.get("models", []):
            if model["name"].startswith(model_name):
                return model.get("digest", "")
    except Exception:
        pass
    return ""


def pull_model(model_name: str) -> bool:
    """Télécharge la dernière version d'un modèle."""
    try:
        print(f"Checking for updates: {model_name}")
        result = subprocess.run(
            ["ollama", "pull", model_name],
            capture_output=True,
            text=True,
            timeout=3600
        )
        return result.returncode == 0
    except Exception as e:
        print(f"Error pulling {model_name}: {e}")
        return False


def check_and_update_models():
    """Vérifie et met à jour tous les modèles trackés."""
    print(f"Model update check started at {datetime.now().isoformat()}")
    updated = []

    for model in TRACKED_MODELS:
        old_digest = get_local_model_digest(model)
        success = pull_model(model)

        if success:
            new_digest = get_local_model_digest(model)
            if old_digest != new_digest:
                print(f"Updated: {model}")
                updated.append(model)
            else:
                print(f"Already up to date: {model}")

    # Sauvegarde toujours la date de vérification
    try:
        from core.memory import save_setting
        save_setting("last_model_update", datetime.now().isoformat())
        if updated:
            save_setting("last_updated_models", ", ".join(updated))
            print(f"Models updated: {updated}")
        else:
            save_setting("last_updated_models", "All up to date")
    except Exception:
        pass

    print("Model update check done.")
    return updated
