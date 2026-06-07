"""
Tests for the FastMCP server tools defined in gateway/mcp_server/server.py.

All HTTP calls to the gateway loopback are intercepted with respx so no live
server is required.
"""
from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx

pytestmark = pytest.mark.asyncio

# The MCP tools call the gateway at this base URL
_GATEWAY = "http://127.0.0.1:8080"


# ---------------------------------------------------------------------------
# Import guard — skip entire module if fastmcp is not installed
# ---------------------------------------------------------------------------

fastmcp = pytest.importorskip("fastmcp", reason="fastmcp not installed")

from fastmcp.exceptions import ToolError  # noqa: E402

from gateway.mcp_server.server import (  # noqa: E402
    chat,
    health_check,
    list_models,
    search_documents,
    speak,
    transcribe,
)


# ---------------------------------------------------------------------------
# chat
# ---------------------------------------------------------------------------

class TestChatTool:
    async def test_chat_tool_calls_completions_endpoint(self):
        payload = {
            "id": "chatcmpl-123",
            "object": "chat.completion",
            "created": 1700000000,
            "model": "qwen3.5:9b",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello from Ollama!"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
        }

        with respx.mock(base_url=_GATEWAY) as mock:
            mock.post("/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=payload)
            )
            result = await chat(message="Say hello", model="main")

        assert result == "Hello from Ollama!"

    async def test_chat_tool_sends_system_prompt(self):
        captured: list[dict] = []

        async def capture(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
                    ]
                },
            )

        with respx.mock(base_url=_GATEWAY) as mock:
            mock.post("/v1/chat/completions").mock(side_effect=capture)
            await chat(message="hi", system="You are a helpful assistant.")

        assert captured[0]["messages"][0] == {"role": "system", "content": "You are a helpful assistant."}
        assert captured[0]["messages"][1] == {"role": "user", "content": "hi"}

    async def test_chat_tool_http_error_raises_tool_error(self):
        with respx.mock(base_url=_GATEWAY) as mock:
            mock.post("/v1/chat/completions").mock(
                return_value=httpx.Response(502, text="bad gateway")
            )
            with pytest.raises(ToolError, match="502"):
                await chat(message="hi")

    async def test_chat_tool_connect_error_raises_tool_error(self):
        with respx.mock(base_url=_GATEWAY) as mock:
            mock.post("/v1/chat/completions").mock(
                side_effect=httpx.ConnectError("refused")
            )
            with pytest.raises(ToolError, match="Could not reach gateway"):
                await chat(message="hi")


# ---------------------------------------------------------------------------
# list_models
# ---------------------------------------------------------------------------

class TestListModelsTool:
    async def test_list_models_returns_list(self):
        models_payload = {
            "object": "list",
            "data": [
                {"id": "main", "object": "model", "created": 1700000000, "owned_by": "ollama"},
                {"id": "small", "object": "model", "created": 1700000000, "owned_by": "ollama"},
            ],
        }

        with respx.mock(base_url=_GATEWAY) as mock:
            mock.get("/v1/models").mock(
                return_value=httpx.Response(200, json=models_payload)
            )
            result = await list_models()

        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["id"] == "main"

    async def test_list_models_error_raises_tool_error(self):
        with respx.mock(base_url=_GATEWAY) as mock:
            mock.get("/v1/models").mock(
                return_value=httpx.Response(503, text="service unavailable")
            )
            with pytest.raises(ToolError, match="Could not fetch models"):
                await list_models()


# ---------------------------------------------------------------------------
# transcribe
# ---------------------------------------------------------------------------

class TestTranscribeTool:
    async def test_transcribe_decodes_base64_and_sends_file(self):
        captured_files: list = []

        async def capture(request: httpx.Request) -> httpx.Response:
            captured_files.append(request.content)
            return httpx.Response(200, json={"text": "Hello world"})

        raw_audio = b"RIFF" + b"\x00" * 36  # minimal WAV-like bytes
        audio_b64 = base64.b64encode(raw_audio).decode()

        with respx.mock(base_url=_GATEWAY) as mock:
            mock.post("/v1/audio/transcriptions").mock(side_effect=capture)
            result = await transcribe(audio_base64=audio_b64)

        assert result == "Hello world"
        # The multipart body should contain the raw audio bytes
        assert raw_audio in captured_files[0]

    async def test_transcribe_invalid_base64_raises_tool_error(self):
        with pytest.raises(ToolError, match="Invalid base64"):
            await transcribe(audio_base64="not-valid-base64!!!")

    async def test_transcribe_http_error_raises_tool_error(self):
        raw_audio = b"RIFF" + b"\x00" * 36
        audio_b64 = base64.b64encode(raw_audio).decode()

        with respx.mock(base_url=_GATEWAY) as mock:
            mock.post("/v1/audio/transcriptions").mock(
                return_value=httpx.Response(503, text="whisper not available")
            )
            with pytest.raises(ToolError, match="503"):
                await transcribe(audio_base64=audio_b64)


