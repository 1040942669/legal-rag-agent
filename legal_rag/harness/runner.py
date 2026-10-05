from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from legal_rag.chat import LegalChatAssistant, RetrievedTurn
from legal_rag.observability.events import Observer
from legal_rag.services.run_executor import (
    AssistantFactory,
    ExecutionFailure,
    RunExecutionInput,
    SafeRunResult,
    SafeStageCallback,
    restore_completed_history,
)
from legal_rag.services.run_service import FrozenRunInput, RunService

from .budget import BudgetExhausted
from .checkpoint import FencedPostgresSaver, checkpoint_namespace, checkpoint_pool
from .graph import build_bounded_graph
from .nodes import BoundedHarnessNodes, FaultHook
from .observations import HarnessObservationAdapter
from .state import (
    HARNESS_GRAPH_VERSION,
    HARNESS_STATE_SCHEMA_VERSION,
    HarnessState,
    new_harness_state,
    validate_harness_state,
)

FollowupPlanner = Callable[[HarnessState, RetrievedTurn], Sequence[str]]


@dataclass(slots=True)
class GraphRunExecutor:
    """Run the M5 graph from the last trusted JSON projection.

    Every lease epoch gets its own LangGraph namespace.  Recovery seeds that
    namespace from the application-owned checkpoint projection, so a stale
    writer in an older namespace cannot become the trusted resume source.
    """

    service: RunService
    assistant_factory: AssistantFactory
    generate: bool = False
    followup_planner: FollowupPlanner | None = None
    retry_unknown_external: bool = False
    fault_hook: FaultHook | None = None
    observer: Observer | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.service, RunService):
            raise TypeError("service must be a RunService")
        if not callable(self.assistant_factory):
            raise TypeError("assistant_factory must be callable")
        if not isinstance(self.generate, bool):
            raise TypeError("generate must be a boolean")
        if self.followup_planner is not None and not callable(self.followup_planner):
            raise TypeError("followup_planner must be callable")
        if not isinstance(self.retry_unknown_external, bool):
            raise TypeError("retry_unknown_external must be a boolean")
        if self.fault_hook is not None and not callable(self.fault_hook):
            raise TypeError("fault_hook must be callable")

    def execute_claimed(
        self,
        frozen: FrozenRunInput,
        worker_id: str,
        emit_event: SafeStageCallback,
    ) -> None:
        if not isinstance(frozen, FrozenRunInput):
            raise ExecutionFailure(code="invalid_execution_input", stage="input")
        if frozen.graph_version != HARNESS_GRAPH_VERSION:
            raise ExecutionFailure(
                code="checkpoint_incompatible",
                stage="checkpoint",
                retryable=False,
            )
        if frozen.state_schema_version != HARNESS_STATE_SCHEMA_VERSION:
            raise ExecutionFailure(
                code="checkpoint_incompatible",
                stage="checkpoint",
                retryable=False,
            )
        if not callable(emit_event):
            raise ExecutionFailure(code="invalid_event_callback", stage="input")

        execution_input = frozen.to_execution_input()
        if execution_input.execution_policy is not None:
            persisted_policy = self.service.get_execution_policy(frozen.run_id)
            if persisted_policy.to_dict() != dict(execution_input.execution_policy):
                raise ExecutionFailure(code="checkpoint_incompatible", stage="execution_policy")
        if self._deadline_expired(frozen.execution_deadline_at):
            self.service.publish_terminal_result(
                frozen.run_id,
                worker_id,
                frozen.lease_epoch,
                self._limited_result(execution_input, "deadline_exceeded"),
                completion_status="completed_with_limits",
                stop_reason="deadline_exceeded",
            )
            return

        checkpoint = self.service.load_trusted_checkpoint(frozen.run_id)
        state = checkpoint or self._new_state(frozen)
        self._assert_frozen_state(frozen, state)

        assistant = self.assistant_factory(execution_input)
        if not isinstance(assistant, LegalChatAssistant):
            raise ExecutionFailure(
                code="invalid_assistant_factory",
                stage="assistant_factory",
            )
        generate_enabled = self.generate
        if execution_input.execution_policy is not None:
            from legal_rag.services.execution_policy import ServiceExecutionPolicy
            from legal_rag.services.governed_calls import GovernedCompletionClient
            from legal_rag.semantic import CompletionSemanticChecker
            policy = ServiceExecutionPolicy.from_dict(dict(execution_input.execution_policy))
            generate_enabled = policy.generation.enabled
            assistant.semantic_policy = policy.semantic_policy
            assistant.semantic_checker = None
            if self.followup_planner is not None:
                # A default-off modern policy is not permission for a plugin
                # to bypass destination, money, usage or unknown-result rules.
                # Existing deterministic followup_queries need no such client.
                raise ExecutionFailure(code="ungoverned_planner_disabled", stage="assistant_factory")
            if generate_enabled:
                assistant.llm = GovernedCompletionClient(
                    assistant.llm, service=self.service, policy=policy.generation,
                    run_id=frozen.run_id, worker_id=worker_id, lease_epoch=frozen.lease_epoch,
                    fault_hook=(lambda point: self.fault_hook(point, state)) if self.fault_hook else None,
                )
                if policy.semantic_policy is not None:
                    assistant.semantic_checker = CompletionSemanticChecker(
                        policy.semantic_policy,
                        assistant.llm.operation_view("semantic_check", identity=policy.semantic_policy.fingerprint),
                    )
        restore_completed_history(assistant, execution_input)
        observation_adapter = (
            HarnessObservationAdapter(
                self.observer,
                run_id=frozen.run_id,
                session_id=frozen.session_id,
                persistence=self.service,
                completion_client=assistant.llm,
            )
            if self.observer is not None
            else None
        )
        if getattr(assistant.llm, "governed", False):
            assistant.llm.set_observation_adapter(observation_adapter)
        nodes = BoundedHarnessNodes(
            assistant=assistant,
            execution_input=execution_input,
            persistence=self.service,
            worker_id=worker_id,
            lease_epoch=frozen.lease_epoch,
            emit_event=emit_event,
            generate_enabled=generate_enabled,
            followup_planner=self.followup_planner,
            retry_unknown_external=self.retry_unknown_external,
            fault_hook=self.fault_hook,
            observation_adapter=observation_adapter,
        )
        namespace = checkpoint_namespace(
            graph_version=frozen.graph_version,
            schema_version=frozen.state_schema_version,
            lease_epoch=frozen.lease_epoch,
        )
        try:
            with checkpoint_pool(self.service.engine) as pool:
                saver = FencedPostgresSaver(
                    pool,
                    fence=self.service,
                    run_id=frozen.run_id,
                    worker_id=worker_id,
                    lease_epoch=frozen.lease_epoch,
                    namespace=namespace,
                )
                graph = build_bounded_graph(
                    checkpointer=saver,
                    nodes=nodes,
                    node_wrapper=observation_adapter.wrap_node
                    if observation_adapter
                    else None,
                )
                final_envelope = graph.invoke(
                    {"payload": state},
                    {
                        "configurable": {
                            "thread_id": saver.thread_id,
                            "checkpoint_ns": "",
                        }
                    },
                    durability="sync",
                )
            resolved = validate_harness_state(dict(final_envelope["payload"]))
            if resolved["last_completed_node"] != "persist_result":
                raise ExecutionFailure(
                    code="graph_incomplete",
                    stage="checkpoint",
                )
        except BudgetExhausted as exc:
            self.service.publish_terminal_result(
                frozen.run_id,
                worker_id,
                frozen.lease_epoch,
                self._limited_result(execution_input, exc.stop_reason),
                completion_status="completed_with_limits",
                stop_reason=exc.stop_reason,
            )

    @staticmethod
    def _new_state(frozen: FrozenRunInput) -> HarnessState:
        return new_harness_state(
            run_id=frozen.run_id,
            session_id=frozen.session_id,
            user_id=frozen.user_id,
            graph_version=frozen.graph_version,
            retrieval_config_hash=frozen.retrieval_config_hash,
            question=frozen.question,
            bounded_history_refs=[item.message_id for item in frozen.completed_history],
            snapshot_id=frozen.snapshot_id,
            embedding_profile_id=frozen.profile_id,
            execution_deadline_at=frozen.execution_deadline_at,
        )

    @staticmethod
    def _assert_frozen_state(
        frozen: FrozenRunInput,
        state: HarnessState,
    ) -> None:
        expected: dict[str, Any] = {
            "run_id": frozen.run_id,
            "session_id": frozen.session_id,
            "user_id": frozen.user_id,
            "schema_version": frozen.state_schema_version,
            "graph_version": frozen.graph_version,
            "retrieval_config_hash": frozen.retrieval_config_hash,
            "question": frozen.question,
            "snapshot_id": frozen.snapshot_id,
            "embedding_profile_id": frozen.profile_id,
            "execution_deadline_at": frozen.execution_deadline_at.astimezone(
                timezone.utc
            ).isoformat(),
        }
        for field, value in expected.items():
            if state[field] != value:
                raise ExecutionFailure(
                    code="checkpoint_incompatible",
                    stage="checkpoint",
                    retryable=False,
                )

    @staticmethod
    def _deadline_expired(deadline: datetime) -> bool:
        normalized = (
            deadline
            if deadline.tzinfo is not None
            else deadline.replace(tzinfo=timezone.utc)
        )
        return normalized <= datetime.now(timezone.utc)

    @staticmethod
    def _limited_result(
        execution_input: RunExecutionInput,
        reason: str,
    ) -> SafeRunResult:
        answer_text = (
            "本次运行已达到服务端执行限制，未继续发起外部调用。"
            if reason != "deadline_exceeded"
            else "本次运行已超过绝对执行截止时间，未继续发起外部调用。"
        )
        return SafeRunResult(
            answer_text=answer_text,
            answer_payload={
                "answer_text": answer_text,
                "answer_mode": "insufficient_evidence",
                "claims": [],
                "limitations": [reason],
                "clarification_question": None,
            },
            evidence_payload={
                "schema_version": 1,
                **execution_input.boundary_metadata,
                "result_count": 0,
                "rejected_count": 0,
                "terminal_kind": "execution_limit",
                "evidence_check": {
                    "sufficient": False,
                    "checked_result_count": 0,
                    "stop_reason": reason,
                },
                "sources": [],
            },
            verification_payload={
                "passed": True,
                "performed": False,
                "fallback_used": False,
                "mode": "programmatic_limit",
                "reason_codes": [reason],
            },
        )


__all__ = ["FollowupPlanner", "GraphRunExecutor"]
