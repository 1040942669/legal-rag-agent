from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from threading import Event

import pytest
from sqlalchemy import Engine, func, select, text

from integration_tests.test_m3_import_workflow_db import _import_artifacts
from legal_rag.jobs.handlers import _activation_ref, _item_key, process_job
from legal_rag.jobs.registry import JobRegistry
from legal_rag.jobs.store import JobStore
from legal_rag.storage.catalog import PostgresLegalCatalogRepository
from legal_rag.storage.import_workflow import (
    apply_prepared_import,
    build_import_plan,
    validate_import_plan,
    write_machine_artifact,
)
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


def _claimed_final_activation(engine: Engine, tmp_path: Path, *, prefix: str):
    scope_id = f"m6-{prefix}-scope"
    baseline_entry, _ = _registration(
        tmp_path,
        reference=f"{prefix}-baseline",
        snapshot_id=f"m6-{prefix}-baseline",
        scope_id=scope_id,
        index_mode="exact",
    )
    candidate_entry, _ = _registration(
        tmp_path,
        reference=f"{prefix}-candidate",
        snapshot_id=f"m6-{prefix}-candidate",
        scope_id=scope_id,
        index_mode="exact",
    )
    baseline_ref = f"{prefix}-baseline"
    candidate_ref = f"{prefix}-candidate"
    registry = _registry(
        tmp_path,
        {baseline_ref: baseline_entry, candidate_ref: candidate_entry},
    )
    store = JobStore(engine)
    baseline_registration = registry.resolve("ingestion", baseline_ref)
    registration = registry.resolve("ingestion", candidate_ref)
    assert baseline_registration is not None and registration is not None
    baseline_job = store.create_job(
        owner_id="m6-owner",
        kind="ingestion",
        request_ref=baseline_ref,
        request_hash=baseline_registration.fingerprint,
        total=6,
    )
    assert (
        process_job(
            store, registry, engine, baseline_job.job_id, worker_id="baseline-worker"
        )
        == "succeeded"
    )
    prepared = validate_import_plan(
        registration.manifest_path,
        registration.plan_path,
        source_root=registration.source_root,
    )
    apply_prepared_import(prepared, engine=engine)
    job = store.create_job(
        owner_id="m6-owner",
        kind="ingestion",
        request_ref=candidate_ref,
        request_hash=registration.fingerprint,
        total=6,
    )
    lease = store.claim_job(job.job_id, "worker-a", lease_seconds=30)
    assert lease is not None
    for stage in ("received", "parsed", "embedded", "indexed", "validated"):
        assert store.set_stage(job.job_id, "worker-a", lease.lease_epoch, stage)
        item_key = _item_key(
            job_id=job.job_id,
            kind="ingestion",
            identity=f"{registration.snapshot_id}:{registration.profile_id}:{stage}",
            config_hash=registration.fingerprint,
        )
        assert store.claim_item(
            job.job_id, "worker-a", lease.lease_epoch, item_key
        ).acquired
        assert store.complete_item(
            job.job_id, "worker-a", lease.lease_epoch, item_key, "a" * 64
        )
    assert store.set_stage(job.job_id, "worker-a", lease.lease_epoch, "activated")
    activation_key = _item_key(
        job_id=job.job_id,
        kind="ingestion",
        identity=f"{registration.snapshot_id}:{registration.profile_id}:activated",
        config_hash=registration.fingerprint,
    )
    assert store.claim_item(
        job.job_id, "worker-a", lease.lease_epoch, activation_key
    ).acquired
    return store, registration, lease, activation_key, scope_id


def test_m6_db_stale_activation_cannot_move_pointer_after_takeover(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    store, registration, lease, item_key, scope_id = _claimed_final_activation(
        migrated_engine, tmp_path, prefix="takeover"
    )
    catalog = PostgresLegalCatalogRepository(migrated_engine)
    before = catalog.get_active_snapshot(scope_id)
    with migrated_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 second' "
                "WHERE job_id=:job_id"
            ),
            {"job_id": lease.job_id},
        )
    replacement = store.claim_job(lease.job_id, "worker-b", lease_seconds=30)
    assert replacement is not None and replacement.lease_epoch > lease.lease_epoch

    def forbidden_activation(_connection):
        raise AssertionError("stale worker reached catalog activation")

    assert (
        store.activate_ingestion_job(
            lease.job_id,
            "worker-a",
            lease.lease_epoch,
            item_key,
            request_hash=registration.fingerprint,
            activate=forbidden_activation,
        )
        is None
    )
    assert catalog.get_active_snapshot(scope_id) == before
    assert not any(
        event.actor == lease.job_id
        for event in catalog.list_activation_history(scope_id)
    )
    assert store.claim_item(
        lease.job_id, "worker-b", replacement.lease_epoch, item_key
    ).acquired
    finished = store.activate_ingestion_job(
        lease.job_id,
        "worker-b",
        replacement.lease_epoch,
        item_key,
        request_hash=registration.fingerprint,
        activate=lambda connection: _activation_ref(
            migrated_engine,
            job=replacement,
            registration=registration,
            connection=connection,
        ),
    )
    assert finished is not None and finished.status == "succeeded"
    assert (finished.completed, finished.failed) == (6, 0)
    assert catalog.get_active_snapshot(scope_id).snapshot_id == registration.snapshot_id
    assert (
        sum(
            event.actor == lease.job_id
            for event in catalog.list_activation_history(scope_id)
        )
        == 1
    )


