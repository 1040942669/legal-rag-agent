from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from legal_rag.experiment_lifecycle import run_experiment
from legal_rag.experiment_runner import _MODEL_USAGE_ROLE_TO_CALL_KIND
from legal_rag.experiment_store import ExperimentStore
from legal_rag.observability import Observation, ObservationContext
from legal_rag.observability.tracing import (
    observe_evaluation_attempt,
    observe_stored_evaluation_attempt,
)
from legal_rag.tracing import JsonlTraceWriter, build_retrieval_trace_record


class CaptureObserver:
    def __init__(self) -> None:
        self.events: list[Observation] = []

    def record(self, observation: Observation) -> None:
        self.events.append(observation)


def _emit_synthetic_generation(
    calls: dict[str, int],
    *,
    origin: str = "fresh",
    source_calls: dict[str, int] | None = None,
) -> list[Observation]:
    """Exercise only the safe projection, without a dataset or provider."""

    observer = CaptureObserver()
    observe_evaluation_attempt(
        observer,
        context=ObservationContext(trace_id="synthetic-semantic-trace"),
        result={
            "case": {"case_id": "synthetic-semantic-case"},
            "output": {"trace_record": {"results": []}},
            "stage_observations": {
                "generation": {
                    "status": "succeeded",
                    "origin": origin,
                    "duration_ms": 1.0,
                    "cache_key": "a" * 64,
                    "external_calls": calls,
                    "source_external_calls": source_calls or {},
                }
            },
        },
        attempt_number=1,
        cache_mode=origin if origin != "fresh" else "fresh",
    )
    return observer.events


@pytest.mark.parametrize("generation_calls,semantic_calls", [(0, 1), (1, 1)])
def test_current_semantic_dispatch_is_in_model_observation(
    generation_calls: int, semantic_calls: int
) -> None:
    events = _emit_synthetic_generation(
        {"generation": generation_calls, "semantic": semantic_calls}
    )
    expected = generation_calls + semantic_calls
    model = next(event for event in events if event.name == "model.completed")
    node = next(event for event in events if event.name == "node.completed")
    assert model.counts == {"model_calls": expected}
    assert model.budget_used == {"model_attempts": expected}
    assert node.budget_used["model_attempts"] == expected
    assert model.model_input_tokens is None
    assert model.model_output_tokens is None
    assert model.cost_usd is None


@pytest.mark.parametrize(
    "kind", [*_MODEL_USAGE_ROLE_TO_CALL_KIND.values(), "rerank"]
)
def test_model_observation_uses_the_closed_runner_call_kind_contract(kind: str) -> None:
    events = _emit_synthetic_generation({kind: 1})
    model = next(event for event in events if event.name == "model.completed")
    assert model.counts == {"model_calls": 1}
    assert model.budget_used == {"model_attempts": 1}


@pytest.mark.parametrize("origin", ["cache", "replay"])
def test_semantic_source_history_is_not_a_current_model_dispatch(origin: str) -> None:
    events = _emit_synthetic_generation(
        {"generation": 1, "semantic": 1},
        origin=origin,
        source_calls={"generation": 7, "semantic": 9},
    )
    assert not any(event.name == "model.completed" for event in events)
    node = next(event for event in events if event.name == "node.completed")
    assert node.status == "skipped"
    assert node.budget_used["model_attempts"] == 0
    assert node.duration_ms is None


@pytest.mark.parametrize("semantic_calls", [0, 1])
def test_non_model_and_unknown_call_kinds_do_not_acquire_model_counts(
    semantic_calls: int,
) -> None:
    events = _emit_synthetic_generation(
        {
            "semantic": semantic_calls,
            "embedding": 3,
            "other": 5,
            "unrecognized_model": 11,
        }
    )
    node = next(event for event in events if event.name == "node.completed")
    assert node.budget_used == {
        "model_attempts": semantic_calls,
        "embedding_attempts": 3,
    }
    models = [event for event in events if event.name == "model.completed"]
    assert len(models) == bool(semantic_calls)
    if models:
        assert models[0].counts == {"model_calls": semantic_calls}


