"""Fixed provider-free, paired lexical retrieval experiment, not a legal audit.

Uses the registered 120-case legacy regression set and the existing local article
index. The new phrasing/negative controls are synthetic, not a legal holdout.
No dotenv, provider factory, generation, Judge, embedding, reranker or followup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from legal_rag.chat import LegalChatAssistant  # noqa: E402
from legal_rag.chunking import load_chunks  # noqa: E402
from legal_rag.config import load_config  # noqa: E402
from legal_rag.evaluation import bootstrap_ci, percentile  # noqa: E402
from legal_rag.evaluation_artifacts import eval_case_to_artifact  # noqa: E402
from legal_rag.evaluation_scoring import (  # noqa: E402
    CompletedCaseOutcome, ModelUsageDelta, result_matches, score_completed_case,
)
from legal_rag.evidence import check_evidence_sufficiency  # noqa: E402
from legal_rag.experiment_datasets import load_dataset_registry  # noqa: E402
from legal_rag.experiment_lifecycle import (  # noqa: E402
    execution_environment, repository_code_identity, stage_implementation_fingerprints,
)
from legal_rag.experiment_runtime import canonical_hash  # noqa: E402
from legal_rag.llm import CompletionUsage  # noqa: E402
from legal_rag.manifest import utc_now  # noqa: E402
from legal_rag.models import EvalCase  # noqa: E402
from legal_rag.retrieval import (  # noqa: E402
    BM25Retriever, bm25_text_versions, lexical_expansion_terms,
)
from legal_rag.tracing import JsonlTraceWriter  # noqa: E402

PROFILES = ("legacy-v1", "local-lexical-v2")
TOP_K = 5
INDEX = REPOSITORY_ROOT / "artifacts/indexes/article/chunks.jsonl"
OUTPUT_ROOT = REPOSITORY_ROOT / "artifacts/experiments"
METRICS = ("hit_at_3", "hit_at_5", "mrr", "target_coverage")
CRITICAL_FILES = (
    "scripts/offline_retrieval_ab.py", "tests/test_offline_retrieval_ab.py",
    "tests/test_bm25_lexical_profile.py",
    "tests/test_lexical_evidence_safety.py",
    "legal_rag/retrieval.py", "legal_rag/chat.py", "legal_rag/evidence.py",
    "legal_rag/query.py", "legal_rag/evaluation_scoring.py",
    "legal_rag/evaluation_artifacts.py", "legal_rag/experiment_lifecycle.py",
    "legal_rag/config.py", "configs/default.yaml", "eval_cases/registry.json",
)


class ABError(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class NoCallsClient:
    propagate_control_errors = True

    def __init__(self):
        self.usage = CompletionUsage()

    def complete(self, prompt: str) -> str:
        del prompt
        self.usage.calls += 1
        self.usage.failed_calls += 1
        raise ABError("provider_call_forbidden")


def write_json(path: Path, payload: Any) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create_run_directory(root: Path, run_id: str) -> Path:
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}", run_id):
        raise ABError("invalid_run_id")
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / run_id
    try:
        target.mkdir()
    except FileExistsError:
        raise ABError("run_already_exists") from None
    return target


def challenge_cases() -> list[EvalCase]:
    """New synthetic phrasing with inherited, non-authoritatively-reviewed gold.

    No case text/ID/gold is passed to the lexical expansion implementation.
    The eight no-gold controls measure alias activation, not legal accuracy.
    """
    definitions = [
        ("online-fit", "网上买的衣服尺寸不合适，能退吗？", "中华人民共和国消费者权益保护法", "第二十五条"),
        ("online-quality", "在电商订购手机收到坏的想退货", "中华人民共和国消费者权益保护法", "第二十四条"),
        ("offline-quality", "在实体店购买家电存在质量问题要求退货", "中华人民共和国消费者权益保护法", "第二十四条"),
        ("overtime", "休息日被安排加班而不给补休，应怎样计算报酬？", "中华人民共和国劳动法", "第四十四条"),
        ("employment", "入职几个月仍未订立书面劳动合同，双倍工资规定是什么？", "中华人民共和国劳动合同法", "第八十二条"),
        ("privacy", "自然人的私人生活安宁与不愿公开的私密信息受什么保护？", "中华人民共和国民法典", "第一千零三十二条"),
        ("food", "经营者销售不安全食品时，消费者能否索要十倍价款？", "中华人民共和国食品安全法", "第一百四十八条"),
        ("trademark", "同类商品擅自使用近似注册商标的侵权规定？", "中华人民共和国商标法", "第五十七条"),
        ("deposit", "租房押金不让退", "", ""),
        ("tax", "在网上申请退税", "", ""),
        ("exit", "退出线上系统", "", ""),
        ("bribe", "花钱买通关系", "", ""),
        ("ticket", "线下订票要求退票", "", ""),
        ("rights", "网上买断版权后退出合作", "", ""),
        ("retirement", "退休之后如何退税", "", ""),
        ("school", "退学之后能否取回押金", "", ""),
    ]
    return [EvalCase(case_id=f"lexical-challenge-{name}", question=question,
                     case_type="synthetic_paraphrase" if article else "synthetic_negative",
                     expected_law=law, expected_articles=[article] if article else [],
                     keywords=[], expected_behavior="evidence_answer")
            for name, question, law, article in definitions]


def evaluate_arm(case, retriever, *, run_id: str) -> dict[str, Any]:
    client = NoCallsClient()
    assistant = LegalChatAssistant(
        retriever, model="retrieval-only", top_k=TOP_K,
        adaptive_enabled=False, adaptive_use_llm=False, condense_with_llm=False,
        completion_client=client,
        evidence_rules_version="general-reference-v2",
    )
    started = time.perf_counter()
    try:
        prepared = assistant.prepare_question(case.question)
        retrieved = assistant.retrieve_turn(prepared, max_followup_rounds=0)
        results = list(retrieved.results)
        evidence = retrieved.evidence_check or check_evidence_sufficiency(
            case.question, results, analysis=prepared.analysis, rules_version="general-reference-v2",
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        if client.usage.calls:
            raise ABError("provider_call_forbidden")
        evaluated = score_completed_case(CompletedCaseOutcome(
            case=case, model="retrieval-only", retriever="bm25",
            chunk_strategy="article", top_k=TOP_K, generate=False,
            results=tuple(results), answer="", analysis=prepared.analysis,
            adaptive_trace={"enabled": False, "used": False, "max_followup_rounds": 0},
            evidence_check=evidence, verification=None, structured_answer=None,
            pre_fallback_answer=None, pre_fallback_verification=None,
            generation_kind="retrieval_only", generation_error=None,
            judge_configured=False, judge_result=None, error="",
            latency_ms=int(elapsed_ms), assistant_usage=ModelUsageDelta(),
            normalizer_usage=ModelUsageDelta(), judge_usage=ModelUsageDelta(),
            trace_metadata={"run_id": run_id, "lexical_profile": retriever.lexical_profile,
                            "provider_policy": "forbidden"},
        ))
        trace = {**evaluated.trace_record,
                 "metrics": evaluated.record.canonical_metrics,
                 "metrics_schema_version": evaluated.record.metrics_schema_version}
        metrics = {name: evaluated.record.canonical_metrics[name]["value"] for name in METRICS}
        return {
            "status": "succeeded", "error_code": None, "metrics": metrics,
            "top5_chunk_ids": [r.chunk.chunk_id for r in results],
            "ranked_results_sha256": canonical_hash([
                {"chunk_id": r.chunk.chunk_id, "score": r.score, "rank": r.rank}
                for r in results
            ]),
            "first_hit_rank": next((r.rank for r in results if result_matches(r, case)), None),
            "evidence_sufficient": evidence.sufficient,
            "failure_label": evaluated.record.failure_label,
            "terminal_kind": retrieved.terminal_kind,
            "refusal_route_pass": (
                retrieved.terminal_kind == "pre_retrieval_refusal"
                if case.expected_behavior == "out_of_scope" else None
            ),
            "provider_calls": 0, "latency_ms_samples": [elapsed_ms], "trace": trace,
        }
    except Exception:
        return {
            "status": "failed", "error_code": "retrieval_execution_failed",
            "metrics": {name: None for name in METRICS}, "top5_chunk_ids": [],
            "ranked_results_sha256": None,
            "first_hit_rank": None, "evidence_sufficient": None, "failure_label": None,
            "terminal_kind": None, "refusal_route_pass": None,
            "provider_calls": client.usage.calls,
            "latency_ms_samples": [], "trace": None,
        }


def run_pairs(cases, chunks, *, run_id: str, retriever_factory=BM25Retriever,
              writer=None, build_metrics=None) -> list[dict[str, Any]]:
    config = load_config()["retrieval"]
    retrievers = {}
    for profile in PROFILES:
        started = time.perf_counter()
        retrievers[profile] = retriever_factory(
            chunks, k1=float(config["bm25_k1"]), b=float(config["bm25_b"]),
            law_boost=float(config["bm25_law_boost"]),
            article_boost=float(config["bm25_article_boost"]),
            deprecated_penalty=float(config["deprecated_penalty"]), lexical_profile=profile,
        )
        if build_metrics is not None:
            build_metrics[profile] = {"build_ms": (time.perf_counter() - started) * 1000}
    rows = []
    for case in cases:
        orders = [list(PROFILES), list(reversed(PROFILES))]
        attempts = {profile: [] for profile in PROFILES}
        for order_index, order in enumerate(orders):
            for profile in order:
                arm = evaluate_arm(case, retrievers[profile], run_id=run_id)
                attempts[profile].append(arm)
                if writer is not None:
                    writer.write({"case_id": case.case_id, "profile": profile,
                                  "order_index": order_index, **arm})
        arms = {}
        consistent = True
        for profile, pair in attempts.items():
            first, repeat = pair
            same = all(first[key] == repeat[key] for key in (
                "status", "metrics", "top5_chunk_ids", "ranked_results_sha256", "first_hit_rank",
                "evidence_sufficient", "terminal_kind", "refusal_route_pass",
            ))
            consistent = consistent and same
            arms[profile] = {**first, "repeat_status": repeat["status"],
                             "provider_calls": sum(x["provider_calls"] for x in pair),
                             "latency_ms_samples": [v for x in pair for v in x["latency_ms_samples"]]}
        rows.append({
            "case_id": case.case_id, "case_type": case.case_type,
            "has_retrieval_gold": bool(case.expected_law or case.expected_articles),
            "expected_behavior": case.expected_behavior, "orders": orders,
            "repeat_consistent": consistent, "arms": arms,
            "alias_terms": lexical_expansion_terms(case.question),
        })
    return rows


def _mean(values):
    return sum(values) / len(values) if values else None


def summarize_pairs(rows) -> dict[str, Any]:
    gold = [r for r in rows if r["has_retrieval_gold"]]
    valid = [r for r in gold if r["repeat_consistent"] and all(
        a["status"] == "succeeded" and a["repeat_status"] == "succeeded"
        for a in r["arms"].values())]
    failed = [r for r in rows if not r["repeat_consistent"] or any(
        a["status"] != "succeeded" or a["repeat_status"] != "succeeded"
        for a in r["arms"].values())]
    transitions = Counter({"improved": 0, "regressed": 0, "unchanged_hit": 0,
                           "unchanged_miss": 0, "unknown": len(gold) - len(valid)})
    for row in valid:
        old, new = (row["arms"][p]["metrics"]["hit_at_5"] for p in PROFILES)
        transitions["improved" if new > old else "regressed" if new < old
                    else "unchanged_hit" if old else "unchanged_miss"] += 1
    arms = {}
    for profile in PROFILES:
        quality = [r["arms"][profile] for r in valid]
        successful = [r["arms"][profile] for r in rows
                      if r["arms"][profile]["status"] == "succeeded"
                      and r["arms"][profile]["repeat_status"] == "succeeded"]
        latencies = [v for arm in successful for v in arm["latency_ms_samples"]]
        hit_ranks = [a["first_hit_rank"] for a in quality if a["first_hit_rank"] is not None]
        refusal = [r["arms"][profile] for r in rows if r["expected_behavior"] == "out_of_scope"]
        arms[profile] = {
            **{name: _mean([a["metrics"][name] for a in quality
                           if a["metrics"][name] is not None]) for name in METRICS},
            "mean_first_hit_rank": _mean(hit_ranks), "first_hit_rank_denominator": len(hit_ranks),
            "evidence_gate_pass_rate": _mean([int(a["evidence_sufficient"]) for a in quality]),
            "heuristic_sufficient_without_gold_hit_count": sum(
                a["evidence_sufficient"] and not a["metrics"]["hit_at_5"] for a in quality),
            "median_latency_ms": statistics.median(latencies) if latencies else None,
            "p95_latency_ms": percentile(latencies, .95) if latencies else None,
            "latency_sample_count": len(latencies),
            "refusal_route_pass_count": sum(a["refusal_route_pass"] is True for a in refusal),
            "refusal_route_unknown_count": sum(a["refusal_route_pass"] is None for a in refusal),
            "failure_labels": dict(Counter(a["failure_label"] for a in quality if a["failure_label"])),
        }
    deltas = {}
    for name in METRICS:
        values = [r["arms"][PROFILES[1]]["metrics"][name] - r["arms"][PROFILES[0]]["metrics"][name]
                  for r in valid if all(r["arms"][p]["metrics"][name] is not None for p in PROFILES)]
        deltas[name] = {"mean": _mean(values), "denominator": len(values),
                        "ci95": list(bootstrap_ci(values, seed=42)) if values else None}
    route_failures = [r for r in rows if r["expected_behavior"] == "out_of_scope"
                      and any(a["refusal_route_pass"] is not True for a in r["arms"].values())]
    return {
        "status": "failed" if failed or route_failures else "completed",
        "case_count": len(rows), "retrieval_gold_count": len(gold),
        "refusal_count": sum(r["expected_behavior"] == "out_of_scope" for r in rows),
        "paired_valid_count": len(valid), "execution_failed_count": len(failed),
        "repeat_inconsistent_count": sum(not r["repeat_consistent"] for r in rows),
        "provider_calls": sum(a["provider_calls"] for r in rows for a in r["arms"].values()),
        "transitions": dict(transitions), "arms": arms, "paired_deltas": deltas,
    }


def input_identity(chunks) -> dict[str, Any]:
    source_root = (REPOSITORY_ROOT / "Chinese-Laws/Chinese-Laws").resolve()
    evidence = []
    for raw in sorted({source for chunk in chunks for source in chunk.source_files}):
        path = Path(raw).resolve(strict=True)
        if not path.is_relative_to(source_root) or path.is_symlink() or path.suffix != ".txt":
            raise ABError("unsupported_index_source")
        data = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
        evidence.append({"path": path.relative_to(source_root).as_posix(), "size": len(data),
                         "sha256": hashlib.sha256(data).hexdigest()})
    if not chunks or any(c.strategy != "article" for c in chunks):
        raise ABError("article_index_required")
    if len({c.chunk_id for c in chunks}) != len(chunks):
        raise ABError("duplicate_chunk_id")
    return {"index_sha256": sha256(INDEX), "chunk_count": len(chunks),
            "source_file_count": len(evidence),
            "source_collection_sha256": canonical_hash(evidence),
            "source_files": evidence,
            "source_license_boundary": "existing public Chinese-Laws local snapshot; no legal currency claim"}


def critical_identity() -> dict[str, str]:
    return {name: sha256(REPOSITORY_ROOT / name) for name in CRITICAL_FILES}


def run_ab(run_id: str) -> dict[str, Any]:
    started_at = utc_now()
    started = time.perf_counter()
    registry = load_dataset_registry(REPOSITORY_ROOT / "eval_cases/registry.json",
                                     repository_root=REPOSITORY_ROOT)
    dataset = registry.dataset("legal-eval-v3")
    generation_subset = registry.dataset("legal-eval-v3-generation-30")
    cases = list(dataset.cases)
    if len(cases) != 120 or sum(bool(c.expected_law or c.expected_articles) for c in cases) != 108:
        raise ABError("registered_case_denominator_invalid")
    chunks = load_chunks(INDEX)
    identity = input_identity(chunks)
    code = repository_code_identity(REPOSITORY_ROOT)
    critical = critical_identity()
    challenge = challenge_cases()
    directory = create_run_directory(OUTPUT_ROOT, run_id)
    manifest = {
        "schema_version": 1, "run_id": run_id, "created_at": utc_now(),
        "purpose": "paired provider-free lexical retrieval, not generation or legal authority validation",
        "code": code, "critical_file_sha256": critical,
        "stage_implementation_fingerprints": stage_implementation_fingerprints(REPOSITORY_ROOT),
        "dataset": dataset.manifest_payload(), "corpus": identity,
        "generation_subset_cohort": {
            "dataset_id": generation_subset.entry.dataset_id,
            "case_file_sha256": generation_subset.entry.file_sha256,
            "case_set_sha256": generation_subset.entry.manifest_case_set_sha256,
            "case_count": 30, "role": "overlapping legacy cohort, not independent validation",
            "generate": False,
        },
        "challenge": {"role": "synthetic_challenge", "is_holdout": False,
                      "legal_authority_status": "not_authoritatively_reviewed",
                      "case_count": len(challenge),
                      "case_set_sha256": canonical_hash([eval_case_to_artifact(c) for c in challenge]),
                      "cases": [eval_case_to_artifact(c) for c in challenge]},
        "parameters": {"profiles": list(PROFILES), "top_k": TOP_K,
                       "bm25": load_config()["retrieval"],
                       "text_versions": {p: list(bm25_text_versions(p)) for p in PROFILES},
                       "orders": [list(PROFILES), list(reversed(PROFILES))],
                       "quality_sample": "first execution, paired complete cases only",
                       "latency_scope": "two actual preparation+bare-retrieval+evidence executions per arm/case, excludes build/scoring",
                       "adaptive": False, "followup_rounds": 0, "generation": False,
                       "embedding": False, "reranker": False, "judge": False,
                       "bootstrap": {"seed": 42, "resamples": 2000, "confidence": .95}},
        "environment": execution_environment(), "network_policy": "forbidden",
        "provider_policy": "forbidden", "dotenv_loaded": False,
        "privacy": "raw questions/text/traces only in ignored local run directory; numeric/id summary only",
    }
    write_json(directory / "manifest.json", manifest)
    writer = JsonlTraceWriter(directory / "traces.jsonl", run_id=run_id)
    build = {}
    rows = run_pairs(cases + challenge, chunks, run_id=run_id, writer=writer, build_metrics=build)
    main_rows, challenge_rows = rows[:len(cases)], rows[len(cases):]
    summary = {"schema_version": 1, "run_id": run_id,
               "manifest_sha256": sha256(directory / "manifest.json"),
               "legacy_regression": summarize_pairs(main_rows),
               "synthetic_challenge": summarize_pairs(challenge_rows),
               "generation_subset_cohort": summarize_pairs([
                   r for r in main_rows if r["case_id"] in
                   {c.case_id for c in generation_subset.cases}
               ]),
               "categories": {category: summarize_pairs([r for r in main_rows if r["case_type"] == category])
                              for category in sorted({r["case_type"] for r in main_rows})},
               "build": build, "actual_provider_calls": sum(
                   a["provider_calls"] for row in rows for a in row["arms"].values()),
               "generation_quality": {"value": None, "unavailable_reason": "retrieval_only"},
               "estimated_model_cost_cny": "0", "invoice_verified": False,
               "limits": ["legacy reused development cases, not holdout", "synthetic challenge gold not legally reviewed",
                          "evidence gate is heuristic, not semantic/legal support",
                          "local-only lexical quality and latency; not OS-level airgap"]}
    summary["case_transitions"] = [
        {"case_id": r["case_id"], "case_type": r["case_type"],
         "repeat_consistent": r["repeat_consistent"],
         "arms": {p: {k: a[k] for k in ("status", "metrics", "top5_chunk_ids", "first_hit_rank",
                                        "evidence_sufficient", "refusal_route_pass", "error_code")}
                  for p, a in r["arms"].items()}} for r in main_rows]
    summary["negative_alias_controls"] = {
        "count": 8, "unexpected_activation_case_ids": [r["case_id"] for r in challenge_rows
                  if r["case_type"] == "synthetic_negative" and r["alias_terms"]]}
    summary["input_stable"] = input_identity(chunks) == identity
    summary["dataset_stable"] = load_dataset_registry(
        REPOSITORY_ROOT / "eval_cases/registry.json", repository_root=REPOSITORY_ROOT,
    ).dataset("legal-eval-v3").manifest_payload() == dataset.manifest_payload()
    summary["code_stable"] = critical_identity() == critical and repository_code_identity(REPOSITORY_ROOT) == code
    summary["status"] = (
        "completed" if summary["legacy_regression"]["status"] == "completed"
        and summary["synthetic_challenge"]["status"] == "completed"
        and summary["input_stable"] and summary["dataset_stable"] and summary["code_stable"]
        and not summary["negative_alias_controls"]["unexpected_activation_case_ids"]
        and summary["actual_provider_calls"] == 0 else "failed"
    )
    summary["started_at"] = started_at
    summary["completed_at"] = utc_now()
    summary["elapsed_ms"] = (time.perf_counter() - started) * 1000
    write_json(directory / "summary.json", summary)
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True, help="fresh local immutable experiment ID")
    args = parser.parse_args(argv)
    try:
        summary = run_ab(args.run_id)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0 if summary["status"] == "completed" else 2
    except Exception as error:
        category = error.reason if isinstance(error, ABError) else "offline_experiment_failed"
        print(json.dumps({"status": "failed", "error_code": category,
                          "provider_policy": "forbidden", "actual_provider_calls": None,
                          "call_count_status": "unknown_if_not_persisted"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
