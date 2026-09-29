from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone
import traceback

import pytest

from legal_rag.jobs.dispatcher import dispatch_once
from legal_rag.jobs.handlers import (
    JobPermanentError,
    _stage_item,
    process_job,
    run_evaluation_job,
)
from legal_rag.jobs.command import main as jobs_main
from legal_rag.jobs.worker import WorkerSettings, celery_configuration


def test_worker_configuration_uses_json_late_ack_and_bounded_fair_fetch() -> None:
    settings = WorkerSettings(
        broker_url="redis://localhost:6379/9",
        concurrency=2,
        broker_connect_timeout=3,
        broker_read_timeout=9,
        soft_time_limit=120,
        hard_time_limit=150,
        visibility_timeout=180,
    )
    config = celery_configuration(settings)
    assert config["task_serializer"] == "json"
    assert config["accept_content"] == ["json"]
    assert config["task_ignore_result"] is True
    assert config["task_acks_late"] is True
    assert config["task_reject_on_worker_lost"] is True
    assert config["worker_concurrency"] == 2
    assert config["worker_prefetch_multiplier"] == 1
    assert config["task_soft_time_limit"] == 120
    assert config["task_time_limit"] == 150
    assert config["broker_transport_options"]["visibility_timeout"] == 180
    assert config["broker_transport_options"]["socket_connect_timeout"] == 3
    assert config["broker_transport_options"]["socket_timeout"] == 9


def test_worker_configuration_rejects_unsafe_limits() -> None:
    try:
        WorkerSettings(
            broker_url="redis://localhost:6379/0",
            soft_time_limit=120,
            hard_time_limit=120,
        )
    except ValueError as exc:
        assert "hard_time_limit" in str(exc)
    else:
        raise AssertionError("equal soft and hard limits were accepted")


def test_jobs_cli_help_is_available_without_broker_or_database(capsys) -> None:
    try:
        jobs_main(["--help"])
    except SystemExit as exc:
        assert exc.code == 0
    assert "dispatch" in capsys.readouterr().out


class _FakeStore:
    def __init__(self) -> None:
        self.records = [
            SimpleNamespace(
                id=8, job_id="job-8", schema_version=1, lease_epoch=3, attempts=0
            )
        ]
        self.delivered: list[tuple[int, str, int]] = []
        self.retried: list[tuple[int, str, int, str]] = []
        self.recovery_scans = 0

    def requeue_expired_jobs(self, *, limit: int) -> list[str]:
        self.recovery_scans += 1
        return ["job-recovered"]

    def claim_outbox(self, dispatcher_id: str, *, limit: int, lease_seconds: int):
        return self.records[:limit]

    def mark_outbox_delivered(
        self, outbox_id: int, dispatcher_id: str, lease_epoch: int
    ) -> bool:
        self.delivered.append((outbox_id, dispatcher_id, lease_epoch))
        return True

    def mark_outbox_retry(
        self,
        outbox_id: int,
        dispatcher_id: str,
        lease_epoch: int,
        error_code: str,
        *,
        delay_seconds: int,
    ) -> bool:
        self.retried.append((outbox_id, dispatcher_id, lease_epoch, error_code))
        return True


class _FakePublisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[tuple[str, int, str]] = []

    def publish(self, *, job_id: str, schema_version: int, task_id: str) -> None:
        if self.fail:
            raise ConnectionError("private-broker-url")
        self.messages.append((job_id, schema_version, task_id))


def test_dispatcher_retries_committed_outbox_when_broker_returns() -> None:
    store = _FakeStore()
    first = dispatch_once(
        store, _FakePublisher(fail=True), dispatcher_id="dispatcher-1"
    )
    assert first.recovered == 1
    assert first.delivered == 0
    assert first.deferred == 1
    assert store.retried == [(8, "dispatcher-1", 3, "broker_unavailable")]
    assert store.delivered == []

    publisher = _FakePublisher()
    second = dispatch_once(store, publisher, dispatcher_id="dispatcher-1")
    assert second.delivered == 1
    assert publisher.messages == [("job-8", 1, "legal-rag-job-job-8")]
    assert store.delivered == [(8, "dispatcher-1", 3)]
    assert store.recovery_scans == 2


