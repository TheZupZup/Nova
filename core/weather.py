import urllib.request
import urllib.error
import json


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


def detect_weather_city(user_input: str):
    """Détecte si la requête est une demande météo et retourne la ville."""
    lower = user_input.lower()
    weather_words = ["météo", "meteo", "température", "temperature", "temps qu'il fait", "weather"]
    
    if not any(w in lower for w in weather_words):
        return None

    for key, value in CITIES.items():
        if key in lower:
            return value

    return None
