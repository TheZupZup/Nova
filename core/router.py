import logging
import httpx
import ollama
from config import MODELS
from core.ollama_client import client

logger = logging.getLogger(__name__)

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

# Compiled-in routing map. Kept as a module constant (and asserted as
# such by the registry / pull suites) so the *config* routing contract
# is documented and unchanged. The admin-selected default model is an
# overlay applied at call time in ``route`` below — it never rewrites
# this map, so `code`/`advanced` routing is untouched.
MODEL_MAP = {
    "simple":   MODELS["default"],
    "normal":   MODELS["default"],
    "advanced": MODELS["advanced"],
    "code":     MODELS["code"],
}

FALLBACK_MODEL = MODELS["default"]


def _default_model() -> str:
    """The default chat model: admin-selected if safely persisted, else config.

    Resolved per call (not bound at import) so an admin's validated
    choice takes effect without a restart. Network-free and never
    raises; with nothing persisted it returns ``config.MODELS["default"]``
    so existing behaviour — and the routing-preserved suites — are
    unaffected.
    """
    try:
        from core.model_settings import resolve_default_model

        return resolve_default_model()
    except Exception:  # never let routing fail on a settings read
        return FALLBACK_MODEL


def route(user_input: str) -> str:
    """Choisit le bon modèle selon la complexité de la requête."""
    default_model = _default_model()
    # `simple`/`normal` and the fallback follow the admin-selected
    # default; `code`/`advanced` keep their dedicated config models.
    model_map = {
        "simple":   default_model,
        "normal":   default_model,
        "advanced": MODELS["advanced"],
        "code":     MODELS["code"],
    }
    prompt = ROUTER_PROMPT.format(query=user_input)
    try:
        response = client.chat(
            model=MODELS["router"],
            messages=[{"role": "user", "content": prompt}]
        )
        content = response["message"]["content"].strip().lower()
        category = content.split()[0] if content else ""
        return model_map.get(category, default_model)
    except (ollama.ResponseError, ConnectionError, httpx.HTTPError) as e:
        logger.warning("Router model unavailable, falling back to default: %s", e)
        return default_model
