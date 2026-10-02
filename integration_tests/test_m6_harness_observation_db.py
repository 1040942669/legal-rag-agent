"""M6 facts from real M5 PostgreSQL graph execution, with no live providers."""

import json

import pytest

from integration_tests.m5_support import create_run_case, ensure_persistent_checkpointer
from legal_rag.harness.runner import GraphRunExecutor
from legal_rag.llm import CompletionUsage
from legal_rag.observability.events import LocalJsonlObserver
from legal_rag.services.run_service import RunService
from legal_rag.services.service_retrieval import PostgresAssistantFactory


def execute(service, case, factory, observer, *, generate=False):
    worker = "m6-observation-worker"
    claimed = service.claim_next_run(worker, lease_seconds=30)
    assert claimed.run_id == case.run_id
    frozen = service.load_execution_input(
        case.run_id, worker, lease_epoch=claimed.lease_epoch
    )
    GraphRunExecutor(
        service, factory, generate=generate, observer=observer
    ).execute_claimed(
        frozen,
        worker,
        lambda event_type, payload: None,
    )


def test_m6_real_graph_records_nodes_evidence_cache_and_authoritative_budget(
    migrated_engine, tmp_path
):
    ensure_persistent_checkpointer(migrated_engine)
    service = RunService(migrated_engine)
    case = create_run_case(
        migrated_engine, service=service, case_name="m6-observe", real_corpus=True
    )
    trace = tmp_path / "events.jsonl"
    execute(
        service,
        case,
        PostgresAssistantFactory(migrated_engine),
        LocalJsonlObserver(trace),
    )
    events = [
        json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()
    ]
    assert events
    assert all(
        row["trace_id"] == case.run_id
        and row["run_id"] == case.run_id
        and row["session_id"] == case.session_id
        for row in events
    )
    nodes = [row for row in events if row["name"] == "node.completed"]
    assert {row["node"] for row in nodes} == {
        "analyze_query",
        "route",
        "retrieve",
        "merge_evidence",
        "check_evidence",
        "generate",
        "verify",
        "persist_result",
    }
    assert all(row["duration_ms"] >= 0 for row in events)
    retrieval = next(row for row in events if row["name"] == "tool.completed")
    assert retrieval["counts"] == {"tool_calls": 1}
    durable = service.get_budget(case.run_id)
    assert nodes[-1]["budget_used"]["tool_attempts"] == durable.tool_attempts_used == 1
    assert nodes[-1]["budget_used"]["model_attempts"] == 0
    assert next(row for row in nodes if row["node"] == "retrieve")["evidence_ids"]
    assert any(
        row["name"] == "cache.lookup" and row["cache_status"] == "hit" for row in events
    )
    assert not any(row["name"] == "model.completed" for row in events)
    for forbidden in ("question", "answer_text", "Authorization", "prompt", "text"):
        assert all(forbidden not in row for row in events)
    assert service.get_run(case.boundary.owner, case.run_id).status == "succeeded"


@pytest.mark.parametrize("broken_sink", [False, True])
def test_m6_real_graph_retry_usage_and_exporter_failure_preserve_results(
    migrated_engine, tmp_path, broken_sink
):
    ensure_persistent_checkpointer(migrated_engine)
    service = RunService(migrated_engine)
    case = create_run_case(
        migrated_engine, service=service, case_name="m6-observe-retry", real_corpus=True
    )

    class Client:
        hidden_retries_disabled = True
        propagate_control_errors = True

        def __init__(self):
            self.usage = CompletionUsage()

        def complete(self, prompt):
            self.usage.calls += 1
            if self.usage.calls == 1:
                raise TimeoutError("private timeout details")
            self.usage.record_tokens(input_tokens=12, output_tokens=7, total_tokens=19)
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

    client = Client()
    postgres_factory = PostgresAssistantFactory(migrated_engine)

    def factory(execution):
        assistant = postgres_factory(execution)
        assistant.llm = client
        return assistant

    trace = tmp_path / "events.jsonl"

    class Broken:
        def record(self, event):
            raise OSError("private exporter failure")

    execute(
        service,
        case,
        factory,
        Broken() if broken_sink else LocalJsonlObserver(trace),
        generate=True,
    )
    assert client.usage.calls == 2
    assert service.get_budget(case.run_id).model_attempts_used == 2
    assert service.get_run(case.boundary.owner, case.run_id).status == "succeeded"
    if not broken_sink:
        events = [
            json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()
        ]
        attempts = [row for row in events if row["name"] == "model.completed"]
        assert [row["retry_count"] for row in attempts] == [0, 1]
        assert [row["budget_used"]["model_attempts"] for row in attempts] == [1, 2]
        assert attempts[0]["error_category"] == "provider_timeout"
        assert attempts[0]["model_input_tokens"] is None
        assert attempts[1]["model_input_tokens"] == 12
        assert attempts[1]["model_output_tokens"] == 7
