from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import httpx
from sqlalchemy import Engine

from integration_tests.m4_process_app import BLOCKING_QUESTION
from integration_tests.m4_support import (
    authorization,
    read_sse_frames,
    seed_m4_service_boundary,
    wait_for_run_status,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@dataclass(slots=True)
class _ApplicationProcess:
    process: subprocess.Popen[bytes]
    base_url: str
    log_handle: BinaryIO


def _start_application_process(
    tmp_path: Path,
    *,
    name: str,
    environment: dict[str, str],
    executor_mode: str,
) -> _ApplicationProcess:
    port_file = tmp_path / f"{name}-port.txt"
    log_path = tmp_path / f"{name}.log"
    child_environment = dict(environment)
    child_environment["M4_TEST_EXECUTOR_MODE"] = executor_mode
    log_handle = log_path.open("wb")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "integration_tests.m4_process_app",
            "--port-file",
            str(port_file),
        ],
        cwd=REPOSITORY_ROOT,
        env=child_environment,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + 15.0
    port: int | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            log_handle.close()
            raise AssertionError(
                f"{name} exited before startup with code {process.returncode}; "
                f"sanitized diagnostics are in {log_path.name}"
            )
        if port_file.is_file():
            raw_port = port_file.read_text(encoding="ascii").strip()
            if raw_port.isdigit():
                port = int(raw_port)
                break
        time.sleep(0.02)
    if port is None:
        process.terminate()
        process.wait(timeout=5)
        log_handle.close()
        raise AssertionError(f"{name} did not publish its loopback port")
    base_url = f"http://127.0.0.1:{port}"
    with httpx.Client(base_url=base_url, timeout=2) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                log_handle.close()
                raise AssertionError(
                    f"{name} exited during readiness with code {process.returncode}; "
                    f"sanitized diagnostics are in {log_path.name}"
                )
            try:
                live = client.get("/health/live")
                ready = client.get("/health/ready")
            except httpx.TransportError:
                time.sleep(0.02)
                continue
            if live.status_code == 200 and ready.status_code == 200:
                return _ApplicationProcess(process, base_url, log_handle)
            time.sleep(0.02)
    process.terminate()
    process.wait(timeout=5)
    log_handle.close()
    raise AssertionError(f"{name} did not become ready")


def _stop_application_process(application: _ApplicationProcess) -> int:
    process = application.process
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        return int(process.returncode or 0)
    finally:
        application.log_handle.close()


def _create_session(client: httpx.Client, token: str, title: str) -> str:
    response = client.post(
        "/api/v1/sessions",
        headers=authorization(token),
        json={"title": title},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["session_id"])


