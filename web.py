import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

from core.auth import verify_credentials, create_token, verify_token
from apscheduler.schedulers.background import BackgroundScheduler
from core.learner import learn_from_feeds
from core.updater import check_and_update_models
from core.chat import chat
from core.memory import (
    initialize_db, load_memories, save_memory,
    create_conversation, load_conversations,
    load_conversation_messages, save_message,
    delete_conversation, update_conversation_title,
    get_setting, save_setting
)
import sqlite3

DB_PATH = "nova.db"
security = HTTPBearer()

MODE_MAP = {
    "chat": "gemma4",
    "code": "deepseek-coder-v2",
    "deep": "qwen2.5:32b",
}


scheduler = BackgroundScheduler()
scheduler.add_job(learn_from_feeds, "interval", hours=6)
scheduler.add_job(check_and_update_models, "interval", weeks=1)

@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize_db()
    scheduler.start()
    learn_from_feeds()
    yield
    scheduler.shutdown()


app = FastAPI(docs_url=None, redoc_url=None, lifespan=lifespan)


class LoginRequest(BaseModel):
    username: str
    password: str


class ChatRequest(BaseModel):
    message: str
    conversation_id: int | None = None
    mode: str = "auto"
    search: bool = False


class NewConversationRequest(BaseModel):
    title: str = "Nouvelle conversation"


class MemoryUpdateRequest(BaseModel):
    category: str
    content: str


class MemoryAddRequest(BaseModel):
    category: str
    content: str


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not verify_token(credentials.credentials):
        raise HTTPException(status_code=401, detail="Token invalide ou expiré.")
    return True


@app.post("/login")
def login(request: LoginRequest):
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

    conversation_id = request.conversation_id
    if not conversation_id:
        conversation_id = create_conversation(request.message[:40])

    history = [
        {"role": m["role"], "content": m["content"]}
        for m in load_conversation_messages(conversation_id)
    ]

    forced_model = MODE_MAP.get(request.mode)
    response, model_used = chat(history, request.message, memories, forced_model=forced_model, force_search=request.search)

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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, category, content, created FROM memories ORDER BY created DESC"
    ).fetchall()
    conn.close()
    return [{"id": r["id"], "category": r["category"], "content": r["content"], "created": r["created"]} for r in rows]


@app.put("/memories/{memory_id}")
def update_memory(memory_id: int, request: MemoryUpdateRequest, _: bool = Depends(get_current_user)):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE memories SET category = ?, content = ? WHERE id = ?",
        (request.category, request.content, memory_id)
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/memories/{memory_id}")
def delete_memory(memory_id: int, _: bool = Depends(get_current_user)):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    conn.commit()
    conn.close()
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
        "last_updated_models": get_setting("last_updated_models", "")
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
def update_settings(data: dict, _: bool = Depends(get_current_user)):
    for key, value in data.items():
        save_setting(key, str(value))
    return {"ok": True}


app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    uvicorn.run("web:app", host="0.0.0.0", port=8080, reload=False)
