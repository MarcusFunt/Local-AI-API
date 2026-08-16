"""Loopback-only FastAPI service for the v0.1 lab controller's first phases."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from hmac import compare_digest
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import ControllerSettings, settings
from .database import (
    ControllerDatabase,
    IdempotencyConflictError,
    LeaseConflictError,
    LeaseExpiredError,
    WorkerNotRegisteredError,
)
from .models import (
    ControllerStatus,
    JobDetail,
    JobListResponse,
    JobState,
    JobSubmission,
    JobSubmissionResult,
    WorkerAttemptReference,
    WorkerClaimResponse,
    WorkerClaimRequest,
    WorkerCompletion,
    WorkerHeartbeatResponse,
    WorkerRegistration,
)

logger = logging.getLogger(__name__)


def _error(status_code: int, message: str, code: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": {"message": message, "type": "invalid_request_error", "code": code}},
    )


def _assert_allowed_candidate_scope(job: JobSubmission, config: ControllerSettings) -> None:
    if job.candidate is None:
        return
    if not any(
        job.candidate.target.startswith(prefix) for prefix in config.candidate_target_prefixes
    ):
        allowed = ", ".join(config.candidate_target_prefixes)
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Candidate target must start with one of: {allowed}.",
            "candidate_target_not_allowed",
        )
    disallowed_changes = sorted(
        set(job.candidate.allowed_changes) - set(config.candidate_change_fields)
    )
    if disallowed_changes:
        allowed = ", ".join(config.candidate_change_fields)
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Candidate allowed_changes must be configured fields: {allowed}.",
            "candidate_change_not_allowed",
        )


def _require_worker_token(request: Request) -> None:
    config: ControllerSettings = request.app.state.controller_settings
    if not config.controller_worker_token:
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Worker API is disabled until CONTROLLER_WORKER_TOKEN is configured.",
            "worker_api_not_configured",
        )
    received = request.headers.get("x-lab-worker-token", "")
    if not received or not compare_digest(received, config.controller_worker_token):
        raise _error(
            status.HTTP_401_UNAUTHORIZED,
            "Missing or invalid worker token.",
            "invalid_worker_token",
        )


def _lease_error(exc: Exception) -> HTTPException:
    if isinstance(exc, WorkerNotRegisteredError):
        return _error(status.HTTP_409_CONFLICT, str(exc), "worker_not_registered")
    if isinstance(exc, LeaseExpiredError):
        return _error(status.HTTP_409_CONFLICT, str(exc), "lease_expired")
    return _error(status.HTTP_409_CONFLICT, str(exc), "lease_conflict")


def create_app(controller_settings: ControllerSettings | None = None) -> FastAPI:
    """Create a testable controller app; startup applies SQLite migrations."""
    config = controller_settings or settings
    database = ControllerDatabase(config.controller_database_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database.migrate()
        app.state.database = database
        app.state.controller_settings = config
        stop_reaper = asyncio.Event()

        async def reap_expired_leases() -> None:
            while not stop_reaper.is_set():
                try:
                    reaped = database.reap_expired_leases()
                    if reaped:
                        logger.warning("Reaped %d expired lab-controller worker lease(s).", reaped)
                except Exception:
                    logger.exception("Could not reap expired lab-controller worker leases.")
                try:
                    await asyncio.wait_for(
                        stop_reaper.wait(), timeout=config.controller_scheduler_interval_seconds
                    )
                except TimeoutError:
                    continue

        reaper_task = asyncio.create_task(
            reap_expired_leases(), name="lab-controller-lease-reaper"
        )
        try:
            yield
        finally:
            stop_reaper.set()
            await reaper_task

    app = FastAPI(
        title="Local AI Lab Controller",
        description="Loopback-only durable job control plane for local AI experiments.",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            content = exc.detail
        else:
            content = {
                "error": {"message": str(exc.detail), "type": "api_error", "code": "error"}
            }
        return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "message": "Job request validation failed.",
                    "type": "invalid_request_error",
                    "code": "invalid_job",
                }
            },
        )

    @app.get("/health")
    def health(request: Request) -> dict[str, str | int]:
        database_for_request: ControllerDatabase = request.app.state.database
        return {
            "status": "ok",
            "service": "local-ai-lab-controller",
            "schema_version": database_for_request.schema_version(),
        }

    @app.get("/v1/lab/status", response_model=ControllerStatus)
    def controller_status(request: Request) -> ControllerStatus:
        database_for_request: ControllerDatabase = request.app.state.database
        return ControllerStatus(
            schema_version=database_for_request.schema_version(),
            jobs_by_state=database_for_request.jobs_by_state(),
            workers_by_state=database_for_request.workers_by_state(),
        )

    @app.post(
        "/v1/lab/jobs",
        response_model=JobSubmissionResult,
        status_code=status.HTTP_201_CREATED,
    )
    def submit_job(job: JobSubmission, request: Request, response: Response) -> JobSubmissionResult:
        config_for_request: ControllerSettings = request.app.state.controller_settings
        _assert_allowed_candidate_scope(job, config_for_request)
        database_for_request: ControllerDatabase = request.app.state.database
        try:
            created_job, created = database_for_request.create_job(job)
        except IdempotencyConflictError as exc:
            raise _error(
                status.HTTP_409_CONFLICT,
                str(exc),
                "idempotency_conflict",
            ) from exc
        response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return JobSubmissionResult(**created_job, created=created)

    @app.get("/v1/lab/jobs", response_model=JobListResponse)
    def list_jobs(
        request: Request,
        state: JobState | None = None,
        limit: int = Query(default=50, ge=1),
        offset: int = Query(default=0, ge=0),
    ) -> JobListResponse:
        config_for_request: ControllerSettings = request.app.state.controller_settings
        if limit > config_for_request.controller_max_list_limit:
            raise _error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"limit must not exceed {config_for_request.controller_max_list_limit}.",
                "list_limit_exceeded",
            )
        database_for_request: ControllerDatabase = request.app.state.database
        jobs, total = database_for_request.list_jobs(state, limit, offset)
        return JobListResponse(jobs=jobs, limit=limit, offset=offset, total=total)

    @app.get("/v1/lab/jobs/{job_id}", response_model=JobDetail)
    def get_job(job_id: str, request: Request) -> JobDetail:
        database_for_request: ControllerDatabase = request.app.state.database
        job = database_for_request.get_job(job_id)
        if job is None:
            raise _error(status.HTTP_404_NOT_FOUND, "Job was not found.", "job_not_found")
        return JobDetail(**job)

    @app.post("/v1/lab/workers/register")
    def register_worker(
        worker: WorkerRegistration, request: Request
    ) -> dict[str, object]:
        _require_worker_token(request)
        database_for_request: ControllerDatabase = request.app.state.database
        return database_for_request.register_worker(
            worker.worker_id,
            worker.worker_class,
            worker.image_digest,
            worker.capabilities,
        )

    @app.post("/v1/lab/worker-claims/next", response_model=WorkerClaimResponse)
    def claim_next_job(
        claim_request: WorkerClaimRequest, request: Request
    ) -> WorkerClaimResponse:
        _require_worker_token(request)
        config_for_request: ControllerSettings = request.app.state.controller_settings
        database_for_request: ControllerDatabase = request.app.state.database
        try:
            claim = database_for_request.claim_next_job(
                claim_request.worker_id,
                claim_request.worker_class,
                config_for_request.controller_lease_seconds,
            )
        except (WorkerNotRegisteredError, LeaseConflictError, LeaseExpiredError) as exc:
            raise _lease_error(exc) from exc
        return WorkerClaimResponse(claim=claim)

    @app.post(
        "/v1/lab/attempts/{attempt_id}/heartbeat",
        response_model=WorkerHeartbeatResponse,
    )
    def heartbeat_attempt(
        attempt_id: str, heartbeat: WorkerAttemptReference, request: Request
    ) -> WorkerHeartbeatResponse:
        _require_worker_token(request)
        config_for_request: ControllerSettings = request.app.state.controller_settings
        database_for_request: ControllerDatabase = request.app.state.database
        try:
            renewed = database_for_request.heartbeat_attempt(
                attempt_id,
                heartbeat.worker_id,
                heartbeat.fence_token,
                config_for_request.controller_lease_seconds,
            )
        except (WorkerNotRegisteredError, LeaseConflictError, LeaseExpiredError) as exc:
            raise _lease_error(exc) from exc
        return WorkerHeartbeatResponse(**renewed)

    @app.post("/v1/lab/attempts/{attempt_id}/complete", response_model=JobDetail)
    def complete_attempt(
        attempt_id: str, completion: WorkerCompletion, request: Request
    ) -> JobDetail:
        _require_worker_token(request)
        database_for_request: ControllerDatabase = request.app.state.database
        try:
            job = database_for_request.complete_attempt(
                attempt_id,
                completion.worker_id,
                completion.fence_token,
                completion.outcome,
                completion.exit_class,
                completion.summary,
            )
        except (WorkerNotRegisteredError, LeaseConflictError, LeaseExpiredError) as exc:
            raise _lease_error(exc) from exc
        return JobDetail(**job)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "lab_controller.main:app",
        host=settings.controller_host,
        port=settings.controller_port,
    )
