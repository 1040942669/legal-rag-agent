from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, TypedDict


HARNESS_STATE_SCHEMA_VERSION = 1
HARNESS_GRAPH_VERSION = "m5-bounded-v1"
HARNESS_NODE_NAMES = frozenset(
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
    }
)
HARNESS_TERMINAL_STATUSES = frozenset(
    {"succeeded", "completed_with_limits", "needs_clarification"}
)

_SHA256 = re.compile(r"[0-9a-f]{64}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,254}")
_FORBIDDEN_STATE_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "client",
        "connection",
        "cookie",
        "credential",
        "database_url",
        "draft",
        "hidden_reasoning",
        "password",
        "prompt",
        "raw_model_output",
        "raw_response",
        "secret",
        "system_prompt",
        "token",
    }
)


class HarnessState(TypedDict):
    run_id: str
    session_id: str
    user_id: str
    schema_version: int
    graph_version: str
    retrieval_config_hash: str
    question: str
    bounded_history_refs: list[str]
    snapshot_id: str
    embedding_profile_id: str
    analysis: dict[str, Any]
    proposed_queries: list[str]
    completed_query_hashes: list[str]
    retrieval_rounds_used: int
    model_attempts_used: int
    tool_attempts_used: int
    embedding_attempts_used: int
    execution_deadline_at: str
    budget_ledger_ref: str
    retrieved_evidence_refs: list[str]
    immutable_evidence_hashes: list[str]
    retrieved_artifact_ref: str | None
    retrieved_artifact_hash: str | None
    answer_draft_ref: None
    verification_result_ref: str | None
    verification_result_hash: str | None
    last_completed_node: str | None
    checkpoint_id: str | None
    stop_reason: str | None
    last_error_code: str | None
    route: str | None
    completion_status: str | None
    next_node: str | None


_STATE_FIELDS = frozenset(HarnessState.__required_keys__)


class HarnessStateError(ValueError):
    """The durable graph state is not safe or compatible."""


def new_harness_state(
    *,
    run_id: str,
    session_id: str,
    user_id: str,
    graph_version: str,
    retrieval_config_hash: str,
    question: str,
    bounded_history_refs: list[str],
    snapshot_id: str,
    embedding_profile_id: str,
    execution_deadline_at: datetime,
) -> HarnessState:
    deadline = _aware_datetime(execution_deadline_at, "execution_deadline_at")
    state: HarnessState = {
        "run_id": run_id,
        "session_id": session_id,
        "user_id": user_id,
        "schema_version": HARNESS_STATE_SCHEMA_VERSION,
        "graph_version": graph_version,
        "retrieval_config_hash": retrieval_config_hash,
        "question": question,
        "bounded_history_refs": list(bounded_history_refs),
        "snapshot_id": snapshot_id,
        "embedding_profile_id": embedding_profile_id,
        "analysis": {},
        "proposed_queries": [],
        "completed_query_hashes": [],
        "retrieval_rounds_used": 0,
        "model_attempts_used": 0,
        "tool_attempts_used": 0,
        "embedding_attempts_used": 0,
        "execution_deadline_at": deadline.astimezone(timezone.utc).isoformat(),
        "budget_ledger_ref": f"run-budget:{run_id}",
        "retrieved_evidence_refs": [],
        "immutable_evidence_hashes": [],
        "retrieved_artifact_ref": None,
        "retrieved_artifact_hash": None,
        "answer_draft_ref": None,
        "verification_result_ref": None,
        "verification_result_hash": None,
        "last_completed_node": None,
        "checkpoint_id": None,
        "stop_reason": None,
        "last_error_code": None,
        "route": None,
        "completion_status": None,
        "next_node": "analyze_query",
    }
    return validate_harness_state(state)


