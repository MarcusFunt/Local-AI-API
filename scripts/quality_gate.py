#!/usr/bin/env python3
"""Require repeatable, blinded evidence before promoting a quality candidate."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

_CRITERIA = ("factual_correctness", "instruction_adherence", "source_support", "completeness", "safety")


def _report(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 2 or not isinstance(data.get("means"), dict):
        raise ValueError(f"{path} is not a version-2 quality benchmark report.")
    if any(name not in data["means"] for name in _CRITERIA) or not isinstance(data.get("records"), list):
        raise ValueError(f"{path} lacks rubric means or records.")
    return data


def _records_by_key(report: dict[str, Any]) -> dict[tuple[str, int, int], dict[str, Any]]:
    records: dict[tuple[str, int, int], dict[str, Any]] = {}
    for record in report["records"]:
        if not isinstance(record, dict):
            raise ValueError("Benchmark report contains an invalid record.")
        key = (str(record.get("id")), int(record.get("repeat", -1)), int(record.get("seed", -1)))
        if key in records:
            raise ValueError(f"Benchmark report has duplicate record {key}.")
        records[key] = record
    return records


def _records_digest(report: dict[str, Any]) -> str:
    payload = json.dumps(report["records"], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _human_result(key_path: Path, review_path: Path, baseline: dict[str, Any], candidate: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    key = json.loads(key_path.read_text(encoding="utf-8"))
    review = json.loads(review_path.read_text(encoding="utf-8"))
    if key.get("version") != 1 or review.get("version") != 1:
        raise ValueError("Blind key and human review must both be version 1.")
    if key.get("baseline_records_digest") != _records_digest(baseline) or key.get("candidate_records_digest") != _records_digest(candidate):
        raise ValueError("Blind key does not match the baseline and candidate benchmark records.")
    candidate_label = key.get("candidate_label")
    pairs = key.get("pairs")
    winners = review.get("winners")
    if candidate_label not in {"A", "B"} or not isinstance(pairs, list) or not isinstance(winners, dict):
        raise ValueError("Blind review artifacts are malformed.")
    expected = {str(pair["id"]) for pair in pairs if isinstance(pair, dict) and pair.get("id")}
    if set(winners) != expected:
        raise ValueError("Human review must score every blinded pair exactly once.")
    candidate_wins = sum(winners[pair_id] == candidate_label for pair_id in expected)
    baseline_wins = sum(winners[pair_id] in {"A", "B"} and winners[pair_id] != candidate_label for pair_id in expected)
    return candidate_wins > baseline_wins, {"candidate_wins": candidate_wins, "baseline_wins": baseline_wins, "ties": len(expected) - candidate_wins - baseline_wins}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--blind-key", type=Path, required=True)
    parser.add_argument("--human-review", type=Path, required=True)
    parser.add_argument("--minimum-overall-improvement", type=float, default=0.05)
    parser.add_argument("--allowed-per-criterion-regression", type=float, default=0.0)
    args = parser.parse_args()

    baseline, candidate = _report(args.baseline), _report(args.candidate)
    baseline_records, candidate_records = _records_by_key(baseline), _records_by_key(candidate)
    if set(baseline_records) != set(candidate_records):
        raise ValueError("Baseline and candidate must cover identical case/repeat/seed records.")
    regressions = {name: round(float(candidate["means"][name]) - float(baseline["means"][name]), 3) for name in _CRITERIA if float(candidate["means"][name]) < float(baseline["means"][name]) - args.allowed_per_criterion_regression}
    failed_checks = [key for key, record in candidate_records.items() if not record.get("checks", {}).get("passed")]
    paired_regressions = [{"record": key, "criterion": name} for key in baseline_records for name in _CRITERIA if int(candidate_records[key]["scores"][name]) < int(baseline_records[key]["scores"][name]) - args.allowed_per_criterion_regression]
    human_passed, human_summary = _human_result(args.blind_key, args.human_review, baseline, candidate)
    overall_delta = round(float(candidate["overall_mean"]) - float(baseline["overall_mean"]), 3)
    passed = overall_delta >= args.minimum_overall_improvement and not regressions and not failed_checks and not paired_regressions and human_passed
    verdict = {"passed": passed, "overall_delta": overall_delta, "regressions": regressions, "failed_checks": failed_checks, "paired_regressions": paired_regressions, "human_review": human_summary}
    print(json.dumps(verdict, indent=2))
    if not passed:
        print("Candidate is not promotable; retain the current production quality configuration.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
