from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import fields
from typing import Any

from .evaluation_contracts import validate_eval_case
from .experiment_runtime import canonical_json_bytes
from .json_utils import validate_json_unicode
from .models import (
    ANSWER_MODES,
    EVALUATION_METRICS_SCHEMA_VERSION,
    SEMANTIC_SUPPORT_STATUSES,
    Chunk,
    EvalCase,
    EvalRecord,
    SearchResult,
)


EVALUATION_ARTIFACT_SCHEMA_VERSION = 1
_STAGE_STATUSES = frozenset({"succeeded", "degraded", "error", "not_run"})
_EXECUTION_STAGES = frozenset({"service", "generation", "verification", "judge"})
_STAGE_ALLOWED_STATUSES = {
    "service": frozenset({"succeeded", "degraded", "error"}),
    "generation": frozenset({"succeeded", "error", "not_run"}),
    "verification": frozenset({"succeeded", "error", "not_run"}),
    "judge": frozenset({"succeeded", "error", "not_run"}),
}
_CANONICAL_METRICS = frozenset(
    {
        "answer_text",
        "hit_at_3",
        "hit_at_5",
        "mrr",
        "target_coverage",
        "retrieval_target_hit",
        "citation_hit",
        "keyword_coverage",
        "schema_valid",
        "evidence_catalog_valid",
        "source_ids_exist",
        "citation_ids_valid",
        "citation_alignment_valid",
        "evidence_scope_valid",
        "citation_valid",
        "disclaimer_present",
        "response_mode_valid",
        "verifier_pass",
        "semantic_support_status",
        "response_mode_correct",
        "refusal_recall_hit",
        "refusal_correctness",
        "over_refusal",
        "judge_faithfulness",
        "judge_relevance",
        "judge_completeness",
        "judge_pass",
    }
)
_BOOLEAN_METRICS = frozenset(
    {
        "schema_valid",
        "evidence_catalog_valid",
        "source_ids_exist",
        "citation_ids_valid",
        "citation_alignment_valid",
        "evidence_scope_valid",
        "citation_valid",
        "disclaimer_present",
        "response_mode_valid",
        "verifier_pass",
        "response_mode_correct",
        "refusal_recall_hit",
        "refusal_correctness",
        "over_refusal",
        "judge_pass",
    }
)
_BINARY_INTEGER_METRICS = frozenset(
    {"hit_at_3", "hit_at_5", "retrieval_target_hit", "citation_hit"}
)
_UNIT_INTERVAL_METRICS = frozenset(
    {
        "mrr",
        "target_coverage",
        "keyword_coverage",
        "judge_faithfulness",
        "judge_relevance",
        "judge_completeness",
    }
)


def _json_copy(value: Any) -> Any:
    try:
        return json.loads(canonical_json_bytes(value).decode("utf-8"))
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("evaluation artifact must contain strict JSON values") from exc


