import logging

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

_HEALTH_TIMEOUT = 5.0


def _health_error(message: str, code: str) -> dict:
    return {
        "error": {
            "message": message,
            "type": "upstream_error",
            "code": code,
        }
    }


@router.get("/health")
async def health() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "gateway": "local-ai-api",
            "ollama_base_url": settings.ollama_base_url,
        }
    )


@router.get("/health/ollama")
async def health_ollama() -> JSONResponse:
    try:
        async with httpx.AsyncClient(timeout=_HEALTH_TIMEOUT) as client:
            response = await client.get(settings.ollama_base_url + "/api/tags")
        if response.status_code < 400:
            return JSONResponse({"status": "ok"})
        return JSONResponse(
            _health_error("Ollama returned HTTP " + str(response.status_code), "ollama_error"),
            status_code=502,
        )
    except httpx.ConnectError as exc:
        logger.warning("Ollama health check connect error: %s", exc)
        return JSONResponse(
            _health_error("Could not connect to Ollama", "ollama_error"),
            status_code=502,
        )
    except httpx.TimeoutException as exc:
        logger.warning("Ollama health check timed out: %s", exc)
        return JSONResponse(
            _health_error("Ollama health check timed out", "ollama_timeout"),
            status_code=502,
        )


@router.get("/health/qdrant")
async def health_qdrant() -> JSONResponse:
    """Check Qdrant vector store reachability. Requires RAG_ENABLED=true."""
    # Lazy import so the gateway starts without qdrant-client installed.
    try:
        from ..rag.config import RAG_ENABLED, QDRANT_URL
        from ..rag.store import qdrant_healthy
    except ImportError:
        return JSONResponse(
            _health_error("qdrant-client is not installed", "qdrant_not_installed"),
            status_code=503,
        )

    if not RAG_ENABLED:
        return JSONResponse({"status": "disabled", "message": "RAG_ENABLED is false"})

    healthy = await qdrant_healthy()
    if healthy:
        return JSONResponse({"status": "ok", "qdrant_url": QDRANT_URL})
    return JSONResponse(
        _health_error("Could not connect to Qdrant", "qdrant_error"),
        status_code=502,
    )
