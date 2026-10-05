from __future__ import annotations

from scripts.benchmark_chinese_bm25 import aggregate, paired_summary, score_case, write_new, selection, run_worker, critical_identity
from legal_rag.models import Chunk, EvalCase, SearchResult
import pytest


def row(case_id, hit=None, status="succeeded"):
    return {"case_id": case_id, "status": status, "latency_ms": 1,
            "metrics": None if hit is None else {"hit_at_3": hit, "hit_at_5": hit,
                                                  "mrr": hit, "target_coverage": hit}}


def test_errors_and_no_gold_do_not_become_quality_zero():
    summary = aggregate([row("hit", 1), row("miss", 0), row("error", status="failed"), row("refusal")])
    assert summary["quality_denominator"] == 2
    assert summary["hit_at_5_count"] == 1
    assert summary["metrics"]["hit_at_5"] == .5
    assert summary["error_count"] == summary["no_gold_count"] == 1
    assert aggregate([row("error", status="failed")])["metrics"]["hit_at_5"] is None


def test_paired_comparison_requires_success_and_gold_in_both_arms():
    old = {"passes": [[row("a", 0), row("b", 1), row("c", 1), row("d")]]}
    new = {"passes": [[row("a", 1), row("b", 0), row("c", status="failed"), row("d")]]}
    result = paired_summary(old, new)
    assert result["paired_quality_denominator"] == 2
    assert result["improved_case_ids"] == ["a"]
    assert result["regressed_case_ids"] == ["b"]
    assert paired_summary({"passes": [[]]}, {"passes": [[]]})["hit_at_5_delta_ci95"] is None


def test_scoring_reuses_canonical_metrics_without_production_reference_parser():
    case = EvalCase("synthetic", "合成问题", "synthetic", "合成法", ["第十条"], [])
    result = SearchResult(Chunk("c", "正文", ["合成法"], ["第十条"], [], [], "article"), 1, 1, "bm25")
    assert score_case(case, [result])["hit_at_5"] == 1
    assert score_case(case, [result])["target_coverage"] == 1
    assert score_case(EvalCase("nogold", "合成问题", "synthetic", "", [], []), [result]) is None


def test_new_run_results_never_overwrite_existing_evidence(tmp_path):
    path = tmp_path / "summary.json"
    write_new(path, {"status": "failed"})
    with pytest.raises(FileExistsError):
        write_new(path, {"status": "completed"})
    assert '"failed"' in path.read_text(encoding="utf-8")


def selection_inputs():
    protocol = {"arms": [{"id": "legacy-v1", "backend": "historical"},
                         {"id": "modern", "backend": "bm25s"}],
                "promotion_requires_legacy_non_regression": True}
    results = {}
    for name in ("legacy-v1", "modern"):
        results[name] = {"arm_id": name, "inputs_and_implementation_stable": True,
                         "repeat_mismatch_case_ids": [],
                         "first_pass": {"error_count": 0, "metrics": {"hit_at_5": .7, "mrr": .6}},
                         "second_pass": {"error_count": 0, "query_p95_ms": 1}}
    return protocol, results, {name: {"exit_code": 0} for name in results}


@pytest.mark.parametrize("cause", ["exit", "timeout", "missing", "repeat", "drift", "error", "global-drift"])
def test_partial_failure_or_source_drift_never_selects_a_winner(cause):
    protocol, results, executions = selection_inputs()
    if cause == "exit": executions["modern"]["exit_code"] = 1
    if cause == "timeout": executions["modern"]["exit_code"] = "timeout"
    if cause == "missing": results.pop("modern")
    if cause == "repeat": results["modern"]["repeat_mismatch_case_ids"] = ["a"]
    if cause == "drift": results["modern"]["inputs_and_implementation_stable"] = False
    if cause == "error": results["modern"]["second_pass"]["error_count"] = 1
    assert selection(protocol, results, executions, cause != "global-drift") == (False, None, None)


@pytest.mark.parametrize("metric", ["hit_at_5", "mrr"])
def test_best_modern_arm_is_not_promotable_when_either_metric_regresses(metric):
    protocol, results, executions = selection_inputs()
    assert selection(protocol, results, executions, True) == (True, "modern", "modern")
    results["modern"]["first_pass"]["metrics"][metric] -= .01
    assert selection(protocol, results, executions, True) == (True, "modern", None)


def test_worker_timeout_is_recorded_not_converted_to_success(tmp_path, monkeypatch):
    import subprocess
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("synthetic-worker", 1)
    monkeypatch.setattr(subprocess, "run", timeout)
    assert run_worker(tmp_path, {"id": "synthetic"}, 1) == "timeout"


def test_source_freeze_includes_actual_scoring_and_contract_dependencies():
    identity = critical_identity()
    assert "legal_rag/evaluation.py" in identity
    assert "legal_rag/retrieval_contracts.py" in identity


def test_bootstrap_uses_explicit_protocol_options(monkeypatch):
    import legal_rag.evaluation as evaluation
    observed = []
    def fake(values, **kwargs):
        observed.append((values, kwargs))
        return (-1, 1)
    monkeypatch.setattr(evaluation, "bootstrap_ci", fake)
    paired_summary({"passes": [[row("a", 0)]]}, {"passes": [[row("a", 1)]]},
                   {"resamples": 7, "confidence": .8, "seed": 99})
    assert observed == [([1], {"n_resamples": 7, "confidence": .8, "seed": 99})]
