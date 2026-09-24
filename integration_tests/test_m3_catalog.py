from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Barrier, Event
from typing import Callable

import numpy as np
import pytest
from sqlalchemy import Engine, select, update
from sqlalchemy.exc import DBAPIError

from legal_rag.chunking import article_chunks, chunk_from_articles, long_split_chunks
from legal_rag.embeddings import (
    EMBEDDING_CACHE_SCHEMA_VERSION,
    EmbeddingCache,
    EmbeddingModelConfig,
    chunk_corpus_fingerprint,
    embedding_contract_fingerprint,
)
from legal_rag.models import Chunk, LawArticle
from legal_rag.storage.catalog import (
    ArticleLookupBoundary,
    ArticleLookupRequest,
    CatalogContractError,
    CatalogUnavailableError,
    PostgresLegalCatalogRepository,
    SnapshotActivationConflictError,
)
from legal_rag.storage.contracts import (
    LawVersionSpec,
    build_storage_import_bundle,
    sha256_json,
)
from legal_rag.storage.repository import PostgresCorpusRepository
from legal_rag.storage.schema import (
    active_snapshot_pointers,
    corpus_snapshots,
    snapshot_activation_events,
)


@dataclass(frozen=True)
class _ArticleSpec:
    law_id: str
    version_id: str
    title: str
    article_id: str
    article_number: str
    body: str
    valid_from: str | None = None
    valid_to: str | None = None
    verification_status: str = "verified"


def _unit_vectors(count: int) -> list[list[float]]:
    basis = (
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
    )
    return [basis[index % len(basis)] for index in range(count)]


def _build_bundle(
    *,
    prefix: str,
    scope_id: str,
    snapshot_id: str,
    specs: list[_ArticleSpec],
    chunks_factory: Callable[[list[LawArticle]], list[Chunk]] = article_chunks,
):
    articles = [
        LawArticle(
            article_id=spec.article_id,
            law_name=spec.title,
            article_number=spec.article_number,
            body=spec.body,
            raw_text=f"{spec.article_number} {spec.body}",
            source_file=f"fixtures/{spec.version_id}.txt",
            line_no=index,
            parse_status="from_filename",
        )
        for index, spec in enumerate(specs, start=1)
    ]
    chunks = chunks_factory(articles)
    model = EmbeddingModelConfig(
        key="m3-catalog-fixture",
        provider="fixture",
        model_name="fixture/catalog-model",
        role="retrieval",
        revision="catalog-revision-1",
        normalize=True,
        dimensions=3,
    )
    vectors = np.asarray(_unit_vectors(len(chunks)), dtype=np.float32)
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
    laws: list[LawVersionSpec] = []
    seen_versions: set[str] = set()
    for spec in specs:
        if spec.version_id in seen_versions:
            continue
        seen_versions.add(spec.version_id)
        laws.append(
            LawVersionSpec(
                law_id=spec.law_id,
                version_id=spec.version_id,
                title=spec.title,
                verification_status=spec.verification_status,
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
            "fixture": "m3-structured-catalog",
            "prefix": prefix,
            "snapshot_id": snapshot_id,
        },
        laws=laws,
        articles=articles,
        chunks=chunks,
        embedding_cache=cache,
        model_config=model,
        model_revision=model.revision,
        chunk_recipe={"fixture": "m3-structured-catalog"},
        article_version_ids={spec.article_id: spec.version_id for spec in specs},
    )


def _lookup_request(
    bundle,
    *,
    law_title: str,
    article_number: str,
    law_id: str | None = None,
    version_id: str | None = None,
    effective_on: str | None = None,
) -> ArticleLookupRequest:
    return ArticleLookupRequest(
        boundary=ArticleLookupBoundary(
            scope_id=bundle.snapshot.scope_id,
            snapshot_id=bundle.snapshot.snapshot_id,
        ),
        law_title=law_title,
        article_number=article_number,
        law_id=law_id,
        version_id=version_id,
        effective_on=effective_on,
    )


