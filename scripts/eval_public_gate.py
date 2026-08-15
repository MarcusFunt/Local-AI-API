#!/usr/bin/env python3
"""Reject a public-evaluation candidate that regresses a pinned core suite."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_REQUIRED = {
    "ifeval": ("strict_accuracy",),
    "evalplus-humaneval": ("plus_pass_at_1",),
    "evalplus-mbpp": ("plus_pass_at_1",),
}


def _reports(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    reports: dict[tuple[str, str], dict[str, Any]] = {}
    for report_path in path.rglob("eval-report.json"):
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("version") != 1 or not isinstance(report.get("metrics"), dict):
            raise ValueError(f"{report_path} is not a version-1 public evaluation report.")
        key = (str(report.get("suite")), str(report.get("surface")))
        if key in reports:
            raise ValueError(f"{path} contains multiple reports for {key}; pass one round directory only.")
        reports[key] = report
    return reports


def compare(baseline_dir: Path, candidate_dir: Path, livebench_tolerance: float) -> dict[str, Any]:
    baseline, candidate = _reports(baseline_dir), _reports(candidate_dir)
    failures: list[str] = []
    deltas: dict[str, float] = {}
    for surface in ("model", "agent"):
        for suite, names in _REQUIRED.items():
            key = (suite, surface)
            if key not in baseline or key not in candidate:
                failures.append(f"missing required {suite} report for {surface}")
                continue
            for name in names:
                before = baseline[key]["metrics"].get(name)
                after = candidate[key]["metrics"].get(name)
                if not isinstance(before, (int, float)) or not isinstance(after, (int, float)):
                    failures.append(f"missing {name} for {suite}/{surface}")
                    continue
                delta = float(after) - float(before)
                deltas[f"{suite}/{surface}/{name}"] = delta
                if delta < 0:
                    failures.append(f"regression {suite}/{surface}/{name}: {delta:.4f}")
        key = ("livebench-core", surface)
        if key in baseline and key in candidate:
            for name, before in baseline[key]["metrics"].items():
                after = candidate[key]["metrics"].get(name)
                if isinstance(before, (int, float)) and isinstance(after, (int, float)):
                    delta = float(after) - float(before)
                    deltas[f"livebench-core/{surface}/{name}"] = delta
                    if delta < -livebench_tolerance:
                        failures.append(f"LiveBench regression {surface}/{name}: {delta:.4f}")
    return {"passed": not failures, "failures": failures, "deltas": deltas}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--livebench-tolerance", type=float, default=0.01)
    args = parser.parse_args()
    if args.livebench_tolerance < 0:
        parser.error("--livebench-tolerance cannot be negative")
    verdict = compare(args.baseline, args.candidate, args.livebench_tolerance)
    print(json.dumps(verdict, indent=2, sort_keys=True))
    if not verdict["passed"]:
        print("Candidate is not promotable; retain the current production quality configuration.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
