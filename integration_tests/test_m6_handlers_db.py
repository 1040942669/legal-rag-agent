from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import Engine, func, select

from integration_tests.test_m3_import_workflow_db import _import_artifacts
from legal_rag.jobs.handlers import process_job
from legal_rag.jobs.registry import JobRegistry
from legal_rag.jobs.store import JobStore
from legal_rag.storage.catalog import PostgresLegalCatalogRepository
from legal_rag.storage.import_workflow import build_import_plan, write_machine_artifact
from legal_rag.storage.schema import snapshot_activation_events, snapshot_chunks


def _registration(
    tmp_path: Path,
    *,
    reference: str,
    snapshot_id: str,
    scope_id: str,
    index_mode: str,
) -> tuple[dict[str, object], dict[str, object]]:
    source_root, manifest = _import_artifacts(tmp_path / reference)
    request = json.loads(manifest.read_text(encoding="utf-8"))
    request["snapshot_id"] = snapshot_id
    request["scope_id"] = scope_id
    request["law_versions"][0]["law_id"] = f"m6-{reference}-law"
    request["law_versions"][0]["version_id"] = f"m6-{reference}-law-v1"
    manifest.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    prepared = build_import_plan(manifest)
    plan_path = source_root / "plan.json"
    write_machine_artifact(plan_path, prepared.plan)
    entry = {
        "scope_id": scope_id,
        "profile_id": prepared.plan["target"]["profile_id"],
        "source_root": source_root.relative_to(tmp_path).as_posix(),
        "manifest_path": "request.json",
        "plan_path": "plan.json",
        "snapshot_id": snapshot_id,
        "index_mode": index_mode,
    }
    return entry, prepared.plan


def _registry(tmp_path: Path, ingestions: dict[str, object]) -> JobRegistry:
    config = {
        "schema_version": 1,
        "artifact_root": str(tmp_path),
        "repository_root": str(Path(__file__).resolve().parents[1]),
        "evaluations": {},
        "ingestions": ingestions,
    }
    path = tmp_path / "job-registry.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return JobRegistry.from_json_file(path)


def test_m6_db_ingestion_activates_once_and_duplicate_delivery_reuses_rows(
    migrated_engine: Engine,
    tmp_path: Path,
) -> None:
    entry, plan = _registration(
        tmp_path,
        reference="first",
        snapshot_id="m6-db-first",
        scope_id="m6-db-scope",
        index_mode="exact",
    )
    registry = _registry(tmp_path, {"ingest-first": entry})
    registration = registry.resolve("ingestion", "ingest-first")
    assert registration is not None
    store = JobStore(migrated_engine)
    job = store.create_job(
        owner_id="m6-owner",
        kind="ingestion",
        request_ref="ingest-first",
        request_hash=registration.fingerprint,
        total=6,
    )
    assert (
        process_job(
            store, registry, migrated_engine, job.job_id, worker_id="m6-db-worker-1"
        )
        == "succeeded"
    )
    first = store.get_job_internal(job.job_id)
    assert first is not None and (first.completed, first.failed, first.pending) == (
        6,
        0,
        0,
    )
    assert first.stage == "activated"
    catalog = PostgresLegalCatalogRepository(migrated_engine)
    assert catalog.get_active_snapshot("m6-db-scope").snapshot_id == "m6-db-first"
    with migrated_engine.connect() as connection:
        activation_count = connection.scalar(
            select(func.count())
            .select_from(snapshot_activation_events)
            .where(snapshot_activation_events.c.scope_id == "m6-db-scope")
        )
        chunk_count = connection.scalar(
            select(func.count())
            .select_from(snapshot_chunks)
            .where(snapshot_chunks.c.snapshot_id == "m6-db-first")
        )
    assert chunk_count == plan["expected"]["chunk_count"]
    assert (
        process_job(
            store, registry, migrated_engine, job.job_id, worker_id="m6-db-worker-2"
        )
        == "succeeded"
    )
    with migrated_engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(snapshot_activation_events)
                .where(snapshot_activation_events.c.scope_id == "m6-db-scope")
            )
            == activation_count
            == 1
        )
        assert (
            connection.scalar(
                select(func.count())
                .select_from(snapshot_chunks)
                .where(snapshot_chunks.c.snapshot_id == "m6-db-first")
            )
            == chunk_count
        )


def test_m6_db_index_build_failure_preserves_active_snapshot(
    migrated_engine: Engine,
    tmp_path: Path,
    monkeypatch,
) -> None:
    first_entry, _ = _registration(
        tmp_path,
        reference="baseline",
        snapshot_id="m6-db-baseline",
        scope_id="m6-db-failure-scope",
        index_mode="exact",
    )
    second_entry, _ = _registration(
        tmp_path,
        reference="candidate",
        snapshot_id="m6-db-candidate",
        scope_id="m6-db-failure-scope",
        index_mode="hnsw",
    )
    registry = _registry(
        tmp_path, {"ingest-baseline": first_entry, "ingest-candidate": second_entry}
    )
    store = JobStore(migrated_engine)
    baseline = registry.resolve("ingestion", "ingest-baseline")
    candidate = registry.resolve("ingestion", "ingest-candidate")
    assert baseline is not None and candidate is not None
    first_job = store.create_job(
        owner_id="m6-owner",
        kind="ingestion",
        request_ref="ingest-baseline",
        request_hash=baseline.fingerprint,
        total=6,
    )
    baseline_result = process_job(
        store, registry, migrated_engine, first_job.job_id, worker_id="m6-db-worker-1"
    )
    assert baseline_result == "succeeded", store.get_job_internal(first_job.job_id)
    before = PostgresLegalCatalogRepository(migrated_engine).get_active_snapshot(
        "m6-db-failure-scope"
    )

    from legal_rag.storage.ann import PostgresHnswIndexManager

    def fail_index(*_args, **_kwargs):
        raise RuntimeError("synthetic index failure")

    monkeypatch.setattr(PostgresHnswIndexManager, "ensure_index", fail_index)
    next_job = store.create_job(
        owner_id="m6-owner",
        kind="ingestion",
        request_ref="ingest-candidate",
        request_hash=candidate.fingerprint,
        total=6,
    )
    assert (
        process_job(
            store,
            registry,
            migrated_engine,
            next_job.job_id,
            worker_id="m6-db-worker-2",
        )
        == "failed"
    )
    failed = store.get_job_internal(next_job.job_id)
    assert failed is not None and failed.error_code == "index_build_failed"
    after = PostgresLegalCatalogRepository(migrated_engine).get_active_snapshot(
        "m6-db-failure-scope"
    )
    assert (after.snapshot_id, after.revision, after.activation_id) == (
        before.snapshot_id,
        before.revision,
        before.activation_id,
    )
