"""Modern reference mechanics through the actual default evidence/adaptive path.

All laws and passages are fictional. These tests do not label legal entailment.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from legal_rag.adaptive import retrieve_adaptive
from legal_rag.evidence import check_evidence_sufficiency, with_stop_reason
from legal_rag.models import Chunk, NormalizedQuery, RetrievalPlan, SearchResult
from legal_rag.query import analyze_query


GENERAL = "general-reference-v3"
HISTORICAL = "legacy-hints-and-return-v1"


def row(law="合成甲法", article="第十条", *, rank=1, score=100.0, text="合成条文记录事项。"):
    return SearchResult(Chunk(str(rank), text, [law], [article], ["fictional.txt"],
                              [rank], "article"), score, rank, "bm25",
                        {"lexical_profile": "local-lexical-v2"})


def test_default_multi_law_checker_rejects_swapped_pairs_and_keeps_typed_reason():
    checked = check_evidence_sufficiency(
        "《合成甲法》第十条和《合成乙法》第五条是什么？",
        [row(article="第五条"), row("合成乙法", "第十条", rank=2)])
    assert not checked.sufficient
    assert checked.rules_version == GENERAL
    assert "missing_reference_pair:合成甲法:第十条" in checked.missing_law_support
    assert len(checked.mechanical_check["missing_pairs"]) == 2
    assert checked.mechanical_check["semantic_support_status"] == "not_checked"


def test_correct_multi_law_pairs_are_mechanically_complete_not_semantically_proven():
    checked = check_evidence_sufficiency(
        "《合成甲法》第10条和《合成乙法》第5条是什么？",
        [row(), row("合成乙法", "第五条", rank=2)])
    assert checked.sufficient
    assert checked.mechanical_check["reference_coverage_status"] == "complete"
    assert checked.mechanical_check["semantic_support_status"] == "not_checked"
    assert with_stop_reason(checked, "sufficient_after_followup").mechanical_check == checked.mechanical_check


@pytest.mark.parametrize("query", [
    "解释《合成甲法》第十条，不是《合成乙法》第五条。",
    "解释《合成甲法》第十条，不要检索《合成乙法》第五条。",
    "对方曾引用《合成乙法》第五条，我现在只要求解释《合成甲法》第十条。",
])
def test_excluded_and_reported_references_do_not_force_unasked_second_law(query):
    assert check_evidence_sufficiency(query, [row()]).sufficient


def test_ambiguous_law_group_requests_clarification_instead_of_guessing_pairs():
    checked = check_evidence_sufficiency(
        "《合成甲法》和《合成乙法》的第十条、第五条分别是什么？",
        [row(), row("合成乙法", "第五条", rank=2)])
    assert not checked.sufficient
    assert checked.stop_reason == "needs_clarification"
    assert checked.followup_queries == []
    assert checked.mechanical_check["reference_coverage_status"] == "unresolved"


def test_modern_checker_never_uses_historical_shopping_anchor_for_synonyms():
    checked = check_evidence_sufficiency(
        "我在网上购买衣服后想退货，可以退吗？",
        [row(text="产品不符要求可以退回。")])
    assert checked.sufficient  # Candidate availability, not a claim of entitlement.
    assert "missing_goods_return_anchor" not in checked.low_coverage
    assert checked.mechanical_check["semantic_support_status"] == "not_checked"


def test_inferred_normalizer_and_plan_hints_are_not_hard_reference_requirements():
    query = "如何记录合成事项？"
    normalized = NormalizedQuery(query, [query], [], ["合成乙法"], ["第五条"], [], [], 0.9)
    plan = RetrievalPlan("p1", query, ["合成丙法"], ["第七条"], [], 1, "synthetic hint")
    injected_analysis = replace(analyze_query(query), law_names=["合成丁法"], article_numbers=["第九条"])
    checked = check_evidence_sufficiency(query, [row()], analysis=injected_analysis,
                                         normalized_query=normalized, plans=[plan])
    assert checked.sufficient
    assert checked.missing_law_support == []
    assert checked.mechanical_check["reference_coverage_status"] == "not_requested"
    missing = check_evidence_sufficiency(query, [row()], normalized_query=replace(
        normalized, missing_facts=["事项发生日期"]), plans=[plan])
    assert not missing.sufficient
    assert missing.missing_facts == ["事项发生日期"]
    assert missing.stop_reason == "needs_clarification"


def test_unquoted_missing_law_is_checked_using_authorized_corpus_titles():
    checked = check_evidence_sufficiency("合成星云规则第2条是什么？", [row()],
                                         known_law_titles=("合成星云规则", "合成甲法"))
    assert not checked.sufficient
    assert "missing_law:合成星云规则" in checked.missing_law_support
    assert "missing_reference_pair:合成星云规则:第二条" in checked.missing_law_support


@pytest.mark.parametrize("score", [True, None, "100", float("nan"), float("inf")])
def test_modern_default_keeps_invalid_score_guard(score):
    checked = check_evidence_sufficiency("合成记录", [row(score=score)])
    assert not checked.sufficient
    assert "invalid_scores" in checked.low_coverage


def test_unknown_evidence_rules_are_not_silently_decoded_or_executed():
    with pytest.raises(ValueError, match="rules"):
        check_evidence_sufficiency("合成记录", [row()], rules_version="unsupported")


def test_legacy_rules_are_explicit_historical_behavior_not_new_default():
    checked = check_evidence_sufficiency(
        "我在网上购买衣服后想退货，可以退吗？", [row(text="产品不符要求可以退回。")],
        rules_version=HISTORICAL)
    assert not checked.sufficient
    assert "missing_goods_return_anchor" in checked.low_coverage
    assert checked.rules_version == HISTORICAL
    assert checked.mechanical_check is None


class FixedRetriever:
    name = "bm25"
    known_law_hints = ["合成星云规则", "合成甲法"]

    def __init__(self):
        self.calls = []

    def retrieve(self, query, top_k=5):
        self.calls.append(query)
        return [row()]


def test_adaptive_direct_path_carries_fixed_rules_and_authorized_title_catalog():
    retriever = FixedRetriever()
    result = retrieve_adaptive("合成星云规则第2条是什么？", retriever,
                                enabled=False, max_followup_rounds=0)
    assert not result.evidence_check.sufficient
    assert "missing_law:合成星云规则" in result.evidence_check.missing_law_support
    assert result.evidence_check.rules_version == GENERAL
    assert result.followup_trace["evidence_rules_version"] == GENERAL
    legacy = retrieve_adaptive("如何记录合成事项？", retriever, enabled=False,
                                max_followup_rounds=0, evidence_rules_version=HISTORICAL)
    assert legacy.evidence_check.rules_version == HISTORICAL


def test_long_query_is_classified_without_retrieval_or_model_dispatch():
    query = "合成" * 50_000
    checked = check_evidence_sufficiency(query, [row()])
    assert not checked.sufficient
    assert checked.stop_reason == "needs_clarification"
    assert "reference_query_limit_exceeded" in checked.low_coverage
    retriever = FixedRetriever()
    result = retrieve_adaptive(query, retriever, enabled=True, use_llm=True,
                                llm_client=object(), max_followup_rounds=1)
    assert result.evidence_check.stop_reason == "needs_clarification"
    assert retriever.calls == []


def test_historical_cross_sentence_reference_verification_requires_the_original_owned_pair():
    query = "对方此前引用《合成甲法》第十条；请判断这种引用是否正确？"
    missing = check_evidence_sufficiency(query, [row("合成乙法", "第十条")], rules_version="general-reference-v2")
    assert not missing.sufficient
    assert "missing_reference_pair:合成甲法:第十条" in missing.missing_law_support
    assert check_evidence_sufficiency(query, [row()], rules_version="general-reference-v2").sufficient


def test_modern_cross_sentence_selection_is_explicitly_unknown_not_guessed():
    query = "对方此前引用《合成甲法》第十条；请判断这种引用是否正确？"
    for evidence in ([row()], [row("合成乙法", "第十条")]):
        checked = check_evidence_sufficiency(query, evidence)
        assert not checked.sufficient
        assert checked.stop_reason == "needs_clarification"
        assert checked.mechanical_check["reference_coverage_status"] == "unresolved"
        assert checked.followup_queries == []


def test_invalid_query_numerals_are_classified_unknown_without_followup_or_dispatch():
    checked = check_evidence_sufficiency("解释《合成甲法》第十十条。", [row(article="第二十条")])
    assert not checked.sufficient
    assert checked.stop_reason == "needs_clarification"
    assert "unresolved_reference:invalid_article_numeral" in checked.missing_law_support
    assert checked.followup_queries == []
