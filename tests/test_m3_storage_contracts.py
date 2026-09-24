from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

import legal_rag.embedding_contracts as embedding_contracts_module
import legal_rag.embeddings as embeddings_module
from legal_rag.chunking import article_chunks, neighbor_chunks
from legal_rag.embeddings import (
    EMBEDDING_CACHE_SCHEMA_VERSION,
    BoundQueryEncoder,
    EmbeddingCache,
    EmbeddingModelConfig,
    chunk_corpus_fingerprint,
    embedding_contract_fingerprint,
    embedding_profile_identity,
    build_bound_query_encoder,
)
from legal_rag.models import LawArticle
from legal_rag.storage.contracts import (
    LawVersionSpec,
    StorageContractError,
    build_storage_import_bundle,
    validate_storage_import_bundle,
)


def _articles() -> list[LawArticle]:
    return [
        LawArticle(
            article_id="article-1",
            law_name="测试法",
            article_number="第一条",
            body="第一条正文。",
            raw_text="第一条 第一条正文。",
            source_file="fixtures/test-law.txt",
            line_no=1,
            parse_status="from_filename",
        ),
        LawArticle(
            article_id="article-2",
            law_name="测试法",
            article_number="第二条",
            body="第二条正文。",
            raw_text="第二条 第二条正文。",
            source_file="fixtures/test-law.txt",
            line_no=2,
            parse_status="from_filename",
        ),
    ]


def _model_config(
    *,
    dimensions: int = 3,
    normalize: bool = True,
    revision: str = "fixture-rev-1",
    trust_remote_code: bool = False,
) -> EmbeddingModelConfig:
    return EmbeddingModelConfig(
        key="fixture-embedding",
        provider="fixture",
        model_name="fixture/model",
        role="retrieval",
        revision=revision,
        normalize=normalize,
        trust_remote_code=trust_remote_code,
        dimensions=dimensions,
        query_prefix="query: ",
        document_prefix="passage: ",
        embed_with_metadata=True,
    )


def _cache(chunks, model_config, vectors) -> EmbeddingCache:
    matrix = np.asarray(vectors, dtype=np.float32)
    metadata = {
        "schema_version": EMBEDDING_CACHE_SCHEMA_VERSION,
        "chunk_count": len(chunks),
        "vector_count": len(chunks),
        "dimension": int(matrix.shape[1]),
        "dtype": str(matrix.dtype),
        "chunk_strategy": chunks[0].strategy,
        "chunk_fingerprint": chunk_corpus_fingerprint(chunks),
        "embedding_key": model_config.key,
        "provider": model_config.provider,
        "model_name": model_config.model_name,
        "revision": model_config.revision,
        "normalize": model_config.normalize,
        "trust_remote_code": model_config.trust_remote_code,
        "query_prefix": model_config.query_prefix,
        "document_prefix": model_config.document_prefix,
        "embed_with_metadata": model_config.embed_with_metadata,
        "embedding_contract_fingerprint": embedding_contract_fingerprint(model_config),
    }
    return EmbeddingCache(
        cache_dir=Path("fixture-cache"),
        metadata=metadata,
        chunk_ids=[chunk.chunk_id for chunk in chunks],
        vectors=matrix,
    )


def _build_bundle(
    *,
    articles=None,
    chunks=None,
    vectors=None,
    dimensions=3,
    model_revision="fixture-rev-1",
    config_revision=None,
    article_version_ids=None,
    laws=None,
    normalize=True,
    trust_remote_code=False,
):
    if articles is None:
        articles = _articles()
    if chunks is None:
        chunks = article_chunks(articles)
    if vectors is None:
        vectors = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    model_config = _model_config(
        dimensions=dimensions,
        normalize=normalize,
        revision=model_revision if config_revision is None else config_revision,
        trust_remote_code=trust_remote_code,
    )
    cache = _cache(chunks, model_config, vectors)
    if laws is None:
        laws = [
            LawVersionSpec(
                law_id="law-test",
                version_id="law-test-v1",
                title="测试法",
                verification_status="unknown",
                source_ref="fixtures/test-law.txt",
            )
        ]
    return build_storage_import_bundle(
        snapshot_id="snapshot-fixture-v1",
        scope_id="public-cn-law",
        source_manifest={
            "schema_version": 1,
            "source": "synthetic-fixture",
            "files": ["fixtures/test-law.txt"],
        },
        laws=laws,
        articles=articles,
        chunks=chunks,
        embedding_cache=cache,
        model_config=model_config,
        model_revision=model_revision,
        chunk_recipe={"strategy": chunks[0].strategy},
        article_version_ids=article_version_ids,
    )


