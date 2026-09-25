from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest

from legal_rag.embedding_contracts import EmbeddingProfileIdentity
from legal_rag.models import Chunk, SearchResult
from legal_rag.retrieval_contracts import RetrievalBoundary, RetrievalContractError
from legal_rag.storage.ann import (
    AnnIndexError,
    AnnIndexBuild,
    AnnIndexUnsupportedError,
    AnnSearchOutcome,
    AnnSearchPolicy,
    AnnUnderfillError,
    HnswIndexSpec,
    PgVectorAnnRetriever,
    PostgresHnswIndexManager,
    _build_id,
    _canonical_receipt_params,
    _index_params,
    _physical_identity,
    _physical_index_name,
)
from legal_rag.storage.contracts import sha256_json
from legal_rag.storage.retrieval import PostgresExactRetrievalRepository


_BUILD_ID = "ann-hnsw-" + "a" * 64
_INDEX_NAME = "ix_ce_hnsw_ip_" + "b" * 32
_MANIFEST_HASH = "c" * 64
_INSTANCE_ID = "b" * 64


def _profile(*, dimensions: int = 3, revision: str = "ann-rev-1"):
    return EmbeddingProfileIdentity(
        provider="fixture",
        model="fixture/ann",
        revision=revision,
        dimensions=dimensions,
        normalization=False,
        query_prefix="",
        document_prefix="",
        embed_with_metadata=False,
    )


def _boundary(profile: EmbeddingProfileIdentity) -> RetrievalBoundary:
    return RetrievalBoundary(
        scope_id="ann-scope",
        snapshot_id="ann-snapshot-v1",
        profile_id=profile.profile_id,
    )


def _result(index: int) -> SearchResult:
    return SearchResult(
        chunk=Chunk(
            chunk_id=f"chunk-{index}",
            text=f"fixture {index}",
            law_names=["测试法"],
            article_numbers=[f"第{index}条"],
            source_files=["fixture.txt"],
            line_nos=[index],
            strategy="article",
        ),
        score=float(index),
        rank=index,
        retriever="fixture",
    )


def _build(profile: EmbeddingProfileIdentity) -> AnnIndexBuild:
    params = _index_params(
        profile_id=profile.profile_id,
        dimensions=profile.dimensions,
        profile_embedding_count=3,
        profile_embedding_manifest_hash=_MANIFEST_HASH,
        physical_instance_id=_INSTANCE_ID,
        pgvector_version="0.8.6",
        postgres_major=18,
        spec=HnswIndexSpec(),
    )
    return AnnIndexBuild(
        build_id=_BUILD_ID,
        snapshot_id="ann-snapshot-v1",
        profile_id=profile.profile_id,
        dimensions=profile.dimensions,
        profile_embedding_count=3,
        profile_embedding_manifest_hash=_MANIFEST_HASH,
        physical_instance_id=_INSTANCE_ID,
        pgvector_version="0.8.6",
        postgres_major=18,
        physical_index_name=str(params["physical_index_name"]),
        index_params_hash=sha256_json(params),
        status="validated",
        reused=False,
    )


def test_hnsw_index_spec_and_search_policy_fail_closed() -> None:
    assert HnswIndexSpec().payload() == {
        "algorithm": "hnsw",
        "distance": "inner_product",
        "operator": "<#>",
        "operator_class": "vector_ip_ops",
        "m": 16,
        "ef_construction": 64,
    }
    with pytest.raises(TypeError, match="m must be an integer"):
        HnswIndexSpec(m=True)
    with pytest.raises(ValueError, match=r"at least 2 \* m"):
        HnswIndexSpec(m=20, ef_construction=32)
    with pytest.raises(ValueError, match="iterative_scan"):
        AnnSearchPolicy(iterative_scan="unsafe")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="iterative_scan"):
        AnnSearchPolicy(iterative_scan="relaxed_order")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="boolean"):
        AnnSearchPolicy(exact_fallback_on_underfill=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="between 1 and 60000"):
        AnnSearchPolicy(exact_fallback_timeout_ms=60_001)


