from __future__ import annotations

import json
from collections.abc import Sequence

import pytest
from sqlalchemy import Engine, select

from integration_tests.m5_support import (
    create_run_case,
    ensure_persistent_checkpointer,
    record_scenario,
)
from legal_rag.chat import LegalChatAssistant
from legal_rag.harness.budget import HarnessBudgetConfig
from legal_rag.harness.runner import GraphRunExecutor
from legal_rag.models import VerificationContext
from legal_rag.provider_errors import ProviderCallError
from legal_rag.retrieval import BM25Retriever
from legal_rag.retrieval_contracts import RetrievalBoundary
from legal_rag.services.run_executor import ExecutionFailure, RunExecutionInput
from legal_rag.services.run_service import RunService
from legal_rag.services.service_retrieval import PostgresAssistantFactory
from legal_rag.storage.retrieval import BoundCorpus, BoundaryBoundRetriever
from legal_rag.storage.schema import run_external_attempts


class _NeverCompletionClient:
    def complete(self, prompt: str) -> str:
        del prompt
        raise AssertionError("limited graph execution must not call a model")


class _ScriptedCompletionClient:
    hidden_retries_disabled = True
    propagate_control_errors = True

    def __init__(self, outcomes: Sequence[BaseException | str]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def complete(self, prompt: str) -> str:
        del prompt
        self.calls += 1
        if not self.outcomes:
            raise AssertionError("completion script was exhausted")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome != "success":
            raise AssertionError("unknown completion outcome")
        return json.dumps(
            {
                "answer_text": "服务端测试法第一条要求遵守测试义务 [S1]。",
                "answer_mode": "evidence_answer",
                "claims": [
                    {
                        "claim_id": "C1",
                        "text": "服务端测试法第一条要求遵守测试义务",
                        "source_ids": ["S1"],
                    }
                ],
                "limitations": [],
                "clarification_question": None,
            },
            ensure_ascii=False,
        )


def _empty_assistant(execution: RunExecutionInput) -> LegalChatAssistant:
    boundary = RetrievalBoundary(
        scope_id=execution.scope_id,
        snapshot_id=execution.snapshot_id,
        profile_id=execution.profile_id,
    )
    assert boundary.fingerprint == execution.boundary_fingerprint
    corpus = BoundCorpus(boundary=boundary, entries=())
    retriever = BoundaryBoundRetriever(BM25Retriever([]), corpus=corpus)
    return LegalChatAssistant(
        retriever,
        model="m5-provider-free-empty",
        completion_client=_NeverCompletionClient(),
        verification_context=VerificationContext(
            snapshot_id=execution.snapshot_id,
            allowed_scope_ids=[execution.scope_id],
        ),
    )


def _run_claimed_graph(
    service: RunService,
    *,
    run_id: str,
    worker: str,
    assistant_factory,
    generate: bool,
    followup_planner=None,
    retry_unknown_external: bool = False,
) -> int:
    claimed = service.claim_next_run(worker, lease_seconds=30)
    assert claimed is not None and claimed.run_id == run_id
    frozen = service.load_execution_input(
        run_id,
        worker,
        lease_epoch=claimed.lease_epoch,
    )
    GraphRunExecutor(
        service,
        assistant_factory,
        generate=generate,
        followup_planner=followup_planner,
        retry_unknown_external=retry_unknown_external,
    ).execute_claimed(frozen, worker, lambda event_type, payload: None)
    return claimed.lease_epoch


def _scripted_factory(engine: Engine, client: _ScriptedCompletionClient):
    postgres_factory = PostgresAssistantFactory(engine)

    def create(execution: RunExecutionInput) -> LegalChatAssistant:
        assistant = postgres_factory(execution)
        assistant.llm = client
        return assistant

    return create


def _attempt_rows(engine: Engine, run_id: str):
    with engine.connect() as connection:
        return (
            connection.execute(
                select(run_external_attempts)
                .where(run_external_attempts.c.run_id == run_id)
                .order_by(
                    run_external_attempts.c.operation_key,
                    run_external_attempts.c.attempt_no,
                )
            )
            .mappings()
            .all()
        )


def test_m5_t04_adversarial_followup_requests_stop_at_durable_global_budgets(
    migrated_engine: Engine,
) -> None:
    ensure_persistent_checkpointer(migrated_engine)
    budget_config = HarnessBudgetConfig(
        max_retrieval_rounds=2,
        max_queries_per_round=1,
        max_tool_attempts=2,
        max_model_attempts=2,
        max_embedding_attempts=0,
        max_retry_per_operation=1,
        execution_deadline_seconds=90,
        evidence_top_k=3,
    )
    service = RunService(migrated_engine, budget_config=budget_config)
    case = create_run_case(
        migrated_engine,
        service=service,
        case_name="t04-adversarial-followup",
    )
    planner_calls: list[int] = []

    def adversarial_planner(state, retrieved):
        del retrieved
        planner_calls.append(int(state["retrieval_rounds_used"]))
        return [f"adversarial followup {index}" for index in range(100)]

    _run_claimed_graph(
        service,
        run_id=case.run_id,
        worker="m5-t04-graph-worker",
        assistant_factory=_empty_assistant,
        generate=False,
        followup_planner=adversarial_planner,
    )
    terminal = service.get_run(case.boundary.owner, case.run_id)
    durable = RunService(migrated_engine).get_budget(case.run_id)
    assert terminal.status == "completed_with_limits"
    assert terminal.stop_reason == "max_retrieval_rounds"
    assert planner_calls == [1]
    assert durable.retrieval_rounds_used == 2
    assert durable.queries_used == 2
    assert durable.tool_attempts_used == 2
    assert durable.model_attempts_used == 1
    attempts = _attempt_rows(migrated_engine, case.run_id)
    assert [row["operation_kind"] for row in attempts].count("tool") == 2
    assert [row["operation_name"] for row in attempts].count("plan_followup") == 1
    record_scenario(
        migrated_engine,
        "M5-T04",
        {
            "global_budget_enforced": True,
            "durable_ledger": True,
            "graph_loop_exercised": True,
        },
    )


@pytest.mark.parametrize(
    "first_error",
    [
        ProviderCallError(
            "rate_limited",
            provider="fixture",
            operation="completion",
            status_code=429,
        ),
        TimeoutError("synthetic controlled timeout"),
    ],
    ids=["http-429", "timeout"],
)
def test_m5_t05_transient_429_and_timeout_retry_once_with_global_budget(
    migrated_engine: Engine,
    first_error: BaseException,
) -> None:
    ensure_persistent_checkpointer(migrated_engine)
    service = RunService(
        migrated_engine,
        budget_config=HarnessBudgetConfig(
            max_retrieval_rounds=1,
            max_queries_per_round=1,
            max_tool_attempts=2,
            max_model_attempts=2,
            max_embedding_attempts=0,
            max_retry_per_operation=1,
            execution_deadline_seconds=90,
            evidence_top_k=3,
        ),
    )
    case = create_run_case(
        migrated_engine,
        service=service,
        case_name=f"t05-transient-{type(first_error).__name__}",
        real_corpus=True,
    )
    client = _ScriptedCompletionClient([first_error, "success"])
    _run_claimed_graph(
        service,
        run_id=case.run_id,
        worker=f"m5-t05-transient-{type(first_error).__name__}",
        assistant_factory=_scripted_factory(migrated_engine, client),
        generate=True,
        retry_unknown_external=True,
    )
    terminal = service.get_run(case.boundary.owner, case.run_id)
    attempts = [
        row
        for row in _attempt_rows(migrated_engine, case.run_id)
        if row["operation_kind"] == "model"
    ]
    assert terminal.status == "succeeded"
    assert client.calls == 2
    assert [row["attempt_no"] for row in attempts] == [1, 2]
    assert [row["status"] for row in attempts] in (
        ["failed", "succeeded"],
        ["outcome_unknown", "succeeded"],
    )
    assert all(row["reserved_at"] <= row["dispatched_at"] for row in attempts)
    assert service.get_budget(case.run_id).model_attempts_used == 2
    record_scenario(
        migrated_engine,
        "M5-T05",
        {
            "transient_retry_bounded": True,
            "attempts_reserved_before_dispatch": True,
            "node_retry_path_exercised": True,
        },
    )


@pytest.mark.parametrize(
    "error",
    [
        ProviderCallError(
            "invalid_request",
            provider="fixture",
            operation="completion",
            status_code=400,
        ),
        ProviderCallError(
            "auth",
            provider="fixture",
            operation="completion",
            status_code=401,
        ),
    ],
    ids=["http-400", "http-401"],
)
def test_m5_t05_400_and_401_are_terminal_without_retry(
    migrated_engine: Engine,
    error: ProviderCallError,
) -> None:
    ensure_persistent_checkpointer(migrated_engine)
    service = RunService(migrated_engine)
    case = create_run_case(
        migrated_engine,
        service=service,
        case_name=f"t05-permanent-{error.error_code}",
        real_corpus=True,
    )
    client = _ScriptedCompletionClient([error])
    with pytest.raises(ExecutionFailure, match=f"provider_{error.error_code}"):
        _run_claimed_graph(
            service,
            run_id=case.run_id,
            worker=f"m5-t05-permanent-{error.error_code}",
            assistant_factory=_scripted_factory(migrated_engine, client),
            generate=True,
        )
    attempts = [
        row
        for row in _attempt_rows(migrated_engine, case.run_id)
        if row["operation_kind"] == "model"
    ]
    assert client.calls == 1
    assert len(attempts) == 1
    assert attempts[0]["attempt_no"] == 1
    assert attempts[0]["status"] == "failed"
    assert attempts[0]["retryable"] is False
    assert service.get_budget(case.run_id).model_attempts_used == 1
    service.cancel_run(case.boundary.owner, case.run_id)
    record_scenario(
        migrated_engine,
        "M5-T05",
        {
            "permanent_error_retried": False,
            "attempts_reserved_before_dispatch": True,
            "node_retry_path_exercised": True,
        },
    )
