from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select

from integration_tests.m4_support import authorization, seed_m4_service_boundary
from integration_tests.m5_support import ensure_persistent_checkpointer, record_scenario
from legal_rag.api.app import create_app
from legal_rag.api.auth import TokenAuthenticator
from legal_rag.api.settings import ServiceSettings
from legal_rag.harness.state import HARNESS_GRAPH_VERSION
from legal_rag.services.run_executor import DeterministicRunExecutor
from legal_rag.services.run_service import RunService, canonical_json_sha256
from legal_rag.services.supervisor import RunSupervisor
from legal_rag.storage.schema import idempotency_keys, messages, runs


def _create_session(client: TestClient, token: str, title: str) -> str:
    response = client.post(
        "/api/v1/sessions",
        headers=authorization(token),
        json={"title": title},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["session_id"])


def _submit_run(
    client: TestClient,
    *,
    token: str,
    session_id: str,
    idempotency_key: str,
    question: str,
    parent_run_id: str | None = None,
):
    body: dict[str, object] = {
        "question": question,
        "retrieval": {"top_k": 3},
    }
    if parent_run_id is not None:
        body["parent_run_id"] = parent_run_id
    headers = authorization(token)
    headers["Idempotency-Key"] = idempotency_key
    return client.post(
        f"/api/v1/sessions/{session_id}/runs",
        headers=headers,
        json=body,
    )


def _terminalize_next_run(
    service: RunService,
    *,
    expected_run_id: str,
    completion_status: str,
    stop_reason: str | None,
) -> None:
    worker_id = f"m5-followup-{uuid.uuid4()}"
    claimed = service.claim_next_run(worker_id, lease_seconds=30)
    assert claimed is not None and claimed.run_id == expected_run_id
    frozen = service.load_execution_input(
        claimed.run_id,
        worker_id,
        lease_epoch=claimed.lease_epoch,
    )
    base = DeterministicRunExecutor().execute(frozen.to_execution_input())
    answer_text = (
        "请补充争议发生时间和适用地区。"
        if completion_status == "needs_clarification"
        else base.answer_text
    )
    answer_payload = dict(base.answer_payload)
    answer_payload.update(
        {
            "answer_text": answer_text,
            "answer_mode": (
                "needs_clarification"
                if completion_status == "needs_clarification"
                else answer_payload["answer_mode"]
            ),
            "clarification_question": (
                "争议发生在什么时间和地区？"
                if completion_status == "needs_clarification"
                else None
            ),
        }
    )
    service.publish_terminal_result(
        claimed.run_id,
        worker_id,
        claimed.lease_epoch,
        {
            "answer_text": answer_text,
            "answer_payload": answer_payload,
            "evidence_payload": dict(base.evidence_payload),
            "verification_payload": dict(base.verification_payload),
        },
        completion_status=completion_status,
        stop_reason=stop_reason,
    )


def _session_write_counts(engine: Engine, session_id: str) -> dict[str, int]:
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


