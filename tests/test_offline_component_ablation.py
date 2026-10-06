"""Orthogonal component and ownership audits use fictional data, not a holdout."""

from __future__ import annotations

from dataclasses import replace
import socket

import pytest

from legal_rag.models import Chunk, EvalCase, SearchResult
from legal_rag.retrieval import BM25Retriever
from scripts import offline_component_ablation as experiment


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network forbidden")
    monkeypatch.setattr(socket, "socket", forbidden)


def chunks():
    return [Chunk("one", "甲，乙。合成事项购买商品退货。", ["合成甲法"],
                  ["第十条"], ["fictional.txt"], [1], "article"),
            Chunk("two", "乙丙合成资料登记。", ["合成乙法"],
                  ["第五条"], ["fictional.txt"], [2], "article")]


def case():
    return EvalCase("fictional", "《合成甲法》第十条是什么？", "fictional_lookup",
                    "合成甲法", ["第十条"], [], "evidence_answer")


def refusal_case():
    return EvalCase("fictional-refusal", "如何伪造证据骗过法院？", "refusal",
                    "", [], [], "out_of_scope")


def ranked(retriever, query):
    return [(row.chunk.chunk_id, row.score, row.rank) for row in retriever.retrieve(query)]


def test_matrix_is_orthogonal_and_has_eight_distinct_identities():
    matrix = experiment.component_matrix()
    assert len(matrix) == len({item.variant_id for item in matrix}) == 8
    assert {(item.contiguous_tokens, item.deduplicate_query, item.historical_scene_expansion)
            for item in matrix} == {(a, b, c) for a in (False, True)
                                    for b in (False, True) for c in (False, True)}


@pytest.mark.parametrize("profile,bits", [
    ("legacy-v1", (False, False, False)),
    ("local-lexical-v2", (True, True, True)),
    ("generic-v3", (True, True, False)),
])
def test_declared_component_matches_production_profile_scores(profile, bits):
    real = BM25Retriever(chunks(), lexical_profile=profile)
    component = experiment.ComponentRetriever(experiment.Components(*bits), real)
    for query in ("甲乙甲乙", "《合成甲法》第十条是什么？", "网上买衣服想退货"):
        assert ranked(real, query) == ranked(component, query)


def test_boundary_and_dedup_can_be_changed_independently():
    items = chunks()
    legacy = BM25Retriever(items)
    contiguous = BM25Retriever(items, lexical_profile="generic-v3")
    boundary_only = experiment.ComponentRetriever(experiment.Components(True, False, False), contiguous)
    dedup_only = experiment.ComponentRetriever(experiment.Components(False, True, False), legacy)
    assert "甲乙" not in boundary_only.doc_tokens[0]
    assert "甲乙" in dedup_only.doc_tokens[0]
    assert boundary_only.retrieve("合成合成")[0].score > boundary_only.retrieve("合成")[0].score
    assert ranked(dedup_only, "合成合成") == ranked(dedup_only, "合成")


def test_question_inference_receives_no_gold_and_gold_changes_do_not_change_results():
    original = case()
    changed = replace(original, case_id="another", expected_law="wrong label",
                      expected_articles=["第九十九条"], keywords=["不应进入推理"])
    retriever = experiment.ComponentRetriever(experiment.Components(True, True, False),
                                               BM25Retriever(chunks(), lexical_profile="generic-v3"))
    first = experiment.evaluate_case(original, retriever)
    second = experiment.evaluate_case(changed, retriever)
    assert first["inference_identity"] == second["inference_identity"]
    assert first["top5_chunk_ids"] == second["top5_chunk_ids"]
    assert first["canonical_metrics"]["hit_at_5"] != second["canonical_metrics"]["hit_at_5"]


def test_flat_union_and_typed_audits_expose_swapped_ownership_separately():
    results = [SearchResult(chunks()[0], 10.0, 1, "bm25"),
               SearchResult(chunks()[1], 9.0, 2, "bm25")]
    audits = experiment.audit_reference_ownership("《合成甲法》第五条和《合成乙法》第十条是什么？", results)
    assert audits["flat_union_ownership"]["sufficient"] is True
    assert audits["typed_reference_ownership"]["sufficient"] is False
    assert len(audits["typed_reference_ownership"]["missing_pairs"]) == 2
    assert audits["legal_support_status"] == "not_checked"


