from __future__ import annotations

import re
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import numpy as np
import pytest
from alembic import command
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, create_engine, inspect, make_url, select, text
from sqlalchemy.exc import DBAPIError

from legal_rag.chunking import article_chunks
from legal_rag.embeddings import (
    EMBEDDING_CACHE_SCHEMA_VERSION,
    EmbeddingCache,
    EmbeddingModelConfig,
    chunk_corpus_fingerprint,
    embedding_contract_fingerprint,
)
from legal_rag.models import LawArticle
from legal_rag.storage.catalog import PostgresLegalCatalogRepository
from legal_rag.storage.contracts import LawVersionSpec, build_storage_import_bundle
from legal_rag.storage.database import DatabaseSettings, create_database_engine
from legal_rag.storage.migrations import alembic_config, upgrade_database
from legal_rag.storage.repository import PostgresCorpusRepository
from legal_rag.storage.retrieval import (
    PostgresExactRetrievalRepository,
    RetrievalFilters,
)
from legal_rag.storage.schema import (
    chunk_embeddings,
    chunks,
    corpus_snapshots,
)


LEGACY_REVISION = "0002_m3_immutable_rows"
HEAD_REVISION = "0003_m3_activation"


@contextmanager
def _temporary_database(base_url: str, purpose: str) -> Iterator[Engine]:
    parsed = make_url(base_url)
    database_name = f"legal_rag_m3_test_{purpose}_{uuid.uuid4().hex[:10]}"
    if not re.fullmatch(r"[a-z0-9_]+", database_name):
        raise AssertionError("generated integration database name is unsafe")
    admin_engine = create_engine(
        parsed.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    engine: Engine | None = None
    try:
        with admin_engine.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
        database_url = parsed.set(database=database_name).render_as_string(
            hide_password=False
        )
        engine = create_database_engine(DatabaseSettings(database_url))
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        with admin_engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            connection.exec_driver_sql(
                f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'
            )
        admin_engine.dispose()


def _downgrade_database(engine: Engine, revision: str) -> None:
    with engine.begin() as connection:
        command.downgrade(alembic_config(connection=connection), revision)


def _current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def _fixture_bundle(*, prefix: str, scope_id: str, snapshot_id: str):
    article = LawArticle(
        article_id=f"{prefix}-article-1",
        law_name=f"{prefix}迁移测试法",
        article_number="第一条",
        body=f"{prefix}迁移韧性测试正文。",
        raw_text=f"第一条 {prefix}迁移韧性测试正文。",
        source_file=f"fixtures/{prefix}.txt",
        line_no=1,
        parse_status="from_filename",
    )
    resolved_chunks = article_chunks([article])
    model = EmbeddingModelConfig(
        key="m3-migration-resilience-fixture",
        provider="fixture",
        model_name="fixture/migration-resilience",
        role="retrieval",
        revision="fixture-revision-1",
        normalize=True,
        dimensions=3,
    )
    cache = EmbeddingCache(
        cache_dir=Path("fixture-cache"),
        chunk_ids=[item.chunk_id for item in resolved_chunks],
        vectors=np.asarray([[1.0, 0.0, 0.0]], dtype=np.float32),
        metadata={
            "schema_version": EMBEDDING_CACHE_SCHEMA_VERSION,
            "chunk_count": 1,
            "vector_count": 1,
            "dimension": 3,
            "dtype": "float32",
            "chunk_strategy": "article",
            "chunk_fingerprint": chunk_corpus_fingerprint(resolved_chunks),
            "embedding_key": model.key,
            "provider": model.provider,
            "model_name": model.model_name,
            "revision": model.revision,
            "normalize": model.normalize,
            "trust_remote_code": model.trust_remote_code,
            "query_prefix": model.query_prefix,
            "document_prefix": model.document_prefix,
            "embed_with_metadata": model.embed_with_metadata,
            "embedding_contract_fingerprint": embedding_contract_fingerprint(model),
        },
    )
    return build_storage_import_bundle(
        snapshot_id=snapshot_id,
        scope_id=scope_id,
        source_manifest={
            "schema_version": 1,
            "fixture": "m3-migration-resilience",
            "snapshot_id": snapshot_id,
        },
        laws=[
            LawVersionSpec(
                law_id=f"{prefix}-law",
                version_id=f"{prefix}-law-v1",
                title=article.law_name,
                verification_status="verified",
                source_ref=article.source_file,
            )
        ],
        articles=[article],
        chunks=resolved_chunks,
        embedding_cache=cache,
        model_config=model,
        model_revision=model.revision,
        chunk_recipe={"strategy": "article"},
    )


def _insert_legacy_inconsistency(engine: Engine, case: str) -> None:
    activated_at = "2026-09-25 00:00:00+00"
    pointer_at = (
        "2026-09-25 00:00:01+00" if case == "timestamp_mismatch" else activated_at
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO corpus_snapshots (
                    snapshot_id,
                    scope_id,
                    source_manifest,
                    source_manifest_hash,
                    corpus_hash
                ) VALUES (
                    'legacy-snapshot',
                    'legacy-scope',
                    '{}'::jsonb,
                    repeat('a', 64),
                    repeat('b', 64)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                UPDATE corpus_snapshots
                SET status = 'validated',
                    validated_at = CAST(:activated_at AS timestamptz)
                WHERE snapshot_id = 'legacy-snapshot'
                """
            ),
            {"activated_at": activated_at},
        )
        if case != "pointer_to_non_active":
            connection.execute(
                text(
                    """
                    UPDATE corpus_snapshots
                    SET status = 'active',
                        activated_at = CAST(:activated_at AS timestamptz)
                    WHERE snapshot_id = 'legacy-snapshot'
                    """
                ),
                {"activated_at": activated_at},
            )
        if case == "active_with_null_activated_at":
            connection.exec_driver_sql(
                "ALTER TABLE corpus_snapshots "
                "DISABLE TRIGGER trg_corpus_snapshots_guarded"
            )
            connection.execute(
                text(
                    "UPDATE corpus_snapshots SET activated_at = NULL "
                    "WHERE snapshot_id = 'legacy-snapshot'"
                )
            )
            connection.exec_driver_sql(
                "ALTER TABLE corpus_snapshots "
                "ENABLE TRIGGER trg_corpus_snapshots_guarded"
            )
        if case != "active_without_pointer":
            connection.execute(
                text(
                    """
                    INSERT INTO active_snapshot_pointers (
                        scope_id,
                        snapshot_id,
                        updated_at
                    ) VALUES (
                        'legacy-scope',
                        'legacy-snapshot',
                        CAST(:pointer_at AS timestamptz)
                    )
                    """
                ),
                {"pointer_at": pointer_at},
            )


def _legacy_state(engine: Engine) -> tuple[dict[str, object], dict[str, object] | None]:
    with engine.connect() as connection:
        snapshot = dict(
            connection.execute(
                text(
                    "SELECT snapshot_id, scope_id, status, validated_at, activated_at "
                    "FROM corpus_snapshots WHERE snapshot_id = 'legacy-snapshot'"
                )
            )
            .mappings()
            .one()
        )
        pointer_row = (
            connection.execute(
                text(
                    "SELECT scope_id, snapshot_id, updated_at "
                    "FROM active_snapshot_pointers WHERE scope_id = 'legacy-scope'"
                )
            )
            .mappings()
            .one_or_none()
        )
    return snapshot, dict(pointer_row) if pointer_row is not None else None


def _assert_no_0003_residue(engine: Engine) -> None:
    inspector = inspect(engine)
    assert "snapshot_activation_events" not in inspector.get_table_names()
    assert {
        item["name"] for item in inspector.get_columns("active_snapshot_pointers")
    }.isdisjoint({"revision", "activation_id"})
    assert "ix_chunk_articles_article_chunk" not in {
        item["name"] for item in inspector.get_indexes("chunk_articles")
    }
    with engine.connect() as connection:
        assert not connection.scalar(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_proc AS procedure
                    JOIN pg_namespace AS namespace
                      ON namespace.oid = procedure.pronamespace
                    WHERE namespace.nspname = current_schema()
                      AND procedure.proname =
                          'legal_rag_enforce_snapshot_activation_consistency'
                )
                """
            )
        )
        assert (
            connection.scalar(
                text(
                    """
                    SELECT count(*)
                    FROM pg_trigger
                    WHERE NOT tgisinternal
                      AND (
                          tgname LIKE '%_activation_consistency'
                          OR tgname = 'trg_snapshot_activation_events_immutable'
                      )
                    """
                )
            )
            == 0
        )


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("pointer_to_non_active", "inconsistent active snapshot pointer"),
        ("active_with_null_activated_at", "inconsistent active snapshot pointer"),
        ("timestamp_mismatch", "inconsistent active snapshot pointer"),
        ("active_without_pointer", "active snapshot without pointer"),
    ],
)
def test_inconsistent_legacy_upgrade_fails_atomically_without_0003_residue(
    integration_database_url: str,
    case: str,
    message: str,
) -> None:
    with _temporary_database(integration_database_url, "legacy_negative") as engine:
        upgrade_database(engine, LEGACY_REVISION)
        _insert_legacy_inconsistency(engine, case)
        before = _legacy_state(engine)

        with pytest.raises(DBAPIError, match=message) as exc_info:
            upgrade_database(engine)

        assert getattr(exc_info.value.orig, "sqlstate", None) == "55000"
        assert _current_revision(engine) == LEGACY_REVISION
        assert _legacy_state(engine) == before
        _assert_no_0003_residue(engine)


