import ollama

ROUTER_MODEL = "gemma3:1b"

ROUTER_PROMPT = """Tu es un classificateur de requêtes. Réponds UNIQUEMENT avec un de ces mots exacts :
- simple    (salutations, questions courtes, météo, heure)
- normal    (explications, questions générales, résumés)
- complex   (code, scripts, analyse technique, raisonnement profond)

Requête : {query}
Réponse :"""

MODEL_MAP = {
    "simple":  "gemma3:1b",
    "normal":  "mistral:latest",
    "complex": "qwen2.5:32b",
}

FALLBACK_MODEL = "qwen2.5:14b"


def route(user_input: str) -> str:
    """Choisit le bon modèle selon la complexité de la requête."""
    prompt = ROUTER_PROMPT.format(query=user_input)
    response = ollama.chat(
        model=ROUTER_MODEL,
        messages=[{"role": "user", "content": prompt}]
    )
    category = response["message"]["content"].strip().lower()
    return MODEL_MAP.get(category, FALLBACK_MODEL)
