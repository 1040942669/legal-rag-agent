from __future__ import annotations

from scripts.benchmark_chinese_bm25 import aggregate, paired_summary, score_case, write_new
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
