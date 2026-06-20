"""Ollama embedding client for RAG."""
from __future__ import annotations
import httpx
from .config import OLLAMA_BASE_URL, EMBED_MODEL


async def embed_texts(texts: list[str], model: str = EMBED_MODEL) -> list[list[float]]:
    """Return embeddings for a list of texts via Ollama /api/embed."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{OLLAMA_BASE_URL}/api/embed",
            json={"model": model, "input": texts},
        )
        resp.raise_for_status()
        data = resp.json()
        # Ollama returns {"embeddings": [[...], [...]]}
        return data["embeddings"]


async def embed_text(text: str, model: str = EMBED_MODEL) -> list[float]:
    results = await embed_texts([text], model)
    return results[0]