def test_m6_db_cancel_before_activation_preserves_pointer(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    store, registration, lease, item_key, scope_id = _claimed_final_activation(
        migrated_engine, tmp_path, prefix="cancel-first"
    )
    catalog = PostgresLegalCatalogRepository(migrated_engine)
    before = catalog.get_active_snapshot(scope_id)
    cancelled = store.request_cancel(lease.job_id, "m6-owner")
    assert cancelled is not None and cancelled.cancel_requested

    def forbidden_activation(_connection):
        raise AssertionError("cancelled worker reached catalog activation")

    assert (
        store.activate_ingestion_job(
            lease.job_id,
            "worker-a",
            lease.lease_epoch,
            item_key,
            request_hash=registration.fingerprint,
            activate=forbidden_activation,
        )
        is None
    )
    assert catalog.get_active_snapshot(scope_id) == before
    assert (
        store.finish_job(
            lease.job_id, "worker-a", lease.lease_epoch, status="cancelled"
        ).status
        == "cancelled"
    )


def test_m6_db_activation_and_cancel_serialize_on_job_lock(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    store, registration, lease, item_key, scope_id = _claimed_final_activation(
        migrated_engine, tmp_path, prefix="activate-first"
    )
    inside_transaction = Event()
    release_activation = Event()
    cancel_started = Event()

    def activate(connection):
        inside_transaction.set()
        assert release_activation.wait(timeout=10)
        return _activation_ref(
            migrated_engine,
            job=lease,
            registration=registration,
            connection=connection,
        )

    def request_cancel():
        cancel_started.set()
        return store.request_cancel(lease.job_id, "m6-owner")

    with ThreadPoolExecutor(max_workers=2) as pool:
        final_future = pool.submit(
            store.activate_ingestion_job,
            lease.job_id,
            "worker-a",
            lease.lease_epoch,
            item_key,
            request_hash=registration.fingerprint,
            activate=activate,
        )
        try:
            assert inside_transaction.wait(timeout=10)
            cancel_future = pool.submit(request_cancel)
            assert cancel_started.wait(timeout=10)
            try:
                cancel_future.result(timeout=0.1)
            except FutureTimeoutError:
                pass
            else:
                raise AssertionError("cancel passed a locked activation transaction")
        finally:
            release_activation.set()
        finished = final_future.result(timeout=10)
        cancel_result = cancel_future.result(timeout=10)
    assert finished is not None and finished.status == "succeeded"
    assert cancel_result is not None and cancel_result.status == "succeeded"
    assert not cancel_result.cancel_requested
    catalog = PostgresLegalCatalogRepository(migrated_engine)
    assert catalog.get_active_snapshot(scope_id).snapshot_id == registration.snapshot_id
    assert (
        sum(
            event.actor == lease.job_id
            for event in catalog.list_activation_history(scope_id)
        )
        == 1
    )


def test_m6_db_terminal_failure_rolls_back_catalog_activation(
    migrated_engine: Engine, tmp_path: Path, monkeypatch
) -> None:
    store, registration, lease, item_key, scope_id = _claimed_final_activation(
        migrated_engine, tmp_path, prefix="rollback"
    )
    catalog = PostgresLegalCatalogRepository(migrated_engine)
    before = catalog.get_active_snapshot(scope_id)

    def fail_terminal_read(*_args):
        raise RuntimeError("synthetic terminal failure")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_read_job", fail_terminal_read)
        with pytest.raises(RuntimeError, match="synthetic terminal failure"):
            store.activate_ingestion_job(
                lease.job_id,
                "worker-a",
                lease.lease_epoch,
                item_key,
                request_hash=registration.fingerprint,
                activate=lambda connection: _activation_ref(
                    migrated_engine,
                    job=lease,
                    registration=registration,
                    connection=connection,
                ),
            )
    assert catalog.get_active_snapshot(scope_id) == before
    assert not any(
        event.actor == lease.job_id
        for event in catalog.list_activation_history(scope_id)
    )
    still_running = store.get_job_internal(lease.job_id)
    assert still_running is not None and still_running.status == "running"
    assert still_running.completed == 5
    with migrated_engine.connect() as connection:
        item_status = connection.scalar(
            text(
                "SELECT status FROM job_items WHERE job_id=:job_id AND item_key=:item_key"
            ),
            {"job_id": lease.job_id, "item_key": item_key},
        )
    assert item_status == "running"
