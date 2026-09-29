"""PostgreSQL source of truth for M6 batch jobs and at-least-once delivery.

The broker receives only ``job_id`` and ``schema_version``. Request data is an
immutable, server-registered reference pinned by ``request_hash``. Every worker
write verifies the database lease epoch so a process that loses its lease cannot
publish progress after takeover.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from sqlalchemy import Connection, Engine, text


OUTBOX_SCHEMA_VERSION = 1
_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_STAGE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_ERROR = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\Z")
_RESULT_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_INGESTION_STAGES = (
    "received",
    "parsed",
    "embedded",
    "indexed",
    "validated",
    "activated",
)
_EVALUATION_STAGES = ("received", "evaluating", "aggregating", "completed")
_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


class JobContractError(ValueError):
    """An unsafe or incompatible submission/progress value was rejected."""


class JobConflictError(JobContractError):
    """The same owner and idempotency key already binds a different request."""


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    owner_id: str
    kind: str
    status: str
    request_ref: str
    request_hash: str
    total: int
    completed: int
    failed: int
    pending: int
    stage: str
    cancel_requested: bool
    lease_epoch: int
    claim_count: int
    lease_expires_at: datetime | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    outbox_status: str | None
    outbox_error_code: str | None
    error_code: str | None


@dataclass(frozen=True)
class OutboxRecord:
    id: str
    job_id: str
    schema_version: int
    lease_epoch: int
    attempts: int


@dataclass(frozen=True)
class ItemClaim:
    acquired: bool
    status: str
    attempt_no: int
    result_ref: str | None = None


def _required_string(name: str, value: object, *, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise JobContractError(
            f"{name} must be a non-empty string up to {max_length} characters"
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise JobContractError(f"{name} contains a control character")
    return value


def _opaque_id(name: str, value: object) -> str:
    value = _required_string(name, value, max_length=128)
    if not _OPAQUE_ID.fullmatch(value):
        raise JobContractError(f"{name} must be an opaque server-registered ID")
    return value


def _hash(name: str, value: object) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise JobContractError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _error_code(value: str | None) -> str | None:
    if value is not None and (
        not isinstance(value, str) or not _ERROR.fullmatch(value)
    ):
        raise JobContractError("error_code must be a short machine-readable code")
    return value


def _result_ref(value: object) -> str:
    value = _required_string("result_ref", value, max_length=255)
    if any(not _RESULT_PART.fullmatch(part) for part in value.split("/")):
        raise JobContractError(
            "result_ref must be an opaque ID or safe relative reference"
        )
    return value


def _count(name: str, value: object, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise JobContractError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def _job_record(row: object) -> JobRecord:
    values = row._mapping if hasattr(row, "_mapping") else row
    return JobRecord(
        job_id=values["job_id"],
        owner_id=values["owner_id"],
        kind=values["kind"],
        status=values["status"],
        request_ref=values["request_ref"],
        request_hash=values["request_hash"],
        total=values["total"],
        completed=values["completed"],
        failed=values["failed"],
        pending=values["total"] - values["completed"] - values["failed"],
        stage=values["stage"],
        cancel_requested=values["cancel_requested"],
        lease_epoch=values["lease_epoch"],
        claim_count=values["claim_count"],
        lease_expires_at=values["lease_expires_at"],
        created_at=values["created_at"],
        started_at=values["started_at"],
        finished_at=values["finished_at"],
        outbox_status=values["outbox_status"],
        outbox_error_code=values["outbox_error_code"],
        error_code=values["error_code"],
    )


_JOB_SELECT = """
SELECT j.*, latest.status AS outbox_status,
       latest.last_error_code AS outbox_error_code
