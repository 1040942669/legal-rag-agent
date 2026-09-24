"""Add an immutable, revisioned snapshot activation ledger.

Revision ID: 0003_m3_activation
Revises: 0002_m3_immutable_rows
Create Date: 2026-09-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0003_m3_activation"
down_revision: str | None = "0002_m3_immutable_rows"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


LEGACY_ACTIVATION_ASSERTIONS_SQL = """
DO $$
DECLARE
    inconsistent_scope text;
BEGIN
    SELECT pointer.scope_id INTO inconsistent_scope
    FROM active_snapshot_pointers AS pointer
    JOIN corpus_snapshots AS snapshot
      ON snapshot.scope_id = pointer.scope_id
     AND snapshot.snapshot_id = pointer.snapshot_id
    WHERE snapshot.status <> 'active'
       OR snapshot.activated_at IS NULL
       OR snapshot.activated_at IS DISTINCT FROM pointer.updated_at
    LIMIT 1;

    IF inconsistent_scope IS NOT NULL THEN
        RAISE EXCEPTION
            'cannot migrate inconsistent active snapshot pointer for scope %',
            inconsistent_scope
            USING ERRCODE = '55000';
    END IF;

    SELECT snapshot.scope_id INTO inconsistent_scope
    FROM corpus_snapshots AS snapshot
    WHERE snapshot.status = 'active'
      AND NOT EXISTS (
          SELECT 1
          FROM active_snapshot_pointers AS pointer
          WHERE pointer.scope_id = snapshot.scope_id
            AND pointer.snapshot_id = snapshot.snapshot_id
      )
    LIMIT 1;

    IF inconsistent_scope IS NOT NULL THEN
        RAISE EXCEPTION
            'cannot migrate active snapshot without pointer for scope %',
            inconsistent_scope
            USING ERRCODE = '55000';
    END IF;
END;
$$;
"""


DOWNGRADE_ACTIVATION_HISTORY_ASSERTIONS_SQL = """
DO $$
DECLARE
    unsafe_scope text;
    unsafe_revision bigint;
    unsafe_operation text;
BEGIN
    SELECT event.scope_id, event.revision, event.operation
    INTO unsafe_scope, unsafe_revision, unsafe_operation
    FROM snapshot_activation_events AS event
    WHERE event.revision <> 1
       OR event.operation NOT IN ('migration_bootstrap', 'initial_activate')
    ORDER BY event.scope_id, event.revision
    LIMIT 1;

    IF unsafe_scope IS NOT NULL THEN
        RAISE EXCEPTION
            'cannot downgrade activation history for scope %, revision %, operation %; back up the activation ledger and perform an approved manual migration',
            unsafe_scope, unsafe_revision, unsafe_operation
            USING ERRCODE = '55000';
    END IF;
