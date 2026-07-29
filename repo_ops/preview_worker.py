"""Unnetworked worker that previews one disposable workspace at a time.

The worker is deliberately driven by a file queue on the workspace volume rather
than an HTTP endpoint.  It has no network, source checkout, archives, or Agent
Zero state mounted by Compose.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(os.environ.get("REPO_OPS_WORKSPACES_ROOT", "/workspaces"))
JOBS = ROOT / ".preview-jobs"
RESULTS = ROOT / ".preview-results"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run(task_id: str) -> dict[str, object]:
    workspace = ROOT / task_id
    if not workspace.is_dir():
        return {"status": "failed", "error": "Workspace does not exist.", "finished_at": _now()}
    port = _free_port()
    environment = {
        "HOME": "/home/repoops",
        "NO_PROXY": "*",
        "PLAYWRIGHT_BROWSERS_PATH": "/ms-playwright",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": "/app",
        "PYTHONUNBUFFERED": "1",
    }
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "gateway.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=workspace,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(1.5)
        screenshot = RESULTS / f"{task_id}.png"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "repo_ops.ui_audit",
                "--url",
                f"http://127.0.0.1:{port}/status",
                "--screenshot",
                str(screenshot),
            ],
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        payload: dict[str, object] = json.loads(result.stdout) if result.stdout else {}
        payload.update(
            {
                "status": "passed" if result.returncode == 0 else "failed",
                "finished_at": _now(),
                "screenshot": str(screenshot) if screenshot.is_file() else None,
            }
        )
        if result.returncode:
            payload["error"] = result.stderr[-2000:] or "Preview audit failed."
        return payload
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return {"status": "failed", "error": str(exc), "finished_at": _now()}
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


def main() -> None:
    JOBS.mkdir(parents=True, exist_ok=True)
    RESULTS.mkdir(parents=True, exist_ok=True)
    while True:
        for job in sorted(JOBS.glob("*.json")):
            try:
                data = json.loads(job.read_text(encoding="utf-8"))
                task_id = str(data["task_id"])
                result = _run(task_id)
            except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
                task_id = job.stem
                result = {"status": "failed", "error": str(exc), "finished_at": _now()}
            (RESULTS / f"{task_id}.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            job.unlink(missing_ok=True)
        time.sleep(1)


if __name__ == "__main__":
    main()
