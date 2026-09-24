from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass, replace
from math import sqrt
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from sqlalchemy import Engine, func, insert, text, update
from sqlalchemy.exc import DBAPIError

from legal_rag.adaptive import retrieve_adaptive
from legal_rag.chunking import article_chunks, chunk_from_articles
from legal_rag.embeddings import (
    EMBEDDING_CACHE_SCHEMA_VERSION,
    EmbeddingCache,
    EmbeddingModelConfig,
    chunk_corpus_fingerprint,
    embedding_contract_fingerprint,
)
from legal_rag.embedding_contracts import EmbeddingProfileIdentity
from legal_rag.models import LawArticle
from legal_rag.retrieval import BM25Retriever, CachedDenseRetriever, RRFHybridRetriever
from legal_rag.storage.contracts import LawVersionSpec, build_storage_import_bundle
from legal_rag.storage.database import DatabaseSettings, create_database_engine
from legal_rag.storage.repository import PostgresCorpusRepository
from legal_rag.storage.retrieval import (
    BoundaryBoundRetriever,
    PgVectorExactRetriever,
    PostgresExactRetrievalRepository,
    RetrievalContractError,
    RetrievalDataError,
    RetrievalFilters,
    RetrievalUnavailableError,
)
from legal_rag.storage.schema import (
    chunk_articles,
    chunk_embeddings,
    chunks as chunk_rows,
    corpus_snapshots,
    embedding_imports,
    law_articles,
    snapshot_chunks,
)


@dataclass(frozen=True)
class _ArticleSpec:
    law_id: str
    version_id: str
    title: str
    article_id: str
    article_number: str
    valid_from: str | None = None
    valid_to: str | None = None


class _FixedEncoder:
    def __init__(self, vector, profile) -> None:
        self.vector = np.asarray(vector, dtype=np.float32)
        self.profile = profile
        self.calls = 0

    def encode_query(self, text: str):
        self.calls += 1
        return self.vector.copy()


def _unit_vector(inner_product: float) -> list[float]:
    return [inner_product, sqrt(max(0.0, 1.0 - inner_product**2)), 0.0]


