"""Provider-free component ablation on reused regressions, not legal holdout.

Eight token variants are orthogonal. The separate flat-union/typed ownership
audit is not a reconstruction of any complete historical evidence checker.
Inference only receives questions; legacy gold is used afterwards by scorer.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import hashlib
import itertools
import json
import math
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from legal_rag.chat import LegalChatAssistant, should_refuse_before_retrieval  # noqa: E402
from legal_rag.chunking import load_chunks  # noqa: E402
from legal_rag.config import load_config  # noqa: E402
from legal_rag.evaluation import bootstrap_ci, percentile  # noqa: E402
from legal_rag.evaluation_scoring import (  # noqa: E402
    CompletedCaseOutcome, ModelUsageDelta, score_completed_case,
)
from legal_rag.evidence import check_evidence_sufficiency  # noqa: E402
from legal_rag.experiment_datasets import load_dataset_registry  # noqa: E402
from legal_rag.experiment_lifecycle import repository_code_identity  # noqa: E402
from legal_rag.experiment_runtime import canonical_hash  # noqa: E402
from legal_rag.legal_references import (  # noqa: E402
    canonical_article_number, canonical_law_title, parse_legal_references,
)
from legal_rag.manifest import utc_now  # noqa: E402
from legal_rag.reference_evidence import check_reference_evidence  # noqa: E402
from legal_rag.retrieval import BM25Retriever, lexical_expansion_terms, tokenize  # noqa: E402
from legal_rag.tracing import JsonlTraceWriter  # noqa: E402
from scripts.offline_retrieval_ab import NoCallsClient, input_identity, write_json  # noqa: E402


METRICS = ("hit_at_3", "hit_at_5", "mrr", "target_coverage")
CRITICAL_FILES = (
    "scripts/offline_component_ablation.py", "tests/test_offline_component_ablation.py",
    "legal_rag/legal_references.py", "legal_rag/reference_evidence.py", "legal_rag/retrieval.py",
    "legal_rag/chat.py", "legal_rag/evidence.py", "legal_rag/query.py", "legal_rag/models.py",
    "legal_rag/adaptive.py", "legal_rag/planning.py", "legal_rag/query_understanding.py",
    "legal_rag/retrieval_contracts.py", "legal_rag/chat_artifacts.py", "legal_rag/semantic.py",
    "legal_rag/chunking.py", "legal_rag/tracing.py", "legal_rag/evaluation.py",
    "legal_rag/experiment_datasets.py", "legal_rag/experiment_runtime.py", "legal_rag/experiment_lifecycle.py",
    "legal_rag/evaluation_scoring.py", "legal_rag/evaluation_artifacts.py", "legal_rag/config.py",
    "configs/default.yaml", "eval_cases/registry.json", "scripts/offline_retrieval_ab.py",
)


@dataclass(frozen=True, slots=True)
class Components:
    contiguous_tokens: bool
    deduplicate_query: bool
    historical_scene_expansion: bool

    def __post_init__(self):
        if any(type(value) is not bool for value in (
            self.contiguous_tokens, self.deduplicate_query, self.historical_scene_expansion)):
            raise ValueError("component switches must be booleans")

    @property
    def variant_id(self) -> str:
        return f"c{int(self.contiguous_tokens)}d{int(self.deduplicate_query)}s{int(self.historical_scene_expansion)}"

    def to_dict(self) -> dict[str, Any]:
        return {"variant_id": self.variant_id, "contiguous_tokens": self.contiguous_tokens,
                "deduplicate_query": self.deduplicate_query,
                "historical_scene_expansion": self.historical_scene_expansion,
                "role": "diagnostic_only_not_a_production_profile"}


def component_matrix() -> tuple[Components, ...]:
    return tuple(Components(*bits) for bits in itertools.product((False, True), repeat=3))


class ComponentRetriever(BM25Retriever):
    """Diagnostic-only query features share two read-only document statistics.

