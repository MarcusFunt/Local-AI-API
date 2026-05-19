"""OpenAI-style audio endpoints backed by local Whisper and Chatterbox models."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response

from .. import audio
from ..config import settings
from ..models import AudioSpeechRequest
from ..normalize import resolve_chatterbox_model, resolve_whisper_model

logger = logging.getLogger(__name__)

router = APIRouter()

_TRANSCRIPTION_FORMATS = {"json", "text", "verbose_json"}
_SPEECH_FORMATS = {"wav"}


def _error_response(status_code: int, message: str, code: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "code": code,
            }
        },
    )


def _disabled_whisper_exception(model_alias: str) -> HTTPException:
    return _error_response(
        422,
        (
            f"Whisper transcription is disabled because model '{model_alias}' "
            "resolved to none. Choose tiny, base, or small."
        ),
        "audio_model_disabled",
    )


def _transcription_payload(result: dict[str, Any], verbose: bool) -> dict[str, Any]:
    text = str(result.get("text", ""))
    if not verbose:
        return {"text": text}

    payload: dict[str, Any] = {"text": text}
    if result.get("language") is not None:
        payload["language"] = result["language"]
    if isinstance(result.get("segments"), list):
        payload["segments"] = result["segments"]
    if result.get("duration") is not None:
        payload["duration"] = result["duration"]
    return payload


@router.post("/v1/audio/transcriptions")
async def audio_transcriptions(
    file: UploadFile = File(...),
    model: str | None = Form(None),
    language: str | None = Form(None),
    prompt: str | None = Form(None),
    response_format: str = Form("json"),
    temperature: float | None = Form(None),
) -> Response:
    response_format = response_format.strip().lower()
    if response_format not in _TRANSCRIPTION_FORMATS:
        raise _error_response(
            422,
            "Unsupported transcription response_format. Use json, text, or verbose_json.",
            "unsupported_audio_format",
        )

    model_alias = model if model else settings.default_whisper_model
    resolved_model = resolve_whisper_model(model_alias, settings)
    if resolved_model is None:
        raise _disabled_whisper_exception(model_alias)

    logger.info(
        "Transcription request: alias=%r resolved=%r response_format=%s language=%r",
        model_alias,
        resolved_model,
        response_format,
        language,
    )

    result = await audio.transcribe_with_whisper(
        file=file,
        resolved_model=resolved_model,
        settings=settings,
        language=language,
        prompt=prompt,
        temperature=temperature,
    )
    payload = _transcription_payload(result, verbose=response_format == "verbose_json")

    if response_format == "text":
        return Response(content=payload["text"], media_type="text/plain")
    return JSONResponse(payload)


@router.post("/v1/audio/speech")
async def audio_speech(req: AudioSpeechRequest) -> Response:
    response_format = req.response_format.strip().lower()
    if response_format not in _SPEECH_FORMATS:
        raise _error_response(
            422,
            "Unsupported speech response_format. Chatterbox currently returns wav.",
            "unsupported_audio_format",
        )

    if req.speed is not None and req.speed != 1.0:
        raise _error_response(
            422,
            "Speech speed control is not supported by the local Chatterbox endpoint.",
            "unsupported_speech_option",
        )

    model_alias = req.model if req.model else settings.chatterbox_model
    resolved_model = resolve_chatterbox_model(model_alias, settings)

    logger.info(
        "Speech request: alias=%r resolved=%r response_format=%s chars=%d",
        model_alias,
        resolved_model,
        response_format,
        len(req.input),
    )

    audio_bytes = await audio.synthesize_speech_with_chatterbox(
        text=req.input,
        resolved_model=resolved_model,
        settings=settings,
        language=req.language,
        exaggeration=req.exaggeration,
        cfg_weight=req.cfg_weight,
    )

    return Response(
        content=audio_bytes,
        media_type="audio/wav",
        headers={"Content-Disposition": 'attachment; filename="speech.wav"'},
    )
