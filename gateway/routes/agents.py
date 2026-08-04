"""Slow, performance-oriented agent orchestration endpoints."""
from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..agent_orchestration import run_agent
from ..config import settings
from ..models import AgentCompletionRequest

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/v1/agent/completions")
async def agent_completions(request: AgentCompletionRequest) -> JSONResponse:
    """Run a bounded multi-call agent without changing chat-completion latency."""
    logger.info(
        "Advanced agent request: mode=%s model=%r messages=%d experts=%d",
        request.mode,
        request.model,
        len(request.messages),
        len(request.expert_models),
    )
    response = await run_agent(request, settings)
    return JSONResponse(response.model_dump(exclude_none=True))
