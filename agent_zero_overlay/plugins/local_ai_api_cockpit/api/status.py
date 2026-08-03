"""Authenticated Agent Zero proxy for the internal repo-ops MCP surface."""
from __future__ import annotations

import json
import os
from typing import Any

import httpx

from helpers.api import ApiHandler, Input, Output, Request, Response


_ALLOWED_TOOLS = {
    "archive_workspace",
    "autonomous_status",
    "create_workspace",
    "evaluate_workspace",
    "git_diff",
    "list_workspaces",
    "pause_autonomous_run",
    "preview_workspace",
    "resume_autonomous_run",
    "start_autonomous_run",
    "stop_autonomous_run",
    "task_report",
    "workspace_health",
}
_CANDIDATE_STATUS_TOOL = "candidate_status"


def _mcp_response_json(response: Any) -> Any:
    """Decode the JSON-RPC result from either Streamable HTTP representation."""
    content_type = str(response.headers.get("content-type", "")).lower()
    if "text/event-stream" not in content_type:
        return response.json()

    for line in response.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line.removeprefix("data:").strip())
    raise ValueError("MCP response did not contain an SSE data payload.")


def _cockpit_enabled() -> bool:
    """Keep the browser proxy disabled unless the container opts in explicitly."""
    return os.environ.get("AGENT_ZERO_COCKPIT_ENABLED", "false").strip().lower() == "true"


class Status(ApiHandler):
    """POST /api/plugins/local_ai_api_cockpit/status.

    The browser reaches this authenticated Agent Zero handler only. It cannot
    choose an endpoint, tool outside the allow-list, or arbitrary JSON-RPC
    method; merge, push, deployment, and shell tools are absent by design.
    """

    async def process(self, input: Input, request: Request) -> Output:
        if not _cockpit_enabled():
            return Response("Workspace cockpit is disabled by local configuration.", 403)
        tool = str(input.get("tool", "list_workspaces"))
        arguments = input.get("arguments", {})
        if tool == _CANDIDATE_STATUS_TOOL:
            return await self._candidate_status()
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
            return {"ok": True, "tool": tool, "result": _mcp_response_json(response)}
        except (httpx.HTTPError, ValueError) as exc:
            return Response(f"Workspace cockpit request failed: {exc}", 502)

    async def _candidate_status(self) -> Output:
        url = os.environ.get("GATEWAY_STATUS_URL", "http://host.docker.internal:8080/status.json")
        headers: dict[str, str] = {}
        key = os.environ.get("API_KEY_OTHER", "").strip()
        if key and key != "unused":
            headers["Authorization"] = f"Bearer {key}"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
            payload = response.json()
            return {"ok": True, "tool": _CANDIDATE_STATUS_TOOL, "result": payload.get("agent_zero_candidate")}
        except (httpx.HTTPError, ValueError) as exc:
            return Response(f"Candidate status request failed: {exc}", 502)
