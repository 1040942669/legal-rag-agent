from __future__ import annotations

import hashlib
from unittest.mock import Mock

import pytest

from legal_rag.jobs.store import JobContractError, JobStore


@pytest.mark.parametrize(
    "request_ref",
    [
        "C:/private/evaluation.json",
        "../private/evaluation.json",
        "https://example.org/evaluation.json",
        "safe/private.json",
        "prompt with private content",
        {"registry_id": "safe"},
    ],
)
def test_request_ref_rejects_secrets_and_unbounded_content(request_ref: object) -> None:
    store = JobStore(Mock())
    with pytest.raises(JobContractError):
        store.create_job(
            owner_id="alice",
            kind="evaluation",
            request_ref=request_ref,  # type: ignore[arg-type]
            request_hash="a" * 64,
            total=2,
        )


def test_job_submission_requires_valid_owner_kind_and_progress() -> None:
    store = JobStore(Mock())
    for kwargs in (
        {"owner_id": "", "kind": "evaluation", "total": 1},
        {"owner_id": "alice", "kind": "chat", "total": 1},
        {"owner_id": "alice", "kind": "evaluation", "total": -1},
        {"owner_id": "alice", "kind": "evaluation", "total": True},
    ):
        with pytest.raises(JobContractError):
            store.create_job(
                request_ref="fixture",
                request_hash=hashlib.sha256(b"fixture").hexdigest(),
                **kwargs,  # type: ignore[arg-type]
            )
