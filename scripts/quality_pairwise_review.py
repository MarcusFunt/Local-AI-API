#!/usr/bin/env python3
"""Create a blinded pairwise-review packet from two quality benchmark reports."""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
from pathlib import Path
from typing import Any


def _report(path: Path) -> dict[str, Any]:
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("version") != 2 or not isinstance(report.get("records"), list):
        raise ValueError(f"{path} is not a version-2 quality benchmark report.")
    return report


def _records(report: dict[str, Any]) -> dict[tuple[str, int, int], dict[str, Any]]:
    return {(str(record["id"]), int(record["repeat"]), int(record["seed"])): record for record in report["records"]}


def _digest(report: dict[str, Any]) -> str:
    payload = json.dumps(report["records"], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--blind-output", type=Path, required=True)
    parser.add_argument("--key-output", type=Path, required=True)
    parser.add_argument("--review-template", type=Path, required=True)
    args = parser.parse_args()
    baseline_report, candidate_report = _report(args.baseline), _report(args.candidate)
    baseline, candidate = _records(baseline_report), _records(candidate_report)
    if set(baseline) != set(candidate):
        raise ValueError("Baseline and candidate must cover identical case/repeat/seed records.")
    candidate_label = secrets.choice(("A", "B"))
    baseline_label = "B" if candidate_label == "A" else "A"
    pairs: list[dict[str, Any]] = []
    for case_id, repeat, seed in sorted(baseline):
        pair_id = f"{case_id}:repeat-{repeat}:seed-{seed}"
        pairs.append({"id": pair_id, "task_id": case_id, "repeat": repeat, "seed": seed, "answers": {baseline_label: baseline[(case_id, repeat, seed)]["answer"], candidate_label: candidate[(case_id, repeat, seed)]["answer"]}})
    for path in (args.blind_output, args.key_output, args.review_template):
        path.parent.mkdir(parents=True, exist_ok=True)
    args.blind_output.write_text(json.dumps({"version": 1, "pairs": pairs}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    args.key_output.write_text(json.dumps({"version": 1, "candidate_label": candidate_label, "baseline_records_digest": _digest(baseline_report), "candidate_records_digest": _digest(candidate_report), "pairs": [{"id": pair["id"]} for pair in pairs]}, indent=2) + "\n", encoding="utf-8")
    args.review_template.write_text(json.dumps({"version": 1, "reviewer": "", "winners": {pair["id"]: "tie" for pair in pairs}}, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(pairs)} blinded pairs. Keep {args.key_output} private until review is complete.")


if __name__ == "__main__":
    main()
