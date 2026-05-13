import logging
from typing import Iterator
import httpx
import ollama
from config import NOVA_SYSTEM_PROMPT, CHAT_HISTORY_LIMIT, MODELS
from core.ollama_client import client
from core.memory import format_memories_for_prompt, parse_and_save
from core.identity import IDENTITY_CONTRACT
from core.nova_contract import build_personalization_block
from core.feedback import build_feedback_preferences_block
from core.policies import ADMIN_POLICY, Policy
from core.settings import get_personalization
from core.router import route
from core.search import web_search, should_search
from core.security_feed import is_security_query
from core.security import SilentGuardProvider, build_security_context_block
from core.integrations import silentguard as silentguard_integration
from core.time_context import format_time_context
from core.weather import detect_weather_city, get_weather
from memory.extractor import extract_memories
from memory.policy import is_memory_allowed
from memory.retriever import get_relevant_memories, format_for_prompt
from memory.store import save_memory as save_natural_memory

logger = logging.getLogger(__name__)

OLLAMA_UNAVAILABLE = "Ollama is unreachable. Make sure Ollama is running, then try again."

# Phrases that, when present in a first-pass reply, mean Nova does not
# actually know the answer. The non-streaming chat() retries with web
# search; chat_stream() emits a `replace` event then re-streams. Kept
# as a module-level tuple so both code paths stay in lockstep.
UNCERTAINTY_TRIGGERS = (
    "je ne sais pas", "je n'ai pas", "je n'ai aucune",
    "je ne peux pas", "je ne dispose pas", "je n'ai pas accès",
    "i don't know", "i don't have", "i cannot",
    "je ne trouve pas", "aucune information",
    "je ne suis pas sûr", "je ne suis pas certain",
)


def _reply_is_uncertain(reply: str) -> bool:
    """True iff `reply` contains one of the uncertainty trigger phrases."""
    lowered = reply.lower()
    return any(trigger in lowered for trigger in UNCERTAINTY_TRIGGERS)


def _iter_content_chunks(stream) -> Iterator[str]:
    """Yield non-empty `message.content` strings from an Ollama stream.

    Ollama's chat-stream generator yields events shaped like
    ``{"message": {"content": "..."}, "done": bool}`` — but the concrete
    type depends on the installed ollama-python client. ``ollama>=0.4``
    streams ``ChatResponse`` Pydantic models (subscriptable but **not**
    ``dict`` instances); older releases and our tests yield plain dicts.
    We duck-type on the ``.get`` API both shapes expose so neither one
    is silently dropped — an ``isinstance(event, dict)`` filter here was
    the source of empty-reply regressions in production. Some
    intermediate events have empty content (e.g. metadata frames) and
    the final ``done`` event also carries a synthetic empty content —
    skipping empties keeps the wire format tidy without affecting
    correctness.
    """
    for event in stream:
        if event is None:
            continue
        msg = _safe_get(event, "message") or {}
        chunk = _safe_get(msg, "content") or ""
        if isinstance(chunk, str) and chunk:
            yield chunk


