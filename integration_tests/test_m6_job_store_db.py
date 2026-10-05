from __future__ import annotations

import hashlib
import uuid

import pytest
from alembic import command
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from integration_tests.test_m5_schema import _temporary_database
from legal_rag.jobs.store import JobConflictError, JobContractError, JobStore
from legal_rag.storage.migrations import alembic_config, upgrade_database


def _owner() -> str:
    return f"m6-owner-{uuid.uuid4().hex}"


def _job(store: JobStore, owner_id: str, *, total: int = 2):
    return store.create_job(
        owner_id=owner_id,
        kind="evaluation",
        request_ref="m6-evaluation-fixture",
        request_hash=_key("m6-evaluation-fixture"),
        total=total,
    )


def _key(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def test_job_and_outbox_commit_together_then_dispatch_after_restart(
    migrated_engine: Engine,
) -> None:
    first = JobStore(migrated_engine)
    owner_id = _owner()
    job = _job(first, owner_id)
    assert job.status == "queued"
    assert job.outbox_status == "pending"
    assert first.get_job(job.job_id, "other-owner") is None

    restarted = JobStore(migrated_engine)
    found = restarted.get_job(job.job_id, owner_id)
    assert found is not None and found.job_id == job.job_id
    claimed = restarted.claim_outbox("dispatcher-a", limit=10)
    matching = [row for row in claimed if row.job_id == job.job_id]
    assert len(matching) == 1
    message = matching[0]
    assert message.schema_version == 1
    assert restarted.mark_outbox_delivered(
        message.id, "dispatcher-a", message.lease_epoch
    )
    assert restarted.get_job(job.job_id, owner_id).outbox_status == "delivered"


def test_outbox_insert_failure_rolls_back_job_row(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JobStore(migrated_engine)
    existing = _job(store, _owner())
    with migrated_engine.connect() as connection:
        existing_outbox_id = connection.scalar(
            text("SELECT outbox_id FROM job_outbox WHERE job_id=:job_id"),
            {"job_id": existing.job_id},
        )
    proposed_job_id = uuid.uuid4()
    owner_id = _owner()
    generated = iter((proposed_job_id, uuid.UUID(existing_outbox_id)))
    monkeypatch.setattr("legal_rag.jobs.store.uuid.uuid4", lambda: next(generated))
    with pytest.raises(IntegrityError):
        store.create_job(
            owner_id=owner_id,
            kind="evaluation",
            request_ref="rollback-fixture",
            request_hash=_key("rollback-fixture"),
            total=1,
        )
    with migrated_engine.connect() as connection:
        persisted = connection.scalar(
            text("SELECT count(*) FROM jobs WHERE job_id=:job_id"),
            {"job_id": str(proposed_job_id)},
        )
    assert persisted == 0


def test_outbox_redelivers_if_process_dies_after_send_before_mark(
    migrated_engine: Engine,
) -> None:
    store = JobStore(migrated_engine)
    job = _job(store, _owner())
    first = next(
        row for row in store.claim_outbox("dispatcher-a") if row.job_id == job.job_id
    )
    with migrated_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE job_outbox SET claim_expires_at = now() - interval '1 second' "
                "WHERE outbox_id = :outbox_id"
            ),
            {"outbox_id": first.id},
        )
    second = next(
        row
        for row in JobStore(migrated_engine).claim_outbox("dispatcher-b")
        if row.job_id == job.job_id
    )
    assert second.id == first.id
    assert second.lease_epoch > first.lease_epoch
    assert not store.mark_outbox_delivered(first.id, "dispatcher-a", first.lease_epoch)
    assert store.mark_outbox_delivered(second.id, "dispatcher-b", second.lease_epoch)


def test_duplicate_delivery_and_worker_takeover_do_not_double_count_items(
    migrated_engine: Engine,
) -> None:
    store = JobStore(migrated_engine)
    job = _job(store, _owner())
    old = store.claim_job(job.job_id, "worker-a")
    assert old is not None
    first_key, second_key = _key("first"), _key("second")
    assert store.claim_item(job.job_id, "worker-a", old.lease_epoch, first_key).acquired
    assert store.complete_item(
        job.job_id, "worker-a", old.lease_epoch, first_key, "results/first.json"
    )
    assert not store.complete_item(
        job.job_id, "worker-a", old.lease_epoch, first_key, "results/first.json"
    )
    with pytest.raises(JobConflictError):
        store.complete_item(
            job.job_id, "worker-a", old.lease_epoch, first_key, "results/other.json"
        )
    assert store.claim_item(
        job.job_id, "worker-a", old.lease_epoch, second_key
    ).acquired

    with migrated_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE jobs SET lease_expires_at = now() - interval '1 second' "
                "WHERE job_id = :job_id"
            ),
            {"job_id": job.job_id},
        )
    new = store.claim_job(job.job_id, "worker-b")
    assert new is not None and new.lease_epoch > old.lease_epoch
    assert not store.complete_item(
        job.job_id, "worker-a", old.lease_epoch, second_key, "results/stale.json"
    )
    resumed = store.claim_item(job.job_id, "worker-b", new.lease_epoch, second_key)
    assert resumed.acquired and resumed.attempt_no == 2
    assert store.complete_item(
        job.job_id, "worker-b", new.lease_epoch, second_key, "results/second.json"
    )
    assert store.finish_job(job.job_id, "worker-b", new.lease_epoch, status="succeeded")
    assert store.claim_job(job.job_id, "worker-c") is None
    finished = store.get_job_internal(job.job_id)
    assert finished is not None
    assert (finished.completed, finished.failed, finished.pending) == (2, 0, 0)
    with migrated_engine.connect() as connection:
        count = connection.scalar(
            text("SELECT count(*) FROM job_items WHERE job_id=:job_id"),
            {"job_id": job.job_id},
        )
    assert count == 2


