from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from legal_rag.chat import (
    GeneratedTurn,
    LegalChatAssistant,
    PreparedQuestion,
    RetrievedTurn,
    programmatic_answer,
)
from legal_rag.chat_artifacts import (
    retrieved_turn_from_artifact,
    retrieved_turn_to_artifact,
)
from legal_rag.evaluation_artifacts import search_result_to_artifact
from legal_rag.experiment_runtime import canonical_hash
from legal_rag.models import SearchResult
from legal_rag.services.run_executor import (
    ExecutionFailure,
    RunExecutionInput,
    SafeRunResult,
    SafeStageCallback,
    safe_run_result_from_verified,
)

from .budget import (
    AttemptReservation,
    BudgetSnapshot,
    RetryDecision,
    retry_allowed,
    retry_decision,
)
from .state import (
    HarnessState,
    checkpoint_marker,
    query_hash,
    validate_harness_state,
)


@dataclass(frozen=True, slots=True)
class NodeArtifactRef:
    artifact_id: str
    payload_hash: str


class HarnessPersistence(Protocol):
    def assert_execution_fence(
        self, run_id: str, worker_id: str, lease_epoch: int
    ) -> None: ...

    def get_budget(self, run_id: str) -> BudgetSnapshot: ...

    def begin_retrieval_round(
        self,
        run_id: str,
        *,
        worker_id: str,
        lease_epoch: int,
        query_count: int,
    ) -> BudgetSnapshot: ...

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
    ) -> AttemptReservation: ...

    def mark_attempt_dispatched(
        self,
        attempt_id: str,
        *,
        worker_id: str,
        lease_epoch: int,
    ) -> None: ...

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
    ) -> None: ...

    def has_outcome_unknown(self, run_id: str, *, operation_name: str) -> bool: ...

    def save_node_artifact(
        self,
        run_id: str,
        *,
        worker_id: str,
        lease_epoch: int,
        artifact_kind: str,
        payload: Mapping[str, Any],
    ) -> NodeArtifactRef: ...

    def load_node_artifact(
        self,
        run_id: str,
        artifact_id: str,
        *,
        expected_kind: str,
        expected_hash: str,
    ) -> Mapping[str, Any]: ...

    def publish_terminal_result(
        self,
        run_id: str,
        worker_id: str,
        lease_epoch: int,
        result: SafeRunResult,
        *,
        completion_status: str,
        stop_reason: str | None,
    ) -> Any: ...


class FollowupPlanner(Protocol):
    def __call__(
        self,
        state: HarnessState,
        retrieved: RetrievedTurn,
    ) -> Sequence[str]: ...


FaultHook = Callable[[str, HarnessState], None]


