from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
import uuid

import numpy as np
import pytest
from alembic import command
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, create_engine, inspect, make_url, select, text, update

from legal_rag.chunking import article_chunks
from legal_rag.embeddings import (
    EMBEDDING_CACHE_SCHEMA_VERSION,
    EmbeddingCache,
    EmbeddingModelConfig,
    chunk_corpus_fingerprint,
    embedding_contract_fingerprint,
)
from legal_rag.models import LawArticle
from legal_rag.storage.contracts import LawVersionSpec, build_storage_import_bundle
from legal_rag.storage.database import DatabaseSettings, create_database_engine
from legal_rag.storage.migrations import alembic_config, upgrade_database
from legal_rag.storage.repository import (
    ImportConflictError,
    PostgresCorpusRepository,
)
from legal_rag.storage.schema import chunk_embeddings, chunks, corpus_snapshots


def test_database_settings_repr_redacts_the_url() -> None:
    settings = DatabaseSettings(
        "postgresql+psycopg://fixture-user:supersecret@127.0.0.1/legal_rag_m3_test"
    )

    assert "supersecret" not in repr(settings)
    assert "supersecret" not in settings.redacted_url
    assert "***" in settings.redacted_url


def _fixture_bundle(
    *,
    prefix: str,
    snapshot_id: str,
    vectors: list[list[float]] | None = None,
    model_revision: str = "fixture-revision-1",
):
    body = "第一条正文。"
    articles = [
        LawArticle(
            article_id=f"{prefix}-article-1",
            law_name="集成测试法",
            article_number="第一条",
            body=body,
            raw_text=f"第一条 {body}",
            source_file="fixtures/integration-law.txt",
            line_no=1,
            parse_status="from_filename",
        ),
        LawArticle(
            article_id=f"{prefix}-article-2",
            law_name="集成测试法",
            article_number="第二条",
            body="第二条正文。",
            raw_text="第二条 第二条正文。",
            source_file="fixtures/integration-law.txt",
            line_no=2,
            parse_status="from_filename",
        ),
    ]
    chunks = article_chunks(articles)
    model = EmbeddingModelConfig(
        key="m3-integration-fixture",
        provider="fixture",
        model_name="fixture/model",
        role="retrieval",
        revision=model_revision,
        normalize=True,
        dimensions=3,
    )
    vector_matrix = np.asarray(
        vectors or [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32
    )
    cache = EmbeddingCache(
        cache_dir=Path("fixture-cache"),
        chunk_ids=[item.chunk_id for item in chunks],
        vectors=vector_matrix,
        metadata={
            "schema_version": EMBEDDING_CACHE_SCHEMA_VERSION,
            "chunk_count": 2,
            "vector_count": 2,
            "dimension": 3,
            "dtype": "float32",
            "chunk_strategy": "article",
            "chunk_fingerprint": chunk_corpus_fingerprint(chunks),
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
        scope_id="m3-integration-public",
        source_manifest={
            "schema_version": 1,
            "fixture": "m3-storage",
            "snapshot_id": snapshot_id,
        },
        laws=[
            LawVersionSpec(
                law_id=f"{prefix}-law",
                version_id=f"{prefix}-law-v1",
                title="集成测试法",
                verification_status="unknown",
                source_ref="fixtures/integration-law.txt",
            )
        ],
        articles=articles,
        chunks=chunks,
        embedding_cache=cache,
        model_config=model,
        model_revision=model_revision,
        chunk_recipe={"strategy": "article"},
    )


def test_empty_database_upgrades_to_head_with_vector_extension(
    migrated_engine: Engine,
) -> None:
    expected_tables = {
        "active_snapshot_pointers",
        "chunk_articles",
        "chunk_embeddings",
        "chunks",
        "corpus_snapshots",
        "embedding_profiles",
        "embedding_imports",
        "index_builds",
        "law_articles",
        "law_versions",
        "snapshot_chunks",
        "snapshot_activation_events",
    }
    inspector = inspect(migrated_engine)
    assert expected_tables <= set(inspector.get_table_names())
    assert "ix_chunk_articles_article_chunk" in {
        item["name"] for item in inspector.get_indexes("chunk_articles")
    }
    with migrated_engine.connect() as connection:
        assert MigrationContext.configure(connection).get_current_revision() == (
            "0003_m3_activation"
        )
        extension_version = connection.scalar(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
        command.check(alembic_config(connection=connection))
    assert extension_version


def test_legacy_active_pointers_with_delimiter_ambiguity_upgrade_distinctly(
    integration_database_url: str,
) -> None:
    parsed_url = make_url(integration_database_url)
    database_name = f"legal_rag_m3_test_legacy_{uuid.uuid4().hex[:12]}"
    database_url = parsed_url.set(database=database_name)
    admin_engine = create_engine(
        parsed_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    legacy_engine: Engine | None = None

    try:
        with admin_engine.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
        legacy_engine = create_database_engine(
            DatabaseSettings(database_url.render_as_string(hide_password=False))
        )
        upgrade_database(legacy_engine, "0002_m3_immutable_rows")

        legacy_timestamp = "2026-09-25 00:00:00+00"
        with legacy_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO corpus_snapshots (
                        snapshot_id,
                        scope_id,
                        source_manifest,
                        source_manifest_hash,
                        corpus_hash
                    ) VALUES
                        ('c', 'a:b', '{}'::jsonb, repeat('a', 64), repeat('b', 64)),
                        ('b:c', 'a', '{}'::jsonb, repeat('c', 64), repeat('d', 64))
                    """
                )
            )
            connection.execute(
                text(
                    """
                    UPDATE corpus_snapshots
                    SET status = 'validated',
                        validated_at = CAST(:legacy_timestamp AS timestamptz)
                    WHERE snapshot_id IN ('c', 'b:c')
                    """
                ),
                {"legacy_timestamp": legacy_timestamp},
            )
            connection.execute(
                text(
                    """
                    UPDATE corpus_snapshots
                    SET status = 'active',
                        activated_at = CAST(:legacy_timestamp AS timestamptz)
                    WHERE snapshot_id IN ('c', 'b:c')
                    """
                ),
                {"legacy_timestamp": legacy_timestamp},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO active_snapshot_pointers (
                        scope_id,
                        snapshot_id,
                        updated_at
                    ) VALUES
                        ('a:b', 'c', CAST(:legacy_timestamp AS timestamptz)),
                        ('a', 'b:c', CAST(:legacy_timestamp AS timestamptz))
                    """
                ),
                {"legacy_timestamp": legacy_timestamp},
            )

        upgrade_database(legacy_engine)

        with legacy_engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        """
                    SELECT
                        pointer.scope_id,
                        pointer.snapshot_id,
                        pointer.revision,
                        pointer.activation_id,
                        pointer.updated_at,
                        event.operation,
                        event.target_snapshot_id,
                        event.occurred_at
                    FROM active_snapshot_pointers AS pointer
                    JOIN snapshot_activation_events AS event
                      ON event.scope_id = pointer.scope_id
                     AND event.revision = pointer.revision
                     AND event.activation_id = pointer.activation_id
                     AND event.target_snapshot_id = pointer.snapshot_id
                    ORDER BY pointer.scope_id
                    """
                    )
                )
                .mappings()
                .all()
            )
            command.check(alembic_config(connection=connection))

        assert len(rows) == 2
        assert {row["scope_id"] for row in rows} == {"a", "a:b"}
        assert {row["snapshot_id"] for row in rows} == {"c", "b:c"}
        assert {row["revision"] for row in rows} == {1}
        assert {row["operation"] for row in rows} == {"migration_bootstrap"}
        assert all(row["target_snapshot_id"] == row["snapshot_id"] for row in rows)
        assert len({row["activation_id"] for row in rows}) == 2
        assert all(row["occurred_at"] == row["updated_at"] for row in rows)
    finally:
        if legacy_engine is not None:
            legacy_engine.dispose()
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


