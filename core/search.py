from ddgs import DDGS


def web_search(query: str, max_results: int = 5) -> str:
    """Recherche sur le web via DuckDuckGo et retourne les résultats formatés."""
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))

        if not results:
            return "Aucun résultat trouvé."

        formatted = []
        for i, r in enumerate(results, 1):
            formatted.append(f"[{i}] {r['title']}\n{r['href']}\n{r['body']}")

        return "\n\n".join(formatted)

    except Exception as e:
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
