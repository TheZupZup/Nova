import logging
from typing import Iterator
from config import NOVA_SYSTEM_PROMPT, CHAT_HISTORY_LIMIT
from core.model_providers import (
    ModelProviderError,
    ModelRequest,
    get_provider,
)
from core.model_settings import resolve_default_model
from core.memory import format_memories_for_prompt, parse_and_save
from core.identity import IDENTITY_CONTRACT
from core.nova_contract import build_personalization_block
from core.feedback import build_feedback_preferences_block
from core.policies import ADMIN_POLICY, Policy
from core.relationship_coach import (
    build_relationship_coach_block,
    is_relationship_coach_query,
    is_sensitive_relationship_content,
)
from core.companion import (
    build_companion_grounding_block,
    build_companion_mode_block,
    is_acute_distress,
    is_sensitive_emotional_content,
)
from core.settings import get_personalization, get_user_setting
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


def _generate(model: str, messages: list[dict]) -> str:
    """One non-streamed generation through the active model provider.

    Nova core no longer talks to any concrete client library: it asks the
    provider registry for the configured backend (Ollama by default) and
    works in terms of the backend-agnostic :class:`ModelRequest` /
    :class:`ModelResponse`. The provider is responsible for mapping its
    own transport failures to :class:`ModelProviderError`, which the chat
    entrypoints translate to the existing user-facing "unreachable" reply.
    """
    response = get_provider().generate(
        ModelRequest(model=model, messages=messages)
    )
    return response.content


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


def _extract_and_save_natural_memories(
    user_message: str, user_id: int, project_id: int | None = None
):
    """Runs the rule-based extractor on the user message and persists
    allowed memories under `user_id`.

    ``project_id`` is the *active* project for this turn (the
    conversation's project, resolved by the web layer) or ``None`` for a
    General chat. Memory surfaced inside a project is stored as that
    project's memory; General chats keep storing global memory exactly
    as before.
    """
    try:
        for mem in extract_memories(user_message):
            if is_memory_allowed(mem):
                save_natural_memory(mem, user_id, project_id=project_id)
    except Exception:
        pass  # never let memory extraction break the chat flow


def extract_and_save_memory(
    user_message: str,
    assistant_response: str,
    user_id: int,
    project_id: int | None = None,
):
    """Extrait automatiquement les infos importantes et les sauvegarde
    sous `user_id` (et le projet actif `project_id`, le cas échéant)."""
    prompt = MEMORY_EXTRACTION_PROMPT.format(
        user_message=user_message,
        assistant_response=assistant_response
    )
    try:
        result = _generate(
            resolve_default_model(),
            [{"role": "user", "content": prompt}],
        ).strip()
    except ModelProviderError:
        return
    parse_and_save(result, user_id, project_id)


def _autosave_allowed(
    policy: Policy,
    user_message: str | None,
    assistant_reply: str | None = None,
) -> bool:
    """Whether this turn may be auto-mined for memory.

    Automatic extraction is skipped when *either* the user message or
    the assistant reply carries sensitive relationship detail **or**
    sensitive emotional / mental-state detail: Nova must never silently
    persist who the user is dating, fighting with, or breaking up with,
    nor that the user was distressed, depressed, grieving, or in crisis.
    The reply is checked too because the LLM autosave path
    (``extract_and_save_memory``) builds its extraction prompt from
    *both* the user text and the assistant answer — gating on the user
    message alone would leak context the assistant restated on an
    otherwise-neutral follow-up turn.

    The user can still save such a fact deliberately — the manual memory
    command runs in the web preflight, well before this path, so it is
    unaffected.
    """
    if not policy.memory_save_enabled:
        return False
    user_text = user_message or ""
    reply_text = assistant_reply or ""
    if is_sensitive_relationship_content(user_text):
        return False
    if is_sensitive_relationship_content(reply_text):
        return False
    if is_sensitive_emotional_content(user_text):
        return False
    return not is_sensitive_emotional_content(reply_text)


