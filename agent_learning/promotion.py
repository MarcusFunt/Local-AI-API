"""Independent local-main promotion for fully evaluated candidate patches."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


ALL_GATED_CHECKS = (
    "unit",
    "compile",
    "compose_config",
    "status_ui_tests",
    "repo_ops_tests",
    "dependency_health",
)
_CANDIDATE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}$")
_MAX_EVIDENCE_AGE = timedelta(hours=24)


class PromotionError(ValueError):
    """Raised when a candidate cannot safely reach local main."""


@dataclass(frozen=True)
class PromotionResult:
    candidate_id: str
    base_revision: str
    status: str
    promoted_revision: str | None = None
    rollback_tag: str | None = None


Runner = Callable[[list[str], Path], subprocess.CompletedProcess[str]]


def _default_runner(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False, timeout=900)


class PromotionController:
    """Verify a candidate in a temporary worktree, then fast-forward local main only."""

    def __init__(self, source_root: Path, state_root: Path | None = None, runner: Runner = _default_runner) -> None:
        self.source_root = source_root.resolve()
        self.state_root = (state_root or self.source_root / ".local" / "agent-learning").resolve()
        self.runner = runner

    def _run(self, args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        try:
            return self.runner(args, cwd or self.source_root)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PromotionError(f"Could not execute trusted promotion command: {exc}") from exc

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        result = self._run(["git", *args], cwd)
        if result.returncode:
            output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()[-2_000:]
            raise PromotionError(f"git {' '.join(args)} failed: {output}")
        return result.stdout.strip()

    @staticmethod
    def _load_candidate(path: Path) -> dict[str, Any]:
        try:
            candidate = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PromotionError("Candidate manifest is unreadable.") from exc
        if not isinstance(candidate, dict) or candidate.get("version") != 1:
            raise PromotionError("Candidate manifest must be version 1.")
        candidate_id = candidate.get("candidate_id")
        if not isinstance(candidate_id, str) or not _CANDIDATE_ID.fullmatch(candidate_id):
            raise PromotionError("Candidate ID must contain 1-48 lowercase letters, numbers, or hyphens.")
        if not isinstance(candidate.get("base_revision"), str) or not re.fullmatch(r"[0-9a-f]{7,64}", candidate["base_revision"]):
            raise PromotionError("Candidate manifest has an invalid base revision.")
        return candidate

    @staticmethod
    def _fresh_timestamp(value: object) -> None:
        if not isinstance(value, str):
            raise PromotionError("Candidate evidence must include evaluated_at.")
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PromotionError("Candidate evaluated_at is invalid.") from exc
        if timestamp.tzinfo is None or datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc) > _MAX_EVIDENCE_AGE:
            raise PromotionError("Candidate evidence is older than 24 hours.")

    def _verify_manifest(self, candidate: dict[str, Any], manifest_path: Path) -> Path:
        self._fresh_timestamp(candidate.get("evaluated_at"))
        checks = candidate.get("checks")
        if not isinstance(checks, dict) or set(checks) != set(ALL_GATED_CHECKS):
            raise PromotionError("Candidate must include every named check exactly once.")
        if any(not isinstance(value, dict) or value.get("passed") is not True for value in checks.values()):
            raise PromotionError("Candidate contains a failed named check.")
        for name in ("quality_gate", "public_eval_gate", "dependency_security"):
            if not isinstance(candidate.get(name), dict) or candidate[name].get("passed") is not True:
                raise PromotionError(f"Candidate has not passed {name}.")
        patch_file = candidate.get("patch_file")
        if not isinstance(patch_file, str) or Path(patch_file).name != patch_file or not patch_file.endswith(".patch"):
            raise PromotionError("patch_file must name a sibling .patch file.")
        patch = manifest_path.parent / patch_file
        if not patch.is_file() or patch.stat().st_size > 1_000_000:
            raise PromotionError("Candidate patch is missing or exceeds 1 MB.")
        return patch

    def _verify_source(self, base_revision: str) -> None:
        if self._git("rev-parse", "--is-inside-work-tree") != "true":
            raise PromotionError("Promotion source is not a Git worktree.")
        if self._git("branch", "--show-current") != "main":
            raise PromotionError("Automatic promotion only targets local main.")
        if self._git("status", "--porcelain=v1"):
            raise PromotionError("Automatic promotion requires a clean local main worktree.")
        if self._git("rev-parse", "HEAD") != base_revision:
            raise PromotionError("Candidate base revision no longer matches local main.")

    def _rerun_checks(self, worktree: Path) -> None:
        commands = {
            "unit": ["python", "-m", "pytest", "tests", "-q"],
            "compile": ["python", "-m", "compileall", "gateway", "repo_ops", "agent_learning"],
            "compose_config": ["docker", "compose", "config"],
            "status_ui_tests": ["python", "-m", "pytest", "tests/test_status_ui.py", "-q"],
            "repo_ops_tests": ["python", "-m", "pytest", "tests/test_repo_ops.py", "-q"],
            "dependency_health": ["python", "-m", "pip", "check"],
        }
        for name in ALL_GATED_CHECKS:
            result = self._run(commands[name], worktree)
            if result.returncode:
                output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()[-2_000:]
                raise PromotionError(f"Independent {name} verification failed: {output}")

    def verify(self, manifest_path: Path) -> PromotionResult:
        manifest_path = manifest_path.resolve()
        candidate = self._load_candidate(manifest_path)
        patch = self._verify_manifest(candidate, manifest_path)
        base_revision = self._git("rev-parse", str(candidate["base_revision"]))
        self._verify_source(base_revision)
        self._git("apply", "--check", "--whitespace=nowarn", str(patch))
        return PromotionResult(candidate["candidate_id"], base_revision, "verified")

    def promote(self, manifest_path: Path) -> PromotionResult:
        verified = self.verify(manifest_path)
        candidate = self._load_candidate(manifest_path.resolve())
        patch = self._verify_manifest(candidate, manifest_path.resolve())
        self.state_root.mkdir(parents=True, exist_ok=True)
        temp_root = Path(tempfile.mkdtemp(prefix=f"promote-{verified.candidate_id}-", dir=self.state_root))
        rollback_tag = f"auto-promote/{verified.candidate_id}-before"
        try:
            self._verify_source(verified.base_revision)
            self._git("worktree", "add", "--detach", str(temp_root), verified.base_revision)
            self._git("apply", "--index", "--whitespace=nowarn", str(patch), cwd=temp_root)
            self._rerun_checks(temp_root)
            if not self._git("diff", "--cached", "--name-only", cwd=temp_root):
                raise PromotionError("Candidate patch makes no changes.")
            self._git(
                "-c", "user.name=Local AI API Autopromote",
                "-c", "user.email=autopromote@local.invalid",
                "commit", "-m", f"auto-improve: {verified.candidate_id}",
                cwd=temp_root,
            )
            promoted_revision = self._git("rev-parse", "HEAD", cwd=temp_root)
            self._verify_source(verified.base_revision)
            self._git("tag", "-a", rollback_tag, verified.base_revision, "-m", f"Rollback point for {verified.candidate_id}")
            self._git("merge", "--ff-only", promoted_revision)
            self._append_audit({
                "candidate_id": verified.candidate_id,
                "base_revision": verified.base_revision,
                "promoted_revision": promoted_revision,
                "rollback_tag": rollback_tag,
                "promoted_at": datetime.now(timezone.utc).isoformat(),
            })
            return PromotionResult(verified.candidate_id, verified.base_revision, "promoted", promoted_revision, rollback_tag)
        finally:
            if (temp_root / ".git").exists():
                self._git("worktree", "remove", "--force", str(temp_root))
            shutil.rmtree(temp_root, ignore_errors=True)

    def _append_audit(self, entry: dict[str, str]) -> None:
        audit = self.state_root / "promotions.jsonl"
        with audit.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, sort_keys=True) + "\n")
