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


def _image_data_from_url(value: Any) -> str | None:
    if isinstance(value, dict):
        url = value.get("url")
    else:
        url = value
    if not isinstance(url, str):
        return None
    if url.startswith("data:") and ";base64," in url:
        return url.split(",", 1)[1]
    return None


def _content_for_ollama(content: Any) -> tuple[str, list[str]]:
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return str(content), []

    text_parts: list[str] = []
    images: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type == "text":
            text_parts.append(str(part.get("text", "")))
        elif part_type == "image_url":
            image_data = _image_data_from_url(part.get("image_url"))
            if image_data:
                images.append(image_data)
        elif isinstance(part.get("text"), str):
            text_parts.append(part["text"])

    return "\n".join(text for text in text_parts if text), images


def _messages_for_ollama(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ollama_messages: list[dict[str, Any]] = []
    for message in messages:
        content, images = _content_for_ollama(message.get("content"))
        ollama_message: dict[str, Any] = {
            "role": message["role"],
            "content": content,
        }
        if images:
            ollama_message["images"] = images
        for key in ("name", "tool_call_id", "tool_calls"):
            if message.get(key) is not None:
                ollama_message[key] = message[key]
        ollama_messages.append(ollama_message)
    return ollama_messages


def _format_for_ollama(response_format: dict[str, Any] | None) -> str | dict[str, Any] | None:
    if response_format is None:
        return None
    format_type = response_format.get("type")
    if format_type == "json_object":
        return "json"
    if format_type == "json_schema":
        json_schema = response_format.get("json_schema")
        if isinstance(json_schema, dict) and isinstance(json_schema.get("schema"), dict):
            return json_schema["schema"]
        return "json"
    return None


def _stream_include_usage(stream_options: dict[str, Any] | None) -> bool:
    return bool(stream_options and stream_options.get("include_usage"))


def _build_ollama_body(
    resolved_model: str,
    request_dict: dict[str, Any],
    stream: bool,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": resolved_model,
        "messages": _messages_for_ollama(request_dict["messages"]),
        "stream": stream,
        "think": False,
    }

    tools = request_dict.get("tools")
    if tools and request_dict.get("tool_choice") != "none":
        body["tools"] = tools

    response_format = _format_for_ollama(request_dict.get("response_format"))
    if response_format is not None:
        body["format"] = response_format

    options: dict[str, Any] = {}
    if request_dict.get("temperature") is not None:
        options["temperature"] = request_dict["temperature"]
    if request_dict.get("top_p") is not None:
        options["top_p"] = request_dict["top_p"]
    if request_dict.get("max_tokens") is not None:
        options["num_predict"] = request_dict["max_tokens"]
    if request_dict.get("stop") is not None:
        stop = request_dict["stop"]
        options["stop"] = [stop] if isinstance(stop, str) else stop
    if request_dict.get("seed") is not None:
        options["seed"] = request_dict["seed"]
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


async def _raise_for_ollama_stream_error(response: httpx.Response) -> None:
    if response.status_code >= 400:
        await response.aread()
        _raise_for_ollama_error(response)


def _ollama_timeout_exception(message: str) -> HTTPException:
    return HTTPException(
        status_code=504,
        detail={
            "error": {
                "message": message,
                "type": "upstream_error",
                "code": "ollama_timeout",
            }
        },
    )


def _ollama_connect_exception(message: str) -> HTTPException:
    return HTTPException(
        status_code=502,
        detail={
            "error": {
                "message": message,
                "type": "upstream_error",
                "code": "ollama_error",
            }
        },
    )


def _ollama_invalid_response_exception(message: str) -> HTTPException:
    return HTTPException(
        status_code=502,
        detail={
            "error": {
                "message": message,
                "type": "upstream_error",
                "code": "ollama_invalid_response",
            }
        },
    )


def _make_completion_id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex


def _finish_reason_from_ollama(data: dict[str, Any]) -> str:
    done_reason = str(data.get("done_reason") or "").strip().lower()
    if done_reason == "length":
        return "length"
    return "stop"


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
        raise _ollama_timeout_exception("Request to Ollama timed out.") from exc
    except httpx.ConnectError as exc:
        logger.warning("Could not connect to Ollama: %s", exc)
        raise _ollama_connect_exception("Could not connect to Ollama.") from exc

    _raise_for_ollama_error(response)

    try:
        data = response.json()
    except ValueError as exc:
        logger.warning("Ollama returned invalid JSON: %s", exc)
        raise _ollama_invalid_response_exception("Ollama returned invalid JSON.") from exc
    msg = data.get("message", {})
    msg = msg if isinstance(msg, dict) else {}
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
                    content=msg.get("content") or "",
                    tool_calls=msg.get("tool_calls"),
                    tool_call_id=msg.get("tool_call_id"),
                    name=msg.get("name"),
                ),
                finish_reason=_finish_reason_from_ollama(data),
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
    """Open an Ollama stream and return an OpenAI-compatible SSE generator."""
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

    def _usage_chunk(data: dict[str, Any]) -> str:
        prompt_tokens = data.get("prompt_eval_count", 0) or 0
        completion_tokens = data.get("eval_count", 0) or 0
        chunk = ChatCompletionChunk(
            id=completion_id,
            created=created,
            model=resolved_model,
            choices=[],
            usage=ChatCompletionUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
        )
        return f"data: {chunk.model_dump_json(exclude_none=True)}\n\n"

    def _error_event(message: str, code: str) -> str:
        payload = {
            "error": {
                "message": message,
                "type": "upstream_error",
                "code": code,
            }
        }
        return f"event: error\ndata: {json.dumps(payload)}\n\n"

    logger.info("Sending streaming request to Ollama (model=%s)", resolved_model)

    stream_context = None
    try:
        stream_context = client.stream("POST", "/api/chat", json=body)
        response = await stream_context.__aenter__()
        await _raise_for_ollama_stream_error(response)
    except httpx.TimeoutException as exc:
        logger.warning("Ollama stream timed out: %s", exc)
        raise _ollama_timeout_exception("Request to Ollama timed out.") from exc
    except httpx.ConnectError as exc:
        logger.warning("Could not connect to Ollama for stream: %s", exc)
        raise _ollama_connect_exception("Could not connect to Ollama.") from exc
    except Exception:
        if stream_context is not None:
            await stream_context.__aexit__(None, None, None)
        raise

    async def generate() -> AsyncGenerator[str, None]:
        try:
            # First chunk carries the role
            yield _chunk({"role": "assistant"}, finish_reason=None)

            completed = False
            include_usage = _stream_include_usage(request_dict.get("stream_options"))
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
                message = ollama_chunk.get("message", {})
                message = message if isinstance(message, dict) else {}
                content = message.get("content", "")
                tool_calls = message.get("tool_calls")

                if completed:
                    continue
                if not done:
                    delta: dict[str, Any] = {}
                    if content:
                        delta["content"] = content
                    if tool_calls:
                        delta["tool_calls"] = tool_calls
                    if delta:
                        yield _chunk(delta, finish_reason=None)
                else:
                    yield _chunk({}, finish_reason=_finish_reason_from_ollama(ollama_chunk))
                    if include_usage:
                        yield _usage_chunk(ollama_chunk)
                    yield "data: [DONE]\n\n"
                    completed = True

            if not completed:
                yield _error_event("Ollama stream ended before completion.", "ollama_invalid_response")
        except httpx.TimeoutException as exc:
            logger.warning("Ollama stream timed out after response started: %s", exc)
            yield _error_event("Ollama stream timed out.", "ollama_timeout")
        except httpx.ConnectError as exc:
            logger.warning("Ollama stream connection failed after response started: %s", exc)
            yield _error_event("Ollama stream connection failed.", "ollama_error")
        finally:
            await stream_context.__aexit__(None, None, None)

    return generate()
