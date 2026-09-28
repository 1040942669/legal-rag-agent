from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver

from legal_rag.harness.checkpoint import (
    PersistentCheckpointerRequired,
    require_persistent_checkpointer,
)


def test_m5_persistent_recovery_rejects_in_memory_checkpointer() -> None:
    with pytest.raises(
        PersistentCheckpointerRequired,
        match="cannot satisfy cross-process recovery",
    ):
        require_persistent_checkpointer(InMemorySaver())

    with pytest.raises(
        PersistentCheckpointerRequired,
        match="requires the PostgreSQL LangGraph saver",
    ):
        require_persistent_checkpointer(object())

    require_persistent_checkpointer(PostgresSaver(None))