def test_import_is_idempotent_and_preserves_hash_count_and_dimension(
    migrated_engine: Engine,
) -> None:
    repository = PostgresCorpusRepository(migrated_engine)
    bundle = _fixture_bundle(prefix="idempotent", snapshot_id="m3-idempotent-v1")
    counts_before = repository.table_counts()

    first = repository.import_bundle(bundle)
    counts_after_first = repository.table_counts()
    second = repository.import_bundle(bundle)

    assert first.imported is True
    assert first.snapshot_created is True
    assert second.imported is False
    assert second.snapshot_created is False
    assert first.bundle_hash == bundle.bundle_hash == second.bundle_hash
    assert repository.table_counts() == counts_after_first
    assert {
        name: counts_after_first[name] - counts_before[name] for name in counts_before
    } == {
        "corpus_snapshots": 1,
        "law_versions": 1,
        "law_articles": 2,
        "chunks": 2,
        "chunk_articles": 2,
        "snapshot_chunks": 2,
        "embedding_profiles": 1,
        "chunk_embeddings": 2,
        "embedding_imports": 1,
    }

    with migrated_engine.connect() as connection:
        stored = connection.execute(
            select(
                chunk_embeddings.c.chunk_id,
                chunk_embeddings.c.embedding,
                chunk_embeddings.c.embedding_dimension,
                chunk_embeddings.c.embedding_hash,
            )
            .where(
                chunk_embeddings.c.chunk_id.in_(
                    [item.chunk_id for item in bundle.embeddings]
                )
            )
            .order_by(chunk_embeddings.c.chunk_id)
        ).all()
    assert [row.embedding_dimension for row in stored] == [3, 3]
    expected_vectors = {
        item.chunk_id: np.asarray(item.embedding, dtype=np.float32)
        for item in bundle.embeddings
    }
    assert all(
        np.array_equal(
            np.asarray(row.embedding, dtype=np.float32), expected_vectors[row.chunk_id]
        )
        for row in stored
    )
    assert sorted(row.embedding_hash for row in stored) == sorted(
        item.embedding_hash for item in bundle.embeddings
    )


