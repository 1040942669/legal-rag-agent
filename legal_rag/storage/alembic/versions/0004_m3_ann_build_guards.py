"""Guard ANN build identities and lifecycle transitions.

Revision ID: 0004_m3_ann_guards
Revises: 0003_m3_activation
Create Date: 2026-09-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0004_m3_ann_guards"
down_revision: str | None = "0003_m3_activation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Prevent an embedding INSERT from landing after the backfill snapshot but
    # before the tracking trigger exists. The lock is released with the
    # migration transaction; queued importers then run through the trigger.
    op.execute("LOCK TABLE chunk_embeddings IN SHARE ROW EXCLUSIVE MODE")
    op.create_table(
        "embedding_profile_generations",
        sa.Column("profile_id", sa.String(length=64), nullable=False),
        sa.Column("embedding_count", sa.BigInteger(), nullable=False),
        sa.Column("embedding_manifest_hash", sa.String(length=64), nullable=True),
        sa.Column("ann_physical_instance_id", sa.String(length=64), nullable=True),
        sa.Column("ann_physical_index_name", sa.String(length=63), nullable=True),
        sa.Column("ann_index_params_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "embedding_count >= 0",
            name="ck_embedding_profile_generations_count",
        ),
        sa.CheckConstraint(
            "((embedding_manifest_hash IS NULL "
            "AND ann_physical_instance_id IS NULL "
            "AND ann_physical_index_name IS NULL "
            "AND ann_index_params_hash IS NULL) OR "
            "(embedding_manifest_hash IS NOT NULL "
            "AND ann_physical_instance_id IS NOT NULL "
            "AND ann_physical_index_name IS NOT NULL "
            "AND ann_index_params_hash IS NOT NULL))",
            name="ck_embedding_profile_generations_ann_binding",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["embedding_profiles.profile_id"],
            name="fk_embedding_profile_generations_profile",
        ),
        sa.PrimaryKeyConstraint("profile_id"),
    )
    op.execute(
        """
        INSERT INTO embedding_profile_generations (
            profile_id,
            embedding_count,
            updated_at
        )
        SELECT
            profile.profile_id,
            count(embedding.chunk_id),
            CURRENT_TIMESTAMP
        FROM embedding_profiles AS profile
        LEFT JOIN chunk_embeddings AS embedding
          ON embedding.profile_id = profile.profile_id
        GROUP BY profile.profile_id
        """
    )
    op.execute(
        """
        CREATE FUNCTION legal_rag_guard_embedding_profile_generation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'embedding profile generations cannot be deleted'
                    USING ERRCODE = '55000';
            END IF;

            IF TG_OP = 'INSERT' THEN
                IF pg_trigger_depth() < 2
                    OR NEW.embedding_count <> 1
                    OR NEW.embedding_manifest_hash IS NOT NULL
                    OR NEW.ann_physical_instance_id IS NOT NULL
                    OR NEW.ann_physical_index_name IS NOT NULL
                    OR NEW.ann_index_params_hash IS NOT NULL
                THEN
                    RAISE EXCEPTION 'embedding profile generations are created only by embedding inserts'
                        USING ERRCODE = '55000';
                END IF;
                RETURN NEW;
            END IF;

            IF NEW.profile_id IS DISTINCT FROM OLD.profile_id THEN
                RAISE EXCEPTION 'embedding profile generation identity is immutable'
                    USING ERRCODE = '55000';
            END IF;

            IF NEW.embedding_count = OLD.embedding_count + 1 THEN
                IF pg_trigger_depth() < 2
                    OR NEW.embedding_manifest_hash IS NOT NULL
                    OR NEW.ann_physical_instance_id IS NOT NULL
                    OR NEW.ann_physical_index_name IS NOT NULL
                    OR NEW.ann_index_params_hash IS NOT NULL
                THEN
                    RAISE EXCEPTION 'embedding profile generations advance only through embedding inserts'
                        USING ERRCODE = '55000';
                END IF;
                RETURN NEW;
            END IF;

            IF NEW.embedding_count = OLD.embedding_count
                AND OLD.embedding_manifest_hash IS NULL
                AND OLD.ann_physical_instance_id IS NULL
                AND OLD.ann_physical_index_name IS NULL
                AND OLD.ann_index_params_hash IS NULL
                AND NEW.embedding_manifest_hash IS NOT NULL
                AND NEW.ann_physical_instance_id IS NOT NULL
                AND NEW.ann_physical_index_name IS NOT NULL
                AND NEW.ann_index_params_hash IS NOT NULL
            THEN
                IF current_setting(
                    'legal_rag.ann_binding_managed', true
                ) IS DISTINCT FROM '1'
                    OR NOT EXISTS (
                        SELECT 1
                        FROM index_builds AS build
                        WHERE build.profile_id = NEW.profile_id
                          AND build.index_params_hash =
                              NEW.ann_index_params_hash
                          AND build.status IN (
                              'building', 'validated', 'active'
                          )
                          AND build.index_params ->> 'profile_embedding_count'
                              = NEW.embedding_count::text
                          AND build.index_params
                              ->> 'profile_embedding_manifest_hash'
                              = NEW.embedding_manifest_hash
                          AND build.index_params ->> 'physical_instance_id'
                              = NEW.ann_physical_instance_id
                          AND build.index_params ->> 'physical_index_name'
                              = NEW.ann_physical_index_name
                    )
                    OR NOT EXISTS (
                        SELECT 1
                        FROM pg_index AS catalog_index
                        JOIN pg_class AS index_relation
                          ON index_relation.oid = catalog_index.indexrelid
                        JOIN pg_class AS table_relation
                          ON table_relation.oid = catalog_index.indrelid
                        JOIN pg_namespace AS namespace
                          ON namespace.oid = index_relation.relnamespace
                        JOIN pg_am AS access_method
                          ON access_method.oid = index_relation.relam
                        WHERE namespace.nspname = current_schema()
                          AND index_relation.relname =
                              NEW.ann_physical_index_name
                          AND table_relation.relname = 'chunk_embeddings'
                          AND access_method.amname = 'hnsw'
                          AND catalog_index.indisvalid
                          AND catalog_index.indisready
                    )
                THEN
                    RAISE EXCEPTION 'embedding profile ANN binding requires a managed validated operation'
                        USING ERRCODE = '55000';
                END IF;
                RETURN NEW;
            END IF;

            RAISE EXCEPTION 'embedding profile generation state cannot be rewritten or rolled back'
                USING ERRCODE = '55000';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION legal_rag_bind_embedding_profile_generation(
            requested_profile_id varchar(64),
            expected_embedding_count bigint,
            requested_manifest_hash varchar(64),
            requested_physical_instance_id varchar(64),
            requested_physical_index_name varchar(63),
            requested_index_params_hash varchar(64)
        )
        RETURNS void
        LANGUAGE plpgsql
        AS $$
        DECLARE
            affected_rows integer;
            previous_guard text;
        BEGIN
            previous_guard := current_setting(
                'legal_rag.ann_binding_managed', true
            );
            PERFORM set_config(
                'legal_rag.ann_binding_managed', '1', true
            );
            UPDATE embedding_profile_generations
            SET embedding_manifest_hash = requested_manifest_hash,
                ann_physical_instance_id = requested_physical_instance_id,
                ann_physical_index_name = requested_physical_index_name,
                ann_index_params_hash = requested_index_params_hash,
                updated_at = CURRENT_TIMESTAMP
            WHERE profile_id = requested_profile_id
              AND embedding_count = expected_embedding_count
              AND embedding_manifest_hash IS NULL
              AND ann_physical_instance_id IS NULL
              AND ann_physical_index_name IS NULL
              AND ann_index_params_hash IS NULL;
            GET DIAGNOSTICS affected_rows = ROW_COUNT;
            PERFORM set_config(
                'legal_rag.ann_binding_managed',
                COALESCE(previous_guard, ''),
                true
            );
            IF affected_rows <> 1 THEN
                RAISE EXCEPTION 'embedding generation changed before HNSW binding completed'
                    USING ERRCODE = '55000';
            END IF;
        EXCEPTION
            WHEN OTHERS THEN
                PERFORM set_config(
                    'legal_rag.ann_binding_managed',
                    COALESCE(previous_guard, ''),
                    true
                );
                RAISE;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_embedding_profile_generations_guarded
        BEFORE INSERT OR UPDATE OR DELETE ON embedding_profile_generations
        FOR EACH ROW
        EXECUTE FUNCTION legal_rag_guard_embedding_profile_generation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION legal_rag_track_embedding_profile_generation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            INSERT INTO embedding_profile_generations (
                profile_id,
                embedding_count,
                updated_at
            ) VALUES (
                NEW.profile_id,
                1,
                CURRENT_TIMESTAMP
            )
            ON CONFLICT (profile_id) DO UPDATE
            SET embedding_count =
                    embedding_profile_generations.embedding_count + 1,
                embedding_manifest_hash = NULL,
                ann_physical_instance_id = NULL,
                ann_physical_index_name = NULL,
                ann_index_params_hash = NULL,
                updated_at = CURRENT_TIMESTAMP;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_chunk_embeddings_track_profile_generation
        AFTER INSERT ON chunk_embeddings
        FOR EACH ROW
        EXECUTE FUNCTION legal_rag_track_embedding_profile_generation()
        """
    )
    op.create_check_constraint(
        "ck_index_builds_completion",
        "index_builds",
        "((status = 'building' AND completed_at IS NULL) OR "
        "(status IN ('validated', 'active', 'failed', 'archived') "
        "AND completed_at IS NOT NULL))",
    )
    op.create_index(
        "uq_index_builds_active_boundary",
        "index_builds",
        ["snapshot_id", "profile_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.execute(
        """
        CREATE FUNCTION legal_rag_guard_index_build_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'building' OR NEW.completed_at IS NOT NULL THEN
                    RAISE EXCEPTION 'ANN index builds must begin in building status without completed_at'
                        USING ERRCODE = '55000';
                END IF;
                RETURN NEW;
            END IF;

            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'ANN index build receipts are immutable and cannot be deleted'
                    USING ERRCODE = '55000';
            END IF;

            IF NEW.build_id IS DISTINCT FROM OLD.build_id
                OR NEW.snapshot_id IS DISTINCT FROM OLD.snapshot_id
                OR NEW.profile_id IS DISTINCT FROM OLD.profile_id
                OR NEW.index_params IS DISTINCT FROM OLD.index_params
                OR NEW.index_params_hash IS DISTINCT FROM OLD.index_params_hash
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
            THEN
                RAISE EXCEPTION 'ANN index build identity is immutable'
                    USING ERRCODE = '55000';
            END IF;

            IF NEW.status = OLD.status THEN
                IF NEW.completed_at IS DISTINCT FROM OLD.completed_at THEN
                    RAISE EXCEPTION 'ANN index completion time cannot change without a status transition'
                        USING ERRCODE = '55000';
                END IF;
                RETURN NEW;
            END IF;

            IF NOT (
                (OLD.status = 'building' AND NEW.status IN ('validated', 'failed'))
                OR (OLD.status = 'validated' AND NEW.status IN ('active', 'archived'))
                OR (OLD.status = 'active' AND NEW.status = 'archived')
            ) THEN
                RAISE EXCEPTION 'invalid ANN index build transition: % -> %',
                    OLD.status, NEW.status
                    USING ERRCODE = '55000';
            END IF;

            IF OLD.status = 'building' THEN
                IF OLD.completed_at IS NOT NULL OR NEW.completed_at IS NULL THEN
                    RAISE EXCEPTION 'completed ANN index transitions require completed_at'
                        USING ERRCODE = '55000';
                END IF;
            ELSIF NEW.completed_at IS DISTINCT FROM OLD.completed_at THEN
                RAISE EXCEPTION 'completed ANN index build time is immutable'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_index_builds_guarded
        BEFORE INSERT OR UPDATE OR DELETE ON index_builds
        FOR EACH ROW
        EXECUTE FUNCTION legal_rag_guard_index_build_change()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_index_builds_guarded ON index_builds")
    op.execute("DROP FUNCTION IF EXISTS legal_rag_guard_index_build_change()")
    op.drop_index("uq_index_builds_active_boundary", table_name="index_builds")
    op.drop_constraint(
        "ck_index_builds_completion",
        "index_builds",
        type_="check",
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_chunk_embeddings_track_profile_generation "
        "ON chunk_embeddings"
    )
    op.execute("DROP FUNCTION IF EXISTS legal_rag_track_embedding_profile_generation()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_embedding_profile_generations_guarded "
        "ON embedding_profile_generations"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS legal_rag_bind_embedding_profile_generation("
        "varchar, bigint, varchar, varchar, varchar, varchar)"
    )
    op.execute("DROP FUNCTION IF EXISTS legal_rag_guard_embedding_profile_generation()")
    op.drop_table("embedding_profile_generations")