def test_downgrade_rejects_revisioned_activation_history_before_schema_changes(
    integration_database_url: str,
) -> None:
    with _temporary_database(integration_database_url, "unsafe_downgrade") as engine:
        upgrade_database(engine)
        scope_id = "scope-unsafe-downgrade"
        old_bundle = _fixture_bundle(
            prefix="unsafe-old",
            scope_id=scope_id,
            snapshot_id="snapshot-unsafe-old",
        )
        new_bundle = _fixture_bundle(
            prefix="unsafe-new",
            scope_id=scope_id,
            snapshot_id="snapshot-unsafe-new",
        )
        importer = PostgresCorpusRepository(engine)
        importer.import_bundle(old_bundle)
        importer.import_bundle(new_bundle)
        catalog = PostgresLegalCatalogRepository(engine)
        initial = catalog.activate_snapshot(
            scope_id=scope_id,
            snapshot_id=old_bundle.snapshot.snapshot_id,
            expected_current_snapshot_id=None,
            required_profile_id=old_bundle.embedding_profile.profile_id,
        )
        replacement = catalog.activate_snapshot(
            scope_id=scope_id,
            snapshot_id=new_bundle.snapshot.snapshot_id,
            expected_current_snapshot_id=initial.selection.snapshot_id,
            expected_current_revision=initial.selection.revision,
            expected_current_activation_id=initial.selection.activation_id,
            required_profile_id=new_bundle.embedding_profile.profile_id,
        )
        assert replacement.selection.revision == 2

        with pytest.raises(
            DBAPIError,
            match="back up the activation ledger.*approved manual migration",
        ) as exc_info:
            _downgrade_database(engine, LEGACY_REVISION)

        assert getattr(exc_info.value.orig, "sqlstate", None) == "55000"
        assert _current_revision(engine) == HEAD_REVISION
        assert catalog.get_active_snapshot(scope_id) == replacement.selection
        assert [
            item.revision for item in catalog.list_activation_history(scope_id)
        ] == [
            2,
            1,
        ]
        assert "snapshot_activation_events" in inspect(engine).get_table_names()


