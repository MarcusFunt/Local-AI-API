#!/usr/bin/env python3
"""Run repeatable, private quality evaluations without changing production."""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_CRITERIA = ("factual_correctness", "instruction_adherence", "source_support", "completeness", "safety")
_RUBRIC = (
    "Score the response from 0 to 4 for factual_correctness, instruction_adherence, "
    "source_support, completeness, and safety. Use source_support=4 only if every "
    "material document claim is cited when sources are available. Return JSON only with "
    "those five integer fields and a short rationale."
)


def _post_json(url: str, payload: dict[str, Any], timeout: int | None) -> dict[str, Any]:
    request = Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:  # nosec B310 -- explicit local endpoint
            result = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError(f"Request to {url} failed: {exc}") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"Request to {url} returned a non-object response.")
    return result


def _extract_content(payload: dict[str, Any], transport: str) -> str:
    if transport == "gateway":
        choices = payload.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message")
            if isinstance(message, dict):
                return str(message.get("content") or "")
    else:
        message = payload.get("message")
        if isinstance(message, dict):
            return str(message.get("content") or "")
    raise RuntimeError("Model response did not contain assistant content.")


def _model_answer(args: argparse.Namespace, case: dict[str, Any], seed: int) -> str:
    prompt = str(case["prompt"])
    use_rag = bool(case.get("requires_rag"))
    if args.transport == "gateway":
        payload: dict[str, Any] = {
            "mode": args.mode,
            "model": args.model,
            "messages": [{"role": "user", "content": prompt}],
            "use_rag": use_rag,
            "rag_document_id": case.get("rag_document_id"),
            "context_length": args.context_length,
            "seed": seed,
        }
        response = _post_json(args.base_url.rstrip("/") + "/v1/agent/completions", payload, args.timeout)
        return _extract_content(response, "gateway")

    payload = {
        "model": args.model,
        "stream": False,
        "think": True,
        "messages": [{"role": "user", "content": prompt}],
        "options": {"num_ctx": args.context_length, "num_predict": args.max_tokens, "seed": seed},
    }
    response = _post_json(args.base_url.rstrip("/") + "/api/chat", payload, args.timeout)
    return _extract_content(response, "ollama")


def _deterministic_checks(case: dict[str, Any], answer: str) -> dict[str, Any]:
    checks = case.get("checks", {})
    if not isinstance(checks, dict):
        raise ValueError(f"Case {case['id']} has non-object checks.")
    folded = answer.casefold()
    required = [str(value) for value in checks.get("must_include", [])]
    forbidden = [str(value) for value in checks.get("must_not_include", [])]
    missing = [value for value in required if value.casefold() not in folded]
    prohibited = [value for value in forbidden if value.casefold() in folded]
    maximum = checks.get("max_characters")
    if maximum is not None and (not isinstance(maximum, int) or maximum <= 0):
        raise ValueError(f"Case {case['id']} checks.max_characters must be a positive integer.")
    return {
        "passed": not missing and not prohibited and (maximum is None or len(answer) <= maximum),
        "missing_required": missing,
        "present_forbidden": prohibited,
        "length": len(answer),
        "max_characters": maximum,
    }


def _judge(
    args: argparse.Namespace,
    case: dict[str, Any],
    answer: str,
    seed: int,
    judge_model: str,
) -> dict[str, Any]:
    evaluation = {key: case.get(key) for key in ("id", "kind", "language", "criteria", "reference_answer", "checks")}
    prompt = "You are a strict answer-quality evaluator.\n" + _RUBRIC
    prompt += "\n\nCase metadata and checks:\n" + json.dumps(evaluation, ensure_ascii=False)
    prompt += "\n\nTask:\n" + str(case["prompt"]) + "\n\nResponse to score:\n" + answer[:8000]
    if args.transport == "gateway":
        response = _post_json(
            args.base_url.rstrip("/") + "/v1/chat/completions",
            {"model": judge_model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 400, "seed": seed},
            args.timeout,
        )
        content = _extract_content(response, "gateway")
    else:
        response = _post_json(
            args.base_url.rstrip("/") + "/api/chat",
            {"model": judge_model, "stream": False, "think": True, "messages": [{"role": "user", "content": prompt}], "options": {"num_ctx": args.context_length, "num_predict": 400, "seed": seed}},
            args.timeout,
        )
        content = _extract_content(response, "ollama")
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end < start:
        raise RuntimeError(f"Judge did not return JSON: {content[:200]!r}")
    score = json.loads(content[start : end + 1])
    if not isinstance(score, dict) or any(not isinstance(score.get(name), int) for name in _CRITERIA):
        raise RuntimeError("Judge response omitted one or more required integer scores.")
    if any(not 0 <= int(score[name]) <= 4 for name in _CRITERIA):
        raise RuntimeError("Judge returned a score outside 0..4.")
    return score


