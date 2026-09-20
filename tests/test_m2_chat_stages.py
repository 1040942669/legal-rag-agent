from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from typing import Any

import pytest

from legal_rag.chat import (
    STRUCTURED_ANSWER_PARSER_VERSION,
    ConversationMemory,
    LegalChatAssistant,
)
from legal_rag.models import AnswerClaim, Chunk, SearchResult, StructuredAnswer
from legal_rag.provider_errors import ProviderCallError


def _result() -> SearchResult:
    return SearchResult(
        chunk=Chunk(
            chunk_id="synthetic-a",
            text="合成测试法第一条规定，合成主体应当遵守合成义务。",
            law_names=["合成测试法"],
            article_numbers=["第一条"],
            source_files=["synthetic.txt"],
            line_nos=[1],
            strategy="article",
            metadata={"snapshot_id": "fixture-v1"},
        ),
        score=1.0,
        rank=1,
        retriever="deterministic",
        trace={"fixture": True},
    )


class _DeterministicRetriever:
    name = "deterministic"

    def __init__(self) -> None:
        self.queries: list[str] = []

    def retrieve(self, query: str, top_k: int = 5):
        self.queries.append(query)
        return [_result()]


class _AnswerClient:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return json.dumps(
            {
                "answer_text": "合成主体应当遵守合成义务 [S1]。",
                "answer_mode": "evidence_answer",
                "claims": [
                    {
                        "claim_id": "C1",
                        "text": "合成主体应当遵守合成义务",
                        "source_ids": ["S1"],
                    }
                ],
                "limitations": [],
                "clarification_question": None,
            },
            ensure_ascii=False,
        )


def _assistant(
    *, memory_token_limit: int = 2000
) -> tuple[LegalChatAssistant, _DeterministicRetriever, _AnswerClient]:
    retriever = _DeterministicRetriever()
    client = _AnswerClient()
    assistant = LegalChatAssistant(
        retriever,
        model="deterministic-offline-fixture",
        memory_token_limit=memory_token_limit,
    )
    assistant.llm = client
    return assistant, retriever, client


def test_conversation_memory_add_turn_is_atomic() -> None:
    memory = ConversationMemory(token_limit=2000)
    memory.add_turn("第一问", "第一答")
    before = memory.export_state()

    with pytest.raises(ValueError):
        memory.add_turn("第二问", "\ud800")

    assert memory.export_state() == before


def test_explicit_chat_stages_do_not_publish_state_before_commit() -> None:
    assistant, retriever, client = _assistant()
    question = "《合成测试法》第一条规定什么？"

    prepared = assistant.prepare_question(question)
    retrieved = assistant.retrieve_turn(prepared)
    generated = assistant.generate_turn(retrieved, generate=True)
    verified = assistant.verify_turn(generated)

    assert prepared.original_question == question
    assert prepared.memory_text == ""
    assert prepared.session_state_before == assistant.export_session_state()
    assert retrieved.evidence_check is not None
    assert retrieved.evidence_check.sufficient is True
    assert retrieved.terminal_answer is None
    assert generated.kind == "model"
    assert generated.raw_response is not None
    assert json.loads(generated.raw_response)["answer_mode"] == "evidence_answer"
    assert generated.parser_version == STRUCTURED_ANSWER_PARSER_VERSION
    assert verified.verification is not None
    assert verified.verification.passed is True
    assert retriever.queries
    assert client.prompts
    assert assistant.memory.messages == []
    assert assistant.last_adaptive_result is None
    assert assistant.last_structured_answer is None

    assistant.commit_turn(verified)

    assert [role for role, _ in assistant.memory.messages] == ["user", "assistant"]
    assert assistant.last_adaptive_result == retrieved.adaptive_result
    assert assistant.last_structured_answer == verified.final_answer
    assert assistant.last_verification == verified.verification


