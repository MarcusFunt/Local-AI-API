"""Unit tests for model alias resolution (no HTTP involved)."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from gateway.config import Settings
from gateway.normalize import MODEL_MAP, resolve_model


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

    def test_small_resolves_to_small_model(self):
        assert resolve_model("small", _settings()) == "qwen3.5:4b"

    def test_all_aliases_covered(self):
        """Every key in MODEL_MAP must resolve without error."""
        s = _settings()
        for alias, expected in MODEL_MAP.items():
            assert resolve_model(alias, s) == expected


class TestDirectModelTags:
    def test_direct_large_tag_accepted(self):
        assert resolve_model("qwen3.5:9b", _settings()) == "qwen3.5:9b"

    def test_direct_small_tag_accepted(self):
        assert resolve_model("qwen3.5:4b", _settings()) == "qwen3.5:4b"


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


class TestWhitespaceHandling:
    def test_leading_trailing_whitespace_stripped(self):
        assert resolve_model("  main  ", _settings()) == "qwen3.5:9b"

    def test_whitespace_around_direct_tag(self):
        assert resolve_model("  qwen3.5:9b  ", _settings()) == "qwen3.5:9b"
