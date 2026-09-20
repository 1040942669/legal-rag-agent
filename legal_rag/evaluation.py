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
from .evaluation_contracts import validate_eval_case as validate_eval_case
from .evaluation_scoring import (
    CompletedCaseOutcome,
    EvaluatedCase,
    ModelUsageDelta,
    citation_hit as citation_hit,
    hit_at_k as hit_at_k,
    keyword_coverage as keyword_coverage,
    mean_reciprocal_rank as mean_reciprocal_rank,
    metric_value as metric_value,
    result_matches as result_matches,
    score_completed_case,
    stage_status as stage_status,
    target_coverage as target_coverage,
)
from .judge import judge_answer
from .llm import usage_delta, usage_snapshot
from .models import (
    EVALUATION_METRICS_SCHEMA_VERSION,
    EvalCase,
    EvalRecord,
)
from .query import analyze_query
from .query_understanding import CompletionClient
from .retrieval import Retriever
from .tracing import JsonlTraceWriter
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
                expected_behavior = (
                    "out_of_scope" if case_type == "refusal" else "evidence_answer"
                )
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
        validate_eval_case(case)
        if case.case_id in case_ids:
            raise ValueError(
                f"evaluation case id must be non-empty and unique: {case.case_id!r}"
            )
        case_ids.add(case.case_id)
        group = case.session_group
        if group is None:
            if active_group is not None:
                closed_groups.add(active_group)
            active_group = None
            last_turn_index = -1
            continue
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


class _TraceCollector:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def write(self, record: dict[str, Any]) -> None:
        self.records.append(record)


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
    return _evaluate_cases(
        cases=cases,
        retriever=retriever,
        chunk_strategy=chunk_strategy,
        model=model,
        generate=generate,
        top_k=top_k,
        assistant=assistant,
        trace_writer=trace_writer,
        trace_metadata=trace_metadata,
        adaptive_enabled=adaptive_enabled,
        adaptive_use_llm=adaptive_use_llm,
        adaptive_llm_client=adaptive_llm_client,
        adaptive_max_queries=adaptive_max_queries,
        adaptive_per_plan_top_k=adaptive_per_plan_top_k,
        normalizer_retries=normalizer_retries,
        judge_client=judge_client,
        validate_sequence=True,
        preserve_first_assistant_memory=False,
    )


def evaluate_case(
    *,
    case: EvalCase,
    retriever: Retriever,
    chunk_strategy: str,
    model: str = "retrieval-only",
    generate: bool = False,
    top_k: int = 5,
    assistant: LegalChatAssistant | None = None,
    trace_metadata: dict[str, Any] | None = None,
    adaptive_enabled: bool = False,
    adaptive_use_llm: bool = False,
    adaptive_llm_client: CompletionClient | None = None,
    adaptive_max_queries: int = 3,
    adaptive_per_plan_top_k: int | None = None,
    normalizer_retries: int = 0,
    judge_client: CompletionClient | None = None,
) -> EvaluatedCase:
    """Evaluate exactly one case without resetting caller-owned session state."""

    validate_eval_case(case)
    collector = _TraceCollector()
    records = _evaluate_cases(
        cases=[case],
        retriever=retriever,
        chunk_strategy=chunk_strategy,
        model=model,
        generate=generate,
        top_k=top_k,
        assistant=assistant,
        trace_writer=collector,
        trace_metadata=trace_metadata,
        adaptive_enabled=adaptive_enabled,
        adaptive_use_llm=adaptive_use_llm,
        adaptive_llm_client=adaptive_llm_client,
        adaptive_max_queries=adaptive_max_queries,
        adaptive_per_plan_top_k=adaptive_per_plan_top_k,
        normalizer_retries=normalizer_retries,
        judge_client=judge_client,
        validate_sequence=False,
        preserve_first_assistant_memory=True,
    )
    if len(records) != 1 or len(collector.records) != 1:
        raise RuntimeError(
            "single-case evaluation did not produce exactly one artifact"
        )
    return EvaluatedCase(record=records[0], trace_record=collector.records[0])


