from __future__ import annotations

import csv
import io
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from .evaluation import summarize_evaluation
from .evaluation_artifacts import eval_record_from_artifact, eval_record_to_artifact
from .experiment_adapter import decode_evaluation_output
from .experiment_runner import (
    MODEL_USAGE_ROLES,
    OBSERVATION_STAGES,
    RunnerContractError,
    ValidatedRunnerAttempt,
    WorkUnit,
    plan_work_units,
    validate_persisted_work_unit_history,
)
from .experiment_runtime import (
    EXTERNAL_CALL_KINDS,
    STAGE_CACHE_SCHEMA_VERSION,
    ExperimentContractError,
    build_stage_cache_key,
    canonical_hash,
    canonical_json_bytes,
    validate_experiment_manifest,
)
from .experiment_store import ArtifactCorruptionError, ExperimentStore


AGGREGATION_SCHEMA_VERSION = 1
AGGREGATION_ROW_SCHEMA_VERSION = 1
CASE_STATUSES = (
    "succeeded",
    "failed",
    "interrupted",
    "exhausted",
    "pending",
    "not_run",
    "corrupt",
)

_NUMERIC_METRICS = frozenset(
    {
        "hit_at_3",
        "hit_at_5",
        "mrr",
        "target_coverage",
        "retrieval_target_hit",
        "citation_hit",
        "keyword_coverage",
        "judge_faithfulness",
        "judge_relevance",
        "judge_completeness",
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
_CATEGORICAL_METRICS = frozenset({"semantic_support_status"})
_TEXT_METRICS = frozenset({"answer_text"})
_CANONICAL_METRICS = tuple(
    sorted(_NUMERIC_METRICS | _BOOLEAN_METRICS | _CATEGORICAL_METRICS | _TEXT_METRICS)
)
_ACTUAL_CALL_FIELDS = (
    "attempted",
    "succeeded",
    "failed",
    "duration_ms",
    "provider_wait_ms",
)
_MODEL_USAGE_FIELDS = (
    "calls",
    "failed_calls",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "token_usage_calls",
    "latency_ms",
)
_CSV_BASE_COLUMNS = (
    "aggregation_row_schema_version",
    "experiment_id",
    "manifest_hash",
    "bundle_hash",
    "case_results_hash",
    "aggregation_key",
    "ordinal",
    "case_id",
    "case_hash",
    "status",
    "store_state",
    "attempt_count",
    "trusted_attempt_count",
    "latest_attempt_status",
    "latest_cache_mode",
    "complete_marker_valid",
    "output_sha256",
    "error_code",
    "problem_codes",
)
_CSV_METRICS = tuple(sorted(_NUMERIC_METRICS | _BOOLEAN_METRICS | _CATEGORICAL_METRICS))
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STORE_STATES = frozenset(
    {"succeeded", "failed", "missing", "corrupt", "not_run", "interrupted", "exhausted"}
)
_CASE_ROW_FIELDS = {
    "ordinal",
    "case_id",
    "case_hash",
    "artifact_evidence_sha256",
    "physical_attempt_file_count",
    "completion_artifact_sha256",
    "attempt_history_readable",
    "status",
    "store_state",
    "attempt_count",
    "trusted_attempt_count",
    "latest_attempt_status",
    "latest_cache_mode",
    "complete_marker_valid",
    "output_sha256",
    "error_code",
    "problems",
    "attempts",
    "evaluation_record",
    "trace_record",
}
_ATTEMPT_ROW_FIELDS = {
    "attempt",
    "attempt_payload_sha256",
    "session_checkpoint_sha256",
    "redacted_environment_sha256",
    "status",
    "cache_mode",
    "recorded_at",
    "environment_fingerprint",
    "stage_observations",
    "call_ledger",
    "model_usage",
    "timings_ms",
    "error",
}
_SENSITIVE_FIELD_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "access_token",
        "refresh_token",
        "auth_token",
        "password",
        "passwd",
        "secret",
        "client_secret",
        "cookie",
        "set_cookie",
        "private_key",
        "credential",
        "credentials",
    }
)
_SENSITIVE_FIELD_SUFFIXES = (
    "_api_key",
    "_token",
    "_access_token",
    "_refresh_token",
    "_auth_token",
    "_session_token",
    "_password",
    "_passwd",
    "_secret",
    "_secret_key",
    "_private_key",
    "_authorization",
    "_cookie",
    "_credential",
    "_credentials",
)


class AggregationContractError(ValueError):
    """Raised when persisted artifacts cannot yield a trustworthy aggregation."""


def _json_copy(value: Any) -> Any:
    return json.loads(canonical_json_bytes(value).decode("utf-8"))


def _redact_sensitive_fields(value: Any) -> Any:
    """Return a JSON copy with common credential-shaped fields removed.

    Persisted source artifacts remain immutable.  This only protects derived
    aggregation projections from accidentally repeating secrets placed in
    free-form execution or trace metadata.
    """

    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized = key.strip().lower().replace("-", "_").replace(" ", "_")
            redacted[key] = (
                "[REDACTED]"
                if normalized in _SENSITIVE_FIELD_NAMES
                or normalized.endswith(_SENSITIVE_FIELD_SUFFIXES)
                else _redact_sensitive_fields(item)
            )
        return redacted
    if isinstance(value, list | tuple):
        return [_redact_sensitive_fields(item) for item in value]
    return _json_copy(value)


def _csv_safe_cell(value: Any) -> Any:
    """Neutralize spreadsheet formula prefixes while preserving CSV data types."""

    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def _round_milliseconds(value: int | float) -> float:
    return round(float(value), 3)


def _zero_actual_calls() -> dict[str, dict[str, int | float]]:
    return {
        kind: {
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "duration_ms": 0.0,
            "provider_wait_ms": 0.0,
        }
        for kind in EXTERNAL_CALL_KINDS
    }


def _zero_source_calls() -> dict[str, int]:
    return {kind: 0 for kind in EXTERNAL_CALL_KINDS}


def _zero_model_usage() -> dict[str, dict[str, int | float]]:
    return {
        role: {
            "calls": 0,
            "failed_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "token_usage_calls": 0,
            "latency_ms": 0.0,
        }
        for role in MODEL_USAGE_ROLES
    }


def _model_usage_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    usage = {field_name: value[field_name] for field_name in _MODEL_USAGE_FIELDS}
    usage["unreported_token_usage_calls"] = usage["calls"] - usage["token_usage_calls"]
    usage["token_usage_complete"] = usage["calls"] == usage["token_usage_calls"]
    usage["complete_token_totals"] = {
        field_name: (usage[field_name] if usage["token_usage_complete"] else None)
        for field_name in ("input_tokens", "output_tokens", "total_tokens")
    }
    return usage


def _problem(
    code: str,
    *,
    source: str,
    detail: str,
    path: str | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "source": source,
        "path": path,
        "detail": detail,
    }


def _store_state_by_case(inventory: Any) -> dict[str, str]:
    categories = {
        "succeeded": inventory.succeeded,
        "failed": inventory.failed,
        "missing": inventory.missing,
        "corrupt": inventory.corrupt,
        "not_run": inventory.not_run,
        "interrupted": inventory.interrupted,
        "exhausted": inventory.exhausted,
    }
    result: dict[str, str] = {}
    for category, case_ids in categories.items():
        for case_id in case_ids:
            if case_id in result:
                raise AggregationContractError(
                    f"artifact inventory classified a case more than once: {case_id}"
                )
            result[case_id] = category
    if len(result) != len(inventory.case_order) or set(result) != set(
        inventory.case_order
    ):
        missing = sorted(set(inventory.case_order) - set(result))
        extra = sorted(set(result) - set(inventory.case_order))
        raise AggregationContractError(
            "artifact inventory is not exhaustive: "
            f"missing={missing!r}, extra={extra!r}"
        )
    return result


def _public_status(store_state: str) -> str:
    if store_state == "missing":
        return "pending"
    if store_state in CASE_STATUSES:
        return store_state
    raise AggregationContractError(f"unsupported store state: {store_state!r}")


def _attempt_row(
    validated: ValidatedRunnerAttempt,
    raw_payload: Mapping[str, Any],
) -> dict[str, Any]:
    checkpoint = (
        validated.session_checkpoint.to_dict()
        if validated.session_checkpoint is not None
        else None
    )
    redacted_environment = _redact_sensitive_fields(
        raw_payload["execution_environment"]
    )
    return {
        "attempt": validated.attempt_number,
        "attempt_payload_sha256": canonical_hash(raw_payload),
        "session_checkpoint_sha256": (
            canonical_hash(checkpoint) if checkpoint is not None else None
        ),
        "redacted_environment_sha256": canonical_hash(redacted_environment),
        "status": validated.status,
        "cache_mode": validated.cache_mode,
        "recorded_at": raw_payload["recorded_at"],
        "environment_fingerprint": raw_payload["environment_fingerprint"],
        "stage_observations": _json_copy(dict(validated.stage_observations)),
        "call_ledger": _json_copy(dict(validated.call_ledger)),
        "model_usage": {
            role: _model_usage_projection(validated.model_usage[role])
            for role in MODEL_USAGE_ROLES
        },
        "timings_ms": _json_copy(dict(validated.timings_ms)),
        "error": _json_copy(validated.error),
    }


