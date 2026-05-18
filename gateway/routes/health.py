import logging

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

_HEALTH_TIMEOUT = 5.0


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
            response = await client.get(f"{settings.ollama_base_url}/api/tags")
        if response.status_code < 400:
            return JSONResponse({"status": "ok"})
        return JSONResponse(
            {"status": "error", "detail": f"Ollama returned HTTP {response.status_code}"},
            status_code=502,
        )
    except httpx.ConnectError as exc:
        logger.warning("Ollama health check connect error: %s", exc)
        return JSONResponse(
            {"status": "error", "detail": "Could not connect to Ollama"},
            status_code=502,
        )
    except httpx.TimeoutException as exc:
        logger.warning("Ollama health check timed out: %s", exc)
        return JSONResponse(
            {"status": "error", "detail": "Ollama health check timed out"},
            status_code=502,
        )
