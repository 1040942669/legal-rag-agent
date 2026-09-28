from __future__ import annotations

from sqlalchemy import Engine, select, update

from integration_tests.m5_support import (
    checkpoint_identity,
    create_run_case,
    expire_run_lease,
    record_scenario,
    state_for_checkpoint,
)
from legal_rag.harness.checkpoint import checkpoint_namespace
from legal_rag.harness.state import checkpoint_marker
from legal_rag.services.run_service import CheckpointCompatibilityError
from legal_rag.storage.schema import runs


def test_m5_t07_old_checkpoint_fails_closed_or_enters_explicit_migration_state(
    migrated_engine: Engine,
) -> None:
    case = create_run_case(migrated_engine, case_name="t07-incompatible-checkpoint")
    worker = "m5-t07-first-worker"
    claimed = case.service.claim_next_run(worker, lease_seconds=30)
    assert claimed is not None and claimed.run_id == case.run_id
    frozen = case.service.load_execution_input(
        case.run_id,
        worker,
        lease_epoch=claimed.lease_epoch,
    )
    state = state_for_checkpoint(
        frozen,
        last_completed_node="analyze_query",
        next_node="route",
        retrieval_rounds_used=0,
        tool_attempts_used=0,
    )
    case.service.bind_checkpoint(
        run_id=case.run_id,
        worker_id=worker,
        lease_epoch=claimed.lease_epoch,
        checkpoint_namespace=checkpoint_namespace(
            graph_version=frozen.graph_version,
            schema_version=frozen.state_schema_version,
            lease_epoch=claimed.lease_epoch,
        ),
        checkpoint_id=checkpoint_marker(),
        parent_checkpoint_id=None,
        state=state,
    )
    before = checkpoint_identity(migrated_engine, case.run_id)
    expire_run_lease(migrated_engine, case.run_id)
    case.service.recover_stale_runs()

    with migrated_engine.begin() as connection:
        connection.execute(
            update(runs)
            .where(runs.c.run_id == case.run_id)
            .values(graph_version="m5-bounded-v2")
        )
    try:
        case.service.resume_run(case.boundary.owner, case.run_id)
    except CheckpointCompatibilityError:
        rejected = True
    else:  # pragma: no cover - acceptance invariant.
        rejected = False
    assert rejected is True
    assert checkpoint_identity(migrated_engine, case.run_id) == before
    with migrated_engine.connect() as connection:
        resume_requested_at = connection.scalar(
            select(runs.c.resume_requested_at).where(runs.c.run_id == case.run_id)
        )
    assert resume_requested_at is None
    case.service.cancel_run(case.boundary.owner, case.run_id)
    record_scenario(
        "M5-T07",
        {
            "incompatible_checkpoint_rejected": True,
            "checkpoint_mutated": False,
        },
    )