END;
$$;
"""


CORPUS_SNAPSHOT_GUARD_SQL = """
CREATE OR REPLACE FUNCTION legal_rag_guard_corpus_snapshot_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'building'
            OR NEW.validated_at IS NOT NULL
            OR NEW.activated_at IS NOT NULL
        THEN
            RAISE EXCEPTION 'corpus snapshots must begin in building state'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'corpus snapshot rows cannot be deleted'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.snapshot_id IS DISTINCT FROM OLD.snapshot_id
        OR NEW.scope_id IS DISTINCT FROM OLD.scope_id
        OR NEW.source_manifest IS DISTINCT FROM OLD.source_manifest
        OR NEW.source_manifest_hash IS DISTINCT FROM OLD.source_manifest_hash
        OR NEW.corpus_hash IS DISTINCT FROM OLD.corpus_hash
        OR NEW.built_at IS DISTINCT FROM OLD.built_at
    THEN
        RAISE EXCEPTION 'immutable corpus snapshot identity cannot change'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.status = OLD.status THEN
        IF NEW.validated_at IS DISTINCT FROM OLD.validated_at
            OR NEW.activated_at IS DISTINCT FROM OLD.activated_at
        THEN
            RAISE EXCEPTION 'snapshot timestamps require a status transition'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF NOT (
        (OLD.status = 'building' AND NEW.status IN ('validated', 'failed'))
        OR (OLD.status = 'validated' AND NEW.status IN ('active', 'archived'))
        OR (OLD.status = 'active' AND NEW.status = 'validated')
    ) THEN
        RAISE EXCEPTION 'invalid corpus snapshot status transition: % -> %',
            OLD.status, NEW.status
            USING ERRCODE = '55000';
    END IF;

    IF NEW.validated_at IS DISTINCT FROM OLD.validated_at
        AND NOT (
            OLD.status = 'building'
            AND NEW.status = 'validated'
            AND OLD.validated_at IS NULL
            AND NEW.validated_at IS NOT NULL
        )
    THEN
        RAISE EXCEPTION 'validated_at may change only while validating a snapshot'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.status = 'building'
        AND NEW.status = 'validated'
        AND NEW.validated_at IS NULL
    THEN
        RAISE EXCEPTION 'validated snapshots require validated_at'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.activated_at IS DISTINCT FROM OLD.activated_at
        AND NOT (NEW.status = 'active' AND NEW.activated_at IS NOT NULL)
    THEN
        RAISE EXCEPTION 'activated_at may change only while activating a snapshot'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.status = 'active' AND NEW.activated_at IS NULL THEN
        RAISE EXCEPTION 'active snapshots require activated_at'
            USING ERRCODE = '55000';
    END IF;

    RETURN NEW;
END;
$$;
"""


LEGACY_CORPUS_SNAPSHOT_GUARD_SQL = """
CREATE OR REPLACE FUNCTION legal_rag_guard_corpus_snapshot_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'building'
            OR NEW.validated_at IS NOT NULL
            OR NEW.activated_at IS NOT NULL
        THEN
            RAISE EXCEPTION 'corpus snapshots must begin in building state'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'corpus snapshot rows cannot be deleted'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.snapshot_id IS DISTINCT FROM OLD.snapshot_id
        OR NEW.scope_id IS DISTINCT FROM OLD.scope_id
        OR NEW.source_manifest IS DISTINCT FROM OLD.source_manifest
        OR NEW.source_manifest_hash IS DISTINCT FROM OLD.source_manifest_hash
        OR NEW.corpus_hash IS DISTINCT FROM OLD.corpus_hash
        OR NEW.built_at IS DISTINCT FROM OLD.built_at
    THEN
        RAISE EXCEPTION 'immutable corpus snapshot identity cannot change'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.status = OLD.status THEN
        IF NEW.validated_at IS DISTINCT FROM OLD.validated_at
            OR NEW.activated_at IS DISTINCT FROM OLD.activated_at
        THEN
            RAISE EXCEPTION 'snapshot timestamps require a status transition'
                USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;

    IF NOT (
        (OLD.status = 'building' AND NEW.status IN ('validated', 'failed'))
        OR (OLD.status = 'validated' AND NEW.status IN ('active', 'archived'))
        OR (OLD.status = 'active' AND NEW.status = 'archived')
        OR (OLD.status = 'archived' AND NEW.status = 'active')
    ) THEN
        RAISE EXCEPTION 'invalid corpus snapshot status transition: % -> %',
            OLD.status, NEW.status
            USING ERRCODE = '55000';
    END IF;

    IF NEW.validated_at IS DISTINCT FROM OLD.validated_at
        AND NOT (
            OLD.status = 'building'
            AND NEW.status = 'validated'
            AND OLD.validated_at IS NULL
            AND NEW.validated_at IS NOT NULL
        )
    THEN
        RAISE EXCEPTION 'validated_at may change only while validating a snapshot'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.status = 'building'
        AND NEW.status = 'validated'
        AND NEW.validated_at IS NULL
    THEN
        RAISE EXCEPTION 'validated snapshots require validated_at'
            USING ERRCODE = '55000';
    END IF;

    IF NEW.activated_at IS DISTINCT FROM OLD.activated_at
        AND NOT (NEW.status = 'active' AND NEW.activated_at IS NOT NULL)
    THEN
        RAISE EXCEPTION 'activated_at may change only while activating a snapshot'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.status = 'active' AND NEW.activated_at IS NULL THEN
        RAISE EXCEPTION 'active snapshots require activated_at'
            USING ERRCODE = '55000';
    END IF;

    RETURN NEW;