def test_catalog_resolves_exact_title_normalized_number_version_and_effective_date(
    migrated_engine: Engine,
) -> None:
    title = "目录版本测试法"
    law_id = "catalog-version-law"
    v1 = _ArticleSpec(
        law_id=law_id,
        version_id="catalog-version-law-v1",
        title=title,
        article_id="catalog-version-v1-article-1",
        article_number="第一条",
        body="旧版本第一条正文。",
        valid_from="2024-01-01",
        valid_to="2025-01-01",
    )
    v2 = _ArticleSpec(
        law_id=law_id,
        version_id="catalog-version-law-v2",
        title=title,
        article_id="catalog-version-v2-article-1",
        article_number="第一条",
        body="新版本第一条正文。",
        valid_from="2025-01-01",
    )
    bundle = _build_bundle(
        prefix="catalog-version",
        scope_id="scope-catalog-version",
        snapshot_id="snapshot-catalog-version",
        specs=[v1, v2],
    )
    PostgresCorpusRepository(migrated_engine).import_bundle(bundle)
    catalog = PostgresLegalCatalogRepository(migrated_engine)

    ambiguous = catalog.lookup_article(
        _lookup_request(
            bundle,
            law_title=title,
            article_number="第一 条",
        )
    )
    assert ambiguous.status == "needs_disambiguation"
    assert ambiguous.reason == "multiple_exact_versions"
    assert ambiguous.match is None
    assert [item.version_id for item in ambiguous.candidates] == [
        v1.version_id,
        v2.version_id,
    ]
    assert not hasattr(ambiguous.candidates[0], "body")

    explicit = catalog.lookup_article(
        _lookup_request(
            bundle,
            law_title=title,
            article_number="第一 条",
            version_id=v1.version_id,
        )
    )
    assert explicit.status == "found"
    assert explicit.candidates == ()
    assert explicit.match is not None
    assert explicit.match.body == v1.body
    assert explicit.match.article_number == "第一条"
    assert explicit.match.version_id == v1.version_id
    assert explicit.match.provenance.request_fingerprint == explicit.request.fingerprint

    absent_version = catalog.lookup_article(
        _lookup_request(
            bundle,
            law_title=title,
            article_number="第一条",
            version_id="catalog-version-law-v3",
        )
    )
    assert absent_version.status == "not_found"
    assert absent_version.match is None
    assert absent_version.candidates == ()

    explicit_v1_outside_interval = catalog.lookup_article(
        _lookup_request(
            bundle,
            law_title=title,
            article_number="第一条",
            version_id=v1.version_id,
            effective_on="2025-01-01",
        )
    )
    assert explicit_v1_outside_interval.status == "not_found"
    assert explicit_v1_outside_interval.match is None
    assert explicit_v1_outside_interval.candidates == ()

    last_v1_day = catalog.lookup_article(
        _lookup_request(
            bundle,
            law_title=title,
            article_number="第一条",
            effective_on="2024-12-31",
        )
    )
    first_v2_day = catalog.lookup_article(
        _lookup_request(
            bundle,
            law_title=title,
            article_number="第一条",
            effective_on="2025-01-01",
        )
    )
    assert last_v1_day.status == "found"
    assert last_v1_day.match is not None
    assert last_v1_day.match.version_id == v1.version_id
    assert first_v2_day.status == "found"
    assert first_v2_day.match is not None
    assert first_v2_day.match.version_id == v2.version_id


def test_catalog_never_falls_back_across_scope_snapshot_title_or_article(
    migrated_engine: Engine,
) -> None:
    title = "目录隔离测试法"
    target = _ArticleSpec(
        law_id="catalog-isolation-target-law",
        version_id="catalog-isolation-target-v1",
        title=title,
        article_id="catalog-isolation-target-article-1",
        article_number="第一条",
        body="目标快照正文。",
        valid_from="2024-01-01",
    )
    other_snapshot = _ArticleSpec(
        law_id="catalog-isolation-other-snapshot-law",
        version_id="catalog-isolation-other-snapshot-v1",
        title=title,
        article_id="catalog-isolation-other-snapshot-article-1",
        article_number="第一条",
        body="同范围其他快照正文。",
        valid_from="2024-01-01",
    )
    other_scope = _ArticleSpec(
        law_id="catalog-isolation-other-scope-law",
        version_id="catalog-isolation-other-scope-v1",
        title=title,
        article_id="catalog-isolation-other-scope-article-1",
        article_number="第一条",
        body="其他范围正文。",
        valid_from="2024-01-01",
    )
    bundles = [
        _build_bundle(
            prefix="catalog-isolation-target",
            scope_id="scope-catalog-isolation",
            snapshot_id="snapshot-catalog-isolation-target",
            specs=[target],
        ),
        _build_bundle(
            prefix="catalog-isolation-other-snapshot",
            scope_id="scope-catalog-isolation",
            snapshot_id="snapshot-catalog-isolation-other",
            specs=[other_snapshot],
        ),
        _build_bundle(
            prefix="catalog-isolation-other-scope",
            scope_id="scope-catalog-isolation-other",
            snapshot_id="snapshot-catalog-isolation-other-scope",
            specs=[other_scope],
        ),
    ]
    importer = PostgresCorpusRepository(migrated_engine)
    for bundle in bundles:
        importer.import_bundle(bundle)
    catalog = PostgresLegalCatalogRepository(migrated_engine)
    target_bundle = bundles[0]

    found = catalog.lookup_article(
        _lookup_request(
            target_bundle,
            law_title=title,
            article_number="第一条",
        )
    )
    assert found.status == "found"
    assert found.match is not None
    assert found.match.body == target.body
    assert found.match.version_id == target.version_id

    other_scope_bundle = bundles[2]
    cross_scope_boundaries = (
        ArticleLookupBoundary(
            scope_id=other_scope_bundle.snapshot.scope_id,
            snapshot_id=target_bundle.snapshot.snapshot_id,
        ),
        ArticleLookupBoundary(
            scope_id=target_bundle.snapshot.scope_id,
            snapshot_id=other_scope_bundle.snapshot.snapshot_id,
        ),
    )
    for boundary in cross_scope_boundaries:
        with pytest.raises(CatalogUnavailableError, match="not serviceable"):
            catalog.lookup_article(
                ArticleLookupRequest(
                    boundary=boundary,
                    law_title=title,
                    article_number="第一条",
                )
            )

    cross_snapshot_version = catalog.lookup_article(
        _lookup_request(
            target_bundle,
            law_title=title,
            article_number="第一条",
            version_id=other_snapshot.version_id,
        )
    )
    wrong_title = catalog.lookup_article(
        _lookup_request(
            target_bundle,
            law_title="不存在的目录测试法",
            article_number="第一条",
        )
    )
    wrong_article = catalog.lookup_article(
        _lookup_request(
            target_bundle,
            law_title=title,
            article_number="第九十九条",
        )
    )
    for result in (cross_snapshot_version, wrong_title, wrong_article):
        assert result.status == "not_found"
        assert result.match is None
        assert result.candidates == ()

    deny_all = catalog.lookup_article(
        ArticleLookupRequest(
            boundary=ArticleLookupBoundary(
                scope_id="scope-that-must-not-be-probed",
                snapshot_id="snapshot-that-must-not-be-probed",
                article_ids=(),
            ),
            law_title=title,
            article_number="第一条",
        )
    )
    assert deny_all.status == "not_found"
    assert deny_all.reason == "boundary_denies_all"