class _EvaluationStore:
    def __init__(self) -> None:
        self.items: dict[str, str] = {}
        self.completed = 0
        self.stages: list[str] = []

    def set_stage(
        self, job_id: str, worker_id: str, lease_epoch: int, stage: str
    ) -> bool:
        self.stages.append(stage)
        return True

    def claim_item(
        self,
        job_id: str,
        worker_id: str,
        lease_epoch: int,
        item_key: str,
        *,
        max_attempts: int,
    ):
        if item_key in self.items:
            return SimpleNamespace(
                acquired=False, status="succeeded", result_ref=self.items[item_key]
            )
        return SimpleNamespace(acquired=True, status="running", result_ref=None)

    def complete_item(
        self,
        job_id: str,
        worker_id: str,
        lease_epoch: int,
        item_key: str,
        result_ref: str,
    ) -> bool:
        self.items[item_key] = result_ref
        self.completed += 1
        return True

    def reconcile_item(
        self,
        job_id: str,
        worker_id: str,
        lease_epoch: int,
        item_key: str,
        result_ref: str,
    ) -> bool:
        return self.complete_item(job_id, worker_id, lease_epoch, item_key, result_ref)

    def fail_item(
        self,
        job_id: str,
        worker_id: str,
        lease_epoch: int,
        item_key: str,
        error_code: str,
        *,
        retryable: bool,
    ) -> bool:
        return True


def test_evaluation_handler_resumes_m2_cases_without_repeating_completed_artifacts(
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    registration = SimpleNamespace(
        dataset_id="synthetic-offline-v1",
        repository_root=repository_root,
        experiment_root=tmp_path / "experiments",
        total=2,
        fingerprint="a" * 64,
    )
    job = SimpleNamespace(job_id="m6-evaluation-unit", total=2, cancel_requested=False)
    store = _EvaluationStore()
    assert (
        run_evaluation_job(
            store,
            job,
            registration,
            worker_id="unit-worker",
            lease_epoch=1,
            check_active=lambda: None,
        )
        == "succeeded"
    )
    assert store.completed == 2
    from legal_rag.experiment_store import ExperimentStore

    artifact = ExperimentStore.open(registration.experiment_root, job.job_id)
    attempts_before = [
        len(artifact.load_attempts(case_id)) for case_id in artifact.scan().succeeded
    ]
    assert attempts_before == [1, 1]
    assert (
        run_evaluation_job(
            store,
            job,
            registration,
            worker_id="unit-worker",
            lease_epoch=1,
            check_active=lambda: None,
        )
        == "succeeded"
    )
    assert store.completed == 2
    assert [
        len(artifact.load_attempts(case_id)) for case_id in artifact.scan().succeeded
    ] == attempts_before


def test_worker_persists_only_machine_code_when_handler_exception_contains_private_text(
    monkeypatch,
) -> None:
    now = datetime.now(timezone.utc)
    job = SimpleNamespace(
        job_id="m6-redaction-test",
        kind="evaluation",
        request_ref="registered-offline",
        request_hash="a" * 64,
        status="queued",
        lease_epoch=1,
        created_at=now,
        started_at=now,
        cancel_requested=False,
        error_code=None,
    )

    class _Store:
        def get_job_internal(self, job_id):
            return job

        def claim_job(self, job_id, worker_id, *, lease_seconds):
            job.status = "running"
            return job

        def finish_job(
            self, job_id, worker_id, lease_epoch, *, status, error_code=None
        ):
            job.status = status
            job.error_code = error_code
            return job

    registry = SimpleNamespace(
        resolve=lambda kind, ref: SimpleNamespace(fingerprint="a" * 64)
    )

    def fail_with_private_text(*args, **kwargs):
        raise RuntimeError("private input text and redis://secret-host:6379")

    monkeypatch.setattr(
        "legal_rag.jobs.handlers.run_evaluation_job", fail_with_private_text
    )
    assert (
        process_job(_Store(), registry, object(), job.job_id, worker_id="test-worker")
        == "failed"
    )
    assert job.error_code == "job_failed"
    assert "private input text" not in repr(job)
    assert "secret-host" not in repr(job)


def test_ingestion_stage_traceback_suppresses_private_source_exception() -> None:
    class _Store:
        def set_stage(self, *args):
            return True

        def claim_item(self, *args, **kwargs):
            return SimpleNamespace(acquired=True, status="running")

        def fail_item(self, *args, **kwargs):
            return True

    def fail_with_secret() -> str:
        raise RuntimeError("private input text and redis://secret-host:6379")

    with pytest.raises(JobPermanentError) as caught:
        _stage_item(
            _Store(),
            SimpleNamespace(job_id="m6-redaction-stage", stage="received"),
            SimpleNamespace(
                snapshot_id="snapshot", profile_id="profile", fingerprint="a" * 64
            ),
            stage="indexed",
            worker_id="test-worker",
            lease_epoch=1,
            action=fail_with_secret,
            check_active=lambda: None,
        )
    rendered = "".join(traceback.format_exception(caught.value))
    assert "index_build_failed" in rendered
    assert "private input text" not in rendered
    assert "secret-host" not in rendered
