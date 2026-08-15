"""Expose either Local AI API evaluation surface as OpenAI-compatible chat."""
from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class ProxyConfig:
    gateway_url: str
    surface: str
    context_length: int
    api_key: str = ""


def _post_json(url: str, payload: dict[str, Any], api_key: str) -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urlopen(request, timeout=None) as response:  # nosec B310 -- configured local gateway
            body = json.loads(response.read().decode("utf-8"))
            return response.status, body
    except HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = {"error": {"message": str(exc), "type": "upstream_error", "code": "gateway_error"}}
        return exc.code, body
    except (URLError, TimeoutError) as exc:
        return 502, {"error": {"message": f"Gateway unavailable: {exc}", "type": "upstream_error", "code": "gateway_unavailable"}}


def _agent_payload(payload: dict[str, Any], context_length: int) -> dict[str, Any]:
    allowed = {
        "model", "messages", "temperature", "top_p", "max_tokens", "max_completion_tokens",
        "stop", "seed", "use_rag", "rag_document_id",
    }
    agent_payload = {key: value for key, value in payload.items() if key in allowed}
    agent_payload["mode"] = "graph"
    agent_payload["stream"] = False
    agent_payload["context_length"] = context_length
    return agent_payload


def _handler(config: ProxyConfig) -> type[BaseHTTPRequestHandler]:
    class GatewayProxyHandler(BaseHTTPRequestHandler):
        server_version = "LocalAIEvalProxy/1.0"

        def log_message(self, _format: str, *_args: object) -> None:
            """Keep evaluator prompts and responses out of logs."""

        def _write(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler hook
            if self.path == "/health":
                self._write(200, {"status": "ok", "surface": config.surface})
            else:
                self._write(404, {"error": {"message": "Not found", "type": "invalid_request_error", "code": "not_found"}})

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler hook
            if self.path.rstrip("/") != "/v1/chat/completions":
                self._write(404, {"error": {"message": "Not found", "type": "invalid_request_error", "code": "not_found"}})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
                self._write(400, {"error": {"message": "Invalid JSON", "type": "invalid_request_error", "code": "invalid_json"}})
                return
            if not isinstance(payload, dict) or payload.get("stream"):
                self._write(422, {"error": {"message": "The evaluation proxy supports non-streaming chat only.", "type": "invalid_request_error", "code": "stream_unsupported"}})
                return
            endpoint = "/v1/chat/completions"
            if config.surface == "agent":
                endpoint = "/v1/agent/completions"
                payload = _agent_payload(payload, config.context_length)
            status, response = _post_json(config.gateway_url.rstrip("/") + endpoint, payload, config.api_key)
            if config.surface == "agent" and status < 400 and response.get("object") == "agent.completion":
                response["object"] = "chat.completion"
            self._write(status, response)

    return GatewayProxyHandler


@contextmanager
def running_proxy(config: ProxyConfig, host: str = "127.0.0.1", port: int = 0) -> Iterator[str]:
    """Run a loopback-only adapter and yield its OpenAI-compatible base URL."""
    server = ThreadingHTTPServer((host, port), _handler(config))
    thread = threading.Thread(target=server.serve_forever, name="local-ai-eval-proxy", daemon=True)
    thread.start()
    try:
        yield f"http://{host}:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
