"""
First-class model profiles — a model is more than a name.

Nova stays **model-flexible**: the operator still points each routing
role at whatever local model they installed, through the same
``NOVA_ROUTER_MODEL`` / ``NOVA_DEFAULT_MODEL`` / ``NOVA_CODE_MODEL`` /
``NOVA_ADVANCED_MODEL`` variables as before. This module adds the small
missing piece: a **profile** that says what Nova should assume *about*
that model — the role it fills, a sane context size, whether it can use
tools, whether it is coding-specialised, roughly how heavy it is, and
any operator note.

Why it exists
-------------
Treating an Ollama tag as an interchangeable string means Nova cannot
size a code-context block, cannot tell a 1B router from a 32B reasoner,
and cannot describe what it is running. A profile makes those answers
explicit and local.

Design rules (deliberate, and deliberately small)
-------------------------------------------------
* **No model database.** A short family table plus parameter-size
  parsing covers the models people actually run locally. Anything
  unknown degrades to a calm ``unknown`` profile — never an error.
* **Local + operator-owned.** An optional JSON file
  (``NOVA_MODEL_PROFILES_PATH``, else ``<data root>/model-profiles.json``)
  overlays or adds profiles. It is read-only, validated, size-capped,
  and absent by default — an existing install needs no migration and no
  new file.
* **Never a download trigger.** Nothing here contacts a backend, pulls,
  or generates. Profiles are metadata, resolved offline.
* **Never a privilege.** ``supports_tools`` records a *capability claim*
  about a model; it grants nothing. Nova's tool routing, safety
  contract, and Dev Workspace boundaries are unchanged by any profile.
* **Never raises.** Every public function degrades to a default rather
  than breaking the chat hot path.

Future ``nova-coder``
---------------------
A fine-tuned derivative (e.g. ``NOVA_CODE_MODEL=nova-coder:14b``) needs
*no code change* to work: the ``nova-coder`` family is already known
here, so it resolves to a coding-specialised profile with a large
context. See ``docs/model-profiles.md``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Roles ───────────────────────────────────────────────────────────
#
# Nova's four routing roles. ``chat`` is the public name for what
# ``config.MODELS`` calls ``default`` — the env var and the config key
# are unchanged, this is only a friendlier label.
ROLE_ROUTER = "router"
ROLE_CHAT = "chat"
ROLE_CODE = "code"
ROLE_ADVANCED = "advanced"

ROLES: tuple[str, ...] = (ROLE_ROUTER, ROLE_CHAT, ROLE_CODE, ROLE_ADVANCED)

#: role -> the key it occupies in ``config.MODELS`` (compatibility map).
ROLE_CONFIG_KEYS: dict[str, str] = {
    ROLE_ROUTER: "router",
    ROLE_CHAT: "default",
    ROLE_CODE: "code",
    ROLE_ADVANCED: "advanced",
}

#: role -> the env var an operator sets for it (documentation only).
ROLE_ENV_VARS: dict[str, str] = {
    ROLE_ROUTER: "NOVA_ROUTER_MODEL",
    ROLE_CHAT: "NOVA_DEFAULT_MODEL",
    ROLE_CODE: "NOVA_CODE_MODEL",
    ROLE_ADVANCED: "NOVA_ADVANCED_MODEL",
}

# ── Resource classes ────────────────────────────────────────────────
#
# Coarse on purpose. Nova only needs "will this plausibly run here and
# how much context can I afford", not a benchmark score.
CLASS_TINY = "tiny"        # < 2B params
CLASS_SMALL = "small"      # 2B - 5B
CLASS_MEDIUM = "medium"    # 6B - 15B
CLASS_LARGE = "large"      # 16B - 40B
CLASS_XLARGE = "xlarge"    # > 40B
CLASS_UNKNOWN = "unknown"

RESOURCE_CLASSES: tuple[str, ...] = (
    CLASS_TINY, CLASS_SMALL, CLASS_MEDIUM,
    CLASS_LARGE, CLASS_XLARGE, CLASS_UNKNOWN,
)

#: Where a profile's values came from, so the admin surface can say so.
SOURCE_BUILTIN = "builtin"      # family table / parameter inference
SOURCE_OVERRIDE = "override"    # operator JSON overlay
SOURCE_UNKNOWN = "unknown"      # no family matched; conservative default

# Context sizes Nova is willing to *recommend*. These are hints for
# building bounded prompts, not hard limits imposed on a backend.
_MIN_CONTEXT = 512
_MAX_CONTEXT = 1_048_576

#: Fallback recommended context per role when nothing better is known.
_ROLE_CONTEXT_DEFAULTS: dict[str, int] = {
    ROLE_ROUTER: 2048,
    ROLE_CHAT: 8192,
    ROLE_CODE: 16384,
    ROLE_ADVANCED: 16384,
}


@dataclass(frozen=True)
class _Family:
    """One entry in the (small) builtin family table."""

    prefix: str
    code_specialized: bool = False
    supports_tools: bool = False
    context_size: Optional[int] = None
    notes: str = ""


# Longest prefix wins, so ``qwen2.5-coder`` beats ``qwen2.5``. Kept
# short on purpose: this is a heuristic table, not a model registry.
_FAMILIES: tuple[_Family, ...] = (
    _Family(
        "nova-coder", code_specialized=True, supports_tools=True,
        context_size=32768,
        notes="Nova-specific coding model (fine-tuned derivative).",
    ),
    _Family("deepseek-coder", code_specialized=True, context_size=16384),
    _Family("deepseek-v", supports_tools=True, context_size=32768),
    _Family("qwen2.5-coder", code_specialized=True, supports_tools=True,
            context_size=32768),
    _Family("qwen3-coder", code_specialized=True, supports_tools=True,
            context_size=32768),
    _Family("codellama", code_specialized=True, context_size=16384),
    _Family("codegemma", code_specialized=True, context_size=8192),
    _Family("codestral", code_specialized=True, context_size=32768),
    _Family("starcoder", code_specialized=True, context_size=8192),
    _Family("granite-code", code_specialized=True, context_size=8192),
    _Family("qwen2.5", supports_tools=True, context_size=32768),
    _Family("qwen3", supports_tools=True, context_size=32768),
    _Family("llama3", supports_tools=True, context_size=8192),
    _Family("mistral", supports_tools=True, context_size=32768),
    _Family("mixtral", supports_tools=True, context_size=32768),
    _Family("gemma4", context_size=8192),
    _Family("gemma3", context_size=8192),
    _Family("gemma2", context_size=8192),
    _Family("phi", context_size=8192),
    _Family("tinyllama", context_size=2048),
)

# ``14b`` / ``1.5b`` / ``32B`` anywhere in the tag, and quantisation
# markers such as ``q4_K_M`` / ``Q8_0``.
_PARAM_RE = re.compile(r"(?<![a-z0-9.])(\d+(?:\.\d+)?)\s*b(?![a-z0-9])", re.I)
_QUANT_RE = re.compile(r"(?<![a-z0-9])(?:iq|q)(\d)(?:[_-][a-z0-9]+)*", re.I)

#: Defensive caps so a crafted override file cannot balloon anything.
MAX_MODEL_NAME_LEN = 200
MAX_NOTES_LEN = 300
_MAX_OVERRIDE_BYTES = 256 * 1024
_MAX_OVERRIDE_ENTRIES = 200

ENV_PROFILES_PATH = "NOVA_MODEL_PROFILES_PATH"
PROFILES_FILENAME = "model-profiles.json"


@dataclass(frozen=True)
class ModelProfile:
    """What Nova assumes about one local model.

    Every field is a *description*, never a permission. ``supports_tools``
    records that a model was trained for tool syntax; it does not enable
    any tool, and it can never widen what Nova will do on the user's
    behalf.
    """

    name: str
    role: str = ROLE_CHAT
    context_size: int = 8192
    supports_tools: bool = False
    code_specialized: bool = False
    resource_class: str = CLASS_UNKNOWN
    parameter_size_b: Optional[float] = None
    quantization: str = ""
    notes: str = ""
    source: str = SOURCE_BUILTIN

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "role": self.role,
            "context_size": self.context_size,
            "supports_tools": self.supports_tools,
            "code_specialized": self.code_specialized,
            "resource_class": self.resource_class,
            "parameter_size_b": self.parameter_size_b,
            "quantization": self.quantization,
            "notes": self.notes,
            "source": self.source,
        }


# ── Inference helpers ───────────────────────────────────────────────


def normalize_model_name(raw: object) -> str:
    """A trimmed, length-capped model name, or ``""``.

    Never raises: a non-string, an empty string, or an oversized blob
    all degrade to ``""`` so callers can treat "no usable name" as one
    case.
    """
    if not isinstance(raw, str):
        return ""
    name = raw.strip()
    if not name or len(name) > MAX_MODEL_NAME_LEN:
        return ""
    return name


def _base_name(name: str) -> str:
    """The family part of an Ollama tag: ``qwen2.5-coder:14b`` -> that stem.

    Strips any registry/namespace prefix (``hf.co/user/model``) and the
    ``:tag`` suffix, lowercased.
    """
    stem = name.split(":", 1)[0]
    if "/" in stem:
        stem = stem.rsplit("/", 1)[-1]
    return stem.strip().lower()


def _match_family(name: str) -> Optional[_Family]:
    base = _base_name(name)
    if not base:
        return None
    best: Optional[_Family] = None
    for fam in _FAMILIES:
        if base.startswith(fam.prefix):
            if best is None or len(fam.prefix) > len(best.prefix):
                best = fam
    return best


def parse_parameter_size(name: str) -> Optional[float]:
    """Billions of parameters parsed from the tag, else ``None``.

    ``qwen2.5:32b`` -> 32.0, ``model-1.5b-instruct`` -> 1.5. A value that
    is not plausible for a local model is discarded rather than trusted.
    """
    best: Optional[float] = None
    for match in _PARAM_RE.finditer(name):
        try:
            value = float(match.group(1))
        except (TypeError, ValueError):
            continue
        if not (0.05 <= value <= 2000):
            continue
        if best is None or value > best:
            best = value
    return best


def parse_quantization(name: str) -> str:
    """A normalised quantisation marker (``q4``, ``q8``…) or ``""``."""
    match = _QUANT_RE.search(name)
    if match is None:
        return ""
    return f"q{match.group(1)}"


def resource_class_for(parameter_size_b: Optional[float]) -> str:
    """Coarse weight class from a parameter count."""
    if parameter_size_b is None:
        return CLASS_UNKNOWN
    if parameter_size_b < 2:
        return CLASS_TINY
    if parameter_size_b < 6:
        return CLASS_SMALL
    if parameter_size_b < 16:
        return CLASS_MEDIUM
    if parameter_size_b < 41:
        return CLASS_LARGE
    return CLASS_XLARGE


def clamp_context(value: object, fallback: int = 8192) -> int:
    """A sane, bounded context size; anything unusable falls back."""
    try:
        size = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    if size < _MIN_CONTEXT:
        return fallback
    return min(size, _MAX_CONTEXT)


def _normalize_role(role: object, default: str = ROLE_CHAT) -> str:
    if not isinstance(role, str):
        return default
    candidate = role.strip().lower()
    if candidate == "default":  # config's name for the chat role
        return ROLE_CHAT
    return candidate if candidate in ROLES else default


# ── Operator override file ──────────────────────────────────────────


def profiles_path() -> Optional[Path]:
    """Where the optional operator overlay lives, or ``None``.

    ``NOVA_MODEL_PROFILES_PATH`` wins when set. Otherwise Nova looks for
    ``model-profiles.json`` beside its other data. Never creates the
    file — an absent overlay is the normal, supported state.
    """
    raw = (os.getenv(ENV_PROFILES_PATH, "") or "").strip()
    if raw:
        try:
            return Path(raw).expanduser()
        except (OSError, ValueError):
            return None
    try:
        from core import paths

        return paths.effective_data_root() / PROFILES_FILENAME
    except Exception as exc:  # pragma: no cover - paths is stable
        logger.debug("model profiles: data root lookup failed: %s", exc)
        return None


def _coerce_override(entry: object) -> Optional[ModelProfile]:
    """One validated overlay entry, or ``None`` when unusable."""
    if not isinstance(entry, dict):
        return None
    name = normalize_model_name(entry.get("name") or entry.get("model"))
    if not name:
        return None
    role = _normalize_role(entry.get("role"))
    inferred = _infer_profile(name, role)

    context_size = (
        clamp_context(entry["context_size"], inferred.context_size)
        if "context_size" in entry else inferred.context_size
    )
    supports_tools = (
        bool(entry["supports_tools"]) if isinstance(
            entry.get("supports_tools"), bool
        ) else inferred.supports_tools
    )
    code_specialized = (
        bool(entry["code_specialized"]) if isinstance(
            entry.get("code_specialized"), bool
        ) else inferred.code_specialized
    )
    raw_class = entry.get("resource_class")
    resource_class = (
        raw_class if isinstance(raw_class, str)
        and raw_class in RESOURCE_CLASSES else inferred.resource_class
    )
    raw_notes = entry.get("notes")
    notes = (
        raw_notes.strip()[:MAX_NOTES_LEN]
        if isinstance(raw_notes, str) else inferred.notes
    )
    return ModelProfile(
        name=name,
        role=role,
        context_size=context_size,
        supports_tools=supports_tools,
        code_specialized=code_specialized,
        resource_class=resource_class,
        parameter_size_b=inferred.parameter_size_b,
        quantization=inferred.quantization,
        notes=notes,
        source=SOURCE_OVERRIDE,
    )


def load_overrides(path: Optional[Path] = None) -> dict[str, ModelProfile]:
    """Read the operator overlay into ``{model_name: profile}``.

    Never raises and never partially fails a caller: a missing file, a
    file that is too large, malformed JSON, or an entry that does not
    validate all degrade to "no override for that model". Accepts either
    a top-level list or ``{"profiles": [...]}``.
    """
    target = path if path is not None else profiles_path()
    if target is None:
        return {}
    try:
        if not target.is_file():
            return {}
        if target.stat().st_size > _MAX_OVERRIDE_BYTES:
            logger.warning(
                "model profiles: overlay is too large, ignoring it"
            )
            return {}
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        logger.warning("model profiles: overlay unreadable (%s)", type(exc).__name__)
        return {}

    if isinstance(payload, dict):
        entries = payload.get("profiles", [])
    else:
        entries = payload
    if not isinstance(entries, list):
        return {}

    out: dict[str, ModelProfile] = {}
    for entry in entries[:_MAX_OVERRIDE_ENTRIES]:
        profile = _coerce_override(entry)
        if profile is not None:
            out[profile.name] = profile
    return out


# ── Public resolution ───────────────────────────────────────────────


def _infer_profile(name: str, role: str) -> ModelProfile:
    """Builtin inference for ``name`` in ``role`` — no overlay, no I/O."""
    family = _match_family(name)
    parameter_size = parse_parameter_size(name)
    quantization = parse_quantization(name)
    role_default = _ROLE_CONTEXT_DEFAULTS.get(role, 8192)

    if family is None:
        return ModelProfile(
            name=name,
            role=role,
            context_size=role_default,
            supports_tools=False,
            code_specialized=(role == ROLE_CODE),
            resource_class=resource_class_for(parameter_size),
            parameter_size_b=parameter_size,
            quantization=quantization,
            notes="No builtin profile for this model family; using "
                  "conservative defaults.",
            source=SOURCE_UNKNOWN,
        )

    context_size = clamp_context(family.context_size or role_default, role_default)
    # The router role is a one-word classifier: never spend a big window
    # on it even when the underlying family supports one.
    if role == ROLE_ROUTER:
        context_size = min(context_size, _ROLE_CONTEXT_DEFAULTS[ROLE_ROUTER])
    return ModelProfile(
        name=name,
        role=role,
        context_size=context_size,
        supports_tools=family.supports_tools,
        code_specialized=family.code_specialized,
        resource_class=resource_class_for(parameter_size),
        parameter_size_b=parameter_size,
        quantization=quantization,
        notes=family.notes,
        source=SOURCE_BUILTIN,
    )


def profile_for(
    model: object,
    role: Optional[str] = None,
    *,
    overrides: Optional[dict[str, ModelProfile]] = None,
) -> ModelProfile:
    """The profile Nova assumes for ``model`` when used in ``role``.

    Resolution order: operator overlay → builtin family table →
    conservative unknown defaults. Never raises; an unusable name yields
    a named-``unknown`` profile so callers always get an object.
    """
    resolved_role = _normalize_role(role)
    name = normalize_model_name(model)
    if not name:
        return ModelProfile(
            name="",
            role=resolved_role,
            context_size=_ROLE_CONTEXT_DEFAULTS.get(resolved_role, 8192),
            resource_class=CLASS_UNKNOWN,
            notes="No model is configured for this role.",
            source=SOURCE_UNKNOWN,
        )

    table = overrides if overrides is not None else load_overrides()
    override = table.get(name)
    if override is not None:
        # An overlay entry is authored per model, not per role; keep the
        # operator's values but report the role it is being used in.
        return replace(override, role=resolved_role)
    return _infer_profile(name, resolved_role)


def configured_role_models() -> dict[str, str]:
    """``{role: model_name}`` for the four roles, honouring existing config.

    The chat role follows the admin-selected default when one is
    persisted (exactly as ``core.router`` and the chat endpoints already
    do); every other role reads ``config.MODELS``, so
    ``NOVA_ROUTER_MODEL`` / ``NOVA_CODE_MODEL`` / ``NOVA_ADVANCED_MODEL``
    keep working unchanged. Never raises.
    """
    try:
        from config import MODELS

        source = MODELS or {}
    except Exception as exc:  # pragma: no cover - config is stable
        logger.warning("model profiles: config import failed: %s", exc)
        source = {}

    out: dict[str, str] = {}
    for role in ROLES:
        key = ROLE_CONFIG_KEYS[role]
        out[role] = normalize_model_name(source.get(key, ""))

    try:
        from core.model_settings import resolve_default_model

        chat_model = normalize_model_name(resolve_default_model())
        if chat_model:
            out[ROLE_CHAT] = chat_model
    except Exception as exc:  # never block on a settings read
        logger.debug("model profiles: default-model read failed: %s", exc)
    return out


def role_profiles() -> dict[str, ModelProfile]:
    """``{role: ModelProfile}`` for Nova's four roles. Never raises."""
    overrides = load_overrides()
    models = configured_role_models()
    return {
        role: profile_for(models.get(role, ""), role, overrides=overrides)
        for role in ROLES
    }


