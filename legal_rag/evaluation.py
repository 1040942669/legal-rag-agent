from __future__ import annotations

import csv
import json
import random
import time
from collections import defaultdict
from dataclasses import fields
from pathlib import Path
from typing import Any

from .adaptive import retrieve_adaptive
from .chat import LegalChatAssistant
from .evidence import check_evidence_sufficiency
from .failure_analysis import label_retrieval_failure
from .judge import judge_answer
from .llm import usage_delta, usage_snapshot
from .models import (
    ANSWER_MODES,
    EVALUATION_METRICS_SCHEMA_VERSION,
    EvalCase,
    EvalRecord,
    SearchResult,
)
from .query import analyze_query
from .query_understanding import CompletionClient
from .retrieval import Retriever, format_sources
from .tracing import JsonlTraceWriter, build_retrieval_trace_record
from .verifier import verify_answer


def load_eval_cases(path: str | Path) -> list[EvalCase]:
    cases: list[EvalCase] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            item = json.loads(line)
            case_type = item.get("type", "unknown")
            expected_behavior = item.get("expected_behavior")
            expected_answer_mode = item.get("expected_answer_mode")
            if (
                expected_behavior is not None
                and expected_answer_mode is not None
                and expected_behavior != expected_answer_mode
            ):
                raise ValueError(
                    f"conflicting expected behavior aliases for {item.get('id')!r}"
                )
            if expected_behavior is None:
                expected_behavior = expected_answer_mode
            if expected_behavior is None:
                expected_behavior = "out_of_scope" if case_type == "refusal" else "evidence_answer"
            cases.append(
                EvalCase(
                    case_id=item["id"],
                    question=item["question"],
                    case_type=case_type,
                    expected_law=item.get("expected_law", ""),
                    expected_articles=item.get("expected_articles", []),
                    keywords=item.get("keywords", []),
                    expected_behavior=expected_behavior,
                    session_group=item.get("session_group"),
                    turn_index=item.get("turn_index", 0),
                    schema_version=item.get("schema_version", 1),
                )
            )
    validate_eval_cases(cases)
    return cases


def validate_eval_cases(cases: list[EvalCase]) -> None:
    """Validate explicit single-turn and multi-turn evaluation boundaries."""

    case_ids: set[str] = set()
    active_group: str | None = None
    closed_groups: set[str] = set()
    last_turn_index = -1
    for case in cases:
        if not case.case_id or case.case_id in case_ids:
            raise ValueError(f"evaluation case id must be non-empty and unique: {case.case_id!r}")
        case_ids.add(case.case_id)
        behavior = case.resolved_expected_behavior
        if not isinstance(behavior, str) or behavior not in ANSWER_MODES:
            raise ValueError(
                f"unknown expected_behavior for {case.case_id}: "
                f"{behavior!r}"
            )
        if isinstance(case.turn_index, bool) or not isinstance(case.turn_index, int):
            raise ValueError(f"turn_index must be an integer for {case.case_id}")
        if case.turn_index < 0:
            raise ValueError(f"turn_index must be non-negative for {case.case_id}")
        if isinstance(case.schema_version, bool) or not isinstance(case.schema_version, int):
            raise ValueError(f"schema_version must be an integer for {case.case_id}")
        if case.schema_version < 1:
            raise ValueError(f"schema_version must be at least 1 for {case.case_id}")

        group = case.session_group
        if group is None:
            if case.turn_index != 0:
                raise ValueError(
                    f"single-turn case {case.case_id} must use turn_index=0"
                )
            if active_group is not None:
                closed_groups.add(active_group)
            active_group = None
            last_turn_index = -1
            continue
        if not isinstance(group, str) or not group.strip():
            raise ValueError(f"session_group must be a non-empty string for {case.case_id}")
        if group != active_group:
            if active_group is not None:
                closed_groups.add(active_group)
            if group in closed_groups:
                raise ValueError(f"session_group must be contiguous: {group!r}")
            if case.turn_index != 0:
                raise ValueError(f"session_group {group!r} must start at turn_index=0")
            active_group = group
            last_turn_index = 0
            continue
        if case.turn_index != last_turn_index + 1:
            raise ValueError(
                f"session_group {group!r} turn_index must be contiguous; "
                f"expected {last_turn_index + 1}, got {case.turn_index}"
            )
        last_turn_index = case.turn_index


def metric_value(value: Any, unavailable_reason: str | None = None) -> dict[str, Any]:
    if value is None and not unavailable_reason:
        raise ValueError("an unavailable metric must include an unavailable_reason")
    return {"value": value, "unavailable_reason": unavailable_reason}


def stage_status(status: str, reason: str | None = None) -> dict[str, str | None]:
    return {"status": status, "reason": reason}


