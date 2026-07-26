"""Focused tests for the isolated repository-operations core."""
from __future__ import annotations

import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from repo_ops.core import RepoOpsConfig, RepoOpsError, RepoOpsManager


def _git(root: Path, *args: str) -> None:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def manager(tmp_path: Path) -> RepoOpsManager:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "--initial-branch", "main")
    _git(source, "config", "user.name", "Repo Ops Test")
    _git(source, "config", "user.email", "repo-ops@example.test")
    (source / "gateway").mkdir()
    (source / "gateway" / "sample.py").write_text("# TODO: improve example\ndef hello():\n    return 'hello'\n", encoding="utf-8")
    (source / "README.md").write_text("Repository worker fixture\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "fixture")
    return RepoOpsManager(RepoOpsConfig(source, tmp_path / "workspaces"))


def test_repo_status_and_workspace_are_isolated(manager: RepoOpsManager):
    status = manager.repo_status()

    assert status["source_read_only"] is True
    assert status["branch"] == "main"
    workspace = manager.create_workspace("status-audit")

    assert workspace["branch"] == "agent/status-audit"
    assert manager.repo_status()["workspaces"] == ["status-audit"]
    remote = manager._command(["git", "remote", "get-url", "--push", "origin"], cwd=Path(workspace["workspace"]))
    assert remote.stdout.strip() == "DISABLED"


def test_read_search_write_and_diff_require_a_workspace_hash(manager: RepoOpsManager):
    manager.create_workspace("fix-readme")
    before = manager.read_file("README.md", task_id="fix-readme")
    matches = manager.search_code("hello", task_id="fix-readme")

    assert {match["line"] for match in matches} == {2, 3}
    assert all(match["path"] == "gateway/sample.py" for match in matches)
    updated = manager.write_file("fix-readme", "README.md", "Improved documentation\n", before["sha256"])

    assert updated["sha256"] != before["sha256"]
    assert "Improved documentation" in manager.git_diff("fix-readme")["diff"]
    with pytest.raises(RepoOpsError, match="expected_sha256"):
        manager.write_file("fix-readme", "README.md", "stale update\n", before["sha256"])


def test_paths_cannot_escape_or_follow_symlinks(manager: RepoOpsManager):
    manager.create_workspace("path-safety")
    workspace = manager.config.workspaces_root / "path-safety"
    target = manager.config.source_root / "README.md"
    try:
        (workspace / "linked.md").symlink_to(target)
    except OSError:
        pytest.skip("Symlink creation requires Windows developer privileges.")

    with pytest.raises(RepoOpsError, match="relative path"):
        manager.read_file("../README.md", task_id="path-safety")
    with pytest.raises(RepoOpsError, match="not available"):
        manager.read_file(".git/config", task_id="path-safety")
    with pytest.raises(RepoOpsError, match="Symlinked"):
        manager.read_file("linked.md", task_id="path-safety")


def test_only_named_check_presets_are_available(manager: RepoOpsManager):
    manager.create_workspace("check-preset")
    workspace = manager.config.workspaces_root / "check-preset"

    assert manager._check_command(workspace, "compile")[-2:] == ["compileall", "gateway"]
    with pytest.raises(RepoOpsError, match="Unknown check"):
        manager._check_command(workspace, "rm -rf /")
    with pytest.raises(RepoOpsError, match="requires REPO_OPS_UI_BASE_URL"):
        manager._check_command(workspace, "ui_audit")


def test_task_report_never_commits_or_merges(manager: RepoOpsManager):
    manager.create_workspace("report")
    report = manager.task_report("report")

    assert report["branch"] == "agent/report"
    assert report["changed"] is False
    assert "Review the branch" in report["next_step"]
    assert report["lifecycle_state"] == "active"
    assert report["base_revision"]
    assert report["archive_due_at"]


def test_improvement_inventory_and_experiment_ledger(manager: RepoOpsManager):
    inventory = manager.improvement_inventory()
    manager.create_workspace("iterate-ui")
    entry = manager.record_experiment(
        "iterate-ui",
        "Find improvement signals",
        "A static inventory will surface a small safe target.",
        "Found one explicit TODO and one untested module candidate.",
        "improvement_inventory returned gateway/sample.py.",
    )
    report = manager.task_report("iterate-ui")

    assert inventory["markers"] == [{"path": "gateway/sample.py", "line": 1, "text": "# TODO: improve example"}]
    assert inventory["test_candidates"] == ["gateway/sample.py"]
    assert manager.experiment_history("iterate-ui") == [entry]
    assert report["experiments"] == [entry]


def test_capture_ui_queues_unnetworked_workspace_preview(manager: RepoOpsManager):
    manager.create_workspace("ui-evidence")
    payload = manager.capture_ui("ui-evidence")

    assert payload["status"] == "queued"
    assert manager.preview_status("ui-evidence") == {"status": "queued"}
    assert (manager.config.workspaces_root / ".preview-jobs" / "ui-evidence.json").is_file()


def test_autonomous_run_is_bounded_and_archives_only_passing_changed_work(manager: RepoOpsManager, monkeypatch: pytest.MonkeyPatch):
    manager.create_workspace("autonomous")
    before = manager.read_file("README.md", task_id="autonomous")
    manager.write_file("autonomous", "README.md", "Autonomous improvement\n", before["sha256"])
    started = manager.start_autonomous_run("autonomous")
    assert started["state"] == "running"
    assert started["policy"]["max_runtime_seconds"] == 86_400

    def passing_check(task_id: str, preset: str) -> dict[str, object]:
        result: dict[str, object] = {"preset": preset, "passed": True, "recorded_at": manager._timestamp()}
        manager._record_check(task_id, result)
        return result

    monkeypatch.setattr(manager, "run_check", passing_check)
    completed = manager.evaluate_workspace("autonomous")

    assert completed["state"] == "review_ready"
    assert completed["evaluations"][-1]["score"] == 1.0
    assert manager.workspace_status("autonomous")["state"] == "review_ready"


def test_autonomous_run_pauses_at_hard_storage_budget(manager: RepoOpsManager):
    manager.create_workspace("budgeted")
    manager.start_autonomous_run("budgeted", policy={"max_storage_bytes": 1})

    status = manager.autonomous_status("budgeted")

    assert status["state"] == "paused"
    assert status["stop_reason"] == "storage_budget_exceeded"


def test_autonomous_run_stops_after_three_non_improving_evaluations(manager: RepoOpsManager, monkeypatch: pytest.MonkeyPatch):
    manager.create_workspace("plateau")
    manager.start_autonomous_run("plateau")

    def failing_check(task_id: str, preset: str) -> dict[str, object]:
        return {"preset": preset, "passed": False, "recorded_at": manager._timestamp()}

    monkeypatch.setattr(manager, "run_check", failing_check)
    manager.evaluate_workspace("plateau")
    manager.evaluate_workspace("plateau")
    manager.evaluate_workspace("plateau")
    status = manager.evaluate_workspace("plateau")

    assert status["state"] == "stopped"
    assert status["stop_reason"] == "non_improving_evaluation_limit"


def test_quarantine_evidence_requires_manual_promotion(tmp_path: Path):
    artifact = tmp_path / "skill.zip"
    output = tmp_path / "evidence.json"
    artifact.write_bytes(b"untrusted skill artifact")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "repo_ops.quarantine",
            "--source",
            "https://catalog.example/skill",
            "--package",
            "example-skill",
            "--version",
            "1.0.0",
            "--license",
            "MIT",
            "--artifact",
            str(artifact),
            "--smoke-result",
            "passed",
            "--snapshot",
            "before-example-skill",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert '"promotion": "manual approval required"' in output.read_text(encoding="utf-8")


def test_lifecycle_archives_evidence_and_restores_to_a_new_workspace(manager: RepoOpsManager):
    manager.create_workspace("recoverable")
    original = manager.read_file("README.md", task_id="recoverable")
    manager.write_file("recoverable", "README.md", "Recovered work\n", original["sha256"])
    manager._record_check("recoverable", {"preset": "compile", "passed": True, "recorded_at": manager._timestamp()})
    (manager._workspace("recoverable") / ".env").write_text("secret=nope\n", encoding="utf-8")

    archived = manager.archive_workspace("recoverable")

    assert archived["state"] == "archived"
    assert archived["archive_available"] is True
    archive_dir = manager._archive_root / archived["archive"]["directory"]
    assert (archive_dir / "workspace.tar.gz").is_file()
    assert (archive_dir / "workspace.diff").read_text(encoding="utf-8")
    assert not manager._workspace("recoverable").exists()

    restored = manager.restore_workspace("recoverable")

    assert restored["task_id"] != "recoverable"
    assert restored["restored_from"] == "recoverable"
    assert (Path(restored["workspace"]) / "README.md").read_text(encoding="utf-8") == "Recovered work\n"
    assert not (Path(restored["workspace"]) / ".env").exists()


def test_review_ready_is_protected_and_cleanup_expiry_creates_tombstone(manager: RepoOpsManager):
    manager.create_workspace("review-candidate")
    before = manager.read_file("README.md", task_id="review-candidate")
    manager.write_file("review-candidate", "README.md", "For review\n", before["sha256"])
    manager._record_check("review-candidate", {"preset": "unit", "passed": True, "recorded_at": manager._timestamp()})
    review = manager.mark_review_ready("review-candidate")

    assert review["state"] == "review_ready"
    future = manager._now() + timedelta(days=30)
    assert manager.cleanup_workspaces(dry_run=True, now=future)["actions"] == []

    manager.create_workspace("expire-me")
    archived = manager.archive_workspace("expire-me")
    actions = manager.cleanup_workspaces(dry_run=False, now=future)["actions"]

    assert {item["action"] for item in actions} == {"expire"}
    assert manager.workspace_status("expire-me")["state"] == "expired"
    assert not (manager._archive_root / archived["archive"]["directory"]).exists()
    assert (manager._archive_root / "tombstones.jsonl").is_file()


def test_lifecycle_renews_activity_and_cleanup_previews_stale_workspaces(manager: RepoOpsManager):
    manager.create_workspace("stale-work")
    initial = manager.workspace_status("stale-work")
    manager.renew_workspace_lease("stale-work")
    renewed = manager.workspace_status("stale-work")

    assert renewed["lease_until"] >= initial["lease_until"]
    payload = manager._load_lifecycle("stale-work")
    payload["last_activity_at"] = manager._timestamp(manager._now() - timedelta(days=8))
    manager._save_lifecycle("stale-work", payload)
    preview = manager.cleanup_workspaces(dry_run=True)

    assert preview == {"dry_run": True, "actions": [{"task_id": "stale-work", "action": "archive"}]}
    health = manager.workspace_health("stale-work")
    assert health["recoverable"] is True


def test_restore_rejects_a_tampered_archive_manifest(manager: RepoOpsManager):
    manager.create_workspace("tampered")
    archive = manager.archive_workspace("tampered")
    manifest = manager._archive_root / archive["archive"]["directory"] / "manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")

    with pytest.raises(RepoOpsError, match="signature"):
        manager.restore_workspace("tampered")
