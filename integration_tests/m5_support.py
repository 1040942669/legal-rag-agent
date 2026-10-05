from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO

from sqlalchemy import Engine, func, select, text, update

from integration_tests.m4_support import (
    SeededBoundary,
    seed_m4_service_boundary,
    seed_m4_service_corpus,
)
from legal_rag.harness.checkpoint import (
    assert_postgres_checkpointer_ready,
    setup_postgres_checkpointer,
)
from legal_rag.harness.state import HarnessState, new_harness_state
from legal_rag.services.run_service import FrozenRunInput, RunService
from legal_rag.storage.schema import run_checkpoints, runs

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_SHA = re.compile(r"[0-9a-f]{40}")

M5_TEST_SELECTORS: dict[str, tuple[str, ...]] = {
    "M5-T01": (
        "integration_tests/test_m5_fault_recovery.py::test_m5_t01_kill_after_retrieval_checkpoint_resumes_without_retrieval",
    ),
    "M5-T02": (
        "integration_tests/test_m5_fault_recovery.py::test_m5_t02_kill_after_model_dispatch_records_unknown_outcome_and_keeps_reserved_budget",
    ),
    "M5-T03": (
        "integration_tests/test_m5_fault_recovery.py::test_m5_t03_kill_after_result_commit_reconciles_without_duplicate_answer",
    ),
    "M5-T04": (
        "integration_tests/test_m5_budget_and_errors.py::test_m5_t04_adversarial_followup_requests_stop_at_durable_global_budgets",
        "integration_tests/test_m5_followup_runs.py::test_m5_t04_clarification_followup_creates_new_parent_bound_run",
    ),
    "M5-T05": (
        "tests/test_m5_retry_policy.py::test_m5_t05_retryable_429_and_timeout_retry_once_and_consume_attempts",
        "tests/test_m5_retry_policy.py::test_m5_t05_nonretryable_400_and_401_do_not_retry",
        "integration_tests/test_m5_budget_and_errors.py::test_m5_t05_transient_429_and_timeout_retry_once_with_global_budget",
        "integration_tests/test_m5_budget_and_errors.py::test_m5_t05_400_and_401_are_terminal_without_retry",
    ),
    "M5-T06": (
        "integration_tests/test_m5_concurrent_resume.py::test_m5_t06_two_processes_resume_one_run_only_one_gets_execution_lease",
        "integration_tests/test_m5_concurrent_resume.py::test_m5_t06_stale_owner_is_fenced_after_lease_takeover",
    ),
    "M5-T07": (
        "integration_tests/test_m5_checkpoint_compatibility.py::test_m5_t07_old_checkpoint_fails_closed_or_enters_explicit_migration_state",
    ),
    "M5-T08": (
        "tests/test_m5_tool_security.py::test_m5_t08_prompt_injection_cannot_select_unlisted_tool_or_override_frozen_scope",
        "integration_tests/test_m5_prompt_injection.py::test_m5_t08_untrusted_evidence_cannot_expand_tool_or_scope_boundary",
    ),
    "M5-T09": (
        "integration_tests/test_m5_fault_recovery.py::test_m5_t09_resume_after_absolute_deadline_finishes_without_new_dispatch",
    ),
    "M5-T10": (
        "tests/test_m5_configuration.py::test_m5_persistent_recovery_rejects_in_memory_checkpointer",
        "integration_tests/test_m5_fault_recovery.py::test_m5_t10_new_process_cannot_pass_recovery_acceptance_with_in_memory_saver",
    ),
}


@dataclass(frozen=True, slots=True)
class M5RunCase:
    service: RunService
    boundary: SeededBoundary
    run_id: str
    session_id: str


@dataclass(slots=True)
class ChildProcess:
    process: subprocess.Popen[bytes]
    output_path: Path
    log_path: Path
    log_handle: BinaryIO


