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


def test_ollama_url_with_path_is_rejected():
    with pytest.raises(ValidationError):
        Settings(ollama_base_url="http://127.0.0.1:11434/foo")


def test_ollama_url_with_query_is_rejected():
    with pytest.raises(ValidationError):
        Settings(ollama_base_url="http://127.0.0.1:11434?x=1")


def test_ollama_url_with_credentials_is_rejected():
    with pytest.raises(ValidationError):
        Settings(ollama_base_url="http://user:pass@127.0.0.1:11434")


def test_auth_enabled_requires_non_empty_api_key():
    with pytest.raises(ValidationError):
        Settings(enable_api_key_auth=True, api_key="")


def test_auth_enabled_accepts_non_empty_api_key():
    settings = Settings(enable_api_key_auth=True, api_key="test-secret")
    assert settings.api_key == "test-secret"


def test_invalid_default_model_profile_is_rejected_by_default():
    with pytest.raises(ValidationError):
        Settings(default_model_profile="llama3:8b")


def test_direct_default_model_tag_is_allowed():
    settings = Settings(default_model_profile="qwen3.5:4b")
    assert settings.default_model_profile == "qwen3.5:4b"


def test_arbitrary_default_model_profile_is_allowed_when_enabled():
    settings = Settings(default_model_profile="llama3:8b", enable_arbitrary_models=True)
    assert settings.default_model_profile == "llama3:8b"


def test_invalid_default_whisper_model_is_rejected():
    with pytest.raises(ValidationError):
        Settings(default_whisper_model="large")


def test_invalid_chatterbox_model_is_rejected():
    with pytest.raises(ValidationError):
        Settings(chatterbox_model="other")