def evaluate(
    *,
    cases: list[EvalCase],
    retriever: Retriever,
    chunk_strategy: str,
    model: str = "retrieval-only",
    generate: bool = False,
    top_k: int = 5,
    assistant: LegalChatAssistant | None = None,
    trace_writer: JsonlTraceWriter | None = None,
    trace_metadata: dict[str, Any] | None = None,
    adaptive_enabled: bool = False,
    adaptive_use_llm: bool = False,
    adaptive_llm_client: CompletionClient | None = None,
    adaptive_max_queries: int = 3,
    adaptive_per_plan_top_k: int | None = None,
    normalizer_retries: int = 0,
    judge_client: CompletionClient | None = None,
) -> list[EvalRecord]:
    validate_eval_cases(cases)
    records: list[EvalRecord] = []
    active_session_group: str | None = None
    for case in cases:
        expected_behavior = case.resolved_expected_behavior
        assistant_client = getattr(assistant, "llm", None)
        assistant_usage_before = usage_snapshot(assistant_client)
        normalizer_usage_before = usage_snapshot(adaptive_llm_client)
        judge_usage_before = usage_snapshot(judge_client)
        if assistant is not None:
            # Independent cases are always reset. An explicitly named session
            # keeps memory only for its contiguous, ordered turns.
            if case.session_group is None or case.session_group != active_session_group:
                assistant.reset_memory()
        active_session_group = case.session_group
        analysis = analyze_query(case.question)
        adaptive_trace: dict[str, Any] = {"enabled": adaptive_enabled, "used": False}
        started = time.perf_counter()
        answer = ""
        error = ""
        generation_error: str | None = None
        evidence_check = None
        verification = None
        structured_answer = None
        pre_fallback_answer = None
        pre_fallback_verification = None
        service_status = stage_status("succeeded")
        generation_status = (
            stage_status("succeeded") if generate else stage_status("not_run", "retrieval_only")
        )
        try:
            if generate:
                if assistant is None:
                    raise RuntimeError("assistant is required when generate=True")
                answer, results = assistant.answer(case.question, generate=True)
                adaptive_result = getattr(assistant, "last_adaptive_result", None)
                if adaptive_result is not None:
                    analysis = adaptive_result.analysis
                    adaptive_trace = adaptive_result.to_trace()
                evidence_check = getattr(assistant, "last_evidence_check", None)
                verification = getattr(assistant, "last_verification", None)
                structured_answer = getattr(assistant, "last_structured_answer", None)
                pre_fallback_answer = getattr(assistant, "last_pre_fallback_answer", None)
                pre_fallback_verification = getattr(
                    assistant,
                    "last_pre_fallback_verification",
                    None,
                )
                generation_error = getattr(assistant, "last_generation_error", None)
                if generation_error:
                    generation_status = stage_status("error", generation_error)
                    service_status = stage_status("degraded", generation_error)
                elif getattr(structured_answer, "adapter_source", None) == "programmatic":
                    generation_status = stage_status("not_run", "programmatic_terminal")
            else:
                adaptive_result = retrieve_adaptive(
                    case.question,
                    retriever,
                    top_k=top_k,
                    enabled=adaptive_enabled,
                    use_llm=adaptive_use_llm,
                    llm_client=adaptive_llm_client,
                    max_queries=adaptive_max_queries,
                    per_plan_top_k=adaptive_per_plan_top_k,
                    normalizer_retries=normalizer_retries,
                )
                results = adaptive_result.results
                analysis = adaptive_result.analysis
                adaptive_trace = adaptive_result.to_trace() if adaptive_enabled else adaptive_trace
                evidence_check = adaptive_result.evidence_check
        except Exception as exc:  # Keep evaluation running across model failures.
            results = []
            error = str(exc) or type(exc).__name__
            service_status = stage_status("error", "service_error")
            if generate:
                generation_status = stage_status("error", "service_error")
        if evidence_check is None:
            evidence_check = check_evidence_sufficiency(case.question, results, analysis=analysis)
        if generate and verification is None and not error:
            verification = verify_answer(
                answer,
                results,
                evidence_check=evidence_check,
                risk_flags=analysis.risk_flags,
            )
        latency_ms = int((time.perf_counter() - started) * 1000)
        failure = label_retrieval_failure(results, case, top_k=top_k)
        judge_result = None
        if judge_client is not None and generate and not error and not generation_error:
            judge_result = judge_answer(
                judge_client,
                question=case.question,
                answer=answer,
                results=results,
            )
        judge_succeeded = judge_result is not None and judge_result.status == "succeeded"
        if not generate:
            judge_status = stage_status("not_run", "retrieval_only")
        elif error:
            judge_status = stage_status("not_run", "service_error")
        elif generation_error:
            judge_status = stage_status("not_run", generation_error)
        elif judge_client is None:
            judge_status = stage_status("not_run", "judge_not_configured")
        elif judge_succeeded:
            judge_status = stage_status("succeeded")
        else:
            judge_status = stage_status(
                "error",
                judge_result.error_code if judge_result is not None else "judge_error",
            )

        if not generate:
            verification_status = stage_status("not_run", "retrieval_only")
        elif error:
            verification_status = stage_status("not_run", "service_error")
        elif verification is None:
            verification_status = stage_status("not_run", "verification_not_available")
        else:
            verification_status = stage_status("succeeded")

        execution = {
            "service": service_status,
            "generation": generation_status,
            "verification": verification_status,
            "judge": judge_status,
        }
        if not generate:
            generation_attempt = {"attempted": False, "reason": "retrieval_only"}
            final_response = {"value": None, "unavailable_reason": "retrieval_only"}
        else:
            attempted_mode = getattr(pre_fallback_answer, "answer_mode", None)
            if attempted_mode is None:
                attempted_mode = getattr(structured_answer, "answer_mode", None)
            programmatic_terminal = (
                getattr(structured_answer, "adapter_source", None) == "programmatic"
                and not generation_error
                and pre_fallback_verification is None
            )
            generation_attempt = {
                "attempted": not bool(error) and not programmatic_terminal,
                "status": (
                    "rejected"
                    if pre_fallback_verification is not None
                    else "error"
                    if error or generation_error
                    else "not_run"
                    if programmatic_terminal
                    else "accepted"
                ),
                "reason": (
                    "verification_failed"
                    if pre_fallback_verification is not None
                    else generation_error
                    or ("service_error" if error else None)
                    or ("programmatic_terminal" if programmatic_terminal else None)
                ),
                "answer_mode": attempted_mode,
                "verification": (
                    pre_fallback_verification.to_dict()
                    if pre_fallback_verification is not None
                    else None
                ),
            }
            if error:
                final_response = {"value": None, "unavailable_reason": "service_error"}
            else:
                observed_mode_for_trace = getattr(structured_answer, "answer_mode", None)
                if observed_mode_for_trace is None and verification is not None:
                    observed_mode_for_trace = verification.actual_answer_mode
                final_response = {
                    "value": {
                        "answer_mode": observed_mode_for_trace,
                        "verification": verification.to_dict() if verification is not None else None,
                    },
                    "unavailable_reason": None,
                }

        observed_answer_mode = getattr(structured_answer, "answer_mode", None)
        if observed_answer_mode is None and verification is not None:
            observed_answer_mode = verification.actual_answer_mode
        if not generate:
            answer_reason = "retrieval_only"
        elif error:
            answer_reason = "service_error"
        else:
            answer_reason = None
        if verification is None:
            verification_reason = "retrieval_only" if not generate else "service_error" if error else "verification_not_available"
        else:
            verification_reason = None
        if judge_succeeded:
            judge_reason = None
        else:
            judge_reason = str(judge_status["reason"] or "judge_not_available")

        keyword_value = keyword_coverage(answer, case.keywords) if answer_reason is None else None
        citation_ids_value = verification.citation_ids_valid if verification is not None else None
        verifier_value = verification.passed if verification is not None else None
        semantic_value = (
            verification.semantic_support_status if verification is not None else None
        )
        response_mode_value = (
            bool(observed_answer_mode == expected_behavior and verification.response_mode_valid)
            if verification is not None and observed_answer_mode is not None
            else None
        )
        if not generate:
            refusal_value = None
            refusal_reason = "retrieval_only"
        elif expected_behavior == "out_of_scope":
            refusal_value = (
                bool(verification.refusal_present)
                if verification is not None
                else None
            )
            refusal_reason = verification_reason if refusal_value is None else None
        else:
            refusal_value = None
            refusal_reason = "not_expected_to_refuse"
        if expected_behavior in {"evidence_answer", "insufficient_evidence"}:
            over_refusal_value = (
                bool(verification.refusal_present)
                if verification is not None
                else None
            )
            over_refusal_reason = verification_reason if over_refusal_value is None else None
        else:
            over_refusal_value = None
            over_refusal_reason = "not_expected_to_answer"

        has_retrieval_gold = bool(case.expected_law or case.expected_articles)
        no_gold_reason = None if has_retrieval_gold else "no_retrieval_gold"
        canonical_metrics = {
            "answer_text": metric_value(answer if answer_reason is None else None, answer_reason),
            "hit_at_3": metric_value(hit_at_k(results, case, 3) if has_retrieval_gold else None, no_gold_reason),
            "hit_at_5": metric_value(hit_at_k(results, case, 5) if has_retrieval_gold else None, no_gold_reason),
            "mrr": metric_value(mean_reciprocal_rank(results, case) if has_retrieval_gold else None, no_gold_reason),
            "target_coverage": metric_value(target_coverage(results, case, top_k) if case.expected_articles else None, None if case.expected_articles else "no_article_gold"),
            "retrieval_target_hit": metric_value(
                citation_hit(results, case) if has_retrieval_gold else None,
                no_gold_reason,
            ),
            "citation_hit": metric_value(
                citation_hit(results, case) if has_retrieval_gold else None,
                no_gold_reason,
            ),
            "keyword_coverage": metric_value(keyword_value, answer_reason),
            "citation_ids_valid": metric_value(citation_ids_value, verification_reason),
            "citation_valid": metric_value(citation_ids_value, verification_reason),
            "verifier_pass": metric_value(verifier_value, verification_reason),
            "semantic_support_status": metric_value(semantic_value, verification_reason),
            "response_mode_correct": metric_value(response_mode_value, verification_reason),
            "refusal_recall_hit": metric_value(refusal_value, refusal_reason),
            "refusal_correctness": metric_value(refusal_value, refusal_reason),
            "over_refusal": metric_value(over_refusal_value, over_refusal_reason),
            "judge_faithfulness": metric_value(
                judge_result.faithfulness if judge_succeeded else None,
                judge_reason,
            ),
            "judge_relevance": metric_value(
                judge_result.relevance if judge_succeeded else None,
                judge_reason,
            ),
            "judge_completeness": metric_value(
                judge_result.completeness if judge_succeeded else None,
                judge_reason,
            ),
            "judge_pass": metric_value(
                judge_result.passed if judge_succeeded else None,
                judge_reason,
            ),
        }

        assistant_usage = usage_delta(assistant_usage_before, usage_snapshot(assistant_client))
        normalizer_usage = usage_delta(
            normalizer_usage_before,
            usage_snapshot(adaptive_llm_client),
        )
        judge_usage = usage_delta(judge_usage_before, usage_snapshot(judge_client))
        usage_parts = (assistant_usage, normalizer_usage, judge_usage)
        if trace_writer:
            trace_writer.write(
                build_retrieval_trace_record(
                    case_id=case.case_id,
                    query=case.question,
                    retriever=getattr(retriever, "name", "unknown"),
                    top_k=top_k,
                    results=results,
                    latency_ms=latency_ms,
                    analyzer=analysis.to_dict(),
                    adaptive=adaptive_trace,
                    evidence=evidence_check.to_dict(),
                    verifier=verification.to_dict() if generate and verification is not None else None,
                    execution=execution,
                    generation_attempt=generation_attempt,
                    final_response=final_response,
                    failure=failure.to_dict(),
                    metadata=trace_metadata,
                )
            )
        records.append(
            EvalRecord(
                case_id=case.case_id,
                case_type=case.case_type,
                model=model,
                retriever=getattr(retriever, "name", "unknown"),
                chunk_strategy=chunk_strategy,
                hit_at_3=hit_at_k(results, case, 3),
                hit_at_5=hit_at_k(results, case, 5),
                mrr=mean_reciprocal_rank(results, case),
                target_coverage=target_coverage(results, case, top_k),
                keyword_coverage=keyword_value if keyword_value is not None else -1.0,
                citation_hit=citation_hit(results, case),
                sufficiency_pass=int(evidence_check.sufficient),
                citation_valid=int(citation_ids_value) if citation_ids_value is not None else -1,
                verifier_pass=int(verifier_value) if verifier_value is not None else -1,
                refusal_correctness=(
                    int(refusal_value) if refusal_value is not None else -1
                ),
                latency_ms=latency_ms,
                answer=answer[:1200],
                sources=format_sources(results),
                error=error,
                failure_label=failure.label,
                failure_reason=failure.reason,
                judge_faithfulness=float(judge_result.faithfulness) if judge_succeeded else -1.0,
                judge_relevance=float(judge_result.relevance) if judge_succeeded else -1.0,
                judge_completeness=float(judge_result.completeness) if judge_succeeded else -1.0,
                judge_pass=int(judge_result.passed) if judge_succeeded else -1,
                judge_comment=judge_result.comment if judge_succeeded else "",
                judge_error=judge_result.error if judge_result and not judge_succeeded else "",
                assistant_llm_calls=int(assistant_usage["calls"]),
                normalizer_llm_calls=int(normalizer_usage["calls"]),
                judge_llm_calls=int(judge_usage["calls"]),
                llm_failed_calls=sum(int(item["failed_calls"]) for item in usage_parts),
                input_tokens=sum(int(item["input_tokens"]) for item in usage_parts),
                output_tokens=sum(int(item["output_tokens"]) for item in usage_parts),
                total_tokens=sum(int(item["total_tokens"]) for item in usage_parts),
                token_usage_calls=sum(int(item["token_usage_calls"]) for item in usage_parts),
                llm_latency_ms=round(sum(float(item["latency_ms"]) for item in usage_parts), 3),
                metrics_schema_version=EVALUATION_METRICS_SCHEMA_VERSION,
                expected_behavior=expected_behavior,
                observed_answer_mode=observed_answer_mode,
                execution=execution,
                canonical_metrics=canonical_metrics,
                generation_attempt=generation_attempt,
            )
        )
    return records


