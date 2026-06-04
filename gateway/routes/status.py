from __future__ import annotations

import platform
import socket
import time
from datetime import UTC, datetime
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


_STATUS_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Local AI API Status</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #090b0f;
      --bg-elevated: #0d1118;
      --panel: #111821;
      --panel-soft: #151d28;
      --panel-strong: #1a2432;
      --text: #edf3fb;
      --muted: #93a4b8;
      --subtle: #69798d;
      --border: #243141;
      --border-strong: #314255;
      --green: #38d989;
      --green-bg: rgba(56, 217, 137, 0.13);
      --amber: #f3b44b;
      --amber-bg: rgba(243, 180, 75, 0.14);
      --red: #ff5e73;
      --red-bg: rgba(255, 94, 115, 0.14);
      --neutral: #8da2b7;
      --neutral-bg: rgba(141, 162, 183, 0.13);
      --blue: #6aa6ff;
      --blue-bg: rgba(106, 166, 255, 0.12);
      --focus: #7dd3fc;
      --shadow: 0 16px 40px rgba(0, 0, 0, 0.34);
    }

    * {
      box-sizing: border-box;
    }

    html {
      min-height: 100%;
      background: var(--bg);
    }

    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 14px;
      line-height: 1.45;
    }

    button {
      font: inherit;
    }

    .shell {
      min-height: 100vh;
    }

    .topbar {
      position: sticky;
      top: 0;
      z-index: 20;
      display: grid;
      gap: 12px;
      padding: 14px 22px 12px;
      background: rgba(9, 11, 15, 0.94);
      border-bottom: 1px solid var(--border);
      backdrop-filter: blur(14px);
    }

    .top-row,
    .brand,
    .top-meta,
    .active-tab,
    .tablist {
      display: flex;
      align-items: center;
    }

    .top-row {
      justify-content: space-between;
      gap: 18px;
    }

    .brand {
      min-width: 0;
      gap: 12px;
    }

    .brand h1 {
      margin: 0;
      font-size: 22px;
      line-height: 1.1;
      letter-spacing: 0;
    }

    .brand-mark {
      width: 11px;
      height: 31px;
      border-radius: 999px;
      background: var(--green);
      box-shadow: 0 0 22px rgba(56, 217, 137, 0.46);
      flex: 0 0 auto;
    }

    .top-meta {
      justify-content: flex-end;
      gap: 10px;
      min-width: 0;
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }

    .active-tab {
      min-height: 32px;
      gap: 8px;
      padding: 0 10px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--panel-soft);
      color: var(--muted);
      font-weight: 700;
    }

    .active-tab strong {
      color: var(--text);
      font-weight: 750;
    }

    .button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      min-height: 34px;
      padding: 6px 12px;
      border: 1px solid var(--border-strong);
      border-radius: 6px;
      color: var(--text);
      background: var(--panel-soft);
      cursor: pointer;
      font-size: 13px;
      font-weight: 750;
      transition: border-color 140ms ease, background 140ms ease, transform 140ms ease, opacity 140ms ease;
    }

    .button:hover:not(:disabled) {
      border-color: var(--blue);
      background: #1a2635;
      transform: translateY(-1px);
    }

    .button:focus-visible,
    .tab:focus-visible {
      outline: 3px solid rgba(125, 211, 252, 0.28);
      outline-offset: 2px;
    }

    .button:disabled {
      cursor: not-allowed;
      opacity: 0.52;
      transform: none;
    }

    .button.primary {
      border-color: rgba(106, 166, 255, 0.62);
      background: #19304b;
      color: #f3f8ff;
    }

    .tablist {
      gap: 6px;
      overflow-x: auto;
      padding-bottom: 2px;
      scrollbar-width: thin;
    }

    .tab {
      min-height: 34px;
      padding: 6px 12px;
      border: 1px solid transparent;
      border-radius: 6px;
      color: var(--muted);
      background: transparent;
      cursor: pointer;
      font-size: 13px;
      font-weight: 750;
      white-space: nowrap;
      transition: color 140ms ease, background 140ms ease, border-color 140ms ease;
    }

    .tab:hover {
      color: var(--text);
      background: rgba(255, 255, 255, 0.04);
    }

    .tab[aria-selected="true"] {
      color: var(--text);
      border-color: var(--border-strong);
      background: var(--panel-soft);
    }

    main {
      max-width: 1480px;
      margin: 0 auto;
      padding: 22px;
    }

    .tab-panel {
      display: none;
    }

    .tab-panel.active {
      display: block;
    }

    .overview-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }

    .content-grid {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(320px, 0.42fr);
      gap: 14px;
      align-items: start;
    }

    .panel,
    .metric {
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: var(--shadow);
    }

    .metric {
      min-height: 150px;
      padding: 15px;
      border-top: 3px solid var(--neutral);
    }

    .metric[data-state="good"] {
      border-top-color: var(--green);
    }

    .metric[data-state="warn"] {
      border-top-color: var(--amber);
    }

    .metric[data-state="bad"] {
      border-top-color: var(--red);
    }

    .metric[data-state="neutral"] {
      border-top-color: var(--neutral);
    }

    .metric h2,
    .panel h2 {
      margin: 0;
      font-size: 15px;
      line-height: 1.2;
      letter-spacing: 0;
    }

    .metric .value {
      display: block;
      margin-top: 10px;
      color: var(--text);
      font-size: 24px;
      font-weight: 800;
      line-height: 1.1;
      overflow-wrap: anywhere;
    }

    .metric dl {
      display: grid;
      grid-template-columns: minmax(90px, 0.45fr) minmax(0, 1fr);
      gap: 6px 12px;
      margin: 14px 0 0;
    }

    .panel {
      overflow: hidden;
    }

    .panel + .panel {
      margin-top: 14px;
    }

    .panel header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      min-height: 48px;
      padding: 12px 15px;
      border-bottom: 1px solid var(--border);
      background: var(--bg-elevated);
    }

    .panel-body {
      padding: 15px;
    }

    .toolbar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 12px;
    }

    .table-wrap {
      overflow-x: auto;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }

    th,
    td {
      padding: 10px 13px;
      border-bottom: 1px solid var(--border);
      text-align: left;
      vertical-align: top;
      white-space: nowrap;
    }

    th {
      color: var(--muted);
      background: var(--panel-soft);
      font-size: 11px;
      font-weight: 800;
      letter-spacing: 0;
      text-transform: uppercase;
    }

    tbody tr {
      transition: background 120ms ease;
    }

    tbody tr:hover {
      background: rgba(255, 255, 255, 0.025);
    }

    tr:last-child td {
      border-bottom: 0;
    }

    dt {
      color: var(--muted);
      font-size: 11px;
      font-weight: 800;
      letter-spacing: 0;
      text-transform: uppercase;
    }

    dd {
      margin: 0;
      min-width: 0;
      color: var(--text);
      overflow-wrap: anywhere;
    }

    .kv {
      display: grid;
      grid-template-columns: minmax(150px, 0.35fr) minmax(0, 1fr);
      gap: 10px 16px;
      margin: 0;
    }

    .kv.compact {
      grid-template-columns: minmax(120px, 0.45fr) minmax(0, 1fr);
      gap: 8px 12px;
    }

    .status-pill {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 24px;
      gap: 7px;
      padding: 2px 9px;
      border: 1px solid transparent;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 850;
      line-height: 1;
      white-space: nowrap;
    }

    .status-pill::before {
      content: "";
      width: 7px;
      height: 7px;
      border-radius: 999px;
      background: currentColor;
      box-shadow: 0 0 12px currentColor;
    }

    .status-pill.good {
      color: var(--green);
      border-color: rgba(56, 217, 137, 0.34);
      background: var(--green-bg);
    }

    .status-pill.warn {
      color: var(--amber);
      border-color: rgba(243, 180, 75, 0.38);
      background: var(--amber-bg);
    }

    .status-pill.bad {
      color: var(--red);
      border-color: rgba(255, 94, 115, 0.38);
      background: var(--red-bg);
    }

    .status-pill.neutral {
      color: var(--neutral);
      border-color: rgba(141, 162, 183, 0.28);
      background: var(--neutral-bg);
    }

    .muted {
      color: var(--muted);
    }

    .subtle {
      color: var(--subtle);
    }

    .code {
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 12px;
    }

    .prebox,
    .response-box {
      display: block;
      min-height: 38px;
      max-height: 280px;
      overflow: auto;
      padding: 10px 11px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: #0b1017;
      color: var(--text);
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }

    .empty {
      padding: 14px;
      border: 1px dashed var(--border-strong);
      border-radius: 8px;
      color: var(--muted);
      background: rgba(255, 255, 255, 0.02);
    }

    .result-label {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 14px;
      padding: 12px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--panel-soft);
    }

    .result-label strong {
      font-size: 16px;
    }

    .error-panel {
      border-color: rgba(255, 94, 115, 0.4);
      background: var(--red-bg);
      color: var(--red);
    }

    @media (max-width: 1060px) {
      .overview-grid,
      .content-grid {
        grid-template-columns: 1fr;
      }
    }

    @media (max-width: 760px) {
      .topbar {
        padding: 12px 14px 10px;
      }

      .top-row {
        align-items: flex-start;
        flex-direction: column;
      }

      .top-meta {
        width: 100%;
        justify-content: flex-start;
        flex-wrap: wrap;
        white-space: normal;
      }

      main {
        padding: 14px;
      }

      .brand h1 {
        font-size: 20px;
      }

      .overview-grid,
      .content-grid {
        gap: 10px;
      }

      .metric,
      .panel-body {
        padding: 12px;
      }

      .metric dl,
      .kv,
      .kv.compact {
        grid-template-columns: 1fr;
        gap: 5px;
      }

      th,
      td {
        padding: 9px 11px;
      }
    }

    @media (prefers-reduced-motion: reduce) {
      *,
      *::before,
      *::after {
        scroll-behavior: auto !important;
        transition-duration: 0.01ms !important;
        animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important;
      }
    }
  </style>
