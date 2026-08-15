"""Optional redacted telemetry for the advanced agent loop."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agent_learning import LearningRecordStore, build_learning_record, summarize_text

from .models import AgentCompletionRequest, AgentCompletionResponse


logger = logging.getLogger(__name__)


def record_agent_completion(
    request: AgentCompletionRequest,
    response: AgentCompletionResponse,
    stage_contents: list[str],
    stage_finish_reasons: list[str],
    learning_dir: str,
    policy_version: str,
) -> None:
    """Persist metadata only; telemetry errors must never fail a client completion."""
    if not learning_dir:
        return
    try:
        trace: dict[str, Any] = {
            "mode": request.mode,
            "message_count": len(request.messages),
            "grounding_enabled": request.use_rag,
            "stages": [
                {"finish_reason": reason, "output": summarize_text(content)}
                for content, reason in zip(stage_contents, stage_finish_reasons, strict=True)
            ],
        }
        record = build_learning_record(
            surface="gateway_agent",
            outcome="completed",
            policy_version=policy_version,
            metrics={
                "steps_completed": response.metadata.steps_completed,
                "elapsed_ms": response.metadata.elapsed_ms,
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
            },
            trace=trace,
        )
        LearningRecordStore(Path(learning_dir)).append(record)
    except Exception:  # pragma: no cover - telemetry must never fail an answer
        logger.exception("Could not record agent-learning telemetry")
