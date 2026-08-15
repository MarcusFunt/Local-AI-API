"""Opt-in, live Agent Zero output-contract regression coverage.

Unlike a health check, this test runs the real Agent Zero API and verifies
that its returned, user-visible answer follows an instruction.  It is opt-in
because it intentionally uses the local 14B Ollama model and can be slow.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.integration


def test_agent_zero_returns_instruction_following_output() -> None:
    """Assert a real Agent Zero run returns valid JSON and useful content."""
    if os.getenv("RUN_AGENT_ZERO_E2E") != "1":
        pytest.skip("set RUN_AGENT_ZERO_E2E=1 to run the local Agent Zero output test")
    if shutil.which("docker") is None:
        pytest.skip("the live Agent Zero output test must run on the Docker host")

    probe = r'''
import asyncio
import json
import httpx
from helpers.settings import get_settings

async def main() -> None:
    async with httpx.AsyncClient(timeout=180.0) as client:
        response = await client.post(
            "http://127.0.0.1/api/api_message",
            headers={"X-API-KEY": get_settings()["mcp_server_token"]},
            json={
                "message": "Use the response tool and return exactly this word: violet.",
                "agent_profile": "agent0",
                "lifetime_hours": 0.1,
            },
        )
    print(json.dumps({"status": response.status_code, "body": response.json()}))

asyncio.run(main())
'''
    command = [
        "docker", "compose", "-f", "compose.yaml", "-f", "compose.qdrant.yaml",
        "-f", "compose.agent-zero.yaml", "exec", "-T", "-w", "/a0", "agent-zero",
        "/opt/venv-a0/bin/python", "-c", probe,
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=240,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["status"] == 200, payload
    answer = payload["body"].get("response")
    assert isinstance(answer, str) and answer.strip(), payload
    assert answer.strip().casefold() == "violet", payload
