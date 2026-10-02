from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from legal_rag.jobs.dispatcher import dispatch_once
from legal_rag.jobs.handlers import (
    JobPermanentError,
    _stage_item,
    process_job,
    run_evaluation_job,
)


class _Clock:
    def __init__(self) -> None:
        self.value = 20.0

    def __call__(self) -> float:
        self.value += 0.025
        return self.value


def _job(**overrides):
    created = datetime(2026, 10, 2, tzinfo=timezone.utc)
    fields = dict(
        job_id="observed-job",
        kind="evaluation",
        request_ref="registered",
        request_hash="a" * 64,
        total=2,
        completed=0,
        failed=0,
        pending=2,
        status="queued",
        stage="received",
        lease_epoch=2,
        claim_count=3,
        cancel_requested=False,
        created_at=created,
        started_at=created + timedelta(seconds=2),
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


class _StageStore:
    def __init__(self, *, reuse=False):
        self.job = _job(kind="ingestion", total=6, pending=6)
        self.reuse = reuse

    def get_job_internal(self, job_id):
        return self.job

    def set_stage(self, *args):
        return True

    def claim_item(self, *args, **kwargs):
        return SimpleNamespace(
            acquired=not self.reuse,
            status="succeeded" if self.reuse else "running",
            attempt_no=3,
        )

    def complete_item(self, *args):
        self.job.completed += 1
        self.job.pending -= 1
        return True

    def fail_item(self, *args, **kwargs):
        self.job.failed += 1
        self.job.pending -= 1
        return True


@pytest.mark.parametrize("reuse", [False, True])
def test_ingestion_step_records_current_timing_attempt_and_reuse(
    monkeypatch, reuse
) -> None:
    monkeypatch.setattr("legal_rag.jobs.handlers.time.perf_counter", _Clock())
    events = []
    store = _StageStore(reuse=reuse)
    actions = []
    _stage_item(
        store,
        store.job,
        SimpleNamespace(
            snapshot_id="snapshot", profile_id="profile", fingerprint="a" * 64
        ),
        stage="indexed",
        worker_id="worker",
        lease_epoch=2,
        action=lambda: actions.append(True) or "result-ref",
        check_active=lambda: None,
        observer=SimpleNamespace(record=events.append),
    )
    event = next(event for event in events if event.name == "ingestion.step")
    assert event.duration_ms == pytest.approx(25.0)
    assert event.retry_count == 2
    assert event.cache_status == ("hit" if reuse else "miss")
    assert event.status == ("skipped" if reuse else "succeeded")
    assert actions == ([] if reuse else [True])
    assert event.counts["completed"] == (0 if reuse else 1)


def test_ingestion_failure_records_current_error_without_private_exception(
    monkeypatch,
) -> None:
    monkeypatch.setattr("legal_rag.jobs.handlers.time.perf_counter", _Clock())
    events = []
    store = _StageStore()

    def fail():
        raise RuntimeError("private input and broker credential")

    with pytest.raises(JobPermanentError, match="index_build_failed"):
        _stage_item(
            store,
            store.job,
            SimpleNamespace(
                snapshot_id="snapshot", profile_id="profile", fingerprint="a" * 64
            ),
            stage="indexed",
            worker_id="worker",
            lease_epoch=2,
            action=fail,
            check_active=lambda: None,
            observer=SimpleNamespace(record=events.append),
        )
    assert len(events) == 1
    assert events[0].status == "failed"
    assert events[0].error_category == "index_build_failed"
    assert events[0].duration_ms == pytest.approx(25.0)
    assert events[0].retry_count == 2
    assert "private" not in str(events[0].to_local_record())


@pytest.mark.parametrize("schema", [1, 2])
def test_dispatcher_observes_real_delivery_retry_and_classification(
    monkeypatch, schema
) -> None:
    monkeypatch.setattr("legal_rag.jobs.dispatcher.time.perf_counter", _Clock())
    events = []
    publish_calls = []
    record = SimpleNamespace(
        id="outbox",
        job_id="observed-job",
        schema_version=schema,
        lease_epoch=1,
        attempts=3,
    )
    store = SimpleNamespace(
        requeue_expired_jobs=lambda **kwargs: [],
        claim_outbox=lambda *args, **kwargs: [record],
        mark_outbox_retry=lambda *args, **kwargs: True,
    )

    def fail(**kwargs):
        publish_calls.append(kwargs)
        raise ConnectionError("private broker credential")

    result = dispatch_once(
        store,
        SimpleNamespace(publish=fail),
        dispatcher_id="dispatcher",
        observer=SimpleNamespace(record=events.append),
    )
    assert result.deferred == 1
    assert len(events) == 1
    assert events[0].retry_count == 2
    assert events[0].duration_ms == pytest.approx(25.0)
    assert events[0].error_category == (
        "broker_unavailable" if schema == 1 else "validation_failed"
    )
    assert len(publish_calls) == (1 if schema == 1 else 0)
    assert "credential" not in str(events[0].to_local_record())


def test_worker_observes_current_attempt_execution_and_durable_queue_wait(
    monkeypatch,
) -> None:
    monkeypatch.setattr("legal_rag.jobs.handlers.time.perf_counter", _Clock())
    events = []
    job = _job()

    def claim(*args, **kwargs):
        job.status = "running"
        return job

    def finish(*args, status, error_code=None):
        job.status = status
        return job

    store = SimpleNamespace(
        get_job_internal=lambda job_id: job,
        claim_job=claim,
        finish_job=finish,
    )
    monkeypatch.setattr(
        "legal_rag.jobs.handlers.run_evaluation_job",
        lambda *args, **kwargs: "succeeded",
    )
    result = process_job(
        store,
        SimpleNamespace(resolve=lambda *args: SimpleNamespace(fingerprint="a" * 64)),
        object(),
        job.job_id,
        worker_id="worker",
        observer=SimpleNamespace(record=events.append),
    )
    assert result == "succeeded"
    assert [event.name for event in events] == ["job.running", "job.succeeded"]
    assert events[0].queue_wait_ms == 2000.0
    assert events[0].retry_count == 2
    assert events[1].retry_count == 2
    assert events[1].duration_ms == pytest.approx(25.0)


def test_dispatcher_success_reports_delivery_not_a_new_business_enqueue(
    monkeypatch,
) -> None:
    monkeypatch.setattr("legal_rag.jobs.dispatcher.time.perf_counter", _Clock())
    events = []
    record = SimpleNamespace(
        id="outbox", job_id="observed-job", schema_version=1, lease_epoch=1, attempts=4
    )
    store = SimpleNamespace(
        requeue_expired_jobs=lambda **kwargs: [],
        claim_outbox=lambda *args, **kwargs: [record],
        mark_outbox_delivered=lambda *args, **kwargs: True,
    )
    result = dispatch_once(
        store,
        SimpleNamespace(publish=lambda **kwargs: None),
        dispatcher_id="dispatcher",
        observer=SimpleNamespace(record=events.append),
    )
    assert result.delivered == 1
    assert events[0].name == "tool.completed"
    assert events[0].status == "succeeded"
    assert events[0].tool == "dispatcher"
    assert events[0].retry_count == 3
    assert events[0].duration_ms == pytest.approx(25.0)


def test_completed_job_delivery_observes_reuse_without_new_worker_execution(
    monkeypatch,
) -> None:
    monkeypatch.setattr("legal_rag.jobs.handlers.time.perf_counter", _Clock())
    job = _job(status="succeeded", completed=2, pending=0)
    events = []
    assert (
        process_job(
            SimpleNamespace(get_job_internal=lambda job_id: job),
            object(),
            object(),
            job.job_id,
            worker_id="worker",
            observer=SimpleNamespace(record=events.append),
        )
        == "succeeded"
    )
    assert len(events) == 1
    assert events[0].name == "cache.lookup"
    assert events[0].cache_status == "hit"
    assert events[0].status == "skipped"
    assert events[0].duration_ms == pytest.approx(25.0)
    assert events[0].counts["completed"] == 2


class _EvaluationStore(_StageStore):
    def __init__(self):
        self.job = _job()
        self.items = {}

    def claim_item(self, job_id, worker_id, lease_epoch, item_key, **kwargs):
        ref = self.items.get(item_key)
        return SimpleNamespace(
            acquired=ref is None,
            status="running" if ref is None else "succeeded",
            attempt_no=2,
            result_ref=ref,
        )

    def complete_item(self, job_id, worker_id, lease_epoch, item_key, result_ref):
        self.items[item_key] = result_ref
        self.job.completed += 1
        self.job.pending -= 1
        return True


def test_evaluation_new_attempt_bridge_is_not_replayed_on_completed_reuse(
    tmp_path, monkeypatch
) -> None:
    from pathlib import Path

    from legal_rag.jobs.handlers import _observe_evaluation_attempt

    store = _EvaluationStore()
    registration = SimpleNamespace(
        dataset_id="synthetic-offline-v1",
        repository_root=Path(__file__).resolve().parents[1],
        experiment_root=tmp_path / "experiments",
        total=2,
        fingerprint="a" * 64,
    )
    bridges = []

    def capture_bridge(observer, job, artifact, case_id):
        bridges.append(artifact.load_completed(case_id))
        _observe_evaluation_attempt(observer, job, artifact, case_id)

    monkeypatch.setattr(
        "legal_rag.jobs.handlers._observe_evaluation_attempt",
        capture_bridge,
    )
    events = []
    for _ in range(2):
        assert (
            run_evaluation_job(
                store,
                store.job,
                registration,
                worker_id="worker",
                lease_epoch=2,
                check_active=lambda: None,
                observer=SimpleNamespace(record=events.append),
            )
            == "succeeded"
        )
    assert len(bridges) == 2
    assert all(attempt["status"] == "succeeded" for attempt in bridges)
    cases = [event for event in events if event.name == "evaluation.case"]
    assert [event.cache_status for event in cases] == ["miss", "miss", "hit", "hit"]
    assert [event.status for event in cases] == [
        "succeeded",
        "succeeded",
        "skipped",
        "skipped",
    ]
    assert all(event.duration_ms >= 0 for event in cases)
    assert all(event.retry_count == 1 for event in cases)
    assert store.job.completed == 2
    retrieval = [
        event
        for event in events
        if event.name == "node.completed" and event.node == "retrieve"
    ]
    assert len(retrieval) == 2  # Completed-case reuse does not replay stage facts.
    assert all(event.context.job_id == store.job.job_id for event in retrieval)
    assert all(event.context.experiment_id == store.job.job_id for event in retrieval)
    assert len({event.context.run_id for event in retrieval}) == 2
    assert all(event.evidence_ids for event in retrieval)
    assert not any(event.name == "model.completed" for event in events)


def test_optional_observer_exception_does_not_change_stage_completion() -> None:
    store = _StageStore()

    def fail_observer(event):
        raise RuntimeError("optional telemetry unavailable")

    _stage_item(
        store,
        store.job,
        SimpleNamespace(
            snapshot_id="snapshot", profile_id="profile", fingerprint="a" * 64
        ),
        stage="indexed",
        worker_id="worker",
        lease_epoch=2,
        action=lambda: "result-ref",
        check_active=lambda: None,
        observer=SimpleNamespace(record=fail_observer),
    )
    assert store.job.completed == 1
