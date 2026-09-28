from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, func, select

from integration_tests.m4_support import (
    LoopbackUvicornServer,
    assert_non_enumerating_404,
    authorization,
    read_sse_frames,
    run_counts,
    seed_m4_service_boundary,
    wait_for_run_status,
)
from legal_rag.api.app import create_app
from legal_rag.api.auth import ServicePrincipal, TokenAuthenticator
from legal_rag.api.settings import ServiceSettings
from legal_rag.services.run_executor import DeterministicRunExecutor
from legal_rag.services.run_service import (
    RunService,
    UnsafePayloadError,
    WorkerLeaseLostError,
)
from legal_rag.services.supervisor import RunSupervisor
from legal_rag.storage.database import DatabaseSettings, create_database_engine
from legal_rag.storage.schema import (
    idempotency_keys,
    messages,
    run_events,
    run_results,
    runs,
)


@dataclass(slots=True)
class _ServiceHarness:
    service: RunService
    supervisor: RunSupervisor
    owner_token: str
    other_token: str
    server: LoopbackUvicornServer

    @property
    def base_url(self) -> str:
        assert self.server.base_url is not None
        return self.server.base_url


def _app_harness(
    engine: Engine,
    *,
    executor: Any | None = None,
    start_supervisor: bool = False,
    settings: ServiceSettings | None = None,
) -> tuple[_ServiceHarness, Any]:
    boundary = seed_m4_service_boundary(engine)
    service = RunService(engine)
    resolved_settings = settings or ServiceSettings(
        lease_seconds=10,
        executor_timeout_seconds=2,
        supervisor_poll_seconds=0.01,
        sse_poll_seconds=0.01,
        sse_heartbeat_seconds=1,
    )
    supervisor = RunSupervisor(
        service,
        executor or DeterministicRunExecutor("安全确定性回答"),
        lease_seconds=resolved_settings.lease_seconds,
        execution_timeout_seconds=resolved_settings.executor_timeout_seconds,
        poll_seconds=resolved_settings.supervisor_poll_seconds,
    )
    authenticator = TokenAuthenticator(
        {
            boundary.owner_token: boundary.owner,
            boundary.other_token: boundary.other,
        }
    )
    app = create_app(
        service=service,
        authenticator=authenticator,
        supervisor=supervisor,
        settings=resolved_settings,
        start_supervisor=start_supervisor,
    )
    server = LoopbackUvicornServer(app)
    return (
        _ServiceHarness(
            service=service,
            supervisor=supervisor,
            owner_token=boundary.owner_token,
            other_token=boundary.other_token,
            server=server,
        ),
        boundary,
    )


def _create_session(client: httpx.Client, token: str, title: str = "M4") -> str:
    response = client.post(
        "/api/v1/sessions",
        headers=authorization(token),
        json={"title": title},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["session_id"])


def _submit_run(
    client: httpx.Client,
    token: str,
    session_id: str,
    key: str,
    question: str = "合成法律问题",
) -> httpx.Response:
    headers = authorization(token)
    headers["Idempotency-Key"] = key
    return client.post(
        f"/api/v1/sessions/{session_id}/runs",
        headers=headers,
        json={"question": question, "retrieval": {"top_k": 3}},
    )


def _session_counts(engine: Engine, session_id: str) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            "runs": int(
                connection.scalar(
                    select(func.count())
                    .select_from(runs)
                    .where(runs.c.session_id == session_id)
                )
                or 0
            ),
            "messages": int(
                connection.scalar(
                    select(func.count())
                    .select_from(messages)
                    .where(messages.c.session_id == session_id)
                )
                or 0
            ),
            "keys": int(
                connection.scalar(
                    select(func.count())
                    .select_from(idempotency_keys)
                    .where(idempotency_keys.c.session_id == session_id)
                )
                or 0
            ),
        }