def _build_bundle(
    *,
    prefix: str,
    scope_id: str,
    snapshot_id: str,
    specs: list[_ArticleSpec],
    vectors: list[list[float]],
    model_revision: str = "exact-revision-1",
    chunks=None,
):
    articles = [
        LawArticle(
            article_id=spec.article_id,
            law_name=spec.title,
            article_number=spec.article_number,
            body=f"{spec.article_id} 正文。",
            raw_text=f"{spec.article_number} {spec.article_id} 正文。",
            source_file=f"fixtures/{spec.version_id}.txt",
            line_no=index,
            parse_status="from_filename",
        )
        for index, spec in enumerate(specs, start=1)
    ]
    resolved_chunks = article_chunks(articles) if chunks is None else chunks(articles)
    model = EmbeddingModelConfig(
        key="m3-exact-fixture",
        provider="fixture",
        model_name="fixture/exact-model",
        role="retrieval",
        revision=model_revision,
        normalize=True,
        dimensions=3,
    )
    matrix = np.asarray(vectors, dtype=np.float32)
    cache = EmbeddingCache(
        cache_dir=Path("fixture-cache"),
        chunk_ids=[chunk.chunk_id for chunk in resolved_chunks],
        vectors=matrix,
        metadata={
            "schema_version": EMBEDDING_CACHE_SCHEMA_VERSION,
            "chunk_count": len(resolved_chunks),
            "vector_count": len(resolved_chunks),
            "dimension": 3,
            "dtype": "float32",
            "chunk_strategy": resolved_chunks[0].strategy,
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
    law_specs: list[LawVersionSpec] = []
    seen_versions: set[str] = set()
    for spec in specs:
        if spec.version_id in seen_versions:
            continue
        seen_versions.add(spec.version_id)
        law_specs.append(
            LawVersionSpec(
                law_id=spec.law_id,
                version_id=spec.version_id,
                title=spec.title,
                verification_status="verified",
                source_ref=f"fixtures/{spec.version_id}.txt",
                valid_from=spec.valid_from,
                valid_to=spec.valid_to,
            )
        )
    return build_storage_import_bundle(
        snapshot_id=snapshot_id,
        scope_id=scope_id,
        source_manifest={
            "schema_version": 1,
            "fixture": "m3-exact-retrieval",
            "prefix": prefix,
            "snapshot_id": snapshot_id,
        },
        laws=law_specs,
        articles=articles,
        chunks=resolved_chunks,
        embedding_cache=cache,
        model_config=model,
        model_revision=model_revision,
        chunk_recipe={"strategy": resolved_chunks[0].strategy},
        article_version_ids={spec.article_id: spec.version_id for spec in specs},
    )


def _one_law_specs(prefix: str, count: int) -> list[_ArticleSpec]:
    return [
        _ArticleSpec(
            law_id=f"{prefix}-law",
            version_id=f"{prefix}-law-v1",
            title=f"{prefix}测试法",
            article_id=f"{prefix}-article-{index}",
            article_number=f"第{index}条",
            valid_from="2024-01-01",
        )
        for index in range(1, count + 1)
    ]


def test_pgvector_exact_matches_numpy_inner_product_and_stable_ties(
    migrated_engine: Engine,
) -> None:
    specs = _one_law_specs("equivalence", 5)

    def spoofed_chunks(articles):
        resolved = article_chunks(articles)
        resolved[0].metadata.update(
            {
                "snapshot_id": "spoofed-snapshot",
                "scope_id": "spoofed-scope",
                "access_scope_ids": ["spoofed-scope"],
                "profile_id": "spoofed-profile",
            }
        )
        return resolved

    matrix = np.asarray(
        [
            [1.0, 0.0, 0.0],
            _unit_vector(0.5),
            [0.0, 1.0, 0.0],
            [-1.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
        ],
        dtype=np.float32,
    )
    bundle = _build_bundle(
        prefix="equivalence",
        scope_id="scope-equivalence",
        snapshot_id="snapshot-equivalence-v1",
        specs=specs,
        vectors=matrix.tolist(),
        chunks=spoofed_chunks,
    )
    PostgresCorpusRepository(migrated_engine).import_bundle(bundle)
    filters = RetrievalFilters(
        scope_id=bundle.snapshot.scope_id,
        snapshot_id=bundle.snapshot.snapshot_id,
        profile_id=bundle.embedding_profile.profile_id,
    )
    query = np.asarray([1.0, 0.0, 0.0], dtype=np.float32)
    repository = PostgresExactRetrievalRepository(migrated_engine)
    profile = bundle.embedding_profile.to_identity()

    results = repository.search_vector(
        query,
        top_k=5,
        filters=filters,
        expected_profile=profile,
    )

    baseline = CachedDenseRetriever.__new__(CachedDenseRetriever)
    baseline.chunks = repository.load_bound_corpus(
        filters=filters, expected_profile=profile
    ).chunks
    baseline.cache = SimpleNamespace(vectors=matrix)
    baseline.encoder = _FixedEncoder(query, profile)
    baseline.model_config = SimpleNamespace(
        provider="fixture", normalize=profile.normalization
    )
    baseline.deprecated_penalty = 1.0
    baseline.deprecated_indexes = []
    baseline.known_law_hints = []
    baseline_results = baseline.retrieve("固定向量问题", top_k=5)
    assert [result.chunk.chunk_id for result in results] == [
        result.chunk.chunk_id for result in baseline_results
    ]
    np.testing.assert_allclose(
        [result.score for result in results],
        [result.score for result in baseline_results],
        rtol=1e-5,
        atol=1e-6,
    )
    assert [result.rank for result in results] == [1, 2, 3, 4, 5]
    assert results[-1].score == pytest.approx(-1.0)
    assert results[0].trace["score_kind"] == "inner_product"
    assert results[0].trace["pgvector_operator"] == "<#>"
    assert results[0].trace["boundary_fingerprint"] == filters.boundary_fingerprint
    assert results[0].chunk.metadata["snapshot_id"] == filters.snapshot_id
    assert results[0].chunk.metadata["scope_id"] == filters.scope_id
    assert results[0].chunk.metadata["access_scope_ids"] == [filters.scope_id]
    assert results[0].chunk.metadata["profile_id"] == filters.profile_id
    assert results[0].chunk.metadata["boundary_fingerprint"] == (
        filters.boundary_fingerprint
    )
    assert results[0].chunk.law_names == [specs[0].title]
    assert results[0].chunk.article_numbers == [specs[0].article_number]

    negative_filters = replace(
        filters,
        article_ids=(specs[0].article_id, specs[1].article_id),
    )
    negative_results = repository.search_vector(
        [-1.0, 0.0, 0.0],
        top_k=1,
        filters=negative_filters,
        expected_profile=profile,
    )
    assert [item.chunk.metadata["article_ids"][0] for item in negative_results] == [
        specs[1].article_id
    ]
    assert negative_results[0].score == pytest.approx(-0.5)

    encoder = _FixedEncoder(query, profile)
    retriever = PgVectorExactRetriever(
        repository,
        encoder=encoder,
        filters=filters,
    )
    with pytest.raises(RetrievalContractError, match="top_k"):
        retriever.retrieve("固定向量问题", top_k=True)
    assert encoder.calls == 0
    with pytest.raises(FrozenInstanceError):
        retriever._boundary = replace(filters, snapshot_id="rewritten-snapshot")
    adaptive = retrieve_adaptive(
        "固定向量问题",
        retriever,
        top_k=5,
        enabled=False,
        max_followup_rounds=0,
    )
    assert [item.chunk.chunk_id for item in adaptive.results] == [
        item.chunk.chunk_id for item in results
    ]
    assert all(
        item.trace["boundary_fingerprint"] == filters.boundary_fingerprint
        for item in adaptive.results
    )
    assert encoder.calls == 1


def test_filters_apply_before_limit_and_isolate_scope_snapshot_profile_and_version(
    migrated_engine: Engine,
) -> None:
    specs = [
        _ArticleSpec(
            "filter-law-a",
            "filter-law-a-v1",
            "过滤测试甲法",
            "filter-a-v1-article-1",
            "第一条",
            "2024-01-01",
            "2025-01-01",
        ),
        _ArticleSpec(
            "filter-law-a",
            "filter-law-a-v1",
            "过滤测试甲法",
            "filter-a-v1-article-2",
            "第二条",
            "2024-01-01",
            "2025-01-01",
        ),
        _ArticleSpec(
            "filter-law-a",
            "filter-law-a-v2",
            "过滤测试甲法",
            "filter-a-v2-article-1",
            "第一条",
            "2025-01-01",
            None,
        ),
        _ArticleSpec(
            "filter-law-a",
            "filter-law-a-v2",
            "过滤测试甲法",
            "filter-a-v2-article-2",
            "第二条",
            "2025-01-01",
            None,
        ),
        _ArticleSpec(
            "filter-law-b",
            "filter-law-b-v1",
            "过滤测试乙法",
            "filter-b-v1-article-1",
            "第一条",
            "2024-01-01",
            None,
        ),
        _ArticleSpec(
            "filter-law-b",
            "filter-law-b-v1",
            "过滤测试乙法",
            "filter-b-v1-article-2",
            "第二条",
            "2024-01-01",
            None,
        ),
    ]
    vectors = [
        _unit_vector(0.2),
        _unit_vector(0.1),
        _unit_vector(0.9),
        _unit_vector(0.8),
        [1.0, 0.0, 0.0],
        _unit_vector(0.95),
    ]
    target = _build_bundle(
        prefix="filter-target",
        scope_id="scope-filter-a",
        snapshot_id="snapshot-filter-a1",
        specs=specs,
        vectors=vectors,
    )
    importer = PostgresCorpusRepository(migrated_engine)
    importer.import_bundle(target)
    other_scope = _build_bundle(
        prefix="filter-other-scope",
        scope_id="scope-filter-b",
        snapshot_id="snapshot-filter-b1",
        specs=_one_law_specs("filter-other-scope", 2),
        vectors=[[1.0, 0.0, 0.0], _unit_vector(0.99)],
        model_revision="other-scope-only-revision",
    )
    other_snapshot = _build_bundle(
        prefix="filter-other-snapshot",
        scope_id="scope-filter-a",
        snapshot_id="snapshot-filter-a2",
        specs=_one_law_specs("filter-other-snapshot", 2),
        vectors=[[1.0, 0.0, 0.0], _unit_vector(0.99)],
    )
    importer.import_bundle(other_scope)
    importer.import_bundle(other_snapshot)
    second_profile = _build_bundle(
        prefix="filter-target",
        scope_id="scope-filter-a",
        snapshot_id="snapshot-filter-a1",
        specs=specs,
        vectors=list(reversed(vectors)),
        model_revision="exact-revision-2",
    )
    importer.import_bundle(second_profile)

    repository = PostgresExactRetrievalRepository(migrated_engine)
    v1_filters = RetrievalFilters(
        scope_id="scope-filter-a",
        snapshot_id="snapshot-filter-a1",
        profile_id=target.embedding_profile.profile_id,
        law_ids=("filter-law-a",),
        version_ids=("filter-law-a-v1",),
        effective_on="2024-06-01",
    )
    results = repository.search_vector(
        [1.0, 0.0, 0.0],
        top_k=2,
        filters=v1_filters,
        expected_profile=target.embedding_profile.to_identity(),
    )
    assert [item.chunk.metadata["article_ids"][0] for item in results] == [
        "filter-a-v1-article-1",
        "filter-a-v1-article-2",
    ]
    assert len(results) == 2
    assert all(item.chunk.metadata["scope_id"] == "scope-filter-a" for item in results)
    assert all(
        item.chunk.metadata["snapshot_id"] == "snapshot-filter-a1" for item in results
    )
    assert all(
        item.chunk.metadata["profile_id"] == target.embedding_profile.profile_id
        for item in results
    )

    exact_article = replace(
        v1_filters,
        article_ids=("filter-a-v1-article-2",),
        article_numbers=("第二条",),
    )
    assert [
        item.chunk.metadata["article_ids"][0]
        for item in repository.search_vector(
            [1.0, 0.0, 0.0],
            top_k=5,
            filters=exact_article,
            expected_profile=target.embedding_profile.to_identity(),
        )
    ] == ["filter-a-v1-article-2"]

    mismatched_relation = replace(
        v1_filters,
        version_ids=("filter-law-b-v1",),
        article_ids=("filter-a-v1-article-1",),
    )
    assert (
        repository.search_vector(
            [1.0, 0.0, 0.0],
            top_k=5,
            filters=mismatched_relation,
            expected_profile=target.embedding_profile.to_identity(),
        )
        == []
    )
    assert (
        repository.search_vector(
            [1.0, 0.0, 0.0],
            top_k=5,
            filters=replace(v1_filters, article_ids=()),
            expected_profile=target.embedding_profile.to_identity(),
        )
        == []
    )
    assert (
        repository.search_vector(
            [1.0, 0.0, 0.0],
            top_k=5,
            filters=replace(v1_filters, effective_on="2025-01-01"),
            expected_profile=target.embedding_profile.to_identity(),
        )
        == []
    )

    profile_two_filters = RetrievalFilters(
        scope_id="scope-filter-a",
        snapshot_id="snapshot-filter-a1",
        profile_id=second_profile.embedding_profile.profile_id,
    )
    profile_two_results = repository.search_vector(
        [1.0, 0.0, 0.0],
        top_k=1,
        filters=profile_two_filters,
        expected_profile=second_profile.embedding_profile.to_identity(),
    )
    assert profile_two_results[0].chunk.metadata["article_ids"] == [
        "filter-a-v1-article-2"
    ]

    with pytest.raises(RetrievalUnavailableError, match="requested scope"):
        repository.search_vector(
            [1.0, 0.0, 0.0],
            top_k=1,
            filters=replace(v1_filters, scope_id="scope-filter-b"),
            expected_profile=target.embedding_profile.to_identity(),
        )
    with pytest.raises(RetrievalUnavailableError, match="not validated"):
        repository.search_vector(
            [1.0, 0.0, 0.0],
            top_k=1,
            filters=replace(
                v1_filters, profile_id=other_scope.embedding_profile.profile_id
            ),
            expected_profile=other_scope.embedding_profile.to_identity(),
        )
    with pytest.raises(RetrievalContractError, match="dimension 2.*dimension 3"):
        repository.search_vector(
            [1.0, 0.0],
            top_k=1,
            filters=v1_filters,
            expected_profile=target.embedding_profile.to_identity(),
        )

    encoder = _FixedEncoder(
        [1.0, 0.0, 0.0],
        replace(target.embedding_profile.to_identity(), model="fixture/wrong-model"),
    )
    with pytest.raises(RetrievalContractError, match="query encoder profile"):
        PgVectorExactRetriever(
            repository,
            encoder=encoder,
            filters=RetrievalFilters(
                scope_id="scope-filter-a",
                snapshot_id="snapshot-filter-a1",
                profile_id=target.embedding_profile.profile_id,
            ),
        )
    assert encoder.calls == 0

    with pytest.raises(DBAPIError, match="immutable corpus snapshot identity"):
        with migrated_engine.begin() as connection:
            connection.execute(
                update(corpus_snapshots)
                .where(corpus_snapshots.c.snapshot_id == target.snapshot.snapshot_id)
                .values(scope_id="scope-tampered")
            )

    with pytest.raises(DBAPIError, match="membership is closed"):
        with migrated_engine.begin() as connection:
            connection.execute(
                insert(snapshot_chunks).values(
                    snapshot_id=target.snapshot.snapshot_id,
                    chunk_id=other_scope.chunks[0].chunk_id,
                    ordinal=999,
                )
            )

    with pytest.raises(DBAPIError, match="relations are closed"):
        with migrated_engine.begin() as connection:
            connection.execute(
                insert(chunk_articles).values(
                    chunk_id=target.chunks[0].chunk_id,
                    article_id=other_scope.articles[0].article_id,
                    ordinal=999,
                )
            )

    with pytest.raises(DBAPIError, match="law version articles are closed"):
        with migrated_engine.begin() as connection:
            connection.execute(
                insert(law_articles).values(
                    article_id="forbidden-article-append",
                    version_id=target.articles[0].version_id,
                    law_id=target.articles[0].law_id,
                    article_number="禁止追加",
                    body="已验证版本不得追加条文。",
                    raw_text="禁止追加 已验证版本不得追加条文。",
                    source_ref="fixtures/forbidden.txt",
                    source_line=1,
                    parse_status="from_filename",
                    content_hash="f" * 64,
                )
            )

    with pytest.raises(DBAPIError, match="must begin in building state"):
        with migrated_engine.begin() as connection:
            connection.execute(
                insert(corpus_snapshots).values(
                    snapshot_id="snapshot-direct-active",
                    scope_id="scope-filter-direct",
                    source_manifest={"source": "forbidden-direct-insert"},
                    source_manifest_hash="a" * 64,
                    corpus_hash="b" * 64,
                    status="active",
                    activated_at=func.now(),
                )
            )

    with migrated_engine.begin() as connection:
        connection.execute(
            update(corpus_snapshots)
            .where(corpus_snapshots.c.snapshot_id == target.snapshot.snapshot_id)
            .values(status="archived")
        )
    try:
        archived_encoder = _FixedEncoder(
            [1.0, 0.0, 0.0], target.embedding_profile.to_identity()
        )
        with pytest.raises(RetrievalUnavailableError, match="not serviceable"):
            PgVectorExactRetriever(
                repository,
                encoder=archived_encoder,
                filters=v1_filters,
            )
        assert archived_encoder.calls == 0
    finally:
        with migrated_engine.begin() as connection:
            connection.execute(
                update(corpus_snapshots)
                .where(corpus_snapshots.c.snapshot_id == target.snapshot.snapshot_id)
                .values(status="active", activated_at=func.now())
            )

    with pytest.raises(DBAPIError, match="immutable storage table embedding_imports"):
        with migrated_engine.begin() as connection:
            connection.execute(
                update(embedding_imports)
                .where(
                    embedding_imports.c.snapshot_id == target.snapshot.snapshot_id,
                    embedding_imports.c.profile_id
                    == target.embedding_profile.profile_id,
                )
                .values(status="failed")
            )

    with migrated_engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE embedding_imports DISABLE TRIGGER "
                "trg_embedding_imports_immutable"
            )
        )
        connection.execute(
            update(embedding_imports)
            .where(
                embedding_imports.c.snapshot_id == target.snapshot.snapshot_id,
                embedding_imports.c.profile_id == target.embedding_profile.profile_id,
            )
            .values(status="failed")
        )
        connection.execute(
            text(
                "ALTER TABLE embedding_imports ENABLE TRIGGER "
                "trg_embedding_imports_immutable"
            )
        )
    try:
        failed_receipt_encoder = _FixedEncoder(
            [1.0, 0.0, 0.0], target.embedding_profile.to_identity()
        )
        with pytest.raises(RetrievalUnavailableError, match="not validated"):
            PgVectorExactRetriever(
                repository,
                encoder=failed_receipt_encoder,
                filters=v1_filters,
            )
        assert failed_receipt_encoder.calls == 0
    finally:
        with migrated_engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE embedding_imports DISABLE TRIGGER "
                    "trg_embedding_imports_immutable"
                )
            )
            connection.execute(
                update(embedding_imports)
                .where(
                    embedding_imports.c.snapshot_id == target.snapshot.snapshot_id,
                    embedding_imports.c.profile_id
                    == target.embedding_profile.profile_id,
                )
                .values(status="validated")
            )
            connection.execute(
                text(
                    "ALTER TABLE embedding_imports ENABLE TRIGGER "
                    "trg_embedding_imports_immutable"
                )
            )


