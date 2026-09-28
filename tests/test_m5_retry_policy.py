from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from legal_rag.harness.budget import (
    HarnessBudgetConfig,
    RetryDecision,
    retry_allowed,
    retry_decision,
)


class StatusError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"synthetic HTTP {status_code}")


def test_harness_budget_defaults_match_the_bounded_m5_contract() -> None:
    budget = HarnessBudgetConfig()

    assert budget.max_retrieval_rounds == 2
    assert budget.max_queries_per_round == 3
    assert budget.max_tool_attempts == 8
    assert budget.max_model_attempts == 4
    assert budget.max_embedding_attempts == 4
    assert budget.max_retry_per_operation == 1
    assert budget.execution_deadline_seconds == 90
    assert budget.evidence_top_k == 5

    with pytest.raises(FrozenInstanceError):
        budget.max_model_attempts = 5  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_retrieval_rounds", 0),
        ("max_queries_per_round", 33),
        ("max_tool_attempts", True),
        ("max_model_attempts", 1_001),
        ("max_embedding_attempts", -1),
        ("max_retry_per_operation", 21),
        ("execution_deadline_seconds", 0),
        ("execution_deadline_seconds", 86_401),
        ("evidence_top_k", 101),
    ],
)
def test_harness_budget_rejects_invalid_or_unbounded_limits(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError, match=field):
        HarnessBudgetConfig(**{field: value})  # type: ignore[arg-type]


def test_harness_budget_accepts_zero_embedding_and_retry_limits() -> None:
    budget = HarnessBudgetConfig(
        max_embedding_attempts=0,
        max_retry_per_operation=0,
    )

    assert budget.max_embedding_attempts == 0
    assert budget.max_retry_per_operation == 0


def test_m5_t05_retryable_429_and_timeout_retry_once_and_consume_attempts() -> None:
    rate_limited = retry_decision(StatusError(429))
    timed_out = retry_decision(TimeoutError("private provider detail"))

    assert rate_limited == RetryDecision(
        error_code="rate_limited",
        retryable=True,
        status_code=429,
        cause_type="StatusError",
    )
    assert timed_out.error_code == "timeout"
    assert timed_out.retryable is True
    assert timed_out.status_code is None

    for decision in (rate_limited, timed_out):
        assert retry_allowed(decision, attempt_no=1, maximum_retries=1) is True
        assert retry_allowed(decision, attempt_no=2, maximum_retries=1) is False


def test_m5_t05_nonretryable_400_and_401_do_not_retry() -> None:
    invalid_request = retry_decision(StatusError(400))
    unauthorized = retry_decision(StatusError(401))

    assert invalid_request.error_code == "invalid_request"
    assert invalid_request.retryable is False
    assert invalid_request.status_code == 400
    assert unauthorized.error_code == "auth"
    assert unauthorized.retryable is False
    assert unauthorized.status_code == 401
    assert retry_allowed(invalid_request, attempt_no=1, maximum_retries=1) is False
    assert retry_allowed(unauthorized, attempt_no=1, maximum_retries=1) is False


@pytest.mark.parametrize(
    ("attempt_no", "maximum_retries"),
    [(0, 1), (True, 1), (1, -1), (1, True)],
)
def test_retry_policy_rejects_invalid_attempt_or_retry_counters(
    attempt_no: object,
    maximum_retries: object,
) -> None:
    decision = RetryDecision(
        error_code="timeout",
        retryable=True,
        status_code=None,
        cause_type="TimeoutError",
    )

    with pytest.raises(ValueError):
        retry_allowed(
            decision,
            attempt_no=attempt_no,  # type: ignore[arg-type]
            maximum_retries=maximum_retries,  # type: ignore[arg-type]
        )
