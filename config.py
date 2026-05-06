import os
from dotenv import load_dotenv

load_dotenv()

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")

_raw_channel = os.getenv("NOVA_CHANNEL", "stable").lower()
NOVA_CHANNEL = _raw_channel if _raw_channel in ("stable", "beta", "alpha") else "stable"
NOVA_BRANCH = os.getenv("NOVA_BRANCH", "main")
NOVA_ADMIN_UI = os.getenv("NOVA_ADMIN_UI", "false").lower() == "true"
# Automatic background RSS/web learning is off by default to avoid polluting the memory DB.
# Set NOVA_AUTO_WEB_LEARNING=true in .env to re-enable it.
NOVA_AUTO_WEB_LEARNING = os.getenv("NOVA_AUTO_WEB_LEARNING", "false").lower() == "true"

GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET", "")
GITHUB_OAUTH_REDIRECT_URI = os.getenv("GITHUB_OAUTH_REDIRECT_URI", "")

# ── Optional integrations (passive bridges) ─────────────────────────
# SilentGuard is read from a JSON file on disk; the path is overridable
# via NOVA_SILENTGUARD_PATH (used by core.security_feed). NexaNote is
# reached over HTTP. All integration switches are per-user and default
# to disabled; nothing is contacted until the user opts in.
NEXANOTE_API_URL = os.getenv("NEXANOTE_API_URL", "").strip().rstrip("/")
NEXANOTE_API_TOKEN = os.getenv("NEXANOTE_API_TOKEN", "").strip()
NEXANOTE_TIMEOUT_SECONDS = float(os.getenv("NEXANOTE_TIMEOUT_SECONDS", "3.0"))

# ── Optional local TTS: Piper ─────────────────────────────────────────
# Browser speechSynthesis remains the safe default. Piper is an opt-in
# local neural voice for hosts where the platform default sounds robotic
# (notably some Fedora/Linux desktops). When both NOVA_PIPER_BINARY and
# NOVA_PIPER_VOICE_MODEL resolve, the server can render audio locally;
# anything missing or broken falls back to the browser engine without
# raising. Models are never downloaded automatically — see README.
NOVA_PIPER_BINARY = os.getenv("NOVA_PIPER_BINARY", "").strip()
NOVA_PIPER_VOICE_MODEL = os.getenv("NOVA_PIPER_VOICE_MODEL", "").strip()
NOVA_PIPER_VOICE_CONFIG = os.getenv("NOVA_PIPER_VOICE_CONFIG", "").strip()
# Soft cap on synthesis time so a hung subprocess never wedges the API.
try:
    NOVA_PIPER_TIMEOUT_SECONDS = float(os.getenv("NOVA_PIPER_TIMEOUT_SECONDS", "20"))
except ValueError:
    NOVA_PIPER_TIMEOUT_SECONDS = 20.0
NOVA_ALPHA_ALLOWED_USERS: frozenset[str] = frozenset(
    u.strip().lower()
    for u in os.getenv("NOVA_ALPHA_ALLOWED_USERS", "").split(",")
    if u.strip()
)

MODELS = {
    "router":   "gemma3:1b",        # lightweight classifier, learner
    "default":  "gemma4",           # general chat, vision, memory extraction
    "code":     "deepseek-coder-v2",
    "advanced": "qwen2.5:32b",
}

NOVA_MODEL_DEFAULT_NAME = "nova-assistant"

NOVA_SYSTEM_PROMPT = "{memories}"

CHAT_HISTORY_LIMIT = 20

ALLOWED_SETTINGS = {
    "ram_budget": {"type": int, "min": 256, "max": 16384},
    "nova_model_enabled": {"type": str, "allowed": ["true", "false"]},
    "nova_model_name": {"type": str, "max_len": 100},
    "silentguard_enabled": {"type": str, "allowed": ["true", "false"]},
    "nexanote_enabled": {"type": str, "allowed": ["true", "false"]},
    "nexanote_write_enabled": {"type": str, "allowed": ["true", "false"]},
}
