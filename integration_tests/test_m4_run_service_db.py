from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event, current_thread

import pytest
from sqlalchemy import Engine, event, func, insert, select, update

from legal_rag.services.run_executor import DeterministicRunExecutor
from legal_rag.services.run_service import (
    ActiveRunConflictError,
    IdempotencyConflictError,
    ResourceNotFoundError,
    ResumeUnsupportedError,
    RunService,
    ServicePrincipal,
    UnsafePayloadError,
    WorkerLeaseLostError,
)
from legal_rag.services.supervisor import RunSupervisor
from legal_rag.storage.schema import (
    active_snapshot_pointers,
    corpus_snapshots,
    embedding_imports,
    embedding_profiles,
    idempotency_keys,
    messages,
    run_events,
    run_results,
    runs,
    snapshot_activation_events,
)


def _seed_service_boundary(engine: Engine) -> tuple[ServicePrincipal, ServicePrincipal]:
    suffix = uuid.uuid4().hex
    scope_id = f"m4-scope-{suffix}"
    snapshot_id = f"m4-snapshot-{suffix}"
    activation_id = uuid.uuid4().hex
    profile_id = suffix * 2
    with engine.begin() as connection:
        connection.execute(
            insert(corpus_snapshots).values(
                snapshot_id=snapshot_id,
                scope_id=scope_id,
                source_manifest={"schema_version": 1, "source": "m4-synthetic"},
                source_manifest_hash="1" * 64,
                corpus_hash="2" * 64,
                status="building",
            )
        )
        connection.execute(
            insert(embedding_profiles).values(
                profile_id=profile_id,
                provider="fixture",
                model="fixture/m4",
                revision="fixture-v1",
                dimensions=3,
                normalization=True,
                query_prefix="",
                document_prefix="",
                embed_with_metadata=True,
                recipe_hash="3" * 64,
            )
        )
        connection.execute(
            insert(embedding_imports).values(
                snapshot_id=snapshot_id,
                profile_id=profile_id,
                bundle_hash="4" * 64,
                status="validated",
            )
        )
        connection.execute(
            update(corpus_snapshots)
            .where(corpus_snapshots.c.snapshot_id == snapshot_id)
            .where(corpus_snapshots.c.status == "building")
            .values(status="validated", validated_at=func.now())
        )
        occurred_at = connection.scalar(select(func.transaction_timestamp()))
        connection.execute(
            insert(snapshot_activation_events).values(
                activation_id=activation_id,
                scope_id=scope_id,
                revision=1,
                operation="initial_activate",
                target_snapshot_id=snapshot_id,
                actor="m4-integration-test",
                reason="synthetic service boundary",
                occurred_at=occurred_at,
            )
        )
        connection.execute(
            update(corpus_snapshots)
            .where(corpus_snapshots.c.snapshot_id == snapshot_id)
            .where(corpus_snapshots.c.status == "validated")
            .values(status="active", activated_at=occurred_at)
        )
        connection.execute(
            insert(active_snapshot_pointers).values(
                scope_id=scope_id,
                snapshot_id=snapshot_id,
                revision=1,
                activation_id=activation_id,
                updated_at=occurred_at,
            )
        )
    return (
        ServicePrincipal(
            user_id=f"user-a-{suffix}",
            scope_id=scope_id,
            profile_id=profile_id,
        ),
        ServicePrincipal(
            user_id=f"user-b-{suffix}",
            scope_id=scope_id,
            profile_id=profile_id,
        ),
    )


def _request(question: str = "合成测试问题") -> dict:
    return {"question": question, "retrieval": {"top_k": 3}}


def _run_counts(engine: Engine, run_id: str) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            "runs": connection.scalar(
                select(func.count()).select_from(runs).where(runs.c.run_id == run_id)
            ),
            "messages": connection.scalar(
                select(func.count())
                .select_from(messages)
                .where(messages.c.run_id == run_id)
            ),
            "results": connection.scalar(
                select(func.count())
                .select_from(run_results)
                .where(run_results.c.run_id == run_id)
            ),
            "keys": connection.scalar(
                select(func.count())
                .select_from(idempotency_keys)
                .where(idempotency_keys.c.run_id == run_id)
            ),
        }


