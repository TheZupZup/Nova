import uvicorn
from fastapi import FastAPI, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

from core.auth import verify_password, create_token, verify_token
from core.chat import chat
from core.memory import initialize_db, load_memories, save_memory

app = FastAPI(docs_url=None, redoc_url=None)
security = HTTPBearer()

history = []


class LoginRequest(BaseModel):
    password: str


class ChatRequest(BaseModel):
    message: str


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Vérifie le token JWT sur chaque requête protégée."""
    if not verify_token(credentials.credentials):
        raise HTTPException(status_code=401, detail="Token invalide ou expiré.")
    return True


@app.on_event("startup")
def startup():
    initialize_db()


@app.post("/login")
def login(request: LoginRequest):
    if not verify_password(request.password):
        raise HTTPException(status_code=401, detail="Mot de passe incorrect.")
    return {"token": create_token()}


@app.post("/chat")
def chat_endpoint(request: ChatRequest, _: bool = Depends(get_current_user)):
    memories = load_memories()

    # Commande souviens-toi via interface web
    if request.message.lower().startswith("souviens-toi:"):
        parts = request.message[13:].strip().split(":", 1)
        if len(parts) == 2:
            save_memory(parts[0].strip(), parts[1].strip())
            return {"response": "Souvenir sauvegardé.", "model": "system"}

    response, model_used = chat(history, request.message, memories)

    history.append({"role": "user", "content": request.message})
    history.append({"role": "assistant", "content": response})

    return {"response": response, "model": model_used}


app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    uvicorn.run("web:app", host="0.0.0.0", port=8080, reload=False)