def _output_stage_identity_matches_runner(
    output: Mapping[str, Any],
    attempt: ValidatedRunnerAttempt,
) -> bool:
    identity = output.get("identity")
    if not isinstance(identity, Mapping):
        return False
    output_stages = identity.get("stages")
    if not isinstance(output_stages, Mapping):
        return False
    for stage in OBSERVATION_STAGES:
        output_stage = output_stages.get(stage)
        runner_stage = attempt.stage_observations.get(stage)
        if not isinstance(output_stage, Mapping) or not isinstance(
            runner_stage, Mapping
        ):
            return False
        if output_stage.get("status") != runner_stage.get("status"):
            return False
        if output_stage.get("cache_key") != runner_stage.get("cache_key"):
            return False
    return True


def _output_usage_matches_runner(
    output: Mapping[str, Any],
    attempt: ValidatedRunnerAttempt,
) -> bool:
    facts = output.get("scoring_facts")
    if not isinstance(facts, Mapping):
        return False
    fact_names = {
        "assistant": "assistant_usage",
        "normalizer": "normalizer_usage",
        "judge": "judge_usage",
    }
    for role, fact_name in fact_names.items():
        persisted_usage = facts.get(fact_name)
        if not isinstance(persisted_usage, Mapping):
            return False
        if canonical_json_bytes(dict(persisted_usage)) != canonical_json_bytes(
            attempt.model_usage[role]
        ):
            return False
    return True


def _blank_audit(
    *,
    persisted_attempt_count: int,
    untrusted_attempt_count: int = 0,
    untrusted_case_ids: Sequence[str] = (),
    unreadable_case_ids: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "persisted_attempt_count": persisted_attempt_count,
        "trusted_attempt_count": 0,
        "untrusted_attempt_count": untrusted_attempt_count,
        "untrusted_case_ids": list(untrusted_case_ids),
        "unreadable_case_ids": list(unreadable_case_ids),
        "attempt_statuses": {},
        "cache_modes": {},
        "failure_codes": {},
        "actual_calls": {"by_kind": _zero_actual_calls()},
        "source_provenance": {
            "by_kind": _zero_source_calls(),
            "by_origin": {},
            "stage_origins": {stage: {} for stage in OBSERVATION_STAGES},
        },
        "model_usage": _zero_model_usage(),
        "timings_by_cache_mode": {},
        "by_cache_mode": {},
        "environments": {},
    }


def _add_attempt_to_audit(
    audit: dict[str, Any],
    attempt: ValidatedRunnerAttempt,
    raw_payload: Mapping[str, Any],
) -> None:
    audit["trusted_attempt_count"] += 1
    audit["attempt_statuses"][attempt.status] = (
        audit["attempt_statuses"].get(attempt.status, 0) + 1
    )
    audit["cache_modes"][attempt.cache_mode] = (
        audit["cache_modes"].get(attempt.cache_mode, 0) + 1
    )
    if attempt.error is not None:
        code = attempt.error["code"]
        audit["failure_codes"][code] = audit["failure_codes"].get(code, 0) + 1

    mode_summary = audit["by_cache_mode"].setdefault(
        attempt.cache_mode,
        {
            "attempts": 0,
            "attempt_statuses": {},
            "failure_codes": {},
            "actual_calls": _zero_actual_calls(),
            "source_external_calls": _zero_source_calls(),
            "model_usage": _zero_model_usage(),
            "queue_wait_ms": 0.0,
            "end_to_end_ms": 0.0,
        },
    )
    mode_summary["attempts"] += 1
    mode_summary["attempt_statuses"][attempt.status] = (
        mode_summary["attempt_statuses"].get(attempt.status, 0) + 1
    )
    if attempt.error is not None:
        code = attempt.error["code"]
        mode_summary["failure_codes"][code] = (
            mode_summary["failure_codes"].get(code, 0) + 1
        )
    mode_summary["queue_wait_ms"] += attempt.timings_ms["queue_wait"]
    mode_summary["end_to_end_ms"] += attempt.timings_ms["end_to_end"]

    for kind in EXTERNAL_CALL_KINDS:
        actual = attempt.call_ledger["actual"][kind]
        aggregate_actual = audit["actual_calls"]["by_kind"][kind]
        for field_name in _ACTUAL_CALL_FIELDS:
            aggregate_actual[field_name] += actual[field_name]
            mode_summary["actual_calls"][kind][field_name] += actual[field_name]
        audit["source_provenance"]["by_kind"][kind] += attempt.call_ledger["source"][
            kind
        ]
        mode_summary["source_external_calls"][kind] += attempt.call_ledger["source"][
            kind
        ]

    for role in MODEL_USAGE_ROLES:
        usage = attempt.model_usage[role]
        aggregate_usage = audit["model_usage"][role]
        for field_name in _MODEL_USAGE_FIELDS:
            aggregate_usage[field_name] += usage[field_name]
            mode_summary["model_usage"][role][field_name] += usage[field_name]

    cache_timing = audit["timings_by_cache_mode"].setdefault(
        attempt.cache_mode,
        {
            "attempts": 0,
            "queue_wait_ms": 0.0,
            "end_to_end_ms": 0.0,
        },
    )
    cache_timing["attempts"] += 1
    cache_timing["queue_wait_ms"] += attempt.timings_ms["queue_wait"]
    cache_timing["end_to_end_ms"] += attempt.timings_ms["end_to_end"]

    fingerprint = raw_payload["environment_fingerprint"]
    redacted_environment = _redact_sensitive_fields(
        raw_payload["execution_environment"]
    )
    environment = audit["environments"].setdefault(
        fingerprint,
        {
            "execution_environment": redacted_environment,
            "redacted_environment_sha256": canonical_hash(redacted_environment),
            "attempts": 0,
            "queue_wait_ms": 0.0,
            "end_to_end_ms": 0.0,
        },
    )
    if canonical_json_bytes(
        environment["execution_environment"]
    ) != canonical_json_bytes(redacted_environment) or environment[
        "redacted_environment_sha256"
    ] != canonical_hash(redacted_environment):
        raise AggregationContractError(
            "one environment fingerprint identifies different environment summaries"
        )
    environment["attempts"] += 1
    environment["queue_wait_ms"] += attempt.timings_ms["queue_wait"]
    environment["end_to_end_ms"] += attempt.timings_ms["end_to_end"]

    for stage in OBSERVATION_STAGES:
        observation = attempt.stage_observations[stage]
        origin = observation["origin"]
        stage_origins = audit["source_provenance"]["stage_origins"][stage]
        stage_origins[origin] = stage_origins.get(origin, 0) + 1
        origin_payload = audit["source_provenance"]["by_origin"].setdefault(
            origin,
            {
                "stage_observations": 0,
                "timed_stage_observations": 0,
                "duration_ms": 0.0,
                "source_external_calls": _zero_source_calls(),
            },
        )
        origin_payload["stage_observations"] += 1
        if observation["duration_ms"] is not None:
            origin_payload["timed_stage_observations"] += 1
            origin_payload["duration_ms"] += observation["duration_ms"]
        for kind in EXTERNAL_CALL_KINDS:
            origin_payload["source_external_calls"][kind] += observation[
                "source_external_calls"
            ][kind]


