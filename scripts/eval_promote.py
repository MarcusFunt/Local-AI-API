#!/usr/bin/env python3
"""Require both the private blind gate and public non-regression gate."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-baseline", type=Path, required=True)
    parser.add_argument("--private-candidate", type=Path, required=True)
    parser.add_argument("--blind-key", type=Path, required=True)
    parser.add_argument("--human-review", type=Path, required=True)
    parser.add_argument("--public-baseline", type=Path, required=True)
    parser.add_argument("--public-candidate", type=Path, required=True)
    args = parser.parse_args()
    commands = (
        [sys.executable, "scripts/quality_gate.py", "--baseline", str(args.private_baseline), "--candidate", str(args.private_candidate), "--blind-key", str(args.blind_key), "--human-review", str(args.human_review)],
        [sys.executable, "scripts/eval_public_gate.py", "--baseline", str(args.public_baseline), "--candidate", str(args.public_candidate)],
    )
    for command in commands:
        if subprocess.run(command, check=False).returncode != 0:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
