"""Durable, bounded policy primitives for local autonomous workspace runs."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


RUN_STATES = {"queued", "running", "evaluating", "paused", "review_ready", "stopped"}
TERMINAL_STATES = {"review_ready", "stopped"}
MAX_RUNTIME_SECONDS = 24 * 60 * 60
MAX_STORAGE_BYTES = 20 * 1024 * 1024 * 1024
MAX_NON_IMPROVING_EVALUATIONS = 3


@dataclass(frozen=True)
class AutonomyPolicy:
    """Hard limits that an agent cannot broaden through an MCP request."""

    max_runtime_seconds: int = MAX_RUNTIME_SECONDS
    max_storage_bytes: int = MAX_STORAGE_BYTES
    max_non_improving_evaluations: int = MAX_NON_IMPROVING_EVALUATIONS

    @classmethod
    def from_input(cls, value: dict[str, Any] | None = None) -> "AutonomyPolicy":
        value = value or {}
        try:
            runtime = int(value.get("max_runtime_seconds", MAX_RUNTIME_SECONDS))
            storage = int(value.get("max_storage_bytes", MAX_STORAGE_BYTES))
            non_improving = int(value.get("max_non_improving_evaluations", MAX_NON_IMPROVING_EVALUATIONS))
        except (TypeError, ValueError) as exc:
            raise ValueError("Autonomy policy limits must be integers.") from exc
        if not 1 <= runtime <= MAX_RUNTIME_SECONDS:
            raise ValueError("max_runtime_seconds must be between 1 and 86400.")
        if not 1 <= storage <= MAX_STORAGE_BYTES:
            raise ValueError("max_storage_bytes must be between 1 byte and 20 GiB.")
        if not 1 <= non_improving <= MAX_NON_IMPROVING_EVALUATIONS:
            raise ValueError("max_non_improving_evaluations must be between 1 and 3.")
        return cls(runtime, storage, non_improving)

    def as_dict(self) -> dict[str, int]:
        return asdict(self)
