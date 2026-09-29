from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from legal_rag.api.app import create_app
from legal_rag.api.auth import ServicePrincipal, TokenAuthenticator
from legal_rag.jobs.registry import (
    EvaluationRegistration,
    IngestionRegistration,
    JobRegistry,
)


TOKEN = "m6-owner-" + "a" * 40
OTHER_TOKEN = "m6-other-" + "b" * 40
PROFILE = "a" * 64
OTHER_PROFILE = "b" * 64
NOW = datetime(2026, 9, 29, tzinfo=timezone.utc)
OWNER = ServicePrincipal(user_id="owner", scope_id="scope-a", profile_id=PROFILE)
OTHER = ServicePrincipal(user_id="other", scope_id="scope-b", profile_id=OTHER_PROFILE)


class _FakeJobStore:
    def __init__(self) -> None:
        self.records: dict[str, SimpleNamespace] = {}
        self.submissions: list[dict[str, object]] = []

    def create_job(self, **kwargs) -> SimpleNamespace:
        self.submissions.append(kwargs)
        record = SimpleNamespace(
            job_id="job-123",
            owner_id=kwargs["owner_id"],
            kind=kwargs["kind"],
            status="queued",
            total=kwargs["total"],
            completed=0,
            failed=0,
            pending=kwargs["total"],
            stage="received",
            cancel_requested=False,
            outbox_status="pending",
            outbox_error_code=None,
            error_code=None,
            created_at=NOW,
            started_at=None,
            finished_at=None,
        )
        self.records[record.job_id] = record
        return record

    def get_job(self, job_id: str, owner_id: str) -> SimpleNamespace | None:
        record = self.records.get(job_id)
        return record if record and record.owner_id == owner_id else None

    def request_cancel(self, job_id: str, owner_id: str) -> SimpleNamespace | None:
        record = self.get_job(job_id, owner_id)
        if record is not None:
            record = SimpleNamespace(**{**vars(record), "cancel_requested": True})
            self.records[job_id] = record
        return record


def _client(tmp_path: Path, *, job_observer=None) -> tuple[TestClient, _FakeJobStore]:
    registry = JobRegistry(
        evaluations={
            "eval-a": EvaluationRegistration(
                "eval-a",
                "scope-a",
                PROFILE,
                "offline-synthetic",
                tmp_path,
                tmp_path / "experiments",
                2,
                "c" * 64,
            )
        },
        ingestions={
            "ingest-a": IngestionRegistration(
                "ingest-a",
                "scope-a",
                PROFILE,
                tmp_path,
                tmp_path / "request.json",
                tmp_path / "plan.json",
                "snapshot-a",
                "exact",
                6,
                "d" * 64,
            )
        },
    )
    store = _FakeJobStore()
    app = create_app(
        service=SimpleNamespace(engine=SimpleNamespace()),
        authenticator=TokenAuthenticator({TOKEN: OWNER, OTHER_TOKEN: OTHER}),
        supervisor=SimpleNamespace(),
        start_supervisor=False,
        job_store=store,
        job_registry=registry,
        job_observer=job_observer,
    )
    return TestClient(app), store


def _headers(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": "batch-1"}


def test_m6_evaluation_submission_is_durable_only_and_owner_isolated(
    tmp_path: Path,
) -> None:
    client, store = _client(tmp_path)
    response = client.post(
        "/api/v1/evaluations", json={"experiment_id": "eval-a"}, headers=_headers()
    )
    assert response.status_code == 202, response.text
    assert response.json()["job_id"] == "job-123"
    assert response.json()["status"] == "queued"
    assert response.json()["status_url"].endswith("/api/v1/jobs/job-123")
    assert store.submissions[0]["kind"] == "evaluation"
    assert store.submissions[0]["request_ref"] == "eval-a"
    assert store.submissions[0]["request_hash"] == "c" * 64
    assert store.submissions[0]["total"] == 2

    owner = client.get("/api/v1/jobs/job-123", headers=_headers())
    assert owner.status_code == 200
    assert owner.json()["outbox_status"] == "pending"
    assert owner.json()["pending"] == 2
    assert owner.json()["queue_wait_ms"] is None
    store.records["job-123"].started_at = NOW + timedelta(seconds=2)
    running = client.get("/api/v1/jobs/job-123", headers=_headers())
    assert running.json()["queue_wait_ms"] == 2000.0
    foreign = client.get("/api/v1/jobs/job-123", headers=_headers(OTHER_TOKEN))
    assert foreign.status_code == 404


def test_m6_ingestion_requires_registered_scope_and_no_client_paths(
    tmp_path: Path,
) -> None:
    client, store = _client(tmp_path)
    unknown = client.post(
        "/api/v1/ingestions",
        json={"artifact_ref": "ingest-a"},
        headers=_headers(OTHER_TOKEN),
    )
    assert unknown.status_code == 404
    assert not store.submissions

    extra_path = client.post(
        "/api/v1/ingestions",
        json={"artifact_ref": "ingest-a", "source_root": str(tmp_path)},
        headers=_headers(),
    )
    assert extra_path.status_code == 422
    assert not store.submissions

    accepted = client.post(
        "/api/v1/ingestions", json={"artifact_ref": "ingest-a"}, headers=_headers()
    )
    assert accepted.status_code == 202, accepted.text
    assert store.submissions[0]["kind"] == "ingestion"
    assert store.submissions[0]["request_ref"] == "ingest-a"
    assert store.submissions[0]["total"] == 6


def test_m6_job_cancel_is_owner_scoped_and_does_not_claim_external_revoke(
    tmp_path: Path,
) -> None:
    client, _ = _client(tmp_path)
    created = client.post(
        "/api/v1/evaluations", json={"experiment_id": "eval-a"}, headers=_headers()
    )
    assert created.status_code == 202
    foreign = client.post("/api/v1/jobs/job-123/cancel", headers=_headers(OTHER_TOKEN))
    assert foreign.status_code == 404
    owner = client.post("/api/v1/jobs/job-123/cancel", headers=_headers())
    assert owner.status_code == 200
    assert owner.json() == {
        "job_id": "job-123",
        "status": "queued",
        "cancel_requested": True,
    }


def test_m6_observer_failure_cannot_undo_accepted_job(tmp_path: Path) -> None:
    class BrokenObserver:
        def record(self, observation) -> None:
            raise RuntimeError("telemetry offline")

    client, store = _client(tmp_path, job_observer=BrokenObserver())
    response = client.post(
        "/api/v1/evaluations", json={"experiment_id": "eval-a"}, headers=_headers()
    )
    assert response.status_code == 202
    assert store.get_job("job-123", store.submissions[0]["owner_id"]) is not None