def test_multi_article_hard_filters_do_not_expose_nonmatching_relations_and_rrf_is_bound(
    migrated_engine: Engine,
) -> None:
    specs = [
        _ArticleSpec(
            "strict-law-a",
            "strict-law-a-v1",
            "严格测试甲法",
            "strict-a-article-1",
            "第一条",
            "2024-01-01",
        ),
        _ArticleSpec(
            "strict-law-b",
            "strict-law-b-v1",
            "严格测试乙法",
            "strict-b-article-2",
            "第二条",
            "2024-01-01",
        ),
        _ArticleSpec(
            "strict-law-a",
            "strict-law-a-v1",
            "严格测试甲法",
            "strict-a-article-2",
            "第二条",
            "2024-01-01",
        ),
    ]

    def mixed_chunks(articles):
        return [
            chunk_from_articles([articles[0], articles[1]], "neighbor"),
            chunk_from_articles([articles[2]], "neighbor"),
        ]

    bundle = _build_bundle(
        prefix="strict-boundary",
        scope_id="scope-strict",
        snapshot_id="snapshot-strict-v1",
        specs=specs,
        vectors=[[1.0, 0.0, 0.0], _unit_vector(0.4)],
        chunks=mixed_chunks,
    )
    PostgresCorpusRepository(migrated_engine).import_bundle(bundle)
    filters = RetrievalFilters(
        scope_id="scope-strict",
        snapshot_id="snapshot-strict-v1",
        profile_id=bundle.embedding_profile.profile_id,
        law_ids=("strict-law-a",),
    )
    repository = PostgresExactRetrievalRepository(migrated_engine)

    exact = repository.search_vector(
        [1.0, 0.0, 0.0],
        top_k=5,
        filters=filters,
        expected_profile=bundle.embedding_profile.to_identity(),
    )
    assert [item.chunk.metadata["article_ids"] for item in exact] == [
        ["strict-a-article-2"]
    ]
    assert (
        repository.search_vector(
            [1.0, 0.0, 0.0],
            top_k=5,
            filters=replace(filters, article_ids=("strict-a-article-1",)),
            expected_profile=bundle.embedding_profile.to_identity(),
        )
        == []
    )

    profile = bundle.embedding_profile.to_identity()
    bound_corpus = repository.load_bound_corpus(
        filters=filters, expected_profile=profile
    )
    scoped_chunks = bound_corpus.chunks
    assert [chunk.metadata["article_ids"] for chunk in scoped_chunks] == [
        ["strict-a-article-2"]
    ]
    bm25 = BoundaryBoundRetriever(BM25Retriever(scoped_chunks), corpus=bound_corpus)
    first_lexical = bm25.retrieve("strict-a-article-2 正文", top_k=5)
    first_lexical[0].chunk.law_names.append("caller poison")
    first_lexical[0].trace["caller"] = {"poison": True}
    second_lexical = bm25.retrieve("strict-a-article-2 正文", top_k=5)
    assert second_lexical[0].chunk.law_names == ["严格测试甲法"]
    assert "caller" not in second_lexical[0].trace
    with pytest.raises(FrozenInstanceError):
        bm25._boundary = replace(filters, scope_id="scope-other")  # type: ignore[misc]
    dense = PgVectorExactRetriever(
        repository,
        encoder=_FixedEncoder([1.0, 0.0, 0.0], profile),
        filters=filters,
    )
    hybrid = RRFHybridRetriever(bm25, dense)
    results = hybrid.retrieve("strict-a-article-2 正文", top_k=5)
    assert [item.chunk.metadata["article_ids"] for item in results] == [
        ["strict-a-article-2"]
    ]
    assert all(
        item.trace["boundary_fingerprint"] == filters.boundary_fingerprint
        for item in results
    )


