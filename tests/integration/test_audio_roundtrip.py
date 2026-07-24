"""End-to-end audio integration test: Chatterbox TTS -> Whisper STT.

Unlike the unit tests (which mock the speech stack), this actually loads the
Whisper and Chatterbox models and runs a real round-trip against a running
gateway. It is what would have caught the Chatterbox / setuptools regression.

It is opt-in: it SKIPS unless a gateway is reachable at GATEWAY_BASE_URL
(default http://127.0.0.1:8080), so the default unit run and CI stay fast.
First run downloads model weights (~1-2 GB) and can take several minutes on CPU.

Run locally against a live gateway:
    GATEWAY_BASE_URL=http://127.0.0.1:8080 pytest tests/integration -m integration -v
"""
from __future__ import annotations

import os

import httpx
import pytest

pytestmark = pytest.mark.integration

BASE_URL = os.environ.get("GATEWAY_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
# Generous: the first request downloads model weights and runs CPU inference.
_TIMEOUT = float(os.environ.get("AUDIO_TEST_TIMEOUT", "900"))
_PHRASE = "The local gateway can speak."


@pytest.fixture(scope="module")
def gateway() -> str:
    try:
        resp = httpx.get(f"{BASE_URL}/health", timeout=5)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - any failure means "not runnable here"
        pytest.skip(
            f"gateway not reachable at {BASE_URL} ({exc}); "
            "set GATEWAY_BASE_URL and start the audio-enabled gateway to run this test"
        )
    return BASE_URL


def test_tts_then_stt_roundtrip(gateway: str) -> None:
    with httpx.Client(timeout=_TIMEOUT) as client:
        # 1. Text -> speech (Chatterbox).
        tts = client.post(
            f"{gateway}/v1/audio/speech",
            json={"model": "chatterbox", "input": _PHRASE, "response_format": "wav"},
        )
        assert tts.status_code == 200, f"TTS failed: {tts.status_code} {tts.text[:300]}"
        wav = tts.content
        assert wav[:4] == b"RIFF", "TTS response is not a WAV file"
        assert len(wav) > 1000, "TTS response is suspiciously small"

        # 2. Speech -> text (Whisper) on the audio we just produced.
        stt = client.post(
            f"{gateway}/v1/audio/transcriptions",
            data={"model": "base"},
            files={"file": ("speech.wav", wav, "audio/wav")},
        )
        assert stt.status_code == 200, f"STT failed: {stt.status_code} {stt.text[:300]}"
        text = stt.json()["text"].strip().lower()

    # Whisper transcription varies slightly; assert the salient words survive
    # the round-trip rather than requiring an exact match.
    assert "gateway" in text, f"round-trip lost 'gateway': {text!r}"
    assert "speak" in text, f"round-trip lost 'speak': {text!r}"
