"""Transactional session and run state for the M4 service boundary.

The HTTP layer is deliberately not represented here.  It authenticates a
request and passes a :class:`ServicePrincipal`; this module owns all database
authorization predicates and state transitions.  Every method uses a short
transaction.  In particular, no model or retrieval work is performed while a
database transaction is open.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Mapping, NoReturn, Protocol

from sqlalchemy import (
    Connection,
    Engine,
    Select,
    and_,
    delete,
    desc,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

from legal_rag.harness.budget import (
    AttemptReservation,
    BudgetExhausted,
    BudgetSnapshot,
    HarnessBudgetConfig,
)
from legal_rag.harness.state import (
    HARNESS_STATE_SCHEMA_VERSION,
    HARNESS_TERMINAL_STATUSES,
    HarnessState,
    HarnessStateError,
    harness_state_hash,
    validate_harness_state,
)
from legal_rag.retrieval_contracts import RetrievalBoundary
from legal_rag.storage.schema import (
    active_snapshot_pointers,
    corpus_snapshots,
    embedding_imports,
    embedding_profiles,
    idempotency_keys,
    messages,
    run_budget_ledgers,
    run_checkpoints,
    run_events,
    run_external_attempts,
    run_node_artifacts,
    run_results,
    runs,
    sessions,
    snapshot_activation_events,
)

JSONMapping = Mapping[str, Any]

ACTIVE_RUN_STATUSES = frozenset({"queued", "running", "interrupted"})
ANSWER_BEARING_RUN_STATUSES = frozenset(HARNESS_TERMINAL_STATUSES)
TERMINAL_RUN_STATUSES = frozenset({*ANSWER_BEARING_RUN_STATUSES, "failed", "cancelled"})
STAGE_EVENT_TYPES = frozenset(
    {
        "retrieval.completed",
        "generation.started",
        "verification.completed",
    }
)
RUN_EVENT_TYPES = frozenset(
    {
        "run.queued",
        "run.started",
        *STAGE_EVENT_TYPES,
        "answer.final",
        "run.failed",
        "run.cancelled",
        "run.interrupted",
        "run.resume_requested",
        "run.resumed",
        "attempt.outcome_unknown",
        "run.completed_with_limits",
        "run.needs_clarification",
    }
)

DEFAULT_IDEMPOTENCY_TTL = timedelta(hours=24)
DEFAULT_MESSAGE_PAGE_SIZE = 50
MAX_MESSAGE_PAGE_SIZE = 100
DEFAULT_EVENT_PAGE_SIZE = 100
MAX_EVENT_PAGE_SIZE = 500
DEFAULT_HISTORY_MESSAGES = 20
MAX_HISTORY_MESSAGES = 100
DEFAULT_HISTORY_CHARACTERS = 16_000
MAX_HISTORY_CHARACTERS = 100_000
DEFAULT_STALE_RECOVERY_BATCH = 100
MAX_STALE_RECOVERY_BATCH = 1_000
MAX_QUESTION_CHARACTERS = 100_000
MAX_RESULT_JSON_BYTES = 1_000_000
MAX_EVENT_JSON_BYTES = 128_000

_ERROR_CODE_PATTERN = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "chain_of_thought",
        "cookie",
        "draft",
        "generated",
        "generation_prompt",
        "hidden_reasoning",
        "memory_text",
        "password",
        "pre_fallback_answer",
        "pre_fallback_verification",
        "prompt",
        "raw_model_output",
        "raw_response",
        "reasoning",
        "secret",
        "session_state_before",
        "system_prompt",
        "token",
        "access_token",
    }
)
_REQUEST_RESERVED_FIELDS = frozenset(
    {
        "activation_id",
        "boundary_fingerprint",
        "graph_version",
        "lease_expires_at",
        "lease_owner",
        "parent_run_id",
        "profile_id",
        "scope_id",
        "snapshot_revision",
        "user_id",
    }
)
_SAFE_ANSWER_FIELDS = frozenset(
    {
        "answer_mode",
        "answer_text",
        "citations",
        "claims",
        "clarification_question",
        "disclaimer",
        "limitations",
        "needs_clarification",
        "schema_version",
        "source_ids",
        "status",
    }
)
_SAFE_EVIDENCE_FIELDS = frozenset(
    {
        "activation_id",
        "boundary_fingerprint",
        "citations",
        "coverage",
        "evidence_check",
        "evidence_ids",
        "items",
        "limitations",
        "profile_id",
        "reason_codes",
        "rejected_count",
        "result_count",
        "schema_version",
        "scope_id",
        "snapshot_id",
        "snapshot_revision",
        "sources",
        "status",
        "terminal_kind",
    }
)
_SAFE_VERIFICATION_FIELDS = frozenset(
    {
        "actual_answer_mode",
        "answer_source_format",
        "citation_alignment_valid",
        "citation_ids",
        "citation_ids_valid",
        "citation_valid",
        "cited_source_ids",
        "claim_source_ids",
        "checks",
        "disclaimer_present",
        "duplicate_source_ids",
        "evidence_coverage",
        "evidence_catalog_valid",
        "evidence_scope_valid",
        "expected_answer_mode",
        "fallback_used",
        "failure_reasons",
        "invalid_scope_citations",
        "limitations",
        "malformed_citation_tokens",
        "missing_citations",
        "missing_source_ids",
        "mode",
        "passed",
        "performed",
        "refusal_ok",
        "refusal_correct",
        "refusal_present",
        "refusal_required",
        "reason_codes",
        "required_checks",
        "response_mode_valid",
        "schema_valid",
        "schema_errors",
        "schema_version",
        "semantic_support_status",
        "source_tokens_ok",
        "status",
        "unsupported_claims",
        "visible_source_ids",
    }
)
_STAGE_EVENT_FIELDS = {
    "retrieval.completed": frozenset(
        {
            "result_count",
            "checked_result_count",
            "rejected_count",
            "stop_reason",
        }
    ),
    "generation.started": frozenset(),
    "verification.completed": frozenset({"passed", "fallback_used"}),
}
_SAFE_FAILURE_FIELDS = frozenset({"error_code", "stage", "retryable"})


class RunServiceError(RuntimeError):
    """Base class for expected M4 service failures."""


class ServiceContractError(ValueError, RunServiceError):
    """A caller supplied an invalid service value."""


class ResourceNotFoundError(RunServiceError):
    """A resource is absent or not owned by the authenticated principal."""

    def __init__(self) -> None:
        # Deliberately identical for absent and foreign-owned identifiers.
        super().__init__("resource was not found")


class IdempotencyConflictError(RunServiceError):
    """An unexpired idempotency key is bound to a different request."""


class ActiveRunConflictError(RunServiceError):
    """The session already has a queued, running, or interrupted run."""

    def __init__(self, active_run_id: str) -> None:
        super().__init__("the session already has an active run")
        self.active_run_id = active_run_id


class SessionInactiveError(RunServiceError):
    """A new run cannot be added to an archived session."""


class RunConfigurationUnavailableError(RunServiceError):
    """The principal's frozen retrieval configuration is not serviceable."""


class ResumeUnsupportedError(RunServiceError):
    """M4 records interruptions but does not implement checkpoint resume."""


class CheckpointCompatibilityError(RunServiceError):
    """A trusted checkpoint does not match the immutable run contract."""


class InvalidRunStateError(RunServiceError):
    """The requested transition is invalid for the persisted run state."""


class WorkerLeaseLostError(RunServiceError):
    """A worker attempted to write after losing its run lease."""


class UnsafePayloadError(ServiceContractError):
    """A payload could expose unsafe execution details or credentials."""


class RunServiceDataError(RunServiceError):
    """Persisted M4 rows violate an invariant required by the service."""


# Concise aliases are useful at protocol boundaries and retain one uniform
# exception type for authorization failures.
NotFoundError = ResourceNotFoundError
LeaseLostError = WorkerLeaseLostError
RunConflictError = ActiveRunConflictError