def test_m4_t02_concurrent_same_key_creates_one_run_and_replays(
    migrated_engine: Engine,
) -> None:
    principal, _ = _seed_service_boundary(migrated_engine)
    service = RunService(migrated_engine)
    session = service.create_session(principal, "并发幂等")

    def submit(_: int):
        return service.create_run(
            principal,
            session.session_id,
            "same-key",
            _request(),
            "m4-linear-v1",
        )

    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(submit, range(12)))

    run_ids = {record.run_id for record, _ in outcomes}
    assert len(run_ids) == 1
    assert sum(not replayed for _, replayed in outcomes) == 1
    run_id = run_ids.pop()
    assert _run_counts(migrated_engine, run_id) == {
        "runs": 1,
        "messages": 1,
        "results": 0,
        "keys": 1,
    }

    supervisor = RunSupervisor(
        service,
        DeterministicRunExecutor(),
        lease_seconds=10,
        execution_timeout_seconds=2,
    )
    assert supervisor.run_once() is True
    statements: list[str] = []

    def capture_statement(
        connection,
        cursor,
        statement,
        parameters,
        context,
        executemany,
    ) -> None:
        del connection, cursor, parameters, context, executemany
        statements.append(" ".join(statement.split()))

    event.listen(migrated_engine, "before_cursor_execute", capture_statement)
    try:
        completed = service.get_run(principal, run_id)
    finally:
        event.remove(migrated_engine, "before_cursor_execute", capture_statement)
    assert completed.status == "succeeded"
    run_reads = [statement for statement in statements if "FROM runs" in statement]
    assert len(run_reads) == 1
    assert "LEFT OUTER JOIN run_results" in run_reads[0]
    assert _run_counts(migrated_engine, run_id)["messages"] == 2
    assert _run_counts(migrated_engine, run_id)["results"] == 1
    replayed, was_replayed = service.create_run(
        principal,
        session.session_id,
        "same-key",
        _request(),
        "m4-linear-v1",
    )
    assert was_replayed is True
    assert replayed.run_id == run_id


def test_m4_t03_conflicts_leave_no_orphans_and_active_index_holds(
    migrated_engine: Engine,
) -> None:
    principal, _ = _seed_service_boundary(migrated_engine)
    service = RunService(migrated_engine)
    session = service.create_session(principal)
    created, replayed = service.create_run(
        principal,
        session.session_id,
        "fixed-key",
        _request("第一个问题"),
        "m4-linear-v1",
    )
    assert replayed is False
    with pytest.raises(IdempotencyConflictError):
        service.create_run(
            principal,
            session.session_id,
            "fixed-key",
            _request("不同正文"),
            "m4-linear-v1",
        )
    with pytest.raises(ActiveRunConflictError):
        service.create_run(
            principal,
            session.session_id,
            "different-key",
            _request("另一个问题"),
            "m4-linear-v1",
        )
    assert _run_counts(migrated_engine, created.run_id)["messages"] == 1
    with migrated_engine.connect() as connection:
        assert connection.scalar(
            select(func.count())
            .select_from(idempotency_keys)
            .where(idempotency_keys.c.session_id == session.session_id)
        ) == 1
    assert service.cancel_run(principal, created.run_id).status == "cancelled"


def test_m4_t01_owner_isolation_precedes_cancel_and_resume_behavior(
    migrated_engine: Engine,
) -> None:
    owner, other = _seed_service_boundary(migrated_engine)
    service = RunService(migrated_engine)
    session = service.create_session(owner)
    run, _ = service.create_run(
        owner,
        session.session_id,
        "owner-key",
        _request(),
        "m4-linear-v1",
    )

    with pytest.raises(ResourceNotFoundError):
        service.list_messages(other, session.session_id)
    with pytest.raises(ResourceNotFoundError):
        service.get_run(other, run.run_id)
    with pytest.raises(ResourceNotFoundError):
        service.list_events(other, run.run_id)
    with pytest.raises(ResourceNotFoundError):
        service.cancel_run(other, run.run_id)
    with pytest.raises(ResourceNotFoundError):
        service.resume_unsupported(other, run.run_id)
    with pytest.raises(ResumeUnsupportedError):
        service.resume_unsupported(owner, run.run_id)

    cancelled = service.cancel_run(owner, run.run_id)
    assert cancelled.status == "cancelled"
    assert service.cancel_run(owner, run.run_id).status == "cancelled"