END;
$$;
"""


ACTIVATION_CONSISTENCY_SQL = """
CREATE FUNCTION legal_rag_enforce_snapshot_activation_consistency()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    inconsistent_scope text;
BEGIN
    SELECT snapshot.scope_id INTO inconsistent_scope
    FROM corpus_snapshots AS snapshot
    WHERE snapshot.status = 'active'
      AND NOT EXISTS (
          SELECT 1
          FROM active_snapshot_pointers AS pointer
          WHERE pointer.scope_id = snapshot.scope_id
            AND pointer.snapshot_id = snapshot.snapshot_id
      )
    LIMIT 1;

    IF inconsistent_scope IS NOT NULL THEN
        RAISE EXCEPTION
            'active snapshot is not selected by its scope pointer: %',
            inconsistent_scope
            USING ERRCODE = '55000';
    END IF;

    SELECT pointer.scope_id INTO inconsistent_scope
    FROM active_snapshot_pointers AS pointer
    LEFT JOIN corpus_snapshots AS snapshot
      ON snapshot.scope_id = pointer.scope_id
     AND snapshot.snapshot_id = pointer.snapshot_id
    LEFT JOIN snapshot_activation_events AS event
      ON event.scope_id = pointer.scope_id
     AND event.revision = pointer.revision
     AND event.activation_id = pointer.activation_id
     AND event.target_snapshot_id = pointer.snapshot_id
    WHERE snapshot.snapshot_id IS NULL
       OR snapshot.status <> 'active'
       OR event.activation_id IS NULL
       OR snapshot.activated_at IS DISTINCT FROM event.occurred_at
       OR pointer.updated_at IS DISTINCT FROM event.occurred_at
       OR pointer.revision IS DISTINCT FROM (
           SELECT max(candidate.revision)
           FROM snapshot_activation_events AS candidate
           WHERE candidate.scope_id = pointer.scope_id
       )
    LIMIT 1;

    IF inconsistent_scope IS NOT NULL THEN
        RAISE EXCEPTION
            'active pointer, snapshot status, and latest activation event disagree for scope %',
            inconsistent_scope
            USING ERRCODE = '55000';
    END IF;

    SELECT event.scope_id INTO inconsistent_scope
    FROM snapshot_activation_events AS event
    WHERE NOT EXISTS (
        SELECT 1
        FROM active_snapshot_pointers AS pointer
        WHERE pointer.scope_id = event.scope_id
    )
    LIMIT 1;

    IF inconsistent_scope IS NOT NULL THEN
        RAISE EXCEPTION
            'activation history exists without an active pointer for scope %',
            inconsistent_scope
            USING ERRCODE = '55000';
    END IF;

    SELECT event.scope_id INTO inconsistent_scope
    FROM snapshot_activation_events AS event
    WHERE event.revision > 1
      AND NOT EXISTS (
          SELECT 1
          FROM snapshot_activation_events AS predecessor
          WHERE predecessor.scope_id = event.scope_id
            AND predecessor.activation_id = event.previous_activation_id
            AND predecessor.revision = event.revision - 1
            AND predecessor.target_snapshot_id = event.previous_snapshot_id
      )
    LIMIT 1;

    IF inconsistent_scope IS NOT NULL THEN
        RAISE EXCEPTION
            'activation event predecessor chain is inconsistent for scope %',
            inconsistent_scope
            USING ERRCODE = '55000';
    END IF;

    RETURN NULL;
