"""
FastMCP server mounted inside the Local AI API gateway.

Exposes local models as MCP tools. Mount at /mcp in main.py:
    app.mount("/mcp", mcp.get_asgi_app())

Claude Code config (~/.claude/settings.json):
    {
      "mcpServers": {
        "local-ai-api": {
          "type": "http",
          "url": "https://<your-machine>.ts.net/mcp/"
        }
      }
    }
"""
from __future__ import annotations

import base64
from typing import Annotated

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from ..config import settings

mcp = FastMCP(
    name="local-ai-api",
    instructions=(
        "Tools for interacting with locally-hosted AI models via the Local AI API gateway. "
        "Chat models run on Ollama. Audio transcription uses Whisper. "
        "Text-to-speech uses Chatterbox. All data stays on the local machine."
    ),
)

# Internal gateway URL — same process, so this is a loopback call
_TIMEOUT = 120.0


# ── helpers ───────────────────────────────────────────────────────────────────

def _client() -> httpx.AsyncClient:
    """Create an authenticated loopback client for this gateway instance."""
    headers: dict[str, str] = {}
    if settings.enable_api_key_auth:
        headers["Authorization"] = f"Bearer {settings.api_key}"
    return httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{settings.port}",
        timeout=_TIMEOUT,
        headers=headers,
    )


# ── tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
async def chat(
    message: Annotated[str, Field(description="The message to send to the language model.")],
    model: Annotated[str, Field(description="Model alias: main (9B), small (4B), dev (0.8B), agent (14B), agent-utility (8B). Default: main.")] = "main",
    system: Annotated[str | None, Field(description="Optional system prompt.")] = None,
    temperature: Annotated[float, Field(description="Sampling temperature 0.0-1.0.", ge=0.0, le=1.0)] = 0.7,
    max_tokens: Annotated[int, Field(description="Maximum tokens to generate.", ge=1, le=8192)] = 1024,
) -> str:
    """Send a message to a locally-hosted language model and return the response."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": message})

    async with _client() as c:
        try:
            resp = await c.post("/v1/chat/completions", json={
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": False,
            })
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ToolError(f"LLM request failed ({exc.response.status_code}): {exc.response.text}")
        except httpx.RequestError as exc:
            raise ToolError(f"Could not reach gateway: {exc}")

    data = resp.json()
    return data["choices"][0]["message"]["content"]


@mcp.tool()
async def list_models() -> list[dict]:
    """List all available model aliases and their Ollama tags."""
    async with _client() as c:
        try:
            resp = await c.get("/v1/models")
            resp.raise_for_status()
        except Exception as exc:
            raise ToolError(f"Could not fetch models: {exc}")
    return resp.json().get("data", [])


@mcp.tool()
async def transcribe(
    audio_base64: Annotated[str, Field(description="Base64-encoded WAV or MP3 audio data.")],
    model: Annotated[str, Field(description="Whisper model: tiny, base, or small. Default: small.")] = "small",
) -> str:
    """Transcribe audio to text using local Whisper. Audio must be base64-encoded WAV."""
    try:
        audio_bytes = base64.b64decode(audio_base64, validate=True)
    except Exception as exc:
        raise ToolError(f"Invalid base64 audio data: {exc}")

    async with _client() as c:
        try:
            resp = await c.post(
                "/v1/audio/transcriptions",
                files={"file": ("audio.wav", audio_bytes, "audio/wav")},
                data={"model": model},
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ToolError(f"Transcription failed ({exc.response.status_code}): {exc.response.text}")
        except httpx.RequestError as exc:
            raise ToolError(f"Could not reach gateway: {exc}")

    return resp.json().get("text", "")


@mcp.tool()
async def speak(
    text: Annotated[str, Field(description="Text to convert to speech.")],
    model: Annotated[str, Field(description="TTS model: chatterbox or chatterbox-multilingual.")] = "chatterbox",
) -> str:
    """
    Convert text to speech using local Chatterbox TTS.

    Returns base64-encoded WAV audio because MCP tool results are text.
    Decode the result with base64.b64decode() to obtain the raw WAV bytes.
    """
    if not text.strip():
        raise ToolError("Text must not be empty.")

    async with _client() as c:
        try:
            resp = await c.post("/v1/audio/speech", json={
                "model": model,
                "input": text,
                "response_format": "wav",
            })
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ToolError(f"TTS failed ({exc.response.status_code}): {exc.response.text}")
        except httpx.RequestError as exc:
            raise ToolError(f"Could not reach gateway: {exc}")

    return base64.b64encode(resp.content).decode()


@mcp.tool()
async def health_check() -> dict:
    """Check the health of the gateway, Ollama, and available services."""
    async with _client() as c:
        try:
            gw = await c.get("/health")
            ollama = await c.get("/health/ollama")
        except Exception as exc:
            raise ToolError(f"Health check failed: {exc}")

    return {
        "gateway": gw.json() if gw.is_success else {"error": gw.text},
        "ollama": ollama.json() if ollama.is_success else {"error": ollama.text},
    }


@mcp.tool()
async def search_documents(
    query: Annotated[str, Field(description="Search query to find relevant document chunks.")],
    top_k: Annotated[int, Field(description="Number of results to return.", ge=1, le=20)] = 4,
) -> list[dict]:
    """
    Search indexed documents using semantic similarity (requires RAG_ENABLED=true).

    Returns relevant text chunks with their source filenames and similarity scores.
    If RAG is not enabled, this tool raises a ToolError explaining how to enable it.
    """
    async with _client() as c:
        try:
            resp = await c.post("/v1/search", json={"query": query, "top_k": top_k})
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 503:
                raise ToolError(
                    "Document search requires RAG_ENABLED=true in the gateway configuration."
                )
            raise ToolError(f"Search failed ({exc.response.status_code}): {exc.response.text}")
        except httpx.RequestError as exc:
            raise ToolError(f"Could not reach gateway: {exc}")

    return resp.json().get("results", [])