def test_exact_retrieval_preserves_empty_article_number_from_import_contract(
    migrated_engine: Engine,
) -> None:
    bundle = _build_bundle(
        prefix="empty-article-number",
        scope_id="scope-empty-article-number",
        snapshot_id="snapshot-empty-article-number-v1",
        specs=[
            _ArticleSpec(
                law_id="empty-number-law",
                version_id="empty-number-law-v1",
                title="空条号测试法",
                article_id="empty-number-article",
                article_number="",
                valid_from="2024-01-01",
            )
        ],
        vectors=[[1.0, 0.0, 0.0]],
    )
    PostgresCorpusRepository(migrated_engine).import_bundle(bundle)
    filters = RetrievalFilters(
        scope_id=bundle.snapshot.scope_id,
        snapshot_id=bundle.snapshot.snapshot_id,
        profile_id=bundle.embedding_profile.profile_id,
    )

    results = PostgresExactRetrievalRepository(migrated_engine).search_vector(
        [1.0, 0.0, 0.0],
        top_k=1,
        filters=filters,
        expected_profile=bundle.embedding_profile.to_identity(),
    )

    assert len(results) == 1
    assert results[0].chunk.article_numbers == []
    assert results[0].provenance is not None
    assert results[0].provenance.articles[0].article_number == ""


