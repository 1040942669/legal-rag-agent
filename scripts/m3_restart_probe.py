from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from math import sqrt
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence

import numpy as np
from alembic.runtime.migration import MigrationContext
from sqlalchemy import func, select, text

from legal_rag.chunking import article_chunks
from legal_rag.embeddings import (
    EMBEDDING_CACHE_SCHEMA_VERSION,
    EmbeddingCache,
    EmbeddingModelConfig,
    chunk_corpus_fingerprint,
    embedding_contract_fingerprint,
)
from legal_rag.models import LawArticle
from legal_rag.storage.ann import (
    AnnSearchPolicy,
    PostgresAnnRetrievalRepository,
    PostgresHnswIndexManager,
)
from legal_rag.storage.catalog import PostgresLegalCatalogRepository
from legal_rag.storage.contracts import LawVersionSpec, build_storage_import_bundle
from legal_rag.storage.database import DatabaseSettings, create_database_engine
from legal_rag.storage.migrations import upgrade_database
from legal_rag.storage.repository import PostgresCorpusRepository
from legal_rag.storage.retrieval import (
    PostgresExactRetrievalRepository,
    RetrievalFilters,
)
from legal_rag.storage.schema import (
    chunk_embeddings,
    embedding_imports,
    embedding_profile_generations,
    index_builds,
    snapshot_activation_events,
)


RECEIPT_SCHEMA_VERSION = 1
EXPECTED_MIGRATION_REVISION = "0004_m3_ann_guards"
FIXTURE_PREFIX = "m3-restart"
FIXTURE_SCOPE_ID = "scope-m3-service-restart"
FIXTURE_SNAPSHOT_ID = "snapshot-m3-service-restart-v1"
FIXTURE_LAW_ID = "m3-restart-law"
FIXTURE_VERSION_ID = "m3-restart-law-v1"
FIXTURE_TITLE = "M3 Service Restart Test Law"
FIXTURE_QUERY_VECTOR = [1.0, 0.0, 0.0]
FIXTURE_TOP_K = 3


def _unit_vector(inner_product: float) -> list[float]:
    return [inner_product, sqrt(max(0.0, 1.0 - inner_product**2)), 0.0]


