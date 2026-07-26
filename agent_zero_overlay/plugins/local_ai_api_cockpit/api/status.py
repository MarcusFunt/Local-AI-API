"""Authenticated Agent Zero proxy for the internal repo-ops MCP surface."""
from __future__ import annotations

import os
from typing import Any

import httpx

from helpers.api import ApiHandler, Input, Output, Request, Response


_ALLOWED_TOOLS = {"list_workspaces", "workspace_health", "autonomous_status", "git_diff", "task_report"}


class Status(ApiHandler):
    """POST /api/plugins/local_ai_api_cockpit/status.

    The browser reaches this authenticated Agent Zero handler only. It cannot
    choose an endpoint, tool outside the allow-list, or arbitrary JSON-RPC
    method; merge, push, deployment, and shell tools are absent by design.
    """

    async def process(self, input: Input, request: Request) -> Output:
        tool = str(input.get("tool", "list_workspaces"))
        arguments = input.get("arguments", {})
        if tool not in _ALLOWED_TOOLS or not isinstance(arguments, dict):
            return Response("Unsupported cockpit action.", 400)
        endpoint = os.environ.get("REPO_OPS_MCP_URL", "http://repo-ops:8090/mcp")
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": "cockpit",
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                headers = {"Accept": "application/json, text/event-stream"}
                initialized = await client.post(
                    endpoint,
                    json={
                        "jsonrpc": "2.0",
                        "id": "cockpit-init",
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {},
                            "clientInfo": {"name": "local-ai-api-cockpit", "version": "1.0"},
                        },
                    },
                    headers=headers,
                )
                initialized.raise_for_status()
                session_id = initialized.headers.get("mcp-session-id")
                if not session_id:
                    return Response("Workspace cockpit could not establish an MCP session.", 502)
                headers["Mcp-Session-Id"] = session_id
                await client.post(
                    endpoint,
                    json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                    headers=headers,
                )
                response = await client.post(endpoint, json=payload, headers=headers)
                response.raise_for_status()
            return {"ok": True, "tool": tool, "result": response.json()}
        except (httpx.HTTPError, ValueError) as exc:
            return Response(f"Workspace cockpit request failed: {exc}", 502)
