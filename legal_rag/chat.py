from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from threading import RLock
from typing import Any

from .adaptive import AdaptiveRetrievalResult, retrieve_adaptive
from .evidence import build_low_confidence_answer, check_evidence_sufficiency
from .json_utils import validate_json_unicode
from .llm import build_completion_client
from .models import (
    AnswerClaim,
    EvidenceCheck,
    SearchResult,
    StructuredAnswer,
    VerificationContext,
    VerificationResult,
)
from .query import (
    QueryAnalysis,
    analyze_query,
    extract_article_numbers,
    extract_law_names,
)
from .retrieval import Retriever, format_sources
from .verifier import (
    SAFE_CLARIFICATION_QUESTION,
    SAFE_CLARIFICATION_RESPONSE,
    STANDARD_DISCLAIMER,
    build_verifier_fallback_answer,
    filter_results_to_context,
    parse_structured_answer,
    verify_answer,
)


LEGAL_DISCLAIMER = STANDARD_DISCLAIMER
CONVERSATION_STATE_SCHEMA_VERSION = 1
ASSISTANT_SESSION_STATE_SCHEMA_VERSION = 1
MAX_RENDERED_MEMORY_MESSAGES = 8
CONVERSATION_ROLES = frozenset({"user", "assistant"})
STRUCTURED_ANSWER_PARSER_VERSION = "m1-structured-answer-v1"
PRE_RETRIEVAL_REFUSAL_FLAGS = {
    "case_strategy",
    "illegal_help",
    "medical_financial_advice",
    "non_legal",
}


@dataclass(frozen=True)
class ConversationMemorySnapshot:
    revision: int
    token_limit: int
    messages: tuple[tuple[str, str], ...]
    rendered: str
    state: Mapping[str, Any]


@dataclass
class ConversationMemory:
    token_limit: int = 2000
    messages: list[tuple[str, str]] = field(default_factory=list)
    _revision: int = field(default=0, init=False, repr=False, compare=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.token_limit, bool)
            or not isinstance(self.token_limit, int)
            or self.token_limit < 1
        ):
            raise ValueError("conversation token_limit must be a positive integer")
        initial_messages = list(self.messages)
        self.messages = []
        for role, content in initial_messages:
            self.add(role, content)

    def add(self, role: str, content: str) -> None:
        with self._lock:
            if role not in CONVERSATION_ROLES:
                raise ValueError(f"unsupported conversation role: {role!r}")
            if not isinstance(content, str):
                raise ValueError("conversation content must be a string")
            validate_json_unicode(content)
            expected_role = "user" if len(self.messages) % 2 == 0 else "assistant"
            if role != expected_role:
                raise ValueError(
                    "conversation messages must alternate user/assistant; "
                    f"expected {expected_role}"
                )
            self.messages.append((role, content))
            if role == "assistant":
                self._trim()
            self._revision += 1

    def add_turn(self, user_content: str, assistant_content: str) -> None:
        """Commit one complete turn after both messages pass validation."""

        with self._lock:
            self._add_turn_locked(user_content, assistant_content)

    def compare_and_add_turn(
        self,
        *,
        expected_revision: int,
        expected_messages: tuple[tuple[str, str], ...],
        expected_token_limit: int,
        user_content: str,
        assistant_content: str,
    ) -> None:
        """Atomically reject a stale snapshot or append one complete turn."""

        with self._lock:
            if (
                self._revision != expected_revision
                or tuple(self.messages) != expected_messages
                or self.token_limit != expected_token_limit
            ):
                raise RuntimeError("conversation state changed before turn commit")
            self._add_turn_locked(user_content, assistant_content)

    def _add_turn_locked(self, user_content: str, assistant_content: str) -> None:
        if len(self.messages) % 2:
            raise ValueError(
                "cannot commit a turn while conversation state is incomplete"
            )
        for name, content in (
            ("user content", user_content),
            ("assistant content", assistant_content),
        ):
            if not isinstance(content, str):
                raise ValueError(f"conversation {name} must be a string")
            validate_json_unicode(content)
        self.messages = [
            *self.messages,
            ("user", user_content),
            ("assistant", assistant_content),
        ]
        self._trim()
        self._revision += 1

    def render(self) -> str:
        with self._lock:
            return self._render_messages(self.messages)

    def last_user_question(self) -> str:
        with self._lock:
            for role, content in reversed(self.messages):
                if role == "user":
                    return content
        return ""

    def last_assistant_answer(self) -> str:
        with self._lock:
            for role, content in reversed(self.messages):
                if role == "assistant":
                    return content
        return ""

    def clear(self) -> None:
        with self._lock:
            self.messages.clear()
            self._revision += 1

    def export_state(self) -> dict[str, Any]:
        """Return a strict JSON checkpoint without sharing mutable state."""

        with self._lock:
            state = self._state_locked()
            self._validated_state_messages(state)
            return state

    def snapshot(self) -> ConversationMemorySnapshot:
        """Capture every value used by a staged turn under one memory lock."""

        with self._lock:
            messages = tuple(self.messages)
            return ConversationMemorySnapshot(
                revision=self._revision,
                token_limit=self.token_limit,
                messages=messages,
                rendered=self._render_messages(list(messages)),
                state=self._state_locked(),
            )

    def _state_locked(self) -> dict[str, Any]:
        return {
            "schema_version": CONVERSATION_STATE_SCHEMA_VERSION,
            "token_limit": self.token_limit,
            "messages": [
                {"role": role, "content": content} for role, content in self.messages
            ],
        }

    def restore_state(self, state: Mapping[str, Any]) -> None:
        """Replace memory only after a checkpoint passes every validation."""

        restored = self._validated_state_messages(state)
        with self._lock:
            self.messages = restored
            self._revision += 1

    def _validated_state_messages(
        self,
        state: Mapping[str, Any],
    ) -> list[tuple[str, str]]:
        if not isinstance(state, Mapping) or set(state) != {
            "schema_version",
            "token_limit",
            "messages",
        }:
            raise ValueError("conversation state fields are invalid")
        validate_json_unicode(dict(state))
        schema_version = state["schema_version"]
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != CONVERSATION_STATE_SCHEMA_VERSION
        ):
            raise ValueError("conversation state schema is unsupported")
        token_limit = state["token_limit"]
        if (
            isinstance(token_limit, bool)
            or not isinstance(token_limit, int)
            or token_limit != self.token_limit
        ):
            raise ValueError("conversation state token_limit is incompatible")
        raw_messages = state["messages"]
        if not isinstance(raw_messages, list):
            raise ValueError("conversation state messages must be a list")
        if len(raw_messages) > MAX_RENDERED_MEMORY_MESSAGES:
            raise ValueError(
                f"conversation state may contain at most {MAX_RENDERED_MEMORY_MESSAGES} messages"
            )
        if len(raw_messages) % 2:
            raise ValueError(
                "conversation state must contain complete user/assistant pairs"
            )
        restored: list[tuple[str, str]] = []
        for index, raw_message in enumerate(raw_messages):
            if not isinstance(raw_message, Mapping) or set(raw_message) != {
                "role",
                "content",
            }:
                raise ValueError("conversation state message fields are invalid")
            role = raw_message["role"]
            content = raw_message["content"]
            if not isinstance(role, str) or role not in CONVERSATION_ROLES:
                raise ValueError("conversation state message role is invalid")
            expected_role = "user" if index % 2 == 0 else "assistant"
            if role != expected_role:
                raise ValueError(
                    "conversation state messages must alternate user/assistant"
                )
            if not isinstance(content, str):
                raise ValueError("conversation state message content must be a string")
            validate_json_unicode(content)
            restored.append((role, content))
        if estimate_tokens(self._render_messages(restored)) > self.token_limit:
            raise ValueError("conversation state exceeds the configured token budget")
        return restored

    @staticmethod
    def _render_messages(messages: list[tuple[str, str]]) -> str:
        return "\n".join(
            f"{role}: {content}"
            for role, content in messages[-MAX_RENDERED_MEMORY_MESSAGES:]
        )

    def _trim(self) -> None:
        while len(self.messages) > MAX_RENDERED_MEMORY_MESSAGES:
            del self.messages[:2]
        while estimate_tokens(self.render()) > self.token_limit and self.messages:
            del self.messages[:2]