def hit_at_k(results: list[SearchResult], case: EvalCase, k: int) -> int:
    if not case.expected_law and not case.expected_articles:
        return 0
    return int(any(result_matches(result, case) for result in results[:k]))


def mean_reciprocal_rank(results: list[SearchResult], case: EvalCase) -> float:
    for result in results:
        if result_matches(result, case):
            return round(1 / result.rank, 4)
    return 0.0


def citation_hit(results: list[SearchResult], case: EvalCase) -> int:
    if not case.expected_law and not case.expected_articles:
        return 0
    return int(any(result_matches(result, case) for result in results))


def target_coverage(results: list[SearchResult], case: EvalCase, k: int = 5) -> float:
    if not case.expected_articles:
        return 0.0
    found = set()
    for result in results[:k]:
        if case.expected_law and case.expected_law not in result.chunk.law_names:
            continue
        for article in case.expected_articles:
            if article in result.chunk.article_numbers:
                found.add(article)
    return round(len(found) / len(case.expected_articles), 4)


def result_matches(result: SearchResult, case: EvalCase) -> bool:
    if not case.expected_law and not case.expected_articles:
        return False
    law_ok = not case.expected_law or case.expected_law in result.chunk.law_names
    article_ok = not case.expected_articles or any(
        article in result.chunk.article_numbers for article in case.expected_articles
    )
    return law_ok and article_ok