def test_build_bundle_is_deterministic_immutable_and_complete() -> None:
    first = _build_bundle()
    second = _build_bundle()

    assert first.bundle_hash == second.bundle_hash
    assert first.corpus_hash == second.corpus_hash
    assert first.snapshot.source_manifest_hash == second.snapshot.source_manifest_hash
    assert len(first.law_versions) == 1
    assert len(first.articles) == 2
    assert len(first.chunks) == 2
    assert len(first.chunk_articles) == 2
    assert len(first.snapshot_chunks) == 2
    assert len(first.embeddings) == 2
    assert first.embedding_profile.dimensions == 3
    assert first.embedding_profile.revision == "fixture-rev-1"
    assert all(len(item.embedding_hash) == 64 for item in first.embeddings)
    assert first.law_versions[0].valid_from is None
    assert first.law_versions[0].valid_to is None
    assert first.law_versions[0].verification_status == "unknown"

    with pytest.raises(FrozenInstanceError):
        first.snapshot.scope_id = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        first.chunks[0] = first.chunks[1]  # type: ignore[index]


def test_bundle_preserves_ordered_multi_article_relations() -> None:
    articles = _articles()
    chunks = neighbor_chunks(articles, window=2, stride=2)
    bundle = _build_bundle(
        chunks=chunks,
        vectors=[[0.57735026, 0.57735026, 0.57735026]],
    )

    assert [item.article_id for item in bundle.chunk_articles] == [
        "article-1",
        "article-2",
    ]
    assert [item.ordinal for item in bundle.chunk_articles] == [0, 1]


def test_bundle_rejects_duplicate_chunk_ids_before_database_write() -> None:
    chunks = article_chunks(_articles())
    duplicate = replace(chunks[1], chunk_id=chunks[0].chunk_id)

    with pytest.raises(StorageContractError, match="duplicate chunk_id"):
        _build_bundle(chunks=[chunks[0], duplicate])


