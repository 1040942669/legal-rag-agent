from __future__ import annotations

import json
import math
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any

from .evaluation_artifacts import (
    eval_record_from_artifact,
    eval_record_to_artifact,
    search_result_from_artifact,
    search_result_to_artifact,
)
from .evaluation_contracts import validate_eval_case
from .experiment_runtime import canonical_json_bytes
from .failure_analysis import label_retrieval_failure
from .judge import JudgeResult
from .models import (
    ANSWER_MODES,
    EVALUATION_METRICS_SCHEMA_VERSION,
    EvalCase,
    EvalRecord,
    EvidenceCheck,
    SearchResult,
    StructuredAnswer,
    VerificationResult,
)
from .query import QueryAnalysis
from .retrieval import format_sources
from .tracing import build_retrieval_trace_record, verification_result_to_trace


_USAGE_FIELDS = frozenset(
    {
        "calls",
        "failed_calls",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "token_usage_calls",
        "latency_ms",
    }
)
_GENERATION_ERROR_CODES = frozenset({"generation_error"})
_JUDGE_ERROR_CODES = frozenset(
    {"judge_error", "timeout", "transport_error", "invalid_json", "invalid_schema"}
)
_SAFE_JUDGE_ERRORS = frozenset(
    {
        "judge request timed out",
        "judge provider request failed",
        "judge response exceeds the maximum accepted size",
        "judge response is not valid JSON",
        "judge response fields do not match the required schema",
        "judge response field `passed` must be a boolean",
        "judge response field `comment` must be a string",
    }
)
_PROGRAMMATIC_GENERATION_KINDS = frozenset(
    {"pre_retrieval_refusal", "evidence_limited", "programmatic_terminal"}
)
_GENERATION_KINDS = frozenset(
    {
        "model",
        "generation_error",
        "retrieval_only",
        "service_error",
        *_PROGRAMMATIC_GENERATION_KINDS,
    }
)


@dataclass(frozen=True)
class ModelUsageDelta:
    """Immutable model-usage facts captured before scoring begins."""

    calls: int = 0
    failed_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    token_usage_calls: int = 0
    latency_ms: float = 0.0

    @classmethod
    def from_mapping(cls, value: Mapping[str, int | float]) -> ModelUsageDelta:
        if not isinstance(value, Mapping) or set(value) != _USAGE_FIELDS:
            raise ValueError("model usage delta fields are invalid")
        counts: dict[str, int] = {}
        for name in _USAGE_FIELDS - {"latency_ms"}:
            item = value[name]
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise ValueError(
                    f"model usage delta {name} must be a non-negative integer"
                )
            counts[name] = item
        latency = value["latency_ms"]
        if isinstance(latency, bool) or not isinstance(latency, (int, float)):
            raise ValueError("model usage delta latency_ms must be a number")
        latency_value = float(latency)
        if not math.isfinite(latency_value) or latency_value < 0:
            raise ValueError(
                "model usage delta latency_ms must be finite and non-negative"
            )
        if counts["failed_calls"] > counts["calls"]:
            raise ValueError("model usage delta failed_calls cannot exceed calls")
        if counts["token_usage_calls"] > counts["calls"]:
            raise ValueError("model usage delta token_usage_calls cannot exceed calls")
        if counts["token_usage_calls"] == 0 and any(
            counts[name] for name in ("input_tokens", "output_tokens", "total_tokens")
        ):
            raise ValueError("model usage delta tokens require a token usage record")
        if counts["total_tokens"] < counts["input_tokens"] + counts["output_tokens"]:
            raise ValueError(
                "model usage delta total_tokens cannot be less than input plus output"
            )
        return cls(**counts, latency_ms=latency_value)


@dataclass(frozen=True)
class CompletedCaseOutcome:
    """All execution facts required to score one completed evaluation case.

    The boundary intentionally contains values only. It has no retriever,
    assistant, provider client, clock, writer, or session object, so scoring a
    cached/replayed outcome cannot accidentally execute an upstream stage.
    """

    case: EvalCase
    model: str
    retriever: str
    chunk_strategy: str
    top_k: int
    generate: bool
    results: tuple[SearchResult, ...]
    answer: str
    analysis: QueryAnalysis
    adaptive_trace: Mapping[str, Any]
    evidence_check: EvidenceCheck
    verification: VerificationResult | None
    structured_answer: StructuredAnswer | None
    pre_fallback_answer: StructuredAnswer | None
    pre_fallback_verification: VerificationResult | None
    generation_kind: str
    generation_error: str | None
    judge_configured: bool
    judge_result: JudgeResult | None
    error: str
    latency_ms: int
    assistant_usage: ModelUsageDelta
    normalizer_usage: ModelUsageDelta
    judge_usage: ModelUsageDelta
    trace_metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class EvaluatedCase:
    record: EvalRecord
    trace_record: dict[str, Any]


