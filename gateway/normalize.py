from fastapi import HTTPException

from .config import Settings

MODEL_MAP: dict[str, str] = {
    "main": "qwen3.5:9b",
    "small": "qwen3.5:4b",
    "dev": "qwen3.5:0.8b",
}

# Direct model tags that are always accepted (same values as map targets)
_ALLOWED_DIRECT = set(MODEL_MAP.values())


def resolve_model(requested: str, settings: Settings) -> str:
    requested = requested.strip()

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
