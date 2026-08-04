"""Tests for the local OpenAI-compatible embeddings endpoint."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

OLLAMA_BASE = "http://127.0.0.1:11434"

pytestmark = pytest.mark.asyncio


class TestEmbeddings:
    async def test_embedding_alias_proxies_to_local_ollama(
        self,
        client: httpx.AsyncClient,
    ) -> None:
        captured: list[dict[str, object]] = []

        async def capture(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"embeddings": [[0.1, -0.2], [0.3, 0.4]]})

        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.post("/api/embed").mock(side_effect=capture)
            response = await client.post(
                "/v1/embeddings",
                json={"model": "embedding", "input": ["first", "second"]},
            )

        assert response.status_code == 200
        assert captured == [{"model": "nomic-embed-text", "input": ["first", "second"]}]
        assert response.json() == {
            "object": "list",
            "data": [
                {"object": "embedding", "embedding": [0.1, -0.2], "index": 0},
                {"object": "embedding", "embedding": [0.3, 0.4], "index": 1},
            ],
            "model": "nomic-embed-text",
            "usage": {"prompt_tokens": 0, "total_tokens": 0},
        }

    async def test_rejects_invalid_embedding_input_and_model(
        self,
        client: httpx.AsyncClient,
    ) -> None:
        empty = await client.post("/v1/embeddings", json={"input": []})
        unknown = await client.post(
            "/v1/embeddings",
            json={"model": "not-allowed", "input": "text"},
        )

        assert empty.status_code == 422
        assert unknown.status_code == 422
        assert unknown.json()["error"]["code"] == "model_not_found"
