"""Tests for OpenAI-style model listing."""
from __future__ import annotations

import httpx
import pytest

from gateway.normalize import allowed_model_ids

pytestmark = pytest.mark.asyncio


async def test_list_models_returns_allowed_model_ids(client: httpx.AsyncClient):
    resp = await client.get("/v1/models")

    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    ids = [item["id"] for item in body["data"]]
    assert ids == allowed_model_ids()
    assert "agent" in ids
    assert "agent-utility" in ids
    assert "qwen3:14b" in ids
    assert "qwen3:8b" in ids
    assert all(item["object"] == "model" for item in body["data"])