def keyword_coverage(answer: str, keywords: list[str]) -> float:
    if not keywords:
        return 0.0
    matched = sum(1 for keyword in keywords if keyword and keyword in answer)
    return round(matched / len(keywords), 4)


def write_eval_outputs(
    records: list[EvalRecord],
    output_dir: str | Path,
    prefix: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{prefix}.csv"
    report_path = out_dir / f"{prefix}.md"

    existing = [path for path in (csv_path, report_path) if path.exists()]
    if existing:
        rendered = ", ".join(str(path) for path in existing)
        raise FileExistsError(f"evaluation outputs are immutable and already exist: {rendered}")

    report = render_eval_report(records, metadata=metadata)

    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[field.name for field in fields(EvalRecord)])
        writer.writeheader()
        for record in records:
            row: dict[str, Any] = {}
            for field in fields(EvalRecord):
                value = getattr(record, field.name)
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False, sort_keys=True)
                row[field.name] = value
            writer.writerow(row)

    report_path.write_text(report, encoding="utf-8")
    return csv_path, report_path


def bootstrap_ci(
    values: list[float],
    *,
    n_resamples: int = 2000,
    confidence: float = 0.95,
    seed: int = 42,
) -> tuple[float, float]:
    """Percentile bootstrap confidence interval for the mean of `values`."""
    if n_resamples < 1:
        raise ValueError("n_resamples must be at least 1")
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be between 0 and 1")
    if not values:
        return (0.0, 0.0)
    if len(values) == 1:
        return (values[0], values[0])
    rng = random.Random(seed)
    n = len(values)
    means = sorted(
        sum(rng.choice(values) for _ in range(n)) / n for _ in range(n_resamples)
    )
    alpha = (1.0 - confidence) / 2
    lower = means[int(alpha * n_resamples)]
    upper = means[min(int((1.0 - alpha) * n_resamples), n_resamples - 1)]
    return (round(lower, 4), round(upper, 4))


