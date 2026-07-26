"""Tests for POST /v1/chat/completions (non-streaming and streaming)."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from gateway.config import Settings
from gateway.main import create_app

pytestmark = pytest.mark.asyncio

OLLAMA_BASE = "http://127.0.0.1:11434"

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

    async def test_length_done_reason_sets_finish_reason_length(
        self, client: httpx.AsyncClient
    ):
        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.post("/api/chat").mock(
                return_value=httpx.Response(
                    200,
                    json={**OLLAMA_SUCCESS, "done_reason": "length"},
                )
            )
            resp = await client.post(
                "/v1/chat/completions",
                json={"model": "main", "messages": [{"role": "user", "content": "hi"}]},
            )

        assert resp.status_code == 200
        assert resp.json()["choices"][0]["finish_reason"] == "length"

    async def test_out_of_memory_returns_insufficient_memory(self, client: httpx.AsyncClient):
        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.post("/api/chat").mock(
                return_value=httpx.Response(
                    500,
                    json={
                        "error": "model requires more system memory "
                        "(5.5 GiB) than is available (3.9 GiB)"
                    },
                )
            )
            resp = await client.post(
                "/v1/chat/completions",
                json={"model": "main", "messages": [{"role": "user", "content": "hi"}]},
            )

        assert resp.status_code == 507
        error = resp.json()["error"]
        assert error["code"] == "insufficient_memory"
        message = error["message"].lower()
        assert "smaller model" in message or "low compute" in message

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

    async def test_dev_alias_sends_development_model_to_ollama(
        self, client: httpx.AsyncClient
    ):
        captured: list[dict] = []

        async def capture(request: httpx.Request, *args, **kwargs):
            captured.append(json.loads(request.content))
            return httpx.Response(
                200, json={**OLLAMA_SUCCESS, "model": "qwen3.5:0.8b"}
            )

        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.post("/api/chat").mock(side_effect=capture)
            await client.post(
                "/v1/chat/completions",
                json={"model": "dev", "messages": [{"role": "user", "content": "hi"}]},
            )

        assert captured[0]["model"] == "qwen3.5:0.8b"
        assert captured[0]["think"] is False

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

    async def test_invalid_json_returns_openai_422(self, client: httpx.AsyncClient):
        resp = await client.post(
            "/v1/chat/completions",
            content=b"{not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "invalid_request"

    async def test_schema_error_returns_openai_422(self, client: httpx.AsyncClient):
        resp = await client.post("/v1/chat/completions", json={"model": "main"})
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "invalid_request"

    async def test_empty_messages_returns_openai_422(self, client: httpx.AsyncClient):
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "main", "messages": []},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "invalid_request"

    async def test_invalid_role_returns_openai_422(self, client: httpx.AsyncClient):
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "main", "messages": [{"role": "moderator", "content": "hi"}]},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "invalid_request"

    async def test_unsupported_openai_field_returns_openai_422(
        self, client: httpx.AsyncClient
    ):
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "main",
                "messages": [{"role": "user", "content": "hi"}],
                "logprobs": True,
            },
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "invalid_request"

    async def test_invalid_sampling_bounds_return_openai_422(
        self, client: httpx.AsyncClient
    ):
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "main",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 0,
            },
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "invalid_request"

    async def test_sampling_stop_and_seed_forwarded(self, client: httpx.AsyncClient):
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
                    "top_p": 0.8,
                    "max_tokens": 512,
                    "stop": ["END"],
                    "seed": 1234,
                },
            )

        options = captured[0]["options"]
        assert options["temperature"] == 0.5
        assert options["top_p"] == 0.8
        assert options["num_predict"] == 512
        assert options["stop"] == ["END"]
        assert options["seed"] == 1234

    async def test_max_completion_tokens_forwarded_as_num_predict(
        self, client: httpx.AsyncClient
    ):
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
                    "max_completion_tokens": 32,
                },
            )

        assert captured[0]["options"]["num_predict"] == 32

    async def test_content_parts_are_normalized_for_ollama(
        self, client: httpx.AsyncClient
    ):
        captured: list[dict] = []

        async def capture(request: httpx.Request, *args, **kwargs):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json=OLLAMA_SUCCESS)

        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.post("/api/chat").mock(side_effect=capture)
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "main",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "hello"},
                                {"type": "text", "text": "world"},
                            ],
                        }
                    ],
                },
            )

        assert resp.status_code == 200
        assert captured[0]["messages"][0]["content"] == "hello\nworld"

    async def test_data_url_image_parts_are_forwarded_to_ollama(
        self, client: httpx.AsyncClient
    ):
        captured: list[dict] = []

        async def capture(request: httpx.Request, *args, **kwargs):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json=OLLAMA_SUCCESS)

        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.post("/api/chat").mock(side_effect=capture)
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "main",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "describe"},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": "data:image/png;base64,aW1hZ2U="
                                    },
                                },
                            ],
                        }
                    ],
                },
            )

        assert resp.status_code == 200
        assert captured[0]["messages"][0]["content"] == "describe"
        assert captured[0]["messages"][0]["images"] == ["aW1hZ2U="]

    async def test_tools_and_json_response_format_are_forwarded(
        self, client: httpx.AsyncClient
    ):
        captured: list[dict] = []
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

        async def capture(request: httpx.Request, *args, **kwargs):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json=OLLAMA_SUCCESS)

        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.post("/api/chat").mock(side_effect=capture)
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "main",
                    "messages": [{"role": "user", "content": "hi"}],
                    "tools": tools,
                    "response_format": {"type": "json_object"},
                },
            )

        assert resp.status_code == 200
        assert captured[0]["tools"] == tools
        assert captured[0]["format"] == "json"

    async def test_tool_choice_none_suppresses_tools(self, client: httpx.AsyncClient):
        captured: list[dict] = []

        async def capture(request: httpx.Request, *args, **kwargs):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json=OLLAMA_SUCCESS)

        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.post("/api/chat").mock(side_effect=capture)
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "main",
                    "messages": [{"role": "user", "content": "hi"}],
                    "tools": [{"type": "function", "function": {"name": "lookup"}}],
                    "tool_choice": "none",
                },
            )

        assert resp.status_code == 200
        assert "tools" not in captured[0]

    async def test_multiple_choices_are_rejected(self, client: httpx.AsyncClient):
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "main",
                "messages": [{"role": "user", "content": "hi"}],
                "n": 2,
            },
        )

        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "invalid_request"

    async def test_assistant_tool_calls_round_trip_in_response(
        self, client: httpx.AsyncClient
    ):
        tool_calls = [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }
        ]
        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.post("/api/chat").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        **OLLAMA_SUCCESS,
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": tool_calls,
                        },
                    },
                )
            )
            resp = await client.post(
                "/v1/chat/completions",
                json={"model": "main", "messages": [{"role": "user", "content": "hi"}]},
            )

        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["tool_calls"] == tool_calls

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

    async def test_malformed_ollama_json_becomes_502(self, client: httpx.AsyncClient):
        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.post("/api/chat").mock(return_value=httpx.Response(200, text="not json"))
            resp = await client.post(
                "/v1/chat/completions",
                json={"model": "main", "messages": [{"role": "user", "content": "hi"}]},
            )
        assert resp.status_code == 502
        assert resp.json()["error"]["code"] == "ollama_invalid_response"

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

    async def test_body_too_large_detected_without_content_length(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        import gateway.config as cfg_module
        import gateway.routes.health as health_module
        import gateway.routes.chat as chat_module
        from gateway import main as main_module

        small_limit_settings = Settings(
            ollama_base_url=OLLAMA_BASE,
            enable_api_key_auth=False,
            api_key="",
            enable_arbitrary_models=False,
            request_timeout_seconds=10,
            max_request_body_bytes=64,
        )
        monkeypatch.setattr(cfg_module, "settings", small_limit_settings)
        monkeypatch.setattr(health_module, "settings", small_limit_settings)
        monkeypatch.setattr(chat_module, "settings", small_limit_settings)
        monkeypatch.setattr(main_module, "settings", small_limit_settings)

        app = create_app()
        request_messages = [
            {
                "type": "http.request",
                "body": b'{"model":"main","messages":[{"role":"user","content":"',
                "more_body": True,
            },
            {"type": "http.request", "body": b"x" * 80, "more_body": True},
            {"type": "http.request", "body": b'"}]}', "more_body": False},
        ]
        sent_messages: list[dict] = []

        async def receive() -> dict:
            return request_messages.pop(0)

        async def send(message: dict) -> None:
            sent_messages.append(message)

        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "method": "POST",
                "scheme": "http",
                "path": "/v1/chat/completions",
                "raw_path": b"/v1/chat/completions",
                "query_string": b"",
                "headers": [(b"content-type", b"application/json")],
                "client": ("testclient", 50000),
                "server": ("testserver", 80),
                "root_path": "",
            },
            receive,
            send,
        )

        response_start = next(m for m in sent_messages if m["type"] == "http.response.start")
        response_body = b"".join(
            m.get("body", b"") for m in sent_messages if m["type"] == "http.response.body"
        )
        assert response_start["status"] == 413
        assert json.loads(response_body)["error"]["code"] == "request_too_large"

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

    async def test_streaming_length_done_reason_sets_finish_reason_length(
        self, client: httpx.AsyncClient
    ):
        chunks = [
            {"model": "qwen3.5:9b", "message": {"role": "assistant", "content": "A"}, "done": False},
            {"model": "qwen3.5:9b", "message": {"role": "assistant", "content": ""}, "done": True,
             "done_reason": "length", "prompt_eval_count": 2, "eval_count": 1},
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
        stop_chunk = json.loads(data_lines[-1])
        assert stop_chunk["choices"][0]["finish_reason"] == "length"

    async def test_streaming_include_usage_emits_usage_chunk(
        self, client: httpx.AsyncClient
    ):
        chunks = [
            {"model": "qwen3.5:9b", "message": {"role": "assistant", "content": "A"}, "done": False},
            {
                "model": "qwen3.5:9b",
                "message": {"role": "assistant", "content": ""},
                "done": True,
                "prompt_eval_count": 11,
                "eval_count": 4,
            },
        ]

        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.post("/api/chat").mock(
                return_value=httpx.Response(200, content=_ndjson(*chunks))
            )
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "main",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                    "stream_options": {"include_usage": True},
                },
            )

        data_lines = [
            l[6:] for l in resp.text.splitlines()
            if l.startswith("data: ") and l != "data: [DONE]"
        ]
        usage_chunk = json.loads(data_lines[-1])
        assert usage_chunk["choices"] == []
        assert usage_chunk["usage"] == {
            "prompt_tokens": 11,
            "completion_tokens": 4,
            "total_tokens": 15,
        }

    async def test_streaming_connect_error_becomes_502(self, client: httpx.AsyncClient):
        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.post("/api/chat").mock(side_effect=httpx.ConnectError("refused"))
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "main",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )

        assert resp.status_code == 502
        assert resp.json()["error"]["code"] == "ollama_error"

    async def test_streaming_timeout_becomes_504(self, client: httpx.AsyncClient):
        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.post("/api/chat").mock(side_effect=httpx.TimeoutException("timeout"))
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "main",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )

        assert resp.status_code == 504
        assert resp.json()["error"]["code"] == "ollama_timeout"
