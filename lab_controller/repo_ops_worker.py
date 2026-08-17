"""A constrained pull worker that prepares one disposable repo-ops workspace.

This adapter deliberately stops at workspace creation.  It does not invoke a
model, modify a workspace, run a check, create a commit, or promote anything.
Its only secret is the controller worker token; it receives no production
credential.
"""
from __future__ import annotations

import argparse
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from repo_ops.core import RepoOpsConfig, RepoOpsError, RepoOpsManager

from .models import JobSubmission, WorkerClaim

logger = logging.getLogger(__name__)

_WORKER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CONTROLLER_HOSTS = {"127.0.0.1", "localhost", "::1", "lab-controller"}


class RepoOpsWorkerSettings(BaseSettings):
    """Environment configuration for the one phase-2 worker capability."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    controller_worker_token: str = Field(min_length=1, max_length=512)
    repo_ops_controller_base_url: str = "http://127.0.0.1:8091"
    repo_ops_controller_worker_id: str = "repo-ops-workspace-adapter"
    repo_ops_controller_image_digest: str = "local-dev-unpinned"
    repo_ops_controller_poll_seconds: int = Field(default=5, ge=1, le=60)
    repo_ops_controller_heartbeat_seconds: int = Field(default=10, ge=5, le=15)

    @field_validator("controller_worker_token")
    @classmethod
    def validate_worker_token(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("CONTROLLER_WORKER_TOKEN must not be empty.")
        return normalized

    @field_validator("repo_ops_controller_base_url")
    @classmethod
    def validate_controller_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlsplit(normalized)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in _CONTROLLER_HOSTS
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
            or parsed.port is None
        ):
            raise ValueError(
                "REPO_OPS_CONTROLLER_BASE_URL must be an http URL for the local "
                "controller, without credentials or a path."
            )
        return normalized

    @field_validator("repo_ops_controller_worker_id")
    @classmethod
    def validate_worker_id(cls, value: str) -> str:
        normalized = value.strip()
        if not _WORKER_ID_RE.fullmatch(normalized):
            raise ValueError("REPO_OPS_CONTROLLER_WORKER_ID is not a valid worker ID.")
        return normalized

    @field_validator("repo_ops_controller_image_digest")
    @classmethod
    def validate_image_digest(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 256:
            raise ValueError("REPO_OPS_CONTROLLER_IMAGE_DIGEST must be 1-256 characters.")
        return normalized


class ControllerClientError(RuntimeError):
    """Raised when the local controller cannot be safely contacted."""


class _ControllerClient(Protocol):
    def register(self) -> None: ...

    def claim(self) -> WorkerClaim | None: ...

    def heartbeat(self, claim: WorkerClaim) -> None: ...

    def complete(
        self,
        claim: WorkerClaim,
        outcome: str,
        exit_class: str,
        summary: dict[str, str],
    ) -> None: ...


class ControllerClient:
    """Minimal authenticated client for the worker-only controller endpoints."""

    def __init__(self, settings: RepoOpsWorkerSettings) -> None:
        self.base_url = settings.repo_ops_controller_base_url
        self.worker_id = settings.repo_ops_controller_worker_id
        self.image_digest = settings.repo_ops_controller_image_digest
        self.headers = {"X-Lab-Worker-Token": settings.controller_worker_token}
        self.client = httpx.Client(timeout=httpx.Timeout(15.0), headers=self.headers)

    def close(self) -> None:
        self.client.close()

    def register(self) -> None:
        self._request(
            "POST",
            "/v1/lab/workers/register",
            json={
                "worker_id": self.worker_id,
                "worker_class": "repo_ops",
                "image_digest": self.image_digest,
                "capabilities": ["workspace_prepare"],
            },
        )

    def claim(self) -> WorkerClaim | None:
        body = self._request(
            "POST",
            "/v1/lab/worker-claims/next",
            json={"worker_id": self.worker_id, "worker_class": "repo_ops"},
        )
        claim = body.get("claim")
        if claim is None:
            return None
        try:
            return WorkerClaim.model_validate(claim)
        except ValidationError as exc:
            raise ControllerClientError("The local controller returned an invalid claim.") from exc

    def heartbeat(self, claim: WorkerClaim) -> None:
        self._request(
            "POST",
            f"/v1/lab/attempts/{claim.attempt_id}/heartbeat",
            json={"worker_id": self.worker_id, "fence_token": claim.fence_token},
        )

    def complete(
        self,
        claim: WorkerClaim,
        outcome: str,
        exit_class: str,
        summary: dict[str, str],
    ) -> None:
        self._request(
            "POST",
            f"/v1/lab/attempts/{claim.attempt_id}/complete",
            json={
                "worker_id": self.worker_id,
                "fence_token": claim.fence_token,
                "outcome": outcome,
                "exit_class": exit_class,
                "summary": summary,
            },
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self.client.request(method, f"{self.base_url}{path}", **kwargs)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ControllerClientError("The local controller request failed.") from exc
        if not isinstance(body, dict):
            raise ControllerClientError("The local controller returned an invalid response.")
        return body


class _WorkerPolicyError(ValueError):
    """Raised when a claimed job falls outside this adapter's single capability."""


