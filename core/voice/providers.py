"""
Voice synthesis providers.

Two pieces live here:

* ``VoiceConfig`` — the small payload the client uses to drive playback.
  It carries the active engine name and a calm, feminine voice profile
  (preferred voice list, gentle rate / pitch, fade timing).
* ``Provider`` — the abstract surface a future server-rendered engine
  can implement. The only provider shipped today is ``BrowserProvider``,
  which delegates synthesis to the user's browser (``speechSynthesis``).

The shape is deliberately minimal. We are not building a "voice
platform" — only a seam that lets us swap the engine later without
touching the chat code or the HTTP layer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


ENGINE_BROWSER = "browser"
ENGINE_PIPER = "piper"


# Ordered preference list for the client's voice picker. The frontend
# walks this list against ``speechSynthesis.getVoices()`` and uses the
# first match it finds; if none of these are installed it falls back
# to the platform default voice without raising.
#
# The list is curated for Nova's profile: feminine, calm, warm,
# natural-sounding, low listening fatigue. Neural / "Online (Natural)"
# voices come first where the platform offers them; older voices are
# kept as fallbacks because they remain pleasant on long sessions.
DEFAULT_VOICE_NAMES: tuple[str, ...] = (
    # Apple (macOS, iOS) — neural, warm, low-fatigue
    "Samantha",
    "Ava",
    "Allison",
    "Karen",
    "Moira",
    # Microsoft Edge / Windows (online + offline natural voices)
    "Microsoft Aria Online (Natural) - English (United States)",
    "Microsoft Jenny Online (Natural) - English (United States)",
    "Microsoft Sonia Online (Natural) - English (United Kingdom)",
    "Microsoft Hazel - English (Great Britain)",
    "Microsoft Zira - English (United States)",
    # French neural voices, for FR users (Nova ships bilingual)
    "Microsoft Denise Online (Natural) - French (France)",
    "Audrey",
    "Amelie",
    "Marie",
    # Google (Chrome / ChromeOS)
    "Google UK English Female",
    "Google US English",
    "Google français",
)


@dataclass(frozen=True)
class VoiceConfig:
    """Voice preferences exposed to the client.

    ``engine`` tells the client where synthesis happens. Today's only
    value is ``ENGINE_BROWSER``; future providers may emit
    server-rendered audio instead.

    ``preferred_voice_names`` is consumed in order by the client; the
    rest of the fields are gentle defaults tuned for a calm, natural
    delivery (slightly slowed speech, neutral pitch, short fade so
    starts and stops never click).
    """

    engine: str = ENGINE_BROWSER
    preferred_voice_names: tuple[str, ...] = field(default_factory=tuple)
    rate: float = 0.97
    pitch: float = 1.0
    volume: float = 1.0
    fade_ms: int = 220

    def as_dict(self) -> dict:
        return {
            "engine": self.engine,
            "preferred_voice_names": list(self.preferred_voice_names),
            "rate": self.rate,
            "pitch": self.pitch,
            "volume": self.volume,
            "fade_ms": self.fade_ms,
        }


class Provider(ABC):
    """Abstract voice provider.

    A provider answers two questions: is it usable on this host, and
    what voice profile should the client request? Server-rendered
    providers may also implement ``synthesize(text) -> bytes`` to
    return raw audio; client-side engines (the browser default) leave
    that method as a no-op so the same call site works for both.
    """

    name: str

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if the provider can serve a request right now."""

    @abstractmethod
    def voice_config(self) -> VoiceConfig:
        """Return the voice profile the client should use."""

    def synthesize(self, text: str) -> bytes:
        """Render ``text`` to audio bytes.

        Default implementation raises ``NotImplementedError`` because
        the browser engine renders client-side and never reaches this
        path. Server-rendered providers (Piper, Coqui, …) override.
        """
        raise NotImplementedError(
            f"{self.name} provider does not render audio server-side"
        )


class BrowserProvider(Provider):
    """Default provider: synthesis happens in the user's browser.

    The server only echoes voice preferences. No audio bytes are
    rendered, stored, or sent over the network — keeping the
    foundation fully local and zero-install.
    """

    name = ENGINE_BROWSER

    def is_available(self) -> bool:
        return True

    def voice_config(self) -> VoiceConfig:
        return VoiceConfig(
            engine=ENGINE_BROWSER,
            preferred_voice_names=DEFAULT_VOICE_NAMES,
        )


def get_default_provider() -> Provider:
    """Return the active server-default TTS provider.

    Browser remains the default because it works zero-install on every
    modern OS, including mobile. The server will *offer* additional
    engines through ``available_engines`` so the user can opt into a
    local neural voice from Settings without changing the read-aloud
    fallback path.
    """
    return BrowserProvider()


def get_piper_provider():
    """Return a freshly-resolved ``PiperProvider``, or None on import error.

    Imported lazily so a clean install never has to load the Piper
    surface, and an env-driven misconfiguration never breaks the
    voice foundation as a whole.
    """
    try:
        from .piper import PiperProvider
        from config import (
            NOVA_PIPER_BINARY,
            NOVA_PIPER_VOICE_MODEL,
            NOVA_PIPER_VOICE_CONFIG,
            NOVA_PIPER_TIMEOUT_SECONDS,
        )
    except ImportError:
        return None
    return PiperProvider(
        binary=NOVA_PIPER_BINARY,
        model=NOVA_PIPER_VOICE_MODEL,
        config=NOVA_PIPER_VOICE_CONFIG,
        timeout_seconds=NOVA_PIPER_TIMEOUT_SECONDS,
    )


def list_available_engines() -> list[str]:
    """Return the engines this server can currently serve.

    Browser is always available (the client renders it). Piper is
    listed only when its binary and voice model both resolve; a
    half-configured Piper stays absent so the UI never offers a
    broken option.
    """
    engines = [ENGINE_BROWSER]
    piper = get_piper_provider()
    if piper is not None and piper.is_available():
        engines.append(ENGINE_PIPER)
    return engines


def get_provider(name: str) -> Provider | None:
    """Resolve an engine name to a configured provider, or None.

    Unknown / unavailable names return None so callers can fall back
    to the browser engine without raising.
    """
    if name == ENGINE_BROWSER:
        return BrowserProvider()
    if name == ENGINE_PIPER:
        piper = get_piper_provider()
        if piper is not None and piper.is_available():
            return piper
        return None
    return None