def validate_harness_state(value: Any) -> HarnessState:
    if not isinstance(value, dict) or set(value) != _STATE_FIELDS:
        raise HarnessStateError("harness state fields are invalid")
    _assert_json_tree(value)
    if value["schema_version"] != HARNESS_STATE_SCHEMA_VERSION:
        raise HarnessStateError("harness state schema version is incompatible")
    for name in (
        "run_id",
        "session_id",
        "user_id",
        "graph_version",
        "snapshot_id",
        "embedding_profile_id",
        "budget_ledger_ref",
    ):
        _identifier(value[name], name)
    _sha256(value["retrieval_config_hash"], "retrieval_config_hash")
    question = value["question"]
    if not isinstance(question, str) or not question or question != question.strip():
        raise HarnessStateError("question must be a non-empty normalized string")
    if len(question) > 100_000:
        raise HarnessStateError("question exceeds the state limit")
    for name in (
        "bounded_history_refs",
        "proposed_queries",
        "completed_query_hashes",
        "retrieved_evidence_refs",
        "immutable_evidence_hashes",
    ):
        items = value[name]
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            raise HarnessStateError(f"{name} must be a string list")
        if len(items) > 1_000:
            raise HarnessStateError(f"{name} exceeds the state item limit")
    for digest in [
        *value["completed_query_hashes"],
        *value["immutable_evidence_hashes"],
    ]:
        _sha256(digest, "state digest")
    for name in (
        "retrieval_rounds_used",
        "model_attempts_used",
        "tool_attempts_used",
        "embedding_attempts_used",
    ):
        counter = value[name]
        if type(counter) is not int or counter < 0:
            raise HarnessStateError(f"{name} must be a non-negative integer")
    deadline = _parse_datetime(value["execution_deadline_at"])
    _aware_datetime(deadline, "execution_deadline_at")
    if not isinstance(value["analysis"], dict):
        raise HarnessStateError("analysis must be an object")
    for name in (
        "retrieved_artifact_ref",
        "verification_result_ref",
        "checkpoint_id",
        "stop_reason",
        "last_error_code",
        "route",
        "next_node",
    ):
        item = value[name]
        if item is not None:
            _identifier(item, name)
    for name in ("retrieved_artifact_hash", "verification_result_hash"):
        item = value[name]
        if item is not None:
            _sha256(item, name)
    if value["answer_draft_ref"] is not None:
        raise HarnessStateError("unverified answer drafts cannot enter checkpoints")
    last_node = value["last_completed_node"]
    if last_node is not None and last_node not in HARNESS_NODE_NAMES:
        raise HarnessStateError("last_completed_node is invalid")
    completion_status = value["completion_status"]
    if completion_status is not None and completion_status not in HARNESS_TERMINAL_STATUSES:
        raise HarnessStateError("completion_status is invalid")
    return json.loads(canonical_state_bytes(value).decode("utf-8"))


def canonical_state_bytes(value: Any) -> bytes:
    _assert_json_tree(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def harness_state_hash(value: HarnessState) -> str:
    return hashlib.sha256(canonical_state_bytes(validate_harness_state(value))).hexdigest()


def query_hash(query: str) -> str:
    if not isinstance(query, str) or not query or query != query.strip():
        raise HarnessStateError("query must be a non-empty normalized string")
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def checkpoint_marker() -> str:
    return str(uuid.uuid4())


def _assert_json_tree(value: Any, *, path: str = "state") -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if type(value) is int:
        return
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise HarnessStateError(f"{path} contains a non-finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_json_tree(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise HarnessStateError(f"{path} contains a non-string key")
            if key.casefold() in _FORBIDDEN_STATE_KEYS:
                raise HarnessStateError(f"{path} contains forbidden field {key!r}")
            _assert_json_tree(item, path=f"{path}.{key}")
        return
    raise HarnessStateError(f"{path} contains a non-JSON value")


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise HarnessStateError(f"{name} is not a safe identifier")
    return value


def _sha256(value: Any, name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise HarnessStateError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _parse_datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise HarnessStateError("execution_deadline_at must be an RFC 3339 string")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HarnessStateError(
            "execution_deadline_at must be an RFC 3339 string"
        ) from exc


def _aware_datetime(value: Any, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise HarnessStateError(f"{name} must be timezone-aware")
    return value


__all__ = [
    "HARNESS_GRAPH_VERSION",
    "HARNESS_NODE_NAMES",
    "HARNESS_STATE_SCHEMA_VERSION",
    "HARNESS_TERMINAL_STATUSES",
    "HarnessState",
    "HarnessStateError",
    "canonical_state_bytes",
    "checkpoint_marker",
    "harness_state_hash",
    "new_harness_state",
    "query_hash",
    "validate_harness_state",
]
