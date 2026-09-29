"""M6 execution facts and local trace records.

This event model intentionally has no free-form message, prompt, response, or
metadata field.  It complements the existing detailed local retrieval trace.
"""

from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Protocol


OBSERVATION_NAMES = frozenset(
    {
        "job.queued",
        "job.running",
        "job.progress",
        "job.succeeded",
        "job.failed",
        "job.cancelled",
        "job.retry",
        "evaluation.case",
        "ingestion.step",
        "node.completed",
        "tool.completed",
        "model.completed",
        "cache.lookup",
    }
)
OBSERVATION_STATUSES = frozenset(
    {"queued", "running", "succeeded", "failed", "cancelled", "retrying", "skipped"}
)
ERROR_CATEGORIES = frozenset(
    {
        "broker_unavailable",
        "database_unavailable",
        "index_build_failed",
        "provider_timeout",
        "provider_error",
        "validation_failed",
        "budget_exceeded",
        "deadline_exceeded",
        "cancelled",
        "lease_lost",
        "other",
    }
)
COUNT_NAMES = frozenset(
    {
        "total",
        "completed",
        "failed",
        "pending",
        "queued",
        "running",
        "succeeded",
        "retrievals",
        "queries",
        "tool_calls",
        "model_calls",
        "embedding_calls",
        "cache_hits",
        "cache_misses",
    }
)
BUDGET_NAMES = frozenset(
    {
        "retrieval_rounds",
        "queries",
        "tool_attempts",
        "model_attempts",
        "embedding_attempts",
        "retry_attempts",
    }
)
CACHE_STATUSES = frozenset({"hit", "miss", "bypass", "error"})
NODE_NAMES = frozenset(
    {
        "analyze_query",
        "route",
        "retrieve",
        "merge_evidence",
        "check_evidence",
        "plan_followup",
        "generate",
        "verify",
        "persist_result",
        "received",
        "parsed",
        "embedded",
        "indexed",
        "validated",
        "activated",
        "evaluation",
        "other",
    }
)
TOOL_NAMES = frozenset(
    {
        "retriever",
        "embedding",
        "generator",
        "verifier",
        "dispatcher",
        "worker",
        "indexer",
        "evaluation",
        "ingestion",
        "other",
    }
)


def _identifier(value: str | None, name: str, *, required: bool = False) -> None:
    if value is None and not required:
        return
    if not isinstance(value, str) or not value or len(value) > 255:
        raise ValueError(f"{name} must be a non-empty identifier")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name} contains control characters")


def _nonnegative_number(value: float | None, name: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a non-negative finite number")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a non-negative finite number")


def _counters(
    value: Mapping[str, int], allowed: frozenset[str], name: str
) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    result: dict[str, int] = {}
    for key, count in value.items():
        if key not in allowed or type(count) is not int or count < 0:
            raise ValueError(f"{name} contains an unsupported counter")
        result[key] = count
    return result


@dataclass(frozen=True, slots=True)
class ObservationContext:
    trace_id: str
    session_id: str | None = None
    run_id: str | None = None
    experiment_id: str | None = None
    job_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("trace_id", "session_id", "run_id", "experiment_id", "job_id"):
            _identifier(getattr(self, name), name, required=name == "trace_id")


@dataclass(frozen=True, slots=True)
class Observation:
    context: ObservationContext
    name: str
    status: str
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    node: str | None = None
    tool: str | None = None
    duration_ms: float | None = None
    queue_wait_ms: float | None = None
    counts: Mapping[str, int] = field(default_factory=dict)
    budget_used: Mapping[str, int] = field(default_factory=dict)
    retry_count: int = 0
    cache_status: str | None = None
    evidence_ids: tuple[str, ...] = ()
    error_category: str | None = None
    model_input_tokens: int | None = None
    model_output_tokens: int | None = None
    cost_usd: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.context, ObservationContext):
            raise ValueError("context must be an ObservationContext")
        if self.name not in OBSERVATION_NAMES:
            raise ValueError("name is not a supported observation type")
        if self.status not in OBSERVATION_STATUSES:
            raise ValueError("status is not supported")
        if (
            not isinstance(self.occurred_at, datetime)
            or self.occurred_at.tzinfo is None
        ):
            raise ValueError("occurred_at must be timezone-aware")
        if self.node is not None and self.node not in NODE_NAMES:
            raise ValueError("node is not supported")
        if self.tool is not None and self.tool not in TOOL_NAMES:
            raise ValueError("tool is not supported")
        for name in ("duration_ms", "queue_wait_ms", "cost_usd"):
            _nonnegative_number(getattr(self, name), name)
        object.__setattr__(
            self, "counts", _counters(self.counts, COUNT_NAMES, "counts")
        )
        object.__setattr__(
            self,
            "budget_used",
            _counters(self.budget_used, BUDGET_NAMES, "budget_used"),
        )
        for name in ("retry_count", "model_input_tokens", "model_output_tokens"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a non-negative integer")
        if self.cache_status is not None and self.cache_status not in CACHE_STATUSES:
            raise ValueError("cache_status is not supported")
        evidence_ids = tuple(self.evidence_ids)
        if len(evidence_ids) > 100:
            raise ValueError("too many evidence IDs")
        for evidence_id in evidence_ids:
            _identifier(evidence_id, "evidence_id", required=True)
        object.__setattr__(self, "evidence_ids", evidence_ids)
        if (
            self.error_category is not None
            and self.error_category not in ERROR_CATEGORIES
        ):
            # Never copy an exception string into a trace or remote payload.
            object.__setattr__(self, "error_category", "other")

    def to_local_record(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "trace_id": self.context.trace_id,
            "session_id": self.context.session_id,
            "run_id": self.context.run_id,
            "experiment_id": self.context.experiment_id,
            "job_id": self.context.job_id,
            "name": self.name,
            "status": self.status,
            "occurred_at": self.occurred_at.astimezone(timezone.utc).isoformat(),
            "node": self.node,
            "tool": self.tool,
            "duration_ms": self.duration_ms,
            "queue_wait_ms": self.queue_wait_ms,
            "counts": dict(self.counts),
            "budget_used": dict(self.budget_used),
            "retry_count": self.retry_count,
            "cache_status": self.cache_status,
            "evidence_ids": list(self.evidence_ids),
            "error_category": self.error_category,
            "model_input_tokens": self.model_input_tokens,
            "model_output_tokens": self.model_output_tokens,
            "cost_usd": self.cost_usd,
        }


class Observer(Protocol):
    def record(self, observation: Observation) -> None: ...


class LocalJsonlObserver:
    """Append M6 execution facts to a local UTF-8 JSONL trace."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, observation: Observation) -> None:
        serialized = json.dumps(observation.to_local_record(), ensure_ascii=False)
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(serialized + "\n")


class CompositeObserver:
    """Record to each sink without letting an optional sink change execution."""

    def __init__(self, *observers: Observer) -> None:
        self.observers = observers
        self.failed_count = 0

    def record(self, observation: Observation) -> None:
        for observer in self.observers:
            try:
                observer.record(observation)
            except Exception:
                self.failed_count += 1
