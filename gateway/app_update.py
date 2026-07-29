from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


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


def _availability(root: Path) -> dict[str, Any]:
    if shutil.which("git") is None:
        return {"available": False, "reason": "git executable was not found."}
    try:
        result = _run_git(["rev-parse", "--show-toplevel"], root)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "reason": f"Could not inspect Git checkout: {exc}"}
    if result.returncode != 0:
        return {"available": False, "reason": "Gateway is not running from a Git checkout."}
    return {"available": True, "root": result.stdout.strip() or str(root)}


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
    """Report repository state; scheduled installers are the only update owner."""
    root = _repo_root()
    availability = await asyncio.to_thread(_availability, root)
    payload: dict[str, Any] = {
        "status": "idle",
        "available": availability["available"],
        "update_owner": "installer_schedule",
    }
    if not availability["available"]:
        payload["reason"] = availability["reason"]
        return payload
    payload["root"] = availability["root"]
    try:
        payload.update(await asyncio.to_thread(_read_git_state, root))
    except (OSError, subprocess.TimeoutExpired) as exc:
        payload["reason"] = f"Git metadata is temporarily unavailable: {exc}"
    return payload
