"""The general lexical profile has no scene expansion or semantic promise."""

from __future__ import annotations

import pytest

from legal_rag.models import Chunk
from legal_rag.retrieval import BM25Retriever, bm25_text_versions, build_retriever, tokenize


def chunk():
    return Chunk("fictional", "甲，乙 A 丙丁；购买商品退货生态生态环境。",
                 ["合成甲法"], ["第一条"], ["fictional.txt"], [1], "article")


@pytest.mark.parametrize("query", [
    "网上买的衣服收到后不喜欢，没有质量问题也能退吗？",
    "网上买了雨伞，想退货", "网上下单一件衣服，可以退吗？",
    "我从直播间买的衣服，不想要了怎么办？", "购买股票怎样登记？",
])
def test_generic_never_calls_or_adds_shopping_expansion(monkeypatch, query):
    def forbidden(*args, **kwargs):
        raise AssertionError("scene expansion must not execute")
    monkeypatch.setattr("legal_rag.retrieval.lexical_expansion_terms", forbidden)
    result = BM25Retriever([chunk()], lexical_profile="generic-v3").retrieve(query)[0]
    assert result.trace["lexical_expansion_terms"] == []
    assert result.trace["lexical_profile"] == "generic-v3"


def test_generic_contiguous_boundary_and_query_dedup_keep_document_frequency():
    retriever = BM25Retriever([chunk()], lexical_profile="generic-v3")
    assert "甲乙" not in retriever.doc_tokens[0]
    assert "丙丁" in retriever.doc_tokens[0]
    assert retriever.term_freqs[0]["生态"] == 2
    assert retriever.retrieve("生态环境")[0].score == retriever.retrieve("生态环境，生态环境")[0].score


def test_generic_preserves_query_negation_references_and_has_unique_versions():
    tokens = tokenize("没有购买《合成甲法》第十条商品", profile="generic-v3")
    assert all(term in tokens for term in ("没有", "合成甲法", "第十条"))
    assert bm25_text_versions("generic-v3") not in {
        bm25_text_versions("legacy-v1"), bm25_text_versions("local-lexical-v2")}
    assert build_retriever("bm25", [chunk()], bm25_lexical_profile="generic-v3").lexical_profile == "generic-v3"
    assert BM25Retriever([chunk()]).lexical_profile == "legacy-v1"


def test_corpus_title_catalog_is_an_immutable_tuple_without_scene_inference():
    retriever = BM25Retriever([chunk()], lexical_profile="generic-v3")
    assert isinstance(retriever.known_law_hints, tuple)
    assert set(retriever.known_law_hints) == {"合成甲法"}
