"""Tests for the opt-in public evaluation boundary and promotion gate."""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.request import Request, urlopen

from evals.proxy import ProxyConfig, running_proxy

ROOT = Path(__file__).resolve().parents[1]


def _script_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=5) as response:  # nosec B310 -- loopback test server
        return json.loads(response.read().decode("utf-8"))


def test_agent_proxy_adapts_graph_completion_to_openai_chat():
    calls: list[tuple[str, dict[str, Any]]] = []

    class Gateway(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            pass

        def do_POST(self) -> None:  # noqa: N802
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
            calls.append((self.path, payload))
            body = json.dumps({"object": "agent.completion", "choices": [{"message": {"role": "assistant", "content": "answer"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    gateway = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    thread.start()
    try:
        config = ProxyConfig(f"http://127.0.0.1:{gateway.server_port}", "agent", 12288)
        with running_proxy(config) as base_url:
            response = _post(base_url + "/chat/completions", {"model": "quality", "messages": [{"role": "user", "content": "test"}], "max_tokens": 12})
        assert response["object"] == "chat.completion"
        assert calls == [("/v1/agent/completions", {"model": "quality", "messages": [{"role": "user", "content": "test"}], "max_tokens": 12, "mode": "graph", "stream": False, "context_length": 12288})]
    finally:
        gateway.shutdown()
        gateway.server_close()
        thread.join(timeout=5)


def test_model_proxy_preserves_openai_chat_payload():
    calls: list[dict[str, Any]] = []

    class Gateway(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            pass

        def do_POST(self) -> None:  # noqa: N802
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8")))
            body = json.dumps({"object": "chat.completion", "choices": [{"message": {"role": "assistant", "content": "answer"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    gateway = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    thread.start()
    try:
        with running_proxy(ProxyConfig(f"http://127.0.0.1:{gateway.server_port}", "model", 8192)) as base_url:
            _post(base_url + "/chat/completions", {"model": "quality", "messages": [{"role": "user", "content": "test"}], "seed": 7})
        assert calls == [{"model": "quality", "messages": [{"role": "user", "content": "test"}], "seed": 7}]
    finally:
        gateway.shutdown()
        gateway.server_close()
        thread.join(timeout=5)


def test_public_gate_rejects_required_metric_regression(tmp_path: Path):
    gate = _script_module("eval_public_gate_test", "eval_public_gate.py")
    baseline, candidate = tmp_path / "baseline", tmp_path / "candidate"
    for root, ifeval_score in ((baseline, 0.9), (candidate, 0.8)):
        for surface in ("model", "agent"):
            for suite, metrics in (
                ("ifeval", {"strict_accuracy": ifeval_score}),
                ("evalplus-humaneval", {"plus_pass_at_1": 0.5}),
                ("evalplus-mbpp", {"plus_pass_at_1": 0.5}),
            ):
                directory = root / surface / suite
                directory.mkdir(parents=True)
                (directory / "eval-report.json").write_text(json.dumps({"version": 1, "suite": suite, "surface": surface, "metrics": metrics}), encoding="utf-8")
    verdict = gate.compare(baseline, candidate, 0.01)
    assert not verdict["passed"]
    assert any("ifeval/model/strict_accuracy" in failure for failure in verdict["failures"])


def test_evalplus_sandbox_has_no_network_or_privileged_mount(monkeypatch, tmp_path: Path):
    runner = _script_module("eval_runner_test", "eval_runner.py")
    samples = tmp_path / "samples.jsonl"
    samples.write_text('{"task_id":"HumanEval/0","solution":"pass"}\n', encoding="utf-8")
    captured: dict[str, Any] = {}

    class Container:
        def wait(self):
            return {"StatusCode": 0}

        def logs(self, **_kwargs):
            return b"ok"

        def remove(self, **_kwargs):
            return None

    class Client:
        class containers:  # type: ignore[valid-type]
            @staticmethod
            def run(*_args, **kwargs):
                captured.update(kwargs)
                samples.with_name("samples_eval_results.json").write_text('{"pass_at_k":{"plus":{"pass@1":1}}}', encoding="utf-8")
                return Container()

    monkeypatch.setitem(sys.modules, "docker", SimpleNamespace(from_env=lambda: Client()))
    monkeypatch.setenv("EVAL_RESULTS_VOLUME", "test-results")
    result = runner._run_evalplus_sandbox(samples, "humaneval", tmp_path)
    assert result.name == "samples_eval_results.json"
    assert captured["network_mode"] == "none"
    assert captured["read_only"] is True
    assert captured["entrypoint"] == ["python", "-m", "evalplus.evaluate"]
    assert captured["cap_drop"] == ["ALL"]
    assert captured["security_opt"] == ["no-new-privileges"]
    assert captured["volumes"] == {"test-results": {"bind": "/results", "mode": "rw"}}


def test_evalplus_uses_sanitized_samples_not_raw_generation(tmp_path: Path):
    runner = _script_module("eval_runner_sample_test", "eval_runner.py")
    sanitized = tmp_path / "quality_openai_temp_0.0.jsonl"
    raw = tmp_path / "quality_openai_temp_0.0.raw.jsonl"
    sanitized.write_text('{"task_id":"HumanEval/0","solution":"pass"}\n', encoding="utf-8")
    raw.write_text('{"task_id":"HumanEval/0","solution":"untrusted raw output"}\n', encoding="utf-8")
    assert runner._latest_evalplus_samples(tmp_path) == sanitized


def test_livebench_summary_keeps_per_category_metrics(tmp_path: Path):
    runner = _script_module("eval_runner_livebench_test", "eval_runner.py")
    (tmp_path / "all_groups.csv").write_text(",average,math,reasoning\nlocal-ai,61.5,60,63\naverage,61.5,60,63\n", encoding="utf-8")
    metrics = runner._find_livebench_summary(tmp_path)
    assert metrics == {"all_groups.average": 61.5, "all_groups.math": 60.0, "all_groups.reasoning": 63.0}