def _fixture_bundle():
    articles = [
        LawArticle(
            article_id=f"{FIXTURE_PREFIX}-article-{index}",
            law_name=FIXTURE_TITLE,
            article_number=f"Article {index}",
            body=f"Synthetic restart verification text {index}.",
            raw_text=(f"Article {index} Synthetic restart verification text {index}."),
            source_file="fixtures/m3-service-restart.txt",
            line_no=index,
            parse_status="from_filename",
        )
        for index in range(1, 9)
    ]
    chunks = article_chunks(articles)
    model = EmbeddingModelConfig(
        key="m3-service-restart-fixture",
        provider="fixture",
        model_name="fixture/m3-service-restart",
        role="retrieval",
        revision="m3-service-restart-v1",
        normalize=True,
        dimensions=3,
    )
    scores = (1.0, 0.96, 0.92, 0.84, 0.76, 0.68, 0.60, 0.52)
    vectors = np.asarray([_unit_vector(score) for score in scores], dtype=np.float32)
    cache = EmbeddingCache(
        cache_dir=Path("fixture-cache"),
        chunk_ids=[chunk.chunk_id for chunk in chunks],
        vectors=vectors,
        metadata={
            "schema_version": EMBEDDING_CACHE_SCHEMA_VERSION,
            "chunk_count": len(chunks),
            "vector_count": len(chunks),
            "dimension": 3,
            "dtype": "float32",
            "chunk_strategy": chunks[0].strategy,
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
        snapshot_id=FIXTURE_SNAPSHOT_ID,
        scope_id=FIXTURE_SCOPE_ID,
        source_manifest={
            "schema_version": 1,
            "fixture": "m3-service-restart",
            "snapshot_id": FIXTURE_SNAPSHOT_ID,
        },
        laws=[
            LawVersionSpec(
                law_id=FIXTURE_LAW_ID,
                version_id=FIXTURE_VERSION_ID,
                title=FIXTURE_TITLE,
                verification_status="verified",
                source_ref="fixtures/m3-service-restart.txt",
                valid_from="2024-01-01",
            )
        ],
        articles=articles,
        chunks=chunks,
        embedding_cache=cache,
        model_config=model,
        model_revision=model.revision,
        chunk_recipe={"fixture": "m3-service-restart", "strategy": chunks[0].strategy},
        article_version_ids={
            article.article_id: FIXTURE_VERSION_ID for article in articles
        },
    )


def _database_facts(engine) -> dict[str, Any]:
    with engine.connect() as connection:
        revision = MigrationContext.configure(connection).get_current_revision()
        pgvector_version = connection.scalar(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
        server_version_num = connection.scalar(text("SHOW server_version_num"))
    if revision != EXPECTED_MIGRATION_REVISION:
        raise RuntimeError(
            f"unexpected migration revision: {revision!r}; "
            f"expected {EXPECTED_MIGRATION_REVISION!r}"
        )
    if not isinstance(pgvector_version, str) or not pgvector_version:
        raise RuntimeError("pgvector extension version is unavailable")
    try:
        postgres_major = int(str(server_version_num)) // 10_000
    except (TypeError, ValueError) as exc:
        raise RuntimeError("PostgreSQL server version is invalid") from exc
    return {
        "migration_revision": revision,
        "pgvector_version": pgvector_version,
        "postgres_major": postgres_major,
    }


def _result_ids(results: Sequence[Any]) -> list[str]:
    return [str(result.chunk.chunk_id) for result in results]


def _write_receipt(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary_path = Path(handle.name)
    try:
        with handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _load_receipt(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("restart probe receipt is missing or invalid") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("restart probe receipt must be a JSON object")
    if payload.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise RuntimeError("restart probe receipt schema version is unsupported")
    if payload.get("stage") != "prepared_before_service_restart":
        raise RuntimeError("restart probe receipt stage is invalid")
    return payload


def _prepare(receipt_path: Path) -> dict[str, Any]:
    engine = create_database_engine(DatabaseSettings.from_env())
    try:
        upgrade_database(engine)
        facts = _database_facts(engine)
        bundle = _fixture_bundle()
        profile = bundle.embedding_profile.to_identity()
        filters = RetrievalFilters(
            scope_id=bundle.snapshot.scope_id,
            snapshot_id=bundle.snapshot.snapshot_id,
            profile_id=profile.profile_id,
        )
        import_result = PostgresCorpusRepository(engine).import_bundle(bundle)
        catalog = PostgresLegalCatalogRepository(engine)
        activation = catalog.activate_snapshot(
            scope_id=bundle.snapshot.scope_id,
            snapshot_id=bundle.snapshot.snapshot_id,
            expected_current_snapshot_id=None,
            required_profile_id=profile.profile_id,
            actor="m3-restart-probe",
            reason="synthetic M3 service restart verification",
        )
        exact_results = PostgresExactRetrievalRepository(engine).search_vector(
            FIXTURE_QUERY_VECTOR,
            top_k=FIXTURE_TOP_K,
            filters=filters,
            expected_profile=profile,
        )
        lookup = catalog.lookup_active_article(
            scope_id=bundle.snapshot.scope_id,
            law_title=FIXTURE_TITLE,
            article_number="Article 1",
            version_id=FIXTURE_VERSION_ID,
        )
        if lookup.status != "found" or lookup.match is None:
            raise RuntimeError(
                "synthetic catalog lookup did not resolve before restart"
            )
        build = PostgresHnswIndexManager(engine).ensure_index(
            filters=filters,
            expected_profile=profile,
        )
        ann_outcome = PostgresAnnRetrievalRepository(engine).search_vector(
            FIXTURE_QUERY_VECTOR,
            top_k=FIXTURE_TOP_K,
            filters=filters,
            expected_profile=profile,
            build=build,
            policy=AnnSearchPolicy(exact_fallback_on_underfill=True),
        )
        exact_ids = _result_ids(exact_results)
        ann_ids = _result_ids(ann_outcome.results)
        if len(exact_ids) != FIXTURE_TOP_K or ann_ids != exact_ids:
            raise RuntimeError("ANN and exact fixture results disagree before restart")
        payload: dict[str, Any] = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "stage": "prepared_before_service_restart",
            "database": facts,
            "snapshot": {
                "scope_id": bundle.snapshot.scope_id,
                "snapshot_id": bundle.snapshot.snapshot_id,
                "bundle_hash": bundle.bundle_hash,
                "corpus_hash": bundle.corpus_hash,
            },
            "profile": asdict(profile),
            "import_result": asdict(import_result),
            "activation": {
                "revision": activation.selection.revision,
                "activation_id": activation.selection.activation_id,
                "activated_at": activation.selection.activated_at.isoformat(),
                "operation": activation.event.operation,
            },
            "ann_build": asdict(build),
            "expected": {
                "exact_chunk_ids": exact_ids,
                "ann_chunk_ids": ann_ids,
                "catalog_article_id": lookup.match.article_id,
                "catalog_version_id": lookup.match.version_id,
            },
        }
        _write_receipt(receipt_path, payload)
        return payload
    finally:
        engine.dispose()


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"restart probe receipt field {label!r} is invalid")
    return value


def _verify(receipt_path: Path) -> dict[str, Any]:
    receipt = _load_receipt(receipt_path)
    expected_snapshot = _require_mapping(receipt.get("snapshot"), "snapshot")
    expected_activation = _require_mapping(receipt.get("activation"), "activation")
    expected_build = _require_mapping(receipt.get("ann_build"), "ann_build")
    expected_results = _require_mapping(receipt.get("expected"), "expected")
    expected_database = _require_mapping(receipt.get("database"), "database")

    engine = create_database_engine(DatabaseSettings.from_env())
    try:
        facts = _database_facts(engine)
        if facts != expected_database:
            raise RuntimeError(
                "database version or migration revision changed on restart"
            )
        bundle = _fixture_bundle()
        profile = bundle.embedding_profile.to_identity()
        if bundle.bundle_hash != expected_snapshot.get("bundle_hash"):
            raise RuntimeError("synthetic bundle identity changed across restart")
        if bundle.corpus_hash != expected_snapshot.get("corpus_hash"):
            raise RuntimeError("synthetic corpus identity changed across restart")
        if asdict(profile) != receipt.get("profile"):
            raise RuntimeError("embedding profile identity changed across restart")

        repeated_import = PostgresCorpusRepository(engine).import_bundle(bundle)
        if repeated_import.imported or repeated_import.snapshot_created:
            raise RuntimeError(
                "restart verification unexpectedly re-imported fixture data"
            )
        filters = RetrievalFilters(
            scope_id=bundle.snapshot.scope_id,
            snapshot_id=bundle.snapshot.snapshot_id,
            profile_id=profile.profile_id,
        )
        catalog = PostgresLegalCatalogRepository(engine)
        active = catalog.get_active_snapshot(bundle.snapshot.scope_id)
        if (
            active.snapshot_id != bundle.snapshot.snapshot_id
            or active.revision != expected_activation.get("revision")
            or active.activation_id != expected_activation.get("activation_id")
            or active.activated_at.isoformat()
            != expected_activation.get("activated_at")
        ):
            raise RuntimeError("active snapshot pointer changed across service restart")
        history = catalog.list_activation_history(bundle.snapshot.scope_id)
        if (
            len(history) != 1
            or history[0].activation_id != active.activation_id
            or history[0].operation != expected_activation.get("operation")
        ):
            raise RuntimeError("activation ledger changed across service restart")
        lookup = catalog.lookup_active_article(
            scope_id=bundle.snapshot.scope_id,
            law_title=FIXTURE_TITLE,
            article_number="Article 1",
            version_id=FIXTURE_VERSION_ID,
        )
        if (
            lookup.status != "found"
            or lookup.match is None
            or lookup.match.article_id != expected_results.get("catalog_article_id")
            or lookup.match.version_id != expected_results.get("catalog_version_id")
        ):
            raise RuntimeError("catalog result changed across service restart")

        exact_results = PostgresExactRetrievalRepository(engine).search_vector(
            FIXTURE_QUERY_VECTOR,
            top_k=FIXTURE_TOP_K,
            filters=filters,
            expected_profile=profile,
        )
        exact_ids = _result_ids(exact_results)
        if exact_ids != expected_results.get("exact_chunk_ids"):
            raise RuntimeError("exact retrieval result changed across service restart")
        build = PostgresHnswIndexManager(engine).ensure_index(
            filters=filters,
            expected_profile=profile,
        )
        build_values = asdict(build)
        for field_name in (
            "build_id",
            "snapshot_id",
            "profile_id",
            "dimensions",
            "profile_embedding_count",
            "profile_embedding_manifest_hash",
            "physical_instance_id",
            "pgvector_version",
            "postgres_major",
            "physical_index_name",
            "index_params_hash",
            "status",
        ):
            if build_values[field_name] != expected_build.get(field_name):
                raise RuntimeError(
                    f"ANN build field {field_name!r} changed across service restart"
                )
        if not build.reused:
            raise RuntimeError(
                "ANN physical instance was rebuilt after service restart"
            )
        ann_outcome = PostgresAnnRetrievalRepository(engine).search_vector(
            FIXTURE_QUERY_VECTOR,
            top_k=FIXTURE_TOP_K,
            filters=filters,
            expected_profile=profile,
            build=build,
            policy=AnnSearchPolicy(exact_fallback_on_underfill=True),
        )
        ann_ids = _result_ids(ann_outcome.results)
        if ann_ids != expected_results.get("ann_chunk_ids") or ann_ids != exact_ids:
            raise RuntimeError("ANN retrieval result changed across service restart")

        with engine.connect() as connection:
            embedding_count = connection.scalar(
                select(func.count())
                .select_from(chunk_embeddings)
                .where(chunk_embeddings.c.profile_id == profile.profile_id)
            )
            import_count = connection.scalar(
                select(func.count())
                .select_from(embedding_imports)
                .where(
                    embedding_imports.c.snapshot_id == bundle.snapshot.snapshot_id,
                    embedding_imports.c.profile_id == profile.profile_id,
                    embedding_imports.c.bundle_hash == bundle.bundle_hash,
                    embedding_imports.c.status == "validated",
                )
            )
            generation = (
                connection.execute(
                    select(embedding_profile_generations).where(
                        embedding_profile_generations.c.profile_id == profile.profile_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            build_count = connection.scalar(
                select(func.count())
                .select_from(index_builds)
                .where(
                    index_builds.c.build_id == build.build_id,
                    index_builds.c.status.in_(("validated", "active")),
                )
            )
            event_count = connection.scalar(
                select(func.count())
                .select_from(snapshot_activation_events)
                .where(
                    snapshot_activation_events.c.scope_id == bundle.snapshot.scope_id,
                    snapshot_activation_events.c.activation_id == active.activation_id,
                )
            )
        if embedding_count != len(bundle.embeddings) or import_count != 1:
            raise RuntimeError("embedding import state did not survive service restart")
        if generation is None:
            raise RuntimeError("embedding generation state is missing after restart")
        if (
            generation["embedding_count"] != build.profile_embedding_count
            or generation["embedding_manifest_hash"]
            != build.profile_embedding_manifest_hash
            or generation["ann_physical_instance_id"] != build.physical_instance_id
            or generation["ann_physical_index_name"] != build.physical_index_name
            or generation["ann_index_params_hash"] != build.index_params_hash
        ):
            raise RuntimeError("ANN generation binding changed across service restart")
        if build_count != 1 or event_count != 1:
            raise RuntimeError(
                "ANN receipt or activation event is missing after restart"
            )

        return {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "stage": "verified_after_service_restart",
            "database": facts,
            "snapshot_id": bundle.snapshot.snapshot_id,
            "profile_id": profile.profile_id,
            "activation_id": active.activation_id,
            "activation_revision": active.revision,
            "ann_build_id": build.build_id,
            "ann_physical_index_name": build.physical_index_name,
            "ann_status": ann_outcome.status,
            "exact_chunk_ids": exact_ids,
            "ann_chunk_ids": ann_ids,
        }
    finally:
        engine.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare or verify the synthetic M3 PostgreSQL restart fixture."
    )
    parser.add_argument("phase", choices=("prepare", "verify"))
    parser.add_argument("--receipt", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = (
        _prepare(args.receipt) if args.phase == "prepare" else _verify(args.receipt)
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
