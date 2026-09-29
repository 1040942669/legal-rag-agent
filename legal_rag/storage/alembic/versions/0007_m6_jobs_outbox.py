"""Add durable M6 batch jobs, item checkpoints, and a transactional outbox.

Revision ID: 0007_m6_jobs_outbox
Revises: 0006_m5_harness_recovery
Create Date: 2026-09-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0007_m6_jobs_outbox"
down_revision: str | None = "0006_m5_harness_recovery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("job_id", sa.String(length=36), primary_key=True),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("request_ref", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("completed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_epoch", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("claim_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("recovery_queued_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("btrim(owner_id) <> ''", name="ck_jobs_owner"),
        sa.CheckConstraint("kind IN ('evaluation', 'ingestion')", name="ck_jobs_kind"),
        sa.CheckConstraint(
            "request_ref ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'",
            name="ck_jobs_request_ref",
        ),
        sa.CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$'", name="ck_jobs_request_hash"
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_jobs_status",
        ),
        sa.CheckConstraint("stage ~ '^[a-z][a-z0-9_]{0,63}$'", name="ck_jobs_stage"),
        sa.CheckConstraint(
            "total >= 0 AND completed >= 0 AND failed >= 0 "
            "AND completed + failed <= total",
            name="ck_jobs_progress",
        ),
        sa.CheckConstraint("lease_epoch >= 0", name="ck_jobs_lease_epoch"),
        sa.CheckConstraint("claim_count >= 0", name="ck_jobs_claim_count"),
        sa.CheckConstraint(
            "(status = 'running' AND lease_owner IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'running' AND lease_owner IS NULL "
            "AND lease_expires_at IS NULL)",
            name="ck_jobs_lease_pair",
        ),
        sa.CheckConstraint(
            "(status IN ('queued', 'running') AND finished_at IS NULL) OR "
            "(status IN ('succeeded', 'failed', 'cancelled') AND finished_at IS NOT NULL)",
            name="ck_jobs_finished_at",
        ),
        sa.UniqueConstraint(
            "owner_id", "kind", "idempotency_key", name="uq_jobs_owner_kind_idempotency"
        ),
    )
    op.create_index("ix_jobs_owner_created", "jobs", ["owner_id", "created_at"])
    op.create_index("ix_jobs_recovery", "jobs", ["status", "lease_expires_at"])

    op.create_table(
        "job_outbox",
        sa.Column("outbox_id", sa.String(length=36), primary_key=True),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("claim_owner", sa.String(length=128), nullable=True),
        sa.Column("claim_epoch", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "delivery_attempts", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.job_id"], ondelete="CASCADE"),
        sa.CheckConstraint("schema_version = 1", name="ck_job_outbox_schema_version"),
        sa.CheckConstraint(
            "status IN ('pending', 'delivered')", name="ck_job_outbox_status"
        ),
        sa.CheckConstraint(
            "claim_epoch >= 0 AND delivery_attempts >= 0", name="ck_job_outbox_counters"
        ),
        sa.CheckConstraint(
            "(claim_owner IS NULL) = (claim_expires_at IS NULL)",
            name="ck_job_outbox_claim_pair",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND delivered_at IS NULL) OR "
            "(status = 'delivered' AND delivered_at IS NOT NULL)",
            name="ck_job_outbox_delivered_at",
        ),
    )
    op.create_index(
        "ix_job_outbox_due", "job_outbox", ["status", "next_attempt_at", "created_at"]
    )
    op.create_index("ix_job_outbox_job_created", "job_outbox", ["job_id", "created_at"])
    op.create_index(
        "uq_job_outbox_pending_per_job",
        "job_outbox",
        ["job_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "job_items",
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("item_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("lease_epoch", sa.BigInteger(), nullable=False),
        sa.Column("result_ref", sa.String(length=255), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
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
        sa.ForeignKeyConstraint(["job_id"], ["jobs.job_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("job_id", "item_key"),
        sa.CheckConstraint("item_key ~ '^[0-9a-f]{64}$'", name="ck_job_items_key"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')",
            name="ck_job_items_status",
        ),
        sa.CheckConstraint(
            "max_attempts BETWEEN 1 AND 10 AND attempt_count BETWEEN 1 AND max_attempts",
            name="ck_job_items_attempts",
        ),
        sa.CheckConstraint("lease_epoch > 0", name="ck_job_items_lease_epoch"),
        sa.CheckConstraint(
            "(status = 'succeeded') = (result_ref IS NOT NULL)",
            name="ck_job_items_result_ref",
        ),
    )
    op.create_index("ix_job_items_status", "job_items", ["job_id", "status"])


def downgrade() -> None:
    has_jobs = (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS (SELECT 1 FROM jobs)"))
        .scalar_one()
    )
    if has_jobs:
        raise RuntimeError(
            "cannot downgrade M6 while jobs exist; archive or migrate job records first"
        )

    op.drop_index("ix_job_items_status", table_name="job_items")
    op.drop_table("job_items")
    op.drop_index("uq_job_outbox_pending_per_job", table_name="job_outbox")
    op.drop_index("ix_job_outbox_job_created", table_name="job_outbox")
    op.drop_index("ix_job_outbox_due", table_name="job_outbox")
    op.drop_table("job_outbox")
    op.drop_index("ix_jobs_recovery", table_name="jobs")
    op.drop_index("ix_jobs_owner_created", table_name="jobs")
    op.drop_table("jobs")