@dataclass(frozen=True)
class PreparedQuestion:
    original_question: str
    standalone_question: str
    analysis: QueryAnalysis
    memory_text: str
    session_state_before: Mapping[str, Any]
    memory_messages_before: tuple[tuple[str, str], ...]
    memory_token_limit: int
    memory_revision: int
    session_revision: int


@dataclass(frozen=True)
class RetrievedTurn:
    prepared: PreparedQuestion
    adaptive_result: AdaptiveRetrievalResult | None
    results: tuple[SearchResult, ...]
    evidence_check: EvidenceCheck | None
    rejected_source_ids: tuple[str, ...] = ()
    source_id_map: Mapping[str, str] = field(default_factory=dict)
    terminal_kind: str | None = None
    terminal_answer: StructuredAnswer | None = None
    terminal_expected_answer_mode: str | None = None


@dataclass(frozen=True)
class GeneratedTurn:
    retrieved: RetrievedTurn
    kind: str
    answer_text: str
    answer: StructuredAnswer | None = None
    generation_error: str | None = None
    expected_answer_mode: str | None = None
    raw_response: str | StructuredAnswer | None = None
    parser_version: str | None = None


@dataclass(frozen=True)
class VerifiedTurn:
    generated: GeneratedTurn
    answer_text: str
    final_answer: StructuredAnswer | None
    verification: VerificationResult | None
    pre_fallback_answer: StructuredAnswer | None = None
    pre_fallback_verification: VerificationResult | None = None


