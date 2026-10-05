"""Frozen, local-only ranking comparison. Never an end-to-end legal benchmark."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROTOCOL = ROOT / "configs/chinese-bm25-benchmark.json"
CRITICAL = ("scripts/benchmark_chinese_bm25.py", "configs/chinese-bm25-benchmark.json",
            "legal_rag/chinese_bm25.py", "legal_rag/retrieval.py", "legal_rag/models.py",
            "legal_rag/chunking.py", "legal_rag/evaluation_scoring.py",
            "legal_rag/experiment_datasets.py", "legal_rag/evaluation_artifacts.py",
            "scripts/offline_retrieval_ab.py", "pyproject.toml", "uv.lock")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def critical_identity():
    return {name: digest(ROOT / name) for name in CRITICAL}


def write_new(path, value):
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def disable_network():
    # Process-local enforced guard, not an OS-level air gap. No model client exists.
    def guard(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
            raise RuntimeError("benchmark_network_forbidden")
    sys.addaudithook(guard)


def load_inputs(protocol):
    from legal_rag.chunking import load_chunks
    from legal_rag.experiment_datasets import load_dataset_registry
    registry = load_dataset_registry(ROOT / "eval_cases/registry.json", repository_root=ROOT)
    dataset = registry.dataset(protocol["dataset_id"])
    cases = list(dataset.cases)
    if len(cases) != 120 or sum(bool(c.expected_law or c.expected_articles) for c in cases) != 108:
        raise ValueError("registered_denominator_changed")
    return load_chunks(ROOT / protocol["index"]), cases, dataset


def build_arm(arm, chunks):
    if arm["backend"] == "historical":
        from legal_rag.retrieval import BM25Retriever
        return BM25Retriever(chunks, **{key: value for key, value in arm.items()
                                       if key not in {"id", "backend"}})
    from legal_rag.chinese_bm25 import ChineseBM25Retriever
    if arm["backend"] != "bm25s" or arm["method"] != "lucene":
        raise ValueError("unsupported_protocol_arm")
    return ChineseBM25Retriever(chunks, **{key: value for key, value in arm.items()
                                         if key not in {"id", "backend", "method"}})


def score_case(case, results):
    from legal_rag.evaluation_scoring import hit_at_k, mean_reciprocal_rank, target_coverage
    if not (case.expected_law or case.expected_articles):
        return None
    return {"hit_at_3": hit_at_k(results, case, 3), "hit_at_5": hit_at_k(results, case, 5),
            "mrr": mean_reciprocal_rank(results, case), "target_coverage": target_coverage(results, case, 5)}


def aggregate(rows):
    from legal_rag.evaluation import percentile
    valid = [row for row in rows if row["status"] == "succeeded" and row["metrics"] is not None]
    latency = [row["latency_ms"] for row in rows if row["status"] == "succeeded"]
    return {"case_count": len(rows), "quality_denominator": len(valid),
            "error_count": sum(row["status"] != "succeeded" for row in rows),
            "no_gold_count": sum(row["status"] == "succeeded" and row["metrics"] is None for row in rows),
            "metrics": {name: statistics.mean(row["metrics"][name] for row in valid) if valid else None
                        for name in ("hit_at_3", "hit_at_5", "mrr", "target_coverage")},
            "hit_at_5_count": sum(row["metrics"]["hit_at_5"] for row in valid) if valid else None,
            "query_p50_ms": percentile(latency, .5) if latency else None,
            "query_p95_ms": percentile(latency, .95) if latency else None}


def execute_worker(directory, arm_id):
    import psutil
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    protocol = manifest["protocol"]
    arm = next(arm for arm in protocol["arms"] if arm["id"] == arm_id)
    if critical_identity() != manifest["critical_file_sha256"]:
        raise ValueError("implementation_changed_before_worker")
    chunks, cases, dataset = load_inputs(protocol)
    if digest(ROOT / protocol["index"]) != manifest["index_sha256"] or dataset.manifest_payload() != manifest["dataset"]:
        raise ValueError("inputs_changed_before_worker")
    process = psutil.Process()
    rss_before = process.memory_info().rss
    started = time.perf_counter()
    retriever = build_arm(arm, chunks)
    build_ms = (time.perf_counter() - started) * 1000
    rss_after = process.memory_info().rss
    passes = []
    for repetition in range(protocol["passes"]):
        rows = []
        for case in (cases if repetition == 0 else list(reversed(cases))):
            started = time.perf_counter()
            try:
                results = retriever.retrieve(case.question, top_k=protocol["top_k"])
                elapsed = (time.perf_counter() - started) * 1000
                rows.append({"case_id": case.case_id, "case_type": case.case_type,
                             "stratum": "explicit" if re.search(r"《[^》]+》.*第[^条]{1,30}条", case.question) else "scenario",
                             "status": "succeeded", "error_type": None, "latency_ms": elapsed,
                             "metrics": score_case(case, results),
                             "ranked_results": [{"chunk_id": row.chunk.chunk_id, "score": row.score,
                                                 "rank": row.rank} for row in results]})
            except Exception as exc:
                rows.append({"case_id": case.case_id, "case_type": case.case_type, "stratum": "unknown",
                             "status": "failed", "error_type": type(exc).__name__, "metrics": None,
                             "latency_ms": (time.perf_counter() - started) * 1000, "ranked_results": None})
        passes.append(rows)
    first = {row["case_id"]: row for row in passes[0]}
    repeats = [row["case_id"] for row in passes[1] if row["status"] != "succeeded"
               or first[row["case_id"]]["status"] != "succeeded"
               or row["ranked_results"] != first[row["case_id"]]["ranked_results"]]
    info = process.memory_info()
    peak = getattr(info, "peak_wset", None)
    if peak is None:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    stable = (critical_identity() == manifest["critical_file_sha256"]
              and digest(ROOT / protocol["index"]) == manifest["index_sha256"]
              and load_inputs(protocol)[2].manifest_payload() == manifest["dataset"])
    result = {"arm_id": arm_id, "config_identity": getattr(retriever, "config_identity", arm),
              "build_ms": build_ms, "rss_before_build_bytes": rss_before, "rss_after_build_bytes": rss_after,
              "process_peak_rss_bytes": peak, "memory_scope": "fresh arm subprocess including Python, imports and corpus",
              "first_pass": aggregate(passes[0]), "second_pass": aggregate(passes[1]),
              "strata": {name: aggregate([row for row in passes[0] if row["stratum"] == name])
                         for name in ("explicit", "scenario")},
              "categories": {name: aggregate([row for row in passes[0] if row["case_type"] == name])
                             for name in sorted({case.case_type for case in cases})},
              "repeat_mismatch_case_ids": repeats, "inputs_and_implementation_stable": stable,
              "passes": passes, "actual_provider_calls": 0}
    write_new(directory / (arm_id + ".json"), result)
    return 0 if stable and not repeats and result["first_pass"]["error_count"] == 0 else 1


def paired_summary(baseline, candidate):
    from legal_rag.evaluation import bootstrap_ci
    old = {row["case_id"]: row for row in baseline["passes"][0]}
    pairs = [(old[row["case_id"]], row) for row in candidate["passes"][0]]
    valid = [(a, b) for a, b in pairs if a["status"] == b["status"] == "succeeded"
             and a["metrics"] is not None and b["metrics"] is not None]
    deltas = [b["metrics"]["hit_at_5"] - a["metrics"]["hit_at_5"] for a, b in valid]
    return {"paired_quality_denominator": len(valid),
            "improved_case_ids": [b["case_id"] for a, b in valid if b["metrics"]["hit_at_5"] > a["metrics"]["hit_at_5"]],
            "regressed_case_ids": [b["case_id"] for a, b in valid if b["metrics"]["hit_at_5"] < a["metrics"]["hit_at_5"]],
            "hit_at_5_delta_ci95": list(bootstrap_ci(deltas)) if deltas else None}


def run(run_id):
    from scripts.offline_retrieval_ab import create_run_directory, input_identity
    from legal_rag.manifest import utc_now
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    chunks, cases, dataset = load_inputs(protocol)
    directory = create_run_directory(ROOT / "artifacts/experiments", run_id)
    manifest = {"run_id": run_id, "created_at": utc_now(), "protocol": protocol,
                "protocol_sha256": digest(PROTOCOL), "critical_file_sha256": critical_identity(),
                "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "index_sha256": digest(ROOT / protocol["index"]), "corpus": input_identity(chunks),
                "dataset": dataset.manifest_payload(), "actual_calls_policy": "no model client; socket audit guard",
                "python": sys.version.split()[0]}
    write_new(directory / "manifest.json", manifest)
    results, executions = {}, {}
    for arm in protocol["arms"]:
        started = time.perf_counter()
        try:
            with (directory / (arm["id"] + ".log")).open("x", encoding="utf-8") as output:
                completed = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--worker", arm["id"],
                                            "--directory", str(directory)], cwd=ROOT,
                                           stdout=output, stderr=subprocess.STDOUT, timeout=protocol["worker_timeout_seconds"])
            code = completed.returncode
        except subprocess.TimeoutExpired:
            code = "timeout"
        executions[arm["id"]] = {"exit_code": code, "elapsed_ms": (time.perf_counter()-started)*1000}
        result_path = directory / (arm["id"] + ".json")
        if result_path.exists():
            results[arm["id"]] = json.loads(result_path.read_text(encoding="utf-8"))
        print(json.dumps({"arm": arm["id"], **executions[arm["id"]]}, ensure_ascii=False), flush=True)
    completed = all(value["exit_code"] == 0 for value in executions.values()) and len(results) == len(protocol["arms"])
    stable = critical_identity() == manifest["critical_file_sha256"] and digest(PROTOCOL) == manifest["protocol_sha256"]
    eligible = [results[name] for name in ("bm25s-jieba-precise", "bm25s-jieba-search")
                if name in results and executions[name]["exit_code"] == 0]
    selected = max(eligible, key=lambda item: (item["first_pass"]["metrics"]["hit_at_5"],
                  item["first_pass"]["metrics"]["mrr"], -item["second_pass"]["query_p95_ms"]))["arm_id"] if completed and stable else None
    summary = {"run_id": run_id, "status": "completed" if completed and stable else "failed",
               "manifest_sha256": digest(directory / "manifest.json"), "protocol_sha256": manifest["protocol_sha256"],
               "critical_identity_stable": stable, "executions": executions, "selected_modern_arm": selected,
               "arms": {name: {key: value for key, value in result.items() if key != "passes"} for name, result in results.items()},
               "paired_vs_legacy": {name: paired_summary(results["legacy-v1"], result) for name, result in results.items()
                                    if name != "legacy-v1"} if "legacy-v1" in results else None,
               "actual_provider_calls": 0, "new_model_cost_cny": "0", "limits": protocol["limits"],
               "finished_at": utc_now()}
    write_new(directory / "summary.json", summary)
    print(json.dumps({"run_id": run_id, "status": summary["status"], "selected": selected,
                      "summary_sha256": digest(directory / "summary.json")}, ensure_ascii=False), flush=True)
    return 0 if summary["status"] == "completed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id")
    parser.add_argument("--worker", choices=("legacy-v1", "generic-v3", "bm25s-jieba-precise", "bm25s-jieba-search"))
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    for name, value in {"LEGAL_RAG_DISABLE_DOTENV": "1", "PYTHON_DOTENV_DISABLED": "1", "ALLOW_LIVE_MODEL_CALLS": "false",
                        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}.items():
        os.environ[name] = value
    disable_network()
    if args.worker:
        if args.directory is None or args.directory.resolve().parent != (ROOT / "artifacts/experiments").resolve():
            parser.error("worker directory must be one immediate local experiment directory")
        return execute_worker(args.directory.resolve(), args.worker)
    if not args.run_id:
        parser.error("--run-id is required")
    return run(args.run_id)


if __name__ == "__main__":
    raise SystemExit(main())
