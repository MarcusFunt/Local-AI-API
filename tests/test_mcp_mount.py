"""Regression coverage for mounting FastMCP under the gateway ASGI app."""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import ModuleType
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import main


def test_mcp_mount_uses_http_app_and_runs_its_lifespan(monkeypatch):
    events: list[str] = []

    @asynccontextmanager
    async def mcp_lifespan(_app):
        events.append("started")
        yield
        events.append("stopped")

    mcp_app = FastAPI()
    mcp_app.lifespan = mcp_lifespan

    class FakeMCP:
        def http_app(self, *, path: str):
            assert path == "/"
            return mcp_app

    fake_server = ModuleType("gateway.mcp_server.server")
    fake_server.mcp = FakeMCP()
    monkeypatch.setitem(sys.modules, "gateway.mcp_server.server", fake_server)

    app = main.create_app()

    with TestClient(app):
        assert events == ["started"]

    assert events == ["started", "stopped"]
    assert next(route for route in app.routes if route.path == "/mcp").app is mcp_app