class LegalChatAssistant:
    def __init__(
        self,
        retriever: Retriever,
        *,
        model: str,
        top_k: int = 5,
        memory_token_limit: int = 2000,
        ollama_base_url: str = "http://localhost:11434",
        request_timeout: int = 180,
        adaptive_enabled: bool = False,
        adaptive_use_llm: bool = False,
        adaptive_max_queries: int = 3,
        adaptive_per_plan_top_k: int | None = None,
        normalizer_retries: int = 0,
        condense_with_llm: bool = False,
        verification_context: VerificationContext | None = None,
    ) -> None:
        self.retriever = retriever
        self.model = model
        self.top_k = top_k
        self.adaptive_enabled = adaptive_enabled
        self.adaptive_use_llm = adaptive_use_llm
        self.adaptive_max_queries = adaptive_max_queries
        self.adaptive_per_plan_top_k = adaptive_per_plan_top_k
        self.normalizer_retries = normalizer_retries
        self.condense_with_llm = condense_with_llm
        self.verification_context = verification_context
        self.last_adaptive_result: AdaptiveRetrievalResult | None = None
        self.last_evidence_check: EvidenceCheck | None = None
        self.last_verification: VerificationResult | None = None
        self.last_pre_fallback_verification: VerificationResult | None = None
        self.last_pre_fallback_answer: StructuredAnswer | None = None
        self.last_structured_answer: StructuredAnswer | None = None
        self.last_rejected_original_source_ids: list[str] = []
        self.last_evidence_source_id_map: dict[str, str] = {}
        self.last_generation_error: str | None = None
        self.memory = ConversationMemory(token_limit=memory_token_limit)
        self._commit_lock = RLock()
        self._session_revision = 0
        self.llm = build_completion_client(
            model,
            ollama_base_url=ollama_base_url,
            request_timeout=request_timeout,
        )

    def reset_memory(self) -> None:
        """Clear conversation state so independent questions do not leak context.

        Used by evaluation, where each case must be answered in isolation.
        """
        with self._commit_lock:
            self.memory.clear()
            self._clear_observations()
            self._session_revision += 1

    def _clear_observations(self) -> None:
        self.last_adaptive_result = None
        self.last_evidence_check = None
        self.last_verification = None
        self.last_pre_fallback_verification = None
        self.last_pre_fallback_answer = None
        self.last_structured_answer = None
        self.last_rejected_original_source_ids = []
        self.last_evidence_source_id_map = {}
        self.last_generation_error = None

    def export_session_state(self) -> dict[str, Any]:
        """Export only durable conversation state, never per-attempt telemetry."""

        with self._commit_lock:
            return {
                "schema_version": ASSISTANT_SESSION_STATE_SCHEMA_VERSION,
                "memory": self.memory.export_state(),
            }

    def restore_session_state(self, state: Mapping[str, Any]) -> None:
        """Restore a validated conversation checkpoint without partial mutation."""

        if not isinstance(state, Mapping) or set(state) != {
            "schema_version",
            "memory",
        }:
            raise ValueError("assistant session state fields are invalid")
        validate_json_unicode(dict(state))
        schema_version = state["schema_version"]
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != ASSISTANT_SESSION_STATE_SCHEMA_VERSION
        ):
            raise ValueError("assistant session state schema is unsupported")
        restored_memory = ConversationMemory(token_limit=self.memory.token_limit)
        restored_memory.restore_state(state["memory"])
        with self._commit_lock:
            self.memory = restored_memory
            self._clear_observations()
            self._session_revision += 1

    def answer(
        self, question: str, *, generate: bool = True
    ) -> tuple[str, list[SearchResult]]:
        prepared = self.prepare_question(question)
        with self._commit_lock:
            current_memory = self.memory.snapshot()
            if (
                self._session_revision == prepared.session_revision
                and current_memory.revision == prepared.memory_revision
                and current_memory.messages == prepared.memory_messages_before
                and current_memory.token_limit == prepared.memory_token_limit
            ):
                self._clear_observations()
        retrieved = self.retrieve_turn(prepared)
        generated = self.generate_turn(retrieved, generate=generate)
        verified = self.verify_turn(generated)
        self.commit_turn(verified)
        return verified.answer_text, list(retrieved.results)

    def prepare_question(self, question: str) -> PreparedQuestion:
        with self._commit_lock:
            memory_snapshot = self.memory.snapshot()
            session_revision = self._session_revision
        standalone_question = self.condense_question(
            question,
            memory_messages=memory_snapshot.messages,
        )
        return PreparedQuestion(
            original_question=question,
            standalone_question=standalone_question,
            analysis=analyze_query(standalone_question),
            memory_text=memory_snapshot.rendered,
            session_state_before={
                "schema_version": ASSISTANT_SESSION_STATE_SCHEMA_VERSION,
                "memory": memory_snapshot.state,
            },
            memory_messages_before=memory_snapshot.messages,
            memory_token_limit=memory_snapshot.token_limit,
            memory_revision=memory_snapshot.revision,
            session_revision=session_revision,
        )

    def bind_prepared_question(
        self,
        *,
        original_question: str,
        standalone_question: str,
        analysis: QueryAnalysis,
        memory_text: str,
        session_state_before: Mapping[str, Any],
    ) -> PreparedQuestion:
        """Bind cached semantic preparation to the current session fence.

        Process-local memory/session revisions are never replayable data.  A
        cached preparation may only be rebound when its durable checkpoint and
        rendered memory exactly match the assistant's current state.
        """

        for name, value in (
            ("original_question", original_question),
            ("standalone_question", standalone_question),
            ("memory_text", memory_text),
        ):
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a string")
            validate_json_unicode(value)
        if not isinstance(analysis, QueryAnalysis):
            raise ValueError("analysis must be a QueryAnalysis")
        if analysis.original_query != standalone_question:
            raise ValueError("analysis is not bound to the standalone question")
        if analysis != analyze_query(standalone_question):
            raise ValueError("analysis does not match the standalone question")
        if not isinstance(session_state_before, Mapping):
            raise ValueError("session_state_before must be an object")
        checkpoint = deepcopy(dict(session_state_before))
        validate_json_unicode(checkpoint)
        if set(checkpoint) != {"schema_version", "memory"}:
            raise ValueError("session_state_before fields are invalid")
        schema_version = checkpoint["schema_version"]
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != ASSISTANT_SESSION_STATE_SCHEMA_VERSION
        ):
            raise ValueError("session_state_before schema is unsupported")
        memory_state = checkpoint["memory"]
        if not isinstance(memory_state, Mapping):
            raise ValueError("session_state_before.memory must be an object")
        self.memory._validated_state_messages(memory_state)

        with self._commit_lock:
            memory_snapshot = self.memory.snapshot()
            current_state = {
                "schema_version": ASSISTANT_SESSION_STATE_SCHEMA_VERSION,
                "memory": memory_snapshot.state,
            }
            if checkpoint != current_state or memory_text != memory_snapshot.rendered:
                raise RuntimeError(
                    "cached preparation does not match current conversation state"
                )
            return PreparedQuestion(
                original_question=original_question,
                standalone_question=standalone_question,
                analysis=deepcopy(analysis),
                memory_text=memory_text,
                session_state_before=deepcopy(current_state),
                memory_messages_before=memory_snapshot.messages,
                memory_token_limit=memory_snapshot.token_limit,
                memory_revision=memory_snapshot.revision,
                session_revision=self._session_revision,
            )

    def retrieve_turn(self, prepared: PreparedQuestion) -> RetrievedTurn:
        if should_refuse_before_retrieval(prepared.analysis.risk_flags):
            answer = programmatic_answer(
                build_risk_refusal_answer(prepared.analysis.risk_flags),
                answer_mode="out_of_scope",
                limitations=["请求超出法律文本学习助手允许的帮助范围。"],
            )
            return RetrievedTurn(
                prepared=prepared,
                adaptive_result=None,
                results=(),
                evidence_check=None,
                terminal_kind="pre_retrieval_refusal",
                terminal_answer=answer,
                terminal_expected_answer_mode="out_of_scope",
            )

        adaptive_result = retrieve_adaptive(
            prepared.standalone_question,
            self.retriever,
            top_k=self.top_k,
            enabled=self.adaptive_enabled,
            use_llm=self.adaptive_use_llm,
            llm_client=self.llm if self.adaptive_use_llm else None,
            max_queries=self.adaptive_max_queries,
            per_plan_top_k=self.adaptive_per_plan_top_k,
            normalizer_retries=self.normalizer_retries,
        )
        results, rejected_source_ids, source_id_map = filter_results_to_context(
            adaptive_result.results,
            self.verification_context,
        )
        if rejected_source_ids:
            evidence_check = check_evidence_sufficiency(
                prepared.standalone_question,
                results,
                analysis=adaptive_result.analysis,
                normalized_query=adaptive_result.normalized_query,
                plans=adaptive_result.plans,
            )
            evidence_check = replace(
                evidence_check, stop_reason="evidence_scope_filtered"
            )
            adaptive_result = replace(
                adaptive_result,
                results=results,
                evidence_check=evidence_check,
                merge_trace={
                    **adaptive_result.merge_trace,
                    "evidence_filter": {
                        "rejected_original_source_ids": rejected_source_ids,
                        "source_id_map": source_id_map,
                        "returned_count": len(results),
                    },
                },
            )

        evidence_check = adaptive_result.evidence_check
        terminal_answer = None
        terminal_kind = None
        terminal_expected_answer_mode = None
        if not results or not evidence_check.sufficient:
            terminal_answer = build_limited_structured_answer(evidence_check)
            terminal_kind = "evidence_limited"
            terminal_expected_answer_mode = expected_limited_answer_mode(evidence_check)
        return RetrievedTurn(
            prepared=prepared,
            adaptive_result=adaptive_result,
            results=tuple(results),
            evidence_check=evidence_check,
            rejected_source_ids=tuple(rejected_source_ids),
            source_id_map=dict(source_id_map),
            terminal_kind=terminal_kind,
            terminal_answer=terminal_answer,
            terminal_expected_answer_mode=terminal_expected_answer_mode,
        )

    def generate_turn(
        self,
        retrieved: RetrievedTurn,
        *,
        generate: bool,
    ) -> GeneratedTurn:
        if retrieved.terminal_answer is not None:
            if (
                retrieved.terminal_kind is None
                or retrieved.terminal_expected_answer_mode is None
            ):
                raise RuntimeError("terminal retrieval result is missing its contract")
            return GeneratedTurn(
                retrieved=retrieved,
                kind=retrieved.terminal_kind,
                answer_text=retrieved.terminal_answer.answer_text,
                answer=retrieved.terminal_answer,
                expected_answer_mode=retrieved.terminal_expected_answer_mode,
            )
        results = list(retrieved.results)
        if not generate:
            answer_text = append_disclaimer(render_retrieval_only_answer(results))
            return GeneratedTurn(
                retrieved=retrieved,
                kind="retrieval_only",
                answer_text=answer_text,
            )

        prompt = build_qa_prompt(
            question=retrieved.prepared.standalone_question,
            original_question=retrieved.prepared.original_question,
            memory=retrieved.prepared.memory_text,
            results=results,
        )
        try:
            raw_answer = self.llm.complete(prompt)
        except Exception:
            answer = programmatic_answer(
                "当前无法连接生成模型，因此无法给出可靠结论。",
                answer_mode="insufficient_evidence",
                limitations=["生成模型当前不可用。"],
            )
            return GeneratedTurn(
                retrieved=retrieved,
                kind="generation_error",
                answer_text=answer.answer_text,
                answer=answer,
                generation_error="generation_error",
                expected_answer_mode="insufficient_evidence",
            )

        if isinstance(raw_answer, StructuredAnswer):
            raw_response: str | StructuredAnswer | None = deepcopy(raw_answer)
            parser_input: str | StructuredAnswer | None = deepcopy(raw_answer)
        else:
            raw_response = raw_answer if isinstance(raw_answer, str) else None
            parser_input = raw_response
        answer = parse_structured_answer(parser_input)  # type: ignore[arg-type]
        answer = replace(answer, answer_text=append_disclaimer(answer.answer_text))
        return GeneratedTurn(
            retrieved=retrieved,
            kind="model",
            answer_text=answer.answer_text,
            answer=answer,
            expected_answer_mode="evidence_answer",
            raw_response=raw_response,
            parser_version=STRUCTURED_ANSWER_PARSER_VERSION,
        )

    def verify_turn(self, generated: GeneratedTurn) -> VerifiedTurn:
        retrieved = generated.retrieved
        results = list(retrieved.results)
        if generated.kind == "retrieval_only":
            expected_text = append_disclaimer(render_retrieval_only_answer(results))
            if (
                retrieved.terminal_kind is not None
                or retrieved.terminal_answer is not None
                or retrieved.terminal_expected_answer_mode is not None
                or generated.answer is not None
                or generated.generation_error is not None
                or generated.expected_answer_mode is not None
                or generated.raw_response is not None
                or generated.parser_version is not None
                or generated.answer_text != expected_text
            ):
                raise RuntimeError("retrieval-only turn contract is invalid")
            return VerifiedTurn(
                generated=generated,
                answer_text=expected_text,
                final_answer=None,
                verification=None,
            )
        if generated.answer is None or generated.expected_answer_mode is None:
            raise RuntimeError("generated turn is missing its answer contract")
        if generated.answer_text != generated.answer.answer_text:
            raise RuntimeError("generated answer text does not match its envelope")

        terminal_kinds = {"pre_retrieval_refusal", "evidence_limited"}
        if generated.kind in terminal_kinds:
            if (
                retrieved.terminal_kind != generated.kind
                or retrieved.terminal_answer != generated.answer
                or retrieved.terminal_expected_answer_mode
                != generated.expected_answer_mode
                or generated.generation_error is not None
                or generated.raw_response is not None
                or generated.parser_version is not None
            ):
                raise RuntimeError("terminal generated turn contract is invalid")
        elif (
            retrieved.terminal_kind is not None
            or retrieved.terminal_answer is not None
            or retrieved.terminal_expected_answer_mode is not None
        ):
            raise RuntimeError("non-terminal turn contains a terminal contract")

        if generated.kind == "pre_retrieval_refusal":
            if (
                generated.expected_answer_mode != "out_of_scope"
                or retrieved.adaptive_result is not None
                or retrieved.results
                or retrieved.evidence_check is not None
            ):
                raise RuntimeError("pre-retrieval refusal contract is invalid")
            answer, verification = self._verify_programmatic_answer(
                generated.answer,
                [],
                expected_answer_mode=generated.expected_answer_mode,
                risk_flags=retrieved.prepared.analysis.risk_flags,
                disclaimer=LEGAL_DISCLAIMER,
                context=self.verification_context,
            )
        elif generated.kind == "evidence_limited":
            if (
                retrieved.adaptive_result is None
                or retrieved.evidence_check is None
                or retrieved.evidence_check.sufficient
            ):
                raise RuntimeError("limited-evidence turn contract is invalid")
            answer, verification = self._verify_programmatic_answer(
                generated.answer,
                results,
                expected_answer_mode=generated.expected_answer_mode,
                evidence_check=retrieved.evidence_check,
                risk_flags=(
                    retrieved.adaptive_result.analysis.risk_flags
                    if retrieved.adaptive_result is not None
                    else retrieved.prepared.analysis.risk_flags
                ),
                disclaimer=LEGAL_DISCLAIMER,
                context=self.verification_context,
            )
        elif generated.kind == "generation_error":
            expected_error_answer = programmatic_answer(
                "当前无法连接生成模型，因此无法给出可靠结论。",
                answer_mode="insufficient_evidence",
                limitations=["生成模型当前不可用。"],
            )
            if (
                retrieved.adaptive_result is None
                or retrieved.evidence_check is None
                or generated.answer != expected_error_answer
                or generated.generation_error != "generation_error"
                or generated.expected_answer_mode != "insufficient_evidence"
                or generated.raw_response is not None
                or generated.parser_version is not None
            ):
                raise RuntimeError("generation-error turn contract is invalid")
            answer, verification = self._verify_programmatic_answer(
                generated.answer,
                results,
                expected_answer_mode=generated.expected_answer_mode,
                disclaimer=LEGAL_DISCLAIMER,
                context=self.verification_context,
            )
        elif generated.kind == "model":
            if retrieved.adaptive_result is None or retrieved.evidence_check is None:
                raise RuntimeError("model generation requires retrieved evidence")
            if (
                generated.generation_error is not None
                or generated.expected_answer_mode != "evidence_answer"
                or generated.parser_version != STRUCTURED_ANSWER_PARSER_VERSION
            ):
                raise RuntimeError("model generated turn contract is invalid")
            reparsed_answer = parse_structured_answer(generated.raw_response)
            reparsed_answer = replace(
                reparsed_answer,
                answer_text=append_disclaimer(reparsed_answer.answer_text),
            )
            if reparsed_answer != generated.answer:
                raise RuntimeError("model answer does not match its raw response")
            answer = generated.answer
            verification = verify_answer(
                answer,
                results,
                evidence_check=retrieved.evidence_check,
                risk_flags=retrieved.adaptive_result.analysis.risk_flags,
                expected_answer_mode=generated.expected_answer_mode,
                disclaimer=LEGAL_DISCLAIMER,
                context=self.verification_context,
            )
            if not verification.passed:
                pre_fallback_answer = answer
                pre_fallback_verification = verification
                low_confidence = build_low_confidence_answer(retrieved.evidence_check)
                fallback_text = build_verifier_fallback_answer(
                    answer.answer_text,
                    verification,
                    low_confidence_answer=low_confidence,
                )
                answer = programmatic_answer(
                    fallback_text,
                    answer_mode="insufficient_evidence",
                    limitations=["原生成回答未通过结构或行为检查。"],
                )
                answer, verification = self._verify_programmatic_answer(
                    answer,
                    results,
                    expected_answer_mode="insufficient_evidence",
                    disclaimer=LEGAL_DISCLAIMER,
                    context=self.verification_context,
                )
                return VerifiedTurn(
                    generated=generated,
                    answer_text=answer.answer_text,
                    final_answer=answer,
                    verification=verification,
                    pre_fallback_answer=pre_fallback_answer,
                    pre_fallback_verification=pre_fallback_verification,
                )
        else:
            raise RuntimeError(f"unsupported generated turn kind: {generated.kind!r}")

        return VerifiedTurn(
            generated=generated,
            answer_text=answer.answer_text,
            final_answer=answer,
            verification=verification,
        )

    def commit_turn(self, verified: VerifiedTurn) -> None:
        if not isinstance(verified, VerifiedTurn):
            raise TypeError("commit_turn requires a VerifiedTurn")

        # Materialize every caller-controlled value before changing durable or
        # observable assistant state.  This keeps commit atomic even when a
        # stage object contains a malformed Mapping or mutable nested value.
        candidate = deepcopy(verified)
        generated = candidate.generated
        retrieved = generated.retrieved

        published_adaptive_result = deepcopy(retrieved.adaptive_result)
        published_evidence_check = deepcopy(retrieved.evidence_check)
        published_rejected_source_ids = list(deepcopy(retrieved.rejected_source_ids))
        published_source_id_map = dict(deepcopy(retrieved.source_id_map))
        published_generation_error = deepcopy(generated.generation_error)

        canonical = self.verify_turn(generated)
        candidate_outputs = (
            candidate.answer_text,
            candidate.final_answer,
            candidate.verification,
            candidate.pre_fallback_answer,
            candidate.pre_fallback_verification,
        )
        canonical_outputs = (
            canonical.answer_text,
            canonical.final_answer,
            canonical.verification,
            canonical.pre_fallback_answer,
            canonical.pre_fallback_verification,
        )
        if candidate_outputs != canonical_outputs:
            raise RuntimeError(
                "verified turn does not match deterministic verification"
            )
        if canonical.verification is not None and not canonical.verification.passed:
            raise RuntimeError("verified turn did not pass required checks")

        published_verification = deepcopy(canonical.verification)
        published_pre_fallback_verification = deepcopy(
            canonical.pre_fallback_verification
        )
        published_pre_fallback_answer = deepcopy(canonical.pre_fallback_answer)
        published_structured_answer = deepcopy(canonical.final_answer)
        published_answer_text = canonical.answer_text

        # Only the short compare-and-publish section is serialized.  All
        # expensive or caller-controlled work above may fail without holding
        # the lock or changing state; inside the lock, one stale snapshot can
        # win at most once.
        with self._commit_lock:
            if self._session_revision != retrieved.prepared.session_revision:
                raise RuntimeError("conversation state changed before turn commit")
            self.memory.compare_and_add_turn(
                expected_revision=retrieved.prepared.memory_revision,
                expected_messages=retrieved.prepared.memory_messages_before,
                expected_token_limit=retrieved.prepared.memory_token_limit,
                user_content=retrieved.prepared.original_question,
                assistant_content=published_answer_text,
            )
            self.last_adaptive_result = published_adaptive_result
            self.last_evidence_check = published_evidence_check
            self.last_verification = published_verification
            self.last_pre_fallback_verification = published_pre_fallback_verification
            self.last_pre_fallback_answer = published_pre_fallback_answer
            self.last_structured_answer = published_structured_answer
            self.last_rejected_original_source_ids = published_rejected_source_ids
            self.last_evidence_source_id_map = published_source_id_map
            self.last_generation_error = published_generation_error
            self._session_revision += 1

    def _verify_programmatic_answer(
        self,
        answer: StructuredAnswer,
        results: list[SearchResult],
        *,
        expected_answer_mode: str,
        evidence_check: EvidenceCheck | None = None,
        risk_flags: list[str] | None = None,
        disclaimer: str = "",
        context: VerificationContext | None = None,
    ) -> tuple[StructuredAnswer, VerificationResult]:
        verification = verify_answer(
            answer,
            results,
            expected_answer_mode=expected_answer_mode,
            evidence_check=evidence_check,
            risk_flags=risk_flags,
            disclaimer=disclaimer,
            context=context,
        )
        if verification.passed:
            return answer, verification

        safe_answer = safe_terminal_answer(expected_answer_mode)
        safe_verification = verify_answer(
            safe_answer,
            results,
            expected_answer_mode=expected_answer_mode,
            evidence_check=evidence_check,
            risk_flags=risk_flags,
            disclaimer=disclaimer,
            context=context,
        )
        if not safe_verification.passed:
            raise RuntimeError("programmatic terminal response failed verification")
        return safe_answer, safe_verification

    def condense_question(
        self,
        question: str,
        *,
        memory_messages: tuple[tuple[str, str], ...] | None = None,
    ) -> str:
        if memory_messages is None:
            memory_messages = self.memory.snapshot().messages
        last_question = next(
            (content for role, content in reversed(memory_messages) if role == "user"),
            "",
        )
        if not last_question:
            return question
        pronouns = ("这个", "这条", "上一条", "刚才", "它", "该条", "这一条")
        if not any(word in question for word in pronouns):
            return question
        last_answer = next(
            (
                content
                for role, content in reversed(memory_messages)
                if role == "assistant"
            ),
            "",
        )
        if self.condense_with_llm:
            rewritten = self.condense_with_llm_rewrite(
                question,
                last_question,
                last_answer=last_answer,
            )
            if rewritten:
                return rewritten
        # Rule fallback: carry over the previous question plus the laws and
        # article numbers the assistant just cited, so retrieval keeps anchors.
        references = extract_reference_hints(last_answer)
        condensed = f"结合上一轮问题“{last_question}”，回答：{question}"
        if references:
            condensed += f"（上一轮涉及：{'、'.join(references)}）"
        return condensed

    def condense_with_llm_rewrite(
        self,
        question: str,
        last_question: str,
        *,
        last_answer: str | None = None,
    ) -> str:
        if last_answer is None:
            last_answer = self.memory.last_assistant_answer()
        prompt = (
            "把用户的追问改写成一个不依赖上下文的独立中文检索问题。\n"
            "只输出改写后的问题本身，不要解释，不要加引号。\n\n"
            f"上一轮问题: {last_question}\n"
            f"上一轮回答摘要: {last_answer[:300]}\n"
            f"用户追问: {question}\n"
        )
        try:
            rewritten = self.llm.complete(prompt).strip().strip("\"'“”")
        except Exception:
            return ""
        if 0 < len(rewritten) <= 120 and "\n" not in rewritten:
            return rewritten
        return ""