def test_m4_t01_real_http_owner_isolation_is_non_enumerating(
    migrated_engine: Engine,
) -> None:
    harness, boundary = _app_harness(migrated_engine)
    with harness.server, httpx.Client(base_url=harness.base_url, timeout=5) as client:
        session_id = _create_session(client, harness.owner_token)
        created = _submit_run(
            client,
            harness.owner_token,
            session_id,
            "owner-key",
        )
        assert created.status_code == 202
        run_id = str(created.json()["run_id"])

        foreign_headers = authorization(harness.other_token)
        foreign_headers["X-User-ID"] = boundary.owner.user_id
        foreign_run_headers = dict(foreign_headers)
        foreign_run_headers["Idempotency-Key"] = "foreign-key"
        responses = (
            client.get(
                f"/api/v1/sessions/{session_id}/messages",
                headers=foreign_headers,
            ),
            client.post(
                f"/api/v1/sessions/{session_id}/runs",
                headers=foreign_run_headers,
                json={"question": "不能越权"},
            ),
            client.get(f"/api/v1/runs/{run_id}", headers=foreign_headers),
            client.get(
                f"/api/v1/runs/{run_id}/evidence",
                headers=foreign_headers,
            ),
            client.get(
                f"/api/v1/runs/{run_id}/events",
                headers=foreign_headers,
            ),
            client.post(
                f"/api/v1/runs/{run_id}/cancel",
                headers=foreign_headers,
            ),
            client.post(
                f"/api/v1/runs/{run_id}/resume",
                headers=foreign_headers,
            ),
        )
        for response in responses:
            assert_non_enumerating_404(response)

        owner_resume = client.post(
            f"/api/v1/runs/{run_id}/resume",
            headers=authorization(harness.owner_token),
        )
        assert owner_resume.status_code == 501
        assert owner_resume.json()["error"]["code"] == (
            "resume_not_supported_until_m5"
        )
        owner_cancel = client.post(
            f"/api/v1/runs/{run_id}/cancel",
            headers=authorization(harness.owner_token),
        )
        assert owner_cancel.status_code == 200
        assert owner_cancel.json()["status"] == "cancelled"


def test_m4_t02_concurrent_http_same_key_creates_one_run_message_and_answer(
    migrated_engine: Engine,
) -> None:
    harness, _ = _app_harness(migrated_engine)
    with harness.server, httpx.Client(base_url=harness.base_url, timeout=5) as client:
        session_id = _create_session(client, harness.owner_token, "并发幂等")
        barrier = threading.Barrier(8)

        def submit(_: int) -> dict[str, Any]:
            barrier.wait(timeout=5)
            with httpx.Client(base_url=harness.base_url, timeout=5) as worker:
                response = _submit_run(
                    worker,
                    harness.owner_token,
                    session_id,
                    "same-http-key",
                )
            assert response.status_code == 202, response.text
            return response.json()

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(submit, range(8)))

        run_ids = {str(item["run_id"]) for item in outcomes}
        assert len(run_ids) == 1
        assert sum(item["replayed"] is False for item in outcomes) == 1
        run_id = run_ids.pop()
        assert run_counts(migrated_engine, run_id) == {
            "runs": 1,
            "messages": 1,
            "results": 0,
            "keys": 1,
            "events": 1,
        }

        assert harness.supervisor.run_once() is True
        completed = wait_for_run_status(
            client,
            run_id,
            harness.owner_token,
            {"succeeded"},
        )
        assert completed["answer"]["answer_text"] == "安全确定性回答"
        counts = run_counts(migrated_engine, run_id)
        assert counts == {
            "runs": 1,
            "messages": 2,
            "results": 1,
            "keys": 1,
            "events": 6,
        }

        replay = _submit_run(
            client,
            harness.owner_token,
            session_id,
            "same-http-key",
        )
        assert replay.status_code == 202
        assert replay.json()["run_id"] == run_id
        assert replay.json()["replayed"] is True


