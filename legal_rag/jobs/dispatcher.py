from __future__ import annotations

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


def _observe_dispatch(observer: Any | None, job_id: str, *, delivered: bool) -> None:
    if observer is None:
        return
    try:
        from legal_rag.observability.events import Observation, ObservationContext

        observer.record(
            Observation(
                context=ObservationContext(trace_id=job_id, job_id=job_id),
                name="job.queued" if delivered else "job.retry",
                status="queued" if delivered else "retrying",
                tool="dispatcher",
                error_category=None if delivered else "broker_unavailable",
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
        if record.schema_version != MESSAGE_SCHEMA_VERSION:
            store.mark_outbox_retry(
                record.id,
                dispatcher_id,
                record.lease_epoch,
                "unsupported_message_schema",
                delay_seconds=retry_delay_seconds,
            )
            deferred += 1
            _observe_dispatch(observer, record.job_id, delivered=False)
            continue
        try:
            publisher.publish(
                job_id=record.job_id,
                schema_version=record.schema_version,
                task_id=f"legal-rag-job-{record.job_id}",
            )
        except Exception:  # noqa: BLE001 - do not leak broker credentials.
            store.mark_outbox_retry(
                record.id,
                dispatcher_id,
                record.lease_epoch,
                "broker_unavailable",
                delay_seconds=retry_delay_seconds,
            )
            deferred += 1
            _observe_dispatch(observer, record.job_id, delivered=False)
            continue
        if store.mark_outbox_delivered(
            record.id,
            dispatcher_id,
            record.lease_epoch,
        ):
            delivered += 1
            _observe_dispatch(observer, record.job_id, delivered=True)
    return DispatchReport(
        recovered=len(recovered),
        attempted=len(records),
        delivered=delivered,
        deferred=deferred,
    )
