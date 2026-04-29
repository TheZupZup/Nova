import logging
import feedparser
import httpx
import ollama
import sqlite3
from config import MODELS
from core.memory import cleanup_old_knowledge, parse_and_save
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

logger = logging.getLogger(__name__)


def learn_from_feeds():
    """Scanne les flux RSS et sauvegarde les infos importantes."""
    logger.info("Nova learning from web...")
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
                parse_and_save(result)
        except (ollama.ResponseError, ConnectionError, httpx.HTTPError,
                KeyError, sqlite3.OperationalError, sqlite3.DatabaseError) as e:
            logger.warning("Error learning from %s: %s", url, e)
    cleanup_old_knowledge(MAX_KNOWLEDGE_MEMORIES)
    logger.info("Nova learning done.")
