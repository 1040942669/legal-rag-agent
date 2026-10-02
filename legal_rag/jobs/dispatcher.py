from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

from .worker import MESSAGE_SCHEMA_VERSION, TASK_NAME


@dataclass(frozen=True, slots=True)
class DispatchReport:
    recovered: int
    attempted: int
    delivered: int
    deferred: int


class Publisher(Protocol):
    def publish(self, *, job_id: str, schema_version: int, task_id: str) -> None: ...


class CeleryPublisher:
    def __init__(self, app: Any) -> None:
        self.app = app

    def publish(self, *, job_id: str, schema_version: int, task_id: str) -> None:
        self.app.send_task(
            TASK_NAME,
            args=[job_id, schema_version],
            task_id=task_id,
            serializer="json",
            retry=False,
            queue="legal_rag_jobs",
        )


def _observe_dispatch(
    observer: Any | None,
    record: Any,
    *,
    delivered: bool,
    duration_ms: float,
    error_category: str | None = None,
    retry_recorded: bool = False,
) -> None:
    if observer is None:
        return
    try:
        from legal_rag.observability.events import Observation, ObservationContext

        observer.record(
            Observation(
                context=ObservationContext(
                    trace_id=record.job_id, job_id=record.job_id
                ),
                # Publishing a duplicate may happen after the business job has
                # started or finished; delivery does not put it back in queued.
                name="job.retry" if retry_recorded else "tool.completed",
                status=(
                    "succeeded"
                    if delivered
                    else "retrying"
                    if retry_recorded
                    else "failed"
                ),
                tool="dispatcher",
                # claim_outbox increments delivery_attempts before this attempt.
                # Recovery-row generation is not a broker publish retry count.
                retry_count=max(0, record.attempts - 1),
                duration_ms=duration_ms,
                error_category=error_category,
            )
        )
    except Exception:  # noqa: BLE001 - exporter failures cannot block delivery.
        return


def dispatch_once(
    store: Any,
    publisher: Publisher,
    *,
    dispatcher_id: str,
    limit: int = 20,
    lease_seconds: int = 30,
    retry_delay_seconds: int = 5,
    observer: Any | None = None,
) -> DispatchReport:
    """Drain committed outbox rows; uncertain publishes may be repeated safely."""

    if not dispatcher_id or limit < 1 or lease_seconds < 1 or retry_delay_seconds < 1:
        raise ValueError("dispatcher limits and identity are invalid")
    recovered = store.requeue_expired_jobs(limit=limit)
    records = store.claim_outbox(
        dispatcher_id,
        limit=limit,
        lease_seconds=lease_seconds,
    )
    delivered = 0
    deferred = 0
    for record in records:
        started = time.perf_counter()
        if record.schema_version != MESSAGE_SCHEMA_VERSION:
            retry_recorded = store.mark_outbox_retry(
                record.id,
                dispatcher_id,
                record.lease_epoch,
                "unsupported_message_schema",
                delay_seconds=retry_delay_seconds,
            )
            deferred += 1
            _observe_dispatch(
                observer,
                record,
                delivered=False,
                duration_ms=(time.perf_counter() - started) * 1000,
                error_category="validation_failed",
                retry_recorded=retry_recorded,
            )
            continue
        try:
            publisher.publish(
                job_id=record.job_id,
                schema_version=record.schema_version,
                task_id=f"legal-rag-job-{record.job_id}",
            )
        except Exception:  # noqa: BLE001 - do not leak broker credentials.
            retry_recorded = store.mark_outbox_retry(
                record.id,
                dispatcher_id,
                record.lease_epoch,
                "broker_unavailable",
                delay_seconds=retry_delay_seconds,
            )
            deferred += 1
            _observe_dispatch(
                observer,
                record,
                delivered=False,
                duration_ms=(time.perf_counter() - started) * 1000,
                error_category="broker_unavailable",
                retry_recorded=retry_recorded,
            )
            continue
        if store.mark_outbox_delivered(
            record.id,
            dispatcher_id,
            record.lease_epoch,
        ):
            delivered += 1
            _observe_dispatch(
                observer,
                record,
                delivered=True,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
    return DispatchReport(
        recovered=len(recovered),
        attempted=len(records),
        delivered=delivered,
        deferred=deferred,
    )
