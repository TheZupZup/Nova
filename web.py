import time
import secrets as _secrets
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, field_validator

from core.auth import verify_credentials, create_token, verify_token
from core.rate_limiter import check_login_rate_limit
from apscheduler.schedulers.background import BackgroundScheduler
from core.learner import learn_from_feeds
from core.updater import check_and_update_models
from core.chat import chat
from core.memory import (
    initialize_db, load_memories, save_memory,
    create_conversation, load_conversations,
    load_conversation_messages, save_message,
    delete_conversation, update_conversation_title,
    get_setting, save_setting,
    list_memories, update_memory, delete_memory,
)
from memory.store import (
    list_memories as list_natural_memories,
    delete_memories_matching,
)
from config import (
    MODELS, ALLOWED_SETTINGS, NOVA_MODEL_DEFAULT_NAME,
    NOVA_CHANNEL, NOVA_BRANCH,
    GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, GITHUB_OAUTH_REDIRECT_URI,
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
scheduler.add_job(learn_from_feeds, "interval", hours=1)
scheduler.add_job(check_and_update_models, "interval", weeks=1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize_db()
    scheduler.start()
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


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not verify_token(credentials.credentials):
        raise HTTPException(status_code=401, detail="Token invalide ou expiré.")
    return True


@app.post("/login")
def login(request: LoginRequest, _: None = Depends(check_login_rate_limit)):
    if not verify_credentials(request.username, request.password):
        raise HTTPException(status_code=401, detail="Identifiants incorrects.")
    return {"token": create_token()}


@app.get("/conversations")
def get_conversations(_: bool = Depends(get_current_user)):
    return load_conversations()


@app.post("/conversations")
def new_conversation(request: NewConversationRequest, _: bool = Depends(get_current_user)):
    conv_id = create_conversation(request.title)
    return {"id": conv_id, "title": request.title}


@app.get("/conversations/{conversation_id}/messages")
def get_messages(conversation_id: int, _: bool = Depends(get_current_user)):
    return load_conversation_messages(conversation_id)


@app.delete("/conversations/{conversation_id}")
def remove_conversation(conversation_id: int, _: bool = Depends(get_current_user)):
    delete_conversation(conversation_id)
    return {"ok": True}


@app.post("/chat")
def chat_endpoint(request: ChatRequest, _: bool = Depends(get_current_user)):
    memories = load_memories()

    if request.message.lower().startswith("souviens-toi:"):
        parts = request.message[13:].strip().split(":", 1)
        if len(parts) == 2:
            save_memory(parts[0].strip(), parts[1].strip())
            return {"response": "Souvenir sauvegardé.", "model": "system", "conversation_id": request.conversation_id}

    msg_lower = request.message.lower().strip()

    if msg_lower.startswith("forget that ") or msg_lower.startswith("oublie que "):
        query = request.message.split(" ", 2)[2].strip()
        count = delete_memories_matching(query)
        reply = f"Done. Removed {count} memory(ies) matching '{query}'." if count else "No matching memories found."
        return {"response": reply, "model": "system", "conversation_id": request.conversation_id}

    if msg_lower.startswith("forget everything about ") or msg_lower.startswith("oublie tout sur "):
        parts = msg_lower.split(" ", 3)
        query = request.message.split(" ", 3)[-1].strip()
        count = delete_memories_matching(query)
        reply = f"Done. Removed {count} memory(ies) about '{query}'." if count else "No matching memories found."
        return {"response": reply, "model": "system", "conversation_id": request.conversation_id}

    if msg_lower in (
        "what do you remember about me?", "show my memories", "show memories",
        "what do you know about me?", "que sais-tu de moi ?", "que sais-tu de moi?",
        "montre mes souvenirs", "montre-moi mes souvenirs",
    ):
        mems = list_natural_memories()
        if not mems:
            return {"response": "I don't have any natural memories stored yet.", "model": "system", "conversation_id": request.conversation_id}
        lines = ["Here's what I remember about you:\n"]
        for m in mems:
            lines.append(f"- [{m.kind}/{m.topic}] {m.content}")
        return {"response": "\n".join(lines), "model": "system", "conversation_id": request.conversation_id}

    conversation_id = request.conversation_id
    if not conversation_id:
        conversation_id = create_conversation(request.message[:40])

    history = [
        {"role": m["role"], "content": m["content"]}
        for m in load_conversation_messages(conversation_id)
    ]

    nova_enabled = get_setting("nova_model_enabled", "false") == "true"
    if nova_enabled:
        forced_model = get_setting("nova_model_name", NOVA_MODEL_DEFAULT_NAME)
    else:
        forced_model = MODE_MAP.get(request.mode)
    print(f"IMAGE RECEIVED: {bool(request.image)} - Length: {len(request.image) if request.image else 0}")
    response, model_used = chat(history, request.message, memories, forced_model=forced_model, force_search=request.search, image=request.image)

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
def get_memories(_: bool = Depends(get_current_user)):
    return list_memories()


@app.put("/memories/{memory_id}")
def update_memory_endpoint(memory_id: int, request: MemoryUpdateRequest, _: bool = Depends(get_current_user)):
    update_memory(memory_id, request.category, request.content)
    return {"ok": True}


@app.delete("/memories/{memory_id}")
def delete_memory_endpoint(memory_id: int, _: bool = Depends(get_current_user)):
    delete_memory(memory_id)
    return {"ok": True}


@app.post("/memories")
def add_memory(request: MemoryAddRequest, _: bool = Depends(get_current_user)):
    save_memory(request.category, request.content)
    return {"ok": True}


@app.get("/settings")
def get_settings(_: bool = Depends(get_current_user)):
    return {
        "ram_budget": get_setting("ram_budget", "2048"),
        "last_model_update": get_setting("last_model_update", "Never"),
        "last_updated_models": get_setting("last_updated_models", ""),
        "nova_model_enabled": get_setting("nova_model_enabled", "false") == "true",
        "nova_model_name": get_setting("nova_model_name", NOVA_MODEL_DEFAULT_NAME),
    }


@app.post("/models/update")
def trigger_model_update(_: bool = Depends(get_current_user)):
    """Lance une vérification manuelle des mises à jour."""
    import threading
    thread = threading.Thread(target=check_and_update_models)
    thread.daemon = True
    thread.start()
    return {"ok": True, "message": "Update started in background"}


@app.post("/settings")
def update_settings(data: SettingsUpdateRequest, _: bool = Depends(get_current_user)):
    if data.ram_budget is not None:
        save_setting("ram_budget", str(data.ram_budget))
    if data.nova_model_enabled is not None:
        save_setting("nova_model_enabled", "true" if data.nova_model_enabled else "false")
    if data.nova_model_name is not None:
        save_setting("nova_model_name", data.nova_model_name)
    return {"ok": True}


@app.get("/channel")
def get_channel():
    return {"channel": NOVA_CHANNEL, "branch": NOVA_BRANCH}


@app.get("/health")
def health():
    return {"status": "ok"}


app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    uvicorn.run("web:app", host="0.0.0.0", port=8080, reload=False)
