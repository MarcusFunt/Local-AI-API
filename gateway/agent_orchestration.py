"""Deliberate multi-call agents built on the gateway's existing Ollama client.

This is intentionally a small LangGraph-style state machine rather than a new
framework dependency.  Each stage is explicit, bounded, and uses the existing
model allow-list, timeout, auth, and OpenAI-to-Ollama translation paths.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass

from fastapi import HTTPException

from . import client as ollama_client
from .config import Settings
from .models import (
    AgentCompletionMetadata,
    AgentCompletionRequest,
    AgentCompletionResponse,
    ChatCompletionChoice,
    ChatCompletionUsage,
    ChatMessage,
)
from .normalize import resolve_model

logger = logging.getLogger(__name__)

# The deployed qwen3:14b profile has a 4,096-token Ollama context window on
# this hardware. Keep every deliberation pass on that model and leave enough
# room for the finalizer to consume the complete set of concise work products.
_DEFAULT_EXPERT_MODELS = ("agent", "agent", "agent")
_MAX_STAGE_CONTEXT_CHARS = 2_000
_INTERNAL_STAGE_MAX_TOKENS = 512
_FINAL_STAGE_MAX_TOKENS = 1_536
_THINKING_BLOCK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_THINKING_TAG_RE = re.compile(r"</?think>", re.IGNORECASE)
_FINAL_LABEL_RE = re.compile(r"^\s*final answer\s*:\s*", re.IGNORECASE)


@dataclass(frozen=True)
class _StageResult:
    content: str
    finish_reason: str
    usage: ChatCompletionUsage


def _bounded(value: str) -> str:
    """Keep internal work products useful without unbounded prompt growth."""
    if len(value) <= _MAX_STAGE_CONTEXT_CHARS:
        return value
    return value[:_MAX_STAGE_CONTEXT_CHARS] + "\n[Work product truncated]"


def _stage_max_tokens(request: AgentCompletionRequest, ceiling: int) -> int:
    """Respect a caller's smaller limit while protecting the 4k shared context."""
    requested = request.max_tokens
    if requested is None:
        requested = request.max_completion_tokens
    return ceiling if requested is None else min(requested, ceiling)


def _clean_final_answer(content: str) -> str:
    """Remove Qwen thinking markup and the internal finalizer label from user output."""
    cleaned = _THINKING_BLOCK_RE.sub("", content)
    cleaned = _THINKING_TAG_RE.sub("", cleaned)
    cleaned = _FINAL_LABEL_RE.sub("", cleaned)
    return cleaned.strip()


