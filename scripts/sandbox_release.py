"""Host-controlled staging, visual evidence, and approval helpers.

This tool deliberately has no service endpoint. A sandbox agent cannot invoke
it, push a branch, or restart the user-facing stack.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def _run(root: Path, *args: str) -> str:
    result = subprocess.run(args, cwd=root, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "command failed").strip())
    return result.stdout.strip()


def _state_path(root: Path) -> Path:
    return root / ".local" / "sandbox" / "release.json"


def _write_state(root: Path, payload: dict[str, object]) -> None:
    path = _state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def stage(root: Path) -> dict[str, object]:
    if _run(root, "git", "status", "--porcelain=v1"):
        raise RuntimeError("Staging requires a clean local checkout.")
    _run(root, "git", "fetch", "origin", "main")
    revision = _run(root, "git", "rev-parse", "origin/main")
    worktree = root / ".local" / "sandbox" / "worktrees" / revision[:12]
    if not worktree.exists():
        worktree.parent.mkdir(parents=True, exist_ok=True)
        _run(root, "git", "worktree", "add", "--detach", str(worktree), revision)
    checks = {
        "unit": ["python", "-m", "pytest", "tests", "-q"],
        "compile": ["python", "-m", "compileall", "gateway", "repo_ops", "agent_learning"],
        "compose": ["docker", "compose", "-f", "compose.sandbox.yaml", "config"],
    }
    results: dict[str, bool] = {}
    for name, command in checks.items():
        results[name] = subprocess.run(command, cwd=worktree, check=False).returncode == 0
    payload: dict[str, object] = {
        "version": 1,
        "revision": revision,
        "worktree": str(worktree),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "checks": results,
        "status": "ready_for_repair" if not all(results.values()) else "ready_for_approval",
        "staging_url": "http://127.0.0.1:50082",
    }
    _write_state(root, payload)
    return payload


def status(root: Path) -> dict[str, object]:
    try:
        payload = json.loads(_state_path(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "not_staged"}
    return payload if isinstance(payload, dict) else {"status": "invalid_state"}


def approve(root: Path, candidate: Path) -> dict[str, object]:
    """Promote a verified candidate, then publish it only from this host CLI."""
    from agent_learning.promotion import PromotionController

    result = PromotionController(root).promote(candidate.resolve())
    _run(root, "git", "push", "origin", "main", "--follow-tags")
    payload: dict[str, object] = {
        "version": 1,
        "revision": result.promoted_revision,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": "published_pending_deploy",
        "rollback_tag": result.rollback_tag,
    }
    _write_state(root, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage a local, non-deploying sandbox candidate.")
    parser.add_argument("command", choices=("stage", "status", "approve"))
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--candidate", type=Path, help="Verified candidate manifest required by approve.")
    args = parser.parse_args()
    root = args.source.resolve()
    if args.command == "stage":
        result = stage(root)
    elif args.command == "approve":
        if args.candidate is None:
            parser.error("approve requires --candidate")
        result = approve(root, args.candidate)
    else:
        result = status(root)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
