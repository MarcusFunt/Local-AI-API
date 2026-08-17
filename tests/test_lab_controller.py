"""Focused tests for the v0.1 local lab controller's implemented phases."""
from __future__ import annotations

from contextlib import asynccontextmanager
import sqlite3
from typing import AsyncIterator

import httpx
import pytest
from pydantic import ValidationError

from lab_controller.config import ControllerSettings
from lab_controller.database import ControllerDatabase
from lab_controller.main import create_app
from lab_controller.models import JobDetail, JobSubmission, WorkerClaim
from lab_controller.repo_ops_worker import RepoOpsWorkspaceWorker, RepoOpsWorkerSettings


def _candidate_job_payload(idempotency_key: str = "candidate-build-001") -> dict:
    return {
        "schema_version": "lab.job/v1",
        "idempotency_key": idempotency_key,
        "kind": "candidate_build",
        "priority": 60,
        "baseline_release": "release-0007",
        "input_artifacts": ["sha256:" + "a" * 64],
        "candidate": {
            "type": "policy",
            "target": "agent-zero/research-profile",
            "allowed_changes": ["system_prompt", "stage_token_limits"],
            "base_revision": "git:" + "b" * 40,
        },
        "isolation": {
            "worker_class": "repo_ops",
            "network": "none",
            "source_access": "read_only",
            "credential_profile": "none",
        },
        "budget": {
            "wall_seconds": 14_400,
            "cpu_seconds": 7_200,
            "gpu_seconds": 3_600,
            "max_memory_mib": 16_384,
            "max_disk_mib": 20_480,
            "max_model_calls": 30,
            "max_attempts": 2,
        },
        "evaluation": {
            "suite_id": "policy-private-regression",
            "suite_revision": "2026-08-16.1",
            "baseline_required": True,
            "repeat_count": 3,
        },
        "review": {"required": True},
        "labels": {"project": "local-ai-api", "risk": "medium"},
    }


def _workspace_job_payload(idempotency_key: str = "workspace-build-001") -> dict:
    payload = _candidate_job_payload(idempotency_key)
    payload["candidate"] = {
        "type": "code_patch",
        "target": "repo-ops/workspace-adapter",
        "allowed_changes": ["patch_manifest"],
        "base_revision": "git:" + "b" * 40,
    }
    return payload


@pytest.fixture()
def controller_settings(tmp_path) -> ControllerSettings:
    return ControllerSettings(
        _env_file=None,
        controller_host="127.0.0.1",
        controller_database_path=str(tmp_path / "controller.sqlite3"),
        controller_max_list_limit=100,
        controller_worker_token="test-worker-token",
    )


@asynccontextmanager
async def _controller_client(settings: ControllerSettings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def test_migrations_are_idempotent_and_recorded(tmp_path) -> None:
    database_path = tmp_path / "controller.sqlite3"
    database = ControllerDatabase(str(database_path))

    database.migrate()
    database.migrate()

    assert database.schema_version() == 2
    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        }
        migration_rows = connection.execute("SELECT version FROM schema_migrations").fetchall()
    assert {"events", "job_attempts", "jobs", "schema_migrations", "workers"}.issubset(tables)
    assert migration_rows == [(1,), (2,)]


def test_controller_rejects_non_loopback_binding() -> None:
    with pytest.raises(ValidationError):
        ControllerSettings(_env_file=None, controller_host="0.0.0.0")


async def test_submit_job_then_read_status_and_events(
    controller_settings: ControllerSettings,
) -> None:
    async with _controller_client(controller_settings) as client:
        submission = await client.post("/v1/lab/jobs", json=_candidate_job_payload())
        health = await client.get("/health")
        status = await client.get("/v1/lab/status")
        listed = await client.get("/v1/lab/jobs?state=queued")
        detail = await client.get(f"/v1/lab/jobs/{submission.json()['id']}")

    assert submission.status_code == 201
    submitted = submission.json()
    assert submitted["created"] is True
    assert submitted["state"] == "queued"
    assert submitted["max_attempts"] == 2
    assert submitted["events"] == [
        {
            "id": 1,
            "event_type": "job_submitted",
            "created_at": submitted["created_at"],
            "payload": {"kind": "candidate_build", "schema_version": "lab.job/v1"},
        }
    ]
    assert health.status_code == 200
    assert health.json()["schema_version"] == 2
    assert status.json()["scheduler"] == "active"
    assert status.json()["worker_execution"] == "repo_ops_adapter"
    assert status.json()["jobs_by_state"] == {"queued": 1}
    assert status.json()["workers_by_state"] == {}
    assert listed.json()["total"] == 1
    assert listed.json()["jobs"][0]["id"] == submitted["id"]
    assert detail.json()["spec"]["candidate"]["target"] == "agent-zero/research-profile"


async def test_job_submission_is_idempotent_but_conflicting_reuse_is_rejected(
    controller_settings: ControllerSettings,
) -> None:
    payload = _candidate_job_payload()
    async with _controller_client(controller_settings) as client:
        first = await client.post("/v1/lab/jobs", json=payload)
        replay = await client.post("/v1/lab/jobs", json=payload)
        changed_payload = {**payload, "priority": 61}
        conflict = await client.post("/v1/lab/jobs", json=changed_payload)

    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.json()["created"] is False
    assert replay.json()["id"] == first.json()["id"]
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


