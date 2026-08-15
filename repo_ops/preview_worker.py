"""Unnetworked worker that previews one disposable workspace at a time.

The worker is deliberately driven by a file queue on the workspace volume rather
than an HTTP endpoint.  It has no network, source checkout, archives, or Agent
Zero state mounted by Compose.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(os.environ.get("REPO_OPS_WORKSPACES_ROOT", "/workspaces"))
JOBS_ROOT = Path(os.environ.get("REPO_OPS_JOBS_ROOT", "/jobs"))
JOBS = JOBS_ROOT / "preview-jobs"
RESULTS = JOBS_ROOT / "preview-results"
VERIFICATION_JOBS = JOBS_ROOT / "verification-jobs"
VERIFICATION_RESULTS = JOBS_ROOT / "verification-results"

_TASK_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}$")
_ALLOWED_CHECKS = {
    "unit",
    "compile",
    "compose_config",
    "status_ui_tests",
    "repo_ops_tests",
    "dependency_health",
}
_MAX_OUTPUT_CHARS = 12_000


def _worker_timeout() -> int:
    try:
        return min(600, max(1, int(os.environ.get("REPO_OPS_WORKER_TIMEOUT_SECONDS", "600"))))
    except ValueError:
        return 600


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _valid_task_id(task_id: str) -> bool:
    return bool(_TASK_ID.fullmatch(task_id))


def _verification_command(preset: str) -> list[str]:
    python = os.environ.get("REPO_OPS_VERIFICATION_PYTHON", sys.executable)
    if preset == "unit":
        return [python, "-m", "pytest", "tests/", "-v", "--cov=gateway"]
    if preset == "compile":
        return [python, "-m", "compileall", "gateway"]
    if preset == "compose_config":
        return ["docker-compose", "-f", "compose.yaml", "-f", "compose.agent-zero.yaml", "config"]
    if preset == "status_ui_tests":
        return [python, "-m", "pytest", "tests/test_status_ui.py", "-v"]
    if preset == "repo_ops_tests":
        return [python, "-m", "pytest", "tests/test_repo_ops.py", "tests/test_repo_ops_deployment.py", "-v"]
    if preset == "dependency_health":
        return [python, "-m", "pip", "check"]
    raise ValueError("Unknown verification preset.")


def _copy_workspace_for_verification(task_id: str) -> tuple[Path, tempfile.TemporaryDirectory[str]]:
    source = ROOT / task_id
    if not source.is_dir():
        raise ValueError("Workspace does not exist.")
    if not _valid_task_id(task_id):
        raise ValueError("Invalid workspace task ID.")
    if any(path.is_symlink() for path in source.rglob("*")):
        raise ValueError("Verification rejects workspaces containing symbolic links.")
    scratch = tempfile.TemporaryDirectory(prefix=f"repo-ops-{task_id}-")
    copied = Path(scratch.name) / "workspace"
    shutil.copytree(source, copied, ignore=shutil.ignore_patterns(".pytest_cache", "__pycache__", ".coverage"))
    return copied, scratch


def _run_verification(job: dict[str, object]) -> dict[str, object]:
    job_id = job.get("job_id")
    task_id = job.get("task_id")
    preset = job.get("preset")
    if not all(isinstance(value, str) for value in (job_id, task_id, preset)):
        raise ValueError("Verification job is malformed.")
    if not _valid_task_id(task_id) or preset not in _ALLOWED_CHECKS:
        raise ValueError("Verification job is not allowed.")

    workspace, scratch = _copy_workspace_for_verification(task_id)
    environment = {
        "HOME": "/tmp/repoops",
        "NO_PROXY": "*",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": "/app",
        "PYTHONUNBUFFERED": "1",
        "PYTEST_ADDOPTS": "-p no:cacheprovider",
        "COVERAGE_FILE": "/tmp/.coverage",
    }
    try:
        try:
            result = subprocess.run(
                _verification_command(preset),
                cwd=workspace,
                env=environment,
                capture_output=True,
                text=True,
                timeout=_worker_timeout(),
                check=False,
            )
            output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
            returncode = result.returncode
        except subprocess.TimeoutExpired as exc:
            output = "\n".join(
                part.decode("utf-8", errors="replace") if isinstance(part, bytes) else part
                for part in (exc.stdout, exc.stderr)
                if part
            )
            output = (output + "\nVerification timed out.").strip()
            returncode = 124
        return {
            "job_id": job_id,
            "task_id": task_id,
            "preset": preset,
            "passed": returncode == 0,
            "returncode": returncode,
            "output": output[-_MAX_OUTPUT_CHARS:],
            "finished_at": _now(),
        }
    finally:
        scratch.cleanup()


def _run(task_id: str) -> dict[str, object]:
    if not _valid_task_id(task_id):
        return {"status": "failed", "error": "Invalid workspace task ID.", "finished_at": _now()}
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
        artifact_dir = RESULTS / f"{task_id}-visual"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "repo_ops.ui_audit",
                "--url",
                f"http://127.0.0.1:{port}/status",
                "--screenshot",
                str(screenshot),
                "--artifact-dir",
                str(artifact_dir),
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
                "visual_artifacts": str(artifact_dir) if artifact_dir.is_dir() else None,
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
    VERIFICATION_JOBS.mkdir(parents=True, exist_ok=True)
    VERIFICATION_RESULTS.mkdir(parents=True, exist_ok=True)
    while True:
        for job in sorted(VERIFICATION_JOBS.glob("*.json")):
            try:
                payload = json.loads(job.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("Verification job is malformed.")
                result = _run_verification(payload)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                result = {
                    "job_id": job.stem,
                    "task_id": "",
                    "preset": "",
                    "passed": False,
                    "returncode": 1,
                    "output": str(exc),
                    "finished_at": _now(),
                }
            (VERIFICATION_RESULTS / f"{job.stem}.json").write_text(
                json.dumps(result, indent=2) + "\n", encoding="utf-8"
            )
            job.unlink(missing_ok=True)
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
