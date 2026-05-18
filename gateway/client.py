"""Async httpx client for Ollama with OpenAI-compatible response translation."""
from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from fastapi import HTTPException

from .config import Settings
from .models import (
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionChunkDelta,
    ChatCompletionChoice,
    ChatCompletionResponse,
    ChatCompletionUsage,
    ChatMessage,
)

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


def init(settings: Settings) -> None:
    global _client
    _client = httpx.AsyncClient(
        base_url=settings.ollama_base_url,
        timeout=httpx.Timeout(settings.request_timeout_seconds),
    )
    logger.info("Ollama httpx client initialised (base_url=%s)", settings.ollama_base_url)


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
        logger.info("Ollama httpx client closed")


def _get_client() -> httpx.AsyncClient:
    if _client is None:
        raise RuntimeError("Ollama client not initialised — app startup not complete")
    return _client


def _build_ollama_body(
    resolved_model: str,
    request_dict: dict[str, Any],
    stream: bool,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": resolved_model,
        "messages": request_dict["messages"],
        "stream": stream,
    }

    options: dict[str, Any] = {}
    if request_dict.get("temperature") is not None:
        options["temperature"] = request_dict["temperature"]
    if request_dict.get("top_p") is not None:
        options["top_p"] = request_dict["top_p"]
    if request_dict.get("max_tokens") is not None:
        options["num_predict"] = request_dict["max_tokens"]
    if options:
        body["options"] = options

    return body


def _raise_for_ollama_error(response: httpx.Response) -> None:
    if response.status_code >= 400:
        try:
            detail = response.json()
        except Exception:
            detail = response.text
        raise HTTPException(
            status_code=502,
            detail={
                "error": {
                    "message": f"Ollama returned HTTP {response.status_code}: {detail}",
                    "type": "upstream_error",
                    "code": "ollama_error",
                }
            },
        )


def _make_completion_id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex


async def proxy_non_streaming(
    resolved_model: str,
    request_dict: dict[str, Any],
    settings: Settings,
) -> ChatCompletionResponse:
    client = _get_client()
    body = _build_ollama_body(resolved_model, request_dict, stream=False)

    logger.info("Sending non-streaming request to Ollama (model=%s)", resolved_model)

    try:
        response = await client.post("/api/chat", json=body)
    except httpx.TimeoutException as exc:
        logger.warning("Ollama request timed out: %s", exc)
        raise HTTPException(
            status_code=504,
            detail={
                "error": {
                    "message": "Request to Ollama timed out.",
                    "type": "upstream_error",
                    "code": "ollama_timeout",
                }
            },
        ) from exc
    except httpx.ConnectError as exc:
        logger.warning("Could not connect to Ollama: %s", exc)
        raise HTTPException(
            status_code=502,
            detail={
                "error": {
                    "message": "Could not connect to Ollama.",
                    "type": "upstream_error",
                    "code": "ollama_error",
                }
            },
        ) from exc

    _raise_for_ollama_error(response)

    data = response.json()
    msg = data.get("message", {})
    prompt_tokens = data.get("prompt_eval_count", 0) or 0
    completion_tokens = data.get("eval_count", 0) or 0

    return ChatCompletionResponse(
        id=_make_completion_id(),
        created=int(time.time()),
        model=resolved_model,
        choices=[
            ChatCompletionChoice(
                index=0,
                message=ChatMessage(
                    role=msg.get("role", "assistant"),
                    content=msg.get("content", ""),
                ),
                finish_reason="stop",
            )
        ],
        usage=ChatCompletionUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )


async def proxy_streaming(
    resolved_model: str,
    request_dict: dict[str, Any],
    settings: Settings,
) -> AsyncGenerator[str, None]:
    """Yield SSE lines translating Ollama's NDJSON stream to OpenAI SSE format."""
    client = _get_client()
    body = _build_ollama_body(resolved_model, request_dict, stream=True)

    completion_id = _make_completion_id()
    created = int(time.time())

    def _chunk(delta: dict[str, Any], finish_reason: str | None) -> str:
        chunk = ChatCompletionChunk(
            id=completion_id,
            created=created,
            model=resolved_model,
            choices=[
                ChatCompletionChunkChoice(
                    index=0,
                    delta=ChatCompletionChunkDelta(**delta),
                    finish_reason=finish_reason,
                )
            ],
        )
        return f"data: {chunk.model_dump_json(exclude_none=True)}\n\n"

    logger.info("Sending streaming request to Ollama (model=%s)", resolved_model)

    try:
        async with client.stream("POST", "/api/chat", json=body) as response:
            _raise_for_ollama_error(response)

            # First chunk carries the role
            yield _chunk({"role": "assistant"}, finish_reason=None)

            async for raw_line in response.aiter_lines():
                raw_line = raw_line.strip()
                if not raw_line:
                    continue

                try:
                    ollama_chunk = json.loads(raw_line)
                except json.JSONDecodeError:
                    logger.warning("Unparseable Ollama stream line: %r", raw_line)
                    continue

                done = ollama_chunk.get("done", False)
                content = ollama_chunk.get("message", {}).get("content", "")

                if not done:
                    yield _chunk({"content": content}, finish_reason=None)
                else:
                    yield _chunk({}, finish_reason="stop")
                    yield "data: [DONE]\n\n"
                    return

    except httpx.TimeoutException as exc:
        logger.warning("Ollama stream timed out: %s", exc)
        # Stream may have already started; emit [DONE] and bail
        yield "data: [DONE]\n\n"
    except httpx.ConnectError as exc:
        logger.warning("Could not connect to Ollama for stream: %s", exc)
        yield "data: [DONE]\n\n"
