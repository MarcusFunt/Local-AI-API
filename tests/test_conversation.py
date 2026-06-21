"""Tests for realtime speech-to-speech conversation WebSockets."""
from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException
from fastapi.testclient import TestClient
import httpx
import pytest
import respx
from starlette.websockets import WebSocketDisconnect

from gateway.config import Settings
from gateway.main import create_app

OLLAMA_BASE = "http://127.0.0.1:11434"


def _ndjson(*chunks: dict[str, Any]) -> bytes:
    return b"\n".join(json.dumps(c).encode() for c in chunks) + b"\n"


def _patch_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    import gateway.config as cfg_module
    import gateway.routes.audio as audio_route
    import gateway.routes.chat as chat_route
    import gateway.routes.conversation as conversation_route
    import gateway.routes.health as health_route
    import gateway.routes.status as status_route
    from gateway import main as main_module

    monkeypatch.setattr(cfg_module, "settings", settings)
    monkeypatch.setattr(audio_route, "settings", settings)
    monkeypatch.setattr(chat_route, "settings", settings)
    monkeypatch.setattr(conversation_route, "settings", settings)
    monkeypatch.setattr(health_route, "settings", settings)
    monkeypatch.setattr(status_route, "settings", settings)
    monkeypatch.setattr(main_module, "settings", settings)


def _settings(**overrides: Any) -> Settings:
    values = {
        "ollama_base_url": OLLAMA_BASE,
        "enable_api_key_auth": False,
        "api_key": "",
        "enable_arbitrary_models": False,
        "request_timeout_seconds": 10,
        "max_request_body_bytes": 10_485_760,
    }
    values.update(overrides)
    return Settings(**values)


def _start_session(ws: Any, **overrides: Any) -> dict[str, Any]:
    frame: dict[str, Any] = {"type": "session.start", "whisper_model": "tiny"}
    frame.update(overrides)
    ws.send_json(frame)
    return ws.receive_json()


