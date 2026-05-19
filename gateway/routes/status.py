from __future__ import annotations

import platform
import socket
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

from ..config import settings
from ..normalize import MODEL_MAP, resolve_model

router = APIRouter()

_STARTED_AT = time.monotonic()
_STATUS_TIMEOUT = 5.0
_CHECK_TIMEOUT = 120.0
_DEV_ALIAS = "dev"
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
    for alias, resolved_model in MODEL_MAP.items():
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
        "api_key_auth_enabled": settings.enable_api_key_auth,
        "arbitrary_models_enabled": settings.enable_arbitrary_models,
        "request_timeout_seconds": settings.request_timeout_seconds,
        "max_request_body_bytes": settings.max_request_body_bytes,
    }


async def _build_status_payload() -> dict[str, Any]:
    ollama, ollama_models = await _fetch_ollama_tags()
    profiles = _profile_statuses(ollama_models)
    missing_profiles = [profile for profile in profiles if profile["status"] != "ready"]
    overall_status = "ok" if ollama["status"] == "ok" and not missing_profiles else "degraded"

    return {
        "status": overall_status,
        "generated_at": _iso_now(),
        "gateway": _runtime_status(),
        "ollama": ollama,
        "models": profiles,
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
        result.update(
            {
                "status": "passed" if content.strip() else "failed",
                "response": content.strip(),
                "prompt_tokens": payload.get("prompt_eval_count", 0) or 0,
                "completion_tokens": payload.get("eval_count", 0) or 0,
            }
        )
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


_STATUS_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Local AI API Status</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --panel-soft: #fafbfc;
      --text: #111827;
      --muted: #5f6b7a;
      --border: #d9dee7;
      --ok: #168a3a;
      --warn: #b7791f;
      --bad: #c53030;
      --focus: #2463eb;
      --shadow: 0 1px 2px rgba(15, 23, 42, 0.05);
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.45;
    }

    button {
      font: inherit;
    }

    .shell {
      min-height: 100vh;
    }

    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 24px;
      background: rgba(255, 255, 255, 0.92);
      border-bottom: 1px solid var(--border);
      position: sticky;
      top: 0;
      z-index: 10;
      backdrop-filter: blur(12px);
    }

    .brand {
      display: flex;
      align-items: baseline;
      gap: 10px;
      min-width: 0;
    }

    .brand h1 {
      margin: 0;
      font-size: 24px;
      line-height: 1.1;
      letter-spacing: 0;
    }

    .brand span {
      display: inline-flex;
      align-items: center;
      min-height: 26px;
      padding: 3px 8px;
      border: 1px solid var(--border);
      border-radius: 6px;
      color: var(--muted);
      background: var(--panel-soft);
      font-size: 13px;
      font-weight: 600;
      white-space: nowrap;
    }

    .actions {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 16px;
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }

    .button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      min-height: 36px;
      padding: 7px 12px;
      border: 1px solid var(--border);
      border-radius: 6px;
      color: var(--text);
      background: var(--panel);
      cursor: pointer;
      font-size: 14px;
      font-weight: 650;
      box-shadow: var(--shadow);
    }

    .button:hover {
      border-color: #aeb8c7;
      background: #fdfefe;
    }

    .button:focus-visible {
      outline: 3px solid rgba(36, 99, 235, 0.22);
      outline-offset: 2px;
    }

    .button:disabled {
      cursor: wait;
      opacity: 0.62;
    }

    main {
      padding: 24px;
    }

    .summary {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
      margin-bottom: 20px;
    }

    .metric,
    .panel {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }

    .metric {
      padding: 16px;
      min-height: 128px;
      border-left: 3px solid var(--border);
    }

    .metric[data-state="ok"] {
      border-left-color: var(--ok);
    }

    .metric[data-state="warn"] {
      border-left-color: var(--warn);
    }

    .metric[data-state="bad"] {
      border-left-color: var(--bad);
    }

    .metric h2,
    .panel h2 {
      margin: 0;
      font-size: 16px;
      line-height: 1.25;
      letter-spacing: 0;
    }

    .state {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      margin-top: 10px;
      font-weight: 700;
      color: var(--muted);
    }

    .dot {
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: var(--muted);
      flex: 0 0 auto;
    }

    .state.ok {
      color: var(--ok);
    }

    .state.warn {
      color: var(--warn);
    }

    .state.bad {
      color: var(--bad);
    }

    .state.ok .dot {
      background: var(--ok);
    }

    .state.warn .dot {
      background: var(--warn);
    }

    .state.bad .dot {
      background: var(--bad);
    }

    .metric dl {
      display: grid;
      gap: 2px;
      margin: 14px 0 0;
    }

    dt {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
    }

    dd {
      margin: 0 0 8px;
      overflow-wrap: anywhere;
    }

    .grid {
      display: grid;
      grid-template-columns: minmax(0, 1.35fr) minmax(320px, 0.65fr);
      gap: 18px;
      align-items: start;
    }

    .stack {
      display: grid;
      gap: 18px;
    }

    .panel header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 14px 16px;
      border-bottom: 1px solid var(--border);
    }

    .panel-body {
      padding: 16px;
    }

    .table-wrap {
      overflow-x: auto;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }

    th,
    td {
      padding: 11px 16px;
      border-bottom: 1px solid var(--border);
      text-align: left;
      vertical-align: top;
      white-space: nowrap;
    }

    th {
      color: #2f3a4a;
      background: var(--panel-soft);
      font-size: 12px;
      text-transform: uppercase;
    }

    tr:last-child td {
      border-bottom: 0;
    }

    .code {
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 13px;
    }

    .muted {
      color: var(--muted);
    }

    .check-result {
      display: grid;
      gap: 12px;
    }

    .check-result .row {
      display: grid;
      grid-template-columns: 128px minmax(0, 1fr);
      gap: 16px;
      align-items: start;
    }

    .response-box {
      min-height: 40px;
      padding: 10px 12px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--panel-soft);
      overflow-wrap: anywhere;
    }

    .footer {
      display: flex;
      flex-wrap: wrap;
      gap: 10px 18px;
      color: var(--muted);
      font-size: 13px;
      padding: 20px 4px 4px;
    }

    .footer span + span::before {
      content: "/";
      margin-right: 18px;
      color: #a0a8b4;
    }

    .error {
      border-color: rgba(197, 48, 48, 0.35);
      color: var(--bad);
      background: #fff8f8;
    }

    @media (max-width: 980px) {
      .summary,
      .grid {
        grid-template-columns: 1fr;
      }

      .metric {
        min-height: auto;
      }
    }

    @media (max-width: 640px) {
      .topbar {
        align-items: flex-start;
        flex-direction: column;
        padding: 16px;
      }

      .actions {
        width: 100%;
        justify-content: space-between;
      }

      main {
        padding: 16px;
      }

      .brand h1 {
        font-size: 21px;
      }

      th,
      td {
        padding: 10px 12px;
      }

      .check-result .row {
        grid-template-columns: 1fr;
        gap: 4px;
      }
    }
  </style>