</head>
<body>
  <div class="shell">
    <header class="topbar">
      <div class="top-row">
        <div class="brand">
          <span class="brand-mark" aria-hidden="true"></span>
          <h1>Local AI API</h1>
          <span id="global-status" class="status-pill neutral">Loading</span>
        </div>
        <div class="top-meta">
          <span class="active-tab">Tab <strong id="active-tab-label">Overview</strong></span>
          <button class="button" id="refresh" type="button" aria-label="Refresh status">
            <span aria-hidden="true">&#8635;</span>
            Refresh
          </button>
          <span id="last-updated">Last updated: --</span>
        </div>
      </div>
      <nav class="tablist" role="tablist" aria-label="Status dashboard tabs">
        <button class="tab" id="tab-overview" type="button" role="tab" aria-selected="true" aria-controls="panel-overview" data-tab="overview">Overview</button>
        <button class="tab" id="tab-models" type="button" role="tab" aria-selected="false" aria-controls="panel-models" data-tab="models">Models</button>
        <button class="tab" id="tab-checks" type="button" role="tab" aria-selected="false" aria-controls="panel-checks" data-tab="checks">Checks</button>
        <button class="tab" id="tab-update" type="button" role="tab" aria-selected="false" aria-controls="panel-update" data-tab="update">Update</button>
        <button class="tab" id="tab-runtime" type="button" role="tab" aria-selected="false" aria-controls="panel-runtime" data-tab="runtime">Runtime</button>
        <button class="tab" id="tab-audio" type="button" role="tab" aria-selected="false" aria-controls="panel-audio" data-tab="audio">Audio</button>
        <button class="tab" id="tab-settings" type="button" role="tab" aria-selected="false" aria-controls="panel-settings" data-tab="settings">Settings</button>
      </nav>
    </header>

    <main>
      <section class="tab-panel active" id="panel-overview" role="tabpanel" aria-labelledby="tab-overview">
        <div class="overview-grid" id="overview-grid"></div>
      </section>

      <section class="tab-panel" id="panel-models" role="tabpanel" aria-labelledby="tab-models" hidden>
        <article class="panel">
          <header>
            <h2>Model aliases</h2>
            <span class="muted" id="models-count">--</span>
          </header>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Alias</th>
                  <th>Resolved model</th>
                  <th>Status</th>
                  <th>Size</th>
                  <th>Details</th>
                  <th>Modified date</th>
                </tr>
              </thead>
              <tbody id="models-body"></tbody>
            </table>
          </div>
        </article>
      </section>

      <section class="tab-panel" id="panel-checks" role="tabpanel" aria-labelledby="tab-checks" hidden>
        <div class="content-grid">
          <article class="panel">
            <header>
              <h2>Service checks</h2>
            </header>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Check</th>
                    <th>Status</th>
                    <th>Target</th>
                    <th>Latency</th>
                  </tr>
                </thead>
                <tbody id="checks-body"></tbody>
              </table>
            </div>
          </article>

          <article class="panel">
            <header>
              <h2>Last inference check</h2>
            </header>
            <div class="panel-body">
              <div id="dev-check-result"></div>
              <button class="button primary" id="run-check" type="button" style="width:100%; margin-top:14px;">
                <span aria-hidden="true">&#9654;</span>
                Run dev model check
              </button>
            </div>
          </article>
        </div>
      </section>

      <section class="tab-panel" id="panel-update" role="tabpanel" aria-labelledby="tab-update" hidden>
        <div class="content-grid">
          <article class="panel">
            <header>
              <h2>Repository state</h2>
              <span id="repository-status-pill" class="status-pill neutral">Idle</span>
            </header>
            <div class="panel-body">
              <dl class="kv" id="repository-state"></dl>
            </div>
          </article>

          <article class="panel">
            <header>
              <h2>Last update</h2>
            </header>
            <div class="panel-body">
              <div id="update-result-label"></div>
              <dl class="kv compact" id="last-update"></dl>
              <button class="button primary" id="run-update" type="button" style="width:100%; margin-top:14px;">
                <span aria-hidden="true">&#8681;</span>
                Update from Git
              </button>
            </div>
          </article>
        </div>
      </section>

      <section class="tab-panel" id="panel-runtime" role="tabpanel" aria-labelledby="tab-runtime" hidden>
        <article class="panel">
          <header>
            <h2>Runtime</h2>
          </header>
          <div class="panel-body">
            <dl class="kv" id="runtime-state"></dl>
          </div>
        </article>
      </section>

      <section class="tab-panel" id="panel-audio" role="tabpanel" aria-labelledby="tab-audio" hidden>
        <article class="panel">
          <header>
            <h2>Audio</h2>
          </header>
          <div class="panel-body">
            <dl class="kv" id="audio-state"></dl>
          </div>
        </article>
      </section>

      <section class="tab-panel" id="panel-settings" role="tabpanel" aria-labelledby="tab-settings" hidden>
        <article class="panel">
          <header>
            <h2>Settings</h2>
            <span class="status-pill neutral">Read only</span>
          </header>
          <div class="panel-body">
            <dl class="kv" id="settings-state"></dl>
          </div>
        </article>
      </section>
    </main>
  </div>

  <script>
    const TABS = ["overview", "models", "checks", "update", "runtime", "audio", "settings"];

    const els = {
      globalStatus: document.querySelector("#global-status"),
      activeTabLabel: document.querySelector("#active-tab-label"),
      lastUpdated: document.querySelector("#last-updated"),
      refresh: document.querySelector("#refresh"),
      overviewGrid: document.querySelector("#overview-grid"),
      modelsCount: document.querySelector("#models-count"),
      modelsBody: document.querySelector("#models-body"),
      checksBody: document.querySelector("#checks-body"),
      devCheckResult: document.querySelector("#dev-check-result"),
      runCheck: document.querySelector("#run-check"),
      repositoryStatusPill: document.querySelector("#repository-status-pill"),
      repositoryState: document.querySelector("#repository-state"),
      updateResultLabel: document.querySelector("#update-result-label"),
      lastUpdate: document.querySelector("#last-update"),
      runUpdate: document.querySelector("#run-update"),
      runtimeState: document.querySelector("#runtime-state"),
      audioState: document.querySelector("#audio-state"),
      settingsState: document.querySelector("#settings-state"),
      tabs: [...document.querySelectorAll("[role='tab']")],
      panels: [...document.querySelectorAll("[role='tabpanel']")],
    };

    let currentData = null;
    let activeTab = "overview";

    function escapeHtml(value) {
      return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }

    function titleCase(value) {
      if (!value) return "Unknown";
      return String(value)
        .replace(/[_-]+/g, " ")
        .replace(/\\b\\w/g, (char) => char.toUpperCase());
    }

    function statusKind(status) {
      const normalized = String(status ?? "unknown").toLowerCase();
      if (["ok", "healthy", "ready", "passed", "enabled"].includes(normalized)) return "good";
      if (["degraded", "missing", "running", "updating", "restart recommended"].includes(normalized)) return "warn";
      if (["failed", "unavailable", "error", "disabled"].includes(normalized)) return "bad";
      return "neutral";
    }

    function pill(status, label = titleCase(status)) {
      const kind = statusKind(status);
      return `<span class="status-pill ${kind}">${escapeHtml(label)}</span>`;
    }

    function boolText(value) {
      if (value === true) return "yes";
      if (value === false) return "no";
      return "--";
    }

    function enabledText(value) {
      return value ? "enabled" : "disabled";
    }

    function fmtTime(value) {
      if (!value) return "--";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return String(value);
      return new Intl.DateTimeFormat(undefined, {
        year: "numeric",
        month: "short",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      }).format(date);
    }

    function fmtShortTime(value) {
      if (!value) return "--";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return String(value);
      return new Intl.DateTimeFormat(undefined, {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      }).format(date);
    }

    function fmtDuration(seconds) {
      if (!Number.isFinite(seconds)) return "--";
      const total = Math.max(0, Math.round(seconds));
      const hrs = Math.floor(total / 3600);
      const mins = Math.floor((total % 3600) / 60);
      const secs = total % 60;
      if (hrs > 0) return `${hrs}h ${mins}m ${secs}s`;
      if (mins > 0) return `${mins}m ${secs}s`;
      return `${secs}s`;
    }

    function fmtMs(ms) {
      if (!Number.isFinite(ms)) return "--";
      if (ms >= 1000) return `${(ms / 1000).toFixed(2)}s`;
      return `${ms} ms`;
    }

    function kvRows(rows) {
      return rows.map(([label, value, options = {}]) => {
        const className = options.className ? ` class="${options.className}"` : "";
        const rendered = options.html ? value : escapeHtml(value ?? "--");
        return `<dt>${escapeHtml(label)}</dt><dd${className}>${rendered || "--"}</dd>`;
      }).join("");
    }

    function metric({ title, status, value, rows }) {
      return `
        <article class="metric" data-state="${statusKind(status)}">
          <h2>${escapeHtml(title)}</h2>
          <span class="value">${escapeHtml(value ?? "--")}</span>
          <dl>${kvRows(rows)}</dl>
        </article>
      `;
    }

    function deriveRepoStatus(repository) {
      if (!repository) return "unavailable";
      if (!repository.available) return "unavailable";
      if (repository.status === "running" || repository.last_update?.status === "running") return "running";
      if (["passed", "failed", "unavailable"].includes(repository.last_update?.status)) return repository.last_update.status;
      return repository.status ?? "idle";
    }

    function globalStatus(data) {
      const repoStatus = deriveRepoStatus(data.repository);
      if (repoStatus === "running") return ["updating", "Updating"];
      return data.status === "ok" ? ["healthy", "Healthy"] : ["degraded", "Degraded"];
    }

    function updateResult(repository, update = repository?.last_update) {
      if (!repository?.available && !update) return ["unavailable", "Update unavailable"];
      if (update?.status === "running" && update?.message) return ["running", "Already running"];
      if (update?.status === "running" || repository?.status === "running") return ["running", "Updating&hellip;"];
      if (update?.status === "passed" && update?.updated && update?.restart_recommended) {
        return ["restart recommended", "Updated &mdash; restart recommended"];
      }
      if (update?.status === "passed" && update?.updated) return ["passed", "Updated"];
      if (update?.status === "passed") return ["idle", "Already up to date"];
      if (update?.status === "failed") return ["failed", "Update failed"];
      if (update?.status === "unavailable") return ["unavailable", "Update unavailable"];
      if (!repository?.available) return ["unavailable", "Update unavailable"];
      return ["idle", "Idle"];
    }

    function setActiveTab(tab) {
      if (!TABS.includes(tab)) tab = "overview";
      activeTab = tab;
      els.activeTabLabel.textContent = titleCase(tab);
      els.tabs.forEach((button) => {
        const selected = button.dataset.tab === tab;
        button.setAttribute("aria-selected", String(selected));
      });
      els.panels.forEach((panel) => {
        const selected = panel.id === `panel-${tab}`;
        panel.hidden = !selected;
        panel.classList.toggle("active", selected);
      });
      if (location.hash.replace("#", "") !== tab) {
        history.replaceState(null, "", `#${tab}`);
      }
    }

    function renderOverview(data) {
      const models = data.models ?? [];
      const readyModels = models.filter((model) => model.status === "ready").length;
      const missingModels = models.length - readyModels;
      const devModel = models.find((model) => model.alias === "dev");
      const check = data.last_dev_check;
      const repo = data.repository;
      const repoStatus = deriveRepoStatus(repo);
      const repoUpdate = updateResult(repo);

      els.overviewGrid.innerHTML = [
        metric({
          title: "Gateway status",
          status: data.gateway?.status ?? "unknown",
          value: data.gateway?.status === "ok" ? "Healthy" : titleCase(data.gateway?.status),
          rows: [["Uptime", fmtDuration(data.gateway?.uptime_seconds)], ["Listen", `${data.gateway?.host ?? "--"}:${data.gateway?.port ?? "--"}`]],
        }),
        metric({
          title: "Ollama status",
          status: data.ollama?.status ?? "unknown",
          value: data.ollama?.status === "ok" ? "Connected" : "Unavailable",
          rows: [["Endpoint", data.ollama?.base_url], ["Latency", fmtMs(data.ollama?.latency_ms)]],
        }),
        metric({
          title: "Models ready/missing",
          status: missingModels === 0 ? "ready" : "missing",
          value: `${readyModels} / ${models.length} ready`,
          rows: [["Missing", missingModels], ["Dev model", devModel?.status ?? "unknown"]],
        }),
        metric({
          title: "Last dev-model check",
          status: check?.status ?? "unknown",
          value: check ? titleCase(check.status) : "Not run",
          rows: [["Model", check?.model ?? devModel?.model ?? "qwen3.5:0.8b"], ["Latency", fmtMs(check?.latency_ms)]],
        }),
        metric({
          title: "Repository/update status",
          status: repoStatus,
          value: repoUpdate[1].replace(/&hellip;/g, "...").replace(/&mdash;/g, "-"),
          rows: [["Branch", repo?.branch ?? "--"], ["Head", repo?.head ?? repo?.last_update?.head_after ?? "--"]],
        }),
        metric({
          title: "Runtime basics",
          status: "idle",
          value: data.gateway?.hostname ?? "--",
          rows: [["Python", data.gateway?.python], ["Platform", data.gateway?.platform]],
        }),
      ].join("");
    }

    function renderModels(data) {
      const models = data.models ?? [];
      const readyModels = models.filter((model) => model.status === "ready").length;
      els.modelsCount.textContent = `${readyModels} ready, ${models.length - readyModels} missing`;
      els.modelsBody.innerHTML = models.map((model) => `
        <tr>
          <td class="code">${escapeHtml(model.alias)}</td>
          <td class="code">${escapeHtml(model.model)}</td>
          <td>${pill(model.status)}</td>
          <td>${escapeHtml(model.size ?? "--")}</td>
          <td>${escapeHtml(model.details ?? "--")}</td>
          <td>${escapeHtml(fmtTime(model.modified_at))}</td>
        </tr>
      `).join("");
    }

    function renderChecks(data) {
      const devModel = (data.models ?? []).find((model) => model.alias === "dev");
      const check = data.last_dev_check;
      const rows = [
        ["Gateway check", data.gateway?.status ?? "unknown", "/health", "--"],
        ["Ollama check", data.ollama?.status ?? "unknown", `${data.ollama?.base_url ?? "--"}/api/tags`, fmtMs(data.ollama?.latency_ms)],
        ["Dev model installed check", devModel?.status ?? "unknown", devModel?.model ?? "qwen3.5:0.8b", "--"],
        ["Last inference check", check?.status ?? "unknown", check?.model ?? "Run dev model check", fmtMs(check?.latency_ms)],
      ];
      els.checksBody.innerHTML = rows.map(([name, status, target, latency]) => `
        <tr>
          <td>${escapeHtml(name)}</td>
          <td>${pill(status)}</td>
          <td class="code">${escapeHtml(target)}</td>
          <td>${escapeHtml(latency)}</td>
        </tr>
      `).join("");
      renderDevCheck(check);
    }

    function renderDevCheck(check) {
      if (!check) {
        els.devCheckResult.innerHTML = `<div class="empty">No dev-model inference check has run in this gateway process.</div>`;
        return;
      }
      els.devCheckResult.innerHTML = `
        <dl class="kv compact">
          ${kvRows([
            ["Status", pill(check.status), { html: true }],
            ["Model", check.model],
            ["Latency", fmtMs(check.latency_ms)],
            ["Prompt tokens", check.prompt_tokens ?? "--"],
            ["Completion tokens", check.completion_tokens ?? "--"],
            ["Checked time", fmtTime(check.checked_at)],
            ["Response or error", `<span class="response-box">${escapeHtml(check.response ?? check.error ?? "--")}</span>`, { html: true }],
          ])}
        </dl>
      `;
    }

    function renderUpdate(data) {
      const repo = data.repository ?? {};
      const status = deriveRepoStatus(repo);
      const last = repo.last_update;
      const [resultStatus, resultLabel] = updateResult(repo, last);
      const updateRunning = status === "running" || last?.status === "running";

      els.repositoryStatusPill.outerHTML = pill(status, titleCase(status)).replace("<span", '<span id="repository-status-pill"');
      els.repositoryStatusPill = document.querySelector("#repository-status-pill");
      els.repositoryState.innerHTML = kvRows([
        ["Available", boolText(repo.available)],
        ["Root", repo.root],
        ["Branch", repo.branch],
        ["Head", repo.head],
        ["Upstream", repo.upstream],
        ["Dirty", boolText(repo.dirty)],
        ["Status", pill(status), { html: true }],
        ["Reason", repo.available === false ? repo.reason : "--"],
      ]);

      els.updateResultLabel.innerHTML = `
        <div class="result-label">
          <strong>${resultLabel}</strong>
          ${pill(resultStatus, resultLabel.replace(/&hellip;/g, "...").replace(/&mdash;/g, "-"))}
        </div>
      `;
      els.lastUpdate.innerHTML = kvRows([
        ["Started time", fmtTime(last?.started_at)],
        ["Finished time", fmtTime(last?.finished_at)],
        ["Duration", fmtMs(last?.latency_ms)],
        ["Head before", last?.head_before],
        ["Head after", last?.head_after],
        ["Updated", boolText(last?.updated)],
        ["Restart recommended", boolText(last?.restart_recommended)],
        ["Output", `<span class="prebox code">${escapeHtml(last?.output ?? "--")}</span>`, { html: true }],
        ["Error", `<span class="prebox code">${escapeHtml(last?.error ?? "--")}</span>`, { html: true }],
      ]);

      els.runUpdate.disabled = !repo.available || updateRunning;
      els.runUpdate.title = repo.available ? "" : (repo.reason ?? "Repository update is unavailable.");
      els.runUpdate.setAttribute("aria-busy", String(updateRunning));
    }

    function renderRuntime(data) {
      const gateway = data.gateway ?? {};
      els.runtimeState.innerHTML = kvRows([
        ["Hostname", gateway.hostname],
        ["Listen host/port", `${gateway.host ?? "--"}:${gateway.port ?? "--"}`],
        ["Python version", gateway.python],
        ["Platform", gateway.platform],
        ["Default model profile", gateway.default_model_profile],
        ["Default Whisper model", gateway.default_whisper_model],
        ["Chatterbox model", gateway.chatterbox_model],
        ["Agent Zero", enabledText(gateway.agent_zero_enabled)],
        ["API key auth", enabledText(gateway.api_key_auth_enabled)],
        ["Arbitrary models", enabledText(gateway.arbitrary_models_enabled)],
        ["Request timeout", `${gateway.request_timeout_seconds ?? "--"} seconds`],
        ["Max request body size", `${gateway.max_request_body_bytes ?? "--"} bytes`],
      ]);
    }

    function renderAudio(data) {
      const gateway = data.gateway ?? {};
      const whisperDefault = gateway.default_whisper_model ?? "none";
      const transcriptionState = whisperDefault === "none" ? "disabled by default" : "enabled";
      els.audioState.innerHTML = kvRows([
        ["Whisper default model", whisperDefault],
        ["Chatterbox model", gateway.chatterbox_model],
        ["Transcription default", transcriptionState],
        ["Audio runtime state", data.audio?.status ?? "not reported"],
      ]);
    }

    function renderSettings(data) {
      const gateway = data.gateway ?? {};
      els.settingsState.innerHTML = kvRows([
        ["Service", gateway.service],
        ["Ollama base URL", data.ollama?.base_url],
        ["Host", gateway.host],
        ["Port", gateway.port],
        ["Default model profile", gateway.default_model_profile],
        ["Default Whisper model", gateway.default_whisper_model],
        ["Chatterbox model", gateway.chatterbox_model],
        ["Agent Zero enabled", boolText(gateway.agent_zero_enabled)],
        ["API key auth enabled", boolText(gateway.api_key_auth_enabled)],
        ["Arbitrary models enabled", boolText(gateway.arbitrary_models_enabled)],
        ["Request timeout seconds", gateway.request_timeout_seconds],
        ["Max request body bytes", gateway.max_request_body_bytes],
      ]);
    }

    function render(data) {
      currentData = data;
      const [overallStatus, overallLabel] = globalStatus(data);
      els.globalStatus.outerHTML = pill(overallStatus, overallLabel).replace("<span", '<span id="global-status"');
      els.globalStatus = document.querySelector("#global-status");
      els.lastUpdated.textContent = `Last updated: ${fmtShortTime(data.generated_at)}`;

      renderOverview(data);
      renderModels(data);
      renderChecks(data);
      renderUpdate(data);
      renderRuntime(data);
      renderAudio(data);
      renderSettings(data);
    }

    async function refreshStatus() {
      els.refresh.disabled = true;
      els.refresh.setAttribute("aria-busy", "true");
      try {
        const response = await fetch("/status.json", { cache: "no-store" });
        if (!response.ok) throw new Error(`Status request returned HTTP ${response.status}`);
        render(await response.json());
      } catch (error) {
        els.globalStatus.outerHTML = pill("error", "Degraded").replace("<span", '<span id="global-status"');
        els.globalStatus = document.querySelector("#global-status");
        els.overviewGrid.innerHTML = `
          <article class="metric error-panel" data-state="bad">
            <h2>Status unavailable</h2>
            <span class="value">Failed</span>
            <dl>${kvRows([["Error", error.message]])}</dl>
          </article>
        `;
      } finally {
        els.refresh.disabled = false;
        els.refresh.setAttribute("aria-busy", "false");
      }
    }

    async function runCheck() {
      els.runCheck.disabled = true;
      els.runCheck.setAttribute("aria-busy", "true");
      els.runCheck.innerHTML = '<span aria-hidden="true">&#9654;</span> Running check';
      try {
        const response = await fetch("/status/check", { method: "POST" });
        const payload = await response.json();
        renderDevCheck(payload);
        await refreshStatus();
      } catch (error) {
        renderDevCheck({
          status: "failed",
          model: "qwen3.5:0.8b",
          error: error.message,
          checked_at: new Date().toISOString(),
        });
      } finally {
        els.runCheck.disabled = false;
        els.runCheck.setAttribute("aria-busy", "false");
        els.runCheck.innerHTML = '<span aria-hidden="true">&#9654;</span> Run dev model check';
      }
    }

    async function runUpdate() {
      if (!window.confirm("Pull the latest Git changes for Local AI API?")) return;
      els.runUpdate.disabled = true;
      els.runUpdate.setAttribute("aria-busy", "true");
      els.runUpdate.innerHTML = '<span aria-hidden="true">&#8681;</span> Updating&hellip;';
      if (currentData?.repository) {
        currentData.repository.status = "running";
        renderUpdate(currentData);
      }
      try {
        const response = await fetch("/status/update", {
          method: "POST",
          headers: { "X-Local-AI-Admin-Action": "repo-update" },
        });
        const payload = await response.json();
        if (!response.ok && response.status !== 409) {
          throw new Error(payload?.error?.message ?? `Update request returned HTTP ${response.status}`);
        }
        const repo = currentData?.repository ?? { available: true };
        repo.last_update = payload;
        repo.status = payload.status;
        renderUpdate({ repository: repo });
        await refreshStatus();
      } catch (error) {
        const repo = currentData?.repository ?? { available: true };
        repo.status = "failed";
        repo.last_update = {
          status: "failed",
          error: error.message,
          started_at: new Date().toISOString(),
          finished_at: new Date().toISOString(),
        };
        renderUpdate({ repository: repo });
      } finally {
        els.runUpdate.innerHTML = '<span aria-hidden="true">&#8681;</span> Update from Git';
        if (currentData) renderUpdate(currentData);
      }
    }

    els.tabs.forEach((button) => {
      button.addEventListener("click", () => setActiveTab(button.dataset.tab));
    });
    window.addEventListener("hashchange", () => setActiveTab(location.hash.replace("#", "")));
    els.refresh.addEventListener("click", refreshStatus);
    els.runCheck.addEventListener("click", runCheck);
    els.runUpdate.addEventListener("click", runUpdate);

    setActiveTab(location.hash.replace("#", "") || "overview");
    refreshStatus();
    setInterval(refreshStatus, 15000);
  </script>
</body>
</html>
"""
