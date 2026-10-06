from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from alembic import command
from alembic.runtime.migration import MigrationContext
from sqlalchemy import (
    Engine,
    create_engine,
    func,
    inspect,
    insert,
    make_url,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError

from legal_rag.storage.database import DatabaseSettings, create_database_engine
from legal_rag.storage.migrations import alembic_config, upgrade_database
from legal_rag.storage.schema import (
    active_snapshot_pointers,
    corpus_snapshots,
    embedding_profiles,
    run_budget_ledgers,
    run_checkpoints,
    run_events,
    run_external_attempts,
    run_node_artifacts,
    runs,
    sessions,
    snapshot_activation_events,
)


HEAD_REVISION = "0008_execution_money"
M4_REVISION = "0005_m4_api_sessions"
_APPLICATION_RECOVERY_TABLES = {
    "run_budget_ledgers",
    "run_external_attempts",
    "run_node_artifacts",
    "run_checkpoints",
}
_LANGGRAPH_PRIVATE_TABLES = {
    "checkpoint_migrations",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "store",
    "store_migrations",
}


@contextmanager
def _temporary_database(base_url: str, purpose: str) -> Iterator[Engine]:
    parsed = make_url(base_url)
    database_name = f"legal_rag_m5_schema_{purpose}_{uuid.uuid4().hex[:10]}"
    if re.fullmatch(r"[a-z0-9_]+", database_name) is None:
        raise AssertionError("generated integration database name is unsafe")
    admin_engine = create_engine(
        parsed.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    engine: Engine | None = None
    try:
        with admin_engine.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
        database_url = parsed.set(database=database_name).render_as_string(
            hide_password=False
        )
        engine = create_database_engine(DatabaseSettings(database_url))
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        with admin_engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            connection.exec_driver_sql(
                f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'
            )
        admin_engine.dispose()


def _downgrade_database(engine: Engine, revision: str) -> None:
    with engine.begin() as connection:
        command.downgrade(alembic_config(connection=connection), revision)


def _current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def _seed_m4_compatible_run(engine: Engine) -> dict[str, str]:
    suffix = uuid.uuid4().hex
    run_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    user_id = f"m5-user-{suffix}"
    scope_id = f"m5-scope-{suffix}"
    snapshot_id = f"m5-snapshot-{suffix}"
    activation_id = uuid.uuid4().hex
    profile_id = suffix * 2
    graph_version = "m5-bounded-v1"
    retrieval_config_hash = "7" * 64
    with engine.begin() as connection:
        connection.execute(
            insert(corpus_snapshots).values(
                snapshot_id=snapshot_id,
                scope_id=scope_id,
                source_manifest={"schema_version": 1, "source": "m5-schema-test"},
                source_manifest_hash="1" * 64,
                corpus_hash="2" * 64,
                status="building",
            )
        )
        connection.execute(
            insert(embedding_profiles).values(
                profile_id=profile_id,
                provider="fixture",
                model="fixture/m5-schema",
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
            update(corpus_snapshots)
            .where(corpus_snapshots.c.snapshot_id == snapshot_id)
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
                actor="m5-schema-test",
                reason="synthetic M5 migration fixture",
                occurred_at=occurred_at,
            )
        )
        connection.execute(
            update(corpus_snapshots)
            .where(corpus_snapshots.c.snapshot_id == snapshot_id)
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
        connection.execute(
            insert(sessions).values(
                session_id=session_id,
                user_id=user_id,
                title="M5 schema test",
            )
        )
        connection.execute(
            insert(runs).values(
                run_id=run_id,
                session_id=session_id,
                user_id=user_id,
                status="queued",
                request_hash="4" * 64,
                request_payload={"question": "M5 schema migration test"},
                scope_id=scope_id,
                snapshot_id=snapshot_id,
                snapshot_revision=1,
                activation_id=activation_id,
                profile_id=profile_id,
                boundary_fingerprint="5" * 64,
                retrieval_config_hash=retrieval_config_hash,
                graph_version=graph_version,
                event_sequence=1,
            )
        )
        connection.execute(
            insert(run_events).values(
                run_id=run_id,
                sequence=1,
                event_type="run.queued",
                safe_payload={"status": "queued"},
            )
        )
    return {
        "run_id": run_id,
        "session_id": session_id,
        "user_id": user_id,
        "snapshot_id": snapshot_id,
        "profile_id": profile_id,
        "graph_version": graph_version,
        "retrieval_config_hash": retrieval_config_hash,
    }


def _checkpoint_state(identity: dict[str, str], checkpoint_id: str) -> dict:
    return {
        "run_id": identity["run_id"],
        "session_id": identity["session_id"],
        "user_id": identity["user_id"],
        "schema_version": 1,
        "graph_version": identity["graph_version"],
        "retrieval_config_hash": identity["retrieval_config_hash"],
        "question": "M5 schema migration test",
        "bounded_history_refs": [],
        "snapshot_id": identity["snapshot_id"],
        "embedding_profile_id": identity["profile_id"],
        "analysis": {},
        "proposed_queries": [],
        "completed_query_hashes": [],
        "retrieval_rounds_used": 1,
        "model_attempts_used": 0,
        "tool_attempts_used": 1,
        "embedding_attempts_used": 1,
        "execution_deadline_at": (
            datetime.now(timezone.utc) + timedelta(minutes=2)
        ).isoformat(),
        "budget_ledger_ref": f"run-budget:{identity['run_id']}",
        "retrieved_evidence_refs": ["artifact:retrieval-1"],
        "immutable_evidence_hashes": ["8" * 64],
        "retrieved_artifact_ref": "artifact:retrieval-1",
        "retrieved_artifact_hash": "8" * 64,
        "answer_draft_ref": None,
        "verification_result_ref": None,
        "verification_result_hash": None,
        "last_completed_node": "retrieve",
        "checkpoint_id": checkpoint_id,
        "stop_reason": None,
        "last_error_code": None,
        "route": "retrieve",
        "completion_status": None,
        "next_node": "merge_evidence",
    }


def _payload_hash(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def test_m5_schema_matches_metadata_without_langgraph_private_tables(
    integration_database_url: str,
) -> None:
    with _temporary_database(integration_database_url, "shape") as engine:
        upgrade_database(engine)
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        assert _APPLICATION_RECOVERY_TABLES <= table_names
        assert _LANGGRAPH_PRIVATE_TABLES.isdisjoint(table_names)
        assert _current_revision(engine) == HEAD_REVISION

        run_columns = {item["name"]: item for item in inspector.get_columns("runs")}
        assert {
            "parent_run_id",
            "state_schema_version",
            "checkpoint_namespace",
            "last_checkpoint_id",
            "last_completed_node",
            "execution_deadline_at",
            "stop_reason",
            "lease_epoch",
            "resume_requested_at",
        } <= set(run_columns)
        assert run_columns["state_schema_version"]["nullable"] is False
        assert run_columns["lease_epoch"]["nullable"] is False
        assert run_columns["checkpoint_namespace"]["nullable"] is True

        run_checks = {item["name"] for item in inspector.get_check_constraints("runs")}
        assert {
            "ck_runs_status",
            "ck_runs_finished_at",
            "ck_runs_parent_not_self",
            "ck_runs_state_schema_version",
            "ck_runs_lease_epoch",
            "ck_runs_checkpoint_pointer",
        } <= run_checks
        run_indexes = {item["name"] for item in inspector.get_indexes("runs")}
        assert {"ix_runs_parent_run_id", "ix_runs_resume_requested"} <= run_indexes

        checkpoint_columns = {
            item["name"] for item in inspector.get_columns("run_checkpoints")
        }
        assert {
            "run_id",
            "checkpoint_namespace",
            "checkpoint_id",
            "parent_checkpoint_id",
            "schema_version",
            "graph_version",
            "retrieval_config_hash",
            "last_completed_node",
            "state_payload",
            "state_hash",
            "lease_epoch",
            "created_at",
        } == checkpoint_columns
        checkpoint_checks = {
            item["name"] for item in inspector.get_check_constraints("run_checkpoints")
        }
        assert "ck_run_checkpoints_state_payload" in checkpoint_checks
        checkpoint_foreign_keys = {
            item["name"] for item in inspector.get_foreign_keys("run_checkpoints")
        }
        assert {
            "fk_run_checkpoints_run",
            "fk_run_checkpoints_parent",
        } <= checkpoint_foreign_keys

        with engine.connect() as connection:
            command.check(alembic_config(connection=connection))


def test_m5_database_constraints_fence_budget_attempts_and_checkpoint_state(
    integration_database_url: str,
) -> None:
    with _temporary_database(integration_database_url, "constraints") as engine:
        upgrade_database(engine)
        identity = _seed_m4_compatible_run(engine)
        run_id = identity["run_id"]
        with engine.connect() as connection:
            run_row = connection.execute(
                select(
                    runs.c.state_schema_version,
                    runs.c.lease_epoch,
                    runs.c.checkpoint_namespace,
                ).where(runs.c.run_id == run_id)
            ).one()
        assert tuple(run_row) == (1, 0, None)
        namespace = "m5:m5-bounded-v1:1:lease-1"
        with engine.begin() as connection:
            connection.execute(
                update(runs)
                .where(runs.c.run_id == run_id)
                .values(
                    status="running",
                    started_at=func.now(),
                    execution_deadline_at=func.now() + text("interval '2 minutes'"),
                    checkpoint_namespace=namespace,
                    lease_epoch=1,
                )
            )

        with engine.begin() as connection:
            connection.execute(
                insert(run_budget_ledgers).values(
                    run_id=run_id,
                    max_retrieval_rounds=2,
                    max_queries_per_round=3,
                    max_tool_attempts=8,
                    max_model_attempts=4,
                    max_embedding_attempts=4,
                    max_retry_per_operation=1,
                    evidence_top_k=5,
                    retrieval_rounds_used=1,
                    queries_used=2,
                    tool_attempts_used=1,
                    model_attempts_used=0,
                    embedding_attempts_used=1,
                )
            )
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    update(run_budget_ledgers)
                    .where(run_budget_ledgers.c.run_id == run_id)
                    .values(model_attempts_used=5)
                )

        with engine.begin() as connection:
            connection.execute(
                insert(run_node_artifacts).values(
                    artifact_id=str(uuid.uuid4()),
                    run_id=run_id,
                    node_name="retrieve",
                    artifact_kind="retrieval",
                    artifact_ref="artifact:retrieval-1",
                    payload_hash="8" * 64,
                    payload={"evidence_refs": ["evidence:1"]},
                    lease_epoch=1,
                )
            )
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    insert(run_node_artifacts).values(
                        artifact_id=str(uuid.uuid4()),
                        run_id=run_id,
                        node_name="generate",
                        artifact_kind="draft",
                        artifact_ref="artifact:unsafe-draft",
                        payload_hash="9" * 64,
                        payload={"draft": "must not persist"},
                        lease_epoch=1,
                    )
                )

        checkpoint_id = str(uuid.uuid4())
        state = _checkpoint_state(identity, checkpoint_id)
        with engine.begin() as connection:
            connection.execute(
                insert(run_checkpoints).values(
                    run_id=run_id,
                    checkpoint_namespace=namespace,
                    checkpoint_id=checkpoint_id,
                    schema_version=1,
                    graph_version=identity["graph_version"],
                    retrieval_config_hash=identity["retrieval_config_hash"],
                    last_completed_node="retrieve",
                    state_payload=state,
                    state_hash=_payload_hash(state),
                    lease_epoch=1,
                )
            )
        unsafe_checkpoint_id = str(uuid.uuid4())
        unsafe_state = _checkpoint_state(identity, unsafe_checkpoint_id)
        unsafe_state["api_key"] = "must-not-enter-checkpoint"
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    insert(run_checkpoints).values(
                        run_id=run_id,
                        checkpoint_namespace=namespace,
                        checkpoint_id=unsafe_checkpoint_id,
                        parent_checkpoint_id=checkpoint_id,
                        schema_version=1,
                        graph_version=identity["graph_version"],
                        retrieval_config_hash=identity["retrieval_config_hash"],
                        last_completed_node="retrieve",
                        state_payload=unsafe_state,
                        state_hash=_payload_hash(unsafe_state),
                        lease_epoch=1,
                    )
                )

        timestamp = datetime.now(timezone.utc)
        with engine.begin() as connection:
            connection.execute(
                insert(run_external_attempts).values(
                    attempt_id=str(uuid.uuid4()),
                    run_id=run_id,
                    lease_epoch=1,
                    operation_key="generate:primary",
                    operation_kind="model",
                    operation_name="generate",
                    attempt_no=1,
                    request_hash="a" * 64,
                    provider_request_id="provider-request-1",
                    status="outcome_unknown",
                    retryable=True,
                    error_code="provider_outcome_unknown",
                    reserved_at=timestamp,
                    dispatched_at=timestamp,
                    completed_at=timestamp,
                )
            )
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    insert(run_external_attempts).values(
                        attempt_id=str(uuid.uuid4()),
                        run_id=run_id,
                        lease_epoch=1,
                        operation_key="generate:not-dispatched",
                        operation_kind="model",
                        operation_name="generate",
                        attempt_no=1,
                        request_hash="b" * 64,
                        provider_request_id="impossible-before-dispatch",
                        status="reserved",
                    )
                )

        with engine.begin() as connection:
            connection.execute(
                insert(run_events).values(
                    run_id=run_id,
                    sequence=2,
                    event_type="attempt.outcome_unknown",
                    safe_payload={"attempt_ref": "generate:primary:1"},
                )
            )
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    insert(run_events).values(
                        run_id=run_id,
                        sequence=3,
                        event_type="node.draft.persisted",
                        safe_payload={"draft": "unsafe"},
                    )
                )

        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    update(runs)
                    .where(runs.c.run_id == run_id)
                    .values(status="completed_with_limits")
                )
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    update(runs)
                    .where(runs.c.run_id == run_id)
                    .values(parent_run_id=run_id)
                )
        with engine.begin() as connection:
            connection.execute(
                update(runs)
                .where(runs.c.run_id == run_id)
                .values(
                    status="completed_with_limits",
                    finished_at=func.now(),
                    stop_reason="model_budget_exhausted",
                    checkpoint_namespace=namespace,
                    last_checkpoint_id=checkpoint_id,
                    last_completed_node="retrieve",
                    lease_epoch=1,
                )
            )


