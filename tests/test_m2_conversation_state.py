from __future__ import annotations

from copy import deepcopy

import pytest

from legal_rag.chat import ConversationMemory, LegalChatAssistant


class _RecordingEmptyRetriever:
    name = "recording-empty"

    def __init__(self) -> None:
        self.queries: list[str] = []

    def retrieve(self, query: str, top_k: int = 5):
        self.queries.append(query)
        return []


def test_conversation_memory_state_is_strict_and_defensively_copied() -> None:
    memory = ConversationMemory(token_limit=2000)
    memory.add("user", "第一轮问题")
    memory.add("assistant", "第一轮回答")

    state = memory.export_state()
    state["messages"][0]["content"] = "caller mutation"

    assert memory.export_state() == {
        "schema_version": 1,
        "token_limit": 2000,
        "messages": [
            {"role": "user", "content": "第一轮问题"},
            {"role": "assistant", "content": "第一轮回答"},
        ],
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda state: state.update({"unexpected": True}),
        lambda state: state.update({"schema_version": True}),
        lambda state: state.update({"schema_version": 2}),
        lambda state: state.update({"token_limit": 1000}),
        lambda state: state.update({"messages": "not-a-list"}),
        lambda state: state.update(
            {"messages": [{"role": "assistant", "content": "wrong order"}]}
        ),
        lambda state: state["messages"].pop(),
        lambda state: state["messages"].append(
            {"role": "system", "content": "not allowed"}
        ),
        lambda state: state["messages"].append({"role": "user", "content": "\ud800"}),
        lambda state: state["messages"].append(
            {"role": "user", "content": "x", "extra": "field"}
        ),
    ],
)
def test_invalid_conversation_state_is_rejected_without_mutation(mutate) -> None:
    memory = ConversationMemory(token_limit=2000)
    memory.add("user", "original")
    memory.add("assistant", "answer")
    before = memory.export_state()
    candidate = deepcopy(before)
    mutate(candidate)

    with pytest.raises(ValueError):
        memory.restore_state(candidate)

    assert memory.export_state() == before


def test_conversation_state_rejects_unbounded_or_over_budget_history() -> None:
    memory = ConversationMemory(token_limit=20)
    base = memory.export_state()

    too_many = deepcopy(base)
    too_many["messages"] = [
        {"role": "user", "content": str(index)} for index in range(9)
    ]
    with pytest.raises(ValueError, match="at most 8"):
        memory.restore_state(too_many)

    over_budget = deepcopy(base)
    over_budget["messages"] = [
        {"role": "user", "content": "很长的合成内容" * 20},
        {"role": "assistant", "content": "很长的合成回答" * 20},
    ]
    with pytest.raises(ValueError, match="token budget"):
        memory.restore_state(over_budget)

    assert memory.export_state() == base


def test_real_assistant_resume_matches_uninterrupted_followup() -> None:
    first_question = "合成规则第一条怎么理解？"
    followup = "这个还有什么限制？"

    uninterrupted_retriever = _RecordingEmptyRetriever()
    uninterrupted = LegalChatAssistant(
        uninterrupted_retriever,
        model="deterministic-offline-fixture",
    )
    uninterrupted.answer(first_question)
    checkpoint = uninterrupted.export_session_state()
    checkpoint_query_count = len(uninterrupted_retriever.queries)
    uninterrupted_answer, uninterrupted_results = uninterrupted.answer(followup)

    resumed_retriever = _RecordingEmptyRetriever()
    resumed = LegalChatAssistant(
        resumed_retriever,
        model="deterministic-offline-fixture",
    )
    resumed.last_generation_error = "stale-observation"
    resumed.restore_session_state(checkpoint)
    assert resumed.last_generation_error is None
    resumed_answer, resumed_results = resumed.answer(followup)

    assert (
        resumed_retriever.queries
        == uninterrupted_retriever.queries[checkpoint_query_count:]
    )
    assert resumed_answer == uninterrupted_answer
    assert resumed_results == uninterrupted_results
    assert resumed.export_session_state() == uninterrupted.export_session_state()
    assert checkpoint == {
        "schema_version": 1,
        "memory": {
            "schema_version": 1,
            "token_limit": 2000,
            "messages": checkpoint["memory"]["messages"],
        },
    }


def test_invalid_assistant_checkpoint_does_not_replace_live_memory() -> None:
    assistant = LegalChatAssistant(
        _RecordingEmptyRetriever(),
        model="deterministic-offline-fixture",
    )
    assistant.memory.add("user", "keep me")
    assistant.memory.add("assistant", "keep answer")
    before = assistant.export_session_state()
    invalid = deepcopy(before)
    invalid["memory"]["token_limit"] = 1

    with pytest.raises(ValueError):
        assistant.restore_session_state(invalid)

    assert assistant.export_session_state() == before
