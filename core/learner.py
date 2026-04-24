import feedparser
import ollama
from core.memory import save_memory

SOURCES = [
    "https://hnrss.org/frontpage",
    "https://www.reddit.com/r/LocalLLaMA/.rss",
    "https://www.reddit.com/r/selfhosted/.rss",
]

EXTRACT_PROMPT = """Lis ce titre et résumé d'article. 
Si c'est une information technique importante sur l'IA, Linux, ou la technologie, 
extrais l'info clé en une phrase courte.
Réponds avec: SAVE:knowledge:ta phrase courte
Sinon réponds: NOTHING

Titre: {title}
Résumé: {summary}"""


def learn_from_feeds():
    """Scanne les flux RSS et sauvegarde les infos importantes."""
    print("Nexus learning from web...")
    for url in SOURCES:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:5]:
                title = entry.get("title", "")
                summary = entry.get("summary", "")[:500]
                prompt = EXTRACT_PROMPT.format(title=title, summary=summary)
                response = ollama.chat(
                    model="gemma3:1b",
                    messages=[{"role": "user", "content": prompt}]
                )
                result = response["message"]["content"].strip()
                if result.startswith("SAVE:"):
                    parts = result[5:].split(":", 1)
                    if len(parts) == 2:
                        save_memory(parts[0].strip(), parts[1].strip())
        except Exception as e:
            print(f"Error learning from {url}: {e}")
    print("Learning done.")