def test_bundle_rejects_non_finite_or_wrong_dimension_vectors() -> None:
    with pytest.raises((StorageContractError, ValueError), match="finite|NaN|infinity"):
        _build_bundle(vectors=[[1.0, 0.0, 0.0], [float("nan"), 0.0, 1.0]])

    articles = _articles()
    chunks = article_chunks(articles)
    model_config = _model_config(dimensions=4)
    cache = _cache(
        chunks,
        model_config,
        [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
    )
    with pytest.raises((StorageContractError, ValueError), match="dimension"):
        build_storage_import_bundle(
            snapshot_id="snapshot-fixture-v1",
            scope_id="public-cn-law",
            source_manifest={"schema_version": 1, "source": "fixture"},
            laws=[
                LawVersionSpec(
                    law_id="law-test",
                    version_id="law-test-v1",
                    title="测试法",
                    verification_status="unknown",
                    source_ref="fixtures/test-law.txt",
                )
            ],
            articles=articles,
            chunks=chunks,
            embedding_cache=cache,
            model_config=model_config,
            model_revision="fixture-rev-1",
            chunk_recipe={"strategy": "article"},
        )


def test_normalized_profile_rejects_non_unit_and_zero_vectors() -> None:
    with pytest.raises(StorageContractError, match="L2 norm"):
        _build_bundle(vectors=[[0.5, 0.5, 0.0], [0.0, 1.0, 0.0]])
    with pytest.raises(StorageContractError, match="L2 norm"):
        _build_bundle(vectors=[[0.0, 0.0, 0.0], [0.0, 1.0, 0.0]])

    unnormalized = _build_bundle(
        vectors=[[0.5, 0.5, 0.0], [0.0, 0.0, 0.0]], normalize=False
    )
    assert unnormalized.embedding_profile.normalization is False


def test_bound_query_encoder_uses_the_same_revision_and_vector_contract() -> None:
    model = _model_config(revision="fixture-rev-bound")
    profile = embedding_profile_identity(model, expected_dimension=3)
    same_profile = embedding_profile_identity(model, expected_dimension=3)
    other_profile = embedding_profile_identity(
        replace(model, revision="fixture-rev-other"), expected_dimension=3
    )

    assert profile == same_profile
    assert profile.profile_id != other_profile.profile_id

    class _Delegate:
        def __init__(self, vector) -> None:
            self.vector = vector

        def encode_query(self, text: str):
            return self.vector

    encoded = BoundQueryEncoder(_Delegate([1.0, 0.0, 0.0]), profile).encode_query(
        "query"
    )
    assert encoded.dtype == np.dtype("float32")
    with pytest.raises(ValueError, match="L2 norm"):
        BoundQueryEncoder(_Delegate([0.5, 0.5, 0.0]), profile).encode_query("query")
    with pytest.raises(ValueError, match="revision"):
        embedding_profile_identity(replace(model, revision=""), expected_dimension=3)


def test_bound_query_encoder_factory_requires_provider_verifiable_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = "a" * 40
    model = replace(
        _model_config(revision=revision),
        provider="sentence_transformers",
        model_name="fixture-owner/fixture-model",
    )
    captured: dict[str, object] = {}

    class _Delegate:
        def encode_query(self, text: str):
            return [1.0, 0.0, 0.0]

    def fake_build_encoder(config, **kwargs):
        captured["config"] = config
        captured.update(kwargs)
        return _Delegate()

    monkeypatch.setattr(embeddings_module, "build_encoder", fake_build_encoder)
    encoder = build_bound_query_encoder(model, expected_dimension=3)

    assert encoder.profile.revision == revision
    assert captured["config"] == model
    assert captured["expected_dimension"] == 3


@pytest.mark.parametrize(
    ("provider", "model_name", "revision", "message"),
    [
        ("sentence_transformers", "owner/model", "main", "commit SHA"),
        ("sentence_transformers", "owner/model", "a" * 12, "commit SHA"),
        ("sentence_transformers", "owner/model", "A" * 40, "commit SHA"),
        (
            "sentence_transformers",
            "C:\\models\\local-model",
            "a" * 40,
            "Hub repository ID",
        ),
        ("siliconflow", "remote/model", "provider-alias-v1", "does not attest"),
    ],
)
def test_bound_query_encoder_factory_rejects_unverifiable_models_before_loading(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    model_name: str,
    revision: str,
    message: str,
) -> None:
    model = replace(
        _model_config(revision=revision),
        provider=provider,
        model_name=model_name,
    )

    def forbidden_build_encoder(*args, **kwargs):
        raise AssertionError("provider loading must not begin")

    monkeypatch.setattr(
        embeddings_module,
        "build_encoder",
        forbidden_build_encoder,
    )
    with pytest.raises(ValueError, match=message):
        build_bound_query_encoder(model, expected_dimension=3)

    # Import identities remain declarative and support fixtures/non-hex
    # revisions; provider attestation is enforced only by the serving factory.
    assert (
        embedding_profile_identity(
            replace(model, provider="fixture", revision="fixture-revision"),
            expected_dimension=3,
        ).revision
        == "fixture-revision"
    )


def test_bound_query_encoder_rejects_repo_shaped_local_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "owner" / "model").mkdir(parents=True)
    model = replace(
        _model_config(revision="a" * 40),
        provider="sentence_transformers",
        model_name="owner/model",
    )

    monkeypatch.setattr(
        embeddings_module,
        "build_encoder",
        lambda *args, **kwargs: pytest.fail("local model loading must not begin"),
    )
    with pytest.raises(ValueError, match="Hub repository ID"):
        build_bound_query_encoder(model, expected_dimension=3)

    with pytest.raises(ValueError, match="trust_remote_code"):
        build_bound_query_encoder(
            replace(
                model,
                model_name="owner/remote-model",
                trust_remote_code=True,
            ),
            expected_dimension=3,
        )


def test_encoder_adapter_version_invalidates_profile_and_cache_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _model_config(revision="fixture-adapter-version")
    original_profile = embedding_profile_identity(model, expected_dimension=3)
    original_profile_id = original_profile.profile_id
    original_cache_contract = embedding_contract_fingerprint(model)

    monkeypatch.setattr(
        embedding_contracts_module,
        "EMBEDDING_ENCODER_ADAPTER_VERSION",
        "bound-encoder-v2",
    )
    monkeypatch.setattr(
        embeddings_module,
        "EMBEDDING_ENCODER_ADAPTER_VERSION",
        "bound-encoder-v2",
    )

    assert (
        embedding_profile_identity(model, expected_dimension=3).profile_id
        != original_profile_id
    )
    assert embedding_contract_fingerprint(model) != original_cache_contract


def test_bundle_rejects_vector_above_pgvector_storage_limit() -> None:
    oversized = np.zeros((2, 16_001), dtype=np.float32)
    oversized[:, 0] = 1.0

    with pytest.raises(StorageContractError, match="storage limit 16000"):
        _build_bundle(vectors=oversized, dimensions=16_001)


def test_bundle_requires_explicit_model_revision_and_known_article_relations() -> None:
    with pytest.raises(StorageContractError, match="model_revision"):
        _build_bundle(model_revision="")
    with pytest.raises(StorageContractError, match="revision"):
        _build_bundle(
            model_revision="fixture-rev-2",
            config_revision="fixture-rev-1",
        )
    with pytest.raises(StorageContractError, match="trust_remote_code"):
        _build_bundle(trust_remote_code=True)

    articles = _articles()
    chunks = article_chunks(articles)
    chunks[0].metadata["article_ids"] = ["missing-article"]
    with pytest.raises(StorageContractError, match="unknown article_id"):
        _build_bundle(chunks=chunks)