@pytest.fixture
def m2_attempt(tmp_path: Path) -> dict:
    root = Path(__file__).resolve().parents[1]
    experiments = tmp_path / "experiments"
    execution = run_experiment(
        experiment_id="observation-source",
        repository_root=root,
        experiment_root=experiments,
        stop_after_completed=1,
    )
    case_id = execution.summary.completed_case_ids[0]
    return ExperimentStore.open(experiments, "observation-source").load_completed(
        case_id
    )


def _emit(attempt: dict, observer: CaptureObserver) -> None:
    observe_evaluation_attempt(
        observer,
        context=ObservationContext(
            trace_id="trace-execution",
            experiment_id="observation-source",
            job_id="job-execution",
        ),
        result=attempt["result"],
        attempt_number=attempt["attempt"],
        cache_mode=attempt["cache_mode"],
    )


def test_actual_m2_attempt_records_stage_facts_and_safe_correlation(
    m2_attempt: dict,
) -> None:
    observer = CaptureObserver()
    _emit(m2_attempt, observer)

    nodes = [event for event in observer.events if event.name == "node.completed"]
    assert {event.node for event in nodes} == {"analyze_query", "retrieve"}
    assert all(event.status == "succeeded" for event in nodes)
    assert all(event.duration_ms is not None for event in nodes)
    assert all(event.context.trace_id == "trace-execution" for event in nodes)
    assert all(event.context.experiment_id == "observation-source" for event in nodes)
    assert all(event.context.job_id == "job-execution" for event in nodes)
    assert len({event.context.run_id for event in nodes}) == 1
    assert nodes[0].context.run_id
    assert all(event.context.session_id is None for event in nodes)
    assert not any(event.name == "evaluation.case" for event in observer.events)
    assert not any(event.name == "model.completed" for event in observer.events)
    retrieval = next(event for event in nodes if event.node == "retrieve")
    expected_ids = tuple(
        item["chunk_id"]
        for item in m2_attempt["result"]["output"]["trace_record"]["results"]
    )
    assert retrieval.evidence_ids == expected_ids
    assert all(event.cache_status == "bypass" for event in nodes)


def test_m2_cache_reuse_never_counts_source_provider_calls(m2_attempt: dict) -> None:
    attempt = deepcopy(m2_attempt)
    attempt["cache_mode"] = "cache"
    stage = attempt["result"]["stage_observations"]["retrieval"]
    stage["origin"] = "cache"
    stage["source_external_calls"]["generation"] = 9
    observer = CaptureObserver()
    _emit(attempt, observer)

    cached = [event for event in observer.events if event.node == "retrieve"]
    assert (
        next(event for event in cached if event.name == "cache.lookup").cache_status
        == "hit"
    )
    assert (
        next(event for event in cached if event.name == "node.completed").status
        == "skipped"
    )
    assert all(event.counts.get("model_calls", 0) == 0 for event in cached)
    assert all(event.budget_used.get("model_attempts", 0) == 0 for event in cached)
    assert not any(event.name == "model.completed" for event in observer.events)


def test_actual_m2_cache_attempt_emits_lookup_facts(
    tmp_path: Path, m2_attempt: dict
) -> None:
    execution = run_experiment(
        experiment_id="observation-cache",
        repository_root=Path(__file__).resolve().parents[1],
        experiment_root=tmp_path / "experiments",
        cache_mode="cache",
        stop_after_completed=1,
    )
    store = ExperimentStore.open(tmp_path / "experiments", "observation-cache")
    observer = CaptureObserver()
    observe_stored_evaluation_attempt(
        observer,
        context=ObservationContext(
            trace_id="actual-cache-trace", experiment_id="observation-cache"
        ),
        store=store,
        case_id=execution.summary.completed_case_ids[0],
    )

    cache_events = [event for event in observer.events if event.name == "cache.lookup"]
    assert {event.node for event in cache_events} == {"analyze_query", "retrieve"}
    assert all(event.cache_status == "hit" for event in cache_events)
    assert all(event.counts == {"cache_hits": 1} for event in cache_events)
    assert all(event.duration_ms is not None for event in cache_events)
    assert not any(event.name == "model.completed" for event in observer.events)