def scored_records(records: list[EvalRecord]) -> list[EvalRecord]:
    """Records with a retrieval target (refusal cases have none and would dilute hit metrics)."""
    return [record for record in records if record.failure_label != "not_applicable"]


def available_mean(records: list[EvalRecord], field_name: str) -> float | None:
    values = [float(getattr(record, field_name)) for record in records]
    available = [value for value in values if value >= 0.0]
    if not available:
        return None
    return sum(available) / len(available)


def _record_expected_behavior(record: EvalRecord) -> str:
    if record.expected_behavior:
        return record.expected_behavior
    return "out_of_scope" if record.case_type == "refusal" else "evidence_answer"


def _record_stage_status(record: EvalRecord, stage: str) -> str:
    payload = record.execution.get(stage, {}) if record.execution else {}
    status = payload.get("status")
    if isinstance(status, str) and status:
        return status
    if stage == "service":
        return "error" if record.error else "succeeded"
    if stage == "judge":
        if record.judge_pass >= 0:
            return "succeeded"
        if record.judge_error:
            return "error"
        return "not_run"
    return "succeeded"


def _record_metric(record: EvalRecord, metric_name: str) -> Any:
    payload = record.canonical_metrics.get(metric_name, {})
    if isinstance(payload, dict) and "value" in payload:
        return payload["value"]
    return None


def _available_ratio(values: list[bool]) -> dict[str, Any]:
    denominator = len(values)
    numerator = sum(bool(value) for value in values)
    if denominator == 0:
        return {
            "numerator": 0,
            "denominator": 0,
            "value": None,
            "unavailable_reason": "no_eligible_cases",
        }
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": round(numerator / denominator, 4),
    }


def summarize_evaluation(records: list[EvalRecord]) -> dict[str, Any]:
    """Aggregate M1 metrics with disjoint, explicit denominators."""

    should_answer = [
        record
        for record in records
        if _record_expected_behavior(record) in {"evidence_answer", "insufficient_evidence"}
    ]
    should_refuse = [
        record for record in records if _record_expected_behavior(record) == "out_of_scope"
    ]
    should_clarify = [
        record
        for record in records
        if _record_expected_behavior(record) == "needs_clarification"
    ]
    service_failures = [
        record
        for record in records
        if _record_stage_status(record, "service") in {"error", "degraded"}
    ]
    judge_succeeded = [
        record for record in records if _record_stage_status(record, "judge") == "succeeded"
    ]
    judge_failed = [
        record for record in records if _record_stage_status(record, "judge") == "error"
    ]
    judge_not_run = [
        record for record in records if _record_stage_status(record, "judge") == "not_run"
    ]
    retrieval_gold = [
        record
        for record in records
        if (
            _record_metric(record, "hit_at_5") is not None
            or (
                not record.canonical_metrics
                and record.failure_label != "not_applicable"
                and record.case_type != "refusal"
            )
        )
    ]

    refusal_values: list[bool] = []
    for record in should_refuse:
        if _record_stage_status(record, "service") in {"error", "degraded"}:
            continue
        value = _record_metric(record, "refusal_recall_hit")
        if value is None and record.observed_answer_mode is not None:
            value = record.observed_answer_mode == "out_of_scope"
        if value is not None:
            refusal_values.append(bool(value))

    over_refusal_values: list[bool] = []
    for record in should_answer:
        if _record_stage_status(record, "service") in {"error", "degraded"}:
            continue
        value = _record_metric(record, "over_refusal")
        if value is None and record.observed_answer_mode is not None:
            value = record.observed_answer_mode == "out_of_scope"
        if value is not None:
            over_refusal_values.append(bool(value))

    clarification_values: list[bool] = []
    for record in should_clarify:
        if _record_stage_status(record, "service") in {"error", "degraded"}:
            continue
        if record.observed_answer_mode is not None:
            clarification_values.append(record.observed_answer_mode == "needs_clarification")

    mode_values: list[bool] = []
    for record in records:
        if _record_stage_status(record, "service") in {"error", "degraded"}:
            continue
        value = _record_metric(record, "response_mode_correct")
        if value is None and record.observed_answer_mode is not None:
            value = record.observed_answer_mode == _record_expected_behavior(record)
        if value is not None:
            mode_values.append(bool(value))

    return {
        "metrics_schema_version": EVALUATION_METRICS_SCHEMA_VERSION,
        "denominators": {
            "retrieval_gold": len(retrieval_gold),
            "should_answer": len(should_answer),
            "should_refuse": len(should_refuse),
            "should_clarify": len(should_clarify),
            "service_failures": len(service_failures),
            "judge_succeeded": len(judge_succeeded),
            "judge_failed": len(judge_failed),
            "judge_not_run": len(judge_not_run),
        },
        "refusal_recall": _available_ratio(refusal_values),
        "over_refusal_rate": _available_ratio(over_refusal_values),
        "clarification_recall": _available_ratio(clarification_values),
        "answer_mode_accuracy": _available_ratio(mode_values),
    }


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    if not 0.0 <= quantile <= 1.0:
        raise ValueError("quantile must be between 0 and 1")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction)


