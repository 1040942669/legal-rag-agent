"""Ranking scores are not calibrated evidence confidence."""
from dataclasses import replace

import pytest

from legal_rag.legal_references import parse_legal_references
from legal_rag.models import Chunk, SearchResult
from legal_rag.reference_evidence import check_reference_evidence


def result(score, retriever="bm25"):
    return SearchResult(Chunk("synthetic", "合成条文", ["合成甲法"], ["第十条"], [], [], "article"),
                        score, 1, retriever)


@pytest.mark.parametrize("kind", ["bm25", "dense", "hybrid", "exact_reference"])
def test_positive_score_rescaling_cannot_change_mechanical_sufficiency(kind):
    analysis = parse_legal_references("解释《合成甲法》第十条")
    checks = [check_reference_evidence(analysis, [result(score, kind)]) for score in (.000001, .005, 5, 5000)]
    assert all(check.sufficient for check in checks)
    assert {check.semantic_support_status for check in checks} == {"not_checked"}
    assert {check.to_dict()["rules_version"] for check in checks} == {"reference-evidence-v2"}


def test_valid_rrf_k_200_scores_do_not_imply_no_evidence():
    analysis = parse_legal_references("解释《合成甲法》第十条")
    assert check_reference_evidence(analysis, [result(2 / 201, "hybrid")]).sufficient


def test_old_score_contract_remains_explicitly_replayable():
    analysis = parse_legal_references("解释《合成甲法》第十条")
    old = check_reference_evidence(analysis, [result(.005)], rules_version="reference-evidence-v1")
    assert not old.sufficient
    assert old.to_dict()["rules_version"] == "reference-evidence-v1"
    assert old.reasons == ("low_scores",)


@pytest.mark.parametrize("score", [True, False, None, "1", float("nan"), float("inf")])
def test_invalid_scores_still_fail(score):
    checked = check_reference_evidence(parse_legal_references("解释《合成甲法》第十条"), [result(score)])
    assert not checked.sufficient
    assert "invalid_scores" in checked.reasons


def test_empty_or_wrong_law_evidence_is_not_rescued_by_removing_threshold():
    analysis = parse_legal_references("解释《合成甲法》第十条")
    assert not check_reference_evidence(analysis, []).sufficient
    wrong = result(10000)
    wrong = replace(wrong, chunk=replace(wrong.chunk, law_names=["合成乙法"]))
    assert not check_reference_evidence(analysis, [wrong]).sufficient


@pytest.mark.parametrize("kind", ["bm25", "hybrid", "rrf", "exact_reference"])
@pytest.mark.parametrize("score", [0, -1])
def test_positive_match_backends_reject_nonpositive_values(kind, score):
    checked = check_reference_evidence(parse_legal_references("解释《合成甲法》第十条"), [result(score, kind)])
    assert not checked.scores_valid


def test_signed_cosine_values_are_not_treated_as_positive_bm25_scores():
    checked = check_reference_evidence(parse_legal_references("解释《合成甲法》第十条"), [result(-.2, "dense")])
    assert checked.scores_valid and checked.sufficient
    assert checked.semantic_support_status == "not_checked"
