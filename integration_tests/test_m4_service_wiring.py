from __future__ import annotations

from sqlalchemy import Engine

from integration_tests.m4_support import seed_m4_service_corpus
from legal_rag.services.run_executor import LegalChatRunExecutor
from legal_rag.services.run_service import RunService
from legal_rag.services.service_retrieval import PostgresAssistantFactory
from legal_rag.services.supervisor import RunSupervisor


def test_m4_t08_provider_free_service_wiring_uses_frozen_postgres_corpus(
    migrated_engine: Engine,
) -> None:
    boundary = seed_m4_service_corpus(migrated_engine)
    service = RunService(migrated_engine)
    session = service.create_session(boundary.owner, "生产接线验证")
    run, replayed = service.create_run(
        boundary.owner,
        session.session_id,
        "provider-free-wiring",
        {
            "question": "服务端测试法第一条规定了什么？",
            "retrieval": {"top_k": 3},
        },
        "m4-linear-v1",
    )
    assert replayed is False
    supervisor = RunSupervisor(
        service,
        LegalChatRunExecutor(
            PostgresAssistantFactory(migrated_engine),
            generate=False,
        ),
        lease_seconds=10,
        execution_timeout_seconds=2,
    )

    assert supervisor.run_once() is True

    completed = service.get_run(boundary.owner, run.run_id)
    assert completed.status == "succeeded"
    assert completed.result is not None
    assert completed.result.answer_payload["answer_mode"] == "retrieval_only"
    assert "服务端测试法" in completed.result.answer_text
    assert completed.result.evidence_payload["scope_id"] == boundary.owner.scope_id
    assert completed.result.evidence_payload["snapshot_id"] == completed.snapshot_id
    assert completed.result.evidence_payload["profile_id"] == boundary.owner.profile_id
    assert completed.result.verification_payload == {
        "passed": True,
        "performed": False,
        "fallback_used": False,
    }
