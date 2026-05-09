import time
import secrets as _secrets
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, field_validator

from core.auth import (
    CurrentUser,
    authenticate,
    create_token,
    load_current_user,
)
from core.rate_limiter import check_login_rate_limit
from apscheduler.schedulers.background import BackgroundScheduler
from core.learner import learn_from_feeds
from core.updater import check_and_update_models
from core.chat import chat
from core.memory_command import handle_manual_memory_command
from core.session_continuity import build_session_continuity
from core.memory import (
    initialize_db, load_memories, save_memory,
    create_conversation, load_conversations,
    load_conversation_messages, save_message,
    delete_conversation, update_conversation_title,
    conversation_belongs_to,
    get_setting, save_setting,
    list_memories, update_memory, delete_memory,
)
from core.settings import (
    get_user_setting, save_user_setting,
    get_personalization,
    validate_personalization_value,
    CUSTOM_INSTRUCTIONS_MAX_LEN,
    PERSONALIZATION_ENUMS,
)
from core.policies import (
    KNOWN_MODES,
    PolicyDenial,
    check_chat_request,
    enforce_daily_limit,
    get_family_controls_dict,
    get_policy,
    set_family_controls,
)
from core.model_registry import (
    list_registered as list_registered_models,
    reconcile_installed as reconcile_installed_models,
)
from core import model_pulls as _model_pulls
from core import model_access as _model_access
from core import local_models as _local_models
from core.ollama_client import OllamaUnavailable
from core.integrations import silentguard as _silentguard_integration
from core.integrations import nexanote as _nexanote_integration
from core.security import ensure_silentguard_running as _ensure_silentguard_running
from core.security import lifecycle as _silentguard_lifecycle
from core.security import SilentGuardProvider as _SilentGuardProvider
from core import voice as _voice
import sqlite3 as _sqlite3
from core import users as _users_mod
from memory.store import (
    list_memories as list_natural_memories,
    delete_memories_matching,
)
from config import (
    MODELS, ALLOWED_SETTINGS, NOVA_MODEL_DEFAULT_NAME,
    NOVA_CHANNEL, NOVA_BRANCH,
    GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, GITHUB_OAUTH_REDIRECT_URI,
    NOVA_AUTO_WEB_LEARNING,
)
from core.github_oauth import build_auth_url, exchange_code, fetch_username, is_allowed

# ── ALPHA CHANNEL: SESSION STORE ────────────────────────────────────────────
# In-memory store keyed by a random, opaque session ID (256-bit entropy).
# Only the GitHub username is persisted — the OAuth token is never stored.
# Limitation: sessions are cleared on server restart and are not shared
# across multiple workers or containers. Acceptable for a single-process
# Alpha instance; replace with a persistent store for multi-worker setups.
_sessions: dict[str, dict] = {}
_SESSION_COOKIE = "nova_alpha_sess"
_SESSION_TTL = 28800  # 8 hours
_SECURE_COOKIE = GITHUB_OAUTH_REDIRECT_URI.startswith("https://")


def _session_read(request: Request) -> dict | None:
    sid = request.cookies.get(_SESSION_COOKIE)
    if not sid:
        return None
    entry = _sessions.get(sid)
    if not entry or entry["exp"] < time.time():
        _sessions.pop(sid, None)
        return None
    return entry["data"]


def _session_purge() -> None:
    """Remove all expired entries from the session store."""
    now = time.time()
    for sid in [s for s, e in _sessions.items() if e["exp"] < now]:
        del _sessions[sid]


def _session_create(data: dict) -> str:
    _session_purge()
    sid = _secrets.token_urlsafe(32)
    _sessions[sid] = {"data": data, "exp": time.time() + _SESSION_TTL}
    return sid


def _session_destroy(request: Request) -> None:
    sid = request.cookies.get(_SESSION_COOKIE)
    if sid:
        _sessions.pop(sid, None)


def _set_cookie(response, sid: str) -> None:
    response.set_cookie(
        _SESSION_COOKIE, sid,
        httponly=True,
        secure=_SECURE_COOKIE,
        samesite="lax",
        max_age=_SESSION_TTL,
    )


def _access_denied_page(username: str) -> str:
    return (
        "<!DOCTYPE html><html><head><title>Access Denied — Nova</title>"
        "<style>*{box-sizing:border-box;margin:0;padding:0}"
        "body{background:#0d0d0d;color:#e0e0e0;font-family:monospace;"
        "display:flex;align-items:center;justify-content:center;height:100dvh}"
        ".box{text-align:center;border:1px solid #222;border-radius:12px;"
        "padding:32px 40px;background:#111}"
        "h1{color:#f44336;font-size:1rem;letter-spacing:2px;margin-bottom:12px}"
        "p{color:#666;font-size:0.85rem;margin:6px 0}"
        "a{color:#00bcd4;text-decoration:none;font-size:0.8rem}"
        "</style></head><body><div class='box'>"
        "<h1>⬡ ACCESS DENIED</h1>"
        f"<p>@{username} is not authorised to access this instance.</p>"
        "<p style='margin-top:16px'><a href='/auth/logout'>← Sign out</a></p>"
        "</div></body></html>"
    )


security = HTTPBearer()

MODE_MAP = {
    "chat": MODELS["default"],
    "code": MODELS["code"],
    "deep": MODELS["advanced"],
}


scheduler = BackgroundScheduler()
if NOVA_AUTO_WEB_LEARNING:
    scheduler.add_job(learn_from_feeds, "interval", hours=1)