END;
$$;
"""


def upgrade() -> None:
    # Keep the legacy assertion and bootstrap copy in one quiescent view.  The
    # lock is held until Alembic commits the transactional migration, so a
    # legacy writer cannot change a snapshot or pointer between validation and
    # installation of the new revisioned consistency constraints.
    op.execute(
        "LOCK TABLE active_snapshot_pointers, corpus_snapshots "
        "IN SHARE ROW EXCLUSIVE MODE"
    )
    op.execute(LEGACY_ACTIVATION_ASSERTIONS_SQL)
    op.create_index(
        "ix_chunk_articles_article_chunk",
        "chunk_articles",
        ["article_id", "chunk_id"],
    )

    op.add_column(
        "active_snapshot_pointers",
        sa.Column("revision", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "active_snapshot_pointers",
        sa.Column("activation_id", sa.String(length=64), nullable=True),
    )
    op.create_table(
        "snapshot_activation_events",
        sa.Column("activation_id", sa.String(length=64), primary_key=True),
        sa.Column("scope_id", sa.String(length=255), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("previous_snapshot_id", sa.String(length=255), nullable=True),
        sa.Column("target_snapshot_id", sa.String(length=255), nullable=False),
        sa.Column("previous_activation_id", sa.String(length=64), nullable=True),
        sa.Column("actor", sa.String(length=128), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "revision > 0",
            name="ck_snapshot_activation_events_revision_positive",
        ),
        sa.CheckConstraint(
            "operation IN "
            "('initial_activate', 'replace', 'rollback', 'migration_bootstrap')",
            name="ck_snapshot_activation_events_operation",
        ),
        sa.CheckConstraint(
            "((revision = 1 "
            "AND previous_snapshot_id IS NULL "
            "AND previous_activation_id IS NULL) "
            "OR (revision > 1 "
            "AND previous_snapshot_id IS NOT NULL "
            "AND previous_activation_id IS NOT NULL))",
            name="ck_snapshot_activation_events_predecessor",
        ),
        sa.CheckConstraint(
            "((operation IN ('initial_activate', 'migration_bootstrap') "
            "AND revision = 1) "
            "OR (operation IN ('replace', 'rollback') AND revision > 1))",
            name="ck_snapshot_activation_events_operation_revision",
        ),
        sa.CheckConstraint(
            "previous_snapshot_id IS NULL "
            "OR target_snapshot_id <> previous_snapshot_id",
            name="ck_snapshot_activation_events_target_changes",
        ),
        sa.ForeignKeyConstraint(
            ["scope_id", "previous_snapshot_id"],
            ["corpus_snapshots.scope_id", "corpus_snapshots.snapshot_id"],
            name="fk_snapshot_activation_events_previous_snapshot",
        ),
        sa.ForeignKeyConstraint(
            ["scope_id", "target_snapshot_id"],
            ["corpus_snapshots.scope_id", "corpus_snapshots.snapshot_id"],
            name="fk_snapshot_activation_events_target_snapshot",
        ),
        sa.ForeignKeyConstraint(
            ["scope_id", "previous_activation_id"],
            [
                "snapshot_activation_events.scope_id",
                "snapshot_activation_events.activation_id",
            ],
            name="fk_snapshot_activation_events_previous_event",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.UniqueConstraint(
            "scope_id",
            "revision",
            name="uq_snapshot_activation_events_scope_revision",
        ),
        sa.UniqueConstraint(
            "scope_id",
            "activation_id",
            name="uq_snapshot_activation_events_scope_activation",
        ),
        sa.UniqueConstraint(
            "scope_id",
            "revision",
            "activation_id",
            "target_snapshot_id",
            name="uq_snapshot_activation_events_pointer_target",
        ),
    )

    op.execute(
        """
        INSERT INTO snapshot_activation_events (
            activation_id,
            scope_id,
            revision,
            operation,
            previous_snapshot_id,
            target_snapshot_id,
            previous_activation_id,
            actor,
            reason,
            occurred_at
        )
        SELECT
            md5(
                jsonb_build_array(
                    'm3-bootstrap-a',
                    pointer.scope_id,
                    pointer.snapshot_id,
                    pointer.updated_at
                )::text
            ) || md5(
                jsonb_build_array(
                    'm3-bootstrap-b',
                    pointer.scope_id,
                    pointer.snapshot_id,
                    pointer.updated_at
                )::text
            ),
            pointer.scope_id,
            1,
            'migration_bootstrap',
            NULL,
            pointer.snapshot_id,
            NULL,
            NULL,
            'Bootstrapped from the pre-0003 active snapshot pointer.',
            pointer.updated_at
        FROM active_snapshot_pointers AS pointer
        """
    )
    op.execute(
        """
        UPDATE active_snapshot_pointers AS pointer
        SET revision = event.revision,
            activation_id = event.activation_id
        FROM snapshot_activation_events AS event
        WHERE event.scope_id = pointer.scope_id
          AND event.target_snapshot_id = pointer.snapshot_id
          AND event.operation = 'migration_bootstrap'
        """
    )
    op.alter_column("active_snapshot_pointers", "revision", nullable=False)
    op.alter_column("active_snapshot_pointers", "activation_id", nullable=False)
    op.create_check_constraint(
        "ck_active_snapshot_pointers_revision_positive",
        "active_snapshot_pointers",
        "revision > 0",
    )
    op.create_foreign_key(
        "fk_active_snapshot_pointer_activation_event",
        "active_snapshot_pointers",
        "snapshot_activation_events",
        ["scope_id", "revision", "activation_id", "snapshot_id"],
        ["scope_id", "revision", "activation_id", "target_snapshot_id"],
        deferrable=True,
        initially="DEFERRED",
    )

    op.execute(
        """
        CREATE TRIGGER trg_snapshot_activation_events_immutable
        BEFORE UPDATE OR DELETE ON snapshot_activation_events
        FOR EACH ROW
        EXECUTE FUNCTION legal_rag_reject_immutable_row_change()
        """
    )
    op.execute(CORPUS_SNAPSHOT_GUARD_SQL)
    op.execute(ACTIVATION_CONSISTENCY_SQL)
    for table_name in (
        "corpus_snapshots",
        "active_snapshot_pointers",
        "snapshot_activation_events",
    ):
        op.execute(
            f"""
            CREATE CONSTRAINT TRIGGER trg_{table_name}_activation_consistency
            AFTER INSERT OR UPDATE OR DELETE ON {table_name}
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            EXECUTE FUNCTION legal_rag_enforce_snapshot_activation_consistency()
            """
        )


def downgrade() -> None:
    # Revisioned replace/rollback history cannot be represented by the 0002
    # schema.  Hold writers out while checking and fail before dropping any
    # ledger object so operators must explicitly back up and migrate history.
    op.execute(
        "LOCK TABLE active_snapshot_pointers, snapshot_activation_events, "
        "corpus_snapshots IN SHARE ROW EXCLUSIVE MODE"
    )
    op.execute(DOWNGRADE_ACTIVATION_HISTORY_ASSERTIONS_SQL)
    for table_name in reversed(
        (
            "corpus_snapshots",
            "active_snapshot_pointers",
            "snapshot_activation_events",
        )
    ):
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_{table_name}_activation_consistency "
            f"ON {table_name}"
        )
    op.execute(
        "DROP FUNCTION IF EXISTS legal_rag_enforce_snapshot_activation_consistency()"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_snapshot_activation_events_immutable "
        "ON snapshot_activation_events"
    )
    op.execute(LEGACY_CORPUS_SNAPSHOT_GUARD_SQL)
    op.drop_constraint(
        "fk_active_snapshot_pointer_activation_event",
        "active_snapshot_pointers",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_active_snapshot_pointers_revision_positive",
        "active_snapshot_pointers",
        type_="check",
    )
    op.drop_column("active_snapshot_pointers", "activation_id")
    op.drop_column("active_snapshot_pointers", "revision")
    op.drop_table("snapshot_activation_events")
    op.drop_index("ix_chunk_articles_article_chunk", table_name="chunk_articles")
