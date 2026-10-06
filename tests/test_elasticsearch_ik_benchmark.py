"""Small synthetic contracts for the frozen, experimental ES + IK comparison."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts import benchmark_elasticsearch_ik as benchmark


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = ROOT / "configs/chinese-bm25-benchmark-v4.json"


def protocol():
    # Public configuration only; no questions, corpus, credentials or model data.
    return json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))


def synthetic_rows(*, hit_count=60, hit_mrr=1.0):
    rows = []
    for number in range(120):
        hit = int(number < hit_count)
        metrics = None if number >= 108 else {
            "hit_at_3": hit if hit_mrr >= 1 / 3 else 0,
            "hit_at_5": hit,
            "mrr": hit * hit_mrr,
            "target_coverage": hit,
        }
        rows.append({
            "case_id": f"synthetic-{number}",
            "status": "succeeded",
            "latency_ms": 1.0,
            "metrics": metrics,
            "ranked_results": [{
                "chunk_id": f"synthetic-chunk-{number}",
                "score": 1.0,
                "rank": 1,
            }],
        })
    return rows


def result_for(arm_id, *, hit_count=60, hit_mrr=1.0):
    first = synthetic_rows(hit_count=hit_count, hit_mrr=hit_mrr)
    second = list(reversed(deepcopy(first)))
    return {
        "arm_id": arm_id,
        "worker_failure": None,
        "inputs_and_implementation_stable": True,
        "runtime_identity_stable": True,
        "service_cleanup_succeeded": True,
        "actual_provider_calls": 0,
        "repeat_mismatch_case_ids": [],
        "first_pass": benchmark.aggregate(first),
        "second_pass": benchmark.aggregate(second),
        "passes": [first, second],
    }


def selection_inputs():
    fixed = protocol()
    results = {arm["id"]: result_for(arm["id"]) for arm in fixed["arms"]}
    executions = {arm["id"]: {"exit_code": 0} for arm in fixed["arms"]}
    return fixed, results, executions


def test_protocol_freezes_three_arms_and_actual_permission_boundaries():
    fixed = protocol()
    benchmark.validate_protocol(fixed)
    assert [arm["id"] for arm in fixed["arms"]] == [
        "legacy-v1", "bm25s-sklearn-char", "elasticsearch-ik",
    ]
    assert fixed["provider_calls_allowed"] == 0
    assert fixed["network_allowed"] == "literal_loopback_only"
    assert fixed["top_k"] == 5 and fixed["passes"] == 2
    assert all(arm["k1"] == 1.5 and arm["b"] == .75 for arm in fixed["arms"])
    assert fixed["arms"][-1]["index_analyzer"] == "ik_max_word"
    assert fixed["arms"][-1]["query_analyzer"] == "ik_smart"
    assert fixed["arms"][-1]["max_unique_query_terms"] == 1024
    assert fixed["promotion_requires_legacy_non_regression"] is True


@pytest.mark.parametrize("mutation", [
    "candidate", "network", "provider", "passes", "float-top-k", "dictionary",
])
def test_protocol_rejects_candidate_permission_and_dictionary_changes(mutation):
    fixed = protocol()
    if mutation == "candidate":
        fixed["arms"][-1]["query_analyzer"] = "ik_max_word"
    elif mutation == "network":
        fixed["network_allowed"] = True
    elif mutation == "provider":
        fixed["provider_calls_allowed"] = 1
    elif mutation == "passes":
        fixed["passes"] = 3
    elif mutation == "float-top-k":
        fixed["top_k"] = 5.0
    elif mutation == "dictionary":
        fixed["ik_dictionary"] += "; permit case-derived custom words"
    with pytest.raises((TypeError, ValueError)):
        benchmark.validate_protocol(fixed)


def test_elasticsearch_is_an_eligible_modern_arm_not_silently_excluded():
    fixed, results, executions = selection_inputs()
    results["elasticsearch-ik"] = result_for("elasticsearch-ik", hit_count=80)
    assert benchmark.selection(fixed, results, executions, True) == (
        True, "elasticsearch-ik", "elasticsearch-ik",
    )


def test_higher_hit_char_with_lower_mrr_is_selected_but_not_promotable():
    fixed, results, executions = selection_inputs()
    results["bm25s-sklearn-char"] = result_for(
        "bm25s-sklearn-char", hit_count=90, hit_mrr=.25,
    )
    assert benchmark.selection(fixed, results, executions, True) == (
        True, "bm25s-sklearn-char", None,
    )


@pytest.mark.parametrize("cause", ["incomplete", "nan"])
def test_incomplete_or_nonfinite_results_cannot_select_a_winner(cause):
    fixed, results, executions = selection_inputs()
    if cause == "incomplete":
        results.pop("elasticsearch-ik")
    else:
        result = results["elasticsearch-ik"]
        for rows in result["passes"]:
            next(row for row in rows if row["case_id"] == "synthetic-0")["metrics"]["mrr"] = float("nan")
        result["first_pass"] = benchmark.aggregate(result["passes"][0])
        result["second_pass"] = benchmark.aggregate(result["passes"][1])
    assert benchmark.selection(fixed, results, executions, True) == (False, None, None)


def test_cleanup_is_explicitly_successful_for_every_arm_or_no_winner():
    # A claimed winner must not hide a failed cleanup in an unselected arm.
    for arm_id in ("legacy-v1", "bm25s-sklearn-char", "elasticsearch-ik"):
        for cleanup in (False, None, 1):
            fixed, results, executions = selection_inputs()
            results[arm_id]["service_cleanup_succeeded"] = cleanup
            assert benchmark.selection(fixed, results, executions, True) == (False, None, None)


def test_row_identity_gold_mask_or_ranking_mismatch_cannot_hide_behind_summary():
    for cause in ("arm-id", "duplicate-id", "gold-mask", "repeat-ranking", "forged-summary"):
        fixed, results, executions = selection_inputs()
        result = results["elasticsearch-ik"]
        if cause == "arm-id":
            result["arm_id"] = "bm25s-sklearn-char"
        elif cause == "duplicate-id":
            result["passes"][0][1]["case_id"] = "synthetic-0"
        elif cause == "gold-mask":
            # Counts still 108/12; the scored case identities no longer match.
            for rows in result["passes"]:
                by_id = {row["case_id"]: row for row in rows}
                by_id["synthetic-108"]["metrics"] = by_id["synthetic-0"]["metrics"]
                by_id["synthetic-0"]["metrics"] = None
            result["first_pass"] = benchmark.aggregate(result["passes"][0])
            result["second_pass"] = benchmark.aggregate(result["passes"][1])
        elif cause == "repeat-ranking":
            result["passes"][1][0]["ranked_results"][0]["chunk_id"] = "changed-result"
        elif cause == "forged-summary":
            result["first_pass"]["metrics"]["hit_at_5"] = .99
        assert benchmark.selection(fixed, results, executions, True) == (False, None, None)


def test_failed_worker_drift_repeat_and_bad_denominator_all_prevent_selection():
    for cause in ("worker", "runtime", "source", "global", "repeat", "denominator"):
        fixed, results, executions = selection_inputs()
        result = results["elasticsearch-ik"]
        if cause == "worker":
            result["worker_failure"] = "synthetic_failure"
        elif cause == "runtime":
            result["runtime_identity_stable"] = False
        elif cause == "source":
            result["inputs_and_implementation_stable"] = False
        elif cause == "repeat":
            result["repeat_mismatch_case_ids"] = ["synthetic-0"]
        elif cause == "denominator":
            result["second_pass"]["quality_denominator"] = 107
        assert benchmark.selection(fixed, results, executions, cause != "global") == (False, None, None)


def test_no_gold_is_na_and_failed_or_misaligned_pairing_stays_unknown():
    baseline = result_for("legacy-v1")
    candidate = result_for("elasticsearch-ik", hit_count=61)
    assert baseline["first_pass"]["quality_denominator"] == 108
    assert baseline["first_pass"]["no_gold_count"] == 12
    assert baseline["passes"][0][-1]["metrics"] is None
    paired = benchmark.safe_paired_summary(baseline, candidate, protocol()["bootstrap"])
    assert paired["paired_quality_denominator"] == 108
    assert paired["improved_case_ids"] == ["synthetic-60"]
    for cause in ("failed-no-gold", "misaligned-id"):
        invalid = deepcopy(candidate)
        if cause == "failed-no-gold":
            invalid["passes"][0][-1]["status"] = "failed"
        else:
            invalid["passes"][0][0]["case_id"] = "different-id"
        assert benchmark.safe_paired_summary(baseline, invalid, protocol()["bootstrap"]) is None


def test_sampler_initialization_failure_still_cleans_up_the_owned_worker(monkeypatch, tmp_path):
    import psutil

    class OwnedWorker:
        pid = 123456
        cleaned = False
        killed = False
        reaped = False

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            # Only the fallback cleanup may wait after initialization failed.
            assert self.killed is True
            self.reaped = True
            return 0

    worker = OwnedWorker()
    cleanup_calls = []

    def initialization_failure(pid):
        assert pid == worker.pid
        raise psutil.AccessDenied(pid)

    def cleanup(process, *, known_descendants=(), **kwargs):
        assert process is worker
        assert known_descendants == ()
        cleanup_calls.append(process)
        process.cleaned = True

    monkeypatch.setattr(benchmark.subprocess, "Popen", lambda *args, **kwargs: worker)
    monkeypatch.setattr(psutil, "Process", initialization_failure)
    monkeypatch.setattr(benchmark, "terminate_process_tree", cleanup)
    assert benchmark.run_worker(tmp_path, protocol()["arms"][-1], .01) == "cleanup_failed"
    assert cleanup_calls == [worker]
    assert worker.cleaned is True
    assert worker.killed is True and worker.reaped is True