def test_cancellation_stops_new_item_claims_at_boundary(
    migrated_engine: Engine,
) -> None:
    store = JobStore(migrated_engine)
    owner_id = _owner()
    job = _job(store, owner_id)
    lease = store.claim_job(job.job_id, "worker-a")
    assert lease is not None
    cancelled = store.request_cancel(job.job_id, owner_id)
    assert cancelled is not None and cancelled.cancel_requested
    assert not store.claim_item(
        job.job_id, "worker-a", lease.lease_epoch, _key("later")
    ).acquired
    assert store.finish_job(
        job.job_id, "worker-a", lease.lease_epoch, status="cancelled"
    )
    assert store.get_job(job.job_id, owner_id).status == "cancelled"


def test_idempotency_is_scoped_to_owner_and_rejects_changed_request(
    migrated_engine: Engine,
) -> None:
    store = JobStore(migrated_engine)
    owner_id = _owner()
    request = {
        "owner_id": owner_id,
        "kind": "evaluation",
        "request_ref": "registered-experiment",
        "request_hash": _key("immutable-work-package"),
        "total": 3,
        "idempotency_key": "one-submit",
    }
    first = store.create_job(**request)
    same = store.create_job(**request)
    assert same.job_id == first.job_id
    with migrated_engine.connect() as connection:
        outboxes = connection.scalar(
            text("SELECT count(*) FROM job_outbox WHERE job_id=:job_id"),
            {"job_id": first.job_id},
        )
    assert outboxes == 1
    with pytest.raises(JobConflictError):
        store.create_job(**{**request, "request_hash": _key("changed-work-package")})
    other_owner = store.create_job(**{**request, "owner_id": _owner()})
    assert other_owner.job_id != first.job_id
    assert store.request_cancel(first.job_id, "wrong-owner") is None


def test_broker_error_is_visible_and_expired_worker_is_requeued(
    migrated_engine: Engine,
) -> None:
    store = JobStore(migrated_engine)
    owner_id = _owner()
    job = _job(store, owner_id)
    first_message = next(
        row for row in store.claim_outbox("dispatcher-down") if row.job_id == job.job_id
    )
    assert store.mark_outbox_retry(
        first_message.id,
        "dispatcher-down",
        first_message.lease_epoch,
        "broker_unavailable",
        delay_seconds=0,
    )
    pending = store.get_job(job.job_id, owner_id)
    assert pending is not None
    assert pending.status == "queued"
    assert pending.outbox_status == "pending"
    assert pending.outbox_error_code == "broker_unavailable"
    resent = next(
        row for row in store.claim_outbox("dispatcher-back") if row.job_id == job.job_id
    )
    assert resent.attempts == 2
    assert store.mark_outbox_delivered(resent.id, "dispatcher-back", resent.lease_epoch)
    lease = store.claim_job(job.job_id, "worker-died")
    assert lease is not None
    with migrated_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE jobs SET lease_expires_at=now()-interval '1 second' "
                "WHERE job_id=:job_id"
            ),
            {"job_id": job.job_id},
        )
    assert store.requeue_expired_jobs() == [job.job_id]
    assert store.requeue_expired_jobs() == []
    recovered_message = next(
        row
        for row in store.claim_outbox("dispatcher-recovery")
        if row.job_id == job.job_id
    )
    assert recovered_message.id != resent.id
    assert store.mark_outbox_delivered(
        recovered_message.id, "dispatcher-recovery", recovered_message.lease_epoch
    )
    resumed = store.claim_job(job.job_id, "worker-restarted")
    assert resumed is not None and resumed.lease_epoch > lease.lease_epoch