def _finish_audit(audit: dict[str, Any]) -> dict[str, Any]:
    for kind in EXTERNAL_CALL_KINDS:
        item = audit["actual_calls"]["by_kind"][kind]
        item["duration_ms"] = _round_milliseconds(item["duration_ms"])
        item["provider_wait_ms"] = _round_milliseconds(item["provider_wait_ms"])
    audit["actual_calls"]["totals"] = {
        field_name: (
            _round_milliseconds(
                sum(
                    float(audit["actual_calls"]["by_kind"][kind][field_name])
                    for kind in EXTERNAL_CALL_KINDS
                )
            )
            if field_name in {"duration_ms", "provider_wait_ms"}
            else sum(
                int(audit["actual_calls"]["by_kind"][kind][field_name])
                for kind in EXTERNAL_CALL_KINDS
            )
        )
        for field_name in _ACTUAL_CALL_FIELDS
    }
    audit["source_provenance"]["total_source_external_calls"] = sum(
        audit["source_provenance"]["by_kind"].values()
    )
    for origin in audit["source_provenance"]["by_origin"].values():
        origin["duration_ms"] = _round_milliseconds(origin["duration_ms"])
        timed = origin["timed_stage_observations"]
        origin["mean_duration_ms"] = (
            _round_milliseconds(origin["duration_ms"] / timed) if timed else None
        )
    for role in MODEL_USAGE_ROLES:
        usage = audit["model_usage"][role]
        usage["latency_ms"] = _round_milliseconds(usage["latency_ms"])
        usage["unreported_token_usage_calls"] = (
            usage["calls"] - usage["token_usage_calls"]
        )
        usage["token_usage_complete"] = usage["calls"] == usage["token_usage_calls"]
        usage["complete_token_totals"] = {
            field_name: (usage[field_name] if usage["token_usage_complete"] else None)
            for field_name in ("input_tokens", "output_tokens", "total_tokens")
        }
    for cache_mode, timing in audit["timings_by_cache_mode"].items():
        attempts = timing["attempts"]
        timing["queue_wait_ms"] = _round_milliseconds(timing["queue_wait_ms"])
        timing["end_to_end_ms"] = _round_milliseconds(timing["end_to_end_ms"])
        timing["mean_queue_wait_ms"] = (
            _round_milliseconds(timing["queue_wait_ms"] / attempts)
            if attempts
            else None
        )
        timing["mean_end_to_end_ms"] = (
            _round_milliseconds(timing["end_to_end_ms"] / attempts)
            if attempts
            else None
        )
    audit["attempt_statuses"] = dict(sorted(audit["attempt_statuses"].items()))
    audit["cache_modes"] = dict(sorted(audit["cache_modes"].items()))
    audit["failure_codes"] = dict(sorted(audit["failure_codes"].items()))
    audit["timings_by_cache_mode"] = {
        key: audit["timings_by_cache_mode"][key]
        for key in sorted(audit["timings_by_cache_mode"])
    }
    for mode in audit["by_cache_mode"].values():
        for kind in EXTERNAL_CALL_KINDS:
            actual = mode["actual_calls"][kind]
            actual["duration_ms"] = _round_milliseconds(actual["duration_ms"])
            actual["provider_wait_ms"] = _round_milliseconds(actual["provider_wait_ms"])
        for role in MODEL_USAGE_ROLES:
            usage = mode["model_usage"][role]
            usage["latency_ms"] = _round_milliseconds(usage["latency_ms"])
            usage["unreported_token_usage_calls"] = (
                usage["calls"] - usage["token_usage_calls"]
            )
            usage["token_usage_complete"] = usage["calls"] == usage["token_usage_calls"]
            usage["complete_token_totals"] = {
                field_name: (
                    usage[field_name] if usage["token_usage_complete"] else None
                )
                for field_name in ("input_tokens", "output_tokens", "total_tokens")
            }
        mode["queue_wait_ms"] = _round_milliseconds(mode["queue_wait_ms"])
        mode["end_to_end_ms"] = _round_milliseconds(mode["end_to_end_ms"])
        mode["mean_queue_wait_ms"] = _round_milliseconds(
            mode["queue_wait_ms"] / mode["attempts"]
        )
        mode["mean_end_to_end_ms"] = _round_milliseconds(
            mode["end_to_end_ms"] / mode["attempts"]
        )
        mode["attempt_statuses"] = dict(sorted(mode["attempt_statuses"].items()))
        mode["failure_codes"] = dict(sorted(mode["failure_codes"].items()))
    audit["by_cache_mode"] = {
        key: audit["by_cache_mode"][key] for key in sorted(audit["by_cache_mode"])
    }
    for environment in audit["environments"].values():
        attempts = environment["attempts"]
        environment["queue_wait_ms"] = _round_milliseconds(environment["queue_wait_ms"])
        environment["end_to_end_ms"] = _round_milliseconds(environment["end_to_end_ms"])
        environment["mean_queue_wait_ms"] = _round_milliseconds(
            environment["queue_wait_ms"] / attempts
        )
        environment["mean_end_to_end_ms"] = _round_milliseconds(
            environment["end_to_end_ms"] / attempts
        )
    audit["performance_comparable"] = len(audit["environments"]) <= 1
    audit["environments"] = {
        key: audit["environments"][key] for key in sorted(audit["environments"])
    }
    audit["untrusted_case_ids"] = sorted(audit["untrusted_case_ids"])
    audit["unreadable_case_ids"] = sorted(audit["unreadable_case_ids"])
    return _json_copy(audit)


def _metric_summary(records: Sequence[Any]) -> dict[str, Any]:
    summaries: dict[str, Any] = {}
    for metric_name in _CANONICAL_METRICS:
        values: list[Any] = []
        unavailable_reasons: Counter[str] = Counter()
        for record in records:
            metric = record.canonical_metrics[metric_name]
            value = metric["value"]
            if value is None:
                unavailable_reasons[metric["unavailable_reason"]] += 1
            else:
                values.append(value)
        common = {
            "denominator": len(values),
            "unavailable_count": len(records) - len(values),
            "unavailable_reasons": dict(sorted(unavailable_reasons.items())),
        }
        if metric_name in _NUMERIC_METRICS:
            numerator = round(sum(float(value) for value in values), 6)
            summaries[metric_name] = {
                "kind": "mean",
                "numerator": numerator,
                **common,
                "value": round(numerator / len(values), 4) if values else None,
                "unavailable_reason": None if values else "no_eligible_cases",
            }
        elif metric_name in _BOOLEAN_METRICS:
            numerator = sum(bool(value) for value in values)
            summaries[metric_name] = {
                "kind": "ratio",
                "numerator": numerator,
                **common,
                "value": round(numerator / len(values), 4) if values else None,
                "unavailable_reason": None if values else "no_eligible_cases",
            }
        elif metric_name in _CATEGORICAL_METRICS:
            summaries[metric_name] = {
                "kind": "distribution",
                **common,
                "distribution": dict(sorted(Counter(values).items())),
            }
        else:
            summaries[metric_name] = {
                "kind": "availability",
                **common,
            }
    return summaries


def _empty_case_row(
    case: Mapping[str, Any],
    *,
    store_state: str,
    problems: list[dict[str, Any]],
    artifact_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "ordinal": case["ordinal"],
        "case_id": case["case_id"],
        "case_hash": case["case_hash"],
        "artifact_evidence_sha256": canonical_hash(artifact_evidence),
        "physical_attempt_file_count": artifact_evidence["physical_attempt_file_count"],
        "completion_artifact_sha256": artifact_evidence["completion_artifact_sha256"],
        "attempt_history_readable": True,
        "status": _public_status(store_state),
        "store_state": store_state,
        "attempt_count": 0 if store_state == "not_run" else None,
        "trusted_attempt_count": 0,
        "latest_attempt_status": None,
        "latest_cache_mode": None,
        "complete_marker_valid": False,
        "output_sha256": None,
        "error_code": None,
        "problems": problems,
        "attempts": [],
        "evaluation_record": None,
        "trace_record": None,
    }


def _set_corrupt(
    row: dict[str, Any],
    problem: dict[str, Any],
    *,
    attempt_count: int | None = None,
) -> None:
    row["status"] = "corrupt"
    row["complete_marker_valid"] = False
    row["evaluation_record"] = None
    row["trace_record"] = None
    row["output_sha256"] = None
    row["problems"].append(problem)
    if attempt_count is not None:
        row["attempt_count"] = attempt_count


def _validate_work_unit_trust_prefix(
    unit: WorkUnit,
    raw_attempts_by_case: Mapping[str, tuple[dict[str, Any], ...]],
    unreadable_case_ids: set[str],
) -> tuple[Any | None, int, Exception | None]:
    """Validate the longest directional prefix that cannot depend on corruption."""

    unreadable_indexes = [
        index
        for index, case in enumerate(unit.cases)
        if case["case_id"] in unreadable_case_ids
    ]
    readable_limit = min(unreadable_indexes, default=len(unit.cases))
    if readable_limit == 0:
        return None, 0, None

    candidate = replace(unit, cases=unit.cases[:readable_limit])
    attempts = {
        case["case_id"]: raw_attempts_by_case.get(case["case_id"], ())
        for case in candidate.cases
    }
    try:
        return (
            validate_persisted_work_unit_history(candidate, attempts),
            readable_limit,
            None,
        )
    except (RunnerContractError, ExperimentContractError, ValueError) as full_error:
        last_validated = None
        valid_length = 0
        for length in range(1, readable_limit + 1):
            prefix = replace(unit, cases=unit.cases[:length])
            prefix_attempts = {
                case["case_id"]: raw_attempts_by_case.get(case["case_id"], ())
                for case in prefix.cases
            }
            try:
                last_validated = validate_persisted_work_unit_history(
                    prefix, prefix_attempts
                )
            except (RunnerContractError, ExperimentContractError, ValueError):
                break
            valid_length = length
        return last_validated, valid_length, full_error