def test_m4_t04_stale_running_becomes_interrupted_and_persists(
    migrated_engine: Engine,
) -> None:
    principal, _ = _seed_service_boundary(migrated_engine)
    first_process = RunService(migrated_engine)
    session = first_process.create_session(principal)
    run, _ = first_process.create_run(
        principal,
        session.session_id,
        "restart-key",
        _request(),
        "m4-linear-v1",
    )
    claimed = first_process.claim_next_run("process-one", 1)
    assert claimed is not None and claimed.run_id == run.run_id

    second_process = RunService(migrated_engine)
    recovered = second_process.recover_stale_runs(
        datetime.now(timezone.utc) + timedelta(seconds=5)
    )
    assert [item.run_id for item in recovered] == [run.run_id]
    assert second_process.get_run(principal, run.run_id).status == "interrupted"
    assert [item.content for item in second_process.list_messages(principal, session.session_id).items] == [
        "合成测试问题"
    ]
    with pytest.raises(ActiveRunConflictError):
        second_process.create_run(
            principal,
            session.session_id,
            "blocked-by-interrupted",
            _request("不能越过中断任务"),
            "m4-linear-v1",
        )
    second_process.cancel_run(principal, run.run_id)
    replacement, _ = second_process.create_run(
        principal,
        session.session_id,
        "after-cancel",
        _request("取消后允许"),
        "m4-linear-v1",
    )
    assert replacement.status == "queued"
    assert second_process.cancel_run(principal, replacement.run_id).status == "cancelled"


def test_m4_t05_events_are_ordered_replayable_and_final_is_atomic(
    migrated_engine: Engine,
) -> None:
    principal, _ = _seed_service_boundary(migrated_engine)
    service = RunService(migrated_engine)
    session = service.create_session(principal)
    run, _ = service.create_run(
        principal,
        session.session_id,
        "events-key",
        _request(),
        "m4-linear-v1",
    )
    supervisor = RunSupervisor(
        service,
        DeterministicRunExecutor("安全最终回答"),
        lease_seconds=10,
        execution_timeout_seconds=2,
    )
    assert supervisor.run_once() is True

    all_events = service.list_events(principal, run.run_id, limit=100)
    assert [event.sequence for event in all_events] == list(
        range(1, len(all_events) + 1)
    )
    assert [event.event_type for event in all_events] == [
        "run.queued",
        "run.started",
        "retrieval.completed",
        "generation.started",
        "verification.completed",
        "answer.final",
    ]
    suffix = service.list_events(
        principal,
        run.run_id,
        after_sequence=3,
        limit=100,
    )
    assert [event.sequence for event in suffix] == [4, 5, 6]
    final = service.get_run(principal, run.run_id)
    assert final.status == "succeeded"
    assert final.result is not None
    assert final.result.answer_text == "安全最终回答"