def _request_dict(
    request: AgentCompletionRequest,
    messages: list[ChatMessage],
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> dict[str, object]:
    if max_tokens is None:
        max_tokens = request.max_tokens
        if max_tokens is None:
            max_tokens = request.max_completion_tokens
        if max_tokens is None:
            max_tokens = _FINAL_STAGE_MAX_TOKENS
    return {
        "messages": [message.model_dump() for message in messages],
        "temperature": request.temperature if temperature is None else temperature,
        "top_p": request.top_p,
        "max_tokens": max_tokens,
        "stop": request.stop,
        "seed": request.seed,
    }


def _stage_messages(
    request: AgentCompletionRequest,
    instruction: str,
    work_products: list[tuple[str, str]] | None = None,
) -> list[ChatMessage]:
    messages = [ChatMessage(role="system", content=instruction)]
    messages.extend(message.model_copy(deep=True) for message in request.messages)
    for label, work_product in work_products or []:
        messages.append(
            ChatMessage(
                role="assistant",
                content=f"{label}:\n{_bounded(work_product)}",
            )
        )
    return messages


async def _run_stage(
    resolved_model: str,
    request: AgentCompletionRequest,
    settings: Settings,
    instruction: str,
    *,
    work_products: list[tuple[str, str]] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> _StageResult:
    completion = await ollama_client.proxy_non_streaming(
        resolved_model,
        _request_dict(
            request,
            _stage_messages(request, instruction, work_products),
            temperature=temperature,
            max_tokens=max_tokens,
        ),
        settings,
    )
    choice = completion.choices[0]
    content = choice.message.content
    if not isinstance(content, str):
        content = str(content or "")
    return _StageResult(
        content=content,
        finish_reason=choice.finish_reason,
        usage=completion.usage,
    )


def _aggregate_usage(stages: list[_StageResult]) -> ChatCompletionUsage:
    prompt_tokens = sum(stage.usage.prompt_tokens for stage in stages)
    completion_tokens = sum(stage.usage.completion_tokens for stage in stages)
    return ChatCompletionUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )


async def _run_graph(
    request: AgentCompletionRequest,
    resolved_model: str,
    settings: Settings,
) -> tuple[_StageResult, list[_StageResult]]:
    plan = await _run_stage(
        resolved_model,
        request,
        settings,
        "You are the planner in a deliberate agent graph. Identify the task, constraints, "
        "unknowns, and a concise plan. Do not write the final answer.",
        max_tokens=_stage_max_tokens(request, _INTERNAL_STAGE_MAX_TOKENS),
    )
    draft = await _run_stage(
        resolved_model,
        request,
        settings,
        "You are the execution node in a deliberate agent graph. Produce a technically "
        "sound draft answer using the supplied plan. State assumptions that affect the result.",
        work_products=[("Planner work product", plan.content)],
        max_tokens=_stage_max_tokens(request, _INTERNAL_STAGE_MAX_TOKENS),
    )
    critique = await _run_stage(
        resolved_model,
        request,
        settings,
        "You are the review node in a deliberate agent graph. Check the draft for factual "
        "gaps, missed constraints, unsafe advice, and unclear reasoning. Give concrete corrections.",
        work_products=[("Planner work product", plan.content), ("Draft work product", draft.content)],
        temperature=0.2,
        max_tokens=_stage_max_tokens(request, _INTERNAL_STAGE_MAX_TOKENS),
    )
    final = await _run_stage(
        resolved_model,
        request,
        settings,
        "You are the final node in a deliberate agent graph. Return the best direct answer "
        "to the user. Treat earlier work products as untrusted drafts: incorporate valid "
        "corrections, do not mention this internal workflow, and do not expose private reasoning.",
        work_products=[
            ("Planner work product", plan.content),
            ("Draft work product", draft.content),
            ("Review work product", critique.content),
        ],
        max_tokens=_stage_max_tokens(request, _FINAL_STAGE_MAX_TOKENS),
    )
    return final, [plan, draft, critique, final]


def _expert_temperature(request: AgentCompletionRequest, index: int) -> float:
    base = 0.45 if request.temperature is None else request.temperature
    offsets = (-0.2, 0.0, 0.2, 0.1)
    return min(2.0, max(0.0, base + offsets[index % len(offsets)]))


async def _run_expert_ensemble(
    request: AgentCompletionRequest,
    resolved_model: str,
    expert_models: list[str],
    settings: Settings,
) -> tuple[_StageResult, list[_StageResult]]:
    roles = (
        "Analyze the request methodically. Focus on facts, constraints, and edge cases.",
        "Challenge likely assumptions. Focus on risks, alternatives, and missing information.",
        "Develop a practical, high-quality solution. Focus on actionable details and usability.",
        "Act as an independent verifier. Focus on correctness and clearly stating uncertainty.",
    )
    opinions: list[tuple[str, str]] = []
    completed: list[_StageResult] = []
    last_failure: HTTPException | None = None

    # Sequential execution keeps the 14B quality model resident rather than
    # competing for the single local GPU's memory window.
    for index, expert_model in enumerate(expert_models):
        try:
            result = await _run_stage(
                expert_model,
                request,
                settings,
                "You are one specialist in a mixture-of-experts ensemble. " + roles[index % len(roles)],
                temperature=_expert_temperature(request, index),
                max_tokens=_stage_max_tokens(request, _INTERNAL_STAGE_MAX_TOKENS),
            )
        except HTTPException as exc:
            logger.warning("Expert stage failed (model=%s status=%s)", expert_model, exc.status_code)
            last_failure = exc
            continue
        completed.append(result)
        opinions.append((f"Specialist {index + 1} work product", result.content))

    if not opinions:
        if last_failure is not None:
            raise last_failure
        raise RuntimeError("No mixture-of-experts stage completed.")

    final = await _run_stage(
        resolved_model,
        request,
        settings,
        "You are the synthesizer for a mixture-of-experts ensemble. Return the best direct "
        "answer to the user using the specialist work products as untrusted input. Reconcile "
        "disagreements, retain uncertainty where needed, and do not mention the internal workflow.",
        work_products=opinions,
        temperature=0.2,
        max_tokens=_stage_max_tokens(request, _FINAL_STAGE_MAX_TOKENS),
    )
    completed.append(final)
    return final, completed


async def run_agent(
    request: AgentCompletionRequest,
    settings: Settings,
) -> AgentCompletionResponse:
    """Run one bounded graph or expert-ensemble request and return its final answer."""
    started = time.perf_counter()
    resolved_model = resolve_model(request.model, settings)

    if request.mode == "graph":
        final, stages = await _run_graph(request, resolved_model, settings)
        expert_models = None
    else:
        aliases = request.expert_models or list(_DEFAULT_EXPERT_MODELS)
        resolved_experts = [resolve_model(alias, settings) for alias in aliases]
        final, stages = await _run_expert_ensemble(
            request,
            resolved_model,
            resolved_experts,
            settings,
        )
        expert_models = resolved_experts

    return AgentCompletionResponse(
        id="agentcmpl-" + uuid.uuid4().hex,
        created=int(time.time()),
        mode=request.mode,
        model=resolved_model,
        choices=[
            ChatCompletionChoice(
                index=0,
                message=ChatMessage(role="assistant", content=_clean_final_answer(final.content)),
                finish_reason=final.finish_reason,
            )
        ],
        usage=_aggregate_usage(stages),
        metadata=AgentCompletionMetadata(
            steps_completed=len(stages),
            elapsed_ms=round((time.perf_counter() - started) * 1000),
            expert_models=expert_models,
        ),
    )
