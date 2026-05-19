"""Tests for OpenAI-style local audio endpoints."""
from __future__ import annotations

from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.asyncio


class TestAudioTranscriptions:
    async def test_success_returns_json(
        self,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import gateway.audio as audio_module

        captured: dict[str, Any] = {}

        async def fake_transcribe(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"text": "hello world", "language": "en"}

        monkeypatch.setattr(audio_module, "transcribe_with_whisper", fake_transcribe)

        resp = await client.post(
            "/v1/audio/transcriptions",
            data={"model": "tiny", "language": "en"},
            files={"file": ("sample.wav", b"RIFF....WAVE", "audio/wav")},
        )

        assert resp.status_code == 200
        assert resp.json() == {"text": "hello world"}
        assert captured["resolved_model"] == "tiny"
        assert captured["language"] == "en"

    async def test_text_response_format(
        self,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import gateway.audio as audio_module

        async def fake_transcribe(**kwargs: Any) -> dict[str, Any]:
            return {"text": "plain text"}

        monkeypatch.setattr(audio_module, "transcribe_with_whisper", fake_transcribe)

        resp = await client.post(
            "/v1/audio/transcriptions",
            data={"model": "base", "response_format": "text"},
            files={"file": ("sample.wav", b"RIFF....WAVE", "audio/wav")},
        )

        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        assert resp.text == "plain text"

    async def test_verbose_json_includes_metadata(
        self,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import gateway.audio as audio_module

        async def fake_transcribe(**kwargs: Any) -> dict[str, Any]:
            return {
                "text": "hello",
                "language": "en",
                "segments": [{"id": 0, "text": "hello"}],
            }

        monkeypatch.setattr(audio_module, "transcribe_with_whisper", fake_transcribe)

        resp = await client.post(
            "/v1/audio/transcriptions",
            data={"model": "small", "response_format": "verbose_json"},
            files={"file": ("sample.wav", b"RIFF....WAVE", "audio/wav")},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["text"] == "hello"
        assert body["language"] == "en"
        assert body["segments"] == [{"id": 0, "text": "hello"}]

    async def test_none_model_disables_transcription(
        self,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import gateway.audio as audio_module

        async def fake_transcribe(**kwargs: Any) -> dict[str, Any]:
            raise AssertionError("transcription should not be called")

        monkeypatch.setattr(audio_module, "transcribe_with_whisper", fake_transcribe)

        resp = await client.post(
            "/v1/audio/transcriptions",
            data={"model": "none"},
            files={"file": ("sample.wav", b"RIFF....WAVE", "audio/wav")},
        )

        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "audio_model_disabled"

    async def test_unknown_model_returns_422(self, client: httpx.AsyncClient):
        resp = await client.post(
            "/v1/audio/transcriptions",
            data={"model": "large"},
            files={"file": ("sample.wav", b"RIFF....WAVE", "audio/wav")},
        )

        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "audio_model_not_found"

    async def test_unsupported_response_format_returns_422(
        self,
        client: httpx.AsyncClient,
    ):
        resp = await client.post(
            "/v1/audio/transcriptions",
            data={"model": "tiny", "response_format": "srt"},
            files={"file": ("sample.wav", b"RIFF....WAVE", "audio/wav")},
        )

        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "unsupported_audio_format"


class TestAudioSpeech:
    async def test_success_returns_wav(
        self,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import gateway.audio as audio_module

        captured: dict[str, Any] = {}

        async def fake_speech(**kwargs: Any) -> bytes:
            captured.update(kwargs)
            return b"RIFF....WAVE"

        monkeypatch.setattr(audio_module, "synthesize_speech_with_chatterbox", fake_speech)

        resp = await client.post(
            "/v1/audio/speech",
            json={"model": "chatterbox", "input": "Say hello", "response_format": "wav"},
        )

        assert resp.status_code == 200
        assert "audio/wav" in resp.headers["content-type"]
        assert resp.content == b"RIFF....WAVE"
        assert captured["resolved_model"] == "chatterbox"
        assert captured["text"] == "Say hello"

    async def test_multilingual_model_forwards_language(
        self,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import gateway.audio as audio_module

        captured: dict[str, Any] = {}

        async def fake_speech(**kwargs: Any) -> bytes:
            captured.update(kwargs)
            return b"RIFF....WAVE"

        monkeypatch.setattr(audio_module, "synthesize_speech_with_chatterbox", fake_speech)

        resp = await client.post(
            "/v1/audio/speech",
            json={
                "model": "chatterbox-multilingual",
                "input": "Bonjour",
                "language": "fr",
            },
        )

        assert resp.status_code == 200
        assert captured["resolved_model"] == "chatterbox-multilingual"
        assert captured["language"] == "fr"

    async def test_unknown_model_returns_422(self, client: httpx.AsyncClient):
        resp = await client.post(
            "/v1/audio/speech",
            json={"model": "other-tts", "input": "hello"},
        )

        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "audio_model_not_found"

    async def test_unsupported_format_returns_422(self, client: httpx.AsyncClient):
        resp = await client.post(
            "/v1/audio/speech",
            json={"model": "chatterbox", "input": "hello", "response_format": "mp3"},
        )

        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "unsupported_audio_format"

    async def test_unsupported_speed_returns_422(self, client: httpx.AsyncClient):
        resp = await client.post(
            "/v1/audio/speech",
            json={"model": "chatterbox", "input": "hello", "speed": 1.5},
        )

        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "unsupported_speech_option"