def test_conversation_happy_path(monkeypatch: pytest.MonkeyPatch):
    settings = _settings()
    _patch_settings(monkeypatch, settings)

    import gateway.audio as audio_module

    captured: dict[str, Any] = {}

    async def fake_transcribe(**kwargs: Any) -> dict[str, Any]:
        captured["transcribe"] = kwargs
        return {"text": "hello there", "language": "en"}

    async def fake_speech(**kwargs: Any) -> bytes:
        captured["speech"] = kwargs
        return b"RIFF....WAVE"

    monkeypatch.setattr(audio_module, "transcribe_audio_bytes_with_whisper", fake_transcribe)
    monkeypatch.setattr(audio_module, "synthesize_speech_with_chatterbox", fake_speech)

    captured_ollama: list[dict[str, Any]] = []

    async def capture_ollama(request: httpx.Request, *args: Any, **kwargs: Any) -> httpx.Response:
        captured_ollama.append(json.loads(request.content))
        return httpx.Response(
            200,
            content=_ndjson(
                {
                    "model": "qwen3.5:0.8b",
                    "message": {"role": "assistant", "content": "Hello "},
                    "done": False,
                },
                {
                    "model": "qwen3.5:0.8b",
                    "message": {
                        "role": "assistant",
                        "content": "[world](https://example.test).",
                    },
                    "done": False,
                },
                {
                    "model": "qwen3.5:0.8b",
                    "message": {"role": "assistant", "content": ""},
                    "done": True,
                    "prompt_eval_count": 7,
                    "eval_count": 3,
                },
            ),
        )

    with respx.mock(base_url=OLLAMA_BASE) as mock:
        mock.post("/api/chat").mock(side_effect=capture_ollama)
        with TestClient(create_app()) as client:
            with client.websocket_connect("/v1/audio/conversations") as ws:
                ws.send_json(
                    {
                        "type": "session.start",
                        "model": "dev",
                        "whisper_model": "small",
                        "tts_model": "chatterbox",
                        "input_audio_format": "wav",
                        "language": "en",
                        "max_tokens": 64,
                    }
                )
                created = ws.receive_json()
                assert created["type"] == "session.created"
                assert created["session"]["text_type"] == "plain_speech_text"

                ws.send_json({"type": "input_audio.start"})
                assert ws.receive_json()["type"] == "input_audio.started"
                ws.send_bytes(b"RIFF....WAVE")
                ws.send_json({"type": "input_audio.commit"})

                transcript = ws.receive_json()
                assert transcript["type"] == "transcript.completed"
                assert transcript["text"] == "hello there"

                assert ws.receive_json()["type"] == "response.started"
                assert ws.receive_json() == {"type": "response.text.delta", "delta": "Hello "}
                assert ws.receive_json() == {
                    "type": "response.text.delta",
                    "delta": "[world](https://example.test).",
                }

                completed_text = ws.receive_json()
                assert completed_text["type"] == "response.text.completed"
                assert completed_text["display_text"] == "Hello [world](https://example.test)."
                assert completed_text["speech_text"] == "Hello world."

                assert ws.receive_json() == {"type": "response.audio.started", "format": "wav"}
                assert ws.receive_bytes() == b"RIFF....WAVE"
                assert ws.receive_json() == {
                    "type": "response.audio.completed",
                    "format": "wav",
                    "bytes": 12,
                }
                response_done = ws.receive_json()
                assert response_done["type"] == "response.completed"
                assert response_done["usage"] == {
                    "prompt_tokens": 7,
                    "completion_tokens": 3,
                    "total_tokens": 10,
                }

    assert captured["transcribe"]["audio_bytes"] == b"RIFF....WAVE"
    assert captured["transcribe"]["filename"] == "input.wav"
    assert captured["transcribe"]["resolved_model"] == "small"
    assert captured["speech"]["text"] == "Hello world."
    assert captured["speech"]["resolved_model"] == "chatterbox"
    assert captured_ollama[0]["model"] == "qwen3.5:0.8b"
    assert captured_ollama[0]["stream"] is True
    assert captured_ollama[0]["messages"][-1] == {"role": "user", "content": "hello there"}


def test_conversation_websocket_requires_auth_when_enabled(monkeypatch: pytest.MonkeyPatch):
    _patch_settings(
        monkeypatch,
        _settings(enable_api_key_auth=True, api_key="test-secret"),
    )

    with TestClient(create_app()) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/v1/audio/conversations"):
                pass

    assert exc_info.value.code == 1008


def test_conversation_websocket_accepts_valid_auth(monkeypatch: pytest.MonkeyPatch):
    _patch_settings(
        monkeypatch,
        _settings(enable_api_key_auth=True, api_key="test-secret"),
    )

    with TestClient(create_app()) as client:
        with client.websocket_connect(
            "/v1/audio/conversations",
            headers={"Authorization": "Bearer test-secret"},
        ) as ws:
            ws.send_json({"type": "session.start", "whisper_model": "tiny"})
            assert ws.receive_json()["type"] == "session.created"


def test_conversation_audio_size_limit_is_per_turn(monkeypatch: pytest.MonkeyPatch):
    _patch_settings(monkeypatch, _settings(max_request_body_bytes=4))

    import gateway.audio as audio_module

    async def fail_transcribe(**kwargs: Any) -> dict[str, Any]:
        raise AssertionError("transcription should not be called for oversized audio")

    monkeypatch.setattr(audio_module, "transcribe_audio_bytes_with_whisper", fail_transcribe)

    with TestClient(create_app()) as client:
        with client.websocket_connect("/v1/audio/conversations") as ws:
            ws.send_json({"type": "session.start", "whisper_model": "tiny"})
            assert ws.receive_json()["type"] == "session.created"

            ws.send_json({"type": "input_audio.start"})
            assert ws.receive_json()["type"] == "input_audio.started"
            ws.send_bytes(b"12345")

            error = ws.receive_json()
            assert error["type"] == "error"
            assert error["error"]["code"] == "request_too_large"

            ws.send_json({"type": "input_audio.commit"})
            commit_error = ws.receive_json()
            assert commit_error["type"] == "error"
            assert commit_error["error"]["code"] == "no_input_audio"


