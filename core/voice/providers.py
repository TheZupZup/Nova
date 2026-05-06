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
    providers will extend this with a ``synthesize(text) -> bytes``
    method when they land; the foundation deliberately stops here.
    """

    name: str

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if the provider can serve a request right now."""

    @abstractmethod
    def voice_config(self) -> VoiceConfig:
        """Return the voice profile the client should use."""


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
    """Return the active TTS provider for this Nova instance.

    Today this is always ``BrowserProvider``: Nova ships with no
    server-side TTS dependency and the browser already gives us a
    calm, high-quality default voice on every modern OS. A future
    config switch can route this to a local server engine without
    touching call sites.
    """
    return BrowserProvider()