</head>
<body>
  <div class="shell">
    <header class="topbar">
      <div class="brand">
        <h1>Local AI API</h1>
        <span>Gateway</span>
      </div>
      <div class="actions">
        <button class="button" id="refresh" type="button" aria-label="Refresh status">
          <span aria-hidden="true">&#8635;</span>
          Refresh
        </button>
        <span id="last-updated">Last updated: --</span>
      </div>
    </header>

    <main>
      <section class="summary" id="summary" aria-label="Status summary"></section>

      <section class="grid">
        <div class="stack">
          <article class="panel">
            <header>
              <h2>Models</h2>
              <span class="muted" id="models-count">--</span>
            </header>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Alias</th>
                    <th>Model</th>
                    <th>Status</th>
                    <th>Size</th>
                    <th>Details</th>
                  </tr>
                </thead>
                <tbody id="models"></tbody>
              </table>
            </div>
          </article>

          <article class="panel">
            <header>
              <h2>Service checks</h2>
            </header>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Service</th>
                    <th>Status</th>
                    <th>Endpoint / Info</th>
                    <th>Latency</th>
                  </tr>
                </thead>
                <tbody id="checks"></tbody>
              </table>
            </div>
          </article>
        </div>

        <div class="stack">
          <article class="panel">
            <header>
              <h2>End-to-end check</h2>
            </header>
            <div class="panel-body">
              <div class="check-result" id="check-result"></div>
              <button class="button" id="run-check" type="button" style="width:100%; margin-top:16px;">
                <span aria-hidden="true">&#9654;</span>
                Run dev model check
              </button>
            </div>
          </article>

          <article class="panel">
            <header>
              <h2>Runtime</h2>
            </header>
            <div class="panel-body">
              <dl id="runtime"></dl>
            </div>
          </article>
        </div>
      </section>

      <footer class="footer" id="footer"></footer>
    </main>
  </div>

  <script>
    const els = {
      summary: document.querySelector("#summary"),
      models: document.querySelector("#models"),
      modelsCount: document.querySelector("#models-count"),
      checks: document.querySelector("#checks"),
      runtime: document.querySelector("#runtime"),
      footer: document.querySelector("#footer"),
      lastUpdated: document.querySelector("#last-updated"),
      refresh: document.querySelector("#refresh"),
      runCheck: document.querySelector("#run-check"),
      checkResult: document.querySelector("#check-result"),
    };

    const fmtTime = (value) => {
      if (!value) return "--";
      return new Intl.DateTimeFormat(undefined, {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      }).format(new Date(value));
    };

    const fmtDuration = (seconds) => {
      if (!Number.isFinite(seconds)) return "--";
      const mins = Math.floor(seconds / 60);
      const hrs = Math.floor(mins / 60);
      const remMins = mins % 60;
      const remSecs = Math.floor(seconds % 60);
      if (hrs > 0) return `${hrs}h ${remMins}m ${remSecs}s`;
      if (mins > 0) return `${mins}m ${remSecs}s`;
      return `${remSecs}s`;
    };

    const escapeHtml = (value) => String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");

    const stateKind = (status) => {
      if (["ok", "ready", "passed"].includes(status)) return "ok";
      if (["degraded", "missing", "unknown"].includes(status)) return "warn";
      return "bad";
    };

    const statusLabel = (status) => {
      if (!status) return "Unknown";
      return status.charAt(0).toUpperCase() + status.slice(1);
    };

    const state = (status, label = statusLabel(status)) => {
      const kind = stateKind(status);
      return `<span class="state ${kind}"><span class="dot"></span>${escapeHtml(label)}</span>`;
    };

    const metric = ({ title, status, label, rows }) => `
      <article class="metric" data-state="${stateKind(status)}">
        <h2>${escapeHtml(title)}</h2>
        ${state(status, label)}
        <dl>${rows.map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v ?? "--")}</dd>`).join("")}</dl>
      </article>
    `;

    function render(data) {
      const readyModels = data.models.filter((m) => m.status === "ready").length;
      const check = data.last_dev_check;
      els.lastUpdated.textContent = `Last updated: ${fmtTime(data.generated_at)}`;
      els.summary.innerHTML = [
        metric({
          title: "Gateway",
          status: data.gateway.status,
          label: "Healthy",
          rows: [["Uptime", fmtDuration(data.gateway.uptime_seconds)], ["Default", data.gateway.default_model_profile]],
        }),
        metric({
          title: "Ollama",
          status: data.ollama.status,
          label: data.ollama.status === "ok" ? "Connected" : "Unavailable",
          rows: [["Endpoint", data.ollama.base_url], ["Models", data.ollama.models_count]],
        }),
        metric({
          title: "Models",
          status: readyModels === data.models.length ? "ok" : "degraded",
          label: `${readyModels} / ${data.models.length} ready`,
          rows: [["Dev model", data.models.find((m) => m.alias === "dev")?.status ?? "unknown"]],
        }),
        metric({
          title: "End-to-end",
          status: check?.status ?? "unknown",
          label: check ? statusLabel(check.status) : "Not run",
          rows: [["Model", check?.model ?? "qwen3.5:0.8b"], ["Latency", check?.latency_ms ? `${check.latency_ms} ms` : "--"]],
        }),
      ].join("");

      els.modelsCount.textContent = `${readyModels} / ${data.models.length} ready`;
      els.models.innerHTML = data.models.map((model) => `
        <tr>
          <td class="code">${escapeHtml(model.alias)}</td>
          <td class="code">${escapeHtml(model.model)}</td>
          <td>${state(model.status)}</td>
          <td>${escapeHtml(model.size ?? "--")}</td>
          <td>${escapeHtml(model.details ?? "--")}</td>
        </tr>
      `).join("");

      const devModel = data.models.find((m) => m.alias === "dev");
      const checks = [
        ["Gateway (FastAPI)", "ok", "/health", "--"],
        ["Ollama", data.ollama.status, `${data.ollama.base_url}/api/tags`, data.ollama.latency_ms ? `${data.ollama.latency_ms} ms` : "--"],
        ["Dev model installed", devModel?.status ?? "unknown", devModel?.model ?? "qwen3.5:0.8b", "--"],
        ["Inference (dev model)", check?.status ?? "unknown", check?.model ?? "Run check to verify", check?.latency_ms ? `${check.latency_ms} ms` : "--"],
      ];
      els.checks.innerHTML = checks.map(([name, checkStatus, info, latency]) => `
        <tr>
          <td>${escapeHtml(name)}</td>
          <td>${state(checkStatus)}</td>
          <td class="code">${escapeHtml(info)}</td>
          <td>${escapeHtml(latency)}</td>
        </tr>
      `).join("");

      renderCheck(check);
      els.runtime.innerHTML = [
        ["Host", data.gateway.hostname],
        ["Listen", `${data.gateway.host}:${data.gateway.port}`],
        ["Whisper", data.gateway.default_whisper_model],
        ["TTS", data.gateway.chatterbox_model],
        ["Python", data.gateway.python],
        ["Platform", data.gateway.platform],
        ["API key auth", data.gateway.api_key_auth_enabled ? "enabled" : "disabled"],
        ["Arbitrary models", data.gateway.arbitrary_models_enabled ? "enabled" : "disabled"],
      ].map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`).join("");

      els.footer.innerHTML = [
        `Environment: ${data.gateway.api_key_auth_enabled ? "auth enabled" : "default auth disabled"}`,
        `Timeout: ${data.gateway.request_timeout_seconds}s`,
        `Request limit: ${data.gateway.max_request_body_bytes} bytes`,
        `Generated: ${fmtTime(data.generated_at)}`,
      ].map((item) => `<span>${escapeHtml(item)}</span>`).join("");
    }

    function renderCheck(check) {
      if (!check) {
        els.checkResult.innerHTML = `
          <div class="muted">No dev model check has run in this gateway process.</div>
        `;
        return;
      }
      els.checkResult.innerHTML = `
        <div class="row"><strong>Model</strong><span class="code">${escapeHtml(check.model)}</span></div>
        <div class="row"><strong>Status</strong><span>${state(check.status)}</span></div>
        <div class="row"><strong>Latency</strong><span>${escapeHtml(check.latency_ms ? `${check.latency_ms} ms` : "--")}</span></div>
        <div class="row"><strong>Tokens</strong><span>${escapeHtml((check.prompt_tokens ?? 0) + (check.completion_tokens ?? 0))}</span></div>
        <div class="row"><strong>Checked</strong><span>${escapeHtml(fmtTime(check.checked_at))}</span></div>
        <div class="row"><strong>Response</strong><span class="response-box">${escapeHtml(check.response ?? check.error ?? "--")}</span></div>
      `;
    }

    async function refreshStatus() {
      els.refresh.disabled = true;
      try {
        const response = await fetch("/status.json", { cache: "no-store" });
        if (!response.ok) throw new Error(`Status request returned HTTP ${response.status}`);
        render(await response.json());
      } catch (error) {
        els.summary.innerHTML = `<article class="metric error"><h2>Status unavailable</h2><p>${escapeHtml(error.message)}</p></article>`;
      } finally {
        els.refresh.disabled = false;
      }
    }

    async function runCheck() {
      els.runCheck.disabled = true;
      els.runCheck.textContent = "Running dev model check...";
      try {
        const response = await fetch("/status/check", { method: "POST" });
        const payload = await response.json();
        renderCheck(payload);
        await refreshStatus();
      } catch (error) {
        renderCheck({ status: "failed", model: "qwen3.5:0.8b", error: error.message });
      } finally {
        els.runCheck.disabled = false;
        els.runCheck.innerHTML = '<span aria-hidden="true">&#9654;</span> Run dev model check';
      }
    }

    els.refresh.addEventListener("click", refreshStatus);
    els.runCheck.addEventListener("click", runCheck);
    refreshStatus();
    setInterval(refreshStatus, 15000);
  </script>
</body>
</html>
"""
