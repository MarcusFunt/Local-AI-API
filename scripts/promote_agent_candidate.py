#!/usr/bin/env python3
"""Verify and optionally fast-forward one fully gated agent-improvement candidate."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent_learning.promotion import PromotionController, PromotionError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True, help="Version-1 candidate manifest JSON.")
    parser.add_argument("--source", type=Path, default=Path.cwd(), help="Clean local main checkout.")
    parser.add_argument("--state-dir", type=Path, help="Ignored local audit and temporary-worktree directory.")
    parser.add_argument("--apply", action="store_true", help="Fast-forward local main after verification; never deploys or pushes.")
    args = parser.parse_args()
    controller = PromotionController(args.source, args.state_dir)
    try:
        result = controller.promote(args.candidate) if args.apply else controller.verify(args.candidate)
    except PromotionError as exc:
        parser.error(str(exc))
    print(json.dumps(result.__dict__, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
