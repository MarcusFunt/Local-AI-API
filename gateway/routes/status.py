from __future__ import annotations

import json
import platform
import socket
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from ..app_update import get_repo_update_status, run_repo_update
from ..config import settings
from ..normalize import MODEL_MAP, required_model_aliases, resolve_model

router = APIRouter()

_STARTED_AT = time.monotonic()
_STATUS_TIMEOUT = 5.0
_CHECK_TIMEOUT = 120.0
_DEV_ALIAS = "dev"
_UPDATE_HEADER_VALUE = "repo-update"
_LAST_DEV_CHECK: dict[str, Any] | None = None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso_now() -> str:
    return _utc_now().isoformat().replace("+00:00", "Z")


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((time.monotonic() - started_at) * 1000))


def _format_bytes(size: int | None) -> str | None:
    if size is None:
        return None
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} {unit}"
        value /= 1024
    return f"{size} B"


def _model_name(model: dict[str, Any]) -> str:
    name = model.get("model") or model.get("name")
    return str(name) if name is not None else ""


def _model_detail(model: dict[str, Any]) -> str | None:
    details = model.get("details")
    if not isinstance(details, dict):
        return None
    family = details.get("family")
    parameter_size = details.get("parameter_size")
    if family and parameter_size:
        return f"{family} {parameter_size}"
    if parameter_size:
        return str(parameter_size)
    if family:
        return str(family)
    return None


async def _fetch_ollama_tags() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    started_at = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_STATUS_TIMEOUT) as client:
            response = await client.get(f"{settings.ollama_base_url}/api/tags")
        latency_ms = _elapsed_ms(started_at)
        if response.status_code >= 400:
            return (
                {
                    "status": "error",
                    "base_url": settings.ollama_base_url,
                    "latency_ms": latency_ms,
                    "error": f"Ollama returned HTTP {response.status_code}",
                    "models_count": 0,
                },
                [],
            )
        payload = response.json()
        models = payload.get("models", [])
        if not isinstance(models, list):
            models = []
        return (
            {
                "status": "ok",
                "base_url": settings.ollama_base_url,
                "latency_ms": latency_ms,
                "models_count": len(models),
            },
            [m for m in models if isinstance(m, dict)],
        )
    except httpx.TimeoutException as exc:
        return (
            {
                "status": "error",
                "base_url": settings.ollama_base_url,
                "latency_ms": _elapsed_ms(started_at),
                "error": f"Ollama status check timed out: {exc}",
                "models_count": 0,
            },
            [],
        )
    except (httpx.ConnectError, ValueError) as exc:
        return (
            {
                "status": "error",
                "base_url": settings.ollama_base_url,
                "latency_ms": _elapsed_ms(started_at),
                "error": f"Could not read Ollama status: {exc}",
                "models_count": 0,
            },
            [],
        )