def test_catalog_applies_law_version_and_article_allowlists_as_an_intersection(
    migrated_engine: Engine,
) -> None:
    scope_id = "scope-catalog-allowlist-intersection"
    snapshot_id = "snapshot-catalog-allowlist-intersection"
    title = "目录授权交集测试法"
    law_id = "catalog-allowlist-law"
    v1 = _ArticleSpec(
        law_id=law_id,
        version_id="catalog-allowlist-law-v1",
        title=title,
        article_id="catalog-allowlist-v1-article-1",
        article_number="第一条",
        body="授权交集版本一正文。",
        valid_from="2024-01-01",
        valid_to="2025-01-01",
    )
    v2 = _ArticleSpec(
        law_id=law_id,
        version_id="catalog-allowlist-law-v2",
        title=title,
        article_id="catalog-allowlist-v2-article-1",
        article_number="第一条",
        body="授权交集版本二正文。",
        valid_from="2025-01-01",
    )
    bundle = _build_bundle(
        prefix="catalog-allowlist-intersection",
        scope_id=scope_id,
        snapshot_id=snapshot_id,
        specs=[v1, v2],
    )
    PostgresCorpusRepository(migrated_engine).import_bundle(bundle)
    catalog = PostgresLegalCatalogRepository(migrated_engine)

    allowed = catalog.lookup_article(
        ArticleLookupRequest(
            boundary=ArticleLookupBoundary(
                scope_id=scope_id,
                snapshot_id=snapshot_id,
                law_ids=("unused-law", law_id),
                version_ids=("unused-version", v1.version_id),
                article_ids=("unused-article", v1.article_id),
            ),
            law_title=title,
            article_number="第一条",
        )
    )
    assert allowed.status == "found"
    assert allowed.match is not None
    assert allowed.match.version_id == v1.version_id
    assert allowed.match.article_id == v1.article_id

    empty_intersection = catalog.lookup_article(
        ArticleLookupRequest(
            boundary=ArticleLookupBoundary(
                scope_id=scope_id,
                snapshot_id=snapshot_id,
                law_ids=(law_id,),
                version_ids=(v1.version_id,),
                article_ids=(v2.article_id,),
            ),
            law_title=title,
            article_number="第一条",
        )
    )
    assert empty_intersection.status == "not_found"
    assert empty_intersection.reason == "no_exact_match"
    assert empty_intersection.match is None
    assert empty_intersection.candidates == ()


