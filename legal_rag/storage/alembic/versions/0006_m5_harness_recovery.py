"""Add M5 durable harness recovery, budgets, attempts, and checkpoints.

Revision ID: 0006_m5_harness_recovery
Revises: 0005_m4_api_sessions
Create Date: 2026-09-29

Only application-owned recovery records are defined here.  LangGraph's private
PostgreSQL checkpoint tables remain owned and migrated by LangGraph.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0006_m5_harness_recovery"
down_revision: str | None = "0005_m4_api_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_M5_CHECKPOINT_STATE_KEYS = (
    "run_id",
    "session_id",
    "user_id",
    "schema_version",
    "graph_version",
    "retrieval_config_hash",
    "question",
    "bounded_history_refs",
    "snapshot_id",
    "embedding_profile_id",
    "analysis",
    "proposed_queries",
    "completed_query_hashes",
    "retrieval_rounds_used",
    "model_attempts_used",
    "tool_attempts_used",
    "embedding_attempts_used",
    "execution_deadline_at",
    "budget_ledger_ref",
    "retrieved_evidence_refs",
    "immutable_evidence_hashes",
    "retrieved_artifact_ref",
    "retrieved_artifact_hash",
    "answer_draft_ref",
    "verification_result_ref",
    "verification_result_hash",
    "last_completed_node",
    "checkpoint_id",
    "stop_reason",
    "last_error_code",
    "route",
    "completion_status",
    "next_node",
)
_M5_CHECKPOINT_STATE_KEYS_SQL = "ARRAY[{}]::text[]".format(
    ", ".join(f"'{key}'" for key in _M5_CHECKPOINT_STATE_KEYS)
)
_M5_CHECKPOINT_STATE_CONSTRAINT = (
    "jsonb_typeof(state_payload) = 'object' "
    f"AND state_payload ?& {_M5_CHECKPOINT_STATE_KEYS_SQL} "
    f"AND (state_payload - {_M5_CHECKPOINT_STATE_KEYS_SQL}) = '{{}}'::jsonb "
    "AND jsonb_typeof(state_payload -> 'run_id') = 'string' "
    "AND state_payload ->> 'run_id' = run_id "
    "AND jsonb_typeof(state_payload -> 'schema_version') = 'number' "
    "AND (state_payload ->> 'schema_version')::integer = schema_version "
    "AND jsonb_typeof(state_payload -> 'graph_version') = 'string' "
    "AND state_payload ->> 'graph_version' = graph_version "
    "AND jsonb_typeof(state_payload -> 'retrieval_config_hash') = 'string' "
    "AND state_payload ->> 'retrieval_config_hash' = retrieval_config_hash "
    "AND jsonb_typeof(state_payload -> 'checkpoint_id') = 'string' "
    "AND state_payload ->> 'checkpoint_id' = checkpoint_id "
    "AND (state_payload ->> 'last_completed_node') "
    "IS NOT DISTINCT FROM last_completed_node "
    "AND state_payload -> 'answer_draft_ref' = 'null'::jsonb"
)


def upgrade() -> None:
    op.drop_constraint("ck_runs_status", "runs", type_="check")
    op.drop_constraint("ck_runs_finished_at", "runs", type_="check")

    op.add_column(
        "runs",
        sa.Column("parent_run_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "runs",
        sa.Column(
            "state_schema_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.add_column(
        "runs",
        sa.Column("checkpoint_namespace", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "runs",
        sa.Column("last_checkpoint_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "runs",
        sa.Column("last_completed_node", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "runs",
        sa.Column("execution_deadline_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "runs",
        sa.Column("stop_reason", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "runs",
        sa.Column(
            "lease_epoch",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "runs",
        sa.Column("resume_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_runs_parent_run",
        "runs",
        "runs",
        ["parent_run_id"],
        ["run_id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_runs_status",
        "runs",
        "status IN ('queued', 'running', 'interrupted', 'succeeded', "
        "'completed_with_limits', 'needs_clarification', 'failed', 'cancelled')",
    )
    op.create_check_constraint(
        "ck_runs_parent_not_self",
        "runs",
        "parent_run_id IS NULL OR parent_run_id <> run_id",
    )
    op.create_check_constraint(
        "ck_runs_state_schema_version",
        "runs",
        "state_schema_version > 0",
    )
    op.create_check_constraint(
        "ck_runs_lease_epoch",
        "runs",
        "lease_epoch >= 0",
    )
    op.create_check_constraint(
        "ck_runs_checkpoint_pointer",
        "runs",
        "last_checkpoint_id IS NULL OR checkpoint_namespace IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_runs_finished_at",
        "runs",
        "((status IN ('succeeded', 'completed_with_limits', "
        "'needs_clarification', 'failed', 'cancelled') "
        "AND finished_at IS NOT NULL) OR "
        "(status IN ('queued', 'running', 'interrupted') "
        "AND finished_at IS NULL))",
    )
    op.create_index("ix_runs_parent_run_id", "runs", ["parent_run_id"])
    op.create_index(
        "ix_runs_resume_requested",
        "runs",
        ["status", "resume_requested_at"],
        postgresql_where=sa.text("resume_requested_at IS NOT NULL"),
    )

    op.drop_constraint("ck_run_events_type", "run_events", type_="check")
    op.create_check_constraint(
        "ck_run_events_type",
        "run_events",
        "event_type IN ('run.queued', 'run.started', 'retrieval.completed', "
        "'generation.started', 'verification.completed', 'answer.final', "
        "'run.failed', 'run.cancelled', 'run.interrupted', "
        "'run.resume_requested', 'run.resumed', 'attempt.outcome_unknown', "
        "'run.completed_with_limits', 'run.needs_clarification')",
    )

    op.create_table(
        "run_budget_ledgers",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("max_retrieval_rounds", sa.Integer(), nullable=False),
        sa.Column("max_queries_per_round", sa.Integer(), nullable=False),
        sa.Column("max_tool_attempts", sa.Integer(), nullable=False),
        sa.Column("max_model_attempts", sa.Integer(), nullable=False),
        sa.Column("max_embedding_attempts", sa.Integer(), nullable=False),
        sa.Column("max_retry_per_operation", sa.Integer(), nullable=False),
        sa.Column("evidence_top_k", sa.Integer(), nullable=False),
        sa.Column(
            "retrieval_rounds_used",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "queries_used",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "tool_attempts_used",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "model_attempts_used",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "embedding_attempts_used",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "revision",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "max_retrieval_rounds BETWEEN 1 AND 32",
            name="ck_run_budget_ledgers_retrieval_limit",
        ),
        sa.CheckConstraint(
            "max_queries_per_round BETWEEN 1 AND 32",
            name="ck_run_budget_ledgers_query_limit",
        ),
        sa.CheckConstraint(
            "max_tool_attempts BETWEEN 1 AND 1000",
            name="ck_run_budget_ledgers_tool_limit",
        ),
        sa.CheckConstraint(
            "max_model_attempts BETWEEN 1 AND 1000",
            name="ck_run_budget_ledgers_model_limit",
        ),
        sa.CheckConstraint(
            "max_embedding_attempts BETWEEN 0 AND 1000",
            name="ck_run_budget_ledgers_embedding_limit",
        ),
        sa.CheckConstraint(
            "max_retry_per_operation BETWEEN 0 AND 20",
            name="ck_run_budget_ledgers_retry_limit",
        ),
        sa.CheckConstraint(
            "evidence_top_k BETWEEN 1 AND 100",
            name="ck_run_budget_ledgers_evidence_limit",
        ),
        sa.CheckConstraint(
            "retrieval_rounds_used BETWEEN 0 AND max_retrieval_rounds",
            name="ck_run_budget_ledgers_retrieval_used",
        ),
        sa.CheckConstraint(
            "queries_used BETWEEN 0 AND (max_retrieval_rounds * max_queries_per_round)",
            name="ck_run_budget_ledgers_queries_used",
        ),
        sa.CheckConstraint(
            "tool_attempts_used BETWEEN 0 AND max_tool_attempts",
            name="ck_run_budget_ledgers_tool_used",
        ),
        sa.CheckConstraint(
            "model_attempts_used BETWEEN 0 AND max_model_attempts",
            name="ck_run_budget_ledgers_model_used",
        ),
        sa.CheckConstraint(
            "embedding_attempts_used BETWEEN 0 AND max_embedding_attempts",
            name="ck_run_budget_ledgers_embedding_used",
        ),
        sa.CheckConstraint(
            "revision >= 0",
            name="ck_run_budget_ledgers_revision",
        ),
        sa.CheckConstraint(
            "updated_at >= created_at",
            name="ck_run_budget_ledgers_timestamps",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.run_id"],
            name="fk_run_budget_ledgers_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id"),
    )

    op.create_table(
        "run_external_attempts",
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("lease_epoch", sa.BigInteger(), nullable=False),
        sa.Column("operation_key", sa.String(length=255), nullable=False),
        sa.Column("operation_kind", sa.String(length=32), nullable=False),
        sa.Column("operation_name", sa.String(length=64), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("provider_request_id", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'reserved'"),
        ),
        sa.Column("retryable", sa.Boolean(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("result_ref", sa.String(length=255), nullable=True),
        sa.Column("result_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "reserved_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "lease_epoch >= 0",
            name="ck_run_external_attempts_lease_epoch",
        ),
        sa.CheckConstraint(
            "btrim(operation_key) <> '' AND btrim(operation_name) <> ''",
            name="ck_run_external_attempts_operation",
        ),
        sa.CheckConstraint(
            "operation_kind IN ('model', 'embedding', 'tool')",
            name="ck_run_external_attempts_kind",
        ),
        sa.CheckConstraint(
            "attempt_no > 0",
            name="ck_run_external_attempts_attempt_no",
        ),
        sa.CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$'",
            name="ck_run_external_attempts_request_hash",
        ),
        sa.CheckConstraint(
            "result_hash IS NULL OR result_hash ~ '^[0-9a-f]{64}$'",
            name="ck_run_external_attempts_result_hash",
        ),
        sa.CheckConstraint(
            "status IN ('reserved', 'dispatched', 'succeeded', 'failed', "
            "'outcome_unknown', 'abandoned_before_dispatch')",
            name="ck_run_external_attempts_status",
        ),
        sa.CheckConstraint(
            "((result_ref IS NULL AND result_hash IS NULL) OR "
            "(result_ref IS NOT NULL AND result_hash IS NOT NULL))",
            name="ck_run_external_attempts_result_pair",
        ),
        sa.CheckConstraint(
            "(dispatched_at IS NULL OR dispatched_at >= reserved_at) AND "
            "(completed_at IS NULL OR completed_at >= "
            "COALESCE(dispatched_at, reserved_at))",
            name="ck_run_external_attempts_timestamps",
        ),
        sa.CheckConstraint(
            "((status = 'reserved' AND dispatched_at IS NULL "
            "AND completed_at IS NULL AND provider_request_id IS NULL "
            "AND retryable IS NULL AND error_code IS NULL "
            "AND result_ref IS NULL) OR "
            "(status = 'dispatched' AND dispatched_at IS NOT NULL "
            "AND completed_at IS NULL AND retryable IS NULL "
            "AND error_code IS NULL AND result_ref IS NULL) OR "
            "(status = 'succeeded' AND dispatched_at IS NOT NULL "
            "AND completed_at IS NOT NULL AND retryable IS NULL "
            "AND error_code IS NULL AND result_ref IS NOT NULL) OR "
            "(status IN ('failed', 'outcome_unknown') "
            "AND dispatched_at IS NOT NULL AND completed_at IS NOT NULL "
            "AND retryable IS NOT NULL AND error_code IS NOT NULL "
            "AND result_ref IS NULL) OR "
            "(status = 'abandoned_before_dispatch' "
            "AND dispatched_at IS NULL AND completed_at IS NOT NULL "
            "AND provider_request_id IS NULL AND retryable IS NOT NULL "
            "AND error_code IS NOT NULL AND result_ref IS NULL))",
            name="ck_run_external_attempts_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.run_id"],
            name="fk_run_external_attempts_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("attempt_id"),
        sa.UniqueConstraint(
            "run_id",
            "operation_key",
            "attempt_no",
            name="uq_run_external_attempts_operation_attempt",
        ),
    )
    op.create_index(
        "ix_run_external_attempts_recovery",
        "run_external_attempts",
        ["run_id", "status", "reserved_at"],
    )
    op.create_index(
        "ix_run_external_attempts_provider_request",
        "run_external_attempts",
        ["provider_request_id"],
        postgresql_where=sa.text("provider_request_id IS NOT NULL"),
    )

    op.create_table(
        "run_node_artifacts",
        sa.Column("artifact_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("node_name", sa.String(length=64), nullable=False),
        sa.Column("artifact_kind", sa.String(length=32), nullable=False),
        sa.Column("artifact_ref", sa.String(length=255), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("lease_epoch", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "btrim(node_name) <> '' AND btrim(artifact_ref) <> ''",
            name="ck_run_node_artifacts_identity",
        ),
        sa.CheckConstraint(
            "artifact_kind IN ('retrieval', 'verification', 'terminal')",
            name="ck_run_node_artifacts_kind",
        ),
        sa.CheckConstraint(
            "payload_hash ~ '^[0-9a-f]{64}$'",
            name="ck_run_node_artifacts_payload_hash",
        ),
        sa.CheckConstraint(
            "payload IS NULL OR jsonb_typeof(payload) = 'object'",
            name="ck_run_node_artifacts_payload",
        ),
        sa.CheckConstraint(
            "lease_epoch >= 0",
            name="ck_run_node_artifacts_lease_epoch",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.run_id"],
            name="fk_run_node_artifacts_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("artifact_id"),
        sa.UniqueConstraint(
            "run_id",
            "node_name",
            "artifact_kind",
            "artifact_ref",
            name="uq_run_node_artifacts_identity",
        ),
    )
    op.create_index(
        "ix_run_node_artifacts_node_created",
        "run_node_artifacts",
        ["run_id", "node_name", "created_at"],
    )

    op.create_table(
        "run_checkpoints",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("checkpoint_namespace", sa.String(length=255), nullable=False),
        sa.Column("checkpoint_id", sa.String(length=255), nullable=False),
        sa.Column("parent_checkpoint_id", sa.String(length=255), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("graph_version", sa.String(length=64), nullable=False),
        sa.Column("retrieval_config_hash", sa.String(length=64), nullable=False),
        sa.Column("last_completed_node", sa.String(length=64), nullable=True),
        sa.Column(
            "state_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("lease_epoch", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "btrim(checkpoint_namespace) <> '' AND btrim(checkpoint_id) <> ''",
            name="ck_run_checkpoints_identity",
        ),
        sa.CheckConstraint(
            "parent_checkpoint_id IS NULL OR parent_checkpoint_id <> checkpoint_id",
            name="ck_run_checkpoints_parent_not_self",
        ),
        sa.CheckConstraint(
            "schema_version > 0",
            name="ck_run_checkpoints_schema_version",
        ),
        sa.CheckConstraint(
            "lease_epoch >= 0",
            name="ck_run_checkpoints_lease_epoch",
        ),
        sa.CheckConstraint(
            "retrieval_config_hash ~ '^[0-9a-f]{64}$'",
            name="ck_run_checkpoints_config_hash",
        ),
        sa.CheckConstraint(
            "state_hash ~ '^[0-9a-f]{64}$'",
            name="ck_run_checkpoints_state_hash",
        ),
        sa.CheckConstraint(
            _M5_CHECKPOINT_STATE_CONSTRAINT,
            name="ck_run_checkpoints_state_payload",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.run_id"],
            name="fk_run_checkpoints_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "checkpoint_namespace", "parent_checkpoint_id"],
            [
                "run_checkpoints.run_id",
                "run_checkpoints.checkpoint_namespace",
                "run_checkpoints.checkpoint_id",
            ],
            name="fk_run_checkpoints_parent",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint(
            "run_id",
            "checkpoint_namespace",
            "checkpoint_id",
        ),
    )
    op.create_index(
        "ix_run_checkpoints_run_created",
        "run_checkpoints",
        ["run_id", "created_at"],
    )
    op.create_index(
        "ix_run_checkpoints_lease",
        "run_checkpoints",
        ["run_id", "checkpoint_namespace", "lease_epoch"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    incompatible_status = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM runs "
            "WHERE status IN ('completed_with_limits', 'needs_clarification'))"
        )
    ).scalar_one()
    incompatible_event = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM run_events "
            "WHERE event_type IN ('run.resume_requested', 'run.resumed', "
            "'attempt.outcome_unknown', 'run.completed_with_limits', "
            "'run.needs_clarification'))"
        )
    ).scalar_one()
    if incompatible_status or incompatible_event:
        raise RuntimeError(
            "cannot downgrade M5 while runs or events use M5-only states; "
            "archive or migrate those records first"
        )

    op.drop_index("ix_run_checkpoints_lease", table_name="run_checkpoints")
    op.drop_index("ix_run_checkpoints_run_created", table_name="run_checkpoints")
    op.drop_table("run_checkpoints")
    op.drop_index(
        "ix_run_node_artifacts_node_created",
        table_name="run_node_artifacts",
    )
    op.drop_table("run_node_artifacts")
    op.drop_index(
        "ix_run_external_attempts_provider_request",
        table_name="run_external_attempts",
    )
    op.drop_index(
        "ix_run_external_attempts_recovery",
        table_name="run_external_attempts",
    )
    op.drop_table("run_external_attempts")
    op.drop_table("run_budget_ledgers")

    op.drop_constraint("ck_run_events_type", "run_events", type_="check")
    op.create_check_constraint(
        "ck_run_events_type",
        "run_events",
        "event_type IN ('run.queued', 'run.started', 'retrieval.completed', "
        "'generation.started', 'verification.completed', 'answer.final', "
        "'run.failed', 'run.cancelled', 'run.interrupted')",
    )

    op.drop_index("ix_runs_resume_requested", table_name="runs")
    op.drop_index("ix_runs_parent_run_id", table_name="runs")
    op.drop_constraint("ck_runs_finished_at", "runs", type_="check")
    op.drop_constraint("ck_runs_status", "runs", type_="check")
    op.drop_constraint("ck_runs_checkpoint_pointer", "runs", type_="check")
    op.drop_constraint("ck_runs_lease_epoch", "runs", type_="check")
    op.drop_constraint("ck_runs_state_schema_version", "runs", type_="check")
    op.drop_constraint("ck_runs_parent_not_self", "runs", type_="check")
    op.drop_constraint("fk_runs_parent_run", "runs", type_="foreignkey")

    op.drop_column("runs", "resume_requested_at")
    op.drop_column("runs", "lease_epoch")
    op.drop_column("runs", "stop_reason")
    op.drop_column("runs", "execution_deadline_at")
    op.drop_column("runs", "last_completed_node")
    op.drop_column("runs", "last_checkpoint_id")
    op.drop_column("runs", "checkpoint_namespace")
    op.drop_column("runs", "state_schema_version")
    op.drop_column("runs", "parent_run_id")

    op.create_check_constraint(
        "ck_runs_status",
        "runs",
        "status IN ('queued', 'running', 'interrupted', 'succeeded', "
        "'failed', 'cancelled')",
    )
    op.create_check_constraint(
        "ck_runs_finished_at",
        "runs",
        "((status IN ('succeeded', 'failed', 'cancelled') "
        "AND finished_at IS NOT NULL) OR "
        "(status IN ('queued', 'running', 'interrupted') "
        "AND finished_at IS NULL))",
    )