def aggregate_experiment(store: ExperimentStore) -> dict[str, Any]:
    """Aggregate one persisted experiment without executing any runtime component.

    The function only calls the read APIs on ``ExperimentStore``. A case is
    scoreable only after its immutable attempt history, completion marker, runner
    result, evaluation output, and manifest/case identity all validate.
    """

    if not isinstance(store, ExperimentStore):
        raise TypeError("store must be an ExperimentStore")
    manifest = store.load_manifest()
    inventory = store.scan()
    if inventory.global_problems:
        codes = ",".join(problem.code for problem in inventory.global_problems)
        raise AggregationContractError(
            f"global experiment artifact corruption blocks aggregation: {codes}"
        )
    store_states = _store_state_by_case(inventory)
    manifest_cases = manifest["dataset"]["cases"]
    case_by_id = {case["case_id"]: case for case in manifest_cases}
    problems_by_case: dict[str, list[dict[str, Any]]] = {
        case_id: [] for case_id in store_states
    }
    for problem in inventory.problems:
        if problem.case_id is None:
            continue
        problems_by_case[problem.case_id].append(
            _problem(
                problem.code,
                source="store",
                path=problem.path,
                detail=problem.detail,
            )
        )
    artifact_evidence_by_case: dict[str, dict[str, Any]] = {}
    for case_id in inventory.case_order:
        try:
            artifact_evidence_by_case[case_id] = store.case_artifact_evidence(case_id)
        except ArtifactCorruptionError as error:
            artifact_evidence_by_case[case_id] = {
                "artifact_evidence_schema_version": 1,
                "case_path": error.path.name,
                "entries": [],
                "physical_attempt_file_count": 0,
                "completion_artifact_sha256": None,
                "unavailable_reason": error.code,
            }
            problems_by_case[case_id].append(
                _problem(
                    "artifact_evidence_unavailable",
                    source="store",
                    path=error.path.name,
                    detail=f"artifact evidence is unavailable: {error.code}",
                )
            )
    rows = {
        case_id: _empty_case_row(
            case_by_id[case_id],
            store_state=store_states[case_id],
            problems=problems_by_case[case_id],
            artifact_evidence=artifact_evidence_by_case[case_id],
        )
        for case_id in inventory.case_order
    }

    raw_attempts_by_case: dict[str, tuple[dict[str, Any], ...]] = {}
    unreadable_case_ids: set[str] = set()
    for case_id in inventory.case_order:
        if store_states[case_id] == "not_run":
            raw_attempts_by_case[case_id] = ()
            continue
        try:
            raw_attempts = store.load_attempts(case_id)
        except ArtifactCorruptionError as error:
            unreadable_case_ids.add(case_id)
            rows[case_id]["attempt_history_readable"] = False
            _set_corrupt(
                rows[case_id],
                _problem(
                    error.code,
                    source="store",
                    path=error.path.name,
                    detail=f"attempt history failed store validation: {error.code}",
                ),
                attempt_count=rows[case_id]["physical_attempt_file_count"],
            )
            continue
        raw_attempts_by_case[case_id] = raw_attempts
        rows[case_id]["attempt_count"] = len(raw_attempts)
        if raw_attempts:
            rows[case_id]["latest_attempt_status"] = raw_attempts[-1]["status"]
            rows[case_id]["latest_cache_mode"] = raw_attempts[-1]["cache_mode"]

    audit = _blank_audit(
        persisted_attempt_count=sum(
            row["physical_attempt_file_count"] for row in rows.values()
        ),
        untrusted_attempt_count=sum(
            rows[case_id]["physical_attempt_file_count"]
            for case_id in unreadable_case_ids
        ),
        untrusted_case_ids=sorted(unreadable_case_ids),
        unreadable_case_ids=sorted(unreadable_case_ids),
    )
    trusted_records_by_case: dict[str, Any] = {}

    for unit in plan_work_units(manifest):
        validated_unit, trusted_prefix_length, prefix_error = (
            _validate_work_unit_trust_prefix(
                unit,
                raw_attempts_by_case,
                unreadable_case_ids,
            )
        )
        for suffix_index, case in enumerate(
            unit.cases[trusted_prefix_length:], start=trusted_prefix_length
        ):
            case_id = case["case_id"]
            if case_id in unreadable_case_ids:
                continue
            attempts = raw_attempts_by_case.get(case_id, ())
            if not attempts:
                continue
            is_first_invalid = (
                prefix_error is not None and suffix_index == trusted_prefix_length
            )
            _set_corrupt(
                rows[case_id],
                _problem(
                    (
                        "invalid_runner_history"
                        if is_first_invalid
                        else "untrusted_session_predecessor"
                    ),
                    source="runner",
                    detail=(
                        str(prefix_error)
                        if is_first_invalid
                        else "work-unit history depends on an earlier corrupt turn"
                    ),
                ),
                attempt_count=len(attempts),
            )
            audit["untrusted_attempt_count"] += len(attempts)
            audit["untrusted_case_ids"].append(case_id)
        if validated_unit is None:
            continue

        histories = {
            history.case_id: history for history in validated_unit.case_histories
        }
        for case in unit.cases[:trusted_prefix_length]:
            case_id = case["case_id"]
            raw_attempts = raw_attempts_by_case.get(case_id, ())
            history = histories.get(case_id)
            if history is None:
                if raw_attempts:
                    _set_corrupt(
                        rows[case_id],
                        _problem(
                            "missing_validated_history",
                            source="runner",
                            detail="attempts were not present in validated work-unit history",
                        ),
                        attempt_count=len(raw_attempts),
                    )
                    audit["untrusted_attempt_count"] += len(raw_attempts)
                    audit["untrusted_case_ids"].append(case_id)
                continue
            if len(history.attempts) != len(raw_attempts):
                _set_corrupt(
                    rows[case_id],
                    _problem(
                        "attempt_count_mismatch",
                        source="runner",
                        detail="validated and persisted attempt counts disagree",
                    ),
                    attempt_count=len(raw_attempts),
                )
                audit["untrusted_attempt_count"] += len(raw_attempts)
                audit["untrusted_case_ids"].append(case_id)
                continue

            rows[case_id]["trusted_attempt_count"] = len(history.attempts)
            for validated_attempt, raw_payload in zip(history.attempts, raw_attempts):
                rows[case_id]["attempts"].append(
                    _attempt_row(validated_attempt, raw_payload)
                )
                _add_attempt_to_audit(audit, validated_attempt, raw_payload)
            latest = history.attempts[-1]
            rows[case_id]["latest_attempt_status"] = latest.status
            rows[case_id]["latest_cache_mode"] = latest.cache_mode
            rows[case_id]["error_code"] = (
                latest.error["code"] if latest.error is not None else None
            )

            store_state = store_states[case_id]
            if latest.status != "succeeded":
                if store_state not in {"failed", "interrupted", "exhausted"}:
                    _set_corrupt(
                        rows[case_id],
                        _problem(
                            "inventory_runner_status_mismatch",
                            source="aggregation",
                            detail="store inventory and runner status disagree",
                        ),
                    )
                else:
                    rows[case_id]["status"] = store_state
                continue

            if latest.output is None:
                _set_corrupt(
                    rows[case_id],
                    _problem(
                        "missing_evaluation_output",
                        source="aggregation",
                        detail="succeeded attempt has no evaluation output",
                    ),
                )
                continue
            try:
                evaluated = decode_evaluation_output(
                    latest.output,
                    expected_manifest=manifest,
                    expected_case=case,
                )
                if not _output_stage_identity_matches_runner(latest.output, latest):
                    raise AggregationContractError(
                        "evaluation output stage identity disagrees with runner observations"
                    )
                if not _output_usage_matches_runner(latest.output, latest):
                    raise AggregationContractError(
                        "evaluation output usage disagrees with runner model usage"
                    )
            except (ExperimentContractError, ValueError, TypeError) as error:
                _set_corrupt(
                    rows[case_id],
                    _problem(
                        "invalid_evaluation_output",
                        source="evaluation_output",
                        detail=str(error),
                    ),
                )
                continue
            rows[case_id]["output_sha256"] = canonical_hash(latest.output)

            if store_state == "missing":
                rows[case_id]["status"] = "pending"
                continue
            if store_state != "succeeded":
                _set_corrupt(
                    rows[case_id],
                    _problem(
                        "inventory_runner_status_mismatch",
                        source="aggregation",
                        detail="succeeded runner output lacks succeeded inventory state",
                    ),
                )
                continue
            try:
                completed_payload = store.load_completed(case_id)
            except ArtifactCorruptionError as error:
                _set_corrupt(
                    rows[case_id],
                    _problem(
                        error.code,
                        source="complete_marker",
                        path=error.path.name,
                        detail=f"completion proof failed validation: {error.code}",
                    ),
                )
                continue
            if canonical_json_bytes(completed_payload) != canonical_json_bytes(
                raw_attempts[-1]
            ):
                _set_corrupt(
                    rows[case_id],
                    _problem(
                        "complete_attempt_payload_mismatch",
                        source="complete_marker",
                        detail="completion proof does not resolve to the final attempt",
                    ),
                )
                continue
            if evaluated.record.case_id != case_id:
                _set_corrupt(
                    rows[case_id],
                    _problem(
                        "decoded_case_identity_mismatch",
                        source="evaluation_output",
                        detail="decoded evaluation record case_id disagrees with manifest",
                    ),
                )
                continue
            rows[case_id]["status"] = "succeeded"
            rows[case_id]["complete_marker_valid"] = True
            rows[case_id]["evaluation_record"] = _redact_sensitive_fields(
                eval_record_to_artifact(evaluated.record)
            )
            rows[case_id]["trace_record"] = _redact_sensitive_fields(
                evaluated.trace_record
            )
            trusted_records_by_case[case_id] = evaluated.record

        if unit.session_group is not None:
            predecessor_corrupt = False
            for case in unit.cases[:trusted_prefix_length]:
                case_id = case["case_id"]
                row = rows[case_id]
                if predecessor_corrupt and row["attempt_count"]:
                    trusted_records_by_case.pop(case_id, None)
                    _set_corrupt(
                        row,
                        _problem(
                            "untrusted_session_predecessor",
                            source="aggregation",
                            detail=(
                                "case depends on an earlier turn whose aggregate "
                                "semantic proof is corrupt"
                            ),
                        ),
                    )
                    continue
                if row["status"] == "corrupt":
                    predecessor_corrupt = True

    ordered_rows = [rows[case_id] for case_id in inventory.case_order]
    trusted_records = [
        trusted_records_by_case[case_id]
        for case_id in inventory.case_order
        if case_id in trusted_records_by_case
    ]
    counts = {status: 0 for status in CASE_STATUSES}
    case_ids = {status: [] for status in CASE_STATUSES}
    for row in ordered_rows:
        status = row["status"]
        if status not in counts:
            raise AggregationContractError(f"unsupported aggregate status: {status!r}")
        counts[status] += 1
        case_ids[status].append(row["case_id"])
    aggregation_status = (
        "corrupt"
        if counts["corrupt"]
        else "complete"
        if counts["succeeded"] == len(ordered_rows)
        else "partial"
    )
    scoring = {
        "trusted_succeeded_case_count": len(trusted_records),
        "excluded_case_counts": {
            status: counts[status]
            for status in (
                "failed",
                "interrupted",
                "exhausted",
                "pending",
                "not_run",
                "corrupt",
            )
        },
        "behavior": summarize_evaluation(list(trusted_records)),
        "canonical_metrics": _metric_summary(trusted_records),
    }
    aggregation_contract = manifest["stage_contracts"]["aggregation"]
    case_results_hash = canonical_hash(
        {
            "manifest_hash": manifest["identity"]["manifest_hash"],
            "cases": ordered_rows,
        }
    )
    aggregation_key = build_stage_cache_key(
        manifest,
        "aggregation",
        {"case_results_hash": case_results_hash},
    )
    core = {
        "aggregation_schema_version": AGGREGATION_SCHEMA_VERSION,
        "identity": {
            "experiment_id": manifest["experiment_id"],
            "manifest_schema_version": manifest["manifest_schema_version"],
            "manifest_hash": manifest["identity"]["manifest_hash"],
            "resume_compatibility_hash": manifest["identity"][
                "resume_compatibility_hash"
            ],
            "aggregation_contract_fingerprint": aggregation_contract["fingerprint"],
            "case_results_hash": case_results_hash,
            "aggregation_key": aggregation_key,
            "execution_mode": manifest["execution_mode"],
            "dataset_id": manifest["dataset"]["dataset_id"],
            "case_set_hash": manifest["dataset"]["case_set_hash"],
            "case_count": manifest["dataset"]["case_count"],
        },
        "status": aggregation_status,
        "run_context": {
            "default_cache_mode": manifest["cache_policy"]["default_mode"],
            "concurrency": manifest["runtime"]["concurrency"],
            "timing_scope": manifest["runtime"]["timing_scope"],
            "manifest_environment": _redact_sensitive_fields(manifest["environment"]),
        },
        "case_inventory": {
            "expected": len(ordered_rows),
            "counts": counts,
            "case_ids": case_ids,
        },
        "cases": ordered_rows,
        "attempt_audit": _finish_audit(audit),
        "scoring": scoring,
    }
    bundle = {**core, "bundle_hash": canonical_hash(core)}
    return validate_aggregation_bundle(bundle, expected_manifest=manifest)


