"""OpenAI-compatible embeddings backed by the private local Ollama instance."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .. import client as ollama_client
from ..config import settings
from ..models import EmbeddingData, EmbeddingRequest, EmbeddingResponse, EmbeddingUsage
from ..normalize import resolve_embedding_model

router = APIRouter()


@router.post("/v1/embeddings")
async def embeddings(request: EmbeddingRequest) -> JSONResponse:
    """Create local text embeddings using a separately gated model alias."""
    resolved_model = resolve_embedding_model(request.model, settings)
    inputs = [request.input] if isinstance(request.input, str) else request.input
    vectors = await ollama_client.proxy_embeddings(resolved_model, inputs, settings)
    response = EmbeddingResponse(
        data=[
            EmbeddingData(index=index, embedding=vector)
            for index, vector in enumerate(vectors)
        ],
        model=resolved_model,
        usage=EmbeddingUsage(),
    )
    return JSONResponse(response.model_dump())
