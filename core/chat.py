import logging
import httpx
import ollama
from config import NOVA_SYSTEM_PROMPT, CHAT_HISTORY_LIMIT, MODELS
from core.ollama_client import client
from core.memory import format_memories_for_prompt, parse_and_save
from core.identity import IDENTITY_CONTRACT
from core.nova_contract import build_personalization_block
from core.policies import ADMIN_POLICY, Policy
from core.settings import get_personalization
from core.router import route
from core.search import web_search, should_search
from core.security_feed import is_security_query
from core.integrations import silentguard as silentguard_integration
from core.time_context import format_time_context
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

SECURITY_SYSTEM_PROMPT = """Tu es Nova, un assistant personnel intelligent.
Tu reçois un résumé en lecture seule du flux SilentGuard (connexions, processus, statut de confiance).
Analyse ces données pour répondre à la question de l'utilisateur, mets en évidence les anomalies, et n'hésite pas à demander si une action est souhaitée.
Tu n'as AUCUN moyen d'agir : tu ne peux ni bloquer, ni tuer un processus, ni modifier le système. Ne suggère que des pistes d'analyse, pas des commandes destructrices.
Réponds dans la langue de l'utilisateur.

Données SilentGuard (read-only):
{security_data}"""


def _extract_and_save_natural_memories(user_message: str, user_id: int):
    """Runs the rule-based extractor on the user message and persists allowed memories under `user_id`."""
    try:
        for mem in extract_memories(user_message):
            if is_memory_allowed(mem):
                save_natural_memory(mem, user_id)
    except Exception:
        pass  # never let memory extraction break the chat flow


def extract_and_save_memory(user_message: str, assistant_response: str, user_id: int):
    """Extrait automatiquement les infos importantes et les sauvegarde sous `user_id`."""
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
    parse_and_save(result, user_id)


def build_messages(
    history: list[dict],
    user_input: str,
    memories: list[dict],
    extra_context: str = None,
    context_type: str = None,
    natural_memories=None,
    personalization: dict | None = None,
) -> list[dict]:
    """Construit la liste de messages à envoyer à Ollama.

    `personalization` is the per-user style payload from
    `core.settings.get_personalization`. It produces an extra block appended
    after the identity contract. Defaults / empty payloads contribute
    nothing, so existing single-user behaviour is preserved.
    """
    if context_type == "weather":
        system_prompt = WEATHER_SYSTEM_PROMPT.format(weather_data=extra_context)
    elif context_type == "search":
        system_prompt = SEARCH_SYSTEM_PROMPT.format(search_results=extra_context)
    elif context_type == "security":
        system_prompt = SECURITY_SYSTEM_PROMPT.format(security_data=extra_context)
    else:
        memory_text = format_memories_for_prompt(memories)
        natural_text = format_for_prompt(natural_memories) if natural_memories else ""
        combined = "\n\n".join(filter(None, [memory_text, natural_text]))
        system_prompt = NOVA_SYSTEM_PROMPT.format(memories=combined)

    parts = [IDENTITY_CONTRACT, system_prompt]
    pers_block = build_personalization_block(personalization)
    if pers_block:
        parts.append(pers_block)
    parts.append(format_time_context())

    messages = [{"role": "system", "content": "\n\n".join(parts)}]
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


def chat(history: list[dict], user_input: str, memories: list[dict], user_id: int, forced_model: str = None, force_search: bool = False, image: str = None, policy: Policy | None = None) -> tuple[str, str]:  # noqa: E501
    """
    Envoie un message à Nova et retourne sa réponse et le modèle utilisé.

    Toutes les opérations mémoire (récupération, extraction, sauvegarde)
    sont scopées à `user_id` — un utilisateur ne voit jamais les souvenirs
    d'un autre.

    `policy` gates the dual-use side effects (weather lookup, web search,
    memory extraction). It defaults to the admin policy so non-HTTP
    callers (CLI, learner) keep their pre-#108 behaviour.
    """
    if policy is None:
        policy = ADMIN_POLICY
    try:
        # Image → vision model, no routing
        if image:
            logger.debug("Processing image request, encoded length=%d", len(image))
            messages = build_image_messages(user_input, image)
            response = client.chat(model=MODELS["default"], messages=messages)
            reply = response["message"]["content"]
            if policy.memory_save_enabled:
                extract_and_save_memory(user_input or "image", reply, user_id)
            return reply, MODELS["default"]

        model = forced_model if forced_model else route(user_input)

        natural_mems = get_relevant_memories(user_input, user_id)
        # Per-user style preferences. A failure here must never block the
        # chat flow — the panel is opt-in and a fresh DB has no rows.
        try:
            personalization = get_personalization(user_id)
        except Exception:
            personalization = None

        # Weather is gated; restricted users with weather disabled fall
        # straight through to the regular chat branch.
        if policy.weather_enabled:
            weather_result = detect_weather_city(user_input)
            if isinstance(weather_result, tuple):
                lat, lon, city = weather_result
                weather_data = get_weather(lat, lon, city)
                messages = build_messages(history, user_input, memories, weather_data, "weather", personalization=personalization)
                response = client.chat(model=model, messages=messages)
                reply = response["message"]["content"]
                return reply, model

            if weather_result in ("no_city", "multiple"):
                return "Quelle ville ?", model

            if weather_result == "unknown_city":
                return "Je n'ai pas accès à la météo pour cette ville.", model

        # SilentGuard read-only feed — surfaces local security telemetry
        # when the user explicitly asks about it AND the user has turned
        # the SilentGuard integration on in Settings. No system actions.
        if is_security_query(user_input):
            security_data = silentguard_integration.recent_events_summary(user_id)
            if security_data:
                messages = build_messages(history, user_input, memories, security_data, "security", personalization=personalization)
                response = client.chat(model=model, messages=messages)
                reply = response["message"]["content"]
                return reply, model

        # Web search — both the explicit `force_search` flag and the
        # auto-detected `should_search` path require web_search_enabled.
        if policy.web_search_enabled and (force_search or should_search(user_input)):
            search_results = web_search(user_input)
            messages = build_messages(history, user_input, memories, search_results, "search", personalization=personalization)
            response = client.chat(model=model, messages=messages)
            reply = response["message"]["content"]
            return reply, model

        # Chat normal — inject relevant natural memories into context
        messages = build_messages(history, user_input, memories, natural_memories=natural_mems, personalization=personalization)
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

        if policy.web_search_enabled and any(t in reply.lower() for t in uncertainty_triggers):
            search_results = web_search(user_input)
            if search_results and "Aucun résultat" not in search_results:
                messages = build_messages(history, user_input, memories, search_results, "search", personalization=personalization)
                response = client.chat(model=model, messages=messages)
                reply = response["message"]["content"]

        if policy.memory_save_enabled:
            extract_and_save_memory(user_input, reply, user_id)
            _extract_and_save_natural_memories(user_input, user_id)
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