def aggregate_experiment_artifacts(
    root: str | Path,
    experiment_id: str,
) -> dict[str, Any]:
    """Open persisted artifacts and return the same read-only aggregate bundle."""

    return aggregate_experiment(ExperimentStore.open(root, experiment_id))


def validate_aggregation_bundle(
    value: Mapping[str, Any],
    *,
    expected_manifest: Mapping[str, Any] | None = None,
    expected_bundle_hash: str | None = None,
) -> dict[str, Any]:
    """Validate bundle derivations and optionally anchor them to trusted identity."""

    if not isinstance(value, Mapping):
        raise AggregationContractError("aggregation bundle must be an object")
    bundle = _json_copy(dict(value))
    if canonical_json_bytes(_redact_sensitive_fields(bundle)) != canonical_json_bytes(
        bundle
    ):
        raise AggregationContractError(
            "aggregation bundle contains credential-shaped metadata fields"
        )
    required = {
        "aggregation_schema_version",
        "bundle_hash",
        "identity",
        "status",
        "run_context",
        "case_inventory",
        "cases",
        "attempt_audit",
        "scoring",
    }
    if set(bundle) != required:
        raise AggregationContractError("aggregation bundle fields are invalid")
    if (
        type(bundle["aggregation_schema_version"]) is not int
        or bundle["aggregation_schema_version"] != AGGREGATION_SCHEMA_VERSION
    ):
        raise AggregationContractError("aggregation schema version is unsupported")
    identity = bundle["identity"]
    if not isinstance(identity, dict) or set(identity) != {
        "experiment_id",
        "manifest_schema_version",
        "manifest_hash",
        "resume_compatibility_hash",
        "aggregation_contract_fingerprint",
        "case_results_hash",
        "aggregation_key",
        "execution_mode",
        "dataset_id",
        "case_set_hash",
        "case_count",
    }:
        raise AggregationContractError("aggregation identity fields are invalid")
    for name in (
        "manifest_hash",
        "resume_compatibility_hash",
        "aggregation_contract_fingerprint",
        "case_results_hash",
        "aggregation_key",
        "case_set_hash",
    ):
        if not isinstance(identity[name], str) or not _SHA256.fullmatch(identity[name]):
            raise AggregationContractError(
                f"aggregation identity {name} must be a lowercase SHA-256"
            )
    for name in ("experiment_id", "execution_mode", "dataset_id"):
        if not isinstance(identity[name], str) or not identity[name].strip():
            raise AggregationContractError(
                f"aggregation identity {name} must be a non-empty string"
            )
    if (
        isinstance(identity["manifest_schema_version"], bool)
        or not isinstance(identity["manifest_schema_version"], int)
        or identity["manifest_schema_version"] < 1
        or isinstance(identity["case_count"], bool)
        or not isinstance(identity["case_count"], int)
        or identity["case_count"] < 0
    ):
        raise AggregationContractError(
            "aggregation identity versions/counts are invalid"
        )
    if not isinstance(bundle["bundle_hash"], str) or not _SHA256.fullmatch(
        bundle["bundle_hash"]
    ):
        raise AggregationContractError("aggregation bundle hash must be a SHA-256")
    if bundle["status"] not in {"complete", "partial", "corrupt"}:
        raise AggregationContractError("aggregation status is invalid")
    run_context = bundle["run_context"]
    if not isinstance(run_context, dict) or set(run_context) != {
        "default_cache_mode",
        "concurrency",
        "timing_scope",
        "manifest_environment",
    }:
        raise AggregationContractError("aggregation run context is invalid")
    if run_context["default_cache_mode"] not in {"fresh", "cache", "replay"}:
        raise AggregationContractError("aggregation default cache mode is invalid")
    if (
        isinstance(run_context["concurrency"], bool)
        or not isinstance(run_context["concurrency"], int)
        or run_context["concurrency"] < 1
        or not isinstance(run_context["timing_scope"], str)
        or not run_context["timing_scope"].strip()
        or not isinstance(run_context["manifest_environment"], dict)
    ):
        raise AggregationContractError("aggregation run context fields are invalid")
    cases = bundle["cases"]
    if not isinstance(cases, list):
        raise AggregationContractError("aggregation cases must be a list")
    inventory = bundle["case_inventory"]
    if not isinstance(inventory, dict) or set(inventory) != {
        "expected",
        "counts",
        "case_ids",
    }:
        raise AggregationContractError("aggregation case inventory is invalid")
    if (
        isinstance(inventory["expected"], bool)
        or not isinstance(inventory["expected"], int)
        or inventory["expected"] != len(cases)
        or identity["case_count"] != len(cases)
    ):
        raise AggregationContractError("aggregation case counts disagree")
    observed_ids: set[str] = set()
    observed_counts = {status: 0 for status in CASE_STATUSES}
    observed_case_ids = {status: [] for status in CASE_STATUSES}
    known_attempt_count = 0
    trusted_attempt_count = 0
    decoded_records: list[Any] = []
    expected_untrusted_case_ids: list[str] = []
    expected_unreadable_case_ids: list[str] = []
    for expected_ordinal, case in enumerate(cases):
        if not isinstance(case, dict) or set(case) != _CASE_ROW_FIELDS:
            raise AggregationContractError("aggregation case row must be an object")
        if type(case["ordinal"]) is not int or case["ordinal"] != expected_ordinal:
            raise AggregationContractError("aggregation case order is not canonical")
        case_id = case["case_id"]
        if not isinstance(case_id, str) or not case_id:
            raise AggregationContractError("aggregation case_id is invalid")
        if case_id in observed_ids:
            raise AggregationContractError("aggregation case_id is duplicated")
        observed_ids.add(case_id)
        if not isinstance(case["case_hash"], str) or not _SHA256.fullmatch(
            case["case_hash"]
        ):
            raise AggregationContractError("aggregation case_hash is invalid")
        if not isinstance(
            case["artifact_evidence_sha256"], str
        ) or not _SHA256.fullmatch(case["artifact_evidence_sha256"]):
            raise AggregationContractError(
                "aggregation artifact evidence hash is invalid"
            )
        physical_attempt_count = case["physical_attempt_file_count"]
        if (
            isinstance(physical_attempt_count, bool)
            or not isinstance(physical_attempt_count, int)
            or physical_attempt_count < 0
        ):
            raise AggregationContractError(
                "aggregation physical attempt count is invalid"
            )
        completion_artifact_sha256 = case["completion_artifact_sha256"]
        if completion_artifact_sha256 is not None and (
            not isinstance(completion_artifact_sha256, str)
            or not _SHA256.fullmatch(completion_artifact_sha256)
        ):
            raise AggregationContractError(
                "aggregation completion artifact hash is invalid"
            )
        if not isinstance(case["attempt_history_readable"], bool):
            raise AggregationContractError(
                "aggregation attempt history readability is invalid"
            )
        if not case["attempt_history_readable"]:
            expected_unreadable_case_ids.append(case_id)
        status = case["status"]
        if status not in observed_counts:
            raise AggregationContractError("aggregation case status is invalid")
        if case["store_state"] not in _STORE_STATES:
            raise AggregationContractError("aggregation store state is invalid")
        observed_counts[status] += 1
        observed_case_ids[status].append(case_id)
        attempt_count = case["attempt_count"]
        if attempt_count is not None and (
            isinstance(attempt_count, bool)
            or not isinstance(attempt_count, int)
            or attempt_count < 0
        ):
            raise AggregationContractError("aggregation attempt_count is invalid")
        if attempt_count is not None:
            known_attempt_count += attempt_count
            if attempt_count != physical_attempt_count:
                raise AggregationContractError(
                    "aggregation logical and physical attempt counts disagree"
                )
        trusted_count = case["trusted_attempt_count"]
        if (
            isinstance(trusted_count, bool)
            or not isinstance(trusted_count, int)
            or trusted_count < 0
            or (attempt_count is not None and trusted_count > attempt_count)
        ):
            raise AggregationContractError(
                "aggregation trusted_attempt_count is invalid"
            )
        trusted_attempt_count += trusted_count
        if (
            not case["attempt_history_readable"]
            or attempt_count is not None
            and trusted_count < attempt_count
        ):
            expected_untrusted_case_ids.append(case_id)
        attempts = case["attempts"]
        if not isinstance(attempts, list) or len(attempts) != trusted_count:
            raise AggregationContractError(
                "aggregation trusted attempts are inconsistent"
            )
        for expected_attempt, attempt in enumerate(attempts, start=1):
            if not isinstance(attempt, dict) or set(attempt) != _ATTEMPT_ROW_FIELDS:
                raise AggregationContractError("aggregation attempt row is invalid")
            if attempt["attempt"] != expected_attempt:
                raise AggregationContractError(
                    "aggregation attempt rows are not contiguous"
                )
            if not isinstance(
                attempt["attempt_payload_sha256"], str
            ) or not _SHA256.fullmatch(attempt["attempt_payload_sha256"]):
                raise AggregationContractError(
                    "aggregation attempt payload hash is invalid"
                )
            checkpoint_sha256 = attempt["session_checkpoint_sha256"]
            if checkpoint_sha256 is not None and (
                not isinstance(checkpoint_sha256, str)
                or not _SHA256.fullmatch(checkpoint_sha256)
            ):
                raise AggregationContractError(
                    "aggregation session checkpoint hash is invalid"
                )
            if not isinstance(
                attempt["redacted_environment_sha256"], str
            ) or not _SHA256.fullmatch(attempt["redacted_environment_sha256"]):
                raise AggregationContractError(
                    "aggregation redacted environment hash is invalid"
                )
            if attempt["status"] not in {"succeeded", "failed", "interrupted"}:
                raise AggregationContractError("aggregation attempt status is invalid")
            if attempt["cache_mode"] not in {"fresh", "cache", "replay"}:
                raise AggregationContractError(
                    "aggregation attempt cache mode is invalid"
                )
            usage_projection = attempt["model_usage"]
            if not isinstance(usage_projection, dict) or set(usage_projection) != set(
                MODEL_USAGE_ROLES
            ):
                raise AggregationContractError(
                    "aggregation attempt model usage roles are invalid"
                )
            for role in MODEL_USAGE_ROLES:
                role_usage = usage_projection[role]
                if not isinstance(role_usage, dict) or any(
                    field_name not in role_usage for field_name in _MODEL_USAGE_FIELDS
                ):
                    raise AggregationContractError(
                        "aggregation attempt model usage fields are invalid"
                    )
                expected_usage_projection = _model_usage_projection(role_usage)
                if canonical_json_bytes(role_usage) != canonical_json_bytes(
                    expected_usage_projection
                ):
                    raise AggregationContractError(
                        "aggregation attempt model usage projection is inconsistent"
                    )
            if not isinstance(
                attempt["environment_fingerprint"], str
            ) or not _SHA256.fullmatch(attempt["environment_fingerprint"]):
                raise AggregationContractError(
                    "aggregation attempt environment fingerprint is invalid"
                )
        latest_status = case["latest_attempt_status"]
        if latest_status not in {None, "succeeded", "failed", "interrupted"}:
            raise AggregationContractError(
                "aggregation latest attempt status is invalid"
            )
        latest_cache_mode = case["latest_cache_mode"]
        if latest_cache_mode not in {None, "fresh", "cache", "replay"}:
            raise AggregationContractError("aggregation latest cache mode is invalid")
        if attempts and (
            latest_status != attempts[-1]["status"]
            or latest_cache_mode != attempts[-1]["cache_mode"]
        ):
            raise AggregationContractError(
                "aggregation latest attempt identity is inconsistent"
            )
        if not isinstance(case["complete_marker_valid"], bool):
            raise AggregationContractError(
                "aggregation completion marker status is invalid"
            )
        if case["output_sha256"] is not None and (
            not isinstance(case["output_sha256"], str)
            or not _SHA256.fullmatch(case["output_sha256"])
        ):
            raise AggregationContractError("aggregation output hash is invalid")
        if not isinstance(case["problems"], list) or any(
            not isinstance(problem, dict)
            or set(problem) != {"code", "source", "path", "detail"}
            for problem in case["problems"]
        ):
            raise AggregationContractError("aggregation case problems are invalid")
        if status == "succeeded" and (
            case["store_state"] != "succeeded"
            or case["latest_attempt_status"] != "succeeded"
            or case["complete_marker_valid"] is not True
            or not isinstance(case["output_sha256"], str)
            or not isinstance(case["completion_artifact_sha256"], str)
            or not isinstance(case["evaluation_record"], dict)
            or not isinstance(case["trace_record"], dict)
        ):
            raise AggregationContractError(
                "succeeded aggregation case lacks trusted completion artifacts"
            )
        if status == "succeeded":
            try:
                decoded_record = eval_record_from_artifact(case["evaluation_record"])
            except (TypeError, ValueError) as error:
                raise AggregationContractError(
                    "succeeded aggregation case has an invalid evaluation record"
                ) from error
            if decoded_record.case_id != case_id:
                raise AggregationContractError(
                    "aggregation evaluation record case identity is inconsistent"
                )
            decoded_records.append(decoded_record)
        if status != "succeeded" and (
            case["evaluation_record"] is not None or case["trace_record"] is not None
        ):
            raise AggregationContractError(
                "non-succeeded aggregation case cannot publish scoring artifacts"
            )
        expected_state = {
            "failed": "failed",
            "interrupted": "interrupted",
            "exhausted": "exhausted",
            "pending": "missing",
            "not_run": "not_run",
        }.get(status)
        if expected_state is not None and case["store_state"] != expected_state:
            raise AggregationContractError(
                "aggregation public status disagrees with store state"
            )
        if status == "pending" and case["latest_attempt_status"] != "succeeded":
            raise AggregationContractError(
                "pending aggregation case must have a succeeded attempt"
            )
        if status == "not_run" and (
            attempt_count != 0
            or trusted_count != 0
            or case["latest_attempt_status"] is not None
        ):
            raise AggregationContractError("not-run aggregation case has attempts")
    if inventory["counts"] != observed_counts:
        raise AggregationContractError("aggregation status counts are inconsistent")
    if inventory["case_ids"] != observed_case_ids:
        raise AggregationContractError("aggregation status case_ids are inconsistent")
    scoring = bundle["scoring"]
    if (
        not isinstance(scoring, dict)
        or scoring.get("trusted_succeeded_case_count") != observed_counts["succeeded"]
    ):
        raise AggregationContractError("aggregation scoring scope is inconsistent")
    expected_scoring = {
        "trusted_succeeded_case_count": len(decoded_records),
        "excluded_case_counts": {
            status: observed_counts[status]
            for status in (
                "failed",
                "interrupted",
                "exhausted",
                "pending",
                "not_run",
                "corrupt",
            )
        },
        "behavior": summarize_evaluation(decoded_records),
        "canonical_metrics": _metric_summary(decoded_records),
    }
    if canonical_json_bytes(scoring) != canonical_json_bytes(expected_scoring):
        raise AggregationContractError(
            "aggregation scoring disagrees with trusted case artifacts"
        )
    audit = bundle["attempt_audit"]
    if not isinstance(audit, dict):
        raise AggregationContractError("aggregation attempt audit is invalid")
    if audit.get("persisted_attempt_count") != known_attempt_count:
        raise AggregationContractError(
            "aggregation persisted attempt count is inconsistent"
        )
    if audit.get("trusted_attempt_count") != trusted_attempt_count:
        raise AggregationContractError(
            "aggregation trusted attempt count is inconsistent"
        )
    untrusted = audit.get("untrusted_attempt_count")
    if (
        isinstance(untrusted, bool)
        or not isinstance(untrusted, int)
        or untrusted < 0
        or audit["trusted_attempt_count"] + untrusted
        != audit["persisted_attempt_count"]
    ):
        raise AggregationContractError(
            "aggregation untrusted attempt count is inconsistent"
        )
    expected_untrusted_case_ids = sorted(expected_untrusted_case_ids)
    if audit.get("untrusted_case_ids") != expected_untrusted_case_ids:
        raise AggregationContractError(
            "aggregation untrusted case identities are inconsistent"
        )
    expected_unreadable_case_ids = sorted(expected_unreadable_case_ids)
    unreadable_ids = audit.get("unreadable_case_ids")
    if unreadable_ids != expected_unreadable_case_ids:
        raise AggregationContractError(
            "aggregation unreadable case identities are inconsistent"
        )
    try:
        environment_catalog = audit["environments"]
        if not isinstance(environment_catalog, dict):
            raise TypeError("environment catalog must be an object")
        recomputed_audit = _blank_audit(
            persisted_attempt_count=audit["persisted_attempt_count"],
            untrusted_attempt_count=untrusted,
            untrusted_case_ids=audit["untrusted_case_ids"],
            unreadable_case_ids=audit["unreadable_case_ids"],
        )
        for case in cases:
            for attempt in case["attempts"]:
                fingerprint = attempt["environment_fingerprint"]
                environment_entry = environment_catalog[fingerprint]
                execution_environment = environment_entry["execution_environment"]
                redacted_environment_sha256 = canonical_hash(execution_environment)
                if (
                    attempt["redacted_environment_sha256"]
                    != redacted_environment_sha256
                    or environment_entry["redacted_environment_sha256"]
                    != redacted_environment_sha256
                ):
                    raise ValueError(
                        "redacted execution environment identity is inconsistent"
                    )
                validated_attempt = ValidatedRunnerAttempt(
                    attempt_number=attempt["attempt"],
                    case_id=case["case_id"],
                    status=attempt["status"],
                    cache_mode=attempt["cache_mode"],
                    output=None,
                    session_checkpoint=None,
                    stage_observations=attempt["stage_observations"],
                    call_ledger=attempt["call_ledger"],
                    model_usage={
                        role: {
                            field_name: attempt["model_usage"][role][field_name]
                            for field_name in _MODEL_USAGE_FIELDS
                        }
                        for role in MODEL_USAGE_ROLES
                    },
                    timings_ms=attempt["timings_ms"],
                    error=attempt["error"],
                )
                _add_attempt_to_audit(
                    recomputed_audit,
                    validated_attempt,
                    {
                        "environment_fingerprint": fingerprint,
                        "execution_environment": execution_environment,
                    },
                )
        recomputed_audit = _finish_audit(recomputed_audit)
    except (KeyError, TypeError, ValueError) as error:
        raise AggregationContractError(
            "aggregation attempt audit contains invalid nested data"
        ) from error
    if canonical_json_bytes(audit) != canonical_json_bytes(recomputed_audit):
        raise AggregationContractError(
            "aggregation attempt audit disagrees with case attempt rows"
        )
    expected_status = (
        "corrupt"
        if observed_counts["corrupt"]
        else "complete"
        if observed_counts["succeeded"] == len(cases)
        else "partial"
    )
    if bundle["status"] != expected_status:
        raise AggregationContractError("aggregation overall status is inconsistent")
    expected_case_results_hash = canonical_hash(
        {"manifest_hash": identity["manifest_hash"], "cases": cases}
    )
    if identity["case_results_hash"] != expected_case_results_hash:
        raise AggregationContractError("aggregation case results hash is invalid")
    expected_aggregation_key = canonical_hash(
        {
            "cache_schema_version": STAGE_CACHE_SCHEMA_VERSION,
            "stage": "aggregation",
            "contract_fingerprint": identity["aggregation_contract_fingerprint"],
            "input": {"case_results_hash": identity["case_results_hash"]},
        }
    )
    if identity["aggregation_key"] != expected_aggregation_key:
        raise AggregationContractError("aggregation cache key is invalid")
    if expected_manifest is not None:
        manifest = validate_experiment_manifest(expected_manifest)
        expected_identity = {
            "experiment_id": manifest["experiment_id"],
            "manifest_schema_version": manifest["manifest_schema_version"],
            "manifest_hash": manifest["identity"]["manifest_hash"],
            "resume_compatibility_hash": manifest["identity"][
                "resume_compatibility_hash"
            ],
            "aggregation_contract_fingerprint": manifest["stage_contracts"][
                "aggregation"
            ]["fingerprint"],
            "execution_mode": manifest["execution_mode"],
            "dataset_id": manifest["dataset"]["dataset_id"],
            "case_set_hash": manifest["dataset"]["case_set_hash"],
            "case_count": manifest["dataset"]["case_count"],
        }
        for name, expected_value in expected_identity.items():
            if identity[name] != expected_value:
                raise AggregationContractError(
                    f"aggregation identity disagrees with manifest: {name}"
                )
        expected_context = {
            "default_cache_mode": manifest["cache_policy"]["default_mode"],
            "concurrency": manifest["runtime"]["concurrency"],
            "timing_scope": manifest["runtime"]["timing_scope"],
            "manifest_environment": _redact_sensitive_fields(manifest["environment"]),
        }
        if canonical_json_bytes(run_context) != canonical_json_bytes(expected_context):
            raise AggregationContractError(
                "aggregation run context disagrees with manifest"
            )
    core = {key: item for key, item in bundle.items() if key != "bundle_hash"}
    if bundle["bundle_hash"] != canonical_hash(core):
        raise AggregationContractError("aggregation bundle hash is invalid")
    if expected_bundle_hash is not None:
        if (
            not isinstance(expected_bundle_hash, str)
            or not _SHA256.fullmatch(expected_bundle_hash)
            or bundle["bundle_hash"] != expected_bundle_hash
        ):
            raise AggregationContractError(
                "aggregation bundle hash disagrees with trusted identity"
            )
    return bundle