@dataclass
class _HeartbeatMonitor:
    client: _ControllerClient
    claim: WorkerClaim
    interval_seconds: int
    stop_event: threading.Event = field(default_factory=threading.Event, init=False)
    failure: Exception | None = None

    def __post_init__(self) -> None:
        self._thread = threading.Thread(target=self._run, name="repo-ops-lease-heartbeat")
        self._thread.daemon = True

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self._thread.join(timeout=max(self.interval_seconds + 2, 17))

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval_seconds):
            try:
                self.client.heartbeat(self.claim)
            except ControllerClientError as exc:
                self.failure = exc
                logger.warning("Lease heartbeat failed; leaving completion to expiry recovery.")
                return


class RepoOpsWorkspaceWorker:
    """Claims at most one job and only creates its disposable repo-ops workspace."""

    def __init__(
        self,
        settings: RepoOpsWorkerSettings,
        manager: RepoOpsManager | None = None,
        client: _ControllerClient | None = None,
    ) -> None:
        self.settings = settings
        self.manager = manager or RepoOpsManager(RepoOpsConfig.from_environment())
        self.client = client or ControllerClient(settings)

    def close(self) -> None:
        if isinstance(self.client, ControllerClient):
            self.client.close()

    def register(self) -> None:
        self.client.register()

    def run_once(self) -> bool:
        """Claim and process one job; return whether a lease was claimed."""
        claim = self.client.claim()
        if claim is None:
            return False

        try:
            self.client.heartbeat(claim)
        except ControllerClientError:
            logger.warning("Claimed job could not be started; it will recover by lease expiry.")
            return True

        monitor = _HeartbeatMonitor(
            client=self.client,
            claim=claim,
            interval_seconds=self.settings.repo_ops_controller_heartbeat_seconds,
        )
        monitor.start()
        try:
            summary = self._prepare_workspace(claim)
            outcome, exit_class = "succeeded", "success"
        except _WorkerPolicyError:
            logger.warning("Controller offered work outside the repo-ops workspace capability.")
            summary = {"operation": "workspace_prepare", "reason": "worker_policy_denied"}
            outcome, exit_class = "failed", "policy_denied"
        except RepoOpsError:
            logger.warning("Repo-ops could not prepare a disposable workspace.")
            summary = {"operation": "workspace_prepare", "reason": "repo_ops_unavailable"}
            outcome, exit_class = "failed", "infra_error"
        except Exception:
            logger.warning("Unexpected repo-ops workspace adapter failure.")
            summary = {"operation": "workspace_prepare", "reason": "unexpected_worker_error"}
            outcome, exit_class = "failed", "infra_error"
        finally:
            monitor.stop()

        if monitor.failure is not None:
            return True
        try:
            self.client.complete(claim, outcome, exit_class, summary)
        except ControllerClientError:
            logger.warning("Could not record attempt completion; lease expiry will recover it.")
        return True

    def run_forever(self) -> None:
        """Keep polling; a controller outage never turns into uncontrolled local work."""
        while True:
            try:
                self.register()
                claimed = self.run_once()
            except ControllerClientError:
                logger.warning("Controller unavailable; retrying after the poll interval.")
                claimed = False
            if not claimed:
                time.sleep(self.settings.repo_ops_controller_poll_seconds)

    def _prepare_workspace(self, claim: WorkerClaim) -> dict[str, str]:
        job = claim.job.spec
        self._assert_supported_job(job)
        assert job.candidate is not None
        task_id = self._task_id(claim.attempt_id)
        workspace = self.manager.create_workspace(
            task_id=task_id,
            base_revision=job.candidate.base_revision.removeprefix("git:"),
        )
        return {
            "operation": "workspace_prepared",
            "task_id": task_id,
            "branch": workspace["branch"],
            "base_revision": job.candidate.base_revision.removeprefix("git:"),
            "source_access": "read_only",
            "network": "controller_only",
        }

    @staticmethod
    def _assert_supported_job(job: JobSubmission) -> None:
        candidate = job.candidate
        if (
            job.kind != "candidate_build"
            or candidate is None
            or candidate.type != "code_patch"
            or candidate.allowed_changes != ["patch_manifest"]
            or job.isolation.worker_class != "repo_ops"
            or job.isolation.network != "none"
            or job.isolation.source_access != "read_only"
            or job.isolation.credential_profile != "none"
        ):
            raise _WorkerPolicyError("This adapter supports only read-only code-patch workspace jobs.")

    @staticmethod
    def _task_id(attempt_id: str) -> str:
        suffix = attempt_id.removeprefix("attempt_")
        task_id = f"lab-{suffix}"
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", task_id):
            raise _WorkerPolicyError("Controller attempt ID cannot become a repo-ops task ID.")
        return task_id


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the constrained repo-ops lab worker.")
    parser.add_argument("--once", action="store_true", help="Claim and process at most one job.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    worker = RepoOpsWorkspaceWorker(RepoOpsWorkerSettings())
    try:
        if args.once:
            worker.register()
            worker.run_once()
        else:
            worker.run_forever()
    finally:
        worker.close()


if __name__ == "__main__":
    main()
