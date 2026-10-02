"""M6 acceptance against PostgreSQL, Redis and Linux Celery prefork processes.

This file deliberately does not use Celery's eager mode or a fake publisher.
Every scenario is independently repeatable against the dedicated integration DB.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest
from sqlalchemy import Engine, func, select

from integration_tests.test_m6_handlers_db import _registration
from legal_rag.experiment_store import ExperimentStore
from legal_rag.jobs.dispatcher import CeleryPublisher, dispatch_once
from legal_rag.jobs.registry import JobRegistry
from legal_rag.jobs.store import JobStore
from legal_rag.jobs.worker import WorkerSettings, build_celery_app
from legal_rag.storage.catalog import PostgresLegalCatalogRepository
from legal_rag.storage.schema import snapshot_activation_events, snapshot_chunks

_SCENARIOS = frozenset({"M6-T01", "M6-T02", "M6-T03", "M6-T04", "M6-T05", "M6-T07"})
_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def real_broker_url() -> str:
    if sys.platform != "linux":
        pytest.fail("M6 real-worker acceptance requires Linux Celery prefork")
    url = os.environ.get("LEGAL_RAG_REDIS_URL", "")
    if not url:
        pytest.fail("LEGAL_RAG_REDIS_URL is required for real Redis acceptance")
    try:
        from redis import Redis

        client = Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
        assert client.ping()
    except Exception as exc:
        pytest.fail(f"real Redis broker is unavailable: {type(exc).__name__}")
    return url


def _write_registry(
    root: Path,
    *,
    evaluations: dict[str, object] | None = None,
    ingestions: dict[str, object] | None = None,
) -> tuple[Path, JobRegistry]:
    (root / "experiments").mkdir(exist_ok=True)
    path = root / "job-registry.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifact_root": str(root),
                "repository_root": str(_ROOT),
                "evaluations": evaluations or {},
                "ingestions": ingestions or {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path, JobRegistry.from_json_file(path)


def _evaluation_registry(root: Path, *, reference: str) -> tuple[Path, JobRegistry]:
    return _write_registry(
        root,
        evaluations={
            reference: {
                "scope_id": "m6-synthetic-offline",
                "profile_id": "a" * 64,
                "dataset_id": "synthetic-offline-v1",
                "experiment_root": "experiments",
                "total": 2,
            }
        },
    )


def _job(store: JobStore, registry: JobRegistry, kind: str, reference: str):
    registration = registry.resolve(kind, reference)
    assert registration is not None
    return store.create_job(
        owner_id="m6-real-test-owner",
        kind=kind,
        request_ref=reference,
        request_hash=registration.fingerprint,
        total=registration.total,
    )


def _publisher(broker_url: str) -> CeleryPublisher:
    return CeleryPublisher(build_celery_app(WorkerSettings(broker_url=broker_url)))


def _dispatch(store: JobStore, broker_url: str):
    return dispatch_once(
        store,
        _publisher(broker_url),
        dispatcher_id=f"m6-real-{uuid.uuid4().hex}",
    )


def _wait_until(predicate, *, seconds: float = 35.0) -> Any:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    pytest.fail("real M6 condition did not become true before timeout")


def _terminal(store: JobStore, job_id: str, expected: str, *, seconds: float = 45.0):
    return _wait_until(
        lambda: (
            record
            if (record := store.get_job_internal(job_id)) is not None
            and record.status == expected
            else None
        ),
        seconds=seconds,
    )


def _worker_env(
    database_url: str,
    broker_url: str,
    registry_path: Path,
    *,
    observations: Path | None = None,
    pause_job_id: str | None = None,
    pause_marker: Path | None = None,
    pause_seconds: int = 20,
    fail_index: bool = False,
) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "LEGAL_RAG_DATABASE_URL": database_url,
            "LEGAL_RAG_REDIS_URL": broker_url,
            "LEGAL_RAG_JOB_REGISTRY_PATH": str(registry_path),
            "LEGAL_RAG_JOB_CONCURRENCY": "2",
            "LEGAL_RAG_LANGFUSE_ENABLED": "0",
            "LEGAL_RAG_LANGFUSE_EXPORT_ACK": "0",
        }
    )
    env.pop("LEGAL_RAG_OBSERVATION_JSONL_PATH", None)
    env.pop("LEGAL_RAG_M6_TEST_PAUSE_JOB_ID", None)
    env.pop("LEGAL_RAG_M6_TEST_PAUSE_MARKER", None)
    env.pop("LEGAL_RAG_M6_TEST_PAUSE_SECONDS", None)
    env.pop("LEGAL_RAG_M6_TEST_FAIL_INDEX", None)
    if observations is not None:
        env["LEGAL_RAG_OBSERVATION_JSONL_PATH"] = str(observations)
    if pause_job_id is not None:
        assert pause_marker is not None
        env["LEGAL_RAG_M6_TEST_PAUSE_JOB_ID"] = pause_job_id
        env["LEGAL_RAG_M6_TEST_PAUSE_MARKER"] = str(pause_marker)
        env["LEGAL_RAG_M6_TEST_PAUSE_SECONDS"] = str(pause_seconds)
    if fail_index:
        env["LEGAL_RAG_M6_TEST_FAIL_INDEX"] = "1"
    return env


def _stop_worker(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


@contextmanager
def _worker(
    root: Path, env: dict[str, str]
) -> Iterator[tuple[subprocess.Popen[bytes], Path]]:
    log_path = root / f"celery-{uuid.uuid4().hex}.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "integration_tests.m6_worker_entry"],
            cwd=_ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            _wait_until(
                lambda: (
                    process.poll() is None
                    and log_path.exists()
                    and "ready."
                    in log_path.read_text(encoding="utf-8", errors="replace")
                ),
                seconds=35,
            )
            yield process, log_path
        finally:
            _stop_worker(process)


def _attempt_counts(root: Path, job_id: str) -> list[int]:
    artifact = ExperimentStore.open(root / "experiments", job_id)
    case_ids = sorted(artifact.scan().succeeded)
    return [len(artifact.load_attempts(case_id)) for case_id in case_ids]


def _receipt(scenario: str, selector: str, evidence: dict[str, bool | int]) -> None:
    path_value = os.environ.get("LEGAL_RAG_M6_RECEIPT")
    if not path_value:
        return
    candidate_sha = os.environ.get("LEGAL_RAG_M6_CANDIDATE_SHA", "")
    if re.fullmatch(r"[0-9a-f]{40}", candidate_sha) is None:
        pytest.fail("LEGAL_RAG_M6_CANDIDATE_SHA must pin the tested commit")
    path = Path(path_value)
    if not path.is_absolute():
        pytest.fail("LEGAL_RAG_M6_RECEIPT must be absolute")
    existing: dict[str, Any] = {}
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
    scenarios = (
        existing.get("scenarios", {})
        if existing.get("candidate_sha") == candidate_sha
        else {}
    )
    scenarios[scenario] = {
        "status": "passed",
        "test_selectors": [f"integration_tests/test_m6_worker_real.py::{selector}"],
        "evidence": evidence,
    }
    receipt = {
        "schema_version": 1,
        "milestone": "M6",
        "candidate_sha": candidate_sha,
        "status": "passed" if _SCENARIOS.issubset(scenarios) else "partial",
        "live_model_calls": False,
        "database": {
            "backend": "postgresql",
            "migration_head": "0007_m6_jobs_outbox",
        },
        "broker": {
            "backend": "redis",
            "worker_backend": "celery",
            "real_worker_process": True,
        },
        "scenarios": scenarios,
        "redaction": {
            "contains_credentials": False,
            "contains_private_text": False,
            "contains_database_url": False,
            "contains_redis_url": False,
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )


def test_m6_t01_outbox_recovery(
    migrated_engine: Engine,
    integration_database_url: str,
    real_broker_url: str,
    tmp_path: Path,
) -> None:
    registry_path, _ = _evaluation_registry(tmp_path, reference="eval-t01")
    store = JobStore(migrated_engine)
    env = _worker_env(integration_database_url, real_broker_url, registry_path)
    submit_env = {
        **env,
        "LEGAL_RAG_M6_TEST_SUBMIT_KIND": "evaluation",
        "LEGAL_RAG_M6_TEST_SUBMIT_REF": "eval-t01",
    }
    submitted = subprocess.run(
        [sys.executable, "-m", "integration_tests.m6_submit_entry"],
        cwd=_ROOT,
        env=submit_env,
        capture_output=True,
        timeout=30,
        check=True,
    )
    job_id = submitted.stdout.decode("ascii").strip()
    assert re.fullmatch(r"[0-9a-f-]{36}", job_id)
    committed = store.get_job_internal(job_id)
    assert committed is not None and committed.status == "queued"
    assert committed.outbox_status == "pending" and committed.claim_count == 0
    assert not (tmp_path / "experiments" / job_id).exists()
    with _worker(tmp_path, env):
        dispatched = subprocess.run(
            [sys.executable, "-m", "legal_rag.jobs.command", "dispatch", "--once"],
            cwd=_ROOT,
            env=env,
            capture_output=True,
            timeout=30,
            check=True,
        )
        report = json.loads(dispatched.stdout.decode("utf-8").strip())
        assert report["delivered"] >= 1
        done = _terminal(store, job_id, "succeeded")
    assert done.completed == 2 and _attempt_counts(tmp_path, job_id) == [1, 1]
    _receipt(
        "M6-T01",
        "test_m6_t01_outbox_recovery",
        {"committed_before_publish": True, "recovery_dispatch_observed": True},
    )


def test_m6_t02_duplicate_delivery(
    migrated_engine: Engine,
    integration_database_url: str,
    real_broker_url: str,
    tmp_path: Path,
) -> None:
    registry_path, registry = _evaluation_registry(tmp_path, reference="eval-t02")
    store = JobStore(migrated_engine)
    job = _job(store, registry, "evaluation", "eval-t02")
    env = _worker_env(integration_database_url, real_broker_url, registry_path)
    with _worker(tmp_path, env) as (_, log_path):
        assert _dispatch(store, real_broker_url).delivered >= 1
        first = _terminal(store, job.job_id, "succeeded")
        assert first.claim_count == 1
        duplicate_task_id = f"m6-duplicate-{uuid.uuid4().hex}"
        _publisher(real_broker_url).publish(
            job_id=job.job_id, schema_version=1, task_id=duplicate_task_id
        )
        _wait_until(
            lambda: (
                f"Task legal_rag.jobs.execute[{duplicate_task_id}] succeeded"
                in log_path.read_text(encoding="utf-8", errors="replace")
            ),
            seconds=20,
        )
        _wait_until(
            lambda: (
                record.claim_count == 1
                if (record := store.get_job_internal(job.job_id)) is not None
                else False
            )
        )
    assert _attempt_counts(tmp_path, job.job_id) == [1, 1]
    _receipt(
        "M6-T02",
        "test_m6_t02_duplicate_delivery",
        {"duplicate_delivery_observed": True, "unique_business_result": True},
    )


def test_m6_t03_kill_resume(
    migrated_engine: Engine,
    integration_database_url: str,
    real_broker_url: str,
    tmp_path: Path,
) -> None:
    registry_path, registry = _evaluation_registry(tmp_path, reference="eval-t03")
    store = JobStore(migrated_engine)
    job = _job(store, registry, "evaluation", "eval-t03")
    marker = tmp_path / "after-first-commit.marker"
    first_env = _worker_env(
        integration_database_url,
        real_broker_url,
        registry_path,
        pause_job_id=job.job_id,
        pause_marker=marker,
        pause_seconds=60,
    )
    with _worker(tmp_path, first_env) as (first_worker, _):
        assert _dispatch(store, real_broker_url).delivered >= 1
        _wait_until(
            lambda: (
                marker.exists()
                and (record := store.get_job_internal(job.job_id)) is not None
                and record.status == "running"
                and record.completed == 1
            ),
            seconds=30,
        )
        assert _attempt_counts(tmp_path, job.job_id) == [1]
        first_pid = first_worker.pid
        os.killpg(first_pid, signal.SIGKILL)
        first_worker.wait(timeout=10)
    resumed_env = _worker_env(integration_database_url, real_broker_url, registry_path)
    with _worker(tmp_path, resumed_env) as (resume_worker, _):
        recovery = _wait_until(
            lambda: (
                report
                if (report := _dispatch(store, real_broker_url)).recovered
                else None
            ),
            seconds=50,
        )
        assert recovery.recovered >= 1 and recovery.delivered >= 1
        done = _terminal(store, job.job_id, "succeeded", seconds=50)
        resume_pid = resume_worker.pid
    assert first_pid != resume_pid
    assert done.claim_count >= 2 and done.completed == 2
    assert _attempt_counts(tmp_path, job.job_id) == [1, 1]
    _receipt(
        "M6-T03",
        "test_m6_t03_kill_resume",
        {
            "worker_killed": True,
            "redelivery_observed": True,
            "completed_items_not_recomputed": True,
            "first_pid": first_pid,
            "resume_pid": resume_pid,
        },
    )


def test_m6_t04_broker_disconnect(
    migrated_engine: Engine,
    integration_database_url: str,
    real_broker_url: str,
    tmp_path: Path,
) -> None:
    registry_path, registry = _evaluation_registry(tmp_path, reference="eval-t04")
    store = JobStore(migrated_engine)
    existing = _job(store, registry, "evaluation", "eval-t04")
    env = _worker_env(integration_database_url, real_broker_url, registry_path)
    with _worker(tmp_path, env):
        assert _dispatch(store, real_broker_url).delivered >= 1
        _terminal(store, existing.job_id, "succeeded")
    existing_attempts = _attempt_counts(tmp_path, existing.job_id)
    assert existing_attempts == [1, 1]
    job = _job(store, registry, "evaluation", "eval-t04")
    # An unreachable loopback Redis endpoint gives a real connection failure
    # without disrupting the CI service or another test's broker database.
    disconnected = dispatch_once(
        store,
        _publisher("redis://127.0.0.1:1/15"),
        dispatcher_id=f"m6-disconnect-{uuid.uuid4().hex}",
        retry_delay_seconds=1,
    )
    assert disconnected.deferred >= 1 and disconnected.delivered == 0
    queued = store.get_job_internal(job.job_id)
    assert queued is not None and queued.status == "queued"
    assert queued.outbox_error_code == "broker_unavailable" and queued.completed == 0
    assert not (tmp_path / "experiments" / job.job_id).exists()
    assert store.get_job_internal(existing.job_id).status == "succeeded"
    assert _attempt_counts(tmp_path, existing.job_id) == existing_attempts
    with _worker(tmp_path, env):
        _wait_until(lambda: _dispatch(store, real_broker_url).delivered >= 1)
        done = _terminal(store, job.job_id, "succeeded")
    assert done.completed == 2 and _attempt_counts(tmp_path, job.job_id) == [1, 1]
    _receipt(
        "M6-T04",
        "test_m6_t04_broker_disconnect",
        {
            "redis_disconnect_observed": True,
            "job_state_explainable": True,
            "persisted_records_unchanged": True,
        },
    )


def test_m6_t05_index_failure(
    migrated_engine: Engine,
    integration_database_url: str,
    real_broker_url: str,
    tmp_path: Path,
) -> None:
    suffix = uuid.uuid4().hex[:12]
    scope_id = f"m6-real-index-{suffix}"
    baseline_id = f"m6-baseline-{suffix}"
    candidate_id = f"m6-candidate-{suffix}"
    baseline, _ = _registration(
        tmp_path,
        reference=f"baseline-{suffix}",
        snapshot_id=baseline_id,
        scope_id=scope_id,
        index_mode="exact",
    )
    candidate, _ = _registration(
        tmp_path,
        reference=f"candidate-{suffix}",
        snapshot_id=candidate_id,
        scope_id=scope_id,
        index_mode="hnsw",
    )
    registry_path, registry = _write_registry(
        tmp_path,
        ingestions={"ingest-baseline": baseline, "ingest-candidate": candidate},
    )
    store = JobStore(migrated_engine)
    env = _worker_env(
        integration_database_url, real_broker_url, registry_path, fail_index=True
    )
    with _worker(tmp_path, env):
        first = _job(store, registry, "ingestion", "ingest-baseline")
        assert _dispatch(store, real_broker_url).delivered >= 1
        _terminal(store, first.job_id, "succeeded")
        catalog = PostgresLegalCatalogRepository(migrated_engine)
        before = catalog.get_active_snapshot(scope_id)
        second = _job(store, registry, "ingestion", "ingest-candidate")
        assert _dispatch(store, real_broker_url).delivered >= 1
        failed = _terminal(store, second.job_id, "failed")
        after = catalog.get_active_snapshot(scope_id)
    assert failed.error_code == "index_build_failed"
    assert (after.snapshot_id, after.revision, after.activation_id) == (
        before.snapshot_id,
        before.revision,
        before.activation_id,
    )
    with migrated_engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(snapshot_activation_events)
                .where(snapshot_activation_events.c.scope_id == scope_id)
            )
            == 1
        )
        assert (
            connection.scalar(
                select(func.count())
                .select_from(snapshot_chunks)
                .where(snapshot_chunks.c.snapshot_id == candidate_id)
            )
            > 0
        )
    _receipt(
        "M6-T05",
        "test_m6_t05_index_failure",
        {"index_failure_observed": True, "active_snapshot_unchanged": True},
    )


def test_m6_t07_parallel_progress(
    migrated_engine: Engine,
    integration_database_url: str,
    real_broker_url: str,
    tmp_path: Path,
) -> None:
    registry_path, registry = _evaluation_registry(tmp_path, reference="eval-t07")
    store = JobStore(migrated_engine)
    slow = _job(store, registry, "evaluation", "eval-t07")
    marker = tmp_path / "slow-after-first-commit.marker"
    observations = tmp_path / "observations.jsonl"
    env = _worker_env(
        integration_database_url,
        real_broker_url,
        registry_path,
        observations=observations,
        pause_job_id=slow.job_id,
        pause_marker=marker,
        pause_seconds=30,
    )
    with _worker(tmp_path, env):
        assert _dispatch(store, real_broker_url).delivered >= 1
        _wait_until(
            lambda: (
                marker.exists() and store.get_job_internal(slow.job_id).completed == 1
            )
        )
        fast = _job(store, registry, "evaluation", "eval-t07")
        assert _dispatch(store, real_broker_url).delivered >= 1
        fast_done = _terminal(store, fast.job_id, "succeeded", seconds=25)
        slow_during = store.get_job_internal(slow.job_id)
        assert slow_during is not None and slow_during.status == "running"
        assert fast_done.completed == 2
        _terminal(store, slow.job_id, "succeeded", seconds=45)
    rows = [
        json.loads(line)
        for line in observations.read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        row["job_id"] == fast.job_id
        and row["name"] == "job.running"
        and isinstance(row["queue_wait_ms"], (int, float))
        and row["queue_wait_ms"] >= 0
        for row in rows
    )
    assert any(
        row["job_id"] == fast.job_id
        and row["name"] == "job.progress"
        and row["counts"]["completed"] >= 1
        for row in rows
    )
    assert any(
        row["job_id"] == fast.job_id
        and row["name"] == "job.succeeded"
        and isinstance(row["duration_ms"], (int, float))
        and row["duration_ms"] >= 0
        and row["retry_count"] == 0
        for row in rows
    )
    assert any(
        row["job_id"] == fast.job_id
        and row["experiment_id"] == fast.job_id
        and isinstance(row["run_id"], str)
        and row["name"] == "node.completed"
        and row["node"] == "retrieve"
        and row["evidence_ids"]
        and row["budget_used"]["model_attempts"] == 0
        for row in rows
    )
    _receipt(
        "M6-T07",
        "test_m6_t07_parallel_progress",
        {
            "slow_task_observed": True,
            "new_job_progress_observed": True,
            "queue_wait_recorded": True,
        },
    )
