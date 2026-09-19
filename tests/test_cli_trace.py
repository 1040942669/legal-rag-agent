from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from legal_rag import cli


class TraceValue:
    def __init__(self, **payload: Any) -> None:
        self.payload = payload

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


class FakeChatAssistant:
    def __init__(self, *, rejected: bool) -> None:
        self.calls: list[bool] = []
        self.last_adaptive_result = None
        self.last_evidence_check = None
        self.last_generation_error = None
        self.last_structured_answer = SimpleNamespace(
            answer_mode="insufficient_evidence",
            adapter_source="programmatic",
        )
        self.last_verification = TraceValue(
            passed=True,
            actual_answer_mode="insufficient_evidence",
        )
        if rejected:
            self.last_pre_fallback_answer = SimpleNamespace(answer_mode="evidence_answer")
            self.last_pre_fallback_verification = TraceValue(
                passed=False,
                actual_answer_mode="evidence_answer",
                citation_ids_valid=False,
            )
        else:
            self.last_pre_fallback_answer = None
            self.last_pre_fallback_verification = None

    def answer(self, question: str, *, generate: bool = True):
        self.calls.append(generate)
        return "最终交付的安全回答", []


def run_chat_once(
    monkeypatch,
    tmp_path: Path,
    *,
    assistant: FakeChatAssistant,
    no_generate: bool,
) -> dict[str, Any]:
    config = {
        "chunking": {"default_strategy": "article"},
        "retrieval": {"default": "bm25", "top_k": 5},
        "models": {"default": "offline-test-model"},
        "chat": {
            "memory_token_limit": 512,
            "ollama_base_url": "http://127.0.0.1:11434",
            "request_timeout": 1,
        },
        "adaptive": {},
    }
    trace_path = tmp_path / "chat-trace.jsonl"
    args = SimpleNamespace(
        config="unused.yaml",
        chunk_strategy=None,
        index_dir=None,
        retriever=None,
        top_k=None,
        embedding=None,
        embedding_cache_dir=None,
        reranker=None,
        rerank_top_n=None,
        model=None,
        trace_path=str(trace_path),
        no_generate=no_generate,
        adaptive=False,
        adaptive_use_llm=False,
        adaptive_max_queries=None,
        adaptive_per_plan_top_k=None,
        normalizer_retries=None,
        condense_llm=False,
    )
    inputs = iter(["合成问题", "exit"])

    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "resolve_index_dir", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(cli, "resolve_chunks_path", lambda path: tmp_path / "chunks.jsonl")
    monkeypatch.setattr(cli, "load_chunks", lambda path: [])
    monkeypatch.setattr(cli, "create_retriever", lambda *args, **kwargs: SimpleNamespace(name="fake"))
    monkeypatch.setattr(cli, "LegalChatAssistant", lambda *args, **kwargs: assistant)
    monkeypatch.setattr(cli, "new_run_id", lambda prefix: "chat-trace-test")
    monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))

    assert cli.handle_chat(args) == 0
    records = trace_path.read_text(encoding="utf-8").splitlines()
    assert len(records) == 1
    return json.loads(records[0])


def test_interactive_chat_trace_separates_rejected_attempt_from_delivered_final(
    monkeypatch,
    tmp_path: Path,
) -> None:
    assistant = FakeChatAssistant(rejected=True)

    trace = run_chat_once(
        monkeypatch,
        tmp_path,
        assistant=assistant,
        no_generate=False,
    )

    assert assistant.calls == [True]
    assert trace["generation_attempt"] == {
        "attempted": True,
        "status": "rejected",
        "reason": "verification_failed",
        "answer_mode": "evidence_answer",
        "verification": {
            "passed": False,
            "actual_answer_mode": "evidence_answer",
            "citation_ids_valid": False,
        },
    }
    assert trace["final_response"] == {
        "value": {
            "answer_mode": "insufficient_evidence",
            "verification": {
                "passed": True,
                "actual_answer_mode": "insufficient_evidence",
            },
        },
        "unavailable_reason": None,
    }
    assert trace["verifier"] == trace["final_response"]["value"]["verification"]
    assert trace["execution"] == {
        "service": {"status": "succeeded", "reason": None},
        "generation": {"status": "succeeded", "reason": None},
        "verification": {"status": "succeeded", "reason": None},
        "judge": {"status": "not_run", "reason": "judge_not_configured"},
    }


def test_interactive_retrieval_only_trace_does_not_fabricate_generation_or_verification(
    monkeypatch,
    tmp_path: Path,
) -> None:
    # Populate rejected/final state deliberately: retrieval-only trace semantics
    # must be driven by the requested mode rather than stale assistant fields.
    assistant = FakeChatAssistant(rejected=True)

    trace = run_chat_once(
        monkeypatch,
        tmp_path,
        assistant=assistant,
        no_generate=True,
    )

    assert assistant.calls == [False]
    assert trace["generation_attempt"] == {
        "attempted": False,
        "reason": "retrieval_only",
    }
    assert trace["final_response"] == {
        "value": None,
        "unavailable_reason": "retrieval_only",
    }
    assert trace["verifier"] == {}
    assert trace["execution"]["generation"] == {
        "status": "not_run",
        "reason": "retrieval_only",
    }
    assert trace["execution"]["verification"] == {
        "status": "not_run",
        "reason": "retrieval_only",
    }
    assert trace["execution"]["judge"] == {
        "status": "not_run",
        "reason": "retrieval_only",
    }
