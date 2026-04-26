import html
import re
import httpx
from ddgs import DDGS


def sanitize_search_text(text: str, max_length: int = 500) -> str:
    """Strip HTML, decode entities, normalize whitespace, and cap length."""
    text = html.unescape(text)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:max_length]


def clean_query(query: str) -> str:
    """Nettoie la requête pour une meilleure recherche."""
    stop_words = ["cherche", "trouve", "dis-moi", "quelle", "quelles", "est-ce que", 
                  "je veux savoir", "peux-tu", "pourrait-tu", "s'il te plait"]
    q = query.lower()
    for word in stop_words:
        q = q.replace(word, "")
    return q.strip()


def web_search(query: str, max_results: int = 5) -> str:
    """Recherche sur le web via DuckDuckGo et retourne les résultats formatés."""
    try:
        cleaned = clean_query(query)
        with DDGS() as ddgs:
            results = list(ddgs.text(cleaned, max_results=max_results))

        if not results:
            return "Aucun résultat trouvé."

        formatted = []
        for i, r in enumerate(results, 1):
            title = sanitize_search_text(r['title'], max_length=200)
            body = sanitize_search_text(r['body'], max_length=500)
            formatted.append(f"[{i}] {title}\n{r['href']}\n{body}")

        return "\n\n".join(formatted)

    except (httpx.HTTPError, httpx.ConnectError, httpx.TimeoutException, ValueError) as e:
        return f"Erreur de recherche : {e}"


def should_search(user_input: str) -> bool:
    """Détecte si la requête nécessite une recherche web."""
    triggers = [
        "cherche", "search", "trouve", "find",
        "actualité", "news", "aujourd'hui", "maintenant",
        "récent", "dernier", "latest", "current",
        "météo", "weather", "meteo", "prix", "price", "température", "temperature",
        "qui est", "who is", "c'est quoi", "what is", "dis-moi", "tell me",
        "quand", "when", "où", "where",
    ]
    lower = user_input.lower()
    return any(t in lower for t in triggers)
