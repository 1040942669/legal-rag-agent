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
    run_checkpoints,
    run_events,
    run_external_attempts,
    run_node_artifacts,
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
            migrated_engine,
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
    post_return_first = None
    post_return_second = None
    planner_post_return_first = None
    planner_post_return_second = None
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

        post_return_session = case.service.create_session(
            case.boundary.owner,
            "M5 T02 provider result durability window",
        )
        post_return_run, replayed = case.service.create_run(
            case.boundary.owner,
            post_return_session.session_id,
            "m5-t02-provider-result-not-durable",
            {
                "question": "服务端测试法第一条规定了什么？",
                "retrieval": {"top_k": 3},
            },
            "m5-bounded-v1",
        )
        assert replayed is False
        post_return_first = start_child(
            tmp_path,
            phase="m5-t02-post-return-first",
            database_url=integration_database_url,
            run_id=post_return_run.run_id,
        )
        post_return_payload = wait_child_output(post_return_first)
        assert post_return_payload == {
            "pid": int(post_return_payload["pid"]),
            "provider_returned": True,
            "attempt_succeeded_before_artifact": True,
        }
        post_return_first_pid = int(post_return_payload["pid"])

        with migrated_engine.connect() as connection:
            succeeded_attempt = (
                connection.execute(
                    select(run_external_attempts).where(
                        run_external_attempts.c.run_id == post_return_run.run_id,
                        run_external_attempts.c.operation_kind == "model",
                        run_external_attempts.c.operation_name == "generate_answer",
                    )
                )
                .mappings()
                .one()
            )
            verification_artifacts_before_kill = int(
                connection.scalar(
                    select(func.count())
                    .select_from(run_node_artifacts)
                    .where(
                        run_node_artifacts.c.run_id == post_return_run.run_id,
                        run_node_artifacts.c.artifact_kind == "verification",
                    )
                )
                or 0
            )
            post_return_pointer = (
                connection.execute(
                    select(
                        runs.c.checkpoint_namespace,
                        runs.c.last_checkpoint_id,
                    ).where(runs.c.run_id == post_return_run.run_id)
                )
                .mappings()
                .one()
            )
            post_return_checkpoint_created_at = connection.scalar(
                select(run_checkpoints.c.created_at).where(
                    run_checkpoints.c.run_id == post_return_run.run_id,
                    run_checkpoints.c.checkpoint_namespace
                    == post_return_pointer["checkpoint_namespace"],
                    run_checkpoints.c.checkpoint_id
                    == post_return_pointer["last_checkpoint_id"],
                )
            )
        assert succeeded_attempt["status"] == "succeeded"
        assert succeeded_attempt["retryable"] is None
        assert succeeded_attempt["error_code"] is None
        assert succeeded_attempt["provider_request_id"] is not None
        assert succeeded_attempt["dispatched_at"] is not None
        assert succeeded_attempt["result_hash"] is not None
        assert succeeded_attempt["result_ref"] is not None
        assert succeeded_attempt["completed_at"] is not None
        assert post_return_checkpoint_created_at is not None
        assert succeeded_attempt["completed_at"] > post_return_checkpoint_created_at
        assert verification_artifacts_before_kill == 0
        pre_kill_checkpoint = case.service.load_trusted_checkpoint(
            post_return_run.run_id
        )
        assert pre_kill_checkpoint is not None
        assert pre_kill_checkpoint["next_node"] == "generate"
        assert pre_kill_checkpoint["verification_result_ref"] is None
        assert pre_kill_checkpoint["verification_result_hash"] is None
        hard_kill(post_return_first, actual_pid=post_return_first_pid)

        expire_run_lease(migrated_engine, post_return_run.run_id)
        recovered_post_return = case.service.recover_stale_runs()
        assert [item.run_id for item in recovered_post_return] == [
            post_return_run.run_id
        ]
        with migrated_engine.connect() as connection:
            reconciled_attempt = (
                connection.execute(
                    select(run_external_attempts).where(
                        run_external_attempts.c.run_id == post_return_run.run_id,
                        run_external_attempts.c.operation_kind == "model",
                        run_external_attempts.c.operation_name == "generate_answer",
                    )
                )
                .mappings()
                .one()
            )
            reconciliation_events = (
                connection.execute(
                    select(run_events.c.safe_payload).where(
                        run_events.c.run_id == post_return_run.run_id,
                        run_events.c.event_type == "attempt.outcome_unknown",
                    )
                )
                .scalars()
                .all()
            )
        assert reconciled_attempt["status"] == "outcome_unknown"
        assert reconciled_attempt["error_code"] == "provider_result_not_durable"
        assert reconciled_attempt["retryable"] is False
        assert reconciled_attempt["attempt_id"] == succeeded_attempt["attempt_id"]
        assert reconciled_attempt["lease_epoch"] == succeeded_attempt["lease_epoch"]
        assert reconciled_attempt["provider_request_id"] is not None
        assert reconciled_attempt["dispatched_at"] is not None
        assert reconciled_attempt["result_ref"] is None
        assert reconciled_attempt["result_hash"] is None
        assert reconciliation_events == [
            {
                "status": "outcome_unknown",
                "possible_duplicate_cost": True,
                "budget_refunded": False,
            }
        ]
        assert case.service.get_budget(post_return_run.run_id).model_attempts_used == 1

        requested = case.service.resume_run(
            case.boundary.owner,
            post_return_run.run_id,
        )
        assert requested.resume_requested_at is not None
        post_return_second = start_child(
            tmp_path,
            phase="m5-t02-post-return-resume",
            database_url=integration_database_url,
            run_id=post_return_run.run_id,
        )
        post_return_resume_payload = wait_child_exit(post_return_second)
        post_return_resume_pid = int(post_return_resume_payload["pid"])
        assert post_return_first_pid != post_return_resume_pid
        assert post_return_resume_payload == {
            "pid": post_return_resume_pid,
            "outcome_unknown": True,
            "provider_calls_after_resume": 0,
        }
        post_return_terminal = case.service.get_run(
            case.boundary.owner,
            post_return_run.run_id,
        )
        assert post_return_terminal.status == "completed_with_limits"
        assert post_return_terminal.stop_reason == "external_outcome_unknown"
        assert case.service.get_budget(post_return_run.run_id).model_attempts_used == 1

        planner_session = case.service.create_session(
            case.boundary.owner,
            "M5 T02 planner result durability window",
        )
        planner_run, replayed = case.service.create_run(
            case.boundary.owner,
            planner_session.session_id,
            "m5-t02-planner-result-not-durable",
            {
                "question": "没有证据时请规划一次后续检索。",
                "retrieval": {"top_k": 3},
            },
            "m5-bounded-v1",
        )
        assert replayed is False
        planner_post_return_first = start_child(
            tmp_path,
            phase="m5-t02-planner-post-return-first",
            database_url=integration_database_url,
            run_id=planner_run.run_id,
        )
        planner_post_return_payload = wait_child_output(planner_post_return_first)
        planner_post_return_pid = int(planner_post_return_payload["pid"])
        assert planner_post_return_payload == {
            "pid": planner_post_return_pid,
            "planner_returned": True,
            "attempt_succeeded_before_checkpoint": True,
        }

        with migrated_engine.connect() as connection:
            planner_attempt_before_kill = (
                connection.execute(
                    select(run_external_attempts).where(
                        run_external_attempts.c.run_id == planner_run.run_id,
                        run_external_attempts.c.operation_kind == "model",
                        run_external_attempts.c.operation_name == "plan_followup",
                    )
                )
                .mappings()
                .one()
            )
            planner_run_pointer = (
                connection.execute(
                    select(
                        runs.c.checkpoint_namespace,
                        runs.c.last_checkpoint_id,
                    ).where(runs.c.run_id == planner_run.run_id)
                )
                .mappings()
                .one()
            )
            planner_checkpoint_created_at = connection.scalar(
                select(run_checkpoints.c.created_at).where(
                    run_checkpoints.c.run_id == planner_run.run_id,
                    run_checkpoints.c.checkpoint_namespace
                    == planner_run_pointer["checkpoint_namespace"],
                    run_checkpoints.c.checkpoint_id
                    == planner_run_pointer["last_checkpoint_id"],
                )
            )
        assert planner_attempt_before_kill["status"] == "succeeded"
        assert planner_attempt_before_kill["operation_key"] == "planner-round-2"
        assert planner_attempt_before_kill["attempt_no"] == 1
        assert planner_attempt_before_kill["retryable"] is None
        assert planner_attempt_before_kill["error_code"] is None
        assert planner_attempt_before_kill["provider_request_id"] is not None
        assert planner_attempt_before_kill["dispatched_at"] is not None
        assert planner_attempt_before_kill["result_hash"] is not None
        assert planner_attempt_before_kill["result_ref"] is not None
        assert planner_attempt_before_kill["completed_at"] is not None
        assert planner_checkpoint_created_at is not None
        assert (
            planner_attempt_before_kill["completed_at"] > planner_checkpoint_created_at
        )
        planner_checkpoint = case.service.load_trusted_checkpoint(planner_run.run_id)
        assert planner_checkpoint is not None
        assert planner_checkpoint["last_completed_node"] == "check_evidence"
        assert planner_checkpoint["next_node"] == "plan_followup"
        assert planner_checkpoint["proposed_queries"] == []
        assert planner_checkpoint["completion_status"] is None
        assert planner_checkpoint["stop_reason"] is None
        assert planner_checkpoint["model_attempts_used"] == 0
        assert planner_checkpoint["retrieval_rounds_used"] == 1
        assert planner_checkpoint["tool_attempts_used"] == 1
        assert planner_checkpoint["retrieved_artifact_ref"] is not None
        assert planner_checkpoint["retrieved_artifact_hash"] is not None
        assert planner_checkpoint["verification_result_ref"] is None
        assert planner_checkpoint["verification_result_hash"] is None
        planner_budget_before_kill = case.service.get_budget(planner_run.run_id)
        assert planner_budget_before_kill.model_attempts_used == 1
        assert planner_budget_before_kill.retrieval_rounds_used == 1
        assert planner_budget_before_kill.tool_attempts_used == 1
        hard_kill(
            planner_post_return_first,
            actual_pid=planner_post_return_pid,
        )

        expire_run_lease(migrated_engine, planner_run.run_id)
        recovered_planner = case.service.recover_stale_runs()
        assert [item.run_id for item in recovered_planner] == [planner_run.run_id]
        with migrated_engine.connect() as connection:
            reconciled_planner_attempt = (
                connection.execute(
                    select(run_external_attempts).where(
                        run_external_attempts.c.run_id == planner_run.run_id,
                        run_external_attempts.c.operation_kind == "model",
                        run_external_attempts.c.operation_name == "plan_followup",
                    )
                )
                .mappings()
                .one()
            )
            planner_reconciliation_events = (
                connection.execute(
                    select(run_events.c.safe_payload).where(
                        run_events.c.run_id == planner_run.run_id,
                        run_events.c.event_type == "attempt.outcome_unknown",
                    )
                )
                .scalars()
                .all()
            )
        assert reconciled_planner_attempt["status"] == "outcome_unknown"
        assert reconciled_planner_attempt["error_code"] == "provider_result_not_durable"
        assert reconciled_planner_attempt["retryable"] is False
        assert (
            reconciled_planner_attempt["attempt_id"]
            == planner_attempt_before_kill["attempt_id"]
        )
        assert (
            reconciled_planner_attempt["lease_epoch"]
            == planner_attempt_before_kill["lease_epoch"]
        )
        assert reconciled_planner_attempt["provider_request_id"] is not None
        assert reconciled_planner_attempt["dispatched_at"] is not None
        assert reconciled_planner_attempt["result_ref"] is None
        assert reconciled_planner_attempt["result_hash"] is None
        assert planner_reconciliation_events == [
            {
                "status": "outcome_unknown",
                "possible_duplicate_cost": True,
                "budget_refunded": False,
            }
        ]
        assert case.service.get_budget(planner_run.run_id).model_attempts_used == 1

        requested_planner = case.service.resume_run(
            case.boundary.owner,
            planner_run.run_id,
        )
        assert requested_planner.resume_requested_at is not None
        planner_post_return_second = start_child(
            tmp_path,
            phase="m5-t02-planner-post-return-resume",
            database_url=integration_database_url,
            run_id=planner_run.run_id,
        )
        planner_resume_payload = wait_child_exit(planner_post_return_second)
        planner_resume_pid = int(planner_resume_payload["pid"])
        assert planner_post_return_pid != planner_resume_pid
        assert planner_resume_payload == {
            "pid": planner_resume_pid,
            "outcome_unknown": True,
            "planner_calls_after_resume": 0,
        }
        planner_terminal = case.service.get_run(
            case.boundary.owner,
            planner_run.run_id,
        )
        assert planner_terminal.status == "completed_with_limits"
        assert planner_terminal.stop_reason == "external_outcome_unknown"
        assert case.service.get_budget(planner_run.run_id).model_attempts_used == 1
        with migrated_engine.connect() as connection:
            planner_attempt_count = int(
                connection.scalar(
                    select(func.count())
                    .select_from(run_external_attempts)
                    .where(
                        run_external_attempts.c.run_id == planner_run.run_id,
                        run_external_attempts.c.operation_name == "plan_followup",
                    )
                )
                or 0
            )
        assert planner_attempt_count == 1
        final_planner_checkpoint = case.service.load_trusted_checkpoint(
            planner_run.run_id
        )
        assert final_planner_checkpoint is not None
        assert final_planner_checkpoint["last_completed_node"] == "persist_result"
        assert final_planner_checkpoint["next_node"] is None
        assert final_planner_checkpoint["completion_status"] == (
            "completed_with_limits"
        )
        assert final_planner_checkpoint["stop_reason"] == "external_outcome_unknown"
        assert final_planner_checkpoint["model_attempts_used"] == 1

        record_scenario(
            migrated_engine,
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
                "post_provider_pre_artifact_crash_exercised": True,
                "orphaned_succeeded_attempt_reconciled": True,
                "duplicate_provider_call_avoided": True,
                "post_planner_pre_checkpoint_crash_exercised": True,
                "orphaned_planner_succeeded_attempt_reconciled": True,
                "duplicate_planner_call_avoided": True,
                "first_pid": first_pid,
                "resume_pid": resume_pid,
            },
        )
    finally:
        stop_child(first)
        if second is not None:
            stop_child(second)
        if post_return_first is not None:
            stop_child(post_return_first)
        if post_return_second is not None:
            stop_child(post_return_second)
        if planner_post_return_first is not None:
            stop_child(planner_post_return_first)
        if planner_post_return_second is not None:
            stop_child(planner_post_return_second)


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
            migrated_engine,
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
        migrated_engine,
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
            migrated_engine,
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