def test_exact_retrieval_rejects_persisted_payload_or_vector_hash_drift(
    migrated_engine: Engine,
) -> None:
    bundle = _build_bundle(
        prefix="tamper-detection",
        scope_id="scope-tamper-detection",
        snapshot_id="snapshot-tamper-detection-v1",
        specs=_one_law_specs("tamper-detection", 1),
        vectors=[[1.0, 0.0, 0.0]],
    )
    PostgresCorpusRepository(migrated_engine).import_bundle(bundle)
    filters = RetrievalFilters(
        scope_id=bundle.snapshot.scope_id,
        snapshot_id=bundle.snapshot.snapshot_id,
        profile_id=bundle.embedding_profile.profile_id,
    )
    repository = PostgresExactRetrievalRepository(migrated_engine)
    profile = bundle.embedding_profile.to_identity()
    chunk_id = bundle.chunks[0].chunk_id

    with pytest.raises(DBAPIError, match="immutable storage table chunks"):
        with migrated_engine.begin() as connection:
            connection.execute(
                update(chunk_rows)
                .where(chunk_rows.c.chunk_id == chunk_id)
                .values(text="blocked tamper")
            )

    with migrated_engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE chunks DISABLE TRIGGER trg_chunks_immutable")
        )
        connection.execute(
            update(chunk_rows)
            .where(chunk_rows.c.chunk_id == chunk_id)
            .values(text="tampered persisted text")
        )
        connection.execute(
            text("ALTER TABLE chunks ENABLE TRIGGER trg_chunks_immutable")
        )
    try:
        with pytest.raises(RetrievalDataError, match="content hash"):
            repository.search_vector(
                [1.0, 0.0, 0.0],
                top_k=1,
                filters=filters,
                expected_profile=profile,
            )
    finally:
        with migrated_engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE chunks DISABLE TRIGGER trg_chunks_immutable")
            )
            connection.execute(
                update(chunk_rows)
                .where(chunk_rows.c.chunk_id == chunk_id)
                .values(text=bundle.chunks[0].text)
            )
            connection.execute(
                text("ALTER TABLE chunks ENABLE TRIGGER trg_chunks_immutable")
            )

    with pytest.raises(DBAPIError, match="immutable storage table chunk_embeddings"):
        with migrated_engine.begin() as connection:
            connection.execute(
                update(chunk_embeddings)
                .where(
                    chunk_embeddings.c.chunk_id == chunk_id,
                    chunk_embeddings.c.profile_id == profile.profile_id,
                )
                .values(embedding=[0.0, 1.0, 0.0])
            )

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
                chunk_embeddings.c.chunk_id == chunk_id,
                chunk_embeddings.c.profile_id == profile.profile_id,
            )
            .values(embedding=[0.0, 1.0, 0.0])
        )
        connection.execute(
            text(
                "ALTER TABLE chunk_embeddings ENABLE TRIGGER "
                "trg_chunk_embeddings_immutable"
            )
        )
    try:
        with pytest.raises(RetrievalDataError, match="embedding hash"):
            repository.search_vector(
                [0.0, 1.0, 0.0],
                top_k=1,
                filters=filters,
                expected_profile=profile,
            )
    finally:
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
                    chunk_embeddings.c.chunk_id == chunk_id,
                    chunk_embeddings.c.profile_id == profile.profile_id,
                )
                .values(embedding=list(bundle.embeddings[0].embedding))
            )
            connection.execute(
                text(
                    "ALTER TABLE chunk_embeddings ENABLE TRIGGER "
                    "trg_chunk_embeddings_immutable"
                )
            )


