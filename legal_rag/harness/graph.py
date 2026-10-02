from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph

from .checkpoint import require_persistent_checkpointer
from .state import HARNESS_NODE_NAMES, HarnessState, validate_harness_state


class HarnessGraphError(RuntimeError):
    """The graph attempted an invalid or unbounded transition."""


class _GraphEnvelope(TypedDict):
    """Keep application state out of LangGraph's reserved channel namespace."""

    payload: HarnessState


class HarnessNodes(Protocol):
    def analyze_query(self, state: HarnessState) -> HarnessState: ...

    def route(self, state: HarnessState) -> HarnessState: ...

    def retrieve(self, state: HarnessState) -> HarnessState: ...

    def merge_evidence(self, state: HarnessState) -> HarnessState: ...

    def check_evidence(self, state: HarnessState) -> HarnessState: ...

    def plan_followup(self, state: HarnessState) -> HarnessState: ...

    def generate(self, state: HarnessState) -> HarnessState: ...

    def verify(self, state: HarnessState) -> HarnessState: ...

    def persist_result(self, state: HarnessState) -> HarnessState: ...


_SUCCESSORS: dict[str | None, frozenset[str | None]] = {
    None: frozenset({"analyze_query"}),
    "analyze_query": frozenset({"route"}),
    "route": frozenset({"retrieve"}),
    "retrieve": frozenset({"merge_evidence"}),
    "merge_evidence": frozenset({"check_evidence"}),
    "check_evidence": frozenset({"plan_followup", "generate"}),
    "plan_followup": frozenset({"retrieve", "generate"}),
    "generate": frozenset({"verify"}),
    "verify": frozenset({"persist_result"}),
    "persist_result": frozenset({None}),
}


def build_bounded_graph(
    *,
    checkpointer: Any,
    nodes: HarnessNodes,
    node_wrapper: Callable[[str, Callable], Callable] | None = None,
):
    """Compile the single controller for M5 retrieval and generation.

    Resumed executions seed a new lease-epoch namespace with their last trusted
    JSON state.  The conditional entry point continues at ``next_node`` rather
    than replaying already completed retrieval.
    """

    require_persistent_checkpointer(checkpointer)
    builder = StateGraph(_GraphEnvelope)
    for name in sorted(HARNESS_NODE_NAMES):
        callback = getattr(nodes, name, None)
        if not callable(callback):
            raise TypeError(f"harness node {name!r} is not callable")
        validated = _validated_node(name, callback)
        builder.add_node(
            name, node_wrapper(name, validated) if node_wrapper else validated
        )

    entry_mapping = {name: name for name in HARNESS_NODE_NAMES}
    entry_mapping["__end__"] = END
    builder.add_conditional_edges(START, _next_node, entry_mapping)
    builder.add_edge("analyze_query", "route")
    builder.add_edge("route", "retrieve")
    builder.add_edge("retrieve", "merge_evidence")
    builder.add_edge("merge_evidence", "check_evidence")
    builder.add_conditional_edges(
        "check_evidence",
        _next_node,
        {"plan_followup": "plan_followup", "generate": "generate"},
    )
    builder.add_conditional_edges(
        "plan_followup",
        _next_node,
        {"retrieve": "retrieve", "generate": "generate"},
    )
    builder.add_edge("generate", "verify")
    builder.add_edge("verify", "persist_result")
    builder.add_edge("persist_result", END)
    return builder.compile(checkpointer=checkpointer, name="legal-rag-m5-bounded")


def _validated_node(
    name: str,
    callback: Callable[[HarnessState], HarnessState],
) -> Callable[[_GraphEnvelope], _GraphEnvelope]:
    def invoke(envelope: _GraphEnvelope) -> _GraphEnvelope:
        before = validate_harness_state(dict(envelope["payload"]))
        result = callback(before)
        after = validate_harness_state(result)
        if after["run_id"] != before["run_id"]:
            raise HarnessGraphError("a node changed run_id")
        if after["graph_version"] != before["graph_version"]:
            raise HarnessGraphError("a node changed graph_version")
        if after["retrieval_config_hash"] != before["retrieval_config_hash"]:
            raise HarnessGraphError("a node changed retrieval_config_hash")
        if after["execution_deadline_at"] != before["execution_deadline_at"]:
            raise HarnessGraphError("a node reset the absolute deadline")
        if after["last_completed_node"] != name:
            raise HarnessGraphError(f"node {name!r} did not mark its completion")
        expected = _SUCCESSORS[name]
        if after["next_node"] not in expected:
            raise HarnessGraphError(
                f"node {name!r} selected invalid successor {after['next_node']!r}"
            )
        return {"payload": after}

    return invoke


def _next_node(envelope: _GraphEnvelope) -> str:
    resolved = validate_harness_state(dict(envelope["payload"]))
    next_node = resolved["next_node"]
    if next_node is None:
        if resolved["last_completed_node"] != "persist_result":
            raise HarnessGraphError("only a persisted result can terminate the graph")
        return "__end__"
    if next_node not in HARNESS_NODE_NAMES:
        raise HarnessGraphError("next_node is invalid")
    expected = _SUCCESSORS[resolved["last_completed_node"]]
    if next_node not in expected:
        raise HarnessGraphError("checkpoint successor disagrees with completed node")
    return next_node


__all__ = ["HarnessGraphError", "HarnessNodes", "build_bounded_graph"]
