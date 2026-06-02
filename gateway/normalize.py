from fastapi import HTTPException

from .config import Settings

MODEL_MAP: dict[str, str] = {
    "main": "qwen3.5:9b",
    "small": "qwen3.5:4b",
    "dev": "qwen3.5:0.8b",
}

# Direct model tags that are always accepted (same values as map targets)
_ALLOWED_DIRECT = set(MODEL_MAP.values())

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

    if requested in MODEL_MAP:
        return MODEL_MAP[requested]

    if requested in _ALLOWED_DIRECT:
        return requested

    if settings.enable_arbitrary_models:
        return requested

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
