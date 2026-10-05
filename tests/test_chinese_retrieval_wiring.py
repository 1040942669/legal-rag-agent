"""Actual factory wiring contracts, using only fictional text and no models."""
from pathlib import Path

import pytest

from legal_rag.bm25_settings import BM25Settings
from legal_rag.chinese_bm25 import ChineseBM25Retriever
from legal_rag.models import Chunk
from legal_rag.retrieval import build_retriever


@pytest.mark.parametrize("kind", ["bm25", "rrf", "hybrid"])
def test_shared_builder_wires_mature_engine_into_each_lexical_path(kind, monkeypatch):
    class FakeDense:
        name = "dense"

        def __init__(self, *args, **kwargs):
            pass

        def retrieve(self, query, top_k=5):
            return []

    monkeypatch.setattr("legal_rag.retrieval.DenseRetriever", FakeDense)
    settings = BM25Settings(lexical_profile="bm25s-jieba-precise-v1", k1=1.1, b=.3, hmm=False)
    chunks = [Chunk("a", "合成合同义务", ["合成法"], ["第一条"], [], [], "article")]
    built = build_retriever(kind, chunks, bm25_settings=settings)
    lexical = built if kind == "bm25" else built._bm25
    assert type(lexical) is ChineseBM25Retriever
    assert lexical.config_identity == settings.identity
    assert built.retrieve("合同")[0].chunk is chunks[0]


def test_experiment_rejects_actual_engine_drift_without_relaxing_contract(tmp_path):
    from dataclasses import replace
    from legal_rag.experiment_lifecycle import build_lifecycle_plan, build_corpus_snapshot, _runtime_factory
    from legal_rag.experiment_adapter import LegalEvaluationRuntimeFactory
    from legal_rag.experiment_runtime import ExperimentContractError
    root = Path(__file__).resolve().parents[1]
    settings = BM25Settings(lexical_profile="bm25s-jieba-precise-v1", hmm=False)
    manifest = build_lifecycle_plan(experiment_id="actual-engine-drift", repository_root=root,
                                    bm25_settings=settings)["manifest"]
    corpus = build_corpus_snapshot(root / "tests/fixtures/synthetic/synthetic_non_law.txt",
                                   repository_root=root, require_offline_fixture=True)
    factory = _runtime_factory(manifest, corpus=corpus, cache_directory=tmp_path)
    changed = ChineseBM25Retriever(corpus.chunks, mode="search", hmm=False)
    with pytest.raises(ExperimentContractError, match="actual BM25"):
        LegalEvaluationRuntimeFactory(replace(factory.spec, retriever=changed))


def test_api_selectors_accept_only_declared_mature_profiles():
    from pydantic import ValidationError
    from legal_rag.api.schemas import RetrievalOptions
    for profile in ("bm25s-jieba-precise-v1", "bm25s-jieba-search-v1",
                    "bm25s-sklearn-char-v1", "bm25s-sklearn-char-bigram-v1"):
        assert RetrievalOptions(lexical_profile=profile).lexical_profile == profile
    with pytest.raises(ValidationError):
        RetrievalOptions(lexical_profile="arbitrary-network-service")