def test_staged_composition_matches_legacy_answer_wrapper() -> None:
    staged, staged_retriever, staged_client = _assistant()
    wrapped, wrapped_retriever, wrapped_client = _assistant()
    question = "《合成测试法》第一条规定什么？"

    prepared = staged.prepare_question(question)
    retrieved = staged.retrieve_turn(prepared)
    generated = staged.generate_turn(retrieved, generate=True)
    verified = staged.verify_turn(generated)
    staged.commit_turn(verified)

    wrapped_answer, wrapped_results = wrapped.answer(question, generate=True)

    assert verified.answer_text == wrapped_answer
    assert list(retrieved.results) == wrapped_results
    assert staged.export_session_state() == wrapped.export_session_state()
    assert staged.last_structured_answer == wrapped.last_structured_answer
    assert staged.last_verification == wrapped.last_verification
    assert staged.last_pre_fallback_answer == wrapped.last_pre_fallback_answer
    assert (
        staged.last_pre_fallback_verification == wrapped.last_pre_fallback_verification
    )
    assert staged_retriever.queries == wrapped_retriever.queries
    assert staged_client.prompts == wrapped_client.prompts


def test_stale_staged_turn_cannot_commit_over_changed_memory() -> None:
    assistant, _, _ = _assistant()
    prepared = assistant.prepare_question("《合成测试法》第一条规定什么？")
    retrieved = assistant.retrieve_turn(prepared)
    generated = assistant.generate_turn(retrieved, generate=True)
    verified = assistant.verify_turn(generated)
    assistant.memory.add_turn("并发问题", "并发回答")
    state_after_other_turn = assistant.export_session_state()

    with pytest.raises(RuntimeError, match="state changed"):
        assistant.commit_turn(verified)

    assert assistant.export_session_state() == state_after_other_turn


def test_pre_retrieval_refusal_is_an_explicit_programmatic_stage() -> None:
    class _ForbiddenRetriever:
        name = "forbidden"

        def retrieve(self, query: str, top_k: int = 5):
            raise AssertionError("pre-retrieval refusal must not retrieve")

    assistant = LegalChatAssistant(
        _ForbiddenRetriever(),
        model="deterministic-offline-fixture",
    )
    prepared = assistant.prepare_question("这个案子怎么起诉才能胜诉？")
    retrieved = assistant.retrieve_turn(prepared)
    generated = assistant.generate_turn(retrieved, generate=True)
    verified = assistant.verify_turn(generated)

    assert retrieved.terminal_kind == "pre_retrieval_refusal"
    assert retrieved.adaptive_result is None
    assert generated.kind == "pre_retrieval_refusal"
    assert verified.final_answer is not None
    assert verified.final_answer.answer_mode == "out_of_scope"
    assert assistant.memory.messages == []

    assistant.commit_turn(verified)

    assert assistant.last_adaptive_result is None
    assert assistant.last_verification is not None
    assert len(assistant.memory.messages) == 2


def test_generation_error_is_explicit_and_committed_only_after_verification() -> None:
    assistant, _, _ = _assistant()

    class _FailingClient:
        def complete(self, prompt: str) -> str:
            raise TimeoutError("synthetic timeout")

    assistant.llm = _FailingClient()
    prepared = assistant.prepare_question("《合成测试法》第一条规定什么？")
    retrieved = assistant.retrieve_turn(prepared)
    generated = assistant.generate_turn(retrieved, generate=True)
    verified = assistant.verify_turn(generated)

    assert generated.kind == "generation_error"
    assert generated.generation_error == "generation_error"
    assert verified.final_answer is not None
    assert verified.final_answer.answer_mode == "insufficient_evidence"
    assert assistant.last_generation_error is None
    assert assistant.memory.messages == []

    assistant.commit_turn(verified)

    assert assistant.last_generation_error == "generation_error"
    assert len(assistant.memory.messages) == 2


def test_typed_provider_errors_only_escape_strict_experiment_clients() -> None:
    class Client:
        def __init__(self, *, strict: bool) -> None:
            self.propagate_provider_errors = strict

        def complete(self, prompt: str) -> str:
            raise ProviderCallError(
                "timeout",
                provider="test_provider",
                operation="completion",
            )

    compatible, _, _ = _assistant()
    compatible.llm = Client(strict=False)
    retrieved = compatible.retrieve_turn(compatible.prepare_question("合成问题"))
    generated = compatible.generate_turn(retrieved, generate=True)
    assert generated.kind == "generation_error"
    assert compatible.condense_with_llm_rewrite("追问", "上一问") == ""

    strict, _, _ = _assistant()
    strict.llm = Client(strict=True)
    retrieved = strict.retrieve_turn(strict.prepare_question("合成问题"))
    with pytest.raises(ProviderCallError):
        strict.generate_turn(retrieved, generate=True)
    with pytest.raises(ProviderCallError):
        strict.condense_with_llm_rewrite("追问", "上一问")


def test_commit_rejects_forged_verified_output_without_publishing_state() -> None:
    assistant, _, _ = _assistant()
    prepared = assistant.prepare_question("《合成测试法》第一条规定什么？")
    retrieved = assistant.retrieve_turn(prepared)
    generated = assistant.generate_turn(retrieved, generate=True)
    verified = assistant.verify_turn(generated)
    forged = replace(verified, answer_text="未经验证的回答")

    with pytest.raises(RuntimeError, match="deterministic verification"):
        assistant.commit_turn(forged)

    assert assistant.memory.messages == []
    assert assistant.last_adaptive_result is None
    assert assistant.last_verification is None
    assert assistant.last_structured_answer is None


def test_retrieval_only_text_must_be_rendered_from_retrieved_evidence() -> None:
    assistant, _, _ = _assistant()
    prepared = assistant.prepare_question("《合成测试法》第一条规定什么？")
    retrieved = assistant.retrieve_turn(prepared)
    generated = assistant.generate_turn(retrieved, generate=False)
    forged = replace(generated, answer_text="未经验证的检索结果")

    with pytest.raises(RuntimeError, match="retrieval-only turn contract"):
        assistant.verify_turn(forged)

    assert assistant.memory.messages == []


class _ExplodingMapping(Mapping[str, str]):
    def __getitem__(self, key: str) -> str:
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        raise RuntimeError("synthetic mapping failure")

    def __len__(self) -> int:
        return 1

    def __deepcopy__(self, memo: dict[int, Any]) -> _ExplodingMapping:
        return self


def test_commit_materializes_telemetry_before_any_state_change() -> None:
    assistant, _, _ = _assistant()
    prepared = assistant.prepare_question("《合成测试法》第一条规定什么？")
    retrieved = assistant.retrieve_turn(prepared)
    generated = assistant.generate_turn(retrieved, generate=True)
    poisoned_retrieved = replace(retrieved, source_id_map=_ExplodingMapping())
    poisoned_generated = replace(generated, retrieved=poisoned_retrieved)
    poisoned_verified = assistant.verify_turn(poisoned_generated)

    with pytest.raises(RuntimeError, match="synthetic mapping failure"):
        assistant.commit_turn(poisoned_verified)

    assert assistant.memory.messages == []
    assert assistant.last_adaptive_result is None
    assert assistant.last_evidence_check is None
    assert assistant.last_verification is None
    assert assistant.last_structured_answer is None
    assert assistant.last_evidence_source_id_map == {}


def test_concurrent_commits_from_the_same_snapshot_have_one_winner() -> None:
    assistant, _, _ = _assistant()
    prepared = assistant.prepare_question("《合成测试法》第一条规定什么？")
    retrieved = assistant.retrieve_turn(prepared)
    generated = assistant.generate_turn(retrieved, generate=True)
    verified = assistant.verify_turn(generated)
    original_verify_turn = assistant.verify_turn
    verification_barrier = Barrier(2)

    def synchronized_verify_turn(candidate):
        canonical = original_verify_turn(candidate)
        verification_barrier.wait(timeout=5)
        return canonical

    assistant.verify_turn = synchronized_verify_turn  # type: ignore[method-assign]

    def commit() -> str:
        try:
            assistant.commit_turn(verified)
        except RuntimeError as exc:
            return str(exc)
        return "committed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: commit(), range(2)))

    assert outcomes.count("committed") == 1
    assert outcomes.count("conversation state changed before turn commit") == 1
    assert len(assistant.memory.messages) == 2


def test_trimmed_memory_cannot_create_an_aba_duplicate_commit() -> None:
    assistant, _, _ = _assistant(memory_token_limit=1)
    prepared = assistant.prepare_question("《合成测试法》第一条规定什么？")
    retrieved = assistant.retrieve_turn(prepared)
    generated = assistant.generate_turn(retrieved, generate=True)
    verified = assistant.verify_turn(generated)

    assistant.commit_turn(verified)
    assert assistant.memory.messages == []

    with pytest.raises(RuntimeError, match="state changed"):
        assistant.commit_turn(verified)

    assert assistant.memory.messages == []


def test_reset_invalidates_a_prepared_turn_even_when_memory_stays_empty() -> None:
    assistant, _, _ = _assistant()
    prepared = assistant.prepare_question("《合成测试法》第一条规定什么？")
    retrieved = assistant.retrieve_turn(prepared)
    generated = assistant.generate_turn(retrieved, generate=True)
    verified = assistant.verify_turn(generated)

    assistant.reset_memory()
    assert assistant.memory.messages == []

    with pytest.raises(RuntimeError, match="state changed"):
        assistant.commit_turn(verified)

    assert assistant.memory.messages == []


def test_direct_memory_mutation_cannot_create_an_aba_commit() -> None:
    assistant, _, _ = _assistant(memory_token_limit=1)
    prepared = assistant.prepare_question("《合成测试法》第一条规定什么？")
    retrieved = assistant.retrieve_turn(prepared)
    generated = assistant.generate_turn(retrieved, generate=True)
    verified = assistant.verify_turn(generated)

    assistant.memory.add_turn("并发问题", "并发回答")
    assert assistant.memory.messages == []

    with pytest.raises(RuntimeError, match="state changed"):
        assistant.commit_turn(verified)

    assert assistant.memory.messages == []


def test_structured_provider_response_remains_replayable() -> None:
    assistant, _, _ = _assistant()
    structured_response = StructuredAnswer(
        answer_text="合成主体应当遵守合成义务 [S1]。",
        answer_mode="evidence_answer",
        claims=[
            AnswerClaim(
                claim_id="C1",
                text="合成主体应当遵守合成义务",
                source_ids=["S1"],
            )
        ],
        limitations=[],
        clarification_question=None,
    )

    class _StructuredClient:
        def complete(self, prompt: str) -> StructuredAnswer:
            return structured_response

    assistant.llm = _StructuredClient()
    prepared = assistant.prepare_question("《合成测试法》第一条规定什么？")
    retrieved = assistant.retrieve_turn(prepared)
    generated = assistant.generate_turn(retrieved, generate=True)
    verified = assistant.verify_turn(generated)

    assert generated.raw_response == structured_response
    assert verified.verification is not None
    assert verified.verification.passed is True
    assert isinstance(generated.raw_response, StructuredAnswer)
    assert generated.answer is not None
    assert generated.answer.limitations is not generated.raw_response.limitations

    generated.answer.limitations.append("篡改后的限制")

    assert generated.raw_response.limitations == []
    with pytest.raises(RuntimeError, match="does not match its raw response"):
        assistant.verify_turn(generated)


def test_model_raw_response_tampering_is_rejected() -> None:
    assistant, _, _ = _assistant()
    prepared = assistant.prepare_question("《合成测试法》第一条规定什么？")
    retrieved = assistant.retrieve_turn(prepared)
    generated = assistant.generate_turn(retrieved, generate=True)
    tampered = replace(generated, raw_response="篡改后的原始响应")

    with pytest.raises(RuntimeError, match="does not match its raw response"):
        assistant.verify_turn(tampered)

    assert assistant.memory.messages == []
