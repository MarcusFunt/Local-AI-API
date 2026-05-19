"""Tests for the built-in status web UI and status JSON endpoints."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

pytestmark = pytest.mark.asyncio
OLLAMA_BASE = "http://127.0.0.1:11434"


def _ollama_model(name: str, size: int = 123_456_789) -> dict:
    return {
        "name": name,
        "model": name,
        "modified_at": "2026-05-19T08:00:00Z",
        "size": size,
        "details": {
            "family": "qwen3.5",
            "parameter_size": name.split(":")[-1],
        },
    }


class TestStatusPage:
    async def test_root_renders_status_gui(self, client: httpx.AsyncClient):
        resp = await client.get("/")

        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Local AI API" in resp.text
        assert "End-to-end check" in resp.text
        assert "/status.json" in resp.text

    async def test_status_path_renders_same_gui(self, client: httpx.AsyncClient):
        resp = await client.get("/status")

        assert resp.status_code == 200
        assert "Local AI API" in resp.text


class TestStatusJson:
    async def test_reports_all_profiles_ready(self, client: httpx.AsyncClient):
        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.get("/api/tags").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "models": [
                            _ollama_model("qwen3.5:9b"),
                            _ollama_model("qwen3.5:4b"),
                            _ollama_model("qwen3.5:0.8b"),
                        ]
                    },
                )
            )
            resp = await client.get("/status.json")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["ollama"]["status"] == "ok"
        assert {model["alias"] for model in body["models"]} == {"main", "small", "dev"}
        assert all(model["status"] == "ready" for model in body["models"])

    async def test_reports_missing_dev_model_as_degraded(self, client: httpx.AsyncClient):
        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.get("/api/tags").mock(
                return_value=httpx.Response(
                    200,
                    json={"models": [_ollama_model("qwen3.5:9b")]},
                )
            )
            resp = await client.get("/status.json")

        assert resp.status_code == 200
        body = resp.json()
        dev = next(model for model in body["models"] if model["alias"] == "dev")
        assert body["status"] == "degraded"
        assert dev["model"] == "qwen3.5:0.8b"
        assert dev["status"] == "missing"

    async def test_ollama_error_returns_degraded_status(self, client: httpx.AsyncClient):
        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.get("/api/tags").mock(return_value=httpx.Response(500, text="nope"))
            resp = await client.get("/status.json")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["ollama"]["status"] == "error"
        assert "HTTP 500" in body["ollama"]["error"]


class TestStatusCheck:
    async def test_dev_check_uses_08b_model(self, client: httpx.AsyncClient):
        captured: list[dict] = []

        async def capture(request: httpx.Request, *args, **kwargs):
            captured.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "model": "qwen3.5:0.8b",
                    "created_at": "2026-05-19T08:00:00Z",
                    "message": {"role": "assistant", "content": "ok"},
                    "done": True,
                    "prompt_eval_count": 7,
                    "eval_count": 1,
                },
            )

        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.post("/api/chat").mock(side_effect=capture)
            resp = await client.post("/status/check")

        assert resp.status_code == 200
        assert captured[0]["model"] == "qwen3.5:0.8b"
        assert captured[0]["stream"] is False
        assert captured[0]["think"] is False
        body = resp.json()
        assert body["status"] == "passed"
        assert body["model_alias"] == "dev"
        assert body["model"] == "qwen3.5:0.8b"
