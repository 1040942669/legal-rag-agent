"""Test-only submitter that exits before any outbox publish begins."""

from __future__ import annotations

import os

from legal_rag.jobs.registry import JobRegistry
from legal_rag.jobs.store import JobStore
from legal_rag.storage.database import DatabaseSettings, create_database_engine


def main() -> None:
    registry = JobRegistry.from_json_file(os.environ["LEGAL_RAG_JOB_REGISTRY_PATH"])
    kind = os.environ["LEGAL_RAG_M6_TEST_SUBMIT_KIND"]
    reference = os.environ["LEGAL_RAG_M6_TEST_SUBMIT_REF"]
    registration = registry.resolve(kind, reference)
    if registration is None:
        raise ValueError("test registration is missing")
    engine = create_database_engine(
        DatabaseSettings.from_env(connect_timeout_seconds=5)
    )
    try:
        job = JobStore(engine).create_job(
            owner_id="m6-real-test-owner",
            kind=kind,
            request_ref=reference,
            request_hash=registration.fingerprint,
            total=registration.total,
        )
        print(job.job_id, flush=True)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