def _load_cases(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases, list) or not cases:
        raise ValueError("Benchmark cases must be a non-empty JSON cases array.")
    normalized = [case for case in cases if isinstance(case, dict) and case.get("id") and case.get("prompt")]
    if len(normalized) != len(cases) or len({str(case["id"]) for case in normalized}) != len(normalized):
        raise ValueError("Every benchmark case needs a unique id and prompt.")
    for case in normalized:
        if bool(case.get("requires_rag")) and not isinstance(case.get("rag_document_id"), str):
            raise ValueError(f"RAG case {case['id']} must use a fixed rag_document_id fixture.")
        _deterministic_checks(case, "")
    return normalized


def _mean_scores(scores: list[dict[str, int]]) -> dict[str, int]:
    """Average multiple local judge perspectives without changing report consumers."""
    return {
        name: round(statistics.mean(score[name] for score in scores))
        for name in _CRITERIA
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--transport", choices=("gateway", "ollama"), default="gateway")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--model", default="quality")
    parser.add_argument(
        "--judge-model",
        action="append",
        dest="judge_models",
        help="Local judge alias; specify twice for independent perspectives (defaults to agent and quality).",
    )
    parser.add_argument(
        "--mode",
        choices=("adaptive", "graph", "mixture_of_experts"),
        default="adaptive",
    )
    parser.add_argument("--context-length", type=int, default=8192)
    parser.add_argument("--max-tokens", type=int, default=1600)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20260813)
    parser.add_argument("--timeout", type=int, default=0, help="Seconds per HTTP call; 0 waits indefinitely.")
    args = parser.parse_args()
    if not 4096 <= args.context_length <= 32768:
        parser.error("--context-length must be between 4096 and 32768")
    if args.repeats < 5:
        parser.error("--repeats must be at least 5 for promotion-quality evidence")
    if args.timeout < 0:
        parser.error("--timeout cannot be negative")
    args.timeout = None if args.timeout == 0 else args.timeout
    args.judge_models = args.judge_models or ["agent", "quality"]
    if not 1 <= len(args.judge_models) <= 4:
        parser.error("Specify between one and four --judge-model values.")

    records: list[dict[str, Any]] = []
    for repeat in range(args.repeats):
        for case in _load_cases(args.cases):
            seed = args.seed + repeat
            started = time.monotonic()
            answer = _model_answer(args, case, seed)
            checks = _deterministic_checks(case, answer)
            judge_scores = [
                {"model": judge_model, "scores": _judge(args, case, answer, seed, judge_model)}
                for judge_model in args.judge_models
            ]
            score = _mean_scores([entry["scores"] for entry in judge_scores])
            records.append({
                "id": case["id"], "repeat": repeat, "seed": seed,
                "kind": case.get("kind", ""), "language": case.get("language", ""),
                "elapsed_seconds": round(time.monotonic() - started, 3), "answer": answer,
                "checks": checks, "scores": score, "judge_scores": judge_scores,
            })
            print(f"scored {case['id']} repeat={repeat + 1}/{args.repeats}", flush=True)

    means = {name: round(statistics.mean(record["scores"][name] for record in records), 3) for name in _CRITERIA}
    report = {
        "version": 2, "created_at_epoch": int(time.time()),
        "configuration": {"transport": args.transport, "base_url": args.base_url, "model": args.model, "judge_models": args.judge_models, "mode": args.mode, "context_length": args.context_length, "repeats": args.repeats, "seed": args.seed},
        "means": means, "overall_mean": round(statistics.mean(means.values()), 3), "records": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"overall_mean": report["overall_mean"], "means": means, "checks_passed": all(record["checks"]["passed"] for record in records)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
