"""FastAPI application entry point with middleware, lifespan, and route registration."""
from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from hmac import compare_digest

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import client as ollama_client
from .config import settings
from .routes.agents import router as agents_router
from .routes.audio import router as audio_router
from .routes.chat import router as chat_router
from .routes.conversation import router as conversation_router
from .routes.documents import router as documents_router
from .routes.embeddings import router as embeddings_router
from .routes.health import router as health_router
from .routes.models import router as models_router
from .routes.status import router as status_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

_HEALTH_PATHS = {"/health", "/health/ollama", "/health/qdrant"}
_PUBLIC_BROWSER_PATHS = {"/live-call"}


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


def request_too_large_response(actual_bytes: int, max_bytes: int) -> JSONResponse:
    return JSONResponse(
        status_code=413,
        content={
            "error": {
                "message": (
                    f"Request body too large "
                    f"({actual_bytes} bytes > {max_bytes} byte limit)."
                ),
                "type": "invalid_request_error",
                "code": "request_too_large",
            }
        },
    )


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        content_length = headers.get("content-length")
        if content_length is not None:
            try:
                length = int(content_length)
            except ValueError:
                length = 0
            if length > self.max_bytes:
                response = request_too_large_response(length, self.max_bytes)
                await response(scope, receive, send)
                return

        body_parts: list[bytes] = []
        bytes_seen = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return

            body = message.get("body", b"")
            if body:
                body_parts.append(body)
                bytes_seen += len(body)
                if bytes_seen > self.max_bytes:
                    response = request_too_large_response(bytes_seen, self.max_bytes)
                    await response(scope, receive, send)
                    return

            if not message.get("more_body", False):
                break

        body = b"".join(body_parts)
        body_sent = False

        async def replay_receive() -> Message:
            nonlocal body_sent
            if body_sent:
                await asyncio.Event().wait()
            body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay_receive, send)


class AuthMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        if not settings.enable_api_key_auth:
            await self.app(scope, receive, send)
            return

        if scope["type"] == "http" and scope.get("path") in (_HEALTH_PATHS | _PUBLIC_BROWSER_PATHS):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        if not _has_valid_api_key(headers):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            response = JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "message": "Missing or invalid Authorization header. Expected: Bearer <key>",
                        "type": "authentication_error",
                        "code": "invalid_api_key",
                    }
                },
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def _has_valid_api_key(headers: Headers) -> bool:
    """Accept an HTTP bearer token or a browser-safe WebSocket subprotocol token."""
    auth_header = headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        return compare_digest(auth_header[len("Bearer "):], settings.api_key)

    # Browsers do not allow custom headers during a WebSocket handshake. The live
    # call page therefore offers the key as a base64url WebSocket subprotocol,
    # keeping it out of URLs, query strings, and application logs.
    for protocol in headers.get("sec-websocket-protocol", "").split(","):
        protocol = protocol.strip()
        if not protocol.startswith("local-ai-api-key."):
            continue
        encoded_token = protocol.removeprefix("local-ai-api-key.")
        try:
            padded = encoded_token + "=" * (-len(encoded_token) % 4)
            token = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if compare_digest(token, settings.api_key):
            return True
    return False


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    ollama_client.init(settings)
    if settings.warm_audio_on_start:
        import threading

        from .audio import warm_audio_models

        logger.info("Pre-warming audio models in the background (WARM_AUDIO_ON_START=true).")
        threading.Thread(
            target=warm_audio_models, args=(settings,), name="warm-audio", daemon=True
        ).start()
    logger.info("Gateway started (host=%s port=%s)", settings.host, settings.port)
    yield
    await ollama_client.close()
    logger.info("Gateway stopped")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    mcp_app = None
    try:
        from .mcp_server.server import mcp as _mcp_server

        # FastMCP 2+ exposes an ASGI application through ``http_app()``.
        # Use path="/" because FastAPI adds the public /mcp mount prefix.
        mcp_app = _mcp_server.http_app(path="/")
    except ImportError:
        logger.info("fastmcp not installed -- MCP server not available")

    @asynccontextmanager
    async def app_lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with lifespan(app):
            if mcp_app is None:
                yield
            else:
                # Mounted sub-applications do not run their own lifespan.
                # FastMCP needs this context to initialize its HTTP sessions.
                async with mcp_app.lifespan(app):
                    yield

    app = FastAPI(
        title="Local AI API Gateway",
        description="Private OpenAI-compatible gateway for Ollama over Tailscale.",
        version="1.0.0",
        lifespan=app_lifespan,
    )

    # Middleware: registered in reverse execution order.
    # AuthMiddleware registered first -> executes second (after body-size check).
    # BodySizeLimitMiddleware registered second -> executes first (outermost).
    app.add_middleware(AuthMiddleware)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)

    # Custom HTTPException handler -- when detail is already our {"error": {...}} dict,
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
        return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)

    # Custom validation error handler -- emit OpenAI-compatible error envelope.
    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> Response:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "message": "Request validation error.",
                    "type": "invalid_request_error",
                    "code": "invalid_request",
                }
            },
        )

    app.include_router(chat_router)
    app.include_router(embeddings_router)
    app.include_router(agents_router)
    app.include_router(models_router)
    app.include_router(audio_router)
    app.include_router(conversation_router)
    app.include_router(health_router)
    app.include_router(status_router)
    app.include_router(documents_router)

    # MCP server -- mount at /mcp if fastmcp is installed.
    if mcp_app is not None:
        app.mount("/mcp", mcp_app)
        logger.info("MCP server mounted at /mcp")

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