def test_catalog_effective_date_fails_closed_on_overlap_or_unknown_validity(
    migrated_engine: Engine,
) -> None:
    overlapping_title = "目录重叠有效期测试法"
    overlapping_bundle = _build_bundle(
        prefix="catalog-overlapping-validity",
        scope_id="scope-catalog-validity",
        snapshot_id="snapshot-catalog-overlapping-validity",
        specs=[
            _ArticleSpec(
                law_id="catalog-overlap-law",
                version_id="catalog-overlap-law-v0",
                title=overlapping_title,
                article_id="catalog-overlap-v0-article-1",
                article_number="第一条",
                body="已经失效且不应进入重叠候选的正文。",
                valid_from="2020-01-01",
                valid_to="2024-01-01",
            ),
            _ArticleSpec(
                law_id="catalog-overlap-law",
                version_id="catalog-overlap-law-v1",
                title=overlapping_title,
                article_id="catalog-overlap-v1-article-1",
                article_number="第一条",
                body="重叠版本一正文。",
                valid_from="2024-01-01",
                valid_to="2026-01-01",
            ),
            _ArticleSpec(
                law_id="catalog-overlap-law",
                version_id="catalog-overlap-law-v2",
                title=overlapping_title,
                article_id="catalog-overlap-v2-article-1",
                article_number="第一条",
                body="重叠版本二正文。",
                valid_from="2025-01-01",
            ),
        ],
    )
    unknown_title = "目录未知有效期测试法"
    unknown_bundle = _build_bundle(
        prefix="catalog-unknown-validity",
        scope_id="scope-catalog-validity",
        snapshot_id="snapshot-catalog-unknown-validity",
        specs=[
            _ArticleSpec(
                law_id="catalog-unknown-law",
                version_id="catalog-unknown-law-v0",
                title=unknown_title,
                article_id="catalog-unknown-v0-article-1",
                article_number="第一条",
                body="已经确定失效且不应进入未知候选的正文。",
                valid_from="2020-01-01",
                valid_to="2024-01-01",
            ),
            _ArticleSpec(
                law_id="catalog-unknown-law",
                version_id="catalog-unknown-law-v1",
                title=unknown_title,
                article_id="catalog-unknown-article-1",
                article_number="第一条",
                body="有效期尚未核验的正文。",
                valid_from=None,
                verification_status="unknown",
            ),
        ],
    )
    same_title = "目录同名法律身份测试法"
    same_title_bundle = _build_bundle(
        prefix="catalog-same-title-identities",
        scope_id="scope-catalog-validity",
        snapshot_id="snapshot-catalog-same-title-identities",
        specs=[
            _ArticleSpec(
                law_id="catalog-same-title-law-a",
                version_id="catalog-same-title-law-a-v1",
                title=same_title,
                article_id="catalog-same-title-law-a-article-1",
                article_number="第一条",
                body="同名法律甲正文。",
                valid_from="2024-01-01",
                valid_to="2026-01-01",
            ),
            _ArticleSpec(
                law_id="catalog-same-title-law-b",
                version_id="catalog-same-title-law-b-v1",
                title=same_title,
                article_id="catalog-same-title-law-b-article-1",
                article_number="第一条",
                body="同名法律乙正文。",
                valid_from="2030-01-01",
            ),
        ],
    )
    importer = PostgresCorpusRepository(migrated_engine)
    importer.import_bundle(overlapping_bundle)
    importer.import_bundle(unknown_bundle)
    importer.import_bundle(same_title_bundle)
    catalog = PostgresLegalCatalogRepository(migrated_engine)

    overlap = catalog.lookup_article(
        _lookup_request(
            overlapping_bundle,
            law_title=overlapping_title,
            article_number="第一条",
            effective_on="2025-06-01",
        )
    )
    assert overlap.status == "needs_disambiguation"
    assert overlap.reason == "overlapping_validity"
    assert overlap.match is None
    assert len(overlap.candidates) == 2
    assert {candidate.version_id for candidate in overlap.candidates} == {
        "catalog-overlap-law-v1",
        "catalog-overlap-law-v2",
    }

    unknown = catalog.lookup_article(
        _lookup_request(
            unknown_bundle,
            law_title=unknown_title,
            article_number="第一条",
            effective_on="2025-06-01",
        )
    )
    assert unknown.status == "needs_disambiguation"
    assert unknown.reason == "validity_unknown"
    assert unknown.match is None
    assert len(unknown.candidates) == 1
    assert unknown.candidates[0].verification_status == "unknown"
    assert unknown.candidates[0].version_id == "catalog-unknown-law-v1"

    same_title_result = catalog.lookup_article(
        _lookup_request(
            same_title_bundle,
            law_title=same_title,
            article_number="第一条",
            effective_on="2025-06-01",
        )
    )
    assert same_title_result.status == "needs_disambiguation"
    assert same_title_result.reason == "multiple_law_identities"
    assert same_title_result.match is None
    assert {candidate.law_id for candidate in same_title_result.candidates} == {
        "catalog-same-title-law-a",
        "catalog-same-title-law-b",
    }


def test_catalog_returns_one_authoritative_article_from_mixed_and_repeated_chunks(
    migrated_engine: Engine,
) -> None:
    title = "目录混合分块测试法"
    target_body = "目标法条唯一权威正文。" + "甲乙丙丁。" * 40
    target = _ArticleSpec(
        law_id="catalog-mixed-law",
        version_id="catalog-mixed-law-v1",
        title=title,
        article_id="catalog-mixed-article-1",
        article_number="第一条",
        body=target_body,
        valid_from="2024-01-01",
    )
    neighbor = _ArticleSpec(
        law_id="catalog-mixed-law",
        version_id="catalog-mixed-law-v1",
        title=title,
        article_id="catalog-mixed-article-2",
        article_number="第二条",
        body="不得泄漏到第一条结构化结果中的相邻正文。",
        valid_from="2024-01-01",
    )

    def mixed_and_repeated_chunks(articles: list[LawArticle]) -> list[Chunk]:
        target_article, neighbor_article = articles
        return [
            chunk_from_articles(
                [target_article, neighbor_article],
                "neighbor",
                extra_metadata={"fixture": "mixed-membership"},
            ),
            *long_split_chunks(
                [target_article],
                max_chars=80,
                overlap_chars=10,
            ),
        ]

    bundle = _build_bundle(
        prefix="catalog-mixed",
        scope_id="scope-catalog-mixed",
        snapshot_id="snapshot-catalog-mixed",
        specs=[target, neighbor],
        chunks_factory=mixed_and_repeated_chunks,
    )
    PostgresCorpusRepository(migrated_engine).import_bundle(bundle)

    result = PostgresLegalCatalogRepository(migrated_engine).lookup_article(
        _lookup_request(
            bundle,
            law_title=title,
            article_number="第一条",
        )
    )

    assert result.status == "found"
    assert result.reason == "exact_match"
    assert result.candidates == ()
    assert result.match is not None
    assert result.match.body == target_body
    assert result.match.raw_text == f"第一条 {target_body}"
    assert neighbor.body not in result.match.body
    assert neighbor.body not in result.match.raw_text
    assert result.match.provenance.article_id == target.article_id
    assert result.match.provenance.version_id == target.version_id
    memberships = result.match.provenance.memberships
    snapshot_ordinals = {item.chunk_id: item.ordinal for item in bundle.snapshot_chunks}
    chunk_content_hashes = {item.chunk_id: item.content_hash for item in bundle.chunks}
    expected_memberships = sorted(
        (
            {
                "chunk_id": relation.chunk_id,
                "snapshot_ordinal": snapshot_ordinals[relation.chunk_id],
                "article_ordinal": relation.ordinal,
                "chunk_content_hash": chunk_content_hashes[relation.chunk_id],
            }
            for relation in bundle.chunk_articles
            if relation.article_id == target.article_id
        ),
        key=lambda item: (item["snapshot_ordinal"], item["chunk_id"]),
    )
    assert len(expected_memberships) > 1
    assert [item.trace_payload() for item in memberships] == expected_memberships
    assert result.match.provenance.membership_fingerprint == sha256_json(
        {
            "schema_version": 1,
            "scope_id": bundle.snapshot.scope_id,
            "snapshot_id": bundle.snapshot.snapshot_id,
            "article_id": target.article_id,
            "memberships": expected_memberships,
        }
    )


