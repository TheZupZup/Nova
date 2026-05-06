"""
Piper voice provider — optional local neural TTS.

Piper (https://github.com/rhasspy/piper) is a small, fast neural
speech engine that runs on CPU and produces calm, natural voices on
Linux hosts where the browser/system default falls back to a robotic
fallback. Nova never bundles or downloads Piper or its voice models;
the user installs the binary and points Nova at a `.onnx` voice model
through environment variables. See README for setup.

Design notes:

* The provider is *advisory*. ``is_available()`` is the truth source;
  if anything is missing or unreadable the caller stays on the browser
  engine. We never raise during availability checks — they run on
  every ``/voice/config`` hit and must be cheap and quiet.
* Synthesis shells out to a single ``piper`` invocation, feeds text on
  stdin, and reads a WAV from a tempfile. No shell, no user-text in
  argv. The temp file is always cleaned up.
* No telemetry, no network. Piper itself runs entirely offline.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .providers import (
    ENGINE_PIPER,
    Provider,
    VoiceConfig,
)

logger = logging.getLogger(__name__)


# Curated suggestions surfaced in docs / status payloads. We do not pin
# Nova to any particular voice — the user picks one when they download
# a model — but these are the calm, soft voices that match the rest of
# Nova's read-aloud profile. Names use Piper's repository slugs so the
# user can drop them straight into an install command.
RECOMMENDED_PIPER_VOICES: tuple[str, ...] = (
    "en_US-amy-medium",
    "en_GB-jenny_dioco-medium",
    "en_US-lessac-medium",
    "fr_FR-siwis-medium",
)


class PiperError(RuntimeError):
    """Raised when a Piper synthesis call fails for any reason."""


@dataclass(frozen=True)
class PiperStatus:
    """Diagnostic snapshot for the UI / logs.

    Carries enough context to render a calm error string in Settings
    without leaking absolute filesystem paths to the browser.
    """

    available: bool
    binary_found: bool
    model_found: bool
    config_found: bool
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "available": self.available,
            "binary_found": self.binary_found,
            "model_found": self.model_found,
            "config_found": self.config_found,
            "detail": self.detail,
        }


def _resolve_binary(binary_setting: str) -> Optional[str]:
    """Return an absolute path to a runnable piper, or None.

    Honours an explicit setting first, then walks PATH. Anything that
    isn't a regular file we can execute is rejected — including a path
    pointing at a directory, a stale symlink, or a config typo.
    """
    candidates: list[str] = []
    if binary_setting:
        candidates.append(binary_setting)
        # If the user gave a bare name, also try resolving it on PATH.
        if os.sep not in binary_setting:
            which = shutil.which(binary_setting)
            if which and which != binary_setting:
                candidates.append(which)
    else:
        which = shutil.which("piper")
        if which:
            candidates.append(which)

    for candidate in candidates:
        try:
            path = Path(candidate)
            if not path.is_file():
                continue
            if not os.access(candidate, os.X_OK):
                continue
            return str(path)
        except OSError:
            continue
    return None


def _resolve_model(model_setting: str) -> Optional[str]:
    """Return a readable model path, or None."""
    if not model_setting:
        return None
    try:
        path = Path(model_setting)
        if not path.is_file():
            return None
        if not os.access(model_setting, os.R_OK):
            return None
        return str(path)
    except OSError:
        return None


def _resolve_config(model_path: Optional[str], config_setting: str) -> Optional[str]:
    """Return a readable model-config (.onnx.json) path, or None.

    Piper voice models are paired with a JSON config that describes
    sample rate, phoneme set, etc. The CLI auto-discovers ``<model>.json``
    next to the model when ``--config`` is omitted. We mirror that
    convention so most users only need to set ``NOVA_PIPER_VOICE_MODEL``.
    """
    if config_setting:
        try:
            path = Path(config_setting)
            if path.is_file() and os.access(config_setting, os.R_OK):
                return str(path)
        except OSError:
            pass
        return None

    if not model_path:
        return None
    sibling = Path(model_path + ".json")
    try:
        if sibling.is_file() and os.access(sibling, os.R_OK):
            return str(sibling)
    except OSError:
        pass
    return None


class PiperProvider(Provider):
    """Local neural TTS via the ``piper`` CLI.

    The provider is constructed eagerly with the resolved settings so
    availability checks stay cheap (no subprocess / no PATH walk on
    each call). Re-instantiate to pick up a config change.
    """

    name = ENGINE_PIPER

    def __init__(
        self,
        binary: str = "",
        model: str = "",
        config: str = "",
        timeout_seconds: float = 20.0,
    ) -> None:
        self._binary = _resolve_binary(binary)
        self._model = _resolve_model(model)
        self._config = _resolve_config(self._model, config)
        # Defensive: a non-positive timeout would let a wedged subprocess
        # block the request indefinitely. Clamp to something sensible.
        self._timeout = float(timeout_seconds) if timeout_seconds and timeout_seconds > 0 else 20.0

    # ── Availability ────────────────────────────────────────────────

    def is_available(self) -> bool:
        return bool(self._binary) and bool(self._model)

    def status(self) -> PiperStatus:
        binary_found = bool(self._binary)
        model_found = bool(self._model)
        config_found = bool(self._config)

        if binary_found and model_found:
            # Synthesis works either way — Piper auto-discovers a
            # sibling config — but a missing one is worth surfacing so
            # a malformed setup is debuggable from Settings.
            detail = (
                ""
                if config_found
                else "Voice config (.onnx.json) not found; Piper will auto-discover."
            )
            return PiperStatus(
                available=True,
                binary_found=True,
                model_found=True,
                config_found=config_found,
                detail=detail,
            )
        if not binary_found:
            detail = "Piper binary not found. Set NOVA_PIPER_BINARY or install piper on PATH."
        else:
            detail = "Voice model not found. Set NOVA_PIPER_VOICE_MODEL to a .onnx file."
        return PiperStatus(
            available=False,
            binary_found=binary_found,
            model_found=model_found,
            config_found=config_found,
            detail=detail,
        )

    # ── Voice profile ───────────────────────────────────────────────

    def voice_config(self) -> VoiceConfig:
        # Piper renders audio server-side — the browser-only voice list
        # is irrelevant here. We still surface the recommended Piper
        # voice slugs so Settings can hint at what to install.
        return VoiceConfig(
            engine=ENGINE_PIPER,
            preferred_voice_names=RECOMMENDED_PIPER_VOICES,
        )

    # ── Synthesis ───────────────────────────────────────────────────

    def synthesize(self, text: str) -> bytes:
        """Render ``text`` as a WAV byte string.

        Raises ``PiperError`` if the binary or model are missing, the
        subprocess fails, or the output file is empty. Callers are
        expected to catch this and fall back to the browser engine.
        """
        if not self.is_available():
            raise PiperError("Piper is not configured")
        binary = self._binary
        model = self._model
        if binary is None or model is None:
            # Defensive — is_available() already covered this, but mypy
            # / readers benefit from the explicit narrowing.
            raise PiperError("Piper is not configured")

        argv: list[str] = [binary, "--model", model]
        if self._config:
            argv.extend(["--config", self._config])

        # WAV output through a tempfile keeps the contract simple and
        # avoids fighting Piper's stdout-mode flag differences across
        # versions (some accept ``--output_file -``, some do not).
        tmp = tempfile.NamedTemporaryFile(prefix="nova-piper-", suffix=".wav", delete=False)
        tmp.close()
        out_path = tmp.name
        argv.extend(["--output_file", out_path])

        try:
            try:
                completed = subprocess.run(
                    argv,
                    input=text,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout,
                    check=False,
                )
            except FileNotFoundError as exc:
                raise PiperError(f"Piper binary missing at runtime: {exc}") from exc
            except subprocess.TimeoutExpired as exc:
                raise PiperError(f"Piper timed out after {self._timeout}s") from exc
            except OSError as exc:
                raise PiperError(f"Piper failed to start: {exc}") from exc

            if completed.returncode != 0:
                stderr = (completed.stderr or "").strip()
                # Avoid logging the user's text — only the binary's own
                # diagnostic line, capped to keep logs readable.
                logger.warning(
                    "Piper exit %s: %s",
                    completed.returncode,
                    stderr[:300] if stderr else "(no stderr)",
                )
                raise PiperError(
                    f"Piper exited with code {completed.returncode}"
                )

            try:
                audio = Path(out_path).read_bytes()
            except OSError as exc:
                raise PiperError(f"Could not read Piper output: {exc}") from exc

            if not audio:
                raise PiperError("Piper produced an empty audio file")
            return audio
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass
