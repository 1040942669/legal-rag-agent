from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from legal_rag.chat import LegalChatAssistant
from legal_rag.chat_artifacts import (
    generated_turn_from_artifact,
    generated_turn_to_artifact,
    prepared_question_from_artifact,
    prepared_question_to_artifact,
    retrieved_turn_from_artifact,
    retrieved_turn_to_artifact,
    structured_answer_from_artifact,
    structured_answer_to_artifact,
    verified_turn_from_artifact,
    verified_turn_to_artifact,
)
from legal_rag.experiment_runtime import canonical_hash, canonical_json_bytes
from legal_rag.models import (
    AnswerClaim,
    Chunk,
    SearchResult,
    StructuredAnswer,
    VerificationContext,
)


QUESTION = "《合成测试法》第一条规定什么？"


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
            metadata={
                "snapshot_id": "fixture-v1",
                "nested": {"permissions": ["public"]},
            },
        ),
        score=1.0,
        rank=1,
        retriever="deterministic",
        trace={"fixture": True, "scores": [1.0]},
    )


class _DeterministicRetriever:
    name = "deterministic"

    def __init__(self, *, results: list[SearchResult] | None = None) -> None:
        self.results = [_result()] if results is None else results
        self.queries: list[str] = []

    def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
        self.queries.append(query)
        return deepcopy(self.results[:top_k])


class _TextAnswerClient:
    def complete(self, prompt: str) -> str:
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


class _FailingClient:
    def complete(self, prompt: str) -> str:
        raise TimeoutError("synthetic timeout")


class _UnsupportedObjectClient:
    def complete(self, prompt: str) -> object:
        return object()


class _StructuredAnswerClient:
    def __init__(self, answer: StructuredAnswer) -> None:
        self.answer = answer

    def complete(self, prompt: str) -> StructuredAnswer:
        return deepcopy(self.answer)


def _assistant(
    *,
    retriever: _DeterministicRetriever | None = None,
    client: object | None = None,
    memory_token_limit: int = 2000,
    adaptive_enabled: bool = False,
    verification_context: VerificationContext | None = None,
) -> LegalChatAssistant:
    assistant = LegalChatAssistant(
        retriever or _DeterministicRetriever(),
        model="deterministic-offline-fixture",
        memory_token_limit=memory_token_limit,
        adaptive_enabled=adaptive_enabled,
        verification_context=verification_context,
    )
    assistant.llm = client or _TextAnswerClient()
    return assistant


def _full_turn(
    assistant: LegalChatAssistant,
    *,
    question: str = QUESTION,
    generate: bool = True,
):
    prepared = assistant.prepare_question(question)
    retrieved = assistant.retrieve_turn(prepared)
    generated = assistant.generate_turn(retrieved, generate=generate)
    verified = assistant.verify_turn(generated)
    return prepared, retrieved, generated, verified


def _round_trip_full_turn(
    assistant: LegalChatAssistant,
    *,
    question: str = QUESTION,
    generate: bool = True,
):
    prepared, retrieved, generated, verified = _full_turn(
        assistant, question=question, generate=generate
    )
    prepared_artifact = prepared_question_to_artifact(prepared)
    restored_prepared = prepared_question_from_artifact(
        prepared_artifact, assistant=assistant
    )
    retrieved_artifact = retrieved_turn_to_artifact(retrieved)
    restored_retrieved = retrieved_turn_from_artifact(
        retrieved_artifact, prepared=restored_prepared
    )
    generated_artifact = generated_turn_to_artifact(generated)
    restored_generated = generated_turn_from_artifact(
        generated_artifact, retrieved=restored_retrieved
    )
    verified_artifact = verified_turn_to_artifact(verified)
    restored_verified = verified_turn_from_artifact(
        verified_artifact, generated=restored_generated
    )
    return (
        (prepared, retrieved, generated, verified),
        (
            restored_prepared,
            restored_retrieved,
            restored_generated,
            restored_verified,
        ),
        (
            prepared_artifact,
            retrieved_artifact,
            generated_artifact,
            verified_artifact,
        ),
    )


def test_chat_stage_artifacts_round_trip_and_commit_canonically() -> None:
    assistant = _assistant()
    original, restored, artifacts = _round_trip_full_turn(assistant)
    prepared, retrieved, generated, verified = original
    (
        restored_prepared,
        restored_retrieved,
        restored_generated,
        restored_verified,
    ) = restored
    prepared_artifact, retrieved_artifact, generated_artifact, verified_artifact = (
        artifacts
    )

    assert prepared_question_to_artifact(restored_prepared) == prepared_artifact
    assert retrieved_turn_to_artifact(restored_retrieved) == retrieved_artifact
    assert generated_turn_to_artifact(restored_generated) == generated_artifact
    assert verified_turn_to_artifact(restored_verified) == verified_artifact
    assert restored_retrieved.results == retrieved.results
    assert restored_generated.raw_response == generated.raw_response
    assert restored_verified == verified
    assert all(
        canonical_json_bytes(artifact)
        for artifact in (
            prepared_artifact,
            retrieved_artifact,
            generated_artifact,
            verified_artifact,
        )
    )
    assert retrieved_artifact["payload"]["prepared_sha256"] == canonical_hash(
        prepared_artifact
    )
    assert generated_artifact["payload"]["retrieved_sha256"] == canonical_hash(
        retrieved_artifact
    )
    assert verified_artifact["payload"]["generated_sha256"] == canonical_hash(
        generated_artifact
    )

    assistant.commit_turn(restored_verified)

    assert len(assistant.memory.messages) == 2
    assert assistant.last_structured_answer == restored_verified.final_answer


def test_integer_search_score_is_canonical_across_the_hash_chain() -> None:
    integer_score = replace(_result(), score=1)
    assistant = _assistant(retriever=_DeterministicRetriever(results=[integer_score]))

    _, restored, artifacts = _round_trip_full_turn(assistant)

    retrieved_artifact = artifacts[1]
    assert retrieved_artifact["payload"]["results"][0]["search_result"]["score"] == 1.0
    assert retrieved_turn_to_artifact(restored[1]) == retrieved_artifact
    assert generated_turn_to_artifact(restored[2]) == artifacts[2]
    assert verified_turn_to_artifact(restored[3]) == artifacts[3]


def test_structured_raw_response_preserves_internal_fields_and_copies() -> None:
    raw = StructuredAnswer(
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
        schema_valid=True,
        adapter_source="structured_object",
        parse_errors=["provider-note"],
    )
    assistant = _assistant(client=_StructuredAnswerClient(raw))
    _, restored, artifacts = _round_trip_full_turn(assistant)
    restored_generated = restored[2]
    generated_artifact = artifacts[2]

    assert generated_artifact["payload"]["raw_response"]["kind"] == (
        "structured_answer"
    )
    assert isinstance(restored_generated.raw_response, StructuredAnswer)
    assert restored_generated.raw_response.schema_valid is True
    assert restored_generated.raw_response.adapter_source == "structured_object"
    assert restored_generated.raw_response.parse_errors == ["provider-note"]

    generated_artifact["payload"]["raw_response"]["value"]["parse_errors"].append(
        "tampered"
    )
    raw.parse_errors.append("after-artifact")

    assert restored_generated.raw_response.parse_errors == ["provider-note"]


def test_schema_invalid_structured_response_fallback_round_trips() -> None:
    malformed = StructuredAnswer(
        answer_text="未经结构约束的回答",
        answer_mode="",
        claims=[],
        limitations=[],
        clarification_question=None,
        schema_valid=False,
        adapter_source="",
        parse_errors=["provider-invalid"],
    )
    assistant = _assistant(client=_StructuredAnswerClient(malformed))

    original, restored, _ = _round_trip_full_turn(assistant)

    assert original[3].pre_fallback_answer is not None
    assert original[3].pre_fallback_verification is not None
    assert original[3].pre_fallback_verification.passed is False
    assert restored[3] == original[3]
    assistant.commit_turn(restored[3])
    assert len(assistant.memory.messages) == 2


def test_unsupported_provider_object_is_replayable_as_a_safe_fallback() -> None:
    assistant = _assistant(client=_UnsupportedObjectClient())

    original, restored, artifacts = _round_trip_full_turn(assistant)

    assert original[2].kind == "model"
    assert original[2].raw_response is None
    assert artifacts[2]["payload"]["raw_response"] == {
        "kind": "none",
        "value": None,
    }
    assert original[3].pre_fallback_verification is not None
    assert restored[3] == original[3]
    assistant.commit_turn(restored[3])
    assert len(assistant.memory.messages) == 2


def test_adaptive_plans_and_normalized_query_round_trip() -> None:
    assistant = _assistant(adaptive_enabled=True)
    question = "《合成测试法》第一条和第二条分别规定什么，并比较差异？"

    original, restored, _ = _round_trip_full_turn(
        assistant, question=question, generate=True
    )

    adaptive = original[1].adaptive_result
    restored_adaptive = restored[1].adaptive_result
    assert adaptive is not None
    assert adaptive.adaptive_used is True
    assert adaptive.normalized_query is not None
    assert adaptive.plans
    assert restored_adaptive == adaptive


def test_mixed_scope_filtered_retrieval_round_trips_with_original_source_map() -> None:
    base = _result()
    private = replace(
        base,
        chunk=replace(
            base.chunk,
            chunk_id="private",
            metadata={"snapshot_id": "snapshot-b", "scope_id": "tenant-b"},
        ),
        rank=1,
    )
    public = replace(
        base,
        chunk=replace(
            base.chunk,
            chunk_id="public",
            metadata={"snapshot_id": "snapshot-a", "scope_id": "public"},
        ),
        rank=2,
    )
    assistant = _assistant(
        retriever=_DeterministicRetriever(results=[private, public]),
        verification_context=VerificationContext(
            snapshot_id="snapshot-a", allowed_scope_ids=["public"]
        ),
    )

    original, restored, artifacts = _round_trip_full_turn(assistant)

    assert original[1].rejected_source_ids == ("S1",)
    assert original[1].source_id_map == {"S2": "S1"}
    assert original[1].adaptive_result is not None
    assert original[1].adaptive_result.results == list(original[1].results)
    assert restored[1] == original[1]

    tampered = deepcopy(artifacts[1])
    tampered["payload"]["results"][0]["search_result"]["rank"] = 99
    tampered["payload"]["adaptive_result"]["results"][0]["search_result"]["rank"] = 99
    with pytest.raises(ValueError, match="scope-filtered result trace"):
        retrieved_turn_from_artifact(tampered, prepared=original[0])


@pytest.mark.parametrize(
    ("retriever", "client", "generate", "question", "expected_kind"),
    [
        (
            _DeterministicRetriever(),
            _TextAnswerClient(),
            True,
            "这个案子怎么起诉才能胜诉？",
            "pre_retrieval_refusal",
        ),
        (
            _DeterministicRetriever(results=[]),
            _TextAnswerClient(),
            True,
            QUESTION,
            "evidence_limited",
        ),
        (
            _DeterministicRetriever(),
            _TextAnswerClient(),
            False,
            QUESTION,
            "retrieval_only",
        ),
        (
            _DeterministicRetriever(),
            _FailingClient(),
            True,
            QUESTION,
            "generation_error",
        ),
    ],
)
def test_programmatic_stage_variants_round_trip(
    retriever: _DeterministicRetriever,
    client: object,
    generate: bool,
    question: str,
    expected_kind: str,
) -> None:
    assistant = _assistant(retriever=retriever, client=client)

    original, restored, _ = _round_trip_full_turn(
        assistant, question=question, generate=generate
    )

    assert original[2].kind == expected_kind
    assert restored[3] == original[3]
    assistant.commit_turn(restored[3])
    assert len(assistant.memory.messages) == 2


def test_prepared_artifact_rebinds_only_to_the_exact_durable_session() -> None:
    source = _assistant()
    source.memory.add_turn("第一问", "第一答")
    prepared = source.prepare_question("那第一条呢？")
    artifact = prepared_question_to_artifact(prepared)

    assert set(artifact["payload"]) == {
        "original_question",
        "standalone_question",
        "analysis",
        "memory_text",
        "session_state_before",
    }
    target = _assistant()
    target.restore_session_state(source.export_session_state())

    restored = prepared_question_from_artifact(artifact, assistant=target)

    assert restored.memory_revision == target.memory.snapshot().revision
    assert restored.session_revision != prepared.session_revision
    assert prepared_question_to_artifact(restored) == artifact

    source.memory.add_turn("第二问", "第二答")
    with pytest.raises(RuntimeError, match="current conversation state"):
        prepared_question_from_artifact(artifact, assistant=source)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda prepared: replace(prepared, memory_text="not-the-checkpoint"),
        lambda prepared: replace(
            prepared,
            memory_messages_before=(("user", "not-the-checkpoint"),),
        ),
        lambda prepared: replace(
            prepared, memory_token_limit=prepared.memory_token_limit + 1
        ),
        lambda prepared: replace(prepared, memory_revision=True),
        lambda prepared: replace(prepared, session_revision=True),
    ],
)
def test_prepared_encoder_rejects_mismatched_omitted_fences(mutation) -> None:
    assistant = _assistant()
    assistant.memory.add_turn("第一问", "第一答")
    prepared = assistant.prepare_question("那第一条呢？")

    with pytest.raises(ValueError):
        prepared_question_to_artifact(mutation(prepared))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda artifact: artifact.update({"artifact_schema_version": True}),
        lambda artifact: artifact["payload"]["analysis"].update({"char_length": True}),
        lambda artifact: artifact["payload"]["analysis"].update(
            {"confidence": float("nan")}
        ),
        lambda artifact: artifact["payload"]["analysis"]["risk_flags"].append(
            "case_strategy"
        ),
        lambda artifact: artifact["payload"].update({"memory_text": "\ud800"}),
        lambda artifact: artifact["payload"]["session_state_before"].update(
            {"schema_version": True}
        ),
        lambda artifact: artifact["payload"]["session_state_before"]["memory"].update(
            {"token_limit": 2000.0}
        ),
    ],
)
def test_prepared_artifact_rejects_ambiguous_or_tampered_values(mutation) -> None:
    assistant = _assistant()
    artifact = prepared_question_to_artifact(assistant.prepare_question(QUESTION))
    mutation(artifact)

    with pytest.raises((ValueError, RuntimeError)):
        prepared_question_from_artifact(artifact, assistant=assistant)


def test_stage_artifacts_reject_unknown_or_missing_fields() -> None:
    assistant = _assistant()
    _, _, artifacts = _round_trip_full_turn(assistant)
    prepared_artifact, retrieved_artifact, generated_artifact, verified_artifact = (
        deepcopy(artifact) for artifact in artifacts
    )
    prepared_artifact["payload"]["unknown"] = True
    retrieved_artifact["payload"].pop("source_id_map")
    generated_artifact["payload"]["raw_response"]["unknown"] = True
    verified_artifact["payload"]["unknown"] = True

    with pytest.raises(ValueError, match="fields are invalid"):
        prepared_question_from_artifact(prepared_artifact, assistant=assistant)
    prepared = assistant.prepare_question(QUESTION)
    with pytest.raises(ValueError, match="fields are invalid"):
        retrieved_turn_from_artifact(retrieved_artifact, prepared=prepared)
    retrieved = assistant.retrieve_turn(prepared)
    with pytest.raises(ValueError, match="fields are invalid"):
        generated_turn_from_artifact(generated_artifact, retrieved=retrieved)
    generated = assistant.generate_turn(retrieved, generate=True)
    with pytest.raises(ValueError, match="fields are invalid"):
        verified_turn_from_artifact(verified_artifact, generated=generated)


def test_every_stage_rejects_an_upstream_hash_mismatch() -> None:
    assistant = _assistant()
    original, _, artifacts = _round_trip_full_turn(assistant)
    prepared, retrieved, generated, _ = original
    retrieved_artifact = deepcopy(artifacts[1])
    generated_artifact = deepcopy(artifacts[2])
    verified_artifact = deepcopy(artifacts[3])
    retrieved_artifact["payload"]["prepared_sha256"] = "0" * 64
    generated_artifact["payload"]["retrieved_sha256"] = "0" * 64
    verified_artifact["payload"]["generated_sha256"] = "0" * 64

    with pytest.raises(ValueError, match="prepared artifact hash"):
        retrieved_turn_from_artifact(retrieved_artifact, prepared=prepared)
    with pytest.raises(ValueError, match="retrieved artifact hash"):
        generated_turn_from_artifact(generated_artifact, retrieved=retrieved)
    with pytest.raises(ValueError, match="generated artifact hash"):
        verified_turn_from_artifact(verified_artifact, generated=generated)


def test_nested_stage_payloads_reject_invalid_map_and_union_values() -> None:
    assistant = _assistant()
    original, _, artifacts = _round_trip_full_turn(assistant)
    prepared, retrieved, _, _ = original
    retrieved_artifact = deepcopy(artifacts[1])
    retrieved_artifact["payload"]["source_id_map"] = {"S1": 1}
    generated_artifact = deepcopy(artifacts[2])
    generated_artifact["payload"]["raw_response"]["kind"] = "opaque"

    with pytest.raises(ValueError, match="must be a non-empty string"):
        retrieved_turn_from_artifact(retrieved_artifact, prepared=prepared)
    with pytest.raises(ValueError, match="kind is unsupported"):
        generated_turn_from_artifact(generated_artifact, retrieved=retrieved)


def test_semantically_impossible_stage_combinations_are_rejected() -> None:
    limited_assistant = _assistant(retriever=_DeterministicRetriever(results=[]))
    limited_prepared, limited_retrieved, _, _ = _full_turn(limited_assistant)
    limited_artifact = retrieved_turn_to_artifact(limited_retrieved)
    limited_artifact["payload"].update(
        {
            "terminal_kind": None,
            "terminal_answer": None,
            "terminal_expected_answer_mode": None,
        }
    )

    with pytest.raises(ValueError, match="limited-evidence retrieval contract"):
        retrieved_turn_from_artifact(limited_artifact, prepared=limited_prepared)

    assistant = _assistant()
    original, _, artifacts = _round_trip_full_turn(assistant)
    generated_artifact = deepcopy(artifacts[2])
    generated_artifact["payload"]["kind"] = "opaque"
    generated_artifact["payload"]["parser_version"] = None
    with pytest.raises(ValueError, match="kind is unsupported"):
        generated_turn_from_artifact(generated_artifact, retrieved=original[1])

    verified_artifact = deepcopy(artifacts[3])
    verified_artifact["payload"]["final_answer"] = None
    verified_artifact["payload"]["verification"] = None
    with pytest.raises(ValueError, match="retrieval-only answer text"):
        verified_turn_from_artifact(verified_artifact, generated=original[2])


def test_adaptive_and_filtered_evidence_cannot_drift_apart() -> None:
    assistant = _assistant()
    original, _, artifacts = _round_trip_full_turn(assistant)
    retrieved_artifact = deepcopy(artifacts[1])
    retrieved_artifact["payload"]["adaptive_result"]["results"][0]["search_result"][
        "chunk"
    ]["text"] = "tampered adaptive evidence"

    with pytest.raises(ValueError, match="evidence filter mapping"):
        retrieved_turn_from_artifact(retrieved_artifact, prepared=original[0])


def test_generation_error_final_answer_is_bound_to_the_safe_terminal() -> None:
    assistant = _assistant(client=_FailingClient())
    original, _, artifacts = _round_trip_full_turn(assistant)
    generated = original[2]
    verified_artifact = deepcopy(artifacts[3])
    generated_artifact = artifacts[2]
    verified_artifact["payload"]["answer_text"] = generated.answer_text
    verified_artifact["payload"]["final_answer"] = deepcopy(
        generated_artifact["payload"]["answer"]
    )

    with pytest.raises(ValueError, match="generation-error final answer"):
        verified_turn_from_artifact(verified_artifact, generated=generated)


def test_artifacts_and_decoders_defensively_copy_mutable_values() -> None:
    assistant = _assistant()
    prepared, retrieved, _, _ = _full_turn(assistant)
    retrieved_artifact = retrieved_turn_to_artifact(retrieved)
    restored = retrieved_turn_from_artifact(
        deepcopy(retrieved_artifact), prepared=prepared
    )

    retrieved.results[0].chunk.metadata["nested"]["permissions"].append("private")
    prepared.analysis.risk_flags.append("mutated-after-decode")
    retrieved_artifact["payload"]["results"][0]["search_result"]["trace"][
        "scores"
    ].append(0.0)

    restored_result = restored.results[0]
    assert restored_result.chunk.metadata["nested"]["permissions"] == ["public"]
    assert restored.prepared.analysis.risk_flags == []
    assert restored_result.trace["scores"] == [1.0]


def test_standalone_structured_answer_codec_preserves_invalid_evidence() -> None:
    answer = StructuredAnswer(
        answer_text="",
        answer_mode="invalid",
        claims=[],
        limitations=[],
        clarification_question=None,
        schema_valid=False,
        adapter_source="",
        parse_errors=["answer_text_must_be_non_empty_string"],
    )

    artifact = structured_answer_to_artifact(answer)
    restored = structured_answer_from_artifact(artifact)

    assert restored == answer