def test_m5_migration_roundtrip_preserves_m4_rows_and_refuses_lossy_statuses(
    integration_database_url: str,
) -> None:
    with _temporary_database(integration_database_url, "roundtrip") as engine:
        upgrade_database(engine)
        identity = _seed_m4_compatible_run(engine)
        run_id = identity["run_id"]

        _downgrade_database(engine, M4_REVISION)
        assert _current_revision(engine) == M4_REVISION
        downgraded_inspector = inspect(engine)
        assert _APPLICATION_RECOVERY_TABLES.isdisjoint(
            downgraded_inspector.get_table_names()
        )
        assert "lease_epoch" not in {
            item["name"] for item in downgraded_inspector.get_columns("runs")
        }
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(runs)
                    .where(runs.c.run_id == run_id)
                )
                == 1
            )
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(run_events)
                    .where(run_events.c.run_id == run_id)
                )
                == 1
            )

        upgrade_database(engine)
        assert _current_revision(engine) == HEAD_REVISION
        with engine.connect() as connection:
            assert tuple(
                connection.execute(
                    select(runs.c.state_schema_version, runs.c.lease_epoch).where(
                        runs.c.run_id == run_id
                    )
                ).one()
            ) == (1, 0)
            command.check(alembic_config(connection=connection))

        with engine.begin() as connection:
            connection.execute(
                update(runs)
                .where(runs.c.run_id == run_id)
                .values(status="completed_with_limits", finished_at=func.now())
            )
            connection.execute(
                insert(run_events).values(
                    run_id=run_id,
                    sequence=2,
                    event_type="run.completed_with_limits",
                    safe_payload={"stop_reason": "deadline_exceeded"},
                )
            )
        with pytest.raises(RuntimeError, match="cannot downgrade M5"):
            _downgrade_database(engine, M4_REVISION)
        assert _current_revision(engine) == HEAD_REVISION
        assert _APPLICATION_RECOVERY_TABLES <= set(inspect(engine).get_table_names())
