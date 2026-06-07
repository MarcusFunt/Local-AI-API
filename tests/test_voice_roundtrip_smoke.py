"""
Live smoke test for the voice round-trip pipeline.

Requires a running Local-AI-API gateway.  Skipped unless::

    RUN_LIVE_AI_TESTS=1 pytest -m live tests/test_voice_roundtrip_smoke.py

This test does NOT mock any calls.  It hits the real gateway.
Set ``GATEWAY_URL`` to override the default ``http://127.0.0.1:8080``.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Make the project root importable so ``evaluation`` package can be found
# regardless of how pytest is invoked.
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ---------------------------------------------------------------------------
# Markers & helpers
# ---------------------------------------------------------------------------
live = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_AI_TESTS") != "1",
    reason="Set RUN_LIVE_AI_TESTS=1 to run live AI tests",
)

GATEWAY_URL: str = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8080")

pytestmark = pytest.mark.live


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@live
def test_tts_produces_valid_wav() -> None:
    """Gateway /v1/audio/speech returns a non-empty valid WAV file."""
    from evaluation.voice_roundtrip import GatewayClient, wav_is_valid

    client = GatewayClient(GATEWAY_URL)
    wav, latency = client.tts("Hello. This is a test.", model="chatterbox")
    assert wav_is_valid(wav), "TTS did not return a valid WAV file"
    assert latency < 60.0, f"TTS took {latency:.1f}s — suspiciously slow"


@live
def test_stt_transcribes_tts_output() -> None:
    """WAV produced by TTS can be transcribed by Whisper to non-empty text."""
    from evaluation.voice_roundtrip import GatewayClient, wav_is_valid

    client = GatewayClient(GATEWAY_URL)
    text = "The sensor recorded a stable reading throughout the test."
    wav, _ = client.tts(text, model="chatterbox")
    assert wav_is_valid(wav), "TTS did not return a valid WAV file"

    transcript, _ = client.stt(wav, model="whisper-small")
    assert transcript.strip(), "Whisper returned an empty transcript"


@live
def test_layer1_deterministic_passes_wer_threshold() -> None:
    """Full layer-1 pipeline meets WER ≤ 5%."""
    from evaluation.voice_roundtrip import GatewayClient, run_layer1

    client = GatewayClient(GATEWAY_URL)
    with tempfile.TemporaryDirectory() as tmp:
        result = run_layer1(client, Path(tmp))

    assert result.get("wav_valid"), "WAV was not valid"
    assert result.get("transcript", "").strip(), "Transcript was empty"

    wer = result.get("wer", 1.0)
    assert wer <= 0.05, (
        f"WER {wer:.1%} exceeded the 5% layer-1 threshold.\n"
        f"Original  : {result.get('original_normalized', '')[:200]}\n"
        f"Transcript: {result.get('transcript_normalized', '')[:200]}"
    )


@live
def test_layer2_controlled_passes_wer_threshold() -> None:
    """Full layer-2 pipeline (fixed LLM prompt, temp=0) meets WER ≤ 10%."""
    from evaluation.voice_roundtrip import GatewayClient, run_layer2

    client = GatewayClient(GATEWAY_URL)
    with tempfile.TemporaryDirectory() as tmp:
        result = run_layer2(client, Path(tmp))

    assert result.get("wav_valid"), "WAV was not valid"
    assert result.get("transcript", "").strip(), "Transcript was empty"

    wer = result.get("wer", 1.0)
    assert wer <= 0.10, (
        f"WER {wer:.1%} exceeded the 10% layer-2 threshold.\n"
        f"Original  : {result.get('original_normalized', '')[:200]}\n"
        f"Transcript: {result.get('transcript_normalized', '')[:200]}"
    )


@live
def test_llm_endpoint_responds() -> None:
    """Gateway /v1/chat/completions returns non-empty text within 120 s."""
    from evaluation.voice_roundtrip import GatewayClient

    client = GatewayClient(GATEWAY_URL)
    text, latency = client.chat(
        [{"role": "user", "content": "Say hello in one sentence."}],
        model="dev",
        max_tokens=50,
    )
    assert text.strip(), "LLM returned an empty response"
    assert latency < 120.0, f"LLM took {latency:.1f}s — exceeded 120 s timeout"