def _submit(
    client: httpx.Client,
    token: str,
    session_id: str,
    key: str,
    question: str,
) -> httpx.Response:
    headers = authorization(token)
    headers["Idempotency-Key"] = key
    return client.post(
        f"/api/v1/sessions/{session_id}/runs",
        headers=headers,
        json={"question": question, "retrieval": {"top_k": 3}},
    )


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def test_m4_t04_independent_process_restart_preserves_history_and_interrupts_stale_run(
    migrated_engine: Engine,
    integration_database_url: str,
    tmp_path: Path,
) -> None:
    boundary = seed_m4_service_boundary(migrated_engine)
    token_registry = {
        boundary.owner_token: {
            "user_id": boundary.owner.user_id,
            "scope_id": boundary.owner.scope_id,
            "profile_id": boundary.owner.profile_id,
        }
    }
    environment = os.environ.copy()
    environment.update(
        {
            "ALLOW_LIVE_MODEL_CALLS": "false",
            "LEGAL_RAG_AUTH_TOKENS_JSON": json.dumps(token_registry),
            "LEGAL_RAG_DATABASE_URL": integration_database_url,
            "LEGAL_RAG_DISABLE_DOTENV": "1",
            "LEGAL_RAG_EXECUTOR_TIMEOUT_SECONDS": "4",
            "LEGAL_RAG_RUN_LEASE_SECONDS": "5",
            "LEGAL_RAG_SUPERVISOR_POLL_SECONDS": "0.05",
            "LEGAL_RAG_SSE_POLL_SECONDS": "0.02",
            "LEGAL_RAG_SSE_HEARTBEAT_SECONDS": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    first: _ApplicationProcess | None = None
    second: _ApplicationProcess | None = None
    first_exit_code: int | None = None
    try:
        first = _start_application_process(
            tmp_path,
            name="first-process",
            environment=environment,
            executor_mode="blocking",
        )
        first_pid = first.process.pid
        with httpx.Client(base_url=first.base_url, timeout=5) as client:
            completed_session = _create_session(
                client, boundary.owner_token, "完成历史"
            )
            completed_response = _submit(
                client,
                boundary.owner_token,
                completed_session,
                "completed-key",
                "完成的确定性问题",
            )
            assert completed_response.status_code == 202
            completed_run_id = str(completed_response.json()["run_id"])
            completed_before = wait_for_run_status(
                client,
                completed_run_id,
                boundary.owner_token,
                {"succeeded"},
                timeout=10,
            )
            evidence_before = client.get(
                f"/api/v1/runs/{completed_run_id}/evidence",
                headers=authorization(boundary.owner_token),
            ).json()
            messages_before = client.get(
                f"/api/v1/sessions/{completed_session}/messages",
                headers=authorization(boundary.owner_token),
            ).json()

            interrupted_session = _create_session(
                client, boundary.owner_token, "中断任务"
            )
            interrupted_response = _submit(
                client,
                boundary.owner_token,
                interrupted_session,
                "blocking-key",
                BLOCKING_QUESTION,
            )
            assert interrupted_response.status_code == 202
            interrupted_run_id = str(interrupted_response.json()["run_id"])
            wait_for_run_status(
                client,
                interrupted_run_id,
                boundary.owner_token,
                {"running"},
                timeout=5,
            )

        first_exit_code = _stop_application_process(first)
        first = None

        second = _start_application_process(
            tmp_path,
            name="second-process",
            environment=environment,
            executor_mode="deterministic",
        )
        second_pid = second.process.pid
        assert first_pid != second_pid
        with httpx.Client(base_url=second.base_url, timeout=10) as client:
            completed_after = client.get(
                f"/api/v1/runs/{completed_run_id}",
                headers=authorization(boundary.owner_token),
            )
            evidence_after = client.get(
                f"/api/v1/runs/{completed_run_id}/evidence",
                headers=authorization(boundary.owner_token),
            )
            messages_after = client.get(
                f"/api/v1/sessions/{completed_session}/messages",
                headers=authorization(boundary.owner_token),
            )
            assert completed_after.status_code == 200
            assert completed_after.json() == completed_before
            assert evidence_after.json() == evidence_before
            assert messages_after.json() == messages_before

            interrupted = wait_for_run_status(
                client,
                interrupted_run_id,
                boundary.owner_token,
                {"interrupted"},
                timeout=12,
            )
            assert interrupted["error_code"] == "worker_lease_expired"
            interrupted_messages = client.get(
                f"/api/v1/sessions/{interrupted_session}/messages",
                headers=authorization(boundary.owner_token),
            )
            assert interrupted_messages.status_code == 200
            assert [
                item["role"] for item in interrupted_messages.json()["items"]
            ] == ["user"]
            with client.stream(
                "GET",
                f"/api/v1/runs/{interrupted_run_id}/events",
                headers=authorization(boundary.owner_token),
            ) as response:
                events = read_sse_frames(response)
            assert events[-1]["event"] == "run.interrupted"
            assert events[-1]["data"]["payload"]["reason"] == (
                "worker_lease_expired"
            )
            assert "answer.final" not in [event["event"] for event in events]

            blocked = _submit(
                client,
                boundary.owner_token,
                interrupted_session,
                "blocked-key",
                "中断任务仍占活动槽位",
            )
            assert blocked.status_code == 409
            assert blocked.json()["error"]["code"] == "active_run_conflict"
            resume = client.post(
                f"/api/v1/runs/{interrupted_run_id}/resume",
                headers=authorization(boundary.owner_token),
            )
            assert resume.status_code == 501
            cancel = client.post(
                f"/api/v1/runs/{interrupted_run_id}/cancel",
                headers=authorization(boundary.owner_token),
            )
            assert cancel.status_code == 200
            assert cancel.json()["status"] == "cancelled"
            replacement = _submit(
                client,
                boundary.owner_token,
                interrupted_session,
                "replacement-key",
                "取消后允许新任务",
            )
            assert replacement.status_code == 202
            replacement_run_id = str(replacement.json()["run_id"])
            replacement_status = wait_for_run_status(
                client,
                replacement_run_id,
                boundary.owner_token,
                {"succeeded"},
                timeout=12,
            )
            assert replacement_status["answer"]["answer_text"] == "进程重启安全回答"

        receipt_target = os.environ.get(
            "LEGAL_RAG_M4_PROCESS_RECEIPT", ""
        ).strip()
        if receipt_target:
            receipt_path = Path(receipt_target)
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "stage": "m4_independent_process_restart_verified",
                        "first_pid": first_pid,
                        "second_pid": second_pid,
                        "pids_differ": first_pid != second_pid,
                        "first_exit_code": first_exit_code,
                        "completed_run_id": completed_run_id,
                        "completed_status": completed_before["status"],
                        "history_digest": _digest(messages_before),
                        "result_digest": _digest(completed_before),
                        "interrupted_run_id": interrupted_run_id,
                        "interrupted_status": interrupted["status"],
                        "interrupted_error_code": interrupted["error_code"],
                        "interrupted_final_event_sequence": events[-1]["id"],
                        "replacement_run_id": replacement_run_id,
                        "replacement_status": replacement_status["status"],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
                newline="\n",
            )
    finally:
        if first is not None:
            _stop_application_process(first)
        if second is not None:
            _stop_application_process(second)
