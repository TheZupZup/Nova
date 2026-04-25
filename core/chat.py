import ollama
from config import NOVA_SYSTEM_PROMPT, CHAT_HISTORY_LIMIT
from core.memory import format_memories_for_prompt, save_memory
from core.router import route
from core.search import web_search, should_search
from core.weather import detect_weather_city, get_weather

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
Réponds toujours en français de manière naturelle et concise.

Résultats de recherche:
{search_results}"""

WEATHER_SYSTEM_PROMPT = """Tu es Nova, un assistant personnel intelligent.
Tu as récupéré les données météo en temps réel pour répondre à cette question.
Utilise ces données pour donner une réponse claire et naturelle en français.

Données météo:
{weather_data}"""


def extract_and_save_memory(user_message: str, assistant_response: str):
    prompt = MEMORY_EXTRACTION_PROMPT.format(
        user_message=user_message,
        assistant_response=assistant_response
    )
    response = ollama.chat(
        model="gemma4",
        messages=[{"role": "user", "content": prompt}]
    )
    result = response["message"]["content"].strip()
    if result.startswith("SAVE:"):
        parts = result[5:].split(":", 1)
        if len(parts) == 2:
            save_memory(parts[0].strip(), parts[1].strip())


def build_messages(history: list[dict], user_input: str, memories: list[dict], extra_context: str = None, context_type: str = None, image: str = None) -> list[dict]:
    if context_type == "weather":
        system_prompt = WEATHER_SYSTEM_PROMPT.format(weather_data=extra_context)
    elif context_type == "search":
        system_prompt = SEARCH_SYSTEM_PROMPT.format(search_results=extra_context)
    else:
        memory_text = format_memories_for_prompt(memories)
        system_prompt = NOVA_SYSTEM_PROMPT.format(memories=memory_text)

    messages = [{"role": "system", "content": system_prompt}]
    messages += history[-CHAT_HISTORY_LIMIT:]

    if image:
        messages.append({
            "role": "user",
            "content": user_input or "Analyse cette image.",
            "images": [image]
        })
    else:
        messages.append({"role": "user", "content": user_input})

    return messages


def chat(history: list[dict], user_input: str, memories: list[dict], forced_model: str = None, force_search: bool = False, image: str = None) -> tuple[str, str]:
    # Si image → force gemma4 (vision)
    if image:
        model = "gemma4"
    else:
        model = forced_model if forced_model else route(user_input)

    # Météo en temps réel
    weather_city = detect_weather_city(user_input)
    if weather_city:
        lat, lon, city = weather_city
        weather_data = get_weather(lat, lon, city)
        messages = build_messages(history, user_input, memories, weather_data, "weather")
        response = ollama.chat(model=model, messages=messages)
        reply = response["message"]["content"]
        extract_and_save_memory(user_input, reply)
        return reply, model

    # Web search — manuel ou automatique
    if force_search or should_search(user_input):
        search_results = web_search(user_input)
        messages = build_messages(history, user_input, memories, search_results, "search")
        response = ollama.chat(model=model, messages=messages)
        reply = response["message"]["content"]
        extract_and_save_memory(user_input, reply)
        return reply, model

    # Chat normal
    messages = build_messages(history, user_input, memories)
    response = ollama.chat(model=model, messages=messages)
    reply = response["message"]["content"]
    extract_and_save_memory(user_input, reply)
    return reply, model


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
    except Exception:
        return 20
