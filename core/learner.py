import feedparser
from config import MODELS
from core.memory import save_memory, cleanup_old_knowledge
from core.ollama_client import client

SOURCES = [
    # Tech & IA
    "https://hnrss.org/frontpage",
    "https://www.reddit.com/r/LocalLLaMA/.rss",
    "https://www.reddit.com/r/selfhosted/.rss",
    "https://www.reddit.com/r/opensource/.rss",
    "https://www.reddit.com/r/linux/.rss",
    "https://www.reddit.com/r/programming/.rss",
    # IA News
    "https://www.reddit.com/r/artificial/.rss",
    "https://www.reddit.com/r/MachineLearning/.rss",
    # Tech général
    "https://feeds.arstechnica.com/arstechnica/technology-lab",
    "https://www.wired.com/feed/rss",
    # Mozilla & Firefox
    "https://blog.mozilla.org/en/feed/",
    "https://www.mozilla.org/en-US/firefox/releases/notes/feed/",
    "https://hacks.mozilla.org/feed/",
]

EXTRACT_PROMPT = """Lis ce titre et résumé d'article.
Si c'est une information technique importante sur l'IA, Linux, la technologie ou la programmation,
extrais l'info clé en une phrase courte et factuelle.
Réponds avec: SAVE:knowledge:ta phrase courte
Sinon réponds: NOTHING

Titre: {title}
Résumé: {summary}"""

MAX_KNOWLEDGE_MEMORIES = 500


def learn_from_feeds():
    """Scanne les flux RSS et sauvegarde les infos importantes."""
    print("Nova learning from web...")
    for url in SOURCES:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:3]:
                title = entry.get("title", "")
                summary = entry.get("summary", "")[:500]
                prompt = EXTRACT_PROMPT.format(title=title, summary=summary)
                response = client.chat(
                    model=MODELS["router"],
                    messages=[{"role": "user", "content": prompt}]
                )
                result = response["message"]["content"].strip()
                if result.startswith("SAVE:"):
                    parts = result[5:].split(":", 1)
                    if len(parts) == 2:
                        save_memory(parts[0].strip(), parts[1].strip())
        except Exception as e:
            print(f"Error learning from {url}: {e}")
    cleanup_old_knowledge(MAX_KNOWLEDGE_MEMORIES)
    print("Nova learning done.")
