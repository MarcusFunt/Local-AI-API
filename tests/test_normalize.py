"""Unit tests for model alias resolution (no HTTP involved)."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from gateway.config import Settings
from gateway.normalize import (
    CHATTERBOX_MODEL_MAP,
    EMBEDDING_MODEL_MAP,
    MODEL_MAP,
    WHISPER_MODEL_MAP,
    required_model_aliases,
    resolve_chatterbox_model,
    resolve_embedding_model,
    resolve_model,
    resolve_whisper_model,
)


def _settings(**overrides) -> Settings:
    base = dict(
        ollama_base_url="http://localhost:11434",
        host="127.0.0.1",
        port=8080,
        default_model_profile="main",
        enable_api_key_auth=False,
        api_key="",
        request_timeout_seconds=60,
        max_request_body_bytes=10_485_760,
        enable_arbitrary_models=False,
    )
    base.update(overrides)
    return Settings(**base)


class TestAliasMapping:
    def test_main_resolves_to_large_model(self):
        assert resolve_model("main", _settings()) == "qwen3.5:9b"

    def test_quality_resolves_to_the_controlled_quality_candidate(self):
        assert resolve_model("quality", _settings()) == "qwen3.5:9b"

    def test_small_resolves_to_small_model(self):
        assert resolve_model("small", _settings()) == "qwen3.5:4b"

    def test_dev_resolves_to_development_model(self):
        assert resolve_model("dev", _settings()) == "qwen3.5:0.8b"

    def test_agent_resolves_to_agent_model(self):
        assert resolve_model("agent", _settings()) == "qwen3:14b"

    def test_agent_utility_resolves_to_utility_model(self):
        assert resolve_model("agent-utility", _settings()) == "qwen3:14b"

    def test_all_aliases_covered(self):
        """Every key in MODEL_MAP must resolve without error."""
        s = _settings()
        for alias, expected in MODEL_MAP.items():
            assert resolve_model(alias, s) == expected

    def test_agent_zero_aliases_are_required(self):
        assert required_model_aliases() == ("main", "quality", "small", "dev", "agent", "agent-utility")


class TestDirectModelTags:
    def test_direct_large_tag_accepted(self):
        assert resolve_model("qwen3.5:9b", _settings()) == "qwen3.5:9b"

    def test_direct_small_tag_accepted(self):
        assert resolve_model("qwen3.5:4b", _settings()) == "qwen3.5:4b"

    def test_direct_development_tag_accepted(self):
        assert resolve_model("qwen3.5:0.8b", _settings()) == "qwen3.5:0.8b"

    def test_direct_agent_tag_accepted(self):
        assert resolve_model("qwen3:14b", _settings()) == "qwen3:14b"

    def test_retired_weaker_utility_tag_is_rejected(self):
        with pytest.raises(HTTPException):
            resolve_model("qwen3:8b", _settings())


class TestSafeProviderPrefixes:
    def test_openai_prefixed_alias_resolves(self):
        assert resolve_model("openai/agent", _settings()) == "qwen3:14b"

    def test_openai_prefixed_direct_tag_resolves(self):
        assert resolve_model("openai/qwen3:14b", _settings()) == "qwen3:14b"

    def test_other_provider_prefix_is_not_stripped(self):
        with pytest.raises(HTTPException) as exc_info:
            resolve_model("anthropic/agent", _settings())
        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["error"]["code"] == "model_not_found"

    def test_unknown_openai_prefixed_model_is_rejected_by_default(self):
        with pytest.raises(HTTPException) as exc_info:
            resolve_model("openai/llama3:8b", _settings())
        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["error"]["code"] == "model_not_found"


class TestArbitraryGating:
    def test_unknown_alias_raises_422_by_default(self):
        with pytest.raises(HTTPException) as exc_info:
            resolve_model("llama3:8b", _settings())
        assert exc_info.value.status_code == 422
        error = exc_info.value.detail["error"]
        assert error["code"] == "model_not_found"
        assert "llama3:8b" in error["message"]

    def test_empty_string_raises_422(self):
        with pytest.raises(HTTPException) as exc_info:
            resolve_model("", _settings())
        assert exc_info.value.status_code == 422

    def test_unknown_alias_allowed_when_arbitrary_enabled(self):
        s = _settings(enable_arbitrary_models=True)
        result = resolve_model("llama3:8b", s)
        assert result == "llama3:8b"

    def test_any_string_passes_when_arbitrary_enabled(self):
        s = _settings(enable_arbitrary_models=True)
        assert resolve_model("my-custom-model:latest", s) == "my-custom-model:latest"

    def test_empty_string_rejected_when_arbitrary_enabled(self):
        s = _settings(enable_arbitrary_models=True)
        with pytest.raises(HTTPException) as exc_info:
            resolve_model("   ", s)
        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["error"]["code"] == "model_not_found"


class TestWhitespaceHandling:
    def test_leading_trailing_whitespace_stripped(self):
        assert resolve_model("  main  ", _settings()) == "qwen3.5:9b"

    def test_whitespace_around_direct_tag(self):
        assert resolve_model("  qwen3.5:9b  ", _settings()) == "qwen3.5:9b"

    def test_whitespace_around_whisper_alias(self):
        assert resolve_whisper_model("  tiny  ", _settings()) == "tiny"

    def test_whitespace_around_chatterbox_alias(self):
        assert resolve_chatterbox_model("  chatterbox  ", _settings()) == "chatterbox"


class TestWhisperModelMapping:
    def test_none_disables_whisper(self):
        assert resolve_whisper_model("none", _settings()) is None

    def test_all_whisper_aliases_covered(self):
        s = _settings()
        for alias, expected in WHISPER_MODEL_MAP.items():
            assert resolve_whisper_model(alias, s) == expected

    def test_unknown_whisper_model_raises_422(self):
        with pytest.raises(HTTPException) as exc_info:
            resolve_whisper_model("large", _settings())
        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["error"]["code"] == "audio_model_not_found"


class TestChatterboxModelMapping:
    def test_all_chatterbox_aliases_covered(self):
        s = _settings()
        for alias, expected in CHATTERBOX_MODEL_MAP.items():
            assert resolve_chatterbox_model(alias, s) == expected

    def test_unknown_chatterbox_model_raises_422(self):
        with pytest.raises(HTTPException) as exc_info:
            resolve_chatterbox_model("other-tts", _settings())
        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["error"]["code"] == "audio_model_not_found"


class TestEmbeddingModelMapping:
    def test_all_embedding_aliases_covered(self):
        settings = _settings()
        for alias, expected in EMBEDDING_MODEL_MAP.items():
            assert resolve_embedding_model(alias, settings) == expected

    def test_direct_embedding_tag_accepted(self):
        assert resolve_embedding_model("nomic-embed-text", _settings()) == "nomic-embed-text"

    def test_openai_prefixed_embedding_alias_resolves(self):
        assert resolve_embedding_model("openai/embedding", _settings()) == "nomic-embed-text"

    def test_chat_alias_is_rejected_by_embedding_endpoint(self):
        with pytest.raises(HTTPException) as exc_info:
            resolve_embedding_model("main", _settings())
        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["error"]["code"] == "model_not_found"
