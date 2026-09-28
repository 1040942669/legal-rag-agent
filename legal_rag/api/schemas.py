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


class RunAcceptedResponse(StrictModel):
    run_id: str
    status: str
    replayed: bool
    status_url: str
    events_url: str


class RunResponse(StrictModel):
    run_id: str
    session_id: str
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
    answer: dict[str, Any] | None = None
    evidence: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None


class CancelRunResponse(StrictModel):
    run_id: str
    status: str
    provider_revoked: Literal[False] = False


class ErrorDetail(StrictModel):
    code: str
    message: str


class ErrorResponse(StrictModel):
    error: ErrorDetail
