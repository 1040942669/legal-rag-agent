from __future__ import annotations

import re
from importlib import import_module, resources
from importlib.metadata import files as distribution_files
from io import StringIO
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql

from legal_rag.storage import schema
from legal_rag.storage.migrations import alembic_config


M4_TABLE_COLUMNS = {
    "sessions": (
        "session_id",
        "user_id",
        "title",
        "status",
        "created_at",
        "updated_at",
    ),
    "runs": (
        "run_id",
        "session_id",
        "user_id",
        "status",
        "request_hash",
        "request_payload",
        "scope_id",
        "snapshot_id",
        "snapshot_revision",
        "activation_id",
        "profile_id",
        "boundary_fingerprint",
        "retrieval_config_hash",
        "graph_version",
        "created_at",
        "queued_at",
        "started_at",
        "finished_at",
        "updated_at",
        "error_code",
        "revision",
        "event_sequence",
        "lease_owner",
        "lease_expires_at",
    ),
    "messages": (
        "message_id",
        "session_id",
        "user_id",
        "role",
        "content",
        "run_id",
        "ordinal",
        "created_at",
    ),
    "run_results": (
        "run_id",
        "final_message_id",
        "final_message_role",
        "answer_payload",
        "evidence_payload",
        "verification_payload",
        "created_at",
    ),
    "idempotency_keys": (
        "user_id",
        "session_id",
        "idempotency_key",
        "request_hash",
        "run_id",
        "created_at",
        "expires_at",
    ),
    "run_events": (
        "run_id",
        "sequence",
        "event_type",
        "safe_payload",
        "created_at",
    ),
}

M4_PRIMARY_KEYS = {
    "sessions": ("session_id",),
    "runs": ("run_id",),
    "messages": ("message_id",),
    "run_results": ("run_id",),
    "idempotency_keys": ("user_id", "session_id", "idempotency_key"),
    "run_events": ("run_id", "sequence"),
}

M4_RUN_EVENT_TYPES = frozenset(
    {
        "run.queued",
        "run.started",
        "retrieval.completed",
        "generation.started",
        "verification.completed",
        "answer.final",
        "run.failed",
        "run.cancelled",
        "run.interrupted",
    }
)
M5_RUN_EVENT_TYPES = frozenset(
    {
        "run.resume_requested",
        "run.resumed",
        "attempt.outcome_unknown",
        "run.completed_with_limits",
        "run.needs_clarification",
    }
)
M4_INDEX_NAMES = frozenset(
    {
        "ix_sessions_user_created",
        "ix_runs_user_created",
        "ix_runs_status_queued",
        "uq_runs_one_active_per_session",
        "ix_messages_session_order",
        "ix_idempotency_keys_expires",
        "ix_run_events_created",
    }
)


def _normalize_sql(value: object) -> str:
    return " ".join(str(value).split())


