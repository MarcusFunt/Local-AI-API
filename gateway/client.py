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
    MAX_CHAT_TEXT_CHARS,
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionChunkDelta,
    ChatCompletionChoice,
    ChatCompletionResponse,
    ChatCompletionUsage,
    ChatMessage,
    validate_base64_image_url,
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


def _invalid_content_exception(message: str) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "code": "invalid_chat_content",
            }
        },
    )


def _image_data_from_url(value: Any) -> str:
    try:
        return validate_base64_image_url(value)
    except ValueError as exc:
        raise _invalid_content_exception(str(exc)) from exc


def _content_for_ollama(content: Any) -> tuple[str, list[str]]:
    if content is None:
        return "", []
    if isinstance(content, str):
        if len(content) > MAX_CHAT_TEXT_CHARS:
            raise _invalid_content_exception(f"Chat message content must not exceed {MAX_CHAT_TEXT_CHARS} characters.")
        return content, []
    if not isinstance(content, list):
        raise _invalid_content_exception("Chat message content must be text or a supported content-part list.")

    text_parts: list[str] = []
    images: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            raise _invalid_content_exception("Each content part must be an object.")
        part_type = part.get("type")
        if part_type == "text":
            text = part.get("text")
            if not isinstance(text, str):
                raise _invalid_content_exception("Text content parts must include text.")
            if len(text) > MAX_CHAT_TEXT_CHARS:
                raise _invalid_content_exception(f"Text content parts must not exceed {MAX_CHAT_TEXT_CHARS} characters.")
            text_parts.append(text)
        elif part_type == "image_url":
            images.append(_image_data_from_url(part.get("image_url")))
        else:
            raise _invalid_content_exception("Unsupported content part type.")

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


def _ollama_error_message(detail: Any) -> str:
    if isinstance(detail, dict):
        err = detail.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)
        if err is not None:
            return str(err)
    return str(detail)


def _is_out_of_memory(message: str) -> bool:
    lowered = message.lower()
    return "more system memory" in lowered or ("requires more" in lowered and "memory" in lowered)


def _raise_for_ollama_error(response: httpx.Response) -> None:
    if response.status_code >= 400:
        try:
            detail = response.json()
        except Exception:
            detail = response.text
        message = _ollama_error_message(detail)
        if _is_out_of_memory(message):
            raise HTTPException(
                status_code=507,
                detail={
                    "error": {
                        "message": (
                            f"Not enough memory to load this model: {message}. "
                            "Choose a smaller model (for example the 'small' or 'dev' profile), "
                            "or reinstall with Low Compute Mode."
                        ),
                        "type": "insufficient_memory",
                        "code": "insufficient_memory",
                    }
                },
            )
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
    if not isinstance(data, dict):
        logger.warning("Ollama returned a non-object JSON response: %s", type(data).__name__)
        raise _ollama_invalid_response_exception("Ollama returned a JSON response that was not an object.")
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


async def proxy_embeddings(
    resolved_model: str,
    inputs: list[str],
    settings: Settings,
) -> list[list[float]]:
    """Return vectors from Ollama's local embedding endpoint."""
    client = _get_client()
    logger.info("Sending embedding request to Ollama (model=%s inputs=%d)", resolved_model, len(inputs))
    try:
        response = await client.post(
            "/api/embed",
            json={"model": resolved_model, "input": inputs},
        )
    except httpx.TimeoutException as exc:
        logger.warning("Ollama embedding request timed out: %s", exc)
        raise _ollama_timeout_exception("Embedding request to Ollama timed out.") from exc
    except httpx.ConnectError as exc:
        logger.warning("Could not connect to Ollama for embeddings: %s", exc)
        raise _ollama_connect_exception("Could not connect to Ollama for embeddings.") from exc

    _raise_for_ollama_error(response)
    try:
        data = response.json()
        vectors = data.get("embeddings") if isinstance(data, dict) else None
        if not isinstance(vectors, list) or len(vectors) != len(inputs):
            raise ValueError("Unexpected embedding count.")
        normalized = [
            [float(value) for value in vector]
            for vector in vectors
            if isinstance(vector, list) and vector
        ]
    except (TypeError, ValueError) as exc:
        raise _ollama_invalid_response_exception(
            "Ollama returned an invalid embedding response."
        ) from exc
    if len(normalized) != len(inputs):
        raise _ollama_invalid_response_exception("Ollama returned an invalid embedding vector.")
    return normalized


