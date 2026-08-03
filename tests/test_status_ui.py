"""Tests for the built-in status web UI and status JSON endpoints."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from gateway.config import Settings

pytestmark = pytest.mark.asyncio
OLLAMA_BASE = "http://127.0.0.1:11434"


def _ollama_model(name: str, size: int = 123_456_789) -> dict:
    return {
        "name": name,
        "model": name,
        "modified_at": "2026-05-19T08:00:00Z",
        "size": size,
        "details": {
            "family": "qwen3.5",
            "parameter_size": name.split(":")[-1],
        },
    }


class TestStatusPage:
    async def test_root_renders_status_gui(self, client: httpx.AsyncClient):
        resp = await client.get("/")

        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Local AI API" in resp.text
        assert "color-scheme: dark" in resp.text
        assert 'role="tablist"' in resp.text
        assert "Overview" in resp.text
        assert "Models" in resp.text
        assert "Checks" in resp.text
        assert "Update" in resp.text
        assert "Runtime" in resp.text
        assert "Audio" in resp.text
        assert "Settings" in resp.text
        assert 'id="global-status"' in resp.text
        assert "Updates are performed only by the configured installer schedule" in resp.text
        assert "/status.json" in resp.text
        assert "/status/update" not in resp.text
        assert "X-Local-AI-Admin-Action" not in resp.text
        assert "Autonomous workspaces" in resp.text

    async def test_status_path_renders_same_gui(self, client: httpx.AsyncClient):
        resp = await client.get("/status")

        assert resp.status_code == 200
        assert "Local AI API" in resp.text


class TestStatusJson:
    async def test_reports_all_profiles_ready(self, client: httpx.AsyncClient, monkeypatch):
        from gateway.routes import status as status_module

        monkeypatch.setattr(status_module, "_last_update_run", lambda: None)
        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.get("/api/tags").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "models": [
                            _ollama_model("qwen3.5:9b"),
                            _ollama_model("qwen3.5:4b"),
                            _ollama_model("qwen3.5:0.8b"),
                            _ollama_model("qwen3:14b"),
                            _ollama_model("qwen3:8b"),
                        ]
                    },
                )
            )
            resp = await client.get("/status.json")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["ollama"]["status"] == "ok"
        assert {model["alias"] for model in body["models"]} == {
            "main",
            "small",
            "dev",
            "agent",
            "agent-utility",
        }
        assert all(model["status"] == "ready" for model in body["models"])
        assert "repository" in body
        assert "available" in body["repository"]
        assert body["autonomy"]["max_runtime_hours"] == 24
        assert body["autonomy"]["max_storage_gib"] == 20

    async def test_reports_missing_dev_model_as_degraded(self, client: httpx.AsyncClient):
        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.get("/api/tags").mock(
                return_value=httpx.Response(
                    200,
                    json={"models": [_ollama_model("qwen3.5:9b")]},
                )
            )
            resp = await client.get("/status.json")

        assert resp.status_code == 200
        body = resp.json()
        dev = next(model for model in body["models"] if model["alias"] == "dev")
        assert body["status"] == "degraded"
        assert dev["model"] == "qwen3.5:0.8b"
        assert dev["status"] == "missing"

    async def test_agent_zero_profiles_are_always_required(
        self,
        client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import gateway.routes.status as status_module

        enabled_settings = Settings(
            ollama_base_url=OLLAMA_BASE,
            enable_api_key_auth=False,
            api_key="",
            enable_arbitrary_models=False,
            request_timeout_seconds=10,
            max_request_body_bytes=10_485_760,
            agent_zero_enabled=False,
        )
        monkeypatch.setattr(status_module, "settings", enabled_settings)

        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.get("/api/tags").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "models": [
                            _ollama_model("qwen3.5:9b"),
                            _ollama_model("qwen3.5:4b"),
                            _ollama_model("qwen3.5:0.8b"),
                        ]
                    },
                )
            )
            resp = await client.get("/status.json")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["gateway"]["agent_zero_enabled"] is True
        assert {model["alias"] for model in body["models"]} == {
            "main",
            "small",
            "dev",
            "agent",
            "agent-utility",
        }
        agent = next(model for model in body["models"] if model["alias"] == "agent")
        assert agent["model"] == "qwen3:14b"
        assert agent["status"] == "missing"

    async def test_ollama_error_returns_degraded_status(self, client: httpx.AsyncClient):
        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.get("/api/tags").mock(return_value=httpx.Response(500, text="nope"))
            resp = await client.get("/status.json")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["ollama"]["status"] == "error"
        assert "HTTP 500" in body["ollama"]["error"]


class TestStatusCheck:
    async def test_dev_check_uses_08b_model(self, client: httpx.AsyncClient):
        captured: list[dict] = []

        async def capture(request: httpx.Request, *args, **kwargs):
            captured.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "model": "qwen3.5:0.8b",
                    "created_at": "2026-05-19T08:00:00Z",
                    "message": {"role": "assistant", "content": "ok"},
                    "done": True,
                    "prompt_eval_count": 7,
                    "eval_count": 1,
                },
            )

        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.post("/api/chat").mock(side_effect=capture)
            resp = await client.post("/status/check")

        assert resp.status_code == 200
        assert captured[0]["model"] == "qwen3.5:0.8b"
        assert captured[0]["stream"] is False
        assert captured[0]["think"] is False
        body = resp.json()
        assert body["status"] == "passed"
        assert body["model_alias"] == "dev"
        assert body["model"] == "qwen3.5:0.8b"

    async def test_dev_check_fails_on_non_exact_response(self, client: httpx.AsyncClient):
        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.post("/api/chat").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "model": "qwen3.5:0.8b",
                        "created_at": "2026-05-19T08:00:00Z",
                        "message": {"role": "assistant", "content": "okay"},
                        "done": True,
                        "prompt_eval_count": 7,
                        "eval_count": 1,
                    },
                )
            )
            resp = await client.post("/status/check")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "failed"
        assert body["response"] == "okay"
        assert body["error"] == "Dev model check expected exactly 'ok'."

    async def test_dev_check_is_rate_limited(self, client: httpx.AsyncClient):
        import gateway.routes.status as status_module

        status_module._STATUS_CHECK_LAST.clear()
        with respx.mock(base_url=OLLAMA_BASE) as mock:
            mock.post("/api/chat").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "message": {"role": "assistant", "content": "ok"},
                        "prompt_eval_count": 1,
                        "eval_count": 1,
                    },
                )
            )
            first = await client.post("/status/check")
            second = await client.post("/status/check")

        assert first.status_code == 200
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "status_check_rate_limited"
        assert int(second.headers["retry-after"]) >= 1


async def test_status_update_endpoint_is_not_available(client: httpx.AsyncClient):
    resp = await client.post("/status/update")

    assert resp.status_code == 404


async def test_last_update_run_reads_marker_and_tolerates_missing():
    """The status payload surfaces the installer's update marker so a silently
    failed scheduled auto-update is visible, and tolerates a missing marker."""
    from pathlib import Path

    from gateway.routes import status as status_module

    marker = Path(status_module.__file__).resolve().parents[2] / ".local" / "last-update.json"
    existed = marker.exists()
    backup = marker.read_bytes() if existed else None
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_bytes(
            b'\xef\xbb\xbf{"status": "failed", "scheduled": true, "finished_at": "2026-01-01T00:00:00Z"}'
        )
        assert status_module._last_update_run() == {
            "status": "failed",
            "scheduled": True,
            "finished_at": "2026-01-01T00:00:00Z",
        }

        marker.unlink()
        assert status_module._last_update_run() is None
    finally:
        if existed and backup is not None:
            marker.write_bytes(backup)
        elif marker.exists():
            marker.unlink()


async def test_failed_scheduled_update_degrades_the_status_payload(monkeypatch):
    from gateway.routes import status as status_module

    async def healthy_ollama():
        return {"status": "ok"}, []

    async def repository_status():
        return {"status": "idle", "available": True, "update_owner": "installer_schedule"}

    failed_update = {"status": "failed", "scheduled": True, "error": "scheduled update refused dirty checkout"}
    monkeypatch.setattr(status_module, "_fetch_ollama_tags", healthy_ollama)
    monkeypatch.setattr(status_module, "_profile_statuses", lambda _models: [])
    monkeypatch.setattr(status_module, "get_repo_update_status", repository_status)
    monkeypatch.setattr(status_module, "_last_update_run", lambda: failed_update)

    payload = await status_module._build_status_payload()

    assert payload["status"] == "degraded"
    assert payload["last_update_run"] == failed_update
    assert payload["repository"]["last_update"] == failed_update
