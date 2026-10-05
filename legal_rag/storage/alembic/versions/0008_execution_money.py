"""Add frozen service policy and per-attempt monetary reservations.

Revision ID: 0008_execution_money
Revises: 0007_m6_jobs_outbox
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0008_execution_money"
down_revision = "0007_m6_jobs_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_execution_policies",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.run_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("policy", postgresql.JSONB(), nullable=False),
        sa.Column("policy_hash", sa.String(64), nullable=False),
        sa.CheckConstraint("jsonb_typeof(policy) = 'object'", name="ck_execution_policy_object"),
        sa.CheckConstraint("policy_hash ~ '^[0-9a-f]{64}$'", name="ck_execution_policy_hash"),
    )
    op.create_table(
        "run_monetary_attempts",
        sa.Column("attempt_id", sa.String(36), sa.ForeignKey("run_external_attempts.attempt_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("policy_hash", sa.String(64), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("reserved_cost", sa.Numeric(24, 12), nullable=False),
        sa.Column("known_cost", sa.Numeric(24, 12)),
        sa.Column("input_tokens", sa.BigInteger()),
        sa.Column("output_tokens", sa.BigInteger()),
        sa.Column("cost_status", sa.String(32), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.CheckConstraint("reserved_cost > 0", name="ck_monetary_reserved_positive"),
        sa.CheckConstraint("known_cost IS NULL OR known_cost >= 0", name="ck_monetary_known_nonnegative"),
        sa.CheckConstraint("currency = 'CNY'", name="ck_monetary_currency"),
        sa.CheckConstraint("cost_status IN ('reserved', 'known', 'unknown', 'overrun')", name="ck_monetary_status"),
        sa.CheckConstraint("input_tokens IS NULL OR input_tokens >= 0", name="ck_monetary_inputs"),
        sa.CheckConstraint("output_tokens IS NULL OR output_tokens >= 0", name="ck_monetary_outputs"),
        sa.CheckConstraint("(cost_status IN ('known', 'overrun')) = (known_cost IS NOT NULL)", name="ck_monetary_known_status"),
    )
    op.create_index("ix_monetary_run", "run_monetary_attempts", ["run_id"])


def downgrade() -> None:
    # Dropping valued monetary journals is intentionally refused.
    from sqlalchemy import text
    connection = op.get_bind()
    if connection.scalar(text("SELECT count(*) FROM run_monetary_attempts")) or connection.scalar(text("SELECT count(*) FROM run_execution_policies")):
        raise RuntimeError("valued monetary journal requires a forward repair")
    op.drop_table("run_monetary_attempts")
    op.drop_table("run_execution_policies")