def render_eval_report(records: list[EvalRecord], *, metadata: dict[str, Any] | None = None) -> str:
    if not records:
        return "# 评估报告\n\n没有评估记录。\n"
    summary = summarize_evaluation(records)
    scored = scored_records(records)
    avg_hit3 = sum(record.hit_at_3 for record in scored) / len(scored) if scored else None
    avg_hit5 = sum(record.hit_at_5 for record in scored) / len(scored) if scored else None
    avg_mrr = sum(record.mrr for record in scored) / len(scored) if scored else None
    avg_target_coverage = (
        sum(record.target_coverage for record in scored) / len(scored) if scored else None
    )
    avg_latency = sum(record.latency_ms for record in records) / len(records)
    latency_values = [float(record.latency_ms) for record in records]
    avg_keyword = available_mean(records, "keyword_coverage")
    avg_sufficiency = sum(record.sufficiency_pass for record in records) / len(records)
    avg_citation_valid = available_mean(records, "citation_valid")
    avg_verifier = available_mean(records, "verifier_pass")
    avg_refusal = available_mean(records, "refusal_correctness")
    first = records[0]

    lines = [
        "# 评估报告",
        "",
        "## 实验配置",
        f"- 模型: `{first.model}`",
        f"- 检索器: `{first.retriever}`",
        f"- Chunk 策略: `{first.chunk_strategy}`",
        f"- 样例数: {len(records)}",
        f"- 指标 schema: v{summary['metrics_schema_version']}",
    ]
    if metadata:
        for label, value in report_metadata_items(metadata):
            lines.append(f"- {label}: {value}")
    lines.extend([
        "",
        "## 汇总指标",
        f"- Hit@3 (retrieval gold): {avg_hit3:.3f}" if avg_hit3 is not None else "- Hit@3 (retrieval gold): N/A",
        f"- Hit@5 (retrieval gold): {avg_hit5:.3f}" if avg_hit5 is not None else "- Hit@5 (retrieval gold): N/A",
        f"- MRR (retrieval gold): {avg_mrr:.3f}" if avg_mrr is not None else "- MRR (retrieval gold): N/A",
        f"- 目标条文覆盖率 (retrieval gold): {avg_target_coverage:.3f}" if avg_target_coverage is not None else "- 目标条文覆盖率 (retrieval gold): N/A",
        f"- 关键词覆盖率: {avg_keyword:.3f}" if avg_keyword is not None else "- 关键词覆盖率: N/A (answer unavailable)",
        f"- Evidence sufficiency pass: {avg_sufficiency:.3f}",
        f"- Citation validity: {avg_citation_valid:.3f}" if avg_citation_valid is not None else "- Citation validity: N/A (retrieval-only)",
        f"- Verifier pass: {avg_verifier:.3f}" if avg_verifier is not None else "- Verifier pass: N/A (retrieval-only)",
        f"- Refusal correctness: {avg_refusal:.3f}" if avg_refusal is not None else "- Refusal correctness: N/A (retrieval-only)",
        f"- 平均延迟: {avg_latency:.1f} ms",
        f"- P50 延迟: {percentile(latency_values, 0.50):.1f} ms",
        f"- P95 延迟: {percentile(latency_values, 0.95):.1f} ms",
        "- 兼容字段说明: `citation_hit` 等同检索目标命中，不表示回答中的 claim 获得语义支持。",
    ])
    lines.extend(["", "## M1 v2 显式分母"])
    for denominator_name, value in summary["denominators"].items():
        lines.append(f"- {denominator_name}: {value}")
    for metric_name in (
        "refusal_recall",
        "over_refusal_rate",
        "clarification_recall",
        "answer_mode_accuracy",
    ):
        metric = summary[metric_name]
        value = metric["value"]
        if value is None:
            lines.append(
                f"- {metric_name}: {metric['numerator']}/{metric['denominator']} = N/A "
                f"({metric['unavailable_reason']})"
            )
        else:
            lines.append(
                f"- {metric_name}: {metric['numerator']}/{metric['denominator']} = {value:.3f}"
            )
    assistant_calls = sum(record.assistant_llm_calls for record in records)
    normalizer_calls = sum(record.normalizer_llm_calls for record in records)
    judge_calls = sum(record.judge_llm_calls for record in records)
    total_llm_calls = assistant_calls + normalizer_calls + judge_calls
    token_usage_calls = sum(record.token_usage_calls for record in records)
    if total_llm_calls:
        input_tokens = sum(record.input_tokens for record in records)
        output_tokens = sum(record.output_tokens for record in records)
        total_tokens = sum(record.total_tokens for record in records)
        lines.extend([
            "",
            "## 调用与成本观测",
            f"- LLM calls: assistant={assistant_calls}, normalizer={normalizer_calls}, judge={judge_calls}",
            f"- LLM failed calls: {sum(record.llm_failed_calls for record in records)}",
            f"- LLM provider latency: {sum(record.llm_latency_ms for record in records):.1f} ms",
        ])
        if token_usage_calls:
            lines.append(
                f"- Tokens: input={input_tokens}, output={output_tokens}, total={total_tokens} "
                f"(usage available for {token_usage_calls}/{total_llm_calls} calls)"
            )
            input_rate = (metadata or {}).get("input_cost_per_million")
            output_rate = (metadata or {}).get("output_cost_per_million")
            if input_rate is not None or output_rate is not None:
                estimated_cost = (
                    input_tokens * float(input_rate or 0.0)
                    + output_tokens * float(output_rate or 0.0)
                ) / 1_000_000
                lines.append(
                    f"- Estimated model cost (user-supplied blended rates): ${estimated_cost:.6f}"
                )
        else:
            lines.append("- Tokens: N/A (provider did not expose usage metadata)")
    if scored:
        scored_hit3 = [float(record.hit_at_3) for record in scored]
        scored_hit5 = [float(record.hit_at_5) for record in scored]
        scored_mrr = [record.mrr for record in scored]
        hit3_ci = bootstrap_ci(scored_hit3)
        hit5_ci = bootstrap_ci(scored_hit5)
        mrr_ci = bootstrap_ci(scored_mrr)
        lines.extend([
            "",
            f"## 有目标样例指标 (n={len(scored)}, 排除拒答类, bootstrap 95% CI)",
            f"- Hit@3: {sum(scored_hit3) / len(scored):.3f} [{hit3_ci[0]:.3f}, {hit3_ci[1]:.3f}]",
            f"- Hit@5: {sum(scored_hit5) / len(scored):.3f} [{hit5_ci[0]:.3f}, {hit5_ci[1]:.3f}]",
            f"- MRR: {sum(scored_mrr) / len(scored):.3f} [{mrr_ci[0]:.3f}, {mrr_ci[1]:.3f}]",
        ])
    judged = [
        record
        for record in records
        if _record_stage_status(record, "judge") == "succeeded" and record.judge_pass >= 0
    ]
    judge_errors = [
        record for record in records if _record_stage_status(record, "judge") == "error"
    ]
    judge_not_run = [
        record for record in records if _record_stage_status(record, "judge") == "not_run"
    ]
    lines.extend([
        "",
        (
            f"## LLM Judge 指标 (成功 n={len(judged)}, 失败 n={len(judge_errors)}, "
            f"未执行 n={len(judge_not_run)})"
        ),
    ])
    if judged:
        avg_faithfulness = sum(record.judge_faithfulness for record in judged) / len(judged)
        avg_relevance = sum(record.judge_relevance for record in judged) / len(judged)
        avg_completeness = sum(record.judge_completeness for record in judged) / len(judged)
        pass_rate = sum(record.judge_pass for record in judged) / len(judged)
        lines.extend([
            f"- Faithfulness: {avg_faithfulness:.3f}",
            f"- Relevance: {avg_relevance:.3f}",
            f"- Completeness: {avg_completeness:.3f}",
            f"- Judge pass rate: {pass_rate:.3f}",
        ])
    if judge_errors:
        lines.append("- Judge 调用或格式错误已从质量均值中排除。")
    if not judged:
        lines.append("- Judge 质量均值: N/A (没有成功的 judge 结果)")
    models = sorted({record.model for record in records})
    if len(models) > 1:
        lines.extend([
            "",
            "## 按模型分组",
        ])
        for model_name in models:
            group = [record for record in records if record.model == model_name]
            group_scored = scored_records(group)
            hit5 = (
                sum(record.hit_at_5 for record in group_scored) / len(group_scored)
                if group_scored
                else 0.0
            )
            keyword = sum(record.keyword_coverage for record in group) / len(group)
            verifier = available_mean(group, "verifier_pass")
            verifier_text = f"{verifier:.3f}" if verifier is not None else "N/A"
            latency = sum(record.latency_ms for record in group) / len(group)
            line = (
                f"- `{model_name}` n={len(group)} Hit@5(scored)={hit5:.3f} "
                f"KeywordCov={keyword:.3f} VerifierPass={verifier_text} "
            )
            line += f"latency={latency:.1f}ms"
            judged_group = [record for record in group if record.judge_pass >= 0]
            if judged_group:
                faith = sum(record.judge_faithfulness for record in judged_group) / len(judged_group)
                jpass = sum(record.judge_pass for record in judged_group) / len(judged_group)
                line += f" JudgeFaith={faith:.3f} JudgePass={jpass:.3f}"
            judge_error_count = sum(bool(record.judge_error) for record in group)
            if judge_error_count:
                line += f" JudgeErrors={judge_error_count}"
            lines.append(line)
    lines.extend([
        "",
        "## 分组指标",
    ])
    for case_type, group in grouped_records(records).items():
        group_hit3 = sum(record.hit_at_3 for record in group) / len(group)
        group_hit5 = sum(record.hit_at_5 for record in group) / len(group)
        group_mrr = sum(record.mrr for record in group) / len(group)
        group_target = sum(record.target_coverage for record in group) / len(group)
        group_sufficiency = sum(record.sufficiency_pass for record in group) / len(group)
        group_verifier = available_mean(group, "verifier_pass")
        group_verifier_text = f"{group_verifier:.3f}" if group_verifier is not None else "N/A"
        group_latency = sum(record.latency_ms for record in group) / len(group)
        lines.append(
            f"- `{case_type}` n={len(group)} Hit@3={group_hit3:.3f} "
            f"Hit@5={group_hit5:.3f} MRR={group_mrr:.3f} "
            f"TargetCoverage={group_target:.3f} Sufficiency={group_sufficiency:.3f} "
            f"Verifier={group_verifier_text} latency={group_latency:.1f}ms"
        )
    failure_counts = defaultdict(int)
    for record in records:
        if record.failure_label:
            failure_counts[record.failure_label] += 1
    lines.extend([
        "",
        "## 失败归因",
    ])
    for label, count in sorted(failure_counts.items()):
        lines.append(f"- `{label}`: {count}")
    lines.extend([
        "",
        "## 失败样例",
    ])
    failures = [
        record
        for record in records
        if record.case_type != "refusal"
        and record.failure_label not in {"", "hit", "not_applicable"}
        and not record.error
    ]
    if not failures:
        lines.append("- 未发现 Hit@5 失败样例。")
    for record in failures[:10]:
        lines.append(
            f"- `{record.case_id}` `{record.failure_label}` {record.failure_reason} "
            f"sources: {record.sources.splitlines()[:2]}"
        )
    errors = [record for record in records if record.error]
    if errors:
        lines.extend(["", "## 运行错误"])
        for record in errors[:10]:
            lines.append(f"- `{record.case_id}` {record.error}")
    lines.append("")
    return "\n".join(lines)


