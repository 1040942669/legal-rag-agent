from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import func, select

from legal_rag.chat import LegalChatAssistant
from legal_rag.harness.checkpoint import (
    PersistentCheckpointerRequired,
    require_persistent_checkpointer,
)
from legal_rag.harness.runner import GraphRunExecutor
from legal_rag.models import VerificationContext
from legal_rag.retrieval import BM25Retriever
from legal_rag.retrieval_contracts import RetrievalBoundary
from legal_rag.services.run_executor import (
    DeterministicRunExecutor,
    SafeRunResult,
)
from legal_rag.services.run_service import RunService
from legal_rag.services.service_retrieval import PostgresAssistantFactory
from legal_rag.storage.database import DatabaseSettings, create_database_engine
from legal_rag.storage.retrieval import BoundCorpus, BoundaryBoundRetriever
from legal_rag.storage.schema import (
    messages,
    run_external_attempts,
    run_results,
    runs,
)


def _write_output(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, path)


def _hang() -> None:
    while True:
        time.sleep(1.0)


def _worker(phase: str) -> str:
    return f"{phase}-{os.getpid()}"


def _claim(service: RunService, phase: str):
    worker_id = _worker(phase)
    run = service.claim_next_run(worker_id, lease_seconds=30)
    if run is None:
        raise AssertionError("expected one claimable M5 run")
    return worker_id, run


def _deterministic_result(service: RunService, run_id: str, worker: str, epoch: int):
    frozen = service.load_execution_input(
        run_id,
        worker,
        lease_epoch=epoch,
    )
    result = DeterministicRunExecutor("M5 provider-free verified result").execute(
        frozen.to_execution_input(),
        lambda event_type, safe_payload: None,
    )
    return frozen, result


def _graph_executor(
    service: RunService,
    *,
    fault_hook=None,
    generate: bool = False,
) -> GraphRunExecutor:
    return GraphRunExecutor(
        service,
        PostgresAssistantFactory(service.engine),
        generate=generate,
        fault_hook=fault_hook,
    )


def _empty_assistant(execution) -> LegalChatAssistant:
    boundary = RetrievalBoundary(
        scope_id=execution.scope_id,
        snapshot_id=execution.snapshot_id,
        profile_id=execution.profile_id,
    )
    if boundary.fingerprint != execution.boundary_fingerprint:
        raise AssertionError("empty assistant boundary mismatch")
    corpus = BoundCorpus(boundary=boundary, entries=())
    retriever = BoundaryBoundRetriever(BM25Retriever([]), corpus=corpus)

    class RejectCompletionClient:
        def complete(self, prompt: str) -> str:
            del prompt
            raise AssertionError("limited planner graph must not call a generator")

    return LegalChatAssistant(
        retriever,
        model="m5-provider-free-empty",
        completion_client=RejectCompletionClient(),
        verification_context=VerificationContext(
            snapshot_id=execution.snapshot_id,
            allowed_scope_ids=[execution.scope_id],
        ),
    )


def _execute_graph(
    service: RunService,
    *,
    run_id: str,
    worker: str,
    lease_epoch: int,
    fault_hook=None,
    generate: bool = False,
) -> None:
    frozen = service.load_execution_input(
        run_id,
        worker,
        lease_epoch=lease_epoch,
    )

    def emit_event(event_type, safe_payload) -> None:
        service.append_stage_event(
            run_id,
            event_type,
            safe_payload,
            worker_id=worker,
            lease_epoch=lease_epoch,
        )

    _graph_executor(
        service,
        fault_hook=fault_hook,
        generate=generate,
    ).execute_claimed(
        frozen,
        worker,
        emit_event,
    )


def _t01_first(service: RunService, run_id: str, output: Path) -> None:
    worker, run = _claim(service, "m5-t01-first")
    if run.run_id != run_id:
        raise AssertionError("claimed an unexpected run")

    def fault(point, state) -> None:
        del state
        if point != "after_retrieval_checkpoint_before_generate":
            return
        budget = service.get_budget(run_id)
        _write_output(
            output,
            {
                "pid": os.getpid(),
                "checkpoint_committed": True,
                "retrieval_invocations": budget.retrieval_rounds_used,
            },
        )
        _hang()

    _execute_graph(
        service,
        run_id=run_id,
        worker=worker,
        lease_epoch=run.lease_epoch,
        fault_hook=fault,
    )