def build_messages(
    history: list[dict],
    user_input: str,
    memories: list[dict],
    extra_context: str = None,
    context_type: str = None,
    natural_memories=None,
    personalization: dict | None = None,
    feedback_preferences: str | None = None,
    companion_mode: bool = False,
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

    `companion_mode` is the per-user opt-in toggle (resolved by the
    caller from ``user_settings``). When ``True`` the deterministic
    companion-presence block is appended. Independently of that toggle,
    a clear acute-distress message always appends the grounding safety
    net. Both blocks sit *below* the identity/safety contract for the
    same reason every other tone block does — ordering guarantees they
    can never override identity, safety, or capability bounds.
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

    # Relationship Situation Coach — a deterministic, local tone block
    # appended only when the user message is clearly about a sensitive
    # relationship situation. It sits last, well below IDENTITY_CONTRACT
    # and the safety blocks, so it can never override identity or safety
    # rules; it only shapes how Nova answers this one topic.
    if is_relationship_coach_query(user_input):
        parts.append(build_relationship_coach_block())

    # Companion Mode — opt-in calm presence. Only when the user has
    # explicitly enabled it in Settings; a fresh install pays zero token
    # cost and behaves identically. Sits below IDENTITY_CONTRACT and the
    # safety blocks like every other tone block, so it can never weaken
    # identity, safety, or capability rules — it only shapes tone, and
    # its own text forbids manipulation, dependency, and isolation.
    if companion_mode:
        parts.append(build_companion_mode_block())

    # Acute-distress grounding — an always-on safety net. Appended
    # whenever the user message carries clear acute-distress wording,
    # regardless of the companion-mode toggle, so a person in genuine
    # difficulty is met warmly and pointed toward real human /
    # professional / emergency help. Last on purpose: it is the most
    # important tone instruction when someone is in crisis, while still
    # sitting below the identity/safety contract that opens the prompt.
    if is_acute_distress(user_input):
        parts.append(build_companion_grounding_block())

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


def chat(history: list[dict], user_input: str, memories: list[dict], user_id: int, forced_model: str = None, force_search: bool = False, image: str = None, policy: Policy | None = None, project_id: int | None = None) -> tuple[str, str]:  # noqa: E501
    """
    Envoie un message à Nova et retourne sa réponse et le modèle utilisé.

    Toutes les opérations mémoire (récupération, extraction, sauvegarde)
    sont scopées à `user_id` — un utilisateur ne voit jamais les souvenirs
    d'un autre.

    `project_id` is the active project for this conversation (resolved by
    the web layer from the conversation's ``project_id``) or ``None`` for
    a General/unscoped chat. It only bounds *contextual user memory*:
    global memory stays visible, the active project's memory is added,
    and other projects' memory is never retrieved. It can never raise a
    project's priority above the identity/safety contract — project
    memory flows through the same memory block that already sits below
    ``IDENTITY_CONTRACT`` in :func:`build_messages`.

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
            default_model = resolve_default_model()
            reply = _generate(default_model, messages)
            if _autosave_allowed(policy, user_input, reply):
                extract_and_save_memory(
                    user_input or "image", reply, user_id, project_id
                )
            return reply, default_model

        model = forced_model if forced_model else route(user_input)

        natural_mems = get_relevant_memories(
            user_input, user_id, project_scope=project_id
        )
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
        # Per-user opt-in companion-mode toggle. A read failure must
        # never block chat — a fresh DB has no row, so the default is
        # off and behaviour is unchanged.
        try:
            companion_mode = (
                get_user_setting(user_id, "companion_mode_enabled", "false")
                == "true"
            )
        except Exception:
            companion_mode = False

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
                    feedback_preferences=feedback_prefs, companion_mode=companion_mode,
                )
                reply = _generate(model, messages)
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
                    feedback_preferences=feedback_prefs, companion_mode=companion_mode,
                )
                reply = _generate(model, messages)
                return reply, model

        # Web search — both the explicit `force_search` flag and the
        # auto-detected `should_search` path require web_search_enabled.
        if policy.web_search_enabled and (force_search or should_search(user_input)):
            search_results = web_search(user_input)
            messages = build_messages(
                history, user_input, memories, search_results, "search",
                personalization=personalization,
                feedback_preferences=feedback_prefs, companion_mode=companion_mode,
            )
            reply = _generate(model, messages)
            return reply, model

        # Chat normal — inject relevant natural memories into context
        messages = build_messages(
            history, user_input, memories,
            natural_memories=natural_mems,
            personalization=personalization,
            feedback_preferences=feedback_prefs, companion_mode=companion_mode,
        )
        reply = _generate(model, messages)

        # Si Nova sait pas → cherche sur le web automatiquement
        if policy.web_search_enabled and _reply_is_uncertain(reply):
            search_results = web_search(user_input)
            if search_results and "Aucun résultat" not in search_results:
                messages = build_messages(
                    history, user_input, memories, search_results, "search",
                    personalization=personalization,
                    feedback_preferences=feedback_prefs, companion_mode=companion_mode,
                )
                reply = _generate(model, messages)

        if _autosave_allowed(policy, user_input, reply):
            extract_and_save_memory(user_input, reply, user_id, project_id)
            _extract_and_save_natural_memories(
                user_input, user_id, project_id
            )
        return reply, model

    except ModelProviderError as e:
        logger.warning("Model provider unavailable during chat: %s", e)
        return OLLAMA_UNAVAILABLE, resolve_default_model()


def chat_stream(
    history: list[dict],
    user_input: str,
    memories: list[dict],
    user_id: int,
    forced_model: str = None,
    force_search: bool = False,
    image: str = None,
    policy: Policy | None = None,
    project_id: int | None = None,
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
            default_model = resolve_default_model()
            reply = _generate(default_model, messages)
            if _autosave_allowed(policy, user_input, reply):
                extract_and_save_memory(
                    user_input or "image", reply, user_id, project_id
                )
            yield {"type": "meta", "model": default_model}
            if reply:
                yield {"type": "delta", "content": reply}
            yield {"type": "done", "reply": reply, "model": default_model}
            return

        model = forced_model if forced_model else route(user_input)
        yield {"type": "meta", "model": model}

        natural_mems = get_relevant_memories(
            user_input, user_id, project_scope=project_id
        )
        try:
            personalization = get_personalization(user_id)
        except Exception:
            personalization = None
        try:
            feedback_prefs = build_feedback_preferences_block(user_id)
        except Exception:
            feedback_prefs = ""
        # Per-user opt-in companion-mode toggle. A read failure must
        # never block chat — a fresh DB has no row, so the default is
        # off and behaviour is unchanged.
        try:
            companion_mode = (
                get_user_setting(user_id, "companion_mode_enabled", "false")
                == "true"
            )
        except Exception:
            companion_mode = False

        # Weather branch — single short reply, stream it.
        if policy.weather_enabled:
            weather_result = detect_weather_city(user_input)
            if isinstance(weather_result, tuple):
                lat, lon, city = weather_result
                weather_data = get_weather(lat, lon, city)
                messages = build_messages(
                    history, user_input, memories, weather_data,
                    "weather", personalization=personalization,
                    feedback_preferences=feedback_prefs, companion_mode=companion_mode,
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
                    feedback_preferences=feedback_prefs, companion_mode=companion_mode,
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
                feedback_preferences=feedback_prefs, companion_mode=companion_mode,
            )
            reply = yield from _stream_and_accumulate(model, messages)
            yield {"type": "done", "reply": reply, "model": model}
            return

        # Regular chat path — stream the first pass.
        messages = build_messages(
            history, user_input, memories,
            natural_memories=natural_mems, personalization=personalization,
            feedback_preferences=feedback_prefs, companion_mode=companion_mode,
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
                    feedback_preferences=feedback_prefs, companion_mode=companion_mode,
                )
                reply = yield from _stream_and_accumulate(model, messages)

        if _autosave_allowed(policy, user_input, reply):
            extract_and_save_memory(user_input, reply, user_id, project_id)
            _extract_and_save_natural_memories(
                user_input, user_id, project_id
            )

        yield {"type": "done", "reply": reply, "model": model}

    except ModelProviderError as e:
        logger.warning("Model provider unavailable during chat stream: %s", e)
        yield {"type": "error", "detail": OLLAMA_UNAVAILABLE}


def _stream_and_accumulate(model: str, messages: list[dict]) -> Iterator[dict]:
    """Stream one generation through the provider, re-emitting each token.

    The generator behaves as a coroutine: ``reply = yield from
    _stream_and_accumulate(...)`` lets the caller inspect the full
    concatenated text once the stream is exhausted, while the intermediate
    `delta` events propagate to whoever is iterating chat_stream.

    Backend specifics — the legacy single-shot fallback for old clients
    and the streamed-event duck-typing — live in the provider now; this
    function only knows about :class:`ModelChunk`. A backend failure
    surfaces as :class:`ModelProviderError`, which the ``chat_stream``
    wrapper turns into the existing `error` event.
    """
    parts: list[str] = []
    request = ModelRequest(model=model, messages=messages, stream=True)
    for chunk in get_provider().stream(request):
        if not chunk.content:
            continue
        parts.append(chunk.content)
        yield {"type": "delta", "content": chunk.content}
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
