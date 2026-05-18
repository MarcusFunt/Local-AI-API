"""Tests for GET /health and GET /health/ollama."""
from __future__ import annotations

import pytest
import httpx
import respx

pytestmark = pytest.mark.asyncio
OLLAMA_BASE = "http://127.0.0.1:11434"


class TestHealthGateway:
    async def test_health_always_200(self, client: httpx.AsyncClient):
        resp = await client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["gateway"] == "local-ai-api"
        assert "ollama_base_url" in body


class TestHealthOllama:
    async def test_ollama_up_returns_200(self, client: httpx.AsyncClient):
        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.get("/api/tags").mock(
                return_value=httpx.Response(200, json={"models": []})
            )
            resp = await client.get("/health/ollama")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_ollama_500_returns_502(self, client: httpx.AsyncClient):
        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.get("/api/tags").mock(
                return_value=httpx.Response(500, text="internal error")
            )
            resp = await client.get("/health/ollama")
        assert resp.status_code == 502
        assert resp.json()["error"]["code"] == "ollama_error"

    async def test_ollama_connect_error_returns_502(self, client: httpx.AsyncClient):
        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.get("/api/tags").mock(side_effect=httpx.ConnectError("refused"))
            resp = await client.get("/health/ollama")
        assert resp.status_code == 502
        error = resp.json()["error"]
        assert error["code"] == "ollama_error"
        assert "connect" in error["message"].lower()

    async def test_ollama_timeout_returns_502(self, client: httpx.AsyncClient):
        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.get("/api/tags").mock(side_effect=httpx.TimeoutException("timeout"))
            resp = await client.get("/health/ollama")
        assert resp.status_code == 502
        error = resp.json()["error"]
        assert error["code"] == "ollama_timeout"
        assert "timed out" in error["message"].lower()
