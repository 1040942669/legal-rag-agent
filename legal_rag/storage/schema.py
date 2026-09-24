from __future__ import annotations

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB


metadata = MetaData()


corpus_snapshots = Table(
    "corpus_snapshots",
    metadata,
    Column("snapshot_id", String(255), primary_key=True),
    Column("scope_id", String(255), nullable=False),
    Column("source_manifest", JSONB, nullable=False),
    Column("source_manifest_hash", String(64), nullable=False),
    Column("corpus_hash", String(64), nullable=False),
    Column("status", String(32), nullable=False, server_default="building"),
    Column(
        "built_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    Column("validated_at", DateTime(timezone=True), nullable=True),
    Column("activated_at", DateTime(timezone=True), nullable=True),
    CheckConstraint(
        "status IN ('building', 'validated', 'active', 'failed', 'archived')",
        name="ck_corpus_snapshots_status",
    ),
    UniqueConstraint(
        "scope_id", "snapshot_id", name="uq_corpus_snapshots_scope_snapshot"
    ),
)

Index("ix_corpus_snapshots_scope", corpus_snapshots.c.scope_id)
Index(
    "ix_corpus_snapshots_scope_manifest",
    corpus_snapshots.c.scope_id,
    corpus_snapshots.c.source_manifest_hash,
)
Index(
    "uq_corpus_snapshots_active_scope",
    corpus_snapshots.c.scope_id,
    unique=True,
    postgresql_where=corpus_snapshots.c.status == "active",
)


law_versions = Table(
    "law_versions",
    metadata,
    Column("version_id", String(255), primary_key=True),
    Column("law_id", String(255), nullable=False),
    Column("title", Text, nullable=False),
    Column("valid_from", Date, nullable=True),
    Column("valid_to", Date, nullable=True),
    Column("verification_status", String(32), nullable=False),
    Column("source_ref", Text, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    CheckConstraint(
        "verification_status IN ('unknown', 'verified', 'unverified')",
        name="ck_law_versions_verification_status",
    ),
    CheckConstraint(
        "valid_from IS NULL OR valid_to IS NULL OR valid_from < valid_to",
        name="ck_law_versions_valid_interval",
    ),
    UniqueConstraint("version_id", "law_id", name="uq_law_versions_version_law"),
)

Index("ix_law_versions_law_id", law_versions.c.law_id)
Index("ix_law_versions_title", law_versions.c.title)


law_articles = Table(
    "law_articles",
    metadata,
    Column("article_id", String(255), primary_key=True),
    Column("version_id", String(255), nullable=False),
    Column("law_id", String(255), nullable=False),
    Column("article_number", String(128), nullable=False),
    Column("body", Text, nullable=False),
    Column("raw_text", Text, nullable=False),
    Column("source_ref", Text, nullable=False),
    Column("source_line", Integer, nullable=False),
    Column("parse_status", String(64), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    CheckConstraint("source_line > 0", name="ck_law_articles_source_line"),
    ForeignKeyConstraint(
        ["version_id", "law_id"],
        ["law_versions.version_id", "law_versions.law_id"],
        name="fk_law_articles_version_law",
    ),
)

Index(
    "ix_law_articles_exact_lookup",
    law_articles.c.law_id,
    law_articles.c.version_id,
    law_articles.c.article_number,
)
Index(
    "uq_law_articles_version_number",
    law_articles.c.version_id,
    law_articles.c.article_number,
    unique=True,
    postgresql_where=law_articles.c.article_number != "",
)


chunks = Table(
    "chunks",
    metadata,
    Column("chunk_id", String(255), primary_key=True),
    Column("text", Text, nullable=False),
    Column("strategy", String(64), nullable=False),
    Column("metadata", JSONB, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("recipe_hash", String(64), nullable=False),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
)


chunk_articles = Table(
    "chunk_articles",
    metadata,
    Column("chunk_id", String(255), ForeignKey("chunks.chunk_id"), primary_key=True),
    Column(
        "article_id",
        String(255),
        ForeignKey("law_articles.article_id"),
        primary_key=True,
    ),
    Column("ordinal", Integer, nullable=False),
    CheckConstraint("ordinal >= 0", name="ck_chunk_articles_ordinal"),
    UniqueConstraint("chunk_id", "ordinal", name="uq_chunk_articles_ordinal"),
)

Index(
    "ix_chunk_articles_article_chunk",
    chunk_articles.c.article_id,
    chunk_articles.c.chunk_id,
)


snapshot_chunks = Table(
    "snapshot_chunks",
    metadata,
    Column(
        "snapshot_id",
        String(255),
        ForeignKey("corpus_snapshots.snapshot_id"),
        primary_key=True,
    ),
    Column("chunk_id", String(255), ForeignKey("chunks.chunk_id"), primary_key=True),
    Column("ordinal", Integer, nullable=False),
    CheckConstraint("ordinal >= 0", name="ck_snapshot_chunks_ordinal"),
    UniqueConstraint("snapshot_id", "ordinal", name="uq_snapshot_chunks_ordinal"),
)

Index("ix_snapshot_chunks_chunk", snapshot_chunks.c.chunk_id)


embedding_profiles = Table(
    "embedding_profiles",
    metadata,
    Column("profile_id", String(64), primary_key=True),
    Column("provider", String(128), nullable=False),
    Column("model", Text, nullable=False),
    Column("revision", Text, nullable=False),
    Column("dimensions", Integer, nullable=False),
    Column("normalization", Boolean, nullable=False),
    Column("query_prefix", Text, nullable=False),
    Column("document_prefix", Text, nullable=False),
    Column("embed_with_metadata", Boolean, nullable=False),
    Column("recipe_hash", String(64), nullable=False),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    CheckConstraint("dimensions > 0", name="ck_embedding_profiles_dimensions"),
)


chunk_embeddings = Table(
    "chunk_embeddings",
    metadata,
    Column("chunk_id", String(255), ForeignKey("chunks.chunk_id"), primary_key=True),
    Column(
        "profile_id",
        String(64),
        ForeignKey("embedding_profiles.profile_id"),
        primary_key=True,
    ),
    Column("embedding", VECTOR(), nullable=False),
    Column("embedding_dimension", Integer, nullable=False),
    Column("embedding_hash", String(64), nullable=False),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    CheckConstraint("embedding_dimension > 0", name="ck_chunk_embeddings_dimension"),
    CheckConstraint(
        "vector_dims(embedding) = embedding_dimension",
        name="ck_chunk_embeddings_vector_dimension",
    ),
)

Index("ix_chunk_embeddings_profile", chunk_embeddings.c.profile_id)


embedding_imports = Table(
    "embedding_imports",
    metadata,
    Column(
        "snapshot_id",
        String(255),
        ForeignKey("corpus_snapshots.snapshot_id"),
        primary_key=True,
    ),
    Column(
        "profile_id",
        String(64),
        ForeignKey("embedding_profiles.profile_id"),
        primary_key=True,
    ),
    Column("bundle_hash", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column(
        "imported_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    CheckConstraint(
        "status IN ('validated', 'failed')", name="ck_embedding_imports_status"
    ),
)


embedding_profile_generations = Table(
    "embedding_profile_generations",
    metadata,
    Column(
        "profile_id",
        String(64),
        primary_key=True,
    ),
    Column("embedding_count", BigInteger, nullable=False),
    Column("embedding_manifest_hash", String(64), nullable=True),
    Column("ann_physical_instance_id", String(64), nullable=True),
    Column("ann_physical_index_name", String(63), nullable=True),
    Column("ann_index_params_hash", String(64), nullable=True),
    Column(
        "updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    CheckConstraint(
        "embedding_count >= 0",
        name="ck_embedding_profile_generations_count",
    ),
    CheckConstraint(
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
    ForeignKeyConstraint(
        ["profile_id"],
        ["embedding_profiles.profile_id"],
        name="fk_embedding_profile_generations_profile",
    ),
)


index_builds = Table(
    "index_builds",
    metadata,
    Column("build_id", String(255), primary_key=True),
    Column("snapshot_id", String(255), nullable=False),
    Column("profile_id", String(64), nullable=False),
    Column("index_params", JSONB, nullable=False),
    Column("index_params_hash", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    CheckConstraint(
        "status IN ('building', 'validated', 'active', 'failed', 'archived')",
        name="ck_index_builds_status",
    ),
    CheckConstraint(
        "((status = 'building' AND completed_at IS NULL) OR "
        "(status IN ('validated', 'active', 'failed', 'archived') "
        "AND completed_at IS NOT NULL))",
        name="ck_index_builds_completion",
    ),
    ForeignKeyConstraint(
        ["snapshot_id", "profile_id"],
        ["embedding_imports.snapshot_id", "embedding_imports.profile_id"],
        name="fk_index_builds_embedding_import",
    ),
    UniqueConstraint(
        "snapshot_id",
        "profile_id",
        "index_params_hash",
        name="uq_index_build_identity",
    ),
)

Index(
    "uq_index_builds_active_boundary",
    index_builds.c.snapshot_id,
    index_builds.c.profile_id,
    unique=True,
    postgresql_where=index_builds.c.status == "active",
)


snapshot_activation_events = Table(
    "snapshot_activation_events",
    metadata,
    Column("activation_id", String(64), primary_key=True),
    Column("scope_id", String(255), nullable=False),
    Column("revision", BigInteger, nullable=False),
    Column("operation", String(32), nullable=False),
    Column("previous_snapshot_id", String(255), nullable=True),
    Column("target_snapshot_id", String(255), nullable=False),
    Column("previous_activation_id", String(64), nullable=True),
    Column("actor", String(128), nullable=True),
    Column("reason", Text, nullable=True),
    Column(
        "occurred_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    CheckConstraint(
        "revision > 0",
        name="ck_snapshot_activation_events_revision_positive",
    ),
    CheckConstraint(
        "operation IN "
        "('initial_activate', 'replace', 'rollback', 'migration_bootstrap')",
        name="ck_snapshot_activation_events_operation",
    ),
    CheckConstraint(
        "((revision = 1 "
        "AND previous_snapshot_id IS NULL "
        "AND previous_activation_id IS NULL) "
        "OR (revision > 1 "
        "AND previous_snapshot_id IS NOT NULL "
        "AND previous_activation_id IS NOT NULL))",
        name="ck_snapshot_activation_events_predecessor",
    ),
    CheckConstraint(
        "((operation IN ('initial_activate', 'migration_bootstrap') "
        "AND revision = 1) "
        "OR (operation IN ('replace', 'rollback') AND revision > 1))",
        name="ck_snapshot_activation_events_operation_revision",
    ),
    CheckConstraint(
        "previous_snapshot_id IS NULL OR target_snapshot_id <> previous_snapshot_id",
        name="ck_snapshot_activation_events_target_changes",
    ),
    ForeignKeyConstraint(
        ["scope_id", "previous_snapshot_id"],
        ["corpus_snapshots.scope_id", "corpus_snapshots.snapshot_id"],
        name="fk_snapshot_activation_events_previous_snapshot",
    ),
    ForeignKeyConstraint(
        ["scope_id", "target_snapshot_id"],
        ["corpus_snapshots.scope_id", "corpus_snapshots.snapshot_id"],
        name="fk_snapshot_activation_events_target_snapshot",
    ),
    ForeignKeyConstraint(
        ["scope_id", "previous_activation_id"],
        [
            "snapshot_activation_events.scope_id",
            "snapshot_activation_events.activation_id",
        ],
        name="fk_snapshot_activation_events_previous_event",
        deferrable=True,
        initially="DEFERRED",
    ),
    UniqueConstraint(
        "scope_id",
        "revision",
        name="uq_snapshot_activation_events_scope_revision",
    ),
    UniqueConstraint(
        "scope_id",
        "activation_id",
        name="uq_snapshot_activation_events_scope_activation",
    ),
    UniqueConstraint(
        "scope_id",
        "revision",
        "activation_id",
        "target_snapshot_id",
        name="uq_snapshot_activation_events_pointer_target",
    ),
)


active_snapshot_pointers = Table(
    "active_snapshot_pointers",
    metadata,
    Column("scope_id", String(255), primary_key=True),
    Column(
        "snapshot_id",
        String(255),
        nullable=False,
    ),
    Column("revision", BigInteger, nullable=False),
    Column("activation_id", String(64), nullable=False),
    Column(
        "updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    CheckConstraint(
        "revision > 0",
        name="ck_active_snapshot_pointers_revision_positive",
    ),
    ForeignKeyConstraint(
        ["scope_id", "snapshot_id"],
        ["corpus_snapshots.scope_id", "corpus_snapshots.snapshot_id"],
        name="fk_active_snapshot_pointer_scope_snapshot",
    ),
    ForeignKeyConstraint(
        ["scope_id", "revision", "activation_id", "snapshot_id"],
        [
            "snapshot_activation_events.scope_id",
            "snapshot_activation_events.revision",
            "snapshot_activation_events.activation_id",
            "snapshot_activation_events.target_snapshot_id",
        ],
        name="fk_active_snapshot_pointer_activation_event",
        deferrable=True,
        initially="DEFERRED",
    ),
)


EMBEDDING_DIMENSION_TRIGGER_SQL = """
CREATE OR REPLACE FUNCTION legal_rag_enforce_embedding_profile_dimension()
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
$$;

CREATE TRIGGER trg_chunk_embeddings_profile_dimension
BEFORE INSERT OR UPDATE ON chunk_embeddings
FOR EACH ROW
EXECUTE FUNCTION legal_rag_enforce_embedding_profile_dimension();
"""


DROP_EMBEDDING_DIMENSION_TRIGGER_SQL = """
DROP TRIGGER IF EXISTS trg_chunk_embeddings_profile_dimension ON chunk_embeddings;
DROP FUNCTION IF EXISTS legal_rag_enforce_embedding_profile_dimension();
"""