def test_failed_cache_origin_never_reports_a_successful_cache_hit(
    m2_attempt: dict,
) -> None:
    attempt = deepcopy(m2_attempt)
    attempt["cache_mode"] = "cache"
    attempt["result"]["stage_observations"]["retrieval"].update(
        origin="cache", status="error", error_code="invalid_schema"
    )
    observer = CaptureObserver()
    _emit(attempt, observer)
    cache = next(
        event
        for event in observer.events
        if event.name == "cache.lookup" and event.node == "retrieve"
    )
    assert cache.status == "failed"
    assert cache.cache_status == "error"
    assert cache.counts == {}
    assert cache.error_category == "validation_failed"


@pytest.mark.parametrize("token_usage_calls", [0, 1])
def test_m2_normalized_usage_without_component_coverage_stays_unknown(
    m2_attempt: dict, token_usage_calls: int
) -> None:
    attempt = deepcopy(m2_attempt)
    result = attempt["result"]
    stage = result["stage_observations"]["generation"]
    stage.update(
        status="succeeded",
        origin="fresh",
        duration_ms=4.5,
        cache_key="a" * 64,
        unavailable_reason=None,
    )
    stage["external_calls"]["generation"] = 1
    result["call_ledger"]["actual"]["generation"].update(
        attempted=1, succeeded=1, duration_ms=4.5
    )
    result["model_usage"]["assistant"].update(
        calls=1,
        token_usage_calls=token_usage_calls,
        input_tokens=20 if token_usage_calls else 0,
        output_tokens=4 if token_usage_calls else 0,
        total_tokens=24 if token_usage_calls else 0,
    )
    observer = CaptureObserver()
    _emit(attempt, observer)

    model = next(event for event in observer.events if event.name == "model.completed")
    assert model.counts["model_calls"] == 1
    assert model.budget_used["model_attempts"] == 1
    assert model.model_input_tokens is None
    assert model.model_output_tokens is None
    assert model.cost_usd is None


def test_bridge_drops_untrusted_text_and_normalizes_errors(m2_attempt: dict) -> None:
    attempt = deepcopy(m2_attempt)
    result = attempt["result"]
    trace = result["output"]["trace_record"]
    trace.update(
        query="private-question",
        answer="private-answer",
        metadata={"trace_id": "private-id", "reasoning": "private-reasoning"},
        tokens=999,
        cost_usd=999,
    )
    result["stage_observations"]["retrieval"].update(
        status="error", error_code="Authorization Bearer private-secret"
    )
    observer = CaptureObserver()
    _emit(attempt, observer)

    serialized = json.dumps([event.to_local_record() for event in observer.events])
    for forbidden in (
        "private-question",
        "private-answer",
        "private-reasoning",
        "private-id",
        "private-secret",
        "Authorization",
        "Bearer",
    ):
        assert forbidden not in serialized
    failed = next(
        event
        for event in observer.events
        if event.name == "node.completed" and event.node == "retrieve"
    )
    assert failed.status == "failed"
    assert failed.error_category == "other"


def test_optional_trace_writer_bridges_actual_record_without_metadata_ids(
    tmp_path: Path,
) -> None:
    observer = CaptureObserver()
    path = tmp_path / "retrieval.jsonl"
    writer = JsonlTraceWriter(
        path,
        run_id="actual-run",
        observer=observer,
        observation_context=ObservationContext(trace_id="actual-trace"),
    )
    record = build_retrieval_trace_record(
        query="private-question",
        retriever="bm25",
        top_k=3,
        results=[],
        latency_ms=12,
        case_id="safe-case",
        metadata={"run_id": "private-run", "reasoning": "private-reasoning"},
    )
    writer.write(record)

    assert json.loads(path.read_text(encoding="utf-8"))["query"] == "private-question"
    assert len(observer.events) == 1
    event = observer.events[0]
    assert event.name == "evaluation.case"
    assert event.context.trace_id == "actual-trace"
    assert event.context.run_id == "actual-run"
    assert event.duration_ms == 12
    assert event.cache_status is None
    assert event.model_input_tokens is None
    assert "private" not in json.dumps(event.to_local_record())


