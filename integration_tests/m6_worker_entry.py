"""Test-only Celery process entry with deterministic crash/index fault points.

This module is never imported by the application command or packaged scripts.
The real handler, PostgreSQL store and Redis broker remain in the test path.
"""

from __future__ import annotations

import os
import time
from pathlib import Path


def _install_pause_after_item_commit() -> None:
    target_job_id = os.environ.get("LEGAL_RAG_M6_TEST_PAUSE_JOB_ID")
    if not target_job_id:
        return
    marker = Path(os.environ["LEGAL_RAG_M6_TEST_PAUSE_MARKER"])
    delay = float(os.environ.get("LEGAL_RAG_M6_TEST_PAUSE_SECONDS", "20"))
    from legal_rag.jobs.store import JobStore

    original = JobStore.complete_item

    def complete_then_pause(self, job_id, worker_id, lease_epoch, item_key, result_ref):
        committed = original(self, job_id, worker_id, lease_epoch, item_key, result_ref)
        if committed and job_id == target_job_id and not marker.exists():
            marker.write_text("committed", encoding="ascii")
            time.sleep(delay)
        return committed

    JobStore.complete_item = complete_then_pause


def _install_index_failure() -> None:
    if os.environ.get("LEGAL_RAG_M6_TEST_FAIL_INDEX") != "1":
        return
    from legal_rag.storage.ann import PostgresHnswIndexManager

    def fail_index(self, *args, **kwargs):
        raise RuntimeError("test-only index failure")

    PostgresHnswIndexManager.ensure_index = fail_index


def main() -> int:
    _install_pause_after_item_commit()
    _install_index_failure()
    from legal_rag.jobs.command import main as jobs_main

    return jobs_main(["worker"])


if __name__ == "__main__":
    raise SystemExit(main())
