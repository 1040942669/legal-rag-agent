from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from .models import NormalizedQuery, RetrievalPlan, SearchResult
from .evidence import check_evidence_sufficiency, with_stop_reason
from .planning import build_retrieval_plans
from .query import QueryAnalysis, analyze_query, should_use_adaptive
from .query_understanding import (
    CompletionClient,
    fallback_normalized_query,
    normalize_query,
)
from .retrieval import (
    Retriever,
    assert_results_match_boundary,
    retrieval_boundary,
)
from .retrieval_contracts import (
    RetrievalBoundaryViolation,
    validate_retrieval_top_k,
)


@dataclass(frozen=True)
class AdaptiveRetrievalResult:
    results: list[SearchResult]
    analysis: QueryAnalysis
    normalized_query: NormalizedQuery | None
    plans: list[RetrievalPlan]
    planner_trace: dict[str, Any]
    merge_trace: dict[str, Any]
    evidence_check: Any
    followup_trace: dict[str, Any]
    adaptive_used: bool
    adaptive_enabled: bool
    trigger_reasons: list[str]

    def to_trace(self) -> dict[str, Any]:
        return {
            "enabled": self.adaptive_enabled,
            "used": self.adaptive_used,
            "trigger_reasons": self.trigger_reasons,
            "normalized_query": self.normalized_query.to_dict()
            if self.normalized_query
            else {},
            "plans": [plan.to_dict() for plan in self.plans],
            "planner": self.planner_trace,
            "merge": self.merge_trace,
            "evidence": self.evidence_check.to_dict() if self.evidence_check else {},
            "followup": self.followup_trace,
        }


def retrieve_adaptive(
    query: str,
    retriever: Retriever,
    *,
    top_k: int = 5,
    enabled: bool = False,
    use_llm: bool = False,
    llm_client: CompletionClient | None = None,
    max_queries: int = 3,
    per_plan_top_k: int | None = None,
    normalizer_retries: int = 0,
    max_followup_rounds: int = 1,
) -> AdaptiveRetrievalResult:
    top_k = validate_retrieval_top_k(top_k)
    if per_plan_top_k is not None:
        per_plan_top_k = validate_retrieval_top_k(per_plan_top_k)
    boundary = retrieval_boundary(retriever)
    analysis = analyze_query(query)
    trigger = enabled and should_use_adaptive(analysis)
    if not trigger:
        results = retriever.retrieve(query, top_k=top_k)
        assert_results_match_boundary(
            results, boundary, stage="adaptive direct retrieval"
        )
        evidence = check_evidence_sufficiency(query, results, analysis=analysis)
        results, evidence, followup_trace = run_bounded_followup(
            query,
            retriever,
            results,
            evidence,
            top_k=top_k,
            max_rounds=max_followup_rounds,
            analysis=analysis,
        )
        assert_results_match_boundary(
            results, boundary, stage="adaptive direct/follow-up merge"
        )
        return AdaptiveRetrievalResult(
            results=results,
            analysis=analysis,
            normalized_query=None,
            plans=[],
            planner_trace={"mode": "direct"},
            merge_trace={
                "mode": "direct",
                "input_result_count": len(results),
                "deduped_count": len(results),
            },
            evidence_check=evidence,
            followup_trace=followup_trace,
            adaptive_used=False,
            adaptive_enabled=enabled,
            trigger_reasons=analysis.adaptive_reasons if enabled else [],
        )

    normalized = normalize_query(
        query,
        analysis=analysis,
        llm_client=llm_client,
        use_llm=use_llm,
        max_retries=normalizer_retries,
    )
    plans, planner_trace = build_retrieval_plans(
        normalized,
        max_queries=max_queries,
        per_plan_top_k=per_plan_top_k or top_k,
    )
    merged, merge_trace = retrieve_and_merge_plans(
        retriever,
        plans,
        final_top_k=top_k,
    )
    assert_results_match_boundary(merged, boundary, stage="adaptive multi-plan merge")
    evidence = check_evidence_sufficiency(
        query,
        merged,
        analysis=analysis,
        normalized_query=normalized,
        plans=plans,
    )
    merged, evidence, followup_trace = run_bounded_followup(
        query,
        retriever,
        merged,
        evidence,
        top_k=top_k,
        max_rounds=max_followup_rounds,
        analysis=analysis,
        normalized_query=normalized,
        plans=plans,
    )
    assert_results_match_boundary(
        merged, boundary, stage="adaptive multi-plan/follow-up merge"
    )
    return AdaptiveRetrievalResult(
        results=merged,
        analysis=analysis,
        normalized_query=normalized,
        plans=plans,
        planner_trace=planner_trace,
        merge_trace=merge_trace,
        evidence_check=evidence,
        followup_trace=followup_trace,
        adaptive_used=True,
        adaptive_enabled=enabled,
        trigger_reasons=analysis.adaptive_reasons,
    )


