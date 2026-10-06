"""One frozen SmartCN comparison, with no production or provider integration."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import benchmark_chinese_bm25 as common

PROTOCOL = ROOT / "configs/chinese-bm25-benchmark-v3.json"
aggregate = common.aggregate
score_case = common.score_case
paired_summary = common.paired_summary
FIXED_ARMS = [
    {"id": "legacy-v1", "backend": "historical", "lexical_profile": "legacy-v1",
     "k1": 1.5, "b": .75, "law_boost": 40.0, "article_boost": 80.0, "deprecated_penalty": .5},
    {"id": "bm25s-sklearn-char", "backend": "bm25s", "mode": "char",
     "k1": 1.5, "b": .75, "hmm": None, "method": "lucene"},
    {"id": "bm25s-smartcn", "backend": "bm25s-smartcn", "k1": 1.5, "b": .75},
    {"id": "lucene-smartcn", "backend": "lucene-smartcn", "k1": 1.5, "b": .75},
]
JAR_HASHES = {
    "lucene-core": "b64a3f8098a7572034fb30085cdee01b34ec81fb0e5a31b471536af58dc6c01b",
    "lucene-analysis-common": "fa571bd7caf0f0b4faf46a72ca004a7836f348c31d92bd522dddcc3d128d287e",
    "lucene-analysis-smartcn": "06db5436be801787738b9735ebe81622e263826a39dedc66402a99a1c33fab36",
}


def canonical(value):
    return json.dumps(value, sort_keys=True, allow_nan=False, ensure_ascii=False, separators=(",", ":"))


def validate_protocol(protocol):
    """Reject silent candidate, parameter, denominator or permission expansion."""
    expected = {"schema_version": 1, "protocol_id": "chinese-bm25-smartcn-fixed-v3",
                "dataset_id": "legal-eval-v3", "dataset_role": "repeated_development_not_holdout",
                "index": "artifacts/indexes/article/chunks.jsonl", "top_k": 5, "passes": 2,
                "arms": FIXED_ARMS, "provider_calls_allowed": 0, "network_allowed": False,
                "promotion_requires_legacy_non_regression": True, "worker_timeout_seconds": 900,
                "bootstrap": {"seed": 42, "resamples": 2000, "confidence": .95},
                "metrics": ["hit_at_3", "hit_at_5", "mrr", "target_coverage"]}
    descriptions = {"modern_index_text", "smartcn_analyzer", "smartcn_query", "native_engine",
                    "token_control_engine", "selection", "runtime_scope", "timing_scope",
                    "memory_scope", "network_boundary"}
    if not isinstance(protocol, dict) or set(protocol) != set(expected) | descriptions | {"dependencies", "limits"}:
        raise ValueError("invalid_protocol_fields")
    if any(canonical(protocol[name]) != canonical(value) for name, value in expected.items()):
        raise ValueError("unfrozen_protocol_value")
    if any(not isinstance(protocol[name], str) or not protocol[name].strip() for name in descriptions):
        raise ValueError("missing_protocol_description")
    if not isinstance(protocol["limits"], list) or not protocol["limits"] or any(
        not isinstance(item, str) or not item for item in protocol["limits"]
    ):
        raise ValueError("invalid_protocol_limits")
    dependencies = {"version": "9.12.3", "java_major": 17, "artifacts": [
        {"artifact": name, "filename": f"{name}-9.12.3.jar", "sha256": digest,
         "url": f"https://repo.maven.apache.org/maven2/org/apache/lucene/{name}/9.12.3/{name}-9.12.3.jar"}
        for name, digest in JAR_HASHES.items()
    ]}
    if canonical(protocol["dependencies"]) != canonical(dependencies):
        raise ValueError("unfrozen_java_dependencies")


def source_identity(protocol_path=PROTOCOL):
    names = (*common.CRITICAL, "scripts/benchmark_smartcn.py", "scripts/smartcn_bridge.py",
             "scripts/java/SmartCnBridge.java", str(Path(protocol_path).resolve().relative_to(ROOT)).replace("\\", "/"))
    return {name: common.digest(ROOT / name) for name in dict.fromkeys(names)}


def selection(protocol, results, executions, stable):
    """All four arms must complete both full passes before selecting any arm."""
    try:
        return _selection(protocol, results, executions, stable)
    except (KeyError, TypeError, ValueError, AttributeError):
        return False, None, None


def _selection(protocol, results, executions, stable):
    names = {arm["id"] for arm in protocol["arms"]}
    if not stable or set(results) != names or set(executions) != names:
        return False, None, None
    for name in names:
        result = results[name]
        if (executions[name]["exit_code"] != 0 or result.get("worker_failure")
                or not result.get("runtime_identity_stable")
                or not result.get("inputs_and_implementation_stable") or result.get("repeat_mismatch_case_ids")):
            return False, None, None
        for pass_name in ("first_pass", "second_pass"):
            part = result[pass_name]
            if (part["error_count"] != 0 or part["case_count"] != 120
                    or part["quality_denominator"] != 108 or part["no_gold_count"] != 12):
                return False, None, None
            if set(part["metrics"]) != set(protocol["metrics"]):
                return False, None, None
            values = [*part["metrics"].values(), part["query_p95_ms"]]
            if any(isinstance(value, bool) or not isinstance(value, (int, float))
                   or not math.isfinite(value) or value < 0 for value in values):
                return False, None, None
    # Reuse the previously tested selection/guard, making all explicitly
    # declared modern engines eligible rather than silently excluding Lucene.
    equivalent = deepcopy(protocol)
    for arm in equivalent["arms"]:
        if arm["backend"] in {"bm25s-smartcn", "lucene-smartcn"}:
            arm["backend"] = "bm25s"
    return common.selection(equivalent, results, executions, stable)


def safe_paired_summary(baseline, candidate, bootstrap):
    """Incomplete or misaligned observations are unknown, not measured zeroes."""
    try:
        left, right = baseline["passes"][0], candidate["passes"][0]
        left_ids, right_ids = ({row["case_id"] for row in rows} for rows in (left, right))
        if len(left) != 120 or len(right) != 120 or len(left_ids) != 120 or left_ids != right_ids:
            return None
        if any(row["status"] != "succeeded" for row in (*left, *right)):
            return None
        result = paired_summary(baseline, candidate, bootstrap)
        return result if result["paired_quality_denominator"] == 108 else None
    except (KeyError, TypeError, ValueError, IndexError):
        return None


class ProcessTreeSampler:
    """Sample simultaneous RSS, not the sum of unrelated process peaks."""

    def __init__(self, process, interval=.02):
        self.process, self.interval = process, interval
        self.peak_bytes, self.samples, self.errors = 0, 0, 0
        self.descendants = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _sample(self):
        import psutil
        try:
            children = self.process.children(recursive=True)
            for child in children:
                self.descendants[(child.pid, child.create_time())] = child
            processes = [self.process, *children]
            total = 0
            for process in processes:
                try:
                    total += process.memory_info().rss
                except psutil.NoSuchProcess:
                    pass
            self.peak_bytes = max(self.peak_bytes, total)
            self.samples += 1
        except psutil.NoSuchProcess:
            pass
        except psutil.Error:
            self.errors += 1

    def _loop(self):
        while not self._stop.is_set():
            self._sample()
            self._stop.wait(self.interval)

    def start(self):
        self._sample()
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)
        self._sample()
        return {"sampled_process_tree_peak_rss_bytes": self.peak_bytes,
                "memory_sample_interval_ms": self.interval * 1000,
                "memory_samples": self.samples, "memory_sampling_errors": self.errors,
                "memory_scope": "simultaneous sampled worker plus live descendants RSS; javac preparation excluded"}


def terminate_process_tree(process, *, known_descendants=(), grace_seconds=3.0):
    """Only stop this spawned worker and its observed descendants, never a name scan."""
    import psutil
    owned = list(known_descendants)
    try:
        parent = psutil.Process(process.pid)
        owned.extend(parent.children(recursive=True))
        owned.append(parent)
    except psutil.NoSuchProcess:
        pass
    unique = {}
    for item in owned:
        try:
            unique[(item.pid, item.create_time())] = item
        except psutil.NoSuchProcess:
            pass
    for item in unique.values():
        try:
            item.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(list(unique.values()), timeout=grace_seconds)
    for item in alive:
        try:
            item.kill()
        except psutil.NoSuchProcess:
            pass
    _, survivors = psutil.wait_procs(alive, timeout=grace_seconds)
    if survivors:
        raise RuntimeError("worker_descendant_cleanup_failed")
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=grace_seconds)


def run_worker(directory, arm, timeout):
    import psutil
    with (directory / (arm["id"] + ".log")).open("x", encoding="utf-8") as log:
        try:
            process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()),
                                        "--worker", arm["id"], "--directory", str(directory)],
                                       cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, shell=False)
        except OSError:
            return "start_failed"
        sampler = None
        try:
            try:
                sampler = ProcessTreeSampler(psutil.Process(process.pid)).start()
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                code = "timeout"
            finally:
                try:
                    if sampler is not None:
                        sampler.stop()
                finally:
                    terminate_process_tree(process, known_descendants=tuple(sampler.descendants.values()) if sampler else ())
            return code
        except Exception:
            # A cleanup error is not a successful benchmark result.
            return "cleanup_failed"


def build_arm(arm, chunks, directory, manifest):
    if arm["backend"] in {"historical", "bm25s"}:
        return common.build_arm(arm, chunks)
    from scripts.smartcn_bridge import SmartCnRetriever
    return SmartCnRetriever(chunks, backend="lucene" if arm["backend"] == "lucene-smartcn" else "bm25s",
                            dependency_dir=Path(manifest["dependency_dir"]),
                            work_dir=directory / (arm["id"] + "-runtime"),
                            prepared_build_dir=directory / "prepared-java", k1=arm["k1"], b=arm["b"])


def runtime_identity(manifest, directory):
    from scripts.smartcn_bridge import verify_prepared_bridge
    return verify_prepared_bridge(Path(manifest["dependency_dir"]), directory / "prepared-java")


def execute_worker(directory, arm_id):
    import psutil
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    protocol = manifest["protocol"]
    validate_protocol(protocol)
    protocol_path = ROOT / manifest["protocol_path"]
    arm = next(arm for arm in protocol["arms"] if arm["id"] == arm_id)
    if source_identity(protocol_path) != manifest["critical_file_sha256"]:
        raise ValueError("source_changed_before_worker")
    if runtime_identity(manifest, directory) != manifest["runtime_identity"]:
        raise ValueError("runtime_changed_before_worker")
    sampler = ProcessTreeSampler(psutil.Process()).start()
    retriever, passes, failure = None, [], None
    build_ms = None
    try:
        chunks, cases, dataset = common.load_inputs(protocol)
        if common.digest(ROOT / protocol["index"]) != manifest["index_sha256"] or dataset.manifest_payload() != manifest["dataset"]:
            raise ValueError("inputs_changed_before_worker")
        started = time.perf_counter()
        retriever = build_arm(arm, chunks, directory, manifest)
        build_ms = (time.perf_counter() - started) * 1000
        for repetition in range(protocol["passes"]):
            rows = []
            for case in (cases if repetition == 0 else list(reversed(cases))):
                started = time.perf_counter()
                base = {"case_id": case.case_id, "case_type": case.case_type,
                        "stratum": "explicit" if re.search(r"《[^》]+》.*第[^条]{1,30}条", case.question) else "scenario"}
                try:
                    results = retriever.retrieve(case.question, top_k=protocol["top_k"])
                    elapsed = (time.perf_counter() - started) * 1000
                    rows.append({**base, "status": "succeeded", "error_type": None, "latency_ms": elapsed,
                                 "metrics": score_case(case, results),
                                 "ranked_results": [{"chunk_id": item.chunk.chunk_id, "score": item.score,
                                                     "rank": item.rank} for item in results]})
                except Exception as exc:
                    rows.append({**base, "status": "failed", "error_type": type(exc).__name__,
                                 "latency_ms": (time.perf_counter()-started)*1000,
                                 "metrics": None, "ranked_results": None})
            passes.append(rows)
    except Exception as exc:
        failure = type(exc).__name__
    finally:
        if retriever is not None and hasattr(retriever, "close"):
            try:
                retriever.close()
            except Exception as exc:
                failure = "cleanup_" + type(exc).__name__
        memory = sampler.stop()
    try:
        source_stable = source_identity(protocol_path) == manifest["critical_file_sha256"]
        inputs_stable = (common.digest(ROOT / protocol["index"]) == manifest["index_sha256"]
                         and common.load_inputs(protocol)[2].manifest_payload() == manifest["dataset"])
    except Exception:
        source_stable = inputs_stable = False
    try:
        runtime_stable = runtime_identity(manifest, directory) == manifest["runtime_identity"]
    except Exception:
        runtime_stable = False
    passes += [[]] * (2 - len(passes))
    first = {row["case_id"]: row for row in passes[0]}
    repeats = [row["case_id"] for row in passes[1] if row["status"] != "succeeded"
               or first.get(row["case_id"], {}).get("status") != "succeeded"
               or row["ranked_results"] != first.get(row["case_id"], {}).get("ranked_results")]
    result = {"arm_id": arm_id, "worker_failure": failure, "build_ms": build_ms,
              "config_identity": getattr(retriever, "config_identity", arm),
              "timings": getattr(retriever, "timings", {}),
              "index_stats": getattr(retriever, "index_stats", None), **memory,
              "first_pass": aggregate(passes[0]), "second_pass": aggregate(passes[1]),
              "strata": {name: aggregate([row for row in passes[0] if row["stratum"] == name])
                         for name in ("explicit", "scenario")},
              "repeat_mismatch_case_ids": repeats,
              "inputs_and_implementation_stable": source_stable and inputs_stable,
              "runtime_identity_stable": runtime_stable, "passes": passes, "actual_provider_calls": 0}
    common.write_new(directory / (arm_id + ".json"), result)
    return 0 if (not failure and source_stable and inputs_stable and runtime_stable and not repeats
                 and all(part["status"] == "succeeded" for rows in passes for part in rows)
                 and all(len(rows) == 120 for rows in passes)) else 1


def run(run_id, dependency_dir, protocol_path=PROTOCOL):
    from legal_rag.manifest import utc_now
    from scripts.offline_retrieval_ab import create_run_directory, input_identity
    from scripts.smartcn_bridge import prepare_bridge
    protocol_path, dependency_dir = Path(protocol_path).resolve(), Path(dependency_dir).resolve()
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    validate_protocol(protocol)
    chunks, _, dataset = common.load_inputs(protocol)
    directory = create_run_directory(ROOT / "artifacts/experiments", run_id)
    try:
        runtime = prepare_bridge(dependency_dir, directory / "prepared-java")
    except Exception as exc:
        common.write_new(directory / "preparation-failure.json", {
            "run_id": run_id, "status": "failed", "stage": "prepare_java",
            "error_type": type(exc).__name__, "protocol_sha256": common.digest(protocol_path),
            "actual_provider_calls": 0, "production_default_changed": False})
        raise
    manifest = {"run_id": run_id, "created_at": utc_now(), "protocol": protocol,
                "protocol_path": str(protocol_path.relative_to(ROOT)).replace("\\", "/"),
                "protocol_sha256": common.digest(protocol_path), "critical_file_sha256": source_identity(protocol_path),
                "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "index_sha256": common.digest(ROOT / protocol["index"]), "corpus": input_identity(chunks),
                "dataset": dataset.manifest_payload(), "runtime_identity": runtime,
                "dependency_dir": str(dependency_dir), "python": sys.version.split()[0],
                "actual_calls_policy": protocol["network_boundary"]}
    common.write_new(directory / "manifest.json", manifest)
    results, executions = {}, {}
    for arm in protocol["arms"]:
        started = time.perf_counter()
        code = run_worker(directory, arm, protocol["worker_timeout_seconds"])
        executions[arm["id"]] = {"exit_code": code, "elapsed_ms": (time.perf_counter()-started)*1000}
        path = directory / (arm["id"] + ".json")
        if path.is_file():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("invalid_result_type")
                results[arm["id"]] = value
            except (OSError, ValueError) as exc:
                executions[arm["id"]]["result_error"] = type(exc).__name__
        print(json.dumps({"arm": arm["id"], **executions[arm["id"]]}), flush=True)
    verification_error = None
    try:
        stable = (source_identity(protocol_path) == manifest["critical_file_sha256"]
                  and common.digest(protocol_path) == manifest["protocol_sha256"]
                  and common.digest(ROOT / protocol["index"]) == manifest["index_sha256"]
                  and common.load_inputs(protocol)[2].manifest_payload() == manifest["dataset"]
                  and runtime_identity(manifest, directory) == manifest["runtime_identity"])
    except Exception as exc:
        stable, verification_error = False, type(exc).__name__
    complete, selected, promotable = selection(protocol, results, executions, stable)
    summary = {"run_id": run_id, "status": "completed" if complete else "failed",
               "manifest_sha256": common.digest(directory / "manifest.json"),
               "protocol_sha256": manifest["protocol_sha256"], "critical_identity_stable": stable,
               "final_identity_verification_error": verification_error,
               "executions": executions, "selected_modern_arm": selected,
               "promotion_eligible_modern_arm": promotable, "production_default_changed": False,
               "arms": {name: {key: value for key, value in result.items() if key != "passes"}
                        for name, result in results.items()},
               "paired_vs_legacy": {name: safe_paired_summary(results["legacy-v1"], result, protocol["bootstrap"])
                                    for name, result in results.items() if name != "legacy-v1"}
               if "legacy-v1" in results else None,
               "actual_provider_calls": 0, "new_model_cost_cny": "0", "limits": protocol["limits"],
               "finished_at": utc_now()}
    common.write_new(directory / "summary.json", summary)
    print(json.dumps({"run_id": run_id, "status": summary["status"], "selected": selected,
                      "summary_sha256": common.digest(directory / "summary.json")}), flush=True)
    return 0 if complete else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id")
    parser.add_argument("--protocol", type=Path, default=PROTOCOL)
    parser.add_argument("--dependency-dir", type=Path, default=ROOT / ".tmp/smartcn-deps/9.12.3")
    parser.add_argument("--worker")
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    for name, value in {"LEGAL_RAG_DISABLE_DOTENV": "1", "PYTHON_DOTENV_DISABLED": "1",
                        "ALLOW_LIVE_MODEL_CALLS": "false", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}.items():
        os.environ[name] = value
    common.disable_network()
    if args.worker and args.directory:
        return execute_worker(args.directory.resolve(), args.worker)
    if args.run_id:
        return run(args.run_id, args.dependency_dir, args.protocol)
    parser.error("provide --run-id, or --worker and --directory")


if __name__ == "__main__":
    raise SystemExit(main())