def test_active_snapshot_replace_pins_requests_and_rollback_is_revisioned(
    migrated_engine: Engine,
) -> None:
    scope_id = "scope-catalog-activation"
    title = "目录激活测试法"
    old_spec = _ArticleSpec(
        law_id="catalog-activation-law",
        version_id="catalog-activation-law-v1",
        title=title,
        article_id="catalog-activation-v1-article-1",
        article_number="第一条",
        body="旧快照权威正文。",
        valid_from="2024-01-01",
        valid_to="2025-01-01",
    )
    new_spec = _ArticleSpec(
        law_id="catalog-activation-law",
        version_id="catalog-activation-law-v2",
        title=title,
        article_id="catalog-activation-v2-article-1",
        article_number="第一条",
        body="新快照权威正文。",
        valid_from="2025-01-01",
    )
    old_bundle = _build_bundle(
        prefix="catalog-activation-old",
        scope_id=scope_id,
        snapshot_id="snapshot-catalog-activation-old",
        specs=[old_spec],
    )
    new_bundle = _build_bundle(
        prefix="catalog-activation-new",
        scope_id=scope_id,
        snapshot_id="snapshot-catalog-activation-new",
        specs=[new_spec],
    )
    importer = PostgresCorpusRepository(migrated_engine)
    importer.import_bundle(old_bundle)
    importer.import_bundle(new_bundle)
    catalog = PostgresLegalCatalogRepository(migrated_engine)

    initial = catalog.activate_snapshot(
        scope_id=scope_id,
        snapshot_id=old_bundle.snapshot.snapshot_id,
        expected_current_snapshot_id=None,
        required_profile_id=old_bundle.embedding_profile.profile_id,
        actor="integration-test",
        reason="initial activation",
    )
    assert initial.changed is True
    assert initial.event.operation == "initial_activate"
    assert initial.selection.revision == 1
    assert catalog.get_active_snapshot(scope_id) == initial.selection

    pinned_old_request = ArticleLookupRequest(
        boundary=initial.selection.boundary,
        law_title=title,
        article_number="第一条",
    )
    old_active = catalog.lookup_active_article(
        scope_id=scope_id,
        law_title=title,
        article_number="第一条",
    )
    assert old_active.match is not None
    assert old_active.match.body == old_spec.body
    assert old_active.request.boundary == initial.selection.boundary

    replacement = catalog.activate_snapshot(
        scope_id=scope_id,
        snapshot_id=new_bundle.snapshot.snapshot_id,
        expected_current_snapshot_id=old_bundle.snapshot.snapshot_id,
        expected_current_revision=initial.selection.revision,
        expected_current_activation_id=initial.selection.activation_id,
        actor="integration-test",
        reason="validated replacement",
    )
    assert replacement.changed is True
    assert replacement.event.operation == "replace"
    assert replacement.event.previous_snapshot_id == old_bundle.snapshot.snapshot_id
    assert replacement.event.previous_activation_id == initial.event.activation_id
    assert replacement.selection.revision == 2

    pinned_old = catalog.lookup_article(pinned_old_request)
    current_new = catalog.lookup_active_article(
        scope_id=scope_id,
        law_title=title,
        article_number="第一条",
    )
    assert pinned_old.match is not None
    assert pinned_old.match.body == old_spec.body
    assert current_new.match is not None
    assert current_new.match.body == new_spec.body
    assert current_new.request.boundary == replacement.selection.boundary

    invalid_pins = (
        ArticleLookupBoundary(
            scope_id=scope_id,
            snapshot_id=old_bundle.snapshot.snapshot_id,
            pointer_revision=initial.selection.revision,
            activation_id="f" * 64,
        ),
        ArticleLookupBoundary(
            scope_id=scope_id,
            snapshot_id=new_bundle.snapshot.snapshot_id,
            pointer_revision=initial.selection.revision,
            activation_id=initial.selection.activation_id,
        ),
        ArticleLookupBoundary(
            scope_id=scope_id,
            snapshot_id=old_bundle.snapshot.snapshot_id,
            pointer_revision=replacement.selection.revision,
            activation_id=replacement.selection.activation_id,
        ),
    )
    for boundary in invalid_pins:
        with pytest.raises(
            CatalogUnavailableError,
            match="pinned active snapshot event",
        ):
            catalog.lookup_article(
                ArticleLookupRequest(
                    boundary=boundary,
                    law_title=title,
                    article_number="第一条",
                )
            )

    with pytest.raises(
        SnapshotActivationConflictError,
        match="expected current snapshot",
    ):
        catalog.activate_snapshot(
            scope_id=scope_id,
            snapshot_id=old_bundle.snapshot.snapshot_id,
            expected_current_snapshot_id=old_bundle.snapshot.snapshot_id,
            expected_current_revision=initial.selection.revision,
            expected_current_activation_id=initial.selection.activation_id,
        )
    assert catalog.get_active_snapshot(scope_id) == replacement.selection
    assert len(catalog.list_activation_history(scope_id)) == 2

    with pytest.raises(CatalogContractError, match="required_profile_id"):
        catalog.rollback_snapshot(
            scope_id=scope_id,
            target_snapshot_id=old_bundle.snapshot.snapshot_id,
            expected_current_snapshot_id=replacement.selection.snapshot_id,
            expected_current_revision=replacement.selection.revision,
            expected_current_activation_id=replacement.selection.activation_id,
            required_profile_id=None,  # type: ignore[arg-type]
        )
    assert catalog.get_active_snapshot(scope_id) == replacement.selection
    assert len(catalog.list_activation_history(scope_id)) == 2

    with pytest.raises(CatalogUnavailableError, match="required profile"):
        catalog.rollback_snapshot(
            scope_id=scope_id,
            target_snapshot_id=old_bundle.snapshot.snapshot_id,
            expected_current_snapshot_id=replacement.selection.snapshot_id,
            expected_current_revision=replacement.selection.revision,
            expected_current_activation_id=replacement.selection.activation_id,
            required_profile_id="f" * 64,
        )
    assert catalog.get_active_snapshot(scope_id) == replacement.selection
    assert len(catalog.list_activation_history(scope_id)) == 2

    rollback = catalog.rollback_snapshot(
        scope_id=scope_id,
        target_snapshot_id=old_bundle.snapshot.snapshot_id,
        expected_current_snapshot_id=new_bundle.snapshot.snapshot_id,
        expected_current_revision=replacement.selection.revision,
        expected_current_activation_id=replacement.selection.activation_id,
        required_profile_id=old_bundle.embedding_profile.profile_id,
        actor="integration-test",
        reason="verified rollback",
    )
    assert rollback.changed is True
    assert rollback.event.operation == "rollback"
    assert rollback.selection.revision == 3
    assert rollback.event.previous_activation_id == replacement.event.activation_id
    restored = catalog.lookup_active_article(
        scope_id=scope_id,
        law_title=title,
        article_number="第一条",
    )
    assert restored.match is not None
    assert restored.match.body == old_spec.body

    with pytest.raises(
        SnapshotActivationConflictError,
        match="expected current snapshot",
    ):
        catalog.activate_snapshot(
            scope_id=scope_id,
            snapshot_id=new_bundle.snapshot.snapshot_id,
            expected_current_snapshot_id=initial.selection.snapshot_id,
            expected_current_revision=initial.selection.revision,
            expected_current_activation_id=initial.selection.activation_id,
        )
    assert catalog.get_active_snapshot(scope_id) == rollback.selection

    history = catalog.list_activation_history(scope_id)
    assert [event.revision for event in history] == [3, 2, 1]
    assert [event.operation for event in history] == [
        "rollback",
        "replace",
        "initial_activate",
    ]
    with pytest.raises(CatalogUnavailableError, match="required profile"):
        catalog.activate_snapshot(
            scope_id=scope_id,
            snapshot_id=old_bundle.snapshot.snapshot_id,
            expected_current_snapshot_id=rollback.selection.snapshot_id,
            expected_current_revision=rollback.selection.revision,
            expected_current_activation_id=rollback.selection.activation_id,
            required_profile_id="f" * 64,
        )
    assert len(catalog.list_activation_history(scope_id)) == 3

    unchanged = catalog.activate_snapshot(
        scope_id=scope_id,
        snapshot_id=old_bundle.snapshot.snapshot_id,
        expected_current_snapshot_id=old_bundle.snapshot.snapshot_id,
        expected_current_revision=rollback.selection.revision,
        expected_current_activation_id=rollback.selection.activation_id,
        required_profile_id=old_bundle.embedding_profile.profile_id,
    )
    assert unchanged.changed is False
    assert unchanged.selection == rollback.selection
    assert len(catalog.list_activation_history(scope_id)) == 3

    with pytest.raises(CatalogUnavailableError, match="required profile"):
        catalog.activate_snapshot(
            scope_id=scope_id,
            snapshot_id=new_bundle.snapshot.snapshot_id,
            expected_current_snapshot_id=old_bundle.snapshot.snapshot_id,
            expected_current_revision=rollback.selection.revision,
            expected_current_activation_id=rollback.selection.activation_id,
            required_profile_id="f" * 64,
        )
    assert catalog.get_active_snapshot(scope_id) == rollback.selection
    assert len(catalog.list_activation_history(scope_id)) == 3

    with migrated_engine.connect() as connection:
        statuses = dict(
            connection.execute(
                select(
                    corpus_snapshots.c.snapshot_id,
                    corpus_snapshots.c.status,
                ).where(corpus_snapshots.c.scope_id == scope_id)
            ).all()
        )
        pointer = (
            connection.execute(
                select(active_snapshot_pointers).where(
                    active_snapshot_pointers.c.scope_id == scope_id
                )
            )
            .mappings()
            .one()
        )
        latest_event = (
            connection.execute(
                select(snapshot_activation_events).where(
                    snapshot_activation_events.c.scope_id == scope_id,
                    snapshot_activation_events.c.revision == 3,
                )
            )
            .mappings()
            .one()
        )
    assert statuses == {
        old_bundle.snapshot.snapshot_id: "active",
        new_bundle.snapshot.snapshot_id: "validated",
    }
    assert pointer["snapshot_id"] == old_bundle.snapshot.snapshot_id
    assert pointer["revision"] == 3
    assert pointer["activation_id"] == latest_event["activation_id"]
    assert pointer["updated_at"] == latest_event["occurred_at"]


