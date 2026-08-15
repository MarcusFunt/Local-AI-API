#!/usr/bin/env python3
"""Run one serial public-evaluation round daily without overlapping the GPU."""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _lock(path: Path) -> object:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError("Another public evaluation round is already running.") from exc
    return handle


def _run_once(results_dir: Path) -> int:
    command = [sys.executable, "/workspace/scripts/eval_runner.py", "run", "--suite", "all", "--surface", "model"]
    model_result = subprocess.run(command, check=False).returncode
    agent_result = subprocess.run(command[:-1] + ["agent"], check=False).returncode
    status = "passed" if model_result == 0 and agent_result == 0 else "failed"
    marker = {
        "status": status,
        "finished_at_epoch": int(time.time()),
        "model_exit_code": model_result,
        "agent_exit_code": agent_result,
    }
    (results_dir / "latest-round.json").write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
    return 0 if status == "passed" else 1


def main() -> int:
    results_dir = Path(os.environ.get("EVAL_RESULTS_DIR", "/results"))
    interval = int(os.environ.get("EVAL_INTERVAL_SECONDS", "86400"))
    if interval < 60:
        raise SystemExit("EVAL_INTERVAL_SECONDS must be at least 60.")
    results_dir.mkdir(parents=True, exist_ok=True)
    lock = _lock(results_dir / ".scheduler.lock")
    try:
        while True:
            started = time.monotonic()
            _run_once(results_dir)
            # A long run never overlaps with its successor. If it used the full
            # interval, start the queued next round immediately after it ends.
            time.sleep(max(0, interval - (time.monotonic() - started)))
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