def test_m4_t06_publish_rejects_private_draft_fields(
    migrated_engine: Engine,
) -> None:
    principal, _ = _seed_service_boundary(migrated_engine)
    service = RunService(migrated_engine)
    session = service.create_session(principal)
    run, _ = service.create_run(
        principal,
        session.session_id,
        "unsafe-result-key",
        _request(),
        "m4-linear-v1",
    )
    service.claim_next_run("unsafe-worker", 30)
    marker = "REJECTED_DRAFT_SENTINEL_M4"
    with pytest.raises(UnsafePayloadError, match="exactly"):
        service.append_stage_event(
            run.run_id,
            "generation.started",
            {"content": marker},
            worker_id="unsafe-worker",
        )
    with pytest.raises(UnsafePayloadError, match="unsupported fields"):
        service.fail_run(
            run.run_id,
            "unsafe-worker",
            "verification_rejected",
            {
                "stage": "verification",
                "retryable": False,
                "content": marker,
            },
        )
    with pytest.raises(UnsafePayloadError):
        service.publish_success(
            run.run_id,
            "unsafe-worker",
            {
                "answer_text": "安全回答",
                "answer_payload": {
                    "answer_text": "安全回答",
                    "answer_mode": "insufficient_evidence",
                    "claims": [],
                },
                "evidence_payload": {"sources": [], "raw_model_output": "秘密草稿"},
                "verification_payload": {"passed": True},
            },
        )
    with pytest.raises(UnsafePayloadError, match="passed must be true"):
        service.publish_success(
            run.run_id,
            "unsafe-worker",
            {
                "answer_text": "未经验证回答",
                "answer_payload": {
                    "answer_text": "未经验证回答",
                    "answer_mode": "insufficient_evidence",
                    "claims": [],
                },
                "evidence_payload": {"sources": []},
                "verification_payload": {"passed": False},
            },
        )
    assert service.get_run(principal, run.run_id).status == "running"
    assert _run_counts(migrated_engine, run.run_id)["results"] == 0
    with migrated_engine.connect() as connection:
        payloads = connection.scalars(
            select(run_events.c.safe_payload).where(run_events.c.run_id == run.run_id)
        ).all()
    assert marker not in repr(payloads)
    assert "秘密草稿" not in repr(payloads)
    failed = service.fail_run(
        run.run_id,
        "unsafe-worker",
        "verification_rejected",
        {"stage": "verification", "retryable": False},
    )
    assert failed.status == "failed"


def test_m4_t07_expired_worker_cannot_publish_after_waiting_on_a_row_lock(
    migrated_engine: Engine,
) -> None:
    principal, _ = _seed_service_boundary(migrated_engine)
    service = RunService(migrated_engine)
    session = service.create_session(principal)
    run, _ = service.create_run(
        principal,
        session.session_id,
        "expired-lease-key",
        _request(),
        "m4-linear-v1",
    )
    claimed = service.claim_next_run("expiring-worker", 1)
    assert claimed is not None and claimed.run_id == run.run_id

    query_started = Event()

    def observe_locking_query(
        connection,
        cursor,
        statement,
        parameters,
        context,
        executemany,
    ) -> None:
        del connection, cursor, parameters, context, executemany
        if (
            current_thread().name.startswith("m4-expired-worker")
            and "FROM runs" in statement
            and "FOR UPDATE" in statement
        ):
            query_started.set()

    locker = migrated_engine.connect()
    transaction = locker.begin()
    locker.execute(
        select(runs.c.run_id).where(runs.c.run_id == run.run_id).with_for_update()
    )
    event.listen(migrated_engine, "before_cursor_execute", observe_locking_query)
    pool = ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="m4-expired-worker",
    )
    try:
        future = pool.submit(
            service.publish_success,
            run.run_id,
            "expiring-worker",
            {
                "answer_text": "不得发布的过期结果",
                "answer_payload": {
                    "answer_text": "不得发布的过期结果",
                    "answer_mode": "insufficient_evidence",
                    "claims": [],
                },
                "evidence_payload": {"sources": []},
                "verification_payload": {"passed": True},
            },
        )
        assert query_started.wait(timeout=2)
        time.sleep(1.1)
        transaction.commit()
        with pytest.raises(WorkerLeaseLostError):
            future.result(timeout=2)
    finally:
        event.remove(migrated_engine, "before_cursor_execute", observe_locking_query)
        if transaction.is_active:
            transaction.rollback()
        locker.close()
        pool.shutdown(wait=True, cancel_futures=True)

    current = service.get_run(principal, run.run_id)
    assert current.status == "running"
    counts = _run_counts(migrated_engine, run.run_id)
    assert counts["results"] == 0
    with migrated_engine.connect() as connection:
        assert connection.scalar(
            select(func.count())
            .select_from(run_events)
            .where(run_events.c.run_id == run.run_id)
        ) == 2
    recovered = service.recover_stale_runs()
    assert [item.run_id for item in recovered] == [run.run_id]
    assert service.cancel_run(principal, run.run_id).status == "cancelled"