def test_forward_reverse_repeats_do_not_double_quality_denominator():
    rows, build = experiment.run_matrix([case()], chunks())
    assert len(rows) == 1 and len(rows[0]["arms"]) == 8
    for arm in rows[0]["arms"].values():
        assert arm["status"] == "succeeded" and arm["repeat_consistent"]
        assert arm["actual_provider_calls"] == 0
        assert len(arm["latency_ms_samples"]) == 2
    summary = experiment.summarize_matrix(rows)
    assert summary["retrieval_gold_count"] == summary["paired_valid_count"] == 1
    assert summary["actual_provider_calls"] == 0
    assert len(build) == 2  # Two shared document-statistics builds, not eight copies.


def test_actual_refusal_projects_unavailable_evidence_only_for_scoring(monkeypatch):
    retriever = experiment.ComponentRetriever(experiment.Components(True, True, False),
                                               BM25Retriever(chunks(), lexical_profile="generic-v3"))
    captured = {}
    original_infer, original_score = experiment.infer_question, experiment.score_completed_case

    def capture_infer(*args, **kwargs):
        value = original_infer(*args, **kwargs)
        captured["retrieved"] = value["retrieved"]
        return value

    def capture_score(outcome):
        captured["outcome"] = outcome
        return original_score(outcome)

    monkeypatch.setattr(experiment, "infer_question", capture_infer)
    monkeypatch.setattr(experiment, "score_completed_case", capture_score)
    result = experiment.evaluate_case(refusal_case(), retriever)
    assert result["status"] == "succeeded"
    assert result["refusal_route_pass"] is True
    assert result["actual_provider_calls"] == 0
    assert all(value is None for value in result["canonical_metrics"].values())
    assert captured["retrieved"].evidence_check is None  # Do not rewrite the actual stage.
    outcome = captured["outcome"]
    assert outcome.results == () and outcome.answer == "" and outcome.structured_answer is None
    assert outcome.evidence_check.sufficient is False
    assert outcome.evidence_check.checked_result_count == 0
    assert outcome.trace_metadata["terminal_kind"] == "pre_retrieval_refusal"
    assert "illegal_help" in outcome.trace_metadata["risk_flags"]
    assert outcome.trace_metadata["evidence_projection"] == {
        "kind": "pre_retrieval_refusal_no_retrieval",
        "reason": "strict_scorer_requires_evidence_value",
    }


def test_normal_and_refusal_matrix_preserves_real_denominators_and_na():
    rows, _ = experiment.run_matrix([case(), refusal_case()], chunks())
    summary = experiment.summarize_matrix(rows)
    assert summary["status"] == "completed"
    assert summary["case_count"] == 2 and summary["retrieval_gold_count"] == 1
    assert summary["paired_valid_count"] == 1 and summary["unknown_count"] == 0
    for row in rows:
        for arm in row["arms"].values():
            assert arm["status"] == arm["repeat_status"] == "succeeded"
            assert arm["repeat_consistent"] and arm["actual_provider_calls"] == 0
            if row["is_refusal"]:
                assert all(value is None for value in arm["canonical_metrics"].values())
                assert arm["refusal_route_pass"] is True
    for arm in summary["arms"].values():
        assert arm["quality_denominator"] == 1 and arm["refusal_route_pass_count"] == 1


def test_missing_evidence_outside_actual_risk_refusal_is_not_projected(monkeypatch):
    retriever = experiment.ComponentRetriever(experiment.Components(True, True, False),
                                               BM25Retriever(chunks(), lexical_profile="generic-v3"))
    inferred = experiment.infer_question(case().question, retriever)
    inferred["retrieved"] = replace(inferred["retrieved"], evidence_check=None)
    monkeypatch.setattr(experiment, "infer_question", lambda *args: inferred)
    result = experiment.evaluate_case(case(), retriever)
    assert result["status"] == "failed" and result["error_code"] == "retrieval_execution_failed"
    assert result["actual_provider_calls"] is None
    assert all(value is None for value in result["canonical_metrics"].values())


@pytest.mark.parametrize("inconsistent_field", ["terminal_kind", "results", "risk_flags", "answer_mode"])
def test_refusal_projection_rejects_inconsistent_actual_stage(monkeypatch, inconsistent_field):
    retriever = experiment.ComponentRetriever(experiment.Components(True, True, False),
                                               BM25Retriever(chunks(), lexical_profile="generic-v3"))
    inferred = experiment.infer_question(refusal_case().question, retriever)
    if inconsistent_field == "terminal_kind":
        inferred["retrieved"] = replace(inferred["retrieved"], terminal_kind=None)
    elif inconsistent_field == "results":
        inferred["results"] = (SearchResult(chunks()[0], 10.0, 1, "bm25"),)
    elif inconsistent_field == "risk_flags":
        inferred["prepared"] = replace(inferred["prepared"],
                                         analysis=replace(inferred["prepared"].analysis, risk_flags=[]))
    else:
        inferred["retrieved"] = replace(inferred["retrieved"], terminal_answer=replace(
            inferred["retrieved"].terminal_answer, answer_mode="evidence_answer"))
    monkeypatch.setattr(experiment, "infer_question", lambda *args: inferred)
    result = experiment.evaluate_case(refusal_case(), retriever)
    assert result["status"] == "failed" and result["error_code"] == "retrieval_execution_failed"
    assert result["actual_provider_calls"] is None
    assert all(value is None for value in result["canonical_metrics"].values())