def ensure_persistent_checkpointer(engine: Engine) -> None:
    setup_postgres_checkpointer(engine)
    assert_postgres_checkpointer_ready(engine)


def create_run_case(
    engine: Engine,
    *,
    service: RunService | None = None,
    case_name: str,
    real_corpus: bool = False,
    question: str | None = None,
) -> M5RunCase:
    resolved_service = service or RunService(engine)
    boundary = (
        seed_m4_service_corpus(engine)
        if real_corpus
        else seed_m4_service_boundary(engine)
    )
    session = resolved_service.create_session(boundary.owner, f"M5 {case_name}")
    run, replayed = resolved_service.create_run(
        boundary.owner,
        session.session_id,
        f"m5-{case_name}-{uuid.uuid4().hex}",
        {
            "question": question
            or (
                "服务端测试法第一条规定了什么？"
                if real_corpus
                else f"M5 deterministic {case_name}"
            ),
            "retrieval": {"top_k": 3},
        },
        "m5-bounded-v1",
    )
    assert replayed is False
    return M5RunCase(
        service=resolved_service,
        boundary=boundary,
        run_id=run.run_id,
        session_id=session.session_id,
    )


def state_for_checkpoint(
    frozen: FrozenRunInput,
    *,
    last_completed_node: str = "retrieve",
    next_node: str = "merge_evidence",
    retrieval_rounds_used: int = 1,
    tool_attempts_used: int = 1,
    artifact_id: str | None = None,
    artifact_hash: str | None = None,
) -> HarnessState:
    state = new_harness_state(
        run_id=frozen.run_id,
        session_id=frozen.session_id,
        user_id=frozen.user_id,
        graph_version=frozen.graph_version,
        retrieval_config_hash=frozen.retrieval_config_hash,
        question=frozen.question,
        bounded_history_refs=[],
        snapshot_id=frozen.snapshot_id,
        embedding_profile_id=frozen.profile_id,
        execution_deadline_at=frozen.execution_deadline_at,
    )
    state["last_completed_node"] = last_completed_node
    state["next_node"] = next_node
    state["retrieval_rounds_used"] = retrieval_rounds_used
    state["tool_attempts_used"] = tool_attempts_used
    if artifact_id is not None:
        state["retrieved_artifact_ref"] = artifact_id
        state["retrieved_artifact_hash"] = artifact_hash
        state["retrieved_evidence_refs"] = ["evidence:fixture"]
        state["immutable_evidence_hashes"] = ["e" * 64]
    return state


def expire_run_lease(engine: Engine, run_id: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            update(runs)
            .where(runs.c.run_id == run_id)
            .values(lease_expires_at=func.now() - timedelta(seconds=1))
        )


def expire_run_deadline(engine: Engine, run_id: str) -> datetime:
    expired = datetime.now(timezone.utc) - timedelta(seconds=5)
    with engine.begin() as connection:
        connection.execute(
            update(runs)
            .where(runs.c.run_id == run_id)
            .values(execution_deadline_at=expired)
        )
    return expired


def checkpoint_identity(engine: Engine, run_id: str) -> tuple[str, str, str]:
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(
                    run_checkpoints.c.checkpoint_namespace,
                    run_checkpoints.c.checkpoint_id,
                    run_checkpoints.c.state_hash,
                )
                .where(run_checkpoints.c.run_id == run_id)
                .order_by(run_checkpoints.c.created_at.desc())
                .limit(1)
            )
            .mappings()
            .one()
        )
    return (
        str(row["checkpoint_namespace"]),
        str(row["checkpoint_id"]),
        str(row["state_hash"]),
    )


