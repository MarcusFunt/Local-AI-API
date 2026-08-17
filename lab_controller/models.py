"""Versioned request and response contracts for the lab controller."""
from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ArtifactRef = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]
JobKind = Literal["research", "candidate_build", "candidate_evaluate", "promotion_verify"]
CandidateType = Literal[
    "policy",
    "prompt",
    "skill",
    "model_routing",
    "rag_config",
    "code_patch",
]
JobState = Literal[
    "queued",
    "leased",
    "running",
    "evaluating",
    "awaiting_review",
    "succeeded",
    "failed",
    "cancelled",
    "expired",
]
AttemptState = Literal["leased", "running", "finished", "failed", "lost", "cancelled"]
WorkerState = Literal["ready", "busy", "offline"]

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TARGET_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._/-]{0,126}$")
_GIT_REVISION_RE = re.compile(r"^git:[a-f0-9]{40,64}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CandidateSpec(_StrictModel):
    """The bounded artifact that a candidate-building job may alter."""

    type: CandidateType
    target: str = Field(min_length=3, max_length=128)
    allowed_changes: list[str] = Field(min_length=1, max_length=2)
    base_revision: str = Field(min_length=44, max_length=68)

    @field_validator("target")
    @classmethod
    def validate_target(cls, value: str) -> str:
        normalized = value.strip()
        if not _TARGET_RE.fullmatch(normalized):
            raise ValueError(
                "Candidate target must be a namespaced target such as 'gateway/quality-profile'."
            )
        return normalized

    @field_validator("allowed_changes")
    @classmethod
    def validate_allowed_changes(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item or len(item) > 80 for item in normalized):
            raise ValueError(
                "Candidate allowed_changes must contain non-empty values up to 80 characters."
            )
        if len(set(normalized)) != len(normalized):
            raise ValueError("Candidate allowed_changes must not contain duplicates.")
        return normalized

    @field_validator("base_revision")
    @classmethod
    def validate_base_revision(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _GIT_REVISION_RE.fullmatch(normalized):
            raise ValueError("Candidate base_revision must be a git: SHA-1 or SHA-256 revision.")
        return normalized


class IsolationSpec(_StrictModel):
    """Immutable execution boundary carried by every submitted job."""

    worker_class: Literal["repo_ops", "research", "evaluator"]
    network: Literal["none"] = "none"
    source_access: Literal["read_only", "none"]
    credential_profile: Literal["none"] = "none"


class ResourceBudget(_StrictModel):
    wall_seconds: int = Field(ge=1, le=86_400)
    cpu_seconds: int = Field(ge=0, le=86_400)
    gpu_seconds: int = Field(ge=0, le=86_400)
    max_memory_mib: int = Field(ge=128, le=131_072)
    max_disk_mib: int = Field(ge=128, le=20_480)
    max_model_calls: int = Field(ge=0, le=1_000)
    max_attempts: int = Field(ge=1, le=3)

    @model_validator(mode="after")
    def validate_cpu_budget(self) -> "ResourceBudget":
        if self.cpu_seconds > self.wall_seconds:
            raise ValueError("cpu_seconds cannot exceed wall_seconds in v0.1.")
        if self.gpu_seconds > self.wall_seconds:
            raise ValueError("gpu_seconds cannot exceed wall_seconds in v0.1.")
        return self


class EvaluationSpec(_StrictModel):
    suite_id: str = Field(min_length=1, max_length=128)
    suite_revision: str = Field(min_length=1, max_length=128)
    baseline_required: bool = True
    repeat_count: int = Field(ge=1, le=5)

    @field_validator("suite_id", "suite_revision")
    @classmethod
    def validate_suite_field(cls, value: str) -> str:
        normalized = value.strip()
        if not _IDENTIFIER_RE.fullmatch(normalized):
            raise ValueError(
                "Evaluation suite identifiers may contain only letters, numbers, '.', '_', ':', "
                "and '-'."
            )
        return normalized


class ReviewSpec(_StrictModel):
    required: bool = True


class JobSubmission(_StrictModel):
    """The strict `lab.job/v1` contract persisted by phase 1."""

    schema_version: Literal["lab.job/v1"]
    idempotency_key: str = Field(min_length=1, max_length=200)
    kind: JobKind
    priority: int = Field(default=50, ge=0, le=100)
    parent_job_id: str | None = Field(default=None, max_length=128)
    baseline_release: str | None = Field(default=None, max_length=128)
    input_artifacts: list[ArtifactRef] = Field(default_factory=list, max_length=32)
    candidate: CandidateSpec | None = None
    candidate_id: str | None = Field(default=None, max_length=128)
    isolation: IsolationSpec
    budget: ResourceBudget
    evaluation: EvaluationSpec | None = None
    review: ReviewSpec = Field(default_factory=ReviewSpec)
    labels: dict[str, str] = Field(default_factory=dict, max_length=16)

    @field_validator("idempotency_key", "parent_job_id", "baseline_release", "candidate_id")
    @classmethod
    def validate_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.strip()
        if not _IDENTIFIER_RE.fullmatch(normalized):
            raise ValueError(
                "Identifiers may contain only letters, numbers, '.', '_', ':', and '-'."
            )
        return normalized

    @field_validator("input_artifacts")
    @classmethod
    def validate_input_artifacts(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("input_artifacts must not contain duplicates.")
        return value

    @field_validator("labels")
    @classmethod
    def validate_labels(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for key, item in value.items():
            clean_key = key.strip()
            clean_value = item.strip()
            if not _IDENTIFIER_RE.fullmatch(clean_key) or not clean_value or len(clean_value) > 128:
                raise ValueError(
                    "labels must use identifier keys and non-empty values up to 128 characters."
                )
            normalized[clean_key] = clean_value
        return normalized

    @model_validator(mode="after")
    def validate_job_kind_contract(self) -> "JobSubmission":
        if self.kind == "research":
            if (
                self.candidate is not None
                or self.candidate_id is not None
                or self.evaluation is not None
            ):
                raise ValueError("research jobs cannot create, evaluate, or promote a candidate.")
            if self.review.required:
                raise ValueError("research jobs must set review.required=false.")
        elif self.kind == "candidate_build":
            if self.candidate is None or self.evaluation is None:
                raise ValueError("candidate_build jobs require candidate and evaluation sections.")
            if self.candidate_id is not None:
                raise ValueError("candidate_build jobs must not include candidate_id.")
            if not self.review.required:
                raise ValueError("candidate_build jobs require human review.")
        elif self.kind == "candidate_evaluate":
            if self.candidate is not None or self.candidate_id is None or self.evaluation is None:
                raise ValueError(
                    "candidate_evaluate jobs require candidate_id and evaluation only."
                )
            if not self.review.required:
                raise ValueError("candidate_evaluate jobs require human review.")
        elif self.kind == "promotion_verify":
            if self.candidate is not None or self.candidate_id is None:
                raise ValueError(
                    "promotion_verify jobs require candidate_id and no candidate section."
                )
            if not self.review.required:
                raise ValueError("promotion_verify jobs require human review.")
        return self


class JobEvent(_StrictModel):
    id: int
    event_type: str
    created_at: str
    payload: dict[str, str]


class JobAttempt(_StrictModel):
    id: str
    worker_id: str
    attempt_number: int
    state: AttemptState
    fence_token: int
    lease_expires_at: str
    started_at: str | None
    finished_at: str | None
    exit_class: str | None
    summary: dict[str, str] | None


class JobSummary(_StrictModel):
    id: str
    kind: JobKind
    state: JobState
    priority: int
    baseline_release: str | None
    parent_job_id: str | None
    max_attempts: int
    attempt_count: int
    next_run_at: str
    deadline_at: str | None
    created_at: str
    updated_at: str


class JobDetail(JobSummary):
    spec: JobSubmission
    events: list[JobEvent]
    attempts: list[JobAttempt] = Field(default_factory=list)


class JobSubmissionResult(JobDetail):
    created: bool


class JobListResponse(_StrictModel):
    jobs: list[JobSummary]
    limit: int
    offset: int
    total: int


class ControllerStatus(_StrictModel):
    service: Literal["local-ai-lab-controller"] = "local-ai-lab-controller"
    status: Literal["ok"] = "ok"
    schema_version: int
    scheduler: Literal["active"] = "active"
    worker_execution: Literal["repo_ops_adapter"] = "repo_ops_adapter"
    promotion: Literal["not_implemented"] = "not_implemented"
    jobs_by_state: dict[str, int]
    workers_by_state: dict[str, int]


class WorkerRegistration(_StrictModel):
    worker_id: str = Field(min_length=1, max_length=128)
    worker_class: Literal["repo_ops"]
    image_digest: str = Field(min_length=1, max_length=256)
    capabilities: list[str] = Field(default_factory=lambda: ["workspace_prepare"], max_length=8)

    @field_validator("worker_id", "image_digest")
    @classmethod
    def validate_worker_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Worker fields must not be empty.")
        if len(normalized) > 256:
            raise ValueError("Worker fields must not exceed 256 characters.")
        return normalized

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        allowed = {"workspace_prepare"}
        if not normalized or any(item not in allowed for item in normalized):
            raise ValueError("Only the workspace_prepare capability is supported in phase 2.")
        if len(set(normalized)) != len(normalized):
            raise ValueError("Worker capabilities must not contain duplicates.")
        return normalized


class WorkerClaimRequest(_StrictModel):
    worker_id: str = Field(min_length=1, max_length=128)
    worker_class: Literal["repo_ops"]

    @field_validator("worker_id")
    @classmethod
    def validate_worker_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("worker_id must not be empty.")
        return normalized


class WorkerAttemptReference(_StrictModel):
    worker_id: str = Field(min_length=1, max_length=128)
    fence_token: int = Field(ge=1)

    @field_validator("worker_id")
    @classmethod
    def validate_attempt_worker_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("worker_id must not be empty.")
        return normalized


class WorkerCompletion(WorkerAttemptReference):
    outcome: Literal["succeeded", "failed"]
    exit_class: Literal["success", "policy_denied", "infra_error", "cancelled"]
    summary: dict[str, str] = Field(default_factory=dict, max_length=16)

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for key, item in value.items():
            clean_key = key.strip()
            clean_value = item.strip()
            if not _IDENTIFIER_RE.fullmatch(clean_key) or len(clean_value) > 256:
                raise ValueError("Completion summary entries must be bounded identifier/value pairs.")
            normalized[clean_key] = clean_value
        return normalized


class WorkerClaim(_StrictModel):
    attempt_id: str
    job_id: str
    worker_id: str
    fence_token: int
    lease_expires_at: str
    job: JobDetail


class WorkerClaimResponse(_StrictModel):
    claim: WorkerClaim | None


class WorkerHeartbeatResponse(_StrictModel):
    attempt_id: str
    state: Literal["running"]
    lease_expires_at: str
