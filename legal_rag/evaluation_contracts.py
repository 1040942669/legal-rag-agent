from __future__ import annotations

from .models import ANSWER_MODES, EvalCase


def validate_eval_case(case: EvalCase) -> None:
    """Validate one case without requiring it to start a session sequence."""

    if not isinstance(case, EvalCase):
        raise ValueError("evaluation case must be an EvalCase")
    if not isinstance(case.case_id, str) or not case.case_id.strip():
        raise ValueError(
            f"evaluation case id must be non-empty and unique: {case.case_id!r}"
        )
    if not isinstance(case.question, str) or not case.question.strip():
        raise ValueError(f"question must be a non-empty string for {case.case_id}")
    if not isinstance(case.case_type, str) or not case.case_type.strip():
        raise ValueError(f"case_type must be a non-empty string for {case.case_id}")
    if not isinstance(case.expected_law, str):
        raise ValueError(f"expected_law must be a string for {case.case_id}")
    for name, values in (
        ("expected_articles", case.expected_articles),
        ("keywords", case.keywords),
    ):
        if not isinstance(values, list) or not all(
            isinstance(value, str) for value in values
        ):
            raise ValueError(f"{name} must be a list of strings for {case.case_id}")
    behavior = case.resolved_expected_behavior
    if not isinstance(behavior, str) or behavior not in ANSWER_MODES:
        raise ValueError(f"unknown expected_behavior for {case.case_id}: {behavior!r}")
    if isinstance(case.turn_index, bool) or not isinstance(case.turn_index, int):
        raise ValueError(f"turn_index must be an integer for {case.case_id}")
    if case.turn_index < 0:
        raise ValueError(f"turn_index must be non-negative for {case.case_id}")
    if isinstance(case.schema_version, bool) or not isinstance(
        case.schema_version, int
    ):
        raise ValueError(f"schema_version must be an integer for {case.case_id}")
    if case.schema_version < 1:
        raise ValueError(f"schema_version must be at least 1 for {case.case_id}")
    group = case.session_group
    if group is None:
        if case.turn_index != 0:
            raise ValueError(f"single-turn case {case.case_id} must use turn_index=0")
    elif not isinstance(group, str) or not group.strip():
        raise ValueError(f"session_group must be a non-empty string for {case.case_id}")