def profile_for_role(role: str) -> ModelProfile:
    """The profile of whichever model currently fills ``role``."""
    return role_profiles().get(
        _normalize_role(role), profile_for("", role)
    )


def context_size_for_role(role: str) -> int:
    """Recommended context window for ``role``'s configured model."""
    return profile_for_role(role).context_size


def describe_profiles() -> dict:
    """JSON-serialisable snapshot for the admin surface. Never raises.

    Reports the resolved profile per role, the overlay path and whether
    it was found, and the env var an operator would change. Contains no
    secrets and performs no network I/O.
    """
    overlay = profiles_path()
    overlay_str = str(overlay) if overlay is not None else ""
    overlay_present = False
    try:
        overlay_present = overlay is not None and overlay.is_file()
    except OSError:
        overlay_present = False

    profiles = role_profiles()
    return {
        "roles": [
            {
                "role": role,
                "env_var": ROLE_ENV_VARS[role],
                "config_key": ROLE_CONFIG_KEYS[role],
                "profile": profiles[role].as_dict(),
            }
            for role in ROLES
        ],
        "overrides_path": overlay_str,
        "overrides_present": overlay_present,
        "resource_classes": list(RESOURCE_CLASSES),
    }


__all__ = [
    "ROLE_ROUTER", "ROLE_CHAT", "ROLE_CODE", "ROLE_ADVANCED", "ROLES",
    "ROLE_CONFIG_KEYS", "ROLE_ENV_VARS",
    "CLASS_TINY", "CLASS_SMALL", "CLASS_MEDIUM", "CLASS_LARGE",
    "CLASS_XLARGE", "CLASS_UNKNOWN", "RESOURCE_CLASSES",
    "SOURCE_BUILTIN", "SOURCE_OVERRIDE", "SOURCE_UNKNOWN",
    "ENV_PROFILES_PATH", "PROFILES_FILENAME",
    "MAX_MODEL_NAME_LEN", "MAX_NOTES_LEN",
    "ModelProfile",
    "normalize_model_name", "parse_parameter_size", "parse_quantization",
    "resource_class_for", "clamp_context",
    "profiles_path", "load_overrides",
    "profile_for", "configured_role_models", "role_profiles",
    "profile_for_role", "context_size_for_role", "describe_profiles",
]
