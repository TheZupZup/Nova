import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

from core.auth import verify_credentials, create_token, verify_token
from core.chat import chat
from core.memory import (
    initialize_db, load_memories, save_memory,
    create_conversation, load_conversations,
    load_conversation_messages, save_message,
    delete_conversation, update_conversation_title
)

security = HTTPBearer()


@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize_db()
    yield


app = FastAPI(docs_url=None, redoc_url=None, lifespan=lifespan)


class LoginRequest(BaseModel):
    username: str
    password: str


class ChatRequest(BaseModel):
    message: str
    conversation_id: int | None = None


class NewConversationRequest(BaseModel):
    title: str = "Nouvelle conversation"


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Vérifie le token JWT sur chaque requête protégée."""
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

    # Commande souviens-toi
    if request.message.lower().startswith("souviens-toi:"):
        parts = request.message[13:].strip().split(":", 1)
        if len(parts) == 2:
            save_memory(parts[0].strip(), parts[1].strip())
            return {"response": "Souvenir sauvegardé.", "model": "system", "conversation_id": request.conversation_id}

    # Crée une conversation si pas fournie
    conversation_id = request.conversation_id
    if not conversation_id:
        conversation_id = create_conversation(request.message[:40])

    # Charge l'historique de cette conversation
    history = [
        {"role": m["role"], "content": m["content"]}
        for m in load_conversation_messages(conversation_id)
    ]

    response, model_used = chat(history, request.message, memories)

    # Sauvegarde les deux messages
    save_message(conversation_id, "user", request.message)
    save_message(conversation_id, "assistant", response, model_used)

    # Met à jour le titre si c'est le premier message
    if len(history) == 0:
        update_conversation_title(conversation_id, request.message[:40])

    return {
        "response": response,
        "model": model_used,
        "conversation_id": conversation_id
    }


app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    uvicorn.run("web:app", host="0.0.0.0", port=8080, reload=False)
