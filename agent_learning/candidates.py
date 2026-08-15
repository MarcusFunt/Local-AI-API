"""Bounded candidate manifests for behavioral improvement experiments."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .records import LearningRecordError, _identifier


_ALLOWED_POLICY_FIELDS = {"stage_order", "stage_token_limits", "system_prompt", "tool_preference"}


@dataclass(frozen=True)
class PolicyCandidate:
    """A candidate can alter only explicit, versioned policy artifacts."""

    version: int
    candidate_id: str
    base_policy_version: str
    hypothesis: str
    changes: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def create_policy_candidate(
    *,
    candidate_id: str,
    base_policy_version: str,
    hypothesis: str,
    changes: dict[str, Any],
) -> PolicyCandidate:
    """Create a small candidate that evaluators can accept or reject deterministically."""
    _identifier(candidate_id, "candidate_id")
    _identifier(base_policy_version, "base_policy_version")
    if not 1 <= len(hypothesis.strip()) <= 1_000:
        raise LearningRecordError("hypothesis must contain 1-1000 characters.")
    if not changes or set(changes) - _ALLOWED_POLICY_FIELDS:
        raise LearningRecordError("changes must contain only approved policy fields.")
    if len(changes) > 2:
        raise LearningRecordError("A behavioral candidate may change at most two policy fields.")
    for value in changes.values():
        if len(str(value)) > 4_000:
            raise LearningRecordError("candidate policy changes must stay bounded.")
    return PolicyCandidate(1, candidate_id, base_policy_version, hypothesis.strip(), changes)
