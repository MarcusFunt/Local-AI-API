"""Tests for the deliberate graph and mixture-of-experts agent endpoint."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

OLLAMA_BASE = "http://127.0.0.1:11434"

pytestmark = pytest.mark.asyncio


def _ollama_response(content: str, *, model: str) -> dict[str, object]:
    return {
        "model": model,
        "message": {"role": "assistant", "content": content},
        "done": True,
        "prompt_eval_count": 10,
        "eval_count": 3,
    }


class TestAdvancedAgents:
    async def test_graph_runs_all_nodes_and_returns_only_final_answer(
        self,
        client: httpx.AsyncClient,
    ) -> None:
        captured: list[dict[str, object]] = []
        responses = iter(["plan", "draft", "critique", "final answer"])

        async def capture(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(
                200,
                json=_ollama_response(next(responses), model="qwen3:14b"),
            )

        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.post("/api/chat").mock(side_effect=capture)
            response = await client.post(
                "/v1/agent/completions",
                json={
                    "mode": "graph",
                    "messages": [{"role": "user", "content": "Solve this carefully."}],
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert body["object"] == "agent.completion"
        assert body["id"].startswith("agentcmpl-")
        assert body["mode"] == "graph"
        assert body["model"] == "qwen3:14b"
        assert body["choices"][0]["message"]["content"] == "final answer"
        assert body["metadata"] == {"steps_completed": 4, "elapsed_ms": body["metadata"]["elapsed_ms"]}
        assert body["usage"] == {"prompt_tokens": 40, "completion_tokens": 12, "total_tokens": 52}
        assert len(captured) == 4
        assert [item["model"] for item in captured] == ["qwen3:14b"] * 4
        assert "planner" in captured[0]["messages"][0]["content"].lower()
        assert "Review work product" in captured[3]["messages"][-1]["content"]
        assert captured[0]["options"]["num_predict"] == 4096

    async def test_expert_ensemble_uses_selected_experts_then_synthesizes(
        self,
        client: httpx.AsyncClient,
    ) -> None:
        captured: list[dict[str, object]] = []
        responses = iter(["first opinion", "second opinion", "synthesis"])

        async def capture(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            captured.append(payload)
            return httpx.Response(
                200,
                json=_ollama_response(next(responses), model=payload["model"]),
            )

        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.post("/api/chat").mock(side_effect=capture)
            response = await client.post(
                "/v1/agent/completions",
                json={
                    "mode": "mixture_of_experts",
                    "model": "agent",
                    "expert_models": ["main", "small"],
                    "messages": [{"role": "user", "content": "Compare the options."}],
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert body["choices"][0]["message"]["content"] == "synthesis"
        assert body["metadata"]["steps_completed"] == 3
        assert body["metadata"]["expert_models"] == ["qwen3.5:9b", "qwen3.5:4b"]
        assert [item["model"] for item in captured] == ["qwen3.5:9b", "qwen3.5:4b", "qwen3:14b"]
        assert "Specialist 1 work product" in captured[2]["messages"][-2]["content"]

    async def test_rejects_streaming_tools_and_bad_expert_models(
        self,
        client: httpx.AsyncClient,
    ) -> None:
        base = {"mode": "mixture_of_experts", "messages": [{"role": "user", "content": "Hi"}]}

        streaming = await client.post("/v1/agent/completions", json={**base, "stream": True})
        tools = await client.post("/v1/agent/completions", json={**base, "tools": []})
        unknown_expert = await client.post(
            "/v1/agent/completions",
            json={**base, "expert_models": ["not-allowed", "main"]},
        )

        assert streaming.status_code == 422
        assert tools.status_code == 422
        assert unknown_expert.status_code == 422
        assert unknown_expert.json()["error"]["code"] == "model_not_found"

    async def test_rejects_experts_for_graph_mode(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            "/v1/agent/completions",
            json={
                "mode": "graph",
                "expert_models": ["main"],
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"

    async def test_rejects_a_single_explicit_expert(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            "/v1/agent/completions",
            json={
                "mode": "mixture_of_experts",
                "expert_models": ["main"],
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"
