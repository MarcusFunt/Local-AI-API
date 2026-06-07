"""ASGI middleware to strip /mcp prefix before handing off to FastMCP."""
from __future__ import annotations
from typing import Callable


class StripPrefixMiddleware:
    """Strip a URL prefix so sub-mounted apps receive clean paths."""

    def __init__(self, app, prefix: str) -> None:
        self.app = app
        self.prefix = prefix.rstrip("/")

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            if path.startswith(self.prefix):
                scope = {**scope, "path": path[len(self.prefix):] or "/"}
        await self.app(scope, receive, send)