def _safe_get(obj, key: str):
    """Read ``key`` from a dict-like or Pydantic ``SubscriptableBaseModel``.

    Both shapes expose ``.get`` with the same contract, but a non-dict,
    non-Pydantic object (e.g. an unexpected string event) would raise on
    subscript. Falling back to ``getattr`` keeps the streaming loop
    robust without re-introducing an ``isinstance(dict)`` filter that
    silently drops valid ``ChatResponse`` events.
    """
    if obj is None:
        return None
    getter = getattr(obj, "get", None)
    if callable(getter):
        try:
            return getter(key)
        except TypeError:
            pass
    return getattr(obj, key, None)


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
    feedback_preferences: str | None = None,
) -> list[dict]:
    """Construit la liste de messages à envoyer à Ollama.

    `personalization` is the per-user style payload from
    `core.settings.get_personalization`. It produces an extra block appended
    after the identity contract. Defaults / empty payloads contribute
    nothing, so existing single-user behaviour is preserved.

    `feedback_preferences` is the short, deterministic preference block
    built from this user's thumbs-up / thumbs-down history (see
    ``core.feedback.build_feedback_preferences_block``). It is appended
    *below* the identity contract and the personalization block so it
    cannot override safety rules, identity rules, or capability bounds —
    feedback shapes style, not power.
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
    if feedback_preferences:
        # Sits below identity + personalization on purpose: the system
        # prompt is ordered so safety/identity rules always win.
        parts.append(feedback_preferences)
    parts.append(format_time_context())

    # Read-only SilentGuard context. Probes the local provider on
    # demand (no background polling), produces a short bullet block,
    # and never raises. The block always re-states that Nova may
    # explain but must not perform security actions.
    try:
        sec_block = build_security_context_block(SilentGuardProvider())
    except Exception:  # pragma: no cover — the helper is contract-bound to never raise
        logger.debug("security context block raised; omitting", exc_info=True)
        sec_block = ""
    if sec_block:
        parts.append(sec_block)

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
        try:
            feedback_prefs = build_feedback_preferences_block(user_id)
        except Exception:
            feedback_prefs = ""

        # Weather is gated; restricted users with weather disabled fall
        # straight through to the regular chat branch.
        if policy.weather_enabled:
            weather_result = detect_weather_city(user_input)
            if isinstance(weather_result, tuple):
                lat, lon, city = weather_result
                weather_data = get_weather(lat, lon, city)
                messages = build_messages(
                    history, user_input, memories, weather_data, "weather",
                    personalization=personalization,
                    feedback_preferences=feedback_prefs,
                )
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
                messages = build_messages(
                    history, user_input, memories, security_data, "security",
                    personalization=personalization,
                    feedback_preferences=feedback_prefs,
                )
                response = client.chat(model=model, messages=messages)
                reply = response["message"]["content"]
                return reply, model

        # Web search — both the explicit `force_search` flag and the
        # auto-detected `should_search` path require web_search_enabled.
        if policy.web_search_enabled and (force_search or should_search(user_input)):
            search_results = web_search(user_input)
            messages = build_messages(
                history, user_input, memories, search_results, "search",
                personalization=personalization,
                feedback_preferences=feedback_prefs,
            )
            response = client.chat(model=model, messages=messages)
            reply = response["message"]["content"]
            return reply, model

        # Chat normal — inject relevant natural memories into context
        messages = build_messages(
            history, user_input, memories,
            natural_memories=natural_mems,
            personalization=personalization,
            feedback_preferences=feedback_prefs,
        )
        response = client.chat(model=model, messages=messages)
        reply = response["message"]["content"]

        # Si Nova sait pas → cherche sur le web automatiquement
        if policy.web_search_enabled and _reply_is_uncertain(reply):
            search_results = web_search(user_input)
            if search_results and "Aucun résultat" not in search_results:
                messages = build_messages(
                    history, user_input, memories, search_results, "search",
                    personalization=personalization,
                    feedback_preferences=feedback_prefs,
                )
                response = client.chat(model=model, messages=messages)
                reply = response["message"]["content"]

        if policy.memory_save_enabled:
            extract_and_save_memory(user_input, reply, user_id)
            _extract_and_save_natural_memories(user_input, user_id)
        return reply, model

    except (ollama.ResponseError, ConnectionError, httpx.HTTPError) as e:
        logger.warning("Ollama unreachable during chat: %s", e)
        return OLLAMA_UNAVAILABLE, MODELS["default"]


def chat_stream(
    history: list[dict],
    user_input: str,
    memories: list[dict],
    user_id: int,
    forced_model: str = None,
    force_search: bool = False,
    image: str = None,
    policy: Policy | None = None,
) -> Iterator[dict]:
    """Generator twin of :func:`chat` that yields incremental events.

    The shape mirrors what `/chat/stream` forwards to the browser:

      ``{"type": "meta", "model": "..."}``      — first event, fixed model id
      ``{"type": "delta", "content": "..."}``  — one or more text fragments
      ``{"type": "replace"}``                   — clear bubble (uncertainty
                                                  fallback only; followed by
                                                  further `delta` events)
      ``{"type": "done", "reply": "...",
          "model": "..."}``                     — final event, full text
      ``{"type": "error", "detail": "..."}``    — fatal, no `done` follows

    Memory extraction runs after the stream completes, identical to the
    non-streaming path. Callers are responsible for persisting the user
    message + the final assistant reply once they have seen ``done``;
    nothing is persisted on the `error` path.

    The image / vision branch is **not** streamed — Ollama returns
    vision results as a single response. We emit the full reply as one
    ``delta`` followed by ``done`` so the wire format stays uniform.
    """
    if policy is None:
        policy = ADMIN_POLICY

    try:
        # Image → vision model, no routing, no streaming.
        if image:
            logger.debug("Processing streaming-image request, encoded length=%d", len(image))
            messages = build_image_messages(user_input, image)
            response = client.chat(model=MODELS["default"], messages=messages)
            reply = response["message"]["content"]
            if policy.memory_save_enabled:
                extract_and_save_memory(user_input or "image", reply, user_id)
            yield {"type": "meta", "model": MODELS["default"]}
            if reply:
                yield {"type": "delta", "content": reply}
            yield {"type": "done", "reply": reply, "model": MODELS["default"]}
            return

        model = forced_model if forced_model else route(user_input)
        yield {"type": "meta", "model": model}

        natural_mems = get_relevant_memories(user_input, user_id)
        try:
            personalization = get_personalization(user_id)
        except Exception:
            personalization = None
        try:
            feedback_prefs = build_feedback_preferences_block(user_id)
        except Exception:
            feedback_prefs = ""

        # Weather branch — single short reply, stream it.
        if policy.weather_enabled:
            weather_result = detect_weather_city(user_input)
            if isinstance(weather_result, tuple):
                lat, lon, city = weather_result
                weather_data = get_weather(lat, lon, city)
                messages = build_messages(
                    history, user_input, memories, weather_data,
                    "weather", personalization=personalization,
                    feedback_preferences=feedback_prefs,
                )
                reply = yield from _stream_and_accumulate(model, messages)
                yield {"type": "done", "reply": reply, "model": model}
                return

            if weather_result == "no_city" or weather_result == "multiple":
                reply = "Quelle ville ?"
                yield {"type": "delta", "content": reply}
                yield {"type": "done", "reply": reply, "model": model}
                return

            if weather_result == "unknown_city":
                reply = "Je n'ai pas accès à la météo pour cette ville."
                yield {"type": "delta", "content": reply}
                yield {"type": "done", "reply": reply, "model": model}
                return

        # SilentGuard read-only feed — same gating as the non-streaming path.
        if is_security_query(user_input):
            security_data = silentguard_integration.recent_events_summary(user_id)
            if security_data:
                messages = build_messages(
                    history, user_input, memories, security_data,
                    "security", personalization=personalization,
                    feedback_preferences=feedback_prefs,
                )
                reply = yield from _stream_and_accumulate(model, messages)
                yield {"type": "done", "reply": reply, "model": model}
                return

        # Forced or auto-detected web search.
        if policy.web_search_enabled and (force_search or should_search(user_input)):
            search_results = web_search(user_input)
            messages = build_messages(
                history, user_input, memories, search_results,
                "search", personalization=personalization,
                feedback_preferences=feedback_prefs,
            )
            reply = yield from _stream_and_accumulate(model, messages)
            yield {"type": "done", "reply": reply, "model": model}
            return

        # Regular chat path — stream the first pass.
        messages = build_messages(
            history, user_input, memories,
            natural_memories=natural_mems, personalization=personalization,
            feedback_preferences=feedback_prefs,
        )
        reply = yield from _stream_and_accumulate(model, messages)

        # Uncertainty fallback (same trigger set as the non-streaming
        # path). We clear the bubble via `replace` then stream the
        # search-augmented retry so the user only ends up reading the
        # search answer, not "I don't know" + the answer.
        if policy.web_search_enabled and _reply_is_uncertain(reply):
            search_results = web_search(user_input)
            if search_results and "Aucun résultat" not in search_results:
                yield {"type": "replace"}
                messages = build_messages(
                    history, user_input, memories, search_results,
                    "search", personalization=personalization,
                    feedback_preferences=feedback_prefs,
                )
                reply = yield from _stream_and_accumulate(model, messages)

        if policy.memory_save_enabled:
            extract_and_save_memory(user_input, reply, user_id)
            _extract_and_save_natural_memories(user_input, user_id)

        yield {"type": "done", "reply": reply, "model": model}

    except (ollama.ResponseError, ConnectionError, httpx.HTTPError) as e:
        logger.warning("Ollama unreachable during chat stream: %s", e)
        yield {"type": "error", "detail": OLLAMA_UNAVAILABLE}


def _stream_and_accumulate(model: str, messages: list[dict]) -> Iterator[dict]:
    """Stream a single Ollama chat call and re-emit each token as `delta`.

    The generator behaves as a coroutine: ``reply = yield from
    _stream_and_accumulate(...)`` lets the caller inspect the full
    concatenated text once the upstream stream is exhausted, while the
    intermediate events propagate to whoever is iterating chat_stream.

    Falls back gracefully if the installed Ollama client predates the
    streaming kwarg — the request degrades to a single-shot reply
    surfaced as one `delta`.
    """
    parts: list[str] = []
    try:
        stream = client.chat(model=model, messages=messages, stream=True)
    except TypeError:
        # Older ollama clients lacked the streaming kwarg. Fall back to a
        # single, non-streaming call so the endpoint still works.
        response = client.chat(model=model, messages=messages)
        reply = response["message"]["content"]
        if reply:
            yield {"type": "delta", "content": reply}
        return reply

    for chunk in _iter_content_chunks(stream):
        parts.append(chunk)
        yield {"type": "delta", "content": chunk}
    return "".join(parts)


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