def _evaluate_cases(
    *,
    cases: list[EvalCase],
    retriever: Retriever,
    chunk_strategy: str,
    model: str,
    generate: bool,
    top_k: int,
    assistant: LegalChatAssistant | None,
    trace_writer: JsonlTraceWriter | _TraceCollector | None,
    trace_metadata: dict[str, Any] | None,
    adaptive_enabled: bool,
    adaptive_use_llm: bool,
    adaptive_llm_client: CompletionClient | None,
    adaptive_max_queries: int,
    adaptive_per_plan_top_k: int | None,
    normalizer_retries: int,
    judge_client: CompletionClient | None,
    validate_sequence: bool,
    preserve_first_assistant_memory: bool,
) -> list[EvalRecord]:
    if validate_sequence:
        validate_eval_cases(cases)
    records: list[EvalRecord] = []
    active_session_group: str | None = None
    for case_index, case in enumerate(cases):
        assistant_client = getattr(assistant, "llm", None)
        assistant_usage_before = usage_snapshot(assistant_client)
        normalizer_usage_before = usage_snapshot(adaptive_llm_client)
        judge_usage_before = usage_snapshot(judge_client)
        if assistant is not None:
            # Independent cases are always reset. An explicitly named session
            # keeps memory only for its contiguous, ordered turns.
            preserve_current = preserve_first_assistant_memory and case_index == 0
            if not preserve_current and (
                case.session_group is None or case.session_group != active_session_group
            ):
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
        generation_kind = "retrieval_only" if not generate else "service_error"
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
                pre_fallback_answer = getattr(
                    assistant, "last_pre_fallback_answer", None
                )
                pre_fallback_verification = getattr(
                    assistant,
                    "last_pre_fallback_verification",
                    None,
                )
                generation_error = getattr(assistant, "last_generation_error", None)
                generation_kind = getattr(assistant, "last_generation_kind", None)
                if generation_kind is None:
                    if generation_error:
                        generation_kind = "generation_error"
                    elif (
                        getattr(structured_answer, "adapter_source", None)
                        == "programmatic"
                        and pre_fallback_verification is None
                    ):
                        generation_kind = "programmatic_terminal"
                    else:
                        generation_kind = "model"
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
                adaptive_trace = (
                    adaptive_result.to_trace() if adaptive_enabled else adaptive_trace
                )
                evidence_check = adaptive_result.evidence_check
        except Exception:  # Keep evaluation running across model failures.
            results = []
            answer = ""
            error = "evaluation service failed"
            generation_error = None
            evidence_check = None
            verification = None
            structured_answer = None
            pre_fallback_answer = None
            pre_fallback_verification = None
            generation_kind = "service_error" if generate else "retrieval_only"
        if evidence_check is None:
            evidence_check = check_evidence_sufficiency(
                case.question, results, analysis=analysis
            )
        if generate and verification is None and not error:
            verification = verify_answer(
                answer,
                results,
                evidence_check=evidence_check,
                risk_flags=analysis.risk_flags,
            )
        latency_ms = int((time.perf_counter() - started) * 1000)
        judge_result = None
        if judge_client is not None and generate and not error and not generation_error:
            judge_result = judge_answer(
                judge_client,
                question=case.question,
                answer=answer,
                results=results,
            )
        assistant_usage = usage_delta(
            assistant_usage_before, usage_snapshot(assistant_client)
        )
        normalizer_usage = usage_delta(
            normalizer_usage_before,
            usage_snapshot(adaptive_llm_client),
        )
        judge_usage = usage_delta(judge_usage_before, usage_snapshot(judge_client))
        evaluated = score_completed_case(
            CompletedCaseOutcome(
                case=case,
                model=model,
                retriever=getattr(retriever, "name", "unknown"),
                chunk_strategy=chunk_strategy,
                top_k=top_k,
                generate=generate,
                results=tuple(results),
                answer=answer,
                analysis=analysis,
                adaptive_trace=adaptive_trace,
                evidence_check=evidence_check,
                verification=verification,
                structured_answer=structured_answer,
                pre_fallback_answer=pre_fallback_answer,
                pre_fallback_verification=pre_fallback_verification,
                generation_kind=generation_kind,
                generation_error=generation_error,
                judge_configured=judge_client is not None,
                judge_result=judge_result,
                error=error,
                latency_ms=latency_ms,
                assistant_usage=ModelUsageDelta.from_mapping(assistant_usage),
                normalizer_usage=ModelUsageDelta.from_mapping(normalizer_usage),
                judge_usage=ModelUsageDelta.from_mapping(judge_usage),
                trace_metadata=trace_metadata,
            )
        )
        if trace_writer:
            trace_writer.write(evaluated.trace_record)
        records.append(evaluated.record)
    return records


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
        raise FileExistsError(
            f"evaluation outputs are immutable and already exist: {rendered}"
        )

    report = render_eval_report(records, metadata=metadata)

    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=[field.name for field in fields(EvalRecord)]
        )
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
    scored: list[EvalRecord] = []
    for record in records:
        if record.canonical_metrics:
            if _record_metric(record, "hit_at_5") is not None:
                scored.append(record)
            continue
        if record.failure_label != "not_applicable" and record.case_type != "refusal":
            scored.append(record)
    return scored


