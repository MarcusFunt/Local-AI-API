"""Tests for optional bearer-token auth middleware."""
from __future__ import annotations

import json
import pytest
import httpx
import respx

from gateway.config import Settings
from gateway.main import create_app
import gateway.client as client_module

pytestmark = pytest.mark.asyncio
OLLAMA_BASE = "http://127.0.0.1:11434"


def _make_client(settings: Settings) -> httpx.AsyncClient:
    import gateway.config as cfg_module
    import gateway.routes.health as health_module
    import gateway.routes.chat as chat_module
    import gateway.routes.status as status_module
    from gateway import main as main_module

    # We create a fresh app per test so middleware reads the right settings.
    # We can't use monkeypatch here because these are module-level fixtures,
    # so we patch directly and rely on cleanup via the caller.
    cfg_module.settings = settings
    health_module.settings = settings
    chat_module.settings = settings
    status_module.settings = settings
    main_module.settings = settings

    app = create_app()
    client_module.init(settings)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


OLLAMA_SUCCESS = {
    "model": "qwen3.5:9b",
    "created_at": "2024-01-01T00:00:00Z",
    "message": {"role": "assistant", "content": "hi"},
    "done": True,
    "prompt_eval_count": 5,
    "eval_count": 2,
}

CHAT_PAYLOAD = {
    "model": "main",
    "messages": [{"role": "user", "content": "hello"}],
}


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    await client_module.close()


class TestAuthDisabled:
    async def test_no_header_allowed(self):
        settings = Settings(
            ollama_base_url=OLLAMA_BASE,
            enable_api_key_auth=False,
            api_key="secret",
            enable_arbitrary_models=False,
            request_timeout_seconds=10,
            max_request_body_bytes=10_485_760,
        )
        async with _make_client(settings) as ac:
            with respx.mock(base_url=OLLAMA_BASE) as mock:
                mock.post("/api/chat").mock(
                    return_value=httpx.Response(200, json=OLLAMA_SUCCESS)
                )
                resp = await ac.post("/v1/chat/completions", json=CHAT_PAYLOAD)
        assert resp.status_code == 200

    async def test_wrong_key_still_allowed_when_auth_disabled(self):
        settings = Settings(
            ollama_base_url=OLLAMA_BASE,
            enable_api_key_auth=False,
            api_key="secret",
            enable_arbitrary_models=False,
            request_timeout_seconds=10,
            max_request_body_bytes=10_485_760,
        )
        async with _make_client(settings) as ac:
            with respx.mock(base_url=OLLAMA_BASE) as mock:
                mock.post("/api/chat").mock(
                    return_value=httpx.Response(200, json=OLLAMA_SUCCESS)
                )
                resp = await ac.post(
                    "/v1/chat/completions",
                    json=CHAT_PAYLOAD,
                    headers={"Authorization": "Bearer wrong"},
                )
        assert resp.status_code == 200


class TestAuthEnabled:
    def _auth_settings(self) -> Settings:
        return Settings(
            ollama_base_url=OLLAMA_BASE,
            enable_api_key_auth=True,
            api_key="test-secret",
            enable_arbitrary_models=False,
            request_timeout_seconds=10,
            max_request_body_bytes=10_485_760,
        )

    async def test_missing_header_returns_401(self):
        async with _make_client(self._auth_settings()) as ac:
            resp = await ac.post("/v1/chat/completions", json=CHAT_PAYLOAD)
        assert resp.status_code == 401
        body = resp.json()
        assert body["error"]["code"] == "invalid_api_key"

    async def test_wrong_key_returns_401(self):
        async with _make_client(self._auth_settings()) as ac:
            resp = await ac.post(
                "/v1/chat/completions",
                json=CHAT_PAYLOAD,
                headers={"Authorization": "Bearer wrong-key"},
            )
        assert resp.status_code == 401

    async def test_correct_key_allowed(self):
        async with _make_client(self._auth_settings()) as ac:
            with respx.mock(base_url=OLLAMA_BASE) as mock:
                mock.post("/api/chat").mock(
                    return_value=httpx.Response(200, json=OLLAMA_SUCCESS)
                )
                resp = await ac.post(
                    "/v1/chat/completions",
                    json=CHAT_PAYLOAD,
                    headers={"Authorization": "Bearer test-secret"},
                )
        assert resp.status_code == 200

    async def test_health_bypasses_auth_no_header(self):
        async with _make_client(self._auth_settings()) as ac:
            resp = await ac.get("/health")
        assert resp.status_code == 200

    async def test_health_ollama_bypasses_auth_no_header(self):
        async with _make_client(self._auth_settings()) as ac:
            with respx.mock(base_url=OLLAMA_BASE) as mock:
                mock.get("/api/tags").mock(
                    return_value=httpx.Response(200, json={"models": []})
                )
                resp = await ac.get("/health/ollama")
        assert resp.status_code == 200

    async def test_status_gui_requires_auth_no_header(self):
        async with _make_client(self._auth_settings()) as ac:
            resp = await ac.get("/status")
        assert resp.status_code == 401

    async def test_status_json_requires_auth_no_header(self):
        async with _make_client(self._auth_settings()) as ac:
            resp = await ac.get("/status.json")
        assert resp.status_code == 401