def _t01_resume(service: RunService, run_id: str, output: Path) -> None:
    worker, run = _claim(service, "m5-t01-resume")
    if run.run_id != run_id:
        raise AssertionError("claimed an unexpected resumed run")
    state = service.load_trusted_checkpoint(run_id)
    if state is None or state["next_node"] != "generate":
        raise AssertionError("retrieval checkpoint was not durable")
    budget = service.get_budget(run_id)
    if budget.retrieval_rounds_used != 1 or budget.tool_attempts_used != 1:
        raise AssertionError("durable retrieval budget was not reused")
    _execute_graph(
        service,
        run_id=run_id,
        worker=worker,
        lease_epoch=run.lease_epoch,
    )
    _write_output(
        output,
        {
            "pid": os.getpid(),
            "retrieval_reused": True,
            "retrieval_invocations": budget.retrieval_rounds_used,
        },
    )


def _t02_first(service: RunService, run_id: str, output: Path) -> None:
    worker, run = _claim(service, "m5-t02-first")
    if run.run_id != run_id:
        raise AssertionError("claimed an unexpected run")
    frozen = service.load_execution_input(
        run_id,
        worker,
        lease_epoch=run.lease_epoch,
    )

    class BlockingProviderFreeClient:
        hidden_retries_disabled = True
        propagate_control_errors = True

        def complete(self, prompt: str) -> str:
            del prompt
            budget = service.get_budget(run_id)
            _write_output(
                output,
                {
                    "pid": os.getpid(),
                    "dispatch_observed": True,
                    "provider_call_observed": True,
                    "attempt_no": budget.model_attempts_used,
                },
            )
            _hang()
            raise AssertionError("unreachable")

    postgres_factory = PostgresAssistantFactory(service.engine)

    def assistant_factory(execution):
        assistant = postgres_factory(execution)
        assistant.llm = BlockingProviderFreeClient()
        return assistant

    GraphRunExecutor(
        service,
        assistant_factory,
        generate=True,
    ).execute_claimed(
        frozen,
        worker,
        lambda event_type, safe_payload: service.append_stage_event(
            run_id,
            event_type,
            safe_payload,
            worker_id=worker,
            lease_epoch=run.lease_epoch,
        ),
    )


def _t02_resume(service: RunService, run_id: str, output: Path) -> None:
    worker, run = _claim(service, "m5-t02-resume")
    if run.run_id != run_id:
        raise AssertionError("claimed an unexpected resumed run")
    if not service.has_outcome_unknown(run_id, operation_name="generate_answer"):
        raise AssertionError("dispatched attempt was not classified outcome_unknown")
    _execute_graph(
        service,
        run_id=run_id,
        worker=worker,
        lease_epoch=run.lease_epoch,
        generate=True,
    )
    _write_output(output, {"pid": os.getpid(), "outcome_unknown": True})


def _t02_post_return_first(service: RunService, run_id: str, output: Path) -> None:
    worker, run = _claim(service, "m5-t02-post-return-first")
    if run.run_id != run_id:
        raise AssertionError("claimed an unexpected post-return run")
    frozen = service.load_execution_input(
        run_id,
        worker,
        lease_epoch=run.lease_epoch,
    )
    observations = {"provider_returns": 0}

    class ReturningProviderFreeClient:
        hidden_retries_disabled = True
        propagate_control_errors = True

        def complete(self, prompt: str) -> str:
            del prompt
            observations["provider_returns"] += 1
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

    def fault(point, state) -> None:
        del state
        if point != "after_model_attempt_succeeded_before_artifact":
            return
        if observations["provider_returns"] != 1:
            raise AssertionError("provider return was not observed exactly once")
        _write_output(
            output,
            {
                "pid": os.getpid(),
                "provider_returned": True,
                "attempt_succeeded_before_artifact": True,
            },
        )
        _hang()

    postgres_factory = PostgresAssistantFactory(service.engine)

    def assistant_factory(execution):
        assistant = postgres_factory(execution)
        assistant.llm = ReturningProviderFreeClient()
        return assistant

    GraphRunExecutor(
        service,
        assistant_factory,
        generate=True,
        fault_hook=fault,
    ).execute_claimed(
        frozen,
        worker,
        lambda event_type, safe_payload: service.append_stage_event(
            run_id,
            event_type,
            safe_payload,
            worker_id=worker,
            lease_epoch=run.lease_epoch,
        ),
    )


def _t02_post_return_resume(service: RunService, run_id: str, output: Path) -> None:
    worker, run = _claim(service, "m5-t02-post-return-resume")
    if run.run_id != run_id:
        raise AssertionError("claimed an unexpected post-return resumed run")
    if not service.has_outcome_unknown(run_id, operation_name="generate_answer"):
        raise AssertionError("lost provider result was not reconciled outcome_unknown")
    frozen = service.load_execution_input(
        run_id,
        worker,
        lease_epoch=run.lease_epoch,
    )
    observations = {"provider_calls": 0}

    class RejectDuplicateProviderCall:
        hidden_retries_disabled = True
        propagate_control_errors = True

        def complete(self, prompt: str) -> str:
            del prompt
            observations["provider_calls"] += 1
            raise AssertionError("resume silently called the provider a second time")

    postgres_factory = PostgresAssistantFactory(service.engine)

    def assistant_factory(execution):
        assistant = postgres_factory(execution)
        assistant.llm = RejectDuplicateProviderCall()
        return assistant

    GraphRunExecutor(
        service,
        assistant_factory,
        generate=True,
    ).execute_claimed(
        frozen,
        worker,
        lambda event_type, safe_payload: service.append_stage_event(
            run_id,
            event_type,
            safe_payload,
            worker_id=worker,
            lease_epoch=run.lease_epoch,
        ),
    )
    _write_output(
        output,
        {
            "pid": os.getpid(),
            "outcome_unknown": True,
            "provider_calls_after_resume": observations["provider_calls"],
        },
    )


def _t02_planner_post_return_first(
    service: RunService,
    run_id: str,
    output: Path,
) -> None:
    worker, run = _claim(service, "m5-t02-planner-post-return-first")
    if run.run_id != run_id:
        raise AssertionError("claimed an unexpected planner post-return run")
    frozen = service.load_execution_input(
        run_id,
        worker,
        lease_epoch=run.lease_epoch,
    )
    observations = {"planner_returns": 0}

    def returning_planner(state, retrieved):
        del state, retrieved
        observations["planner_returns"] += 1
        return ["bounded provider-free planner followup"]

    def fault(point, state) -> None:
        del state
        if point != "after_planner_attempt_succeeded_before_checkpoint":
            return
        if observations["planner_returns"] != 1:
            raise AssertionError("planner return was not observed exactly once")
        _write_output(
            output,
            {
                "pid": os.getpid(),
                "planner_returned": True,
                "attempt_succeeded_before_checkpoint": True,
            },
        )
        _hang()

    GraphRunExecutor(
        service,
        _empty_assistant,
        generate=False,
        followup_planner=returning_planner,
        fault_hook=fault,
    ).execute_claimed(
        frozen,
        worker,
        lambda event_type, safe_payload: service.append_stage_event(
            run_id,
            event_type,
            safe_payload,
            worker_id=worker,
            lease_epoch=run.lease_epoch,
        ),
    )


def _t02_planner_post_return_resume(
    service: RunService,
    run_id: str,
    output: Path,
) -> None:
    worker, run = _claim(service, "m5-t02-planner-post-return-resume")
    if run.run_id != run_id:
        raise AssertionError("claimed an unexpected resumed planner run")
    if not service.has_outcome_unknown(run_id, operation_name="plan_followup"):
        raise AssertionError("lost planner result was not reconciled outcome_unknown")
    frozen = service.load_execution_input(
        run_id,
        worker,
        lease_epoch=run.lease_epoch,
    )
    observations = {"planner_calls": 0}

    def reject_duplicate_planner_call(state, retrieved):
        del state, retrieved
        observations["planner_calls"] += 1
        raise AssertionError("resume silently called the planner a second time")

    GraphRunExecutor(
        service,
        _empty_assistant,
        generate=False,
        followup_planner=reject_duplicate_planner_call,
    ).execute_claimed(
        frozen,
        worker,
        lambda event_type, safe_payload: service.append_stage_event(
            run_id,
            event_type,
            safe_payload,
            worker_id=worker,
            lease_epoch=run.lease_epoch,
        ),
    )
    _write_output(
        output,
        {
            "pid": os.getpid(),
            "outcome_unknown": True,
            "planner_calls_after_resume": observations["planner_calls"],
        },
    )


def _t03_first(service: RunService, run_id: str, output: Path) -> None:
    worker, run = _claim(service, "m5-t03-first")
    if run.run_id != run_id:
        raise AssertionError("claimed an unexpected run")

    def fault(point, state) -> None:
        del state
        if point != "after_final_answer_stored_before_graph_finish":
            return
        _write_output(output, {"pid": os.getpid(), "result_committed": True})
        _hang()

    _execute_graph(
        service,
        run_id=run_id,
        worker=worker,
        lease_epoch=run.lease_epoch,
        fault_hook=fault,
    )


def _t03_reconcile(service: RunService, run_id: str, output: Path) -> None:
    with service.engine.connect() as connection:
        persisted = (
            connection.execute(
                select(
                    run_results.c.answer_payload,
                    run_results.c.evidence_payload,
                    run_results.c.verification_payload,
                    messages.c.content,
                    runs.c.status,
                    runs.c.stop_reason,
                    runs.c.lease_epoch,
                )
                .select_from(
                    run_results.join(
                        messages,
                        messages.c.message_id == run_results.c.final_message_id,
                    ).join(runs, runs.c.run_id == run_results.c.run_id)
                )
                .where(run_results.c.run_id == run_id)
            )
            .mappings()
            .one()
        )
        attempts_before = int(
            connection.scalar(
                select(func.count())
                .select_from(run_external_attempts)
                .where(
                    run_external_attempts.c.run_id == run_id,
                    run_external_attempts.c.operation_kind == "model",
                )
            )
            or 0
        )
    result = SafeRunResult(
        answer_text=str(persisted["content"]),
        answer_payload=persisted["answer_payload"],
        evidence_payload=persisted["evidence_payload"],
        verification_payload=persisted["verification_payload"],
    )
    reconciled = service.publish_terminal_result(
        run_id,
        _worker("m5-t03-reconcile"),
        int(persisted["lease_epoch"]),
        result,
        completion_status=str(persisted["status"]),
        stop_reason=persisted["stop_reason"],
    )
    if reconciled.status != persisted["status"]:
        raise AssertionError("terminal result reconciliation did not stay succeeded")

    with service.engine.connect() as connection:
        answer_count = int(
            connection.scalar(
                select(func.count())
                .select_from(run_results)
                .where(run_results.c.run_id == run_id)
            )
            or 0
        )
        assistant_count = int(
            connection.scalar(
                select(func.count())
                .select_from(messages)
                .where(
                    messages.c.run_id == run_id,
                    messages.c.role == "assistant",
                )
            )
            or 0
        )
        attempts_after = int(
            connection.scalar(
                select(func.count())
                .select_from(run_external_attempts)
                .where(
                    run_external_attempts.c.run_id == run_id,
                    run_external_attempts.c.operation_kind == "model",
                )
            )
            or 0
        )
    calls_after_resume = attempts_after - attempts_before
    if (answer_count, assistant_count, calls_after_resume) != (1, 1, 0):
        raise AssertionError("committed result was not exactly-once durable")
    _write_output(
        output,
        {
            "pid": os.getpid(),
            "answer_count": answer_count,
            "generator_calls_after_resume": calls_after_resume,
            "reconciliation_path_exercised": True,
        },
    )


def _contend(
    service: RunService,
    run_id: str,
    output: Path,
    start_file: Path | None,
) -> None:
    if start_file is None:
        raise AssertionError("contender requires a start barrier")
    deadline = time.monotonic() + 20.0
    while not start_file.is_file():
        if time.monotonic() >= deadline:
            raise AssertionError("contender start barrier timed out")
        time.sleep(0.01)
    worker = _worker("m5-t06-contender")
    claimed = service.claim_next_run(worker, lease_seconds=30)
    if claimed is not None and claimed.run_id != run_id:
        raise AssertionError("contender claimed an unexpected run")
    _write_output(
        output,
        {"pid": os.getpid(), "claimed": claimed is not None, "worker": worker},
    )


def _stale_first(service: RunService, run_id: str, output: Path) -> None:
    worker, run = _claim(service, "m5-t06-stale-first")
    if run.run_id != run_id:
        raise AssertionError("claimed an unexpected run")
    _write_output(
        output,
        {"pid": os.getpid(), "worker": worker, "lease_epoch": run.lease_epoch},
    )
    _hang()


def _stale_resume(service: RunService, run_id: str, output: Path) -> None:
    worker, run = _claim(service, "m5-t06-stale-resume")
    if run.run_id != run_id:
        raise AssertionError("claimed an unexpected resumed run")
    _write_output(
        output,
        {"pid": os.getpid(), "worker": worker, "lease_epoch": run.lease_epoch},
    )


def _deadline_resume(service: RunService, run_id: str, output: Path) -> None:
    worker, run = _claim(service, "m5-t09-deadline-resume")
    if run.run_id != run_id:
        raise AssertionError("claimed an unexpected resumed run")
    with service.engine.connect() as connection:
        before = int(
            connection.scalar(
                select(func.count())
                .select_from(run_external_attempts)
                .where(
                    run_external_attempts.c.run_id == run_id,
                    run_external_attempts.c.dispatched_at.is_not(None),
                )
            )
            or 0
        )
    _execute_graph(
        service,
        run_id=run_id,
        worker=worker,
        lease_epoch=run.lease_epoch,
        generate=True,
    )
    with service.engine.connect() as connection:
        after = int(
            connection.scalar(
                select(func.count())
                .select_from(run_external_attempts)
                .where(
                    run_external_attempts.c.run_id == run_id,
                    run_external_attempts.c.dispatched_at.is_not(None),
                )
            )
            or 0
        )
    terminal = service.get_budget(run_id)
    _write_output(
        output,
        {
            "pid": os.getpid(),
            "post_deadline_dispatches": after - before,
            "deadline_preserved": terminal.execution_deadline_at.isoformat(),
        },
    )


def _in_memory(output: Path, *, hang: bool) -> None:
    rejected = False
    try:
        require_persistent_checkpointer(InMemorySaver())
    except PersistentCheckpointerRequired:
        rejected = True
    if not rejected:
        raise AssertionError("InMemorySaver unexpectedly passed durable recovery")
    inherited_sensitive_environment = any(
        os.environ.get(name)
        for name in (
            "OPENAI_API_KEY",
            "AWS_SECRET_ACCESS_KEY",
            "M5_SYNTHETIC_SECRET",
        )
    )
    _write_output(
        output,
        {
            "pid": os.getpid(),
            "in_memory_rejected": True,
            "inherited_sensitive_environment": inherited_sensitive_environment,
        },
    )
    if hang:
        _hang()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start-file", type=Path)
    args = parser.parse_args(argv)

    if args.phase == "m5-t10-first":
        _in_memory(args.output, hang=True)
        return 0
    if args.phase == "m5-t10-resume":
        _in_memory(args.output, hang=False)
        return 0

    database_url = os.environ.get("LEGAL_RAG_DATABASE_URL", "").strip()
    if not database_url:
        raise AssertionError("M5 child requires the integration database")
    engine = create_database_engine(DatabaseSettings(database_url))
    service = RunService(engine)
    try:
        if args.phase == "m5-t01-first":
            _t01_first(service, args.run_id, args.output)
        elif args.phase == "m5-t01-resume":
            _t01_resume(service, args.run_id, args.output)
        elif args.phase == "m5-t02-first":
            _t02_first(service, args.run_id, args.output)
        elif args.phase == "m5-t02-resume":
            _t02_resume(service, args.run_id, args.output)
        elif args.phase == "m5-t02-post-return-first":
            _t02_post_return_first(service, args.run_id, args.output)
        elif args.phase == "m5-t02-post-return-resume":
            _t02_post_return_resume(service, args.run_id, args.output)
        elif args.phase == "m5-t02-planner-post-return-first":
            _t02_planner_post_return_first(service, args.run_id, args.output)
        elif args.phase == "m5-t02-planner-post-return-resume":
            _t02_planner_post_return_resume(service, args.run_id, args.output)
        elif args.phase == "m5-t03-first":
            _t03_first(service, args.run_id, args.output)
        elif args.phase == "m5-t03-reconcile":
            _t03_reconcile(service, args.run_id, args.output)
        elif args.phase == "m5-t06-contend":
            _contend(service, args.run_id, args.output, args.start_file)
        elif args.phase == "m5-t06-stale-first":
            _stale_first(service, args.run_id, args.output)
        elif args.phase == "m5-t06-stale-resume":
            _stale_resume(service, args.run_id, args.output)
        elif args.phase == "m5-t09-deadline-resume":
            _deadline_resume(service, args.run_id, args.output)
        else:
            raise AssertionError("unknown M5 child phase")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