def build_qa_prompt(
    *,
    question: str,
    original_question: str,
    memory: str,
    results: list[SearchResult],
) -> str:
    context_lines = []
    for result in results:
        chunk = result.chunk
        law = "、".join(chunk.law_names) or "未知法律"
        article = "、".join(chunk.article_numbers) or "未知条文"
        context_lines.append(
            f"[S{result.rank}] 来源: {law} {article}; 文件: {chunk.source_files[0] if chunk.source_files else ''}\n{chunk.text}"
        )
    context = "\n\n".join(context_lines)
    return f"""你是一个中国现行法律文本学习助手。
你只能根据给定的检索资料回答。资料不足时必须拒答，不能编造法律依据。
回答要求:
1. 用中文回答。
2. 先给结论，再列出依据。
3. 必须引用资料编号，例如 [S1]。
4. 具体案件策略、胜诉判断、个性化法律意见必须拒答。
5. 末尾保留免责声明: {LEGAL_DISCLAIMER}
6. 只输出一个 JSON 对象，不要输出 Markdown 代码围栏或额外说明。
7. JSON 必须且只能包含以下字段:
   - answer_text: 面向用户的完整字符串
   - answer_mode: evidence_answer / insufficient_evidence / needs_clarification / out_of_scope 之一
   - claims: 数组；每项只能包含 claim_id、text、source_ids
   - limitations: 字符串数组
   - clarification_question: 字符串或 null
8. 每条需要证据支撑的 claim 都要绑定本次资料中的 source_ids，禁止生成不存在的编号。
9. 不要在 JSON 中填写 snapshot、用户、权限或其他系统字段。

最近对话:
{memory or "无"}

用户原始问题:
{original_question}

独立检索问题:
{question}

检索资料:
{context}

请给出回答:
"""