def test_filter_and_top_k_contracts_fail_closed_before_distance_query() -> None:
    with pytest.raises(RetrievalContractError, match="scope_id"):
        RetrievalFilters(scope_id=" ", snapshot_id="snapshot", profile_id="a" * 64)
    with pytest.raises(RetrievalContractError, match="duplicates"):
        RetrievalFilters(
            scope_id="scope",
            snapshot_id="snapshot",
            profile_id="a" * 64,
            law_ids=("law", "law"),
        )

    # Engine access is not needed for constructor-level top_k/vector validation.
    class _Dialect:
        name = "postgresql"

    class _Engine:
        dialect = _Dialect()

    repository = PostgresExactRetrievalRepository(_Engine())  # type: ignore[arg-type]
    profile = EmbeddingProfileIdentity(
        provider="fixture",
        model="fixture/raw",
        revision="revision-1",
        dimensions=1,
        normalization=False,
        query_prefix="",
        document_prefix="",
        embed_with_metadata=False,
    )
    filters = RetrievalFilters(
        scope_id="scope", snapshot_id="snapshot", profile_id=profile.profile_id
    )
    with pytest.raises(RetrievalContractError, match="top_k"):
        repository.search_vector(
            [1.0], top_k=True, filters=filters, expected_profile=profile
        )
    with pytest.raises(RetrievalContractError, match="finite"):
        repository.search_vector(
            [float("nan")], top_k=1, filters=filters, expected_profile=profile
        )
    with pytest.raises(RetrievalContractError, match="finite float32"):
        repository.search_vector(
            [1e100], top_k=1, filters=filters, expected_profile=profile
        )

    normalized_profile = replace(profile, dimensions=3, normalization=True)
    normalized_filters = replace(filters, profile_id=normalized_profile.profile_id)
    with pytest.raises(RetrievalContractError, match="L2 norm"):
        repository.search_vector(
            [0.5, 0.5, 0.0],
            top_k=1,
            filters=normalized_filters,
            expected_profile=normalized_profile,
        )
    with pytest.raises(RetrievalContractError, match="L2 norm"):
        repository.search_vector(
            [0.0, 0.0, 0.0],
            top_k=1,
            filters=normalized_filters,
            expected_profile=normalized_profile,
        )