def _canonical_identifier(value: Any, field_name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value:
        raise ServiceContractError(f"{field_name} must be a non-empty string")
    if value != value.strip():
        raise ServiceContractError(
            f"{field_name} must not contain surrounding whitespace"
        )
    if len(value) > max_length:
        raise ServiceContractError(f"{field_name} exceeds maximum length {max_length}")
    return value


def _canonical_sha256_identifier(value: Any, field_name: str) -> str:
    resolved = _canonical_identifier(value, field_name, 64)
    if _SHA256_PATTERN.fullmatch(resolved) is None:
        raise ServiceContractError(f"{field_name} must be a lowercase SHA-256 digest")
    return resolved


def _canonical_non_negative_integer(value: Any, field_name: str) -> int:
    if type(value) is not int or value < 0:
        raise ServiceContractError(f"{field_name} must be a non-negative integer")
    return value


def _canonical_positive_integer(
    value: Any, field_name: str, *, maximum: int | None = None
) -> int:
    if type(value) is not int or value <= 0:
        raise ServiceContractError(f"{field_name} must be a positive integer")
    if maximum is not None and value > maximum:
        raise ServiceContractError(f"{field_name} exceeds maximum {maximum}")
    return value


def _normalize_json_tree(
    value: Any,
    *,
    path: str = "payload",
    active: set[int] | None = None,
) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        # json.dumps(..., allow_nan=False) rejects non-finite values.  Keeping
        # the check there avoids a second floating-point implementation here.
        return value
    if active is None:
        active = set()
    if isinstance(value, Mapping):
        marker = id(value)
        if marker in active:
            raise ServiceContractError(f"{path} must not contain cycles")
        active.add(marker)
        try:
            copied: dict[str, Any] = {}
            for key, nested in value.items():
                if not isinstance(key, str):
                    raise ServiceContractError(f"{path} object keys must be strings")
                copied[key] = _normalize_json_tree(
                    nested,
                    path=f"{path}.{key}",
                    active=active,
                )
            return copied
        finally:
            active.remove(marker)
    if isinstance(value, (list, tuple)):
        marker = id(value)
        if marker in active:
            raise ServiceContractError(f"{path} must not contain cycles")
        active.add(marker)
        try:
            return [
                _normalize_json_tree(
                    nested,
                    path=f"{path}[{index}]",
                    active=active,
                )
                for index, nested in enumerate(value)
            ]
        finally:
            active.remove(marker)
    raise ServiceContractError(f"{path} is not a canonical JSON value")


def canonical_json_bytes(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON bytes or fail closed.

    The canonical form is shared by request idempotency, frozen retrieval
    configuration hashes, and defensive comparisons of persisted results.
    """

    normalized = _normalize_json_tree(value)
    try:
        rendered = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return rendered.encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ServiceContractError(f"value is not canonical JSON: {exc}") from exc


def canonical_json_sha256(value: Any) -> str:
    """Return the lowercase SHA-256 digest of canonical JSON bytes."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _canonical_json_copy(value: Any) -> Any:
    return json.loads(canonical_json_bytes(value).decode("utf-8"))


def _readonly_json_mapping(value: Any, field_name: str) -> JSONMapping:
    if not isinstance(value, Mapping):
        raise RunServiceDataError(f"stored {field_name} is not an object")
    copied = _canonical_json_copy(value)
    if not isinstance(copied, dict):  # pragma: no cover - guarded above.
        raise RunServiceDataError(f"stored {field_name} is not an object")
    return MappingProxyType(copied)


def _reject_forbidden_payload_keys(value: Any, *, path: str) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = key.casefold().replace("-", "_")
            if normalized in _FORBIDDEN_PAYLOAD_KEYS:
                raise UnsafePayloadError(f"{path} contains forbidden field {key!r}")
            _reject_forbidden_payload_keys(nested, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _reject_forbidden_payload_keys(nested, path=f"{path}[{index}]")


def _safe_payload(
    value: Any,
    *,
    field_name: str,
    maximum_bytes: int,
    allowlist: frozenset[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise UnsafePayloadError(f"{field_name} must be an object")
    copied = _canonical_json_copy(value)
    if not isinstance(copied, dict):  # pragma: no cover - guarded above.
        raise UnsafePayloadError(f"{field_name} must be an object")
    _reject_forbidden_payload_keys(copied, path=field_name)
    if allowlist is not None:
        copied = {key: item for key, item in copied.items() if key in allowlist}
    if len(canonical_json_bytes(copied)) > maximum_bytes:
        raise UnsafePayloadError(f"{field_name} exceeds the safe payload limit")
    return copied


def _require_closed_fields(
    payload: Mapping[str, Any],
    expected: frozenset[str],
    *,
    field_name: str,
) -> None:
    actual = frozenset(payload)
    if actual != expected:
        raise UnsafePayloadError(
            f"{field_name} must contain exactly {sorted(expected)!r}"
        )


def _safe_stage_event_payload(event_type: str, value: Any) -> dict[str, Any]:
    payload = _safe_payload(
        value,
        field_name="safe_payload",
        maximum_bytes=MAX_EVENT_JSON_BYTES,
    )
    expected = _STAGE_EVENT_FIELDS[event_type]
    _require_closed_fields(payload, expected, field_name=f"{event_type} payload")
    if event_type == "retrieval.completed":
        for field_name in (
            "result_count",
            "checked_result_count",
            "rejected_count",
        ):
            value = payload[field_name]
            if type(value) is not int or value < 0:
                raise UnsafePayloadError(
                    f"{event_type} payload {field_name} must be a non-negative integer"
                )
        stop_reason = payload["stop_reason"]
        if (
            not isinstance(stop_reason, str)
            or _ERROR_CODE_PATTERN.fullmatch(stop_reason) is None
        ):
            raise UnsafePayloadError(
                f"{event_type} payload stop_reason must be a safe name"
            )
    elif event_type == "verification.completed":
        if type(payload["passed"]) is not bool:
            raise UnsafePayloadError(f"{event_type} payload passed must be a boolean")
        if type(payload["fallback_used"]) is not bool:
            raise UnsafePayloadError(
                f"{event_type} payload fallback_used must be a boolean"
            )
    return payload


def _safe_failure_payload(
    value: Any,
    *,
    error_code: str,
) -> dict[str, Any]:
    payload = _safe_payload(
        value,
        field_name="safe_payload",
        maximum_bytes=MAX_EVENT_JSON_BYTES,
    )
    unexpected = frozenset(payload) - _SAFE_FAILURE_FIELDS
    if unexpected:
        raise UnsafePayloadError(
            f"run.failed payload contains unsupported fields {sorted(unexpected)!r}"
        )
    supplied_error = payload.get("error_code")
    if supplied_error is not None and (
        not isinstance(supplied_error, str)
        or _ERROR_CODE_PATTERN.fullmatch(supplied_error) is None
    ):
        raise UnsafePayloadError("run.failed payload error_code must be a safe name")
    stage = payload.get("stage")
    if stage is not None and (
        not isinstance(stage, str) or _ERROR_CODE_PATTERN.fullmatch(stage) is None
    ):
        raise UnsafePayloadError("run.failed payload stage must be a safe name")
    retryable = payload.get("retryable")
    if retryable is not None and type(retryable) is not bool:
        raise UnsafePayloadError("run.failed payload retryable must be a boolean")
    details = {key: payload[key] for key in ("stage", "retryable") if key in payload}
    details["status"] = "failed"
    details["error_code"] = error_code
    return details


@dataclass(frozen=True, slots=True)
class ServicePrincipal:
    """Authenticated identity plus server-selected retrieval binding."""

    user_id: str
    scope_id: str
    profile_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "user_id", _canonical_identifier(self.user_id, "user_id", 128)
        )
        object.__setattr__(
            self, "scope_id", _canonical_identifier(self.scope_id, "scope_id", 255)
        )
        object.__setattr__(
            self,
            "profile_id",
            _canonical_sha256_identifier(self.profile_id, "profile_id"),
        )


Principal = ServicePrincipal


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session_id: str
    user_id: str
    title: str | None
    status: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class MessageRecord:
    message_id: str
    session_id: str
    user_id: str
    role: str
    content: str
    run_id: str
    ordinal: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MessagePage:
    messages: tuple[MessageRecord, ...]
    next_after_ordinal: int | None
    has_more: bool

    @property
    def items(self) -> tuple[MessageRecord, ...]:
        return self.messages

    @property
    def next_cursor(self) -> int | None:
        return self.next_after_ordinal


@dataclass(frozen=True, slots=True)
class RunResultRecord:
    run_id: str
    final_message_id: str
    answer_text: str
    answer_payload: JSONMapping
    evidence_payload: JSONMapping
    verification_payload: JSONMapping
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    session_id: str
    user_id: str
    status: str
    request_hash: str
    request_payload: JSONMapping
    scope_id: str
    snapshot_id: str
    snapshot_revision: int
    activation_id: str
    profile_id: str
    boundary_fingerprint: str
    retrieval_config_hash: str
    graph_version: str
    created_at: datetime
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime
    error_code: str | None
    revision: int
    event_sequence: int
    lease_owner: str | None
    lease_expires_at: datetime | None
    result: RunResultRecord | None = None
    parent_run_id: str | None = None
    state_schema_version: int = HARNESS_STATE_SCHEMA_VERSION
    checkpoint_namespace: str | None = None
    last_checkpoint_id: str | None = None
    last_completed_node: str | None = None
    execution_deadline_at: datetime | None = None
    stop_reason: str | None = None
    lease_epoch: int = 0
    resume_requested_at: datetime | None = None

    @property
    def answer_payload(self) -> JSONMapping | None:
        return self.result.answer_payload if self.result is not None else None

    @property
    def evidence_payload(self) -> JSONMapping | None:
        return self.result.evidence_payload if self.result is not None else None

    @property
    def verification_payload(self) -> JSONMapping | None:
        return self.result.verification_payload if self.result is not None else None


@dataclass(frozen=True, slots=True)
class RunEventRecord:
    run_id: str
    sequence: int
    event_type: str
    safe_payload: JSONMapping
    created_at: datetime

    @property
    def id(self) -> int:
        """SSE-compatible event identifier."""

        return self.sequence


@dataclass(frozen=True, slots=True)
class FrozenRunInput:
    """Immutable execution input loaded only by the current lease owner."""

    run_id: str
    session_id: str
    user_id: str
    question: str
    request_payload: JSONMapping
    scope_id: str
    snapshot_id: str
    snapshot_revision: int
    activation_id: str
    profile_id: str
    boundary_fingerprint: str
    retrieval_config_hash: str
    graph_version: str
    state_schema_version: int
    execution_deadline_at: datetime
    lease_epoch: int
    checkpoint_namespace: str | None
    last_checkpoint_id: str | None
    completed_history: tuple[MessageRecord, ...]

    @property
    def history(self) -> tuple[MessageRecord, ...]:
        return self.completed_history

    @property
    def request_options(self) -> JSONMapping:
        return MappingProxyType(_retrieval_configuration(self.request_payload))

    @property
    def options(self) -> JSONMapping:
        return self.request_options

    def to_execution_input(self) -> Any:
        """Adapt the database DTO to the executor DTO without a module cycle."""

        from .run_executor import CompletedHistoryMessage, RunExecutionInput

        return RunExecutionInput(
            run_id=self.run_id,
            question=self.question,
            history=tuple(
                CompletedHistoryMessage(role=item.role, content=item.content)
                for item in self.completed_history
            ),
            scope_id=self.scope_id,
            snapshot_id=self.snapshot_id,
            snapshot_revision=self.snapshot_revision,
            activation_id=self.activation_id,
            profile_id=self.profile_id,
            boundary_fingerprint=self.boundary_fingerprint,
            request_options=self.request_options,
        )


@dataclass(frozen=True, slots=True)
class SafeRunResult:
    """Verified result envelope accepted by :meth:`RunService.publish_success`.

    Only explicitly allowlisted fields from the three structured payloads are
    persisted.  Unknown top-level fields are discarded, while credential or
    hidden-reasoning field names anywhere in the retained tree are rejected.
    """

    answer_text: str
    answer_payload: JSONMapping
    evidence_payload: JSONMapping
    verification_payload: JSONMapping


class SafeRunResultLike(Protocol):
    answer_text: str
    answer_payload: JSONMapping
    evidence_payload: JSONMapping
    verification_payload: JSONMapping


@dataclass(frozen=True, slots=True)
class _PreparedSafeRunResult:
    answer_text: str
    answer_payload: dict[str, Any]
    evidence_payload: dict[str, Any]
    verification_payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StoredArtifactRef:
    artifact_id: str
    payload_hash: str


def _coerce_principal(value: Any) -> ServicePrincipal:
    if isinstance(value, ServicePrincipal):
        return value
    try:
        return ServicePrincipal(
            user_id=value.user_id,
            scope_id=value.scope_id,
            profile_id=value.profile_id,
        )
    except AttributeError as exc:
        raise ServiceContractError("principal is invalid") from exc


def _coerce_safe_result(value: Any) -> _PreparedSafeRunResult:
    if isinstance(value, Mapping):
        try:
            answer_text = value["answer_text"]
            answer_payload = value["answer_payload"]
            evidence_payload = value["evidence_payload"]
            verification_payload = value["verification_payload"]
        except KeyError as exc:
            raise UnsafePayloadError("safe run result fields are incomplete") from exc
    else:
        try:
            answer_text = value.answer_text
            answer_payload = value.answer_payload
            evidence_payload = value.evidence_payload
            verification_payload = value.verification_payload
        except AttributeError as exc:
            raise UnsafePayloadError("safe run result fields are incomplete") from exc
    if not isinstance(answer_text, str) or not answer_text:
        raise UnsafePayloadError("answer_text must be a non-empty string")
    if len(answer_text) > MAX_QUESTION_CHARACTERS:
        raise UnsafePayloadError("answer_text exceeds the safe payload limit")
    safe_answer = _safe_payload(
        answer_payload,
        field_name="answer_payload",
        maximum_bytes=MAX_RESULT_JSON_BYTES,
        allowlist=_SAFE_ANSWER_FIELDS,
    )
    existing_answer = safe_answer.get("answer_text")
    if existing_answer is not None and existing_answer != answer_text:
        raise UnsafePayloadError(
            "answer_payload.answer_text does not match answer_text"
        )
    safe_answer["answer_text"] = answer_text
    safe_evidence = _safe_payload(
        evidence_payload,
        field_name="evidence_payload",
        maximum_bytes=MAX_RESULT_JSON_BYTES,
        allowlist=_SAFE_EVIDENCE_FIELDS,
    )
    safe_verification = _safe_payload(
        verification_payload,
        field_name="verification_payload",
        maximum_bytes=MAX_RESULT_JSON_BYTES,
        allowlist=_SAFE_VERIFICATION_FIELDS,
    )
    if safe_verification.get("passed") is not True:
        raise UnsafePayloadError(
            "verification_payload.passed must be true before final publication"
        )
    return _PreparedSafeRunResult(
        answer_text=answer_text,
        answer_payload=safe_answer,
        evidence_payload=safe_evidence,
        verification_payload=safe_verification,
    )


def _canonical_optional_title(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ServiceContractError("title must be a string or null")
    resolved = value.strip()
    if not resolved:
        return None
    if len(resolved) > 500:
        raise ServiceContractError("title exceeds maximum length 500")
    return resolved


def _canonical_request_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ServiceContractError("request_payload must be an object")
    copied = _canonical_json_copy(value)
    if not isinstance(copied, dict):  # pragma: no cover - guarded above.
        raise ServiceContractError("request_payload must be an object")
    reserved = sorted(_REQUEST_RESERVED_FIELDS.intersection(copied))
    if reserved:
        raise ServiceContractError(
            "request_payload cannot override server bindings: " + ", ".join(reserved)
        )
    question = copied.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ServiceContractError(
            "request_payload.question must be a non-empty string"
        )
    if question != question.strip():
        raise ServiceContractError(
            "request_payload.question must not contain surrounding whitespace"
        )
    if len(question) > MAX_QUESTION_CHARACTERS:
        raise ServiceContractError(
            f"request_payload.question exceeds maximum length {MAX_QUESTION_CHARACTERS}"
        )
    requested_snapshot_id = copied.get("snapshot_id")
    if requested_snapshot_id is not None:
        _canonical_identifier(requested_snapshot_id, "request_payload.snapshot_id", 255)
    if "retrieval" in copied and "retrieval_options" in copied:
        raise ServiceContractError(
            "request_payload must not contain both retrieval and retrieval_options"
        )
    retrieval = copied.get("retrieval_options", copied.get("retrieval", {}))
    if not isinstance(retrieval, Mapping):
        raise ServiceContractError("retrieval options must be an object")
    return copied


def _retrieval_configuration(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = payload.get("retrieval_options", payload.get("retrieval", {}))
    copied = _canonical_json_copy(value)
    if not isinstance(copied, dict):  # pragma: no cover - validated before.
        raise ServiceContractError("retrieval options must be an object")
    return copied


def _canonical_error_code(value: Any) -> str:
    if not isinstance(value, str) or not _ERROR_CODE_PATTERN.fullmatch(value):
        raise ServiceContractError("error_code must match [a-z0-9][a-z0-9_.-]{0,63}")
    return value


def _canonical_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ServiceContractError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ServiceContractError(f"{field_name} must be timezone-aware")
    return value


class RunService:
    """Business transaction boundary for durable M4 sessions and runs."""

    def __init__(
        self,
        engine: Engine,
        *,
        idempotency_ttl: timedelta = DEFAULT_IDEMPOTENCY_TTL,
        history_message_limit: int = DEFAULT_HISTORY_MESSAGES,
        history_character_limit: int = DEFAULT_HISTORY_CHARACTERS,
        budget_config: HarnessBudgetConfig | None = None,
    ) -> None:
        if not isinstance(engine, Engine):
            raise TypeError("engine must be a SQLAlchemy Engine")
        if not isinstance(idempotency_ttl, timedelta) or idempotency_ttl <= timedelta(
            0
        ):
            raise ServiceContractError("idempotency_ttl must be a positive timedelta")
        self.engine = engine
        self.idempotency_ttl = idempotency_ttl
        self.history_message_limit = _canonical_positive_integer(
            history_message_limit,
            "history_message_limit",
            maximum=MAX_HISTORY_MESSAGES,
        )
        self.history_character_limit = _canonical_positive_integer(
            history_character_limit,
            "history_character_limit",
            maximum=MAX_HISTORY_CHARACTERS,
        )
        if budget_config is None:
            budget_config = HarnessBudgetConfig()
        if not isinstance(budget_config, HarnessBudgetConfig):
            raise TypeError("budget_config must be a HarnessBudgetConfig")
        self.budget_config = budget_config

    def create_session(
        self,
        principal: ServicePrincipal,
        title: str | None = None,
    ) -> SessionRecord:
        """Create one active session owned by the authenticated principal."""

        bound = _coerce_principal(principal)
        resolved_title = _canonical_optional_title(title)
        session_id = str(uuid.uuid4())
        with self.engine.begin() as connection:
            connection.execute(
                insert(sessions).values(
                    session_id=session_id,
                    user_id=bound.user_id,
                    title=resolved_title,
                    status="active",
                )
            )
            row = self._session_row(connection, session_id, bound.user_id)
            if row is None:  # pragma: no cover - insert/read invariant.
                raise RunServiceDataError("created session could not be read back")
            return self._session_record(row)

    def list_messages(
        self,
        principal: ServicePrincipal,
        session_id: str,
        *,
        limit: int = DEFAULT_MESSAGE_PAGE_SIZE,
        after_ordinal: int = 0,
    ) -> MessagePage:
        """List every persisted session message in stable ordinal order.

        Execution history is filtered separately by ``_completed_history``;
        the user-facing transcript must not hide a queued, failed, cancelled,
        or interrupted question that was durably accepted.
        """

        bound = _coerce_principal(principal)
        resolved_session_id = _canonical_identifier(session_id, "session_id", 36)
        resolved_limit = _canonical_positive_integer(
            limit, "limit", maximum=MAX_MESSAGE_PAGE_SIZE
        )
        resolved_after = _canonical_non_negative_integer(after_ordinal, "after_ordinal")
        with self.engine.connect() as connection:
            if (
                self._session_row(connection, resolved_session_id, bound.user_id)
                is None
            ):
                raise ResourceNotFoundError()
            rows = (
                connection.execute(
                    select(messages)
                    .where(
                        messages.c.session_id == resolved_session_id,
                        messages.c.user_id == bound.user_id,
                        messages.c.ordinal > resolved_after,
                    )
                    .order_by(messages.c.ordinal, messages.c.message_id)
                    .limit(resolved_limit + 1)
                )
                .mappings()
                .all()
            )
        has_more = len(rows) > resolved_limit
        visible_rows = rows[:resolved_limit]
        records = tuple(self._message_record(row) for row in visible_rows)
        next_after = records[-1].ordinal if has_more and records else None
        return MessagePage(
            messages=records,
            next_after_ordinal=next_after,
            has_more=has_more,
        )

    def create_run(
        self,
        principal: ServicePrincipal,
        session_id: str,
        idempotency_key: str,
        request_payload: JSONMapping,
        graph_version: str,
        *,
        parent_run_id: str | None = None,
    ) -> tuple[RunRecord, bool]:
        """Create a frozen queued run, or replay an unexpired idempotent result.

        The owned session row serializes all run creation for one session.  The
        transaction checks idempotency before the active-run invariant, freezes
        the current activation pointer, verifies the configured profile import,
        and writes the run, user message, key, and first event atomically.
        """

        bound = _coerce_principal(principal)
        resolved_session_id = _canonical_identifier(session_id, "session_id", 36)
        resolved_key = _canonical_identifier(idempotency_key, "idempotency_key", 255)
        resolved_graph_version = _canonical_identifier(
            graph_version, "graph_version", 64
        )
        payload = _canonical_request_payload(request_payload)
        resolved_parent_run_id = (
            None
            if parent_run_id is None
            else _canonical_identifier(parent_run_id, "parent_run_id", 36)
        )
        # Preserve the M4 hash for ordinary runs so an unexpired idempotency
        # key remains replayable during the M5 rollout.  Follow-up runs bind
        # the parent into the hash, preventing one key from being replayed
        # against a different clarification lineage.
        request_hash = (
            canonical_json_sha256(payload)
            if resolved_parent_run_id is None
            else canonical_json_sha256(
                {"request": payload, "parent_run_id": resolved_parent_run_id}
            )
        )
        retrieval_config = _retrieval_configuration(payload)
        retrieval_config_hash = canonical_json_sha256(retrieval_config)

        with self.engine.begin() as connection:
            session_row = self._session_row(
                connection,
                resolved_session_id,
                bound.user_id,
                for_update=True,
            )
            if session_row is None:
                raise ResourceNotFoundError()

            now = self._database_now(connection)
            idempotency_row = (
                connection.execute(
                    select(idempotency_keys).where(
                        idempotency_keys.c.user_id == bound.user_id,
                        idempotency_keys.c.session_id == resolved_session_id,
                        idempotency_keys.c.idempotency_key == resolved_key,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if idempotency_row is not None:
                if idempotency_row["expires_at"] > now:
                    if idempotency_row["request_hash"] != request_hash:
                        raise IdempotencyConflictError(
                            "idempotency key is bound to a different request"
                        )
                    replayed = self._owned_run_record(
                        connection,
                        idempotency_row["run_id"],
                        bound.user_id,
                    )
                    if replayed is None:
                        raise RunServiceDataError(
                            "idempotency key references a missing owned run"
                        )
                    return replayed, True
                connection.execute(
                    delete(idempotency_keys).where(
                        idempotency_keys.c.user_id == bound.user_id,
                        idempotency_keys.c.session_id == resolved_session_id,
                        idempotency_keys.c.idempotency_key == resolved_key,
                        idempotency_keys.c.expires_at <= now,
                    )
                )

            if session_row["status"] != "active":
                raise SessionInactiveError("the session is not active")

            if resolved_parent_run_id is not None:
                parent = self._owned_run_row(
                    connection,
                    resolved_parent_run_id,
                    bound.user_id,
                )
                if (
                    parent is None
                    or parent["session_id"] != resolved_session_id
                    or parent["status"] not in ANSWER_BEARING_RUN_STATUSES
                ):
                    raise ResourceNotFoundError()

            active_row = (
                connection.execute(
                    select(runs.c.run_id)
                    .where(
                        runs.c.session_id == resolved_session_id,
                        runs.c.user_id == bound.user_id,
                        runs.c.status.in_(tuple(ACTIVE_RUN_STATUSES)),
                    )
                    .order_by(runs.c.queued_at, runs.c.run_id)
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
            if active_row is not None:
                raise ActiveRunConflictError(active_row["run_id"])

            frozen = self._freeze_retrieval_binding(connection, bound)
            requested_snapshot_id = payload.get("snapshot_id")
            if (
                requested_snapshot_id is not None
                and requested_snapshot_id != frozen["snapshot_id"]
            ):
                raise RunConfigurationUnavailableError(
                    "the requested snapshot is not the active snapshot for this scope"
                )
            # This must be the exact retrieval-layer boundary fingerprint.  The
            # activation revision and configuration hash remain separate frozen
            # run fields and must not silently alter RetrievalBoundary identity.
            boundary_fingerprint = RetrievalBoundary(
                scope_id=bound.scope_id,
                snapshot_id=frozen["snapshot_id"],
                profile_id=bound.profile_id,
            ).fingerprint

            run_id = str(uuid.uuid4())
            message_id = str(uuid.uuid4())
            ordinal = self._next_message_ordinal(connection, resolved_session_id)
            connection.execute(
                insert(runs).values(
                    run_id=run_id,
                    session_id=resolved_session_id,
                    user_id=bound.user_id,
                    parent_run_id=resolved_parent_run_id,
                    status="queued",
                    request_hash=request_hash,
                    request_payload=payload,
                    scope_id=bound.scope_id,
                    snapshot_id=frozen["snapshot_id"],
                    snapshot_revision=frozen["revision"],
                    activation_id=frozen["activation_id"],
                    profile_id=bound.profile_id,
                    boundary_fingerprint=boundary_fingerprint,
                    retrieval_config_hash=retrieval_config_hash,
                    graph_version=resolved_graph_version,
                    state_schema_version=HARNESS_STATE_SCHEMA_VERSION,
                    revision=1,
                    event_sequence=1,
                )
            )
            connection.execute(
                insert(run_budget_ledgers).values(
                    run_id=run_id,
                    max_retrieval_rounds=self.budget_config.max_retrieval_rounds,
                    max_queries_per_round=self.budget_config.max_queries_per_round,
                    max_tool_attempts=self.budget_config.max_tool_attempts,
                    max_model_attempts=self.budget_config.max_model_attempts,
                    max_embedding_attempts=self.budget_config.max_embedding_attempts,
                    max_retry_per_operation=self.budget_config.max_retry_per_operation,
                    evidence_top_k=self.budget_config.evidence_top_k,
                )
            )
            connection.execute(
                insert(messages).values(
                    message_id=message_id,
                    session_id=resolved_session_id,
                    user_id=bound.user_id,
                    role="user",
                    content=payload["question"],
                    run_id=run_id,
                    ordinal=ordinal,
                )
            )
            connection.execute(
                insert(idempotency_keys).values(
                    user_id=bound.user_id,
                    session_id=resolved_session_id,
                    idempotency_key=resolved_key,
                    request_hash=request_hash,
                    run_id=run_id,
                    expires_at=now + self.idempotency_ttl,
                )
            )
            connection.execute(
                insert(run_events).values(
                    run_id=run_id,
                    sequence=1,
                    event_type="run.queued",
                    safe_payload={
                        "status": "queued",
                        "scope_id": bound.scope_id,
                        "snapshot_id": frozen["snapshot_id"],
                        "snapshot_revision": frozen["revision"],
                        "profile_id": bound.profile_id,
                        "boundary_fingerprint": boundary_fingerprint,
                    },
                )
            )
            created_row = self._owned_run_row(connection, run_id, bound.user_id)
            if created_row is None:  # pragma: no cover - insert/read invariant.
                raise RunServiceDataError("created run could not be read back")
            return self._run_record(connection, created_row), False

    def get_run(self, principal: ServicePrincipal, run_id: str) -> RunRecord:
        """Return an owned run and its final result, if one exists."""

        bound = _coerce_principal(principal)
        resolved_run_id = _canonical_identifier(run_id, "run_id", 36)
        with self.engine.connect() as connection:
            record = self._owned_run_record(
                connection,
                resolved_run_id,
                bound.user_id,
            )
            if record is None:
                raise ResourceNotFoundError()
            return record

    def assert_owned_run(self, principal: ServicePrincipal, run_id: str) -> RunRecord:
        """Authorization helper with the same non-enumerating not-found result."""

        return self.get_run(principal, run_id)

    def list_events(
        self,
        principal: ServicePrincipal,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = DEFAULT_EVENT_PAGE_SIZE,
    ) -> tuple[RunEventRecord, ...]:
        """Read persisted events after an SSE cursor in strict sequence order."""

        bound = _coerce_principal(principal)
        resolved_run_id = _canonical_identifier(run_id, "run_id", 36)
        resolved_after = _canonical_non_negative_integer(
            after_sequence, "after_sequence"
        )
        resolved_limit = _canonical_positive_integer(
            limit, "limit", maximum=MAX_EVENT_PAGE_SIZE
        )
        with self.engine.connect() as connection:
            if self._owned_run_row(connection, resolved_run_id, bound.user_id) is None:
                raise ResourceNotFoundError()
            rows = (
                connection.execute(
                    select(run_events)
                    .where(
                        run_events.c.run_id == resolved_run_id,
                        run_events.c.sequence > resolved_after,
                    )
                    .order_by(run_events.c.sequence)
                    .limit(resolved_limit)
                )
                .mappings()
                .all()
            )
            return tuple(self._event_record(row) for row in rows)

    def cancel_run(self, principal: ServicePrincipal, run_id: str) -> RunRecord:
        """Cancel an owned active run; repeated and late cancellation is safe."""

        bound = _coerce_principal(principal)
        resolved_run_id = _canonical_identifier(run_id, "run_id", 36)
        with self.engine.begin() as connection:
            row = self._owned_run_row(
                connection,
                resolved_run_id,
                bound.user_id,
                for_update=True,
            )
            if row is None:
                raise ResourceNotFoundError()
            if row["status"] in TERMINAL_RUN_STATUSES:
                return self._run_record(connection, row)
            if row["status"] not in ACTIVE_RUN_STATUSES:
                raise InvalidRunStateError("run cannot be cancelled from its state")
            now = self._database_now(connection)
            self._transition_with_event(
                connection,
                row,
                values={
                    "status": "cancelled",
                    "finished_at": now,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "error_code": None,
                },
                event_type="run.cancelled",
                safe_payload={"status": "cancelled", "reason": "user_requested"},
                now=now,
            )
            changed = self._run_row(connection, resolved_run_id)
            if changed is None:  # pragma: no cover - locked row invariant.
                raise RunServiceDataError("cancelled run disappeared")
            return self._run_record(connection, changed)

    def resume_unsupported(self, principal: ServicePrincipal, run_id: str) -> NoReturn:
        """Authorize first, then report the explicit M4 resume boundary."""

        self.assert_owned_run(principal, run_id)
        raise ResumeUnsupportedError(
            "run resume requires M5 checkpoint recovery and is not supported in M4"
        )

    def resume_run(self, principal: ServicePrincipal, run_id: str) -> RunRecord:
        """Request explicit recovery of one owned interrupted run.

        Resume never resets the absolute deadline or any durable budget counter.
        A repeated request is idempotent.  Incompatible trusted state fails before
        the row becomes claimable by a worker.
        """

        bound = _coerce_principal(principal)
        resolved_run_id = _canonical_identifier(run_id, "run_id", 36)
        with self.engine.begin() as connection:
            row = self._owned_run_row(
                connection,
                resolved_run_id,
                bound.user_id,
                for_update=True,
            )
            if row is None:
                raise ResourceNotFoundError()
            if row["status"] in TERMINAL_RUN_STATUSES:
                return self._run_record(connection, row)
            if row["status"] != "interrupted":
                raise InvalidRunStateError("only an interrupted run can be resumed")
            self._trusted_checkpoint_row(connection, row, validate_payload=True)
            if row["resume_requested_at"] is not None:
                return self._run_record(connection, row)
            now = self._database_now(connection)
            self._transition_with_event(
                connection,
                row,
                values={"resume_requested_at": now, "error_code": None},
                event_type="run.resume_requested",
                safe_payload={"status": "interrupted"},
                now=now,
            )
            changed = self._run_row(connection, resolved_run_id)
            if changed is None:  # pragma: no cover - locked row invariant.
                raise RunServiceDataError("resume-requested run disappeared")
            return self._run_record(connection, changed)

    def claim_next_run(
        self,
        worker_id: str,
        lease_seconds: int,
    ) -> RunRecord | None:
        """Claim the oldest queued or explicitly resumed interrupted run."""

        resolved_worker = _canonical_identifier(worker_id, "worker_id", 128)
        resolved_seconds = _canonical_positive_integer(
            lease_seconds, "lease_seconds", maximum=86_400
        )
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    select(runs)
                    .where(
                        or_(
                            runs.c.status == "queued",
                            and_(
                                runs.c.status == "interrupted",
                                runs.c.resume_requested_at.is_not(None),
                            ),
                        )
                    )
                    .order_by(runs.c.queued_at, runs.c.run_id)
                    .limit(1)
                    .with_for_update(skip_locked=True, of=runs)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            now = self._database_now(connection)
            resumed = row["status"] == "interrupted"
            lease_epoch = int(row["lease_epoch"]) + 1
            execution_deadline_at = row["execution_deadline_at"]
            if execution_deadline_at is None:
                execution_deadline_at = now + timedelta(
                    seconds=self.budget_config.execution_deadline_seconds
                )
            self._transition_with_event(
                connection,
                row,
                values={
                    "status": "running",
                    "started_at": row["started_at"] or now,
                    "lease_owner": resolved_worker,
                    "lease_expires_at": now + timedelta(seconds=resolved_seconds),
                    "lease_epoch": lease_epoch,
                    "execution_deadline_at": execution_deadline_at,
                    "resume_requested_at": None,
                    "error_code": None,
                },
                event_type="run.resumed" if resumed else "run.started",
                safe_payload={"status": "running", "lease_epoch": lease_epoch},
                now=now,
            )
            claimed = self._run_row(connection, row["run_id"])
            if claimed is None:  # pragma: no cover - locked row invariant.
                raise RunServiceDataError("claimed run disappeared")
            return self._run_record(connection, claimed)

    def recover_stale_runs(
        self,
        now: datetime | None = None,
        *,
        batch_limit: int = DEFAULT_STALE_RECOVERY_BATCH,
    ) -> tuple[RunRecord, ...]:
        """Fence expired workers and classify their in-flight attempts."""

        resolved_limit = _canonical_positive_integer(
            batch_limit, "batch_limit", maximum=MAX_STALE_RECOVERY_BATCH
        )
        if now is not None:
            now = _canonical_datetime(now, "now")
        with self.engine.begin() as connection:
            cutoff = now if now is not None else self._database_now(connection)
            stale_rows = (
                connection.execute(
                    select(runs)
                    .where(
                        runs.c.status == "running",
                        or_(
                            runs.c.lease_expires_at.is_(None),
                            runs.c.lease_expires_at <= cutoff,
                        ),
                    )
                    .order_by(runs.c.lease_expires_at, runs.c.run_id)
                    .limit(resolved_limit)
                    .with_for_update(skip_locked=True, of=runs)
                )
                .mappings()
                .all()
            )
            recovered: list[RunRecord] = []
            for row in stale_rows:
                checkpoint_created_at = None
                if (
                    row["checkpoint_namespace"] is not None
                    and row["last_checkpoint_id"] is not None
                ):
                    checkpoint_created_at = connection.scalar(
                        select(run_checkpoints.c.created_at).where(
                            run_checkpoints.c.run_id == row["run_id"],
                            run_checkpoints.c.checkpoint_namespace
                            == row["checkpoint_namespace"],
                            run_checkpoints.c.checkpoint_id
                            == row["last_checkpoint_id"],
                        )
                    )
                # A successful model attempt newer than the latest trusted
                # checkpoint has only an in-process result.  The old worker
                # observed the provider response, but a new worker cannot
                # reconstruct it from the placeholder attempt reference.
                # Reconcile both planner and generator windows conservatively
                # instead of silently dispatching either model again.
                attempts = (
                    connection.execute(
                        select(run_external_attempts)
                        .where(
                            run_external_attempts.c.run_id == row["run_id"],
                            run_external_attempts.c.lease_epoch == row["lease_epoch"],
                            run_external_attempts.c.status.in_(
                                ("reserved", "dispatched", "succeeded")
                            ),
                        )
                        .order_by(run_external_attempts.c.reserved_at)
                        .with_for_update(of=run_external_attempts)
                    )
                    .mappings()
                    .all()
                )
                unknown_count = 0
                for attempt in attempts:
                    if attempt["status"] == "succeeded":
                        if attempt["operation_kind"] != "model":
                            continue
                        if (
                            checkpoint_created_at is not None
                            # Equal timestamps are conservatively unresolved:
                            # database clocks can have coarser precision than
                            # the transaction ordering we need to prove here.
                            and attempt["completed_at"] < checkpoint_created_at
                        ):
                            continue
                        status = "outcome_unknown"
                        error_code = "provider_result_not_durable"
                        retryable = False
                        unknown_count += 1
                    elif attempt["status"] == "dispatched":
                        status = "outcome_unknown"
                        error_code = "provider_outcome_unknown"
                        retryable = False
                        unknown_count += 1
                    else:
                        status = "abandoned_before_dispatch"
                        error_code = "worker_interrupted_before_dispatch"
                        retryable = True
                    connection.execute(
                        update(run_external_attempts)
                        .where(
                            run_external_attempts.c.attempt_id == attempt["attempt_id"],
                            run_external_attempts.c.status == attempt["status"],
                        )
                        .values(
                            status=status,
                            retryable=retryable,
                            error_code=error_code,
                            result_ref=None,
                            result_hash=None,
                            completed_at=cutoff,
                        )
                    )
                for _ in range(unknown_count):
                    self._transition_with_event(
                        connection,
                        row,
                        values={},
                        event_type="attempt.outcome_unknown",
                        safe_payload={
                            "status": "outcome_unknown",
                            "possible_duplicate_cost": True,
                            "budget_refunded": False,
                        },
                        now=cutoff,
                    )
                    refreshed = self._run_row(connection, row["run_id"])
                    if refreshed is None:
                        raise RunServiceDataError("recovering run disappeared")
                    row = refreshed
                self._transition_with_event(
                    connection,
                    row,
                    values={
                        "status": "interrupted",
                        "lease_owner": None,
                        "lease_expires_at": None,
                        "error_code": "worker_lease_expired",
                    },
                    event_type="run.interrupted",
                    safe_payload={
                        "status": "interrupted",
                        "reason": "worker_lease_expired",
                    },
                    now=cutoff,
                )
                changed = self._run_row(connection, row["run_id"])
                if changed is None:  # pragma: no cover - locked row invariant.
                    raise RunServiceDataError("interrupted run disappeared")
                recovered.append(self._run_record(connection, changed))
            return tuple(recovered)

    def load_execution_input(
        self,
        run_id: str,
        worker_id: str,
        *,
        lease_epoch: int | None = None,
        history_message_limit: int | None = None,
        history_character_limit: int | None = None,
    ) -> FrozenRunInput:
        """Load frozen input and bounded history from succeeded runs only."""

        resolved_run_id = _canonical_identifier(run_id, "run_id", 36)
        resolved_worker = _canonical_identifier(worker_id, "worker_id", 128)
        message_limit = (
            self.history_message_limit
            if history_message_limit is None
            else _canonical_positive_integer(
                history_message_limit,
                "history_message_limit",
                maximum=MAX_HISTORY_MESSAGES,
            )
        )
        character_limit = (
            self.history_character_limit
            if history_character_limit is None
            else _canonical_positive_integer(
                history_character_limit,
                "history_character_limit",
                maximum=MAX_HISTORY_CHARACTERS,
            )
        )
        with self.engine.begin() as connection:
            row = self._run_row(connection, resolved_run_id, for_update=True)
            if row is None:
                # Internal worker lookup is still non-enumerating.
                raise ResourceNotFoundError()
            now = self._database_now(connection)
            self._assert_worker_lease(
                row,
                resolved_worker,
                now,
                lease_epoch=lease_epoch,
            )
            user_message = (
                connection.execute(
                    select(messages).where(
                        messages.c.run_id == resolved_run_id,
                        messages.c.role == "user",
                    )
                )
                .mappings()
                .one_or_none()
            )
            if user_message is None:
                raise RunServiceDataError("running run has no user message")
            payload = _readonly_json_mapping(row["request_payload"], "request_payload")
            question = payload.get("question")
            if not isinstance(question, str) or question != user_message["content"]:
                raise RunServiceDataError(
                    "run request question does not match its user message"
                )
            history = self._completed_history(
                connection,
                session_id=row["session_id"],
                user_id=row["user_id"],
                before_ordinal=user_message["ordinal"],
                message_limit=message_limit,
                character_limit=character_limit,
            )
            execution_deadline_at = row["execution_deadline_at"]
            if not isinstance(execution_deadline_at, datetime):
                raise RunServiceDataError("running run has no execution deadline")
            return FrozenRunInput(
                run_id=row["run_id"],
                session_id=row["session_id"],
                user_id=row["user_id"],
                question=question,
                request_payload=payload,
                scope_id=row["scope_id"],
                snapshot_id=row["snapshot_id"],
                snapshot_revision=int(row["snapshot_revision"]),
                activation_id=row["activation_id"],
                profile_id=row["profile_id"],
                boundary_fingerprint=row["boundary_fingerprint"],
                retrieval_config_hash=row["retrieval_config_hash"],
                graph_version=row["graph_version"],
                state_schema_version=int(row["state_schema_version"]),
                execution_deadline_at=execution_deadline_at,
                lease_epoch=int(row["lease_epoch"]),
                checkpoint_namespace=row["checkpoint_namespace"],
                last_checkpoint_id=row["last_checkpoint_id"],
                completed_history=history,
            )

    def heartbeat_run(
        self,
        run_id: str,
        worker_id: str,
        lease_epoch: int,
        lease_seconds: int,
    ) -> RunRecord:
        """Extend a live lease under both owner and epoch fences."""

        resolved_run_id = _canonical_identifier(run_id, "run_id", 36)
        resolved_worker = _canonical_identifier(worker_id, "worker_id", 128)
        resolved_epoch = _canonical_positive_integer(lease_epoch, "lease_epoch")
        resolved_seconds = _canonical_positive_integer(
            lease_seconds, "lease_seconds", maximum=86_400
        )
        with self.engine.begin() as connection:
            row = self._run_row(connection, resolved_run_id, for_update=True)
            if row is None:
                raise ResourceNotFoundError()
            now = self._database_now(connection)
            self._assert_worker_lease(
                row,
                resolved_worker,
                now,
                lease_epoch=resolved_epoch,
            )
            changed = connection.execute(
                update(runs)
                .where(
                    runs.c.run_id == resolved_run_id,
                    runs.c.revision == row["revision"],
                    runs.c.lease_epoch == resolved_epoch,
                )
                .values(
                    lease_expires_at=now + timedelta(seconds=resolved_seconds),
                    updated_at=now,
                    revision=int(row["revision"]) + 1,
                )
            )
            if changed.rowcount != 1:
                raise WorkerLeaseLostError("worker lost the lease during heartbeat")
            current = self._run_row(connection, resolved_run_id)
            if current is None:  # pragma: no cover - locked row invariant.
                raise RunServiceDataError("heartbeat run disappeared")
            return self._run_record(connection, current)

    def assert_execution_fence(
        self,
        run_id: str,
        worker_id: str,
        lease_epoch: int,
    ) -> None:
        """Reject stale graph and provider writers.

        A final LangGraph checkpoint is allowed after atomic result publication,
        but only for the epoch that published that immutable terminal result.
        """

        resolved_run_id = _canonical_identifier(run_id, "run_id", 36)
        resolved_worker = _canonical_identifier(worker_id, "worker_id", 128)
        resolved_epoch = _canonical_positive_integer(lease_epoch, "lease_epoch")
        with self.engine.connect() as connection:
            row = self._run_row(connection, resolved_run_id)
            if row is None:
                raise ResourceNotFoundError()
            if (
                row["status"] in ANSWER_BEARING_RUN_STATUSES
                and int(row["lease_epoch"]) == resolved_epoch
                and self._run_result_record(connection, resolved_run_id) is not None
            ):
                return
            self._assert_worker_lease(
                row,
                resolved_worker,
                self._database_now(connection),
                lease_epoch=resolved_epoch,
            )

    def get_budget(self, run_id: str) -> BudgetSnapshot:
        resolved_run_id = _canonical_identifier(run_id, "run_id", 36)
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(run_budget_ledgers, runs.c.execution_deadline_at)
                    .select_from(
                        run_budget_ledgers.join(
                            runs, runs.c.run_id == run_budget_ledgers.c.run_id
                        )
                    )
                    .where(run_budget_ledgers.c.run_id == resolved_run_id)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ResourceNotFoundError()
            deadline = row["execution_deadline_at"]
            if not isinstance(deadline, datetime):
                raise RunServiceDataError("run budget has no absolute deadline")
            return self._budget_snapshot(row, deadline)

    def begin_retrieval_round(
        self,
        run_id: str,
        *,
        worker_id: str,
        lease_epoch: int,
        query_count: int,
    ) -> BudgetSnapshot:
        resolved_run_id = _canonical_identifier(run_id, "run_id", 36)
        resolved_worker = _canonical_identifier(worker_id, "worker_id", 128)
        resolved_epoch = _canonical_positive_integer(lease_epoch, "lease_epoch")
        resolved_queries = _canonical_positive_integer(
            query_count, "query_count", maximum=32
        )
        with self.engine.begin() as connection:
            run_row = self._run_row(connection, resolved_run_id, for_update=True)
            if run_row is None:
                raise ResourceNotFoundError()
            now = self._database_now(connection)
            self._assert_worker_lease(
                run_row,
                resolved_worker,
                now,
                lease_epoch=resolved_epoch,
            )
            self._assert_execution_deadline(run_row, now)
            ledger = (
                connection.execute(
                    select(run_budget_ledgers)
                    .where(run_budget_ledgers.c.run_id == resolved_run_id)
                    .with_for_update(of=run_budget_ledgers)
                )
                .mappings()
                .one_or_none()
            )
            if ledger is None:
                raise RunServiceDataError("run has no durable budget ledger")
            if resolved_queries > int(ledger["max_queries_per_round"]):
                raise BudgetExhausted("max_queries_per_round")
            if int(ledger["retrieval_rounds_used"]) >= int(
                ledger["max_retrieval_rounds"]
            ):
                raise BudgetExhausted("max_retrieval_rounds")
            maximum_queries = int(ledger["max_retrieval_rounds"]) * int(
                ledger["max_queries_per_round"]
            )
            if int(ledger["queries_used"]) + resolved_queries > maximum_queries:
                raise BudgetExhausted("max_queries")
            connection.execute(
                update(run_budget_ledgers)
                .where(
                    run_budget_ledgers.c.run_id == resolved_run_id,
                    run_budget_ledgers.c.revision == ledger["revision"],
                )
                .values(
                    retrieval_rounds_used=int(ledger["retrieval_rounds_used"]) + 1,
                    queries_used=int(ledger["queries_used"]) + resolved_queries,
                    revision=int(ledger["revision"]) + 1,
                    updated_at=now,
                )
            )
            current = (
                connection.execute(
                    select(run_budget_ledgers).where(
                        run_budget_ledgers.c.run_id == resolved_run_id
                    )
                )
                .mappings()
                .one()
            )
            return self._budget_snapshot(current, run_row["execution_deadline_at"])

    def reserve_attempt(
        self,
        run_id: str,
        *,
        worker_id: str,
        lease_epoch: int,
        operation_key: str,
        operation_kind: str,
        operation_name: str,
        request_hash: str,
    ) -> AttemptReservation:
        """Durably consume one global attempt before any external dispatch."""

        resolved_run_id = _canonical_identifier(run_id, "run_id", 36)
        resolved_worker = _canonical_identifier(worker_id, "worker_id", 128)
        resolved_epoch = _canonical_positive_integer(lease_epoch, "lease_epoch")
        resolved_key = _canonical_identifier(operation_key, "operation_key", 255)
        resolved_name = _canonical_identifier(operation_name, "operation_name", 64)
        if operation_kind not in {"model", "embedding", "tool"}:
            raise ServiceContractError("operation_kind is invalid")
        resolved_hash = _canonical_sha256_identifier(request_hash, "request_hash")
        with self.engine.begin() as connection:
            run_row = self._run_row(connection, resolved_run_id, for_update=True)
            if run_row is None:
                raise ResourceNotFoundError()
            now = self._database_now(connection)
            self._assert_worker_lease(
                run_row,
                resolved_worker,
                now,
                lease_epoch=resolved_epoch,
            )
            self._assert_execution_deadline(run_row, now)
            ledger = (
                connection.execute(
                    select(run_budget_ledgers)
                    .where(run_budget_ledgers.c.run_id == resolved_run_id)
                    .with_for_update(of=run_budget_ledgers)
                )
                .mappings()
                .one_or_none()
            )
            if ledger is None:
                raise RunServiceDataError("run has no durable budget ledger")
            previous_attempt = connection.scalar(
                select(
                    func.coalesce(func.max(run_external_attempts.c.attempt_no), 0)
                ).where(
                    run_external_attempts.c.run_id == resolved_run_id,
                    run_external_attempts.c.operation_key == resolved_key,
                )
            )
            attempt_no = int(previous_attempt or 0) + 1
            if attempt_no > int(ledger["max_retry_per_operation"]) + 1:
                raise BudgetExhausted("max_retry_per_operation")
            used_column = {
                "model": "model_attempts_used",
                "embedding": "embedding_attempts_used",
                "tool": "tool_attempts_used",
            }[operation_kind]
            max_column = {
                "model": "max_model_attempts",
                "embedding": "max_embedding_attempts",
                "tool": "max_tool_attempts",
            }[operation_kind]
            if int(ledger[used_column]) >= int(ledger[max_column]):
                raise BudgetExhausted(max_column)
            connection.execute(
                update(run_budget_ledgers)
                .where(
                    run_budget_ledgers.c.run_id == resolved_run_id,
                    run_budget_ledgers.c.revision == ledger["revision"],
                )
                .values(
                    **{
                        used_column: int(ledger[used_column]) + 1,
                        "revision": int(ledger["revision"]) + 1,
                        "updated_at": now,
                    }
                )
            )
            attempt_id = str(uuid.uuid4())
            connection.execute(
                insert(run_external_attempts).values(
                    attempt_id=attempt_id,
                    run_id=resolved_run_id,
                    lease_epoch=resolved_epoch,
                    operation_key=resolved_key,
                    operation_kind=operation_kind,
                    operation_name=resolved_name,
                    attempt_no=attempt_no,
                    request_hash=resolved_hash,
                    status="reserved",
                    reserved_at=now,
                )
            )
            return AttemptReservation(
                attempt_id=attempt_id,
                run_id=resolved_run_id,
                lease_epoch=resolved_epoch,
                operation_key=resolved_key,
                operation_kind=operation_kind,
                operation_name=resolved_name,
                attempt_no=attempt_no,
                request_hash=resolved_hash,
            )

    def mark_attempt_dispatched(
        self,
        attempt_id: str,
        *,
        worker_id: str,
        lease_epoch: int,
    ) -> None:
        resolved_attempt_id = _canonical_identifier(attempt_id, "attempt_id", 36)
        resolved_worker = _canonical_identifier(worker_id, "worker_id", 128)
        resolved_epoch = _canonical_positive_integer(lease_epoch, "lease_epoch")
        with self.engine.begin() as connection:
            attempt = (
                connection.execute(
                    select(run_external_attempts)
                    .where(run_external_attempts.c.attempt_id == resolved_attempt_id)
                    .with_for_update(of=run_external_attempts)
                )
                .mappings()
                .one_or_none()
            )
            if attempt is None:
                raise ResourceNotFoundError()
            run_row = self._run_row(connection, attempt["run_id"], for_update=True)
            if run_row is None:
                raise ResourceNotFoundError()
            now = self._database_now(connection)
            self._assert_worker_lease(
                run_row,
                resolved_worker,
                now,
                lease_epoch=resolved_epoch,
            )
            self._assert_execution_deadline(run_row, now)
            if int(attempt["lease_epoch"]) != resolved_epoch:
                raise WorkerLeaseLostError("attempt belongs to an older lease epoch")
            if attempt["status"] == "dispatched":
                return
            if attempt["status"] != "reserved":
                raise InvalidRunStateError(
                    "attempt cannot be dispatched from its state"
                )
            connection.execute(
                update(run_external_attempts)
                .where(
                    run_external_attempts.c.attempt_id == resolved_attempt_id,
                    run_external_attempts.c.status == "reserved",
                )
                .values(
                    status="dispatched",
                    provider_request_id=f"attempt:{resolved_attempt_id}",
                    dispatched_at=now,
                )
            )

    def finish_attempt(
        self,
        attempt_id: str,
        *,
        worker_id: str,
        lease_epoch: int,
        status: str,
        retryable: bool | None = None,
        error_code: str | None = None,
        result_ref: str | None = None,
        result_hash: str | None = None,
        possible_duplicate_cost: bool = False,
    ) -> None:
        resolved_attempt_id = _canonical_identifier(attempt_id, "attempt_id", 36)
        resolved_worker = _canonical_identifier(worker_id, "worker_id", 128)
        resolved_epoch = _canonical_positive_integer(lease_epoch, "lease_epoch")
        if status not in {"succeeded", "failed", "outcome_unknown"}:
            raise ServiceContractError("attempt completion status is invalid")
        if type(possible_duplicate_cost) is not bool:
            raise ServiceContractError("possible_duplicate_cost must be a boolean")
        values: dict[str, Any] = {"status": status}
        if status == "succeeded":
            if result_hash is None:
                raise ServiceContractError("succeeded attempt requires result_hash")
            values.update(
                retryable=None,
                error_code=None,
                result_hash=_canonical_sha256_identifier(result_hash, "result_hash"),
                result_ref=result_ref or f"attempt-result:{resolved_attempt_id}",
            )
        else:
            if type(retryable) is not bool or error_code is None:
                raise ServiceContractError(
                    "failed attempt requires retryable and error_code"
                )
            values.update(
                retryable=retryable,
                error_code=_canonical_error_code(error_code),
                result_ref=None,
                result_hash=None,
            )
        with self.engine.begin() as connection:
            attempt = (
                connection.execute(
                    select(run_external_attempts)
                    .where(run_external_attempts.c.attempt_id == resolved_attempt_id)
                    .with_for_update(of=run_external_attempts)
                )
                .mappings()
                .one_or_none()
            )
            if attempt is None:
                raise ResourceNotFoundError()
            run_row = self._run_row(connection, attempt["run_id"], for_update=True)
            if run_row is None:
                raise ResourceNotFoundError()
            now = self._database_now(connection)
            self._assert_worker_lease(
                run_row,
                resolved_worker,
                now,
                lease_epoch=resolved_epoch,
            )
            if int(attempt["lease_epoch"]) != resolved_epoch:
                raise WorkerLeaseLostError("attempt belongs to an older lease epoch")
            if attempt["status"] == status:
                return
            if attempt["status"] != "dispatched":
                raise InvalidRunStateError("attempt was not dispatched")
            values["completed_at"] = now
            connection.execute(
                update(run_external_attempts)
                .where(
                    run_external_attempts.c.attempt_id == resolved_attempt_id,
                    run_external_attempts.c.status == "dispatched",
                )
                .values(**values)
            )
            if status == "outcome_unknown":
                self._transition_with_event(
                    connection,
                    run_row,
                    values={},
                    event_type="attempt.outcome_unknown",
                    safe_payload={
                        "status": status,
                        "possible_duplicate_cost": possible_duplicate_cost,
                        "budget_refunded": False,
                    },
                    now=now,
                )

    def has_outcome_unknown(self, run_id: str, *, operation_name: str) -> bool:
        resolved_run_id = _canonical_identifier(run_id, "run_id", 36)
        resolved_name = _canonical_identifier(operation_name, "operation_name", 64)
        with self.engine.connect() as connection:
            count = connection.scalar(
                select(func.count())
                .select_from(run_external_attempts)
                .where(
                    run_external_attempts.c.run_id == resolved_run_id,
                    run_external_attempts.c.operation_name == resolved_name,
                    run_external_attempts.c.status == "outcome_unknown",
                )
            )
        return bool(count)

    def save_node_artifact(
        self,
        run_id: str,
        *,
        worker_id: str,
        lease_epoch: int,
        artifact_kind: str,
        payload: Mapping[str, Any],
    ) -> StoredArtifactRef:
        resolved_run_id = _canonical_identifier(run_id, "run_id", 36)
        resolved_worker = _canonical_identifier(worker_id, "worker_id", 128)
        resolved_epoch = _canonical_positive_integer(lease_epoch, "lease_epoch")
        normalized_kind, node_name = self._artifact_identity(artifact_kind)
        copied = _canonical_json_copy(payload)
        if not isinstance(copied, dict):
            raise ServiceContractError("artifact payload must be an object")
        payload_hash = canonical_json_sha256(copied)
        artifact_id = str(uuid.uuid4())
        with self.engine.begin() as connection:
            run_row = self._run_row(connection, resolved_run_id, for_update=True)
            if run_row is None:
                raise ResourceNotFoundError()
            now = self._database_now(connection)
            self._assert_worker_lease(
                run_row,
                resolved_worker,
                now,
                lease_epoch=resolved_epoch,
            )
            connection.execute(
                insert(run_node_artifacts).values(
                    artifact_id=artifact_id,
                    run_id=resolved_run_id,
                    node_name=node_name,
                    artifact_kind=normalized_kind,
                    artifact_ref=f"run-artifact:{artifact_id}",
                    payload_hash=payload_hash,
                    payload=copied,
                    lease_epoch=resolved_epoch,
                    created_at=now,
                )
            )
        return StoredArtifactRef(artifact_id=artifact_id, payload_hash=payload_hash)

    def load_node_artifact(
        self,
        run_id: str,
        artifact_id: str,
        *,
        expected_kind: str,
        expected_hash: str,
    ) -> Mapping[str, Any]:
        resolved_run_id = _canonical_identifier(run_id, "run_id", 36)
        resolved_artifact_id = _canonical_identifier(artifact_id, "artifact_id", 36)
        normalized_kind, _ = self._artifact_identity(expected_kind)
        resolved_hash = _canonical_sha256_identifier(expected_hash, "expected_hash")
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(run_node_artifacts).where(
                        run_node_artifacts.c.run_id == resolved_run_id,
                        run_node_artifacts.c.artifact_id == resolved_artifact_id,
                        run_node_artifacts.c.artifact_kind == normalized_kind,
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise ResourceNotFoundError()
        payload = _readonly_json_mapping(row["payload"], "artifact payload")
        if row["payload_hash"] != resolved_hash:
            raise RunServiceDataError("artifact hash does not match checkpoint")
        if canonical_json_sha256(payload) != resolved_hash:
            raise RunServiceDataError("artifact payload integrity check failed")
        return payload

    def bind_checkpoint(
        self,
        *,
        run_id: str,
        worker_id: str,
        lease_epoch: int,
        checkpoint_namespace: str,
        checkpoint_id: str,
        parent_checkpoint_id: str | None,
        state: HarnessState,
    ) -> None:
        """Project one LangGraph checkpoint into the trusted application schema."""

        resolved_run_id = _canonical_identifier(run_id, "run_id", 36)
        resolved_worker = _canonical_identifier(worker_id, "worker_id", 128)
        resolved_epoch = _canonical_positive_integer(lease_epoch, "lease_epoch")
        resolved_namespace = _canonical_identifier(
            checkpoint_namespace, "checkpoint_namespace", 255
        )
        resolved_checkpoint = _canonical_identifier(checkpoint_id, "checkpoint_id", 255)
        if parent_checkpoint_id is not None:
            _canonical_identifier(parent_checkpoint_id, "parent_checkpoint_id", 255)
        try:
            projected = validate_harness_state(dict(state))
            projected["checkpoint_id"] = resolved_checkpoint
            projected = validate_harness_state(projected)
        except HarnessStateError as exc:
            raise CheckpointCompatibilityError(str(exc)) from exc
        state_hash = harness_state_hash(projected)
        with self.engine.begin() as connection:
            run_row = self._run_row(connection, resolved_run_id, for_update=True)
            if run_row is None:
                raise ResourceNotFoundError()
            now = self._database_now(connection)
            self._assert_epoch_or_published_terminal(
                connection,
                run_row,
                resolved_worker,
                resolved_epoch,
                now,
            )
            self._assert_checkpoint_identity(run_row, projected)
            trusted_parent = (
                run_row["last_checkpoint_id"]
                if run_row["checkpoint_namespace"] == resolved_namespace
                else None
            )
            if trusted_parent == resolved_checkpoint:
                trusted_parent = None
            statement = postgresql_insert(run_checkpoints).values(
                run_id=resolved_run_id,
                checkpoint_namespace=resolved_namespace,
                checkpoint_id=resolved_checkpoint,
                parent_checkpoint_id=trusted_parent,
                schema_version=projected["schema_version"],
                graph_version=projected["graph_version"],
                retrieval_config_hash=projected["retrieval_config_hash"],
                last_completed_node=projected["last_completed_node"],
                state_payload=projected,
                state_hash=state_hash,
                lease_epoch=resolved_epoch,
                created_at=now,
            )
            connection.execute(statement.on_conflict_do_nothing())
            existing = (
                connection.execute(
                    select(run_checkpoints).where(
                        run_checkpoints.c.run_id == resolved_run_id,
                        run_checkpoints.c.checkpoint_namespace == resolved_namespace,
                        run_checkpoints.c.checkpoint_id == resolved_checkpoint,
                    )
                )
                .mappings()
                .one()
            )
            if existing["state_hash"] != state_hash:
                raise CheckpointCompatibilityError(
                    "checkpoint identifier is bound to different state"
                )
            connection.execute(
                update(runs)
                .where(
                    runs.c.run_id == resolved_run_id,
                    runs.c.revision == run_row["revision"],
                    runs.c.lease_epoch == resolved_epoch,
                )
                .values(
                    checkpoint_namespace=resolved_namespace,
                    last_checkpoint_id=resolved_checkpoint,
                    last_completed_node=projected["last_completed_node"],
                    revision=int(run_row["revision"]) + 1,
                    updated_at=now,
                )
            )

    def load_trusted_checkpoint(self, run_id: str) -> HarnessState | None:
        resolved_run_id = _canonical_identifier(run_id, "run_id", 36)
        with self.engine.connect() as connection:
            row = self._run_row(connection, resolved_run_id)
            if row is None:
                raise ResourceNotFoundError()
            checkpoint = self._trusted_checkpoint_row(
                connection,
                row,
                validate_payload=True,
            )
            if checkpoint is None:
                return None
            return validate_harness_state(dict(checkpoint["state_payload"]))

    def append_stage_event(
        self,
        run_id: str,
        event_type: str,
        safe_payload: JSONMapping,
        *,
        worker_id: str,
        lease_epoch: int | None = None,
    ) -> RunEventRecord:
        """Append one safe stage fact under the current worker lease fence."""

        resolved_run_id = _canonical_identifier(run_id, "run_id", 36)
        resolved_worker = _canonical_identifier(worker_id, "worker_id", 128)
        if event_type not in STAGE_EVENT_TYPES:
            raise ServiceContractError("event_type is not an appendable stage event")
        payload = _safe_stage_event_payload(event_type, safe_payload)
        with self.engine.begin() as connection:
            row = self._run_row(connection, resolved_run_id, for_update=True)
            if row is None:
                raise ResourceNotFoundError()
            now = self._database_now(connection)
            self._assert_worker_lease(
                row,
                resolved_worker,
                now,
                lease_epoch=lease_epoch,
            )
            sequence = self._transition_with_event(
                connection,
                row,
                values={},
                event_type=event_type,
                safe_payload=payload,
                now=now,
            )
            event_row = self._event_row(connection, resolved_run_id, sequence)
            if event_row is None:  # pragma: no cover - insert/read invariant.
                raise RunServiceDataError("appended event could not be read back")
            return self._event_record(event_row)

    def publish_success(
        self,
        run_id: str,
        worker_id: str,
        result: SafeRunResult | SafeRunResultLike | JSONMapping,
        *,
        lease_epoch: int | None = None,
    ) -> RunRecord:
        """Atomically publish the only final assistant message and result."""

        return self._publish_terminal_result(
            run_id,
            worker_id,
            lease_epoch,
            result,
            completion_status="succeeded",
            stop_reason=None,
        )

    def publish_terminal_result(
        self,
        run_id: str,
        worker_id: str,
        lease_epoch: int,
        result: SafeRunResult | SafeRunResultLike | JSONMapping,
        *,
        completion_status: str,
        stop_reason: str | None,
    ) -> RunRecord:
        """Publish one verified answer under an explicit M5 terminal status."""

        resolved_epoch = _canonical_positive_integer(lease_epoch, "lease_epoch")
        return self._publish_terminal_result(
            run_id,
            worker_id,
            resolved_epoch,
            result,
            completion_status=completion_status,
            stop_reason=stop_reason,
        )

    def _publish_terminal_result(
        self,
        run_id: str,
        worker_id: str,
        lease_epoch: int | None,
        result: SafeRunResult | SafeRunResultLike | JSONMapping,
        *,
        completion_status: str,
        stop_reason: str | None,
    ) -> RunRecord:
        if completion_status not in ANSWER_BEARING_RUN_STATUSES:
            raise ServiceContractError("completion_status is invalid")
        resolved_stop_reason = (
            None
            if stop_reason is None
            else _canonical_identifier(stop_reason, "stop_reason", 64)
        )

        resolved_run_id = _canonical_identifier(run_id, "run_id", 36)
        resolved_worker = _canonical_identifier(worker_id, "worker_id", 128)
        safe_result = _coerce_safe_result(result)
        with self.engine.begin() as connection:
            row = self._run_row(connection, resolved_run_id, for_update=True)
            if row is None:
                raise ResourceNotFoundError()
            if row["status"] in ANSWER_BEARING_RUN_STATUSES:
                existing = self._run_result_record(connection, resolved_run_id)
                if existing is None:
                    raise RunServiceDataError("terminal run has no final result")
                if (
                    row["status"] != completion_status
                    or row["stop_reason"] != resolved_stop_reason
                    or not self._safe_result_matches(existing, safe_result)
                ):
                    raise InvalidRunStateError(
                        "run already completed with a different final result"
                    )
                return self._run_record(connection, row)
            now = self._database_now(connection)
            self._assert_worker_lease(
                row,
                resolved_worker,
                now,
                lease_epoch=lease_epoch,
            )

            existing_assistant = connection.scalar(
                select(func.count())
                .select_from(messages)
                .where(
                    messages.c.run_id == resolved_run_id,
                    messages.c.role == "assistant",
                )
            )
            if existing_assistant:
                raise RunServiceDataError(
                    "non-succeeded run already has an assistant message"
                )
            message_id = str(uuid.uuid4())
            ordinal = self._next_message_ordinal(connection, row["session_id"])
            connection.execute(
                insert(messages).values(
                    message_id=message_id,
                    session_id=row["session_id"],
                    user_id=row["user_id"],
                    role="assistant",
                    content=safe_result.answer_text,
                    run_id=resolved_run_id,
                    ordinal=ordinal,
                )
            )
            connection.execute(
                insert(run_results).values(
                    run_id=resolved_run_id,
                    final_message_id=message_id,
                    answer_payload=safe_result.answer_payload,
                    evidence_payload=safe_result.evidence_payload,
                    verification_payload=safe_result.verification_payload,
                )
            )
            # The final event is inserted only after the result rows exist in
            # this transaction; all become visible together at commit.
            self._transition_with_event(
                connection,
                row,
                values={
                    "status": completion_status,
                    "finished_at": now,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "error_code": None,
                    "stop_reason": resolved_stop_reason,
                },
                event_type={
                    "succeeded": "answer.final",
                    "completed_with_limits": "run.completed_with_limits",
                    "needs_clarification": "run.needs_clarification",
                }[completion_status],
                safe_payload={
                    "status": completion_status,
                    "message_id": message_id,
                    "stop_reason": resolved_stop_reason,
                },
                now=now,
            )
            changed = self._run_row(connection, resolved_run_id)
            if changed is None:  # pragma: no cover - locked row invariant.
                raise RunServiceDataError("published run disappeared")
            return self._run_record(connection, changed)

    def fail_run(
        self,
        run_id: str,
        worker_id: str,
        error_code: str,
        safe_payload: JSONMapping | None = None,
        *,
        lease_epoch: int | None = None,
    ) -> RunRecord:
        """Atomically mark a leased running run failed without storing errors."""

        resolved_run_id = _canonical_identifier(run_id, "run_id", 36)
        resolved_worker = _canonical_identifier(worker_id, "worker_id", 128)
        resolved_error = _canonical_error_code(error_code)
        details = _safe_failure_payload(
            safe_payload or {},
            error_code=resolved_error,
        )
        with self.engine.begin() as connection:
            row = self._run_row(connection, resolved_run_id, for_update=True)
            if row is None:
                raise ResourceNotFoundError()
            if row["status"] == "failed" and row["error_code"] == resolved_error:
                return self._run_record(connection, row)
            now = self._database_now(connection)
            self._assert_worker_lease(
                row,
                resolved_worker,
                now,
                lease_epoch=lease_epoch,
            )
            self._transition_with_event(
                connection,
                row,
                values={
                    "status": "failed",
                    "finished_at": now,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "error_code": resolved_error,
                },
                event_type="run.failed",
                safe_payload=details,
                now=now,
            )
            changed = self._run_row(connection, resolved_run_id)
            if changed is None:  # pragma: no cover - locked row invariant.
                raise RunServiceDataError("failed run disappeared")
            return self._run_record(connection, changed)

    @staticmethod
    def _artifact_identity(value: str) -> tuple[str, str]:
        aliases = {
            "retrieval": ("retrieval", "retrieve"),
            "retrieved_turn": ("retrieval", "retrieve"),
            "verification": ("verification", "generate"),
            "verified_result": ("verification", "generate"),
            "terminal": ("terminal", "persist_result"),
        }
        try:
            return aliases[value]
        except (KeyError, TypeError) as exc:
            raise ServiceContractError("artifact_kind is invalid") from exc

    @staticmethod
    def _budget_snapshot(
        row: Mapping[str, Any], execution_deadline_at: datetime
    ) -> BudgetSnapshot:
        if not isinstance(execution_deadline_at, datetime):
            raise RunServiceDataError("run budget has no absolute deadline")
        return BudgetSnapshot(
            run_id=row["run_id"],
            max_retrieval_rounds=int(row["max_retrieval_rounds"]),
            max_queries_per_round=int(row["max_queries_per_round"]),
            max_tool_attempts=int(row["max_tool_attempts"]),
            max_model_attempts=int(row["max_model_attempts"]),
            max_embedding_attempts=int(row["max_embedding_attempts"]),
            max_retry_per_operation=int(row["max_retry_per_operation"]),
            evidence_top_k=int(row["evidence_top_k"]),
            retrieval_rounds_used=int(row["retrieval_rounds_used"]),
            queries_used=int(row["queries_used"]),
            tool_attempts_used=int(row["tool_attempts_used"]),
            model_attempts_used=int(row["model_attempts_used"]),
            embedding_attempts_used=int(row["embedding_attempts_used"]),
            execution_deadline_at=execution_deadline_at,
            revision=int(row["revision"]),
        )

    @staticmethod
    def _assert_execution_deadline(row: Mapping[str, Any], now: datetime) -> None:
        deadline = row["execution_deadline_at"]
        if not isinstance(deadline, datetime):
            raise RunServiceDataError("running run has no absolute deadline")
        normalized_deadline = (
            deadline
            if deadline.tzinfo is not None
            else deadline.replace(tzinfo=timezone.utc)
        )
        if normalized_deadline <= now:
            raise BudgetExhausted("deadline_exceeded")

    @staticmethod
    def _assert_checkpoint_identity(
        run_row: Mapping[str, Any], state: HarnessState
    ) -> None:
        expected = {
            "run_id": run_row["run_id"],
            "session_id": run_row["session_id"],
            "user_id": run_row["user_id"],
            "schema_version": int(run_row["state_schema_version"]),
            "graph_version": run_row["graph_version"],
            "retrieval_config_hash": run_row["retrieval_config_hash"],
            "snapshot_id": run_row["snapshot_id"],
            "embedding_profile_id": run_row["profile_id"],
        }
        for field, expected_value in expected.items():
            if state[field] != expected_value:
                raise CheckpointCompatibilityError(
                    f"checkpoint {field} does not match the frozen run"
                )

    @classmethod
    def _trusted_checkpoint_row(
        cls,
        connection: Connection,
        run_row: Mapping[str, Any],
        *,
        validate_payload: bool,
    ) -> Mapping[str, Any] | None:
        namespace = run_row["checkpoint_namespace"]
        checkpoint_id = run_row["last_checkpoint_id"]
        if namespace is None and checkpoint_id is None:
            return None
        if not isinstance(namespace, str) or not isinstance(checkpoint_id, str):
            raise CheckpointCompatibilityError(
                "trusted checkpoint pointer is incomplete"
            )
        row = (
            connection.execute(
                select(run_checkpoints).where(
                    run_checkpoints.c.run_id == run_row["run_id"],
                    run_checkpoints.c.checkpoint_namespace == namespace,
                    run_checkpoints.c.checkpoint_id == checkpoint_id,
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise CheckpointCompatibilityError("trusted checkpoint is missing")
        if not validate_payload:
            return row
        try:
            state = validate_harness_state(dict(row["state_payload"]))
        except HarnessStateError as exc:
            raise CheckpointCompatibilityError(str(exc)) from exc
        if harness_state_hash(state) != row["state_hash"]:
            raise CheckpointCompatibilityError("trusted checkpoint hash is invalid")
        if state["checkpoint_id"] != checkpoint_id:
            raise CheckpointCompatibilityError("trusted checkpoint identity is invalid")
        cls._assert_checkpoint_identity(run_row, state)
        return row

    @classmethod
    def _assert_epoch_or_published_terminal(
        cls,
        connection: Connection,
        row: Mapping[str, Any],
        worker_id: str,
        lease_epoch: int,
        now: datetime,
    ) -> None:
        if (
            row["status"] in ANSWER_BEARING_RUN_STATUSES
            and int(row["lease_epoch"]) == lease_epoch
            and cls._run_result_record(connection, row["run_id"]) is not None
        ):
            return
        cls._assert_worker_lease(
            row,
            worker_id,
            now,
            lease_epoch=lease_epoch,
        )

    @staticmethod
    def _database_now(connection: Connection) -> datetime:
        # PostgreSQL ``now()`` is frozen at transaction start. A worker can wait
        # on a row lock past its lease deadline, so lease fences and TTLs must
        # use the actual wall clock after the lock is acquired.
        value = connection.scalar(select(func.clock_timestamp()))
        if not isinstance(value, datetime):
            raise RunServiceDataError("database did not return a timestamp")
        # PostgreSQL returns an aware value for TIMESTAMPTZ.  Some lightweight
        # SQLAlchemy test dialects return a naive UTC value; normalizing that
        # representation does not alter the persisted instant.
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    @staticmethod
    def _session_row(
        connection: Connection,
        session_id: str,
        user_id: str,
        *,
        for_update: bool = False,
    ) -> Mapping[str, Any] | None:
        statement: Select[Any] = select(sessions).where(
            sessions.c.session_id == session_id,
            sessions.c.user_id == user_id,
        )
        if for_update:
            statement = statement.with_for_update(of=sessions)
        return connection.execute(statement).mappings().one_or_none()

    @staticmethod
    def _run_row(
        connection: Connection,
        run_id: str,
        *,
        for_update: bool = False,
    ) -> Mapping[str, Any] | None:
        statement: Select[Any] = select(runs).where(runs.c.run_id == run_id)
        if for_update:
            statement = statement.with_for_update(of=runs)
        return connection.execute(statement).mappings().one_or_none()

    @staticmethod
    def _owned_run_row(
        connection: Connection,
        run_id: str,
        user_id: str,
        *,
        for_update: bool = False,
    ) -> Mapping[str, Any] | None:
        statement: Select[Any] = select(runs).where(
            runs.c.run_id == run_id,
            runs.c.user_id == user_id,
        )
        if for_update:
            statement = statement.with_for_update(of=runs)
        return connection.execute(statement).mappings().one_or_none()

    @classmethod
    def _owned_run_record(
        cls,
        connection: Connection,
        run_id: str,
        user_id: str,
    ) -> RunRecord | None:
        """Read a run and optional result from one PostgreSQL statement snapshot."""

        row = (
            connection.execute(
                select(
                    runs,
                    run_results.c.run_id.label("_result_run_id"),
                    run_results.c.final_message_id.label("_result_message_id"),
                    run_results.c.answer_payload.label("_result_answer_payload"),
                    run_results.c.evidence_payload.label("_result_evidence_payload"),
                    run_results.c.verification_payload.label(
                        "_result_verification_payload"
                    ),
                    run_results.c.created_at.label("_result_created_at"),
                    messages.c.content.label("_result_answer_text"),
                )
                .select_from(
                    runs.outerjoin(
                        run_results,
                        run_results.c.run_id == runs.c.run_id,
                    ).outerjoin(
                        messages,
                        messages.c.message_id == run_results.c.final_message_id,
                    )
                )
                .where(
                    runs.c.run_id == run_id,
                    runs.c.user_id == user_id,
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        result = cls._embedded_run_result_record(row)
        return cls._build_run_record(row, result)

    @staticmethod
    def _event_row(
        connection: Connection, run_id: str, sequence: int
    ) -> Mapping[str, Any] | None:
        return (
            connection.execute(
                select(run_events).where(
                    run_events.c.run_id == run_id,
                    run_events.c.sequence == sequence,
                )
            )
            .mappings()
            .one_or_none()
        )

    @staticmethod
    def _next_message_ordinal(connection: Connection, session_id: str) -> int:
        value = connection.scalar(
            select(func.coalesce(func.max(messages.c.ordinal), 0)).where(
                messages.c.session_id == session_id
            )
        )
        if type(value) is not int or value < 0:
            raise RunServiceDataError("session message ordinal is invalid")
        return value + 1

    @staticmethod
    def _freeze_retrieval_binding(
        connection: Connection, principal: ServicePrincipal
    ) -> Mapping[str, Any]:
        pointer_event = (
            connection.execute(
                select(
                    active_snapshot_pointers.c.scope_id,
                    active_snapshot_pointers.c.snapshot_id,
                    active_snapshot_pointers.c.revision,
                    active_snapshot_pointers.c.activation_id,
                    corpus_snapshots.c.status.label("snapshot_status"),
                    snapshot_activation_events.c.target_snapshot_id,
                    snapshot_activation_events.c.occurred_at,
                )
                .select_from(
                    active_snapshot_pointers.join(
                        snapshot_activation_events,
                        and_(
                            snapshot_activation_events.c.scope_id
                            == active_snapshot_pointers.c.scope_id,
                            snapshot_activation_events.c.revision
                            == active_snapshot_pointers.c.revision,
                            snapshot_activation_events.c.activation_id
                            == active_snapshot_pointers.c.activation_id,
                            snapshot_activation_events.c.target_snapshot_id
                            == active_snapshot_pointers.c.snapshot_id,
                        ),
                    ).join(
                        corpus_snapshots,
                        and_(
                            corpus_snapshots.c.scope_id
                            == active_snapshot_pointers.c.scope_id,
                            corpus_snapshots.c.snapshot_id
                            == active_snapshot_pointers.c.snapshot_id,
                        ),
                    )
                )
                .where(active_snapshot_pointers.c.scope_id == principal.scope_id)
                .with_for_update(of=active_snapshot_pointers)
            )
            .mappings()
            .one_or_none()
        )
        if pointer_event is None or pointer_event["snapshot_status"] != "active":
            raise RunConfigurationUnavailableError(
                "the configured scope has no serviceable active snapshot"
            )
        profile_import = (
            connection.execute(
                select(
                    embedding_imports.c.status,
                    embedding_profiles.c.profile_id,
                )
                .select_from(
                    embedding_imports.join(
                        embedding_profiles,
                        embedding_profiles.c.profile_id
                        == embedding_imports.c.profile_id,
                    )
                )
                .where(
                    embedding_imports.c.snapshot_id == pointer_event["snapshot_id"],
                    embedding_imports.c.profile_id == principal.profile_id,
                )
            )
            .mappings()
            .one_or_none()
        )
        if profile_import is None or profile_import["status"] != "validated":
            raise RunConfigurationUnavailableError(
                "the configured profile has no validated import for the active snapshot"
            )
        return pointer_event

    @staticmethod
    def _assert_worker_lease(
        row: Mapping[str, Any],
        worker_id: str,
        now: datetime,
        *,
        lease_epoch: int | None = None,
    ) -> None:
        expires_at = row["lease_expires_at"]
        if (
            row["status"] != "running"
            or row["lease_owner"] != worker_id
            or not isinstance(expires_at, datetime)
        ):
            raise WorkerLeaseLostError("worker does not own the running lease")
        if lease_epoch is not None and int(row["lease_epoch"]) != lease_epoch:
            raise WorkerLeaseLostError("worker lease epoch is stale")
        normalized_expiry = (
            expires_at
            if expires_at.tzinfo is not None
            else expires_at.replace(tzinfo=timezone.utc)
        )
        if normalized_expiry <= now:
            raise WorkerLeaseLostError("worker lease has expired")

    @staticmethod
    def _transition_with_event(
        connection: Connection,
        row: Mapping[str, Any],
        *,
        values: Mapping[str, Any],
        event_type: str,
        safe_payload: Mapping[str, Any],
        now: datetime,
    ) -> int:
        if event_type not in RUN_EVENT_TYPES:
            raise ServiceContractError("event_type is not persisted by M4")
        sequence = int(row["event_sequence"]) + 1
        revision = int(row["revision"]) + 1
        update_values = dict(values)
        update_values.update(
            event_sequence=sequence,
            revision=revision,
            updated_at=now,
        )
        changed = connection.execute(
            update(runs)
            .where(
                runs.c.run_id == row["run_id"],
                runs.c.revision == row["revision"],
                runs.c.event_sequence == row["event_sequence"],
            )
            .values(**update_values)
        )
        if changed.rowcount != 1:
            raise InvalidRunStateError("run changed while appending an event")
        connection.execute(
            insert(run_events).values(
                run_id=row["run_id"],
                sequence=sequence,
                event_type=event_type,
                safe_payload=dict(safe_payload),
                created_at=now,
            )
        )
        return sequence

    @staticmethod
    def _completed_history(
        connection: Connection,
        *,
        session_id: str,
        user_id: str,
        before_ordinal: int,
        message_limit: int,
        character_limit: int,
    ) -> tuple[MessageRecord, ...]:
        # Select recent complete turns first.  Joining run_results means a
        # corrupted status flag alone can never introduce an unfinished draft.
        completed_runs = (
            connection.execute(
                select(
                    runs.c.run_id,
                    func.max(messages.c.ordinal).label("last_ordinal"),
                )
                .select_from(
                    runs.join(messages, messages.c.run_id == runs.c.run_id).join(
                        run_results, run_results.c.run_id == runs.c.run_id
                    )
                )
                .where(
                    runs.c.session_id == session_id,
                    runs.c.user_id == user_id,
                    runs.c.status.in_(tuple(ANSWER_BEARING_RUN_STATUSES)),
                    messages.c.ordinal < before_ordinal,
                )
                .group_by(runs.c.run_id)
                .order_by(desc("last_ordinal"), runs.c.run_id)
                .limit(message_limit)
            )
            .mappings()
            .all()
        )
        if not completed_runs:
            return ()
        run_ids = [row["run_id"] for row in completed_runs]
        history_rows = (
            connection.execute(
                select(messages)
                .where(
                    messages.c.run_id.in_(run_ids),
                    messages.c.session_id == session_id,
                    messages.c.user_id == user_id,
                    messages.c.ordinal < before_ordinal,
                )
                .order_by(messages.c.ordinal)
            )
            .mappings()
            .all()
        )
        by_run: dict[str, list[MessageRecord]] = {}
        for history_row in history_rows:
            by_run.setdefault(history_row["run_id"], []).append(
                RunService._message_record(history_row)
            )

        selected_newest_first: list[list[MessageRecord]] = []
        used_messages = 0
        used_characters = 0
        for completed in completed_runs:
            turn = by_run.get(completed["run_id"], [])
            if [item.role for item in turn] != ["user", "assistant"]:
                # A succeeded run is only valid after atomic final publication.
                # Ignore malformed history rather than exposing a partial turn.
                continue
            turn_characters = sum(len(item.content) for item in turn)
            if used_messages + len(turn) > message_limit:
                continue
            if used_characters + turn_characters > character_limit:
                continue
            selected_newest_first.append(turn)
            used_messages += len(turn)
            used_characters += turn_characters
        selected: list[MessageRecord] = []
        for turn in reversed(selected_newest_first):
            selected.extend(turn)
        return tuple(selected)

    @staticmethod
    def _safe_result_matches(
        existing: RunResultRecord, candidate: _PreparedSafeRunResult
    ) -> bool:
        return (
            existing.answer_text == candidate.answer_text
            and canonical_json_bytes(existing.answer_payload)
            == canonical_json_bytes(candidate.answer_payload)
            and canonical_json_bytes(existing.evidence_payload)
            == canonical_json_bytes(candidate.evidence_payload)
            and canonical_json_bytes(existing.verification_payload)
            == canonical_json_bytes(candidate.verification_payload)
        )

    @staticmethod
    def _session_record(row: Mapping[str, Any]) -> SessionRecord:
        return SessionRecord(
            session_id=row["session_id"],
            user_id=row["user_id"],
            title=row["title"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _message_record(row: Mapping[str, Any]) -> MessageRecord:
        return MessageRecord(
            message_id=row["message_id"],
            session_id=row["session_id"],
            user_id=row["user_id"],
            role=row["role"],
            content=row["content"],
            run_id=row["run_id"],
            ordinal=int(row["ordinal"]),
            created_at=row["created_at"],
        )

    @staticmethod
    def _event_record(row: Mapping[str, Any]) -> RunEventRecord:
        return RunEventRecord(
            run_id=row["run_id"],
            sequence=int(row["sequence"]),
            event_type=row["event_type"],
            safe_payload=_readonly_json_mapping(row["safe_payload"], "safe_payload"),
            created_at=row["created_at"],
        )

    @staticmethod
    def _run_result_record(
        connection: Connection, run_id: str
    ) -> RunResultRecord | None:
        row = (
            connection.execute(
                select(
                    run_results.c.run_id,
                    run_results.c.final_message_id,
                    run_results.c.answer_payload,
                    run_results.c.evidence_payload,
                    run_results.c.verification_payload,
                    run_results.c.created_at,
                    messages.c.content.label("answer_text"),
                )
                .select_from(
                    run_results.join(
                        messages,
                        messages.c.message_id == run_results.c.final_message_id,
                    )
                )
                .where(run_results.c.run_id == run_id)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return RunResultRecord(
            run_id=row["run_id"],
            final_message_id=row["final_message_id"],
            answer_text=row["answer_text"],
            answer_payload=_readonly_json_mapping(
                row["answer_payload"], "answer_payload"
            ),
            evidence_payload=_readonly_json_mapping(
                row["evidence_payload"], "evidence_payload"
            ),
            verification_payload=_readonly_json_mapping(
                row["verification_payload"], "verification_payload"
            ),
            created_at=row["created_at"],
        )

    @staticmethod
    def _embedded_run_result_record(
        row: Mapping[str, Any],
    ) -> RunResultRecord | None:
        if row["_result_run_id"] is None:
            return None
        answer_text = row["_result_answer_text"]
        if not isinstance(answer_text, str):
            raise RunServiceDataError("run result has no final assistant message")
        return RunResultRecord(
            run_id=row["_result_run_id"],
            final_message_id=row["_result_message_id"],
            answer_text=answer_text,
            answer_payload=_readonly_json_mapping(
                row["_result_answer_payload"], "answer_payload"
            ),
            evidence_payload=_readonly_json_mapping(
                row["_result_evidence_payload"], "evidence_payload"
            ),
            verification_payload=_readonly_json_mapping(
                row["_result_verification_payload"], "verification_payload"
            ),
            created_at=row["_result_created_at"],
        )

    @staticmethod
    def _build_run_record(
        row: Mapping[str, Any],
        result: RunResultRecord | None,
    ) -> RunRecord:
        if row["status"] in ANSWER_BEARING_RUN_STATUSES and result is None:
            raise RunServiceDataError("answer-bearing run has no final result")
        if row["status"] not in ANSWER_BEARING_RUN_STATUSES and result is not None:
            raise RunServiceDataError("non-answer-bearing run has a final result")
        return RunRecord(
            run_id=row["run_id"],
            session_id=row["session_id"],
            user_id=row["user_id"],
            status=row["status"],
            request_hash=row["request_hash"],
            request_payload=_readonly_json_mapping(
                row["request_payload"], "request_payload"
            ),
            scope_id=row["scope_id"],
            snapshot_id=row["snapshot_id"],
            snapshot_revision=int(row["snapshot_revision"]),
            activation_id=row["activation_id"],
            profile_id=row["profile_id"],
            boundary_fingerprint=row["boundary_fingerprint"],
            retrieval_config_hash=row["retrieval_config_hash"],
            graph_version=row["graph_version"],
            created_at=row["created_at"],
            queued_at=row["queued_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            updated_at=row["updated_at"],
            error_code=row["error_code"],
            revision=int(row["revision"]),
            event_sequence=int(row["event_sequence"]),
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            result=result,
            parent_run_id=row["parent_run_id"],
            state_schema_version=int(row["state_schema_version"]),
            checkpoint_namespace=row["checkpoint_namespace"],
            last_checkpoint_id=row["last_checkpoint_id"],
            last_completed_node=row["last_completed_node"],
            execution_deadline_at=row["execution_deadline_at"],
            stop_reason=row["stop_reason"],
            lease_epoch=int(row["lease_epoch"]),
            resume_requested_at=row["resume_requested_at"],
        )

    @classmethod
    def _run_record(cls, connection: Connection, row: Mapping[str, Any]) -> RunRecord:
        result = cls._run_result_record(connection, row["run_id"])
        return cls._build_run_record(row, result)


__all__ = [
    "ACTIVE_RUN_STATUSES",
    "ANSWER_BEARING_RUN_STATUSES",
    "ActiveRunConflictError",
    "CheckpointCompatibilityError",
    "DEFAULT_EVENT_PAGE_SIZE",
    "DEFAULT_HISTORY_CHARACTERS",
    "DEFAULT_HISTORY_MESSAGES",
    "DEFAULT_IDEMPOTENCY_TTL",
    "DEFAULT_MESSAGE_PAGE_SIZE",
    "FrozenRunInput",
    "IdempotencyConflictError",
    "InvalidRunStateError",
    "JSONMapping",
    "LeaseLostError",
    "MessagePage",
    "MessageRecord",
    "NotFoundError",
    "Principal",
    "ResourceNotFoundError",
    "ResumeUnsupportedError",
    "RunConfigurationUnavailableError",
    "RunConflictError",
    "RunEventRecord",
    "RunRecord",
    "RunResultRecord",
    "RunService",
    "RunServiceDataError",
    "RunServiceError",
    "STAGE_EVENT_TYPES",
    "SafeRunResult",
    "SafeRunResultLike",
    "ServiceContractError",
    "ServicePrincipal",
    "SessionInactiveError",
    "SessionRecord",
    "StoredArtifactRef",
    "TERMINAL_RUN_STATUSES",
    "UnsafePayloadError",
    "WorkerLeaseLostError",
    "canonical_json_bytes",
    "canonical_json_sha256",
]
