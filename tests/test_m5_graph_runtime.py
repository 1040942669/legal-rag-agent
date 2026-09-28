from __future__ import annotations

from typing import Any

import pytest
from langgraph.checkpoint.postgres import PostgresSaver

from legal_rag.harness.checkpoint import checkpoint_thread_id
from legal_rag.harness.graph import build_bounded_graph
from legal_rag.harness.state import HARNESS_NODE_NAMES, HarnessState


class _CompileOnlyNodes:
    def __getattr__(self, name: str):
        if name not in HARNESS_NODE_NAMES:
            raise AttributeError(name)

        def callback(state: HarnessState) -> HarnessState:
            return state

        return callback


def test_m5_graph_compiles_with_application_checkpoint_id_inside_envelope() -> None:
    graph: Any = build_bounded_graph(
        checkpointer=PostgresSaver(None),
        nodes=_CompileOnlyNodes(),
    )

    assert graph.name == "legal-rag-m5-bounded"
    assert "payload" in graph.channels
    assert "checkpoint_id" not in graph.channels


def test_m5_checkpoint_thread_id_is_lease_namespace_isolated() -> None:
    first = checkpoint_thread_id(
        run_id="run-1",
        namespace="m5:m5-bounded-v1:1:lease-1",
    )
    second = checkpoint_thread_id(
        run_id="run-1",
        namespace="m5:m5-bounded-v1:1:lease-2",
    )

    assert first != second
    assert first.startswith("run-1:")
    with pytest.raises(ValueError, match="run_id"):
        checkpoint_thread_id(run_id="", namespace="lease-1")
    with pytest.raises(ValueError, match="namespace"):
        checkpoint_thread_id(run_id="run-1", namespace="")
