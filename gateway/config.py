from urllib.parse import urlparse

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ollama_base_url: str = "http://127.0.0.1:11434"
    host: str = "127.0.0.1"
    port: int = 8080
    default_model_profile: str = "main"
    enable_api_key_auth: bool = False
    api_key: str = ""
    request_timeout_seconds: int = 600
    max_request_body_bytes: int = 10_485_760
    enable_arbitrary_models: bool = False
    agent_zero_enabled: bool = False
    default_whisper_model: str = "none"
    whisper_device: str = "auto"
    whisper_cache_dir: str = ""
    chatterbox_model: str = "chatterbox"
    chatterbox_device: str = "auto"

    @field_validator("ollama_base_url")
    @classmethod
    def validate_ollama_base_url(cls, value: str) -> str:
        value = value.strip()
        parsed = urlparse(value)
        host = parsed.hostname.lower() if parsed.hostname else ""
        if parsed.scheme not in {"http", "https"} or not host:
            raise ValueError("OLLAMA_BASE_URL must be an http(s) URL.")
        if parsed.username or parsed.password:
            raise ValueError("OLLAMA_BASE_URL must not include credentials.")
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("OLLAMA_BASE_URL must point to a loopback host.")
        if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
            raise ValueError("OLLAMA_BASE_URL must not include a path, query, or fragment.")
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_auth_config(self) -> "Settings":
        from .normalize import CHATTERBOX_MODEL_MAP, MODEL_MAP, WHISPER_MODEL_MAP

        if self.enable_api_key_auth and not self.api_key.strip():
            raise ValueError("API_KEY must be set when ENABLE_API_KEY_AUTH=true.")

        self.default_model_profile = self.default_model_profile.strip()
        if not self.default_model_profile:
            raise ValueError("DEFAULT_MODEL_PROFILE must not be empty.")
        allowed_chat_models = set(MODEL_MAP) | set(MODEL_MAP.values())
        if (
            not self.enable_arbitrary_models
            and self.default_model_profile not in allowed_chat_models
        ):
            raise ValueError(
                "DEFAULT_MODEL_PROFILE must be a known alias or approved direct model tag."
            )

        self.default_whisper_model = self.default_whisper_model.strip()
        if self.default_whisper_model not in WHISPER_MODEL_MAP:
            raise ValueError("DEFAULT_WHISPER_MODEL must be one of the supported Whisper aliases.")

        self.chatterbox_model = self.chatterbox_model.strip()
        if self.chatterbox_model not in CHATTERBOX_MODEL_MAP:
            raise ValueError("CHATTERBOX_MODEL must be one of the supported Chatterbox aliases.")
        return self


settings = Settings()