def test_physical_index_identity_is_deterministic_and_profile_isolated() -> None:
    first = _profile(revision="ann-a")
    second = _profile(revision="ann-b")
    default_spec = _physical_identity(
        profile_id=first.profile_id,
        dimensions=first.dimensions,
        profile_embedding_count=3,
        profile_embedding_manifest_hash=_MANIFEST_HASH,
        pgvector_version="0.8.6",
        postgres_major=18,
        spec=HnswIndexSpec(),
    )
    assert default_spec == _physical_identity(
        profile_id=first.profile_id,
        dimensions=first.dimensions,
        profile_embedding_count=3,
        profile_embedding_manifest_hash=_MANIFEST_HASH,
        pgvector_version="0.8.6",
        postgres_major=18,
        spec=HnswIndexSpec(),
    )
    assert default_spec != _physical_identity(
        profile_id=second.profile_id,
        dimensions=second.dimensions,
        profile_embedding_count=3,
        profile_embedding_manifest_hash=_MANIFEST_HASH,
        pgvector_version="0.8.6",
        postgres_major=18,
        spec=HnswIndexSpec(),
    )
    assert default_spec != _physical_identity(
        profile_id=first.profile_id,
        dimensions=first.dimensions,
        profile_embedding_count=3,
        profile_embedding_manifest_hash="d" * 64,
        pgvector_version="0.8.6",
        postgres_major=18,
        spec=HnswIndexSpec(m=24, ef_construction=64),
    )
    assert default_spec != _physical_identity(
        profile_id=first.profile_id,
        dimensions=first.dimensions,
        profile_embedding_count=3,
        profile_embedding_manifest_hash=_MANIFEST_HASH,
        pgvector_version="0.8.7",
        postgres_major=18,
        spec=HnswIndexSpec(),
    )
    default_name = _physical_index_name(physical_instance_id=_INSTANCE_ID)
    assert default_name != _physical_index_name(physical_instance_id="d" * 64)
    assert len(default_name) <= 63

    sql = PostgresHnswIndexManager._create_index_sql(
        physical_name=default_name,
        profile_id=first.profile_id,
        dimensions=first.dimensions,
        spec=HnswIndexSpec(),
    )
    assert "embedding::vector(3)" in sql
    assert "vector_ip_ops" in sql
    assert f"profile_id = '{first.profile_id}'" in sql
    assert "embedding_dimension = 3" in sql
    with pytest.raises(Exception, match="unsafe"):
        PostgresHnswIndexManager._create_index_sql(
            physical_name='unsafe"; DROP TABLE chunks; --',
            profile_id=first.profile_id,
            dimensions=3,
            spec=HnswIndexSpec(),
        )


def test_ann_receipt_identity_is_recomputed_from_exact_canonical_parameters() -> None:
    profile = _profile()
    params = _index_params(
        profile_id=profile.profile_id,
        dimensions=profile.dimensions,
        profile_embedding_count=3,
        profile_embedding_manifest_hash=_MANIFEST_HASH,
        physical_instance_id=_INSTANCE_ID,
        pgvector_version="0.8.6",
        postgres_major=18,
        spec=HnswIndexSpec(),
    )
    params_hash = sha256_json(params)
    row = {
        "build_id": _build_id(
            snapshot_id="ann-snapshot-v1",
            profile_id=profile.profile_id,
            params_hash=params_hash,
        ),
        "snapshot_id": "ann-snapshot-v1",
        "profile_id": profile.profile_id,
        "index_params": params,
        "index_params_hash": params_hash,
    }
    assert _canonical_receipt_params(row) == params

    with pytest.raises(AnnIndexError, match="parameter shape"):
        _canonical_receipt_params(
            {**row, "index_params": {**params, "unexpected": True}}
        )
    with pytest.raises(AnnIndexError, match="identity is not canonical"):
        _canonical_receipt_params({**row, "build_id": "ann-hnsw-" + "f" * 64})
    with pytest.raises(AnnIndexError, match="identity is not canonical"):
        _canonical_receipt_params(
            {
                **row,
                "index_params": {**params, "operator": "<->"},
            }
        )


