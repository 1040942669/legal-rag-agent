from __future__ import annotations

from sqlalchemy import Engine

from integration_tests.m5_support import (
    create_run_case,
    expire_run_lease,
    hard_kill,
    record_scenario,
    start_child,
    stop_child,
    wait_child_exit,
    wait_child_output,
)
from legal_rag.services.run_service import WorkerLeaseLostError


def test_m5_t06_two_processes_resume_one_run_only_one_gets_execution_lease(
    migrated_engine: Engine,
    integration_database_url: str,
    tmp_path,
) -> None:
    case = create_run_case(migrated_engine, case_name="t06-two-contenders")
    start_file = tmp_path / "contenders-start"
    first = start_child(
        tmp_path,
        phase="m5-t06-contend",
        database_url=integration_database_url,
        run_id=case.run_id,
        start_file=start_file,
    )
    second = start_child(
        tmp_path,
        phase="m5-t06-contend",
        database_url=integration_database_url,
        run_id=case.run_id,
        start_file=start_file,
    )
    try:
        start_file.write_text("go\n", encoding="ascii", newline="\n")
        outcomes = [wait_child_exit(first), wait_child_exit(second)]
        contender_pids = [int(item["pid"]) for item in outcomes]
        assert len(set(contender_pids)) == 2
        assert [bool(item["claimed"]) for item in outcomes].count(True) == 1
        running = case.service.get_run(case.boundary.owner, case.run_id)
        assert running.status == "running"
        assert running.lease_epoch == 1
        case.service.cancel_run(case.boundary.owner, case.run_id)
        record_scenario(
            "M5-T06",
            {"single_owner": True, "contender_pids": contender_pids},
        )
    finally:
        stop_child(first)
        stop_child(second)


def test_m5_t06_stale_owner_is_fenced_after_lease_takeover(
    migrated_engine: Engine,
    integration_database_url: str,
    tmp_path,
) -> None:
    case = create_run_case(migrated_engine, case_name="t06-stale-fence")
    first = start_child(
        tmp_path,
        phase="m5-t06-stale-first",
        database_url=integration_database_url,
        run_id=case.run_id,
    )
    second = None
    try:
        first_payload = wait_child_output(first)
        first_pid = int(first_payload["pid"])
        old_worker = str(first_payload["worker"])
        old_epoch = int(first_payload["lease_epoch"])
        hard_kill(first, actual_pid=first_pid)
        expire_run_lease(migrated_engine, case.run_id)
        recovered = case.service.recover_stale_runs()
        assert [item.run_id for item in recovered] == [case.run_id]
        case.service.resume_run(case.boundary.owner, case.run_id)

        second = start_child(
            tmp_path,
            phase="m5-t06-stale-resume",
            database_url=integration_database_url,
            run_id=case.run_id,
        )
        second_payload = wait_child_exit(second)
        assert int(second_payload["lease_epoch"]) == old_epoch + 1
        try:
            case.service.assert_execution_fence(
                case.run_id,
                old_worker,
                old_epoch,
            )
        except WorkerLeaseLostError:
            stale_owner_fenced = True
        else:  # pragma: no cover - acceptance invariant.
            stale_owner_fenced = False
        assert stale_owner_fenced is True
        case.service.cancel_run(case.boundary.owner, case.run_id)
        record_scenario("M5-T06", {"stale_owner_fenced": True})
    finally:
        stop_child(first)
        if second is not None:
            stop_child(second)