def test_m5_t04_clarification_followup_creates_new_parent_bound_run(
    migrated_engine: Engine,
) -> None:
    ensure_persistent_checkpointer(migrated_engine)
    boundary = seed_m4_service_boundary(migrated_engine)
    service = RunService(migrated_engine)
    settings = ServiceSettings(
        graph_version=HARNESS_GRAPH_VERSION,
        lease_seconds=30,
        executor_timeout_seconds=5,
    )
    supervisor = RunSupervisor(
        service,
        DeterministicRunExecutor(),
        lease_seconds=settings.lease_seconds,
        execution_timeout_seconds=settings.executor_timeout_seconds,
    )
    app = create_app(
        service=service,
        authenticator=TokenAuthenticator(
            {
                boundary.owner_token: boundary.owner,
                boundary.other_token: boundary.other,
            }
        ),
        supervisor=supervisor,
        settings=settings,
        start_supervisor=False,
    )

    with TestClient(app) as client:
        session_id = _create_session(client, boundary.owner_token, "M5 澄清链")
        parent_response = _submit_run(
            client,
            token=boundary.owner_token,
            session_id=session_id,
            idempotency_key="m5-followup-parent-one",
            question="这个争议适用哪条规则？",
        )
        assert parent_response.status_code == 202, parent_response.text
        parent_run_id = str(parent_response.json()["run_id"])
        parent_payload = {
            "question": "这个争议适用哪条规则？",
            "retrieval": {"top_k": 3},
        }
        assert service.get_run(boundary.owner, parent_run_id).request_hash == (
            canonical_json_sha256(parent_payload)
        )
        legacy_hash_replay = _submit_run(
            client,
            token=boundary.owner_token,
            session_id=session_id,
            idempotency_key="m5-followup-parent-one",
            question=str(parent_payload["question"]),
        )
        assert legacy_hash_replay.status_code == 202
        assert legacy_hash_replay.json()["run_id"] == parent_run_id
        assert legacy_hash_replay.json()["replayed"] is True
        _terminalize_next_run(
            service,
            expected_run_id=parent_run_id,
            completion_status="needs_clarification",
            stop_reason="clarification_required",
        )
        parent_budget_before = service.get_budget(parent_run_id)

        parent_status = client.get(
            f"/api/v1/runs/{parent_run_id}",
            headers=authorization(boundary.owner_token),
        )
        assert parent_status.status_code == 200, parent_status.text
        assert parent_status.json()["status"] == "needs_clarification"
        assert parent_status.json()["stop_reason"] == "clarification_required"
        assert parent_status.json()["answer"]["clarification_question"]

        other_session_id = _create_session(client, boundary.owner_token, "M5 其他会话")
        cross_session = _submit_run(
            client,
            token=boundary.owner_token,
            session_id=other_session_id,
            idempotency_key="m5-cross-session-parent",
            question="补充：发生在 2026 年。",
            parent_run_id=parent_run_id,
        )
        assert cross_session.status_code == 404
        assert _session_write_counts(migrated_engine, other_session_id) == {
            "runs": 0,
            "messages": 0,
            "keys": 0,
        }

        foreign_session_id = _create_session(
            client, boundary.other_token, "M5 其他用户"
        )
        foreign_parent = _submit_run(
            client,
            token=boundary.other_token,
            session_id=foreign_session_id,
            idempotency_key="m5-foreign-parent",
            question="尝试引用其他用户的运行。",
            parent_run_id=parent_run_id,
        )
        assert foreign_parent.status_code == 404
        assert _session_write_counts(migrated_engine, foreign_session_id) == {
            "runs": 0,
            "messages": 0,
            "keys": 0,
        }
        nonexistent_counts = _session_write_counts(migrated_engine, other_session_id)
        nonexistent_parent = _submit_run(
            client,
            token=boundary.owner_token,
            session_id=other_session_id,
            idempotency_key="m5-nonexistent-parent",
            question="尝试引用不存在的运行。",
            parent_run_id="00000000-0000-0000-0000-000000000000",
        )
        assert nonexistent_parent.status_code == 404
        assert (
            _session_write_counts(migrated_engine, other_session_id)
            == nonexistent_counts
        )

        second_parent_response = _submit_run(
            client,
            token=boundary.owner_token,
            session_id=session_id,
            idempotency_key="m5-followup-parent-two",
            question="另一个待完成问题。",
        )
        assert second_parent_response.status_code == 202
        second_parent_id = str(second_parent_response.json()["run_id"])
        nonterminal_parent = _submit_run(
            client,
            token=boundary.owner_token,
            session_id=session_id,
            idempotency_key="m5-nonterminal-parent",
            question="不能挂到仍在执行的父运行。",
            parent_run_id=second_parent_id,
        )
        assert nonterminal_parent.status_code == 404
        assert _session_write_counts(migrated_engine, session_id) == {
            "runs": 2,
            "messages": 3,
            "keys": 2,
        }
        _terminalize_next_run(
            service,
            expected_run_id=second_parent_id,
            completion_status="succeeded",
            stop_reason=None,
        )

        cancelled_parent_response = _submit_run(
            client,
            token=boundary.owner_token,
            session_id=session_id,
            idempotency_key="m5-cancelled-parent-source",
            question="这个运行将被取消。",
        )
        assert cancelled_parent_response.status_code == 202
        cancelled_parent_id = str(cancelled_parent_response.json()["run_id"])
        assert (
            service.cancel_run(boundary.owner, cancelled_parent_id).status
            == "cancelled"
        )
        cancelled_counts = _session_write_counts(migrated_engine, session_id)
        cancelled_parent = _submit_run(
            client,
            token=boundary.owner_token,
            session_id=session_id,
            idempotency_key="m5-cancelled-parent-child",
            question="不能挂到已取消的运行。",
            parent_run_id=cancelled_parent_id,
        )
        assert cancelled_parent.status_code == 404
        assert _session_write_counts(migrated_engine, session_id) == cancelled_counts

        failed_parent_response = _submit_run(
            client,
            token=boundary.owner_token,
            session_id=session_id,
            idempotency_key="m5-failed-parent-source",
            question="这个运行将失败。",
        )
        assert failed_parent_response.status_code == 202
        failed_parent_id = str(failed_parent_response.json()["run_id"])
        failed_worker = f"m5-followup-failed-{uuid.uuid4()}"
        claimed_failed = service.claim_next_run(failed_worker, lease_seconds=30)
        assert claimed_failed is not None and claimed_failed.run_id == failed_parent_id
        service.fail_run(
            failed_parent_id,
            failed_worker,
            "synthetic_failure",
            {"stage": "execution", "retryable": False},
            lease_epoch=claimed_failed.lease_epoch,
        )
        failed_counts = _session_write_counts(migrated_engine, session_id)
        failed_parent = _submit_run(
            client,
            token=boundary.owner_token,
            session_id=session_id,
            idempotency_key="m5-failed-parent-child",
            question="不能挂到失败的运行。",
            parent_run_id=failed_parent_id,
        )
        assert failed_parent.status_code == 404
        assert _session_write_counts(migrated_engine, session_id) == failed_counts

        child_payload = {
            "question": "补充：争议发生在 2026 年，地点为测试地区。",
            "retrieval": {"top_k": 3},
        }
        child_response = _submit_run(
            client,
            token=boundary.owner_token,
            session_id=session_id,
            idempotency_key="m5-parent-bound-child",
            question=str(child_payload["question"]),
            parent_run_id=parent_run_id,
        )
        assert child_response.status_code == 202, child_response.text
        child_run_id = str(child_response.json()["run_id"])
        assert child_run_id != parent_run_id

        child_status = client.get(
            f"/api/v1/runs/{child_run_id}",
            headers=authorization(boundary.owner_token),
        )
        assert child_status.status_code == 200
        assert child_status.json()["parent_run_id"] == parent_run_id
        stored_child = service.get_run(boundary.owner, child_run_id)
        assert stored_child.parent_run_id == parent_run_id
        assert stored_child.request_payload == child_payload
        assert stored_child.request_hash == canonical_json_sha256(
            {"request": child_payload, "parent_run_id": parent_run_id}
        )

        replay = _submit_run(
            client,
            token=boundary.owner_token,
            session_id=session_id,
            idempotency_key="m5-parent-bound-child",
            question=str(child_payload["question"]),
            parent_run_id=parent_run_id,
        )
        assert replay.status_code == 202
        assert replay.json()["run_id"] == child_run_id
        assert replay.json()["replayed"] is True

        different_parent = _submit_run(
            client,
            token=boundary.owner_token,
            session_id=session_id,
            idempotency_key="m5-parent-bound-child",
            question=str(child_payload["question"]),
            parent_run_id=second_parent_id,
        )
        assert different_parent.status_code == 409
        assert different_parent.json()["error"]["code"] == "idempotency_conflict"

        child_worker = f"m5-followup-child-{uuid.uuid4()}"
        claimed_child = service.claim_next_run(child_worker, lease_seconds=30)
        assert claimed_child is not None and claimed_child.run_id == child_run_id
        child_input = service.load_execution_input(
            child_run_id,
            child_worker,
            lease_epoch=claimed_child.lease_epoch,
        )
        assert child_input.execution_deadline_at != (
            parent_budget_before.execution_deadline_at
        )
        assert any(
            message.run_id == parent_run_id
            and message.role == "assistant"
            and message.content == "请补充争议发生时间和适用地区。"
            for message in child_input.completed_history
        )
        assert service.get_budget(child_run_id).run_id == child_run_id
        assert service.get_budget(parent_run_id) == parent_budget_before
        assert service.cancel_run(boundary.owner, child_run_id).status == "cancelled"

    record_scenario(
        "M5-T04",
        {
            "clarification_followup_created_new_run": True,
            "parent_run_link_preserved": True,
            "original_run_budget_unchanged": True,
        },
    )
