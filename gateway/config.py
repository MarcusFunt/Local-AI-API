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
    default_whisper_model: str = "none"
    whisper_device: str = "auto"
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
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("OLLAMA_BASE_URL must point to a loopback host.")
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_auth_config(self) -> "Settings":
        if self.enable_api_key_auth and not self.api_key.strip():
            raise ValueError("API_KEY must be set when ENABLE_API_KEY_AUTH=true.")
        return self


settings = Settings()