def programmatic_answer(
    answer_text: str,
    *,
    answer_mode: str,
    limitations: list[str] | None = None,
    clarification_question: str | None = None,
    claims: list[AnswerClaim] | None = None,
) -> StructuredAnswer:
    if answer_mode != "evidence_answer":
        answer_text = sanitize_source_tokens(answer_text)
        limitations = [sanitize_source_tokens(item) for item in limitations or []]
        if clarification_question is not None:
            clarification_question = sanitize_source_tokens(clarification_question)
    return StructuredAnswer(
        answer_text=append_disclaimer(answer_text),
        answer_mode=answer_mode,
        claims=claims or [],
        limitations=limitations or [],
        clarification_question=clarification_question,
        adapter_source="programmatic",
    )


def safe_terminal_answer(answer_mode: str) -> StructuredAnswer:
    if answer_mode == "out_of_scope":
        return programmatic_answer(
            "这个请求超出当前法律文本学习助手的范围，我不能提供具体操作方案。",
            answer_mode="out_of_scope",
            limitations=["请求超出允许范围。"],
        )
    if answer_mode == "needs_clarification":
        question = SAFE_CLARIFICATION_QUESTION
        return programmatic_answer(
            SAFE_CLARIFICATION_RESPONSE,
            answer_mode="needs_clarification",
            limitations=["缺少作答所需的关键事实。"],
            clarification_question=question,
        )
    return programmatic_answer(
        "当前检索资料不足，无法给出可靠结论。",
        answer_mode="insufficient_evidence",
        limitations=["当前检索资料不足。"],
    )


