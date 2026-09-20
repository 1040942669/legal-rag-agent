from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from legal_rag.experiment_runner import (
    OBSERVATION_STAGES,
    RUNNER_RESULT_SCHEMA_VERSION,
    RunnerContractError,
    StageObservation,
    WorkUnit,
    validate_persisted_attempt_history,
    validate_persisted_runner_attempt,
    validate_persisted_work_unit_history,
)
from legal_rag.experiment_runtime import EXTERNAL_CALL_KINDS, canonical_hash


def _case(
    case_id: str,
    ordinal: int,
    *,
    session_group: str | None = None,
    turn_index: int = 0,
) -> dict[str, Any]:
    case = {
        "case_id": case_id,
        "case_hash": canonical_hash(
            {
                "case_id": case_id,
                "session_group": session_group,
                "turn_index": turn_index,
            }
        ),
        "ordinal": ordinal,
        "turn_index": turn_index,
    }
    if session_group is not None:
        case["session_group"] = session_group
    return case


def _unit(*cases: dict[str, Any], session_group: str | None = None) -> WorkUnit:
    identity = {
        "session_group": session_group,
        "cases": [
            {
                "ordinal": case["ordinal"],
                "case_id": case["case_id"],
                "case_hash": case["case_hash"],
                "turn_index": case["turn_index"],
            }
            for case in cases
        ],
    }
    work_unit_id = canonical_hash(identity)
    return WorkUnit(
        work_unit_id=work_unit_id,
        session_group=session_group,
        group_identity_hash=work_unit_id,
        first_ordinal=cases[0]["ordinal"],
        cases=tuple(cases),
    )


def _zero_call_ledger() -> dict[str, Any]:
    return {
        "actual": {
            kind: {
                "attempted": 0,
                "succeeded": 0,
                "failed": 0,
                "duration_ms": 0.0,
                "provider_wait_ms": 0.0,
            }
            for kind in EXTERNAL_CALL_KINDS
        },
        "source": {kind: 0 for kind in EXTERNAL_CALL_KINDS},
    }


def _zero_model_usage() -> dict[str, Any]:
    return {
        role: {
            "calls": 0,
            "failed_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "token_usage_calls": 0,
            "latency_ms": 0.0,
        }
        for role in ("assistant", "normalizer", "judge")
    }


def _attempt(
    *,
    unit: WorkUnit,
    case: dict[str, Any],
    attempt_number: int,
    status: str,
    state_before: dict[str, Any] | None = None,
    state_after: dict[str, Any] | None = None,
    retryable: bool = True,
) -> dict[str, Any]:
    if unit.session_group is None:
        checkpoint = None
    else:
        case_index = unit.case_ids.index(case["case_id"])
        checkpoint = {
            "previous_case_id": (
                unit.case_ids[case_index - 1] if case_index > 0 else None
            ),
            "state_before_sha256": canonical_hash(state_before),
            "state_after": state_after if status == "succeeded" else None,
            "state_after_sha256": (
                canonical_hash(state_after) if status == "succeeded" else None
            ),
        }
    result = {
        "runner_schema_version": RUNNER_RESULT_SCHEMA_VERSION,
        "case": {
            "case_id": case["case_id"],
            "case_hash": case["case_hash"],
            "ordinal": case["ordinal"],
            "session_group": unit.session_group,
            "turn_index": case["turn_index"],
        },
        "work_unit": {
            "work_unit_id": unit.work_unit_id,
            "group_identity_hash": unit.group_identity_hash,
        },
        "session_checkpoint": checkpoint,
        "output": (
            {"case_id": case["case_id"], "attempt": attempt_number}
            if status == "succeeded"
            else None
        ),
        "stage_observations": {
            stage: StageObservation.not_run("fixture_not_run").to_dict()
            for stage in OBSERVATION_STAGES
        },
        "call_ledger": _zero_call_ledger(),
        "model_usage": _zero_model_usage(),
        "timings_ms": {
            "queue_wait": 0.0,
            "end_to_end": 1.0,
            "persistence_included": False,
        },
        "error": (
            None
            if status == "succeeded"
            else {"code": "fixture_failure", "retryable": retryable}
        ),
    }
    return {
        "case_id": case["case_id"],
        "case_hash": case["case_hash"],
        "ordinal": case["ordinal"],
        "attempt": attempt_number,
        "status": status,
        "cache_mode": "fresh",
        "result": result,
    }


