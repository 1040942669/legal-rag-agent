from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from legal_rag.provider_errors import (
    ProviderErrorClassification,
    classify_provider_error,
)


class BudgetExhausted(RuntimeError):
    def __init__(self, stop_reason: str) -> None:
        self.stop_reason = stop_reason
        super().__init__(stop_reason)


class ExecutionDeadlineExceeded(BudgetExhausted):
    def __init__(self) -> None:
        super().__init__("deadline_exceeded")


@dataclass(frozen=True, slots=True)
class HarnessBudgetConfig:
    """Frozen limits for one durable run.

    Counters are not stored here.  The PostgreSQL ledger increments an attempt
    before dispatch and never refunds it, including after process restart.
    """

    max_retrieval_rounds: int = 2
    max_queries_per_round: int = 3
    max_tool_attempts: int = 8
    max_model_attempts: int = 4
    max_embedding_attempts: int = 4
    max_retry_per_operation: int = 1
    execution_deadline_seconds: int = 90
    evidence_top_k: int = 5

    def __post_init__(self) -> None:
        limits = {
            "max_retrieval_rounds": (self.max_retrieval_rounds, 1, 32),
            "max_queries_per_round": (self.max_queries_per_round, 1, 32),
            "max_tool_attempts": (self.max_tool_attempts, 1, 1_000),
            "max_model_attempts": (self.max_model_attempts, 1, 1_000),
            "max_embedding_attempts": (self.max_embedding_attempts, 0, 1_000),
            "max_retry_per_operation": (self.max_retry_per_operation, 0, 20),
            "execution_deadline_seconds": (
                self.execution_deadline_seconds,
                1,
                86_400,
            ),
            "evidence_top_k": (self.evidence_top_k, 1, 100),
        }
        for name, (value, minimum, maximum) in limits.items():
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(
                    f"{name} must be an integer between {minimum} and {maximum}"
                )


@dataclass(frozen=True, slots=True)
class RetryDecision:
    error_code: str
    retryable: bool
    status_code: int | None
    cause_type: str


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    run_id: str
    max_retrieval_rounds: int
    max_queries_per_round: int
    max_tool_attempts: int
    max_model_attempts: int
    max_embedding_attempts: int
    max_retry_per_operation: int
    evidence_top_k: int
    retrieval_rounds_used: int
    queries_used: int
    tool_attempts_used: int
    model_attempts_used: int
    embedding_attempts_used: int
    execution_deadline_at: datetime
    revision: int


@dataclass(frozen=True, slots=True)
class AttemptReservation:
    attempt_id: str
    run_id: str
    lease_epoch: int
    operation_key: str
    operation_kind: str
    operation_name: str
    attempt_no: int
    request_hash: str


def retry_decision(error: BaseException) -> RetryDecision:
    """Return the repository-wide, redacted provider retry classification."""

    classification: ProviderErrorClassification = classify_provider_error(error)
    return RetryDecision(
        error_code=classification.error_code,
        retryable=classification.retryable,
        status_code=classification.status_code,
        cause_type=classification.cause_type,
    )


def retry_allowed(
    decision: RetryDecision,
    *,
    attempt_no: int,
    maximum_retries: int,
) -> bool:
    """Apply a bounded retry policy without sleeping or changing a budget."""

    if type(attempt_no) is not int or attempt_no < 1:
        raise ValueError("attempt_no must be a positive integer")
    if type(maximum_retries) is not int or maximum_retries < 0:
        raise ValueError("maximum_retries must be a non-negative integer")
    return decision.retryable and attempt_no <= maximum_retries


__all__ = [
    "AttemptReservation",
    "BudgetExhausted",
    "BudgetSnapshot",
    "ExecutionDeadlineExceeded",
    "HarnessBudgetConfig",
    "RetryDecision",
    "retry_allowed",
    "retry_decision",
]
