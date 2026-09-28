from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Literal, Protocol, TypeAlias, runtime_checkable
from weakref import WeakSet

from ..chat import (
    MAX_RENDERED_MEMORY_MESSAGES,
    GeneratedTurn,
    LegalChatAssistant,
    RetrievedTurn,
    VerifiedTurn,
    estimate_tokens,
)
from ..json_utils import validate_json_unicode
from ..provider_errors import ProviderCallError, classify_provider_error
from ..retrieval import assert_results_match_boundary
from ..retrieval_contracts import RetrievalBoundary


JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
SafePayload: TypeAlias = Mapping[str, JsonValue]

_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "draft",
        "generated",
        "generation_prompt",
        "memory_text",
        "pre_fallback_answer",
        "pre_fallback_verification",
        "prompt",
        "raw_response",
        "session_state_before",
    }
)


def _text(name: str, value: Any, *, non_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    validate_json_unicode(value)
    if non_empty and not value.strip():
        raise ValueError(f"{name} must be non-empty")
    if value != value.strip() and name != "history content":
        raise ValueError(f"{name} must not contain surrounding whitespace")
    return value


def _identifier(name: str, value: Any, *, maximum: int = 255) -> str:
    result = _text(name, value, non_empty=True)
    if len(result) > maximum:
        raise ValueError(f"{name} exceeds maximum length {maximum}")
    return result


def _sha256(name: str, value: Any) -> str:
    result = _identifier(name, value, maximum=64)
    if _SHA256.fullmatch(result) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return result


def _json_value(name: str, value: Any, active: set[int] | None = None) -> JsonValue:
    """Return a detached strict-JSON copy without accepting lossy coercions."""

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{name} must not contain non-finite numbers")
        return value
    if isinstance(value, str):
        validate_json_unicode(value)
        return value

    if active is None:
        active = set()
    marker = id(value)
    if marker in active:
        raise ValueError(f"{name} must not contain cycles")

    if isinstance(value, Mapping):
        active.add(marker)
        try:
            copied: dict[str, JsonValue] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError(f"{name} object keys must be strings")
                validate_json_unicode(key)
                copied[key] = _json_value(f"{name}.{key}", item, active)
            return copied
        finally:
            active.remove(marker)

    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        active.add(marker)
        try:
            return [
                _json_value(f"{name}[{index}]", item, active)
                for index, item in enumerate(value)
            ]
        finally:
            active.remove(marker)

    raise ValueError(f"{name} must contain strict JSON values")


def _json_object(name: str, value: Any) -> dict[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    copied = _json_value(name, value)
    if not isinstance(copied, dict):  # pragma: no cover - guarded above
        raise ValueError(f"{name} must be an object")
    return copied


def _reject_private_fields(name: str, value: JsonValue) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.casefold() in _FORBIDDEN_PUBLIC_KEYS:
                raise ValueError(f"{name} contains a private execution field")
            _reject_private_fields(name, item)
    elif isinstance(value, list):
        for item in value:
            _reject_private_fields(name, item)


@dataclass(frozen=True, slots=True)
class CompletedHistoryMessage:
    """One persisted message from a fully completed conversation turn."""

    role: Literal["user", "assistant"]
    content: str

    def __post_init__(self) -> None:
        if self.role not in {"user", "assistant"}:
            raise ValueError("history role must be user or assistant")
        _text("history content", self.content)


# These aliases make the boundary convenient for callers with either naming style.
CompletedMessage = CompletedHistoryMessage
PersistedHistoryMessage = CompletedHistoryMessage


def _history_message(value: Any, index: int) -> CompletedHistoryMessage:
    if isinstance(value, CompletedHistoryMessage):
        return value
    if isinstance(value, Mapping):
        if "role" not in value or "content" not in value:
            raise ValueError(f"history[{index}] is missing role or content")
        return CompletedHistoryMessage(role=value["role"], content=value["content"])
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        if len(value) != 2:
            raise ValueError(f"history[{index}] must contain role and content")
        return CompletedHistoryMessage(role=value[0], content=value[1])
    role = getattr(value, "role", None)
    content = getattr(value, "content", None)
    if role is not None and content is not None:
        return CompletedHistoryMessage(role=role, content=content)
    raise ValueError(f"history[{index}] is not a persisted message")


def _completed_history(value: Any) -> tuple[CompletedHistoryMessage, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ValueError("history must be a sequence")
    messages = tuple(_history_message(item, index) for index, item in enumerate(value))
    if len(messages) % 2:
        raise ValueError("history must contain complete user/assistant pairs")
    for index, message in enumerate(messages):
        expected = "user" if index % 2 == 0 else "assistant"
        if message.role != expected:
            raise ValueError("history must alternate user and assistant messages")
    return messages


@dataclass(frozen=True, slots=True)
class RunExecutionInput:
    """Immutable database-derived inputs for one assistant execution.

    ``history`` contains completed turns only. The current run's user message is
    represented by ``question`` and must not also be present in ``history``.
    Snapshot and boundary fields are copied from the run row so an execution
    cannot silently follow a later active-snapshot change.
    """

    run_id: str
    question: str
    history: tuple[CompletedHistoryMessage, ...]
    scope_id: str
    snapshot_id: str
    snapshot_revision: int
    activation_id: str
    profile_id: str
    boundary_fingerprint: str
    request_options: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _identifier("run_id", self.run_id, maximum=64))
        object.__setattr__(self, "question", _text("question", self.question, non_empty=True))
        object.__setattr__(self, "history", _completed_history(self.history))
        object.__setattr__(self, "scope_id", _identifier("scope_id", self.scope_id))
        object.__setattr__(
            self, "snapshot_id", _identifier("snapshot_id", self.snapshot_id)
        )
        if (
            isinstance(self.snapshot_revision, bool)
            or not isinstance(self.snapshot_revision, int)
            or self.snapshot_revision < 0
        ):
            raise ValueError("snapshot_revision must be a non-negative integer")
        object.__setattr__(
            self,
            "activation_id",
            _identifier("activation_id", self.activation_id, maximum=64),
        )
        object.__setattr__(self, "profile_id", _sha256("profile_id", self.profile_id))
        object.__setattr__(
            self,
            "boundary_fingerprint",
            _sha256("boundary_fingerprint", self.boundary_fingerprint),
        )
        object.__setattr__(
            self,
            "request_options",
            _json_object("request_options", self.request_options),
        )

    @property
    def options(self) -> Mapping[str, JsonValue]:
        """Compatibility alias for factories that call these values options."""

        return self.request_options

    @property
    def boundary_metadata(self) -> dict[str, JsonValue]:
        return {
            "scope_id": self.scope_id,
            "snapshot_id": self.snapshot_id,
            "snapshot_revision": self.snapshot_revision,
            "activation_id": self.activation_id,
            "profile_id": self.profile_id,
            "boundary_fingerprint": self.boundary_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class SafeRunResult:
    """Only verified public output and allowlisted evidence metadata."""

    answer_text: str
    answer_payload: Mapping[str, JsonValue]
    evidence_payload: Mapping[str, JsonValue]
    verification_payload: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        answer_text = _text("answer_text", self.answer_text, non_empty=True)
        answer = _json_object("answer_payload", self.answer_payload)
        evidence = _json_object("evidence_payload", self.evidence_payload)
        verification = _json_object(
            "verification_payload", self.verification_payload
        )
        for name, payload in (
            ("answer_payload", answer),
            ("evidence_payload", evidence),
            ("verification_payload", verification),
        ):
            _reject_private_fields(name, payload)
        if answer.get("answer_text") != answer_text:
            raise ValueError("answer_payload must contain the verified answer_text")
        if verification.get("passed") is not True:
            raise ValueError("verification_payload must describe a passed final result")
        object.__setattr__(self, "answer_text", answer_text)
        object.__setattr__(self, "answer_payload", answer)
        object.__setattr__(self, "evidence_payload", evidence)
        object.__setattr__(self, "verification_payload", verification)

    @property
    def final_text(self) -> str:
        """Compatibility alias used by early M4 service drafts."""

        return self.answer_text


class ExecutionFailure(RuntimeError):
    """A safe, typed execution failure suitable for persistence and APIs."""

    def __init__(
        self,
        *,
        code: str = "execution_failed",
        stage: str = "execution",
        retryable: bool = False,
    ) -> None:
        if not isinstance(code, str) or _SAFE_NAME.fullmatch(code) is None:
            raise ValueError("execution failure code is invalid")
        if not isinstance(stage, str) or _SAFE_NAME.fullmatch(stage) is None:
            raise ValueError("execution failure stage is invalid")
        if not isinstance(retryable, bool):
            raise ValueError("execution failure retryable must be a boolean")
        self.code = code
        self.error_code = code
        self.stage = stage
        self.retryable = retryable
        super().__init__(f"run execution failed at {stage} ({code})")

    def to_safe_dict(self) -> dict[str, str | bool]:
        return {
            "error_code": self.code,
            "stage": self.stage,
            "retryable": self.retryable,
        }


class ExecutionTimeout(ExecutionFailure):
    """A retryable timeout at a named execution stage."""

    def __init__(self, *, stage: str = "execution") -> None:
        super().__init__(code="execution_timeout", stage=stage, retryable=True)


@runtime_checkable
class SafeStageCallback(Protocol):
    def __call__(self, event_type: str, safe_payload: SafePayload) -> None: ...


@runtime_checkable
class RunExecutor(Protocol):
    def execute(
        self,
        input: RunExecutionInput,
        emit_event: SafeStageCallback,
    ) -> SafeRunResult: ...


AssistantFactory: TypeAlias = Callable[[RunExecutionInput], LegalChatAssistant]


def _ignore_event(event_type: str, safe_payload: SafePayload) -> None:
    del event_type, safe_payload


def _safe_stop_reason(retrieved: RetrievedTurn) -> str:
    if retrieved.evidence_check is not None:
        value = retrieved.evidence_check.stop_reason
    elif retrieved.terminal_kind is not None:
        value = retrieved.terminal_kind
    else:
        value = "retrieval_completed"
    if not isinstance(value, str) or _SAFE_NAME.fullmatch(value) is None:
        raise ExecutionFailure(code="unsafe_stop_reason", stage="retrieval")
    return value


def _retrieval_event_payload(retrieved: RetrievedTurn) -> dict[str, JsonValue]:
    checked_count = (
        retrieved.evidence_check.checked_result_count
        if retrieved.evidence_check is not None
        else 0
    )
    if isinstance(checked_count, bool) or not isinstance(checked_count, int):
        raise ExecutionFailure(code="invalid_retrieval_counts", stage="retrieval")
    return {
        "result_count": len(retrieved.results),
        "checked_result_count": checked_count,
        "rejected_count": len(retrieved.rejected_source_ids),
        "stop_reason": _safe_stop_reason(retrieved),
    }


def _emit(
    callback: SafeStageCallback,
    event_type: str,
    payload: Mapping[str, JsonValue],
) -> None:
    callback(event_type, _json_object(f"{event_type} payload", payload))


def _restore_completed_history(
    assistant: LegalChatAssistant,
    input: RunExecutionInput,
) -> None:
    state = assistant.export_session_state()
    if not isinstance(state, Mapping):
        raise ExecutionFailure(code="invalid_assistant_state", stage="history_restore")
    state_copy = _json_object("assistant session state", state)
    memory = state_copy.get("memory")
    if not isinstance(memory, dict) or not isinstance(memory.get("messages"), list):
        raise ExecutionFailure(code="invalid_assistant_state", stage="history_restore")
    if memory["messages"]:
        raise ExecutionFailure(code="assistant_not_fresh", stage="history_restore")
    restored_messages = [
        {"role": message.role, "content": message.content}
        for message in input.history
    ]
    while len(restored_messages) > MAX_RENDERED_MEMORY_MESSAGES:
        del restored_messages[:2]
    while restored_messages:
        rendered = "\n".join(
            f"{item['role']}: {item['content']}" for item in restored_messages
        )
        if estimate_tokens(rendered) <= assistant.memory.token_limit:
            break
        del restored_messages[:2]
    memory["messages"] = restored_messages
    try:
        assistant.restore_session_state(state_copy)
    except (TypeError, ValueError) as exc:
        del exc
        raise ExecutionFailure(
            code="invalid_completed_history", stage="history_restore"
        ) from None


def _assert_frozen_boundary(
    input: RunExecutionInput,
    retrieved: RetrievedTurn,
) -> RetrievalBoundary:
    boundary = retrieved.retrieval_boundary
    if not isinstance(boundary, RetrievalBoundary):
        raise ExecutionFailure(code="retrieval_boundary_missing", stage="retrieval")
    if (
        boundary.scope_id != input.scope_id
        or boundary.snapshot_id != input.snapshot_id
        or boundary.profile_id != input.profile_id
        or boundary.fingerprint != input.boundary_fingerprint
    ):
        raise ExecutionFailure(code="retrieval_boundary_mismatch", stage="retrieval")
    assert_results_match_boundary(
        list(retrieved.results),
        boundary,
        stage="service run retrieval output",
    )
    return boundary


def _source_payload(result: Any, boundary: RetrievalBoundary) -> dict[str, JsonValue]:
    provenance = getattr(result, "provenance", None)
    if provenance is None or provenance.boundary != boundary or not boundary.allows(provenance):
        raise ExecutionFailure(code="unsafe_evidence_provenance", stage="output")
    articles = [article.to_metadata() for article in provenance.articles]
    return _json_object(
        "evidence source",
        {
            "source_id": f"S{result.rank}",
            "rank": result.rank,
            "chunk_id": provenance.chunk_id,
            "chunk_content_hash": provenance.chunk_content_hash,
            "chunk_payload_hash": provenance.chunk_payload_hash,
            "snapshot_ordinal": provenance.snapshot_ordinal,
            "articles": articles,
        },
    )


def _evidence_payload(
    input: RunExecutionInput,
    retrieved: RetrievedTurn,
    boundary: RetrievalBoundary,
) -> dict[str, JsonValue]:
    evidence_check: dict[str, JsonValue] | None = None
    if retrieved.evidence_check is not None:
        evidence_check = {
            "sufficient": retrieved.evidence_check.sufficient,
            "checked_result_count": retrieved.evidence_check.checked_result_count,
            "stop_reason": _safe_stop_reason(retrieved),
        }
    return _json_object(
        "evidence_payload",
        {
            "schema_version": 1,
            **input.boundary_metadata,
            "result_count": len(retrieved.results),
            "rejected_count": len(retrieved.rejected_source_ids),
            "terminal_kind": retrieved.terminal_kind,
            "evidence_check": evidence_check,
            "sources": [
                _source_payload(result, boundary) for result in retrieved.results
            ],
        },
    )


def _verified_payloads(
    verified: VerifiedTurn,
) -> tuple[dict[str, JsonValue], dict[str, JsonValue], bool, bool]:
    fallback_used = (
        verified.pre_fallback_answer is not None
        or verified.pre_fallback_verification is not None
    )
    if verified.final_answer is None:
        if (
            verified.generated.kind != "retrieval_only"
            or verified.verification is not None
            or fallback_used
        ):
            raise ExecutionFailure(code="unverified_final_answer", stage="verification")
        answer_payload = {
            "answer_text": verified.answer_text,
            "answer_mode": "retrieval_only",
            "claims": [],
            "limitations": [],
            "clarification_question": None,
        }
        verification_payload = {
            "passed": True,
            "performed": False,
            "fallback_used": False,
        }
        return answer_payload, verification_payload, True, False

    if (
        verified.final_answer.answer_text != verified.answer_text
        or verified.verification is None
        or not verified.verification.passed
    ):
        raise ExecutionFailure(code="unverified_final_answer", stage="verification")
    answer_payload = _json_object(
        "verified final answer", verified.final_answer.to_dict()
    )
    verification_payload = _json_object(
        "verified final verification",
        {
            **verified.verification.to_dict(),
            "performed": True,
            "fallback_used": fallback_used,
        },
    )
    return answer_payload, verification_payload, True, fallback_used


def _raise_execution_error(error: Exception, *, stage: str) -> None:
    if isinstance(error, ExecutionFailure):
        raise error
    classification = classify_provider_error(error)
    if classification.error_code == "timeout":
        raise ExecutionTimeout(stage=stage) from None
    if isinstance(error, ProviderCallError):
        code = f"provider_{classification.error_code}"
        raise ExecutionFailure(
            code=code,
            stage=stage,
            retryable=classification.retryable,
        ) from None
    raise ExecutionFailure(code=f"{stage}_failed", stage=stage) from None


class LegalChatRunExecutor:
    """Execute the staged assistant without publishing an unverified draft.

    The factory must create a new assistant whose retriever is already bound to
    the input snapshot. Conversation durability belongs to the database, so the
    adapter deliberately does not call ``assistant.commit_turn``.
    """

    def __init__(
        self,
        assistant_factory: AssistantFactory,
        *,
        generate: bool = True,
    ) -> None:
        if not callable(assistant_factory):
            raise TypeError("assistant_factory must be callable")
        if not isinstance(generate, bool):
            raise TypeError("generate must be a boolean")
        self.assistant_factory = assistant_factory
        self.generate = generate
        self._seen_assistants: WeakSet[LegalChatAssistant] = WeakSet()
        self._seen_lock = Lock()

    def execute(
        self,
        input: RunExecutionInput,
        emit_event: SafeStageCallback = _ignore_event,
    ) -> SafeRunResult:
        if not isinstance(input, RunExecutionInput):
            raise ExecutionFailure(code="invalid_execution_input", stage="input")
        if not callable(emit_event):
            raise ExecutionFailure(code="invalid_event_callback", stage="input")

        stage = "assistant_factory"
        try:
            assistant = self.assistant_factory(input)
            if not isinstance(assistant, LegalChatAssistant):
                raise ExecutionFailure(
                    code="invalid_assistant_factory", stage="assistant_factory"
                )
            with self._seen_lock:
                if assistant in self._seen_assistants:
                    raise ExecutionFailure(
                        code="assistant_reused", stage="assistant_factory"
                    )
                self._seen_assistants.add(assistant)

            stage = "history_restore"
            _restore_completed_history(assistant, input)

            stage = "preparation"
            prepared = assistant.prepare_question(input.question)

            stage = "retrieval"
            retrieved = assistant.retrieve_turn(prepared)
            if not isinstance(retrieved, RetrievedTurn):
                raise ExecutionFailure(
                    code="invalid_retrieval_output", stage="retrieval"
                )
            boundary = _assert_frozen_boundary(input, retrieved)

            stage = "retrieval_event"
            _emit(
                emit_event,
                "retrieval.completed",
                _retrieval_event_payload(retrieved),
            )

            stage = "generation_event"
            _emit(emit_event, "generation.started", {})

            stage = "generation"
            generated = assistant.generate_turn(retrieved, generate=self.generate)
            if not isinstance(generated, GeneratedTurn):
                raise ExecutionFailure(
                    code="invalid_generation_output", stage="generation"
                )

            stage = "verification"
            verified = assistant.verify_turn(generated)
            if not isinstance(verified, VerifiedTurn) or verified.generated != generated:
                raise ExecutionFailure(
                    code="invalid_verification_output", stage="verification"
                )
            (
                answer_payload,
                verification_payload,
                passed,
                fallback_used,
            ) = _verified_payloads(verified)

            stage = "verification_event"
            _emit(
                emit_event,
                "verification.completed",
                {"passed": passed, "fallback_used": fallback_used},
            )

            stage = "output"
            evidence_payload = _evidence_payload(input, retrieved, boundary)
            return SafeRunResult(
                answer_text=verified.answer_text,
                answer_payload=answer_payload,
                evidence_payload=evidence_payload,
                verification_payload=verification_payload,
            )
        except Exception as error:
            _raise_execution_error(error, stage=stage)
            raise AssertionError("unreachable")  # pragma: no cover


class DeterministicRunExecutor:
    """Offline-only executor for service development without model credentials."""

    DEFAULT_ANSWER = (
        "Offline deterministic mode did not retrieve or generate a legal answer."
    )

    def __init__(self, answer_text: str = DEFAULT_ANSWER) -> None:
        self.answer_text = _text(
            "deterministic answer_text", answer_text, non_empty=True
        )

    def execute(
        self,
        input: RunExecutionInput,
        emit_event: SafeStageCallback = _ignore_event,
    ) -> SafeRunResult:
        if not isinstance(input, RunExecutionInput):
            raise ExecutionFailure(code="invalid_execution_input", stage="input")
        if not callable(emit_event):
            raise ExecutionFailure(code="invalid_event_callback", stage="input")
        stage = "retrieval_event"
        try:
            _emit(
                emit_event,
                "retrieval.completed",
                {
                    "result_count": 0,
                    "checked_result_count": 0,
                    "rejected_count": 0,
                    "stop_reason": "offline_deterministic",
                },
            )
            stage = "generation_event"
            _emit(emit_event, "generation.started", {})
            stage = "verification_event"
            _emit(
                emit_event,
                "verification.completed",
                {"passed": True, "fallback_used": False},
            )
            stage = "output"
            return SafeRunResult(
                answer_text=self.answer_text,
                answer_payload={
                    "answer_text": self.answer_text,
                    "answer_mode": "insufficient_evidence",
                    "claims": [],
                    "limitations": ["Offline deterministic executor was used."],
                    "clarification_question": None,
                },
                evidence_payload={
                    "schema_version": 1,
                    **input.boundary_metadata,
                    "result_count": 0,
                    "rejected_count": 0,
                    "terminal_kind": "offline_deterministic",
                    "evidence_check": {
                        "sufficient": False,
                        "checked_result_count": 0,
                        "stop_reason": "offline_deterministic",
                    },
                    "sources": [],
                },
                verification_payload={
                    "passed": True,
                    "performed": False,
                    "fallback_used": False,
                    "mode": "offline_deterministic",
                },
            )
        except Exception as error:
            _raise_execution_error(error, stage=stage)
            raise AssertionError("unreachable")  # pragma: no cover


__all__ = [
    "AssistantFactory",
    "CompletedHistoryMessage",
    "CompletedMessage",
    "DeterministicRunExecutor",
    "ExecutionFailure",
    "ExecutionTimeout",
    "JsonValue",
    "LegalChatRunExecutor",
    "PersistedHistoryMessage",
    "RunExecutionInput",
    "RunExecutor",
    "SafePayload",
    "SafeRunResult",
    "SafeStageCallback",
]