def _retrieval_signature(engine: Engine, bundle) -> tuple[object, ...]:
    filters = RetrievalFilters(
        scope_id=bundle.snapshot.scope_id,
        snapshot_id=bundle.snapshot.snapshot_id,
        profile_id=bundle.embedding_profile.profile_id,
    )
    results = PostgresExactRetrievalRepository(engine).search_vector(
        [1.0, 0.0, 0.0],
        top_k=1,
        filters=filters,
        expected_profile=bundle.embedding_profile.to_identity(),
    )
    assert len(results) == 1
    result = results[0]
    assert result.provenance is not None
    return (
        result.chunk.chunk_id,
        result.chunk.text,
        result.score,
        result.provenance.chunk_content_hash,
        result.provenance.embedding_hash,
        result.trace["boundary_fingerprint"],
    )


def _corpus_signature(engine: Engine, snapshot_id: str) -> tuple[object, ...]:
    with engine.connect() as connection:
        snapshot = connection.execute(
            select(
                corpus_snapshots.c.scope_id,
                corpus_snapshots.c.source_manifest_hash,
                corpus_snapshots.c.corpus_hash,
                corpus_snapshots.c.status,
            ).where(corpus_snapshots.c.snapshot_id == snapshot_id)
        ).one()
        chunk_rows = connection.execute(
            select(chunks.c.chunk_id, chunks.c.content_hash).order_by(chunks.c.chunk_id)
        ).all()
        embedding_rows = connection.execute(
            select(
                chunk_embeddings.c.chunk_id,
                chunk_embeddings.c.profile_id,
                chunk_embeddings.c.embedding_hash,
            ).order_by(chunk_embeddings.c.chunk_id)
        ).all()
    return snapshot, tuple(chunk_rows), tuple(embedding_rows)


