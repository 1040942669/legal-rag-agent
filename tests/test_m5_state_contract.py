from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from legal_rag.harness.state import (
    HARNESS_GRAPH_VERSION,
    HARNESS_STATE_SCHEMA_VERSION,
    HarnessStateError,
    canonical_state_bytes,
    harness_state_hash,
    new_harness_state,
    query_hash,
    validate_harness_state,
)


def _state(*, deadline: datetime | None = None):
    return new_harness_state(
        run_id="run-m5-state",
        session_id="session-m5-state",
        user_id="user-m5-state",
        graph_version=HARNESS_GRAPH_VERSION,
        retrieval_config_hash="a" * 64,
        question="中华人民共和国民法典如何规定合同履行？",
        bounded_history_refs=["message-1", "message-2"],
        snapshot_id="snapshot-m5-state",
        embedding_profile_id="profile-m5-state",
        execution_deadline_at=deadline
        or datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc),
    )


def test_harness_state_is_strict_json_and_preserves_absolute_deadline() -> None:
    deadline = datetime(
        2026,
        9,
        29,
        12,
        30,
        15,
        tzinfo=timezone(timedelta(hours=8)),
    )

    state = _state(deadline=deadline)
    encoded = canonical_state_bytes(state)
    restored = validate_harness_state(json.loads(encoded.decode("utf-8")))

    assert restored["schema_version"] == HARNESS_STATE_SCHEMA_VERSION
    assert restored["execution_deadline_at"] == "2026-09-29T04:30:15+00:00"
    assert restored["execution_deadline_at"] == state["execution_deadline_at"]
    assert restored["answer_draft_ref"] is None
    assert restored["next_node"] == "analyze_query"


def test_harness_state_hash_is_canonical_and_validation_returns_a_deep_copy() -> None:
    first = _state()
    first["analysis"] = {"route": "search", "signals": {"b": 2, "a": 1}}
    second = deepcopy(first)
    second["analysis"] = {"signals": {"a": 1, "b": 2}, "route": "search"}

    validated = validate_harness_state(first)

    assert harness_state_hash(first) == harness_state_hash(second)
    assert validated == first
    assert validated is not first
    assert validated["analysis"] is not first["analysis"]


@pytest.mark.parametrize(
    "forbidden_key",
    ["api_key", "connection", "draft", "raw_model_output", "system_prompt"],
)
def test_harness_state_rejects_nested_forbidden_fields(forbidden_key: str) -> None:
    state = _state()
    state["analysis"] = {"safe": {forbidden_key: "must-not-persist"}}

    with pytest.raises(HarnessStateError, match="forbidden field"):
        validate_harness_state(state)


def test_harness_state_rejects_unverified_draft_reference() -> None:
    state = _state()
    state["answer_draft_ref"] = "private-draft-1"  # type: ignore[typeddict-item]

    with pytest.raises(
        HarnessStateError,
        match="unverified answer drafts cannot enter checkpoints",
    ):
        validate_harness_state(state)


def test_harness_state_rejects_a_real_connection_object() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        state = _state()
        state["analysis"] = {"resource": connection}

        with pytest.raises(HarnessStateError, match="non-JSON value"):
            validate_harness_state(state)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "invalid_value",
    [b"bytes", {"set-value"}, float("nan"), float("inf")],
)
def test_harness_state_rejects_non_json_or_non_finite_values(
    invalid_value: object,
) -> None:
    state = _state()
    state["analysis"] = {"value": invalid_value}

    with pytest.raises(HarnessStateError):
        validate_harness_state(state)


def test_harness_state_rejects_unknown_or_missing_fields() -> None:
    extra = dict(_state())
    extra["unexpected"] = True
    missing = dict(_state())
    del missing["budget_ledger_ref"]

    with pytest.raises(HarnessStateError, match="fields are invalid"):
        validate_harness_state(extra)
    with pytest.raises(HarnessStateError, match="fields are invalid"):
        validate_harness_state(missing)


def test_harness_state_requires_timezone_aware_absolute_deadline() -> None:
    with pytest.raises(HarnessStateError, match="timezone-aware"):
        _state(deadline=datetime(2026, 9, 29, 12, 0))

    state = _state()
    state["execution_deadline_at"] = "2026-09-29T12:00:00"
    with pytest.raises(HarnessStateError, match="timezone-aware"):
        validate_harness_state(state)


def test_query_hash_requires_normalized_text_and_is_deterministic() -> None:
    assert query_hash("合同履行") == query_hash("合同履行")
    assert len(query_hash("合同履行")) == 64

    for invalid in ("", " 合同履行", "合同履行 "):
        with pytest.raises(HarnessStateError, match="normalized"):
            query_hash(invalid)