FROM jobs AS j
LEFT JOIN LATERAL (
    SELECT o.status,o.last_error_code FROM job_outbox AS o
    WHERE o.job_id = j.job_id
    ORDER BY o.created_at DESC,o.outbox_id DESC LIMIT 1
) AS latest ON true
WHERE j.job_id = :job_id
"""


class JobStore:
    """Short transactional operations; no broker or model call runs inside them."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def _read_job(
        self, connection: Connection, job_id: str, owner_id: str | None = None
    ) -> JobRecord | None:
        statement = _JOB_SELECT
        params: dict[str, object] = {"job_id": job_id}
        if owner_id is not None:
            statement += " AND j.owner_id = :owner_id"
            params["owner_id"] = owner_id
        row = connection.execute(text(statement), params).mappings().first()
        return _job_record(row) if row is not None else None

    def get_job(self, job_id: str, owner_id: str) -> JobRecord | None:
        """Return no row for either a missing job or a different owner."""
        _required_string("job_id", job_id, max_length=36)
        _required_string("owner_id", owner_id, max_length=128)
        with self.engine.connect() as connection:
            return self._read_job(connection, job_id, owner_id)

    def get_job_internal(self, job_id: str) -> JobRecord | None:
        """Worker-only lookup; callers must never expose this to HTTP users."""
        _required_string("job_id", job_id, max_length=36)
        with self.engine.connect() as connection:
            return self._read_job(connection, job_id)

    def create_job(
        self,
        *,
        owner_id: str,
        kind: str,
        request_ref: str,
        request_hash: str,
        total: int = 0,
        idempotency_key: str | None = None,
    ) -> JobRecord:
        _required_string("owner_id", owner_id, max_length=128)
        if kind not in {"evaluation", "ingestion"}:
            raise JobContractError("kind must be evaluation or ingestion")
        _opaque_id("request_ref", request_ref)
        _hash("request_hash", request_hash)
        _count("total", total, minimum=0, maximum=1_000_000)
        if idempotency_key is not None:
            _opaque_id("idempotency_key", idempotency_key)
        new_id = str(uuid.uuid4())
        with self.engine.begin() as connection:
            inserted = connection.execute(
                text(
                    "INSERT INTO jobs (job_id,owner_id,kind,request_ref,request_hash,"
                    "idempotency_key,status,stage,total,completed,failed,cancel_requested) "
                    "VALUES (:job_id,:owner_id,:kind,:request_ref,:request_hash,"
                    ":idempotency_key,'queued','received',:total,0,0,false) "
                    "ON CONFLICT (owner_id,kind,idempotency_key) DO NOTHING RETURNING job_id"
                ),
                {
                    "job_id": new_id,
                    "owner_id": owner_id,
                    "kind": kind,
                    "request_ref": request_ref,
                    "request_hash": request_hash,
                    "idempotency_key": idempotency_key,
                    "total": total,
                },
            ).scalar_one_or_none()
            if inserted is None:
                existing = (
                    connection.execute(
                        text(
                            "SELECT job_id,request_ref,request_hash FROM jobs "
                            "WHERE owner_id=:owner_id AND kind=:kind "
                            "AND idempotency_key=:idempotency_key"
                        ),
                        {
                            "owner_id": owner_id,
                            "kind": kind,
                            "idempotency_key": idempotency_key,
                        },
                    )
                    .mappings()
                    .one()
                )
                if (
                    existing["request_ref"] != request_ref
                    or existing["request_hash"] != request_hash
                ):
                    raise JobConflictError(
                        "idempotency key already binds a different request"
                    )
                return self._read_job(connection, existing["job_id"])
            connection.execute(
                text(
                    "INSERT INTO job_outbox "
                    "(outbox_id,job_id,schema_version,status,claim_epoch,delivery_attempts) "
                    "VALUES (:outbox_id,:job_id,:schema_version,'pending',0,0)"
                ),
                {
                    "outbox_id": str(uuid.uuid4()),
                    "job_id": new_id,
                    "schema_version": OUTBOX_SCHEMA_VERSION,
                },
            )
            return self._read_job(connection, new_id)

    def claim_outbox(
        self, dispatcher_id: str, *, limit: int = 20, lease_seconds: int = 30
    ) -> list[OutboxRecord]:
        _required_string("dispatcher_id", dispatcher_id, max_length=128)
        _count("limit", limit, minimum=1, maximum=1000)
        _count("lease_seconds", lease_seconds, minimum=1, maximum=3600)
        with self.engine.begin() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT o.outbox_id FROM job_outbox o JOIN jobs j ON j.job_id=o.job_id "
                        "WHERE o.status='pending' AND j.status IN ('queued','running') "
                        "AND o.next_attempt_at <= clock_timestamp() "
                        "AND (o.claim_expires_at IS NULL OR o.claim_expires_at <= clock_timestamp()) "
                        "ORDER BY o.created_at,o.outbox_id "
                        "LIMIT :limit FOR UPDATE OF o SKIP LOCKED"
                    ),
                    {"limit": limit},
                )
                .mappings()
                .all()
            )
            claimed: list[OutboxRecord] = []
            for row in rows:
                result = (
                    connection.execute(
                        text(
                            "UPDATE job_outbox SET claim_owner=:dispatcher_id, "
                            "claim_epoch=claim_epoch+1, delivery_attempts=delivery_attempts+1, "
                            "claim_expires_at=clock_timestamp()+make_interval(secs=>:lease_seconds) "
                            "WHERE outbox_id=:outbox_id "
                            "RETURNING outbox_id,job_id,schema_version,claim_epoch,delivery_attempts"
                        ),
                        {
                            "outbox_id": row["outbox_id"],
                            "dispatcher_id": dispatcher_id,
                            "lease_seconds": lease_seconds,
                        },
                    )
                    .mappings()
                    .one()
                )
                claimed.append(
                    OutboxRecord(
                        id=result["outbox_id"],
                        job_id=result["job_id"],
                        schema_version=result["schema_version"],
                        lease_epoch=result["claim_epoch"],
                        attempts=result["delivery_attempts"],
                    )
                )
            return claimed

    def mark_outbox_delivered(
        self, outbox_id: str, dispatcher_id: str, lease_epoch: int
    ) -> bool:
        with self.engine.begin() as connection:
            result = connection.execute(
                text(
                    "UPDATE job_outbox SET status='delivered', delivered_at=clock_timestamp(), "
                    "claim_owner=NULL,claim_expires_at=NULL,last_error_code=NULL "
                    "WHERE outbox_id=:outbox_id AND status='pending' "
                    "AND claim_owner=:dispatcher_id AND claim_epoch=:lease_epoch "
                    "AND claim_expires_at > clock_timestamp()"
                ),
                {
                    "outbox_id": outbox_id,
                    "dispatcher_id": dispatcher_id,
                    "lease_epoch": lease_epoch,
                },
            )
            return result.rowcount == 1

    def mark_outbox_retry(
        self,
        outbox_id: str,
        dispatcher_id: str,
        lease_epoch: int,
        error_code: str,
        *,
        delay_seconds: int = 5,
    ) -> bool:
        _error_code(error_code)
        _count("delay_seconds", delay_seconds, minimum=0, maximum=3600)
        with self.engine.begin() as connection:
            result = connection.execute(
                text(
                    "UPDATE job_outbox SET claim_owner=NULL,claim_expires_at=NULL, "
                    "next_attempt_at=clock_timestamp()+make_interval(secs=>:delay_seconds), "
                    "last_error_code=:error_code "
                    "WHERE outbox_id=:outbox_id AND status='pending' "
                    "AND claim_owner=:dispatcher_id AND claim_epoch=:lease_epoch "
                    "AND claim_expires_at > clock_timestamp()"
                ),
                {
                    "outbox_id": outbox_id,
                    "dispatcher_id": dispatcher_id,
                    "lease_epoch": lease_epoch,
                    "error_code": error_code,
                    "delay_seconds": delay_seconds,
                },
            )
            return result.rowcount == 1

    def requeue_expired_jobs(
        self,
        *,
        limit: int = 100,
        min_interval_seconds: int = 30,
        max_interval_seconds: int = 3600,
        max_auto_deliveries: int = 5,
    ) -> list[str]:
        """Bound automatic redelivery of work that may have been lost.

        An early duplicate broker delivery can arrive while the old lease is
        still active. The worker may safely ignore it; this scan later creates
        a fresh message after lease expiry. It also recovers a queued job when
        its last broker delivery was ACKed without any worker claiming it.
        A delivered message may still be waiting in the broker, so repeated
        redelivery backs off and eventually stops with a visible warning.
        """
        _count("limit", limit, minimum=1, maximum=1000)
        _count("min_interval_seconds", min_interval_seconds, minimum=1, maximum=3600)
        _count("max_interval_seconds", max_interval_seconds, minimum=1, maximum=86400)
        _count("max_auto_deliveries", max_auto_deliveries, minimum=1, maximum=100)
        if max_interval_seconds < min_interval_seconds:
            raise JobContractError("max_interval_seconds must cover the minimum")
        with self.engine.begin() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT j.job_id,j.status,d.issued FROM jobs j "
                        "CROSS JOIN LATERAL (SELECT count(*)::int AS issued, "
                        "max(o.delivered_at) AS last_delivered_at FROM job_outbox o "
                        "WHERE o.job_id=j.job_id) d WHERE "
                        "(j.status='queued' OR (j.status='running' AND "
                        "j.lease_expires_at <= clock_timestamp())) "
                        "AND j.error_code IS DISTINCT FROM 'delivery_unconfirmed' "
                        "AND (j.status='running' AND j.recovery_queued_at IS NULL "
                        "OR d.last_delivered_at <= clock_timestamp()-make_interval("
                        "secs=>LEAST(:max_interval_seconds,:min_interval_seconds * "
                        "(1 << LEAST(GREATEST(d.issued-1,0),10))))) "
                        "AND (j.recovery_queued_at IS NULL OR j.recovery_queued_at <= "
                        "clock_timestamp()-make_interval(secs=>:min_interval_seconds)) "
                        "AND NOT EXISTS (SELECT 1 FROM job_outbox o WHERE o.job_id=j.job_id "
                        "AND o.status='pending') "
                        "ORDER BY j.created_at,j.job_id "
                        "LIMIT :limit FOR UPDATE OF j SKIP LOCKED"
                    ),
                    {
                        "limit": limit,
                        "min_interval_seconds": min_interval_seconds,
                        "max_interval_seconds": max_interval_seconds,
                    },
                )
                .mappings()
                .all()
            )
            queued: list[str] = []
            for row in rows:
                job_id = row["job_id"]
                if row["issued"] >= max_auto_deliveries:
                    connection.execute(
                        text(
                            "UPDATE jobs SET status='queued',lease_owner=NULL,"
                            "lease_expires_at=NULL,error_code='delivery_unconfirmed',"
                            "updated_at=clock_timestamp() WHERE job_id=:job_id"
                        ),
                        {"job_id": job_id},
                    )
                    continue
                connection.execute(
                    text(
                        "UPDATE jobs SET recovery_queued_at=clock_timestamp(), "
                        "updated_at=clock_timestamp() WHERE job_id=:job_id"
                    ),
                    {"job_id": job_id},
                )
                connection.execute(
                    text(
                        "INSERT INTO job_outbox "
                        "(outbox_id,job_id,schema_version,status,claim_epoch,delivery_attempts) "
                        "VALUES (:outbox_id,:job_id,:schema_version,'pending',0,0)"
                    ),
                    {
                        "outbox_id": str(uuid.uuid4()),
                        "job_id": job_id,
                        "schema_version": OUTBOX_SCHEMA_VERSION,
                    },
                )
                queued.append(job_id)
            return queued

    def claim_job(
        self,
        job_id: str,
        worker_id: str,
        *,
        lease_seconds: int = 60,
        max_claims: int = 10,
    ) -> JobRecord | None:
        _required_string("worker_id", worker_id, max_length=128)
        _count("lease_seconds", lease_seconds, minimum=1, maximum=3600)
        _count("max_claims", max_claims, minimum=1, maximum=100)
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT status,claim_count,cancel_requested,lease_expires_at,"
                        "clock_timestamp() AS db_now "
                        "FROM jobs WHERE job_id=:job_id FOR UPDATE"
                    ),
                    {"job_id": job_id},
                )
                .mappings()
                .first()
            )
            if row is None or row["status"] in _TERMINAL:
                return None
            if (
                row["status"] == "running"
                and row["lease_expires_at"] is not None
                and row["lease_expires_at"] > row["db_now"]
            ):
                return None
            if row["claim_count"] >= max_claims:
                terminal_status = "cancelled" if row["cancel_requested"] else "failed"
                connection.execute(
                    text(
                        "UPDATE jobs SET status=:status,error_code=:error_code,"
                        "lease_owner=NULL,lease_expires_at=NULL,"
                        "finished_at=clock_timestamp(),updated_at=clock_timestamp() "
                        "WHERE job_id=:job_id"
                    ),
                    {
                        "job_id": job_id,
                        "status": terminal_status,
                        "error_code": "worker_attempts_exhausted"
                        if terminal_status == "failed"
                        else None,
                    },
                )
                return None
            connection.execute(
                text(
                    "UPDATE jobs SET status='running',lease_owner=:worker_id, "
                    "lease_epoch=lease_epoch+1,claim_count=claim_count+1, "
                    "lease_expires_at=clock_timestamp()+make_interval(secs=>:lease_seconds), "
                    "started_at=COALESCE(started_at,clock_timestamp()), "
                    "recovery_queued_at=NULL,"
                    "error_code=CASE WHEN error_code='delivery_unconfirmed' "
                    "THEN NULL ELSE error_code END,updated_at=clock_timestamp() "
                    "WHERE job_id=:job_id"
                ),
                {
                    "job_id": job_id,
                    "worker_id": worker_id,
                    "lease_seconds": lease_seconds,
                },
            )
            return self._read_job(connection, job_id)

    def _locked_active_job(
        self, connection: Connection, job_id: str, worker_id: str, lease_epoch: int
    ) -> object | None:
        row = (
            connection.execute(
                text(
                    "SELECT *,lease_expires_at > clock_timestamp() AS lease_valid "
                    "FROM jobs WHERE job_id=:job_id FOR UPDATE"
                ),
                {"job_id": job_id},
            )
            .mappings()
            .first()
        )
        if (
            row is None
            or row["status"] != "running"
            or row["lease_owner"] != worker_id
            or row["lease_epoch"] != lease_epoch
            or not row["lease_valid"]
        ):
            return None
        return row

    def heartbeat(
        self, job_id: str, worker_id: str, lease_epoch: int, *, lease_seconds: int = 60
    ) -> bool:
        _count("lease_seconds", lease_seconds, minimum=1, maximum=3600)
        with self.engine.begin() as connection:
            if (
                self._locked_active_job(connection, job_id, worker_id, lease_epoch)
                is None
            ):
                return False
            connection.execute(
                text(
                    "UPDATE jobs SET lease_expires_at=clock_timestamp()+"
                    "make_interval(secs=>:lease_seconds),updated_at=clock_timestamp() "
                    "WHERE job_id=:job_id"
                ),
                {"job_id": job_id, "lease_seconds": lease_seconds},
            )
            return True

    def set_total(
        self, job_id: str, worker_id: str, lease_epoch: int, total: int
    ) -> bool:
        _count("total", total, minimum=0, maximum=1_000_000)
        with self.engine.begin() as connection:
            job = self._locked_active_job(connection, job_id, worker_id, lease_epoch)
            if job is None:
                return False
            if (
                job["total"] not in (0, total)
                or total < job["completed"] + job["failed"]
            ):
                raise JobConflictError(
                    "job total is already fixed or below completed progress"
                )
            connection.execute(
                text(
                    "UPDATE jobs SET total=:total,updated_at=clock_timestamp() "
                    "WHERE job_id=:job_id"
                ),
                {"job_id": job_id, "total": total},
            )
            return True

    def set_stage(
        self, job_id: str, worker_id: str, lease_epoch: int, stage: str
    ) -> bool:
        if not isinstance(stage, str) or not _STAGE.fullmatch(stage):
            raise JobContractError("stage must be a machine-readable identifier")
        with self.engine.begin() as connection:
            job = self._locked_active_job(connection, job_id, worker_id, lease_epoch)
            if job is None:
                return False
            allowed = (
                _INGESTION_STAGES if job["kind"] == "ingestion" else _EVALUATION_STAGES
            )
            if stage not in allowed or allowed.index(stage) < allowed.index(
                job["stage"]
            ):
                raise JobContractError("stage is unknown or would move backward")
            connection.execute(
                text(
                    "UPDATE jobs SET stage=:stage,updated_at=clock_timestamp() "
                    "WHERE job_id=:job_id"
                ),
                {"job_id": job_id, "stage": stage},
            )
            return True

    def request_cancel(self, job_id: str, owner_id: str) -> JobRecord | None:
        _required_string("owner_id", owner_id, max_length=128)
        with self.engine.begin() as connection:
            job = (
                connection.execute(
                    text(
                        "SELECT status FROM jobs WHERE job_id=:job_id AND owner_id=:owner_id "
                        "FOR UPDATE"
                    ),
                    {"job_id": job_id, "owner_id": owner_id},
                )
                .mappings()
                .first()
            )
            if job is None:
                return None
            if job["status"] == "queued":
                connection.execute(
                    text(
                        "UPDATE jobs SET status='cancelled',cancel_requested=true, "
                        "finished_at=clock_timestamp(),updated_at=clock_timestamp() "
                        "WHERE job_id=:job_id"
                    ),
                    {"job_id": job_id},
                )
            elif job["status"] == "running":
                connection.execute(
                    text(
                        "UPDATE jobs SET cancel_requested=true,updated_at=clock_timestamp() "
                        "WHERE job_id=:job_id"
                    ),
                    {"job_id": job_id},
                )
            return self._read_job(connection, job_id, owner_id)

    def claim_item(
        self,
        job_id: str,
        worker_id: str,
        lease_epoch: int,
        item_key: str,
        *,
        max_attempts: int = 3,
    ) -> ItemClaim:
        _hash("item_key", item_key)
        _count("max_attempts", max_attempts, minimum=1, maximum=10)
        with self.engine.begin() as connection:
            job = self._locked_active_job(connection, job_id, worker_id, lease_epoch)
            if job is None:
                return ItemClaim(False, "lease_lost", 0)
            if job["cancel_requested"]:
                return ItemClaim(False, "cancelled", 0)
            item = (
                connection.execute(
                    text(
                        "SELECT status,attempt_count,max_attempts,lease_epoch,result_ref "
                        "FROM job_items WHERE job_id=:job_id AND item_key=:item_key FOR UPDATE"
                    ),
                    {"job_id": job_id, "item_key": item_key},
                )
                .mappings()
                .first()
            )
            if item is None:
                existing_items = connection.scalar(
                    text("SELECT count(*) FROM job_items WHERE job_id=:job_id"),
                    {"job_id": job_id},
                )
                if existing_items >= job["total"]:
                    return ItemClaim(False, "total_exhausted", 0)
                connection.execute(
                    text(
                        "INSERT INTO job_items (job_id,item_key,status,attempt_count,"
                        "max_attempts,lease_epoch) "
                        "VALUES (:job_id,:item_key,'running',1,:max_attempts,:lease_epoch)"
                    ),
                    {
                        "job_id": job_id,
                        "item_key": item_key,
                        "max_attempts": max_attempts,
                        "lease_epoch": lease_epoch,
                    },
                )
                return ItemClaim(True, "running", 1)
            if item["status"] == "succeeded":
                return ItemClaim(
                    False, "succeeded", item["attempt_count"], item["result_ref"]
                )
            if item["status"] == "failed":
                return ItemClaim(False, "failed", item["attempt_count"])
            if item["status"] == "running" and item["lease_epoch"] == lease_epoch:
                return ItemClaim(False, "running", item["attempt_count"])
            if item["attempt_count"] >= item["max_attempts"]:
                connection.execute(
                    text(
                        "UPDATE job_items SET status='failed',error_code='attempts_exhausted', "
                        "updated_at=clock_timestamp() "
                        "WHERE job_id=:job_id AND item_key=:item_key"
                    ),
                    {"job_id": job_id, "item_key": item_key},
                )
                connection.execute(
                    text(
                        "UPDATE jobs SET failed=failed+1,updated_at=clock_timestamp() "
                        "WHERE job_id=:job_id"
                    ),
                    {"job_id": job_id},
                )
                return ItemClaim(False, "failed", item["attempt_count"])
            attempt_no = item["attempt_count"] + 1
            connection.execute(
                text(
                    "UPDATE job_items SET status='running',attempt_count=:attempt_no, "
                    "lease_epoch=:lease_epoch,error_code=NULL,updated_at=clock_timestamp() "
                    "WHERE job_id=:job_id AND item_key=:item_key"
                ),
                {
                    "job_id": job_id,
                    "item_key": item_key,
                    "attempt_no": attempt_no,
                    "lease_epoch": lease_epoch,
                },
            )
            return ItemClaim(True, "running", attempt_no)

    def complete_item(
        self,
        job_id: str,
        worker_id: str,
        lease_epoch: int,
        item_key: str,
        result_ref: str,
    ) -> bool:
        _hash("item_key", item_key)
        _result_ref(result_ref)
        with self.engine.begin() as connection:
            if (
                self._locked_active_job(connection, job_id, worker_id, lease_epoch)
                is None
            ):
                return False
            item = (
                connection.execute(
                    text(
                        "SELECT status,lease_epoch,result_ref FROM job_items "
                        "WHERE job_id=:job_id AND item_key=:item_key FOR UPDATE"
                    ),
                    {"job_id": job_id, "item_key": item_key},
                )
                .mappings()
                .first()
            )
            if item is not None and item["status"] == "succeeded":
                if item["result_ref"] != result_ref:
                    raise JobConflictError(
                        "item already has a different immutable result"
                    )
                return False
            if (
                item is None
                or item["status"] != "running"
                or item["lease_epoch"] != lease_epoch
            ):
                return False
            connection.execute(
                text(
                    "UPDATE job_items SET status='succeeded',result_ref=:result_ref,"
                    "error_code=NULL,updated_at=clock_timestamp() "
                    "WHERE job_id=:job_id AND item_key=:item_key"
                ),
                {"job_id": job_id, "item_key": item_key, "result_ref": result_ref},
            )
            connection.execute(
                text(
                    "UPDATE jobs SET completed=completed+1,updated_at=clock_timestamp() "
                    "WHERE job_id=:job_id"
                ),
                {"job_id": job_id},
            )
            return True

    def reconcile_item(
        self,
        job_id: str,
        worker_id: str,
        lease_epoch: int,
        item_key: str,
        result_ref: str,
    ) -> bool:
        """Credit an independently verified immutable artifact after a crash.

        The trusted worker must verify the artifact's identity and checksum
        before calling this method. A stale worker still cannot change the row.
        """
        _hash("item_key", item_key)
        _result_ref(result_ref)
        with self.engine.begin() as connection:
            job = self._locked_active_job(connection, job_id, worker_id, lease_epoch)
            if job is None:
                return False
            item = (
                connection.execute(
                    text(
                        "SELECT status,result_ref FROM job_items "
                        "WHERE job_id=:job_id AND item_key=:item_key "
                        "FOR UPDATE"
                    ),
                    {"job_id": job_id, "item_key": item_key},
                )
                .mappings()
                .first()
            )
            if item is not None and item["status"] == "succeeded":
                if item["result_ref"] != result_ref:
                    raise JobConflictError(
                        "item already has a different immutable result"
                    )
                return False
            if item is None:
                existing_items = connection.scalar(
                    text("SELECT count(*) FROM job_items WHERE job_id=:job_id"),
                    {"job_id": job_id},
                )
                if existing_items >= job["total"]:
                    return False
            if item is None:
                connection.execute(
                    text(
                        "INSERT INTO job_items (job_id,item_key,status,attempt_count,"
                        "max_attempts,lease_epoch,result_ref) "
                        "VALUES (:job_id,:item_key,'succeeded',1,1,:lease_epoch,:result_ref)"
                    ),
                    {
                        "job_id": job_id,
                        "item_key": item_key,
                        "lease_epoch": lease_epoch,
                        "result_ref": result_ref,
                    },
                )
            else:
                connection.execute(
                    text(
                        "UPDATE job_items SET status='succeeded',lease_epoch=:lease_epoch, "
                        "result_ref=:result_ref,error_code=NULL,updated_at=clock_timestamp() "
                        "WHERE job_id=:job_id AND item_key=:item_key"
                    ),
                    {
                        "job_id": job_id,
                        "item_key": item_key,
                        "lease_epoch": lease_epoch,
                        "result_ref": result_ref,
                    },
                )
            connection.execute(
                text(
                    "UPDATE jobs SET completed=completed+1,failed=failed-:failed_delta, "
                    "updated_at=clock_timestamp() WHERE job_id=:job_id"
                ),
                {
                    "job_id": job_id,
                    "failed_delta": int(
                        item is not None and item["status"] == "failed"
                    ),
                },
            )
            return True

    def fail_item(
        self,
        job_id: str,
        worker_id: str,
        lease_epoch: int,
        item_key: str,
        error_code: str,
        *,
        retryable: bool = True,
    ) -> bool:
        _hash("item_key", item_key)
        _error_code(error_code)
        with self.engine.begin() as connection:
            if (
                self._locked_active_job(connection, job_id, worker_id, lease_epoch)
                is None
            ):
                return False
            item = (
                connection.execute(
                    text(
                        "SELECT status,lease_epoch,attempt_count,max_attempts FROM job_items "
                        "WHERE job_id=:job_id AND item_key=:item_key FOR UPDATE"
                    ),
                    {"job_id": job_id, "item_key": item_key},
                )
                .mappings()
                .first()
            )
            if (
                item is None
                or item["status"] != "running"
                or item["lease_epoch"] != lease_epoch
            ):
                return False
            terminal = not retryable or item["attempt_count"] >= item["max_attempts"]
            connection.execute(
                text(
                    "UPDATE job_items SET status=:status,error_code=:error_code, "
                    "updated_at=clock_timestamp() "
                    "WHERE job_id=:job_id AND item_key=:item_key"
                ),
                {
                    "job_id": job_id,
                    "item_key": item_key,
                    "status": "failed" if terminal else "pending",
                    "error_code": error_code,
                },
            )
            if terminal:
                connection.execute(
                    text(
                        "UPDATE jobs SET failed=failed+1,updated_at=clock_timestamp() "
                        "WHERE job_id=:job_id"
                    ),
                    {"job_id": job_id},
                )
            return True

    def finish_job(
        self,
        job_id: str,
        worker_id: str,
        lease_epoch: int,
        *,
        status: str,
        error_code: str | None = None,
    ) -> JobRecord | None:
        if status not in _TERMINAL:
            raise JobContractError(
                "finish status must be succeeded, failed or cancelled"
            )
        _error_code(error_code)
        with self.engine.begin() as connection:
            job = self._locked_active_job(connection, job_id, worker_id, lease_epoch)
            if job is None:
                return None
            if status == "succeeded" and (
                job["cancel_requested"]
                or job["completed"] != job["total"]
                or job["failed"] != 0
            ):
                raise JobConflictError(
                    "a successful job requires every item to succeed"
                )
            if status == "cancelled" and not job["cancel_requested"]:
                raise JobConflictError("job cancellation must first be requested")
            connection.execute(
                text(
                    "UPDATE jobs SET status=:status,error_code=:error_code, "
                    "lease_owner=NULL,lease_expires_at=NULL,finished_at=clock_timestamp(), "
                    "updated_at=clock_timestamp() WHERE job_id=:job_id"
                ),
                {"job_id": job_id, "status": status, "error_code": error_code},
            )
            return self._read_job(connection, job_id)

    def activate_ingestion_job(
        self,
        job_id: str,
        worker_id: str,
        lease_epoch: int,
        item_key: str,
        *,
        request_hash: str,
        activate: Callable[[Connection], str],
    ) -> JobRecord | None:
        """Commit catalog activation and the final item/job state under one lease.

        The callback must use the supplied connection for every catalog read and
        write. Locking the job row first fences takeover and cancellation until
        the catalog pointer and terminal job state commit or roll back together.
        """

        _hash("item_key", item_key)
        _hash("request_hash", request_hash)
        with self.engine.begin() as connection:
            job = self._locked_active_job(connection, job_id, worker_id, lease_epoch)
            if job is None or job["cancel_requested"]:
                return None
            if (
                job["kind"] != "ingestion"
                or job["request_hash"] != request_hash
                or job["stage"] != "activated"
                or job["total"] != len(_INGESTION_STAGES)
                or job["completed"] != job["total"] - 1
                or job["failed"] != 0
            ):
                raise JobConflictError("ingestion activation prerequisites are not met")
            item = (
                connection.execute(
                    text(
                        "SELECT status,lease_epoch FROM job_items "
                        "WHERE job_id=:job_id AND item_key=:item_key FOR UPDATE"
                    ),
                    {"job_id": job_id, "item_key": item_key},
                )
                .mappings()
                .first()
            )
            if (
                item is None
                or item["status"] != "running"
                or item["lease_epoch"] != lease_epoch
            ):
                raise JobConflictError("ingestion activation item is not claimed")
            result_ref = _result_ref(activate(connection))
            connection.execute(
                text(
                    "UPDATE job_items SET status='succeeded',result_ref=:result_ref,"
                    "error_code=NULL,updated_at=clock_timestamp() "
                    "WHERE job_id=:job_id AND item_key=:item_key"
                ),
                {"job_id": job_id, "item_key": item_key, "result_ref": result_ref},
            )
            connection.execute(
                text(
                    "UPDATE jobs SET status='succeeded',completed=completed+1,"
                    "error_code=NULL,lease_owner=NULL,lease_expires_at=NULL,"
                    "finished_at=clock_timestamp(),updated_at=clock_timestamp() "
                    "WHERE job_id=:job_id"
                ),
                {"job_id": job_id},
            )
            return self._read_job(connection, job_id)