def test_concurrent_same_scope_activations_commit_one_full_cas_winner(
    migrated_engine: Engine,
) -> None:
    scope_id = "scope-catalog-concurrent-activation"
    bundles = {}
    for label in ("initial", "candidate-a", "candidate-b"):
        spec = _ArticleSpec(
            law_id=f"catalog-concurrent-{label}-law",
            version_id=f"catalog-concurrent-{label}-law-v1",
            title=f"目录并发激活测试法-{label}",
            article_id=f"catalog-concurrent-{label}-article-1",
            article_number="第一条",
            body=f"并发激活候选正文-{label}。",
            valid_from="2024-01-01",
        )
        bundles[label] = _build_bundle(
            prefix=f"catalog-concurrent-{label}",
            scope_id=scope_id,
            snapshot_id=f"snapshot-catalog-concurrent-{label}",
            specs=[spec],
        )

    importer = PostgresCorpusRepository(migrated_engine)
    for bundle in bundles.values():
        importer.import_bundle(bundle)
    catalog = PostgresLegalCatalogRepository(migrated_engine)
    initial_bundle = bundles["initial"]
    initial = catalog.activate_snapshot(
        scope_id=scope_id,
        snapshot_id=initial_bundle.snapshot.snapshot_id,
        expected_current_snapshot_id=None,
        required_profile_id=initial_bundle.embedding_profile.profile_id,
    )

    start_gate = Barrier(3)

    def compete(label: str):
        start_gate.wait(timeout=10)
        bundle = bundles[label]
        try:
            result = catalog.activate_snapshot(
                scope_id=scope_id,
                snapshot_id=bundle.snapshot.snapshot_id,
                expected_current_snapshot_id=initial.selection.snapshot_id,
                expected_current_revision=initial.selection.revision,
                expected_current_activation_id=initial.selection.activation_id,
                required_profile_id=bundle.embedding_profile.profile_id,
                actor=f"concurrent-{label}",
            )
        except SnapshotActivationConflictError:
            return "conflict", label, None
        return "committed", label, result

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(compete, label) for label in ("candidate-a", "candidate-b")
        ]
        start_gate.wait(timeout=10)
        outcomes = [future.result(timeout=20) for future in futures]

    committed = [outcome for outcome in outcomes if outcome[0] == "committed"]
    conflicted = [outcome for outcome in outcomes if outcome[0] == "conflict"]
    assert len(committed) == 1
    assert len(conflicted) == 1
    _, winner_label, winner = committed[0]
    _, loser_label, loser = conflicted[0]
    assert winner is not None
    assert loser is None
    assert winner.selection.revision == 2
    assert winner.event.operation == "replace"
    assert winner.event.previous_snapshot_id == initial.selection.snapshot_id
    assert winner.event.previous_activation_id == initial.selection.activation_id
    assert catalog.get_active_snapshot(scope_id) == winner.selection

    history = catalog.list_activation_history(scope_id)
    assert [event.revision for event in history] == [2, 1]
    assert [event.operation for event in history] == ["replace", "initial_activate"]
    assert [event.target_snapshot_id for event in history] == [
        bundles[winner_label].snapshot.snapshot_id,
        initial.selection.snapshot_id,
    ]
    with migrated_engine.connect() as connection:
        statuses = dict(
            connection.execute(
                select(
                    corpus_snapshots.c.snapshot_id,
                    corpus_snapshots.c.status,
                ).where(corpus_snapshots.c.scope_id == scope_id)
            ).all()
        )
    assert statuses == {
        initial.selection.snapshot_id: "validated",
        bundles[winner_label].snapshot.snapshot_id: "active",
        bundles[loser_label].snapshot.snapshot_id: "validated",
    }


