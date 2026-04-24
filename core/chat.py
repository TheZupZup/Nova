import ollama
from config import NOVA_SYSTEM_PROMPT, CHAT_HISTORY_LIMIT
from core.memory import format_memories_for_prompt, save_memory
from core.router import route

MEMORY_EXTRACTION_PROMPT = """Analyse cette conversation et extrait UNIQUEMENT les informations personnelles importantes sur l'utilisateur (préférences, faits, projets, habitudes).

Si tu trouves quelque chose d'important, réponds avec ce format exact:
SAVE:categorie:information

Si il n'y a rien d'important à retenir, réponds uniquement:
NOTHING

Exemples valides:
SAVE:projet:L'utilisateur travaille sur Nova, un assistant IA local
SAVE:preference:L'utilisateur préfère les réponses courtes et directes
SAVE:setup:L'utilisateur a un Ryzen 9 5950X avec 128GB RAM et RX 9070 XT

Message utilisateur: {user_message}
Réponse assistant: {assistant_response}

Réponds uniquement avec SAVE:... ou NOTHING:"""


def extract_and_save_memory(user_message: str, assistant_response: str):
    """Extrait automatiquement les infos importantes et les sauvegarde."""
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
            category, content = parts
            save_memory(category.strip(), content.strip())


def build_messages(history: list[dict], user_input: str, memories: list[dict]) -> list[dict]:
    """Construit la liste de messages à envoyer à Ollama."""
    memory_text = format_memories_for_prompt(memories)
    system_prompt = NOVA_SYSTEM_PROMPT.format(memories=memory_text)
    messages = [{"role": "system", "content": system_prompt}]
    messages += history[-CHAT_HISTORY_LIMIT:]
    messages.append({"role": "user", "content": user_input})
    return messages


def chat(history: list[dict], user_input: str, memories: list[dict]) -> tuple[str, str]:
    """Envoie un message à Nova et retourne sa réponse et le modèle utilisé."""
    model = route(user_input)
    messages = build_messages(history, user_input, memories)
    response = ollama.chat(model=model, messages=messages)
    reply = response["message"]["content"]

    # Extraction automatique de mémoire en arrière-plan
    extract_and_save_memory(user_input, reply)

    return reply, model
