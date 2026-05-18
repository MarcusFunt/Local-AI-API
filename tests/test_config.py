"""Tests for application settings validation."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from gateway.config import Settings


def test_loopback_ollama_url_is_allowed():
    settings = Settings(ollama_base_url="http://127.0.0.1:11434")
    assert settings.ollama_base_url == "http://127.0.0.1:11434"


def test_routable_ollama_url_is_rejected():
    with pytest.raises(ValidationError):
        Settings(ollama_base_url="http://192.168.1.10:11434")


def test_zero_address_ollama_url_is_rejected():
    with pytest.raises(ValidationError):
        Settings(ollama_base_url="http://0.0.0.0:11434")


def test_auth_enabled_requires_non_empty_api_key():
    with pytest.raises(ValidationError):
        Settings(enable_api_key_auth=True, api_key="")


def test_auth_enabled_accepts_non_empty_api_key():
    settings = Settings(enable_api_key_auth=True, api_key="test-secret")
    assert settings.api_key == "test-secret"
