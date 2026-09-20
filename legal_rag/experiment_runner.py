from __future__ import annotations

import json
import math
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, TypeVar

from .experiment_runtime import (
    CACHE_MODES,
    EXTERNAL_CALL_KINDS,
    ExperimentContractError,
    canonical_hash,
    canonical_json_bytes,
    validate_experiment_manifest,
)
from .experiment_store import ArtifactInventory, ExperimentStore


RUNNER_RESULT_SCHEMA_VERSION = 2
OBSERVATION_STAGES = (
    "query_analysis",
    "query_embedding",
    "retrieval",
    "rerank",
    "generation",
    "verification",
    "judge",
)
STAGE_STATUSES = frozenset({"succeeded", "error", "not_run"})
STAGE_ORIGINS = frozenset({"fresh", "cache", "replay", "not_run"})
_ALLOWED_STAGE_ORIGINS_BY_CACHE_MODE = {
    "fresh": frozenset({"fresh", "not_run"}),
    "cache": frozenset({"fresh", "cache", "not_run"}),
    "replay": frozenset({"fresh", "replay", "not_run"}),
}
ATTEMPT_TERMINAL_STATUSES = frozenset({"failed", "interrupted"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_T = TypeVar("_T")
_ACTIVE_EXPERIMENTS_LOCK = threading.Lock()
_ACTIVE_EXPERIMENTS: set[str] = set()
MODEL_USAGE_ROLES = ("assistant", "normalizer", "judge")
_MODEL_USAGE_FIELDS = frozenset(
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
_MODEL_USAGE_ROLE_TO_CALL_KIND = {
    "assistant": "generation",
    "normalizer": "normalizer",
    "judge": "judge",
}


class RunnerContractError(ValueError):
    """Raised when runner state cannot prove a safe execution or resume."""


class RunnerBlockedError(RuntimeError):
    """Raised before runtime construction when artifacts are unsafe to execute."""

    def __init__(self, inventory: ArtifactInventory) -> None:
        super().__init__("experiment artifacts block safe execution")
        self.inventory = inventory


class RunnerStopped(RuntimeError):
    """Internal signal for a stop observed after a case attempt has started."""


class CaseExecutionError(RuntimeError):
    """A structured case failure that controls retry and persisted status."""

    def __init__(
        self,
        error_code: str,
        *,
        retryable: bool,
        interrupted: bool = False,
        stage_observations: Mapping[str, StageObservation] | None = None,
    ) -> None:
        if not isinstance(error_code, str) or not _SAFE_ERROR_CODE.fullmatch(
            error_code
        ):
            raise ExperimentContractError(
                "case execution error_code must be a lowercase identifier"
            )
        if not isinstance(retryable, bool) or not isinstance(interrupted, bool):
            raise ExperimentContractError(
                "case execution retryable/interrupted flags must be booleans"
            )
        super().__init__(error_code)
        self.error_code = error_code
        self.retryable = retryable
        self.interrupted = interrupted
        self.stage_observations = stage_observations


def _json_copy(value: Any) -> Any:
    return json.loads(canonical_json_bytes(value).decode("utf-8"))


def _require_non_empty_string(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RunnerContractError(f"{name} must be a non-empty string")
    return value


def _require_non_negative_number(name: str, value: Any) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise RunnerContractError(f"{name} must be a finite non-negative number")
    return round(float(value), 3)


def _zero_counts() -> dict[str, int]:
    return {kind: 0 for kind in EXTERNAL_CALL_KINDS}


def _validate_stage_origin_for_cache_mode(origin: str, cache_mode: str) -> None:
    try:
        allowed_origins = _ALLOWED_STAGE_ORIGINS_BY_CACHE_MODE[cache_mode]
    except KeyError as error:
        raise RunnerContractError(f"unsupported cache mode: {cache_mode!r}") from error
    if origin not in allowed_origins:
        raise RunnerContractError("stage origin conflicts with attempt cache mode")


def _validated_counts(name: str, value: Mapping[str, Any]) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise RunnerContractError(f"{name} must be an object")
    unknown = set(value) - set(EXTERNAL_CALL_KINDS)
    if unknown:
        raise RunnerContractError(
            f"{name} contains unknown call kinds: {sorted(unknown)}"
        )
    counts = _zero_counts()
    for kind, count in value.items():
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise RunnerContractError(f"{name}.{kind} must be a non-negative integer")
        counts[kind] = count
    return counts


def _zero_model_usage() -> dict[str, int | float]:
    return {
        "calls": 0,
        "failed_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "token_usage_calls": 0,
        "latency_ms": 0.0,
    }


def _validated_model_usage(
    name: str,
    value: Mapping[str, Any],
) -> dict[str, int | float]:
    if not isinstance(value, Mapping) or set(value) != _MODEL_USAGE_FIELDS:
        raise RunnerContractError(f"{name} fields are invalid")
    result = _zero_model_usage()
    for field_name in _MODEL_USAGE_FIELDS - {"latency_ms"}:
        item = value[field_name]
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise RunnerContractError(
                f"{name}.{field_name} must be a non-negative integer"
            )
        result[field_name] = item
    result["latency_ms"] = _require_non_negative_number(
        f"{name}.latency_ms", value["latency_ms"]
    )
    calls = int(result["calls"])
    failed_calls = int(result["failed_calls"])
    token_usage_calls = int(result["token_usage_calls"])
    if failed_calls > calls:
        raise RunnerContractError(f"{name}.failed_calls cannot exceed calls")
    if token_usage_calls > calls:
        raise RunnerContractError(f"{name}.token_usage_calls cannot exceed calls")
    token_fields = ("input_tokens", "output_tokens", "total_tokens")
    if token_usage_calls == 0 and any(int(result[field]) for field in token_fields):
        raise RunnerContractError(f"{name} tokens require a token usage record")
    if int(result["total_tokens"]) < (
        int(result["input_tokens"]) + int(result["output_tokens"])
    ):
        raise RunnerContractError(
            f"{name}.total_tokens cannot be less than input plus output"
        )
    return result


def _validated_model_usage_ledger(
    value: Mapping[str, Any],
) -> dict[str, dict[str, int | float]]:
    if not isinstance(value, Mapping) or set(value) != set(MODEL_USAGE_ROLES):
        raise RunnerContractError("model_usage roles are invalid")
    return {
        role: _validated_model_usage(f"model_usage.{role}", value[role])
        for role in MODEL_USAGE_ROLES
    }


def _validate_model_usage_call_ledger(
    model_usage: Mapping[str, Mapping[str, Any]],
    actual_ledger: Mapping[str, Mapping[str, Any]],
) -> None:
    mismatched = []
    for role, kind in _MODEL_USAGE_ROLE_TO_CALL_KIND.items():
        usage = model_usage[role]
        actual = actual_ledger[kind]
        if usage["calls"] != actual["attempted"]:
            mismatched.append(f"{role}.calls")
        if usage["failed_calls"] != actual["failed"]:
            mismatched.append(f"{role}.failed_calls")
    if mismatched:
        raise RunnerContractError(
            "model usage disagrees with provider call ledger: " + ",".join(mismatched)
        )


@dataclass(frozen=True)
class StageObservation:
    status: str
    origin: str
    duration_ms: float | None
    cache_key: str | None = None
    source_external_calls: Mapping[str, int] = field(default_factory=dict)
    error_code: str | None = None
    unavailable_reason: str | None = None

    @classmethod
    def not_run(cls, reason: str) -> StageObservation:
        return cls(
            status="not_run",
            origin="not_run",
            duration_ms=None,
            unavailable_reason=reason,
        )

    def to_dict(
        self,
        *,
        external_calls: Mapping[str, int] | None = None,
        failed_external_calls: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        if self.status not in STAGE_STATUSES:
            raise RunnerContractError(f"unsupported stage status: {self.status!r}")
        if self.origin not in STAGE_ORIGINS:
            raise RunnerContractError(f"unsupported stage origin: {self.origin!r}")
        actual = _validated_counts("external_calls", external_calls or {})
        failed = _validated_counts("failed_external_calls", failed_external_calls or {})
        source = _validated_counts("source_external_calls", self.source_external_calls)
        for kind in EXTERNAL_CALL_KINDS:
            if failed[kind] > actual[kind]:
                raise RunnerContractError(
                    f"failed external calls exceed attempts for {kind}"
                )
        if self.status == "not_run":
            if self.origin != "not_run" or self.duration_ms is not None:
                raise RunnerContractError(
                    "not_run stage must use not_run origin and null duration"
                )
            _require_non_empty_string(
                "stage unavailable_reason", self.unavailable_reason
            )
            if (
                self.error_code is not None
                or any(actual.values())
                or any(source.values())
            ):
                raise RunnerContractError(
                    "not_run stage cannot contain errors or call history"
                )
            duration: float | None = None
        else:
            duration = _require_non_negative_number(
                "stage duration_ms", self.duration_ms
            )
            if self.origin == "not_run" or self.unavailable_reason is not None:
                raise RunnerContractError(
                    "executed stage cannot use not_run origin or unavailable reason"
                )
            if self.status == "error":
                if not isinstance(
                    self.error_code, str
                ) or not _SAFE_ERROR_CODE.fullmatch(self.error_code):
                    raise RunnerContractError(
                        "error stage requires a lowercase error_code"
                    )
            elif self.error_code is not None:
                raise RunnerContractError(
                    "succeeded stage cannot contain an error_code"
                )
        if self.origin in {"cache", "replay"} and any(actual.values()):
            raise RunnerContractError(
                f"{self.origin} stage cannot report current external calls"
            )
        if self.status == "succeeded" and self.origin == "fresh" and source != actual:
            raise RunnerContractError(
                "successful fresh stage source calls must match current attempts"
            )
        if self.cache_key is not None and (
            not isinstance(self.cache_key, str) or not _SHA256.fullmatch(self.cache_key)
        ):
            raise RunnerContractError("stage cache_key must be a lowercase SHA-256")
        return {
            "status": self.status,
            "origin": self.origin,
            "duration_ms": duration,
            "cache_key": self.cache_key,
            "external_calls": actual,
            "failed_external_calls": failed,
            "source_external_calls": source,
            "error_code": self.error_code,
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass(frozen=True)
class CaseExecution:
    output: Mapping[str, Any]
    stage_observations: Mapping[str, StageObservation]
    session_state_after: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class WorkUnit:
    work_unit_id: str
    session_group: str | None
    group_identity_hash: str
    first_ordinal: int
    cases: tuple[dict[str, Any], ...]

    @property
    def case_ids(self) -> tuple[str, ...]:
        return tuple(case["case_id"] for case in self.cases)


@dataclass(frozen=True)
class RunnerSummary:
    status: str
    inventory: ArtifactInventory
    skipped_case_ids: tuple[str, ...]
    reconciled_case_ids: tuple[str, ...]
    attempted_case_ids: tuple[str, ...]
    completed_case_ids: tuple[str, ...]
    terminal_failure_case_ids: tuple[str, ...]
    provider_peak_in_flight: Mapping[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "inventory": self.inventory.to_dict(),
            "skipped_case_ids": list(self.skipped_case_ids),
            "reconciled_case_ids": list(self.reconciled_case_ids),
            "attempted_case_ids": list(self.attempted_case_ids),
            "completed_case_ids": list(self.completed_case_ids),
            "terminal_failure_case_ids": list(self.terminal_failure_case_ids),
            "provider_peak_in_flight": dict(self.provider_peak_in_flight),
        }


class CaseRuntime(Protocol):
    def execute(
        self,
        case: Mapping[str, Any],
        controls: AttemptControls,
    ) -> CaseExecution: ...


class RuntimeFactory(Protocol):
    def __call__(
        self,
        work_unit: WorkUnit,
        resume_state: Mapping[str, Any] | None,
        controls: RunnerControls,
    ) -> CaseRuntime: ...


@dataclass(frozen=True)
class RunnerControls:
    """Non-sensitive policy visible while constructing one runtime.

    Provider permits are intentionally absent. Every provider dispatch must go
    through the attempt-scoped controls passed to ``CaseRuntime.execute``.
    """

    cache_mode: str


class ProviderController:
    """Experiment-wide provider semaphores with attempt-local accounting."""

    def __init__(self, limits: Mapping[str, int]) -> None:
        normalized: dict[str, int] = {}
        for kind in EXTERNAL_CALL_KINDS:
            limit = limits.get(kind)
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
                raise ExperimentContractError(
                    f"provider limit for {kind} must be a positive integer"
                )
            normalized[kind] = limit
        unknown = set(limits) - set(EXTERNAL_CALL_KINDS)
        if unknown:
            raise ExperimentContractError(
                f"unknown provider limit kinds: {sorted(unknown)}"
            )
        self.limits = normalized
        self._semaphores = {
            kind: threading.BoundedSemaphore(limit)
            for kind, limit in normalized.items()
        }
        self._lock = threading.Lock()
        self._active = _zero_counts()
        self._peaks = _zero_counts()

    @contextmanager
    def permit(self, kind: str) -> Iterator[float]:
        if kind not in EXTERNAL_CALL_KINDS:
            raise ExperimentContractError(f"unsupported external call kind: {kind!r}")
        wait_started = time.perf_counter()
        self._semaphores[kind].acquire()
        wait_ms = (time.perf_counter() - wait_started) * 1000
        with self._lock:
            self._active[kind] += 1
            self._peaks[kind] = max(self._peaks[kind], self._active[kind])
        try:
            yield wait_ms
        finally:
            with self._lock:
                self._active[kind] -= 1
            self._semaphores[kind].release()

    @property
    def peaks(self) -> dict[str, int]:
        with self._lock:
            return dict(self._peaks)


class AttemptControls:
    """Controls and records provider calls for exactly one case attempt.

    Runtime implementations must dispatch providers only through :meth:`call`;
    private controller state is not part of the runtime protocol.
    """

    def __init__(
        self,
        provider_controller: ProviderController,
        stop_event: threading.Event,
        *,
        cache_mode: str,
    ) -> None:
        if cache_mode not in CACHE_MODES:
            raise ExperimentContractError(f"unsupported cache mode: {cache_mode!r}")
        self._provider_controller = provider_controller
        self._stop_event = stop_event
        self._cache_mode = cache_mode
        self._lock = threading.Lock()
        self._by_stage = {
            stage: {
                kind: {
                    "attempted": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "duration_ms": 0.0,
                    "provider_wait_ms": 0.0,
                }
                for kind in EXTERNAL_CALL_KINDS
            }
            for stage in OBSERVATION_STAGES
        }
        self._model_usage = {role: _zero_model_usage() for role in MODEL_USAGE_ROLES}

    def call(self, stage: str, kind: str, operation: Callable[[], _T]) -> _T:
        if stage not in OBSERVATION_STAGES:
            raise ExperimentContractError(f"unsupported observation stage: {stage!r}")
        if kind not in EXTERNAL_CALL_KINDS:
            raise ExperimentContractError(f"unsupported external call kind: {kind!r}")
        if self._cache_mode == "replay":
            raise ExperimentContractError(
                "replay mode forbids external calls before provider dispatch"
            )
        if self._stop_event.is_set():
            raise RunnerStopped("experiment stop requested")
        with self._provider_controller.permit(kind) as wait_ms:
            if self._stop_event.is_set():
                raise RunnerStopped("experiment stop requested")
            started = time.perf_counter()
            with self._lock:
                ledger = self._by_stage[stage][kind]
                ledger["attempted"] += 1
                ledger["provider_wait_ms"] += wait_ms
            try:
                result = operation()
            except Exception:
                duration_ms = (time.perf_counter() - started) * 1000
                with self._lock:
                    ledger = self._by_stage[stage][kind]
                    ledger["failed"] += 1
                    ledger["duration_ms"] += duration_ms
                raise
            duration_ms = (time.perf_counter() - started) * 1000
            with self._lock:
                ledger = self._by_stage[stage][kind]
                ledger["succeeded"] += 1
                ledger["duration_ms"] += duration_ms
            return result

    @property
    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    @property
    def cache_mode(self) -> str:
        return self._cache_mode

    def stage_counts(self, stage: str) -> tuple[dict[str, int], dict[str, int]]:
        with self._lock:
            attempted = {
                kind: int(self._by_stage[stage][kind]["attempted"])
                for kind in EXTERNAL_CALL_KINDS
            }
            failed = {
                kind: int(self._by_stage[stage][kind]["failed"])
                for kind in EXTERNAL_CALL_KINDS
            }
        return attempted, failed

    def record_model_usage(
        self,
        role: str,
        usage: Mapping[str, Any],
    ) -> None:
        if role not in MODEL_USAGE_ROLES:
            raise ExperimentContractError(f"unsupported model usage role: {role!r}")
        try:
            normalized = _validated_model_usage(f"model_usage.{role}", usage)
        except RunnerContractError as error:
            raise ExperimentContractError(str(error)) from error
        with self._lock:
            current = self._model_usage[role]
            for field_name in _MODEL_USAGE_FIELDS - {"latency_ms"}:
                current[field_name] = int(current[field_name]) + int(
                    normalized[field_name]
                )
            current["latency_ms"] = float(current["latency_ms"]) + float(
                normalized["latency_ms"]
            )

    def model_usage(self) -> dict[str, dict[str, int | float]]:
        with self._lock:
            snapshot = {
                role: {
                    **{
                        field_name: int(self._model_usage[role][field_name])
                        for field_name in _MODEL_USAGE_FIELDS - {"latency_ms"}
                    },
                    "latency_ms": round(
                        float(self._model_usage[role]["latency_ms"]), 3
                    ),
                }
                for role in MODEL_USAGE_ROLES
            }
        return _validated_model_usage_ledger(snapshot)

    def ledger(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        with self._lock:
            for kind in EXTERNAL_CALL_KINDS:
                attempted = sum(
                    int(self._by_stage[stage][kind]["attempted"])
                    for stage in OBSERVATION_STAGES
                )
                succeeded = sum(
                    int(self._by_stage[stage][kind]["succeeded"])
                    for stage in OBSERVATION_STAGES
                )
                failed = sum(
                    int(self._by_stage[stage][kind]["failed"])
                    for stage in OBSERVATION_STAGES
                )
                duration_ms = sum(
                    float(self._by_stage[stage][kind]["duration_ms"])
                    for stage in OBSERVATION_STAGES
                )
                provider_wait_ms = sum(
                    float(self._by_stage[stage][kind]["provider_wait_ms"])
                    for stage in OBSERVATION_STAGES
                )
                result[kind] = {
                    "attempted": attempted,
                    "succeeded": succeeded,
                    "failed": failed,
                    "duration_ms": round(duration_ms, 3),
                    "provider_wait_ms": round(provider_wait_ms, 3),
                }
        return result


@dataclass(frozen=True)
class _UnitPlan:
    work_unit: WorkUnit
    start_index: int
    resume_state: dict[str, Any] | None


@dataclass(frozen=True)
class _ValidatedAttemptHistory:
    latest_status: str
    state_after: dict[str, Any] | None
    latest_retryable: bool | None


def plan_work_units(manifest: Mapping[str, Any]) -> tuple[WorkUnit, ...]:
    validated = validate_experiment_manifest(manifest)
    raw_cases = validated["dataset"].get("cases")
    if not isinstance(raw_cases, list):
        raise RunnerContractError("manifest dataset cases must be a list")
    cases = tuple(_json_copy(case) for case in raw_cases)
    units: list[WorkUnit] = []
    active_group: str | None = None
    active_cases: list[dict[str, Any]] = []
    closed_groups: set[str] = set()

    def append_unit(unit_cases: list[dict[str, Any]], group: str | None) -> None:
        if not unit_cases:
            return
        identity = {
            "session_group": group,
            "cases": [
                {
                    "ordinal": case["ordinal"],
                    "case_id": case["case_id"],
                    "case_hash": case["case_hash"],
                    "turn_index": case.get("turn_index", 0),
                }
                for case in unit_cases
            ],
        }
        group_hash = canonical_hash(identity)
        units.append(
            WorkUnit(
                work_unit_id=group_hash,
                session_group=group,
                group_identity_hash=group_hash,
                first_ordinal=unit_cases[0]["ordinal"],
                cases=tuple(unit_cases),
            )
        )

    seen_case_ids: set[str] = set()
    for expected_ordinal, case in enumerate(cases):
        if not isinstance(case, dict):
            raise RunnerContractError("manifest dataset case must be an object")
        case_id = _require_non_empty_string("case_id", case.get("case_id"))
        if case_id in seen_case_ids:
            raise RunnerContractError(f"duplicate case_id in manifest: {case_id}")
        seen_case_ids.add(case_id)
        ordinal = case.get("ordinal")
        if (
            isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or ordinal != expected_ordinal
        ):
            raise RunnerContractError("case ordinals must be contiguous from zero")
        case_hash = case.get("case_hash")
        if not isinstance(case_hash, str) or not _SHA256.fullmatch(case_hash):
            raise RunnerContractError("case_hash must be a lowercase SHA-256")
        group = case.get("session_group")
        turn_index = case.get("turn_index", 0)
        if (
            isinstance(turn_index, bool)
            or not isinstance(turn_index, int)
            or turn_index < 0
        ):
            raise RunnerContractError("case turn_index must be a non-negative integer")
        if group is None:
            if turn_index != 0:
                raise RunnerContractError("single case must use turn_index zero")
            if active_cases:
                append_unit(active_cases, active_group)
                if active_group is not None:
                    closed_groups.add(active_group)
                active_cases = []
                active_group = None
            append_unit([case], None)
            continue
        _require_non_empty_string("session_group", group)
        if group != active_group:
            if active_cases:
                append_unit(active_cases, active_group)
                if active_group is not None:
                    closed_groups.add(active_group)
            if group in closed_groups or turn_index != 0:
                raise RunnerContractError(
                    "session groups must be contiguous and start at turn_index zero"
                )
            active_group = group
            active_cases = [case]
            continue
        if turn_index != len(active_cases):
            raise RunnerContractError("session turn_index values must be contiguous")
        active_cases.append(case)
    if active_cases:
        append_unit(active_cases, active_group)
    return tuple(units)


class ExperimentRunner:
    """Resume-safe work-unit scheduler over immutable case artifacts.

    The runner prevents duplicate ownership inside one Python process. A caller
    must still provide a single owner when separate processes target the same
    experiment; cross-process leases belong to the later persistent harness.
    """

    def __init__(
        self,
        *,
        store: ExperimentStore,
        requested_manifest: Mapping[str, Any],
        runtime_factory: RuntimeFactory,
        cache_mode: str,
        execution_environment: Mapping[str, Any],
        retry_sleep: Callable[[float], None] | None = None,
        timestamp: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.manifest = validate_experiment_manifest(requested_manifest)
        self.work_units = plan_work_units(self.manifest)
        self.runtime_factory = runtime_factory
        if cache_mode not in CACHE_MODES:
            raise ExperimentContractError(f"unsupported cache mode: {cache_mode!r}")
        self.cache_mode = cache_mode
        if not isinstance(execution_environment, Mapping):
            raise ExperimentContractError("execution_environment must be an object")
        self.execution_environment = _json_copy(dict(execution_environment))
        if not self.execution_environment:
            raise ExperimentContractError("execution_environment must not be empty")
        runtime = self.manifest["runtime"]
        self.concurrency = runtime["concurrency"]
        self.max_attempts = 1 + runtime["max_retries"]
        provider_limits = runtime.get("provider_limits")
        if provider_limits is None:
            provider_limits = {kind: self.concurrency for kind in EXTERNAL_CALL_KINDS}
        if not isinstance(provider_limits, Mapping):
            raise ExperimentContractError("runtime.provider_limits must be an object")
        self._provider_controller = ProviderController(provider_limits)
        retry_backoff_ms = runtime.get("retry_backoff_ms", 0)
        if (
            isinstance(retry_backoff_ms, bool)
            or not isinstance(retry_backoff_ms, int)
            or retry_backoff_ms < 0
            or retry_backoff_ms > 60_000
        ):
            raise ExperimentContractError(
                "runtime.retry_backoff_ms must be an integer between 0 and 60000"
            )
        self.retry_backoff_seconds = retry_backoff_ms / 1000
        self.retry_sleep = retry_sleep
        self.timestamp = timestamp or self._utc_now
        self._stop_event = threading.Event()
        self._result_lock = threading.Lock()
        self._attempted: set[str] = set()
        self._completed: set[str] = set()
        self._reconciled: set[str] = set()
        self._terminal_failures: set[str] = set()
        self._completed_count = 0
        self._stop_after_completed: int | None = None
        self._has_run = False
        self._runtime_instances: dict[int, tuple[str, CaseRuntime]] = {}

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def request_stop(self) -> None:
        self._stop_event.set()

    @contextmanager
    def _claim_local_experiment(self) -> Iterator[None]:
        key = str(self.store.directory.resolve())
        with _ACTIVE_EXPERIMENTS_LOCK:
            if key in _ACTIVE_EXPERIMENTS:
                raise RunnerContractError(
                    "experiment already has an active runner in this process"
                )
            _ACTIVE_EXPERIMENTS.add(key)
        try:
            yield
        finally:
            with _ACTIVE_EXPERIMENTS_LOCK:
                _ACTIVE_EXPERIMENTS.discard(key)

    def run(self, *, stop_after_completed: int | None = None) -> RunnerSummary:
        with self._claim_local_experiment():
            return self._run_claimed(stop_after_completed=stop_after_completed)

    def _run_claimed(
        self,
        *,
        stop_after_completed: int | None,
    ) -> RunnerSummary:
        if self._has_run:
            raise ExperimentContractError(
                "ExperimentRunner instances are single-use; create a new runner to resume"
            )
        self._has_run = True
        if stop_after_completed is not None:
            if (
                isinstance(stop_after_completed, bool)
                or not isinstance(stop_after_completed, int)
                or stop_after_completed < 1
            ):
                raise ExperimentContractError(
                    "stop_after_completed must be a positive integer"
                )
            if self.concurrency != 1:
                raise ExperimentContractError(
                    "deterministic stop_after_completed requires concurrency one"
                )
        self._stop_after_completed = stop_after_completed
        inventory = self.store.prepare_resume(self.manifest)
        if inventory.corrupt or inventory.global_problems:
            raise RunnerBlockedError(inventory)
        initial_succeeded = set(inventory.succeeded)
        plans = self._prepare_unit_plans(inventory)
        runnable_plans = [plan for plan in plans if plan is not None]
        submitted_at = time.perf_counter()

        if runnable_plans:
            with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
                futures = {
                    executor.submit(self._run_unit, plan, submitted_at): plan
                    for plan in runnable_plans
                }
                for future in as_completed(futures):
                    future.result()

        final_inventory = self.store.scan()
        if final_inventory.corrupt or final_inventory.global_problems:
            raise RunnerBlockedError(final_inventory)
        if len(final_inventory.succeeded) == final_inventory.expected:
            status = "succeeded"
        elif self._stop_event.is_set():
            status = "interrupted"
        elif final_inventory.exhausted or self._terminal_failures:
            status = "completed_with_failures"
        else:
            status = "partial"
        order = final_inventory.case_order

        def ordered(values: set[str]) -> tuple[str, ...]:
            return tuple(case_id for case_id in order if case_id in values)

        return RunnerSummary(
            status=status,
            inventory=final_inventory,
            skipped_case_ids=ordered(initial_succeeded),
            reconciled_case_ids=ordered(self._reconciled),
            attempted_case_ids=ordered(self._attempted),
            completed_case_ids=ordered(self._completed),
            terminal_failure_case_ids=ordered(self._terminal_failures),
            provider_peak_in_flight=self._provider_controller.peaks,
        )

    def _prepare_unit_plans(
        self,
        inventory: ArtifactInventory,
    ) -> list[_UnitPlan | None]:
        categories: dict[str, str] = {}
        for name in (
            "succeeded",
            "failed",
            "pending_commit",
            "not_run",
            "interrupted",
            "exhausted",
        ):
            for case_id in getattr(inventory, name):
                if case_id in categories:
                    raise RunnerContractError(
                        f"case appears in multiple inventory categories: {case_id}"
                    )
                categories[case_id] = name
        plans: list[_UnitPlan | None] = []
        for unit in self.work_units:
            state: dict[str, Any] | None = None
            first_incomplete: int | None = None
            for index, case in enumerate(unit.cases):
                case_id = case["case_id"]
                category = categories.get(case_id)
                if category is None:
                    raise RunnerContractError(
                        f"case is missing from artifact inventory: {case_id}"
                    )
                if category in {"succeeded", "pending_commit"}:
                    if first_incomplete is not None:
                        raise RunnerContractError(
                            "session completion state must be a contiguous prefix"
                        )
                    history = self._validate_attempt_history(
                        case=case,
                        unit=unit,
                        state_before=state,
                    )
                    if history.latest_status != "succeeded":
                        raise RunnerContractError(
                            f"completed case has no final succeeded attempt: {case_id}"
                        )
                    state = history.state_after
                    if category == "pending_commit":
                        self.store.commit_pending(case_id)
                        self._reconciled.add(case_id)
                    continue
                if first_incomplete is None:
                    first_incomplete = index
                    if category in {"failed", "interrupted"}:
                        history = self._validate_attempt_history(
                            case=case,
                            unit=unit,
                            state_before=state,
                        )
                        if history.latest_status != category:
                            raise RunnerContractError(
                                f"artifact inventory status drifted for {case_id}"
                            )
                        if history.latest_retryable is not True:
                            self._require_not_run_suffix(
                                unit,
                                after_index=index,
                                categories=categories,
                            )
                            self._terminal_failures.add(case_id)
                            first_incomplete = None
                            break
                    elif category == "exhausted":
                        self._require_not_run_suffix(
                            unit,
                            after_index=index,
                            categories=categories,
                        )
                        history = self._validate_attempt_history(
                            case=case,
                            unit=unit,
                            state_before=state,
                        )
                        if history.latest_status not in ATTEMPT_TERMINAL_STATUSES:
                            raise RunnerContractError(
                                f"exhausted case has invalid final status: {case_id}"
                            )
                        self._terminal_failures.add(case_id)
                        first_incomplete = None
                        break
                    continue
                if category != "not_run":
                    raise RunnerContractError(
                        "later session turns cannot have attempts before their predecessor"
                    )
            if first_incomplete is None:
                plans.append(None)
            else:
                plans.append(
                    _UnitPlan(
                        work_unit=unit,
                        start_index=first_incomplete,
                        resume_state=_json_copy(state) if state is not None else None,
                    )
                )
        return plans

    def _validate_attempt_history(
        self,
        *,
        case: Mapping[str, Any],
        unit: WorkUnit,
        state_before: dict[str, Any] | None,
    ) -> _ValidatedAttemptHistory:
        attempts = self.store.load_attempts(case["case_id"])
        if not attempts:
            raise RunnerContractError(
                f"persisted case has no attempt: {case['case_id']}"
            )
        state_after = state_before
        latest_retryable: bool | None = None
        for index, payload in enumerate(attempts):
            status = payload.get("status")
            if status == "succeeded":
                if index != len(attempts) - 1:
                    raise RunnerContractError(
                        "succeeded attempt must be the final attempt in its history"
                    )
                validated_state = self._validate_persisted_result(
                    payload,
                    case=case,
                    unit=unit,
                    state_before=state_before,
                    expected_status="succeeded",
                )
                if validated_state is not None and not isinstance(
                    validated_state, dict
                ):
                    raise RunnerContractError(
                        "successful attempt returned an invalid session state"
                    )
                state_after = validated_state
                latest_retryable = None
                continue
            if status not in ATTEMPT_TERMINAL_STATUSES:
                raise RunnerContractError("persisted attempt status is unsupported")
            retryable = self._validate_persisted_result(
                payload,
                case=case,
                unit=unit,
                state_before=state_before,
                expected_status=status,
            )
            if not isinstance(retryable, bool):
                raise RunnerContractError(
                    "failed attempt did not contain a retry decision"
                )
            if index != len(attempts) - 1 and not retryable:
                raise RunnerContractError(
                    "non-retryable attempt cannot be followed by another attempt"
                )
            latest_retryable = retryable
        return _ValidatedAttemptHistory(
            latest_status=attempts[-1]["status"],
            state_after=_json_copy(state_after) if state_after is not None else None,
            latest_retryable=latest_retryable,
        )

    @staticmethod
    def _require_not_run_suffix(
        unit: WorkUnit,
        *,
        after_index: int,
        categories: Mapping[str, str],
    ) -> None:
        for later_case in unit.cases[after_index + 1 :]:
            later_case_id = later_case["case_id"]
            later_category = categories.get(later_case_id)
            if later_category is None:
                raise RunnerContractError(
                    f"case is missing from artifact inventory: {later_case_id}"
                )
            if later_category != "not_run":
                raise RunnerContractError(
                    "later session turns cannot have attempts before their predecessor"
                )

    def _register_runtime(
        self,
        runtime: CaseRuntime,
        *,
        unit: WorkUnit,
        attempt_number: int,
    ) -> CaseRuntime:
        if not callable(getattr(runtime, "execute", None)):
            raise RunnerContractError(
                "runtime factory must return an executable runtime"
            )
        runtime_id = id(runtime)
        construction = f"{unit.work_unit_id}:attempt-{attempt_number}"
        with self._result_lock:
            existing = self._runtime_instances.get(runtime_id)
            if existing is not None and existing[1] is runtime:
                raise RunnerContractError(
                    "runtime factory must return a fresh runtime for each construction"
                )
            self._runtime_instances[runtime_id] = (construction, runtime)
        return runtime

    def _run_unit(self, plan: _UnitPlan, submitted_at: float) -> None:
        if self._stop_event.is_set():
            return
        unit = plan.work_unit
        state = _json_copy(plan.resume_state) if plan.resume_state is not None else None
        runtime: CaseRuntime | None = None
        ready_at = submitted_at
        for case in unit.cases[plan.start_index :]:
            if self._stop_event.is_set():
                break
            queue_wait_ms = (time.perf_counter() - ready_at) * 1000
            succeeded, state, runtime = self._run_case(
                unit=unit,
                case=case,
                state_before=state,
                runtime=runtime,
                queue_wait_ms=queue_wait_ms,
            )
            if not succeeded:
                break
            ready_at = time.perf_counter()
            with self._result_lock:
                self._completed_count += 1
                if (
                    self._stop_after_completed is not None
                    and self._completed_count >= self._stop_after_completed
                ):
                    self._stop_event.set()

    def _run_case(
        self,
        *,
        unit: WorkUnit,
        case: Mapping[str, Any],
        state_before: dict[str, Any] | None,
        runtime: CaseRuntime | None,
        queue_wait_ms: float,
    ) -> tuple[bool, dict[str, Any] | None, CaseRuntime | None]:
        case_id = case["case_id"]
        while True:
            attempts = self.store.load_attempts(case_id)
            attempt_number = len(attempts) + 1
            if attempt_number > self.max_attempts:
                with self._result_lock:
                    self._terminal_failures.add(case_id)
                return False, state_before, None
            # A requested-fresh first attempt publishes exact immutable stages.
            # Its retries reuse that successful prefix and execute only cache
            # misses; recomputing a stochastic prefix could otherwise conflict
            # with the entry just published by the failed attempt.
            attempt_cache_mode = (
                "cache"
                if self.cache_mode == "fresh" and attempt_number > 1
                else self.cache_mode
            )
            attempt_started = time.perf_counter()
            controls = AttemptControls(
                self._provider_controller,
                self._stop_event,
                cache_mode=attempt_cache_mode,
            )
            with self._result_lock:
                self._attempted.add(case_id)
            try:
                if runtime is None:
                    runtime = self._register_runtime(
                        self.runtime_factory(
                            unit,
                            _json_copy(state_before)
                            if state_before is not None
                            else None,
                            RunnerControls(attempt_cache_mode),
                        ),
                        unit=unit,
                        attempt_number=attempt_number,
                    )
                if self._stop_event.is_set():
                    raise RunnerStopped("experiment stop requested")
                outcome = runtime.execute(_json_copy(dict(case)), controls)
                result, next_state = self._success_result(
                    outcome,
                    unit=unit,
                    case=case,
                    state_before=state_before,
                    controls=controls,
                    queue_wait_ms=queue_wait_ms,
                    attempt_started=attempt_started,
                )
            except RunnerStopped:
                error = CaseExecutionError(
                    "controlled_interrupt", retryable=True, interrupted=True
                )
                result = self._failure_result(
                    error,
                    unit=unit,
                    case=case,
                    state_before=state_before,
                    controls=controls,
                    queue_wait_ms=queue_wait_ms,
                    attempt_started=attempt_started,
                )
                status = "interrupted"
                retryable = True
                runtime = None
            except CaseExecutionError as error:
                result = self._failure_result(
                    error,
                    unit=unit,
                    case=case,
                    state_before=state_before,
                    controls=controls,
                    queue_wait_ms=queue_wait_ms,
                    attempt_started=attempt_started,
                )
                status = "interrupted" if error.interrupted else "failed"
                retryable = error.retryable
                runtime = None
            except (ExperimentContractError, RunnerContractError):
                error = CaseExecutionError("executor_contract_error", retryable=False)
                result = self._failure_result(
                    error,
                    unit=unit,
                    case=case,
                    state_before=state_before,
                    controls=controls,
                    queue_wait_ms=queue_wait_ms,
                    attempt_started=attempt_started,
                )
                status = "failed"
                retryable = False
                runtime = None
            except Exception:
                error = CaseExecutionError("unhandled_executor_error", retryable=False)
                result = self._failure_result(
                    error,
                    unit=unit,
                    case=case,
                    state_before=state_before,
                    controls=controls,
                    queue_wait_ms=queue_wait_ms,
                    attempt_started=attempt_started,
                )
                status = "failed"
                retryable = False
                runtime = None
            else:
                status = "succeeded"
                retryable = False

            if attempt_cache_mode == "replay" and any(
                self._provider_controller.peaks.values()
            ):
                error = CaseExecutionError(
                    "replay_provider_bypass",
                    retryable=False,
                )
                result = self._failure_result(
                    error,
                    unit=unit,
                    case=case,
                    state_before=state_before,
                    controls=controls,
                    queue_wait_ms=queue_wait_ms,
                    attempt_started=attempt_started,
                )
                status = "failed"
                retryable = False
                runtime = None

            self.store.write_attempt(
                case_id,
                attempt=attempt_number,
                status=status,
                recorded_at=self.timestamp(),
                cache_mode=attempt_cache_mode,
                execution_environment=self.execution_environment,
                result=result,
            )
            if status == "succeeded":
                self.store.mark_complete(case_id, attempt=attempt_number)
                with self._result_lock:
                    self._completed.add(case_id)
                next_runtime = (
                    runtime if attempt_cache_mode == self.cache_mode else None
                )
                return True, next_state, next_runtime
            if not retryable or attempt_number >= self.max_attempts:
                with self._result_lock:
                    self._terminal_failures.add(case_id)
                return False, state_before, None
            if self._stop_event.is_set():
                return False, state_before, None
            if self.retry_backoff_seconds:
                if self._stop_event.is_set():
                    return False, state_before, None
                if self.retry_sleep is None:
                    if self._stop_event.wait(self.retry_backoff_seconds):
                        return False, state_before, None
                else:
                    self.retry_sleep(self.retry_backoff_seconds)
                if self._stop_event.is_set():
                    return False, state_before, None
            queue_wait_ms = 0.0

    def _success_result(
        self,
        outcome: CaseExecution,
        *,
        unit: WorkUnit,
        case: Mapping[str, Any],
        state_before: dict[str, Any] | None,
        controls: AttemptControls,
        queue_wait_ms: float,
        attempt_started: float,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if controls.cache_mode == "replay" and any(
            self._provider_controller.peaks.values()
        ):
            raise RunnerContractError(
                "replay runtime used provider permits outside attempt controls"
            )
        if not isinstance(outcome, CaseExecution):
            raise RunnerContractError("runtime must return CaseExecution")
        if not isinstance(outcome.output, Mapping):
            raise RunnerContractError("case output must be an object")
        output = _json_copy(dict(outcome.output))
        if unit.session_group is None:
            if outcome.session_state_after is not None:
                raise RunnerContractError(
                    "single-case work unit cannot publish session state"
                )
            next_state = None
        else:
            if not isinstance(outcome.session_state_after, Mapping):
                raise RunnerContractError(
                    "session work unit must publish session_state_after"
                )
            next_state = _json_copy(dict(outcome.session_state_after))
        observations = self._observations_to_dict(
            outcome.stage_observations,
            controls=controls,
        )
        result = self._result_payload(
            unit=unit,
            case=case,
            state_before=state_before,
            state_after=next_state,
            output=output,
            observations=observations,
            controls=controls,
            queue_wait_ms=queue_wait_ms,
            end_to_end_ms=(time.perf_counter() - attempt_started) * 1000,
            error=None,
        )
        return result, next_state

    def _failure_result(
        self,
        error: CaseExecutionError,
        *,
        unit: WorkUnit,
        case: Mapping[str, Any],
        state_before: dict[str, Any] | None,
        controls: AttemptControls,
        queue_wait_ms: float,
        attempt_started: float,
    ) -> dict[str, Any]:
        observations = error.stage_observations
        if observations is None:
            generated: dict[str, StageObservation] = {}
            for stage in OBSERVATION_STAGES:
                attempted, _ = controls.stage_counts(stage)
                if any(attempted.values()):
                    generated[stage] = StageObservation(
                        status="error",
                        origin="fresh",
                        duration_ms=(time.perf_counter() - attempt_started) * 1000,
                        error_code=error.error_code,
                    )
                else:
                    generated[stage] = StageObservation.not_run("case_execution_failed")
            observations = generated
        return self._result_payload(
            unit=unit,
            case=case,
            state_before=state_before,
            state_after=None,
            output=None,
            observations=self._observations_to_dict(
                observations,
                controls=controls,
            ),
            controls=controls,
            queue_wait_ms=queue_wait_ms,
            end_to_end_ms=(time.perf_counter() - attempt_started) * 1000,
            error={"code": error.error_code, "retryable": error.retryable},
        )

    def _observations_to_dict(
        self,
        observations: Mapping[str, StageObservation],
        *,
        controls: AttemptControls,
    ) -> dict[str, Any]:
        if not isinstance(observations, Mapping) or set(observations) != set(
            OBSERVATION_STAGES
        ):
            raise RunnerContractError(
                "stage observations must contain every required stage exactly once"
            )
        result: dict[str, Any] = {}
        for stage in OBSERVATION_STAGES:
            observation = observations[stage]
            if not isinstance(observation, StageObservation):
                raise RunnerContractError(
                    f"stage observation must be StageObservation: {stage}"
                )
            attempted, failed = controls.stage_counts(stage)
            normalized = observation.to_dict(
                external_calls=attempted,
                failed_external_calls=failed,
            )
            _validate_stage_origin_for_cache_mode(
                normalized["origin"], controls.cache_mode
            )
            result[stage] = normalized
        return result

    def _result_payload(
        self,
        *,
        unit: WorkUnit,
        case: Mapping[str, Any],
        state_before: dict[str, Any] | None,
        state_after: dict[str, Any] | None,
        output: dict[str, Any] | None,
        observations: Mapping[str, Any],
        controls: AttemptControls,
        queue_wait_ms: float,
        end_to_end_ms: float,
        error: dict[str, Any] | None,
    ) -> dict[str, Any]:
        index = next(
            index
            for index, unit_case in enumerate(unit.cases)
            if unit_case["case_id"] == case["case_id"]
        )
        checkpoint: dict[str, Any] | None
        if unit.session_group is None:
            checkpoint = None
        else:
            checkpoint = {
                "previous_case_id": (
                    unit.cases[index - 1]["case_id"] if index > 0 else None
                ),
                "state_before_sha256": canonical_hash(state_before),
                "state_after": state_after,
                "state_after_sha256": (
                    canonical_hash(state_after) if state_after is not None else None
                ),
            }
        source_totals = _zero_counts()
        for stage in OBSERVATION_STAGES:
            for kind in EXTERNAL_CALL_KINDS:
                source_totals[kind] += observations[stage]["source_external_calls"][
                    kind
                ]
        actual_ledger = controls.ledger()
        model_usage = controls.model_usage()
        _validate_model_usage_call_ledger(
            model_usage,
            actual_ledger,
        )
        if controls.cache_mode == "replay" and any(
            item["attempted"] for item in actual_ledger.values()
        ):
            raise RunnerContractError(
                "replay result cannot contain current external calls"
            )
        return {
            "runner_schema_version": RUNNER_RESULT_SCHEMA_VERSION,
            "case": {
                "case_id": case["case_id"],
                "case_hash": case["case_hash"],
                "ordinal": case["ordinal"],
                "session_group": unit.session_group,
                "turn_index": case.get("turn_index", 0),
            },
            "work_unit": {
                "work_unit_id": unit.work_unit_id,
                "group_identity_hash": unit.group_identity_hash,
            },
            "session_checkpoint": checkpoint,
            "output": output,
            "stage_observations": _json_copy(dict(observations)),
            "call_ledger": {
                "actual": actual_ledger,
                "source": source_totals,
            },
            "model_usage": model_usage,
            "timings_ms": {
                "queue_wait": _require_non_negative_number(
                    "queue_wait_ms", queue_wait_ms
                ),
                "end_to_end": _require_non_negative_number(
                    "end_to_end_ms", end_to_end_ms
                ),
                "persistence_included": False,
            },
            "error": error,
        }

    def _validate_persisted_result(
        self,
        attempt_payload: Mapping[str, Any],
        *,
        case: Mapping[str, Any],
        unit: WorkUnit,
        state_before: dict[str, Any] | None,
        expected_status: str,
    ) -> dict[str, Any] | bool | None:
        if attempt_payload.get("status") != expected_status:
            raise RunnerContractError(
                f"persisted attempt status mismatch for {case['case_id']}"
            )
        result = attempt_payload.get("result")
        required = {
            "runner_schema_version",
            "case",
            "work_unit",
            "session_checkpoint",
            "output",
            "stage_observations",
            "call_ledger",
            "model_usage",
            "timings_ms",
            "error",
        }
        if not isinstance(result, dict) or set(result) != required:
            raise RunnerContractError(
                f"persisted runner result fields are invalid for {case['case_id']}"
            )
        if (
            type(result["runner_schema_version"]) is not int
            or result["runner_schema_version"] != RUNNER_RESULT_SCHEMA_VERSION
        ):
            raise RunnerContractError("persisted runner result schema is unsupported")
        expected_case = {
            "case_id": case["case_id"],
            "case_hash": case["case_hash"],
            "ordinal": case["ordinal"],
            "session_group": unit.session_group,
            "turn_index": case.get("turn_index", 0),
        }
        expected_unit = {
            "work_unit_id": unit.work_unit_id,
            "group_identity_hash": unit.group_identity_hash,
        }
        if canonical_json_bytes(result["case"]) != canonical_json_bytes(
            expected_case
        ) or canonical_json_bytes(result["work_unit"]) != canonical_json_bytes(
            expected_unit
        ):
            raise RunnerContractError(
                "persisted runner case/work-unit identity mismatch"
            )
        attempt_cache_mode = attempt_payload.get("cache_mode")
        if attempt_cache_mode not in CACHE_MODES:
            raise RunnerContractError("persisted attempt cache mode is invalid")
        self._validate_persisted_observations(
            result["stage_observations"],
            result["call_ledger"],
            attempt_cache_mode=attempt_cache_mode,
        )
        timings = result["timings_ms"]
        if not isinstance(timings, dict) or set(timings) != {
            "queue_wait",
            "end_to_end",
            "persistence_included",
        }:
            raise RunnerContractError("persisted timing fields are invalid")
        _require_non_negative_number("queue_wait", timings["queue_wait"])
        _require_non_negative_number("end_to_end", timings["end_to_end"])
        if timings["persistence_included"] is not False:
            raise RunnerContractError(
                "runner end_to_end timing must explicitly exclude persistence"
            )
        checkpoint = result["session_checkpoint"]
        if unit.session_group is None:
            if checkpoint is not None:
                raise RunnerContractError(
                    "single case persisted unexpected session state"
                )
            next_state = None
        else:
            if not isinstance(checkpoint, dict) or set(checkpoint) != {
                "previous_case_id",
                "state_before_sha256",
                "state_after",
                "state_after_sha256",
            }:
                raise RunnerContractError("persisted session checkpoint is invalid")
            index = next(
                index
                for index, item in enumerate(unit.cases)
                if item["case_id"] == case["case_id"]
            )
            expected_previous = unit.cases[index - 1]["case_id"] if index > 0 else None
            if checkpoint["previous_case_id"] != expected_previous:
                raise RunnerContractError("session checkpoint predecessor mismatch")
            if checkpoint["state_before_sha256"] != canonical_hash(state_before):
                raise RunnerContractError("session checkpoint state chain is broken")
            next_state = checkpoint["state_after"]
            if expected_status == "succeeded":
                if not isinstance(next_state, dict):
                    raise RunnerContractError(
                        "successful session checkpoint must contain state_after"
                    )
                if checkpoint["state_after_sha256"] != canonical_hash(next_state):
                    raise RunnerContractError("session checkpoint checksum mismatch")
            elif next_state is not None or checkpoint["state_after_sha256"] is not None:
                raise RunnerContractError(
                    "failed session attempt cannot advance session state"
                )
        if expected_status == "succeeded":
            if not isinstance(result["output"], dict) or result["error"] is not None:
                raise RunnerContractError("successful runner result is incomplete")
            self._validate_persisted_model_usage(
                result["model_usage"],
                result["call_ledger"],
            )
            return _json_copy(next_state) if next_state is not None else None
        if result["output"] is not None:
            raise RunnerContractError("failed runner result cannot contain output")
        error = result["error"]
        if not isinstance(error, dict) or set(error) != {"code", "retryable"}:
            raise RunnerContractError("failed runner result error is invalid")
        if not isinstance(error["code"], str) or not _SAFE_ERROR_CODE.fullmatch(
            error["code"]
        ):
            raise RunnerContractError("failed runner result error code is invalid")
        if not isinstance(error["retryable"], bool):
            raise RunnerContractError("failed runner retryable flag is invalid")
        self._validate_persisted_model_usage(
            result["model_usage"],
            result["call_ledger"],
        )
        return error["retryable"]

    @staticmethod
    def _validate_persisted_model_usage(
        model_usage: Any,
        call_ledger: Any,
    ) -> None:
        normalized = _validated_model_usage_ledger(model_usage)
        if canonical_json_bytes(normalized) != canonical_json_bytes(model_usage):
            raise RunnerContractError("persisted model usage is not canonical")
        if not isinstance(call_ledger, dict):
            raise RunnerContractError("persisted call ledger is invalid")
        actual = call_ledger.get("actual")
        if not isinstance(actual, dict):
            raise RunnerContractError("persisted actual call ledger is invalid")
        _validate_model_usage_call_ledger(
            normalized,
            actual,
        )

    def _validate_persisted_observations(
        self,
        observations: Any,
        call_ledger: Any,
        *,
        attempt_cache_mode: str,
    ) -> None:
        if not isinstance(observations, dict) or set(observations) != set(
            OBSERVATION_STAGES
        ):
            raise RunnerContractError("persisted stage observations are invalid")
        stage_attempted = _zero_counts()
        stage_failed = _zero_counts()
        source_totals = _zero_counts()
        for stage in OBSERVATION_STAGES:
            payload = observations[stage]
            if not isinstance(payload, dict) or set(payload) != {
                "status",
                "origin",
                "duration_ms",
                "cache_key",
                "external_calls",
                "failed_external_calls",
                "source_external_calls",
                "error_code",
                "unavailable_reason",
            }:
                raise RunnerContractError(
                    "persisted stage observation fields are invalid"
                )
            observation = StageObservation(
                status=payload["status"],
                origin=payload["origin"],
                duration_ms=payload["duration_ms"],
                cache_key=payload["cache_key"],
                source_external_calls=payload["source_external_calls"],
                error_code=payload["error_code"],
                unavailable_reason=payload["unavailable_reason"],
            )
            normalized = observation.to_dict(
                external_calls=payload["external_calls"],
                failed_external_calls=payload["failed_external_calls"],
            )
            _validate_stage_origin_for_cache_mode(
                normalized["origin"], attempt_cache_mode
            )
            if normalized != payload:
                raise RunnerContractError(
                    "persisted stage observation is not canonical"
                )
            for kind in EXTERNAL_CALL_KINDS:
                stage_attempted[kind] += normalized["external_calls"][kind]
                stage_failed[kind] += normalized["failed_external_calls"][kind]
                source_totals[kind] += normalized["source_external_calls"][kind]
        if not isinstance(call_ledger, dict) or set(call_ledger) != {
            "actual",
            "source",
        }:
            raise RunnerContractError("persisted call ledger fields are invalid")
        source = _validated_counts("call_ledger.source", call_ledger["source"])
        if source != source_totals:
            raise RunnerContractError("persisted source call totals are inconsistent")
        actual = call_ledger["actual"]
        if not isinstance(actual, dict) or set(actual) != set(EXTERNAL_CALL_KINDS):
            raise RunnerContractError("persisted actual call ledger is invalid")
        for kind in EXTERNAL_CALL_KINDS:
            item = actual[kind]
            if not isinstance(item, dict) or set(item) != {
                "attempted",
                "succeeded",
                "failed",
                "duration_ms",
                "provider_wait_ms",
            }:
                raise RunnerContractError(
                    "persisted provider ledger fields are invalid"
                )
            for field_name in ("attempted", "succeeded", "failed"):
                value = item[field_name]
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise RunnerContractError("persisted provider counts are invalid")
            if item["attempted"] != item["succeeded"] + item["failed"]:
                raise RunnerContractError(
                    "persisted provider call counts do not balance"
                )
            if (
                item["attempted"] != stage_attempted[kind]
                or item["failed"] != stage_failed[kind]
            ):
                raise RunnerContractError(
                    "persisted provider stage totals are inconsistent"
                )
            if attempt_cache_mode == "replay" and item["attempted"] != 0:
                raise RunnerContractError(
                    "persisted replay attempt cannot contain external calls"
                )
            _require_non_negative_number("provider duration", item["duration_ms"])
            _require_non_negative_number("provider wait", item["provider_wait_ms"])
