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
# via NOVA_SILENTGUARD_PATH (used by core.security_feed). Recent
# SilentGuard builds may also expose an optional loopback-only read
# API; setting NOVA_SILENTGUARD_API_URL switches the read-only security
# provider to probe that endpoint instead. Both transports remain
# read-only and the integration stays off until the user opts in.
# NexaNote is reached over HTTP. All integration switches are per-user
# and default to disabled; nothing is contacted until the user opts in.
NEXANOTE_API_URL = os.getenv("NEXANOTE_API_URL", "").strip().rstrip("/")
NEXANOTE_API_TOKEN = os.getenv("NEXANOTE_API_TOKEN", "").strip()
NEXANOTE_TIMEOUT_SECONDS = float(os.getenv("NEXANOTE_TIMEOUT_SECONDS", "3.0"))

# ── Optional GitHub connector (read-only by default) ───────────────
# A small, opt-in bridge that lets Nova help maintainers *read* the
# state of a repository (issues, pull requests, basic metadata) from
# the GitHub REST API. Nova is a local maintainer assistant, not an
# autonomous bot: this v1 ships read-only methods only. Writes,
# auto-merge, comments, and any background polling are out of scope.
#
# Every switch defaults to OFF so an unconfigured Nova install never
# contacts GitHub. When ``NOVA_GITHUB_ENABLED`` is on but
# ``NOVA_GITHUB_TOKEN`` is missing the status endpoint reports
# ``not_configured`` — Nova does not fall back to anonymous access,
# both to avoid the strict unauthenticated rate limit and to make the
# "no token configured" state visible to the admin.
#
# ``NOVA_GITHUB_TOKEN`` must never be:
#   * returned in any HTTP response body,
#   * embedded in any log line,
#   * included in any error message surfaced to the frontend / chat,
#   * persisted to the database in this PR.
#
#   NOVA_GITHUB_ENABLED       — host-wide opt-in. False → connector
#                               disabled for everyone.
#   NOVA_GITHUB_TOKEN         — personal-access / fine-grained token
#                               with read-only scopes (``repo:read`` /
#                               ``read:org`` is enough for v1).
#   NOVA_GITHUB_DEFAULT_REPO  — optional ``owner/name`` used when an
#                               endpoint is called without an explicit
#                               repo. Empty disables the fallback.
#   NOVA_GITHUB_READ_ONLY     — belt-and-braces switch. Defaults to
#                               True; v1 never performs writes so the
#                               flag is purely informational today.
#                               Future write helpers will refuse when
#                               this is True.
#   NOVA_GITHUB_TIMEOUT_SECONDS — per-request HTTP timeout (default 5).
NOVA_GITHUB_ENABLED = (
    os.getenv("NOVA_GITHUB_ENABLED", "false").strip().lower() == "true"
)
NOVA_GITHUB_TOKEN = os.getenv("NOVA_GITHUB_TOKEN", "").strip()
NOVA_GITHUB_DEFAULT_REPO = os.getenv("NOVA_GITHUB_DEFAULT_REPO", "").strip()
NOVA_GITHUB_READ_ONLY = (
    os.getenv("NOVA_GITHUB_READ_ONLY", "true").strip().lower() != "false"
)
try:
    NOVA_GITHUB_TIMEOUT_SECONDS = float(
        os.getenv("NOVA_GITHUB_TIMEOUT_SECONDS", "5.0")
    )
except ValueError:
    NOVA_GITHUB_TIMEOUT_SECONDS = 5.0

# SilentGuard local read-only HTTP API (optional; off by default).
# An empty value keeps the file-based probe in place. Setting a URL
# (typically loopback, e.g. http://127.0.0.1:8767) enables the HTTP
# probe in core.security.SilentGuardProvider. The transport stays
# read-only — only GET against a fixed path list is ever issued.
# ``NOVA_SILENTGUARD_API_BASE_URL`` is accepted as a synonym for the
# original ``NOVA_SILENTGUARD_API_URL`` so the documented config name
# matches the SilentGuard service contract.
NOVA_SILENTGUARD_API_URL = (
    os.getenv("NOVA_SILENTGUARD_API_BASE_URL")
    or os.getenv("NOVA_SILENTGUARD_API_URL", "")
).strip().rstrip("/")
try:
    NOVA_SILENTGUARD_API_TIMEOUT_SECONDS = float(
        os.getenv("NOVA_SILENTGUARD_API_TIMEOUT_SECONDS", "2.0")
    )
except ValueError:
    NOVA_SILENTGUARD_API_TIMEOUT_SECONDS = 2.0

# ── SilentGuard host-level lifecycle (optional auto-start) ──────────
# Server-level switches that gate Nova's optional, *non-privileged*
# starter for SilentGuard's local read-only API. Every switch defaults
# to "off" so unconfigured Nova installs never try to spawn anything.
#
# ``NOVA_SILENTGUARD_ENABLED``     — host-wide opt-in for the
#   integration's lifecycle helper. The per-user
#   ``silentguard_enabled`` setting still gates whether SilentGuard
#   data is surfaced to that user; this flag governs whether the
#   lifecycle helper may probe / auto-start at all.
# ``NOVA_SILENTGUARD_AUTO_START``  — when both this and the host-level
#   switch are true, the lifecycle helper may spawn the configured
#   start command after a failed probe. Defaults to off.
# ``NOVA_SILENTGUARD_START_MODE``  — selects the start backend.
#   ``"systemd-user"`` is the only allowed non-disabled value, on
#   purpose: it pins Nova to ``systemctl --user start <unit>``, which
#   never escalates privilege and never touches firewall config.
# ``NOVA_SILENTGUARD_SYSTEMD_UNIT`` — the user-level unit name to
#   start. Validated against a strict ``[a-z0-9._-]+\.service``
#   pattern; anything else is rejected before any spawn.
#
# See ``docs/silentguard-integration-roadmap.md`` for the safety
# rationale and the explicit non-goals (no sudo, no firewall, no
# system-level systemctl, no background polling).
NOVA_SILENTGUARD_ENABLED = (
    os.getenv("NOVA_SILENTGUARD_ENABLED", "false").strip().lower() == "true"
)
NOVA_SILENTGUARD_AUTO_START = (
    os.getenv("NOVA_SILENTGUARD_AUTO_START", "false").strip().lower() == "true"
)
NOVA_SILENTGUARD_START_MODE = (
    os.getenv("NOVA_SILENTGUARD_START_MODE", "disabled").strip().lower()
)
NOVA_SILENTGUARD_SYSTEMD_UNIT = (
    os.getenv("NOVA_SILENTGUARD_SYSTEMD_UNIT", "silentguard-api.service").strip()
)

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