def retrieve_and_merge_plans(
    retriever: Retriever,
    plans: list[RetrievalPlan],
    *,
    final_top_k: int = 5,
) -> tuple[list[SearchResult], dict[str, Any]]:
    final_top_k = validate_retrieval_top_k(final_top_k)
    boundary = retrieval_boundary(retriever)
    by_chunk_id: dict[str, SearchResult] = {}
    source_trace: dict[str, list[dict[str, Any]]] = {}
    input_result_count = 0

    for plan in plans:
        results = retriever.retrieve(plan.query, top_k=plan.top_k)
        assert_results_match_boundary(
            results, boundary, stage="adaptive plan retrieval"
        )
        input_result_count += len(results)
        for result in results:
            chunk_id = result.chunk.chunk_id
            source_trace.setdefault(chunk_id, []).append(
                {
                    "plan_id": plan.plan_id,
                    "source_query": plan.query,
                    "source_rank": result.rank,
                    "source_score": result.score,
                    "source_retriever": result.retriever,
                    "ranking_trace": result.trace,
                }
            )
            existing = by_chunk_id.get(chunk_id)
            if existing is not None and (
                existing.chunk != result.chunk
                or existing.provenance != result.provenance
            ):
                raise RetrievalBoundaryViolation(
                    "adaptive plans returned conflicting payloads for one chunk ID"
                )
            if existing is None or result.score > existing.score:
                by_chunk_id[chunk_id] = result

    ranked_items = sorted(
        by_chunk_id.values(),
        key=lambda result: (
            best_source_rank(source_trace[result.chunk.chunk_id]),
            -result.score,
            result.chunk.chunk_id,
        ),
    )[:final_top_k]

    merged: list[SearchResult] = []
    for rank, result in enumerate(ranked_items, start=1):
        chunk_id = result.chunk.chunk_id
        sources = source_trace[chunk_id]
        merged.append(
            replace(
                result,
                score=result.score,
                rank=rank,
                retriever=f"adaptive_{result.retriever}",
                trace={
                    **result.trace,
                    "adaptive": {
                        "source_plans": sources,
                        "source_plan_ids": [source["plan_id"] for source in sources],
                        "best_source_rank": best_source_rank(sources),
                        "matched_plan_count": len(sources),
                    },
                },
            )
        )

    assert_results_match_boundary(merged, boundary, stage="adaptive plan ranking")
    return merged, {
        "mode": "multi_query",
        "plan_count": len(plans),
        "input_result_count": input_result_count,
        "deduped_count": len(by_chunk_id),
        "returned_count": len(merged),
        "truncated_to_top_k": max(0, len(by_chunk_id) - len(merged)),
    }


def run_bounded_followup(
    query: str,
    retriever: Retriever,
    results: list[SearchResult],
    evidence,
    *,
    top_k: int,
    max_rounds: int,
    analysis: QueryAnalysis | None = None,
    normalized_query: NormalizedQuery | None = None,
    plans: list[RetrievalPlan] | None = None,
) -> tuple[list[SearchResult], Any, dict[str, Any]]:
    top_k = validate_retrieval_top_k(top_k)
    boundary = retrieval_boundary(retriever)
    assert_results_match_boundary(
        results, boundary, stage="adaptive initial follow-up candidates"
    )
    trace: dict[str, Any] = {
        "max_rounds": max_rounds,
        "rounds_used": 0,
        "queries": [],
        "stop_reason": evidence.stop_reason,
    }
    if evidence.sufficient:
        trace["stop_reason"] = "sufficient"
        return results, with_stop_reason(evidence, "sufficient"), trace
    if evidence.stop_reason == "needs_clarification":
        trace["stop_reason"] = "needs_clarification"
        return results, with_stop_reason(evidence, "needs_clarification"), trace
    if max_rounds <= 0 or not evidence.followup_queries:
        reason = "max_rounds_reached" if max_rounds <= 0 else "no_followup_query"
        trace["stop_reason"] = reason
        return results, with_stop_reason(evidence, reason), trace

    followup_results: list[SearchResult] = []
    for followup_query in evidence.followup_queries:
        retrieved = retriever.retrieve(followup_query, top_k=top_k)
        assert_results_match_boundary(
            retrieved, boundary, stage="adaptive follow-up retrieval"
        )
        trace["queries"].append(
            {
                "query": followup_query,
                "result_count": len(retrieved),
            }
        )
        followup_results.extend(retrieved)
    trace["rounds_used"] = 1
    merged = merge_followup_results(results, followup_results, final_top_k=top_k)
    assert_results_match_boundary(merged, boundary, stage="adaptive follow-up merge")
    checked = check_evidence_sufficiency(
        query,
        merged,
        analysis=analysis,
        normalized_query=normalized_query,
        plans=plans,
    )
    stop_reason = (
        "sufficient_after_followup" if checked.sufficient else "max_rounds_reached"
    )
    trace["stop_reason"] = stop_reason
    return merged, with_stop_reason(checked, stop_reason), trace


def merge_followup_results(
    initial_results: list[SearchResult],
    followup_results: list[SearchResult],
    *,
    final_top_k: int,
) -> list[SearchResult]:
    final_top_k = validate_retrieval_top_k(final_top_k)
    by_chunk_id: dict[str, SearchResult] = {}
    for result in initial_results + followup_results:
        existing = by_chunk_id.get(result.chunk.chunk_id)
        if existing is not None and (
            existing.chunk != result.chunk or existing.provenance != result.provenance
        ):
            raise RetrievalBoundaryViolation(
                "adaptive follow-up returned conflicting payloads for one chunk ID"
            )
        if existing is None or result.score > existing.score:
            by_chunk_id[result.chunk.chunk_id] = result
    ranked = sorted(
        by_chunk_id.values(),
        key=lambda result: (-result.score, result.rank, result.chunk.chunk_id),
    )[:final_top_k]
    return [
        replace(
            result,
            score=result.score,
            rank=rank,
            retriever=result.retriever,
            trace={
                **result.trace,
                "followup_merge": {
                    "original_rank": result.rank,
                    "source_retriever": result.retriever,
                },
            },
        )
        for rank, result in enumerate(ranked, start=1)
    ]


def direct_normalized_trace(query: str, analysis: QueryAnalysis) -> NormalizedQuery:
    return fallback_normalized_query(query, analysis, source="rules:direct_trace")


def best_source_rank(sources: list[dict[str, Any]]) -> int:
    ranks = [
        int(source["source_rank"]) for source in sources if source.get("source_rank")
    ]
    return min(ranks) if ranks else 999999