def metric_value(value: Any, unavailable_reason: str | None = None) -> dict[str, Any]:
    if value is None and not unavailable_reason:
        raise ValueError("an unavailable metric must include an unavailable_reason")
    return {"value": value, "unavailable_reason": unavailable_reason}


def stage_status(status: str, reason: str | None = None) -> dict[str, str | None]:
    return {"status": status, "reason": reason}


def score_completed_case(outcome: CompletedCaseOutcome) -> EvaluatedCase:
    """Deterministically derive the record and trace from completed stage facts.

    This function deliberately performs no retrieval, generation, verification,
    judging, timing, usage sampling, session commit, or persistence. Inputs are
    snapshotted before use and outputs do not alias mutable caller-owned values.
    """

    _validate_completed_case_outcome(outcome)
    case = deepcopy(outcome.case)
    results = [
        search_result_from_artifact(search_result_to_artifact(result))
        for result in outcome.results
    ]
    analysis = deepcopy(outcome.analysis)
    adaptive_trace = deepcopy(dict(outcome.adaptive_trace))
    evidence_check = deepcopy(outcome.evidence_check)
    verification = deepcopy(outcome.verification)
    structured_answer = deepcopy(outcome.structured_answer)
    pre_fallback_answer = deepcopy(outcome.pre_fallback_answer)
    pre_fallback_verification = deepcopy(outcome.pre_fallback_verification)
    judge_result = deepcopy(outcome.judge_result)
    trace_metadata = (
        deepcopy(dict(outcome.trace_metadata))
        if outcome.trace_metadata is not None
        else None
    )

    expected_behavior = case.resolved_expected_behavior
    error = outcome.error
    generation_error = outcome.generation_error
    generate = outcome.generate

    service_status = stage_status("succeeded")
    generation_status = (
        stage_status("succeeded")
        if generate
        else stage_status("not_run", "retrieval_only")
    )
    if error:
        service_status = stage_status("error", "service_error")
        if generate:
            generation_status = stage_status("error", "service_error")
    elif generation_error:
        generation_status = stage_status("error", generation_error)
        service_status = stage_status("degraded", generation_error)
    elif generate and outcome.generation_kind in _PROGRAMMATIC_GENERATION_KINDS:
        generation_status = stage_status("not_run", "programmatic_terminal")

    judge_succeeded = judge_result is not None and judge_result.status == "succeeded"
    if not generate:
        judge_status = stage_status("not_run", "retrieval_only")
    elif error:
        judge_status = stage_status("not_run", "service_error")
    elif generation_error:
        judge_status = stage_status("not_run", generation_error)
    elif not outcome.judge_configured:
        judge_status = stage_status("not_run", "judge_not_configured")
    elif judge_succeeded:
        judge_status = stage_status("succeeded")
    else:
        judge_status = stage_status("error", judge_result.error_code or "judge_error")

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
            outcome.generation_kind in _PROGRAMMATIC_GENERATION_KINDS
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
                verification_result_to_trace(pre_fallback_verification)
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
                    "verification": verification_result_to_trace(verification),
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
        verification_reason = (
            "retrieval_only"
            if not generate
            else "service_error"
            if error
            else "verification_not_available"
        )
    else:
        verification_reason = None
    judge_reason = (
        None
        if judge_succeeded
        else str(judge_status["reason"] or "judge_not_available")
    )

    keyword_value = (
        keyword_coverage(outcome.answer, case.keywords)
        if answer_reason is None
        else None
    )
    source_ids_exist_value = (
        verification.evidence_catalog_valid and not verification.missing_source_ids
        if verification is not None
        else None
    )
    citation_ids_value = (
        verification.citation_ids_valid if verification is not None else None
    )
    citation_valid_value = (
        verification.citation_valid if verification is not None else None
    )
    verifier_value = verification.passed if verification is not None else None
    semantic_value = (
        verification.semantic_support_status if verification is not None else None
    )
    evidence_scope_value = (
        verification.evidence_scope_valid if verification is not None else None
    )
    evidence_scope_reason = (
        verification_reason
        if verification is None
        else "scope_check_not_configured"
        if evidence_scope_value is None
        else None
    )
    response_mode_value = (
        bool(
            observed_answer_mode == expected_behavior
            and verification.response_mode_valid
        )
        if verification is not None and observed_answer_mode is not None
        else None
    )
    if not generate:
        refusal_value = None
        refusal_reason = "retrieval_only"
    elif expected_behavior == "out_of_scope":
        refusal_value = (
            bool(verification.refusal_present) if verification is not None else None
        )
        refusal_reason = verification_reason if refusal_value is None else None
    else:
        refusal_value = None
        refusal_reason = "not_expected_to_refuse"
    if expected_behavior in {"evidence_answer", "insufficient_evidence"}:
        over_refusal_value = (
            bool(verification.refusal_present) if verification is not None else None
        )
        over_refusal_reason = (
            verification_reason if over_refusal_value is None else None
        )
    else:
        over_refusal_value = None
        over_refusal_reason = "not_expected_to_answer"

    has_retrieval_gold = bool(case.expected_law or case.expected_articles)
    no_gold_reason = None if has_retrieval_gold else "no_retrieval_gold"
    canonical_metrics = {
        "answer_text": metric_value(
            outcome.answer if answer_reason is None else None, answer_reason
        ),
        "hit_at_3": metric_value(
            hit_at_k(results, case, 3) if has_retrieval_gold else None,
            no_gold_reason,
        ),
        "hit_at_5": metric_value(
            hit_at_k(results, case, 5) if has_retrieval_gold else None,
            no_gold_reason,
        ),
        "mrr": metric_value(
            mean_reciprocal_rank(results, case) if has_retrieval_gold else None,
            no_gold_reason,
        ),
        "target_coverage": metric_value(
            target_coverage(results, case, outcome.top_k)
            if case.expected_articles
            else None,
            None if case.expected_articles else "no_article_gold",
        ),
        "retrieval_target_hit": metric_value(
            citation_hit(results, case) if has_retrieval_gold else None,
            no_gold_reason,
        ),
        "citation_hit": metric_value(
            citation_hit(results, case) if has_retrieval_gold else None,
            no_gold_reason,
        ),
        "keyword_coverage": metric_value(keyword_value, answer_reason),
        "schema_valid": metric_value(
            verification.schema_valid if verification is not None else None,
            verification_reason,
        ),
        "evidence_catalog_valid": metric_value(
            verification.evidence_catalog_valid if verification is not None else None,
            verification_reason,
        ),
        "source_ids_exist": metric_value(source_ids_exist_value, verification_reason),
        "citation_ids_valid": metric_value(citation_ids_value, verification_reason),
        "citation_alignment_valid": metric_value(
            verification.citation_alignment_valid if verification is not None else None,
            verification_reason,
        ),
        "evidence_scope_valid": metric_value(
            evidence_scope_value,
            evidence_scope_reason,
        ),
        "citation_valid": metric_value(citation_valid_value, verification_reason),
        "disclaimer_present": metric_value(
            verification.disclaimer_present if verification is not None else None,
            verification_reason,
        ),
        "response_mode_valid": metric_value(
            verification.response_mode_valid if verification is not None else None,
            verification_reason,
        ),
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

    failure = label_retrieval_failure(results, case, top_k=outcome.top_k)
    usage_parts = (
        outcome.assistant_usage,
        outcome.normalizer_usage,
        outcome.judge_usage,
    )
    trace_record = build_retrieval_trace_record(
        case_id=case.case_id,
        query=case.question,
        retriever=outcome.retriever,
        top_k=outcome.top_k,
        results=results,
        latency_ms=outcome.latency_ms,
        analyzer=analysis.to_dict(),
        adaptive=adaptive_trace,
        evidence=evidence_check.to_dict(),
        verifier=(
            verification_result_to_trace(verification)
            if generate and verification is not None
            else None
        ),
        execution=execution,
        generation_attempt=generation_attempt,
        final_response=final_response,
        failure=failure.to_dict(),
        metadata=trace_metadata,
    )
    record = EvalRecord(
        case_id=case.case_id,
        case_type=case.case_type,
        model=outcome.model,
        retriever=outcome.retriever,
        chunk_strategy=outcome.chunk_strategy,
        hit_at_3=hit_at_k(results, case, 3),
        hit_at_5=hit_at_k(results, case, 5),
        mrr=mean_reciprocal_rank(results, case),
        target_coverage=target_coverage(results, case, outcome.top_k),
        keyword_coverage=keyword_value if keyword_value is not None else -1.0,
        citation_hit=citation_hit(results, case),
        sufficiency_pass=int(evidence_check.sufficient),
        citation_valid=(
            int(citation_valid_value) if citation_valid_value is not None else -1
        ),
        verifier_pass=int(verifier_value) if verifier_value is not None else -1,
        refusal_correctness=(int(refusal_value) if refusal_value is not None else -1),
        latency_ms=outcome.latency_ms,
        answer=outcome.answer[:1200],
        sources=format_sources(results),
        error=error,
        failure_label=failure.label,
        failure_reason=failure.reason,
        judge_faithfulness=(
            float(judge_result.faithfulness) if judge_succeeded else -1.0
        ),
        judge_relevance=(float(judge_result.relevance) if judge_succeeded else -1.0),
        judge_completeness=(
            float(judge_result.completeness) if judge_succeeded else -1.0
        ),
        judge_pass=int(judge_result.passed) if judge_succeeded else -1,
        judge_comment=judge_result.comment if judge_succeeded else "",
        judge_error=(
            judge_result.error
            if judge_result is not None and not judge_succeeded
            else ""
        ),
        assistant_llm_calls=outcome.assistant_usage.calls,
        normalizer_llm_calls=outcome.normalizer_usage.calls,
        judge_llm_calls=outcome.judge_usage.calls,
        llm_failed_calls=sum(item.failed_calls for item in usage_parts),
        input_tokens=sum(item.input_tokens for item in usage_parts),
        output_tokens=sum(item.output_tokens for item in usage_parts),
        total_tokens=sum(item.total_tokens for item in usage_parts),
        token_usage_calls=sum(item.token_usage_calls for item in usage_parts),
        llm_latency_ms=round(sum(item.latency_ms for item in usage_parts), 3),
        metrics_schema_version=EVALUATION_METRICS_SCHEMA_VERSION,
        expected_behavior=expected_behavior,
        observed_answer_mode=observed_answer_mode,
        execution=execution,
        canonical_metrics=canonical_metrics,
        generation_attempt=generation_attempt,
    )
    record = eval_record_from_artifact(eval_record_to_artifact(record))
    canonical_trace = json.loads(canonical_json_bytes(trace_record).decode("utf-8"))
    return EvaluatedCase(record=record, trace_record=canonical_trace)


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


def _validate_completed_case_outcome(outcome: CompletedCaseOutcome) -> None:
    if not isinstance(outcome, CompletedCaseOutcome):
        raise TypeError("outcome must be a CompletedCaseOutcome")
    if not isinstance(outcome.case, EvalCase):
        raise ValueError("completed case outcome requires an EvalCase")
    validate_eval_case(outcome.case)
    if outcome.case.resolved_expected_behavior not in ANSWER_MODES:
        raise ValueError("completed case outcome has an unsupported expected behavior")
    for name in ("model", "retriever", "chunk_strategy"):
        value = getattr(outcome, name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"completed case outcome {name} must be non-empty")
    if isinstance(outcome.top_k, bool) or not isinstance(outcome.top_k, int):
        raise ValueError("completed case outcome top_k must be an integer")
    if outcome.top_k < 1:
        raise ValueError("completed case outcome top_k must be positive")
    if not isinstance(outcome.generate, bool):
        raise ValueError("completed case outcome generate must be a boolean")
    if not isinstance(outcome.results, tuple) or not all(
        isinstance(result, SearchResult) for result in outcome.results
    ):
        raise ValueError("completed case outcome results must be SearchResult values")
    if not isinstance(outcome.answer, str) or not isinstance(outcome.error, str):
        raise ValueError("completed case outcome answer and error must be strings")
    if outcome.error not in {"", "evaluation service failed"}:
        raise ValueError("completed case outcome contains an unsafe service error")
    if not isinstance(outcome.analysis, QueryAnalysis):
        raise ValueError("completed case outcome requires QueryAnalysis")
    if not isinstance(outcome.adaptive_trace, Mapping):
        raise ValueError("completed case outcome adaptive_trace must be a mapping")
    if not isinstance(outcome.evidence_check, EvidenceCheck):
        raise ValueError("completed case outcome requires EvidenceCheck")
    _validate_evidence_check(outcome.evidence_check)
    for name, expected_type in (
        ("verification", VerificationResult),
        ("structured_answer", StructuredAnswer),
        ("pre_fallback_answer", StructuredAnswer),
        ("pre_fallback_verification", VerificationResult),
        ("judge_result", JudgeResult),
    ):
        value = getattr(outcome, name)
        if value is not None and not isinstance(value, expected_type):
            raise ValueError(f"completed case outcome {name} has an invalid type")
    if (
        outcome.generation_error is not None
        and outcome.generation_error not in _GENERATION_ERROR_CODES
    ):
        raise ValueError("completed case outcome generation_error is not a stable code")
    if (
        not isinstance(outcome.generation_kind, str)
        or outcome.generation_kind not in _GENERATION_KINDS
    ):
        raise ValueError("completed case outcome generation_kind is unsupported")
    if (outcome.pre_fallback_answer is None) != (
        outcome.pre_fallback_verification is None
    ):
        raise ValueError(
            "pre-fallback answer and verification must be present together"
        )
    if outcome.pre_fallback_verification is not None:
        if outcome.pre_fallback_verification.passed is not False:
            raise ValueError("pre-fallback verification must be rejected")
        if (
            outcome.pre_fallback_answer is None
            or outcome.pre_fallback_answer.answer_mode
            != outcome.pre_fallback_verification.actual_answer_mode
        ):
            raise ValueError("pre-fallback answer and verification mode do not match")
    if not isinstance(outcome.judge_configured, bool):
        raise ValueError("completed case outcome judge_configured must be a boolean")
    if not outcome.generate:
        if outcome.generation_kind != "retrieval_only":
            raise ValueError("retrieval-only outcome has an invalid generation kind")
        if outcome.answer:
            raise ValueError("retrieval-only outcome cannot contain an answer")
        if any(
            value is not None
            for value in (
                outcome.verification,
                outcome.structured_answer,
                outcome.pre_fallback_answer,
                outcome.pre_fallback_verification,
                outcome.generation_error,
                outcome.judge_result,
            )
        ):
            raise ValueError("retrieval-only outcome contains generation-stage facts")
    if outcome.error:
        expected_kind = "service_error" if outcome.generate else "retrieval_only"
        if outcome.generation_kind != expected_kind:
            raise ValueError("service-error outcome has an invalid generation kind")
        if outcome.answer or outcome.results:
            raise ValueError("service-error outcome cannot contain answer or results")
        if any(
            value is not None
            for value in (
                outcome.verification,
                outcome.structured_answer,
                outcome.pre_fallback_answer,
                outcome.pre_fallback_verification,
                outcome.generation_error,
                outcome.judge_result,
            )
        ):
            raise ValueError(
                "service-error outcome contains completed downstream facts"
            )
    if outcome.generation_error is not None:
        if not outcome.generate or outcome.error:
            raise ValueError(
                "generation error requires a generated non-service outcome"
            )
        if outcome.judge_result is not None:
            raise ValueError("generation-error outcome cannot contain a JudgeResult")
        if outcome.pre_fallback_answer is not None:
            raise ValueError("generation-error outcome cannot contain a rejected draft")
        if outcome.generation_kind != "generation_error":
            raise ValueError("generation-error outcome has an invalid generation kind")
    elif outcome.generation_kind == "generation_error":
        raise ValueError("generation-error kind requires a generation error code")
    if (
        outcome.generate
        and not outcome.error
        and outcome.generation_error is None
        and outcome.generation_kind not in {"model", *_PROGRAMMATIC_GENERATION_KINDS}
    ):
        raise ValueError("generated outcome has an invalid generation kind")
    if (
        outcome.generation_kind in _PROGRAMMATIC_GENERATION_KINDS
        and outcome.pre_fallback_answer is not None
    ):
        raise ValueError("programmatic outcome cannot contain a rejected model draft")
    if outcome.pre_fallback_answer is not None and outcome.generation_kind != "model":
        raise ValueError("rejected model draft requires model generation kind")
    if outcome.generate and not outcome.error and outcome.verification is None:
        raise ValueError("generated outcome requires completed verification")
    if (
        outcome.generate
        and not outcome.error
        and not outcome.generation_error
        and outcome.judge_configured
        and outcome.judge_result is None
    ):
        raise ValueError("configured judge requires a completed JudgeResult")
    if not outcome.judge_configured and outcome.judge_result is not None:
        raise ValueError("unconfigured judge cannot have a JudgeResult")
    if isinstance(outcome.latency_ms, bool) or not isinstance(outcome.latency_ms, int):
        raise ValueError("completed case outcome latency_ms must be an integer")
    if outcome.latency_ms < 0:
        raise ValueError("completed case outcome latency_ms must be non-negative")
    for name in ("assistant_usage", "normalizer_usage", "judge_usage"):
        usage = getattr(outcome, name)
        if not isinstance(usage, ModelUsageDelta):
            raise ValueError(f"completed case outcome {name} is invalid")
        ModelUsageDelta.from_mapping(asdict(usage))
    if not outcome.generate and outcome.assistant_usage.calls:
        raise ValueError("retrieval-only outcome cannot contain assistant calls")
    if outcome.judge_result is None and outcome.judge_usage.calls:
        raise ValueError("outcome without a JudgeResult cannot contain judge calls")
    if outcome.trace_metadata is not None and not isinstance(
        outcome.trace_metadata, Mapping
    ):
        raise ValueError("completed case outcome trace_metadata must be a mapping")
    if outcome.judge_result is not None and outcome.judge_result.status == "succeeded":
        if any(
            value is None
            for value in (
                outcome.judge_result.faithfulness,
                outcome.judge_result.relevance,
                outcome.judge_result.completeness,
                outcome.judge_result.passed,
            )
        ):
            raise ValueError("successful judge outcome requires complete scores")
        for name in ("faithfulness", "relevance", "completeness"):
            value = getattr(outcome.judge_result, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0.0 <= float(value) <= 1.0
            ):
                raise ValueError(f"successful judge outcome has invalid {name}")
        if not isinstance(outcome.judge_result.passed, bool):
            raise ValueError("successful judge outcome passed must be a boolean")
        expected_pass = (
            float(outcome.judge_result.faithfulness) >= 0.7
            and float(outcome.judge_result.relevance) >= 0.7
        )
        if outcome.judge_result.passed is not expected_pass:
            raise ValueError("successful judge outcome passed conflicts with scores")
        if outcome.judge_result.error or outcome.judge_result.error_code is not None:
            raise ValueError("successful judge outcome cannot contain an error")
    elif outcome.judge_result is not None:
        if outcome.judge_result.status != "error":
            raise ValueError("judge outcome status is unsupported")
        if any(
            value is not None
            for value in (
                outcome.judge_result.faithfulness,
                outcome.judge_result.relevance,
                outcome.judge_result.completeness,
                outcome.judge_result.passed,
            )
        ):
            raise ValueError("failed judge outcome cannot contain quality scores")
        if outcome.judge_result.error_code not in _JUDGE_ERROR_CODES:
            raise ValueError("failed judge outcome error_code is not a stable code")
        if not _safe_judge_error(outcome.judge_result.error):
            raise ValueError("failed judge outcome contains an unsafe error message")


def _safe_judge_error(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if value in _SAFE_JUDGE_ERRORS:
        return True
    prefix = "judge response has invalid score fields: "
    if not value.startswith(prefix):
        return False
    fields = value.removeprefix(prefix).split(", ")
    return bool(fields) and set(fields) <= {
        "faithfulness",
        "relevance",
        "completeness",
    }


def _validate_evidence_check(value: EvidenceCheck) -> None:
    if not isinstance(value.sufficient, bool):
        raise ValueError("evidence_check.sufficient must be a boolean")
    for name in (
        "missing_facts",
        "missing_law_support",
        "low_coverage",
        "followup_queries",
        "covered_laws",
        "covered_articles",
    ):
        items = getattr(value, name)
        if not isinstance(items, list) or not all(
            isinstance(item, str) for item in items
        ):
            raise ValueError(f"evidence_check.{name} must be a list of strings")
    if not isinstance(value.stop_reason, str) or not value.stop_reason.strip():
        raise ValueError("evidence_check.stop_reason must be a non-empty string")
    if (
        isinstance(value.checked_result_count, bool)
        or not isinstance(value.checked_result_count, int)
        or value.checked_result_count < 0
    ):
        raise ValueError("evidence_check.checked_result_count must be non-negative")
