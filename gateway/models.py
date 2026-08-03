from __future__ import annotations

import base64
import binascii
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_CHAT_ROLES = {"system", "user", "assistant", "tool", "developer"}
_RESPONSE_FORMAT_TYPES = {"text", "json_object", "json_schema"}
_SUPPORTED_CONTENT_PART_TYPES = {"text", "image_url"}
MAX_CHAT_TEXT_CHARS = 100_000
MAX_SPEECH_TEXT_CHARS = 10_000


def validate_base64_image_url(value: Any) -> str:
    """Return validated image data, accepting only local base64 data URLs."""
    url = value.get("url") if isinstance(value, dict) else value
    if not isinstance(url, str):
        raise ValueError("Image content parts must include an image_url.url string.")
    metadata, separator, encoded = url.partition(",")
    if (
        not separator
        or not metadata.lower().startswith("data:image/")
        or not metadata.lower().endswith(";base64")
        or not encoded
    ):
        raise ValueError("Image content parts must use a non-empty base64 data:image URL.")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Image content part contains invalid base64 data.") from exc
    if not decoded:
        raise ValueError("Image content part must contain non-empty image data.")
    return encoded


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        value = value.strip()
        if value not in _CHAT_ROLES:
            allowed = ", ".join(sorted(_CHAT_ROLES))
            raise ValueError("Unsupported chat message role. Allowed roles: " + allowed + ".")
        return value

    @field_validator("content")
    @classmethod
    def validate_content(
        cls, value: str | list[dict[str, Any]] | None
    ) -> str | list[dict[str, Any]] | None:
        if value is None or isinstance(value, str):
            if isinstance(value, str) and len(value) > MAX_CHAT_TEXT_CHARS:
                raise ValueError(f"Chat message content must not exceed {MAX_CHAT_TEXT_CHARS} characters.")
            return value
        if not value:
            raise ValueError("Content part list must not be empty.")
        for part in value:
            if not isinstance(part, dict):
                raise ValueError("Each content part must be an object.")
            part_type = part.get("type")
            if not isinstance(part_type, str) or not part_type:
                raise ValueError("Each content part must include a non-empty type.")
            if part_type not in _SUPPORTED_CONTENT_PART_TYPES:
                allowed = ", ".join(sorted(_SUPPORTED_CONTENT_PART_TYPES))
                raise ValueError(f"Unsupported content part type {part_type!r}. Allowed types: {allowed}.")
            if part_type == "text" and not isinstance(part.get("text"), str):
                raise ValueError("Text content parts must include text.")
            if part_type == "text" and len(part["text"]) > MAX_CHAT_TEXT_CHARS:
                raise ValueError(f"Text content parts must not exceed {MAX_CHAT_TEXT_CHARS} characters.")
            if part_type == "image_url":
                validate_base64_image_url(part.get("image_url"))
        return value

    @model_validator(mode="after")
    def validate_message_payload(self) -> "ChatMessage":
        if self.role != "assistant" and self.content is None:
            raise ValueError("Non-assistant messages must include content.")
        if self.role == "assistant" and self.content is None and not self.tool_calls:
            raise ValueError("Assistant messages must include content or tool_calls.")
        return self


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, gt=0)
    max_completion_tokens: int | None = Field(default=None, gt=0)
    stop: str | list[str] | None = None
    seed: int | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    response_format: dict[str, Any] | None = None
    stream_options: dict[str, Any] | None = None
    user: str | None = None
    n: int | None = Field(default=None, gt=0)
    # RAG extension: set true to retrieve context chunks from the vector store
    # before forwarding to Ollama. Ignored when RAG_ENABLED=false.
    use_rag: bool = False

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("Model must not be empty.")
        return value

    @field_validator("stop")
    @classmethod
    def validate_stop(cls, value: str | list[str] | None) -> str | list[str] | None:
        if value is None:
            return value
        if isinstance(value, str):
            if not value:
                raise ValueError("Stop sequence must not be empty.")
            return value
        if not value:
            raise ValueError("Stop sequence list must not be empty.")
        if any(not item for item in value):
            raise ValueError("Stop sequence list must not contain empty strings.")
        return value

    @model_validator(mode="after")
    def validate_compatibility_options(self) -> "ChatCompletionRequest":
        if self.max_tokens is not None and self.max_completion_tokens is not None:
            raise ValueError("Use either max_tokens or max_completion_tokens, not both.")
        if self.n is not None and self.n != 1:
            raise ValueError("Only n=1 is supported.")
        if self.response_format is not None:
            format_type = self.response_format.get("type")
            if format_type not in _RESPONSE_FORMAT_TYPES:
                allowed = ", ".join(sorted(_RESPONSE_FORMAT_TYPES))
                raise ValueError(
                    "Unsupported response_format type. Allowed types: " + allowed + "."
                )
        return self


class ChatCompletionUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: str


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: ChatCompletionUsage


class ChatCompletionChunkDelta(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: str | None = None
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class ChatCompletionChunkChoice(BaseModel):
    index: int
    delta: ChatCompletionChunkDelta
    finish_reason: str | None = None


class ChatCompletionChunk(BaseModel):
    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: list[ChatCompletionChunkChoice]
    usage: ChatCompletionUsage | None = None


class AudioSpeechRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    input: str = Field(min_length=1, max_length=MAX_SPEECH_TEXT_CHARS)
    voice: str | None = None
    response_format: str = "wav"
    speed: float | None = None
    language: str | None = None
    exaggeration: float | None = None
    cfg_weight: float | None = None


class ConversationSessionStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["session.start"]
    model: str | None = None
    whisper_model: str | None = None
    tts_model: str | None = None
    input_audio_format: str = "wav"
    language: str | None = None
    instructions: str | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=512, gt=0)
    seed: int | None = None
    use_rag: bool = False
    max_history_messages: int = Field(default=20, ge=2, le=100)
    exaggeration: float | None = None
    cfg_weight: float | None = None

    @field_validator("model", "whisper_model", "tts_model", "language")
    @classmethod
    def strip_optional_strings(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        return value or None

    @field_validator("instructions")
    @classmethod
    def strip_instructions(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        return value or None

    @field_validator("input_audio_format")
    @classmethod
    def validate_input_audio_format(cls, value: str | None) -> str:
        normalized = (value or "wav").strip().lower()
        if normalized not in {"wav", "webm"}:
            raise ValueError("Unsupported input_audio_format. Use wav or webm.")
        return normalized