def test_conflicting_immutable_content_rolls_back_without_new_snapshot(
    migrated_engine: Engine,
) -> None:
    repository = PostgresCorpusRepository(migrated_engine)
    baseline = _fixture_bundle(prefix="rollback", snapshot_id="m3-rollback-v1")
    repository.import_bundle(baseline)
    before = repository.table_counts()
    conflicting = _fixture_bundle(
        prefix="rollback",
        snapshot_id="m3-rollback-v2",
        vectors=[[1.0, 0.0, 0.0], [0.0, 0.0, 1.0]],
    )

    with pytest.raises(ImportConflictError, match="embedding.*conflicting"):
        repository.import_bundle(conflicting)

    assert repository.table_counts() == before
    with migrated_engine.connect() as connection:
        assert (
            connection.scalar(
                select(corpus_snapshots.c.snapshot_id).where(
                    corpus_snapshots.c.snapshot_id == "m3-rollback-v2"
                )
            )
            is None
        )


def test_concurrent_retry_is_serialized_and_idempotent(
    migrated_engine: Engine,
) -> None:
    repository = PostgresCorpusRepository(migrated_engine)
    bundle = _fixture_bundle(prefix="concurrent", snapshot_id="m3-concurrent-v1")
    before = repository.table_counts()
    barrier = Barrier(2)

    def import_once():
        barrier.wait(timeout=5)
        return repository.import_bundle(bundle)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: import_once(), range(2)))

    after = repository.table_counts()
    assert sorted(result.imported for result in results) == [False, True]
    assert after["corpus_snapshots"] - before["corpus_snapshots"] == 1
    assert after["law_versions"] - before["law_versions"] == 1
    assert after["law_articles"] - before["law_articles"] == 2
    assert after["chunks"] - before["chunks"] == 2
    assert after["snapshot_chunks"] - before["snapshot_chunks"] == 2
    assert after["chunk_embeddings"] - before["chunk_embeddings"] == 2
    assert after["embedding_imports"] - before["embedding_imports"] == 1


