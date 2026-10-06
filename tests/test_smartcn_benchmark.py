"""Synthetic protocol and selection contracts for the bounded SmartCN run."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess

import pytest

from legal_rag.models import Chunk, EvalCase, SearchResult
from scripts import benchmark_smartcn as benchmark


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = ROOT / "configs/chinese-bm25-benchmark-v3.json"


def protocol():
    # This is a public, predeclared configuration, not a corpus or gold file.
    return json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))


def synthetic_row(case_id: str, *, hit=None, status="succeeded"):
    return {
        "case_id": case_id, "status": status, "latency_ms": 1.0,
        "metrics": None if hit is None else {
            "hit_at_3": hit, "hit_at_5": hit,
            "mrr": hit, "target_coverage": hit,
        },
    }


def selection_inputs():
    fixed = protocol()
    rows = [synthetic_row(f"synthetic-{number}", hit=int(number < 60))
            for number in range(108)]
    rows.extend(synthetic_row(f"no-gold-{number}") for number in range(12))
    results = {
        arm["id"]: {
            "arm_id": arm["id"],
            "inputs_and_implementation_stable": True,
            "runtime_identity_stable": True,
            "repeat_mismatch_case_ids": [],
            "first_pass": benchmark.aggregate(rows),
            "second_pass": benchmark.aggregate(rows),
        }
        for arm in fixed["arms"]
    }
    executions = {arm["id"]: {"exit_code": 0} for arm in fixed["arms"]}
    return fixed, results, executions


def test_protocol_is_fixed_four_arm_offline_comparison():
    fixed = protocol()
    benchmark.validate_protocol(fixed)
    assert len(fixed["arms"]) == 4
    assert {arm["backend"] for arm in fixed["arms"]} == {
        "historical", "bm25s", "bm25s-smartcn", "lucene-smartcn",
    }
    assert fixed["top_k"] == 5 and fixed["passes"] == 2
    assert fixed["provider_calls_allowed"] == 0
    assert fixed["network_allowed"] is False
    assert all(arm["k1"] == 1.5 and arm["b"] == .75 for arm in fixed["arms"])


@pytest.mark.parametrize("mutation", [
    "top-k", "float-top-k", "passes", "extra-arm", "missing-arm",
    "duplicate-id", "backend", "unknown-parameter", "k1", "b",
    "modern-mode", "paid", "network",
])
def test_protocol_rejects_unfrozen_candidate_and_parameter_changes(mutation):
    fixed = protocol()
    if mutation == "top-k": fixed["top_k"] = 10
    elif mutation == "float-top-k": fixed["top_k"] = 5.0
    elif mutation == "passes": fixed["passes"] = 3
    elif mutation == "extra-arm": fixed["arms"].append(deepcopy(fixed["arms"][-1]))
    elif mutation == "missing-arm": fixed["arms"].pop()
    elif mutation == "duplicate-id": fixed["arms"][-1]["id"] = fixed["arms"][0]["id"]
    elif mutation == "backend": fixed["arms"][-1]["backend"] = "unregistered-engine"
    elif mutation == "unknown-parameter": fixed["arms"][-1]["per_case_boost"] = 1
    elif mutation == "k1": fixed["arms"][-1]["k1"] = 1.2
    elif mutation == "b": fixed["arms"][-1]["b"] = .5
    elif mutation == "modern-mode":
        next(arm for arm in fixed["arms"] if arm["backend"] == "bm25s")["mode"] = "search"
    elif mutation == "paid": fixed["provider_calls_allowed"] = 1
    elif mutation == "network": fixed["network_allowed"] = True
    with pytest.raises((TypeError, ValueError)):
        benchmark.validate_protocol(fixed)


@pytest.mark.parametrize("backend", ["bm25s", "bm25s-smartcn", "lucene-smartcn"])
def test_all_declared_modern_backends_can_be_selected(backend):
    fixed, results, executions = selection_inputs()
    target = next(arm["id"] for arm in fixed["arms"] if arm["backend"] == backend)
    results[target]["first_pass"]["metrics"].update(hit_at_5=.9, mrr=.9)
    assert benchmark.selection(fixed, results, executions, True) == (True, target, target)


@pytest.mark.parametrize("cause", [
    "exit", "timeout", "missing-result", "missing-execution", "extra-result",
    "repeat", "source-drift", "error-first", "error-second", "global-drift",
    "ranking-denominator", "case-denominator", "missing-quality",
])
def test_any_failed_partial_or_unstable_arm_prevents_a_winner(cause):
    fixed, results, executions = selection_inputs()
    target = fixed["arms"][-1]["id"]
    if cause == "exit": executions[target]["exit_code"] = 1
    elif cause == "timeout": executions[target]["exit_code"] = "timeout"
    elif cause == "missing-result": results.pop(target)
    elif cause == "missing-execution": executions.pop(target)
    elif cause == "extra-result": results["unregistered"] = deepcopy(results[target])
    elif cause == "repeat": results[target]["repeat_mismatch_case_ids"] = ["synthetic-1"]
    elif cause == "source-drift": results[target]["inputs_and_implementation_stable"] = False
    elif cause == "error-first": results[target]["first_pass"]["error_count"] = 1
    elif cause == "error-second": results[target]["second_pass"]["error_count"] = 1
    elif cause == "ranking-denominator": results[target]["first_pass"]["quality_denominator"] = 107
    elif cause == "case-denominator": results[target]["second_pass"]["case_count"] = 119
    elif cause == "missing-quality": results[target]["first_pass"]["metrics"]["mrr"] = None
    assert benchmark.selection(fixed, results, executions, cause != "global-drift") == (False, None, None)


@pytest.mark.parametrize("regressed_metric", ["hit_at_5", "mrr"])
def test_selected_modern_arm_is_not_promotable_if_either_guard_metric_regresses(regressed_metric):
    fixed, results, executions = selection_inputs()
    target = next(arm["id"] for arm in fixed["arms"] if arm["backend"] == "lucene-smartcn")
    for arm in fixed["arms"]:
        if arm["backend"] != "historical":
            results[arm["id"]]["first_pass"]["metrics"].update(hit_at_5=.4, mrr=.4)
    results[target]["first_pass"]["metrics"].update(hit_at_5=.8, mrr=.8)
    results[target]["first_pass"]["metrics"][regressed_metric] = .5
    assert benchmark.selection(fixed, results, executions, True) == (True, target, None)


def test_no_gold_is_na_and_execution_failure_is_not_a_quality_zero():
    summary = benchmark.aggregate([
        synthetic_row("hit", hit=1), synthetic_row("miss", hit=0),
        synthetic_row("execution-error", status="failed"), synthetic_row("no-gold"),
    ])
    assert summary["case_count"] == 4
    assert summary["quality_denominator"] == 2
    assert summary["error_count"] == 1 and summary["no_gold_count"] == 1
    assert summary["metrics"]["hit_at_5"] == .5
    assert benchmark.aggregate([synthetic_row("error", status="failed")])["metrics"]["hit_at_5"] is None


def test_synthetic_scoring_and_pairing_keep_canonical_metrics_and_valid_denominators():
    case = EvalCase("synthetic", "合成问题", "synthetic", "合成法", ["第十条"], [])
    chunk = Chunk("synthetic-chunk", "合成正文", ["合成法"], ["第十条"], [], [], "article")
    result = SearchResult(chunk, 1.0, 1, "bm25")
    assert benchmark.score_case(case, [result])["hit_at_5"] == 1
    assert benchmark.score_case(EvalCase("no-gold", "合成问题", "synthetic", "", [], []), [result]) is None
    baseline = {"passes": [[synthetic_row("a", hit=0), synthetic_row("b", hit=1), synthetic_row("c")]]}
    candidate = {"passes": [[synthetic_row("a", hit=1), synthetic_row("b", status="failed"), synthetic_row("c")]]}
    pairs = benchmark.paired_summary(baseline, candidate)
    assert pairs["paired_quality_denominator"] == 1
    assert pairs["improved_case_ids"] == ["a"]
    assert pairs["regressed_case_ids"] == []


def test_source_identity_covers_glue_and_actual_shared_scoring_dependencies():
    identity = benchmark.source_identity(PROTOCOL_PATH)
    assert {
        "scripts/benchmark_smartcn.py", "scripts/smartcn_bridge.py",
        "scripts/java/SmartCnBridge.java", "scripts/benchmark_chinese_bm25.py",
        "legal_rag/chinese_bm25.py", "legal_rag/retrieval.py",
        "legal_rag/evaluation.py", "legal_rag/evaluation_scoring.py",
        "legal_rag/retrieval_contracts.py", "configs/chinese-bm25-benchmark-v3.json",
        "pyproject.toml", "uv.lock",
    } <= identity.keys()
    assert all(isinstance(value, str) and len(value) == 64 for value in identity.values())


class FakeProcess:
    """Only the public Popen/psutil lifecycle operations used for cleanup."""

    def __init__(self, pid, *, running=True, resistant=False, children=()):
        self.pid = pid
        self.running = running
        self.resistant = resistant
        self.descendants = list(children)
        self.wait_calls = []

    def children(self, recursive=False):
        assert recursive is True
        return list(self.descendants)

    def create_time(self):
        return float(self.pid)

    def terminate(self):
        if not self.resistant:
            self.running = False

    def kill(self):
        if not self.resistant:
            self.running = False

    def is_running(self):
        return self.running

    def poll(self):
        return None if self.running else 0

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.running:
            raise subprocess.TimeoutExpired("synthetic-process", timeout)
        return 0


def patch_processes(monkeypatch, nodes):
    import psutil

    by_pid = {node.pid: node for node in nodes}

    def process(pid):
        node = by_pid.get(pid)
        if node is None or not node.running:
            raise psutil.NoSuchProcess(pid)
        return node

    monkeypatch.setattr(psutil, "Process", process)
    monkeypatch.setattr(psutil, "wait_procs", lambda values, timeout:
                        ([node for node in values if not node.running],
                         [node for node in values if node.running]))


def test_cleanup_stops_descendants_and_reaps_parent(monkeypatch):
    child, grandchild = FakeProcess(101), FakeProcess(102)
    parent = FakeProcess(100, children=(child, grandchild))
    patch_processes(monkeypatch, (parent, child, grandchild))
    benchmark.terminate_process_tree(parent, grace_seconds=.01)
    assert not any(node.running for node in (parent, child, grandchild))
    assert parent.wait_calls


def test_cleanup_retains_known_descendant_authority_after_parent_has_exited(monkeypatch):
    child = FakeProcess(101)
    parent = FakeProcess(100, running=False)
    patch_processes(monkeypatch, (parent, child))
    benchmark.terminate_process_tree(parent, known_descendants=(child,), grace_seconds=.01)
    assert not child.running
    benchmark.terminate_process_tree(parent, known_descendants=(child,), grace_seconds=.01)
    assert not child.running


def test_cleanup_failure_is_not_silently_reported_as_success(monkeypatch):
    child = FakeProcess(101, resistant=True)
    parent = FakeProcess(100, children=(child,))
    patch_processes(monkeypatch, (parent, child))
    with pytest.raises(RuntimeError):
        benchmark.terminate_process_tree(parent, known_descendants=(child,), grace_seconds=.01)


@pytest.mark.parametrize("cause", ["missing-pass", "missing-exit", "wrong-pass-type", "missing-metric", "worker-failure"])
def test_malformed_or_explicitly_failed_results_are_not_complete(cause):
    fixed, results, executions = selection_inputs()
    target = fixed["arms"][-1]["id"]
    if cause == "missing-pass": results[target].pop("second_pass")
    elif cause == "missing-exit": executions[target].pop("exit_code")
    elif cause == "wrong-pass-type": results[target]["first_pass"] = None
    elif cause == "missing-metric": results[target]["first_pass"]["metrics"].pop("hit_at_5")
    elif cause == "worker-failure": results[target]["worker_failure"] = "cleanup_failed"
    assert benchmark.selection(fixed, results, executions, True) == (False, None, None)


def complete_pass_result():
    rows = [synthetic_row(f"synthetic-{number}", hit=int(number < 60)) for number in range(108)]
    rows.extend(synthetic_row(f"no-gold-{number}") for number in range(12))
    return {"passes": [rows, deepcopy(rows)]}


def test_safe_pairing_retains_the_full_canonical_denominator():
    baseline = complete_pass_result()
    candidate = deepcopy(baseline)
    candidate["passes"][0][60]["metrics"].update(hit_at_5=1)
    paired = benchmark.safe_paired_summary(baseline, candidate, protocol()["bootstrap"])
    assert paired["paired_quality_denominator"] == 108
    assert paired["improved_case_ids"] == ["synthetic-60"]


@pytest.mark.parametrize("cause", ["empty-baseline", "partial", "duplicate", "mismatched-id", "failed", "failed-no-gold", "missing-pass"])
def test_partial_or_failed_pairing_remains_unknown_not_zero(cause):
    baseline = complete_pass_result()
    candidate = complete_pass_result()
    if cause == "empty-baseline": baseline["passes"][0] = []
    elif cause == "partial": candidate["passes"][0].pop()
    elif cause == "duplicate": candidate["passes"][0][1]["case_id"] = candidate["passes"][0][0]["case_id"]
    elif cause == "mismatched-id": candidate["passes"][0][0]["case_id"] = "different-identity"
    elif cause == "failed": candidate["passes"][0][0]["status"] = "failed"
    elif cause == "failed-no-gold": candidate["passes"][0][-1]["status"] = "failed"
    elif cause == "missing-pass": candidate.pop("passes")
    assert benchmark.safe_paired_summary(baseline, candidate, protocol()["bootstrap"]) is None


def test_worker_start_failure_is_an_explicit_failure_code(monkeypatch, tmp_path):
    def start_failure(*args, **kwargs):
        raise OSError("synthetic start failure")
    monkeypatch.setattr(benchmark.subprocess, "Popen", start_failure)
    assert benchmark.run_worker(tmp_path, protocol()["arms"][-1], .01) == "start_failed"


def test_worker_timeout_stays_timeout_after_successful_descendant_cleanup(monkeypatch, tmp_path):
    child = FakeProcess(101)
    parent = FakeProcess(100, children=(child,))
    patch_processes(monkeypatch, (parent, child))
    monkeypatch.setattr(benchmark.subprocess, "Popen", lambda *args, **kwargs: parent)

    class FakeSampler:
        def __init__(self, process):
            self.descendants = {(child.pid, child.create_time()): child}
        def start(self): return self
        def stop(self): return {"memory_sampling_errors": 0}

    monkeypatch.setattr(benchmark, "ProcessTreeSampler", FakeSampler)
    assert benchmark.run_worker(tmp_path, protocol()["arms"][-1], .01) == "timeout"
    assert not child.running and not parent.running


def patch_synthetic_run(monkeypatch, tmp_path):
    from scripts import offline_retrieval_ab, smartcn_bridge

    fixed, results, executions = selection_inputs()
    for result in results.values():
        result.update(complete_pass_result())
    config = tmp_path / "protocol.json"
    config.write_text(json.dumps(fixed), encoding="utf-8")
    monkeypatch.setattr(benchmark, "ROOT", tmp_path)
    monkeypatch.setattr(benchmark, "source_identity", lambda *args: {"synthetic.py": "a" * 64})
    monkeypatch.setattr(benchmark.common, "digest", lambda *args: "b" * 64)
    monkeypatch.setattr(benchmark.subprocess, "check_output", lambda *args, **kwargs: "synthetic-head")

    class FakeDataset:
        def manifest_payload(self): return {"dataset_id": "synthetic-only"}

    monkeypatch.setattr(benchmark.common, "load_inputs", lambda *args: ([], [], FakeDataset()))
    monkeypatch.setattr(offline_retrieval_ab, "input_identity", lambda *args: {"chunk_count": 0})
    monkeypatch.setattr(smartcn_bridge, "prepare_bridge", lambda *args: {"synthetic-runtime": "fixed"})

    def create_directory(root, run_id):
        directory = root / run_id
        directory.mkdir(parents=True)
        return directory

    monkeypatch.setattr(offline_retrieval_ab, "create_run_directory", create_directory)

    def fake_worker(directory, arm, timeout):
        benchmark.common.write_new(directory / (arm["id"] + ".json"), results[arm["id"]])
        return executions[arm["id"]]["exit_code"]

    monkeypatch.setattr(benchmark, "run_worker", fake_worker)
    return config, tmp_path / "artifacts/experiments/synthetic-only"


def test_runtime_validation_exception_preserves_a_failed_summary(monkeypatch, tmp_path):
    config, directory = patch_synthetic_run(monkeypatch, tmp_path)
    def runtime_error(*args):
        raise ValueError("synthetic runtime changed")
    monkeypatch.setattr(benchmark, "runtime_identity", runtime_error)
    assert benchmark.run("synthetic-only", tmp_path, config) == 1
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "failed"
    assert summary["critical_identity_stable"] is False
    assert summary["selected_modern_arm"] is None


@pytest.mark.parametrize("changed_input", ["index", "dataset"])
def test_input_drift_after_all_workers_prevents_final_completion(monkeypatch, tmp_path, changed_input):
    config, directory = patch_synthetic_run(monkeypatch, tmp_path)
    monkeypatch.setattr(benchmark, "runtime_identity", lambda *args: {"synthetic-runtime": "fixed"})
    old_worker, old_digest, old_load = benchmark.run_worker, benchmark.common.digest, benchmark.common.load_inputs
    completed = [0]

    def worker(*args):
        code = old_worker(*args)
        completed[0] += 1
        return code

    def digest(path):
        if completed[0] == 4 and changed_input == "index" and Path(path).name == "chunks.jsonl":
            return "c" * 64
        return old_digest(path)

    class ChangedDataset:
        def manifest_payload(self): return {"dataset_id": "changed-after-last-worker"}

    def load_inputs(*args):
        chunks, cases, dataset = old_load(*args)
        if completed[0] == 4 and changed_input == "dataset":
            return chunks, cases, ChangedDataset()
        return chunks, cases, dataset

    monkeypatch.setattr(benchmark, "run_worker", worker)
    monkeypatch.setattr(benchmark.common, "digest", digest)
    monkeypatch.setattr(benchmark.common, "load_inputs", load_inputs)
    assert benchmark.run("synthetic-only", tmp_path, config) == 1
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "failed" and summary["critical_identity_stable"] is False
    assert summary["selected_modern_arm"] is None
