"""Bridge trusted execution fields from existing local and M2 traces.

Detailed traces may contain questions and private artifact metadata. Only the
known execution fields below enter typed observations. Persisted M2 results
must already have passed their store/runtime validation, and must represent a
newly executed attempt, rather than a reconciled historical completion.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from .events import ERROR_CATEGORIES, Observation, ObservationContext, Observer

if TYPE_CHECKING:
    from legal_rag.experiment_store import ExperimentStore


_STAGES = {
    "query_analysis": ("analyze_query", None),
    "query_embedding": ("embedded", "embedding"),
    "retrieval": ("retrieve", "retriever"),
    "rerank": ("rerank", "reranker"),
    "generation": ("generate", None),
    "verification": ("verify", "verifier"),
    "judge": ("judge", "judge"),
}
_MODEL_CALL_KINDS = ("normalizer", "generation", "rerank", "judge")
_ERROR_MAP = {
    "timeout": "provider_timeout",
    "transport_error": "provider_error",
    "generation_error": "provider_error",
    "judge_error": "provider_error",
    "invalid_json": "validation_failed",
    "invalid_schema": "validation_failed",
    "executor_contract_error": "validation_failed",
    "replay_cache_miss": "validation_failed",
}


def _object(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: Any) -> float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        return None
    return float(value)


def _count(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def _error_category(value: Any) -> str:
    if isinstance(value, str):
        if value in ERROR_CATEGORIES:
            return value
        return _ERROR_MAP.get(value, "other")
    return "other"


def _record(observer: Observer, event: Observation) -> None:
    try:
        observer.record(event)
    except Exception:  # noqa: BLE001 - an optional sink cannot alter execution.
        return


def _evidence_ids(trace: Mapping[str, Any]) -> tuple[str, ...]:
    results = trace.get("results")
    if not isinstance(results, (list, tuple)):
        return ()
    values: list[str] = []
    for result in results:
        value = _object(result).get("chunk_id")
        if (
            isinstance(value, str)
            and 0 < len(value) <= 255
            and not any(
                ord(character) < 32 or ord(character) == 127 for character in value
            )
            and value not in values
        ):
            values.append(value)
            if len(values) == 100:
                break
    return tuple(values)


def _correlation_id(prefix: str, *values: object) -> str:
    encoded = json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode(
        "ascii"
    )
    return prefix + hashlib.sha256(encoded).hexdigest()


def _attempt_context(
    context: ObservationContext,
    result: Mapping[str, Any],
    attempt_number: int,
) -> ObservationContext:
    case = _object(result.get("case"))
    case_id = case.get("case_id")
    session_group = case.get("session_group")
    # Use the validated runner case identity, never trace metadata or text.
    run_id = context.run_id
    if run_id is None and isinstance(case_id, str) and case_id:
        run_id = _correlation_id(
            "m2-run-", context.experiment_id, context.trace_id, case_id, attempt_number
        )
    session_id = context.session_id
    if session_id is None and isinstance(session_group, str) and session_group:
        session_id = _correlation_id(
            "m2-session-", context.experiment_id, context.trace_id, session_group
        )
    return replace(context, run_id=run_id, session_id=session_id)


def observe_retrieval_trace(
    observer: Observer | None,
    *,
    context: ObservationContext,
    record: Mapping[str, Any],
) -> None:
    """Record the available whole-case facts from a detailed local trace.

    The existing latency is an end-to-end case measurement. It does not prove
    individual node durations, cache lookups, provider dispatch, usage, or cost.
    """

    if observer is None:
        return
    try:
        execution = _object(record.get("execution"))
        service = _object(execution.get("service"))
        failed = service.get("status") == "error"
        _record(
            observer,
            Observation(
                context=context,
                name="evaluation.case",
                status="failed" if failed else "succeeded",
                node="evaluation",
                tool="evaluation",
                duration_ms=_number(record.get("latency_ms")),
                evidence_ids=_evidence_ids(record),
                error_category="other" if failed else None,
            ),
        )
    except Exception:  # noqa: BLE001 - trace compatibility takes precedence.
        return


def observe_evaluation_attempt(
    observer: Observer | None,
    *,
    context: ObservationContext,
    result: Mapping[str, Any],
    attempt_number: int,
    cache_mode: str,
) -> None:
    """Publish facts from one newly executed, validated M2 attempt.

    Job handlers own the evaluation.case envelope. This bridge emits only stage,
    tool, cache and current model dispatch facts. Reused source calls never enter
    current counts or budget. M2's normalized usage lacks per-component reporting
    coverage, so its token totals cannot prove complete input/output usage and
    are deliberately not copied into observations.
    """

    if observer is None:
        return
    try:
        if type(attempt_number) is not int or attempt_number < 1:
            return
        if cache_mode not in {"fresh", "cache", "replay"}:
            return
        context = _attempt_context(context, result, attempt_number)
        output = _object(result.get("output"))
        trace = _object(output.get("trace_record"))
        evidence_ids = _evidence_ids(trace)
        stages = _object(result.get("stage_observations"))
        for stage_name, (node, tool) in _STAGES.items():
            stage = _object(stages.get(stage_name))
            stage_status = stage.get("status")
            origin = stage.get("origin")
            if stage_status not in {"succeeded", "error"}:
                continue
            if origin not in {"fresh", "cache", "replay"}:
                continue
            cached = origin in {"cache", "replay"}
            status = (
                "failed"
                if stage_status == "error"
                else "skipped"
                if cached
                else "succeeded"
            )
            duration = _number(stage.get("duration_ms"))
            cache_key = stage.get("cache_key")
            cache_status = (
                (
                    "error"
                    if cached and stage_status == "error"
                    else "hit"
                    if cached
                    else "bypass"
                    if cache_mode == "fresh"
                    else "miss"
                )
                if isinstance(cache_key, str)
                else None
            )
            # Only explicit current attempts count; cache source history is
            # neither a current provider dispatch nor current budget usage.
            calls = _object(stage.get("external_calls")) if not cached else {}
            model_calls = sum(_count(calls.get(kind)) for kind in _MODEL_CALL_KINDS)
            embedding_calls = _count(calls.get("embedding"))
            budget = {
                "model_attempts": model_calls,
                "embedding_attempts": embedding_calls,
            }
            stage_evidence = (
                evidence_ids
                if stage_name
                in {"retrieval", "rerank", "generation", "verification", "judge"}
                else ()
            )
            facts = {
                "context": context,
                "node": node,
                "retry_count": attempt_number - 1,
                "cache_status": cache_status,
                "evidence_ids": stage_evidence,
                "error_category": _error_category(stage.get("error_code"))
                if stage_status == "error"
                else None,
            }
            _record(
                observer,
                Observation(
                    name="node.completed",
                    status=status,
                    duration_ms=None if cached else duration,
                    budget_used=budget,
                    **facts,
                ),
            )
            if tool is not None:
                _record(
                    observer,
                    Observation(
                        name="tool.completed",
                        status=status,
                        tool=tool,
                        duration_ms=None if cached else duration,
                        **facts,
                    ),
                )
            if cache_status in {"hit", "miss", "error"}:
                _record(
                    observer,
                    Observation(
                        name="cache.lookup",
                        status="failed" if cache_status == "error" else "succeeded",
                        duration_ms=duration if cached else None,
                        counts={}
                        if cache_status == "error"
                        else {"cache_hits" if cached else "cache_misses": 1},
                        **facts,
                    ),
                )
            if model_calls:
                _record(
                    observer,
                    Observation(
                        name="model.completed",
                        status="failed" if stage_status == "error" else "succeeded",
                        counts={"model_calls": model_calls},
                        budget_used={"model_attempts": model_calls},
                        **facts,
                    ),
                )
    except Exception:  # noqa: BLE001 - observation is not an execution gate.
        return


def observe_stored_evaluation_attempt(
    observer: Observer | None,
    *,
    context: ObservationContext,
    store: ExperimentStore,
    case_id: str,
) -> None:
    """Validate linked M2 runtime facts before observing a new completion.

    Store integrity proves envelopes and completion links, while the runner owns
    stage, ledger and session-chain validation. The output decoder separately
    recomputes the detailed trace from its scoring facts. Both are required here.
    The caller must invoke this only for execution performed in this invocation;
    historical completion reconciliation must not replay execution observations.
    """

    if observer is None:
        return
    try:
        from legal_rag.experiment_adapter import decode_evaluation_output
        from legal_rag.experiment_runner import (
            plan_work_units,
            validate_persisted_work_unit_history,
        )

        manifest = store.load_manifest()
        experiment_id = manifest["experiment_id"]
        if context.experiment_id not in {None, experiment_id}:
            return
        context = replace(context, experiment_id=experiment_id)
        completed = store.load_completed(case_id)
        unit = next(
            unit for unit in plan_work_units(manifest) if case_id in unit.case_ids
        )
        history = validate_persisted_work_unit_history(
            unit,
            {known_id: store.load_attempts(known_id) for known_id in unit.case_ids},
        )
        case_history = next(
            item for item in history.case_histories if item.case_id == case_id
        )
        attempt = case_history.attempts[-1]
        if (
            attempt.status != "succeeded"
            or attempt.attempt_number != completed["attempt"]
        ):
            return
        output = attempt.output
        if output is None:
            return
        case = next(case for case in unit.cases if case["case_id"] == case_id)
        evaluated = decode_evaluation_output(
            output, expected_manifest=manifest, expected_case=case
        )
        output_stages = output["identity"]["stages"]
        for name, stage in attempt.stage_observations.items():
            if (
                output_stages[name]["status"] != stage["status"]
                or output_stages[name]["cache_key"] != stage["cache_key"]
            ):
                return
        for role, fact_name in {
            "assistant": "assistant_usage",
            "normalizer": "normalizer_usage",
            "judge": "judge_usage",
        }.items():
            if output["scoring_facts"][fact_name] != attempt.model_usage[role]:
                return
        if manifest["execution_policy"]["external_calls_allowed"] is False and any(
            item["attempted"] for item in attempt.call_ledger["actual"].values()
        ):
            return
        observe_evaluation_attempt(
            observer,
            context=context,
            result={
                "case": {"case_id": case_id, "session_group": unit.session_group},
                "stage_observations": attempt.stage_observations,
                "output": {"trace_record": evaluated.trace_record},
            },
            attempt_number=attempt.attempt_number,
            cache_mode=attempt.cache_mode,
        )
    except Exception:  # noqa: BLE001 - invalid telemetry cannot change execution.
        return


__all__ = [
    "observe_evaluation_attempt",
    "observe_retrieval_trace",
    "observe_stored_evaluation_attempt",
]