def report_metadata_items(metadata: dict[str, Any]) -> list[tuple[str, str]]:
    ordered_keys = [
        ("run_id", "Run ID"),
        ("config_path", "配置文件"),
        ("case_path", "评测集"),
        ("top_k", "Top K"),
        ("chunk_count", "Chunk 数"),
        ("index_size_mb", "索引体积 (MB)"),
        ("retriever_build_seconds", "检索器构建耗时 (s)"),
        ("retriever_build_peak_mb", "检索器构建峰值内存 (MB)"),
        ("reranker", "Reranker"),
        ("rerank_candidate_top_n", "Rerank candidate top N"),
        ("rerank_calls", "Rerank calls"),
        ("rerank_failed_calls", "Rerank failed calls"),
        ("rerank_documents", "Reranked documents"),
        ("rerank_total_ms", "Rerank total latency (ms)"),
        ("judge_model", "Judge 模型"),
        ("adaptive_enabled", "Adaptive enabled"),
        ("adaptive_use_llm", "Adaptive LLM normalizer"),
        ("adaptive_max_queries", "Adaptive max queries"),
        ("adaptive_per_plan_top_k", "Adaptive per-plan top K"),
        ("adaptive_normalizer_retries", "Adaptive normalizer retries"),
        ("bm25_k1", "BM25 k1"),
        ("bm25_b", "BM25 b"),
        ("bm25_law_boost", "BM25 law boost"),
        ("bm25_article_boost", "BM25 article boost"),
        ("index_manifest_path", "Index manifest"),
        ("diagnostics_path", "Chunk diagnostics"),
        ("trace_path", "Trace JSONL"),
        ("embedding_key", "Embedding key"),
        ("embedding_cache_dir", "Embedding cache"),
    ]
    items: list[tuple[str, str]] = []
    for key, label in ordered_keys:
        value = metadata.get(key)
        if value in {None, ""}:
            continue
        if isinstance(value, (int, float)):
            rendered = str(value)
        else:
            rendered = f"`{value}`"
        items.append((label, rendered))
    return items


def grouped_records(records: list[EvalRecord]) -> dict[str, list[EvalRecord]]:
    groups: dict[str, list[EvalRecord]] = defaultdict(list)
    for record in records:
        groups[record.case_type].append(record)
    return dict(sorted(groups.items()))
