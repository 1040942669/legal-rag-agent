"""Add durable M4 API sessions, runs, idempotency, and event history.

Revision ID: 0005_m4_api_sessions
Revises: 0004_m3_ann_guards
Create Date: 2026-09-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0005_m4_api_sessions"
down_revision: str | None = "0004_m3_ann_guards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'active'"),
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
            "status IN ('active', 'archived')",
            name="ck_sessions_status",
        ),
        sa.PrimaryKeyConstraint("session_id"),
        sa.UniqueConstraint(
            "session_id",
            "user_id",
            name="uq_sessions_identity_owner",
        ),
    )
    op.create_index(
        "ix_sessions_user_created",
        "sessions",
        ["user_id", "created_at"],
    )

    op.create_table(
        "runs",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'queued'"),
        ),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "request_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("scope_id", sa.String(length=255), nullable=False),
        sa.Column("snapshot_id", sa.String(length=255), nullable=False),
        sa.Column("snapshot_revision", sa.BigInteger(), nullable=False),
        sa.Column("activation_id", sa.String(length=64), nullable=False),
        sa.Column("profile_id", sa.String(length=64), nullable=False),
        sa.Column("boundary_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("retrieval_config_hash", sa.String(length=64), nullable=False),
        sa.Column("graph_version", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "queued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "revision",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "event_sequence",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'interrupted', 'succeeded', 'failed', 'cancelled')",
            name="ck_runs_status",
        ),
        sa.CheckConstraint("revision >= 0", name="ck_runs_revision"),
        sa.CheckConstraint(
            "event_sequence >= 0",
            name="ck_runs_event_sequence",
        ),
        sa.CheckConstraint(
            "((status IN ('succeeded', 'failed', 'cancelled') "
            "AND finished_at IS NOT NULL) OR "
            "(status IN ('queued', 'running', 'interrupted') "
            "AND finished_at IS NULL))",
            name="ck_runs_finished_at",
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "user_id"],
            ["sessions.session_id", "sessions.user_id"],
            name="fk_runs_session_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["scope_id", "snapshot_revision", "activation_id", "snapshot_id"],
            [
                "snapshot_activation_events.scope_id",
                "snapshot_activation_events.revision",
                "snapshot_activation_events.activation_id",
                "snapshot_activation_events.target_snapshot_id",
            ],
            name="fk_runs_snapshot_activation",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["embedding_profiles.profile_id"],
            name="fk_runs_embedding_profile",
        ),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint(
            "run_id",
            "session_id",
            "user_id",
            name="uq_runs_identity_session_owner",
        ),
    )
    op.create_index(
        "ix_runs_user_created",
        "runs",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_runs_status_queued",
        "runs",
        ["status", "queued_at"],
    )
    op.create_index(
        "uq_runs_one_active_per_session",
        "runs",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('queued', 'running', 'interrupted')"
        ),
    )

    op.create_table(
        "messages",
        sa.Column("message_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("ordinal", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant')",
            name="ck_messages_role",
        ),
        sa.CheckConstraint("ordinal > 0", name="ck_messages_ordinal"),
        sa.ForeignKeyConstraint(
            ["session_id", "user_id"],
            ["sessions.session_id", "sessions.user_id"],
            name="fk_messages_session_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "session_id", "user_id"],
            ["runs.run_id", "runs.session_id", "runs.user_id"],
            name="fk_messages_run_session_owner",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("message_id"),
        sa.UniqueConstraint(
            "session_id",
            "ordinal",
            name="uq_messages_session_ordinal",
        ),
        sa.UniqueConstraint("run_id", "role", name="uq_messages_run_role"),
        sa.UniqueConstraint(
            "message_id",
            "run_id",
            "role",
            name="uq_messages_identity_run_role",
        ),
    )
    op.create_index(
        "ix_messages_session_order",
        "messages",
        ["session_id", "ordinal"],
    )

    op.create_table(
        "run_results",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("final_message_id", sa.String(length=36), nullable=False),
        sa.Column(
            "final_message_role",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'assistant'"),
        ),
        sa.Column(
            "answer_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "evidence_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "verification_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "final_message_role = 'assistant'",
            name="ck_run_results_final_message_role",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.run_id"],
            name="fk_run_results_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["final_message_id", "run_id", "final_message_role"],
            ["messages.message_id", "messages.run_id", "messages.role"],
            name="fk_run_results_final_message",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint("final_message_id"),
    )

    op.create_table(
        "idempotency_keys",
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id", "user_id"],
            ["sessions.session_id", "sessions.user_id"],
            name="fk_idempotency_keys_session_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "session_id", "user_id"],
            ["runs.run_id", "runs.session_id", "runs.user_id"],
            name="fk_idempotency_keys_run_session_owner",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "user_id",
            "session_id",
            "idempotency_key",
        ),
    )
    op.create_index(
        "ix_idempotency_keys_expires",
        "idempotency_keys",
        ["expires_at"],
    )

    op.create_table(
        "run_events",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column(
            "safe_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("sequence > 0", name="ck_run_events_sequence"),
        sa.CheckConstraint(
            "event_type IN ('run.queued', 'run.started', "
            "'retrieval.completed', 'generation.started', "
            "'verification.completed', 'answer.final', 'run.failed', "
            "'run.cancelled', 'run.interrupted')",
            name="ck_run_events_type",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.run_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "sequence"),
    )
    op.create_index(
        "ix_run_events_created",
        "run_events",
        ["run_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_run_events_created", table_name="run_events")
    op.drop_table("run_events")
    op.drop_index("ix_idempotency_keys_expires", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")
    op.drop_table("run_results")
    op.drop_index("ix_messages_session_order", table_name="messages")
    op.drop_table("messages")
    op.drop_index("uq_runs_one_active_per_session", table_name="runs")
    op.drop_index("ix_runs_status_queued", table_name="runs")
    op.drop_index("ix_runs_user_created", table_name="runs")
    op.drop_table("runs")
    op.drop_index("ix_sessions_user_created", table_name="sessions")
    op.drop_table("sessions")
