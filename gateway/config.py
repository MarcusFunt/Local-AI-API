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


settings = Settings()
