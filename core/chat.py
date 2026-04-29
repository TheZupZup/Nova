import logging
import httpx
import ollama
from config import NOVA_SYSTEM_PROMPT, CHAT_HISTORY_LIMIT, MODELS
from core.ollama_client import client
from core.memory import format_memories_for_prompt, parse_and_save
from core.router import route
from core.search import web_search, should_search
from core.weather import detect_weather_city, get_weather
from memory.extractor import extract_memories
from memory.policy import is_memory_allowed
from memory.retriever import get_relevant_memories, format_for_prompt
from memory.store import save_memory as save_natural_memory

logger = logging.getLogger(__name__)

OLLAMA_UNAVAILABLE = "Ollama is unreachable. Make sure Ollama is running, then try again."

MEMORY_EXTRACTION_PROMPT = """Analyse cette conversation et extrait UNIQUEMENT les informations personnelles importantes sur l'utilisateur.

Si tu trouves quelque chose d'important, réponds avec ce format exact:
SAVE:categorie:information

Si il n'y a rien d'important, réponds uniquement:
NOTHING

Message utilisateur: {user_message}
Réponse assistant: {assistant_response}

Réponds uniquement avec SAVE:... ou NOTHING:"""

SEARCH_SYSTEM_PROMPT = """Tu es Nova, un assistant personnel intelligent.
Tu as effectué une recherche web pour répondre à cette question.
Utilise les résultats ci-dessous pour donner une réponse précise et à jour.
Réponds toujours dans la langue de l'utilisateur de manière naturelle et concise.

Résultats de recherche:
{search_results}"""

WEATHER_SYSTEM_PROMPT = """Tu es Nova, un assistant personnel intelligent.
Tu as récupéré les données météo en temps réel pour répondre à cette question.
Utilise ces données pour donner une réponse claire et naturelle.
Réponds dans la langue de l'utilisateur.

Données météo:
{weather_data}"""


def _extract_and_save_natural_memories(user_message: str):
    """Runs the rule-based extractor on the user message and persists allowed memories."""
    try:
        for mem in extract_memories(user_message):
            if is_memory_allowed(mem):
                save_natural_memory(mem)
    except Exception:
        pass  # never let memory extraction break the chat flow


def extract_and_save_memory(user_message: str, assistant_response: str):
    """Extrait automatiquement les infos importantes et les sauvegarde."""
    prompt = MEMORY_EXTRACTION_PROMPT.format(
        user_message=user_message,
        assistant_response=assistant_response
    )
    try:
        response = client.chat(
            model=MODELS["default"],
            messages=[{"role": "user", "content": prompt}]
        )
    except (ollama.ResponseError, ConnectionError, httpx.HTTPError):
        return
    result = response["message"]["content"].strip()
    parse_and_save(result)


def build_messages(history: list[dict], user_input: str, memories: list[dict], extra_context: str = None, context_type: str = None, natural_memories=None) -> list[dict]:
    """Construit la liste de messages à envoyer à Ollama."""
    if context_type == "weather":
        system_prompt = WEATHER_SYSTEM_PROMPT.format(weather_data=extra_context)
    elif context_type == "search":
        system_prompt = SEARCH_SYSTEM_PROMPT.format(search_results=extra_context)
    else:
        memory_text = format_memories_for_prompt(memories)
        natural_text = format_for_prompt(natural_memories) if natural_memories else ""
        combined = "\n\n".join(filter(None, [memory_text, natural_text]))
        system_prompt = NOVA_SYSTEM_PROMPT.format(memories=combined)

    messages = [{"role": "system", "content": system_prompt}]
    messages += history[-CHAT_HISTORY_LIMIT:]
    messages.append({"role": "user", "content": user_input})
    return messages


def build_image_messages(user_input: str, image: str) -> list[dict]:
    """Construit les messages pour une requête avec image — sans historique."""
    return [{
        "role": "user",
        "content": user_input or "Analyse et décris cette image.",
        "images": [image]
    }]


def chat(history: list[dict], user_input: str, memories: list[dict], forced_model: str = None, force_search: bool = False, image: str = None) -> tuple[str, str]:
    """Envoie un message à Nova et retourne sa réponse et le modèle utilisé."""
    try:
        # Image → vision model, no routing
        if image:
            logger.debug("Processing image request, encoded length=%d", len(image))
            messages = build_image_messages(user_input, image)
            response = client.chat(model=MODELS["default"], messages=messages)
            reply = response["message"]["content"]
            extract_and_save_memory(user_input or "image", reply)
            return reply, MODELS["default"]

        model = forced_model if forced_model else route(user_input)

        natural_mems = get_relevant_memories(user_input)

        # Météo en temps réel
        weather_result = detect_weather_city(user_input)
        if isinstance(weather_result, tuple):
            lat, lon, city = weather_result
            weather_data = get_weather(lat, lon, city)
            messages = build_messages(history, user_input, memories, weather_data, "weather")
            response = client.chat(model=model, messages=messages)
            reply = response["message"]["content"]
            return reply, model

        if weather_result in ("no_city", "multiple"):
            return "Quelle ville ?", model

        if weather_result == "unknown_city":
            return "Je n'ai pas accès à la météo pour cette ville.", model

        # Web search
        if force_search or should_search(user_input):
            search_results = web_search(user_input)
            messages = build_messages(history, user_input, memories, search_results, "search")
            response = client.chat(model=model, messages=messages)
            reply = response["message"]["content"]
            return reply, model

        # Chat normal — inject relevant natural memories into context
        messages = build_messages(history, user_input, memories, natural_memories=natural_mems)
        response = client.chat(model=model, messages=messages)
        reply = response["message"]["content"]

        # Si Nova sait pas → cherche sur le web automatiquement
        uncertainty_triggers = [
            "je ne sais pas", "je n'ai pas", "je n'ai aucune",
            "je ne peux pas", "je ne dispose pas", "je n'ai pas accès",
            "i don't know", "i don't have", "i cannot",
            "je ne trouve pas", "aucune information",
            "je ne suis pas sûr", "je ne suis pas certain",
        ]

        if any(t in reply.lower() for t in uncertainty_triggers):
            search_results = web_search(user_input)
            if search_results and "Aucun résultat" not in search_results:
                messages = build_messages(history, user_input, memories, search_results, "search")
                response = client.chat(model=model, messages=messages)
                reply = response["message"]["content"]

        extract_and_save_memory(user_input, reply)
        _extract_and_save_natural_memories(user_input)
        return reply, model

    except (ollama.ResponseError, ConnectionError, httpx.HTTPError) as e:
        logger.warning("Ollama unreachable during chat: %s", e)
        return OLLAMA_UNAVAILABLE, MODELS["default"]


def get_history_limit() -> int:
    """Retourne la limite d'historique selon le RAM budget configuré."""
    try:
        from core.memory import get_setting
        budget_mb = int(get_setting("ram_budget", "2048"))
        if budget_mb <= 512:
            return 5
        elif budget_mb <= 1024:
            return 10
        elif budget_mb <= 2048:
            return 20
        elif budget_mb <= 4096:
            return 40
        elif budget_mb <= 8192:
            return 80
        else:
            return 150
    except (ValueError, TypeError):
        return 20