scheduler.add_job(check_and_update_models, "interval", weeks=1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize_db()
    scheduler.start()
    if NOVA_AUTO_WEB_LEARNING:
        learn_from_feeds()
    yield
    scheduler.shutdown()


app = FastAPI(docs_url=None, redoc_url=None, lifespan=lifespan)


# ── ALPHA CHANNEL GUARD ─────────────────────────────────────────────
@app.middleware("http")
async def alpha_channel_guard(request: Request, call_next):
    if NOVA_CHANNEL != "alpha":
        return await call_next(request)

    # OAuth flow paths are always open
    if request.url.path.startswith("/auth/"):
        return await call_next(request)

    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        return HTMLResponse(
            "<h1 style='font-family:monospace;color:#f44336'>"
            "Alpha channel requires GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET.</h1>",
            status_code=503,
        )

    sess = _session_read(request)
    github_user = sess.get("github_user") if sess else None

    if not github_user:
        # API callers with a Bearer token get a JSON 401, not a redirect
        if request.headers.get("authorization"):
            return JSONResponse({"detail": "GitHub authentication required."}, status_code=401)
        return RedirectResponse("/auth/github")

    if not is_allowed(github_user):
        return HTMLResponse(_access_denied_page(github_user), status_code=403)

    return await call_next(request)


# ── GITHUB OAUTH ROUTES ────────────────────────────────────────────
@app.get("/auth/github")
async def auth_github(request: Request):
    state = _secrets.token_urlsafe(16)
    sid = _session_create({"oauth_state": state})
    response = RedirectResponse(build_auth_url(state), status_code=302)
    _set_cookie(response, sid)
    return response


@app.get("/auth/github/callback")
async def auth_github_callback(
    request: Request,
    code: str = "",
    state: str = "",
):
    sess = _session_read(request)
    if not sess or not state or sess.get("oauth_state") != state:
        return HTMLResponse("Invalid or expired OAuth state. Please try again.", status_code=400)

    token = await exchange_code(code)
    if not token:
        return HTMLResponse("Failed to obtain access token from GitHub.", status_code=400)

    username = await fetch_username(token)
    # Token is used once and immediately discarded — never stored or logged
    if not username:
        return HTMLResponse("Failed to verify GitHub identity.", status_code=400)

    # Upgrade the pending session to an authenticated one
    sid = request.cookies.get(_SESSION_COOKIE, "")
    if sid in _sessions:
        _sessions[sid]["data"] = {"github_user": username}
        _sessions[sid]["exp"] = time.time() + _SESSION_TTL

    response = RedirectResponse("/", status_code=302)
    return response


@app.get("/auth/logout")
async def auth_logout(request: Request):
    _session_destroy(request)
    response = RedirectResponse("/auth/github" if NOVA_CHANNEL == "alpha" else "/", status_code=302)
    response.delete_cookie(_SESSION_COOKIE)
    return response


class LoginRequest(BaseModel):
    username: str
    password: str


class ChatRequest(BaseModel):
    message: str
    conversation_id: int | None = None
    mode: str = "auto"
    search: bool = False
    image: str | None = Field(default=None, max_length=13_000_000)


class NewConversationRequest(BaseModel):
    title: str = "Nouvelle conversation"


class MemoryUpdateRequest(BaseModel):
    category: str
    content: str


class MemoryAddRequest(BaseModel):
    category: str
    content: str


class SettingsUpdateRequest(BaseModel):
    model_config = {"extra": "forbid"}

    ram_budget: int | None = None
    nova_model_enabled: bool | None = None
    nova_model_name: str | None = None
    silentguard_enabled: bool | None = None
    nexanote_enabled: bool | None = None
    nexanote_write_enabled: bool | None = None

    # Personalization preferences (per-user). Enum fields are validated
    # against PERSONALIZATION_ENUMS so the DB never carries a value the
    # UI can't render; custom_instructions is length-capped server-side
    # to match the textarea's maxlength.
    response_style: str | None = None
    warmth_level: str | None = None
    enthusiasm_level: str | None = None
    emoji_level: str | None = None
    custom_instructions: str | None = None

    @field_validator("ram_budget")
    @classmethod
    def validate_ram_budget(cls, v):
        if v is not None:
            spec = ALLOWED_SETTINGS["ram_budget"]
            if not (spec["min"] <= v <= spec["max"]):
                raise ValueError(f"must be between {spec['min']} and {spec['max']}")
        return v

    @field_validator("nova_model_name")
    @classmethod
    def validate_nova_model_name(cls, v):
        if v is not None:
            v = v.strip()
            if not v:
                raise ValueError("model name cannot be empty")
            if len(v) > ALLOWED_SETTINGS["nova_model_name"]["max_len"]:
                raise ValueError("model name too long (max 100 characters)")
        return v

    @field_validator(
        "response_style", "warmth_level", "enthusiasm_level", "emoji_level"
    )
    @classmethod
    def validate_personalization_enum(cls, v, info):
        if v is None:
            return v
        allowed = PERSONALIZATION_ENUMS[info.field_name]
        if v not in allowed:
            raise ValueError(
                f"must be one of {sorted(allowed)}"
            )
        return v

    @field_validator("custom_instructions")
    @classmethod
    def validate_custom_instructions(cls, v):
        if v is None:
            return v
        if not isinstance(v, str):
            raise ValueError("must be a string")
        v = v.strip()
        if len(v) > CUSTOM_INSTRUCTIONS_MAX_LEN:
            raise ValueError(
                f"must be {CUSTOM_INSTRUCTIONS_MAX_LEN} characters or fewer"
            )
        return v


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> CurrentUser:
    user = load_current_user(credentials.credentials)
    if user is None:
        raise HTTPException(status_code=401, detail="Token invalide ou expiré.")
    return user


def _raise_policy_denial(denial: PolicyDenial) -> None:
    raise HTTPException(
        status_code=denial.status_code,
        detail=denial.detail,
        headers=denial.headers or None,
    )


@app.post("/login")
def login(request: LoginRequest, _: None = Depends(check_login_rate_limit)):
    user = authenticate(request.username, request.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Identifiants incorrects.")
    return {"token": create_token(user)}


@app.get("/conversations")
def get_conversations(user: CurrentUser = Depends(get_current_user)):
    return load_conversations(user.id)


@app.get("/session-continuity")
def get_session_continuity(
    exclude: int | None = None,
    user: CurrentUser = Depends(get_current_user),
):
    """Return a small, deterministic summary of recent local activity.

    See ``core/session_continuity.py`` for the design notes. The
    endpoint never invents content: if there is nothing meaningful in
    the user's recent conversations, ``has_continuity`` is False and
    the UI shows nothing.
    """
    return build_session_continuity(user.id, exclude_conversation_id=exclude)


@app.post("/conversations")
def new_conversation(
    request: NewConversationRequest,
    user: CurrentUser = Depends(get_current_user),
):
    conv_id = create_conversation(request.title, user.id)
    return {"id": conv_id, "title": request.title}


@app.get("/conversations/{conversation_id}/messages")
def get_messages(
    conversation_id: int,
    user: CurrentUser = Depends(get_current_user),
):
    messages = load_conversation_messages(conversation_id, user.id)
    if messages is None:
        # 404 (not 403) so cross-user access cannot probe for existence.
        raise HTTPException(status_code=404, detail="Conversation introuvable.")
    return messages


@app.delete("/conversations/{conversation_id}")
def remove_conversation(
    conversation_id: int,
    user: CurrentUser = Depends(get_current_user),
):
    if not delete_conversation(conversation_id, user.id):
        raise HTTPException(status_code=404, detail="Conversation introuvable.")
    return {"ok": True}


@app.post("/chat")
def chat_endpoint(request: ChatRequest, user: CurrentUser = Depends(get_current_user)):
    policy = get_policy(user)

    denial = check_chat_request(
        policy,
        mode=request.mode,
        message=request.message,
        requested_search=request.search,
    )
    if denial is not None:
        _raise_policy_denial(denial)

    # Per-user / per-role mode access (#112). Layered on top of the base
    # policy so a crafted request for a mode the admin has scoped away
    # from this account is refused even if the family-controls row would
    # otherwise allow it.
    access_denial = _model_access.check_mode_access(user, request.mode)
    if access_denial is not None:
        raise HTTPException(
            status_code=access_denial.status_code,
            detail=access_denial.detail,
        )

    denial = enforce_daily_limit(policy, user.id)
    if denial is not None:
        _raise_policy_denial(denial)

    memories = load_memories(user.id)

    msg_lower = request.message.lower().strip()
    is_manual_memory_command = any(
        msg_lower.startswith(p)
        for p in ("retiens ça:", "souviens-toi de ça:", "souviens-toi:")
    )
    if is_manual_memory_command and not policy.memory_save_enabled:
        raise HTTPException(
            status_code=403,
            detail="Memory saving is disabled for this account.",
        )

    reply = handle_manual_memory_command(request.message, user.id)
    if reply is not None:
        return {"response": reply, "model": "system", "conversation_id": request.conversation_id}

    if msg_lower.startswith("forget that ") or msg_lower.startswith("oublie que "):
        query = request.message.split(" ", 2)[2].strip()
        count = delete_memories_matching(query, user.id)
        reply = f"Done. Removed {count} memory(ies) matching '{query}'." if count else "No matching memories found."
        return {"response": reply, "model": "system", "conversation_id": request.conversation_id}

    if msg_lower.startswith("forget everything about ") or msg_lower.startswith("oublie tout sur "):
        query = request.message.split(" ", 3)[-1].strip()
        count = delete_memories_matching(query, user.id)
        reply = f"Done. Removed {count} memory(ies) about '{query}'." if count else "No matching memories found."
        return {"response": reply, "model": "system", "conversation_id": request.conversation_id}

    if msg_lower in (
        "what do you remember about me?", "show my memories", "show memories",
        "what do you know about me?", "que sais-tu de moi ?", "que sais-tu de moi?",
        "montre mes souvenirs", "montre-moi mes souvenirs",
    ):
        mems = list_natural_memories(user.id)
        if not mems:
            return {"response": "I don't have any natural memories stored yet.", "model": "system", "conversation_id": request.conversation_id}
        lines = ["Here's what I remember about you:\n"]
        for m in mems:
            lines.append(f"- [{m.kind}/{m.topic}] {m.content}")
        return {"response": "\n".join(lines), "model": "system", "conversation_id": request.conversation_id}

    conversation_id = request.conversation_id
    if conversation_id:
        if not conversation_belongs_to(conversation_id, user.id):
            raise HTTPException(status_code=404, detail="Conversation introuvable.")
    else:
        conversation_id = create_conversation(request.message[:40], user.id)

    history = [
        {"role": m["role"], "content": m["content"]}
        for m in load_conversation_messages(conversation_id, user.id) or []
    ]

    nova_enabled = get_user_setting(user.id, "nova_model_enabled", "false") == "true"
    if nova_enabled:
        forced_model = get_user_setting(
            user.id, "nova_model_name", NOVA_MODEL_DEFAULT_NAME
        )
    else:
        forced_model = MODE_MAP.get(request.mode)

    # Per-user / per-role model access (#112). Only validated when a
    # concrete forced_model is resolved (mode=auto leaves it None and
    # delegates to the router, which already picks from configured
    # models). The mode check above is the primary control for auto.
    if forced_model is not None:
        model_denial = _model_access.check_model_access(user, forced_model)
        if model_denial is not None:
            raise HTTPException(
                status_code=model_denial.status_code,
                detail=model_denial.detail,
            )

    response, model_used = chat(history, request.message, memories, user.id, forced_model=forced_model, force_search=request.search, image=request.image, policy=policy)

    save_message(conversation_id, "user", request.message)
    save_message(conversation_id, "assistant", response, model_used)

    if len(history) == 0:
        update_conversation_title(conversation_id, request.message[:40])

    return {
        "response": response,
        "model": model_used,
        "conversation_id": conversation_id
    }


# ── MEMORY ENDPOINTS ──

@app.get("/memories")
def get_memories(user: CurrentUser = Depends(get_current_user)):
    return list_memories(user.id)


@app.put("/memories/{memory_id}")
def update_memory_endpoint(
    memory_id: int,
    request: MemoryUpdateRequest,
    user: CurrentUser = Depends(get_current_user),
):
    if not update_memory(memory_id, request.category, request.content, user.id):
        # 404 (not 403) so cross-user access cannot probe for existence.
        raise HTTPException(status_code=404, detail="Mémoire introuvable.")
    return {"ok": True}


@app.delete("/memories/{memory_id}")
def delete_memory_endpoint(
    memory_id: int,
    user: CurrentUser = Depends(get_current_user),
):
    if not delete_memory(memory_id, user.id):
        raise HTTPException(status_code=404, detail="Mémoire introuvable.")
    return {"ok": True}


@app.post("/memories")
def add_memory(
    request: MemoryAddRequest,
    user: CurrentUser = Depends(get_current_user),
):
    policy = get_policy(user)
    if not policy.memory_save_enabled:
        raise HTTPException(
            status_code=403,
            detail="Memory saving is disabled for this account.",
        )
    save_memory(request.category, request.content, user.id)
    return {"ok": True}


@app.get("/settings")
def get_settings(user: CurrentUser = Depends(get_current_user)):
    # System-scoped values are global; user-scoped values are read for the
    # caller, so two users see two different `nova_model_*` payloads.
    payload = {
        "ram_budget": get_setting("ram_budget", "2048"),
        "last_model_update": get_setting("last_model_update", "Never"),
        "last_updated_models": get_setting("last_updated_models", ""),
        "nova_model_enabled": (
            get_user_setting(user.id, "nova_model_enabled", "false") == "true"
        ),
        "nova_model_name": get_user_setting(
            user.id, "nova_model_name", NOVA_MODEL_DEFAULT_NAME
        ),
        "silentguard_enabled": (
            get_user_setting(user.id, "silentguard_enabled", "false") == "true"
        ),
        "nexanote_enabled": (
            get_user_setting(user.id, "nexanote_enabled", "false") == "true"
        ),
        "nexanote_write_enabled": (
            get_user_setting(user.id, "nexanote_write_enabled", "false") == "true"
        ),
    }
    # Personalization is read for the caller and merged in flat; the
    # client renders one control per key without checking for missing
    # fields.
    payload.update(get_personalization(user.id))
    return payload


@app.post("/models/update")
def trigger_model_update(_: bool = Depends(get_current_user)):
    """Lance une vérification manuelle des mises à jour."""
    import threading
    thread = threading.Thread(target=check_and_update_models)
    thread.daemon = True
    thread.start()
    return {"ok": True, "message": "Update started in background"}


@app.post("/settings")
def update_settings(
    data: SettingsUpdateRequest,
    user: CurrentUser = Depends(get_current_user),
):
    # System-level settings can only be changed by admins; per-user
    # preferences are written under the caller's id and never affect
    # anyone else.
    if data.ram_budget is not None:
        if user.role != "admin":
            raise HTTPException(
                status_code=403,
                detail="Only admins can change system settings.",
            )
        save_setting("ram_budget", str(data.ram_budget))
    if data.nova_model_enabled is not None:
        save_user_setting(
            user.id,
            "nova_model_enabled",
            "true" if data.nova_model_enabled else "false",
        )
    if data.nova_model_name is not None:
        save_user_setting(user.id, "nova_model_name", data.nova_model_name)
    if data.silentguard_enabled is not None:
        save_user_setting(
            user.id,
            "silentguard_enabled",
            "true" if data.silentguard_enabled else "false",
        )
    if data.nexanote_enabled is not None:
        save_user_setting(
            user.id,
            "nexanote_enabled",
            "true" if data.nexanote_enabled else "false",
        )
    if data.nexanote_write_enabled is not None:
        save_user_setting(
            user.id,
            "nexanote_write_enabled",
            "true" if data.nexanote_write_enabled else "false",
        )
    # Personalization. Pydantic has already validated each field; the
    # second pass through validate_personalization_value is the canonical
    # check used by tests and any non-HTTP caller, and it normalises
    # custom_instructions consistently.
    for key in (
        "response_style",
        "warmth_level",
        "enthusiasm_level",
        "emoji_level",
        "custom_instructions",
    ):
        value = getattr(data, key)
        if value is None:
            continue
        save_user_setting(
            user.id, key, validate_personalization_value(key, value)
        )
    return {"ok": True}


# ── INTEGRATIONS ────────────────────────────────────────────────────
# Optional, per-user, opt-in bridges to external tools. Status is
# computed against the caller's own switches; one user enabling
# SilentGuard never affects another. The endpoint is read-only —
# mutations go through /settings.

@app.get("/integrations/status")
def integrations_status(user: CurrentUser = Depends(get_current_user)):
    """Per-integration availability snapshot for the caller."""
    return {
        "silentguard": _silentguard_integration.status(user.id).as_dict(),
        "nexanote": {
            **_nexanote_integration.status(user.id).as_dict(),
            "write_enabled": _nexanote_integration.is_write_enabled(user.id),
        },
    }


@app.get("/integrations/silentguard/lifecycle")
def silentguard_lifecycle(user: CurrentUser = Depends(get_current_user)):
    """SilentGuard read-only API lifecycle state for the caller.

    Surfaces — and, when the operator has explicitly opted in via
    ``NOVA_SILENTGUARD_AUTO_START`` *and* a ``systemd-user`` start
    mode, may attempt to start — the local SilentGuard read-only API
    service. The endpoint is gated by the per-user
    ``silentguard_enabled`` setting; users who have not opted in see
    a ``state="disabled"`` payload and no spawn is attempted.

    Read-only with one narrow exception: when every gate is on, the
    helper may run a single ``systemctl --user start <unit>`` against
    the configured unit. No ``sudo``, no firewall command, no shell
    interpretation, no remote URLs, no input from chat. See
    :mod:`core.security.lifecycle` and the SilentGuard roadmap for
    the full safety contract.
    """
    if not _silentguard_integration.is_enabled(user.id):
        return _silentguard_lifecycle.disabled_status().as_dict()
    return _ensure_silentguard_running().as_dict()


def _silentguard_summary_payload(user: CurrentUser) -> dict:
    """Build the SilentGuard Settings card payload for ``user``.

    Shared by the GET ``/summary`` read path and the POST
    ``/enable`` / ``/disable`` / ``/retry`` user-action endpoints so
    every caller surfaces the same shape (``lifecycle``, ``counts``,
    ``host_enabled``) without re-deriving the gating logic. The
    function is read-only with the single carve-out documented in
    :func:`core.security.lifecycle.ensure_running` — when the host
    operator has opted into ``systemd-user`` auto-start, the lifecycle
    helper may run a single ``systemctl --user start <unit>``.
    """
    host_enabled = _silentguard_lifecycle.host_enabled()
    if not _silentguard_integration.is_enabled(user.id):
        return {
            "lifecycle": _silentguard_lifecycle.disabled_status().as_dict(),
            "counts": None,
            "host_enabled": host_enabled,
        }
    lifecycle_status = _ensure_silentguard_running()
    counts = None
    if lifecycle_status.state == _silentguard_lifecycle.STATE_CONNECTED:
        try:
            counts = _SilentGuardProvider().get_summary_counts()
        except Exception:  # pragma: no cover — defensive belt-and-braces
            counts = None
    return {
        "lifecycle": lifecycle_status.as_dict(),
        "counts": counts,
        "host_enabled": host_enabled,
    }


@app.get("/integrations/silentguard/summary")
def silentguard_summary(user: CurrentUser = Depends(get_current_user)):
    """SilentGuard Settings status summary for the caller.

    One-call snapshot the Settings UI renders into a small calm status
    card: the same lifecycle state surfaced by
    ``/integrations/silentguard/lifecycle`` plus, when the read-only
    API is reachable, the four optional summary counts produced by
    :class:`SilentGuardProvider.get_summary_counts`.

    Stable shape::

        {
            "lifecycle": LifecycleStatus.as_dict(),
            "counts": {"alerts": int, "blocked": int,
                       "trusted": int, "connections": int} | None,
            "host_enabled": bool,
        }

    ``counts`` is ``None`` whenever the lifecycle state is anything
    other than ``connected``, when the HTTP transport is not
    configured (file-only fallback), or when the optional count probe
    fails. The endpoint never raises; every failure path maps to a
    calm payload the UI renders without alarm.

    ``host_enabled`` mirrors the host-level
    ``NOVA_SILENTGUARD_ENABLED`` switch *as Nova currently sees it*.
    The Settings UI uses it to tell the user *why* the integration is
    off when ``state="disabled"``: the host config is off, or the host
    config is on but the per-user toggle is still off. Without this
    hint the disabled headline conflates the two, and an operator who
    has correctly set the env vars cannot tell that they only need to
    flip their per-user toggle in Settings.

    Read-only and gated by the per-user ``silentguard_enabled``
    setting, exactly like the lifecycle endpoint. No background
    polling — the UI calls this once per Settings open / Refresh
    click, mirroring the trigger set documented in the roadmap.
    """
    return _silentguard_summary_payload(user)


@app.post("/integrations/silentguard/enable")
def silentguard_enable(user: CurrentUser = Depends(get_current_user)):
    """Persist ``silentguard_enabled=true`` for the caller.

    The Settings UI's "Enable SilentGuard" button calls this endpoint.
    After persisting the per-user opt-in, the lifecycle helper is
    invoked once so an operator who has also configured the safe
    ``systemd-user`` auto-start path sees the local read-only API
    come up immediately. The response shape is identical to
    ``GET /integrations/silentguard/summary`` so the UI can paint the
    new state without a follow-up request.

    Safety contract (unchanged from the rest of the integration):

      * only the per-user ``silentguard_enabled`` setting is mutated;
      * lifecycle handling is delegated to
        :func:`core.security.lifecycle.ensure_running`, which is the
        only code path allowed to spawn a process and which already
        forbids ``sudo`` / firewall actions / shell interpretation;
      * no payload is read from the request — there is nothing for the
        client to smuggle in.
    """
    save_user_setting(user.id, "silentguard_enabled", "true")
    return _silentguard_summary_payload(user)


@app.post("/integrations/silentguard/disable")
def silentguard_disable(user: CurrentUser = Depends(get_current_user)):
    """Persist ``silentguard_enabled=false`` for the caller.

    Stops Nova from using the SilentGuard integration for this user.
    The local SilentGuard service itself is **not** stopped — Nova
    deliberately does not offer a stop control for an external
    security tool (see the roadmap §8.4). Returns the same payload
    shape as ``/summary`` so the UI can repaint the disabled state in
    a single round-trip.
    """
    save_user_setting(user.id, "silentguard_enabled", "false")
    return _silentguard_summary_payload(user)


@app.post("/integrations/silentguard/retry")
def silentguard_retry(user: CurrentUser = Depends(get_current_user)):
    """Re-probe SilentGuard for the caller.

    Backs the "Retry" button surfaced by the Settings card when the
    integration is enabled but unreachable. No setting is mutated:
    the call simply re-runs the lifecycle helper, which probes the
    read-only API and — only when the operator has opted into the
    safe ``systemd-user`` start mode — may attempt the same single
    ``systemctl --user start <unit>`` ``ensure_running`` already
    documents. Returns the same payload as ``/summary``.
    """
    return _silentguard_summary_payload(user)


# ── VOICE / TTS ─────────────────────────────────────────────────────
# Opt-in "read aloud" surface on assistant replies. The server returns
# voice preferences, validates input, and (when a local engine is
# configured) renders WAV audio. The browser engine remains the safe
# default — no audio bytes ever leave the user's host on either path.
# Additional engines plug into `core/voice/providers.py` without
# changing this endpoint's shape.

# Engines the request body is allowed to ask for. Unknown values are
# rejected by Pydantic with a 422 before our handler runs.
_TTS_ALLOWED_ENGINES = (_voice.ENGINE_BROWSER, _voice.ENGINE_PIPER)


class TTSRequest(BaseModel):
    model_config = {"extra": "forbid"}
    text: str = Field(min_length=1, max_length=_voice.MAX_TTS_INPUT_CHARS)
    # Optional engine override. When omitted we use the server default
    # (browser), which preserves the original zero-config behaviour.
    engine: str | None = Field(default=None)

    @field_validator("engine")
    @classmethod
    def _engine(cls, v):
        if v is None:
            return v
        if v not in _TTS_ALLOWED_ENGINES:
            raise ValueError(
                f"engine must be one of {list(_TTS_ALLOWED_ENGINES)}"
            )
        return v


def _voice_config_payload() -> dict:
    """Build the /voice/config response.

    Always reports the server-default provider so the existing browser
    flow is unchanged. Adds ``available_engines`` and an optional
    ``piper`` block so the Settings UI can offer Piper as an opt-in
    engine when (and only when) it is fully configured on this host.
    """
    provider = _voice.get_default_provider()
    payload = {
        "available": provider.is_available(),
        **provider.voice_config().as_dict(),
        "available_engines": _voice.list_available_engines(),
    }
    piper = _voice.get_piper_provider()
    if piper is not None:
        # Surface the diagnostic block whether or not Piper resolves —
        # the UI uses ``status.available`` to decide if the option is
        # offered, and ``detail`` to show a calm hint when it isn't.
        payload["piper"] = piper.status().as_dict()
    return payload


@app.get("/voice/config")
def voice_config(_: CurrentUser = Depends(get_current_user)):
    """Return the active voice profile for the calling user."""
    return _voice_config_payload()


@app.post("/voice/synthesize")
def voice_synthesize(
    req: TTSRequest,
    _: CurrentUser = Depends(get_current_user),
):
    """Prepare a single message for playback.

    Browser engine (default): returns the same JSON envelope the client
    has always consumed — text plus voice profile — and the page drives
    `speechSynthesis` locally.

    Piper engine (opt-in): renders WAV audio on the host and returns
    it as ``audio/wav`` bytes. Any failure (binary missing at runtime,
    subprocess error, timeout, …) returns a JSON envelope marked
    ``fallback: true`` so the client can play the message through the
    browser engine instead — the read-aloud experience is never lost.
    """
    try:
        text = _voice.prepare_text(req.text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    requested_engine = req.engine
    if requested_engine == _voice.ENGINE_PIPER:
        provider = _voice.get_provider(_voice.ENGINE_PIPER)
        if provider is None:
            return _piper_fallback_response(
                text, reason="Piper is not configured on this host."
            )
        try:
            audio = provider.synthesize(text)
        except Exception as exc:  # noqa: BLE001 — graceful fallback is the point
            return _piper_fallback_response(text, reason=str(exc))
        headers = {
            "X-Voice-Engine": _voice.ENGINE_PIPER,
            "Cache-Control": "no-store",
        }
        return Response(content=audio, media_type="audio/wav", headers=headers)

    # Browser engine — unchanged JSON envelope.
    provider = _voice.get_default_provider()
    if not provider.is_available():
        raise HTTPException(
            status_code=503, detail="No TTS provider is currently available."
        )
    return {
        "text": text,
        **provider.voice_config().as_dict(),
    }


def _piper_fallback_response(text: str, reason: str) -> JSONResponse:
    """Render the JSON the client uses to fall back to the browser.

    Status 200 with ``fallback: true`` keeps this off the error path;
    the browser engine is a perfectly good outcome, not a 5xx event.
    The ``reason`` is short and user-safe — never the input text.
    """
    browser = _voice.get_default_provider()
    payload = {
        "text": text,
        "fallback": True,
        "fallback_reason": reason[:200],
        **browser.voice_config().as_dict(),
    }
    return JSONResponse(
        content=payload,
        headers={"X-Voice-Engine": _voice.ENGINE_BROWSER},
    )


# ── ADMIN: USER MANAGEMENT ──────────────────────────────────────────

class AdminCreateUserRequest(BaseModel):
    model_config = {"extra": "forbid"}
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    role: str = "user"
    is_restricted: bool = False

    @field_validator("role")
    @classmethod
    def _role(cls, v):
        if v not in ("admin", "user"):
            raise ValueError("role must be 'admin' or 'user'")
        return v

    @field_validator("username")
    @classmethod
    def _username(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("username must be non-empty")
        return v


class AdminSetRoleRequest(BaseModel):
    model_config = {"extra": "forbid"}
    role: str
    is_restricted: bool = False

    @field_validator("role")
    @classmethod
    def _role(cls, v):
        if v not in ("admin", "user"):
            raise ValueError("role must be 'admin' or 'user'")
        return v


class AdminDisableRequest(BaseModel):
    model_config = {"extra": "forbid"}
    disabled: bool


class AdminResetPasswordRequest(BaseModel):
    model_config = {"extra": "forbid"}
    password: str = Field(min_length=1, max_length=256)


class AdminFamilyControlsRequest(BaseModel):
    model_config = {"extra": "forbid"}
    allowed_modes: list[str] | None = None
    web_search_enabled: bool | None = None
    weather_enabled: bool | None = None
    memory_save_enabled: bool | None = None
    memory_import_enabled: bool | None = None
    max_prompt_chars: int | None = Field(default=None, ge=0, le=100_000)
    daily_message_limit: int | None = Field(default=None, ge=0, le=1_000_000)

    @field_validator("allowed_modes")
    @classmethod
    def _modes(cls, v):
        if v is None:
            return v
        cleaned = [m for m in v if m in KNOWN_MODES]
        if not cleaned:
            raise ValueError("allowed_modes must contain at least one known mode")
        return cleaned


def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only.")
    return user


def _admin_db():
    from core.memory import DB_PATH
    return _sqlite3.connect(DB_PATH)


def _user_to_dict(row: dict) -> dict:
    out = dict(row)
    out["disabled"] = row.get("disabled_at") is not None
    out.pop("token_version", None)
    return out


@app.get("/me")
def whoami(user: CurrentUser = Depends(get_current_user)):
    # `available_modes` is the friendly-label view for the client. Raw
    # model names stay admin-only — they are reachable through
    # /admin/models, not /me.
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "is_restricted": user.is_restricted,
        "available_modes": _model_access.available_modes_for(user),
    }


@app.get("/admin/users")
def admin_list_users(_: CurrentUser = Depends(require_admin)):
    with _admin_db() as conn:
        users = _users_mod.list_users(conn)
    out = []
    for u in users:
        entry = _user_to_dict(u)
        if u["is_restricted"]:
            entry["family_controls"] = get_family_controls_dict(u["id"])
        else:
            entry["family_controls"] = None
        out.append(entry)
    return out


@app.post("/admin/users", status_code=201)
def admin_create_user(
    req: AdminCreateUserRequest,
    _: CurrentUser = Depends(require_admin),
):
    if req.role == "admin" and req.is_restricted:
        raise HTTPException(
            status_code=400, detail="Admin cannot be restricted."
        )
    try:
        with _admin_db() as conn:
            try:
                uid = _users_mod.create_user(
                    conn,
                    req.username,
                    req.password,
                    role=req.role,
                    is_restricted=req.is_restricted,
                )
            except _sqlite3.IntegrityError:
                raise HTTPException(
                    status_code=409, detail="Username already exists."
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            row = _users_mod.get_user_by_id(conn, uid)
    finally:
        pass
    return _user_to_dict({
        "id": int(row["id"]),
        "username": row["username"],
        "role": row["role"],
        "is_restricted": bool(row["is_restricted"]),
        "created_at": row["created_at"],
        "disabled_at": row["disabled_at"],
    })


@app.post("/admin/users/{user_id}/disable")
def admin_set_disabled(
    user_id: int,
    req: AdminDisableRequest,
    actor: CurrentUser = Depends(require_admin),
):
    with _admin_db() as conn:
        target = _users_mod.get_user_by_id(conn, user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="User not found.")

        if req.disabled:
            # Block disabling the last active admin or self-lockout.
            if target["role"] == "admin" and target["disabled_at"] is None:
                active = _users_mod.count_active_admins(conn)
                if active <= 1:
                    raise HTTPException(
                        status_code=400,
                        detail="Cannot disable the only active admin.",
                    )
            if int(target["id"]) == int(actor.id):
                raise HTTPException(
                    status_code=400,
                    detail="Admin cannot disable their own account.",
                )

        _users_mod.set_disabled(conn, user_id, req.disabled)
    return {"ok": True}


@app.post("/admin/users/{user_id}/role")
def admin_set_role(
    user_id: int,
    req: AdminSetRoleRequest,
    actor: CurrentUser = Depends(require_admin),
):
    if req.role == "admin" and req.is_restricted:
        raise HTTPException(
            status_code=400, detail="Admin cannot be restricted."
        )
    with _admin_db() as conn:
        target = _users_mod.get_user_by_id(conn, user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="User not found.")
        # Demoting the last admin would lock out admin access entirely.
        if (
            target["role"] == "admin"
            and req.role != "admin"
            and target["disabled_at"] is None
        ):
            active = _users_mod.count_active_admins(conn)
            if active <= 1:
                raise HTTPException(
                    status_code=400,
                    detail="Cannot demote the only active admin.",
                )
        try:
            _users_mod.set_role(
                conn, user_id, req.role, req.is_restricted
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


@app.post("/admin/users/{user_id}/password")
def admin_reset_password(
    user_id: int,
    req: AdminResetPasswordRequest,
    _: CurrentUser = Depends(require_admin),
):
    with _admin_db() as conn:
        ok = _users_mod.reset_password(conn, user_id, req.password)
    if not ok:
        raise HTTPException(status_code=404, detail="User not found.")
    return {"ok": True}


@app.put("/admin/users/{user_id}/family-controls")
def admin_set_family_controls(
    user_id: int,
    req: AdminFamilyControlsRequest,
    _: CurrentUser = Depends(require_admin),
):
    with _admin_db() as conn:
        target = _users_mod.get_user_by_id(conn, user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="User not found.")
        if not bool(target["is_restricted"]):
            raise HTTPException(
                status_code=400,
                detail="Family controls only apply to restricted users.",
            )
    set_family_controls(
        user_id,
        allowed_modes=req.allowed_modes,
        web_search_enabled=req.web_search_enabled,
        weather_enabled=req.weather_enabled,
        memory_save_enabled=req.memory_save_enabled,
        memory_import_enabled=req.memory_import_enabled,
        max_prompt_chars=req.max_prompt_chars,
        daily_message_limit=req.daily_message_limit,
    )
    return {
        "ok": True,
        "family_controls": get_family_controls_dict(user_id),
    }


# ── ADMIN: MODEL REGISTRY ───────────────────────────────────────────
# Read-only view of the local-Ollama model registry seeded at startup
# from config.MODELS. Raw model names are admin-only; non-admin /
# restricted callers are blocked by `require_admin`. Pulling models
# (#111) and per-user model access (#112) are intentionally not wired
# in this endpoint.

@app.get("/admin/models")
def admin_list_models(_: CurrentUser = Depends(require_admin)):
    # Best-effort install-flag refresh; if Ollama is down the persisted
    # flags are returned as-is. This call is read-only — `client.list()` —
    # and never triggers a pull.
    reconcile_installed_models()
    return list_registered_models()


# ── ADMIN: LOCAL OLLAMA MODEL DETECTION ─────────────────────────────
# Detected-model registry. Refresh hits `GET /api/tags` and upserts a
# row per (provider, model_name); models that disappear from Ollama
# stay in the registry. Pulling and GGUF import are out of scope.

@app.post("/admin/ollama/refresh")
def admin_refresh_ollama_models(_: CurrentUser = Depends(require_admin)):
    try:
        stats = _local_models.refresh_from_ollama()
    except OllamaUnavailable:
        raise HTTPException(
            status_code=503,
            detail="Ollama is unavailable.",
        )
    return {"ok": True, **stats}


@app.get("/admin/ollama/models")
def admin_list_ollama_models(_: CurrentUser = Depends(require_admin)):
    return _local_models.list_models()


# ── ADMIN: MODEL PULL ───────────────────────────────────────────────
# Admin-only Ollama pull flow (#111). The pull itself runs on a daemon
# thread so /chat is never blocked. Model names are validated against a
# strict allowlist before any Ollama call. Per-user / per-role model
# access (#112), pull cancellation, and model deletion are intentionally
# not part of this endpoint.

class AdminPullModelRequest(BaseModel):
    model_config = {"extra": "forbid"}
    model: str = Field(min_length=1, max_length=200)


@app.post("/admin/models/pull", status_code=202)
def admin_pull_model(
    req: AdminPullModelRequest,
    _: CurrentUser = Depends(require_admin),
):
    try:
        job = _model_pulls.request_pull(req.model)
    except _model_pulls.InvalidModelName as exc:
        # Surface a generic message; the regex specifics are not useful
        # to the client and could be probed for behaviour.
        raise HTTPException(status_code=400, detail=str(exc))
    except _model_pulls.ModelAlreadyInstalled:
        # 200 + a clear status so the admin UI can show "already installed"
        # without retrying. We do not start a new job.
        return JSONResponse(
            status_code=200,
            content={"status": "already_installed", "model": req.model},
        )
    except _model_pulls.PullAlreadyInProgress as exc:
        # 200 with the existing job — the caller polls the same id.
        return JSONResponse(status_code=200, content=exc.job)
    except _model_pulls.TooManyPullsInProgress as exc:
        raise HTTPException(
            status_code=429,
            detail=f"Too many pulls in progress (cap={exc.cap}).",
        )
    return job


@app.post("/admin/models/pull/preview")
def admin_preview_pull(
    req: AdminPullModelRequest,
    _: CurrentUser = Depends(require_admin),
):
    """
    Return resource warnings for a model name without starting a pull (#126).

    Lets the admin client surface disk / RAM / slowdown guidance ahead of
    triggering the actual download. Warnings are informational only — no
    pull is initiated and no row is inserted.
    """
    try:
        return _model_pulls.preview_pull(req.model)
    except _model_pulls.InvalidModelName as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/admin/models/pulls")
def admin_list_pulls(_: CurrentUser = Depends(require_admin)):
    return _model_pulls.list_pulls()


@app.get("/admin/models/pulls/{pull_id}")
def admin_get_pull(
    pull_id: int,
    _: CurrentUser = Depends(require_admin),
):
    job = _model_pulls.get_pull(pull_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Pull job not found.")
    return job


@app.get("/channel")
def get_channel():
    return {"channel": NOVA_CHANNEL, "branch": NOVA_BRANCH}


@app.get("/health")
def health():
    return {"status": "ok"}


app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    uvicorn.run("web:app", host="0.0.0.0", port=8080, reload=False)