def _check_constraints(table) -> dict[str | None, str]:
    return {
        constraint.name: _normalize_sql(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }


def _unique_constraints(table) -> frozenset[tuple[str | None, tuple[str, ...]]]:
    return frozenset(
        (constraint.name, tuple(column.name for column in constraint.columns))
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    )


def _foreign_keys(
    table,
) -> frozenset[tuple[str | None, tuple[str, ...], tuple[str, ...], str | None]]:
    return frozenset(
        (
            constraint.name,
            tuple(column.name for column in constraint.columns),
            tuple(element.target_fullname for element in constraint.elements),
            constraint.ondelete,
        )
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    )


def _migration_sql() -> str:
    output = StringIO()
    context = MigrationContext.configure(
        url="postgresql://",
        opts={
            "as_sql": True,
            "literal_binds": True,
            "output_buffer": output,
        },
    )
    migration = import_module("legal_rag.storage.alembic.versions.0005_m4_api_sessions")
    with Operations.context(context):
        migration.upgrade()
    return output.getvalue()


def test_m4_metadata_declares_complete_durable_service_tables() -> None:
    for table_name, expected_columns in M4_TABLE_COLUMNS.items():
        table = schema.metadata.tables[table_name]

        assert getattr(schema, table_name) is table
        assert (
            tuple(
                column_name
                for column_name in table.c.keys()
                if column_name in expected_columns
            )
            == expected_columns
        )
        assert (
            tuple(column.name for column in table.primary_key.columns)
            == (M4_PRIMARY_KEYS[table_name])
        )

    assert schema.sessions.c.title.nullable is True
    assert schema.runs.c.started_at.nullable is True
    assert schema.runs.c.finished_at.nullable is True
    assert schema.runs.c.error_code.nullable is True
    assert schema.runs.c.lease_owner.nullable is True
    assert schema.runs.c.lease_expires_at.nullable is True

    for table_name in M4_TABLE_COLUMNS:
        table = schema.metadata.tables[table_name]
        nullable_columns = {
            column_name
            for column_name in M4_TABLE_COLUMNS[table_name]
            if table.c[column_name].nullable
        }
        if table_name == "sessions":
            assert nullable_columns == {"title"}
        elif table_name == "runs":
            assert nullable_columns == {
                "started_at",
                "finished_at",
                "error_code",
                "lease_owner",
                "lease_expires_at",
            }
        else:
            assert nullable_columns == set()


def test_m4_constraints_enforce_owner_order_idempotency_and_result_binding() -> None:
    assert _unique_constraints(schema.sessions) == frozenset(
        {("uq_sessions_identity_owner", ("session_id", "user_id"))}
    )
    assert _unique_constraints(schema.runs) == frozenset(
        {
            (
                "uq_runs_identity_session_owner",
                ("run_id", "session_id", "user_id"),
            )
        }
    )
    assert _unique_constraints(schema.messages) == frozenset(
        {
            ("uq_messages_session_ordinal", ("session_id", "ordinal")),
            ("uq_messages_run_role", ("run_id", "role")),
            (
                "uq_messages_identity_run_role",
                ("message_id", "run_id", "role"),
            ),
        }
    )
    assert _unique_constraints(schema.run_results) == frozenset(
        {(None, ("final_message_id",))}
    )

    assert _foreign_keys(schema.runs) >= frozenset(
        {
            (
                "fk_runs_session_owner",
                ("session_id", "user_id"),
                ("sessions.session_id", "sessions.user_id"),
                "CASCADE",
            ),
            (
                "fk_runs_snapshot_activation",
                ("scope_id", "snapshot_revision", "activation_id", "snapshot_id"),
                (
                    "snapshot_activation_events.scope_id",
                    "snapshot_activation_events.revision",
                    "snapshot_activation_events.activation_id",
                    "snapshot_activation_events.target_snapshot_id",
                ),
                None,
            ),
            (
                "fk_runs_embedding_profile",
                ("profile_id",),
                ("embedding_profiles.profile_id",),
                None,
            ),
        }
    )
    assert _foreign_keys(schema.messages) == frozenset(
        {
            (
                "fk_messages_session_owner",
                ("session_id", "user_id"),
                ("sessions.session_id", "sessions.user_id"),
                "CASCADE",
            ),
            (
                "fk_messages_run_session_owner",
                ("run_id", "session_id", "user_id"),
                ("runs.run_id", "runs.session_id", "runs.user_id"),
                "CASCADE",
            ),
        }
    )
    assert _foreign_keys(schema.idempotency_keys) == frozenset(
        {
            (
                "fk_idempotency_keys_session_owner",
                ("session_id", "user_id"),
                ("sessions.session_id", "sessions.user_id"),
                "CASCADE",
            ),
            (
                "fk_idempotency_keys_run_session_owner",
                ("run_id", "session_id", "user_id"),
                ("runs.run_id", "runs.session_id", "runs.user_id"),
                "CASCADE",
            ),
        }
    )
    assert _foreign_keys(schema.run_results) == frozenset(
        {
            ("fk_run_results_run", ("run_id",), ("runs.run_id",), "CASCADE"),
            (
                "fk_run_results_final_message",
                ("final_message_id", "run_id", "final_message_role"),
                ("messages.message_id", "messages.run_id", "messages.role"),
                "CASCADE",
            ),
        }
    )

    assert _check_constraints(schema.sessions) == {
        "ck_sessions_status": "status IN ('active', 'archived')"
    }
    assert _check_constraints(schema.messages) == {
        "ck_messages_role": "role IN ('user', 'assistant')",
        "ck_messages_ordinal": "ordinal > 0",
    }
    assert _check_constraints(schema.run_results) == {
        "ck_run_results_final_message_role": "final_message_role = 'assistant'"
    }
    run_checks = _check_constraints(schema.runs)
    assert run_checks["ck_runs_revision"] == "revision >= 0"
    assert run_checks["ck_runs_event_sequence"] == "event_sequence >= 0"
    assert frozenset(
        re.findall(r"'([^']+)'", run_checks["ck_runs_status"])
    ) == frozenset(
        {
            "queued",
            "running",
            "interrupted",
            "succeeded",
            "completed_with_limits",
            "needs_clarification",
            "failed",
            "cancelled",
        }
    )
    assert "completed_with_limits" in run_checks["ck_runs_finished_at"]
    assert "needs_clarification" in run_checks["ck_runs_finished_at"]


def test_m4_indexes_cover_service_queries_and_single_active_run() -> None:
    expected_indexes = {
        "sessions": {"ix_sessions_user_created": (False, ("user_id", "created_at"))},
        "runs": {
            "ix_runs_user_created": (False, ("user_id", "created_at")),
            "ix_runs_status_queued": (False, ("status", "queued_at")),
            "uq_runs_one_active_per_session": (True, ("session_id",)),
        },
        "messages": {"ix_messages_session_order": (False, ("session_id", "ordinal"))},
        "run_results": {},
        "idempotency_keys": {"ix_idempotency_keys_expires": (False, ("expires_at",))},
        "run_events": {"ix_run_events_created": (False, ("run_id", "created_at"))},
    }

    for table_name, expected in expected_indexes.items():
        table = schema.metadata.tables[table_name]
        actual = {
            index.name: (
                index.unique,
                tuple(column.name for column in index.columns),
            )
            for index in table.indexes
        }
        assert expected.items() <= actual.items()

    active_index = next(
        index
        for index in schema.runs.indexes
        if index.name == "uq_runs_one_active_per_session"
    )
    predicate = active_index.dialect_options["postgresql"]["where"]
    compiled_predicate = predicate.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    assert _normalize_sql(compiled_predicate) == (
        "runs.status IN ('queued', 'running', 'interrupted')"
    )


def test_run_event_allowlist_is_closed_and_matches_packaged_migration() -> None:
    event_check = _check_constraints(schema.run_events)["ck_run_events_type"]
    assert frozenset(re.findall(r"'([^']+)'", event_check)) == (
        M4_RUN_EVENT_TYPES | M5_RUN_EVENT_TYPES
    )
    assert _check_constraints(schema.run_events)["ck_run_events_sequence"] == (
        "sequence > 0"
    )

    migration_sql = _migration_sql()
    migration_check = re.search(
        r"CONSTRAINT ck_run_events_type CHECK \((.*?)\)",
        migration_sql,
        flags=re.DOTALL,
    )
    assert migration_check is not None
    assert frozenset(re.findall(r"'([^']+)'", migration_check.group(1))) == (
        M4_RUN_EVENT_TYPES
    )
    assert "generation.token" not in migration_sql
    assert "answer.draft" not in migration_sql


def test_m4_migration_remains_packaged_below_the_current_single_head() -> None:
    config = alembic_config()
    scripts = ScriptDirectory.from_config(config)
    m4_migration = import_module(
        "legal_rag.storage.alembic.versions.0005_m4_api_sessions"
    )

    assert scripts.get_heads() == ["0008_execution_money"]
    m4_revision = scripts.get_revision("0005_m4_api_sessions")
    m5_revision = scripts.get_revision("0006_m5_harness_recovery")
    head = scripts.get_revision("0007_m6_jobs_outbox")
    current = scripts.get_revision("0008_execution_money")
    assert m4_revision is not None
    assert m5_revision is not None
    assert head is not None
    assert m4_revision.down_revision == "0004_m3_ann_guards"
    assert m5_revision.down_revision == m4_revision.revision
    assert head.down_revision == m5_revision.revision
    assert current is not None and current.down_revision == head.revision
    assert m4_migration.revision == m4_revision.revision
    assert m4_migration.down_revision == m4_revision.down_revision

    alembic_package = resources.files("legal_rag.storage.alembic")
    packaged_m4_migration = alembic_package.joinpath(
        "versions", "0005_m4_api_sessions.py"
    )
    packaged_m5_migration = alembic_package.joinpath(
        "versions", "0006_m5_harness_recovery.py"
    )
    packaged_m6_migration = alembic_package.joinpath("versions", "0007_m6_jobs_outbox.py")
    assert alembic_package.joinpath("script.py.mako").is_file()
    assert packaged_m4_migration.is_file()
    assert packaged_m5_migration.is_file()
    assert packaged_m6_migration.is_file()
    assert alembic_package.joinpath("versions", "0008_execution_money.py").is_file()
    assert (
        Path(m4_revision.path).resolve() == Path(str(packaged_m4_migration)).resolve()
    )
    assert Path(m5_revision.path).resolve() == Path(str(packaged_m5_migration)).resolve()
    assert Path(head.path).resolve() == Path(str(packaged_m6_migration)).resolve()

    installed_files = distribution_files("legal-rag-assistant")
    assert installed_files is not None
    installed_paths = {
        str(installed_file).replace("\\", "/") for installed_file in installed_files
    }
    assert {
        "legal_rag/storage/alembic/script.py.mako",
        "legal_rag/storage/alembic/versions/0005_m4_api_sessions.py",
        "legal_rag/storage/alembic/versions/0006_m5_harness_recovery.py",
    } <= installed_paths

    migration_sql = _migration_sql()
    normalized_migration_sql = _normalize_sql(migration_sql)
    assert tuple(re.findall(r"CREATE TABLE (\w+)", migration_sql)) == tuple(
        M4_TABLE_COLUMNS
    )
    assert (
        frozenset(re.findall(r"CREATE (?:UNIQUE )?INDEX (\w+)", migration_sql))
        == M4_INDEX_NAMES
    )
    assert (
        "CONSTRAINT ck_runs_status CHECK (status IN ('queued', 'running', "
        "'interrupted', 'succeeded', 'failed', 'cancelled'))"
        in normalized_migration_sql
    )
    assert "completed_with_limits" not in migration_sql
    assert "needs_clarification" not in migration_sql
    for constraint_name in (
        "uq_runs_identity_session_owner",
        "fk_messages_run_session_owner",
        "uq_messages_identity_run_role",
        "fk_run_results_final_message",
        "fk_idempotency_keys_run_session_owner",
    ):
        assert f"CONSTRAINT {constraint_name}" in migration_sql
