#!/usr/bin/env python3
"""Run pinned public LLM evaluations against the model or agent surface.

This program is intended to run in the opt-in ``eval-runner`` Compose profile.
It never writes production configuration and it never runs generated code in the
runner container. EvalPlus execution happens in a child container with no
network, repository mount, Docker socket, or Linux capabilities.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evals.proxy import ProxyConfig, running_proxy

_LOCK_PATH = _ROOT / "evals" / "sources.lock.json"
_LIVEBENCH_BENCHES = (
    "live_bench/reasoning",
    "live_bench/math",
    "live_bench/language",
    "live_bench/data_analysis",
    "live_bench/instruction_following",
)
_SUITES = ("ifeval", "evalplus-humaneval", "evalplus-mbpp", "livebench-core")


def _read_lock(path: Path = _LOCK_PATH) -> dict[str, dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != 1 or not isinstance(payload.get("sources"), dict):
        raise ValueError(f"{path} is not a version-1 evaluation source lock.")
    sources = payload["sources"]
    if any(not isinstance(value, dict) or not isinstance(value.get("revision"), str) for value in sources.values()):
        raise ValueError(f"{path} has an invalid source entry.")
    return sources


def _lock_digest(sources: dict[str, dict[str, Any]]) -> str:
    rendered = json.dumps(sources, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)


def _source_path(cache_dir: Path, name: str) -> Path:
    return cache_dir / name


def sync_sources(cache_dir: Path, selected: tuple[str, ...] = ("ifeval", "livebench")) -> None:
    """Fetch only pinned source trees and verify their checked-out revisions."""
    sources = _read_lock()
    cache_dir.mkdir(parents=True, exist_ok=True)
    for name in selected:
        source = sources[name]
        target = _source_path(cache_dir, name)
        revision = str(source["revision"])
        if not target.exists():
            _run(["git", "clone", "--filter=blob:none", "--no-checkout", str(source["repository"]), str(target)])
        sparse_path = source.get("sparse_path")
        if isinstance(sparse_path, str) and sparse_path:
            _run(["git", "sparse-checkout", "init", "--cone"], cwd=target)
            _run(["git", "sparse-checkout", "set", sparse_path], cwd=target)
        _run(["git", "fetch", "--depth", "1", "origin", revision], cwd=target)
        _run(["git", "checkout", "--detach", "--force", "FETCH_HEAD"], cwd=target)
        actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=target, text=True).strip()
        if actual != revision:
            raise RuntimeError(f"Pinned source verification failed for {name}: expected {revision}, got {actual}.")


def _post_chat(base_url: str, model: str, prompt: str, seed: int, api_key: str) -> str:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "seed": seed,
        "max_tokens": 4096,
    }
    request = Request(base_url.rstrip("/") + "/chat/completions", data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urlopen(request, timeout=None) as response:  # nosec B310 -- loopback evaluation proxy
            body = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Evaluation generation request failed: {exc}") from exc
    choices = body.get("choices") if isinstance(body, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise RuntimeError("Evaluation generation response did not contain a choice.")
    message = choices[0].get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise RuntimeError("Evaluation generation response did not contain text content.")
    return message["content"]


def _strict_ifeval_accuracy(path: Path) -> float:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        raise RuntimeError("IFEval emitted no strict result rows.")
    passed = sum(bool(row.get("follow_all_instructions")) for row in rows)
    return passed / len(rows)


def _evalplus_metrics(path: Path) -> dict[str, float]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    scores = payload.get("pass_at_k") if isinstance(payload, dict) else None
    if not isinstance(scores, dict):
        raise RuntimeError("EvalPlus result did not contain pass_at_k.")
    metrics: dict[str, float] = {}
    for split in ("base", "plus"):
        value = scores.get(split)
        if isinstance(value, dict) and isinstance(value.get("pass@1"), (int, float)):
            metrics[f"{split}_pass_at_1"] = float(value["pass@1"])
    if "plus_pass_at_1" not in metrics:
        raise RuntimeError("EvalPlus result did not contain plus pass@1.")
    return metrics


def _latest_evalplus_samples(run_dir: Path) -> Path:
    candidates = [
        path
        for path in run_dir.rglob("*.jsonl")
        if "eval_results" not in path.name
        and "sanitized" not in path.name
        and not path.name.endswith(".raw.jsonl")
    ]
    if not candidates:
        raise RuntimeError("EvalPlus code generation did not produce samples.")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _run_evalplus_sandbox(samples: Path, dataset: str, results_dir: Path) -> Path:
    """Score samples only inside a least-privilege child container."""
    try:
        import docker
    except ImportError as exc:  # pragma: no cover - only the eval image installs docker SDK
        raise RuntimeError("The evaluation image needs the docker Python package.") from exc
    volume_name = os.environ.get("EVAL_RESULTS_VOLUME", "").strip()
    if not volume_name:
        raise RuntimeError("EVAL_RESULTS_VOLUME is required; refusing unsafe local code execution.")
    image = os.environ.get("EVAL_SANDBOX_IMAGE", "local-ai-api-evals:1.0.0")
    relative_samples = samples.relative_to(results_dir)
    client = docker.from_env()
    container = client.containers.run(
        image,
        command=["--dataset", dataset, "--samples", f"/results/{relative_samples.as_posix()}", "--parallel", "1"],
        entrypoint=["python", "-m", "evalplus.evaluate"],
        detach=True,
        network_mode="none",
        read_only=True,
        tmpfs={"/tmp": "rw,noexec,nosuid,size=256m"},
        volumes={volume_name: {"bind": "/results", "mode": "rw"}},
        cap_drop=["ALL"],
        security_opt=["no-new-privileges"],
        pids_limit=128,
        mem_limit="2g",
        nano_cpus=2_000_000_000,
        environment={"HOME": "/tmp", "XDG_CACHE_HOME": "/tmp"},
        remove=False,
    )
    try:
        result = container.wait()
        logs = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")
        if int(result.get("StatusCode", 1)) != 0:
            raise RuntimeError(f"EvalPlus sandbox failed: {logs[-4000:]}")
    finally:
        container.remove(force=True)
    result_path = samples.with_name(samples.name.replace(".jsonl", "_eval_results.json"))
    if not result_path.exists():
        raise RuntimeError("EvalPlus sandbox completed without an evaluation result file.")
    return result_path


def _find_livebench_summary(run_dir: Path) -> dict[str, float]:
    candidates = list(run_dir.rglob("all_groups.csv")) + list(run_dir.rglob("all_tasks.csv"))
    metrics: dict[str, float] = {}
    for path in candidates:
        with path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("model", "").strip().lower() == "average":
                    continue
                for key, value in row.items():
                    if key and value and key.lower() != "model":
                        try:
                            metrics[f"{path.stem}.{key.lower()}"] = float(value)
                        except ValueError:
                            continue
    if not metrics:
        raise RuntimeError("LiveBench completed without a parseable score CSV.")
    return metrics


def _write_report(run_dir: Path, suite: str, surface: str, model: str, context_length: int, seed: int, metrics: dict[str, float], artifacts: list[Path]) -> Path:
    sources = _read_lock()
    report = {
        "version": 1,
        "id": f"eval-{uuid.uuid4().hex}",
        "created_at_epoch": int(time.time()),
        "suite": suite,
        "surface": surface,
        "model": model,
        "context_length": context_length,
        "seed": seed,
        "source_lock_digest": _lock_digest(sources),
        "source_revisions": {name: value["revision"] for name, value in sources.items() if name in {"ifeval", "evalplus", "livebench"}},
        "metrics": metrics,
        "artifacts": [str(path.relative_to(run_dir)) for path in artifacts],
    }
    output = run_dir / "eval-report.json"
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


def _run_ifeval(run_dir: Path, cache_dir: Path, proxy_url: str, args: argparse.Namespace) -> Path:
    source = _source_path(cache_dir, "ifeval") / "instruction_following_eval"
    input_path = source / "data" / "input_data.jsonl"
    if not input_path.exists():
        raise RuntimeError("IFEval source is missing; run `sync` first.")
    responses_path = run_dir / "responses.jsonl"
    lines: list[str] = []
    for raw in input_path.read_text(encoding="utf-8").splitlines():
        item = json.loads(raw)
        prompt = item.get("prompt")
        if not isinstance(prompt, str):
            raise RuntimeError("IFEval input has a non-text prompt.")
        lines.append(json.dumps({"prompt": prompt, "response": _post_chat(proxy_url, args.model, prompt, args.seed, "")}))
    responses_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    output_dir = run_dir / "official-results"
    output_dir.mkdir()
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(_source_path(cache_dir, "ifeval"))
    _run([sys.executable, "-m", "instruction_following_eval.evaluation_main", f"--input_data={input_path}", f"--input_response_data={responses_path}", f"--output_dir={output_dir}"], cwd=_source_path(cache_dir, "ifeval"), env=environment)
    strict = output_dir / "eval_results_strict.jsonl"
    return _write_report(run_dir, "ifeval", args.surface, args.model, args.context_length, args.seed, {"strict_accuracy": _strict_ifeval_accuracy(strict)}, [responses_path, strict, output_dir / "eval_results_loose.jsonl"])


def _run_evalplus(run_dir: Path, proxy_url: str, args: argparse.Namespace, dataset: str) -> Path:
    environment = os.environ.copy()
    environment["OPENAI_API_KEY"] = "local-evaluation-proxy"
    # EvalPlus uses Fire positional arguments for model and dataset. Keep those
    # positional (and use its documented underscore flag spelling) so this
    # invocation remains stable across Fire's optional hyphen normalization.
    _run(["evalplus.codegen", args.model, dataset, "--greedy", "--root", str(run_dir), "--backend", "openai", "--base_url", proxy_url], env=environment)
    samples = _latest_evalplus_samples(run_dir)
    result = _run_evalplus_sandbox(samples, dataset, args.results_dir)
    return _write_report(run_dir, f"evalplus-{dataset}", args.surface, args.model, args.context_length, args.seed, _evalplus_metrics(result), [samples, result])


def _run_livebench(run_dir: Path, cache_dir: Path, proxy_url: str, args: argparse.Namespace) -> Path:
    source = _source_path(cache_dir, "livebench") / "livebench" / "run_livebench.py"
    if not source.exists():
        raise RuntimeError("LiveBench source is missing; run `sync` first.")
    environment = os.environ.copy()
    environment["OPENAI_API_KEY"] = "local-evaluation-proxy"
    source_dir = source.parent
    release = str(_read_lock()["livebench"]["release"])
    # Keep every run distinct in the shared source cache. The official runner
    # derives answer and judgment paths from the display name.
    display_name = f"local-ai-{args.surface}-{run_dir.parents[1].name}".lower()
    _run(
        [
            sys.executable,
            str(source),
            "--model", args.model,
            "--model-display-name", display_name,
            "--bench-name", *_LIVEBENCH_BENCHES,
            "--mode", "single",
            "--parallel-requests", "1",
            "--api-base", proxy_url,
            "--api-key", "local-evaluation-proxy",
            "--model-provider-override", "openai",
            "--livebench-release-option", release,
        ],
        cwd=source_dir,
        env=environment,
    )
    _run(
        [
            sys.executable,
            str(source_dir / "show_livebench_result.py"),
            "--bench-name", *_LIVEBENCH_BENCHES,
            "--model-list", display_name,
            "--livebench-release-option", release,
            "--show-average-row",
            "--ignore-missing-judgments",
        ],
        cwd=source_dir,
        env=environment,
    )
    artifacts: list[Path] = []
    for name in ("all_groups.csv", "all_tasks.csv"):
        generated = source_dir / name
        if generated.exists():
            target = run_dir / name
            shutil.copy2(generated, target)
            artifacts.append(target)
    return _write_report(run_dir, "livebench-core", args.surface, args.model, args.context_length, args.seed, _find_livebench_summary(run_dir), artifacts)


@contextmanager
def _proxy(args: argparse.Namespace) -> Iterator[str]:
    config = ProxyConfig(args.gateway_url, args.surface, args.context_length, args.gateway_api_key)
    with running_proxy(config) as proxy_url:
        yield proxy_url


def run_suite(args: argparse.Namespace, suite: str) -> Path:
    if suite not in _SUITES:
        raise ValueError(f"Unsupported suite {suite!r}.")
    selected_sources = ("ifeval",) if suite == "ifeval" else ("livebench",) if suite == "livebench-core" else ()
    if selected_sources:
        sync_sources(args.cache_dir, selected_sources)
    run_dir = args.results_dir / time.strftime("%Y%m%d-%H%M%S") / args.surface / suite
    run_dir.mkdir(parents=True, exist_ok=False)
    with _proxy(args) as proxy_url:
        if suite == "ifeval":
            return _run_ifeval(run_dir, args.cache_dir, proxy_url, args)
        if suite == "evalplus-humaneval":
            return _run_evalplus(run_dir, proxy_url, args, "humaneval")
        if suite == "evalplus-mbpp":
            return _run_evalplus(run_dir, proxy_url, args, "mbpp")
        return _run_livebench(run_dir, args.cache_dir, proxy_url, args)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("sync", "run"))
    parser.add_argument("--suite", choices=(*_SUITES, "all"), default="all")
    parser.add_argument("--surface", choices=("model", "agent"), default="model")
    parser.add_argument("--model", default="quality")
    parser.add_argument("--context-length", type=int, default=8192)
    parser.add_argument("--seed", type=int, default=20260813)
    parser.add_argument("--gateway-url", default=os.environ.get("EVAL_GATEWAY_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--gateway-api-key", default=os.environ.get("GATEWAY_API_KEY", ""))
    parser.add_argument("--cache-dir", type=Path, default=Path(os.environ.get("EVAL_CACHE_DIR", ".local/eval-cache")))
    parser.add_argument("--results-dir", type=Path, default=Path(os.environ.get("EVAL_RESULTS_DIR", ".local/eval-results")))
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not 4096 <= args.context_length <= 32768:
        raise SystemExit("--context-length must be between 4096 and 32768.")
    if args.command == "sync":
        sync_sources(args.cache_dir)
        return 0
    suites = _SUITES if args.suite == "all" else (args.suite,)
    for suite in suites:
        report = run_suite(args, suite)
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