async def test_job_contract_and_target_allowlist_are_enforced(
    controller_settings: ControllerSettings,
) -> None:
    invalid_contract = _candidate_job_payload("invalid-contract-001")
    invalid_contract["candidate"]["unexpected"] = "field"
    disallowed_target = _candidate_job_payload("disallowed-target-001")
    disallowed_target["candidate"]["target"] = "other/profile"
    disallowed_change = _candidate_job_payload("disallowed-change-001")
    disallowed_change["candidate"]["allowed_changes"] = ["unbounded_runtime_change"]

    async with _controller_client(controller_settings) as client:
        contract_response = await client.post("/v1/lab/jobs", json=invalid_contract)
        target_response = await client.post("/v1/lab/jobs", json=disallowed_target)
        change_response = await client.post("/v1/lab/jobs", json=disallowed_change)

    assert contract_response.status_code == 422
    assert contract_response.json()["error"]["code"] == "invalid_job"
    assert target_response.status_code == 422
    assert target_response.json()["error"]["code"] == "candidate_target_not_allowed"
    assert change_response.status_code == 422
    assert change_response.json()["error"]["code"] == "candidate_change_not_allowed"


async def test_status_endpoints_do_not_expose_state_mutation(
    controller_settings: ControllerSettings,
) -> None:
    async with _controller_client(controller_settings) as client:
        submission = await client.post("/v1/lab/jobs", json=_candidate_job_payload())
        mutation = await client.patch(
            f"/v1/lab/jobs/{submission.json()['id']}", json={"state": "running"}
        )
        missing = await client.get("/v1/lab/jobs/job_missing")

    assert mutation.status_code == 405
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "job_not_found"


async def test_worker_api_requires_a_token_and_claims_only_supported_jobs(
    controller_settings: ControllerSettings,
) -> None:
    worker = {
        "worker_id": "repo-ops-workspace-adapter",
        "worker_class": "repo_ops",
        "image_digest": "sha256:" + "c" * 64,
        "capabilities": ["workspace_prepare"],
    }
    async with _controller_client(controller_settings) as client:
        no_token = await client.post("/v1/lab/workers/register", json=worker)
        registered = await client.post(
            "/v1/lab/workers/register",
            json=worker,
            headers={"X-Lab-Worker-Token": "test-worker-token"},
        )
        policy_job = await client.post("/v1/lab/jobs", json=_candidate_job_payload())
        unsupported_patch = _workspace_job_payload("workspace-build-unsupported")
        unsupported_patch["candidate"]["allowed_changes"] = ["system_prompt"]
        unsupported_patch_response = await client.post("/v1/lab/jobs", json=unsupported_patch)
        claim = await client.post(
            "/v1/lab/worker-claims/next",
            json={"worker_id": worker["worker_id"], "worker_class": "repo_ops"},
            headers={"X-Lab-Worker-Token": "test-worker-token"},
        )

    assert no_token.status_code == 401
    assert no_token.json()["error"]["code"] == "invalid_worker_token"
    assert registered.status_code == 200
    assert registered.json()["state"] == "ready"
    assert policy_job.status_code == 201
    assert unsupported_patch_response.status_code == 201
    assert claim.status_code == 200
    assert claim.json() == {"claim": None}


async def test_worker_lease_heartbeat_and_completion_are_fenced(
    controller_settings: ControllerSettings,
) -> None:
    headers = {"X-Lab-Worker-Token": "test-worker-token"}
    worker = {
        "worker_id": "repo-ops-workspace-adapter",
        "worker_class": "repo_ops",
        "image_digest": "sha256:" + "c" * 64,
        "capabilities": ["workspace_prepare"],
    }
    async with _controller_client(controller_settings) as client:
        submitted = await client.post("/v1/lab/jobs", json=_workspace_job_payload())
        registration = await client.post("/v1/lab/workers/register", json=worker, headers=headers)
        claim_response = await client.post(
            "/v1/lab/worker-claims/next",
            json={"worker_id": worker["worker_id"], "worker_class": "repo_ops"},
            headers=headers,
        )
        claim = claim_response.json()["claim"]
        heartbeat = await client.post(
            f"/v1/lab/attempts/{claim['attempt_id']}/heartbeat",
            json={"worker_id": worker["worker_id"], "fence_token": claim["fence_token"]},
            headers=headers,
        )
        complete = await client.post(
            f"/v1/lab/attempts/{claim['attempt_id']}/complete",
            json={
                "worker_id": worker["worker_id"],
                "fence_token": claim["fence_token"],
                "outcome": "succeeded",
                "exit_class": "success",
                "summary": {"operation": "workspace_prepared"},
            },
            headers=headers,
        )
        status = await client.get("/v1/lab/status")

    assert submitted.status_code == 201
    assert registration.status_code == 200
    assert claim_response.status_code == 200
    assert claim["job_id"] == submitted.json()["id"]
    assert heartbeat.status_code == 200
    assert heartbeat.json()["state"] == "running"
    assert complete.status_code == 200
    assert complete.json()["state"] == "succeeded"
    assert complete.json()["attempts"][0]["state"] == "finished"
    assert status.json()["workers_by_state"] == {"ready": 1}