def test_same_snapshot_accepts_a_distinct_embedding_profile(
    migrated_engine: Engine,
) -> None:
    repository = PostgresCorpusRepository(migrated_engine)
    first_bundle = _fixture_bundle(
        prefix="multi-profile",
        snapshot_id="m3-multi-profile-v1",
        model_revision="fixture-revision-a",
    )
    second_bundle = _fixture_bundle(
        prefix="multi-profile",
        snapshot_id="m3-multi-profile-v1",
        model_revision="fixture-revision-b",
    )
    repository.import_bundle(first_bundle)
    before = repository.table_counts()

    result = repository.import_bundle(second_bundle)
    repeat = repository.import_bundle(second_bundle)
    after = repository.table_counts()

    assert result.imported is True
    assert result.snapshot_created is False
    assert repeat.imported is False
    assert after["corpus_snapshots"] == before["corpus_snapshots"]
    assert after["snapshot_chunks"] == before["snapshot_chunks"]
    assert after["embedding_profiles"] - before["embedding_profiles"] == 1
    assert after["chunk_embeddings"] - before["chunk_embeddings"] == 2
    assert after["embedding_imports"] - before["embedding_imports"] == 1


def test_database_trigger_rejects_profile_dimension_mismatch(
    migrated_engine: Engine,
) -> None:
    repository = PostgresCorpusRepository(migrated_engine)
    bundle = _fixture_bundle(prefix="dimension", snapshot_id="m3-dimension-v1")
    repository.import_bundle(bundle)

    with pytest.raises(Exception, match="does not match profile dimension"):
        with migrated_engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE chunk_embeddings DISABLE TRIGGER "
                    "trg_chunk_embeddings_immutable"
                )
            )
            connection.execute(
                update(chunk_embeddings)
                .where(
                    chunk_embeddings.c.chunk_id == bundle.embeddings[0].chunk_id,
                    chunk_embeddings.c.profile_id
                    == bundle.embedding_profile.profile_id,
                )
                .values(embedding_dimension=2)
            )

    with migrated_engine.connect() as connection:
        assert (
            connection.scalar(
                select(chunk_embeddings.c.embedding_dimension).where(
                    chunk_embeddings.c.chunk_id == bundle.embeddings[0].chunk_id,
                    chunk_embeddings.c.profile_id
                    == bundle.embedding_profile.profile_id,
                )
            )
            == 3
        )


def test_repeat_import_detects_persisted_text_tampering(
    migrated_engine: Engine,
) -> None:
    repository = PostgresCorpusRepository(migrated_engine)
    bundle = _fixture_bundle(prefix="tamper", snapshot_id="m3-tamper-v1")
    repository.import_bundle(bundle)
    target = bundle.chunks[0]

    with migrated_engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE chunks DISABLE TRIGGER trg_chunks_immutable")
        )
        connection.execute(
            update(chunks)
            .where(chunks.c.chunk_id == target.chunk_id)
            .values(text="tampered database text")
        )
        connection.execute(
            text("ALTER TABLE chunks ENABLE TRIGGER trg_chunks_immutable")
        )
    try:
        with pytest.raises(ImportConflictError, match="immutable content in text"):
            repository.import_bundle(bundle)
    finally:
        with migrated_engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE chunks DISABLE TRIGGER trg_chunks_immutable")
            )
            connection.execute(
                update(chunks)
                .where(chunks.c.chunk_id == target.chunk_id)
                .values(text=target.text)
            )
            connection.execute(
                text("ALTER TABLE chunks ENABLE TRIGGER trg_chunks_immutable")
            )


def test_reconnect_preserves_validated_snapshot_and_counts(
    integration_database_url: str, migrated_engine: Engine
) -> None:
    repository = PostgresCorpusRepository(migrated_engine)
    bundle = _fixture_bundle(prefix="reconnect", snapshot_id="m3-reconnect-v1")
    repository.import_bundle(bundle)
    before = repository.table_counts()
    migrated_engine.dispose()
    replacement = create_database_engine(DatabaseSettings(integration_database_url))
    try:
        repository = PostgresCorpusRepository(replacement)
        assert repository.table_counts() == before
        with replacement.connect() as connection:
            status = connection.scalar(
                select(corpus_snapshots.c.status).where(
                    corpus_snapshots.c.snapshot_id == "m3-reconnect-v1"
                )
            )
        assert status == "validated"
    finally:
        replacement.dispose()
