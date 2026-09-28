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

from legal_rag.retrieval_contracts import RetrievalBoundary
from legal_rag.storage.schema import (
    active_snapshot_pointers,
    corpus_snapshots,
    embedding_imports,
    embedding_profiles,
    idempotency_keys,
    messages,
    run_events,
    run_results,
    runs,
    sessions,
    snapshot_activation_events,
)


JSONMapping = Mapping[str, Any]

ACTIVE_RUN_STATUSES = frozenset({"queued", "running", "interrupted"})
TERMINAL_RUN_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
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
        request_hash = canonical_json_sha256(payload)
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
                    replayed_row = self._owned_run_row(
                        connection,
                        idempotency_row["run_id"],
                        bound.user_id,
                    )
                    if replayed_row is None:
                        raise RunServiceDataError(
                            "idempotency key references a missing owned run"
                        )
                    return self._run_record(connection, replayed_row), True
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
                    revision=1,
                    event_sequence=1,
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
            row = self._owned_run_row(connection, resolved_run_id, bound.user_id)
            if row is None:
                raise ResourceNotFoundError()
            return self._run_record(connection, row)

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

    def resume_run(self, principal: ServicePrincipal, run_id: str) -> NoReturn:
        """Compatibility spelling for the explicit unsupported operation."""

        self.resume_unsupported(principal, run_id)

    def claim_next_run(
        self,
        worker_id: str,
        lease_seconds: int,
    ) -> RunRecord | None:
        """Claim the oldest queued run using ``FOR UPDATE SKIP LOCKED``."""

        resolved_worker = _canonical_identifier(worker_id, "worker_id", 128)
        resolved_seconds = _canonical_positive_integer(
            lease_seconds, "lease_seconds", maximum=86_400
        )
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    select(runs)
                    .where(runs.c.status == "queued")
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
            self._transition_with_event(
                connection,
                row,
                values={
                    "status": "running",
                    "started_at": now,
                    "lease_owner": resolved_worker,
                    "lease_expires_at": now + timedelta(seconds=resolved_seconds),
                    "error_code": None,
                },
                event_type="run.started",
                safe_payload={"status": "running"},
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
        """Move only running rows without a live lease to ``interrupted``.

        Interrupted remains an active status in M4.  A user must cancel it to
        start another run; checkpoint resume is intentionally deferred to M5.
        """

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
            self._assert_worker_lease(row, resolved_worker, now)
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
                completed_history=history,
            )

    def append_stage_event(
        self,
        run_id: str,
        event_type: str,
        safe_payload: JSONMapping,
        *,
        worker_id: str,
    ) -> RunEventRecord:
        """Append one safe stage fact under the current worker lease fence."""

        resolved_run_id = _canonical_identifier(run_id, "run_id", 36)
        resolved_worker = _canonical_identifier(worker_id, "worker_id", 128)
        if event_type not in STAGE_EVENT_TYPES:
            raise ServiceContractError("event_type is not an appendable stage event")
        payload = _safe_payload(
            safe_payload,
            field_name="safe_payload",
            maximum_bytes=MAX_EVENT_JSON_BYTES,
        )
        with self.engine.begin() as connection:
            row = self._run_row(connection, resolved_run_id, for_update=True)
            if row is None:
                raise ResourceNotFoundError()
            now = self._database_now(connection)
            self._assert_worker_lease(row, resolved_worker, now)
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
    ) -> RunRecord:
        """Atomically publish the only final assistant message and result."""

        resolved_run_id = _canonical_identifier(run_id, "run_id", 36)
        resolved_worker = _canonical_identifier(worker_id, "worker_id", 128)
        safe_result = _coerce_safe_result(result)
        with self.engine.begin() as connection:
            row = self._run_row(connection, resolved_run_id, for_update=True)
            if row is None:
                raise ResourceNotFoundError()
            if row["status"] == "succeeded":
                existing = self._run_result_record(connection, resolved_run_id)
                if existing is None:
                    raise RunServiceDataError("succeeded run has no final result")
                if not self._safe_result_matches(existing, safe_result):
                    raise InvalidRunStateError(
                        "run already succeeded with a different final result"
                    )
                return self._run_record(connection, row)
            now = self._database_now(connection)
            self._assert_worker_lease(row, resolved_worker, now)

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
                    "status": "succeeded",
                    "finished_at": now,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "error_code": None,
                },
                event_type="answer.final",
                safe_payload={
                    "status": "succeeded",
                    "message_id": message_id,
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
    ) -> RunRecord:
        """Atomically mark a leased running run failed without storing errors."""

        resolved_run_id = _canonical_identifier(run_id, "run_id", 36)
        resolved_worker = _canonical_identifier(worker_id, "worker_id", 128)
        resolved_error = _canonical_error_code(error_code)
        details = _safe_payload(
            safe_payload or {},
            field_name="safe_payload",
            maximum_bytes=MAX_EVENT_JSON_BYTES,
        )
        details["status"] = "failed"
        details["error_code"] = resolved_error
        with self.engine.begin() as connection:
            row = self._run_row(connection, resolved_run_id, for_update=True)
            if row is None:
                raise ResourceNotFoundError()
            if row["status"] == "failed" and row["error_code"] == resolved_error:
                return self._run_record(connection, row)
            now = self._database_now(connection)
            self._assert_worker_lease(row, resolved_worker, now)
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
    def _database_now(connection: Connection) -> datetime:
        value = connection.scalar(select(func.now()))
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
        row: Mapping[str, Any], worker_id: str, now: datetime
    ) -> None:
        expires_at = row["lease_expires_at"]
        if (
            row["status"] != "running"
            or row["lease_owner"] != worker_id
            or not isinstance(expires_at, datetime)
        ):
            raise WorkerLeaseLostError("worker does not own the running lease")
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
                    runs.c.status == "succeeded",
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

    @classmethod
    def _run_record(cls, connection: Connection, row: Mapping[str, Any]) -> RunRecord:
        result = cls._run_result_record(connection, row["run_id"])
        if row["status"] == "succeeded" and result is None:
            raise RunServiceDataError("succeeded run has no final result")
        if row["status"] != "succeeded" and result is not None:
            raise RunServiceDataError("non-succeeded run has a final result")
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
        )


__all__ = [
    "ACTIVE_RUN_STATUSES",
    "ActiveRunConflictError",
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
    "TERMINAL_RUN_STATUSES",
    "UnsafePayloadError",
    "WorkerLeaseLostError",
    "canonical_json_bytes",
    "canonical_json_sha256",
]