def test_law_version_rejects_invalid_half_open_interval() -> None:
    with pytest.raises(StorageContractError, match="valid_from.*valid_to"):
        LawVersionSpec(
            law_id="law-test",
            version_id="law-test-v1",
            title="测试法",
            verification_status="verified",
            source_ref="fixtures/test-law.txt",
            valid_from="2025-01-01",
            valid_to="2025-01-01",
        )

    with pytest.raises(StorageContractError, match="ISO date"):
        LawVersionSpec(
            law_id="law-test",
            version_id="law-test-v1",
            title="测试法",
            verification_status="verified",
            source_ref="fixtures/test-law.txt",
            valid_from=datetime(2025, 1, 1, 12, 30),
        )


def test_bundle_rejects_duplicate_article_number_and_unknown_mapping_key() -> None:
    articles = _articles()
    duplicate_number = replace(articles[1], article_number=articles[0].article_number)
    with pytest.raises(StorageContractError, match="duplicate article_number"):
        _build_bundle(articles=[articles[0], duplicate_number])

    with pytest.raises(StorageContractError, match="unknown article_id"):
        _build_bundle(article_version_ids={"missing-article": "law-test-v1"})


def test_explicit_mapping_isolates_same_title_across_law_versions() -> None:
    articles = _articles()
    articles[1] = replace(articles[1], article_number="第一条")
    laws = [
        LawVersionSpec(
            law_id="law-test",
            version_id="law-test-v1",
            title="测试法",
            verification_status="verified",
            source_ref="fixtures/test-law-v1.txt",
            valid_from="2024-01-01",
            valid_to="2025-01-01",
        ),
        LawVersionSpec(
            law_id="law-test",
            version_id="law-test-v2",
            title="测试法",
            verification_status="verified",
            source_ref="fixtures/test-law-v2.txt",
            valid_from="2025-01-01",
        ),
    ]
    bundle = _build_bundle(
        articles=articles,
        laws=laws,
        article_version_ids={
            "article-1": "law-test-v1",
            "article-2": "law-test-v2",
        },
    )

    assert [item.version_id for item in bundle.articles] == [
        "law-test-v1",
        "law-test-v2",
    ]
    assert bundle.law_versions[0].content_hash != bundle.law_versions[1].content_hash


def test_bundle_rejects_surrounding_identity_whitespace() -> None:
    chunks = article_chunks(_articles())
    padded = replace(chunks[0], chunk_id=f" {chunks[0].chunk_id} ")

    with pytest.raises(StorageContractError, match="surrounding whitespace"):
        _build_bundle(chunks=[padded, chunks[1]])


def test_bundle_rejects_implicit_float64_conversion() -> None:
    articles = _articles()
    chunks = article_chunks(articles)
    model = _model_config()
    cache = _cache(chunks, model, [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    cache = replace(cache, vectors=cache.vectors.astype(np.float64))

    with pytest.raises((StorageContractError, ValueError), match="dtype|float32"):
        build_storage_import_bundle(
            snapshot_id="snapshot-float64",
            scope_id="public-cn-law",
            source_manifest={"schema_version": 1, "source": "fixture"},
            laws=[
                LawVersionSpec(
                    law_id="law-test",
                    version_id="law-test-v1",
                    title="测试法",
                    verification_status="unknown",
                    source_ref="fixtures/test-law.txt",
                )
            ],
            articles=articles,
            chunks=chunks,
            embedding_cache=cache,
            model_config=model,
            model_revision="fixture-rev-1",
            chunk_recipe={"strategy": "article"},
        )


def test_bundle_validator_detects_dataclass_tampering() -> None:
    bundle = _build_bundle()
    tampered_chunk = replace(bundle.chunks[0], text="被篡改的文本")
    tampered_bundle = replace(bundle, chunks=(tampered_chunk, *bundle.chunks[1:]))
    with pytest.raises(StorageContractError, match="content hash mismatch"):
        validate_storage_import_bundle(tampered_bundle)

    tampered_embedding = replace(bundle.embeddings[0], embedding=(0.0, 0.0, 1.0))
    tampered_bundle = replace(
        bundle, embeddings=(tampered_embedding, *bundle.embeddings[1:])
    )
    with pytest.raises(StorageContractError, match="embedding.*hash mismatch"):
        validate_storage_import_bundle(tampered_bundle)

    non_unit_embedding = replace(bundle.embeddings[0], embedding=(0.5, 0.5, 0.0))
    non_unit_bundle = replace(
        bundle, embeddings=(non_unit_embedding, *bundle.embeddings[1:])
    )
    with pytest.raises(StorageContractError, match="L2 norm"):
        validate_storage_import_bundle(non_unit_bundle)
