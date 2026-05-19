"""Local speech-to-text and text-to-speech helpers.

Heavy speech libraries are imported lazily so chat-only startup and tests do not
load model stacks unless an audio endpoint is called.
"""
from __future__ import annotations

import asyncio
import io
import logging
import tempfile
from pathlib import Path
from typing import Any

from fastapi import HTTPException, UploadFile

from .config import Settings

logger = logging.getLogger(__name__)

_WHISPER_MODELS: dict[tuple[str, str], Any] = {}
_CHATTERBOX_MODELS: dict[tuple[str, str], Any] = {}


def _audio_exception(
    status_code: int,
    message: str,
    code: str,
    error_type: str = "invalid_request_error",
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "error": {
                "message": message,
                "type": error_type,
                "code": code,
            }
        },
    )


def _missing_dependency_exception(feature: str, package: str) -> HTTPException:
    return _audio_exception(
        503,
        (
            f"{feature} support requires {package}. "
            "Install the audio dependencies with: pip install -r requirements-audio.txt"
        ),
        "missing_audio_dependency",
        "service_unavailable",
    )


def _model_runtime_exception(feature: str, exc: Exception) -> HTTPException:
    return _audio_exception(
        502,
        f"{feature} failed: {exc}",
        "audio_model_error",
        "upstream_error",
    )


def _select_device(configured: str) -> str:
    configured = configured.strip().lower()
    if configured and configured != "auto":
        return configured

    try:
        import torch
    except ImportError:
        return "cpu"

    if torch.cuda.is_available():
        return "cuda"
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _audio_suffix(filename: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    if not suffix or len(suffix) > 16:
        return ".audio"
    return suffix


async def _read_upload_to_tempfile(file: UploadFile) -> Path:
    data = await file.read()
    if not data:
        raise _audio_exception(
            422,
            "Audio upload is empty.",
            "empty_audio_file",
        )

    temp = tempfile.NamedTemporaryFile(delete=False, suffix=_audio_suffix(file.filename))
    temp_path = Path(temp.name)
    try:
        temp.write(data)
    finally:
        temp.close()
    return temp_path


def _load_whisper_model(model_name: str, device: str) -> Any:
    try:
        import whisper
    except ImportError as exc:
        raise _missing_dependency_exception("Whisper transcription", "openai-whisper") from exc

    cache_key = (model_name, device)
    model = _WHISPER_MODELS.get(cache_key)
    if model is None:
        logger.info("Loading Whisper model (model=%s device=%s)", model_name, device)
        model = whisper.load_model(model_name, device=device)
        _WHISPER_MODELS[cache_key] = model
    return model


async def transcribe_with_whisper(
    file: UploadFile,
    resolved_model: str,
    settings: Settings,
    language: str | None = None,
    prompt: str | None = None,
    temperature: float | None = None,
) -> dict[str, Any]:
    temp_path = await _read_upload_to_tempfile(file)
    device = _select_device(settings.whisper_device)

    def run_transcription() -> dict[str, Any]:
        model = _load_whisper_model(resolved_model, device)
        options: dict[str, Any] = {}
        if language:
            options["language"] = language
        if prompt:
            options["initial_prompt"] = prompt
        if temperature is not None:
            options["temperature"] = temperature
        result = model.transcribe(str(temp_path), **options)
        if not isinstance(result, dict):
            raise ValueError("Whisper returned an unexpected response.")
        return result

    try:
        return await asyncio.to_thread(run_transcription)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Whisper transcription failed: %s", exc)
        raise _model_runtime_exception("Whisper transcription", exc) from exc
    finally:
        temp_path.unlink(missing_ok=True)


def _load_chatterbox_model(resolved_model: str, device: str) -> Any:
    cache_key = (resolved_model, device)
    model = _CHATTERBOX_MODELS.get(cache_key)
    if model is not None:
        return model

    try:
        if resolved_model == "chatterbox-multilingual":
            from chatterbox.mtl_tts import ChatterboxMultilingualTTS

            logger.info("Loading Chatterbox multilingual model (device=%s)", device)
            model = ChatterboxMultilingualTTS.from_pretrained(device=device)
        else:
            from chatterbox.tts import ChatterboxTTS

            logger.info("Loading Chatterbox model (device=%s)", device)
            model = ChatterboxTTS.from_pretrained(device=device)
    except ImportError as exc:
        raise _missing_dependency_exception("Chatterbox text-to-speech", "chatterbox-tts") from exc

    _CHATTERBOX_MODELS[cache_key] = model
    return model


def _wav_bytes_from_tensor(wav: Any, sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    try:
        import torchaudio as ta

        ta.save(buffer, wav, sample_rate, format="wav")
        return buffer.getvalue()
    except ImportError as exc:
        raise _missing_dependency_exception("Chatterbox text-to-speech", "torchaudio") from exc


async def synthesize_speech_with_chatterbox(
    text: str,
    resolved_model: str,
    settings: Settings,
    language: str | None = None,
    exaggeration: float | None = None,
    cfg_weight: float | None = None,
) -> bytes:
    text = text.strip()
    if not text:
        raise _audio_exception(
            422,
            "Speech input must not be empty.",
            "empty_speech_input",
        )

    device = _select_device(settings.chatterbox_device)

    def run_synthesis() -> bytes:
        model = _load_chatterbox_model(resolved_model, device)
        options: dict[str, Any] = {}
        if resolved_model == "chatterbox-multilingual":
            options["language_id"] = language or "en"
        if exaggeration is not None:
            options["exaggeration"] = exaggeration
        if cfg_weight is not None:
            options["cfg_weight"] = cfg_weight

        wav = model.generate(text, **options)
        sample_rate = int(getattr(model, "sr", 24000))
        return _wav_bytes_from_tensor(wav, sample_rate)

    try:
        return await asyncio.to_thread(run_synthesis)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Chatterbox synthesis failed: %s", exc)
        raise _model_runtime_exception("Chatterbox text-to-speech", exc) from exc
