"""Frozen three-arm local ES/IK development comparison, without provider clients."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import benchmark_chinese_bm25 as common
from scripts.benchmark_smartcn import ProcessTreeSampler, safe_paired_summary, terminate_process_tree

PROTOCOL = ROOT / "configs/chinese-bm25-benchmark-v4.json"
FROZEN_PROTOCOL_CANONICAL_SHA256 = "b8f9f664bdaaef03a33b47e7773718d8aa9e0f036c831e418a6f59644351044f"
aggregate = common.aggregate


def validate_protocol(protocol):
    encoded = json.dumps(protocol, sort_keys=True, allow_nan=False, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    if hashlib.sha256(encoded).hexdigest() != FROZEN_PROTOCOL_CANONICAL_SHA256:
        raise ValueError("unfrozen_ik_protocol")


def source_identity(protocol_path=PROTOCOL):
    names = (*common.CRITICAL, "scripts/benchmark_elasticsearch_ik.py",
             "scripts/benchmark_smartcn.py", "scripts/isolated_elasticsearch.py",
             "scripts/elasticsearch_ik_adapter.py",
             str(Path(protocol_path).resolve().relative_to(ROOT)).replace("\\", "/"))
    return {name: common.digest(ROOT / name) for name in dict.fromkeys(names)}


def selection(protocol, results, executions, stable):
    """Recompute aggregates from complete aligned rows before comparing candidates."""
    try:
        validate_protocol(protocol)
        names = {arm["id"] for arm in protocol["arms"]}
        if not stable or set(results) != names or set(executions) != names:
            return False, None, None
        aligned = None
        gold_mask = None
        for name in names:
            value = results[name]
            if (value.get("arm_id") != name or executions[name]["exit_code"] != 0 or value.get("worker_failure")
                    or value.get("service_cleanup_succeeded") is not True
                    or value.get("runtime_identity_stable") is not True
                    or value.get("inputs_and_implementation_stable") is not True
                    or value.get("repeat_mismatch_case_ids") or len(value["passes"]) != 2):
                return False, None, None
            first_ranking = None
            for index, part_name in enumerate(("first_pass", "second_pass")):
                rows = value["passes"][index]
                ids = {row["case_id"] for row in rows}
                if len(rows) != 120 or len(ids) != 120 or (aligned is not None and ids != aligned):
                    return False, None, None
                aligned = ids
                if any(row["status"] != "succeeded" for row in rows):
                    return False, None, None
                mask = {row["case_id"] for row in rows if row["metrics"] is not None}
                if len(mask) != 108 or (gold_mask is not None and mask != gold_mask):
                    return False, None, None
                gold_mask = mask
                measured = aggregate(rows)
                if measured != value[part_name]:
                    return False, None, None
                numeric = [*measured["metrics"].values(), measured["query_p95_ms"]]
                if any(isinstance(item, bool) or not isinstance(item, (int, float))
                       or not math.isfinite(item) or item < 0 for item in numeric):
                    return False, None, None
                if any(item > 1 for item in measured["metrics"].values()):
                    return False, None, None
                ranking = {row["case_id"]: row["ranked_results"] for row in rows}
                if first_ranking is not None and ranking != first_ranking:
                    return False, None, None
                first_ranking = ranking
        equivalent = deepcopy(protocol)
        equivalent["arms"][-1]["backend"] = "bm25s"
        return common.selection(equivalent, results, executions, stable)
    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
        return False, None, None


def free_port():
    # Reservation is released before the owned service starts; a race is a startup failure.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def worker_environment():
    environment = {name: value for name, value in os.environ.items()
                   if name.upper() in {"SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "PATH"}}
    environment.update(LEGAL_RAG_DISABLE_DOTENV="1", PYTHON_DOTENV_DISABLED="1",
                       ALLOW_LIVE_MODEL_CALLS="false", HF_HUB_OFFLINE="1",
                       TRANSFORMERS_OFFLINE="1", PYTHONUTF8="1")
    return environment


def run_worker(directory, arm, timeout):
    import psutil
    with (directory / (arm["id"] + ".log")).open("x", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()),
                                    "--worker", arm["id"], "--directory", str(directory)],
                                   cwd=ROOT, env=worker_environment(), stdout=log,
                                   stderr=subprocess.STDOUT, shell=False,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        sampler = None
        try:
            try:
                sampler = ProcessTreeSampler(psutil.Process(process.pid), interval=.2).start()
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
            # Popen retains ownership even if psutil/enumeration itself failed.
            # Reap that handle, but keep failure because descendants are unverified.
            try:
                process.kill()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass
            return "cleanup_failed"


def identities_stable(manifest):
    from scripts.isolated_elasticsearch import verify_home
    return (source_identity(ROOT / manifest["protocol_path"]) == manifest["critical_file_sha256"]
            and common.digest(ROOT / manifest["protocol"]["index"]) == manifest["index_sha256"]
            and common.load_inputs(manifest["protocol"])[2].manifest_payload() == manifest["dataset"]
            and verify_home(Path(manifest["engine_home"])) == manifest["runtime_identity"])


def original_package_identity(engine_home):
    from scripts.isolated_elasticsearch import verify_dependencies
    home = Path(engine_home)
    return verify_dependencies(home, {
        "elasticsearch": home.parent / "elasticsearch-9.1.4-windows-x86_64.zip",
        "analysis-ik": home.parent / "analysis-ik-9.1.4.zip",
    })


def execute_worker(directory, arm_id):
    import psutil
    from scripts.elasticsearch_ik_adapter import ElasticsearchIKRetriever, install_loopback_socket_audit
    from scripts.isolated_elasticsearch import IsolatedElasticsearch, verify_home
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    protocol = manifest["protocol"]
    validate_protocol(protocol)
    arm = next(arm for arm in protocol["arms"] if arm["id"] == arm_id)
    endpoint = "http://127.0.0.1:" + str(manifest["http_port"])
    if arm["backend"] == "elasticsearch-ik":
        install_loopback_socket_audit(endpoint)
    else:
        common.disable_network()
    sampler = ProcessTreeSampler(psutil.Process()).start()
    retriever = service = None
    passes, failure, build_ms, timings, diagnostics, service_identity = [], None, None, {}, {}, None
    cleanup_succeeded = True
    try:
        if not identities_stable(manifest):
            raise ValueError("identity_changed_before_worker")
        chunks, cases, _ = common.load_inputs(protocol)
        started = time.perf_counter()
        if arm["backend"] == "elasticsearch-ik":
            service = IsolatedElasticsearch(Path(manifest["engine_home"]), directory / "es-runtime",
                                           manifest["http_port"], manifest["transport_port"])
            service.start()
            service_identity = service.identity
            timings["service_startup_ms"] = (time.perf_counter() - started) * 1000
            indexed = time.perf_counter()
            retriever = ElasticsearchIKRetriever(chunks, endpoint, "ik-fixed-v4", k1=arm["k1"], b=arm["b"])
            timings["index_build_refresh_freeze_ms"] = (time.perf_counter() - indexed) * 1000
        else:
            retriever = common.build_arm(arm, chunks)
        build_ms = (time.perf_counter() - started) * 1000
        for repetition in range(2):
            rows = []
            for case in (cases if repetition == 0 else list(reversed(cases))):
                started = time.perf_counter()
                base = {"case_id": case.case_id, "case_type": case.case_type,
                        "stratum": "explicit" if re.search(r"《[^》]+》.*第[^条]{1,30}条", case.question) else "scenario"}
                try:
                    hits = retriever.retrieve(case.question, top_k=5)
                    elapsed = (time.perf_counter() - started) * 1000
                    rows.append({**base, "status": "succeeded", "error_type": None, "latency_ms": elapsed,
                                 "metrics": common.score_case(case, hits),
                                 "ranked_results": [{"chunk_id": hit.chunk.chunk_id, "score": hit.score,
                                                     "rank": hit.rank} for hit in hits],
                                 "query_tokens": getattr(retriever, "last_query_tokens", None)})
                except Exception as exc:
                    rows.append({**base, "status": "failed", "error_type": type(exc).__name__,
                                 "latency_ms": (time.perf_counter() - started) * 1000,
                                 "metrics": None, "ranked_results": None})
            passes.append(rows)
        if hasattr(retriever, "diagnose_query"):
            diagnostic_started = time.perf_counter()
            items = [{"case_id": case.case_id, **retriever.diagnose_query(case.question)} for case in cases]
            diagnostics = {"first_pass_query_diagnostics": items,
                           "scope": "extra read-only requests after timed passes; not included in query latency"}
            timings["post_query_diagnostics_ms"] = (time.perf_counter() - diagnostic_started) * 1000
    except Exception as exc:
        failure = {"error_type": type(exc).__name__, "detail": str(exc)[:400]}
    finally:
        if retriever is not None and hasattr(retriever, "close"):
            try:
                retriever.close()
            except Exception as exc:
                failure = {"error_type": "index_verification_" + type(exc).__name__, "detail": str(exc)[:400]}
        if service is not None:
            try:
                service.close()
            except Exception as exc:
                cleanup_succeeded = False
                failure = {"error_type": "service_cleanup_" + type(exc).__name__, "detail": str(exc)[:400]}
            finally:
                service_identity = service.identity
        memory = sampler.stop()
    try:
        stable = identities_stable(manifest)
        runtime_stable = verify_home(Path(manifest["engine_home"])) == manifest["runtime_identity"]
    except Exception:
        stable = runtime_stable = False
    passes += [[]] * (2 - len(passes))
    first = {row["case_id"]: row for row in passes[0]}
    repeats = [row["case_id"] for row in passes[1] if row["status"] != "succeeded"
               or first.get(row["case_id"], {}).get("status") != "succeeded"
               or row["ranked_results"] != first.get(row["case_id"], {}).get("ranked_results")]
    result = {"arm_id": arm_id, "worker_failure": failure, "build_ms": build_ms,
              "config_identity": getattr(retriever, "config_identity", arm),
              "index_stats": getattr(retriever, "index_stats", None), "timings": timings,
              "adapter_timings": getattr(retriever, "timings", {}),
              "service_identity": service_identity, "service_cleanup_succeeded": cleanup_succeeded,
              "diagnostics": diagnostics, **memory,
              "first_pass": aggregate(passes[0]), "second_pass": aggregate(passes[1]),
              "strata": {name: aggregate([row for row in passes[0] if row["stratum"] == name])
                         for name in ("explicit", "scenario")},
              "repeat_mismatch_case_ids": repeats, "inputs_and_implementation_stable": stable,
              "runtime_identity_stable": runtime_stable, "passes": passes, "actual_provider_calls": 0}
    common.write_new(directory / (arm_id + ".json"), result)
    return 0 if (not failure and stable and cleanup_succeeded and not repeats
                 and all(len(rows) == 120 and all(row["status"] == "succeeded" for row in rows)
                         for rows in passes)) else 1


def run(run_id, engine_home, protocol_path=PROTOCOL):
    from legal_rag.manifest import utc_now
    from scripts.offline_retrieval_ab import create_run_directory, input_identity
    from scripts.isolated_elasticsearch import verify_home
    protocol_path, engine_home = Path(protocol_path).resolve(), Path(engine_home).resolve()
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    validate_protocol(protocol)
    chunks, _, dataset = common.load_inputs(protocol)
    packages = original_package_identity(engine_home)
    runtime = verify_home(engine_home)
    directory = create_run_directory(ROOT / "artifacts/experiments", run_id)
    http_port, transport_port = free_port(), free_port()
    while transport_port == http_port:
        transport_port = free_port()
    manifest = {"run_id": run_id, "created_at": utc_now(), "protocol": protocol,
                "protocol_path": str(protocol_path.relative_to(ROOT)).replace("\\", "/"),
                "protocol_sha256": common.digest(protocol_path), "critical_file_sha256": source_identity(protocol_path),
                "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "index_sha256": common.digest(ROOT / protocol["index"]), "corpus": input_identity(chunks),
                "dataset": dataset.manifest_payload(), "runtime_identity": runtime,
                "original_package_identity": packages,
                "engine_home": str(engine_home), "http_port": http_port, "transport_port": transport_port,
                "python": sys.version.split()[0], "actual_calls_policy": protocol["network_boundary"]}
    common.write_new(directory / "manifest.json", manifest)
    results, executions = {}, {}
    for arm in protocol["arms"]:
        started = time.perf_counter()
        try:
            code = run_worker(directory, arm, protocol["worker_timeout_seconds"])
        except Exception:
            code = "start_failed"
        executions[arm["id"]] = {"exit_code": code, "elapsed_ms": (time.perf_counter()-started)*1000}
        path = directory / (arm["id"] + ".json")
        if path.is_file():
            try:
                results[arm["id"]] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                executions[arm["id"]]["result_error"] = type(exc).__name__
        print(json.dumps({"arm": arm["id"], **executions[arm["id"]]}), flush=True)
    try:
        stable = identities_stable(manifest)
    except Exception:
        stable = False
    complete, selected, promotable = selection(protocol, results, executions, stable)
    summary = {"run_id": run_id, "status": "completed" if complete else "failed",
               "manifest_sha256": common.digest(directory / "manifest.json"),
               "protocol_sha256": manifest["protocol_sha256"], "critical_identity_stable": stable,
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
    parser.add_argument("--engine-home", type=Path,
                        default=ROOT / ".tmp/ik-deps/9.1.4/elasticsearch-9.1.4")
    parser.add_argument("--worker")
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    os.environ.update(worker_environment())
    if args.worker:
        if args.directory is None or args.directory.resolve().parent != (ROOT / "artifacts/experiments").resolve():
            parser.error("worker directory must be one immediate experiment directory")
        return execute_worker(args.directory.resolve(), args.worker)
    if args.run_id:
        common.disable_network()
        return run(args.run_id, args.engine_home, args.protocol)
    parser.error("provide --run-id, or --worker and --directory")


if __name__ == "__main__":
    raise SystemExit(main())
