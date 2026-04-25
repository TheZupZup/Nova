from config import MODELS
from core.ollama_client import client

ROUTER_PROMPT = """Classify this request with ONE word only.

Rules:
- simple: greetings, compliments, small talk, short questions, yes/no, casual chat
- code: ONLY when user explicitly asks to CREATE, WRITE, BUILD or FIX actual code, scripts, apps, functions, programs
- normal: explain concept, summarize, translate, general question, advice, search, news, specs, memory
- advanced: complex analysis, architecture, deep reasoning, research, long documents

Examples of code: "write a python script", "create an app", "fix this bug", "build a function"
Examples of NOT code: "how does python work", "what is docker", "tell me about programming"

Request: {query}

Reply with ONE word (simple/code/normal/advanced):"""

MODEL_MAP = {
    "simple":   MODELS["default"],
    "normal":   MODELS["default"],
    "advanced": MODELS["advanced"],
    "code":     MODELS["code"],
}

FALLBACK_MODEL = MODELS["default"]


def route(user_input: str) -> str:
    """Choisit le bon modèle selon la complexité de la requête."""
    prompt = ROUTER_PROMPT.format(query=user_input)
    response = client.chat(
        model=MODELS["router"],
        messages=[{"role": "user", "content": prompt}]
    )
    category = response["message"]["content"].strip().lower().split()[0]
    return MODEL_MAP.get(category, FALLBACK_MODEL)
