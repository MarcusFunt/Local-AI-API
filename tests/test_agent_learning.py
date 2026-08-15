"""Tests for redacted cross-loop learning records and local-only promotion."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_learning import LearningRecordStore, build_learning_record, create_policy_candidate, summarize_text
from agent_learning.promotion import ALL_GATED_CHECKS, PromotionController, PromotionError


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _candidate(path: Path, base_revision: str) -> Path:
    patch = path / "candidate.patch"
    patch.write_text(
        "diff --git a/README.md b/README.md\n"
        "index 0000000..1111111 100644\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1 +1 @@\n"
        "-Repository worker fixture\n"
        "+Improved repository worker fixture\n",
        encoding="utf-8",
    )
    payload = {
        "version": 1,
        "candidate_id": "fixture-improvement",
        "base_revision": base_revision,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "patch_file": patch.name,
        "checks": {name: {"passed": True} for name in ALL_GATED_CHECKS},
        "quality_gate": {"passed": True},
        "public_eval_gate": {"passed": True},
        "dependency_security": {"passed": True},
    }
    manifest = path / "candidate.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return manifest


def test_learning_records_store_only_fingerprints(tmp_path: Path) -> None:
    secret_prompt = "private deployment secret must never be stored"
    record = build_learning_record(
        surface="gateway_agent",
        outcome="completed",
        policy_version="agent-policy-v1",
        metrics={"steps_completed": 5, "elapsed_ms": 100},
        trace={"request": summarize_text(secret_prompt)},
    )

    store = LearningRecordStore(tmp_path / "records")
    stored = store.append(record)

    assert stored["trace"]["request"]["characters"] == len(secret_prompt)
    assert secret_prompt not in store.path.read_text(encoding="utf-8")
    assert store.read()[-1]["record_id"] == record["record_id"]


def test_policy_candidates_are_bounded_to_two_approved_changes() -> None:
    candidate = create_policy_candidate(
        candidate_id="shorter-critic",
        base_policy_version="agent-policy-v1",
        hypothesis="A shorter critic output will preserve space for the writer.",
        changes={"stage_token_limits": {"critic": 800}},
    )

    assert candidate.as_dict()["candidate_id"] == "shorter-critic"
    with pytest.raises(ValueError, match="approved policy"):
        create_policy_candidate(
            candidate_id="unsafe",
            base_policy_version="agent-policy-v1",
            hypothesis="Try a deployment mutation.",
            changes={"deployment": {"restart": True}},
        )


def test_promotion_verification_requires_clean_matching_local_main(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "--initial-branch", "main")
    _git(source, "config", "user.name", "Promotion Test")
    _git(source, "config", "user.email", "promotion@example.test")
    (source / "README.md").write_text("Repository worker fixture\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "fixture")
    base = _git(source, "rev-parse", "HEAD")
    manifest = _candidate(tmp_path, base)

    controller = PromotionController(source, tmp_path / "state")
    verified = controller.verify(manifest)

    assert verified.status == "verified"
    (source / "scratch.txt").write_text("user work\n", encoding="utf-8")
    with pytest.raises(PromotionError, match="clean local main"):
        controller.verify(manifest)


def test_promotion_fast_forwards_only_after_independent_checks(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "--initial-branch", "main")
    _git(source, "config", "user.name", "Promotion Test")
    _git(source, "config", "user.email", "promotion@example.test")
    (source / "README.md").write_text("Repository worker fixture\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "fixture")
    manifest = _candidate(tmp_path, _git(source, "rev-parse", "HEAD"))

    def runner(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        if args[0] != "git":
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)

    result = PromotionController(source, tmp_path / "state", runner=runner).promote(manifest)

    assert result.status == "promoted"
    assert _git(source, "show", "-s", "--format=%s", "HEAD") == "auto-improve: fixture-improvement"
    assert _git(source, "tag", "--list", result.rollback_tag or "") == result.rollback_tag
    assert (source / "README.md").read_text(encoding="utf-8") == "Improved repository worker fixture\n"


def test_learning_store_rejects_unsafe_records_and_skips_partial_lines(tmp_path: Path) -> None:
    store = LearningRecordStore(tmp_path / "records")
    with pytest.raises(ValueError, match="gateway_agent or repo_ops"):
        store.append(
            {
                "surface": "unknown",
                "outcome": "completed",
                "policy_version": "agent-policy-v1",
                "metrics": {"steps": 1},
                "trace": {},
            }
        )
    with pytest.raises(ValueError, match="scalar values"):
        build_learning_record(
            surface="gateway_agent",
            outcome="completed",
            policy_version="agent-policy-v1",
            metrics={"nested": {"not": "allowed"}},
            trace={},
        )

    record = build_learning_record(
        surface="repo_ops",
        outcome="experiment_recorded",
        policy_version="repo-ops-v1",
        metrics={"experiment_number": 1},
        trace={"task_id": "safe-task"},
    )
    store.append(record)
    with store.path.open("a", encoding="utf-8") as stream:
        stream.write('{"partial":\n')

    assert store.read() == [record]
    with pytest.raises(ValueError, match="between 1 and 1000"):
        store.read(limit=0)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("evaluated_at", "2000-01-01T00:00:00+00:00", "older than 24 hours"),
        ("quality_gate", {"passed": False}, "quality_gate"),
        ("checks", {"unit": {"passed": True}}, "every named check"),
    ],
)
def test_promotion_rejects_stale_or_incomplete_evidence(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "--initial-branch", "main")
    _git(source, "config", "user.name", "Promotion Test")
    _git(source, "config", "user.email", "promotion@example.test")
    (source / "README.md").write_text("Repository worker fixture\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "fixture")
    manifest = _candidate(tmp_path, _git(source, "rev-parse", "HEAD"))
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload[field] = value
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PromotionError, match=message):
        PromotionController(source, tmp_path / "state").verify(manifest)


def test_promotion_reruns_every_gate_and_writes_a_redacted_audit(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "--initial-branch", "main")
    _git(source, "config", "user.name", "Promotion Test")
    _git(source, "config", "user.email", "promotion@example.test")
    (source / "README.md").write_text("Repository worker fixture\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "fixture")
    manifest = _candidate(tmp_path, _git(source, "rev-parse", "HEAD"))
    commands: list[list[str]] = []

    def runner(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        if args[0] != "git":
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)

    state_root = tmp_path / "state"
    result = PromotionController(source, state_root, runner=runner).promote(manifest)

    assert [args for args in commands if args[0] != "git"] == [
        ["python", "-m", "pytest", "tests", "-q"],
        ["python", "-m", "compileall", "gateway", "repo_ops", "agent_learning"],
        ["docker", "compose", "config"],
        ["python", "-m", "pytest", "tests/test_status_ui.py", "-q"],
        ["python", "-m", "pytest", "tests/test_repo_ops.py", "-q"],
        ["python", "-m", "pip", "check"],
    ]
    assert not any("push" in args or "deploy" in args for args in commands)
    audit = [json.loads(line) for line in (state_root / "promotions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert audit == [{
        "base_revision": result.base_revision,
        "candidate_id": "fixture-improvement",
        "promoted_revision": result.promoted_revision,
        "promoted_at": audit[0]["promoted_at"],
        "rollback_tag": result.rollback_tag,
    }]
