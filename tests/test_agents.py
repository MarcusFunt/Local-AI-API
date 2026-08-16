"""Tests for the deliberate graph and mixture-of-experts agent endpoint."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from gateway.models import AgentCompletionRequest

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


async def test_agent_endpoint_records_redacted_stage_metadata(
    client: httpx.AsyncClient,
    default_settings,
    tmp_path,
) -> None:
    default_settings.agent_learning_dir = str(tmp_path / "learning")
    private_prompt = "customer deployment secret"
    responses = iter([
        "private plan",
        "private draft",
        "private critique",
        "private evidence ledger",
        "private final answer",
    ])

    async def capture(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ollama_response(next(responses), model="qwen3.5:9b"))

    with respx.mock(base_url=OLLAMA_BASE) as mock:
        mock.post("/api/chat").mock(side_effect=capture)
        response = await client.post(
            "/v1/agent/completions",
            json={"mode": "graph", "messages": [{"role": "user", "content": private_prompt}]},
        )

    assert response.status_code == 200
    record = json.loads((tmp_path / "learning" / "records.jsonl").read_text(encoding="utf-8"))
    assert record["surface"] == "gateway_agent"
    assert record["metrics"]["steps_completed"] == 5
    assert [stage["finish_reason"] for stage in record["trace"]["stages"]] == ["stop"] * 5
    assert [stage["output"]["characters"] for stage in record["trace"]["stages"]] == [
        len("private plan"),
        len("private draft"),
        len("private critique"),
        len("private evidence ledger"),
        len("private final answer"),
    ]
    persisted = (tmp_path / "learning" / "records.jsonl").read_text(encoding="utf-8")
    for private_text in (private_prompt, "private plan", "private final answer"):
        assert private_text not in persisted


class TestAdvancedAgents:
    async def test_adaptive_mode_is_default_and_returns_compact_verification_metadata(
        self,
        client: httpx.AsyncClient,
    ) -> None:
        captured: list[dict[str, object]] = []
        responses = iter([
            '{"task":"Review a code change","constraints":["do not invent tests"],"retrieval_queries":[]}',
            "candidate one",
            "candidate two",
            "candidate three",
            '{"accepted_evidence":"The change needs acceptance criteria and a verification plan.","verification":{"passed":true,"checks":["requirements_complete","verification_distinguished"]}}',
            "Final answer",
        ])

        async def capture(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            captured.append(payload)
            return httpx.Response(200, json=_ollama_response(next(responses), model=payload["model"]))

        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.post("/api/chat").mock(side_effect=capture)
            response = await client.post(
                "/v1/agent/completions",
                json={"messages": [{"role": "user", "content": "Review this code change carefully."}]},
            )

        assert response.status_code == 200
        body = response.json()
        assert body["mode"] == "adaptive"
        assert body["choices"][0]["message"]["content"] == "Final answer"
        assert body["metadata"]["steps_completed"] == 6
        assert body["metadata"]["quality_profile"] == "coding"
        assert body["metadata"]["verification_passed"] is True
        assert body["metadata"]["verification_checks"] == [
            "requirements_complete", "verification_distinguished",
        ]
        assert len(captured) == 6
        assert [item["think"] for item in captured] == [True, True, True, True, True, False]
        assert "exactly one JSON object" in captured[0]["messages"][0]["content"]
        assert "Accepted evidence ledger" in captured[-1]["messages"][-1]["content"]

    async def test_adaptive_verifier_fails_closed_on_invalid_protocol(self) -> None:
        from gateway.agent_orchestration import _GroundingEvidence, _verification_from_stage

        result = _verification_from_stage("not json", _GroundingEvidence(prompt=None, sources=[]))

        assert result.passed is False
        assert result.checks == ["structured_verifier_output"]
        assert "conservatively" in result.accepted_evidence

    async def test_adaptive_verifier_rejects_unknown_grounding_citations(self) -> None:
        from gateway.agent_orchestration import _GroundingEvidence, _verification_from_stage

        result = _verification_from_stage(
            '{"accepted_evidence":"Claim [unknown-source]","verification":{"passed":true,"checks":["citation_check"]}}',
            _GroundingEvidence(prompt="source", sources=[{"source_id": "known-source"}]),
        )

        assert result.passed is False
        assert "citation_labels_valid" in result.checks

    async def test_adaptive_rag_reserves_evidence_for_query_facets(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gateway.agent_orchestration import _retrieve_grounding
        from gateway.rag import config as rag_config
        from gateway.rag import store as rag_store

        monkeypatch.setattr(rag_config, "RAG_ENABLED", True)
        calls: list[tuple[str, int]] = []

        async def fake_search(query: str, *, top_k: int, document_id: str | None):
            calls.append((query, top_k))
            return [{
                "source_id": query,
                "filename": "evidence.md",
                "document_id": document_id,
                "chunk_index": 0,
                "text": query,
            }]

        monkeypatch.setattr(rag_store, "search", fake_search)
        request = AgentCompletionRequest.model_validate({
            "mode": "adaptive",
            "use_rag": True,
            "rag_document_id": "doc-1",
            "messages": [{"role": "user", "content": "primary question"}],
        })

        evidence = await _retrieve_grounding(request, ["facet one", "facet two"])

        assert calls == [("primary question", 2), ("facet one", 1), ("facet two", 1)]
        assert [source["source_id"] for source in evidence.sources] == [
            "primary question", "facet one", "facet two",
        ]

    async def test_agent_learning_telemetry_is_redacted(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        from gateway.learning import record_agent_completion
        from gateway.models import AgentCompletionMetadata, AgentCompletionResponse, ChatCompletionChoice, ChatCompletionUsage, ChatMessage

        request = AgentCompletionRequest.model_validate({"mode": "graph", "messages": [{"role": "user", "content": "private prompt"}]})
        response = AgentCompletionResponse(
            id="agentcmpl-test",
            created=1,
            mode="graph",
            model="qwen3.5:9b",
            choices=[ChatCompletionChoice(index=0, message=ChatMessage(role="assistant", content="private answer"), finish_reason="stop")],
            usage=ChatCompletionUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            metadata=AgentCompletionMetadata(steps_completed=1, elapsed_ms=1),
        )

        record_agent_completion(request, response, ["private stage"], ["stop"], str(tmp_path), "agent-policy-v1")

        output = (tmp_path / "records.jsonl").read_text(encoding="utf-8")
        assert "private prompt" not in output
        assert "private answer" not in output
        assert "private stage" not in output
    async def test_final_answer_removes_ungrounded_citations(self) -> None:
        from gateway.agent_orchestration import _clean_final_answer

        assert _clean_final_answer("Claim [source-1] and [2].", set()) == "Claim and."
        assert _clean_final_answer("Claim [a0b1] and code [index].", {"a0b1"}) == "Claim [a0b1] and code [index]."

    async def test_graph_runs_all_nodes_and_returns_only_final_answer(
        self,
        client: httpx.AsyncClient,
    ) -> None:
        captured: list[dict[str, object]] = []
        responses = iter(["plan", "draft", "critique", "verified ledger", "final answer"])

        async def capture(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(
                200,
                json=_ollama_response(next(responses), model="qwen3.5:9b"),
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
        assert body["model"] == "qwen3.5:9b"
        assert body["choices"][0]["message"]["content"] == "final answer"
        assert body["metadata"] == {"steps_completed": 5, "elapsed_ms": body["metadata"]["elapsed_ms"]}
        assert body["usage"] == {"prompt_tokens": 50, "completion_tokens": 15, "total_tokens": 65}
        assert len(captured) == 5
        assert [item["model"] for item in captured] == ["qwen3.5:9b"] * 5
        assert "planner" in captured[0]["messages"][0]["content"].lower()
        assert "Review work product" in captured[3]["messages"][-1]["content"]
        assert "Accepted evidence ledger" in captured[4]["messages"][-1]["content"]
        assert captured[4]["messages"][-1]["role"] == "user"
        assert [item["think"] for item in captured] == [True, True, True, True, False]
        assert [item["options"]["num_predict"] for item in captured] == [1000, 1000, 1000, 1000, 1600]
        assert [item["options"]["num_ctx"] for item in captured] == [8192] * 5

    async def test_expert_ensemble_uses_repeated_14b_experts_then_verifies_and_writes(
        self,
        client: httpx.AsyncClient,
    ) -> None:
        captured: list[dict[str, object]] = []
        responses = iter(["first opinion", "second opinion", "verified ledger", "synthesis"])

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
                    "expert_models": ["agent", "agent"],
                    "messages": [{"role": "user", "content": "Compare the options."}],
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert body["choices"][0]["message"]["content"] == "synthesis"
        assert body["metadata"]["steps_completed"] == 4
        assert body["metadata"]["expert_models"] == ["qwen3:14b", "qwen3:14b"]
        assert [item["model"] for item in captured] == ["qwen3:14b"] * 4
        assert "Specialist 1 work product" in captured[2]["messages"][-2]["content"]
        assert "Accepted evidence ledger" in captured[3]["messages"][-1]["content"]
        assert [item["think"] for item in captured] == [True, True, True, False]
        assert [item["options"]["num_predict"] for item in captured] == [1000, 1000, 1000, 1600]
        assert [item["options"]["num_ctx"] for item in captured] == [8192] * 4

    async def test_default_experts_mix_the_9b_candidate_with_14b_review(
        self,
        client: httpx.AsyncClient,
    ) -> None:
        captured: list[dict[str, object]] = []
        responses = iter(["first", "second", "third", "verified ledger", "synthesis"])

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
                    "messages": [{"role": "user", "content": "Compare the options."}],
                },
            )

        assert response.status_code == 200
        assert response.json()["metadata"]["expert_models"] == ["qwen3.5:9b", "qwen3.5:9b", "qwen3:14b"]
        assert [item["model"] for item in captured] == ["qwen3.5:9b", "qwen3.5:9b", "qwen3:14b", "qwen3.5:9b", "qwen3.5:9b"]
        assert [item["think"] for item in captured] == [True, True, True, True, False]

    async def test_graph_bounds_each_work_product_for_the_final_context(
        self,
        client: httpx.AsyncClient,
    ) -> None:
        captured: list[dict[str, object]] = []
        responses = iter(["p" * 10_000, "d" * 10_000, "c" * 10_000, "v" * 10_000, "final answer"])

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
                    "mode": "graph",
                    "messages": [{"role": "user", "content": "Solve this carefully."}],
                },
            )

        assert response.status_code == 200
        verifier_work_products = [message["content"] for message in captured[3]["messages"][2:]]
        assert len(verifier_work_products) == 3
        assert all(len(content) <= 2_600 for content in verifier_work_products)
        assert len(captured[4]["messages"]) == 3
        assert "Accepted evidence ledger" in captured[4]["messages"][-1]["content"]
        assert len(captured[4]["messages"][-1]["content"]) <= 2_600
        assert all("[Work product truncated]" in content for content in verifier_work_products)

    async def test_final_answer_strips_qwen_reasoning_artifacts(
        self,
        client: httpx.AsyncClient,
    ) -> None:
        responses = iter(["plan", "draft", "critique", "verified", "Final answer:\n</think>\n\n4"])

        async def capture(request: httpx.Request) -> httpx.Response:
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
                    "messages": [{"role": "user", "content": "What is 2 plus 2?"}],
                },
            )

        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "4"

    async def test_quality_agent_rejects_weaker_explicit_experts(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            "/v1/agent/completions",
            json={
                "mode": "mixture_of_experts",
                "expert_models": ["main", "small"],
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "quality_model_required"

    async def test_quality_agent_retrieves_grounding_once_and_preserves_sources(
        self,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gateway.rag import config as rag_config
        from gateway.rag import store as rag_store

        monkeypatch.setattr(rag_config, "RAG_ENABLED", True)
        search = pytest.MonkeyPatch()
        try:
            async def fake_search(*args, **kwargs):
                return [{
                    "source_id": "point-1",
                    "filename": "evidence.md",
                    "document_id": "doc-1",
                    "chunk_index": 2,
                    "text": "The documented answer is four.",
                }]

            search.setattr(rag_store, "search", fake_search)
            responses = iter(["plan", "draft", "critique", "verified", "[point-1] Four."])
            captured: list[dict[str, object]] = []

            async def capture(request: httpx.Request) -> httpx.Response:
                payload = json.loads(request.content)
                captured.append(payload)
                return httpx.Response(200, json=_ollama_response(next(responses), model="qwen3:14b"))

            with respx.mock(base_url=OLLAMA_BASE) as mock:
                mock.post("/api/chat").mock(side_effect=capture)
                response = await client.post(
                    "/v1/agent/completions",
                    json={
                        "mode": "graph",
                        "use_rag": True,
                        "rag_document_id": "doc-1",
                        "messages": [{"role": "user", "content": "What is the documented answer?"}],
                    },
                )
        finally:
            search.undo()

        assert response.status_code == 200
        assert all("[point-1] evidence.md" in item["messages"][1]["content"] for item in captured)
        assert response.json()["metadata"]["grounding_sources"] == [{
            "source_id": "point-1", "filename": "evidence.md", "document_id": "doc-1", "chunk_index": 2,
        }]

    async def test_quality_agent_rejects_source_context_that_cannot_fit(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            "/v1/agent/completions",
            json={"mode": "graph", "messages": [{"role": "user", "content": "x" * 12001}]},
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "agent_context_too_large"

    async def test_quality_agent_accepts_an_explicit_context_experiment(self, client: httpx.AsyncClient) -> None:
        captured: list[dict[str, object]] = []

        async def capture(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(200, json=_ollama_response("ledger", model="qwen3.5:9b"))

        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.post("/api/chat").mock(side_effect=capture)
            response = await client.post(
                "/v1/agent/completions",
                json={"mode": "graph", "context_length": 12288, "messages": [{"role": "user", "content": "Compare contexts."}]},
            )

        assert response.status_code == 200
        assert [item["options"]["num_ctx"] for item in captured] == [12288] * 5

    async def test_accepts_repeated_14b_experts_for_self_critique(self) -> None:
        request = AgentCompletionRequest.model_validate(
            {
                "mode": "mixture_of_experts",
                "expert_models": ["agent", "agent"],
                "messages": [{"role": "user", "content": "Hi"}],
            }
        )

        assert request.expert_models == ["agent", "agent"]

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
