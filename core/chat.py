import ollama
from config import NEXUS_SYSTEM_PROMPT, CHAT_HISTORY_LIMIT
from core.memory import format_memories_for_prompt
from core.router import route


def build_messages(history: list[dict], user_input: str, memories: list[dict]) -> list[dict]:
    """Construit la liste de messages à envoyer à Ollama."""
    memory_text = format_memories_for_prompt(memories)
    system_prompt = NEXUS_SYSTEM_PROMPT.format(memories=memory_text)
    messages = [{"role": "system", "content": system_prompt}]
    messages += history[-CHAT_HISTORY_LIMIT:]
    messages.append({"role": "user", "content": user_input})
    return messages


def chat(history: list[dict], user_input: str, memories: list[dict]) -> tuple[str, str]:
    """Envoie un message à Nexus et retourne sa réponse et le modèle utilisé."""
    model = route(user_input)
    messages = build_messages(history, user_input, memories)
    response = ollama.chat(model=model, messages=messages)
    return response["message"]["content"], model