def test_public_attempt_validator_returns_defensive_frozen_view() -> None:
    case = _case("single", 0)
    unit = _unit(case)
    payload = _attempt(
        unit=unit,
        case=case,
        attempt_number=1,
        status="succeeded",
    )
    original = deepcopy(payload)

    view = validate_persisted_runner_attempt(
        payload,
        case=case,
        unit=unit,
        state_before=None,
    )

    assert payload == original
    assert view.case_id == "single"
    assert view.status == "succeeded"
    assert view.output == {"case_id": "single", "attempt": 1}
    assert view.session_checkpoint is None
    assert view.retryable is None
    with pytest.raises(FrozenInstanceError):
        view.status = "failed"  # type: ignore[misc]
    payload["result"]["output"]["case_id"] = "mutated-after-validation"
    assert view.output == {"case_id": "single", "attempt": 1}


def test_public_attempt_history_validates_retry_order_and_numbers() -> None:
    case = _case("retry", 0)
    unit = _unit(case)
    failed = _attempt(
        unit=unit,
        case=case,
        attempt_number=1,
        status="failed",
        retryable=True,
    )
    succeeded = _attempt(
        unit=unit,
        case=case,
        attempt_number=2,
        status="succeeded",
    )

    history = validate_persisted_attempt_history(
        (failed, succeeded),
        case=case,
        unit=unit,
        state_before=None,
    )

    assert [attempt.status for attempt in history.attempts] == [
        "failed",
        "succeeded",
    ]
    assert history.latest_status == "succeeded"
    assert history.latest_retryable is None

    nonretryable = deepcopy(failed)
    nonretryable["result"]["error"]["retryable"] = False
    with pytest.raises(
        RunnerContractError,
        match="non-retryable attempt cannot be followed",
    ):
        validate_persisted_attempt_history(
            (nonretryable, succeeded),
            case=case,
            unit=unit,
            state_before=None,
        )

    wrong_number = deepcopy(succeeded)
    wrong_number["attempt"] = 3
    with pytest.raises(RunnerContractError, match="not contiguous"):
        validate_persisted_attempt_history(
            (failed, wrong_number),
            case=case,
            unit=unit,
            state_before=None,
        )


def test_public_work_unit_validator_proves_session_checkpoint_chain() -> None:
    first = _case("g-0", 0, session_group="group", turn_index=0)
    second = _case("g-1", 1, session_group="group", turn_index=1)
    third = _case("g-2", 2, session_group="group", turn_index=2)
    unit = _unit(first, second, third, session_group="group")
    first_state = {"history": ["g-0"]}
    second_state = {"history": ["g-0", "g-1"]}
    first_attempt = _attempt(
        unit=unit,
        case=first,
        attempt_number=1,
        status="succeeded",
        state_before=None,
        state_after=first_state,
    )
    second_attempt = _attempt(
        unit=unit,
        case=second,
        attempt_number=1,
        status="succeeded",
        state_before=first_state,
        state_after=second_state,
    )
    attempts = {"g-0": (first_attempt,), "g-1": (second_attempt,)}
    original = deepcopy(attempts)

    history = validate_persisted_work_unit_history(unit, attempts)

    assert attempts == original
    assert [item.case_id for item in history.case_histories] == ["g-0", "g-1"]
    assert history.completed_prefix_length == 2
    assert history.state_after == second_state
    assert history.complete is False

    broken = deepcopy(attempts)
    broken["g-1"][0]["result"]["session_checkpoint"]["state_before_sha256"] = (
        canonical_hash({"history": ["wrong"]})
    )
    with pytest.raises(RunnerContractError, match="state chain is broken"):
        validate_persisted_work_unit_history(unit, broken)


def test_public_work_unit_validator_rejects_attempts_after_session_gap() -> None:
    first = _case("g-0", 0, session_group="group", turn_index=0)
    second = _case("g-1", 1, session_group="group", turn_index=1)
    third = _case("g-2", 2, session_group="group", turn_index=2)
    unit = _unit(first, second, third, session_group="group")
    third_attempt = _attempt(
        unit=unit,
        case=third,
        attempt_number=1,
        status="succeeded",
        state_before=None,
        state_after={"history": ["g-2"]},
    )

    with pytest.raises(
        RunnerContractError,
        match="later session turns cannot have attempts",
    ):
        validate_persisted_work_unit_history(unit, {"g-2": (third_attempt,)})