def sanitize_source_tokens(text: str) -> str:
    """Prevent user/provider text from being interpreted as trusted citations."""

    return re.sub(r"\[S(\d+)\]", r"S\1", text)


def build_limited_structured_answer(check: EvidenceCheck) -> StructuredAnswer:
    limitations = [
        *check.missing_law_support,
        *check.missing_facts,
        *check.low_coverage,
    ]
    if check.missing_facts:
        return programmatic_answer(
            SAFE_CLARIFICATION_RESPONSE,
            answer_mode="needs_clarification",
            limitations=limitations,
            clarification_question=SAFE_CLARIFICATION_QUESTION,
        )
    return programmatic_answer(
        (
            "我无法仅根据当前检索资料给出可靠结论。\n\n"
            "可以补充更具体的法律名称、条文编号或事实背景后再检索。"
        ),
        answer_mode="insufficient_evidence",
        limitations=limitations or ["当前检索资料不足。"],
    )


def expected_limited_answer_mode(check: EvidenceCheck) -> str:
    return "needs_clarification" if check.missing_facts else "insufficient_evidence"


def render_retrieval_only_answer(results: list[SearchResult]) -> str:
    snippets = []
    for result in results:
        chunk = result.chunk
        snippets.append(f"[S{result.rank}] {chunk.text[:400]}")
    return "检索到以下可能相关的法律依据：\n\n" + "\n\n".join(snippets)