def _exact_mapping(
    name: str,
    value: Any,
    expected_fields: set[str] | frozenset[str],
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(expected_fields):
        raise ValueError(f"{name} fields are invalid")
    copied = _json_copy(dict(value))
    if not isinstance(copied, dict):  # pragma: no cover - guarded by Mapping
        raise ValueError(f"{name} must be an object")
    return copied


def _object(name: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    copied = _json_copy(dict(value))
    if not isinstance(copied, dict):  # pragma: no cover - guarded by Mapping
        raise ValueError(f"{name} must be an object")
    return copied


def _string(name: str, value: Any, *, non_empty: bool = False) -> str:
    if not isinstance(value, str) or (non_empty and not value.strip()):
        qualifier = "a non-empty string" if non_empty else "a string"
        raise ValueError(f"{name} must be {qualifier}")
    validate_json_unicode(value)
    return value


def _optional_string(name: str, value: Any) -> str | None:
    if value is None:
        return None
    return _string(name, value)


def _integer(
    name: str,
    value: Any,
    *,
    minimum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _number(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _string_list(name: str, value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    return [_string(f"{name}[{index}]", item) for index, item in enumerate(value)]


def _integer_list(name: str, value: Any) -> list[int]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    return [
        _integer(f"{name}[{index}]", item, minimum=1)
        for index, item in enumerate(value)
    ]


def _artifact_payload(name: str, artifact: Any, payload_field: str) -> dict[str, Any]:
    envelope = _exact_mapping(
        name,
        artifact,
        {"artifact_schema_version", payload_field},
    )
    schema_version = envelope["artifact_schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != EVALUATION_ARTIFACT_SCHEMA_VERSION
    ):
        raise ValueError(f"{name} schema is unsupported")
    payload = envelope[payload_field]
    if not isinstance(payload, dict):
        raise ValueError(f"{name}.{payload_field} must be an object")
    return payload


def eval_case_to_artifact(case: EvalCase) -> dict[str, Any]:
    validate_eval_case(case)
    if case.expected_behavior not in ANSWER_MODES:
        raise ValueError("evaluation artifacts require explicit expected_behavior")
    payload = {field.name: getattr(case, field.name) for field in fields(EvalCase)}
    artifact = {
        "artifact_schema_version": EVALUATION_ARTIFACT_SCHEMA_VERSION,
        "case": payload,
    }
    eval_case_from_artifact(artifact)
    return _json_copy(artifact)


def eval_case_from_artifact(artifact: Mapping[str, Any]) -> EvalCase:
    payload = _artifact_payload("evaluation case artifact", artifact, "case")
    expected_fields = {field.name for field in fields(EvalCase)}
    case_payload = _exact_mapping("evaluation case", payload, expected_fields)
    case_payload["case_id"] = _string(
        "case_id", case_payload["case_id"], non_empty=True
    )
    case_payload["question"] = _string(
        "question", case_payload["question"], non_empty=True
    )
    case_payload["case_type"] = _string(
        "case_type", case_payload["case_type"], non_empty=True
    )
    case_payload["expected_law"] = _string("expected_law", case_payload["expected_law"])
    case_payload["expected_articles"] = _string_list(
        "expected_articles", case_payload["expected_articles"]
    )
    case_payload["keywords"] = _string_list("keywords", case_payload["keywords"])
    expected_behavior = _string(
        "expected_behavior", case_payload["expected_behavior"], non_empty=True
    )
    if expected_behavior not in ANSWER_MODES:
        raise ValueError("expected_behavior is unsupported")
    case_payload["expected_behavior"] = expected_behavior
    session_group = _optional_string("session_group", case_payload["session_group"])
    if session_group is not None and not session_group.strip():
        raise ValueError("session_group must be non-empty when present")
    case_payload["session_group"] = session_group
    case_payload["turn_index"] = _integer(
        "turn_index", case_payload["turn_index"], minimum=0
    )
    case_payload["schema_version"] = _integer(
        "schema_version", case_payload["schema_version"], minimum=1
    )
    case = EvalCase(**case_payload)
    validate_eval_case(case)
    return case


def search_result_to_artifact(result: SearchResult) -> dict[str, Any]:
    if not isinstance(result, SearchResult):
        raise ValueError("result must be a SearchResult")
    artifact = {
        "artifact_schema_version": EVALUATION_ARTIFACT_SCHEMA_VERSION,
        "search_result": {
            "chunk": {
                field.name: getattr(result.chunk, field.name) for field in fields(Chunk)
            },
            "score": result.score,
            "rank": result.rank,
            "retriever": result.retriever,
            "trace": result.trace,
        },
    }
    restored = search_result_from_artifact(artifact)
    canonical_artifact = {
        "artifact_schema_version": EVALUATION_ARTIFACT_SCHEMA_VERSION,
        "search_result": {
            "chunk": {
                field.name: getattr(restored.chunk, field.name)
                for field in fields(Chunk)
            },
            "score": restored.score,
            "rank": restored.rank,
            "retriever": restored.retriever,
            "trace": restored.trace,
        },
    }
    return _json_copy(canonical_artifact)


def search_result_from_artifact(artifact: Mapping[str, Any]) -> SearchResult:
    payload = _artifact_payload("search result artifact", artifact, "search_result")
    result_payload = _exact_mapping(
        "search result",
        payload,
        {"chunk", "score", "rank", "retriever", "trace"},
    )
    chunk_payload = _exact_mapping(
        "search result chunk",
        result_payload["chunk"],
        {field.name for field in fields(Chunk)},
    )
    chunk_payload["chunk_id"] = _string(
        "chunk.chunk_id", chunk_payload["chunk_id"], non_empty=True
    )
    chunk_payload["text"] = _string("chunk.text", chunk_payload["text"])
    chunk_payload["law_names"] = _string_list(
        "chunk.law_names", chunk_payload["law_names"]
    )
    chunk_payload["article_numbers"] = _string_list(
        "chunk.article_numbers", chunk_payload["article_numbers"]
    )
    chunk_payload["source_files"] = _string_list(
        "chunk.source_files", chunk_payload["source_files"]
    )
    chunk_payload["line_nos"] = _integer_list(
        "chunk.line_nos", chunk_payload["line_nos"]
    )
    chunk_payload["strategy"] = _string(
        "chunk.strategy", chunk_payload["strategy"], non_empty=True
    )
    chunk_payload["metadata"] = _object("chunk.metadata", chunk_payload["metadata"])
    return SearchResult(
        chunk=Chunk(**chunk_payload),
        score=_number("score", result_payload["score"]),
        rank=_integer("rank", result_payload["rank"], minimum=1),
        retriever=_string("retriever", result_payload["retriever"], non_empty=True),
        trace=_object("trace", result_payload["trace"]),
    )


def _validate_execution(value: Any) -> dict[str, Any]:
    execution = _exact_mapping("evaluation execution", value, _EXECUTION_STAGES)
    for stage in _EXECUTION_STAGES:
        stage_value = _exact_mapping(
            f"evaluation execution.{stage}",
            execution[stage],
            {"status", "reason"},
        )
        status = _string(
            f"evaluation execution.{stage}.status",
            stage_value["status"],
            non_empty=True,
        )
        if status not in _STAGE_STATUSES:
            raise ValueError(f"evaluation execution.{stage}.status is unsupported")
        if status not in _STAGE_ALLOWED_STATUSES[stage]:
            raise ValueError(
                f"evaluation execution.{stage}.status is invalid for this stage"
            )
        reason = _optional_string(
            f"evaluation execution.{stage}.reason", stage_value["reason"]
        )
        if status == "succeeded" and reason is not None:
            raise ValueError(f"successful {stage} execution cannot contain a reason")
        if status != "succeeded" and (reason is None or not reason.strip()):
            raise ValueError(f"non-successful {stage} execution requires a reason")
        execution[stage] = {"status": status, "reason": reason}
    return execution


def _validate_canonical_metrics(value: Any) -> dict[str, dict[str, Any]]:
    metrics = _object("canonical_metrics", value)
    if set(metrics) != _CANONICAL_METRICS:
        raise ValueError("canonical_metrics fields are invalid")
    normalized: dict[str, dict[str, Any]] = {}
    for name, metric in metrics.items():
        _string("canonical metric name", name, non_empty=True)
        item = _exact_mapping(
            f"canonical_metrics.{name}",
            metric,
            {"value", "unavailable_reason"},
        )
        reason = _optional_string(
            f"canonical_metrics.{name}.unavailable_reason",
            item["unavailable_reason"],
        )
        metric_value = item["value"]
        if metric_value is None:
            if reason is None or not reason.strip():
                raise ValueError(
                    f"canonical_metrics.{name} requires an unavailable reason"
                )
        elif reason is not None:
            raise ValueError(
                f"canonical_metrics.{name} cannot have a value and unavailable reason"
            )
        elif name == "answer_text":
            metric_value = _string(f"canonical_metrics.{name}.value", metric_value)
        elif name in _BOOLEAN_METRICS:
            if not isinstance(metric_value, bool):
                raise ValueError(f"canonical_metrics.{name}.value must be a boolean")
        elif name in _BINARY_INTEGER_METRICS:
            metric_value = _integer(f"canonical_metrics.{name}.value", metric_value)
            if metric_value not in {0, 1}:
                raise ValueError(f"canonical_metrics.{name}.value must be 0 or 1")
        elif name in _UNIT_INTERVAL_METRICS:
            metric_value = _number(f"canonical_metrics.{name}.value", metric_value)
            if not 0.0 <= metric_value <= 1.0:
                raise ValueError(
                    f"canonical_metrics.{name}.value must be between 0 and 1"
                )
        elif name == "semantic_support_status":
            metric_value = _string(
                f"canonical_metrics.{name}.value", metric_value, non_empty=True
            )
            if metric_value not in SEMANTIC_SUPPORT_STATUSES:
                raise ValueError(
                    "canonical_metrics.semantic_support_status.value is unsupported"
                )
        else:  # pragma: no cover - exact metric names are classified above
            raise ValueError(f"canonical_metrics.{name} has no value contract")
        normalized[name] = {
            "value": _json_copy(metric_value),
            "unavailable_reason": reason,
        }
    return normalized


def _validate_generation_attempt(value: Any) -> dict[str, Any]:
    attempt = _object("generation_attempt", value)
    allowed = {"attempted", "status", "reason", "answer_mode", "verification"}
    if "attempted" not in attempt or set(attempt) - allowed:
        raise ValueError("generation_attempt fields are invalid")
    if not isinstance(attempt["attempted"], bool):
        raise ValueError("generation_attempt.attempted must be a boolean")
    if set(attempt) == {"attempted", "reason"}:
        if attempt != {"attempted": False, "reason": "retrieval_only"}:
            raise ValueError("minimal generation_attempt must represent retrieval-only")
        return attempt
    if set(attempt) != allowed:
        raise ValueError("generation_attempt fields are invalid")
    status = _string("generation_attempt.status", attempt["status"], non_empty=True)
    if status not in {"accepted", "rejected", "error", "not_run"}:
        raise ValueError("generation_attempt.status is unsupported")
    reason = _optional_string("generation_attempt.reason", attempt["reason"])
    if reason is not None and not reason.strip():
        raise ValueError("generation_attempt.reason must be non-empty when present")
    if status == "accepted":
        if not attempt["attempted"] or reason is not None:
            raise ValueError("accepted generation_attempt has inconsistent state")
    elif status == "rejected":
        if not attempt["attempted"] or reason is None:
            raise ValueError("rejected generation_attempt has inconsistent state")
    elif status == "not_run":
        if attempt["attempted"] or reason is None:
            raise ValueError("not_run generation_attempt has inconsistent state")
    elif reason is None:
        raise ValueError("error generation_attempt requires a reason")
    answer_mode = _optional_string(
        "generation_attempt.answer_mode", attempt["answer_mode"]
    )
    if answer_mode is not None and answer_mode not in ANSWER_MODES:
        raise ValueError("generation_attempt.answer_mode is unsupported")
    verification = attempt["verification"]
    if verification is not None:
        verification = _object("generation_attempt.verification", verification)
    if status == "rejected" and verification is None:
        raise ValueError("rejected generation_attempt requires verification")
    if status != "rejected" and verification is not None:
        raise ValueError("only a rejected generation_attempt may contain verification")
    attempt.update(
        {
            "status": status,
            "reason": reason,
            "answer_mode": answer_mode,
            "verification": verification,
        }
    )
    return attempt


def _canonical_value(record: dict[str, Any], name: str) -> Any:
    return record["canonical_metrics"][name]["value"]


def _validate_record_consistency(record: dict[str, Any]) -> None:
    legacy_pairs = {
        "hit_at_3": "hit_at_3",
        "hit_at_5": "hit_at_5",
        "mrr": "mrr",
        "target_coverage": "target_coverage",
        "citation_hit": "citation_hit",
        "keyword_coverage": "keyword_coverage",
        "citation_valid": "citation_valid",
        "verifier_pass": "verifier_pass",
        "refusal_correctness": "refusal_correctness",
        "judge_faithfulness": "judge_faithfulness",
        "judge_relevance": "judge_relevance",
        "judge_completeness": "judge_completeness",
        "judge_pass": "judge_pass",
    }
    for metric_name, legacy_name in legacy_pairs.items():
        value = _canonical_value(record, metric_name)
        if value is not None and record[legacy_name] != value:
            raise ValueError(
                f"canonical metric {metric_name} conflicts with legacy {legacy_name}"
            )

    if (
        record["canonical_metrics"]["retrieval_target_hit"]
        != record["canonical_metrics"]["citation_hit"]
    ):
        raise ValueError("retrieval_target_hit and citation_hit must match")
    if (
        record["canonical_metrics"]["refusal_recall_hit"]
        != record["canonical_metrics"]["refusal_correctness"]
    ):
        raise ValueError("refusal recall compatibility metrics must match")

    for metric_name, legacy_name, sentinel in (
        ("keyword_coverage", "keyword_coverage", -1.0),
        ("citation_valid", "citation_valid", -1),
        ("verifier_pass", "verifier_pass", -1),
        ("refusal_correctness", "refusal_correctness", -1),
        ("judge_faithfulness", "judge_faithfulness", -1.0),
        ("judge_relevance", "judge_relevance", -1.0),
        ("judge_completeness", "judge_completeness", -1.0),
        ("judge_pass", "judge_pass", -1),
    ):
        if (
            _canonical_value(record, metric_name) is None
            and record[legacy_name] != sentinel
        ):
            raise ValueError(
                f"unavailable canonical metric {metric_name} requires legacy sentinel"
            )

    judge_succeeded = record["execution"]["judge"]["status"] == "succeeded"
    judge_metric_names = {
        "judge_faithfulness",
        "judge_relevance",
        "judge_completeness",
        "judge_pass",
    }
    judge_available = {
        _canonical_value(record, metric_name) is not None
        for metric_name in judge_metric_names
    }
    if judge_available != {judge_succeeded}:
        raise ValueError("judge execution status conflicts with judge metrics")
    if judge_succeeded and record["judge_error"]:
        raise ValueError("successful judge execution cannot contain judge_error")

    generation_status = record["execution"]["generation"]["status"]
    attempt_status = record["generation_attempt"].get("status")
    if generation_status == "not_run" and attempt_status not in {None, "not_run"}:
        raise ValueError("generation execution conflicts with generation_attempt")
    if generation_status == "error" and attempt_status != "error":
        raise ValueError("generation execution conflicts with generation_attempt")
    if generation_status == "succeeded" and attempt_status not in {
        "accepted",
        "rejected",
    }:
        raise ValueError("generation execution conflicts with generation_attempt")

    total_calls = (
        record["assistant_llm_calls"]
        + record["normalizer_llm_calls"]
        + record["judge_llm_calls"]
    )
    if record["llm_failed_calls"] > total_calls:
        raise ValueError("llm_failed_calls cannot exceed total LLM calls")
    if record["token_usage_calls"] > total_calls:
        raise ValueError("token_usage_calls cannot exceed total LLM calls")
    if record["token_usage_calls"] == 0 and any(
        record[name] for name in ("input_tokens", "output_tokens", "total_tokens")
    ):
        raise ValueError("tokens require at least one token usage record")
    if record["total_tokens"] < record["input_tokens"] + record["output_tokens"]:
        raise ValueError("total_tokens cannot be less than input plus output tokens")


def eval_record_to_artifact(record: EvalRecord) -> dict[str, Any]:
    if not isinstance(record, EvalRecord):
        raise ValueError("record must be an EvalRecord")
    artifact = {
        "artifact_schema_version": EVALUATION_ARTIFACT_SCHEMA_VERSION,
        "evaluation_record": {
            field.name: getattr(record, field.name) for field in fields(EvalRecord)
        },
    }
    eval_record_from_artifact(artifact)
    return _json_copy(artifact)


def eval_record_from_artifact(artifact: Mapping[str, Any]) -> EvalRecord:
    payload = _artifact_payload(
        "evaluation record artifact", artifact, "evaluation_record"
    )
    expected_fields = {field.name for field in fields(EvalRecord)}
    record = _exact_mapping("evaluation record", payload, expected_fields)

    non_empty_strings = {"case_id", "case_type", "model", "retriever", "chunk_strategy"}
    string_fields = {
        "case_id",
        "case_type",
        "model",
        "retriever",
        "chunk_strategy",
        "answer",
        "sources",
        "error",
        "failure_label",
        "failure_reason",
        "judge_comment",
        "judge_error",
        "expected_behavior",
    }
    for name in string_fields:
        record[name] = _string(name, record[name], non_empty=name in non_empty_strings)

    integer_fields = {
        "hit_at_3",
        "hit_at_5",
        "citation_hit",
        "sufficiency_pass",
        "citation_valid",
        "verifier_pass",
        "refusal_correctness",
        "latency_ms",
        "judge_pass",
        "assistant_llm_calls",
        "normalizer_llm_calls",
        "judge_llm_calls",
        "llm_failed_calls",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "token_usage_calls",
        "metrics_schema_version",
    }
    for name in integer_fields:
        record[name] = _integer(name, record[name])
    for name in {"hit_at_3", "hit_at_5", "citation_hit", "sufficiency_pass"}:
        if record[name] not in {0, 1}:
            raise ValueError(f"{name} must be 0 or 1")
    for name in {
        "citation_valid",
        "verifier_pass",
        "refusal_correctness",
        "judge_pass",
    }:
        if record[name] not in {-1, 0, 1}:
            raise ValueError(f"{name} must be -1, 0, or 1")
    for name in {
        "latency_ms",
        "assistant_llm_calls",
        "normalizer_llm_calls",
        "judge_llm_calls",
        "llm_failed_calls",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "token_usage_calls",
    }:
        if record[name] < 0:
            raise ValueError(f"{name} must be non-negative")
    if record["metrics_schema_version"] != EVALUATION_METRICS_SCHEMA_VERSION:
        raise ValueError("evaluation record metrics schema is unsupported")

    for name in {
        "mrr",
        "target_coverage",
        "keyword_coverage",
        "judge_faithfulness",
        "judge_relevance",
        "judge_completeness",
        "llm_latency_ms",
    }:
        record[name] = _number(name, record[name])
    for name in {
        "mrr",
        "target_coverage",
        "keyword_coverage",
        "judge_faithfulness",
        "judge_relevance",
        "judge_completeness",
    }:
        if record[name] != -1.0 and not 0.0 <= record[name] <= 1.0:
            raise ValueError(f"{name} must be -1 or between 0 and 1")
    if record["llm_latency_ms"] < 0:
        raise ValueError("llm_latency_ms must be non-negative")

    expected_behavior = record["expected_behavior"]
    if expected_behavior not in ANSWER_MODES:
        raise ValueError("evaluation record expected_behavior is unsupported")
    observed = _optional_string("observed_answer_mode", record["observed_answer_mode"])
    if observed is not None and observed not in ANSWER_MODES:
        raise ValueError("evaluation record observed_answer_mode is unsupported")
    record["observed_answer_mode"] = observed
    record["execution"] = _validate_execution(record["execution"])
    record["canonical_metrics"] = _validate_canonical_metrics(
        record["canonical_metrics"]
    )
    record["generation_attempt"] = _validate_generation_attempt(
        record["generation_attempt"]
    )
    _validate_record_consistency(record)
    return EvalRecord(**record)