async def proxy_streaming(
    resolved_model: str,
    request_dict: dict[str, Any],
    settings: Settings,
) -> AsyncGenerator[str, None]:
    """Open an Ollama stream and return an OpenAI-compatible SSE generator."""
    events = await proxy_streaming_events(resolved_model, request_dict, settings)

    completion_id = _make_completion_id()
    created = int(time.time())
    include_usage = _stream_include_usage(request_dict.get("stream_options"))

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

    def _usage_chunk(usage: dict[str, int]) -> str:
        chunk = ChatCompletionChunk(
            id=completion_id,
            created=created,
            model=resolved_model,
            choices=[],
            usage=ChatCompletionUsage(
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
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

    async def generate() -> AsyncGenerator[str, None]:
        async for event in events:
            event_type = event.get("type")
            if event_type == "delta":
                yield _chunk(event.get("delta", {}), finish_reason=None)
            elif event_type == "done":
                yield _chunk({}, finish_reason=event.get("finish_reason", "stop"))
                if include_usage:
                    yield _usage_chunk(event.get("usage", {}))
                yield "data: [DONE]\n\n"
            elif event_type == "error":
                error = event.get("error", {})
                yield _error_event(
                    str(error.get("message", "Ollama stream failed.")),
                    str(error.get("code", "ollama_error")),
                )

    return generate()


async def proxy_streaming_events(
    resolved_model: str,
    request_dict: dict[str, Any],
    settings: Settings,
) -> AsyncGenerator[dict[str, Any], None]:
    """Open an Ollama stream and return structured OpenAI-style stream events."""
    client = _get_client()
    body = _build_ollama_body(resolved_model, request_dict, stream=True)

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

    line_iterator = response.aiter_lines()
    try:
        while True:
            raw_line = (await anext(line_iterator)).strip()
            if not raw_line:
                continue
            try:
                first_chunk = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise _ollama_invalid_response_exception("Ollama stream returned invalid JSON.") from exc
            if not isinstance(first_chunk, dict):
                raise _ollama_invalid_response_exception(
                    "Ollama stream returned a JSON value that was not an object."
                )
            break
    except StopAsyncIteration as exc:
        await stream_context.__aexit__(None, None, None)
        raise _ollama_invalid_response_exception("Ollama stream ended before returning a response.") from exc
    except httpx.TimeoutException as exc:
        await stream_context.__aexit__(None, None, None)
        raise _ollama_timeout_exception("Request to Ollama timed out.") from exc
    except Exception:
        await stream_context.__aexit__(None, None, None)
        raise

    async def generate() -> AsyncGenerator[dict[str, Any], None]:
        try:
            # First chunk carries the role
            yield {"type": "delta", "delta": {"role": "assistant"}}

            completed = False
            pending_chunks = [first_chunk]
            while pending_chunks or not completed:
                if pending_chunks:
                    ollama_chunk = pending_chunks.pop()
                else:
                    try:
                        raw_line = (await anext(line_iterator)).strip()
                    except StopAsyncIteration:
                        break
                    if not raw_line:
                        continue
                    try:
                        ollama_chunk = json.loads(raw_line)
                    except json.JSONDecodeError:
                        logger.warning("Unparseable Ollama stream line: %r", raw_line)
                        continue
                    if not isinstance(ollama_chunk, dict):
                        logger.warning("Ollama stream line was not a JSON object: %r", raw_line)
                        yield {
                            "type": "error",
                            "error": {
                                "message": "Ollama stream returned a JSON value that was not an object.",
                                "type": "upstream_error",
                                "code": "ollama_invalid_response",
                            },
                        }
                        return

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
                        yield {"type": "delta", "delta": delta}
                else:
                    prompt_tokens = ollama_chunk.get("prompt_eval_count", 0) or 0
                    completion_tokens = ollama_chunk.get("eval_count", 0) or 0
                    yield {
                        "type": "done",
                        "finish_reason": _finish_reason_from_ollama(ollama_chunk),
                        "usage": {
                            "prompt_tokens": prompt_tokens,
                            "completion_tokens": completion_tokens,
                            "total_tokens": prompt_tokens + completion_tokens,
                        },
                    }
                    completed = True

            if not completed:
                yield {
                    "type": "error",
                    "error": {
                        "message": "Ollama stream ended before completion.",
                        "type": "upstream_error",
                        "code": "ollama_invalid_response",
                    },
                }
        except httpx.TimeoutException as exc:
            logger.warning("Ollama stream timed out after response started: %s", exc)
            yield {
                "type": "error",
                "error": {
                    "message": "Ollama stream timed out.",
                    "type": "upstream_error",
                    "code": "ollama_timeout",
                },
            }
        except httpx.ConnectError as exc:
            logger.warning("Ollama stream connection failed after response started: %s", exc)
            yield {
                "type": "error",
                "error": {
                    "message": "Ollama stream connection failed.",
                    "type": "upstream_error",
                    "code": "ollama_error",
                },
            }
        finally:
            await stream_context.__aexit__(None, None, None)

    return generate()