def test_conversation_rejects_invalid_start_frame(monkeypatch: pytest.MonkeyPatch):
    _patch_settings(monkeypatch, _settings())

    with TestClient(create_app()) as client:
        with client.websocket_connect("/v1/audio/conversations") as ws:
            ws.send_json({"type": "ping"})
            error = ws.receive_json()
            assert error["type"] == "error"
            assert error["error"]["code"] == "invalid_session_start"
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()

    assert exc_info.value.code == 1003


def test_conversation_rejects_disabled_default_whisper(monkeypatch: pytest.MonkeyPatch):
    _patch_settings(monkeypatch, _settings(default_whisper_model="none"))

    with TestClient(create_app()) as client:
        with client.websocket_connect("/v1/audio/conversations") as ws:
            ws.send_json({"type": "session.start"})
            error = ws.receive_json()
            assert error["type"] == "error"
            assert error["error"]["code"] == "audio_model_disabled"
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()

    assert exc_info.value.code == 1008


def test_conversation_ping_clear_and_unexpected_audio(monkeypatch: pytest.MonkeyPatch):
    _patch_settings(monkeypatch, _settings())

    with TestClient(create_app()) as client:
        with client.websocket_connect("/v1/audio/conversations") as ws:
            assert _start_session(ws)["type"] == "session.created"

            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}

            ws.send_bytes(b"not-started")
            error = ws.receive_json()
            assert error["type"] == "error"
            assert error["error"]["code"] == "unexpected_audio"

            ws.send_json({"type": "input_audio.start"})
            assert ws.receive_json()["type"] == "input_audio.started"
            ws.send_bytes(b"RIFF")
            ws.send_json({"type": "input_audio.clear"})
            assert ws.receive_json() == {"type": "input_audio.cleared"}


def test_conversation_empty_transcript_returns_error(monkeypatch: pytest.MonkeyPatch):
    _patch_settings(monkeypatch, _settings())

    import gateway.audio as audio_module

    async def fake_transcribe(**kwargs: Any) -> dict[str, Any]:
        return {"text": "   ", "language": "en"}

    monkeypatch.setattr(audio_module, "transcribe_audio_bytes_with_whisper", fake_transcribe)

    with TestClient(create_app()) as client:
        with client.websocket_connect("/v1/audio/conversations") as ws:
            assert _start_session(ws)["type"] == "session.created"
            ws.send_json({"type": "input_audio.start"})
            assert ws.receive_json()["type"] == "input_audio.started"
            ws.send_bytes(b"RIFF")
            ws.send_json({"type": "input_audio.commit"})

            error = ws.receive_json()
            assert error["type"] == "error"
            assert error["error"]["code"] == "empty_transcript"


def test_conversation_ollama_stream_error_returns_error(monkeypatch: pytest.MonkeyPatch):
    _patch_settings(monkeypatch, _settings())

    import gateway.audio as audio_module

    async def fake_transcribe(**kwargs: Any) -> dict[str, Any]:
        return {"text": "hello", "language": "en"}

    async def fail_speech(**kwargs: Any) -> bytes:
        raise AssertionError("speech should not run after stream failure")

    monkeypatch.setattr(audio_module, "transcribe_audio_bytes_with_whisper", fake_transcribe)
    monkeypatch.setattr(audio_module, "synthesize_speech_with_chatterbox", fail_speech)

    with respx.mock(base_url=OLLAMA_BASE) as mock:
        mock.post("/api/chat").mock(
            return_value=httpx.Response(
                200,
                content=_ndjson(
                    {
                        "model": "qwen3.5:9b",
                        "message": {"role": "assistant", "content": "partial"},
                        "done": False,
                    }
                ),
            )
        )
        with TestClient(create_app()) as client:
            with client.websocket_connect("/v1/audio/conversations") as ws:
                assert _start_session(ws)["type"] == "session.created"
                ws.send_json({"type": "input_audio.start"})
                assert ws.receive_json()["type"] == "input_audio.started"
                ws.send_bytes(b"RIFF")
                ws.send_json({"type": "input_audio.commit"})

                assert ws.receive_json()["type"] == "transcript.completed"
                assert ws.receive_json()["type"] == "response.started"
                assert ws.receive_json() == {"type": "response.text.delta", "delta": "partial"}
                error = ws.receive_json()
                assert error["type"] == "error"
                assert error["error"]["code"] == "ollama_invalid_response"