def test_m4_t03_same_key_different_body_is_409_without_orphans(
    migrated_engine: Engine,
) -> None:
    harness, _ = _app_harness(migrated_engine)
    with harness.server, httpx.Client(base_url=harness.base_url, timeout=5) as client:
        session_id = _create_session(client, harness.owner_token)
        first = _submit_run(
            client,
            harness.owner_token,
            session_id,
            "fixed-http-key",
            "第一个问题",
        )
        conflicting = _submit_run(
            client,
            harness.owner_token,
            session_id,
            "fixed-http-key",
            "不同正文",
        )

        assert first.status_code == 202
        assert conflicting.status_code == 409
        assert conflicting.json()["error"]["code"] == "idempotency_conflict"
        assert _session_counts(migrated_engine, session_id) == {
            "runs": 1,
            "messages": 1,
            "keys": 1,
        }
        run_id = str(first.json()["run_id"])
        cancelled = client.post(
            f"/api/v1/runs/{run_id}/cancel",
            headers=authorization(harness.owner_token),
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"


def test_m4_t03_concurrent_different_keys_preserve_single_active_run(
    migrated_engine: Engine,
) -> None:
    harness, _ = _app_harness(migrated_engine)
    with harness.server, httpx.Client(base_url=harness.base_url, timeout=5) as client:
        session_id = _create_session(client, harness.owner_token)
        barrier = threading.Barrier(2)

        def submit(index: int) -> httpx.Response:
            barrier.wait(timeout=5)
            with httpx.Client(base_url=harness.base_url, timeout=5) as worker:
                return _submit_run(
                    worker,
                    harness.owner_token,
                    session_id,
                    f"different-key-{index}",
                    f"并发问题 {index}",
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(submit, range(2)))

        assert sorted(response.status_code for response in responses) == [202, 409]
        conflict = next(response for response in responses if response.status_code == 409)
        assert conflict.json()["error"]["code"] == "active_run_conflict"
        assert _session_counts(migrated_engine, session_id) == {
            "runs": 1,
            "messages": 1,
            "keys": 1,
        }
        accepted = next(
            response for response in responses if response.status_code == 202
        )
        cancelled = client.post(
            f"/api/v1/runs/{accepted.json()['run_id']}/cancel",
            headers=authorization(harness.owner_token),
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"


def test_m4_t05_sse_disconnect_reconnect_replays_ordered_suffix_without_new_run(
    migrated_engine: Engine,
) -> None:
    harness, boundary = _app_harness(migrated_engine)
    with harness.server, httpx.Client(base_url=harness.base_url, timeout=5) as client:
        session_id = _create_session(client, harness.owner_token, "SSE")
        created = _submit_run(
            client,
            harness.owner_token,
            session_id,
            "sse-key",
        )
        run_id = str(created.json()["run_id"])
        claimed = harness.service.claim_next_run("sse-worker", 30)
        assert claimed is not None and claimed.run_id == run_id

        with client.stream(
            "GET",
            f"/api/v1/runs/{run_id}/events",
            headers=authorization(harness.owner_token),
        ) as first_stream:
            assert first_stream.status_code == 200
            first_frames = read_sse_frames(first_stream, stop_after_sequence=2)
        assert [frame["id"] for frame in first_frames] == [1, 2]
        running = harness.service.get_run(boundary.owner, run_id)
        assert running.status == "running"
        assert running.lease_owner == "sse-worker"

        harness.service.append_stage_event(
            run_id,
            "retrieval.completed",
            {
                "result_count": 0,
                "checked_result_count": 0,
                "rejected_count": 0,
                "stop_reason": "synthetic",
            },
            worker_id="sse-worker",
        )
        harness.service.append_stage_event(
            run_id,
            "generation.started",
            {},
            worker_id="sse-worker",
        )
        harness.service.append_stage_event(
            run_id,
            "verification.completed",
            {"passed": True, "fallback_used": False},
            worker_id="sse-worker",
        )
        harness.service.publish_success(
            run_id,
            "sse-worker",
            {
                "answer_text": "SSE 安全回答",
                "answer_payload": {
                    "answer_text": "SSE 安全回答",
                    "answer_mode": "insufficient_evidence",
                    "claims": [],
                },
                "evidence_payload": {"sources": [], "status": "safe"},
                "verification_payload": {
                    "passed": True,
                    "fallback_used": False,
                },
            },
        )

        replay_headers = authorization(harness.owner_token)
        replay_headers["Last-Event-ID"] = "2"
        with client.stream(
            "GET",
            f"/api/v1/runs/{run_id}/events?after=1",
            headers=replay_headers,
        ) as replay_stream:
            replay_frames = read_sse_frames(replay_stream)
        assert [frame["id"] for frame in replay_frames] == [3, 4, 5, 6]
        assert replay_frames[-1]["event"] == "answer.final"
        visible = client.get(
            f"/api/v1/runs/{run_id}",
            headers=authorization(harness.owner_token),
        )
        assert visible.status_code == 200
        assert visible.json()["answer"]["answer_text"] == "SSE 安全回答"
        assert visible.json()["verification"]["passed"] is True
        assert run_counts(migrated_engine, run_id) == {
            "runs": 1,
            "messages": 2,
            "results": 1,
            "keys": 1,
            "events": 6,
        }
        replay = _submit_run(
            client,
            harness.owner_token,
            session_id,
            "sse-key",
        )
        assert replay.status_code == 202
        assert replay.json()["run_id"] == run_id
        assert replay.json()["replayed"] is True


def test_m4_t06_rejected_draft_never_crosses_http_sse_or_persistence(
    migrated_engine: Engine,
) -> None:
    harness, boundary = _app_harness(migrated_engine)
    marker = "REJECTED_DRAFT_SENTINEL_M4"
    with harness.server, httpx.Client(base_url=harness.base_url, timeout=5) as client:
        session_id = _create_session(client, harness.owner_token, "草稿隔离")
        created = _submit_run(
            client,
            harness.owner_token,
            session_id,
            "draft-key",
        )
        run_id = str(created.json()["run_id"])
        claimed = harness.service.claim_next_run("draft-worker", 30)
        assert claimed is not None and claimed.run_id == run_id
        with pytest.raises(UnsafePayloadError):
            harness.service.publish_success(
                run_id,
                "draft-worker",
                {
                    "answer_text": marker,
                    "answer_payload": {
                        "answer_text": marker,
                        "answer_mode": "direct",
                        "raw_response": marker,
                    },
                    "evidence_payload": {"sources": []},
                    "verification_payload": {"passed": False},
                },
            )
        harness.service.fail_run(
            run_id,
            "draft-worker",
            "verification_rejected",
            {"stage": "verification", "retryable": False},
        )

        with client.stream(
            "GET",
            f"/api/v1/runs/{run_id}/events",
            headers=authorization(harness.owner_token),
        ) as response:
            frames = read_sse_frames(response)
        status = client.get(
            f"/api/v1/runs/{run_id}",
            headers=authorization(harness.owner_token),
        )
        evidence = client.get(
            f"/api/v1/runs/{run_id}/evidence",
            headers=authorization(harness.owner_token),
        )
        transcript = client.get(
            f"/api/v1/sessions/{session_id}/messages",
            headers=authorization(harness.owner_token),
        )
        with migrated_engine.connect() as connection:
            persisted = {
                "events": connection.scalars(
                    select(run_events.c.safe_payload).where(
                        run_events.c.run_id == run_id
                    )
                ).all(),
                "results": connection.scalars(
                    select(run_results.c.answer_payload).where(
                        run_results.c.run_id == run_id
                    )
                ).all(),
                "messages": connection.scalars(
                    select(messages.c.content).where(messages.c.run_id == run_id)
                ).all(),
            }
        exposed = json.dumps(
            {
                "frames": frames,
                "status": status.json(),
                "evidence": evidence.json(),
                "transcript": transcript.json(),
                "persisted": persisted,
            },
            ensure_ascii=False,
            default=str,
        )
        assert marker not in exposed
        assert "raw_response" not in exposed
        assert "raw_model_output" not in exposed
        assert status.json()["status"] == "failed"
        assert status.json()["error_code"] == "verification_rejected"
        assert evidence.json()["evidence"] is None
        assert [frame["event"] for frame in frames][-1] == "run.failed"
        assert "answer.final" not in [frame["event"] for frame in frames]
        assert len(transcript.json()["items"]) == 1
        assert boundary.owner.user_id == harness.service.get_run(
            boundary.owner, run_id
        ).user_id


def test_m4_t07_database_unavailable_is_redacted_and_attributable() -> None:
    unavailable = create_database_engine(
        DatabaseSettings(
            "postgresql+psycopg://m4_user:m4_secret@127.0.0.1:1/m4_unavailable",
            connect_timeout_seconds=1,
            pool_timeout_seconds=1,
            statement_timeout_ms=1_000,
            lock_timeout_ms=1_000,
        )
    )
    principal = ServicePrincipal(
        user_id="m4-unavailable-user",
        scope_id="m4-unavailable-scope",
        profile_id="a" * 64,
    )
    token = "m4-unavailable-token-" + "a" * 32
    service = RunService(unavailable)
    supervisor = RunSupervisor(
        service,
        DeterministicRunExecutor(),
        lease_seconds=10,
        execution_timeout_seconds=2,
    )
    app = create_app(
        service=service,
        authenticator=TokenAuthenticator({token: principal}),
        supervisor=supervisor,
        start_supervisor=False,
    )
    server = LoopbackUvicornServer(app)
    try:
        with server, httpx.Client(base_url=server.base_url, timeout=5) as client:
            live = client.get("/health/live")
            ready = client.get("/health/ready")
            write = client.post(
                "/api/v1/sessions",
                headers=authorization(token),
                json={},
            )
        assert live.status_code == 200
        for response in (ready, write):
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "database_unavailable"
            lowered = response.text.casefold()
            assert "m4_secret" not in lowered
            assert "127.0.0.1:1" not in lowered
            assert "psycopg" not in lowered
    finally:
        unavailable.dispose()


class _TimeoutThenSuccessExecutor:
    def __init__(self) -> None:
        self.release_first = threading.Event()
        self.first_finished = threading.Event()
        self.callback_error: Exception | None = None
        self._calls = 0
        self._lock = threading.Lock()
        self._success = DeterministicRunExecutor("超时后的安全回答")

    def execute(self, execution_input, callback):
        with self._lock:
            self._calls += 1
            call_number = self._calls
        if call_number == 1:
            self.release_first.wait(timeout=5)
            try:
                callback("generation.started", {"late": True})
            except Exception as exc:
                self.callback_error = exc
            finally:
                self.first_finished.set()
            return self._success.execute(execution_input)
        return self._success.execute(execution_input, callback)


def test_m4_t07_model_timeout_is_attributable_and_does_not_poison_next_run(
    migrated_engine: Engine,
) -> None:
    executor = _TimeoutThenSuccessExecutor()
    settings = ServiceSettings(
        lease_seconds=2,
        executor_timeout_seconds=0.15,
        supervisor_poll_seconds=0.01,
        sse_poll_seconds=0.01,
        sse_heartbeat_seconds=1,
    )
    harness, boundary = _app_harness(
        migrated_engine,
        executor=executor,
        start_supervisor=True,
        settings=settings,
    )
    try:
        with harness.server, httpx.Client(
            base_url=harness.base_url,
            timeout=5,
        ) as client:
            session_id = _create_session(client, harness.owner_token, "超时隔离")
            first = _submit_run(
                client,
                harness.owner_token,
                session_id,
                "timeout-key",
                "第一个阻塞问题",
            )
            first_run_id = str(first.json()["run_id"])
            first_status = wait_for_run_status(
                client,
                first_run_id,
                harness.owner_token,
                {"failed"},
            )
            assert first_status["error_code"] == "model_timeout"

            second = _submit_run(
                client,
                harness.owner_token,
                session_id,
                "success-key",
                "第二个立即完成问题",
            )
            assert second.status_code == 202, second.text
            second_run_id = str(second.json()["run_id"])
            second_status = wait_for_run_status(
                client,
                second_run_id,
                harness.owner_token,
                {"succeeded"},
            )
            assert second_status["answer"]["answer_text"] == "超时后的安全回答"

            with client.stream(
                "GET",
                f"/api/v1/runs/{first_run_id}/events",
                headers=authorization(harness.owner_token),
            ) as response:
                frames = read_sse_frames(response)
            assert frames[-1]["event"] == "run.failed"
            assert frames[-1]["data"]["payload"]["error_code"] == "model_timeout"
            assert frames[-1]["data"]["payload"]["retryable"] is True

            executor.release_first.set()
            assert executor.first_finished.wait(timeout=2)
            assert isinstance(executor.callback_error, WorkerLeaseLostError)
            assert harness.service.get_run(
                boundary.owner, first_run_id
            ).status == "failed"
    finally:
        executor.release_first.set()


def test_m4_t07_input_too_long_is_422_without_persistence(
    migrated_engine: Engine,
) -> None:
    settings = ServiceSettings(
        question_max_characters=5,
        lease_seconds=10,
        executor_timeout_seconds=2,
        supervisor_poll_seconds=0.01,
        sse_poll_seconds=0.01,
        sse_heartbeat_seconds=1,
    )
    harness, _ = _app_harness(migrated_engine, settings=settings)
    with harness.server, httpx.Client(base_url=harness.base_url, timeout=5) as client:
        session_id = _create_session(client, harness.owner_token, "长度限制")
        response = _submit_run(
            client,
            harness.owner_token,
            session_id,
            "too-long-key",
            "123456",
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "input_too_long"
        assert _session_counts(migrated_engine, session_id) == {
            "runs": 0,
            "messages": 0,
            "keys": 0,
        }
