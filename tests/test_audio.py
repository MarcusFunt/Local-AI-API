"""Tests for OpenAI-style local audio endpoints."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import io
import sys
import time
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException, UploadFile
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

    async def test_unsupported_voice_returns_422(
        self,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import gateway.audio as audio_module

        async def fake_speech(**kwargs: Any) -> bytes:
            raise AssertionError("speech synthesis should not be called")

        monkeypatch.setattr(audio_module, "synthesize_speech_with_chatterbox", fake_speech)

        resp = await client.post(
            "/v1/audio/speech",
            json={"model": "chatterbox", "input": "hello", "voice": "alloy"},
        )

        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "unsupported_speech_option"


class TestAudioUploadHelpers:
    async def test_upload_is_written_to_tempfile_in_chunks(self):
        import gateway.audio as audio_module

        upload = UploadFile(file=io.BytesIO(b"RIFF....WAVE"), filename="sample.wav")
        path = await audio_module._read_upload_to_tempfile(upload, max_bytes=1024)
        try:
            assert path.read_bytes() == b"RIFF....WAVE"
        finally:
            path.unlink(missing_ok=True)

    async def test_upload_size_limit_returns_413(self):
        import gateway.audio as audio_module

        upload = UploadFile(file=io.BytesIO(b"x" * 8), filename="sample.wav")
        with pytest.raises(HTTPException) as exc_info:
            await audio_module._read_upload_to_tempfile(upload, max_bytes=4)

        assert exc_info.value.status_code == 413
        assert exc_info.value.detail["error"]["code"] == "request_too_large"


class TestAudioModelLoading:
    async def test_whisper_model_load_is_locked_per_cache_key(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        import gateway.audio as audio_module

        audio_module._WHISPER_MODELS.clear()
        audio_module._WHISPER_MODEL_LOCKS.clear()
        calls = 0
        loaded_model = object()

        def fake_load_model(*args: Any, **kwargs: Any) -> object:
            nonlocal calls
            calls += 1
            time.sleep(0.05)
            return loaded_model

        monkeypatch.setitem(
            sys.modules,
            "whisper",
            SimpleNamespace(load_model=fake_load_model),
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda _: audio_module._load_whisper_model("tiny", "cpu"),
                    range(2),
                )
            )

        assert calls == 1
        assert results == [loaded_model, loaded_model]

    async def test_chatterbox_model_load_is_locked_per_cache_key(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        import gateway.audio as audio_module

        audio_module._CHATTERBOX_MODELS.clear()
        audio_module._CHATTERBOX_MODEL_LOCKS.clear()
        calls = 0
        loaded_model = object()

        class FakeChatterboxTTS:
            @classmethod
            def from_pretrained(cls, *args: Any, **kwargs: Any) -> object:
                nonlocal calls
                calls += 1
                time.sleep(0.05)
                return loaded_model

        monkeypatch.setitem(sys.modules, "chatterbox", SimpleNamespace())
        monkeypatch.setitem(
            sys.modules,
            "chatterbox.tts",
            SimpleNamespace(ChatterboxTTS=FakeChatterboxTTS),
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda _: audio_module._load_chatterbox_model("chatterbox", "cpu"),
                    range(2),
                )
            )

        assert calls == 1
        assert results == [loaded_model, loaded_model]


async def test_warm_audio_models_is_best_effort(monkeypatch: pytest.MonkeyPatch):
    """Pre-warming must never crash startup: failures to load models are logged
    and swallowed."""
    import gateway.audio as audio_module

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("model load failed")

    monkeypatch.setattr(audio_module, "_load_whisper_model", boom)
    monkeypatch.setattr(audio_module, "_load_chatterbox_model", boom)

    settings = SimpleNamespace(
        default_whisper_model="base",
        whisper_device="cpu",
        whisper_cache_dir="",
        chatterbox_model="chatterbox",
        chatterbox_device="cpu",
    )

    # Both loads raise; warm_audio_models must not propagate.
    audio_module.warm_audio_models(settings)
