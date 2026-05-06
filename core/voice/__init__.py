"""
Voice (text-to-speech) foundation for Nova.

This package is intentionally small. Nova's first voice surface is a
"read aloud" button on assistant replies that plays speech locally,
with no autoplay, no always-listening microphone, and no telemetry.

The default provider performs synthesis on the client using the
browser's `speechSynthesis` API. That keeps the foundation fully
local — no new server-side dependency, no audio bytes leaving the
user's machine — and lets a calm, natural platform voice (Apple
Samantha, Microsoft Aria/Jenny, Google Female, …) carry Nova's
preferred profile: feminine, warm, low-fatigue, modern.

The provider abstraction (see ``providers``) is here so that a future
local server-side engine (Piper, Coqui, Kokoro, …) can be added
without rewriting the call sites or the HTTP surface. It is not used
by any server-rendered audio path today.
"""

from __future__ import annotations

from .providers import (
    DEFAULT_VOICE_NAMES,
    ENGINE_BROWSER,
    ENGINE_PIPER,
    BrowserProvider,
    Provider,
    VoiceConfig,
    get_default_provider,
    get_piper_provider,
    get_provider,
    list_available_engines,
)


# Server-side input cap for any text passed to the TTS surface. Long
# passages produce no useful spoken audio and would waste the user's
# time on any future server-rendered provider; capping here keeps the
# behaviour predictable across providers.
MAX_TTS_INPUT_CHARS = 4000


def prepare_text(text: str) -> str:
    """Normalise and bound text for TTS.

    Strips surrounding whitespace and rejects empty or excessively long
    input. Callers are expected to translate the ``ValueError`` into a
    400-shaped HTTP response.
    """
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("text is empty")
    if len(cleaned) > MAX_TTS_INPUT_CHARS:
        raise ValueError(f"text exceeds {MAX_TTS_INPUT_CHARS} characters")
    return cleaned


__all__ = [
    "BrowserProvider",
    "DEFAULT_VOICE_NAMES",
    "ENGINE_BROWSER",
    "ENGINE_PIPER",
    "MAX_TTS_INPUT_CHARS",
    "Provider",
    "VoiceConfig",
    "get_default_provider",
    "get_piper_provider",
    "get_provider",
    "list_available_engines",
    "prepare_text",
]
