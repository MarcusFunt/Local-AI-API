"""Tests for POST /v1/chat/completions (non-streaming and streaming)."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

pytestmark = pytest.mark.asyncio

OLLAMA_BASE = "http://ollama-test.local"

OLLAMA_SUCCESS = {
    "model": "qwen3.5:9b",
    "created_at": "2024-01-01T00:00:00Z",
    "message": {"role": "assistant", "content": "Hello!"},
    "done": True,
    "prompt_eval_count": 10,
    "eval_count": 3,
}


def _ndjson(*chunks: dict) -> bytes:
    """Build a newline-delimited JSON bytes response for streaming tests."""
    return b"\n".join(json.dumps(c).encode() for c in chunks) + b"\n"


class TestNonStreamingChatCompletion:
    async def test_success_returns_openai_envelope(self, client: httpx.AsyncClient):
        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.post("/api/chat").mock(
                return_value=httpx.Response(200, json=OLLAMA_SUCCESS)
            )
            resp = await client.post(
                "/v1/chat/completions",
                json={"model": "main", "messages": [{"role": "user", "content": "hi"}]},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["object"] == "chat.completion"
        assert body["id"].startswith("chatcmpl-")
        assert isinstance(body["created"], int)
        assert body["model"] == "qwen3.5:9b"
        choice = body["choices"][0]
        assert choice["index"] == 0
        assert choice["message"]["role"] == "assistant"
        assert choice["message"]["content"] == "Hello!"
        assert choice["finish_reason"] == "stop"

    async def test_usage_fields_from_ollama_counts(self, client: httpx.AsyncClient):
        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.post("/api/chat").mock(
                return_value=httpx.Response(200, json=OLLAMA_SUCCESS)
            )
            resp = await client.post(
                "/v1/chat/completions",
                json={"model": "main", "messages": [{"role": "user", "content": "hi"}]},
            )

        usage = resp.json()["usage"]
        assert usage["prompt_tokens"] == 10
        assert usage["completion_tokens"] == 3
        assert usage["total_tokens"] == 13

    async def test_small_alias_sends_correct_model_to_ollama(self, client: httpx.AsyncClient):
        captured: list[dict] = []

        async def capture(request: httpx.Request, *args, **kwargs):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={**OLLAMA_SUCCESS, "model": "qwen3.5:4b"})

        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.post("/api/chat").mock(side_effect=capture)
            await client.post(
                "/v1/chat/completions",
                json={"model": "small", "messages": [{"role": "user", "content": "hi"}]},
            )

        assert captured[0]["model"] == "qwen3.5:4b"

    async def test_omitted_model_uses_default_profile(self, client: httpx.AsyncClient):
        captured: list[dict] = []

        async def capture(request: httpx.Request, *args, **kwargs):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json=OLLAMA_SUCCESS)

        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.post("/api/chat").mock(side_effect=capture)
            resp = await client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "hi"}]},
            )

        assert resp.status_code == 200
        # default_model_profile="main" → "qwen3.5:9b"
        assert captured[0]["model"] == "qwen3.5:9b"

    async def test_temperature_and_max_tokens_forwarded(self, client: httpx.AsyncClient):
        captured: list[dict] = []

        async def capture(request: httpx.Request, *args, **kwargs):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json=OLLAMA_SUCCESS)

        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.post("/api/chat").mock(side_effect=capture)
            await client.post(
                "/v1/chat/completions",
                json={
                    "model": "main",
                    "messages": [{"role": "user", "content": "hi"}],
                    "temperature": 0.5,
                    "max_tokens": 512,
                },
            )

        assert captured[0]["options"]["temperature"] == 0.5
        assert captured[0]["options"]["num_predict"] == 512

    async def test_unknown_model_returns_422(self, client: httpx.AsyncClient):
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 422
        error = resp.json()["error"]
        assert error["code"] == "model_not_found"
        assert "gpt-4" in error["message"]

    async def test_ollama_500_becomes_502(self, client: httpx.AsyncClient):
        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.post("/api/chat").mock(
                return_value=httpx.Response(500, text="internal error")
            )
            resp = await client.post(
                "/v1/chat/completions",
                json={"model": "main", "messages": [{"role": "user", "content": "hi"}]},
            )
        assert resp.status_code == 502
        assert resp.json()["error"]["code"] == "ollama_error"

    async def test_ollama_connect_error_becomes_502(self, client: httpx.AsyncClient):
        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.post("/api/chat").mock(side_effect=httpx.ConnectError("refused"))
            resp = await client.post(
                "/v1/chat/completions",
                json={"model": "main", "messages": [{"role": "user", "content": "hi"}]},
            )
        assert resp.status_code == 502

    async def test_ollama_timeout_becomes_504(self, client: httpx.AsyncClient):
        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.post("/api/chat").mock(side_effect=httpx.TimeoutException("timeout"))
            resp = await client.post(
                "/v1/chat/completions",
                json={"model": "main", "messages": [{"role": "user", "content": "hi"}]},
            )
        assert resp.status_code == 504
        assert resp.json()["error"]["code"] == "ollama_timeout"

    async def test_body_too_large_returns_413(self, client: httpx.AsyncClient):
        # The fixture uses max_request_body_bytes=10_485_760 but we send
        # Content-Length header that exceeds it manually.
        resp = await client.post(
            "/v1/chat/completions",
            content=b"x",
            headers={"Content-Length": str(10_485_760 + 1), "Content-Type": "application/json"},
        )
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "request_too_large"

    async def test_arbitrary_model_allowed_when_enabled(self, client: httpx.AsyncClient):
        """With ENABLE_ARBITRARY_MODELS=true, any model name should pass through."""
        import gateway.routes.chat as chat_module
        import gateway.config as cfg_module
        from gateway import main as main_module
        from gateway.config import Settings

        arbitrary = Settings(
            ollama_base_url=OLLAMA_BASE,
            enable_api_key_auth=False,
            api_key="",
            enable_arbitrary_models=True,
            request_timeout_seconds=10,
            max_request_body_bytes=10_485_760,
        )
        old_settings = chat_module.settings
        chat_module.settings = arbitrary
        cfg_module.settings = arbitrary
        main_module.settings = arbitrary

        try:
            with respx.mock(base_url=OLLAMA_BASE) as mock:
                mock.post("/api/chat").mock(
                    return_value=httpx.Response(200, json=OLLAMA_SUCCESS)
                )
                resp = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "llama3:8b",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                )
            assert resp.status_code == 200
        finally:
            chat_module.settings = old_settings
            cfg_module.settings = old_settings
            main_module.settings = old_settings


class TestStreamingChatCompletion:
    async def test_streaming_content_type(self, client: httpx.AsyncClient):
        chunks = [
            {"model": "qwen3.5:9b", "message": {"role": "assistant", "content": "Hi"}, "done": False},
            {"model": "qwen3.5:9b", "message": {"role": "assistant", "content": "!"}, "done": False},
            {"model": "qwen3.5:9b", "message": {"role": "assistant", "content": ""}, "done": True,
             "prompt_eval_count": 5, "eval_count": 2},
        ]

        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.post("/api/chat").mock(
                return_value=httpx.Response(200, content=_ndjson(*chunks),
                                            headers={"Content-Type": "application/x-ndjson"})
            )
            resp = await client.post(
                "/v1/chat/completions",
                json={"model": "main", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            )

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")

    async def test_streaming_sse_structure(self, client: httpx.AsyncClient):
        chunks = [
            {"model": "qwen3.5:9b", "message": {"role": "assistant", "content": "Hello"}, "done": False},
            {"model": "qwen3.5:9b", "message": {"role": "assistant", "content": " world"}, "done": False},
            {"model": "qwen3.5:9b", "message": {"role": "assistant", "content": ""}, "done": True,
             "prompt_eval_count": 5, "eval_count": 3},
        ]

        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.post("/api/chat").mock(
                return_value=httpx.Response(200, content=_ndjson(*chunks))
            )
            resp = await client.post(
                "/v1/chat/completions",
                json={"model": "main", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            )

        text = resp.text
        lines = [l for l in text.splitlines() if l.startswith("data: ")]

        # First data line: role delta
        first = json.loads(lines[0][6:])
        assert first["object"] == "chat.completion.chunk"
        assert first["choices"][0]["delta"].get("role") == "assistant"
        assert first["id"].startswith("chatcmpl-")

        # All chunks share the same id
        ids = {json.loads(l[6:])["id"] for l in lines if l != "data: [DONE]"}
        assert len(ids) == 1

        # Last data line before [DONE] has finish_reason=stop
        last_data_line = [l for l in lines if l != "data: [DONE]"][-1]
        last_chunk = json.loads(last_data_line[6:])
        assert last_chunk["choices"][0]["finish_reason"] == "stop"

        # Final line is [DONE]
        assert lines[-1] == "data: [DONE]"

    async def test_streaming_content_tokens(self, client: httpx.AsyncClient):
        chunks = [
            {"model": "qwen3.5:9b", "message": {"role": "assistant", "content": "A"}, "done": False},
            {"model": "qwen3.5:9b", "message": {"role": "assistant", "content": "B"}, "done": False},
            {"model": "qwen3.5:9b", "message": {"role": "assistant", "content": ""}, "done": True,
             "prompt_eval_count": 2, "eval_count": 2},
        ]

        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.post("/api/chat").mock(
                return_value=httpx.Response(200, content=_ndjson(*chunks))
            )
            resp = await client.post(
                "/v1/chat/completions",
                json={"model": "main", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            )

        data_lines = [
            l[6:] for l in resp.text.splitlines()
            if l.startswith("data: ") and l != "data: [DONE]"
        ]
        # Expect: role chunk + "A" chunk + "B" chunk + stop chunk
        assert len(data_lines) == 4
        content_chunks = [json.loads(l)["choices"][0]["delta"].get("content") for l in data_lines[1:-1]]
        assert content_chunks == ["A", "B"]
