"""SQLite migrations and durable job-state access for the lab controller."""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import JobSubmission


@dataclass(frozen=True)
class Migration:
    version: int
    statements: tuple[str, ...]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        statements=(
            """
            CREATE TABLE jobs (
              id TEXT PRIMARY KEY,
              idempotency_key TEXT NOT NULL UNIQUE,
              kind TEXT NOT NULL CHECK (kind IN
                ('research','candidate_build','candidate_evaluate','promotion_verify')),
              state TEXT NOT NULL CHECK (state IN
                ('queued','leased','running','evaluating','awaiting_review',
                 'succeeded','failed','cancelled','expired')),
              priority INTEGER NOT NULL CHECK (priority BETWEEN 0 AND 100),
              spec_json TEXT NOT NULL,
              baseline_release_id TEXT,
              parent_job_id TEXT,
              max_attempts INTEGER NOT NULL CHECK (max_attempts BETWEEN 1 AND 3),
              attempt_count INTEGER NOT NULL DEFAULT 0,
              next_run_at TEXT NOT NULL,
              deadline_at TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              job_id TEXT NOT NULL REFERENCES jobs(id),
              event_type TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX jobs_runnable ON jobs(state, next_run_at, priority DESC)",
            "CREATE INDEX events_job ON events(job_id, id)",
        ),
    ),
    Migration(
        version=2,
        statements=(
            """
            CREATE TABLE workers (
              id TEXT PRIMARY KEY,
              worker_class TEXT NOT NULL CHECK (worker_class IN ('repo_ops')),
              image_digest TEXT NOT NULL,
              capability_json TEXT NOT NULL,
              state TEXT NOT NULL CHECK (state IN ('ready','busy','offline')),
              last_heartbeat_at TEXT NOT NULL,
              created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE job_attempts (
              id TEXT PRIMARY KEY,
              job_id TEXT NOT NULL REFERENCES jobs(id),
              attempt_number INTEGER NOT NULL,
              worker_id TEXT NOT NULL REFERENCES workers(id),
              state TEXT NOT NULL CHECK (state IN
                ('leased','running','finished','failed','lost','cancelled')),
              fence_token INTEGER NOT NULL,
              lease_expires_at TEXT NOT NULL,
              started_at TEXT,
              finished_at TEXT,
              exit_class TEXT,
              summary_json TEXT,
              UNIQUE (job_id, attempt_number),
              UNIQUE (job_id, fence_token)
            )
            """,
            "CREATE INDEX attempts_lease ON job_attempts(state, lease_expires_at)",
            "CREATE INDEX attempts_worker ON job_attempts(worker_id, state)",
        ),
    ),
)


class IdempotencyConflictError(Exception):
    """Raised when a reused key describes different work."""


class WorkerNotRegisteredError(Exception):
    """Raised when a worker tries to use the controller before registration."""


class LeaseConflictError(Exception):
    """Raised when an attempt does not belong to the supplied worker/token."""


class LeaseExpiredError(Exception):
    """Raised when a worker presents an attempt after its lease has expired."""


def _iso_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _iso_after(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class ControllerDatabase:
    """Small SQLite repository with a fresh connection for each operation."""

    def __init__(self, database_path: str) -> None:
        self.database_path = database_path

    def migrate(self) -> None:
        """Apply all outstanding, ordered migrations before serving requests."""
        if self.database_path != ":memory:":
            Path(self.database_path).expanduser().parent.mkdir(parents=True, exist_ok=True)

        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                  version INTEGER PRIMARY KEY,
                  applied_at TEXT NOT NULL
                )
                """
            )
            connection.commit()
            applied = {
                int(row["version"])
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            for migration in MIGRATIONS:
                if migration.version in applied:
                    continue
                connection.execute("BEGIN IMMEDIATE")
                try:
                    for statement in migration.statements:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                        (migration.version, _iso_now()),
                    )
                except Exception:
                    connection.rollback()
                    raise
                else:
                    connection.commit()
        finally:
            connection.close()

    def schema_version(self) -> int:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
            ).fetchone()
            return int(row["version"])
        finally:
            connection.close()

    def create_job(self, job: JobSubmission) -> tuple[dict[str, Any], bool]:
        """Persist a queued job, returning an existing identical request safely."""
        spec_json = _canonical_json(job.model_dump(mode="json"))
        now = _iso_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT id, spec_json FROM jobs WHERE idempotency_key = ?",
                    (job.idempotency_key,),
                ).fetchone()
                if existing is not None:
                    if existing["spec_json"] != spec_json:
                        raise IdempotencyConflictError(
                            "The idempotency key is already associated with a different "
                            "job specification."
                        )
                    connection.commit()
                    return self._get_job_with_connection(connection, existing["id"]), False

                job_id = f"job_{uuid.uuid4().hex}"
                connection.execute(
                    """
                    INSERT INTO jobs (
                      id, idempotency_key, kind, state, priority, spec_json,
                      baseline_release_id, parent_job_id, max_attempts,
                      attempt_count, next_run_at, deadline_at, created_at, updated_at
                    ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, 0, ?, NULL, ?, ?)
                    """,
                    (
                        job_id,
                        job.idempotency_key,
                        job.kind,
                        job.priority,
                        spec_json,
                        job.baseline_release,
                        job.parent_job_id,
                        job.budget.max_attempts,
                        now,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO events (job_id, event_type, payload_json, created_at)
                    VALUES (?, 'job_submitted', ?, ?)
                    """,
                    (
                        job_id,
                        _canonical_json(
                            {
                                "kind": job.kind,
                                "schema_version": job.schema_version,
                            }
                        ),
                        now,
                    ),
                )
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()
            return self._get_job_with_connection(connection, job_id), True
        finally:
            connection.close()

    def register_worker(
        self,
        worker_id: str,
        worker_class: str,
        image_digest: str,
        capabilities: list[str],
    ) -> dict[str, Any]:
        """Register or refresh one local worker without reviving an active lease."""
        now = _iso_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT worker_class, state FROM workers WHERE id = ?", (worker_id,)
            ).fetchone()
            if existing is not None and existing["worker_class"] != worker_class:
                connection.rollback()
                raise WorkerNotRegisteredError("Worker ID is already registered to another worker class.")
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO workers (
                      id, worker_class, image_digest, capability_json, state,
                      last_heartbeat_at, created_at
                    ) VALUES (?, ?, ?, ?, 'ready', ?, ?)
                    """,
                    (worker_id, worker_class, image_digest, _canonical_json(capabilities), now, now),
                )
            else:
                new_state = "ready" if existing["state"] == "offline" else existing["state"]
                connection.execute(
                    """
                    UPDATE workers
                    SET image_digest = ?, capability_json = ?, state = ?, last_heartbeat_at = ?
                    WHERE id = ?
                    """,
                    (image_digest, _canonical_json(capabilities), new_state, now, worker_id),
                )
            connection.commit()
            row = connection.execute(
                """
                SELECT id, worker_class, image_digest, capability_json, state,
                       last_heartbeat_at, created_at
                FROM workers WHERE id = ?
                """,
                (worker_id,),
            ).fetchone()
            return self._worker_from_row(row)
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def claim_next_job(
        self, worker_id: str, worker_class: str, lease_seconds: int
    ) -> dict[str, Any] | None:
        """Lease one compatible queued job and advance its fencing token atomically."""
        self.reap_expired_leases()
        now = _iso_now()
        lease_expires_at = _iso_after(lease_seconds)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            worker = connection.execute(
                "SELECT id, worker_class, state FROM workers WHERE id = ?", (worker_id,)
            ).fetchone()
            if worker is None or worker["worker_class"] != worker_class:
                raise WorkerNotRegisteredError("Worker is not registered for this worker class.")
            if worker["state"] != "ready":
                connection.commit()
                return None

            queued_rows = connection.execute(
                """
                SELECT id, spec_json, attempt_count, max_attempts
                FROM jobs
                WHERE state = 'queued' AND kind = 'candidate_build' AND next_run_at <= ?
                ORDER BY priority DESC, created_at ASC
                """,
                (now,),
            ).fetchall()
            selected = next(
                (row for row in queued_rows if self._is_repo_ops_workspace_job(row["spec_json"])),
                None,
            )
            if selected is None:
                connection.commit()
                return None

            attempt_number = int(selected["attempt_count"]) + 1
            maximum_token = connection.execute(
                "SELECT COALESCE(MAX(fence_token), 0) AS token FROM job_attempts WHERE job_id = ?",
                (selected["id"],),
            ).fetchone()
            fence_token = int(maximum_token["token"]) + 1
            attempt_id = f"attempt_{uuid.uuid4().hex}"
            connection.execute(
                """
                INSERT INTO job_attempts (
                  id, job_id, attempt_number, worker_id, state, fence_token, lease_expires_at
                ) VALUES (?, ?, ?, ?, 'leased', ?, ?)
                """,
                (attempt_id, selected["id"], attempt_number, worker_id, fence_token, lease_expires_at),
            )
            connection.execute(
                """
                UPDATE jobs
                SET state = 'leased', attempt_count = ?, updated_at = ?
                WHERE id = ? AND state = 'queued'
                """,
                (attempt_number, now, selected["id"]),
            )
            connection.execute(
                "UPDATE workers SET state = 'busy', last_heartbeat_at = ? WHERE id = ?",
                (now, worker_id),
            )
            self._insert_event(
                connection,
                selected["id"],
                "attempt_leased",
                {
                    "attempt_id": attempt_id,
                    "fence_token": str(fence_token),
                    "worker_id": worker_id,
                },
                now,
            )
            connection.commit()
            return {
                "attempt_id": attempt_id,
                "job_id": str(selected["id"]),
                "worker_id": worker_id,
                "fence_token": fence_token,
                "lease_expires_at": lease_expires_at,
                "job": self._get_job_with_connection(connection, selected["id"]),
            }
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def heartbeat_attempt(
        self, attempt_id: str, worker_id: str, fence_token: int, lease_seconds: int
    ) -> dict[str, str]:
        """Renew the current lease and transition a newly claimed attempt to running."""
        now = _iso_now()
        lease_expires_at = _iso_after(lease_seconds)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            attempt = self._active_attempt(connection, attempt_id, worker_id, fence_token)
            if attempt["lease_expires_at"] <= now:
                raise LeaseExpiredError("The attempt lease has expired.")
            started = attempt["started_at"] or now
            connection.execute(
                """
                UPDATE job_attempts
                SET state = 'running', started_at = ?, lease_expires_at = ?
                WHERE id = ?
                """,
                (started, lease_expires_at, attempt_id),
            )
            connection.execute(
                "UPDATE jobs SET state = 'running', updated_at = ? WHERE id = ?",
                (now, attempt["job_id"]),
            )
            connection.execute(
                "UPDATE workers SET state = 'busy', last_heartbeat_at = ? WHERE id = ?",
                (now, worker_id),
            )
            if attempt["state"] == "leased":
                self._insert_event(
                    connection,
                    attempt["job_id"],
                    "attempt_started",
                    {"attempt_id": attempt_id, "worker_id": worker_id},
                    now,
                )
            connection.commit()
            return {
                "attempt_id": attempt_id,
                "state": "running",
                "lease_expires_at": lease_expires_at,
            }
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def complete_attempt(
        self,
        attempt_id: str,
        worker_id: str,
        fence_token: int,
        outcome: str,
        exit_class: str,
        summary: dict[str, str],
    ) -> dict[str, Any]:
        """Finish a fenced attempt; workers cannot change another job's state."""
        now = _iso_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            attempt = self._active_attempt(connection, attempt_id, worker_id, fence_token)
            if attempt["lease_expires_at"] <= now:
                raise LeaseExpiredError("The attempt lease has expired.")
            attempt_state = "finished" if outcome == "succeeded" else "failed"
            job_state = "succeeded" if outcome == "succeeded" else "failed"
            connection.execute(
                """
                UPDATE job_attempts
                SET state = ?, finished_at = ?, exit_class = ?, summary_json = ?
                WHERE id = ?
                """,
                (attempt_state, now, exit_class, _canonical_json(summary), attempt_id),
            )
            connection.execute(
                "UPDATE jobs SET state = ?, updated_at = ? WHERE id = ?",
                (job_state, now, attempt["job_id"]),
            )
            connection.execute(
                "UPDATE workers SET state = 'ready', last_heartbeat_at = ? WHERE id = ?",
                (now, worker_id),
            )
            self._insert_event(
                connection,
                attempt["job_id"],
                "attempt_succeeded" if outcome == "succeeded" else "attempt_failed",
                {"attempt_id": attempt_id, "exit_class": exit_class, "worker_id": worker_id},
                now,
            )
            connection.commit()
            return self._get_job_with_connection(connection, attempt["job_id"])
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def reap_expired_leases(self) -> int:
        """Mark stale attempts lost and requeue only idempotent work within its retry budget."""
        now = _iso_now()
        connection = self._connect()
        reaped = 0
        try:
            connection.execute("BEGIN IMMEDIATE")
            stale_attempts = connection.execute(
                """
                SELECT attempts.id, attempts.job_id, attempts.worker_id, jobs.attempt_count,
                       jobs.max_attempts
                FROM job_attempts AS attempts
                JOIN jobs ON jobs.id = attempts.job_id
                WHERE attempts.state IN ('leased', 'running') AND attempts.lease_expires_at < ?
                """,
                (now,),
            ).fetchall()
            for attempt in stale_attempts:
                next_state = "queued" if attempt["attempt_count"] < attempt["max_attempts"] else "expired"
                connection.execute(
                    "UPDATE job_attempts SET state = 'lost', finished_at = ? WHERE id = ?",
                    (now, attempt["id"]),
                )
                connection.execute(
                    "UPDATE jobs SET state = ?, next_run_at = ?, updated_at = ? WHERE id = ?",
                    (next_state, now, now, attempt["job_id"]),
                )
                connection.execute(
                    "UPDATE workers SET state = 'ready', last_heartbeat_at = ? WHERE id = ?",
                    (now, attempt["worker_id"]),
                )
                self._insert_event(
                    connection,
                    attempt["job_id"],
                    "attempt_lost",
                    {"attempt_id": str(attempt["id"]), "next_state": next_state},
                    now,
                )
                reaped += 1
            connection.commit()
            return reaped
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute("SELECT id FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._get_job_with_connection(connection, row["id"]) if row else None
        finally:
            connection.close()

    def list_jobs(
        self, state: str | None, limit: int, offset: int
    ) -> tuple[list[dict[str, Any]], int]:
        where = ""
        parameters: tuple[object, ...] = ()
        if state is not None:
            where = "WHERE state = ?"
            parameters = (state,)
        connection = self._connect()
        try:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) AS count FROM jobs {where}", parameters
                ).fetchone()["count"]
            )
            rows = connection.execute(
                f"""
                SELECT id, kind, state, priority, baseline_release_id, parent_job_id,
                       max_attempts, attempt_count, next_run_at, deadline_at, created_at, updated_at
                FROM jobs {where}
                ORDER BY priority DESC, created_at ASC
                LIMIT ? OFFSET ?
                """,
                (*parameters, limit, offset),
            ).fetchall()
            return [self._summary_from_row(row) for row in rows], total
        finally:
            connection.close()

    def jobs_by_state(self) -> dict[str, int]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT state, COUNT(*) AS count FROM jobs GROUP BY state ORDER BY state"
            ).fetchall()
            return {str(row["state"]): int(row["count"]) for row in rows}
        finally:
            connection.close()

    def workers_by_state(self) -> dict[str, int]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT state, COUNT(*) AS count FROM workers GROUP BY state ORDER BY state"
            ).fetchall()
            return {str(row["state"]): int(row["count"]) for row in rows}
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _get_job_with_connection(
        self, connection: sqlite3.Connection, job_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT id, kind, state, priority, spec_json, baseline_release_id, parent_job_id,
                   max_attempts, attempt_count, next_run_at, deadline_at, created_at, updated_at
            FROM jobs WHERE id = ?
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown job {job_id!r}.")
        event_rows = connection.execute(
            """
            SELECT id, event_type, payload_json, created_at
            FROM events WHERE job_id = ? ORDER BY id
            """,
            (job_id,),
        ).fetchall()
        attempt_rows = connection.execute(
            """
            SELECT id, worker_id, attempt_number, state, fence_token, lease_expires_at,
                   started_at, finished_at, exit_class, summary_json
            FROM job_attempts WHERE job_id = ? ORDER BY attempt_number
            """,
            (job_id,),
        ).fetchall()
        return {
            **self._summary_from_row(row),
            "spec": json.loads(row["spec_json"]),
            "events": [
                {
                    "id": int(event["id"]),
                    "event_type": str(event["event_type"]),
                    "created_at": str(event["created_at"]),
                    "payload": json.loads(event["payload_json"]),
                }
                for event in event_rows
            ],
            "attempts": [self._attempt_from_row(attempt) for attempt in attempt_rows],
        }

    @staticmethod
    def _is_repo_ops_workspace_job(spec_json: str) -> bool:
        """Keep the phase-2 adapter to code-patch workspace preparation only."""
        try:
            spec = json.loads(spec_json)
        except json.JSONDecodeError:
            return False
        candidate = spec.get("candidate")
        isolation = spec.get("isolation")
        return (
            isinstance(candidate, dict)
            and candidate.get("type") == "code_patch"
            and candidate.get("allowed_changes") == ["patch_manifest"]
            and isinstance(isolation, dict)
            and isolation.get("worker_class") == "repo_ops"
            and isolation.get("network") == "none"
            and isolation.get("source_access") == "read_only"
            and isolation.get("credential_profile") == "none"
        )

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        job_id: str,
        event_type: str,
        payload: dict[str, str],
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO events (job_id, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (job_id, event_type, _canonical_json(payload), created_at),
        )

    @staticmethod
    def _active_attempt(
        connection: sqlite3.Connection, attempt_id: str, worker_id: str, fence_token: int
    ) -> sqlite3.Row:
        attempt = connection.execute(
            """
            SELECT id, job_id, worker_id, state, fence_token, lease_expires_at, started_at
            FROM job_attempts WHERE id = ?
            """,
            (attempt_id,),
        ).fetchone()
        if attempt is None or attempt["worker_id"] != worker_id or attempt["fence_token"] != fence_token:
            raise LeaseConflictError("Attempt does not match the supplied worker or fence token.")
        if attempt["state"] not in {"leased", "running"}:
            raise LeaseConflictError("Attempt is no longer active.")
        return attempt

    @staticmethod
    def _worker_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "worker_class": str(row["worker_class"]),
            "image_digest": str(row["image_digest"]),
            "capabilities": json.loads(row["capability_json"]),
            "state": str(row["state"]),
            "last_heartbeat_at": str(row["last_heartbeat_at"]),
            "created_at": str(row["created_at"]),
        }

    @staticmethod
    def _attempt_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "worker_id": str(row["worker_id"]),
            "attempt_number": int(row["attempt_number"]),
            "state": str(row["state"]),
            "fence_token": int(row["fence_token"]),
            "lease_expires_at": str(row["lease_expires_at"]),
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "exit_class": row["exit_class"],
            "summary": json.loads(row["summary_json"]) if row["summary_json"] else None,
        }

    @staticmethod
    def _summary_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "kind": str(row["kind"]),
            "state": str(row["state"]),
            "priority": int(row["priority"]),
            "baseline_release": row["baseline_release_id"],
            "parent_job_id": row["parent_job_id"],
            "max_attempts": int(row["max_attempts"]),
            "attempt_count": int(row["attempt_count"]),
            "next_run_at": str(row["next_run_at"]),
            "deadline_at": row["deadline_at"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }
