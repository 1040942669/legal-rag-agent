from __future__ import annotations

from legal_rag.chat import DEFAULT_ANSWER_MAX_TOKENS, DEFAULT_ENABLE_THINKING
from legal_rag.web import runtime


def test_page_answer_clients_use_the_assistant_generation_defaults(monkeypatch):
    calls = []

    def fake_build(model, **kwargs):
        calls.append((model, kwargs))
        return object()

    monkeypatch.setattr(runtime, "build_completion_client", fake_build)
    factory = runtime.answer_client_factory(
        "siliconflow:example/model",
        {"ollama_base_url": "http://localhost:11434", "request_timeout": 30},
    )

    factory()

    assert calls == [
        (
            "siliconflow:example/model",
            {
                "ollama_base_url": "http://localhost:11434",
                "request_timeout": 30,
                "enable_thinking": DEFAULT_ENABLE_THINKING,
                "max_tokens": DEFAULT_ANSWER_MAX_TOKENS,
            },
        )
    ]
    assert DEFAULT_ENABLE_THINKING is False