This subclass is never registered as an API/CLI production strategy selector.
The production generic-v3 branch itself never executes scene expansion.
"""

    def __init__(self, components: Components, statistics_source: BM25Retriever):
        wanted = "generic-v3" if components.contiguous_tokens else "legacy-v1"
        if statistics_source.lexical_profile not in ({"generic-v3", "local-lexical-v2"}
                                                     if components.contiguous_tokens else {"legacy-v1"}):
            raise ValueError("document statistics do not match the boundary component")
        self.__dict__.update(statistics_source.__dict__)
        self.lexical_profile = wanted
        self.components = components
        self.query_text_version = "component-query-" + components.variant_id
        self.document_text_version = "component-document-contiguous" if components.contiguous_tokens else "component-document-flat"

    def _query_features(self, query):
        profile = "generic-v3" if self.components.contiguous_tokens else "legacy-v1"
        terms = tokenize(query, profile=profile)
        expansion = lexical_expansion_terms(query) if self.components.historical_scene_expansion else []
        terms.extend(expansion)
        if self.components.deduplicate_query:
            terms = list(dict.fromkeys(terms))
        return terms, expansion

    def retrieve(self, query, top_k=5):
        return [replace(row, trace={**row.trace, "component_identity": self.components.to_dict()})
                for row in super().retrieve(query, top_k)]


def audit_reference_ownership(question, results, *, known_law_titles=()):
    known = tuple({*known_law_titles, *(law for result in results for law in result.chunk.law_names)})
    analysis = parse_legal_references(question, known_law_titles=known)
    typed = check_reference_evidence(analysis, results)
    laws: set[str] = set()
    articles: set[str] = set()
    for result in results:
        try:
            if result.provenance is not None:
                laws.update(canonical_law_title(entry.title) for entry in result.provenance.articles)
                articles.update(canonical_article_number(entry.article_number)
                                for entry in result.provenance.articles if entry.article_number)
            else:
                laws.update(canonical_law_title(law) for law in result.chunk.law_names)
                articles.update(canonical_article_number(article) for article in result.chunk.article_numbers if article)
        except (TypeError, ValueError):
            pass  # The typed mechanical check still records absent/invalid support.
    flat_missing = [item for item in analysis.requirements
                    if item.law_title not in laws or item.article_number not in articles]
    flat_reasons = [reason for reason in typed.reasons if not reason.startswith("missing_reference_pair:")]
    flat_reasons.extend("missing_union_reference" for _ in flat_missing)
    return {"analysis_fingerprint": analysis.fingerprint,
            "explicit_pair_count": len(analysis.requirements), "unresolved_count": len(analysis.unresolved),
            "flat_union_ownership": {
                "rules_version": "flat-union-ownership-audit-v1",
                "sufficient": typed.candidate_available and typed.scores_valid and not flat_reasons,
                "missing_pairs": [item.to_dict() for item in flat_missing],
                "note": "ownership-only counterfactual, not full historical evidence behavior"},
            "typed_reference_ownership": typed.to_dict(), "legal_support_status": "not_checked"}


def infer_question(question, retriever):
    """Inference boundary deliberately accepts no case or gold fields."""
    client = NoCallsClient()
    assistant = LegalChatAssistant(retriever, model="retrieval-only", top_k=5,
                                    adaptive_enabled=False, adaptive_use_llm=False,
                                    condense_with_llm=False, completion_client=client)
    prepared = assistant.prepare_question(question)
    retrieved = assistant.retrieve_turn(prepared, max_followup_rounds=0)
    if client.usage.calls:
        raise ValueError("provider dispatch forbidden")
    results = tuple(retrieved.results)
    return {"prepared": prepared, "retrieved": retrieved, "results": results,
            "audit": audit_reference_ownership(question, results,
                known_law_titles=getattr(retriever, "known_law_hints", ())), "actual_provider_calls": 0,
            "identity": canonical_hash({"question": question, "results": [
                {"chunk_id": row.chunk.chunk_id, "score": row.score, "rank": row.rank}
                for row in results], "terminal_kind": retrieved.terminal_kind})}


def evaluate_case(case, retriever):
    started = time.perf_counter()
    try:
        inferred = infer_question(case.question, retriever)
        prepared, retrieved, results = inferred["prepared"], inferred["retrieved"], inferred["results"]
        evidence, projection = retrieved.evidence_check, None
        if evidence is None:
            # The actual risk refusal performs no retrieval and has no evidence.
            # Project only this observed stage into the scorer's required value
            # object, without rewriting the stage or inventing legal support.
            if (
                retrieved.terminal_kind != "pre_retrieval_refusal"
                or results
                or not should_refuse_before_retrieval(prepared.analysis.risk_flags)
                or retrieved.terminal_answer is None
                or retrieved.terminal_answer.answer_mode != "out_of_scope"
            ):
                raise ValueError("missing evidence outside the actual risk refusal")
            evidence = check_evidence_sufficiency(
                prepared.standalone_question, [], analysis=prepared.analysis,
                rules_version="general-reference-v2",
                known_law_titles=getattr(retriever, "known_law_hints", ()),
            )
            if evidence.sufficient or evidence.checked_result_count:
                raise ValueError("risk refusal projection cannot claim retrieved support")
            projection = {"kind": "pre_retrieval_refusal_no_retrieval",
                          "reason": "strict_scorer_requires_evidence_value"}
        elapsed = (time.perf_counter() - started) * 1000
        evaluated = score_completed_case(CompletedCaseOutcome(
            case=case, model="retrieval-only", retriever="bm25", chunk_strategy="article",
            top_k=5, generate=False, results=results, answer="", analysis=prepared.analysis,
            adaptive_trace={"enabled": False, "used": False, "max_followup_rounds": 0},
            evidence_check=evidence, verification=None, structured_answer=None,
            pre_fallback_answer=None, pre_fallback_verification=None, generation_kind="retrieval_only",
            generation_error=None, judge_configured=False, judge_result=None, error="",
            latency_ms=int(elapsed), assistant_usage=ModelUsageDelta(), normalizer_usage=ModelUsageDelta(),
            judge_usage=ModelUsageDelta(), trace_metadata={
                "component_identity": retriever.components.to_dict(),
                "terminal_kind": retrieved.terminal_kind,
                "risk_flags": list(prepared.analysis.risk_flags),
                "evidence_projection": projection,
            },
        ))
        return {"status": "succeeded", "error_code": None,
                "canonical_metrics": {metric: evaluated.record.canonical_metrics[metric]["value"] for metric in METRICS},
                "metrics_schema_version": evaluated.record.metrics_schema_version,
                "inference_identity": inferred["identity"], "ownership_audit": inferred["audit"],
                "actual_provider_calls": inferred["actual_provider_calls"],
                "top5_chunk_ids": [row.chunk.chunk_id for row in results],
                "refusal_route_pass": (retrieved.terminal_kind == "pre_retrieval_refusal"
                                       if case.expected_behavior == "out_of_scope" else None),
                "latency_ms_samples": [elapsed], "trace": evaluated.trace_record}
    except Exception:
        return {"status": "failed", "error_code": "retrieval_execution_failed",
                "canonical_metrics": {metric: None for metric in METRICS},
                "metrics_schema_version": None, "inference_identity": None, "ownership_audit": None,
                "actual_provider_calls": None, "top5_chunk_ids": [], "refusal_route_pass": None,
                "latency_ms_samples": [], "trace": None}


def run_matrix(cases, chunks, *, writer=None):
    settings = load_config()["retrieval"]
    bases, build = {}, {}
    for contiguous, profile in ((False, "legacy-v1"), (True, "generic-v3")):
        started = time.perf_counter()
        bases[contiguous] = BM25Retriever(chunks, lexical_profile=profile,
            k1=float(settings["bm25_k1"]), b=float(settings["bm25_b"]),
            law_boost=float(settings["bm25_law_boost"]), article_boost=float(settings["bm25_article_boost"]),
            deprecated_penalty=float(settings["deprecated_penalty"]))
        build[profile] = {"build_ms": (time.perf_counter() - started) * 1000,
                          "shared_statistics": True}
    variants = component_matrix()
    retrievers = {item.variant_id: ComponentRetriever(item, bases[item.contiguous_tokens]) for item in variants}
    rows = []
    for case in cases:
        attempts = {item.variant_id: [] for item in variants}
        for order_index, order in enumerate((variants, tuple(reversed(variants)))):
            for item in order:
                evaluated = evaluate_case(case, retrievers[item.variant_id])
                attempts[item.variant_id].append(evaluated)
                if writer:
                    writer.write({"case_id": case.case_id, "order_index": order_index,
                                  "component_identity": item.to_dict(), **evaluated})
        arms = {}
        for variant_id, pair in attempts.items():
            first, second = pair
            consistent = all(first[key] == second[key] for key in (
                "status", "canonical_metrics", "inference_identity", "top5_chunk_ids", "ownership_audit", "refusal_route_pass"))
            calls = [value["actual_provider_calls"] for value in pair]
            arms[variant_id] = {**first, "repeat_status": second["status"],
                                "repeat_consistent": consistent,
                                "actual_provider_calls": sum(calls) if all(value is not None for value in calls) else None,
                                "latency_ms_samples": first["latency_ms_samples"] + second["latency_ms_samples"]}
        rows.append({"case_id": case.case_id, "case_type": case.case_type,
                     "has_retrieval_gold": bool(case.expected_law or case.expected_articles),
                     "is_refusal": case.expected_behavior == "out_of_scope", "arms": arms})
    return rows, build


def _mean(values):
    return sum(values) / len(values) if values else None


def _valid_quality_metrics(arm):
    values = arm["canonical_metrics"]
    return all(type(values.get(metric)) in (int, float) and math.isfinite(values[metric])
               and 0 <= values[metric] <= 1 for metric in METRICS)


def summarize_matrix(rows):
    gold = [row for row in rows if row["has_retrieval_gold"]]
    valid = [row for row in gold if all(arm["status"] == arm["repeat_status"] == "succeeded"
                and arm["repeat_consistent"] and _valid_quality_metrics(arm)
                for arm in row["arms"].values())]
    arms = {}
    for component in component_matrix():
        variant = component.variant_id
        quality = [row["arms"][variant] for row in valid]
        latencies = [value for row in rows for value in row["arms"][variant]["latency_ms_samples"]]
        arms[variant] = {**component.to_dict(), **{
            metric: _mean([arm["canonical_metrics"][metric] for arm in quality
                           if arm["canonical_metrics"][metric] is not None]) for metric in METRICS},
            "quality_denominator": len(valid),
            "median_latency_ms": statistics.median(latencies) if latencies else None,
            "p95_latency_ms": percentile(latencies, .95) if latencies else None,
            "latency_sample_count": len(latencies),
            "flat_sufficient_count": sum(arm["ownership_audit"]["flat_union_ownership"]["sufficient"] for arm in quality),
            "typed_sufficient_count": sum(arm["ownership_audit"]["typed_reference_ownership"]["sufficient"] for arm in quality),
            "refusal_route_pass_count": sum(row["arms"][variant]["refusal_route_pass"] is True for row in rows if row["is_refusal"])}
    effects = {}
    # Four matched cells for each main component; every cell retains case pairs.
    for field in ("contiguous_tokens", "deduplicate_query", "historical_scene_expansion"):
        cells = []
        for low in component_matrix():
            if getattr(low, field):
                continue
            high = replace(low, **{field: True})
            values = [row["arms"][high.variant_id]["canonical_metrics"]["hit_at_5"]
                      - row["arms"][low.variant_id]["canonical_metrics"]["hit_at_5"] for row in valid]
            cells.append({"low": low.variant_id, "high": high.variant_id,
                          "hit_at_5_delta": _mean(values), "denominator": len(values),
                          "ci95": list(bootstrap_ci(values, seed=42)) if values else None})
        effects[field] = cells
    calls = [arm["actual_provider_calls"] for row in rows for arm in row["arms"].values()]
    failed = any(arm["status"] != "succeeded" or arm["repeat_status"] != "succeeded"
                 or not arm["repeat_consistent"] for row in rows for arm in row["arms"].values())
    failed |= len(valid) != len(gold)
    refusal_failed = any(row["is_refusal"] and any(arm["refusal_route_pass"] is not True
                         for arm in row["arms"].values()) for row in rows)
    return {"status": "failed" if failed or refusal_failed else "completed", "case_count": len(rows),
            "retrieval_gold_count": len(gold), "paired_valid_count": len(valid),
            "unknown_count": len(gold) - len(valid), "arms": arms, "component_effect_cells": effects,
            "actual_provider_calls": sum(calls) if all(value is not None for value in calls) else None,
            "audit_scope": "flat-union vs typed query-reference ownership, not complete historical guard or legal truth"}


def create_run_directory(root, run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}", run_id):
        raise ValueError("invalid run ID")
    root.mkdir(parents=True, exist_ok=True)
    directory = root / run_id
    if directory.exists():
        raise ValueError("run already exists")
    directory.mkdir()
    return directory


def _critical_identity():
    return {name: hashlib.sha256((REPOSITORY_ROOT / name).read_bytes()).hexdigest() for name in CRITICAL_FILES}


def run_experiment(run_id):
    started_at, started = utc_now(), time.perf_counter()
    registry = load_dataset_registry(REPOSITORY_ROOT / "eval_cases/registry.json", repository_root=REPOSITORY_ROOT)
    dataset = registry.dataset("legal-eval-v3")
    cases = list(dataset.cases)
    if len(cases) != 120 or sum(bool(case.expected_law or case.expected_articles) for case in cases) != 108:
        raise ValueError("registered case denominator changed")
    chunks = load_chunks(REPOSITORY_ROOT / "artifacts/indexes/article/chunks.jsonl")
    corpus, code, critical = input_identity(chunks), repository_code_identity(REPOSITORY_ROOT), _critical_identity()
    directory = create_run_directory(REPOSITORY_ROOT / "artifacts/experiments", run_id)
    manifest = {"schema_version": 1, "run_id": run_id, "created_at": utc_now(),
                "code": code, "critical_file_sha256": critical, "corpus": corpus,
                "dataset": dataset.manifest_payload(), "components": [item.to_dict() for item in component_matrix()],
                "network_policy": "forbidden", "provider_policy": "forbidden", "dotenv_loaded": False,
                "parameters": {"top_k": 5, "bm25": load_config()["retrieval"],
                               "orders": "forward then reverse eight-arm order per case",
                               "quality_sample": "first pass only; no doubled independent sample count",
                               "generation": False, "followup_rounds": 0, "adaptive": False},
                "limits": ["reused development regressions, not holdout", "legacy canonical flat targets unchanged",
                           "typed runtime ownership audit is not legal gold", "diagnostic scene arms are not production profiles",
                           "no semantic checker or quality claim", "latency includes preparation, retrieval and audit, not service SLO"]}
    write_json(directory / "manifest.json", manifest)
    rows, build = run_matrix(cases, chunks, writer=JsonlTraceWriter(directory / "traces.jsonl", run_id=run_id))
    summary = summarize_matrix(rows)
    summary.update({"run_id": run_id, "build": build, "started_at": started_at, "completed_at": utc_now(),
                    "elapsed_ms": (time.perf_counter() - started) * 1000,
                    "code_stable": repository_code_identity(REPOSITORY_ROOT) == code and _critical_identity() == critical,
                    "input_stable": input_identity(chunks) == corpus,
                    "dataset_stable": load_dataset_registry(REPOSITORY_ROOT / "eval_cases/registry.json",
                        repository_root=REPOSITORY_ROOT).dataset("legal-eval-v3").manifest_payload() == dataset.manifest_payload(),
                    "estimated_model_cost_cny": "0" if summary["actual_provider_calls"] == 0 else None,
                    "invoice_verified": False,
                    "case_transitions": [{"case_id": row["case_id"], "arms": {
                        name: {key: arm[key] for key in ("status", "error_code", "canonical_metrics", "top5_chunk_ids",
                                                       "repeat_consistent", "ownership_audit")}
                        for name, arm in row["arms"].items()}} for row in rows]})
    if not all(summary[field] for field in ("code_stable", "input_stable", "dataset_stable")) or summary["actual_provider_calls"] != 0:
        summary["status"] = "failed"
    write_json(directory / "summary.json", summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    try:
        summary = run_experiment(args.run_id)
    except Exception:
        print(json.dumps({"status": "failed", "error_code": "component_experiment_failed",
                          "actual_provider_calls": None, "call_count_status": "unknown_if_not_persisted"}))
        return 2
    print(json.dumps({key: summary[key] for key in ("run_id", "status", "case_count", "retrieval_gold_count",
                     "paired_valid_count", "unknown_count", "actual_provider_calls", "code_stable", "input_stable", "dataset_stable")},
                     ensure_ascii=False, sort_keys=True))
    return 0 if summary["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
