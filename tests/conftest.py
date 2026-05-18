"""Shared pytest fixtures for the gateway test suite."""
from __future__ import annotations

import pytest
import httpx

from gateway.config import Settings
from gateway.main import create_app

OLLAMA_TEST_BASE = "http://127.0.0.1:11434"


@pytest.fixture()
def default_settings() -> Settings:
    """Settings with test-safe defaults (no auth, no arbitrary models)."""
    return Settings(
        ollama_base_url=OLLAMA_TEST_BASE,
        host="127.0.0.1",
        port=8080,
        default_model_profile="main",
        enable_api_key_auth=False,
        api_key="",
        request_timeout_seconds=10,
        max_request_body_bytes=10_485_760,
        enable_arbitrary_models=False,
    )


@pytest.fixture()
def auth_settings() -> Settings:
    """Settings with API-key auth enabled."""
    return Settings(
        ollama_base_url=OLLAMA_TEST_BASE,
        host="127.0.0.1",
        port=8080,
        default_model_profile="main",
        enable_api_key_auth=True,
        api_key="test-secret",
        request_timeout_seconds=10,
        max_request_body_bytes=10_485_760,
        enable_arbitrary_models=False,
    )


@pytest.fixture()
def arbitrary_settings() -> Settings:
    """Settings with arbitrary model names enabled."""
    return Settings(
        ollama_base_url=OLLAMA_TEST_BASE,
        host="127.0.0.1",
        port=8080,
        default_model_profile="main",
        enable_api_key_auth=False,
        api_key="",
        request_timeout_seconds=10,
        max_request_body_bytes=1024,
        enable_arbitrary_models=True,
    )


@pytest.fixture()
async def client(default_settings: Settings, monkeypatch: pytest.MonkeyPatch):
    """Async test client wired to the app, with patched settings."""
    import gateway.config as cfg_module
    import gateway.routes.health as health_module
    import gateway.routes.chat as chat_module
    import gateway.client as client_module

    monkeypatch.setattr(cfg_module, "settings", default_settings)
    monkeypatch.setattr(health_module, "settings", default_settings)
    monkeypatch.setattr(chat_module, "settings", default_settings)

    # Patch the module-level settings used by middleware (read at request time)
    from gateway import main as main_module
    monkeypatch.setattr(main_module, "settings", default_settings)

    app = create_app()

    # Initialise the ollama client with test settings
    client_module.init(default_settings)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    await client_module.close()
