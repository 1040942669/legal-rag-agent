"""Create versioned corpus and pgvector storage.

Revision ID: 0001_m3_storage
Revises: None
Create Date: 2026-09-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import VECTOR
from sqlalchemy.dialects import postgresql


revision: str = "0001_m3_storage"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "corpus_snapshots",
        sa.Column("snapshot_id", sa.String(length=255), primary_key=True),
        sa.Column("scope_id", sa.String(length=255), nullable=False),
        sa.Column("source_manifest", postgresql.JSONB(), nullable=False),
        sa.Column("source_manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("corpus_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="building",
        ),
        sa.Column(
            "built_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('building', 'validated', 'active', 'failed', 'archived')",
            name="ck_corpus_snapshots_status",
        ),
        sa.UniqueConstraint(
            "scope_id",
            "snapshot_id",
            name="uq_corpus_snapshots_scope_snapshot",
        ),
    )
    op.create_index("ix_corpus_snapshots_scope", "corpus_snapshots", ["scope_id"])
    op.create_index(
        "ix_corpus_snapshots_scope_manifest",
        "corpus_snapshots",
        ["scope_id", "source_manifest_hash"],
    )
    op.create_index(
        "uq_corpus_snapshots_active_scope",
        "corpus_snapshots",
        ["scope_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "law_versions",
        sa.Column("version_id", sa.String(length=255), primary_key=True),
        sa.Column("law_id", sa.String(length=255), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("verification_status", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "verification_status IN ('unknown', 'verified', 'unverified')",
            name="ck_law_versions_verification_status",
        ),
        sa.CheckConstraint(
            "valid_from IS NULL OR valid_to IS NULL OR valid_from < valid_to",
            name="ck_law_versions_valid_interval",
        ),
        sa.UniqueConstraint("version_id", "law_id", name="uq_law_versions_version_law"),
    )
    op.create_index("ix_law_versions_law_id", "law_versions", ["law_id"])
    op.create_index("ix_law_versions_title", "law_versions", ["title"])

    op.create_table(
        "law_articles",
        sa.Column("article_id", sa.String(length=255), primary_key=True),
        sa.Column("version_id", sa.String(length=255), nullable=False),
        sa.Column("law_id", sa.String(length=255), nullable=False),
        sa.Column("article_number", sa.String(length=128), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("source_line", sa.Integer(), nullable=False),
        sa.Column("parse_status", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("source_line > 0", name="ck_law_articles_source_line"),
        sa.ForeignKeyConstraint(
            ["version_id", "law_id"],
            ["law_versions.version_id", "law_versions.law_id"],
            name="fk_law_articles_version_law",
        ),
    )
    op.create_index(
        "ix_law_articles_exact_lookup",
        "law_articles",
        ["law_id", "version_id", "article_number"],
    )
    op.create_index(
        "uq_law_articles_version_number",
        "law_articles",
        ["version_id", "article_number"],
        unique=True,
        postgresql_where=sa.text("article_number <> ''"),
    )

    op.create_table(
        "chunks",
        sa.Column("chunk_id", sa.String(length=255), primary_key=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("strategy", sa.String(length=64), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("recipe_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "chunk_articles",
        sa.Column(
            "chunk_id",
            sa.String(length=255),
            sa.ForeignKey("chunks.chunk_id"),
            primary_key=True,
        ),
        sa.Column(
            "article_id",
            sa.String(length=255),
            sa.ForeignKey("law_articles.article_id"),
            primary_key=True,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.CheckConstraint("ordinal >= 0", name="ck_chunk_articles_ordinal"),
        sa.UniqueConstraint("chunk_id", "ordinal", name="uq_chunk_articles_ordinal"),
    )

    op.create_table(
        "snapshot_chunks",
        sa.Column(
            "snapshot_id",
            sa.String(length=255),
            sa.ForeignKey("corpus_snapshots.snapshot_id"),
            primary_key=True,
        ),
        sa.Column(
            "chunk_id",
            sa.String(length=255),
            sa.ForeignKey("chunks.chunk_id"),
            primary_key=True,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.CheckConstraint("ordinal >= 0", name="ck_snapshot_chunks_ordinal"),
        sa.UniqueConstraint(
            "snapshot_id", "ordinal", name="uq_snapshot_chunks_ordinal"
        ),
    )
    op.create_index("ix_snapshot_chunks_chunk", "snapshot_chunks", ["chunk_id"])

    op.create_table(
        "embedding_profiles",
        sa.Column("profile_id", sa.String(length=64), primary_key=True),
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("revision", sa.Text(), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("normalization", sa.Boolean(), nullable=False),
        sa.Column("query_prefix", sa.Text(), nullable=False),
        sa.Column("document_prefix", sa.Text(), nullable=False),
        sa.Column("embed_with_metadata", sa.Boolean(), nullable=False),
        sa.Column("recipe_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("dimensions > 0", name="ck_embedding_profiles_dimensions"),
    )

    op.create_table(
        "chunk_embeddings",
        sa.Column(
            "chunk_id",
            sa.String(length=255),
            sa.ForeignKey("chunks.chunk_id"),
            primary_key=True,
        ),
        sa.Column(
            "profile_id",
            sa.String(length=64),
            sa.ForeignKey("embedding_profiles.profile_id"),
            primary_key=True,
        ),
        sa.Column("embedding", VECTOR(), nullable=False),
        sa.Column("embedding_dimension", sa.Integer(), nullable=False),
        sa.Column("embedding_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "embedding_dimension > 0", name="ck_chunk_embeddings_dimension"
        ),
        sa.CheckConstraint(
            "vector_dims(embedding) = embedding_dimension",
            name="ck_chunk_embeddings_vector_dimension",
        ),
    )
    op.create_index("ix_chunk_embeddings_profile", "chunk_embeddings", ["profile_id"])

    op.execute(
        """
        CREATE FUNCTION legal_rag_enforce_embedding_profile_dimension()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            expected_dimension integer;
        BEGIN
            SELECT dimensions INTO expected_dimension
            FROM embedding_profiles
            WHERE profile_id = NEW.profile_id;
            IF expected_dimension IS NULL THEN
                RAISE EXCEPTION 'embedding profile % does not exist', NEW.profile_id;
            END IF;
            IF NEW.embedding_dimension <> expected_dimension THEN
                RAISE EXCEPTION 'embedding dimension % does not match profile dimension %',
                    NEW.embedding_dimension, expected_dimension;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )

    op.create_table(
        "embedding_imports",
        sa.Column(
            "snapshot_id",
            sa.String(length=255),
            sa.ForeignKey("corpus_snapshots.snapshot_id"),
            primary_key=True,
        ),
        sa.Column(
            "profile_id",
            sa.String(length=64),
            sa.ForeignKey("embedding_profiles.profile_id"),
            primary_key=True,
        ),
        sa.Column("bundle_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('validated', 'failed')",
            name="ck_embedding_imports_status",
        ),
    )
    op.execute(
        """
        CREATE TRIGGER trg_chunk_embeddings_profile_dimension
        BEFORE INSERT OR UPDATE ON chunk_embeddings
        FOR EACH ROW
        EXECUTE FUNCTION legal_rag_enforce_embedding_profile_dimension()
        """
    )

    op.create_table(
        "index_builds",
        sa.Column("build_id", sa.String(length=255), primary_key=True),
        sa.Column("snapshot_id", sa.String(length=255), nullable=False),
        sa.Column("profile_id", sa.String(length=64), nullable=False),
        sa.Column("index_params", postgresql.JSONB(), nullable=False),
        sa.Column("index_params_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('building', 'validated', 'active', 'failed', 'archived')",
            name="ck_index_builds_status",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id", "profile_id"],
            ["embedding_imports.snapshot_id", "embedding_imports.profile_id"],
            name="fk_index_builds_embedding_import",
        ),
        sa.UniqueConstraint(
            "snapshot_id",
            "profile_id",
            "index_params_hash",
            name="uq_index_build_identity",
        ),
    )

    op.create_table(
        "active_snapshot_pointers",
        sa.Column("scope_id", sa.String(length=255), primary_key=True),
        sa.Column(
            "snapshot_id",
            sa.String(length=255),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["scope_id", "snapshot_id"],
            ["corpus_snapshots.scope_id", "corpus_snapshots.snapshot_id"],
            name="fk_active_snapshot_pointer_scope_snapshot",
        ),
    )


