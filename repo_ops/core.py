"""Restricted repository operations used by the repo-ops MCP worker."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from datetime import UTC, datetime, timedelta
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .autonomy import AutonomyPolicy, RUN_STATES, TERMINAL_STATES


_TASK_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}$")
_MAX_FILE_BYTES = 1_000_000
_MAX_SEARCH_RESULTS = 50
_MAX_OUTPUT_CHARS = 12_000
_ALLOWED_CHECKS = {
    "unit",
    "compile",
    "compose_config",
    "status_ui_tests",
    "repo_ops_tests",
    "dependency_health",
}
_IMPROVEMENT_MARKERS = re.compile(r"\b(?:TODO|FIXME|HACK|XXX)\b", re.IGNORECASE)
_LIFECYCLE_STATES = {"active", "paused", "review_ready", "archived", "expired"}
_ARCHIVE_EXCLUDED_PARTS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache"}
_ARCHIVE_EXCLUDED_NAMES = {".env", ".env.local", ".env.production", ".env.development"}


class RepoOpsError(ValueError):
    """Raised when a requested repository operation is unsafe or unavailable."""


@dataclass(frozen=True)
class RepoOpsConfig:
    """Filesystem and tool configuration for one isolated worker instance."""

    source_root: Path
    workspaces_root: Path
    gitnexus_repo: str = "source"
    archive_root: Path | None = None
    lease_hours: int = 24
    workspace_ttl_days: int = 7
    archive_ttl_days: int = 14
    autonomy_max_storage_gib: int = 20

    @classmethod
    def from_environment(cls) -> "RepoOpsConfig":
        return cls(
            source_root=Path(os.environ.get("REPO_OPS_SOURCE_ROOT", "/source")),
            workspaces_root=Path(os.environ.get("REPO_OPS_WORKSPACES_ROOT", "/workspaces")),
            gitnexus_repo=os.environ.get("REPO_OPS_GITNEXUS_REPO", "source"),
            archive_root=Path(os.environ["REPO_OPS_ARCHIVE_ROOT"]) if os.environ.get("REPO_OPS_ARCHIVE_ROOT") else None,
            lease_hours=int(os.environ.get("REPO_OPS_ACTIVE_LEASE_HOURS", "24")),
            workspace_ttl_days=int(os.environ.get("REPO_OPS_WORKSPACE_TTL_DAYS", "7")),
            archive_ttl_days=int(os.environ.get("REPO_OPS_ARCHIVE_TTL_DAYS", "14")),
            autonomy_max_storage_gib=min(20, max(1, int(os.environ.get("REPO_OPS_AUTONOMY_MAX_STORAGE_GIB", "20")))),
        )


@dataclass
class RepoOpsManager:
    """Run only allow-listed operations against disposable Git workspaces."""

    config: RepoOpsConfig
    check_results: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.config.workspaces_root.mkdir(parents=True, exist_ok=True)
        self._lifecycle_root.mkdir(parents=True, exist_ok=True)
        self._archive_root.mkdir(parents=True, exist_ok=True)
        self._autonomy_root.mkdir(parents=True, exist_ok=True)
        self._preview_jobs_root.mkdir(parents=True, exist_ok=True)
        self._preview_results_root.mkdir(parents=True, exist_ok=True)

    @property
    def _lifecycle_root(self) -> Path:
        return self.config.workspaces_root / ".lifecycle"

    @property
    def _archive_root(self) -> Path:
        return self.config.archive_root or self.config.workspaces_root / ".archives"

    @property
    def _autonomy_root(self) -> Path:
        return self.config.workspaces_root / ".autonomy"

    @property
    def _preview_jobs_root(self) -> Path:
        return self.config.workspaces_root / ".preview-jobs"

    @property
    def _preview_results_root(self) -> Path:
        return self.config.workspaces_root / ".preview-results"

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _timestamp(value: datetime | None = None) -> str:
        return (value or RepoOpsManager._now()).isoformat()

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    def _command(
        self,
        args: list[str],
        cwd: Path | None = None,
        timeout: int = 120,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                args,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RepoOpsError(f"Could not run approved command: {exc}") from exc

    @staticmethod
    def _output(result: subprocess.CompletedProcess[str]) -> str:
        output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        return output[-_MAX_OUTPUT_CHARS:]

    def _require_task_id(self, task_id: str) -> str:
        if not _TASK_ID.fullmatch(task_id):
            raise RepoOpsError("Task IDs must use 1-48 lowercase letters, numbers, or hyphens.")
        return task_id

    def _workspace(self, task_id: str) -> Path:
        return self.config.workspaces_root / self._require_task_id(task_id)

    def _artifacts_root(self, task_id: str) -> Path:
        return self.config.workspaces_root / ".artifacts" / self._require_task_id(task_id)

    def _lifecycle_path(self, task_id: str) -> Path:
        return self._lifecycle_root / f"{self._require_task_id(task_id)}.json"

    def _autonomy_path(self, task_id: str) -> Path:
        return self._autonomy_root / f"{self._require_task_id(task_id)}.json"

    def _load_autonomy(self, task_id: str) -> dict[str, Any]:
        path = self._autonomy_path(task_id)
        if not path.is_file():
            raise RepoOpsError("Autonomous-run metadata does not exist.")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RepoOpsError("Autonomous-run metadata is unreadable.") from exc
        if payload.get("state") not in RUN_STATES:
            raise RepoOpsError("Autonomous-run metadata has an invalid state.")
        return payload

    def _save_autonomy(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        payload["updated_at"] = self._timestamp()
        self._autonomy_path(task_id).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return payload

    def _load_lifecycle(self, task_id: str) -> dict[str, Any]:
        path = self._lifecycle_path(task_id)
        if not path.is_file():
            raise RepoOpsError("Workspace lifecycle metadata does not exist.")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RepoOpsError("Workspace lifecycle metadata is unreadable.") from exc
        if payload.get("state") not in _LIFECYCLE_STATES:
            raise RepoOpsError("Workspace lifecycle metadata has an invalid state.")
        return payload

    def _save_lifecycle(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        payload["updated_at"] = self._timestamp()
        encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        self._lifecycle_path(task_id).write_text(encoded, encoding="utf-8")
        return payload

    def _create_lifecycle(self, task_id: str, branch: str, base_revision: str, parent_task_id: str | None = None) -> dict[str, Any]:
        now = self._now()
        payload: dict[str, Any] = {
            "task_id": task_id,
            "branch": branch,
            "base_revision": base_revision,
            "created_at": self._timestamp(now),
            "last_activity_at": self._timestamp(now),
            "lease_until": self._timestamp(now + timedelta(hours=self.config.lease_hours)),
            "state": "active",
            "archive": None,
            "snapshot_sha256": None,
        }
        if parent_task_id:
            payload["parent_task_id"] = parent_task_id
        return self._save_lifecycle(task_id, payload)

    def _touch_activity(self, task_id: str) -> None:
        payload = self._load_lifecycle(task_id)
        if payload["state"] not in {"active", "paused"}:
            return
        now = self._now()
        payload["last_activity_at"] = self._timestamp(now)
        payload["lease_until"] = self._timestamp(now + timedelta(hours=self.config.lease_hours))
        self._save_lifecycle(task_id, payload)

    def _check_log(self, task_id: str) -> Path:
        artifacts = self._artifacts_root(task_id)
        artifacts.mkdir(parents=True, exist_ok=True)
        return artifacts / "checks.json"

    def _archive_signing_key(self) -> bytes:
        """Create a host-backed private key once; MCP tools never expose it."""
        path = self._archive_root / ".manifest-signing-key"
        if not path.exists():
            try:
                with path.open("xb") as stream:
                    stream.write(os.urandom(32))
                path.chmod(0o600)
            except FileExistsError:
                pass
        key = path.read_bytes()
        if len(key) < 32:
            raise RepoOpsError("Archive manifest signing key is invalid.")
        return key

    def _sign_manifest(self, content: bytes) -> str:
        return hmac.new(self._archive_signing_key(), content, hashlib.sha256).hexdigest()

    def _checks(self, task_id: str) -> list[dict[str, Any]]:
        path = self._check_log(task_id)
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise RepoOpsError("Check evidence is unreadable.") from exc
        return self.check_results.get(task_id, [])

    def _record_check(self, task_id: str, payload: dict[str, Any]) -> None:
        checks = self._checks(task_id)
        checks.append(payload)
        self._check_log(task_id).write_text(json.dumps(checks, indent=2) + "\n", encoding="utf-8")
        self.check_results[task_id] = checks

    @staticmethod
    def _hash(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def _safe_path(self, root: Path, relative_path: str, allow_missing: bool = False) -> Path:
        candidate = Path(relative_path)
        if not relative_path or candidate.is_absolute() or ".." in candidate.parts:
            raise RepoOpsError("Path must be a non-empty relative path inside the workspace.")
        if candidate.parts[0] in {".git", ".env", ".agent-workspaces"}:
            raise RepoOpsError("This path is not available to repository tools.")

        root_resolved = root.resolve()
        current = root_resolved
        for part in candidate.parts[:-1]:
            current = current / part
            if current.exists() and current.is_symlink():
                raise RepoOpsError("Symlinked paths are not available to repository tools.")
        raw_path = root_resolved / candidate
        if raw_path.exists() and raw_path.is_symlink():
            raise RepoOpsError("Symlinked files are not available to repository tools.")
        resolved = raw_path.resolve(strict=False)
        try:
            resolved.relative_to(root_resolved)
        except ValueError as exc:
            raise RepoOpsError("Path escapes the workspace.") from exc
        if not allow_missing and not resolved.is_file():
            raise RepoOpsError("Requested file does not exist.")
        return resolved

    def repo_status(self) -> dict[str, Any]:
        """Return repository state without modifying the source checkout."""
        root = self.config.source_root
        if not root.is_dir():
            raise RepoOpsError("The read-only source checkout is unavailable.")
        commands = {
            "head": ["git", "rev-parse", "--short", "HEAD"],
            "branch": ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            "status": ["git", "status", "--short"],
        }
        values: dict[str, str] = {}
        for name, args in commands.items():
            result = self._command(args, cwd=root)
            if result.returncode:
                raise RepoOpsError(f"Could not inspect source checkout: {self._output(result)}")
            values[name] = result.stdout.strip()
        lifecycle = self.list_workspaces()
        notices = [
            f"{item['task_id']} is {item['state']} (lease expired)."
            for item in lifecycle
            if item["state"] in {"active", "paused"} and item["lease_expired"]
        ]
        notices.extend(
            f"{item['task_id']} archive expires {item['expires_at']}."
            for item in lifecycle
            if item.get("expires_at") and item["state"] == "archived"
        )
        return {
            "source_read_only": True,
            "head": values["head"],
            "branch": values["branch"],
            "dirty": bool(values["status"]),
            "workspaces": sorted(
                path.name
                for path in self.config.workspaces_root.iterdir()
                if path.is_dir() and not path.name.startswith(".") and path.name != self.config.gitnexus_repo
            ),
            "lifecycle": lifecycle,
            "notices": notices,
        }

    def create_workspace(self, task_id: str, base_revision: str | None = None, parent_task_id: str | None = None) -> dict[str, str]:
        """Clone the committed source state into a new disposable branch."""
        workspace = self._workspace(task_id)
        if workspace.exists():
            raise RepoOpsError("A workspace already exists for this task ID.")
        if not (self.config.source_root / ".git").exists():
            raise RepoOpsError("The source path is not a Git checkout.")
        result = self._command(
            ["git", "clone", "--no-hardlinks", str(self.config.source_root), str(workspace)],
            timeout=300,
        )
        if result.returncode:
            shutil.rmtree(workspace, ignore_errors=True)
            raise RepoOpsError(f"Could not create workspace: {self._output(result)}")
        branch = f"agent/{task_id}"
        if base_revision:
            checkout = self._command(["git", "checkout", "--detach", base_revision], cwd=workspace)
            if checkout.returncode:
                shutil.rmtree(workspace, ignore_errors=True)
                raise RepoOpsError(f"The recorded base revision is unavailable: {self._output(checkout)}")
        switch = self._command(["git", "switch", "--create", branch], cwd=workspace)
        if switch.returncode:
            shutil.rmtree(workspace, ignore_errors=True)
            raise RepoOpsError(f"Could not create workspace branch: {self._output(switch)}")
        disable_push = self._command(["git", "remote", "set-url", "--push", "origin", "DISABLED"], cwd=workspace)
        if disable_push.returncode:
            raise RepoOpsError(f"Could not lock workspace push remote: {self._output(disable_push)}")
        head = self._command(["git", "rev-parse", "HEAD"], cwd=workspace)
        if head.returncode:
            shutil.rmtree(workspace, ignore_errors=True)
            raise RepoOpsError("Could not record workspace base revision.")
        self._create_lifecycle(task_id, branch, head.stdout.strip(), parent_task_id)
        return {"task_id": task_id, "branch": branch, "workspace": str(workspace)}

    def read_file(
        self,
        relative_path: str,
        task_id: str | None = None,
        start_line: int = 1,
        end_line: int = 200,
    ) -> dict[str, Any]:
        """Read a bounded range from source or one task workspace."""
        if start_line < 1 or end_line < start_line or end_line - start_line > 499:
            raise RepoOpsError("Line range must contain between 1 and 500 lines.")
        root = self._workspace(task_id) if task_id else self.config.source_root
        file_path = self._safe_path(root, relative_path)
        content = file_path.read_bytes()
        if len(content) > _MAX_FILE_BYTES:
            raise RepoOpsError("Requested file exceeds the 1 MB repository-tool limit.")
        lines = content.decode("utf-8", errors="replace").splitlines()
        return {
            "path": relative_path,
            "start_line": start_line,
            "end_line": min(end_line, len(lines)),
            "content": "\n".join(lines[start_line - 1:end_line]),
            "sha256": self._hash(content),
        }

    def search_code(self, query: str, path_glob: str = "", task_id: str | None = None) -> list[dict[str, Any]]:
        """Run a bounded text search without accepting shell syntax."""
        if not query.strip() or len(query) > 300:
            raise RepoOpsError("Search query must contain 1-300 characters.")
        if len(path_glob) > 120 or ".." in Path(path_glob).parts:
            raise RepoOpsError("Search path filter is invalid.")
        root = self._workspace(task_id) if task_id else self.config.source_root
        args = ["rg", "--line-number", "--no-heading", "--color", "never", "--max-count", str(_MAX_SEARCH_RESULTS)]
        if path_glob:
            args.extend(["--glob", path_glob])
        args.extend(["--glob", "!.git/**", "--glob", "!.env", query, "."])
        result = self._command(args, cwd=root, timeout=30)
        if result.returncode not in {0, 1}:
            raise RepoOpsError(f"Search failed: {self._output(result)}")
        matches: list[dict[str, Any]] = []
        for line in result.stdout.splitlines()[:_MAX_SEARCH_RESULTS]:
            path, separator, remainder = line.partition(":")
            line_number, separator_2, text = remainder.partition(":")
            if not separator or not separator_2 or not line_number.isdigit():
                continue
            matches.append({"path": path.removeprefix("./").removeprefix(".\\").replace("\\", "/"), "line": int(line_number), "text": text})
        return matches

    def write_file(self, task_id: str, relative_path: str, content: str, expected_sha256: str | None = None) -> dict[str, str]:
        """Write one regular workspace file with optimistic-concurrency protection."""
        if len(content.encode("utf-8")) > _MAX_FILE_BYTES:
            raise RepoOpsError("File content exceeds the 1 MB repository-tool limit.")
        workspace = self._workspace(task_id)
        if not workspace.is_dir():
            raise RepoOpsError("Workspace does not exist.")
        file_path = self._safe_path(workspace, relative_path, allow_missing=True)
        if file_path.exists():
            current_hash = self._hash(file_path.read_bytes())
            if expected_sha256 != current_hash:
                raise RepoOpsError("expected_sha256 must match the current file before replacing it.")
        elif expected_sha256 is not None:
            raise RepoOpsError("New files must not include expected_sha256.")
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8", newline="\n")
        self._touch_activity(task_id)
        return {"path": relative_path, "sha256": self._hash(file_path.read_bytes())}

    def git_diff(self, task_id: str) -> dict[str, str]:
        """Return the uncommitted diff from one isolated workspace only."""
        workspace = self._workspace(task_id)
        result = self._command(["git", "diff", "--no-ext-diff", "--binary"], cwd=workspace)
        if result.returncode:
            raise RepoOpsError(f"Could not read workspace diff: {self._output(result)}")
        return {"task_id": task_id, "diff": self._output(result)}

    @staticmethod
    def _source_files(root: Path) -> list[Path]:
        ignored_parts = {".git", ".repo-ops", "__pycache__", ".venv", "venv", "node_modules"}
        return [
            path
            for path in root.rglob("*")
            if path.is_file() and not any(part in ignored_parts for part in path.relative_to(root).parts)
        ]

    def improvement_inventory(self, task_id: str | None = None) -> dict[str, Any]:
        """Identify bounded, evidence-based improvement candidates without modifying code."""
        root = self._workspace(task_id) if task_id else self.config.source_root
        if not root.is_dir():
            raise RepoOpsError("Requested repository root is unavailable.")
        markers: list[dict[str, Any]] = []
        large_files: list[dict[str, Any]] = []
        python_modules: list[str] = []
        for file_path in self._source_files(root):
            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            relative = file_path.relative_to(root).as_posix()
            lines = content.splitlines()
            if len(lines) >= 500:
                large_files.append({"path": relative, "lines": len(lines)})
            if file_path.suffix == ".py" and relative.startswith("gateway/") and file_path.name != "__init__.py":
                python_modules.append(relative)
            for line_number, line in enumerate(lines, start=1):
                if _IMPROVEMENT_MARKERS.search(line):
                    markers.append({"path": relative, "line": line_number, "text": line.strip()[:240]})
                    if len(markers) >= _MAX_SEARCH_RESULTS:
                        break
            if len(markers) >= _MAX_SEARCH_RESULTS:
                break
        test_files = {path.name for path in (root / "tests").glob("test_*.py")} if (root / "tests").is_dir() else set()
        untested = [
            module
            for module in python_modules
            if f"test_{Path(module).stem}.py" not in test_files
        ][:_MAX_SEARCH_RESULTS]
        history = self.experiment_history(task_id) if task_id and self._workspace(task_id).is_dir() else []
        candidates: list[dict[str, Any]] = []
        for marker in markers[:20]:
            sensitive = any(term in marker["path"].lower() for term in ("config", "auth", "middleware", "deploy"))
            candidates.append(
                {
                    "kind": "marker",
                    "path": marker["path"],
                    "line": marker["line"],
                    "score": 70 if not sensitive else 20,
                    "impact_risk": "high" if sensitive else "unknown",
                    "autonomous_eligible": not sensitive,
                    "reason": "Explicit maintenance marker; high-risk configuration and auth paths are excluded.",
                }
            )
        for module in untested[:20]:
            candidates.append(
                {
                    "kind": "test_gap",
                    "path": module,
                    "score": 50,
                    "impact_risk": "unknown",
                    "autonomous_eligible": True,
                    "reason": "Gateway module has no same-name focused test; impact analysis remains mandatory before edits.",
                }
            )
        candidates.sort(key=lambda item: int(item["score"]), reverse=True)
        return {
            "root": "workspace" if task_id else "source",
            "markers": markers,
            "large_files": sorted(large_files, key=lambda item: item["lines"], reverse=True)[:20],
            "test_candidates": untested,
            "candidates": candidates[:20],
            "experiment_count": len(history),
            "guidance": "Rankings combine maintenance/test signals and recorded experiment history. High-risk paths are excluded; inspect context and impact before editing.",
        }

    def _experiment_log(self, task_id: str) -> Path:
        artifacts = self._artifacts_root(task_id)
        artifacts.mkdir(parents=True, exist_ok=True)
        return artifacts / "experiments.json"

    def record_experiment(
        self,
        task_id: str,
        title: str,
        hypothesis: str,
        outcome: str,
        evidence: str,
    ) -> dict[str, Any]:
        """Persist a bounded hypothesis/outcome record outside the Git workspace."""
        fields = {"title": title, "hypothesis": hypothesis, "outcome": outcome, "evidence": evidence}
        if any(not value.strip() or len(value) > 2_000 for value in fields.values()):
            raise RepoOpsError("Experiment title, hypothesis, outcome, and evidence must contain 1-2000 characters.")
        if not self._workspace(task_id).is_dir():
            raise RepoOpsError("Workspace does not exist.")
        log_path = self._experiment_log(task_id)
        history = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else []
        entry = {"number": len(history) + 1, **fields}
        history.append(entry)
        log_path.write_text(json.dumps(history, indent=2) + "\n", encoding="utf-8")
        self._touch_activity(task_id)
        return entry

    def experiment_history(self, task_id: str) -> list[dict[str, Any]]:
        """Return the task's persistent improvement ledger without exposing host files."""
        if not self._workspace(task_id).is_dir():
            raise RepoOpsError("Workspace does not exist.")
        log_path = self._experiment_log(task_id)
        return json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else []

    def _check_command(self, workspace: Path, preset: str) -> list[str]:
        if preset not in _ALLOWED_CHECKS:
            raise RepoOpsError(
                "Unknown check preset. Allowed: unit, compile, compose_config, "
                "status_ui_tests, repo_ops_tests, dependency_health."
            )
        if preset == "unit":
            return [self._verification_python(), "-m", "pytest", "tests/", "-v", "--cov=gateway"]
        if preset == "compile":
            return [self._verification_python(), "-m", "compileall", "gateway"]
        if preset == "compose_config":
            return ["docker-compose", "-f", "compose.yaml", "-f", "compose.agent-zero.yaml", "config"]
        if preset == "status_ui_tests":
            return [self._verification_python(), "-m", "pytest", "tests/test_status_ui.py", "-v"]
        if preset == "repo_ops_tests":
            return [self._verification_python(), "-m", "pytest", "tests/test_repo_ops.py", "tests/test_repo_ops_deployment.py", "-v"]
        if preset == "dependency_health":
            return [self._verification_python(), "-m", "pip", "check"]
        raise RepoOpsError("Unknown check preset.")

    def capture_ui(self, task_id: str) -> dict[str, Any]:
        """Queue a workspace-only UI audit; the preview worker has no network access."""
        return self.preview_workspace(task_id)

    @staticmethod
    def _verification_python() -> str:
        """Use the repository-pinned environment when the worker runs in Docker."""
        return os.environ.get("REPO_OPS_VERIFICATION_PYTHON", sys.executable)

    def run_check(self, task_id: str, preset: str) -> dict[str, Any]:
        """Run one named verification command, never arbitrary agent input."""
        workspace = self._workspace(task_id)
        if not workspace.is_dir():
            raise RepoOpsError("Workspace does not exist.")
        command = self._check_command(workspace, preset)
        result = self._command(command, cwd=workspace, timeout=600)
        payload = {
            "preset": preset,
            "passed": result.returncode == 0,
            "returncode": result.returncode,
            "output": self._output(result),
            "recorded_at": self._timestamp(),
        }
        self._record_check(task_id, payload)
        self._touch_activity(task_id)
        return payload

    def _workspace_bytes(self, task_id: str) -> int:
        roots = (self._workspace(task_id), self._artifacts_root(task_id))
        return sum(path.stat().st_size for root in roots if root.is_dir() for path in root.rglob("*") if path.is_file())

    @staticmethod
    def _evaluation_manifest() -> dict[str, Any]:
        path = Path(__file__).with_name("evals") / "manifest.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RepoOpsError("Evaluation manifest is unavailable.") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("evaluations"), list):
            raise RepoOpsError("Evaluation manifest is invalid.")
        return payload

    def _evaluation_definition(self, evaluation_id: str) -> dict[str, Any]:
        for item in self._evaluation_manifest()["evaluations"]:
            if isinstance(item, dict) and item.get("id") == evaluation_id and isinstance(item.get("checks"), list):
                return item
        raise RepoOpsError("Unknown evaluation suite.")

    def start_autonomous_run(self, task_id: str, evaluation_id: str = "core-contracts", policy: dict[str, Any] | None = None) -> dict[str, Any]:
        """Start a bounded run; the caller still has only existing safe MCP tools."""
        if not self._workspace(task_id).is_dir():
            raise RepoOpsError("Workspace does not exist.")
        self._evaluation_definition(evaluation_id)
        if self._autonomy_path(task_id).exists():
            raise RepoOpsError("An autonomous run already exists for this task ID.")
        try:
            safe_policy = AutonomyPolicy.from_input(
                policy or {"max_storage_bytes": self.config.autonomy_max_storage_gib * 1024 * 1024 * 1024}
            )
        except ValueError as exc:
            raise RepoOpsError(str(exc)) from exc
        now = self._timestamp()
        payload: dict[str, Any] = {
            "task_id": task_id,
            "state": "queued",
            "started_at": now,
            "evaluation_id": evaluation_id,
            "policy": safe_policy.as_dict(),
            "progress": [],
            "evaluations": [],
            "non_improving_evaluations": 0,
            "stop_reason": None,
            "preview": {"status": "not_requested"},
        }
        self._save_autonomy(task_id, payload)
        payload["state"] = "running"
        self._save_autonomy(task_id, payload)
        self._touch_activity(task_id)
        return self.autonomous_status(task_id)

    def _apply_autonomy_budget(self, task_id: str, payload: dict[str, Any]) -> bool:
        policy = AutonomyPolicy.from_input(payload["policy"])
        elapsed = (self._now() - self._parse_timestamp(payload["started_at"])).total_seconds()
        size = self._workspace_bytes(task_id)
        if elapsed > policy.max_runtime_seconds:
            payload.update({"state": "paused", "stop_reason": "runtime_budget_exceeded"})
        elif size > policy.max_storage_bytes:
            payload.update({"state": "paused", "stop_reason": "storage_budget_exceeded"})
        else:
            return False
        self._save_autonomy(task_id, payload)
        return True

    def autonomous_status(self, task_id: str) -> dict[str, Any]:
        payload = self._load_autonomy(task_id)
        if payload["state"] not in TERMINAL_STATES:
            self._apply_autonomy_budget(task_id, payload)
            payload = self._load_autonomy(task_id)
        policy = AutonomyPolicy.from_input(payload["policy"])
        elapsed = max(0, int((self._now() - self._parse_timestamp(payload["started_at"])).total_seconds()))
        preview = self.preview_status(task_id)
        payload["preview"] = preview
        payload["resource_use"] = {
            "runtime_seconds": elapsed,
            "runtime_limit_seconds": policy.max_runtime_seconds,
            "storage_bytes": self._workspace_bytes(task_id),
            "storage_limit_bytes": policy.max_storage_bytes,
        }
        return payload

    def pause_autonomous_run(self, task_id: str, reason: str) -> dict[str, Any]:
        if not reason.strip() or len(reason) > 500:
            raise RepoOpsError("Pause reason must contain 1-500 characters.")
        payload = self._load_autonomy(task_id)
        if payload["state"] in TERMINAL_STATES:
            raise RepoOpsError("A completed autonomous run cannot be paused.")
        payload.update({"state": "paused", "stop_reason": reason.strip()})
        self._save_autonomy(task_id, payload)
        return self.autonomous_status(task_id)

    def resume_autonomous_run(self, task_id: str) -> dict[str, Any]:
        payload = self._load_autonomy(task_id)
        if payload["state"] != "paused":
            raise RepoOpsError("Only a paused autonomous run can resume.")
        payload.update({"state": "running", "stop_reason": None})
        self._save_autonomy(task_id, payload)
        return self.autonomous_status(task_id)

    def stop_autonomous_run(self, task_id: str, reason: str) -> dict[str, Any]:
        if not reason.strip() or len(reason) > 500:
            raise RepoOpsError("Stop reason must contain 1-500 characters.")
        payload = self._load_autonomy(task_id)
        if payload["state"] in TERMINAL_STATES:
            return self.autonomous_status(task_id)
        payload.update({"state": "stopped", "stop_reason": reason.strip()})
        self._save_autonomy(task_id, payload)
        return self.autonomous_status(task_id)

    def record_autonomous_progress(self, task_id: str, summary: str) -> dict[str, Any]:
        if not summary.strip() or len(summary) > 2_000:
            raise RepoOpsError("Progress summary must contain 1-2000 characters.")
        payload = self._load_autonomy(task_id)
        if payload["state"] != "running":
            raise RepoOpsError("Progress can only be recorded for a running autonomous task.")
        payload["progress"].append({"recorded_at": self._timestamp(), "summary": summary.strip()})
        self._save_autonomy(task_id, payload)
        self._touch_activity(task_id)
        return self.autonomous_status(task_id)

    def evaluate_workspace(self, task_id: str) -> dict[str, Any]:
        """Run only manifest-defined checks and update a monotonic score history."""
        payload = self._load_autonomy(task_id)
        if payload["state"] not in {"running", "evaluating"}:
            raise RepoOpsError("Only a running autonomous task can be evaluated.")
        payload["state"] = "evaluating"
        self._save_autonomy(task_id, payload)
        definition = self._evaluation_definition(str(payload["evaluation_id"]))
        results = [self.run_check(task_id, str(preset)) for preset in definition["checks"]]
        passed = sum(bool(result["passed"]) for result in results)
        score = round(passed / len(results), 3) if results else 0.0
        previous = payload["evaluations"][-1]["score"] if payload["evaluations"] else None
        improved = previous is None or score > float(previous)
        payload["non_improving_evaluations"] = 0 if improved else int(payload["non_improving_evaluations"]) + 1
        evaluation = {"recorded_at": self._timestamp(), "id": definition["id"], "score": score, "passed": passed, "total": len(results), "results": results}
        payload["evaluations"].append(evaluation)
        policy = AutonomyPolicy.from_input(payload["policy"])
        if passed == len(results) and self.git_diff(task_id)["diff"].strip():
            self.mark_review_ready(task_id)
            payload.update({"state": "review_ready", "stop_reason": "all_evaluations_passed"})
        elif payload["non_improving_evaluations"] >= policy.max_non_improving_evaluations:
            payload.update({"state": "stopped", "stop_reason": "non_improving_evaluation_limit"})
        else:
            payload["state"] = "running"
        self._save_autonomy(task_id, payload)
        return self.autonomous_status(task_id)

    def preview_workspace(self, task_id: str) -> dict[str, Any]:
        if not self._workspace(task_id).is_dir():
            raise RepoOpsError("Workspace does not exist.")
        self._preview_results_root.joinpath(f"{task_id}.json").unlink(missing_ok=True)
        job = {"task_id": task_id, "requested_at": self._timestamp()}
        self._preview_jobs_root.joinpath(f"{task_id}.json").write_text(json.dumps(job) + "\n", encoding="utf-8")
        if self._autonomy_path(task_id).exists():
            payload = self._load_autonomy(task_id)
            payload["preview"] = {"status": "queued", **job}
            self._save_autonomy(task_id, payload)
        return {"task_id": task_id, "status": "queued", "message": "The unnetworked preview worker will collect evidence."}

    def preview_status(self, task_id: str) -> dict[str, Any]:
        result = self._preview_results_root / f"{self._require_task_id(task_id)}.json"
        job = self._preview_jobs_root / f"{task_id}.json"
        if result.is_file():
            try:
                return json.loads(result.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {"status": "failed", "error": "Preview result is unreadable."}
        return {"status": "queued" if job.is_file() else "not_requested"}

    def _gitnexus(self, action: str, symbol: str) -> dict[str, Any]:
        if not symbol or len(symbol) > 200:
            raise RepoOpsError("Symbol must contain 1-200 characters.")
        result = self._command(["gitnexus", action, "-r", self.config.gitnexus_repo, symbol], timeout=60)
        if result.returncode:
            raise RepoOpsError(f"GitNexus {action} failed: {self._output(result)}")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RepoOpsError("GitNexus returned malformed JSON.") from exc

    def symbol_context(self, symbol: str) -> dict[str, Any]:
        return self._gitnexus("context", symbol)

    def impact_analysis(self, symbol: str) -> dict[str, Any]:
        return self._gitnexus("impact", symbol)

    def workspace_status(self, task_id: str) -> dict[str, Any]:
        """Return lifecycle metadata without exposing files outside controlled roots."""
        payload = self._load_lifecycle(task_id).copy()
        now = self._now()
        payload["lease_expired"] = payload["state"] in {"active", "paused"} and self._parse_timestamp(payload["lease_until"]) < now
        archive = payload.get("archive")
        if payload["state"] in {"active", "paused"}:
            payload["archive_due_at"] = self._timestamp(
                self._parse_timestamp(payload["last_activity_at"]) + timedelta(days=self.config.workspace_ttl_days)
            )
        if archive:
            archive_path = self._archive_root / archive["directory"]
            payload["archive_available"] = archive_path.is_dir() and (archive_path / "manifest.json").is_file()
            if payload["state"] == "archived":
                payload["expires_at"] = self._timestamp(self._parse_timestamp(archive["archived_at"]) + timedelta(days=self.config.archive_ttl_days))
        else:
            payload["archive_available"] = False
        payload["workspace_exists"] = self._workspace(task_id).is_dir()
        return payload

    def list_workspaces(self) -> list[dict[str, Any]]:
        """List lifecycle records, including archived tasks without live worktrees."""
        records: list[dict[str, Any]] = []
        for path in sorted(self._lifecycle_root.glob("*.json")):
            try:
                task_id = path.stem
                records.append(self.workspace_status(task_id))
            except RepoOpsError:
                continue
        return records

    def renew_workspace_lease(self, task_id: str) -> dict[str, Any]:
        """Renew an active task lease; archived review evidence is never reactivated implicitly."""
        payload = self._load_lifecycle(task_id)
        if payload["state"] not in {"active", "paused"}:
            raise RepoOpsError("Only active or paused workspaces can renew a lease.")
        self._touch_activity(task_id)
        return self.workspace_status(task_id)

    def pause_workspace(self, task_id: str, reason: str) -> dict[str, Any]:
        """Pause an existing workspace while retaining it for later explicit resumption."""
        if not reason.strip() or len(reason) > 500:
            raise RepoOpsError("Pause reason must contain 1-500 characters.")
        payload = self._load_lifecycle(task_id)
        if payload["state"] != "active" or not self._workspace(task_id).is_dir():
            raise RepoOpsError("Only an active live workspace can be paused.")
        payload["state"] = "paused"
        payload["pause_reason"] = reason.strip()
        self._save_lifecycle(task_id, payload)
        return self.workspace_status(task_id)

    @staticmethod
    def _archive_eligible(path: Path, root: Path) -> bool:
        relative = path.relative_to(root)
        if any(part in _ARCHIVE_EXCLUDED_PARTS for part in relative.parts):
            return False
        name = path.name.lower()
        return not (name in _ARCHIVE_EXCLUDED_NAMES or name.startswith(".env.") or name.endswith((".pem", ".key")))

    def _write_workspace_archive(self, task_id: str, report: dict[str, Any]) -> dict[str, Any]:
        workspace = self._workspace(task_id)
        stamp = self._now().strftime("%Y%m%dT%H%M%SZ")
        directory = f"{task_id}/{stamp}"
        archive_dir = self._archive_root / directory
        archive_dir.mkdir(parents=True, exist_ok=False)
        tree_path = archive_dir / "workspace.tar.gz"
        with tarfile.open(tree_path, "w:gz") as archive:
            for path in sorted(workspace.rglob("*")):
                if path.is_file() and not path.is_symlink() and self._archive_eligible(path, workspace):
                    archive.add(path, arcname=(Path("workspace") / path.relative_to(workspace)).as_posix(), recursive=False)
        diff_path = archive_dir / "workspace.diff"
        diff_path.write_text(self.git_diff(task_id)["diff"], encoding="utf-8")
        report_path = archive_dir / "task-report.json"
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        artifacts = self._artifacts_root(task_id)
        if artifacts.is_dir():
            with tarfile.open(archive_dir / "evidence.tar.gz", "w:gz") as archive:
                for path in sorted(artifacts.rglob("*")):
                    if path.is_file() and not path.is_symlink():
                        archive.add(path, arcname=(Path("evidence") / path.relative_to(artifacts)).as_posix(), recursive=False)
        files = {
            path.name: self._hash(path.read_bytes())
            for path in archive_dir.iterdir()
            if path.is_file()
        }
        manifest = {
            "task_id": task_id,
            "created_at": self._timestamp(),
            "base_revision": self._load_lifecycle(task_id)["base_revision"],
            "files": files,
            "exclusions": sorted(_ARCHIVE_EXCLUDED_PARTS | _ARCHIVE_EXCLUDED_NAMES),
        }
        encoded = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
        manifest["integrity_sha256"] = self._hash(encoded)
        manifest["signature_hmac_sha256"] = self._sign_manifest(encoded)
        (archive_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return {"directory": directory, "archived_at": manifest["created_at"], "integrity_sha256": manifest["integrity_sha256"]}

    def _append_tombstone(self, payload: dict[str, Any]) -> None:
        entry = {
            "task_id": payload["task_id"],
            "expired_at": self._timestamp(),
            "archive": payload.get("archive"),
            "base_revision": payload["base_revision"],
        }
        tombstones = self._archive_root / "tombstones.jsonl"
        with tombstones.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, sort_keys=True) + "\n")

    def archive_workspace(self, task_id: str, review_ready: bool = False) -> dict[str, Any]:
        """Snapshot evidence and remove a live disposable worktree; source is never touched."""
        payload = self._load_lifecycle(task_id)
        workspace = self._workspace(task_id)
        if payload["state"] not in {"active", "paused"} or not workspace.is_dir():
            raise RepoOpsError("Only an active or paused live workspace can be archived.")
        report = self.task_report(task_id)
        archive = self._write_workspace_archive(task_id, report)
        shutil.rmtree(workspace)
        payload["state"] = "review_ready" if review_ready else "archived"
        payload["archive"] = archive
        payload["snapshot_sha256"] = archive["integrity_sha256"]
        self._save_lifecycle(task_id, payload)
        return self.workspace_status(task_id)

    def mark_review_ready(self, task_id: str) -> dict[str, Any]:
        """Preserve a review candidate only after it has both a diff and verification evidence."""
        if not self.git_diff(task_id)["diff"].strip():
            raise RepoOpsError("Review-ready workspaces require a non-empty diff.")
        if not self._checks(task_id):
            raise RepoOpsError("Review-ready workspaces require at least one recorded verification result.")
        return self.archive_workspace(task_id, review_ready=True)

    def _safe_extract(self, archive_path: Path, destination: Path) -> None:
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                member_path = Path(member.name)
                if not member.isfile() or member_path.is_absolute() or ".." in member_path.parts or member_path.parts[:1] != ("workspace",):
                    raise RepoOpsError("Workspace archive contains an unsafe path.")
                target = destination / Path(*member_path.parts[1:])
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise RepoOpsError("Workspace archive member is unreadable.")
                target.write_bytes(source.read())

    def restore_workspace(self, task_id: str) -> dict[str, Any]:
        """Restore an archive into a new branch at its recorded revision without automatic rebasing."""
        payload = self._load_lifecycle(task_id)
        archive = payload.get("archive")
        if payload["state"] not in {"archived", "review_ready"} or not archive:
            raise RepoOpsError("Only an archived workspace can be restored.")
        archive_dir = self._archive_root / archive["directory"]
        manifest_path = archive_dir / "manifest.json"
        if not manifest_path.is_file():
            raise RepoOpsError("The workspace archive is unavailable.")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        unsigned_manifest = {key: value for key, value in manifest.items() if key not in {"integrity_sha256", "signature_hmac_sha256"}}
        encoded = json.dumps(unsigned_manifest, indent=2, sort_keys=True).encode("utf-8")
        if manifest.get("integrity_sha256") != self._hash(encoded) or not hmac.compare_digest(
            manifest.get("signature_hmac_sha256", ""), self._sign_manifest(encoded)
        ):
            raise RepoOpsError("Workspace archive manifest signature validation failed.")
        for name, digest in manifest.get("files", {}).items():
            candidate = archive_dir / name
            if not candidate.is_file() or self._hash(candidate.read_bytes()) != digest:
                raise RepoOpsError("Workspace archive integrity validation failed.")
        restored_id = f"{task_id[:35]}-r-{self._now().strftime('%H%M%S%f')[:8]}"
        created = self.create_workspace(restored_id, payload["base_revision"], parent_task_id=task_id)
        try:
            self._safe_extract(archive_dir / "workspace.tar.gz", self._workspace(restored_id))
        except Exception:
            shutil.rmtree(self._workspace(restored_id), ignore_errors=True)
            self._lifecycle_path(restored_id).unlink(missing_ok=True)
            raise
        return {**created, "restored_from": task_id, "archive_integrity_sha256": archive["integrity_sha256"]}

    def cleanup_workspaces(self, dry_run: bool = True, now: datetime | None = None) -> dict[str, Any]:
        """Archive stale ordinary workspaces and expire old archives; MCP callers are dry-run only."""
        current = now or self._now()
        planned: list[dict[str, str]] = []
        for record in self.list_workspaces():
            state = record["state"]
            task_id = record["task_id"]
            if state in {"active", "paused"}:
                inactive_since = self._parse_timestamp(record["last_activity_at"])
                if inactive_since + timedelta(days=self.config.workspace_ttl_days) <= current:
                    planned.append({"task_id": task_id, "action": "archive"})
                    if not dry_run:
                        self.archive_workspace(task_id)
            elif state == "archived" and record.get("archive"):
                archived_at = self._parse_timestamp(record["archive"]["archived_at"])
                if archived_at + timedelta(days=self.config.archive_ttl_days) <= current:
                    planned.append({"task_id": task_id, "action": "expire"})
                    if not dry_run:
                        shutil.rmtree(self._archive_root / record["archive"]["directory"])
                        self._append_tombstone(record)
                        record["state"] = "expired"
                        self._save_lifecycle(task_id, record)
        return {"dry_run": dry_run, "actions": planned}

    def workspace_health(self, task_id: str) -> dict[str, Any]:
        """Report recoverability, storage use, source drift, lease state, and stale checks."""
        record = self.workspace_status(task_id)
        workspace = self._workspace(task_id)
        size_bytes = sum(path.stat().st_size for path in workspace.rglob("*") if path.is_file()) if workspace.is_dir() else 0
        source_head = self._command(["git", "rev-parse", "HEAD"], cwd=self.config.source_root)
        if source_head.returncode:
            raise RepoOpsError("Could not inspect source revision.")
        checks = self._checks(task_id)
        autonomy = self.autonomous_status(task_id) if self._autonomy_path(task_id).exists() else None
        preview = self.preview_status(task_id)
        next_action = "Review the branch and evidence before merging or deployment."
        if autonomy and autonomy["state"] == "paused":
            next_action = "Review the autonomous-run budget or stop reason, then resume deliberately."
        elif autonomy and autonomy["state"] == "running":
            next_action = "Allow the bounded local run to continue, or inspect its current evidence."
        elif preview["status"] == "queued":
            next_action = "Wait for the unnetworked preview worker to complete the queued UI audit."
        return {
            "task_id": task_id,
            "state": record["state"],
            "workspace_bytes": size_bytes,
            "base_revision": record["base_revision"],
            "source_revision": source_head.stdout.strip(),
            "base_drifted": record["base_revision"] != source_head.stdout.strip(),
            "lease_expired": record["lease_expired"],
            "checks": len(checks),
            "stale_checks": not checks or record["last_activity_at"] > checks[-1].get("recorded_at", ""),
            "recoverable": bool(record["workspace_exists"] or record["archive_available"]),
            "autonomy": autonomy,
            "preview": preview,
            "next_action": next_action,
        }

    def task_report(self, task_id: str) -> dict[str, Any]:
        """Produce review evidence; this never commits, merges, pushes, or deploys."""
        workspace = self._workspace(task_id)
        lifecycle = self.workspace_status(task_id)
        if not workspace.is_dir():
            raise RepoOpsError("A live workspace is required to produce a new task report.")
        branch = self._command(["git", "branch", "--show-current"], cwd=workspace)
        status = self._command(["git", "status", "--short"], cwd=workspace)
        if branch.returncode or status.returncode:
            raise RepoOpsError("Could not inspect task workspace.")
        return {
            "task_id": task_id,
            "branch": branch.stdout.strip(),
            "changed": bool(status.stdout.strip()),
            "checks": self._checks(task_id),
            "experiments": self.experiment_history(task_id),
            "diff": self.git_diff(task_id)["diff"],
            "lifecycle_state": lifecycle["state"],
            "snapshot_sha256": lifecycle.get("snapshot_sha256"),
            "base_revision": lifecycle["base_revision"],
            "post_change_revision": self._command(["git", "rev-parse", "HEAD"], cwd=workspace).stdout.strip(),
            "expires_at": lifecycle.get("expires_at"),
            "archive_due_at": lifecycle.get("archive_due_at"),
            "autonomy": self.autonomous_status(task_id) if self._autonomy_path(task_id).exists() else None,
            "preview": self.preview_status(task_id),
            "next_step": "Review the branch and evidence before merging or deployment.",
        }
