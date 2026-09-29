"""Real PostgreSQL regressions for bounded M6 broker redelivery."""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import Engine, text

from legal_rag.jobs.store import JobStore


def _new_job(store: JobStore) -> str:
    reference = f"bounded-recovery-{uuid.uuid4().hex}"
    job = store.create_job(
        owner_id=f"owner-{uuid.uuid4().hex}",
        kind="evaluation",
        request_ref=reference,
        request_hash=hashlib.sha256(reference.encode("ascii")).hexdigest(),
        total=1,
    )
    return job.job_id


def _outbox_count(engine: Engine, job_id: str) -> int:
    with engine.connect() as connection:
        return connection.scalar(
            text("SELECT count(*) FROM job_outbox WHERE job_id=:job_id"),
            {"job_id": job_id},
        )


def _age_last_delivery(engine: Engine, job_id: str, *, seconds: int) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE job_outbox SET delivered_at=clock_timestamp()-"
                "make_interval(secs=>:seconds) WHERE job_id=:job_id "
                "AND status='delivered'"
            ),
            {"job_id": job_id, "seconds": seconds},
        )
        connection.execute(
            text(
                "UPDATE jobs SET recovery_queued_at=clock_timestamp()-"
                "make_interval(secs=>:seconds) WHERE job_id=:job_id"
            ),
            {"job_id": job_id, "seconds": seconds},
        )


def _deliver(store: JobStore, job_id: str, dispatcher_id: str) -> None:
    message = next(
        row
        for row in store.claim_outbox(dispatcher_id, limit=1000)
        if row.job_id == job_id
    )
    assert store.mark_outbox_delivered(message.id, dispatcher_id, message.lease_epoch)


def test_queued_job_without_worker_has_bounded_automatic_redelivery(
    migrated_engine: Engine,
) -> None:
    store = JobStore(migrated_engine)
    job_id = _new_job(store)
    _deliver(store, job_id, "dispatcher-initial")

    for attempt in range(2):
        _age_last_delivery(migrated_engine, job_id, seconds=3600)
        assert store.requeue_expired_jobs(max_auto_deliveries=3) == [job_id]
        _deliver(store, job_id, f"dispatcher-recovery-{attempt}")

    _age_last_delivery(migrated_engine, job_id, seconds=3600)
    assert store.requeue_expired_jobs(max_auto_deliveries=3) == []
    for _ in range(5):
        assert store.requeue_expired_jobs(max_auto_deliveries=3) == []

    state = store.get_job_internal(job_id)
    assert state is not None
    assert state.status == "queued"
    assert state.error_code == "delivery_unconfirmed"
    assert state.outbox_status == "delivered"
    assert _outbox_count(migrated_engine, job_id) == 3
    assert store.claim_job(job_id, "worker-returned") is not None
    resumed = store.get_job_internal(job_id)
    assert resumed is not None
    assert resumed.status == "running"
    assert resumed.error_code is None


def test_queued_recovery_backs_off_between_successful_publications(
    migrated_engine: Engine,
) -> None:
    store = JobStore(migrated_engine)
    job_id = _new_job(store)
    _deliver(store, job_id, "dispatcher-initial")

    _age_last_delivery(migrated_engine, job_id, seconds=45)
    assert store.requeue_expired_jobs(max_auto_deliveries=4) == [job_id]
    _deliver(store, job_id, "dispatcher-first-recovery")

    _age_last_delivery(migrated_engine, job_id, seconds=45)
    assert store.requeue_expired_jobs(max_auto_deliveries=4) == []
    assert _outbox_count(migrated_engine, job_id) == 2

    _age_last_delivery(migrated_engine, job_id, seconds=120)
    assert store.requeue_expired_jobs(max_auto_deliveries=4) == [job_id]
    assert _outbox_count(migrated_engine, job_id) == 3


def test_expired_running_job_has_same_bound_and_remains_claimable(
    migrated_engine: Engine,
) -> None:
    store = JobStore(migrated_engine)
    job_id = _new_job(store)
    _deliver(store, job_id, "dispatcher-initial")
    first_lease = store.claim_job(job_id, "worker-lost")
    assert first_lease is not None
    with migrated_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 second' "
                "WHERE job_id=:job_id"
            ),
            {"job_id": job_id},
        )

    _age_last_delivery(migrated_engine, job_id, seconds=3600)
    assert store.requeue_expired_jobs(max_auto_deliveries=2) == [job_id]
    _deliver(store, job_id, "dispatcher-recovery")
    _age_last_delivery(migrated_engine, job_id, seconds=3600)
    assert store.requeue_expired_jobs(max_auto_deliveries=2) == []
    state = store.get_job_internal(job_id)
    assert state is not None
    assert state.status == "queued"
    assert state.error_code == "delivery_unconfirmed"
    assert _outbox_count(migrated_engine, job_id) == 2
    assert not store.set_stage(job_id, "worker-lost", first_lease.lease_epoch, "old")
    resumed = store.claim_job(job_id, "worker-returned")
    assert resumed is not None and resumed.lease_epoch > first_lease.lease_epoch
    assert resumed.error_code is None


def test_pending_broker_retry_does_not_spend_recovery_budget(
    migrated_engine: Engine,
) -> None:
    store = JobStore(migrated_engine)
    job_id = _new_job(store)
    initial = next(
        row
        for row in store.claim_outbox("dispatcher-down", limit=1000)
        if row.job_id == job_id
    )
    assert store.mark_outbox_retry(
        initial.id,
        "dispatcher-down",
        initial.lease_epoch,
        "broker_unavailable",
        delay_seconds=0,
    )
    assert store.requeue_expired_jobs(max_auto_deliveries=2) == []
    assert _outbox_count(migrated_engine, job_id) == 1

    _deliver(store, job_id, "dispatcher-back")
    _age_last_delivery(migrated_engine, job_id, seconds=3600)
    assert store.requeue_expired_jobs(max_auto_deliveries=2) == [job_id]
    assert _outbox_count(migrated_engine, job_id) == 2
