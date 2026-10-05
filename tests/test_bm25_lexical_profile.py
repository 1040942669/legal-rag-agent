from __future__ import annotations

import pytest

from legal_rag.models import Chunk
from legal_rag.retrieval import (
    BM25Retriever,
    bm25_text_versions,
    build_retriever,
    lexical_expansion_terms,
)


def chunk(key: str, text: str) -> Chunk:
    return Chunk(key, text, ["测试法"], ["第一条"], ["fixture.txt"], [1], "article")


def test_candidate_does_not_join_chinese_across_punctuation_or_ascii() -> None:
    retriever = BM25Retriever([chunk("x", "甲，乙 A 丙丁")], lexical_profile="local-lexical-v2")
    assert "甲乙" not in retriever.doc_tokens[0]
    assert "乙丙" not in retriever.doc_tokens[0]
    assert "丙丁" in retriever.doc_tokens[0]


def test_candidate_query_frequency_is_bounded_without_changing_document_tf() -> None:
    retriever = BM25Retriever([chunk("x", "生态生态环境")], lexical_profile="local-lexical-v2")
    once = retriever.retrieve("生态环境")[0]
    repeated = retriever.retrieve("生态环境，生态环境，生态环境")[0]
    assert repeated.score == once.score
    assert retriever.term_freqs[0]["生态"] == 2


def test_candidate_expands_colloquial_shopping_not_law_or_answer() -> None:
    retriever = BM25Retriever([chunk("x", "经营者采用网络销售商品，消费者购买商品后要求退货。")], lexical_profile="local-lexical-v2")
    result = retriever.retrieve("网上买的衣服收到后不喜欢，没有质量问题也能退吗？")[0]
    assert result.trace["lexical_profile"] == "local-lexical-v2"
    assert set(result.trace["lexical_expansion_terms"]) == {"网络", "购买", "商品", "退货"}
    assert result.trace["law_hints"] == []
    assert result.trace["article_hints"] == []
    assert "七日" not in result.trace["lexical_expansion_terms"]
    assert "无需说明理由" not in result.trace["lexical_expansion_terms"]


@pytest.mark.parametrize("question", ["网上查退税怎么申请？", "买通关系后退出比赛", "收买证人不退钱", "手机照片泄露能删吗？", "网上申请退学"])
def test_non_shopping_queries_are_not_expanded(question: str) -> None:
    retriever = BM25Retriever([chunk("x", question)], lexical_profile="local-lexical-v2")
    assert retriever.retrieve(question)[0].trace["lexical_expansion_terms"] == []


def test_offline_purchase_and_negation_do_not_infer_online_or_legal_eligibility() -> None:
    retriever = BM25Retriever([chunk("x", "购买商品退货")], lexical_profile="local-lexical-v2")
    trace = retriever.retrieve("实体店买的衣服不让退，也没有质量问题")[0].trace
    assert "网络" not in trace["lexical_expansion_terms"]
    assert "退货" in trace["lexical_expansion_terms"]
    assert trace["law_hints"] == trace["article_hints"] == []


def test_profile_is_validated_and_builder_propagates_it() -> None:
    chunks = [chunk("x", "甲乙")]
    with pytest.raises(ValueError, match="lexical profile"):
        BM25Retriever(chunks, lexical_profile="ignored-typo")
    retriever = build_retriever("bm25", chunks, bm25_lexical_profile="local-lexical-v2")
    assert retriever.lexical_profile == "local-lexical-v2"


def test_legacy_profile_preserves_original_query_frequency_and_bigrams() -> None:
    retriever = BM25Retriever([chunk("x", "甲，乙")], lexical_profile="legacy-v1")
    assert "甲乙" in retriever.doc_tokens[0]
    assert retriever.retrieve("甲甲")[0].score > retriever.retrieve("甲")[0].score


@pytest.mark.parametrize("question", [
    "我没买衣服，只在网上查询退税方法",
    "我在网上查询退税，不购买商品",
    "衣服没有购买，只是借来的，能退给朋友吗",
    "网购平台涉嫌收买官员，是否构成犯罪",
    "我在网上买衣服，另一个问题是退学，能退吗",
])
def test_negated_purchase_and_separate_topic_are_not_inferred(question: str) -> None:
    assert lexical_expansion_terms(question) == []


def test_negated_channel_does_not_infer_online_purchase() -> None:
    terms = lexical_expansion_terms("不是网上买的衣服，而是实体店购买的，可以退吗")
    assert "网络" not in terms


@pytest.mark.parametrize("question", [
    "网上购买保险后不能退吗",
    "在线购买机票后不让退怎么办",
    "线上购买付费会员服务，不退款怎么办",
    "买了衣服；同事要求退居二线是否合法",
    "买了手机；学校要求退还学费怎么办",
])
def test_unknown_purchase_objects_and_different_sentences_are_not_goods(question: str) -> None:
    assert lexical_expansion_terms(question) == []


@pytest.mark.parametrize("question", [
    "买了衣服，同事要求退居二线是否合法",
    "买了手机，学校要求退还学费怎么办",
])
def test_bare_return_feature_has_a_goods_return_boundary(question: str) -> None:
    assert "退货" not in lexical_expansion_terms(question)


def test_default_profile_configuration_and_lifecycle_identity_match() -> None:
    from legal_rag.config import load_config
    from legal_rag.experiment_lifecycle import _manifest_contracts

    retriever = BM25Retriever([chunk("x", "甲乙")])
    assert load_config()["retrieval"]["bm25_lexical_profile"] == retriever.lexical_profile
    assert load_config("configs/default.yaml")["retrieval"]["bm25_lexical_profile"] == retriever.lexical_profile
    versions = bm25_text_versions(retriever.lexical_profile)
    embedding = _manifest_contracts(top_k=5, judge_enabled=False)["embedding"]
    assert (embedding["query_text_version"], embedding["document_text_version"]) == versions
    assert bm25_text_versions("legacy-v1") != bm25_text_versions("local-lexical-v2")


def test_cli_profile_override_is_effective_and_recorded() -> None:
    from legal_rag.cli import create_retriever, retrieval_metadata
    from legal_rag.config import load_config

    config = load_config()
    config["retrieval"]["bm25_lexical_profile"] = "local-lexical-v2"
    retriever = create_retriever("bm25", [chunk("x", "甲乙")], config, top_k=5, chunk_strategy="article")
    assert retriever.lexical_profile == "local-lexical-v2"
    assert retrieval_metadata(config)["bm25_lexical_profile"] == "local-lexical-v2"