def downgrade() -> None:
    op.drop_table("active_snapshot_pointers")
    op.drop_table("index_builds")
    op.drop_table("embedding_imports")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_chunk_embeddings_profile_dimension "
        "ON chunk_embeddings"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS legal_rag_enforce_embedding_profile_dimension()"
    )
    op.drop_index("ix_chunk_embeddings_profile", table_name="chunk_embeddings")
    op.drop_table("chunk_embeddings")
    op.drop_table("embedding_profiles")
    op.drop_index("ix_snapshot_chunks_chunk", table_name="snapshot_chunks")
    op.drop_table("snapshot_chunks")
    op.drop_table("chunk_articles")
    op.drop_table("chunks")
    op.drop_index("ix_law_articles_exact_lookup", table_name="law_articles")
    op.drop_index("uq_law_articles_version_number", table_name="law_articles")
    op.drop_table("law_articles")
    op.drop_index("ix_law_versions_title", table_name="law_versions")
    op.drop_index("ix_law_versions_law_id", table_name="law_versions")
    op.drop_table("law_versions")
    op.drop_index("uq_corpus_snapshots_active_scope", table_name="corpus_snapshots")
    op.drop_index("ix_corpus_snapshots_scope_manifest", table_name="corpus_snapshots")
    op.drop_index("ix_corpus_snapshots_scope", table_name="corpus_snapshots")
    op.drop_table("corpus_snapshots")
