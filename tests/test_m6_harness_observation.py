from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from legal_rag.harness.budget import AttemptReservation, BudgetSnapshot
from legal_rag.harness.observations import HarnessObservationAdapter
from legal_rag.llm import CompletionUsage


def test_completion_usage_preserves_existing_positional_constructor_and_snapshot():
    usage = CompletionUsage(1, 0, 2, 3, 5, 1, 12.5)
    assert usage.latency_ms == 12.5
    assert usage.snapshot() == {
        "calls": 1,
        "failed_calls": 0,
        "input_tokens": 2,
        "output_tokens": 3,
        "total_tokens": 5,
        "token_usage_calls": 1,
        "latency_ms": 12.5,
    }
    assert usage.input_usage_calls == 0
    assert usage.output_usage_calls == 0
    assert usage.total_usage_calls == 0


class Collector:
    def __init__(self):
        self.events = []

    def record(self, event):
        self.events.append(event)


def budget(**changes):
    values = dict(
        run_id="run-1",
        max_retrieval_rounds=2,
        max_queries_per_round=3,
        max_tool_attempts=8,
        max_model_attempts=4,
        max_embedding_attempts=4,
        max_retry_per_operation=1,
        evidence_top_k=5,
        retrieval_rounds_used=1,
        queries_used=1,
        tool_attempts_used=2,
        model_attempts_used=1,
        embedding_attempts_used=0,
        execution_deadline_at=datetime.now(timezone.utc) + timedelta(seconds=90),
        revision=3,
    )
    values.update(changes)
    return BudgetSnapshot(**values)


def adapter(observer, usage=None):
    return HarnessObservationAdapter(
        observer,
        run_id="run-1",
        session_id="session-1",
        persistence=SimpleNamespace(get_budget=lambda run_id: budget()),
        completion_client=SimpleNamespace(usage=usage),
    )


def reservation(kind="model", attempt_no=1):
    return AttemptReservation(
        "attempt-1",
        "run-1",
        1,
        "generate",
        kind,
        "generate_answer",
        attempt_no,
        "a" * 64,
    )


def test_actual_node_wrapper_records_duration_budget_evidence_and_correlation():
    sink = Collector()
    telemetry = adapter(sink)
    before = {"payload": {"retrieved_evidence_refs": []}}

    def actual_node(envelope):
        return {"payload": {"retrieved_evidence_refs": ["chunk-1"]}}

    result = telemetry.wrap_node("retrieve", actual_node)(before)
    event = sink.events[-1]
    assert result["payload"]["retrieved_evidence_refs"] == ["chunk-1"]
    assert event.context.run_id == "run-1"
    assert event.context.session_id == "session-1"
    assert event.context.trace_id == "run-1"
    assert event.duration_ms >= 0
    assert event.budget_used["tool_attempts"] == 2
    assert event.evidence_ids == ("chunk-1",)
    assert event.model_input_tokens is None


def test_actual_attempt_records_retry_error_and_unknown_provider_usage():
    sink = Collector()
    telemetry = adapter(sink)
    with pytest.raises(TimeoutError):
        with telemetry.attempt("generate", "generator", reservation(attempt_no=2)):
            raise TimeoutError("Authorization Bearer private")
    event = sink.events[-1]
    assert event.name == "model.completed"
    assert event.status == "failed"
    assert event.retry_count == 1
    assert event.error_category == "provider_timeout"
    assert event.duration_ms >= 0
    # A reserved model operation is not proof of a provider dispatch.
    assert event.counts == {}
    assert event.model_input_tokens is None
    assert "private" not in str(event.to_local_record())


def test_actual_provider_usage_components_need_complete_coverage():
    sink = Collector()
    usage = CompletionUsage()
    telemetry = adapter(sink, usage)
    with telemetry.attempt("generate", "generator", reservation()):
        usage.calls += 1
        usage.record_tokens(input_tokens=12, output_tokens=7, total_tokens=19)
    assert sink.events[-1].model_input_tokens == 12
    assert sink.events[-1].model_output_tokens == 7
    with telemetry.attempt("generate", "generator", reservation()):
        usage.calls += 1
        usage.record_tokens(input_tokens=None, output_tokens=3, total_tokens=20)
    assert sink.events[-1].model_input_tokens is None
    assert sink.events[-1].model_output_tokens == 3
    assert sink.events[-1].cost_usd is None


def test_model_attempt_without_dispatch_does_not_invent_model_calls():
    sink = Collector()
    telemetry = adapter(sink, CompletionUsage())
    with pytest.raises(ValueError):
        with telemetry.attempt("generate", "generator", reservation()):
            raise ValueError("invalid generation input")
    assert sink.events[-1].counts == {"model_calls": 0}
    assert sink.events[-1].budget_used["model_attempts"] == 1
    assert sink.events[-1].model_input_tokens is None


def test_optional_observer_failure_never_changes_node_result_or_exception():
    class Broken:
        def record(self, event):
            raise OSError("private exporter detail")

    telemetry = adapter(Broken())
    assert telemetry.wrap_node("route", lambda state: state)({"payload": {}}) == {
        "payload": {}
    }
    with pytest.raises(ValueError, match="actual node failure"):
        telemetry.wrap_node(
            "route",
            lambda state: (_ for _ in ()).throw(ValueError("actual node failure")),
        )({"payload": {}})


def test_cache_hit_is_recorded_only_after_successful_artifact_lookup():
    sink = Collector()
    telemetry = adapter(sink)
    with telemetry.cache_lookup("merge_evidence", "retriever"):
        pass
    assert sink.events[-1].cache_status == "hit"
    with pytest.raises(ValueError):
        with telemetry.cache_lookup("merge_evidence", "retriever"):
            raise ValueError("private artifact body")
    assert sink.events[-1].cache_status == "error"