def test_failed_item_retry_and_verified_artifact_reconciliation(
    migrated_engine: Engine,
) -> None:
    store = JobStore(migrated_engine)
    job = _job(store, _owner(), total=1)
    lease = store.claim_job(job.job_id, "worker-a")
    assert lease is not None
    key = _key("only-case")
    claim = store.claim_item(
        job.job_id, "worker-a", lease.lease_epoch, key, max_attempts=2
    )
    assert claim.acquired and claim.attempt_no == 1
    assert store.fail_item(
        job.job_id, "worker-a", lease.lease_epoch, key, "temporary_io", retryable=True
    )
    assert store.get_job_internal(job.job_id).failed == 0
    retry = store.claim_item(
        job.job_id, "worker-a", lease.lease_epoch, key, max_attempts=2
    )
    assert retry.acquired and retry.attempt_no == 2
    with migrated_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE jobs SET lease_expires_at=now()-interval '1 second' "
                "WHERE job_id=:job_id"
            ),
            {"job_id": job.job_id},
        )
    resumed = store.claim_job(job.job_id, "worker-b")
    assert resumed is not None
    assert not store.reconcile_item(
        job.job_id, "worker-a", lease.lease_epoch, key, _key("verified-artifact")
    )
    assert store.reconcile_item(
        job.job_id, "worker-b", resumed.lease_epoch, key, _key("verified-artifact")
    )
    assert not store.reconcile_item(
        job.job_id, "worker-b", resumed.lease_epoch, key, _key("verified-artifact")
    )
    assert store.finish_job(
        job.job_id, "worker-b", resumed.lease_epoch, status="succeeded"
    )
    done = store.get_job_internal(job.job_id)
    assert done is not None and (done.completed, done.failed, done.pending) == (1, 0, 0)


def test_ingestion_stage_is_monotonic_and_fenced(migrated_engine: Engine) -> None:
    store = JobStore(migrated_engine)
    job = store.create_job(
        owner_id=_owner(),
        kind="ingestion",
        request_ref="registered-import",
        request_hash=_key("registered-import"),
    )
    lease = store.claim_job(job.job_id, "worker-a")
    assert lease is not None
    assert store.set_total(job.job_id, "worker-a", lease.lease_epoch, 2)
    assert store.set_stage(job.job_id, "worker-a", lease.lease_epoch, "parsed")
    with pytest.raises(
        JobContractError, match="stage is unknown or would move backward"
    ):
        store.set_stage(job.job_id, "worker-a", lease.lease_epoch, "received")
    assert store.get_job_internal(job.job_id).stage == "parsed"


def test_worker_loss_before_first_item_has_durable_claim_cap(
    migrated_engine: Engine,
) -> None:
    store = JobStore(migrated_engine)
    job = _job(store, _owner(), total=1)
    first = store.claim_job(job.job_id, "worker-died", max_claims=1)
    assert first is not None and first.claim_count == 1
    with migrated_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE jobs SET lease_expires_at=now()-interval '1 second' "
                "WHERE job_id=:job_id"
            ),
            {"job_id": job.job_id},
        )
    assert store.claim_job(job.job_id, "worker-restarted", max_claims=1) is None
    terminal = store.get_job_internal(job.job_id)
    assert terminal is not None
    assert terminal.status == "failed"
    assert terminal.claim_count == 1
    assert terminal.error_code == "worker_attempts_exhausted"


def test_delivered_queued_job_is_redelivered_if_never_claimed(
    migrated_engine: Engine,
) -> None:
    store = JobStore(migrated_engine)
    job = _job(store, _owner(), total=1)
    first = next(
        row for row in store.claim_outbox("dispatcher-a") if row.job_id == job.job_id
    )
    assert store.mark_outbox_delivered(first.id, "dispatcher-a", first.lease_epoch)
    with migrated_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE job_outbox SET delivered_at=now()-interval '1 minute' "
                "WHERE outbox_id=:outbox_id"
            ),
            {"outbox_id": first.id},
        )
    assert store.requeue_expired_jobs(min_interval_seconds=30) == [job.job_id]
    assert store.requeue_expired_jobs(min_interval_seconds=30) == []
    second = next(
        row for row in store.claim_outbox("dispatcher-b") if row.job_id == job.job_id
    )
    assert second.id != first.id
    queued = store.get_job_internal(job.job_id)
    assert queued is not None and queued.status == "queued"
    assert queued.outbox_status == "pending"


def test_m6_downgrade_refuses_to_discard_job_history(
    integration_database_url: str,
) -> None:
    with _temporary_database(integration_database_url, "m6job") as engine:
        upgrade_database(engine)
        job = _job(JobStore(engine), _owner())
        with pytest.raises(RuntimeError, match="cannot downgrade M6 while jobs exist"):
            with engine.begin() as connection:
                command.downgrade(
                    alembic_config(connection=connection), "0006_m5_harness_recovery"
                )
        with engine.connect() as connection:
            assert MigrationContext.configure(connection).get_current_revision() == (
                "0008_execution_money"
            )
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM jobs WHERE job_id=:job_id"),
                    {"job_id": job.job_id},
                )
                == 1
            )


def test_empty_m6_tables_can_downgrade_and_upgrade(
    integration_database_url: str,
) -> None:
    with _temporary_database(integration_database_url, "m6empty") as engine:
        upgrade_database(engine)
        with engine.begin() as connection:
            command.downgrade(
                alembic_config(connection=connection), "0006_m5_harness_recovery"
            )
        with engine.connect() as connection:
            assert MigrationContext.configure(connection).get_current_revision() == (
                "0006_m5_harness_recovery"
            )
        upgrade_database(engine)
        with engine.connect() as connection:
            assert MigrationContext.configure(connection).get_current_revision() == (
                "0008_execution_money"
            )
