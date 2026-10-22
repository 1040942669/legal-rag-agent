"""Answer generation turns thinking off and caps output, without network calls."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from legal_rag.chat import (
    DEFAULT_ANSWER_MAX_TOKENS,
    DEFAULT_ENABLE_THINKING,
    LegalChatAssistant,
)
from legal_rag.llm import OllamaClient, SiliconFlowClient, build_completion_client


class _Endpoint:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="answer"))],
            usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        )


def _fake_sdk() -> tuple[Any, _Endpoint]:
    endpoint = _Endpoint()
    return SimpleNamespace(chat=SimpleNamespace(completions=endpoint)), endpoint


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_LIVE_MODEL_CALLS", "true")
    monkeypatch.setenv("LEGAL_RAG_DISABLE_DOTENV", "1")


class _NoRetriever:
    name = "fixture"

    def retrieve(self, query: str, top_k: int = 5):
        return []


def test_unset_options_keep_the_provider_request_unchanged() -> None:
    fake, endpoint = _fake_sdk()
    client = SiliconFlowClient(model="fake", _client=fake)

    client.complete("prompt")

    assert set(endpoint.calls[0]) == {"model", "messages", "temperature"}


def test_thinking_switch_and_output_cap_reach_the_request() -> None:
    fake, endpoint = _fake_sdk()
    client = SiliconFlowClient(
        model="fake", _client=fake, enable_thinking=False, max_tokens=512
    )

    assert client.complete("prompt") == "answer"

    request = endpoint.calls[0]
    assert request["extra_body"] == {"enable_thinking": False}
    assert request["max_tokens"] == 512
    assert client.usage.calls == 1


@pytest.mark.parametrize(
    "options",
    [
        {"enable_thinking": "false"},
        {"enable_thinking": 0},
        {"max_tokens": 0},
        {"max_tokens": -1},
        {"max_tokens": True},
        {"max_tokens": 1.5},
    ],
)
def test_invalid_generation_options_are_rejected(options: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        SiliconFlowClient(model="fake", **options)


def test_factory_passes_options_to_siliconflow_only() -> None:
    siliconflow = build_completion_client(
        "siliconflow:vendor/model", enable_thinking=False, max_tokens=256
    )
    ollama = build_completion_client(
        "qwen2.5:7b", enable_thinking=False, max_tokens=256
    )

    assert isinstance(siliconflow, SiliconFlowClient)
    assert siliconflow.model == "vendor/model"
    assert siliconflow.enable_thinking is False
    assert siliconflow.max_tokens == 256
    assert isinstance(ollama, OllamaClient)


def test_assistant_answers_with_thinking_off_and_capped_output_by_default() -> None:
    assistant = LegalChatAssistant(_NoRetriever(), model="siliconflow:vendor/model")

    assert DEFAULT_ENABLE_THINKING is False
    assert assistant.llm.enable_thinking is False
    assert assistant.llm.max_tokens == DEFAULT_ANSWER_MAX_TOKENS
    assert assistant.llm.request_options() == {
        "max_tokens": DEFAULT_ANSWER_MAX_TOKENS,
        "extra_body": {"enable_thinking": False},
    }


def test_assistant_can_restore_provider_defaults() -> None:
    assistant = LegalChatAssistant(
        _NoRetriever(),
        model="siliconflow:vendor/model",
        enable_thinking=None,
        max_tokens=None,
    )

    assert assistant.llm.request_options() == {}