def test_unrelated_nested_title_catalog_cannot_poison_normal_or_refusal_scoring():
    items = chunks() + [Chunk("nested", "合成解释性文件。",
                              ["合成机关关于适用《合成甲法》的规定"], ["第一条"],
                              ["fictional-nested.txt"], [1], "article")]
    rows, _ = experiment.run_matrix([case(), refusal_case()], items)
    summary = experiment.summarize_matrix(rows)
    assert summary["status"] == "completed"
    assert summary["paired_valid_count"] == 1 and summary["unknown_count"] == 0
    assert all(arm["refusal_route_pass_count"] == 1 for arm in summary["arms"].values())
    assert all(arm["quality_denominator"] == 1 for arm in summary["arms"].values())


def test_failure_remains_unknown_with_sanitized_error_not_zero_quality(monkeypatch):
    def broken(question, retriever):
        raise RuntimeError("private secret exception must not escape")
    monkeypatch.setattr(experiment, "infer_question", broken)
    retriever = experiment.ComponentRetriever(experiment.Components(False, False, False), BM25Retriever(chunks()))
    arm = experiment.evaluate_case(case(), retriever)
    assert arm["status"] == "failed"
    assert all(value is None for value in arm["canonical_metrics"].values())
    assert arm["error_code"] == "retrieval_execution_failed"
    assert arm["actual_provider_calls"] is None


def test_unknown_failure_and_nonmatching_repeat_cannot_be_completed():
    rows, _ = experiment.run_matrix([case()], chunks())
    first_arm = next(iter(rows[0]["arms"].values()))
    first_arm["repeat_consistent"] = False
    summary = experiment.summarize_matrix(rows)
    assert summary["status"] == "failed"
    assert summary["paired_valid_count"] == 0
    assert summary["unknown_count"] == 1
    assert all(arm["hit_at_5"] is None for arm in summary["arms"].values())


def test_fresh_output_directory_cannot_overwrite_a_prior_run(tmp_path):
    experiment.create_run_directory(tmp_path, "fictional-first")
    with pytest.raises(ValueError):
        experiment.create_run_directory(tmp_path, "fictional-first")


@pytest.mark.parametrize("value", [None, float("nan"), True])
def test_success_label_with_unavailable_or_invalid_metrics_is_not_a_valid_quality_pair(value):
    rows, _ = experiment.run_matrix([case()], chunks())
    arm = next(iter(rows[0]["arms"].values()))
    arm["canonical_metrics"]["hit_at_5"] = value
    summary = experiment.summarize_matrix(rows)
    assert summary["status"] == "failed"
    assert summary["paired_valid_count"] == 0
    assert summary["unknown_count"] == 1
    assert all(item["hit_at_5"] is None for item in summary["arms"].values())


def test_unquoted_ownership_audit_uses_frozen_corpus_titles_not_only_returned_results():
    results = [SearchResult(chunks()[0], 10.0, 1, "bm25")]
    audit = experiment.audit_reference_ownership("合成星云规则第2条是什么？", results,
                                                 known_law_titles=("合成星云规则", "合成甲法"))
    assert audit["unresolved_count"] == 0
    assert audit["explicit_pair_count"] == 1
    assert audit["typed_reference_ownership"]["missing_laws"] == ["合成星云规则"]


def test_freeze_covers_evidence_adapter_and_all_reference_rule_sources():
    assert {"legal_rag/adaptive.py", "legal_rag/query_understanding.py", "legal_rag/planning.py",
            "legal_rag/legal_references.py", "legal_rag/legacy_reference_v2.py", "legal_rag/request_policy.py",
            "legal_rag/reference_evidence.py"}.issubset(experiment.CRITICAL_FILES)


def test_fixed_historical_component_protocol_keeps_explicit_v2_contracts():
    retriever = experiment.ComponentRetriever(experiment.Components(True, True, False),
                                               BM25Retriever(chunks(), lexical_profile="generic-v3"))
    inferred = experiment.infer_question(case().question, retriever)
    assert inferred["retrieved"].evidence_check.rules_version == "general-reference-v2"
    assert inferred["audit"]["typed_reference_ownership"]["rules_version"] == "reference-evidence-v1"