def test_over_dimension_hnsw_rejected_before_database_access() -> None:
    profile = _profile(dimensions=2_001)

    class _NoDatabaseEngine:
        dialect = SimpleNamespace(name="postgresql")

        def begin(self):
            raise AssertionError(
                "database must not be touched for unsupported dimensions"
            )

    manager = PostgresHnswIndexManager(_NoDatabaseEngine())  # type: ignore[arg-type]
    with pytest.raises(AnnIndexUnsupportedError, match="at most 2000 dimensions"):
        manager.ensure_index(
            filters=_boundary(profile),
            expected_profile=profile,
        )


@pytest.mark.parametrize(
    ("outcome"),
    [
        AnnSearchOutcome(
            results=(),
            requested_top_k=2,
            ann_returned_count=0,
            eligible_count=0,
            exact_returned_count=None,
            status="empty_boundary",
            underfill_reason="boundary_denies_all",
            exact_fallback_used=False,
            build_id=_BUILD_ID,
            physical_index_name=_INDEX_NAME,
        ),
        AnnSearchOutcome(
            results=(_result(1), _result(2)),
            requested_top_k=2,
            ann_returned_count=2,
            eligible_count=None,
            exact_returned_count=None,
            status="ann_complete",
            underfill_reason=None,
            exact_fallback_used=False,
            build_id=_BUILD_ID,
            physical_index_name=_INDEX_NAME,
        ),
        AnnSearchOutcome(
            results=(_result(1),),
            requested_top_k=2,
            ann_returned_count=1,
            eligible_count=None,
            exact_returned_count=None,
            status="ann_underfilled",
            underfill_reason="ann_underfill_unclassified",
            exact_fallback_used=False,
            build_id=_BUILD_ID,
            physical_index_name=_INDEX_NAME,
        ),
        AnnSearchOutcome(
            results=(_result(1), _result(2)),
            requested_top_k=2,
            ann_returned_count=1,
            eligible_count=None,
            exact_returned_count=2,
            status="exact_fallback_complete",
            underfill_reason="ann_scan_budget_exhausted",
            exact_fallback_used=True,
            build_id=_BUILD_ID,
            physical_index_name=_INDEX_NAME,
        ),
        AnnSearchOutcome(
            results=(_result(1),),
            requested_top_k=2,
            ann_returned_count=1,
            eligible_count=1,
            exact_returned_count=1,
            status="exact_fallback_exhausted",
            underfill_reason="eligible_population_below_top_k",
            exact_fallback_used=True,
            build_id=_BUILD_ID,
            physical_index_name=_INDEX_NAME,
        ),
        AnnSearchOutcome(
            results=(_result(1),),
            requested_top_k=2,
            ann_returned_count=1,
            eligible_count=None,
            exact_returned_count=None,
            status="exact_fallback_timed_out",
            underfill_reason="exact_fallback_statement_timeout",
            exact_fallback_used=True,
            build_id=_BUILD_ID,
            physical_index_name=_INDEX_NAME,
        ),
    ],
)
def test_ann_outcome_accepts_each_explainable_state(
    outcome: AnnSearchOutcome,
) -> None:
    assert outcome.requested_top_k == 2


def test_ann_outcome_rejects_inconsistent_cross_state_counts() -> None:
    with pytest.raises(ValueError, match="must fill top_k"):
        AnnSearchOutcome(
            results=(_result(1),),
            requested_top_k=2,
            ann_returned_count=1,
            eligible_count=2,
            exact_returned_count=None,
            status="ann_complete",
            underfill_reason=None,
            exact_fallback_used=False,
            build_id=_BUILD_ID,
            physical_index_name=_INDEX_NAME,
        )
    with pytest.raises(ValueError, match="eligible population"):
        AnnSearchOutcome(
            results=(_result(1),),
            requested_top_k=2,
            ann_returned_count=0,
            eligible_count=1,
            exact_returned_count=0,
            status="exact_fallback_exhausted",
            underfill_reason="eligible_population_below_top_k",
            exact_fallback_used=True,
            build_id=_BUILD_ID,
            physical_index_name=_INDEX_NAME,
        )