@dataclass(slots=True)
class BoundedHarnessNodes:
    """Node implementations with PostgreSQL-authoritative budgets and fences."""

    assistant: LegalChatAssistant
    execution_input: RunExecutionInput
    persistence: HarnessPersistence
    worker_id: str
    lease_epoch: int
    emit_event: SafeStageCallback
    generate_enabled: bool = False
    followup_planner: FollowupPlanner | None = None
    retry_unknown_external: bool = False
    fault_hook: FaultHook | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.assistant, LegalChatAssistant):
            raise TypeError("assistant must be a LegalChatAssistant")
        if not isinstance(self.execution_input, RunExecutionInput):
            raise TypeError("execution_input must be a RunExecutionInput")
        if self.assistant.adaptive_enabled or self.assistant.adaptive_use_llm:
            raise ValueError("M5 graph must be the only adaptive loop controller")
        if self.assistant.condense_with_llm:
            raise ValueError("M5 does not permit an unaccounted condense model call")
        if type(self.lease_epoch) is not int or self.lease_epoch < 1:
            raise ValueError("lease_epoch must be positive")
        if not callable(self.emit_event):
            raise TypeError("emit_event must be callable")
        if self.followup_planner is not None and not callable(self.followup_planner):
            raise TypeError("followup_planner must be callable")
        if self.fault_hook is not None and not callable(self.fault_hook):
            raise TypeError("fault_hook must be callable")

    def analyze_query(self, state: HarnessState) -> HarnessState:
        prepared = self._prepared(state)
        changed = self._copy(state)
        changed["analysis"] = prepared.analysis.to_dict()
        changed["proposed_queries"] = [prepared.standalone_question]
        return self._complete(changed, "analyze_query", "route")

    def route(self, state: HarnessState) -> HarnessState:
        prepared = self._prepared(state)
        changed = self._copy(state)
        changed["route"] = (
            "programmatic_terminal"
            if prepared.analysis.risk_flags
            and any(
                flag
                in {
                    "case_strategy",
                    "illegal_help",
                    "medical_financial_advice",
                    "non_legal",
                }
                for flag in prepared.analysis.risk_flags
            )
            else "retrieve"
        )
        return self._complete(changed, "route", "retrieve")

    def retrieve(self, state: HarnessState) -> HarnessState:
        self._fence()
        prepared = self._prepared(state)
        if state["retrieval_rounds_used"] == 0:
            if state["route"] == "programmatic_terminal":
                retrieved = self.assistant.retrieve_turn(
                    prepared,
                    max_followup_rounds=0,
                )
            else:
                queries = [prepared.standalone_question]
                self.persistence.begin_retrieval_round(
                    state["run_id"],
                    worker_id=self.worker_id,
                    lease_epoch=self.lease_epoch,
                    query_count=1,
                )
                retrieved = self._invoke_initial_retrieval(
                    state,
                    prepared,
                    query=queries[0],
                )
        else:
            queries = self._bounded_queries(state["proposed_queries"])
            self.persistence.begin_retrieval_round(
                state["run_id"],
                worker_id=self.worker_id,
                lease_epoch=self.lease_epoch,
                query_count=len(queries),
            )
            followup_results: list[SearchResult] = []
            for query in queries:
                followup_results.extend(
                    self._invoke_followup_retrieval(
                        state,
                        query=query,
                        round_number=state["retrieval_rounds_used"] + 1,
                    )
                )
            previous = self._retrieved(state)
            budget = self.persistence.get_budget(state["run_id"])
            retrieved = self.assistant.merge_followup_turn(
                previous,
                followup_results,
                queries=queries,
                exhausted_stop_reason=(
                    "max_retrieval_rounds"
                    if budget.retrieval_rounds_used >= budget.max_retrieval_rounds
                    else "followup_evidence_insufficient"
                ),
            )

        artifact_payload = retrieved_turn_to_artifact(retrieved)
        artifact = self.persistence.save_node_artifact(
            state["run_id"],
            worker_id=self.worker_id,
            lease_epoch=self.lease_epoch,
            artifact_kind="retrieved_turn",
            payload=artifact_payload,
        )
        budget = self.persistence.get_budget(state["run_id"])
        changed = self._copy(state)
        changed["retrieved_artifact_ref"] = artifact.artifact_id
        changed["retrieved_artifact_hash"] = artifact.payload_hash
        changed["retrieved_evidence_refs"] = [
            item.chunk.chunk_id for item in retrieved.results
        ]
        changed["immutable_evidence_hashes"] = [
            canonical_hash(search_result_to_artifact(item))
            for item in retrieved.results
        ]
        completed_hashes = list(changed["completed_query_hashes"])
        for query in changed["proposed_queries"]:
            digest = query_hash(query)
            if digest not in completed_hashes:
                completed_hashes.append(digest)
        changed["completed_query_hashes"] = completed_hashes
        changed["proposed_queries"] = []
        self._sync_budget(changed, budget)
        self.emit_event(
            "retrieval.completed",
            {
                "result_count": len(retrieved.results),
                "checked_result_count": len(retrieved.results),
                "rejected_count": len(retrieved.rejected_source_ids),
                "stop_reason": (
                    retrieved.evidence_check.stop_reason
                    if retrieved.evidence_check is not None
                    else "programmatic_terminal"
                ),
            },
        )
        return self._complete(changed, "retrieve", "merge_evidence")

    def merge_evidence(self, state: HarnessState) -> HarnessState:
        self._retrieved(state)
        return self._complete(self._copy(state), "merge_evidence", "check_evidence")

    def check_evidence(self, state: HarnessState) -> HarnessState:
        retrieved = self._retrieved(state)
        changed = self._copy(state)
        check = retrieved.evidence_check
        if retrieved.terminal_kind == "pre_retrieval_refusal":
            changed["completion_status"] = "completed_with_limits"
            changed["stop_reason"] = "policy_refusal"
            next_node = "generate"
        elif check is not None and check.sufficient:
            changed["completion_status"] = "succeeded"
            changed["stop_reason"] = check.stop_reason or "sufficient"
            next_node = "generate"
        elif check is not None and check.stop_reason == "needs_clarification":
            changed["completion_status"] = "needs_clarification"
            changed["stop_reason"] = "needs_clarification"
            next_node = "generate"
        else:
            budget = self.persistence.get_budget(state["run_id"])
            self._sync_budget(changed, budget)
            if budget.retrieval_rounds_used >= budget.max_retrieval_rounds:
                changed["completion_status"] = "completed_with_limits"
                changed["stop_reason"] = "max_retrieval_rounds"
                next_node = "generate"
            elif budget.tool_attempts_used >= budget.max_tool_attempts:
                changed["completion_status"] = "completed_with_limits"
                changed["stop_reason"] = "max_tool_attempts"
                next_node = "generate"
            elif self.followup_planner is None and not (
                check and check.followup_queries
            ):
                changed["completion_status"] = "completed_with_limits"
                changed["stop_reason"] = "no_followup_query"
                next_node = "generate"
            else:
                next_node = "plan_followup"
        return self._complete(changed, "check_evidence", next_node)

    def plan_followup(self, state: HarnessState) -> HarnessState:
        retrieved = self._retrieved(state)
        changed = self._copy(state)
        if self.followup_planner is None:
            raw_queries: Sequence[str] = (
                retrieved.evidence_check.followup_queries
                if retrieved.evidence_check is not None
                else []
            )
        else:
            raw_queries = self._invoke_planner(state, retrieved)
        queries = self._bounded_queries(raw_queries, allow_empty=True)
        if not queries:
            changed["completion_status"] = "completed_with_limits"
            changed["stop_reason"] = "no_followup_query"
            return self._complete(changed, "plan_followup", "generate")
        budget = self.persistence.get_budget(state["run_id"])
        self._sync_budget(changed, budget)
        if budget.retrieval_rounds_used >= budget.max_retrieval_rounds:
            changed["completion_status"] = "completed_with_limits"
            changed["stop_reason"] = "max_retrieval_rounds"
            return self._complete(changed, "plan_followup", "generate")
        changed["proposed_queries"] = queries
        return self._complete(changed, "plan_followup", "retrieve")

    def generate(self, state: HarnessState) -> HarnessState:
        self._fault("after_retrieval_checkpoint_before_generate", state)
        retrieved = self._retrieved(state)
        changed = self._copy(state)
        self.emit_event("generation.started", {})
        if (
            self.generate_enabled
            and retrieved.terminal_answer is None
            and self.persistence.has_outcome_unknown(
                state["run_id"], operation_name="generate_answer"
            )
            and not self.retry_unknown_external
        ):
            generated = self._generation_error(retrieved)
            changed["completion_status"] = "completed_with_limits"
            changed["stop_reason"] = "external_outcome_unknown"
        elif self.generate_enabled and retrieved.terminal_answer is None:
            generated = self._invoke_generator(state, retrieved)
        else:
            generated = self.assistant.generate_turn(
                retrieved,
                generate=False,
            )
        verified = self.assistant.verify_turn(generated)
        safe_result = safe_run_result_from_verified(
            self.execution_input,
            retrieved,
            verified,
        )
        payload = {
            "answer_text": safe_result.answer_text,
            "answer_payload": dict(safe_result.answer_payload),
            "evidence_payload": dict(safe_result.evidence_payload),
            "verification_payload": dict(safe_result.verification_payload),
        }
        artifact = self.persistence.save_node_artifact(
            state["run_id"],
            worker_id=self.worker_id,
            lease_epoch=self.lease_epoch,
            artifact_kind="verified_result",
            payload=payload,
        )
        changed["verification_result_ref"] = artifact.artifact_id
        changed["verification_result_hash"] = artifact.payload_hash
        changed["answer_draft_ref"] = None
        budget = self.persistence.get_budget(state["run_id"])
        self._sync_budget(changed, budget)
        if changed["completion_status"] is None:
            changed["completion_status"] = "succeeded"
        self.emit_event(
            "verification.completed",
            {
                "passed": True,
                "fallback_used": bool(
                    safe_result.verification_payload.get("fallback_used", False)
                ),
            },
        )
        return self._complete(changed, "generate", "verify")

    def verify(self, state: HarnessState) -> HarnessState:
        self._safe_result(state)
        return self._complete(self._copy(state), "verify", "persist_result")

    def persist_result(self, state: HarnessState) -> HarnessState:
        result = self._safe_result(state)
        status = state["completion_status"] or "succeeded"
        self.persistence.publish_terminal_result(
            state["run_id"],
            self.worker_id,
            self.lease_epoch,
            result,
            completion_status=status,
            stop_reason=state["stop_reason"],
        )
        self._fault("after_final_answer_stored_before_graph_finish", state)
        changed = self._copy(state)
        changed["completion_status"] = status
        return self._complete(changed, "persist_result", None)

    def _invoke_planner(
        self,
        state: HarnessState,
        retrieved: RetrievedTurn,
    ) -> Sequence[str]:
        budget = self.persistence.get_budget(state["run_id"])
        attempt = 0
        while True:
            attempt += 1
            reservation = self.persistence.reserve_attempt(
                state["run_id"],
                worker_id=self.worker_id,
                lease_epoch=self.lease_epoch,
                operation_key=f"planner-round-{budget.retrieval_rounds_used + 1}",
                operation_kind="model",
                operation_name="plan_followup",
                request_hash=canonical_hash(
                    {
                        "evidence_hashes": state["immutable_evidence_hashes"],
                        "round": budget.retrieval_rounds_used,
                    }
                ),
            )
            self.persistence.mark_attempt_dispatched(
                reservation.attempt_id,
                worker_id=self.worker_id,
                lease_epoch=self.lease_epoch,
            )
            try:
                assert self.followup_planner is not None
                output = self.followup_planner(state, retrieved)
            except Exception as error:
                decision = retry_decision(error)
                status = (
                    "outcome_unknown"
                    if decision.error_code == "timeout"
                    else "failed"
                )
                self.persistence.finish_attempt(
                    reservation.attempt_id,
                    worker_id=self.worker_id,
                    lease_epoch=self.lease_epoch,
                    status=status,
                    retryable=decision.retryable,
                    error_code=decision.error_code,
                    possible_duplicate_cost=status == "outcome_unknown",
                )
                if not retry_allowed(
                    decision,
                    attempt_no=attempt,
                    maximum_retries=budget.max_retry_per_operation,
                ):
                    raise ExecutionFailure(
                        code=f"provider_{decision.error_code}",
                        stage="plan_followup",
                        retryable=decision.retryable,
                    ) from None
                continue
            self.persistence.finish_attempt(
                reservation.attempt_id,
                worker_id=self.worker_id,
                lease_epoch=self.lease_epoch,
                status="succeeded",
                result_hash=canonical_hash(list(output)),
            )
            return output

    def _invoke_generator(
        self,
        state: HarnessState,
        retrieved: RetrievedTurn,
    ) -> GeneratedTurn:
        budget = self.persistence.get_budget(self.execution_input.run_id)
        attempt = 0
        while True:
            attempt += 1
            reservation = self.persistence.reserve_attempt(
                self.execution_input.run_id,
                worker_id=self.worker_id,
                lease_epoch=self.lease_epoch,
                operation_key="generate-answer",
                operation_kind="model",
                operation_name="generate_answer",
                request_hash=canonical_hash(
                    {
                        "question_hash": query_hash(
                            retrieved.prepared.standalone_question
                        ),
                        "evidence_hashes": [
                            canonical_hash(search_result_to_artifact(item))
                            for item in retrieved.results
                        ],
                    }
                ),
            )
            self.persistence.mark_attempt_dispatched(
                reservation.attempt_id,
                worker_id=self.worker_id,
                lease_epoch=self.lease_epoch,
            )
            self._fault("after_model_attempt_dispatched", state)
            try:
                generated = self.assistant.generate_turn(retrieved, generate=True)
            except Exception as error:
                decision = retry_decision(error)
                status = (
                    "outcome_unknown"
                    if decision.error_code == "timeout"
                    else "failed"
                )
                self.persistence.finish_attempt(
                    reservation.attempt_id,
                    worker_id=self.worker_id,
                    lease_epoch=self.lease_epoch,
                    status=status,
                    retryable=decision.retryable,
                    error_code=decision.error_code,
                    possible_duplicate_cost=status == "outcome_unknown",
                )
                if not retry_allowed(
                    decision,
                    attempt_no=attempt,
                    maximum_retries=budget.max_retry_per_operation,
                ):
                    raise ExecutionFailure(
                        code=f"provider_{decision.error_code}",
                        stage="generation",
                        retryable=decision.retryable,
                    ) from None
                continue
            self.persistence.finish_attempt(
                reservation.attempt_id,
                worker_id=self.worker_id,
                lease_epoch=self.lease_epoch,
                status="succeeded",
                result_hash=canonical_hash(
                    {
                        "kind": generated.kind,
                        "answer_text_hash": canonical_hash(generated.answer_text),
                    }
                ),
            )
            return generated

    def _invoke_initial_retrieval(
        self,
        state: HarnessState,
        prepared: PreparedQuestion,
        *,
        query: str,
    ) -> RetrievedTurn:
        budget = self.persistence.get_budget(state["run_id"])
        attempt_no = 0
        while True:
            attempt_no += 1
            reservation = self._reserve_tool(query, round_number=1)
            self.persistence.mark_attempt_dispatched(
                reservation.attempt_id,
                worker_id=self.worker_id,
                lease_epoch=self.lease_epoch,
            )
            try:
                retrieved = self.assistant.retrieve_turn(
                    prepared,
                    max_followup_rounds=0,
                )
            except Exception as error:
                decision = self._finish_failed_external_attempt(
                    reservation,
                    error,
                )
                if not retry_allowed(
                    decision,
                    attempt_no=attempt_no,
                    maximum_retries=budget.max_retry_per_operation,
                ):
                    raise ExecutionFailure(
                        code=f"provider_{decision.error_code}",
                        stage="retrieval",
                        retryable=decision.retryable,
                    ) from None
                continue
            self.persistence.finish_attempt(
                reservation.attempt_id,
                worker_id=self.worker_id,
                lease_epoch=self.lease_epoch,
                status="succeeded",
                result_hash=canonical_hash(
                    [search_result_to_artifact(item) for item in retrieved.results]
                ),
            )
            return retrieved

    def _invoke_followup_retrieval(
        self,
        state: HarnessState,
        *,
        query: str,
        round_number: int,
    ) -> list[SearchResult]:
        budget = self.persistence.get_budget(state["run_id"])
        attempt_no = 0
        while True:
            attempt_no += 1
            reservation = self._reserve_tool(query, round_number=round_number)
            self.persistence.mark_attempt_dispatched(
                reservation.attempt_id,
                worker_id=self.worker_id,
                lease_epoch=self.lease_epoch,
            )
            try:
                items = list(
                    self.assistant.retriever.retrieve(
                        query,
                        top_k=self.assistant.top_k,
                    )
                )
            except Exception as error:
                decision = self._finish_failed_external_attempt(
                    reservation,
                    error,
                )
                if not retry_allowed(
                    decision,
                    attempt_no=attempt_no,
                    maximum_retries=budget.max_retry_per_operation,
                ):
                    raise ExecutionFailure(
                        code=f"provider_{decision.error_code}",
                        stage="retrieval",
                        retryable=decision.retryable,
                    ) from None
                continue
            self.persistence.finish_attempt(
                reservation.attempt_id,
                worker_id=self.worker_id,
                lease_epoch=self.lease_epoch,
                status="succeeded",
                result_hash=canonical_hash(
                    [search_result_to_artifact(item) for item in items]
                ),
            )
            return items

    def _finish_failed_external_attempt(
        self,
        reservation: AttemptReservation,
        error: Exception,
    ) -> RetryDecision:
        decision = retry_decision(error)
        status = "outcome_unknown" if decision.error_code == "timeout" else "failed"
        self.persistence.finish_attempt(
            reservation.attempt_id,
            worker_id=self.worker_id,
            lease_epoch=self.lease_epoch,
            status=status,
            retryable=decision.retryable,
            error_code=decision.error_code,
            possible_duplicate_cost=status == "outcome_unknown",
        )
        return decision

    def _reserve_tool(self, query: str, *, round_number: int) -> AttemptReservation:
        digest = query_hash(query)
        return self.persistence.reserve_attempt(
            self.execution_input.run_id,
            worker_id=self.worker_id,
            lease_epoch=self.lease_epoch,
            operation_key=f"retrieve-{round_number}-{digest[:16]}",
            operation_kind="tool",
            operation_name="search_laws",
            request_hash=digest,
        )

    def _prepared(self, state: HarnessState):
        prepared = self.assistant.prepare_question(state["question"])
        if state["analysis"] and prepared.analysis.to_dict() != state["analysis"]:
            raise ExecutionFailure(
                code="checkpoint_incompatible",
                stage="analysis",
            )
        return prepared

    def _retrieved(self, state: HarnessState) -> RetrievedTurn:
        artifact_id = state["retrieved_artifact_ref"]
        artifact_hash = state["retrieved_artifact_hash"]
        if artifact_id is None or artifact_hash is None:
            raise ExecutionFailure(code="missing_retrieval_artifact", stage="checkpoint")
        payload = self.persistence.load_node_artifact(
            state["run_id"],
            artifact_id,
            expected_kind="retrieved_turn",
            expected_hash=artifact_hash,
        )
        return retrieved_turn_from_artifact(payload, prepared=self._prepared(state))

    def _safe_result(self, state: HarnessState) -> SafeRunResult:
        artifact_id = state["verification_result_ref"]
        artifact_hash = state["verification_result_hash"]
        if artifact_id is None or artifact_hash is None:
            raise ExecutionFailure(code="missing_verified_result", stage="checkpoint")
        payload = self.persistence.load_node_artifact(
            state["run_id"],
            artifact_id,
            expected_kind="verified_result",
            expected_hash=artifact_hash,
        )
        return SafeRunResult(
            answer_text=payload["answer_text"],
            answer_payload=payload["answer_payload"],
            evidence_payload=payload["evidence_payload"],
            verification_payload=payload["verification_payload"],
        )

    @staticmethod
    def _generation_error(retrieved: RetrievedTurn) -> GeneratedTurn:
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

    def _bounded_queries(
        self,
        raw_queries: Sequence[str],
        *,
        allow_empty: bool = False,
    ) -> list[str]:
        if isinstance(raw_queries, (str, bytes)) or not isinstance(
            raw_queries, Sequence
        ):
            raise ExecutionFailure(code="invalid_planner_output", stage="plan_followup")
        budget = self.persistence.get_budget(self.execution_input.run_id)
        queries: list[str] = []
        for raw in raw_queries:
            if not isinstance(raw, str):
                raise ExecutionFailure(
                    code="invalid_planner_output",
                    stage="plan_followup",
                )
            query = " ".join(raw.split())
            if not query or len(query) > 8_000:
                raise ExecutionFailure(
                    code="invalid_planner_output",
                    stage="plan_followup",
                )
            if query not in queries:
                queries.append(query)
            if len(queries) >= budget.max_queries_per_round:
                break
        if not queries and not allow_empty:
            raise ExecutionFailure(code="missing_followup_query", stage="retrieve")
        return queries

    @staticmethod
    def _copy(state: HarnessState) -> HarnessState:
        return validate_harness_state(dict(state))

    @staticmethod
    def _sync_budget(state: HarnessState, budget: BudgetSnapshot) -> None:
        state["retrieval_rounds_used"] = budget.retrieval_rounds_used
        state["model_attempts_used"] = budget.model_attempts_used
        state["tool_attempts_used"] = budget.tool_attempts_used
        state["embedding_attempts_used"] = budget.embedding_attempts_used

    @staticmethod
    def _complete(
        state: HarnessState,
        node: str,
        next_node: str | None,
    ) -> HarnessState:
        state["last_completed_node"] = node
        state["checkpoint_id"] = checkpoint_marker()
        state["next_node"] = next_node
        return validate_harness_state(state)

    def _fence(self) -> None:
        self.persistence.assert_execution_fence(
            self.execution_input.run_id,
            self.worker_id,
            self.lease_epoch,
        )

    def _fault(self, point: str, state: HarnessState) -> None:
        if self.fault_hook is not None:
            self.fault_hook(point, validate_harness_state(dict(state)))

__all__ = [
    "BoundedHarnessNodes",
    "FaultHook",
    "FollowupPlanner",
    "HarnessPersistence",
    "NodeArtifactRef",
]