def start_child(
    tmp_path: Path,
    *,
    phase: str,
    database_url: str,
    run_id: str = "none",
    start_file: Path | None = None,
) -> ChildProcess:
    output_path = tmp_path / f"{phase}-{uuid.uuid4().hex}.json"
    log_path = output_path.with_suffix(".log")
    child_python = (
        getattr(sys, "_base_executable", sys.executable)
        if os.name == "nt"
        else sys.executable
    )
    command = [
        child_python,
        "-m",
        "integration_tests.m5_fault_process",
        "--phase",
        phase,
        "--run-id",
        run_id,
        "--output",
        str(output_path),
    ]
    if start_file is not None:
        command.extend(["--start-file", str(start_file)])
    inherited_names = (
        "COMSPEC",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "VIRTUAL_ENV",
        "WINDIR",
    )
    environment = {
        name: os.environ[name] for name in inherited_names if os.environ.get(name)
    }
    environment.update(
        {
            "ALLOW_LIVE_MODEL_CALLS": "false",
            "LEGAL_RAG_DATABASE_URL": database_url,
            "LEGAL_RAG_DISABLE_DOTENV": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    if os.name == "nt":
        venv_site_packages = Path(sys.prefix) / "Lib" / "site-packages"
        if not venv_site_packages.is_dir():
            raise AssertionError("the locked Windows virtualenv is unavailable")
        environment["PYTHONPATH"] = os.pathsep.join(
            (str(REPOSITORY_ROOT), str(venv_site_packages.resolve()))
        )
    log_handle = log_path.open("wb")
    process = subprocess.Popen(
        command,
        cwd=REPOSITORY_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    return ChildProcess(
        process=process,
        output_path=output_path,
        log_path=log_path,
        log_handle=log_handle,
    )


def wait_child_output(child: ChildProcess, *, timeout: float = 20.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child.output_path.is_file():
            return json.loads(child.output_path.read_text(encoding="utf-8"))
        if child.process.poll() is not None:
            child.log_handle.flush()
            diagnostics = child.log_path.read_text(encoding="utf-8", errors="replace")[
                -4000:
            ]
            configured_url = os.environ.get("LEGAL_RAG_DATABASE_URL", "")
            if configured_url:
                diagnostics = diagnostics.replace(
                    configured_url, "[database-url-redacted]"
                )
            diagnostics = re.sub(
                r"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s]+",
                "[database-url-redacted]",
                diagnostics,
                flags=re.IGNORECASE,
            )
            raise AssertionError(
                "M5 child exited before publishing evidence: "
                f"{child.process.returncode}\n{diagnostics}"
            )
        time.sleep(0.02)
    raise AssertionError("M5 child did not publish evidence before timeout")


def wait_child_exit(child: ChildProcess, *, timeout: float = 20.0) -> dict[str, Any]:
    payload = wait_child_output(child, timeout=timeout)
    return_code = child.process.wait(timeout=timeout)
    child.log_handle.close()
    assert return_code == 0
    return payload


def hard_kill(child: ChildProcess, *, actual_pid: int) -> int:
    import signal

    if type(actual_pid) is not int or actual_pid <= 0:
        raise AssertionError("hard-kill target PID must be a positive integer")
    if child.process.poll() is not None:
        raise AssertionError("hard-kill target is no longer running")
    if actual_pid != child.process.pid:
        raise AssertionError("hard-kill evidence PID does not match the child process")
    os.kill(actual_pid, signal.SIGTERM if os.name == "nt" else signal.SIGKILL)
    try:
        return_code = child.process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.process.kill()
        return_code = child.process.wait(timeout=10)
    child.log_handle.close()
    if return_code == 0:
        raise AssertionError(
            "hard-kill target exited successfully instead of being killed"
        )
    return return_code


def stop_child(child: ChildProcess) -> None:
    if child.process.poll() is None:
        child.process.kill()
        child.process.wait(timeout=10)
    if not child.log_handle.closed:
        child.log_handle.close()


def record_scenario(engine: Engine, test_id: str, evidence: dict[str, Any]) -> None:
    target = os.environ.get("LEGAL_RAG_M5_FAULT_RECEIPT", "").strip()
    if not target:
        return
    if test_id not in M5_TEST_SELECTORS:
        raise AssertionError("unknown M5 receipt scenario")
    candidate_sha = os.environ.get("LEGAL_RAG_M5_CANDIDATE_SHA", "").strip().lower()
    if _SHA.fullmatch(candidate_sha) is None:
        raise AssertionError("LEGAL_RAG_M5_CANDIDATE_SHA must be an exact commit SHA")

    receipt_path = Path(target)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = receipt_path.with_name(receipt_path.name + ".lock")
    lock_fd: int | None = None
    deadline = time.monotonic() + 30.0
    while lock_fd is None and time.monotonic() < deadline:
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            time.sleep(0.02)
    if lock_fd is None:
        raise AssertionError("could not acquire the M5 receipt lock")
    os.close(lock_fd)

    temporary = receipt_path.with_name(
        f".{receipt_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        payload = _load_or_create_receipt(receipt_path, candidate_sha, engine=engine)
        scenarios = payload["scenarios"]
        current = scenarios.get(test_id)
        merged = dict(current["evidence"]) if isinstance(current, dict) else {}
        for key, value in evidence.items():
            if key in merged and merged[key] != value:
                raise AssertionError(
                    f"conflicting M5 receipt evidence for {test_id}.{key}"
                )
            merged[key] = value
        scenarios[test_id] = {
            "status": "passed",
            "test_selectors": list(M5_TEST_SELECTORS[test_id]),
            "evidence": merged,
        }
        if set(scenarios) == set(M5_TEST_SELECTORS):
            payload["status"] = "passed"
            from scripts.quality_gate import validate_m5_fault_receipt_payload

            validation_errors = validate_m5_fault_receipt_payload(
                payload,
                expected_sha=candidate_sha,
                expected_migration_head=payload["database"]["migration_head"],
            )
            if validation_errors:
                raise AssertionError(
                    "M5 fault receipt failed its closed-schema validation: "
                    + "; ".join(validation_errors)
                )
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, receipt_path)
    finally:
        temporary.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)


def _database_migration_head(engine: Engine) -> str:
    """Observe the database used by this test, not the checkout's target head."""
    with engine.connect() as connection:
        heads = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    if (
        len(heads) != 1
        or not isinstance(heads[0], str)
        or re.fullmatch(r"[a-z0-9_]{1,64}", heads[0]) is None
    ):
        raise AssertionError("M5 receipt requires a unique database migration head")
    return heads[0]


def _load_or_create_receipt(
    path: Path, candidate_sha: str, *, engine: Engine
) -> dict[str, Any]:
    migration_head = _database_migration_head(engine)
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("candidate_sha") != candidate_sha:
            raise AssertionError("M5 receipt candidate SHA changed during the suite")
        database = payload.get("database")
        if not isinstance(database, dict) or database.get("migration_head") != migration_head:
            raise AssertionError("M5 receipt database migration head changed during the suite")
        return payload
    return {
        "schema_version": 1,
        "milestone": "M5",
        "candidate_sha": candidate_sha,
        "status": "in_progress",
        "live_model_calls": False,
        "database": {
            "backend": "postgresql",
            "checkpointer_backend": "langgraph-postgresql",
            "persistent": True,
            "in_memory": False,
            "migration_head": migration_head,
        },
        "scenarios": {},
        "redaction": {
            "contains_prompts": False,
            "contains_evidence_text": False,
            "contains_credentials": False,
            "contains_database_url": False,
        },
    }


__all__ = [
    "ChildProcess",
    "M5RunCase",
    "checkpoint_identity",
    "create_run_case",
    "ensure_persistent_checkpointer",
    "expire_run_deadline",
    "expire_run_lease",
    "hard_kill",
    "record_scenario",
    "start_child",
    "state_for_checkpoint",
    "stop_child",
    "wait_child_exit",
    "wait_child_output",
]