def test_deferred_activation_guard_rejects_partial_state_change(
    migrated_engine: Engine,
) -> None:
    scope_id = "scope-catalog-activation-guard"
    spec = _ArticleSpec(
        law_id="catalog-activation-guard-law",
        version_id="catalog-activation-guard-law-v1",
        title="目录激活保护测试法",
        article_id="catalog-activation-guard-article-1",
        article_number="第一条",
        body="原子状态保护正文。",
        valid_from="2024-01-01",
    )
    bundle = _build_bundle(
        prefix="catalog-activation-guard",
        scope_id=scope_id,
        snapshot_id="snapshot-catalog-activation-guard",
        specs=[spec],
    )
    PostgresCorpusRepository(migrated_engine).import_bundle(bundle)
    catalog = PostgresLegalCatalogRepository(migrated_engine)
    activated = catalog.activate_snapshot(
        scope_id=scope_id,
        snapshot_id=bundle.snapshot.snapshot_id,
        expected_current_snapshot_id=None,
    )

    with pytest.raises(DBAPIError, match="active pointer"):
        with migrated_engine.begin() as connection:
            connection.execute(
                update(corpus_snapshots)
                .where(
                    corpus_snapshots.c.scope_id == scope_id,
                    corpus_snapshots.c.snapshot_id == bundle.snapshot.snapshot_id,
                )
                .values(status="validated")
            )

    assert catalog.get_active_snapshot(scope_id) == activated.selection
    assert len(catalog.list_activation_history(scope_id)) == 1


