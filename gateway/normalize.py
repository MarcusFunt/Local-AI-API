from fastapi import HTTPException

from .config import Settings

MODEL_MAP: dict[str, str] = {
    "main": "qwen3.5:9b",
    # The quality profile is deliberately separate from `main`: callers can
    # benchmark and promote quality-agent behavior without changing ordinary
    # chat defaults.
    "quality": "qwen3.5:9b",
    "small": "qwen3.5:4b",
    "dev": "qwen3.5:0.8b",
    "agent": "qwen3:14b",
    # Agent Zero's utility work is quality-critical memory consolidation, not
    # a cheap background task. Keep it on the same 14B model as the agent.
    "agent-utility": "qwen3:14b",
}

EMBEDDING_MODEL_MAP: dict[str, str] = {
    "embedding": "nomic-embed-text",
}

CORE_MODEL_ALIASES = ("main", "quality", "small", "dev")
AGENT_ZERO_MODEL_ALIASES = ("agent", "agent-utility")
REQUIRED_MODEL_ALIASES = CORE_MODEL_ALIASES + AGENT_ZERO_MODEL_ALIASES

# Direct model tags that are always accepted (same values as map targets)
_ALLOWED_DIRECT = set(MODEL_MAP.values())
_ALLOWED_EMBEDDING_DIRECT = set(EMBEDDING_MODEL_MAP.values())

_SAFE_PROVIDER_PREFIXES = ("openai/",)

WHISPER_MODEL_MAP: dict[str, str | None] = {
    "none": None,
    "tiny": "tiny",
    "base": "base",
    "small": "small",
}

CHATTERBOX_MODEL_MAP: dict[str, str] = {
    "chatterbox": "chatterbox",
    "chatterbox-multilingual": "chatterbox-multilingual",
}


def strip_safe_provider_prefix(requested: str) -> str:
    requested = requested.strip()
    lowered = requested.lower()
    for prefix in _SAFE_PROVIDER_PREFIXES:
        if lowered.startswith(prefix):
            return requested[len(prefix):].strip()
    return requested


def required_model_aliases() -> tuple[str, ...]:
    return REQUIRED_MODEL_ALIASES


def allowed_model_ids() -> list[str]:
    ids: list[str] = []
    for value in [
        *MODEL_MAP.keys(),
        *MODEL_MAP.values(),
        *EMBEDDING_MODEL_MAP.keys(),
        *EMBEDDING_MODEL_MAP.values(),
    ]:
        if value not in ids:
            ids.append(value)
    return ids


def resolve_model(requested: str, settings: Settings) -> str:
    requested = requested.strip()
    if not requested:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "message": "Model must not be empty.",
                    "type": "invalid_request_error",
                    "code": "model_not_found",
                }
            },
        )

    normalized = strip_safe_provider_prefix(requested)

    if normalized in MODEL_MAP:
        return MODEL_MAP[normalized]

    if normalized in _ALLOWED_DIRECT:
        return normalized

    if settings.enable_arbitrary_models:
        return normalized

    raise HTTPException(
        status_code=422,
        detail={
            "error": {
                "message": (
                    f"Model '{requested}' is not allowed. "
                    f"Allowed aliases: {list(MODEL_MAP)}. "
                    "Set ENABLE_ARBITRARY_MODELS=true to pass arbitrary model names."
                ),
                "type": "invalid_request_error",
                "code": "model_not_found",
            }
        },
    )


def resolve_embedding_model(requested: str, settings: Settings) -> str:
    """Resolve an embedding alias without widening the chat-model allow-list."""
    requested = requested.strip()
    if not requested:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "message": "Embedding model must not be empty.",
                    "type": "invalid_request_error",
                    "code": "model_not_found",
                }
            },
        )

    normalized = strip_safe_provider_prefix(requested)
    if normalized in EMBEDDING_MODEL_MAP:
        return EMBEDDING_MODEL_MAP[normalized]
    if normalized in _ALLOWED_EMBEDDING_DIRECT:
        return normalized
    if settings.enable_arbitrary_models:
        return normalized
    raise HTTPException(
        status_code=422,
        detail={
            "error": {
                "message": (
                    f"Embedding model '{requested}' is not allowed. "
                    f"Allowed aliases: {list(EMBEDDING_MODEL_MAP)}."
                ),
                "type": "invalid_request_error",
                "code": "model_not_found",
            }
        },
    )


def resolve_whisper_model(requested: str, settings: Settings) -> str | None:
    requested = requested.strip()

    if requested in WHISPER_MODEL_MAP:
        return WHISPER_MODEL_MAP[requested]

    raise HTTPException(
        status_code=422,
        detail={
            "error": {
                "message": (
                    f"Whisper model '{requested}' is not allowed. "
                    f"Allowed values: {list(WHISPER_MODEL_MAP)}."
                ),
                "type": "invalid_request_error",
                "code": "audio_model_not_found",
            }
        },
    )


def resolve_chatterbox_model(requested: str, settings: Settings) -> str:
    requested = requested.strip()

    if requested in CHATTERBOX_MODEL_MAP:
        return CHATTERBOX_MODEL_MAP[requested]

    raise HTTPException(
        status_code=422,
        detail={
            "error": {
                "message": (
                    f"Chatterbox model '{requested}' is not allowed. "
                    f"Allowed values: {list(CHATTERBOX_MODEL_MAP)}."
                ),
                "type": "invalid_request_error",
                "code": "audio_model_not_found",
            }
        },
    )