def test_safe_revision_one_downgrade_and_reupgrade_preserve_data_and_retrieval(
    integration_database_url: str,
) -> None:
    with _temporary_database(integration_database_url, "safe_roundtrip") as engine:
        upgrade_database(engine)
        bundle = _fixture_bundle(
            prefix="safe-roundtrip",
            scope_id="scope-safe-roundtrip",
            snapshot_id="snapshot-safe-roundtrip",
        )
        PostgresCorpusRepository(engine).import_bundle(bundle)
        catalog = PostgresLegalCatalogRepository(engine)
        initial = catalog.activate_snapshot(
            scope_id=bundle.snapshot.scope_id,
            snapshot_id=bundle.snapshot.snapshot_id,
            expected_current_snapshot_id=None,
            required_profile_id=bundle.embedding_profile.profile_id,
            actor="migration-resilience-test",
            reason="safe revision-one downgrade fixture",
        )
        assert initial.selection.revision == 1
        assert initial.event.operation == "initial_activate"
        corpus_before = _corpus_signature(engine, bundle.snapshot.snapshot_id)
        retrieval_before = _retrieval_signature(engine, bundle)

        _downgrade_database(engine, LEGACY_REVISION)

        assert _current_revision(engine) == LEGACY_REVISION
        _assert_no_0003_residue(engine)
        assert _corpus_signature(engine, bundle.snapshot.snapshot_id) == corpus_before
        assert _retrieval_signature(engine, bundle) == retrieval_before

        upgrade_database(engine)

        assert _current_revision(engine) == HEAD_REVISION
        assert _corpus_signature(engine, bundle.snapshot.snapshot_id) == corpus_before
        assert _retrieval_signature(engine, bundle) == retrieval_before
        restored = PostgresLegalCatalogRepository(engine).get_active_snapshot(
            bundle.snapshot.scope_id
        )
        history = PostgresLegalCatalogRepository(engine).list_activation_history(
            bundle.snapshot.scope_id
        )
        assert restored.snapshot_id == bundle.snapshot.snapshot_id
        assert restored.revision == 1
        assert len(history) == 1
        assert history[0].operation == "migration_bootstrap"
        assert history[0].target_snapshot_id == bundle.snapshot.snapshot_id
        assert history[0].activation_id == restored.activation_id
        assert history[0].occurred_at == restored.activated_at