def test_ann_outcome_rejects_ambiguous_types_reasons_and_identities() -> None:
    valid = AnnSearchOutcome(
        results=(_result(1), _result(2)),
        requested_top_k=2,
        ann_returned_count=2,
        eligible_count=None,
        exact_returned_count=None,
        status="ann_complete",
        underfill_reason=None,
        exact_fallback_used=False,
        build_id=_BUILD_ID,
        physical_index_name=_INDEX_NAME,
    )
    with pytest.raises(TypeError, match="ann_returned_count"):
        replace(valid, ann_returned_count=True)
    with pytest.raises(TypeError, match="results"):
        replace(valid, results=[_result(1), _result(2)])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="reason"):
        replace(valid, underfill_reason="arbitrary_reason")
    with pytest.raises(ValueError, match="build_id"):
        replace(valid, build_id="build")
    with pytest.raises(ValueError, match="physical_index_name"):
        replace(valid, physical_index_name="index")


def test_exact_statement_timeout_contract_rejects_invalid_budget_before_io() -> None:
    profile = _profile()

    class _NoDatabaseEngine:
        def connect(self):
            raise AssertionError("invalid timeout must fail before database access")

    repository = PostgresExactRetrievalRepository.__new__(
        PostgresExactRetrievalRepository
    )
    repository.engine = _NoDatabaseEngine()  # type: ignore[assignment]
    with pytest.raises(TypeError, match="statement_timeout_ms"):
        repository.search_vector(
            [1.0, 0.0, 0.0],
            top_k=1,
            filters=_boundary(profile),
            expected_profile=profile,
            statement_timeout_ms=True,
        )


def test_list_only_ann_retriever_refuses_partial_underfill() -> None:
    profile = _profile()
    build = _build(profile)
    partial = AnnSearchOutcome(
        results=(_result(1),),
        requested_top_k=2,
        ann_returned_count=1,
        eligible_count=None,
        exact_returned_count=None,
        status="ann_underfilled",
        underfill_reason="ann_underfill_unclassified",
        exact_fallback_used=False,
        build_id=build.build_id,
        physical_index_name=build.physical_index_name,
    )

    class _Encoder:
        calls = 0

        def __init__(self) -> None:
            self.profile = profile

        def encode_query(self, query: str):
            self.calls += 1
            return [1.0, 0.0, 0.0]

    class _Repository:
        def validate_context(self, filters, *, expected_profile, build):
            return expected_profile

        def search_vector(self, *args, **kwargs):
            return partial

    encoder = _Encoder()
    retriever = PgVectorAnnRetriever(
        _Repository(),  # type: ignore[arg-type]
        encoder=encoder,
        filters=_boundary(profile),
        build=build,
        policy=AnnSearchPolicy(exact_fallback_on_underfill=False),
    )
    with pytest.raises(RetrievalContractError, match="top_k"):
        retriever.retrieve("fixture", top_k=True)
    assert encoder.calls == 0
    with pytest.raises(AnnUnderfillError) as captured:
        retriever.retrieve("fixture", top_k=2)
    assert captured.value.outcome is partial
    assert encoder.calls == 1
    with pytest.raises(FrozenInstanceError):
        retriever._boundary = _boundary(profile)


def test_ann_retriever_revalidates_build_before_query_embedding() -> None:
    profile = _profile()
    build = _build(profile)

    class _Encoder:
        calls = 0

        def __init__(self) -> None:
            self.profile = profile

        def encode_query(self, query: str):
            self.calls += 1
            return [1.0, 0.0, 0.0]

    class _Repository:
        validations = 0

        def validate_context(self, filters, *, expected_profile, build):
            self.validations += 1
            if self.validations > 1:
                raise AnnIndexError("HNSW profile generation changed")
            return expected_profile

        def search_vector(self, *args, **kwargs):
            raise AssertionError("search must not run for a stale build")

    encoder = _Encoder()
    repository = _Repository()
    retriever = PgVectorAnnRetriever(
        repository,  # type: ignore[arg-type]
        encoder=encoder,
        filters=_boundary(profile),
        build=build,
    )
    with pytest.raises(AnnIndexError, match="generation changed"):
        retriever.retrieve_with_outcome("fixture", top_k=1)
    assert repository.validations == 2
    assert encoder.calls == 0