def available_mean(records: list[EvalRecord], field_name: str) -> float | None:
    """Average an available metric without reviving legacy sentinel values.

    A populated v2 canonical metric map is authoritative, including an
    explicit ``null`` value. Legacy scalar fields are used only for records
    that do not carry canonical metrics at all.
    """

    available: list[float] = []
    for record in records:
        if record.canonical_metrics:
            value = _record_metric(record, field_name)
            if isinstance(value, bool):
                available.append(float(value))
            elif isinstance(value, (int, float)):
                available.append(float(value))
            continue
        value = getattr(record, field_name)
        if isinstance(value, bool):
            available.append(float(value))
        elif isinstance(value, (int, float)) and float(value) >= 0.0:
            available.append(float(value))
    if not available:
        return None
    return sum(available) / len(available)


def format_available_metric(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.3f}"


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
        if _record_expected_behavior(record)
        in {"evidence_answer", "insufficient_evidence"}
    ]
    should_refuse = [
        record
        for record in records
        if _record_expected_behavior(record) == "out_of_scope"
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
        record
        for record in records
        if _record_stage_status(record, "judge") == "succeeded"
    ]
    judge_failed = [
        record for record in records if _record_stage_status(record, "judge") == "error"
    ]
    judge_not_run = [
        record
        for record in records
        if _record_stage_status(record, "judge") == "not_run"
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
            clarification_values.append(
                record.observed_answer_mode == "needs_clarification"
            )

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


def render_eval_report(
    records: list[EvalRecord], *, metadata: dict[str, Any] | None = None
) -> str:
    if not records:
        return "# 评估报告\n\n没有评估记录。\n"
    summary = summarize_evaluation(records)
    scored = scored_records(records)
    avg_hit3 = available_mean(scored, "hit_at_3")
    avg_hit5 = available_mean(scored, "hit_at_5")
    avg_mrr = available_mean(scored, "mrr")
    avg_target_coverage = available_mean(scored, "target_coverage")
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
    lines.extend(
        [
            "",
            "## 汇总指标",
            f"- Hit@3 (retrieval gold): {avg_hit3:.3f}"
            if avg_hit3 is not None
            else "- Hit@3 (retrieval gold): N/A",
            f"- Hit@5 (retrieval gold): {avg_hit5:.3f}"
            if avg_hit5 is not None
            else "- Hit@5 (retrieval gold): N/A",
            f"- MRR (retrieval gold): {avg_mrr:.3f}"
            if avg_mrr is not None
            else "- MRR (retrieval gold): N/A",
            f"- 目标条文覆盖率 (retrieval gold): {avg_target_coverage:.3f}"
            if avg_target_coverage is not None
            else "- 目标条文覆盖率 (retrieval gold): N/A",
            f"- 关键词覆盖率: {avg_keyword:.3f}"
            if avg_keyword is not None
            else "- 关键词覆盖率: N/A (answer unavailable)",
            f"- Evidence sufficiency pass: {avg_sufficiency:.3f}",
            f"- Citation validity: {avg_citation_valid:.3f}"
            if avg_citation_valid is not None
            else "- Citation validity: N/A (retrieval-only)",
            f"- Verifier pass: {avg_verifier:.3f}"
            if avg_verifier is not None
            else "- Verifier pass: N/A (retrieval-only)",
            f"- Refusal correctness: {avg_refusal:.3f}"
            if avg_refusal is not None
            else "- Refusal correctness: N/A (retrieval-only)",
            f"- 平均延迟: {avg_latency:.1f} ms",
            f"- P50 延迟: {percentile(latency_values, 0.50):.1f} ms",
            f"- P95 延迟: {percentile(latency_values, 0.95):.1f} ms",
            "- 兼容字段说明: `citation_hit` 等同检索目标命中，不表示回答中的 claim 获得语义支持。",
        ]
    )
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
        lines.extend(
            [
                "",
                "## 调用与成本观测",
                f"- LLM calls: assistant={assistant_calls}, normalizer={normalizer_calls}, judge={judge_calls}",
                f"- LLM failed calls: {sum(record.llm_failed_calls for record in records)}",
                f"- LLM provider latency: {sum(record.llm_latency_ms for record in records):.1f} ms",
            ]
        )
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
        lines.extend(
            [
                "",
                f"## 有目标样例指标 (n={len(scored)}, 排除拒答类, bootstrap 95% CI)",
                f"- Hit@3: {sum(scored_hit3) / len(scored):.3f} [{hit3_ci[0]:.3f}, {hit3_ci[1]:.3f}]",
                f"- Hit@5: {sum(scored_hit5) / len(scored):.3f} [{hit5_ci[0]:.3f}, {hit5_ci[1]:.3f}]",
                f"- MRR: {sum(scored_mrr) / len(scored):.3f} [{mrr_ci[0]:.3f}, {mrr_ci[1]:.3f}]",
            ]
        )
    judged = [
        record
        for record in records
        if _record_stage_status(record, "judge") == "succeeded"
        and record.judge_pass >= 0
    ]
    judge_errors = [
        record for record in records if _record_stage_status(record, "judge") == "error"
    ]
    judge_not_run = [
        record
        for record in records
        if _record_stage_status(record, "judge") == "not_run"
    ]
    lines.extend(
        [
            "",
            (
                f"## LLM Judge 指标 (成功 n={len(judged)}, 失败 n={len(judge_errors)}, "
                f"未执行 n={len(judge_not_run)})"
            ),
        ]
    )
    if judged:
        avg_faithfulness = sum(record.judge_faithfulness for record in judged) / len(
            judged
        )
        avg_relevance = sum(record.judge_relevance for record in judged) / len(judged)
        avg_completeness = sum(record.judge_completeness for record in judged) / len(
            judged
        )
        pass_rate = sum(record.judge_pass for record in judged) / len(judged)
        lines.extend(
            [
                f"- Faithfulness: {avg_faithfulness:.3f}",
                f"- Relevance: {avg_relevance:.3f}",
                f"- Completeness: {avg_completeness:.3f}",
                f"- Judge pass rate: {pass_rate:.3f}",
            ]
        )
    if judge_errors:
        lines.append("- Judge 调用或格式错误已从质量均值中排除。")
    if not judged:
        lines.append("- Judge 质量均值: N/A (没有成功的 judge 结果)")
    models = sorted({record.model for record in records})
    if len(models) > 1:
        lines.extend(
            [
                "",
                "## 按模型分组",
            ]
        )
        for model_name in models:
            group = [record for record in records if record.model == model_name]
            group_scored = scored_records(group)
            hit5 = available_mean(group_scored, "hit_at_5")
            keyword = available_mean(group, "keyword_coverage")
            verifier = available_mean(group, "verifier_pass")
            latency = sum(record.latency_ms for record in group) / len(group)
            line = (
                f"- `{model_name}` n={len(group)} "
                f"Hit@5(scored)={format_available_metric(hit5)} "
                f"KeywordCov={format_available_metric(keyword)} "
                f"VerifierPass={format_available_metric(verifier)} "
            )
            line += f"latency={latency:.1f}ms"
            judged_group = [
                record
                for record in group
                if _record_stage_status(record, "judge") == "succeeded"
            ]
            if judged_group:
                faith = available_mean(judged_group, "judge_faithfulness")
                jpass = available_mean(judged_group, "judge_pass")
                if faith is not None and jpass is not None:
                    line += f" JudgeFaith={faith:.3f} JudgePass={jpass:.3f}"
            judge_error_count = sum(
                _record_stage_status(record, "judge") == "error" for record in group
            )
            if judge_error_count:
                line += f" JudgeErrors={judge_error_count}"
            lines.append(line)
    lines.extend(
        [
            "",
            "## 分组指标",
        ]
    )
    for case_type, group in grouped_records(records).items():
        group_scored = scored_records(group)
        group_hit3 = available_mean(group_scored, "hit_at_3")
        group_hit5 = available_mean(group_scored, "hit_at_5")
        group_mrr = available_mean(group_scored, "mrr")
        group_target = available_mean(group_scored, "target_coverage")
        group_sufficiency = sum(record.sufficiency_pass for record in group) / len(
            group
        )
        group_verifier = available_mean(group, "verifier_pass")
        group_latency = sum(record.latency_ms for record in group) / len(group)
        lines.append(
            f"- `{case_type}` n={len(group)} "
            f"Hit@3={format_available_metric(group_hit3)} "
            f"Hit@5={format_available_metric(group_hit5)} "
            f"MRR={format_available_metric(group_mrr)} "
            f"TargetCoverage={format_available_metric(group_target)} "
            f"Sufficiency={group_sufficiency:.3f} "
            f"Verifier={format_available_metric(group_verifier)} "
            f"latency={group_latency:.1f}ms"
        )
    failure_counts = defaultdict(int)
    for record in records:
        if record.failure_label:
            failure_counts[record.failure_label] += 1
    lines.extend(
        [
            "",
            "## 失败归因",
        ]
    )
    for label, count in sorted(failure_counts.items()):
        lines.append(f"- `{label}`: {count}")
    lines.extend(
        [
            "",
            "## 失败样例",
        ]
    )
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