def _profile_statuses(ollama_models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_name = {_model_name(model): model for model in ollama_models if _model_name(model)}
    profiles: list[dict[str, Any]] = []
    for alias in required_model_aliases():
        resolved_model = MODEL_MAP[alias]
        model = by_name.get(resolved_model)
        profiles.append(
            {
                "alias": alias,
                "model": resolved_model,
                "status": "ready" if model else "missing",
                "size_bytes": model.get("size") if model else None,
                "size": _format_bytes(model.get("size")) if model else None,
                "modified_at": model.get("modified_at") if model else None,
                "details": _model_detail(model) if model else None,
            }
        )
    return profiles


def _runtime_status() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "local-ai-api",
        "hostname": socket.gethostname(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "uptime_seconds": round(time.monotonic() - _STARTED_AT),
        "host": settings.host,
        "port": settings.port,
        "default_model_profile": settings.default_model_profile,
        "default_whisper_model": settings.default_whisper_model,
        "chatterbox_model": settings.chatterbox_model,
        "agent_zero_enabled": settings.agent_zero_enabled,
        "api_key_auth_enabled": settings.enable_api_key_auth,
        "arbitrary_models_enabled": settings.enable_arbitrary_models,
        "request_timeout_seconds": settings.request_timeout_seconds,
        "max_request_body_bytes": settings.max_request_body_bytes,
    }


def _last_update_run() -> dict[str, Any] | None:
    """Read the marker the installer writes after each scheduled/manual update
    run so a silently-failed auto-update is visible on the status page."""
    marker = Path(__file__).resolve().parents[2] / ".local" / "last-update.json"
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


async def _build_status_payload() -> dict[str, Any]:
    ollama, ollama_models = await _fetch_ollama_tags()
    profiles = _profile_statuses(ollama_models)
    repository = await get_repo_update_status()
    missing_profiles = [profile for profile in profiles if profile["status"] != "ready"]
    overall_status = "ok" if ollama["status"] == "ok" and not missing_profiles else "degraded"

    return {
        "status": overall_status,
        "generated_at": _iso_now(),
        "gateway": _runtime_status(),
        "ollama": ollama,
        "models": profiles,
        "repository": repository,
        "last_dev_check": _LAST_DEV_CHECK,
        "last_update_run": _last_update_run(),
    }


async def _run_dev_check() -> dict[str, Any]:
    started_at = time.monotonic()
    resolved_model = resolve_model(_DEV_ALIAS, settings)
    body = {
        "model": resolved_model,
        "messages": [{"role": "user", "content": "Reply with exactly: ok"}],
        "stream": False,
        "think": False,
        "options": {"temperature": 0, "num_predict": 8},
    }

    result: dict[str, Any] = {
        "model_alias": _DEV_ALIAS,
        "model": resolved_model,
        "checked_at": _iso_now(),
    }

    try:
        async with httpx.AsyncClient(timeout=_CHECK_TIMEOUT) as client:
            response = await client.post(f"{settings.ollama_base_url}/api/chat", json=body)
        result["latency_ms"] = _elapsed_ms(started_at)
        if response.status_code >= 400:
            result.update(
                {
                    "status": "failed",
                    "error": f"Ollama returned HTTP {response.status_code}",
                }
            )
            return result

        payload = response.json()
        message = payload.get("message", {})
        content = message.get("content", "") if isinstance(message, dict) else ""
        response_text = content.strip()
        result.update(
            {
                "status": "passed" if response_text == "ok" else "failed",
                "response": response_text,
                "prompt_tokens": payload.get("prompt_eval_count", 0) or 0,
                "completion_tokens": payload.get("eval_count", 0) or 0,
            }
        )
        if response_text != "ok":
            result["error"] = "Dev model check expected exactly 'ok'."
        return result
    except httpx.TimeoutException as exc:
        result.update(
            {
                "status": "failed",
                "latency_ms": _elapsed_ms(started_at),
                "error": f"Dev model check timed out: {exc}",
            }
        )
        return result
    except (httpx.ConnectError, ValueError) as exc:
        result.update(
            {
                "status": "failed",
                "latency_ms": _elapsed_ms(started_at),
                "error": f"Dev model check failed: {exc}",
            }
        )
        return result


@router.get("/", response_class=HTMLResponse)
@router.get("/status", response_class=HTMLResponse)
async def status_page() -> HTMLResponse:
    return HTMLResponse(_STATUS_HTML)


@router.get("/status.json")
async def status_json() -> JSONResponse:
    return JSONResponse(await _build_status_payload())


@router.post("/status/check")
async def run_status_check() -> JSONResponse:
    global _LAST_DEV_CHECK
    _LAST_DEV_CHECK = await _run_dev_check()
    return JSONResponse(_LAST_DEV_CHECK)


@router.post("/status/update")
async def run_status_update(
    x_local_ai_admin_action: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    if x_local_ai_admin_action != _UPDATE_HEADER_VALUE:
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "message": "Missing status update confirmation header.",
                    "type": "forbidden",
                    "code": "status_update_confirmation_required",
                }
            },
        )
    result = await run_repo_update()
    status_code = 409 if result.get("status") == "running" else 200
    return JSONResponse(result, status_code=status_code)


def _load_status_html() -> str:
    return (Path(__file__).resolve().parent.parent / "static" / "status.html").read_text(
        encoding="utf-8"
    )


_STATUS_HTML = _load_status_html()
