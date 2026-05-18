"""FastAPI application entry point with middleware, lifespan, and route registration."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp

from . import client as ollama_client
from .config import settings
from .routes.chat import router as chat_router
from .routes.health import router as health_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

_HEALTH_PATHS = {"/health", "/health/ollama"}


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next: object):  # type: ignore[override]
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                length = int(content_length)
            except ValueError:
                length = 0
            if length > self.max_bytes:
                return JSONResponse(
                    status_code=413,
                    content={
                        "error": {
                            "message": (
                                f"Request body too large "
                                f"({length} bytes > {self.max_bytes} byte limit)."
                            ),
                            "type": "invalid_request_error",
                            "code": "request_too_large",
                        }
                    },
                )
        return await call_next(request)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: object):  # type: ignore[override]
        if not settings.enable_api_key_auth:
            return await call_next(request)

        if request.url.path in _HEALTH_PATHS:
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "message": "Missing or invalid Authorization header. Expected: Bearer <key>",
                        "type": "authentication_error",
                        "code": "invalid_api_key",
                    }
                },
            )

        token = auth_header[len("Bearer "):]
        if token != settings.api_key:
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "message": "Invalid API key.",
                        "type": "authentication_error",
                        "code": "invalid_api_key",
                    }
                },
            )

        return await call_next(request)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    ollama_client.init(settings)
    logger.info("Gateway started (host=%s port=%s)", settings.host, settings.port)
    yield
    await ollama_client.close()
    logger.info("Gateway stopped")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    app = FastAPI(
        title="Local AI API Gateway",
        description="Private OpenAI-compatible gateway for Ollama over Tailscale.",
        version="1.0.0",
        lifespan=lifespan,
    )

    # Middleware: registered in reverse execution order.
    # AuthMiddleware registered first → executes second (after body-size check).
    # BodySizeLimitMiddleware registered second → executes first (outermost).
    app.add_middleware(AuthMiddleware)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)

    # Custom HTTPException handler — when detail is already our {"error": {...}} dict,
    # pass it through directly instead of wrapping in {"detail": ...}.
    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> Response:
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            content = exc.detail
        else:
            content = {
                "error": {
                    "message": str(exc.detail),
                    "type": "api_error",
                    "code": "error",
                }
            }
        return JSONResponse(status_code=exc.status_code, content=content)

    # Custom validation error handler — emit OpenAI-compatible error envelope.
    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> Response:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "message": f"Request validation error: {exc.errors()}",
                    "type": "invalid_request_error",
                    "code": "invalid_request",
                }
            },
        )

    app.include_router(chat_router)
    app.include_router(health_router)

    return app


app = create_app()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "gateway.main:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )
