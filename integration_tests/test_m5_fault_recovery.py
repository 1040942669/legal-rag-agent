from __future__ import annotations

import pytest
from sqlalchemy import Engine, func, select, text

from integration_tests.m5_support import (
    create_run_case,
    ensure_persistent_checkpointer,
    expire_run_deadline,
    expire_run_lease,
    hard_kill,
    record_scenario,
    start_child,
    stop_child,
    wait_child_exit,
    wait_child_output,
)
from legal_rag.storage.schema import (
    messages,
    run_events,
    run_external_attempts,
    run_results,
    runs,
)


def test_m5_t01_kill_after_retrieval_checkpoint_resumes_without_retrieval(
    migrated_engine: Engine,
    integration_database_url: str,
    tmp_path,
) -> None:
    ensure_persistent_checkpointer(migrated_engine)
    case = create_run_case(
        migrated_engine,
        case_name="t01-retrieval-resume",
        real_corpus=True,
    )
    first = start_child(
        tmp_path,
        phase="m5-t01-first",
        database_url=integration_database_url,
        run_id=case.run_id,
    )
    second = None
    try:
        first_payload = wait_child_output(first)
        assert first_payload["checkpoint_committed"] is True
        first_pid = int(first_payload["pid"])
        hard_kill(first, actual_pid=first_pid)

        expire_run_lease(migrated_engine, case.run_id)
        recovered = case.service.recover_stale_runs()
        assert [item.run_id for item in recovered] == [case.run_id]
        interrupted = case.service.get_run(case.boundary.owner, case.run_id)
        assert interrupted.status == "interrupted"
        assert interrupted.last_completed_node in {
            "retrieve",
            "merge_evidence",
            "check_evidence",
        }
        before_resume = case.service.get_budget(case.run_id)
        assert before_resume.retrieval_rounds_used == 1
        assert before_resume.tool_attempts_used == 1
        with migrated_engine.connect() as connection:
            postgres_checkpoint_count = int(
                connection.scalar(
                    text(
                        "SELECT count(*) FROM checkpoints "
                        "WHERE thread_id LIKE :thread_prefix"
                    ),
                    {"thread_prefix": f"{case.run_id}%"},
                )
                or 0
            )
        assert postgres_checkpoint_count > 0

        requested = case.service.resume_run(case.boundary.owner, case.run_id)
        assert requested.resume_requested_at is not None
        second = start_child(
            tmp_path,
            phase="m5-t01-resume",
            database_url=integration_database_url,
            run_id=case.run_id,
        )
        second_payload = wait_child_exit(second)
        resume_pid = int(second_payload["pid"])
        assert first_pid != resume_pid
        assert second_payload == {
            "pid": resume_pid,
            "retrieval_reused": True,
            "retrieval_invocations": 1,
        }
        after_resume = case.service.get_budget(case.run_id)
        assert after_resume.retrieval_rounds_used == 1
        assert after_resume.tool_attempts_used == 1
        assert case.service.get_run(case.boundary.owner, case.run_id).status == (
            "succeeded"
        )

        record_scenario(
            "M5-T01",
            {
                "hard_kill_observed": True,
                "pids_differ": True,
                "retrieval_invocations": 1,
                "retrieval_reused": True,
                "graph_executor_used": True,
                "postgres_saver_checkpoint_observed": True,
                "first_pid": first_pid,
                "resume_pid": resume_pid,
            },
        )
    finally:
        stop_child(first)
        if second is not None:
            stop_child(second)


def test_m5_t02_kill_after_model_dispatch_records_unknown_outcome_and_keeps_reserved_budget(
    migrated_engine: Engine,
    integration_database_url: str,
    tmp_path,
) -> None:
    ensure_persistent_checkpointer(migrated_engine)
    case = create_run_case(
        migrated_engine,
        case_name="t02-unknown-outcome",
        real_corpus=True,
    )
    first = start_child(
        tmp_path,
        phase="m5-t02-first",
        database_url=integration_database_url,
        run_id=case.run_id,
    )
    second = None
    try:
        first_payload = wait_child_output(first)
        assert first_payload["dispatch_observed"] is True
        assert first_payload["provider_call_observed"] is True
        first_pid = int(first_payload["pid"])
        hard_kill(first, actual_pid=first_pid)

        before_recovery = case.service.get_budget(case.run_id)
        assert before_recovery.model_attempts_used == 1
        expire_run_lease(migrated_engine, case.run_id)
        recovered = case.service.recover_stale_runs()
        assert [item.run_id for item in recovered] == [case.run_id]

        with migrated_engine.connect() as connection:
            attempt = (
                connection.execute(
                    select(run_external_attempts).where(
                        run_external_attempts.c.run_id == case.run_id,
                        run_external_attempts.c.operation_kind == "model",
                        run_external_attempts.c.operation_name == "generate_answer",
                    )
                )
                .mappings()
                .one()
            )
            unknown_events = int(
                connection.scalar(
                    select(func.count())
                    .select_from(run_events)
                    .where(
                        run_events.c.run_id == case.run_id,
                        run_events.c.event_type == "attempt.outcome_unknown",
                    )
                )
                or 0
            )
        assert attempt["status"] == "outcome_unknown"
        assert attempt["retryable"] is False
        assert unknown_events == 1
        assert case.service.get_budget(case.run_id).model_attempts_used == 1

        case.service.resume_run(case.boundary.owner, case.run_id)
        second = start_child(
            tmp_path,
            phase="m5-t02-resume",
            database_url=integration_database_url,
            run_id=case.run_id,
        )
        second_payload = wait_child_exit(second)
        resume_pid = int(second_payload["pid"])
        assert first_pid != resume_pid
        assert second_payload == {"pid": resume_pid, "outcome_unknown": True}
        terminal = case.service.get_run(case.boundary.owner, case.run_id)
        assert terminal.status == "completed_with_limits"
        assert terminal.stop_reason == "external_outcome_unknown"
        assert case.service.get_budget(case.run_id).model_attempts_used == 1

        record_scenario(
            "M5-T02",
            {
                "hard_kill_observed": True,
                "pids_differ": True,
                "dispatch_observed": True,
                "provider_call_observed": True,
                "outcome_unknown": True,
                "budget_refunded": False,
                "zero_duplicate_cost_guaranteed": False,
                "graph_unknown_outcome_path_exercised": True,
                "first_pid": first_pid,
                "resume_pid": resume_pid,
            },
        )
    finally:
        stop_child(first)
        if second is not None:
            stop_child(second)


def test_m5_t03_kill_after_result_commit_reconciles_without_duplicate_answer(
    migrated_engine: Engine,
    integration_database_url: str,
    tmp_path,
) -> None:
    ensure_persistent_checkpointer(migrated_engine)
    case = create_run_case(
        migrated_engine,
        case_name="t03-result-reconcile",
        real_corpus=True,
    )
    first = start_child(
        tmp_path,
        phase="m5-t03-first",
        database_url=integration_database_url,
        run_id=case.run_id,
    )
    second = None
    try:
        first_payload = wait_child_output(first)
        assert first_payload["result_committed"] is True
        first_pid = int(first_payload["pid"])
        hard_kill(first, actual_pid=first_pid)

        terminal = case.service.resume_run(case.boundary.owner, case.run_id)
        assert terminal.status == "succeeded"
        second = start_child(
            tmp_path,
            phase="m5-t03-reconcile",
            database_url=integration_database_url,
            run_id=case.run_id,
        )
        second_payload = wait_child_exit(second)
        resume_pid = int(second_payload["pid"])
        assert first_pid != resume_pid
        assert second_payload == {
            "pid": resume_pid,
            "answer_count": 1,
            "generator_calls_after_resume": 0,
            "reconciliation_path_exercised": True,
        }
        with migrated_engine.connect() as connection:
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(run_results)
                    .where(run_results.c.run_id == case.run_id)
                )
                == 1
            )
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(messages)
                    .where(
                        messages.c.run_id == case.run_id,
                        messages.c.role == "assistant",
                    )
                )
                == 1
            )
            assert (
                connection.scalar(
                    select(func.count())
                    .select_from(run_external_attempts)
                    .where(
                        run_external_attempts.c.run_id == case.run_id,
                        run_external_attempts.c.operation_kind == "model",
                    )
                )
                == 0
            )

        record_scenario(
            "M5-T03",
            {
                "hard_kill_observed": True,
                "pids_differ": True,
                "reconciled_existing_result": True,
                "answer_count": 1,
                "generator_calls_after_resume": 0,
                "reconciliation_path_exercised": True,
                "first_pid": first_pid,
                "resume_pid": resume_pid,
            },
        )
    finally:
        stop_child(first)
        if second is not None:
            stop_child(second)


def test_m5_t09_resume_after_absolute_deadline_finishes_without_new_dispatch(
    migrated_engine: Engine,
    integration_database_url: str,
    tmp_path,
) -> None:
    case = create_run_case(migrated_engine, case_name="t09-deadline")
    worker_one = "m5-t09-first"
    first_claim = case.service.claim_next_run(worker_one, lease_seconds=30)
    assert first_claim is not None and first_claim.run_id == case.run_id
    expired = expire_run_deadline(migrated_engine, case.run_id)
    expire_run_lease(migrated_engine, case.run_id)
    recovered = case.service.recover_stale_runs()
    assert [item.run_id for item in recovered] == [case.run_id]
    case.service.resume_run(case.boundary.owner, case.run_id)

    resumed_process = start_child(
        tmp_path,
        phase="m5-t09-deadline-resume",
        database_url=integration_database_url,
        run_id=case.run_id,
    )
    resumed_payload = wait_child_exit(resumed_process)
    assert resumed_payload["post_deadline_dispatches"] == 0
    with migrated_engine.connect() as connection:
        dispatches = int(
            connection.scalar(
                select(func.count())
                .select_from(run_external_attempts)
                .where(
                    run_external_attempts.c.run_id == case.run_id,
                    run_external_attempts.c.dispatched_at.is_not(None),
                )
            )
            or 0
        )
        persisted_deadline = connection.scalar(
            select(runs.c.execution_deadline_at).where(runs.c.run_id == case.run_id)
        )
    assert dispatches == 0
    assert persisted_deadline is not None
    assert abs((persisted_deadline - expired).total_seconds()) < 0.001

    terminal = case.service.get_run(case.boundary.owner, case.run_id)
    assert terminal.status == "completed_with_limits"
    assert terminal.stop_reason == "deadline_exceeded"
    record_scenario(
        "M5-T09",
        {
            "deadline_preserved": True,
            "post_deadline_dispatches": 0,
            "graph_deadline_path_exercised": True,
        },
    )


def test_m5_t10_new_process_cannot_pass_recovery_acceptance_with_in_memory_saver(
    migrated_engine: Engine,
    integration_database_url: str,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_persistent_checkpointer(migrated_engine)
    monkeypatch.setenv("OPENAI_API_KEY", "m5-synthetic-secret-must-not-cross-process")
    first = start_child(
        tmp_path,
        phase="m5-t10-first",
        database_url=integration_database_url,
    )
    second = None
    try:
        first_payload = wait_child_output(first)
        assert first_payload["in_memory_rejected"] is True
        assert first_payload["inherited_sensitive_environment"] is False
        first_pid = int(first_payload["pid"])
        hard_kill(first, actual_pid=first_pid)
        second = start_child(
            tmp_path,
            phase="m5-t10-resume",
            database_url=integration_database_url,
        )
        second_payload = wait_child_exit(second)
        resume_pid = int(second_payload["pid"])
        assert first_pid != resume_pid
        assert second_payload == {
            "pid": resume_pid,
            "in_memory_rejected": True,
            "inherited_sensitive_environment": False,
        }
        record_scenario(
            "M5-T10",
            {
                "in_memory_rejected": True,
                "same_process_demo_accepted": False,
                "first_pid": first_pid,
                "resume_pid": resume_pid,
            },
        )
    finally:
        stop_child(first)
        if second is not None:
            stop_child(second)
