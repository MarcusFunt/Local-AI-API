"""Configuration for the local-only lab controller service."""
from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class ControllerSettings(BaseSettings):
    """Settings kept separate from the network-facing gateway configuration."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    controller_host: str = "127.0.0.1"
    controller_port: int = Field(default=8091, ge=1, le=65535)
    controller_database_path: str = ".local/lab-controller/controller.sqlite3"
    controller_max_list_limit: int = Field(default=100, ge=1, le=500)
    controller_lease_seconds: int = Field(default=60, ge=30, le=300)
    controller_scheduler_interval_seconds: int = Field(default=5, ge=1, le=60)
    controller_worker_token: str = ""
    controller_allowed_candidate_target_prefixes: str = (
        "agent-zero/,gateway/,rag/,repo-ops/"
    )
    controller_allowed_candidate_change_fields: str = (
        "system_prompt,stage_order,stage_token_limits,tool_preference,"
        "skill_manifest,model_routing,rag_configuration,patch_manifest"
    )

    @field_validator("controller_host")
    @classmethod
    def validate_loopback_host(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in _LOOPBACK_HOSTS:
            raise ValueError("CONTROLLER_HOST must be a loopback host.")
        return normalized

    @field_validator("controller_database_path")
    @classmethod
    def validate_database_path(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\x00" in normalized:
            raise ValueError("CONTROLLER_DATABASE_PATH must be a non-empty filesystem path.")
        return normalized

    @field_validator("controller_worker_token")
    @classmethod
    def normalize_worker_token(cls, value: str) -> str:
        return value.strip()

    @field_validator("controller_allowed_candidate_target_prefixes")
    @classmethod
    def validate_target_prefixes(cls, value: str) -> str:
        prefixes = [prefix.strip() for prefix in value.split(",") if prefix.strip()]
        if not prefixes:
            raise ValueError("At least one controller candidate target prefix is required.")
        if any("/" not in prefix for prefix in prefixes):
            raise ValueError("Candidate target prefixes must include a namespace separator.")
        return ",".join(prefixes)

    @field_validator("controller_allowed_candidate_change_fields")
    @classmethod
    def validate_change_fields(cls, value: str) -> str:
        fields = [field.strip() for field in value.split(",") if field.strip()]
        if not fields:
            raise ValueError("At least one controller candidate change field is required.")
        if any("/" in field or "," in field for field in fields):
            raise ValueError("Candidate change fields must be simple field names.")
        return ",".join(fields)

    @property
    def candidate_target_prefixes(self) -> tuple[str, ...]:
        """Return normalized candidate target prefixes."""
        return tuple(self.controller_allowed_candidate_target_prefixes.split(","))

    @property
    def candidate_change_fields(self) -> tuple[str, ...]:
        """Return approved bounded change fields."""
        return tuple(self.controller_allowed_candidate_change_fields.split(","))


settings = ControllerSettings()