def test_active_lookup_keeps_its_pinned_snapshot_during_replace_and_archive(
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope_id = "scope-catalog-pinned-read"
    title = "目录固定读取测试法"
    old_spec = _ArticleSpec(
        law_id="catalog-pinned-law",
        version_id="catalog-pinned-law-v1",
        title=title,
        article_id="catalog-pinned-v1-article-1",
        article_number="第一条",
        body="请求开始时固定的旧正文。",
        valid_from="2024-01-01",
    )
    new_spec = _ArticleSpec(
        law_id="catalog-pinned-law",
        version_id="catalog-pinned-law-v2",
        title=title,
        article_id="catalog-pinned-v2-article-1",
        article_number="第一条",
        body="切换后的新正文。",
        valid_from="2025-01-01",
    )
    old_bundle = _build_bundle(
        prefix="catalog-pinned-old",
        scope_id=scope_id,
        snapshot_id="snapshot-catalog-pinned-old",
        specs=[old_spec],
    )
    new_bundle = _build_bundle(
        prefix="catalog-pinned-new",
        scope_id=scope_id,
        snapshot_id="snapshot-catalog-pinned-new",
        specs=[new_spec],
    )
    importer = PostgresCorpusRepository(migrated_engine)
    importer.import_bundle(old_bundle)
    importer.import_bundle(new_bundle)
    catalog = PostgresLegalCatalogRepository(migrated_engine)
    initial = catalog.activate_snapshot(
        scope_id=scope_id,
        snapshot_id=old_bundle.snapshot.snapshot_id,
        expected_current_snapshot_id=None,
    )

    pointer_read = Event()
    continue_read = Event()
    original_active_snapshot = catalog._active_snapshot

    def paused_active_snapshot(connection, resolved_scope_id, *, for_update=False):
        result = original_active_snapshot(
            connection,
            resolved_scope_id,
            for_update=for_update,
        )
        if not for_update:
            pointer_read.set()
            if not continue_read.wait(timeout=10):
                raise RuntimeError("timed out waiting to resume pinned lookup")
        return result

    monkeypatch.setattr(catalog, "_active_snapshot", paused_active_snapshot)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(
            catalog.lookup_active_article,
            scope_id=scope_id,
            law_title=title,
            article_number="第一条",
        )
        assert pointer_read.wait(timeout=10)
        try:
            replacement = catalog.activate_snapshot(
                scope_id=scope_id,
                snapshot_id=new_bundle.snapshot.snapshot_id,
                expected_current_snapshot_id=initial.selection.snapshot_id,
                expected_current_revision=initial.selection.revision,
                expected_current_activation_id=initial.selection.activation_id,
            )
            with migrated_engine.begin() as connection:
                connection.execute(
                    update(corpus_snapshots)
                    .where(
                        corpus_snapshots.c.scope_id == scope_id,
                        corpus_snapshots.c.snapshot_id
                        == old_bundle.snapshot.snapshot_id,
                        corpus_snapshots.c.status == "validated",
                    )
                    .values(status="archived")
                )
        finally:
            continue_read.set()
        pinned = pending.result(timeout=10)

    assert pinned.match is not None
    assert pinned.match.body == old_spec.body
    assert pinned.request.boundary == initial.selection.boundary
    current = catalog.lookup_active_article(
        scope_id=scope_id,
        law_title=title,
        article_number="第一条",
    )
    assert current.match is not None
    assert current.match.body == new_spec.body
    assert current.request.boundary == replacement.selection.boundary