async def test_expired_lease_requeues_with_a_new_fence_token(
    controller_settings: ControllerSettings,
) -> None:
    headers = {"X-Lab-Worker-Token": "test-worker-token"}
    worker = {
        "worker_id": "repo-ops-workspace-adapter",
        "worker_class": "repo_ops",
        "image_digest": "sha256:" + "c" * 64,
        "capabilities": ["workspace_prepare"],
    }
    async with _controller_client(controller_settings) as client:
        submitted = await client.post("/v1/lab/jobs", json=_workspace_job_payload())
        await client.post("/v1/lab/workers/register", json=worker, headers=headers)
        first = await client.post(
            "/v1/lab/worker-claims/next",
            json={"worker_id": worker["worker_id"], "worker_class": "repo_ops"},
            headers=headers,
        )
        with sqlite3.connect(controller_settings.controller_database_path) as connection:
            connection.execute(
                "UPDATE job_attempts SET lease_expires_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00Z", first.json()["claim"]["attempt_id"]),
            )
        second = await client.post(
            "/v1/lab/worker-claims/next",
            json={"worker_id": worker["worker_id"], "worker_class": "repo_ops"},
            headers=headers,
        )
        stale_heartbeat = await client.post(
            f"/v1/lab/attempts/{first.json()['claim']['attempt_id']}/heartbeat",
            json={
                "worker_id": worker["worker_id"],
                "fence_token": first.json()["claim"]["fence_token"],
            },
            headers=headers,
        )
        detail = await client.get(f"/v1/lab/jobs/{submitted.json()['id']}")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["claim"]["fence_token"] == first.json()["claim"]["fence_token"] + 1
    assert stale_heartbeat.status_code == 409
    assert stale_heartbeat.json()["error"]["code"] == "lease_conflict"
    assert [attempt["state"] for attempt in detail.json()["attempts"]] == ["lost", "leased"]
    assert "attempt_lost" in [event["event_type"] for event in detail.json()["events"]]


class _FakeWorkerClient:
    def __init__(self, claim: WorkerClaim | None) -> None:
        self.claim_to_return = claim
        self.heartbeats = 0
        self.completions: list[tuple[str, str, dict[str, str]]] = []

    def register(self) -> None:
        return None

    def claim(self) -> WorkerClaim | None:
        claim, self.claim_to_return = self.claim_to_return, None
        return claim

    def heartbeat(self, claim: WorkerClaim) -> None:
        self.heartbeats += 1

    def complete(
        self,
        claim: WorkerClaim,
        outcome: str,
        exit_class: str,
        summary: dict[str, str],
    ) -> None:
        self.completions.append((outcome, exit_class, summary))


class _FakeRepoOpsManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def create_workspace(self, task_id: str, base_revision: str) -> dict[str, str]:
        self.calls.append((task_id, base_revision))
        return {"task_id": task_id, "branch": f"agent/{task_id}", "workspace": "/hidden"}


def test_repo_ops_adapter_prepares_a_workspace_without_disclosing_its_path() -> None:
    job = JobSubmission.model_validate(_workspace_job_payload())
    detail = JobDetail(
        id="job_" + "d" * 32,
        kind="candidate_build",
        state="leased",
        priority=60,
        baseline_release="release-0007",
        parent_job_id=None,
        max_attempts=2,
        attempt_count=1,
        next_run_at="2026-08-16T00:00:00Z",
        deadline_at=None,
        created_at="2026-08-16T00:00:00Z",
        updated_at="2026-08-16T00:00:00Z",
        spec=job,
        events=[],
        attempts=[],
    )
    claim = WorkerClaim(
        attempt_id="attempt_" + "e" * 32,
        job_id=detail.id,
        worker_id="repo-ops-workspace-adapter",
        fence_token=1,
        lease_expires_at="2026-08-16T00:01:00Z",
        job=detail,
    )
    settings = RepoOpsWorkerSettings(
        _env_file=None,
        controller_worker_token="test-worker-token",
        repo_ops_controller_poll_seconds=1,
        repo_ops_controller_heartbeat_seconds=5,
    )
    client = _FakeWorkerClient(claim)
    manager = _FakeRepoOpsManager()
    worker = RepoOpsWorkspaceWorker(settings, manager=manager, client=client)

    assert worker.run_once() is True
    assert client.heartbeats == 1
    assert manager.calls == [("lab-" + "e" * 32, "b" * 40)]
    assert client.completions == [
        (
            "succeeded",
            "success",
            {
                "operation": "workspace_prepared",
                "task_id": "lab-" + "e" * 32,
                "branch": "agent/lab-" + "e" * 32,
                "base_revision": "b" * 40,
                "source_access": "read_only",
                "network": "controller_only",
            },
        )
    ]
    assert "workspace" not in client.completions[0][2]