def aggregation_json_bytes(bundle: Mapping[str, Any]) -> bytes:
    """Return canonical local/private JSON used by ``bundle_hash`` validation."""

    return canonical_json_bytes(validate_aggregation_bundle(bundle))


def render_aggregation_json(bundle: Mapping[str, Any]) -> str:
    """Render local/private JSON; it can contain queries and answer text."""

    return aggregation_json_bytes(bundle).decode("utf-8")


def aggregation_jsonl_rows(bundle: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """Return deterministic local/private per-case rows for an atomic writer."""

    validated = validate_aggregation_bundle(bundle)
    identity = validated["identity"]
    return tuple(
        {
            "aggregation_row_schema_version": AGGREGATION_ROW_SCHEMA_VERSION,
            "experiment_id": identity["experiment_id"],
            "manifest_hash": identity["manifest_hash"],
            "bundle_hash": validated["bundle_hash"],
            "case_results_hash": identity["case_results_hash"],
            "aggregation_key": identity["aggregation_key"],
            "case": _json_copy(case),
        }
        for case in validated["cases"]
    )


def render_aggregation_jsonl(bundle: Mapping[str, Any]) -> str:
    """Render local/private JSONL without writing to the experiment directory."""

    rows = aggregation_jsonl_rows(bundle)
    if not rows:
        return ""
    return "".join(canonical_json_bytes(row).decode("utf-8") + "\n" for row in rows)


def aggregation_csv_rows(bundle: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """Return stable, formula-neutralized metric rows without answer text."""

    validated = validate_aggregation_bundle(bundle)
    identity = validated["identity"]
    rows: list[dict[str, Any]] = []
    for case in validated["cases"]:
        record_artifact = case["evaluation_record"]
        canonical_metrics = (
            record_artifact["evaluation_record"]["canonical_metrics"]
            if record_artifact is not None
            else {}
        )
        row: dict[str, Any] = {
            "aggregation_row_schema_version": AGGREGATION_ROW_SCHEMA_VERSION,
            "experiment_id": identity["experiment_id"],
            "manifest_hash": identity["manifest_hash"],
            "bundle_hash": validated["bundle_hash"],
            "case_results_hash": identity["case_results_hash"],
            "aggregation_key": identity["aggregation_key"],
            "ordinal": case["ordinal"],
            "case_id": case["case_id"],
            "case_hash": case["case_hash"],
            "status": case["status"],
            "store_state": case["store_state"],
            "attempt_count": case["attempt_count"],
            "trusted_attempt_count": case["trusted_attempt_count"],
            "latest_attempt_status": case["latest_attempt_status"],
            "latest_cache_mode": case["latest_cache_mode"],
            "complete_marker_valid": case["complete_marker_valid"],
            "output_sha256": case["output_sha256"],
            "error_code": case["error_code"],
            "problem_codes": ",".join(problem["code"] for problem in case["problems"]),
        }
        for metric_name in _CSV_METRICS:
            metric = canonical_metrics.get(metric_name)
            row[f"metric_{metric_name}"] = (
                None if metric is None or metric["value"] is None else metric["value"]
            )
            row[f"metric_{metric_name}_unavailable_reason"] = (
                None if metric is None else metric["unavailable_reason"]
            )
        rows.append({key: _csv_safe_cell(item) for key, item in row.items()})
    return tuple(rows)


def render_aggregation_csv(bundle: Mapping[str, Any]) -> str:
    """Render deterministic UTF-8 CSV text for a later atomic writer."""

    rows = aggregation_csv_rows(bundle)
    metric_columns = tuple(
        column
        for metric_name in _CSV_METRICS
        for column in (
            f"metric_{metric_name}",
            f"metric_{metric_name}_unavailable_reason",
        )
    )
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=(*_CSV_BASE_COLUMNS, *metric_columns),
        lineterminator="\n",
        extrasaction="raise",
    )
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def _markdown_cell(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "\\|")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def render_aggregation_markdown(bundle: Mapping[str, Any]) -> str:
    """Render a deterministic, concise status/audit/metric report."""

    validated = validate_aggregation_bundle(bundle)
    identity = validated["identity"]
    context = validated["run_context"]
    inventory = validated["case_inventory"]
    audit = validated["attempt_audit"]
    lines = [
        "# Experiment aggregation",
        "",
        f"- Experiment: `{_markdown_cell(identity['experiment_id'])}`",
        f"- Manifest: `{identity['manifest_hash']}`",
        f"- Bundle: `{validated['bundle_hash']}`",
        f"- Status: `{validated['status']}`",
        f"- Execution mode: `{_markdown_cell(identity['execution_mode'])}`",
        f"- Default cache mode: `{context['default_cache_mode']}`",
        f"- Concurrency: {context['concurrency']}",
        f"- Timing scope: `{_markdown_cell(context['timing_scope'])}`",
        (
            "- Performance comparable across attempts: "
            f"`{str(audit['performance_comparable']).lower()}`"
        ),
        (
            "- Cases: "
            + ", ".join(
                f"{status}={inventory['counts'][status]}" for status in CASE_STATUSES
            )
        ),
        (
            "- Attempts: "
            f"persisted={audit['persisted_attempt_count']}, "
            f"trusted={audit['trusted_attempt_count']}, "
            f"untrusted={audit['untrusted_attempt_count']}"
        ),
        "",
        "## Cases",
        "",
        "| # | Case | Status | Store state | Attempts | Error | Problems |",
        "|---:|---|---|---|---:|---|---|",
    ]
    for case in validated["cases"]:
        problem_codes = ", ".join(problem["code"] for problem in case["problems"])
        lines.append(
            "| "
            + " | ".join(
                _markdown_cell(value)
                for value in (
                    case["ordinal"] + 1,
                    case["case_id"],
                    case["status"],
                    case["store_state"],
                    "N/A" if case["attempt_count"] is None else case["attempt_count"],
                    case["error_code"] or "",
                    problem_codes,
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Actual external calls",
            "",
            "| Kind | Attempted | Succeeded | Failed | Duration ms | Wait ms |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for kind in EXTERNAL_CALL_KINDS:
        item = audit["actual_calls"]["by_kind"][kind]
        lines.append(
            f"| {kind} | {item['attempted']} | {item['succeeded']} | "
            f"{item['failed']} | {item['duration_ms']} | {item['provider_wait_ms']} |"
        )
    lines.extend(
        [
            "",
            "## Source provenance",
            "",
            "| Kind | Source external calls |",
            "|---|---:|",
        ]
    )
    for kind in EXTERNAL_CALL_KINDS:
        lines.append(f"| {kind} | {audit['source_provenance']['by_kind'][kind]} |")
    lines.extend(
        [
            "",
            "## Cache-mode timing",
            "",
            (
                "Durations are grouped by the attempt's actual cache mode; cache "
                "and replay values are not cold-start latency."
            ),
            "",
            (
                "| Mode | Attempts | Queue total ms | Queue mean ms | "
                "End-to-end total ms | End-to-end mean ms | Actual calls | "
                "Source calls |"
            ),
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for mode, item in audit["by_cache_mode"].items():
        actual_calls = sum(
            item["actual_calls"][kind]["attempted"] for kind in EXTERNAL_CALL_KINDS
        )
        source_calls = sum(item["source_external_calls"].values())
        lines.append(
            f"| {mode} | {item['attempts']} | {item['queue_wait_ms']} | "
            f"{item['mean_queue_wait_ms']} | {item['end_to_end_ms']} | "
            f"{item['mean_end_to_end_ms']} | {actual_calls} | {source_calls} |"
        )
    lines.extend(
        [
            "",
            "## Execution environments",
            "",
            "| Fingerprint | Attempts | Summary |",
            "|---|---:|---|",
        ]
    )
    for fingerprint, item in audit["environments"].items():
        summary = canonical_json_bytes(item["execution_environment"]).decode("utf-8")
        lines.append(
            f"| `{fingerprint}` | {item['attempts']} | {_markdown_cell(summary)} |"
        )
    lines.extend(
        [
            "",
            "## Model usage completeness",
            "",
            "| Role | Calls | Failed | Usage reported | Complete | Total tokens |",
            "|---|---:|---:|---:|---|---:|",
        ]
    )
    for role in MODEL_USAGE_ROLES:
        usage = audit["model_usage"][role]
        total_tokens = usage["complete_token_totals"]["total_tokens"]
        lines.append(
            f"| {role} | {usage['calls']} | {usage['failed_calls']} | "
            f"{usage['token_usage_calls']} | "
            f"{str(usage['token_usage_complete']).lower()} | "
            f"{'N/A' if total_tokens is None else total_tokens} |"
        )
    lines.extend(
        [
            "",
            "## Scoring",
            "",
            (
                "Only trusted succeeded cases are scored. "
                f"Denominator: {validated['scoring']['trusted_succeeded_case_count']}."
            ),
            "",
            ("| Metric | Numerator | Denominator | Value | N/A count | N/A reasons |"),
            "|---|---:|---:|---:|---:|---|",
        ]
    )
    for metric_name, metric in validated["scoring"]["canonical_metrics"].items():
        if metric["kind"] not in {"mean", "ratio"}:
            continue
        value = "N/A" if metric["value"] is None else metric["value"]
        reasons = ", ".join(
            f"{reason}={count}"
            for reason, count in metric["unavailable_reasons"].items()
        )
        lines.append(
            f"| {metric_name} | {metric['numerator']} | {metric['denominator']} | "
            f"{value} | {metric['unavailable_count']} | "
            f"{_markdown_cell(reasons or metric['unavailable_reason'] or '')} |"
        )
    return "\n".join(lines) + "\n"
