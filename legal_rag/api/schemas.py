from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionCreateRequest(StrictModel):
    title: str | None = Field(default=None, max_length=200)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class SessionResponse(StrictModel):
    session_id: str
    title: str | None
    status: Literal["active", "archived"]
    created_at: datetime


class MessageResponse(StrictModel):
    message_id: str
    session_id: str
    run_id: str
    role: Literal["user", "assistant"]
    content: str
    ordinal: int
    created_at: datetime


class MessagePageResponse(StrictModel):
    items: list[MessageResponse]
    next_after_ordinal: int | None


class RetrievalOptions(StrictModel):
    top_k: int = Field(default=5, ge=1, le=20)


class RunCreateRequest(StrictModel):
    question: str = Field(min_length=1, max_length=100_000)
    snapshot_id: str | None = Field(default=None, min_length=1, max_length=255)
    parent_run_id: str | None = Field(default=None, min_length=1, max_length=36)
    retrieval: RetrievalOptions = Field(default_factory=RetrievalOptions)

    @field_validator("question")
    @classmethod
    def normalize_question(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("question must contain non-whitespace characters")
        return normalized

    @field_validator("snapshot_id")
    @classmethod
    def normalize_snapshot(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value != value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("snapshot_id is invalid")
        return value

    @field_validator("parent_run_id")
    @classmethod
    def normalize_parent_run_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value != value.strip() or any(
            ord(character) < 32 or 127 <= ord(character) <= 159 for character in value
        ):
            raise ValueError("parent_run_id is invalid")
        return value


class RunAcceptedResponse(StrictModel):
    run_id: str
    status: str
    replayed: bool
    status_url: str
    events_url: str


class RunResponse(StrictModel):
    run_id: str
    session_id: str
    parent_run_id: str | None
    status: str
    snapshot_id: str
    snapshot_revision: int
    activation_id: str
    profile_id: str
    graph_version: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str | None
    last_completed_node: str | None
    execution_deadline_at: datetime | None
    stop_reason: str | None
    answer: dict[str, Any] | None = None
    evidence: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None


class CancelRunResponse(StrictModel):
    run_id: str
    status: str
    provider_revoked: Literal[False] = False


class EvaluationCreateRequest(StrictModel):
    experiment_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class IngestionCreateRequest(StrictModel):
    artifact_ref: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class JobAcceptedResponse(StrictModel):
    job_id: str
    status: str
    status_url: str


class JobResponse(StrictModel):
    job_id: str
    kind: Literal["evaluation", "ingestion"]
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    total: int
    completed: int
    failed: int
    pending: int
    stage: str
    outbox_status: str | None
    outbox_error_code: str | None
    cancel_requested: bool
    error_code: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    queue_wait_ms: float | None


class CancelJobResponse(StrictModel):
    job_id: str
    status: str
    cancel_requested: bool


class ErrorDetail(StrictModel):
    code: str
    message: str


class ErrorResponse(StrictModel):
    error: ErrorDetail
