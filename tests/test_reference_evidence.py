"""Mechanical ownership only, on entirely fictional evidence."""

from __future__ import annotations

from dataclasses import replace

import pytest

from legal_rag.legal_references import parse_legal_references
from legal_rag.models import Chunk, SearchResult
from legal_rag.reference_evidence import check_reference_evidence
from legal_rag.retrieval_contracts import (
    RetrievalBoundary, RetrievalProvenance, RetrievedArticleProvenance,
)


def row(law="合成甲法", article="第十条", *, rank=1, score=100.0, scope="public"):
    return SearchResult(Chunk(str(rank), "合成条文记录事项。", [law], [article],
                              ["fictional.txt"], [rank], "article",
                              {"scope_id": scope, "snapshot_id": "fictional"}),
                        score, rank, "bm25")


def test_swapped_multiple_law_articles_fail_despite_union_coverage():
    analysis = parse_legal_references("《合成甲法》第十条和《合成乙法》第五条是什么？")
    checked = check_reference_evidence(analysis, [row(article="第五条"),
                                      row("合成乙法", "第十条", rank=2)])
    assert not checked.sufficient
    assert len(checked.missing_pairs) == 2
    assert checked.reference_coverage_status == "missing"
    assert checked.semantic_support_status == "not_checked"


def test_correct_multiple_law_articles_pass_mechanics_without_semantic_promotion():
    analysis = parse_legal_references("《合成甲法》第10条和《合成乙法》第五条是什么？")
    checked = check_reference_evidence(analysis, [row(), row("合成乙法", "第五条", rank=2)])
    assert checked.sufficient
    assert checked.scope_status == "not_configured"
    assert checked.semantic_support_status == "not_checked"
    assert checked.to_dict()["reference_coverage_status"] == "complete"


def test_related_title_does_not_cover_exact_requested_law():
    analysis = parse_legal_references("《合成甲法》第十条是什么？")
    checked = check_reference_evidence(analysis, [row("合成甲法实施细则")])
    assert not checked.sufficient
    assert checked.missing_laws == ("合成甲法",)


def test_untyped_mixed_chunk_does_not_prove_pair_but_typed_entry_does():
    analysis = parse_legal_references("《合成甲法》第十条是什么？")
    result = row()
    mixed = replace(result, chunk=replace(result.chunk, law_names=["合成甲法", "合成乙法"],
                                          article_numbers=["第五条", "第十条"]))
    assert not check_reference_evidence(analysis, [mixed]).sufficient
    entries = tuple(RetrievedArticleProvenance(
        f"a{index}", f"l{index}", f"v{index}", art, law, None, None,
        "fictional.txt", index, "unknown")
        for index, (law, art) in enumerate([("合成甲法", "第十条"),
                                          ("合成乙法", "第五条")], start=1))
    typed = replace(mixed, provenance=RetrievalProvenance(
        RetrievalBoundary("public", "fictional", "a"*64), "public", "fictional", "a"*64,
        mixed.chunk.chunk_id, "b"*64, "c"*64, 0, None, entries))
    assert check_reference_evidence(analysis, [typed]).sufficient


@pytest.mark.parametrize("score", [True, False, "100", None, float("nan"), float("inf")])
def test_invalid_scores_are_mechanical_failures(score):
    checked = check_reference_evidence(parse_legal_references("记录事项是什么？"), [row(score=score)])
    assert not checked.sufficient
    assert not checked.scores_valid


def test_scope_configuration_does_not_count_private_or_wrong_snapshot_evidence():
    analysis = parse_legal_references("《合成甲法》第十条是什么？")
    checked = check_reference_evidence(analysis, [row(scope="private")],
                                       snapshot_id="fictional", allowed_scope_ids=("public",))
    assert not checked.sufficient
    assert checked.scope_status == "invalid"
    assert checked.missing_pairs


def test_unresolved_pair_relationship_is_not_sufficient():
    analysis = parse_legal_references("《合成甲法》和《合成乙法》第十条、第五条是什么？")
    checked = check_reference_evidence(analysis, [row(), row("合成乙法", "第五条", rank=2)])
    assert not checked.sufficient
    assert checked.reference_coverage_status == "unresolved"


def test_high_score_without_explicit_reference_never_means_semantic_supported():
    checked = check_reference_evidence(parse_legal_references("能退回购买的雨伞吗？"), [row()])
    assert checked.sufficient
    assert checked.reference_coverage_status == "not_requested"
    assert checked.semantic_support_status == "not_checked"


def test_excluded_law_is_not_required_and_empty_candidates_are_insufficient():
    analysis = parse_legal_references("解释《合成甲法》第十条，不是《合成乙法》第五条。")
    assert check_reference_evidence(analysis, [row()]).sufficient
    assert not check_reference_evidence(analysis, []).candidate_available
