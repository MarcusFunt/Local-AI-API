"""Small, append-only learning records that never retain prompt or answer text."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
_SURFACES = {"gateway_agent", "repo_ops"}
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}$")
_MAX_RECORD_BYTES = 16_000


class LearningRecordError(ValueError):
    """Raised when a learning artifact is malformed or unsafe to persist."""


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def summarize_text(value: str) -> dict[str, int | str]:
    """Return a stable fingerprint instead of retaining potentially private content."""
    encoded = value.encode("utf-8")
    return {"sha256": hashlib.sha256(encoded).hexdigest(), "characters": len(value)}


def _identifier(value: str, field: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise LearningRecordError(f"{field} must contain 1-80 lowercase letters, numbers, dots, underscores, or hyphens.")
    return value


def _json_value(value: Any, field: str) -> Any:
    try:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise LearningRecordError(f"{field} must be JSON-serializable.") from exc
    if len(encoded.encode("utf-8")) > 8_000:
        raise LearningRecordError(f"{field} is too large for a learning record.")
    return json.loads(encoded)


def build_learning_record(
    *,
    surface: str,
    outcome: str,
    policy_version: str,
    metrics: dict[str, int | float | bool | str],
    trace: dict[str, Any],
    base_revision: str | None = None,
    candidate_id: str | None = None,
) -> dict[str, Any]:
    """Build one immutable record using only bounded, non-content evidence."""
    if surface not in _SURFACES:
        raise LearningRecordError("surface must be gateway_agent or repo_ops.")
    if not outcome.strip() or len(outcome) > 240:
        raise LearningRecordError("outcome must contain 1-240 characters.")
    _identifier(policy_version, "policy_version")
    if candidate_id is not None:
        _identifier(candidate_id, "candidate_id")
    if base_revision is not None and not re.fullmatch(r"[0-9a-f]{7,64}", base_revision):
        raise LearningRecordError("base_revision must be a Git SHA.")
    if not isinstance(metrics, dict) or not metrics:
        raise LearningRecordError("metrics must be a non-empty object.")
    if any(not isinstance(key, str) or not isinstance(value, (int, float, bool, str)) for key, value in metrics.items()):
        raise LearningRecordError("metrics must contain scalar values only.")
    record = {
        "version": SCHEMA_VERSION,
        "record_id": "record-" + uuid.uuid4().hex,
        "recorded_at": _timestamp(),
        "surface": surface,
        "outcome": outcome.strip(),
        "policy_version": policy_version,
        "base_revision": base_revision,
        "candidate_id": candidate_id,
        "metrics": _json_value(metrics, "metrics"),
        "trace": _json_value(trace, "trace"),
    }
    if len(json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > _MAX_RECORD_BYTES:
        raise LearningRecordError("learning record exceeds the 16 KB safety limit.")
    return record


class LearningRecordStore:
    """Append records atomically enough for independent local agent processes."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def path(self) -> Path:
        return self.root / "records.jsonl"

    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        validated = build_learning_record(
            surface=str(record.get("surface", "")),
            outcome=str(record.get("outcome", "")),
            policy_version=str(record.get("policy_version", "")),
            metrics=record.get("metrics") if isinstance(record.get("metrics"), dict) else {},
            trace=record.get("trace") if isinstance(record.get("trace"), dict) else {},
            base_revision=record.get("base_revision") if isinstance(record.get("base_revision"), str) else None,
            candidate_id=record.get("candidate_id") if isinstance(record.get("candidate_id"), str) else None,
        )
        validated["record_id"] = str(record.get("record_id") or validated["record_id"])
        validated["recorded_at"] = str(record.get("recorded_at") or validated["recorded_at"])
        line = json.dumps(validated, sort_keys=True, ensure_ascii=False) + "\n"
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, line.encode("utf-8"))
        finally:
            os.close(descriptor)
        return validated

    def read(self, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1_000:
            raise LearningRecordError("limit must be between 1 and 1000.")
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(item)
        return records[-limit:]
