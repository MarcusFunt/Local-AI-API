"""Realtime speech-to-speech conversation endpoint."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket
from fastapi.responses import HTMLResponse
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from .. import audio
from .. import client as ollama_client
from ..config import settings
from ..models import ConversationSessionStart
from ..normalize import resolve_chatterbox_model, resolve_model, resolve_whisper_model

logger = logging.getLogger(__name__)

router = APIRouter()

_POLICY_VIOLATION = 1008
_UNSUPPORTED_DATA = 1003
_SPEECH_SYSTEM_PROMPT = (
    "You are in a spoken conversation. Reply in concise, natural speech. "
    "Do not use Markdown, tables, code fences, or bullet lists unless the user "
    "explicitly asks for them."
)
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_CODE_FENCE_RE = re.compile(r"```(?:\w+)?\s*(.*?)```", re.DOTALL)


@dataclass
class ConversationState:
    session_id: str
    config: ConversationSessionStart
    resolved_chat_model: str
    resolved_whisper_model: str
    resolved_tts_model: str
    history: list[dict[str, Any]] = field(default_factory=list)
    collecting_audio: bool = False
    audio_buffer: bytearray = field(default_factory=bytearray)
    response_task: asyncio.Task[None] | None = None


def _error_payload(
    message: str,
    code: str,
    error_type: str = "invalid_request_error",
) -> dict[str, Any]:
    return {
        "type": "error",
        "error": {
            "message": message,
            "type": error_type,
            "code": code,
        },
    }


def _error_from_http_exception(exc: HTTPException) -> dict[str, Any]:
    if isinstance(exc.detail, dict) and isinstance(exc.detail.get("error"), dict):
        return {"type": "error", "error": exc.detail["error"]}
    return _error_payload(str(exc.detail), "error", "api_error")


async def _send_json(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    payload: dict[str, Any],
) -> None:
    async with send_lock:
        await websocket.send_json(payload)


async def _send_bytes(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    payload: bytes,
) -> None:
    async with send_lock:
        await websocket.send_bytes(payload)


async def _send_error(
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    message: str,
    code: str,
    error_type: str = "invalid_request_error",
) -> None:
    await _send_json(websocket, send_lock, _error_payload(message, code, error_type))


def _response_is_active(state: ConversationState) -> bool:
    return state.response_task is not None and not state.response_task.done()


def _speech_text_from_display(text: str) -> str:
    text = _CODE_FENCE_RE.sub(lambda match: match.group(1).strip(), text)
    text = _MARKDOWN_LINK_RE.sub(r"\1", text)
    text = re.sub(r"(\*\*|__)(.*?)\1", r"\2", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!_)_([^_]+)_(?!_)", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)

    cleaned_lines: list[str] = []
    for line in text.splitlines():
        cleaned = re.sub(r"^\s{0,3}(#{1,6}\s+|[-*+]\s+|\d+[\.)]\s+|>\s*)", "", line)
        cleaned = cleaned.strip()
        if cleaned:
            cleaned_lines.append(cleaned)

    return re.sub(r"\s+", " ", " ".join(cleaned_lines)).strip()


def _trim_history(state: ConversationState) -> None:
    max_messages = state.config.max_history_messages
    if len(state.history) > max_messages:
        state.history = state.history[-max_messages:]


async def _rag_context_message(query: str) -> dict[str, str] | None:
    try:
        from ..rag import config as rag_config

        if not rag_config.RAG_ENABLED:
            return None

        from ..rag.store import search as rag_search

        chunks = await rag_search(query, top_k=rag_config.TOP_K)
    except Exception as exc:
        logger.warning("Conversation RAG retrieval failed, continuing without context: %s", exc)
        return None

    if not chunks:
        return None

    context_parts = [
        "[Source: " + c["filename"] + "]\n" + c["text"]
        for c in chunks
    ]
    return {
        "role": "system",
        "content": "Use the following context to answer the question:\n\n" + "\n\n".join(context_parts),
    }


async def _chat_messages_for_turn(state: ConversationState, transcript: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": _SPEECH_SYSTEM_PROMPT}]
    if state.config.instructions:
        messages.append({"role": "system", "content": state.config.instructions})

    if state.config.use_rag:
        context = await _rag_context_message(transcript)
        if context is not None:
            messages.append(context)

    messages.extend(state.history)
    return messages


async def _transcribe_turn(state: ConversationState, audio_bytes: bytes) -> dict[str, Any]:
    return await audio.transcribe_audio_bytes_with_whisper(
        audio_bytes=audio_bytes,
        filename="input." + state.config.input_audio_format,
        resolved_model=state.resolved_whisper_model,
        settings=settings,
        language=state.config.language,
    )


async def _stream_assistant_text(
    state: ConversationState,
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    transcript: str,
) -> tuple[str, str, dict[str, int] | None] | None:
    request_dict: dict[str, Any] = {
        "messages": await _chat_messages_for_turn(state, transcript),
        "temperature": state.config.temperature,
        "top_p": state.config.top_p,
        "max_tokens": state.config.max_tokens,
        "stop": None,
        "seed": state.config.seed,
        "tools": None,
        "tool_choice": None,
        "response_format": {"type": "text"},
        "stream_options": None,
    }

    try:
        events = await ollama_client.proxy_streaming_events(
            state.resolved_chat_model,
            request_dict,
            settings,
        )
    except HTTPException as exc:
        await _send_json(websocket, send_lock, _error_from_http_exception(exc))
        return None

    text_parts: list[str] = []
    finish_reason = "stop"
    usage: dict[str, int] | None = None

    await _send_json(
        websocket,
        send_lock,
        {
            "type": "response.started",
            "response": {"format": "plain_speech_text"},
        },
    )

    async for event in events:
        event_type = event.get("type")
        if event_type == "delta":
            delta = event.get("delta", {})
            content = delta.get("content")
            if content:
                text_parts.append(str(content))
                await _send_json(
                    websocket,
                    send_lock,
                    {"type": "response.text.delta", "delta": str(content)},
                )
            if delta.get("tool_calls"):
                await _send_json(
                    websocket,
                    send_lock,
                    {"type": "response.tool_calls.delta", "tool_calls": delta["tool_calls"]},
                )
        elif event_type == "done":
            finish_reason = str(event.get("finish_reason") or "stop")
            raw_usage = event.get("usage")
            usage = raw_usage if isinstance(raw_usage, dict) else None
        elif event_type == "error":
            error = event.get("error", {})
            await _send_json(
                websocket,
                send_lock,
                {"type": "error", "error": error},
            )
            return None

    return "".join(text_parts).strip(), finish_reason, usage


async def _process_turn(
    state: ConversationState,
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    audio_bytes: bytes,
) -> None:
    try:
        transcription = await _transcribe_turn(state, audio_bytes)
    except HTTPException as exc:
        await _send_json(websocket, send_lock, _error_from_http_exception(exc))
        return

    transcript = str(transcription.get("text", "")).strip()
    if not transcript:
        await _send_error(
            websocket,
            send_lock,
            "Transcription did not produce any text.",
            "empty_transcript",
        )
        return

    await _send_json(
        websocket,
        send_lock,
        {
            "type": "transcript.completed",
            "text": transcript,
            "language": transcription.get("language") or state.config.language,
        },
    )

    state.history.append({"role": "user", "content": transcript})
    _trim_history(state)

    streamed = await _stream_assistant_text(state, websocket, send_lock, transcript)
    if streamed is None:
        return

    display_text, finish_reason, usage = streamed
    speech_text = _speech_text_from_display(display_text)
    if not speech_text:
        await _send_error(
            websocket,
            send_lock,
            "Assistant response did not produce speakable text.",
            "empty_assistant_text",
        )
        return

    await _send_json(
        websocket,
        send_lock,
        {
            "type": "response.text.completed",
            "display_text": display_text,
            "speech_text": speech_text,
        },
    )

    state.history.append({"role": "assistant", "content": display_text})
    _trim_history(state)

    await _send_json(
        websocket,
        send_lock,
        {"type": "response.audio.started", "format": "wav"},
    )

    try:
        audio_bytes = await audio.synthesize_speech_with_chatterbox(
            text=speech_text,
            resolved_model=state.resolved_tts_model,
            settings=settings,
            language=state.config.language,
            exaggeration=state.config.exaggeration,
            cfg_weight=state.config.cfg_weight,
        )
    except HTTPException as exc:
        await _send_json(websocket, send_lock, _error_from_http_exception(exc))
        return

    await _send_bytes(websocket, send_lock, audio_bytes)
    await _send_json(
        websocket,
        send_lock,
        {
            "type": "response.audio.completed",
            "format": "wav",
            "bytes": len(audio_bytes),
        },
    )
    await _send_json(
        websocket,
        send_lock,
        {
            "type": "response.completed",
            "finish_reason": finish_reason,
            "usage": usage,
        },
    )


async def _run_turn_task(
    state: ConversationState,
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    audio_bytes: bytes,
) -> None:
    try:
        await _process_turn(state, websocket, send_lock, audio_bytes)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Conversation response failed: %s", exc)
        with suppress(Exception):
            await _send_error(
                websocket,
                send_lock,
                "Conversation response failed.",
                "conversation_response_error",
                "api_error",
            )
    finally:
        if asyncio.current_task() is state.response_task:
            state.response_task = None


def _parse_control_frame(text: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


async def _receive_start_frame(websocket: WebSocket) -> dict[str, Any] | None:
    message = await websocket.receive()
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))
    if message.get("text") is None:
        return None
    return _parse_control_frame(message["text"])


def _create_state(start_frame: dict[str, Any]) -> ConversationState:
    try:
        config = ConversationSessionStart.model_validate(start_frame)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "message": "Conversation session validation error.",
                    "type": "invalid_request_error",
                    "code": "invalid_request",
                }
            },
        ) from exc

    chat_alias = config.model if config.model else settings.default_model_profile
    whisper_alias = config.whisper_model if config.whisper_model else settings.default_whisper_model
    tts_alias = config.tts_model if config.tts_model else settings.chatterbox_model

    resolved_whisper = resolve_whisper_model(whisper_alias, settings)
    if resolved_whisper is None:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "message": (
                        f"Whisper transcription is disabled because model '{whisper_alias}' "
                        "resolved to none. Choose tiny, base, or small."
                    ),
                    "type": "invalid_request_error",
                    "code": "audio_model_disabled",
                }
            },
        )

    return ConversationState(
        session_id="conv-" + uuid.uuid4().hex,
        config=config,
        resolved_chat_model=resolve_model(chat_alias, settings),
        resolved_whisper_model=resolved_whisper,
        resolved_tts_model=resolve_chatterbox_model(tts_alias, settings),
    )


async def _handle_control_frame(
    state: ConversationState,
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    frame: dict[str, Any],
) -> bool:
    frame_type = frame.get("type")

    if frame_type == "input_audio.start":
        if _response_is_active(state):
            await _send_error(websocket, send_lock, "A response is already active.", "busy")
            return True
        if state.collecting_audio:
            await _send_error(
                websocket,
                send_lock,
                "Input audio has already started.",
                "input_audio_already_started",
            )
            return True
        state.collecting_audio = True
        state.audio_buffer.clear()
        await _send_json(websocket, send_lock, {"type": "input_audio.started"})
        return True

    if frame_type == "input_audio.commit":
        if _response_is_active(state):
            await _send_error(websocket, send_lock, "A response is already active.", "busy")
            return True
        if not state.collecting_audio:
            await _send_error(websocket, send_lock, "No input audio is active.", "no_input_audio")
            return True
        audio_bytes = bytes(state.audio_buffer)
        state.collecting_audio = False
        state.audio_buffer.clear()
        if not audio_bytes:
            await _send_error(websocket, send_lock, "Audio upload is empty.", "empty_audio_file")
            return True
        state.response_task = asyncio.create_task(
            _run_turn_task(state, websocket, send_lock, audio_bytes)
        )
        return True

    if frame_type == "input_audio.clear":
        state.collecting_audio = False
        state.audio_buffer.clear()
        await _send_json(websocket, send_lock, {"type": "input_audio.cleared"})
        return True

    if frame_type == "response.cancel":
        if not _response_is_active(state):
            await _send_error(websocket, send_lock, "No response is active.", "no_active_response")
            return True
        assert state.response_task is not None
        state.response_task.cancel()
        await _send_json(websocket, send_lock, {"type": "response.cancelled"})
        return True

    if frame_type == "ping":
        await _send_json(websocket, send_lock, {"type": "pong"})
        return True

    if frame_type == "session.close":
        await websocket.close(code=1000)
        return False

    await _send_error(websocket, send_lock, "Unknown conversation event type.", "unknown_event")
    return True


async def _handle_binary_frame(
    state: ConversationState,
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    data: bytes,
) -> None:
    if _response_is_active(state):
        await _send_error(websocket, send_lock, "A response is already active.", "busy")
        return
    if not state.collecting_audio:
        await _send_error(
            websocket,
            send_lock,
            "Binary audio frames must follow input_audio.start.",
            "unexpected_audio",
        )
        return

    state.audio_buffer.extend(data)
    if len(state.audio_buffer) > settings.max_request_body_bytes:
        bytes_seen = len(state.audio_buffer)
        state.collecting_audio = False
        state.audio_buffer.clear()
        await _send_error(
            websocket,
            send_lock,
            (
                f"Audio upload too large "
                f"({bytes_seen} bytes > {settings.max_request_body_bytes} byte limit)."
            ),
            "request_too_large",
        )


@router.get("/live-call", response_class=HTMLResponse)
async def live_call_page() -> HTMLResponse:
    """Serve the built-in browser client for a private speech conversation."""
    return HTMLResponse(_LIVE_CALL_HTML)


@router.websocket("/v1/audio/conversations")
async def audio_conversation(websocket: WebSocket) -> None:
    await websocket.accept()
    send_lock = asyncio.Lock()

    try:
        start_frame = await _receive_start_frame(websocket)
    except WebSocketDisconnect:
        return

    if start_frame is None or start_frame.get("type") != "session.start":
        await _send_error(
            websocket,
            send_lock,
            "First WebSocket message must be a session.start JSON event.",
            "invalid_session_start",
        )
        await websocket.close(code=_UNSUPPORTED_DATA)
        return

    try:
        state = _create_state(start_frame)
    except HTTPException as exc:
        await _send_json(websocket, send_lock, _error_from_http_exception(exc))
        await websocket.close(code=_POLICY_VIOLATION)
        return

    logger.info(
        "Conversation session started: id=%s model=%r whisper=%r tts=%r input=%s",
        state.session_id,
        state.resolved_chat_model,
        state.resolved_whisper_model,
        state.resolved_tts_model,
        state.config.input_audio_format,
    )

    await _send_json(
        websocket,
        send_lock,
        {
            "type": "session.created",
            "session": {
                "id": state.session_id,
                "input_audio_format": state.config.input_audio_format,
                "output_audio_format": "wav",
                "text_type": "plain_speech_text",
            },
        },
    )

    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break

            if message.get("bytes") is not None:
                await _handle_binary_frame(state, websocket, send_lock, message["bytes"])
                continue

            text = message.get("text")
            if text is None:
                await _send_error(websocket, send_lock, "Unsupported WebSocket frame.", "unsupported_frame")
                continue

            frame = _parse_control_frame(text)
            if frame is None:
                await _send_error(websocket, send_lock, "Invalid JSON control frame.", "invalid_json")
                continue

            should_continue = await _handle_control_frame(state, websocket, send_lock, frame)
            if not should_continue:
                break
    finally:
        if state.response_task is not None and not state.response_task.done():
            state.response_task.cancel()
            with suppress(asyncio.CancelledError):
                await state.response_task
        logger.info("Conversation session ended: id=%s", state.session_id)


def _load_live_call_html() -> str:
    return (Path(__file__).resolve().parent.parent / "static" / "live-call.html").read_text(
        encoding="utf-8"
    )


_LIVE_CALL_HTML = _load_live_call_html()