# ---------------------------------------------------------------------------
# speak
# ---------------------------------------------------------------------------

class TestSpeakTool:
    async def test_speak_returns_base64_encoded_audio(self):
        raw_wav = b"RIFF" + b"\x00" * 100

        with respx.mock(base_url=_GATEWAY) as mock:
            mock.post("/v1/audio/speech").mock(
                return_value=httpx.Response(200, content=raw_wav)
            )
            result = await speak(text="Hello world")

        # Result should be a valid base64 string that decodes back to the raw WAV
        assert base64.b64decode(result) == raw_wav

    async def test_speak_empty_text_raises_tool_error(self):
        with pytest.raises(ToolError, match="empty"):
            await speak(text="   ")

    async def test_speak_http_error_raises_tool_error(self):
        with respx.mock(base_url=_GATEWAY) as mock:
            mock.post("/v1/audio/speech").mock(
                return_value=httpx.Response(503, text="chatterbox not available")
            )
            with pytest.raises(ToolError, match="503"):
                await speak(text="Hello")


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------

class TestHealthCheckTool:
    async def test_health_check_calls_both_health_endpoints(self):
        with respx.mock(base_url=_GATEWAY) as mock:
            mock.get("/health").mock(
                return_value=httpx.Response(200, json={"status": "ok"})
            )
            mock.get("/health/ollama").mock(
                return_value=httpx.Response(200, json={"status": "ok", "models": 5})
            )
            result = await health_check()

        assert result["gateway"]["status"] == "ok"
        assert result["ollama"]["status"] == "ok"
        assert result["ollama"]["models"] == 5

    async def test_health_check_degraded_gateway_returns_error_key(self):
        with respx.mock(base_url=_GATEWAY) as mock:
            mock.get("/health").mock(
                return_value=httpx.Response(503, text="gateway unhealthy")
            )
            mock.get("/health/ollama").mock(
                return_value=httpx.Response(200, json={"status": "ok"})
            )
            result = await health_check()

        assert "error" in result["gateway"]

    async def test_health_check_request_error_raises_tool_error(self):
        with respx.mock(base_url=_GATEWAY) as mock:
            mock.get("/health").mock(side_effect=httpx.ConnectError("refused"))
            with pytest.raises(ToolError, match="Health check failed"):
                await health_check()


# ---------------------------------------------------------------------------
# search_documents
# ---------------------------------------------------------------------------

class TestSearchDocumentsTool:
    async def test_search_documents_returns_results(self):
        results_payload = {
            "results": [
                {"text": "Authentication flow starts here", "source": "auth.py", "score": 0.92},
                {"text": "Token validation logic", "source": "auth.py", "score": 0.85},
            ]
        }

        with respx.mock(base_url=_GATEWAY) as mock:
            mock.post("/v1/search").mock(
                return_value=httpx.Response(200, json=results_payload)
            )
            result = await search_documents(query="authentication", top_k=2)

        assert len(result) == 2
        assert result[0]["source"] == "auth.py"

    async def test_search_documents_503_gives_helpful_error(self):
        """503 from the gateway means RAG is disabled — should surface a clear message."""
        with respx.mock(base_url=_GATEWAY) as mock:
            mock.post("/v1/search").mock(
                return_value=httpx.Response(503, text="RAG not enabled")
            )
            with pytest.raises(ToolError, match="RAG_ENABLED=true"):
                await search_documents(query="anything")

    async def test_search_documents_other_http_error_raises_tool_error(self):
        with respx.mock(base_url=_GATEWAY) as mock:
            mock.post("/v1/search").mock(
                return_value=httpx.Response(500, text="internal server error")
            )
            with pytest.raises(ToolError, match="500"):
                await search_documents(query="anything")

    async def test_search_documents_connect_error_raises_tool_error(self):
        with respx.mock(base_url=_GATEWAY) as mock:
            mock.post("/v1/search").mock(side_effect=httpx.ConnectError("refused"))
            with pytest.raises(ToolError, match="Could not reach gateway"):
                await search_documents(query="anything")
