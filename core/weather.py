import urllib.request
import urllib.error
import json
import re


def get_weather(lat: float, lon: float, city: str) -> str:
    """Récupère la météo en temps réel via Open-Meteo."""
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code"
            f"&timezone=America%2FToronto"
        )
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read())

        current = data["current"]
        temp = current["temperature_2m"]
        humidity = current["relative_humidity_2m"]
        wind = current["wind_speed_10m"]

        return f"Météo actuelle à {city} : {temp}°C, humidité {humidity}%, vent {wind} km/h"

    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError) as e:
        return f"Erreur météo : {e}"


CITIES = {
    "estrie": (45.4042, -71.8929, "Sherbrooke/Estrie"),
    "sherbrooke": (45.4042, -71.8929, "Sherbrooke"),
    "montréal": (45.5017, -73.5673, "Montréal"),
    "montreal": (45.5017, -73.5673, "Montréal"),
    "québec": (46.8139, -71.2080, "Québec"),
    "quebec": (46.8139, -71.2080, "Québec"),
    "toronto": (43.6532, -79.3832, "Toronto"),
    "vancouver": (49.2827, -123.1207, "Vancouver"),
}

_WEATHER_KEYWORDS = [
    "météo", "meteo", "température", "temperature",
    "temps qu'il fait", "weather", "il fait combien",
]

# Words that cannot be city names — used to detect whether a city was mentioned
_NOISE = {
    # weather keywords
    "météo", "meteo", "température", "temperature", "temps", "weather",
    # French function words
    "quel", "quelle", "quels", "quelles", "fait", "il", "y", "a", "à",
    "de", "du", "des", "la", "le", "les", "un", "une", "pour", "en",
    "et", "ou", "je", "tu", "nous", "vous", "comment", "est", "c",
    "qu", "combien", "quand", "où", "ce", "cet", "cette", "ces", "dans", "sur",
    # common contextual words
    "actuelle", "actuel", "aujourd", "hui", "maintenant",
    # English function words
    "what", "is", "the", "it", "like", "how", "at", "in", "for",
    "are", "me", "can", "you", "tell",
    # single-letter fragments left after punctuation stripping
    "s", "d", "l", "m", "n", "j", "t",
}

# Time/context words that look like content but are not city names
_TIME_WORDS = {
    # French
    "demain", "aujourd'hui", "maintenant", "hier", "matin", "soir",
    "midi", "nuit", "semaine", "weekend", "week-end", "prochain",
    "prochaine", "après-midi", "aprem", "jours", "jour", "heures", "heure",
    "minutes", "minute",
    # English
    "today", "tomorrow", "yesterday", "morning", "evening", "night",
    "afternoon", "now", "soon", "later", "week", "weekend", "next",
}

# Weather descriptors and conditions that are not city names
_WEATHER_CONTEXT_WORDS = {
    # French
    "froid", "chaud", "pluie", "neige", "soleil", "vent", "nuage", "nuageux",
    "ensoleillé", "pluvieux", "orageux", "brouillard", "brume", "gel", "verglas",
    "humide", "sec", "doux", "frais", "frisquet", "glacial", "tempête",
    "beau", "mauvais", "prévision", "prévisions",
    # English
    "rain", "rainy", "snow", "snowy", "sun", "sunny", "wind", "windy",
    "cloud", "cloudy", "cold", "hot", "warm", "cool", "fog", "foggy",
    "stormy", "storm", "forecast", "hail",
}

_CITY_IGNORED = _NOISE | _TIME_WORDS | _WEATHER_CONTEXT_WORDS


def _has_unrecognized_city(lower_input: str) -> bool:
    """Returns True if the input contains a token that realistically looks like a city name."""
    words = re.sub(r"[^\w\s]", " ", lower_input).split()
    return any(w not in _CITY_IGNORED and len(w) > 1 and not w[0].isdigit() for w in words)


def detect_weather_city(user_input: str):
    """
    Détecte si la requête concerne la météo et identifie la ville.

    Returns:
      (lat, lon, city_name)  — single recognized city
      "multiple"             — multiple distinct recognized cities
      "no_city"              — weather query but no city mentioned
      "unknown_city"         — weather query with an unrecognized city
      None                   — not a weather query at all
    """
    lower = user_input.lower()

    if not any(w in lower for w in _WEATHER_KEYWORDS):
        return None

    # Collect all matching cities, deduplicated by coordinates
    seen: set = set()
    matches = []
    for key, value in CITIES.items():
        if key in lower and value[:2] not in seen:
            seen.add(value[:2])
            matches.append(value)

    if len(matches) == 1:
        return matches[0]

    if len(matches) > 1:
        return "multiple"

    # No recognized city — check if a city name was mentioned anyway
    if _has_unrecognized_city(lower):
        return "unknown_city"

    return "no_city"
