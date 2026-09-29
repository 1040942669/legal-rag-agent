from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit


TASK_NAME = "legal_rag.jobs.execute"
MESSAGE_SCHEMA_VERSION = 1


def _positive_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a non-negative integer") from exc
    if value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


class TransientJobError(RuntimeError):
    """A job can be retried after a temporary infrastructure failure."""


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    broker_url: str
    concurrency: int = 2
    broker_connect_timeout: int = 3
    broker_read_timeout: int = 10
    soft_time_limit: int = 1800
    hard_time_limit: int = 1860
    visibility_timeout: int = 1920
    max_retries: int = 2

    def __post_init__(self) -> None:
        parsed = urlsplit(self.broker_url)
        if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
            raise ValueError("broker_url must be a Redis URL with a host")
        for field_name in (
            "concurrency",
            "broker_connect_timeout",
            "broker_read_timeout",
            "soft_time_limit",
            "hard_time_limit",
            "visibility_timeout",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{field_name} must be a positive integer")
        if self.hard_time_limit <= self.soft_time_limit:
            raise ValueError("hard_time_limit must exceed soft_time_limit")
        if self.visibility_timeout <= self.hard_time_limit:
            raise ValueError("visibility_timeout must exceed hard_time_limit")
        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int):
            raise ValueError("max_retries must be a non-negative integer")
        if self.max_retries < 0 or self.max_retries > 5:
            raise ValueError("max_retries must be between zero and five")

    @classmethod
    def from_env(cls) -> WorkerSettings:
        broker_url = os.environ.get("LEGAL_RAG_REDIS_URL")
        if not broker_url:
            raise ValueError("LEGAL_RAG_REDIS_URL is required for M6 jobs")
        return cls(
            broker_url=broker_url,
            concurrency=_positive_env("LEGAL_RAG_JOB_CONCURRENCY", 2),
            broker_connect_timeout=_positive_env("LEGAL_RAG_JOB_CONNECT_TIMEOUT", 3),
            broker_read_timeout=_positive_env("LEGAL_RAG_JOB_READ_TIMEOUT", 10),
            soft_time_limit=_positive_env("LEGAL_RAG_JOB_SOFT_LIMIT", 1800),
            hard_time_limit=_positive_env("LEGAL_RAG_JOB_HARD_LIMIT", 1860),
            visibility_timeout=_positive_env("LEGAL_RAG_JOB_VISIBILITY_TIMEOUT", 1920),
            max_retries=_nonnegative_env("LEGAL_RAG_JOB_MAX_RETRIES", 2),
        )


def celery_configuration(settings: WorkerSettings) -> dict[str, Any]:
    """Configuration is independent of Celery imports for offline validation."""

    return {
        "broker_url": settings.broker_url,
        "result_backend": None,
        "task_serializer": "json",
        "accept_content": ["json"],
        "result_serializer": "json",
        "task_ignore_result": True,
        "task_acks_late": True,
        "task_reject_on_worker_lost": True,
        "task_acks_on_failure_or_timeout": True,
        "task_soft_time_limit": settings.soft_time_limit,
        "task_time_limit": settings.hard_time_limit,
        "worker_concurrency": settings.concurrency,
        "worker_prefetch_multiplier": 1,
        "worker_soft_shutdown_timeout": 5.0,
        "broker_connection_timeout": settings.broker_connect_timeout,
        "broker_connection_retry": True,
        "broker_connection_retry_on_startup": True,
        "broker_connection_max_retries": 6,
        "broker_transport_options": {
            "visibility_timeout": settings.visibility_timeout,
            "socket_connect_timeout": settings.broker_connect_timeout,
            "socket_timeout": settings.broker_read_timeout,
            "max_retries": 1,
        },
        "visibility_timeout": settings.visibility_timeout,
        "task_publish_retry": False,
        "task_default_queue": "legal_rag_jobs",
        "task_default_delivery_mode": "persistent",
    }


def build_celery_app(
    settings: WorkerSettings,
    *,
    processor: Callable[[str], str] | None = None,
) -> Any:
    """Register one small broker message; PostgreSQL owns its durable state."""

    try:
        from celery import Celery
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "install the jobs optional dependency to run workers"
        ) from exc

    app = Celery("legal_rag_jobs")
    app.conf.update(celery_configuration(settings))
    selected_processor = processor

    @app.task(
        name=TASK_NAME,
        bind=True,
        max_retries=settings.max_retries,
        acks_late=True,
        reject_on_worker_lost=True,
        ignore_result=True,
    )
    def execute_job(self: Any, job_id: str, schema_version: int) -> str:
        if schema_version != MESSAGE_SCHEMA_VERSION:
            return "unsupported_message_schema"
        if not isinstance(job_id, str) or not job_id or len(job_id) > 128:
            return "invalid_job_id"
        try:
            if selected_processor is None:
                from .handlers import process_job_from_environment

                return process_job_from_environment(job_id)
            return selected_processor(job_id)
        except Exception:  # noqa: BLE001 - finite retry covers infrastructure loss.
            if self.request.retries >= settings.max_retries:
                raise TransientJobError("job_execution_unavailable") from None
            raise self.retry(
                exc=TransientJobError("job_execution_unavailable"),
                countdown=min(2**self.request.retries, 30),
            ) from None

    return app
