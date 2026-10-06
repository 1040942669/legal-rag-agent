from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from threading import RLock
from typing import Any

from .adaptive import (
    AdaptiveRetrievalResult,
    merge_followup_results,
    retrieve_adaptive,
)
from .evidence import (
    EVIDENCE_RULES_VERSIONS,
    GENERAL_EVIDENCE_RULES_VERSION,
    REFERENCE_BASED_EVIDENCE_RULES,
    reference_rules_for_evidence,
    query_compatible_title_hints,
    build_low_confidence_answer,
    check_evidence_sufficiency,
    with_stop_reason,
)
from .json_utils import validate_json_unicode
from .legal_references import MAX_REFERENCE_QUERY_CHARS, parse_legal_references
from .llm import build_completion_client
from .models import (
    AnswerClaim,
    EvidenceCheck,
    SearchResult,
    StructuredAnswer,
    VerificationContext,
    VerificationResult,
)
from .provider_errors import should_propagate_controlled_error
from .query import (
    QueryAnalysis,
    analyze_query,
    extract_article_numbers,
    extract_law_names,
)
from .retrieval import (
    Retriever,
    assert_results_match_boundary,
    format_sources,
    retrieval_boundary as resolve_retrieval_boundary,
)
from .retrieval_contracts import RetrievalBoundary, RetrievalBoundaryViolation
from .retrieval_outcomes import RetrievalOutcome
from .reference_evidence import check_reference_evidence
from .request_policy import request_answer_mode
from .semantic import (
    SemanticAssessment, SemanticChecker, SemanticInput, SemanticPolicy,
    SegmentAssessment, build_semantic_input, validate_semantic_execution_record,
)
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
STRUCTURED_QA_PROMPT_VERSION = "m1-structured-qa-citation-alignment-v2"
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
    # Appended to preserve the legacy positional constructor ABI.
    retrieval_boundary: RetrievalBoundary | None = None
    route_outcome: RetrievalOutcome | None = None


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
    semantic_assessment: SemanticAssessment | None = None
    semantic_policy_fingerprint: str | None = None
    semantic_error: str | None = None


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
        request_timeout: float = 180,
        adaptive_enabled: bool = False,
        adaptive_use_llm: bool = False,
        adaptive_max_queries: int = 3,
        adaptive_per_plan_top_k: int | None = None,
        normalizer_retries: int = 0,
        condense_with_llm: bool = False,
        verification_context: VerificationContext | None = None,
        completion_client: Any | None = None,
        semantic_policy: SemanticPolicy | None = None,
        semantic_checker: SemanticChecker | None = None,
        evidence_rules_version: str = GENERAL_EVIDENCE_RULES_VERSION,
        verification_rules_version: str = "general-bound-v2",
    ) -> None:
        if verification_rules_version not in {"general-bound-v2", "m1"}:
            raise ValueError("verification rules version is unsupported")
        if verification_rules_version == "m1" and (
            semantic_policy is not None or semantic_checker is not None
            or evidence_rules_version != "legacy-hints-and-return-v1"):
            raise ValueError("legacy verification requires explicit historical evidence and no semantic authority")
        if evidence_rules_version not in EVIDENCE_RULES_VERSIONS:
            raise ValueError("evidence rules version is unsupported")
        if semantic_policy is not None and not isinstance(semantic_policy, SemanticPolicy):
            raise ValueError("semantic policy must be a frozen SemanticPolicy")
        if semantic_checker is not None and (
            semantic_policy is None or getattr(semantic_checker, "policy", None) != semantic_policy
        ):
            raise ValueError("semantic checker must match the frozen policy")
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
        self.semantic_policy = semantic_policy
        self.semantic_checker = semantic_checker
        self.evidence_rules_version = evidence_rules_version
        self.verification_rules_version = verification_rules_version
        self.last_adaptive_result: AdaptiveRetrievalResult | None = None
        self.last_evidence_check: EvidenceCheck | None = None
        self.last_verification: VerificationResult | None = None
        self.last_pre_fallback_verification: VerificationResult | None = None
        self.last_pre_fallback_answer: StructuredAnswer | None = None
        self.last_structured_answer: StructuredAnswer | None = None
        self.last_rejected_original_source_ids: list[str] = []
        self.last_evidence_source_id_map: dict[str, str] = {}
        self.last_generation_error: str | None = None
        self.last_generation_kind: str | None = None
        self.memory = ConversationMemory(token_limit=memory_token_limit)
        self._commit_lock = RLock()
        self._session_revision = 0
        self.llm = (
            completion_client
            if completion_client is not None
            else build_completion_client(
                model,
                ollama_base_url=ollama_base_url,
                request_timeout=request_timeout,
            )
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
        self.last_generation_kind = None

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
        generated = self.assess_turn(generated)
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
            analysis=analyze_query(standalone_question, evidence_rules_version=self.evidence_rules_version),
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
        if analysis != analyze_query(standalone_question, evidence_rules_version=self.evidence_rules_version):
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

    def retrieve_turn(
        self,
        prepared: PreparedQuestion,
        *,
        max_followup_rounds: int = 1,
    ) -> RetrievedTurn:
        if type(max_followup_rounds) is not int or max_followup_rounds < 0:
            raise ValueError("max_followup_rounds must be a non-negative integer")
        boundary = resolve_retrieval_boundary(self.retriever)
        if prepared.analysis != analyze_query(prepared.standalone_question, evidence_rules_version=self.evidence_rules_version):
            raise ValueError("prepared analysis differs from the frozen request rules")
        if should_refuse_before_retrieval(prepared.analysis.risk_flags, evidence_rules_version=self.evidence_rules_version):
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
                retrieval_boundary=boundary,
                terminal_kind="pre_retrieval_refusal",
                terminal_answer=answer,
                terminal_expected_answer_mode="out_of_scope",
            )

        outcome_method = getattr(self.retriever, "retrieve_outcome", None)
        known_titles = getattr(self.retriever, "known_law_hints", ())
        references = None
        if callable(outcome_method) and len(prepared.standalone_question) <= MAX_REFERENCE_QUERY_CHARS:
            try:
                references = parse_legal_references(prepared.standalone_question, known_law_titles=known_titles,
                    rules_version=reference_rules_for_evidence(self.evidence_rules_version))
            except (TypeError, ValueError):
                pass  # The bounded ordinary path reports invalid parsing.
        route_outcome = None
        if references is not None and (references.requirements or references.unresolved):
            if self.evidence_rules_version not in REFERENCE_BASED_EVIDENCE_RULES:
                raise ValueError("exact routing requires general evidence rules")
            route_outcome = outcome_method(prepared.standalone_question, top_k=self.top_k)
            if not isinstance(route_outcome, RetrievalOutcome) or route_outcome.route != "exact_reference":
                raise ValueError("explicit references require a typed exact route outcome")
            validate_reference_route(route_outcome, prepared.standalone_question, evidence_rules_version=self.evidence_rules_version)
            checked = check_evidence_sufficiency(
                prepared.standalone_question, list(route_outcome.results),
                rules_version=self.evidence_rules_version, known_law_titles=known_titles)
            checked = constrain_reference_route(checked, route_outcome)
            adaptive_result = AdaptiveRetrievalResult(
                results=deepcopy(list(route_outcome.results)), analysis=prepared.analysis,
                normalized_query=None, plans=[], planner_trace={"mode": "exact_reference"},
                merge_trace={"mode": "exact_reference", "input_result_count": len(route_outcome.results),
                             "deduped_count": len(route_outcome.results)},
                evidence_check=checked,
                followup_trace={"controller": "reference_router", "rounds_used": 0,
                                "queries": [], "stop_reason": checked.stop_reason},
                adaptive_used=False, adaptive_enabled=self.adaptive_enabled, trigger_reasons=[])
        else:
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
                max_followup_rounds=max_followup_rounds,
                evidence_rules_version=self.evidence_rules_version,
            )
        results, rejected_source_ids, source_id_map = filter_results_to_context(
            adaptive_result.results,
            self.verification_context,
        )
        context_configured = self.verification_context is not None and (
            self.verification_context.snapshot_id is not None
            or self.verification_context.allowed_scope_ids is not None)
        if rejected_source_ids or context_configured:
            evidence_check = check_evidence_sufficiency(
                prepared.standalone_question,
                results,
                analysis=adaptive_result.analysis,
                normalized_query=adaptive_result.normalized_query,
                plans=adaptive_result.plans,
                rules_version=self.evidence_rules_version,
                known_law_titles=known_titles,
                snapshot_id=self.verification_context.snapshot_id if context_configured else None,
                allowed_scope_ids=self.verification_context.allowed_scope_ids if context_configured else None,
            )
            if rejected_source_ids and evidence_check.stop_reason != "needs_clarification":
                evidence_check = replace(evidence_check, stop_reason="evidence_scope_filtered")
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

        if route_outcome is not None:
            resolved = surviving_reference_pairs(route_outcome, results)
            status, reasons = route_outcome.status, route_outcome.reason_codes
            if status == "found" and set(resolved) != set(route_outcome.requested_pairs):
                status, reasons = "not_found", (*reasons, "evidence_scope_filtered")
            route_outcome = replace(route_outcome, results=tuple(results), status=status,
                                    resolved_pairs=resolved, reason_codes=tuple(dict.fromkeys(reasons)))
            checked = constrain_reference_route(adaptive_result.evidence_check, route_outcome)
            adaptive_result = replace(adaptive_result, evidence_check=checked,
                                      followup_trace={**adaptive_result.followup_trace,
                                                      "stop_reason": checked.stop_reason})
        elif callable(outcome_method):
            route_outcome = RetrievalOutcome(tuple(results), "lexical", "found" if results else "not_found",
                                             ("lexical_results" if results else "lexical_no_results",))

        evidence_check = adaptive_result.evidence_check
        # Detach the staged result from every mutable object owned by a
        # retriever.  A later caller mutation must not poison a bound corpus or
        # race with prompt construction.
        staged_results = deepcopy(results)
        assert_results_match_boundary(
            staged_results, boundary, stage="chat retrieval staging"
        )
        adaptive_result = replace(
            adaptive_result,
            results=deepcopy(staged_results),
        )
        terminal_answer = None
        terminal_kind = None
        terminal_expected_answer_mode = None
        if not staged_results or not evidence_check.sufficient:
            terminal_answer = build_limited_structured_answer(evidence_check)
            terminal_kind = "evidence_limited"
            terminal_expected_answer_mode = expected_limited_answer_mode(evidence_check)
        return RetrievedTurn(
            prepared=prepared,
            adaptive_result=adaptive_result,
            results=tuple(staged_results),
            evidence_check=evidence_check,
            retrieval_boundary=boundary,
            rejected_source_ids=tuple(rejected_source_ids),
            source_id_map=dict(source_id_map),
            terminal_kind=terminal_kind,
            terminal_answer=terminal_answer,
            terminal_expected_answer_mode=terminal_expected_answer_mode,
            route_outcome=route_outcome,
        )

    def merge_followup_turn(
        self,
        retrieved: RetrievedTurn,
        followup_results: list[SearchResult],
        *,
        queries: list[str],
        exhausted_stop_reason: str = "max_retrieval_rounds",
    ) -> RetrievedTurn:
        """Merge one graph-controlled follow-up round without another loop.

        M5 reserves and dispatches each query outside this method.  This method
        performs only deterministic boundary checks, merge, evidence checking,
        and construction of the next immutable staged turn.
        """

        existing = self._validated_stage_results(
            retrieved,
            stage="chat follow-up input",
        )
        if retrieved.route_outcome is not None and retrieved.route_outcome.route == "exact_reference":
            raise ValueError("exact reference routes cannot enter fuzzy follow-up")
        if retrieved.adaptive_result is None or retrieved.evidence_check is None:
            raise ValueError("follow-up requires a completed retrieval stage")
        if (
            not isinstance(queries, list)
            or not queries
            or not all(
                isinstance(query, str)
                and query
                and query == query.strip()
                for query in queries
            )
        ):
            raise ValueError("follow-up queries must be normalized strings")
        if not isinstance(followup_results, list):
            raise ValueError("followup_results must be a list")
        boundary = retrieved.retrieval_boundary or resolve_retrieval_boundary(
            self.retriever
        )
        assert_results_match_boundary(
            followup_results,
            boundary,
            stage="chat graph follow-up retrieval",
        )
        merged = merge_followup_results(
            list(existing),
            deepcopy(followup_results),
            final_top_k=self.top_k,
        )
        filtered, rejected_source_ids, source_id_map = filter_results_to_context(
            merged,
            self.verification_context,
        )
        checked = check_evidence_sufficiency(
            retrieved.prepared.standalone_question,
            filtered,
            analysis=retrieved.adaptive_result.analysis,
            normalized_query=retrieved.adaptive_result.normalized_query,
            plans=retrieved.adaptive_result.plans,
            rules_version=self.evidence_rules_version,
            known_law_titles=getattr(self.retriever, "known_law_hints", ()),
            snapshot_id=self.verification_context.snapshot_id if self.verification_context else None,
            allowed_scope_ids=self.verification_context.allowed_scope_ids if self.verification_context else None,
        )
        stop_reason = (
            "sufficient_after_followup"
            if checked.sufficient
            else exhausted_stop_reason
        )
        checked = with_stop_reason(checked, stop_reason)
        adaptive = replace(
            retrieved.adaptive_result,
            results=deepcopy(filtered),
            evidence_check=checked,
            merge_trace={
                **retrieved.adaptive_result.merge_trace,
                "graph_followup": {
                    "query_count": len(queries),
                    "input_result_count": len(followup_results),
                    "returned_count": len(filtered),
                },
            },
            followup_trace={
                "controller": "m5_graph",
                "rounds_used": 1,
                "query_hashes": [
                    hashlib.sha256(query.encode("utf-8")).hexdigest()
                    for query in queries
                ],
                "stop_reason": stop_reason,
            },
        )
        terminal_answer = None
        terminal_kind = None
        terminal_expected_answer_mode = None
        if not filtered or not checked.sufficient:
            terminal_answer = build_limited_structured_answer(checked)
            terminal_kind = "evidence_limited"
            terminal_expected_answer_mode = expected_limited_answer_mode(checked)
        return RetrievedTurn(
            prepared=retrieved.prepared,
            adaptive_result=adaptive,
            results=tuple(deepcopy(filtered)),
            evidence_check=checked,
            retrieval_boundary=boundary,
            rejected_source_ids=tuple(rejected_source_ids),
            source_id_map=dict(source_id_map),
            terminal_kind=terminal_kind,
            terminal_answer=terminal_answer,
            terminal_expected_answer_mode=terminal_expected_answer_mode,
            route_outcome=(RetrievalOutcome(tuple(filtered), "lexical", "found" if filtered else "not_found",
                                           ("lexical_results" if filtered else "lexical_no_results",))
                           if retrieved.route_outcome is not None else None),
        )

    def generate_turn(
        self,
        retrieved: RetrievedTurn,
        *,
        generate: bool,
    ) -> GeneratedTurn:
        results = self._validated_stage_results(
            retrieved, stage="chat generation input"
        )
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
        if not generate:
            answer_text = append_disclaimer(render_retrieval_only_answer(results))
            return GeneratedTurn(
                retrieved=retrieved,
                kind="retrieval_only",
                answer_text=answer_text,
            )

        if request_answer_mode(retrieved.prepared.analysis.risk_flags,
                               evidence_rules_version=self.evidence_rules_version,
                               free_generation=True) == "needs_clarification":
            answer = safe_terminal_answer("needs_clarification")
            return GeneratedTurn(retrieved=retrieved, kind="request_clarification",
                                 answer_text=answer.answer_text, answer=answer,
                                 expected_answer_mode="needs_clarification")

        prompt = build_qa_prompt(
            question=retrieved.prepared.standalone_question,
            original_question=retrieved.prepared.original_question,
            memory=retrieved.prepared.memory_text,
            results=results,
        )
        try:
            raw_answer = self.llm.complete(prompt)
        except Exception as exc:
            if should_propagate_controlled_error(exc, self.llm):
                raise
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
            semantic_policy_fingerprint=(self.semantic_policy.fingerprint if self.semantic_policy is not None else None),
        )

    def _semantic_input(self, generated: GeneratedTurn) -> SemanticInput:
        if generated.answer is None or self.semantic_policy is None:
            raise ValueError("semantic assessment requires a draft and frozen policy")
        results = self._validated_stage_results(generated.retrieved, stage="semantic input")
        boundary = generated.retrieved.retrieval_boundary
        return build_semantic_input(
            generated.retrieved.prepared.standalone_question, generated.answer, results,
            self.semantic_policy, context=self.verification_context,
            boundary_fingerprint=boundary.fingerprint if boundary is not None else None,
            disclaimer=LEGAL_DISCLAIMER,
        )

    def assess_turn(self, generated: GeneratedTurn) -> GeneratedTurn:
        """Explicit inference stage; deterministic verification never calls a checker.

        Service callers must inject the governed completion operation view. A
        cached assessment is not a permission to reissue an external request.
        """
        if not isinstance(generated, GeneratedTurn):
            raise TypeError("semantic stage requires a GeneratedTurn")
        if generated.kind == "model" and request_answer_mode(generated.retrieved.prepared.analysis.risk_flags,
                               evidence_rules_version=self.evidence_rules_version,
                               free_generation=True) is not None:
            raise ValueError("unresolved request purpose cannot obtain model assessment")
        if generated.kind != "model" or self.semantic_policy is None:
            return generated
        if self.verification_rules_version != "general-bound-v2":
            raise ValueError("legacy verification cannot acquire semantic authority")
        if generated.semantic_policy_fingerprint != self.semantic_policy.fingerprint:
            raise ValueError("semantic draft policy differs from current frozen policy")
        if generated.semantic_assessment is not None or generated.semantic_error is not None:
            return generated
        structural = verify_answer(
            generated.answer, self._validated_stage_results(generated.retrieved, stage="pre-semantic structure"),
            evidence_check=generated.retrieved.evidence_check,
            risk_flags=generated.retrieved.prepared.analysis.risk_flags,
            expected_answer_mode=generated.expected_answer_mode,
            disclaimer=LEGAL_DISCLAIMER, context=self.verification_context,
            verification_rules_version=self.verification_rules_version,
        )
        if not structural.passed:
            return generated
        if self.semantic_checker is None:
            return replace(generated, semantic_error="checker_unavailable")
        try:
            request = self._semantic_input(generated)
        except (TypeError, ValueError):
            return replace(generated, semantic_error="input_invalid")
        try:
            assessment = self.semantic_checker.assess(request)
            if not isinstance(assessment, SemanticAssessment):
                raise ValueError("checker did not return a typed assessment")
            return replace(generated, semantic_assessment=deepcopy(assessment))
        except Exception as error:
            client = getattr(self.semantic_checker, "client", self.semantic_checker)
            if should_propagate_controlled_error(error, client):
                raise
            assessment = SemanticAssessment(
                request.fingerprint, self.semantic_policy.fingerprint,
                self.semantic_policy.checker_id, self.semantic_policy.checker_revision,
                self.semantic_policy.prompt_version,
                tuple(SegmentAssessment(segment.segment_id, "error", (), ("checker_error",))
                      for segment in request.segments),
            )
            return replace(generated, semantic_assessment=assessment, semantic_error="checker_error")

    def verify_turn(self, generated: GeneratedTurn) -> VerifiedTurn:
        if not isinstance(generated, GeneratedTurn):
            raise TypeError("verification requires a GeneratedTurn")
        validate_semantic_execution_record(generated.semantic_policy_fingerprint,
                                          generated.semantic_assessment, generated.semantic_error)
        if generated.kind != "model" and any(value is not None for value in (
            generated.semantic_assessment, generated.semantic_policy_fingerprint, generated.semantic_error)):
            raise ValueError("nonmodel turns cannot carry semantic authority")
        retrieved = generated.retrieved
        results = self._validated_stage_results(
            retrieved, stage="chat verification input"
        )
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
        elif generated.kind == "request_clarification":
            from .chat_artifacts import _validate_generated_relationships
            _validate_generated_relationships(generated)
            answer, verification = self._verify_programmatic_answer(
                generated.answer, results, expected_answer_mode="needs_clarification",
                evidence_check=retrieved.evidence_check,
                risk_flags=retrieved.prepared.analysis.risk_flags,
                disclaimer=LEGAL_DISCLAIMER, context=self.verification_context)
        elif generated.kind == "model":
            if request_answer_mode(retrieved.prepared.analysis.risk_flags,
                                   evidence_rules_version=self.evidence_rules_version,
                                   free_generation=True) is not None:
                raise RuntimeError("unresolved request purpose cannot obtain free generation")
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
                semantic_policy=self.semantic_policy,
                semantic_assessment=generated.semantic_assessment,
                semantic_question=retrieved.prepared.standalone_question,
                semantic_boundary_fingerprint=(retrieved.retrieval_boundary.fingerprint
                                               if retrieved.retrieval_boundary is not None else None),
                verification_rules_version=self.verification_rules_version,
            )
            if generated.semantic_policy_fingerprint != (
                self.semantic_policy.fingerprint if self.semantic_policy is not None else None
            ):
                verification = replace(verification, passed=False, semantic_support_status="error",
                                       failure_reasons=[*verification.failure_reasons, "semantic_policy_mismatch"])
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

    def _validated_stage_results(
        self,
        retrieved: RetrievedTurn,
        *,
        stage: str,
    ) -> list[SearchResult]:
        """Take and validate a private evidence snapshot for one transition."""

        if not isinstance(retrieved, RetrievedTurn):
            raise TypeError("retrieved stage must be a RetrievedTurn")
        current_boundary = resolve_retrieval_boundary(self.retriever)
        if retrieved.prepared.analysis != analyze_query(retrieved.prepared.standalone_question,
                                                        evidence_rules_version=self.evidence_rules_version):
            raise RetrievalBoundaryViolation(f"{stage} request analysis differs from the current execution contract")
        captured_boundary = retrieved.retrieval_boundary
        if current_boundary != captured_boundary:
            raise RetrievalBoundaryViolation(
                f"{stage} retrieval boundary changed after retrieval"
            )
        if retrieved.evidence_check is not None and retrieved.evidence_check.rules_version != self.evidence_rules_version:
            raise RetrievalBoundaryViolation(f"{stage} evidence rules differ from the current execution contract")
        results = deepcopy(list(retrieved.results))
        if retrieved.route_outcome is not None:
            if retrieved.route_outcome.results != retrieved.results:
                raise RetrievalBoundaryViolation(f"{stage} route and staged evidence snapshots differ")
            validate_reference_route(retrieved.route_outcome, retrieved.prepared.standalone_question,
                                     evidence_rules_version=self.evidence_rules_version)
        if retrieved.adaptive_result is not None and tuple(
            retrieved.adaptive_result.results
        ) != tuple(retrieved.results):
            raise RetrievalBoundaryViolation(
                f"{stage} adaptive and staged evidence snapshots differ"
            )
        assert_results_match_boundary(results, captured_boundary, stage=stage)
        # Use the same full relationship/terminal contract as the codec. A
        # direct stage call must not bypass derived programmatic-answer checks.
        from .chat_artifacts import validate_retrieved_turn_contract
        try:
            validate_retrieved_turn_contract(retrieved)
        except (TypeError, ValueError, RuntimeError):
            raise RetrievalBoundaryViolation(f"{stage} retrieved contract is invalid") from None
        if retrieved.evidence_check is not None and self.evidence_rules_version in REFERENCE_BASED_EVIDENCE_RULES:
            adaptive = retrieved.adaptive_result
            current = check_evidence_sufficiency(
                retrieved.prepared.standalone_question, results,
                analysis=retrieved.prepared.analysis,
                normalized_query=adaptive.normalized_query if adaptive is not None else None,
                plans=adaptive.plans if adaptive is not None else None,
                rules_version=self.evidence_rules_version,
                known_law_titles=getattr(self.retriever, "known_law_hints", ()),
                snapshot_id=self.verification_context.snapshot_id if self.verification_context else None,
                allowed_scope_ids=self.verification_context.allowed_scope_ids if self.verification_context else None)
            if retrieved.route_outcome is not None:
                current = constrain_reference_route(current, retrieved.route_outcome)
            # Stop reasons and follow-up exhaustion belong to the controller.
            # Facts, owned coverage and actual scores must still match now.
            for name in ("sufficient", "missing_facts", "missing_law_support", "low_coverage",
                         "checked_result_count", "covered_laws", "covered_articles", "mechanical_check"):
                if getattr(current, name) != getattr(retrieved.evidence_check, name):
                    raise RetrievalBoundaryViolation(f"{stage} mechanical evidence assessment changed")
        return results

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
        published_generation_kind = deepcopy(generated.kind)

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
            self.last_generation_kind = published_generation_kind
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
            verification_rules_version=self.verification_rules_version,
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
            verification_rules_version=self.verification_rules_version,
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
        if self.evidence_rules_version in REFERENCE_BASED_EVIDENCE_RULES:
            # Current explicit references remain original request authority.
            # Historical dialogue is still available as separate memory, but
            # neither a rule concatenation nor model rewrite may add old pairs.
            try:
                references = parse_legal_references(
                    question, known_law_titles=getattr(self.retriever, "known_law_hints", ()),
                    rules_version=reference_rules_for_evidence(self.evidence_rules_version),
                )
            except (TypeError, ValueError):
                return question
            if (references.mentions or references.excluded or references.requirements
                or references.required_law_titles or references.unresolved):
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
        except Exception as exc:
            if should_propagate_controlled_error(exc, self.llm):
                raise
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
    example_claim = "检索资料支持的单条结论"
    example_source = f"S{results[0].rank}" if results else None
    layout_example = {
        "answer_text": (
            f"{example_claim} [{example_source}]。\n{LEGAL_DISCLAIMER}"
            if example_source
            else f"当前检索资料不足，无法给出可靠结论。\n{LEGAL_DISCLAIMER}"
        ),
        "answer_mode": "evidence_answer" if example_source else "insufficient_evidence",
        "claims": (
            [{"claim_id": "C1", "text": example_claim, "source_ids": [example_source]}]
            if example_source
            else []
        ),
        "limitations": [] if example_source else ["检索资料不足。"],
        "clarification_question": None,
    }
    example_json = json.dumps(layout_example, ensure_ascii=False)
    return f"""你是一个中国现行法律文本学习助手。
你只能根据给定的检索资料回答。资料不足时必须拒答，不能编造法律依据。
回答要求:
1. 用中文回答。
2. 先给结论，再列出依据。
3. 必须引用资料编号，例如 [S1]。可见引用必须写在 answer_text 正文的对应断言同一句内，放在句末标点之前，例如“单条断言 [S1]。”。只在 claims.source_ids 中填写编号不算正文引用；只在回答末尾集中列出引用不能替代同句引用。
4. 具体案件策略、胜诉判断、个性化法律意见必须拒答。
5. 末尾保留免责声明: {LEGAL_DISCLAIMER}
6. 只输出一个 JSON 对象，不要输出 Markdown 代码围栏或额外说明。
7. JSON 必须且只能包含以下字段:
   - answer_text: 面向用户的完整字符串
   - answer_mode: evidence_answer / insufficient_evidence / needs_clarification / out_of_scope 之一
   - claims: 数组；每项只能包含 claim_id、text、source_ids
   - limitations: 字符串数组
   - clarification_question: 字符串或 null
8. 每条需要证据支撑的 claim 都要绑定本次资料中的 source_ids，禁止生成不存在的编号。将 answer_text 中对应单句断言的正文逐字复制到 claims.text，不包含引用标注和句末标点；每条 claim 不得跨句或跨行。source_ids 使用 ["S1"] 这样的字符串数组，不使用 ["[S1]"]，并与该断言同句内的可见引用一致。示例里的占位结论必须根据本次检索资料替换，不能把格式示例当作证据。
9. 不要在 JSON 中填写 snapshot、用户、权限或其他系统字段。

引用布局示例（仅说明格式，不是检索证据，禁止复制示例结论）:
{example_json}

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


def _route_analysis(outcome: RetrievalOutcome, question: str, *, evidence_rules_version=GENERAL_EVIDENCE_RULES_VERSION):
    titles = (*[law for law, _ in outcome.requested_pairs],
              *[law for result in outcome.results for law in result.chunk.law_names])
    if evidence_rules_version == GENERAL_EVIDENCE_RULES_VERSION:
        titles = query_compatible_title_hints(titles)
    return parse_legal_references(question, known_law_titles=titles,
                                  rules_version=reference_rules_for_evidence(evidence_rules_version))


def surviving_reference_pairs(outcome: RetrievalOutcome, results: list[SearchResult]):
    surviving = []
    for law, article in outcome.resolved_pairs:
        analysis = parse_legal_references(f"《{law}》{article}", known_law_titles=(law,))
        checked = check_reference_evidence(analysis, results)
        if not checked.missing_pairs and not checked.missing_laws:
            surviving.append((law, article))
    return tuple(surviving)


def validate_reference_route(outcome: RetrievalOutcome, question: str, *, evidence_rules_version=GENERAL_EVIDENCE_RULES_VERSION) -> None:
    if not isinstance(outcome, RetrievalOutcome):
        raise ValueError("reference route must be typed")
    analysis = _route_analysis(outcome, question, evidence_rules_version=evidence_rules_version)
    expected = tuple(dict.fromkeys((item.law_title, item.article_number) for item in analysis.requirements))
    if len(expected) > 16:
        if (outcome.route != "exact_reference" or outcome.status != "needs_disambiguation"
            or outcome.reason_codes != ("too_many_references",) or outcome.results
            or outcome.requested_pairs or outcome.resolved_pairs):
            raise ValueError("reference overflow requires an empty controlled clarification outcome")
        return
    if outcome.route == "lexical":
        if expected or analysis.unresolved:
            raise ValueError("lexical route cannot replace explicit or unresolved references")
        return
    if set(outcome.requested_pairs) != set(expected):
        raise ValueError("exact route requirements differ from the original question")
    if analysis.unresolved and outcome.status != "needs_disambiguation":
        raise ValueError("unresolved exact references require disambiguation")
    if set(surviving_reference_pairs(outcome, list(outcome.results))) != set(outcome.resolved_pairs):
        raise ValueError("exact resolved pairs are not supported by the original chunks")


def constrain_reference_route(check: EvidenceCheck, outcome: RetrievalOutcome) -> EvidenceCheck:
    if outcome.route != "exact_reference" or outcome.status == "found":
        return check
    stop_reason = "needs_clarification" if outcome.status == "needs_disambiguation" else "exact_reference_not_found"
    return replace(check, sufficient=False, followup_queries=[], stop_reason=stop_reason,
                   missing_law_support=list(dict.fromkeys((*check.missing_law_support,
                                                           "exact_route:" + outcome.status))))


def build_limited_structured_answer(check: EvidenceCheck) -> StructuredAnswer:
    limitations = [
        *check.missing_law_support,
        *check.missing_facts,
        *check.low_coverage,
    ]
    if expected_limited_answer_mode(check) == "needs_clarification":
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
    return "needs_clarification" if check.missing_facts or check.stop_reason == "needs_clarification" else "insufficient_evidence"


def render_retrieval_only_answer(results: list[SearchResult]) -> str:
    snippets = []
    for result in results:
        chunk = result.chunk
        snippets.append(f"[S{result.rank}] {chunk.text[:400]}")
    return "检索到以下可能相关的法律依据：\n\n" + "\n\n".join(snippets)


def should_refuse_before_retrieval(risk_flags: list[str], *, evidence_rules_version="general-reference-v2") -> bool:
    return request_answer_mode(risk_flags, evidence_rules_version=evidence_rules_version,
                               free_generation=False) == "out_of_scope"


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
