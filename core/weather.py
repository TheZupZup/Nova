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
    "qu", "combien", "quand", "où",
    # common contextual words
    "actuelle", "actuel", "aujourd", "hui", "maintenant",
    # English function words
    "what", "is", "the", "it", "like", "how", "at", "in", "for",
    "are", "me", "can", "you", "tell",
    # single-letter fragments left after punctuation stripping
    "s", "d", "l", "m", "n", "j", "t",
}


def _has_unrecognized_city(lower_input: str) -> bool:
    """Returns True if the input contains a word that looks like an unsupported city name."""
    words = re.sub(r"[^\w\s]", " ", lower_input).split()
    return any(w not in _NOISE and len(w) > 1 for w in words)


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
