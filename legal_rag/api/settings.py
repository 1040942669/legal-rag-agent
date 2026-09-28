from __future__ import annotations

import math
import os
from dataclasses import dataclass

from legal_rag.storage.database import DatabaseSettings

from .auth import AuthenticationConfigurationError, TokenAuthenticator


def _positive_int(name: str, default: int, *, maximum: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return value


def _positive_float(name: str, default: float, *, maximum: float) -> float:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not 0 < value <= maximum:
        raise ValueError(f"{name} must be greater than 0 and at most {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class ServiceSettings:
    question_max_characters: int = 8_000
    message_page_max: int = 100
    history_max_messages: int = 16
    history_max_characters: int = 12_000
    idempotency_ttl_seconds: int = 86_400
    lease_seconds: int = 300
    executor_timeout_seconds: float = 180.0
    supervisor_poll_seconds: float = 0.2
    sse_poll_seconds: float = 0.2
    sse_heartbeat_seconds: float = 15.0
    graph_version: str = "m4-linear-v1"

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("LEGAL_RAG_QUESTION_MAX_CHARACTERS", self.question_max_characters, 100_000),
            ("LEGAL_RAG_MESSAGE_PAGE_MAX", self.message_page_max, 100),
            ("LEGAL_RAG_HISTORY_MAX_MESSAGES", self.history_max_messages, 100),
            ("LEGAL_RAG_HISTORY_MAX_CHARACTERS", self.history_max_characters, 100_000),
            ("LEGAL_RAG_IDEMPOTENCY_TTL_SECONDS", self.idempotency_ttl_seconds, 31_536_000),
            ("LEGAL_RAG_RUN_LEASE_SECONDS", self.lease_seconds, 86_400),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be between 1 and {maximum}")
        for name, value, maximum in (
            ("LEGAL_RAG_EXECUTOR_TIMEOUT_SECONDS", self.executor_timeout_seconds, 3_600.0),
            ("LEGAL_RAG_SUPERVISOR_POLL_SECONDS", self.supervisor_poll_seconds, 60.0),
            ("LEGAL_RAG_SSE_POLL_SECONDS", self.sse_poll_seconds, 60.0),
            ("LEGAL_RAG_SSE_HEARTBEAT_SECONDS", self.sse_heartbeat_seconds, 300.0),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0 < value <= maximum
            ):
                raise ValueError(f"{name} must be greater than 0 and at most {maximum}")
        if self.lease_seconds <= self.executor_timeout_seconds:
            raise ValueError("LEGAL_RAG_RUN_LEASE_SECONDS must exceed executor timeout")
        if (
            not isinstance(self.graph_version, str)
            or not self.graph_version
            or self.graph_version != self.graph_version.strip()
            or len(self.graph_version) > 64
            or any(ord(character) < 32 for character in self.graph_version)
        ):
            raise ValueError("graph_version must be a non-empty printable identifier")

    @classmethod
    def from_env(cls) -> ServiceSettings:
        return cls(
            question_max_characters=_positive_int(
                "LEGAL_RAG_QUESTION_MAX_CHARACTERS", 8_000, maximum=100_000
            ),
            message_page_max=_positive_int(
                "LEGAL_RAG_MESSAGE_PAGE_MAX", 100, maximum=100
            ),
            history_max_messages=_positive_int(
                "LEGAL_RAG_HISTORY_MAX_MESSAGES", 16, maximum=100
            ),
            history_max_characters=_positive_int(
                "LEGAL_RAG_HISTORY_MAX_CHARACTERS", 12_000, maximum=100_000
            ),
            idempotency_ttl_seconds=_positive_int(
                "LEGAL_RAG_IDEMPOTENCY_TTL_SECONDS",
                86_400,
                maximum=31_536_000,
            ),
            lease_seconds=_positive_int(
                "LEGAL_RAG_RUN_LEASE_SECONDS", 300, maximum=86_400
            ),
            executor_timeout_seconds=_positive_float(
                "LEGAL_RAG_EXECUTOR_TIMEOUT_SECONDS", 180.0, maximum=3_600.0
            ),
            supervisor_poll_seconds=_positive_float(
                "LEGAL_RAG_SUPERVISOR_POLL_SECONDS", 0.2, maximum=60.0
            ),
            sse_poll_seconds=_positive_float(
                "LEGAL_RAG_SSE_POLL_SECONDS", 0.2, maximum=60.0
            ),
            sse_heartbeat_seconds=_positive_float(
                "LEGAL_RAG_SSE_HEARTBEAT_SECONDS", 15.0, maximum=300.0
            ),
        )


def load_environment_configuration() -> tuple[
    DatabaseSettings,
    TokenAuthenticator,
    ServiceSettings,
]:
    database = DatabaseSettings.from_env()
    token_json = os.environ.get("LEGAL_RAG_AUTH_TOKENS_JSON", "").strip()
    if not token_json:
        raise AuthenticationConfigurationError("LEGAL_RAG_AUTH_TOKENS_JSON is required")
    return (
        database,
        TokenAuthenticator.from_json(token_json),
        ServiceSettings.from_env(),
    )