def test_pgvector_exact_is_stable_after_engine_reconnect(
    integration_database_url: str, migrated_engine: Engine
) -> None:
    bundle = _build_bundle(
        prefix="exact-reconnect",
        scope_id="scope-exact-reconnect",
        snapshot_id="snapshot-exact-reconnect-v1",
        specs=_one_law_specs("exact-reconnect", 2),
        vectors=[_unit_vector(0.25), _unit_vector(0.75)],
    )
    PostgresCorpusRepository(migrated_engine).import_bundle(bundle)
    filters = RetrievalFilters(
        scope_id=bundle.snapshot.scope_id,
        snapshot_id=bundle.snapshot.snapshot_id,
        profile_id=bundle.embedding_profile.profile_id,
    )
    expected = PostgresExactRetrievalRepository(migrated_engine).search_vector(
        [1.0, 0.0, 0.0],
        top_k=2,
        filters=filters,
        expected_profile=bundle.embedding_profile.to_identity(),
    )

    replacement = create_database_engine(DatabaseSettings(integration_database_url))
    try:
        actual = PostgresExactRetrievalRepository(replacement).search_vector(
            [1.0, 0.0, 0.0],
            top_k=2,
            filters=filters,
            expected_profile=bundle.embedding_profile.to_identity(),
        )
    finally:
        replacement.dispose()

    assert [item.chunk.chunk_id for item in actual] == [
        item.chunk.chunk_id for item in expected
    ]
    np.testing.assert_allclose(
        [item.score for item in actual],
        [item.score for item in expected],
        rtol=0,
        atol=0,
    )