def test_conversation_tts_error_returns_error(monkeypatch: pytest.MonkeyPatch):
    _patch_settings(monkeypatch, _settings())

    import gateway.audio as audio_module

    async def fake_transcribe(**kwargs: Any) -> dict[str, Any]:
        return {"text": "hello", "language": "en"}

    async def fail_speech(**kwargs: Any) -> bytes:
        raise HTTPException(
            status_code=502,
            detail={
                "error": {
                    "message": "TTS failed",
                    "type": "upstream_error",
                    "code": "audio_model_error",
                }
            },
        )

    monkeypatch.setattr(audio_module, "transcribe_audio_bytes_with_whisper", fake_transcribe)
    monkeypatch.setattr(audio_module, "synthesize_speech_with_chatterbox", fail_speech)

    with respx.mock(base_url=OLLAMA_BASE) as mock:
        mock.post("/api/chat").mock(
            return_value=httpx.Response(
                200,
                content=_ndjson(
                    {
                        "model": "qwen3.5:9b",
                        "message": {"role": "assistant", "content": "**Hello**"},
                        "done": False,
                    },
                    {
                        "model": "qwen3.5:9b",
                        "message": {"role": "assistant", "content": ""},
                        "done": True,
                        "prompt_eval_count": 2,
                        "eval_count": 1,
                    },
                ),
            )
        )
        with TestClient(create_app()) as client:
            with client.websocket_connect("/v1/audio/conversations") as ws:
                assert _start_session(ws)["type"] == "session.created"
                ws.send_json({"type": "input_audio.start"})
                assert ws.receive_json()["type"] == "input_audio.started"
                ws.send_bytes(b"RIFF")
                ws.send_json({"type": "input_audio.commit"})

                assert ws.receive_json()["type"] == "transcript.completed"
                assert ws.receive_json()["type"] == "response.started"
                assert ws.receive_json() == {"type": "response.text.delta", "delta": "**Hello**"}
                completed_text = ws.receive_json()
                assert completed_text["type"] == "response.text.completed"
                assert completed_text["speech_text"] == "Hello"
                assert ws.receive_json() == {"type": "response.audio.started", "format": "wav"}
                error = ws.receive_json()
                assert error["type"] == "error"
                assert error["error"]["code"] == "audio_model_error"


def test_conversation_busy_and_cancel(monkeypatch: pytest.MonkeyPatch):
    _patch_settings(monkeypatch, _settings())

    import gateway.audio as audio_module

    async def slow_transcribe(**kwargs: Any) -> dict[str, Any]:
        import asyncio

        await asyncio.sleep(1)
        return {"text": "hello", "language": "en"}

    monkeypatch.setattr(audio_module, "transcribe_audio_bytes_with_whisper", slow_transcribe)

    with TestClient(create_app()) as client:
        with client.websocket_connect("/v1/audio/conversations") as ws:
            assert _start_session(ws)["type"] == "session.created"
            ws.send_json({"type": "input_audio.start"})
            assert ws.receive_json()["type"] == "input_audio.started"
            ws.send_bytes(b"RIFF")
            ws.send_json({"type": "input_audio.commit"})

            ws.send_json({"type": "input_audio.start"})
            busy = ws.receive_json()
            assert busy["type"] == "error"
            assert busy["error"]["code"] == "busy"

            ws.send_json({"type": "response.cancel"})
            assert ws.receive_json() == {"type": "response.cancelled"}
