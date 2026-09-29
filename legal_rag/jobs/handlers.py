"""M6 batch handlers over existing M2 and M3 durable business artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import uuid
from contextlib import contextmanager
from datetime import timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .worker import TransientJobError


class JobPermanentError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


class JobLeaseLost(TransientJobError):
    """A stale worker must not publish item or terminal state."""


class JobCancelled(RuntimeError):
    """Cancellation is observed at a durable task boundary."""


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
    ).hexdigest()


def _item_key(*, job_id: str, kind: str, identity: str, config_hash: str) -> str:
    return _digest(
        {
            "schema_version": 1,
            "job_id": job_id,
            "kind": kind,
            "identity": identity,
            "config_hash": config_hash,
        }
    )


def _required_write(result: bool, operation: str) -> None:
    if not result:
        raise JobLeaseLost(f"job lease lost before {operation}")


def _observe(
    observer: Any | None,
    store: Any,
    job: Any,
    *,
    name: str,
    status: str,
    node: str | None = None,
    tool: str = "worker",
    error_category: str | None = None,
    queue_wait_ms: float | None = None,
) -> None:
    if observer is None:
        return
    try:
        from legal_rag.observability.events import Observation, ObservationContext

        current = store.get_job_internal(job.job_id)
        counts = (
            {
                "total": current.total,
                "completed": current.completed,
                "failed": current.failed,
                "pending": current.pending,
            }
            if current is not None
            else {}
        )
        observer.record(
            Observation(
                context=ObservationContext(
                    trace_id=job.job_id,
                    job_id=job.job_id,
                    experiment_id=job.job_id if job.kind == "evaluation" else None,
                ),
                name=name,
                status=status,
                node=node,
                tool=tool,
                counts=counts,
                queue_wait_ms=queue_wait_ms,
                error_category=error_category,
            )
        )
    except Exception:  # noqa: BLE001 - optional observation is never a job gate.
        return


@contextmanager
def _experiment_lock(experiment_root: Path, job_id: str) -> Iterator[None]:
    """Serialize M2 file writers across worker processes, including lease takeover."""

    experiment_root.mkdir(parents=True, exist_ok=True)
    lock_path = experiment_root / f".m6-{job_id}.lock"
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.read(1) != b"1":
                handle.seek(0)
                handle.write(b"1")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _case_result_ref(artifact: Any, case_id: str) -> str | None:
    # load_completed validates the attempt, completion marker and their link.
    evidence = artifact.case_artifact_evidence(case_id)
    ref = evidence["completion_artifact_sha256"]
    if ref is None:
        return None
    artifact.load_completed(case_id)
    return ref


def run_evaluation_job(
    store: Any,
    job: Any,
    registration: Any,
    *,
    worker_id: str,
    lease_epoch: int,
    check_active: Callable[[], None],
    observer: Any | None = None,
) -> str:
    """Run one provider-free M2 case at a time and reconcile immutable artifacts."""

    from legal_rag.experiment_lifecycle import (
        aggregate_experiment_by_id,
        build_lifecycle_plan,
        resume_experiment,
        run_experiment,
    )
    from legal_rag.experiment_store import ExperimentStore

    plan = build_lifecycle_plan(
        experiment_id=job.job_id,
        mode="offline",
        dataset_id=registration.dataset_id,
        repository_root=registration.repository_root,
        concurrency=1,
        max_retries=0,
        allow_external_calls=False,
    )
    if not plan["runnable"]:
        raise JobPermanentError("evaluation_not_runnable")
    manifest = plan["manifest"]
    cases = manifest["dataset"]["cases"]
    if len(cases) != registration.total or job.total != registration.total:
        raise JobPermanentError("evaluation_total_mismatch")
    # The server registration is immutable for this job. The M2 manifest still
    # independently rejects a changed code/corpus contract on an actual resume.
    config_hash = registration.fingerprint
    resumed_stage = getattr(job, "stage", "received")
    with _experiment_lock(registration.experiment_root, job.job_id):
        for case in cases:
            check_active()
            case_id = case["case_id"]
            item_key = _item_key(
                job_id=job.job_id,
                kind="evaluation",
                identity=f"{case_id}:evaluation",
                config_hash=config_hash,
            )
            claim = store.claim_item(
                job.job_id, worker_id, lease_epoch, item_key, max_attempts=3
            )
            artifact_path = registration.experiment_root / job.job_id
            artifact = (
                ExperimentStore.open(registration.experiment_root, job.job_id)
                if artifact_path.exists()
                else None
            )
            existing_ref = _case_result_ref(artifact, case_id) if artifact else None
            if claim.status == "succeeded":
                if existing_ref is None or existing_ref != claim.result_ref:
                    raise JobPermanentError("evaluation_artifact_missing")
                _observe(
                    observer,
                    store,
                    job,
                    name="evaluation.case",
                    status="skipped",
                    node="evaluation",
                    tool="evaluation",
                )
                continue
            if existing_ref is not None:
                _required_write(
                    store.reconcile_item(
                        job.job_id, worker_id, lease_epoch, item_key, existing_ref
                    ),
                    "evaluation artifact reconciliation",
                )
                _observe(
                    observer,
                    store,
                    job,
                    name="evaluation.case",
                    status="succeeded",
                    node="evaluation",
                    tool="evaluation",
                )
                _observe(
                    observer,
                    store,
                    job,
                    name="job.progress",
                    status="running",
                    node="evaluation",
                    tool="worker",
                )
                continue
            if resumed_stage in {"aggregating", "completed"}:
                raise JobPermanentError("evaluation_stage_inconsistent")
            if not claim.acquired:
                if claim.status == "cancelled":
                    raise JobCancelled()
                if claim.status == "lease_lost":
                    raise JobLeaseLost("job lease lost before evaluation item")
                raise JobPermanentError("evaluation_item_unavailable")
            try:
                _required_write(
                    store.set_stage(job.job_id, worker_id, lease_epoch, "evaluating"),
                    "evaluation stage",
                )
                if artifact is None:
                    execution = run_experiment(
                        experiment_id=job.job_id,
                        mode="offline",
                        dataset_id=registration.dataset_id,
                        repository_root=registration.repository_root,
                        experiment_root=registration.experiment_root,
                        concurrency=1,
                        max_retries=0,
                        stop_after_completed=1,
                    )
                else:
                    execution = resume_experiment(
                        experiment_id=job.job_id,
                        repository_root=registration.repository_root,
                        experiment_root=registration.experiment_root,
                        stop_after_completed=1,
                    )
                if case_id not in execution.summary.inventory.succeeded:
                    raise JobPermanentError("evaluation_case_failed")
                artifact = ExperimentStore.open(
                    registration.experiment_root, job.job_id
                )
                result_ref = _case_result_ref(artifact, case_id)
                if result_ref is None:
                    raise JobPermanentError("evaluation_artifact_missing")
                check_active()
                _required_write(
                    store.complete_item(
                        job.job_id, worker_id, lease_epoch, item_key, result_ref
                    ),
                    "evaluation item completion",
                )
                _observe(
                    observer,
                    store,
                    job,
                    name="evaluation.case",
                    status="succeeded",
                    node="evaluation",
                    tool="evaluation",
                )
                _observe(
                    observer,
                    store,
                    job,
                    name="job.progress",
                    status="running",
                    node="evaluation",
                    tool="worker",
                )
            except JobLeaseLost:
                raise
            except JobCancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - persist only a redacted code.
                store.fail_item(
                    job.job_id,
                    worker_id,
                    lease_epoch,
                    item_key,
                    "evaluation_case_failed",
                    retryable=False,
                )
                if isinstance(exc, JobPermanentError):
                    raise
                raise JobPermanentError("evaluation_case_failed") from None
        check_active()
        if resumed_stage != "completed":
            _required_write(
                store.set_stage(job.job_id, worker_id, lease_epoch, "aggregating"),
                "aggregation stage",
            )
        aggregate_experiment_by_id(
            experiment_id=job.job_id,
            repository_root=registration.repository_root,
            experiment_root=registration.experiment_root,
        )
        if resumed_stage != "completed":
            _required_write(
                store.set_stage(job.job_id, worker_id, lease_epoch, "completed"),
                "evaluation completion stage",
            )
    return "succeeded"


def _stage_item(
    store: Any,
    job: Any,
    registration: Any,
    *,
    stage: str,
    worker_id: str,
    lease_epoch: int,
    action: Callable[[], str],
    check_active: Callable[[], None],
    observer: Any | None = None,
) -> None:
    check_active()
    stage_order = (
        "received",
        "parsed",
        "embedded",
        "indexed",
        "validated",
        "activated",
    )
    resumed_stage = getattr(job, "stage", "received")
    if stage_order.index(stage) >= stage_order.index(resumed_stage):
        _required_write(
            store.set_stage(job.job_id, worker_id, lease_epoch, stage), stage
        )
    item_key = _item_key(
        job_id=job.job_id,
        kind="ingestion",
        identity=f"{registration.snapshot_id}:{registration.profile_id}:{stage}",
        config_hash=registration.fingerprint,
    )
    claim = store.claim_item(
        job.job_id, worker_id, lease_epoch, item_key, max_attempts=3
    )
    if claim.status == "succeeded":
        _observe(
            observer,
            store,
            job,
            name="ingestion.step",
            status="skipped",
            node=stage,
            tool="ingestion",
        )
        return
    if stage_order.index(stage) < stage_order.index(resumed_stage):
        raise JobPermanentError("ingestion_stage_inconsistent")
    if not claim.acquired:
        if claim.status == "cancelled":
            raise JobCancelled()
        if claim.status == "lease_lost":
            raise JobLeaseLost("job lease lost before ingestion stage")
        raise JobPermanentError("ingestion_stage_unavailable")
    try:
        result_ref = action()
        check_active()
        _required_write(
            store.complete_item(
                job.job_id, worker_id, lease_epoch, item_key, result_ref
            ),
            f"{stage} completion",
        )
        _observe(
            observer,
            store,
            job,
            name="ingestion.step",
            status="succeeded",
            node=stage,
            tool="indexer" if stage == "indexed" else "ingestion",
        )
        _observe(
            observer,
            store,
            job,
            name="job.progress",
            status="running",
            node=stage,
            tool="worker",
        )
    except (JobLeaseLost, JobCancelled):
        raise
    except Exception as exc:  # noqa: BLE001 - source text and DSNs stay out of job rows.
        code = (
            exc.error_code
            if isinstance(exc, JobPermanentError)
            else "index_build_failed"
            if stage == "indexed"
            else f"ingestion_{stage}_failed"
        )
        store.fail_item(
            job.job_id, worker_id, lease_epoch, item_key, code, retryable=False
        )
        raise JobPermanentError(code) from None


def _activation_ref(engine: Any, *, job: Any, registration: Any) -> str:
    from legal_rag.storage.catalog import (
        CatalogUnavailableError,
        PostgresLegalCatalogRepository,
    )

    catalog = PostgresLegalCatalogRepository(engine)
    # A previous attempt may have committed activation just before worker loss.
    # An audit event from this job is sufficient proof; never re-activate an old
    # snapshot after another actor has since moved the pointer.
    before_revision: int | None = None
    while True:
        page = catalog.list_activation_history(
            registration.scope_id, before_revision=before_revision, limit=100
        )
        for event in page:
            if (
                event.actor == job.job_id
                and event.target_snapshot_id == registration.snapshot_id
            ):
                return _digest({"activation_id": event.activation_id})
        if not page or len(page) < 100:
            break
        before_revision = page[-1].revision
    try:
        current = catalog.get_active_snapshot(registration.scope_id)
    except CatalogUnavailableError:
        current = None
    if current is not None and current.snapshot_id == registration.snapshot_id:
        return _digest({"activation_id": current.activation_id})
    result = catalog.activate_snapshot(
        scope_id=registration.scope_id,
        snapshot_id=registration.snapshot_id,
        expected_current_snapshot_id=current.snapshot_id if current else None,
        expected_current_revision=current.revision if current else None,
        expected_current_activation_id=current.activation_id if current else None,
        required_profile_id=registration.profile_id,
        actor=job.job_id,
        reason="m6_ingestion_job",
    )
    return _digest({"activation_id": result.event.activation_id})


def run_ingestion_job(
    store: Any,
    job: Any,
    registration: Any,
    *,
    engine: Any,
    worker_id: str,
    lease_epoch: int,
    check_active: Callable[[], None],
    observer: Any | None = None,
) -> str:
    """Validate prebuilt artifacts, import atomically, then activate last."""

    from legal_rag.storage.import_workflow import (
        apply_prepared_import,
        validate_import_plan,
    )
    from legal_rag.storage.repository import PostgresCorpusRepository

    if registration.total != 6 or job.total != 6:
        raise JobPermanentError("ingestion_total_mismatch")
    _stage_item(
        store,
        job,
        registration,
        stage="received",
        worker_id=worker_id,
        lease_epoch=lease_epoch,
        action=lambda: registration.fingerprint,
        check_active=check_active,
        observer=observer,
    )
    prepared: Any | None = None

    def parsed() -> str:
        nonlocal prepared
        # Revalidation is intentional on redelivery: a stored fingerprint does
        # not prove that server-local prebuilt files remain unchanged.
        prepared = validate_import_plan(
            registration.manifest_path,
            registration.plan_path,
            source_root=registration.source_root,
        )
        target = prepared.plan["target"]
        if (target["scope_id"], target["profile_id"], target["snapshot_id"]) != (
            registration.scope_id,
            registration.profile_id,
            registration.snapshot_id,
        ):
            raise JobPermanentError("ingestion_target_mismatch")
        return prepared.plan["plan_sha256"]

    _stage_item(
        store,
        job,
        registration,
        stage="parsed",
        worker_id=worker_id,
        lease_epoch=lease_epoch,
        action=parsed,
        check_active=check_active,
        observer=observer,
    )
    if prepared is None:
        try:
            parsed()
        except Exception:  # noqa: BLE001 - no source contents in status.
            raise JobPermanentError("ingestion_artifact_changed") from None
    _stage_item(
        store,
        job,
        registration,
        stage="embedded",
        worker_id=worker_id,
        lease_epoch=lease_epoch,
        action=lambda: prepared.bundle.bundle_hash,
        check_active=check_active,
        observer=observer,
    )

    def indexed() -> str:
        try:
            apply_prepared_import(prepared, engine=engine)
        except Exception:  # noqa: BLE001 - only a stable code is persisted.
            raise JobPermanentError("ingestion_import_failed") from None
        if registration.build_hnsw:
            from legal_rag.storage.ann import PostgresHnswIndexManager
            from legal_rag.storage.retrieval import RetrievalFilters

            PostgresHnswIndexManager(engine).ensure_index(
                filters=RetrievalFilters(
                    scope_id=registration.scope_id,
                    snapshot_id=registration.snapshot_id,
                    profile_id=registration.profile_id,
                ),
                expected_profile=prepared.bundle.embedding_profile.to_identity(),
            )
        return prepared.bundle.bundle_hash

    _stage_item(
        store,
        job,
        registration,
        stage="indexed",
        worker_id=worker_id,
        lease_epoch=lease_epoch,
        action=indexed,
        check_active=check_active,
        observer=observer,
    )

    def validated() -> str:
        persisted = PostgresCorpusRepository(engine).validate_bundle(prepared.bundle)
        return _digest(
            {"bundle_hash": prepared.bundle.bundle_hash, "counts": persisted.__dict__}
        )

    _stage_item(
        store,
        job,
        registration,
        stage="validated",
        worker_id=worker_id,
        lease_epoch=lease_epoch,
        action=validated,
        check_active=check_active,
        observer=observer,
    )
    _stage_item(
        store,
        job,
        registration,
        stage="activated",
        worker_id=worker_id,
        lease_epoch=lease_epoch,
        action=lambda: _activation_ref(engine, job=job, registration=registration),
        check_active=check_active,
        observer=observer,
    )
    return "succeeded"


class _LeaseHeartbeat:
    def __init__(self, store: Any, job_id: str, worker_id: str, epoch: int) -> None:
        self.store = store
        self.job_id = job_id
        self.worker_id = worker_id
        self.epoch = epoch
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self.stop.wait(10):
            try:
                if not self.store.heartbeat(
                    self.job_id, self.worker_id, self.epoch, lease_seconds=30
                ):
                    self.lost.set()
                    return
            except Exception:  # noqa: BLE001 - no progress after uncertain lease.
                self.lost.set()
                return

    def __enter__(self) -> _LeaseHeartbeat:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.stop.set()
        self.thread.join(timeout=2)

    def check(self) -> None:
        if self.lost.is_set():
            raise JobLeaseLost("job heartbeat lost")
        current = self.store.get_job_internal(self.job_id)
        if (
            current is None
            or current.lease_epoch != self.epoch
            or current.status != "running"
        ):
            raise JobLeaseLost("job lease was replaced")
        if current.cancel_requested:
            raise JobCancelled()


def process_job(
    store: Any,
    registry: Any,
    engine: Any,
    job_id: str,
    *,
    worker_id: str,
    observer: Any | None = None,
) -> str:
    """Claim one PostgreSQL job; duplicate Celery deliveries are safe."""

    current = store.get_job_internal(job_id)
    if current is None:
        return "missing"
    if current.status in {"succeeded", "failed", "cancelled"}:
        return current.status
    lease = store.claim_job(job_id, worker_id, lease_seconds=30)
    if lease is None:
        latest = store.get_job_internal(job_id)
        return (
            latest.status
            if latest and latest.status in {"succeeded", "failed", "cancelled"}
            else "busy"
        )
    registration = registry.resolve(lease.kind, lease.request_ref)
    if registration is None or registration.fingerprint != lease.request_hash:
        finished = store.finish_job(
            job_id,
            worker_id,
            lease.lease_epoch,
            status="failed",
            error_code="registry_mismatch",
        )
        if finished is not None:
            _observe(
                observer,
                store,
                finished,
                name="job.failed",
                status="failed",
                error_category="validation_failed",
            )
        return "failed"
    queue_wait_ms = (
        max(
            0.0,
            (
                lease.started_at.astimezone(timezone.utc)
                - lease.created_at.astimezone(timezone.utc)
            ).total_seconds()
            * 1000,
        )
        if lease.started_at is not None
        else None
    )
    _observe(
        observer,
        store,
        lease,
        name="job.running",
        status="running",
        tool="worker",
        queue_wait_ms=queue_wait_ms,
    )
    try:
        with _LeaseHeartbeat(store, job_id, worker_id, lease.lease_epoch) as heartbeat:
            if lease.kind == "evaluation":
                result = run_evaluation_job(
                    store,
                    lease,
                    registration,
                    worker_id=worker_id,
                    lease_epoch=lease.lease_epoch,
                    check_active=heartbeat.check,
                    observer=observer,
                )
            elif lease.kind == "ingestion":
                result = run_ingestion_job(
                    store,
                    lease,
                    registration,
                    engine=engine,
                    worker_id=worker_id,
                    lease_epoch=lease.lease_epoch,
                    check_active=heartbeat.check,
                    observer=observer,
                )
            else:
                raise JobPermanentError("unsupported_job_kind")
            heartbeat.check()
            finished = store.finish_job(
                job_id, worker_id, lease.lease_epoch, status=result
            )
            if finished is None:
                raise JobLeaseLost("job lease lost before terminal status")
            _observe(
                observer, store, finished, name="job.succeeded", status="succeeded"
            )
            return finished.status
    except JobCancelled:
        finished = store.finish_job(
            job_id, worker_id, lease.lease_epoch, status="cancelled"
        )
        if finished is not None:
            _observe(
                observer, store, finished, name="job.cancelled", status="cancelled"
            )
        return "cancelled"
    except JobPermanentError as exc:
        finished = store.finish_job(
            job_id,
            worker_id,
            lease.lease_epoch,
            status="failed",
            error_code=exc.error_code,
        )
        if finished is not None:
            _observe(
                observer,
                store,
                finished,
                name="job.failed",
                status="failed",
                error_category=(
                    "index_build_failed"
                    if exc.error_code == "index_build_failed"
                    else "validation_failed"
                ),
            )
        return "failed"
    except JobLeaseLost:
        raise
    except Exception:  # noqa: BLE001 - do not expose source or DSNs.
        finished = store.finish_job(
            job_id,
            worker_id,
            lease.lease_epoch,
            status="failed",
            error_code="job_failed",
        )
        if finished is not None:
            _observe(
                observer,
                store,
                finished,
                name="job.failed",
                status="failed",
                error_category="other",
            )
        return "failed"


def process_job_from_environment(job_id: str) -> str:
    from legal_rag.storage.database import DatabaseSettings, create_database_engine

    from .registry import JobRegistry
    from .store import JobStore
    from legal_rag.observability.config import (
        close_observer,
        observer_from_environment,
    )

    engine = create_database_engine(
        DatabaseSettings.from_env(
            connect_timeout_seconds=5, statement_timeout_ms=3_000_000
        )
    )
    observer = None
    try:
        worker_id = f"{socket.gethostname()[:40]}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        store = JobStore(engine)
        registry_path = os.environ.get("LEGAL_RAG_JOB_REGISTRY_PATH")
        try:
            if not registry_path:
                raise JobPermanentError("job_registry_unconfigured")
            registry = JobRegistry.from_json_file(registry_path)
        except Exception:  # noqa: BLE001 - invalid server config is a durable failure.
            lease = store.claim_job(job_id, worker_id, lease_seconds=30)
            if lease is not None:
                store.finish_job(
                    job_id,
                    worker_id,
                    lease.lease_epoch,
                    status="failed",
                    error_code="registry_invalid",
                )
                return "failed"
            current = store.get_job_internal(job_id)
            return current.status if current is not None else "missing"
        try:
            observer = observer_from_environment()
        except Exception:  # noqa: BLE001 - optional exporter is not a job gate.
            observer = None
        return process_job(
            store,
            registry,
            engine,
            job_id,
            worker_id=worker_id,
            observer=observer,
        )
    finally:
        close_observer(observer)
        engine.dispose()
