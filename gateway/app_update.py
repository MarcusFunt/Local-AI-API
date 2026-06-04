from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_PULL_TIMEOUT_SECONDS = 180
_MAX_OUTPUT_CHARS = 12_000
_UPDATE_LOCK = asyncio.Lock()
_LAST_UPDATE: dict[str, Any] | None = None


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _iso_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((time.monotonic() - started_at) * 1000))


def _trim_output(value: str) -> str:
    value = value.strip()
    if len(value) <= _MAX_OUTPUT_CHARS:
        return value
    return value[-_MAX_OUTPUT_CHARS:]


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _run_git(args: list[str], root: Path, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        env=_git_env(),
        text=True,
        timeout=timeout,
    )


def _git_output(result: subprocess.CompletedProcess[str]) -> str:
    return _trim_output("\n".join(part for part in (result.stdout, result.stderr) if part))


def _availability(root: Path) -> dict[str, Any]:
    if shutil.which("git") is None:
        return {
            "available": False,
            "reason": "git executable was not found.",
        }

    try:
        result = _run_git(["rev-parse", "--show-toplevel"], root)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "available": False,
            "reason": f"Could not inspect Git checkout: {exc}",
        }

    if result.returncode != 0:
        return {
            "available": False,
            "reason": "Gateway is not running from a Git checkout.",
        }

    return {
        "available": True,
        "root": result.stdout.strip() or str(root),
    }


def _read_git_state(root: Path) -> dict[str, Any]:
    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], root)
    head = _run_git(["rev-parse", "--short", "HEAD"], root)
    upstream = _run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], root)
    status = _run_git(["status", "--short"], root)

    return {
        "branch": branch.stdout.strip() if branch.returncode == 0 else "unknown",
        "head": head.stdout.strip() if head.returncode == 0 else "unknown",
        "upstream": upstream.stdout.strip() if upstream.returncode == 0 else "",
        "dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
    }


async def get_repo_update_status() -> dict[str, Any]:
    root = _repo_root()
    availability = await asyncio.to_thread(_availability, root)
    payload: dict[str, Any] = {
        "status": "idle",
        "available": availability["available"],
        "last_update": _LAST_UPDATE,
    }
    if not availability["available"]:
        payload["reason"] = availability["reason"]
        return payload

    payload["root"] = availability["root"]
    payload.update(await asyncio.to_thread(_read_git_state, root))
    if _UPDATE_LOCK.locked():
        payload["status"] = "running"
    return payload


async def run_repo_update() -> dict[str, Any]:
    global _LAST_UPDATE

    if _UPDATE_LOCK.locked():
        return {
            "status": "running",
            "started_at": _LAST_UPDATE.get("started_at") if _LAST_UPDATE else None,
            "message": "Repository update is already running.",
        }

    async with _UPDATE_LOCK:
        root = _repo_root()
        started_at = time.monotonic()
        _LAST_UPDATE = {
            "status": "running",
            "started_at": _iso_now(),
        }

        availability = await asyncio.to_thread(_availability, root)
        if not availability["available"]:
            _LAST_UPDATE = {
                "status": "unavailable",
                "started_at": _LAST_UPDATE["started_at"],
                "finished_at": _iso_now(),
                "latency_ms": _elapsed_ms(started_at),
                "error": availability["reason"],
            }
            return _LAST_UPDATE

        before = await asyncio.to_thread(_read_git_state, root)
        try:
            result = await asyncio.to_thread(
                _run_git,
                ["pull", "--ff-only"],
                root,
                _PULL_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            _LAST_UPDATE = {
                "status": "failed",
                "started_at": _LAST_UPDATE["started_at"],
                "finished_at": _iso_now(),
                "latency_ms": _elapsed_ms(started_at),
                "branch": before["branch"],
                "head_before": before["head"],
                "error": f"git pull timed out after {exc.timeout} seconds.",
            }
            return _LAST_UPDATE
        except OSError as exc:
            _LAST_UPDATE = {
                "status": "failed",
                "started_at": _LAST_UPDATE["started_at"],
                "finished_at": _iso_now(),
                "latency_ms": _elapsed_ms(started_at),
                "branch": before["branch"],
                "head_before": before["head"],
                "error": f"Could not run git pull: {exc}",
            }
            return _LAST_UPDATE

        after = await asyncio.to_thread(_read_git_state, root)
        output = _git_output(result)
        updated = result.returncode == 0 and before["head"] != after["head"]
        _LAST_UPDATE = {
            "status": "passed" if result.returncode == 0 else "failed",
            "started_at": _LAST_UPDATE["started_at"],
            "finished_at": _iso_now(),
            "latency_ms": _elapsed_ms(started_at),
            "branch": after["branch"],
            "upstream": after["upstream"],
            "head_before": before["head"],
            "head_after": after["head"],
            "updated": updated,
            "restart_recommended": updated,
            "output": output,
        }
        if result.returncode != 0:
            _LAST_UPDATE["error"] = output or f"git pull exited with code {result.returncode}."
        return _LAST_UPDATE
