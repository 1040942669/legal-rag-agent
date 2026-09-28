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


# M4 durable API/session state.  These rows deliberately live beside the M3
# corpus catalog while remaining independent from process-local chat memory.
# Application code generates opaque identifiers; the database enforces the
# ownership, ordering, idempotency, and one-active-run invariants.
sessions = Table(
    "sessions",
    metadata,
    Column("session_id", String(36), primary_key=True),
    Column("user_id", String(128), nullable=False),
    Column("title", Text, nullable=True),
    Column("status", String(32), nullable=False, server_default="active"),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    Column(
        "updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    CheckConstraint(
        "status IN ('active', 'archived')",
        name="ck_sessions_status",
    ),
    UniqueConstraint("session_id", "user_id", name="uq_sessions_identity_owner"),
)

Index("ix_sessions_user_created", sessions.c.user_id, sessions.c.created_at)


runs = Table(
    "runs",
    metadata,
    Column("run_id", String(36), primary_key=True),
    Column("session_id", String(36), nullable=False),
    Column("user_id", String(128), nullable=False),
    Column(
        "parent_run_id",
        String(36),
        ForeignKey(
            "runs.run_id",
            name="fk_runs_parent_run",
            ondelete="SET NULL",
        ),
        nullable=True,
    ),
    Column("status", String(32), nullable=False, server_default="queued"),
    Column("request_hash", String(64), nullable=False),
    Column("request_payload", JSONB, nullable=False),
    Column("scope_id", String(255), nullable=False),
    Column("snapshot_id", String(255), nullable=False),
    Column("snapshot_revision", BigInteger, nullable=False),
    Column("activation_id", String(64), nullable=False),
    Column("profile_id", String(64), nullable=False),
    Column("boundary_fingerprint", String(64), nullable=False),
    Column("retrieval_config_hash", String(64), nullable=False),
    Column("graph_version", String(64), nullable=False),
    Column("state_schema_version", Integer, nullable=False, server_default="1"),
    Column("checkpoint_namespace", String(255), nullable=True),
    Column("last_checkpoint_id", String(255), nullable=True),
    Column("last_completed_node", String(64), nullable=True),
    Column("execution_deadline_at", DateTime(timezone=True), nullable=True),
    Column("stop_reason", String(64), nullable=True),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    Column(
        "queued_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    Column(
        "updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    Column("error_code", String(64), nullable=True),
    Column("revision", BigInteger, nullable=False, server_default="0"),
    Column("event_sequence", BigInteger, nullable=False, server_default="0"),
    Column("lease_owner", String(128), nullable=True),
    Column("lease_expires_at", DateTime(timezone=True), nullable=True),
    Column("lease_epoch", BigInteger, nullable=False, server_default="0"),
    Column("resume_requested_at", DateTime(timezone=True), nullable=True),
    CheckConstraint(
        "status IN ('queued', 'running', 'interrupted', 'succeeded', "
        "'completed_with_limits', 'needs_clarification', 'failed', 'cancelled')",
        name="ck_runs_status",
    ),
    CheckConstraint(
        "parent_run_id IS NULL OR parent_run_id <> run_id",
        name="ck_runs_parent_not_self",
    ),
    CheckConstraint(
        "state_schema_version > 0",
        name="ck_runs_state_schema_version",
    ),
    CheckConstraint("revision >= 0", name="ck_runs_revision"),
    CheckConstraint("event_sequence >= 0", name="ck_runs_event_sequence"),
    CheckConstraint("lease_epoch >= 0", name="ck_runs_lease_epoch"),
    CheckConstraint(
        "last_checkpoint_id IS NULL OR checkpoint_namespace IS NOT NULL",
        name="ck_runs_checkpoint_pointer",
    ),
    CheckConstraint(
        "((status IN ('succeeded', 'completed_with_limits', "
        "'needs_clarification', 'failed', 'cancelled') "
        "AND finished_at IS NOT NULL) "
        "OR (status IN ('queued', 'running', 'interrupted') AND finished_at IS NULL))",
        name="ck_runs_finished_at",
    ),
    ForeignKeyConstraint(
        ["session_id", "user_id"],
        ["sessions.session_id", "sessions.user_id"],
        name="fk_runs_session_owner",
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["scope_id", "snapshot_revision", "activation_id", "snapshot_id"],
        [
            "snapshot_activation_events.scope_id",
            "snapshot_activation_events.revision",
            "snapshot_activation_events.activation_id",
            "snapshot_activation_events.target_snapshot_id",
        ],
        name="fk_runs_snapshot_activation",
    ),
    ForeignKeyConstraint(
        ["profile_id"],
        ["embedding_profiles.profile_id"],
        name="fk_runs_embedding_profile",
    ),
    UniqueConstraint(
        "run_id", "session_id", "user_id", name="uq_runs_identity_session_owner"
    ),
)

Index("ix_runs_user_created", runs.c.user_id, runs.c.created_at)
Index("ix_runs_status_queued", runs.c.status, runs.c.queued_at)
Index("ix_runs_parent_run_id", runs.c.parent_run_id)
Index(
    "ix_runs_resume_requested",
    runs.c.status,
    runs.c.resume_requested_at,
    postgresql_where=runs.c.resume_requested_at.is_not(None),
)
Index(
    "uq_runs_one_active_per_session",
    runs.c.session_id,
    unique=True,
    postgresql_where=runs.c.status.in_(("queued", "running", "interrupted")),
)


# M5 application-owned recovery records.  LangGraph's private checkpoint tables
# are intentionally managed by LangGraph itself and are not part of this
# metadata or the Alembic history.
run_budget_ledgers = Table(
    "run_budget_ledgers",
    metadata,
    Column(
        "run_id",
        String(36),
        ForeignKey(
            "runs.run_id",
            name="fk_run_budget_ledgers_run",
            ondelete="CASCADE",
        ),
        primary_key=True,
    ),
    Column("max_retrieval_rounds", Integer, nullable=False),
    Column("max_queries_per_round", Integer, nullable=False),
    Column("max_tool_attempts", Integer, nullable=False),
    Column("max_model_attempts", Integer, nullable=False),
    Column("max_embedding_attempts", Integer, nullable=False),
    Column("max_retry_per_operation", Integer, nullable=False),
    Column("evidence_top_k", Integer, nullable=False),
    Column("retrieval_rounds_used", Integer, nullable=False, server_default="0"),
    Column("queries_used", Integer, nullable=False, server_default="0"),
    Column("tool_attempts_used", Integer, nullable=False, server_default="0"),
    Column("model_attempts_used", Integer, nullable=False, server_default="0"),
    Column("embedding_attempts_used", Integer, nullable=False, server_default="0"),
    Column("revision", BigInteger, nullable=False, server_default="0"),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    Column(
        "updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    CheckConstraint(
        "max_retrieval_rounds BETWEEN 1 AND 32",
        name="ck_run_budget_ledgers_retrieval_limit",
    ),
    CheckConstraint(
        "max_queries_per_round BETWEEN 1 AND 32",
        name="ck_run_budget_ledgers_query_limit",
    ),
    CheckConstraint(
        "max_tool_attempts BETWEEN 1 AND 1000",
        name="ck_run_budget_ledgers_tool_limit",
    ),
    CheckConstraint(
        "max_model_attempts BETWEEN 1 AND 1000",
        name="ck_run_budget_ledgers_model_limit",
    ),
    CheckConstraint(
        "max_embedding_attempts BETWEEN 0 AND 1000",
        name="ck_run_budget_ledgers_embedding_limit",
    ),
    CheckConstraint(
        "max_retry_per_operation BETWEEN 0 AND 20",
        name="ck_run_budget_ledgers_retry_limit",
    ),
    CheckConstraint(
        "evidence_top_k BETWEEN 1 AND 100",
        name="ck_run_budget_ledgers_evidence_limit",
    ),
    CheckConstraint(
        "retrieval_rounds_used BETWEEN 0 AND max_retrieval_rounds",
        name="ck_run_budget_ledgers_retrieval_used",
    ),
    CheckConstraint(
        "queries_used BETWEEN 0 AND (max_retrieval_rounds * max_queries_per_round)",
        name="ck_run_budget_ledgers_queries_used",
    ),
    CheckConstraint(
        "tool_attempts_used BETWEEN 0 AND max_tool_attempts",
        name="ck_run_budget_ledgers_tool_used",
    ),
    CheckConstraint(
        "model_attempts_used BETWEEN 0 AND max_model_attempts",
        name="ck_run_budget_ledgers_model_used",
    ),
    CheckConstraint(
        "embedding_attempts_used BETWEEN 0 AND max_embedding_attempts",
        name="ck_run_budget_ledgers_embedding_used",
    ),
    CheckConstraint("revision >= 0", name="ck_run_budget_ledgers_revision"),
    CheckConstraint(
        "updated_at >= created_at",
        name="ck_run_budget_ledgers_timestamps",
    ),
)


run_external_attempts = Table(
    "run_external_attempts",
    metadata,
    Column("attempt_id", String(36), primary_key=True),
    Column(
        "run_id",
        String(36),
        ForeignKey(
            "runs.run_id",
            name="fk_run_external_attempts_run",
            ondelete="CASCADE",
        ),
        nullable=False,
    ),
    Column("lease_epoch", BigInteger, nullable=False),
    Column("operation_key", String(255), nullable=False),
    Column("operation_kind", String(32), nullable=False),
    Column("operation_name", String(64), nullable=False),
    Column("attempt_no", Integer, nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("provider_request_id", String(255), nullable=True),
    Column("status", String(32), nullable=False, server_default="reserved"),
    Column("retryable", Boolean, nullable=True),
    Column("error_code", String(64), nullable=True),
    Column("result_ref", String(255), nullable=True),
    Column("result_hash", String(64), nullable=True),
    Column(
        "reserved_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column("dispatched_at", DateTime(timezone=True), nullable=True),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("lease_epoch >= 0", name="ck_run_external_attempts_lease_epoch"),
    CheckConstraint(
        "btrim(operation_key) <> '' AND btrim(operation_name) <> ''",
        name="ck_run_external_attempts_operation",
    ),
    CheckConstraint(
        "operation_kind IN ('model', 'embedding', 'tool')",
        name="ck_run_external_attempts_kind",
    ),
    CheckConstraint("attempt_no > 0", name="ck_run_external_attempts_attempt_no"),
    CheckConstraint(
        "request_hash ~ '^[0-9a-f]{64}$'",
        name="ck_run_external_attempts_request_hash",
    ),
    CheckConstraint(
        "result_hash IS NULL OR result_hash ~ '^[0-9a-f]{64}$'",
        name="ck_run_external_attempts_result_hash",
    ),
    CheckConstraint(
        "status IN ('reserved', 'dispatched', 'succeeded', 'failed', "
        "'outcome_unknown', 'abandoned_before_dispatch')",
        name="ck_run_external_attempts_status",
    ),
    CheckConstraint(
        "((result_ref IS NULL AND result_hash IS NULL) OR "
        "(result_ref IS NOT NULL AND result_hash IS NOT NULL))",
        name="ck_run_external_attempts_result_pair",
    ),
    CheckConstraint(
        "(dispatched_at IS NULL OR dispatched_at >= reserved_at) AND "
        "(completed_at IS NULL OR completed_at >= "
        "COALESCE(dispatched_at, reserved_at))",
        name="ck_run_external_attempts_timestamps",
    ),
    CheckConstraint(
        "((status = 'reserved' AND dispatched_at IS NULL AND completed_at IS NULL "
        "AND provider_request_id IS NULL AND retryable IS NULL "
        "AND error_code IS NULL AND result_ref IS NULL) OR "
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
        "(status = 'abandoned_before_dispatch' AND dispatched_at IS NULL "
        "AND completed_at IS NOT NULL AND provider_request_id IS NULL "
        "AND retryable IS NOT NULL AND error_code IS NOT NULL "
        "AND result_ref IS NULL))",
        name="ck_run_external_attempts_lifecycle",
    ),
    UniqueConstraint(
        "run_id",
        "operation_key",
        "attempt_no",
        name="uq_run_external_attempts_operation_attempt",
    ),
)

Index(
    "ix_run_external_attempts_recovery",
    run_external_attempts.c.run_id,
    run_external_attempts.c.status,
    run_external_attempts.c.reserved_at,
)
Index(
    "ix_run_external_attempts_provider_request",
    run_external_attempts.c.provider_request_id,
    postgresql_where=run_external_attempts.c.provider_request_id.is_not(None),
)


run_node_artifacts = Table(
    "run_node_artifacts",
    metadata,
    Column("artifact_id", String(36), primary_key=True),
    Column(
        "run_id",
        String(36),
        ForeignKey(
            "runs.run_id",
            name="fk_run_node_artifacts_run",
            ondelete="CASCADE",
        ),
        nullable=False,
    ),
    Column("node_name", String(64), nullable=False),
    Column("artifact_kind", String(32), nullable=False),
    Column("artifact_ref", String(255), nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("payload", JSONB, nullable=True),
    Column("lease_epoch", BigInteger, nullable=False),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    CheckConstraint(
        "btrim(node_name) <> '' AND btrim(artifact_ref) <> ''",
        name="ck_run_node_artifacts_identity",
    ),
    CheckConstraint(
        "artifact_kind IN ('retrieval', 'verification', 'terminal')",
        name="ck_run_node_artifacts_kind",
    ),
    CheckConstraint(
        "payload_hash ~ '^[0-9a-f]{64}$'",
        name="ck_run_node_artifacts_payload_hash",
    ),
    CheckConstraint(
        "payload IS NULL OR jsonb_typeof(payload) = 'object'",
        name="ck_run_node_artifacts_payload",
    ),
    CheckConstraint("lease_epoch >= 0", name="ck_run_node_artifacts_lease_epoch"),
    UniqueConstraint(
        "run_id",
        "node_name",
        "artifact_kind",
        "artifact_ref",
        name="uq_run_node_artifacts_identity",
    ),
)

Index(
    "ix_run_node_artifacts_node_created",
    run_node_artifacts.c.run_id,
    run_node_artifacts.c.node_name,
    run_node_artifacts.c.created_at,
)


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


run_checkpoints = Table(
    "run_checkpoints",
    metadata,
    Column(
        "run_id",
        String(36),
        ForeignKey(
            "runs.run_id",
            name="fk_run_checkpoints_run",
            ondelete="CASCADE",
        ),
        primary_key=True,
    ),
    Column("checkpoint_namespace", String(255), primary_key=True),
    Column("checkpoint_id", String(255), primary_key=True),
    Column("parent_checkpoint_id", String(255), nullable=True),
    Column("schema_version", Integer, nullable=False),
    Column("graph_version", String(64), nullable=False),
    Column("retrieval_config_hash", String(64), nullable=False),
    Column("last_completed_node", String(64), nullable=True),
    Column("state_payload", JSONB, nullable=False),
    Column("state_hash", String(64), nullable=False),
    Column("lease_epoch", BigInteger, nullable=False),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    CheckConstraint(
        "btrim(checkpoint_namespace) <> '' AND btrim(checkpoint_id) <> ''",
        name="ck_run_checkpoints_identity",
    ),
    CheckConstraint(
        "parent_checkpoint_id IS NULL OR parent_checkpoint_id <> checkpoint_id",
        name="ck_run_checkpoints_parent_not_self",
    ),
    CheckConstraint("schema_version > 0", name="ck_run_checkpoints_schema_version"),
    CheckConstraint("lease_epoch >= 0", name="ck_run_checkpoints_lease_epoch"),
    CheckConstraint(
        "retrieval_config_hash ~ '^[0-9a-f]{64}$'",
        name="ck_run_checkpoints_config_hash",
    ),
    CheckConstraint(
        "state_hash ~ '^[0-9a-f]{64}$'",
        name="ck_run_checkpoints_state_hash",
    ),
    CheckConstraint(
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
        "AND state_payload -> 'answer_draft_ref' = 'null'::jsonb",
        name="ck_run_checkpoints_state_payload",
    ),
    ForeignKeyConstraint(
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
)

Index(
    "ix_run_checkpoints_run_created",
    run_checkpoints.c.run_id,
    run_checkpoints.c.created_at,
)
Index(
    "ix_run_checkpoints_lease",
    run_checkpoints.c.run_id,
    run_checkpoints.c.checkpoint_namespace,
    run_checkpoints.c.lease_epoch,
)


messages = Table(
    "messages",
    metadata,
    Column("message_id", String(36), primary_key=True),
    Column("session_id", String(36), nullable=False),
    Column("user_id", String(128), nullable=False),
    Column("role", String(16), nullable=False),
    Column("content", Text, nullable=False),
    Column("run_id", String(36), nullable=False),
    Column("ordinal", BigInteger, nullable=False),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    CheckConstraint("role IN ('user', 'assistant')", name="ck_messages_role"),
    CheckConstraint("ordinal > 0", name="ck_messages_ordinal"),
    ForeignKeyConstraint(
        ["session_id", "user_id"],
        ["sessions.session_id", "sessions.user_id"],
        name="fk_messages_session_owner",
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["run_id", "session_id", "user_id"],
        ["runs.run_id", "runs.session_id", "runs.user_id"],
        name="fk_messages_run_session_owner",
        ondelete="CASCADE",
    ),
    UniqueConstraint("session_id", "ordinal", name="uq_messages_session_ordinal"),
    UniqueConstraint("run_id", "role", name="uq_messages_run_role"),
    UniqueConstraint(
        "message_id", "run_id", "role", name="uq_messages_identity_run_role"
    ),
)

Index("ix_messages_session_order", messages.c.session_id, messages.c.ordinal)


run_results = Table(
    "run_results",
    metadata,
    Column(
        "run_id",
        String(36),
        primary_key=True,
    ),
    Column(
        "final_message_id",
        String(36),
        nullable=False,
        unique=True,
    ),
    Column(
        "final_message_role",
        String(16),
        nullable=False,
        server_default="assistant",
    ),
    Column("answer_payload", JSONB, nullable=False),
    Column("evidence_payload", JSONB, nullable=False),
    Column("verification_payload", JSONB, nullable=False),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    CheckConstraint(
        "final_message_role = 'assistant'",
        name="ck_run_results_final_message_role",
    ),
    ForeignKeyConstraint(
        ["run_id"],
        ["runs.run_id"],
        name="fk_run_results_run",
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["final_message_id", "run_id", "final_message_role"],
        ["messages.message_id", "messages.run_id", "messages.role"],
        name="fk_run_results_final_message",
        ondelete="CASCADE",
    ),
)


idempotency_keys = Table(
    "idempotency_keys",
    metadata,
    Column("user_id", String(128), primary_key=True),
    Column("session_id", String(36), primary_key=True),
    Column("idempotency_key", String(255), primary_key=True),
    Column("request_hash", String(64), nullable=False),
    Column("run_id", String(36), nullable=False),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["session_id", "user_id"],
        ["sessions.session_id", "sessions.user_id"],
        name="fk_idempotency_keys_session_owner",
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["run_id", "session_id", "user_id"],
        ["runs.run_id", "runs.session_id", "runs.user_id"],
        name="fk_idempotency_keys_run_session_owner",
        ondelete="CASCADE",
    ),
)

Index("ix_idempotency_keys_expires", idempotency_keys.c.expires_at)


run_events = Table(
    "run_events",
    metadata,
    Column(
        "run_id",
        String(36),
        ForeignKey("runs.run_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("sequence", BigInteger, primary_key=True),
    Column("event_type", String(64), nullable=False),
    Column("safe_payload", JSONB, nullable=False),
    Column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=func.now()
    ),
    CheckConstraint("sequence > 0", name="ck_run_events_sequence"),
    CheckConstraint(
        "event_type IN ('run.queued', 'run.started', 'retrieval.completed', "
        "'generation.started', 'verification.completed', 'answer.final', "
        "'run.failed', 'run.cancelled', 'run.interrupted', "
        "'run.resume_requested', 'run.resumed', 'attempt.outcome_unknown', "
        "'run.completed_with_limits', 'run.needs_clarification')",
        name="ck_run_events_type",
    ),
)

Index("ix_run_events_created", run_events.c.run_id, run_events.c.created_at)


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