def test_observer_failure_cannot_interrupt_local_trace_or_bridge(
    tmp_path: Path,
    m2_attempt: dict,
) -> None:
    class BrokenObserver:
        def record(self, event: Observation) -> None:
            raise RuntimeError("private failure")

    observer = BrokenObserver()
    path = tmp_path / "trace.jsonl"
    JsonlTraceWriter(path, run_id="actual-run", observer=observer).write(
        {"latency_ms": 1, "results": []}
    )
    observe_evaluation_attempt(
        observer,
        context=ObservationContext(trace_id="safe-trace"),
        result=m2_attempt["result"],
        attempt_number=1,
        cache_mode="fresh",
    )
    assert path.exists()


def test_session_and_attempt_correlation_are_stable_and_not_fabricated(
    m2_attempt: dict,
) -> None:
    first = CaptureObserver()
    second = CaptureObserver()
    _emit(m2_attempt, first)
    _emit(m2_attempt, second)
    assert first.events[0].context == second.events[0].context
    later = deepcopy(m2_attempt)
    later["attempt"] = 2
    later["result"]["case"]["session_group"] = "known-session"
    third = CaptureObserver()
    _emit(later, third)
    assert third.events[0].context.run_id != first.events[0].context.run_id
    assert third.events[0].context.session_id
    assert third.events[0].retry_count == 1


def test_stored_bridge_validates_actual_runtime_fields_before_observing(
    tmp_path: Path, m2_attempt: dict
) -> None:
    store = ExperimentStore.open(tmp_path / "experiments", "observation-source")
    observer = CaptureObserver()
    observe_stored_evaluation_attempt(
        observer,
        context=ObservationContext(trace_id="trace", job_id="job"),
        store=store,
        case_id=m2_attempt["case_id"],
    )
    assert {event.node for event in observer.events} == {"analyze_query", "retrieve"}
    assert all(
        event.context.experiment_id == "observation-source" for event in observer.events
    )


@pytest.mark.parametrize("corruption", ["duration", "ledger", "trace", "usage"])
def test_hash_linked_but_invalid_runtime_artifact_produces_no_observation(
    tmp_path: Path, m2_attempt: dict, corruption: str
) -> None:
    original = ExperimentStore.open(tmp_path / "experiments", "observation-source")
    # The store deliberately permits arbitrary result objects. It creates a
    # valid envelope and completion link even when runner-owned facts are bad.
    store = ExperimentStore.create(tmp_path / "reencoded", original.load_manifest())
    attempt = deepcopy(m2_attempt)
    result = attempt["result"]
    if corruption == "duration":
        result["stage_observations"]["retrieval"]["duration_ms"] = -1
    elif corruption == "ledger":
        result["call_ledger"]["actual"]["generation"]["attempted"] = 999
    elif corruption == "trace":
        result["output"]["trace_record"]["results"][0]["chunk_id"] = "invented-id"
    else:
        # These values agree with the runner ledger but contradict the
        # independently recomputed scoring output for this actual completion.
        stage = result["stage_observations"]["retrieval"]
        stage["external_calls"]["normalizer"] = 1
        stage["source_external_calls"]["normalizer"] = 1
        result["call_ledger"]["actual"]["normalizer"].update(attempted=1, succeeded=1)
        result["call_ledger"]["source"]["normalizer"] = 1
        result["model_usage"]["normalizer"]["calls"] = 1
    store.write_attempt(
        attempt["case_id"],
        attempt=attempt["attempt"],
        status=attempt["status"],
        recorded_at=attempt["recorded_at"],
        cache_mode=attempt["cache_mode"],
        execution_environment=attempt["execution_environment"],
        result=result,
    )
    store.mark_complete(attempt["case_id"], attempt=attempt["attempt"])
    assert store.load_completed(attempt["case_id"])["result"] == result
    observer = CaptureObserver()
    observe_stored_evaluation_attempt(
        observer,
        context=ObservationContext(trace_id="safe-trace"),
        store=store,
        case_id=attempt["case_id"],
    )
    assert observer.events == []