def should_refuse_before_retrieval(risk_flags: list[str]) -> bool:
    return bool(set(risk_flags) & PRE_RETRIEVAL_REFUSAL_FLAGS)


def build_risk_refusal_answer(risk_flags: list[str]) -> str:
    if "illegal_help" in risk_flags:
        reason = "我不能提供违法帮助、规避执法或相关操作方案。"
    elif "case_strategy" in risk_flags:
        reason = "我不能直接给出具体案件策略、胜诉判断或个性化法律意见。"
    elif "medical_financial_advice" in risk_flags:
        reason = "我不能提供医疗、金融或投资等专业建议。"
    else:
        reason = "这个问题不属于当前中国现行法律文本检索范围。"
    return append_disclaimer(
        reason + "\n\n我可以帮助检索相关法律条文或解释公开法律文本。"
    )


def append_disclaimer(answer: str) -> str:
    if answer.rstrip().endswith(LEGAL_DISCLAIMER):
        return answer
    return answer.rstrip() + "\n\n" + LEGAL_DISCLAIMER


def extract_reference_hints(text: str, limit: int = 4) -> list[str]:
    if not text:
        return []
    hints = extract_law_names(text) + extract_article_numbers(text)
    return hints[:limit]


def estimate_tokens(text: str) -> int:
    """Approximate token count: one token per CJK char, one per ASCII word.

    The old `len(text) // 2` heuristic underestimated Chinese text by ~2x and
    let the memory window grow far beyond its configured limit.
    """
    cjk_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    ascii_words = len(re.findall(r"[a-zA-Z0-9]+", text))
    other_chars = len(re.findall(r"[^\u4e00-\u9fffa-zA-Z0-9\s]", text))
    return max(1, cjk_chars + ascii_words + other_chars // 2)


def render_sources(results: list[SearchResult]) -> str:
    return format_sources(results)
