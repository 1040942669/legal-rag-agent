"""Reject mutation of content-addressed corpus and embedding rows.

Revision ID: 0002_m3_immutable_rows
Revises: 0001_m3_storage
Create Date: 2026-09-25
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "0002_m3_immutable_rows"
down_revision: str | None = "0001_m3_storage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


IMMUTABLE_TABLES = (
    "law_versions",
    "law_articles",
    "chunks",
    "chunk_articles",
    "snapshot_chunks",
    "embedding_profiles",
    "chunk_embeddings",
    "embedding_imports",
)


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION legal_rag_reject_immutable_row_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'immutable storage table % does not allow %',
                TG_TABLE_NAME, TG_OP
                USING ERRCODE = '55000';
        END;
        $$
        """
    )
    for table_name in IMMUTABLE_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_immutable
            BEFORE UPDATE OR DELETE ON {table_name}
            FOR EACH ROW
            EXECUTE FUNCTION legal_rag_reject_immutable_row_change()
            """
        )
    op.execute(
        """
        CREATE FUNCTION legal_rag_guard_corpus_snapshot_change()
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
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_corpus_snapshots_guarded
        BEFORE INSERT OR UPDATE OR DELETE ON corpus_snapshots
        FOR EACH ROW
        EXECUTE FUNCTION legal_rag_guard_corpus_snapshot_change()
        """
    )
    op.execute(
        """
        CREATE FUNCTION legal_rag_guard_snapshot_chunk_insert()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            parent_status text;
        BEGIN
            SELECT status INTO parent_status
            FROM corpus_snapshots
            WHERE snapshot_id = NEW.snapshot_id
            FOR UPDATE;
            IF parent_status IS NULL OR parent_status <> 'building' THEN
                RAISE EXCEPTION 'snapshot membership is closed outside building state'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_snapshot_chunks_insert_guard
        BEFORE INSERT ON snapshot_chunks
        FOR EACH ROW
        EXECUTE FUNCTION legal_rag_guard_snapshot_chunk_insert()
        """
    )
    op.execute(
        """
        CREATE FUNCTION legal_rag_guard_chunk_article_insert()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            parent_record record;
        BEGIN
            FOR parent_record IN
                SELECT cs.status
                FROM snapshot_chunks AS sc
                JOIN corpus_snapshots AS cs
                    ON cs.snapshot_id = sc.snapshot_id
                WHERE sc.chunk_id = NEW.chunk_id
                FOR UPDATE OF cs
            LOOP
                IF parent_record.status <> 'building' THEN
                    RAISE EXCEPTION 'chunk article relations are closed after validation'
                        USING ERRCODE = '55000';
                END IF;
            END LOOP;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_chunk_articles_insert_guard
        BEFORE INSERT ON chunk_articles
        FOR EACH ROW
        EXECUTE FUNCTION legal_rag_guard_chunk_article_insert()
        """
    )
    op.execute(
        """
        CREATE FUNCTION legal_rag_guard_law_article_insert()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            parent_record record;
        BEGIN
            FOR parent_record IN
                SELECT cs.snapshot_id, cs.status
                FROM law_articles AS existing_article
                JOIN chunk_articles AS ca
                    ON ca.article_id = existing_article.article_id
                JOIN snapshot_chunks AS sc
                    ON sc.chunk_id = ca.chunk_id
                JOIN corpus_snapshots AS cs
                    ON cs.snapshot_id = sc.snapshot_id
                WHERE existing_article.version_id = NEW.version_id
                    AND existing_article.law_id = NEW.law_id
                FOR UPDATE OF cs
            LOOP
                IF parent_record.status <> 'building' THEN
                    RAISE EXCEPTION 'law version articles are closed after validation'
                        USING ERRCODE = '55000';
                END IF;
            END LOOP;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_law_articles_insert_guard
        BEFORE INSERT ON law_articles
        FOR EACH ROW
        EXECUTE FUNCTION legal_rag_guard_law_article_insert()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_law_articles_insert_guard ON law_articles")
    op.execute("DROP FUNCTION IF EXISTS legal_rag_guard_law_article_insert()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_chunk_articles_insert_guard ON chunk_articles"
    )
    op.execute("DROP FUNCTION IF EXISTS legal_rag_guard_chunk_article_insert()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_snapshot_chunks_insert_guard ON snapshot_chunks"
    )
    op.execute("DROP FUNCTION IF EXISTS legal_rag_guard_snapshot_chunk_insert()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_corpus_snapshots_guarded ON corpus_snapshots"
    )
    op.execute("DROP FUNCTION IF EXISTS legal_rag_guard_corpus_snapshot_change()")
    for table_name in reversed(IMMUTABLE_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_immutable ON {table_name}")
    op.execute("DROP FUNCTION IF EXISTS legal_rag_reject_immutable_row_change()")
