from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict, replace

import pytest

import legal_rag.evaluation as evaluation_module
from legal_rag.chat import LegalChatAssistant
from legal_rag.evaluation import evaluate_case
from legal_rag.evaluation_artifacts import (
    eval_record_from_artifact,
    eval_record_to_artifact,
)
from legal_rag.evaluation_scoring import ModelUsageDelta, score_completed_case
from legal_rag.judge import JudgeResult
from legal_rag.llm import CompletionUsage
from legal_rag.models import Chunk, EvalCase, SearchResult
from legal_rag.query import analyze_query


class _CountingRetriever:
    name = "completed-case-fixture"

    def __init__(self) -> None:
        self.calls = 0

    def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
        self.calls += 1
        return [
            SearchResult(
                chunk=Chunk(
                    chunk_id="fixture-chunk",
                    text="合成规则第一条说明合成事项。",
                    law_names=["合成规则"],
                    article_numbers=["第一条"],
                    source_files=["fixture.txt"],
                    line_nos=[1],
                    strategy="article",
                    metadata={"snapshot_id": "fixture-v1"},
                ),
                score=1.0,
                rank=1,
                retriever=self.name,
                trace={"fixture": {"rank": 1}},
            )
        ]


class _CountingAnswerClient:
    def __init__(self) -> None:
        self.calls = 0
        self.usage = CompletionUsage()

    def complete(self, prompt: str) -> str:
        self.calls += 1
        self.usage.calls += 1
        self.usage.record_tokens(input_tokens=11, output_tokens=7, total_tokens=18)
        self.usage.latency_ms += 2.5
        return json.dumps(
            {
                "answer_text": "合成规则第一条说明合成事项 [S1]。",
                "answer_mode": "evidence_answer",
                "claims": [
                    {
                        "claim_id": "C1",
                        "text": "合成规则第一条说明合成事项",
                        "source_ids": ["S1"],
                    }
                ],
                "limitations": [],
                "clarification_question": None,
            },
            ensure_ascii=False,
        )


class _CountingJudgeClient:
    def __init__(self) -> None:
        self.calls = 0
        self.usage = CompletionUsage()

    def complete(self, prompt: str) -> str:
        self.calls += 1
        self.usage.calls += 1
        self.usage.record_tokens(input_tokens=5, output_tokens=3, total_tokens=8)
        self.usage.latency_ms += 1.25
        return (
            '{"faithfulness":0.9,"relevance":0.8,"completeness":0.7,'
            '"passed":true,"comment":"fixture"}'
        )


def _case(**overrides) -> EvalCase:
    values = {
        "case_id": "completed-case",
        "question": "合成规则第一条说明什么？",
        "case_type": "synthetic",
        "expected_law": "合成规则",
        "expected_articles": ["第一条"],
        "keywords": ["合成事项"],
        "expected_behavior": "evidence_answer",
        "session_group": None,
        "turn_index": 0,
        "schema_version": 2,
    }
    values.update(overrides)
    return EvalCase(**values)


def _capture_generated_outcome(monkeypatch):
    retriever = _CountingRetriever()
    assistant = LegalChatAssistant(retriever, model="completed-case-fixture")
    answer_client = _CountingAnswerClient()
    judge_client = _CountingJudgeClient()
    assistant.llm = answer_client
    captured = []
    real_scorer = evaluation_module.score_completed_case

    def capture(outcome):
        captured.append(outcome)
        return real_scorer(outcome)

    monkeypatch.setattr(evaluation_module, "score_completed_case", capture)
    evaluated = evaluate_case(
        case=_case(),
        retriever=retriever,
        chunk_strategy="article",
        model="completed-case-fixture",
        generate=True,
        assistant=assistant,
        judge_client=judge_client,
        trace_metadata={"fixture": {"labels": ["d4b"]}},
    )
    assert len(captured) == 1
    assert captured[0].generation_kind == "model"
    return evaluated, captured[0], retriever, assistant, answer_client, judge_client


def test_completed_case_scoring_has_no_execution_or_persistence_side_effects(
    monkeypatch,
) -> None:
    expected, outcome, retriever, assistant, answer_client, judge_client = (
        _capture_generated_outcome(monkeypatch)
    )
    calls_before = (retriever.calls, answer_client.calls, judge_client.calls)
    memory_before = assistant.export_session_state()
    outcome_before = deepcopy(outcome)

    def forbidden(*args, **kwargs):
        raise AssertionError("pure completed-case scoring executed an upstream stage")

    with monkeypatch.context() as guarded:
        guarded.setattr(evaluation_module, "analyze_query", forbidden)
        guarded.setattr(evaluation_module, "retrieve_adaptive", forbidden)
        guarded.setattr(evaluation_module, "verify_answer", forbidden)
        guarded.setattr(evaluation_module, "judge_answer", forbidden)
        guarded.setattr(evaluation_module, "usage_snapshot", forbidden)
        guarded.setattr(evaluation_module.time, "perf_counter", forbidden)
        actual = score_completed_case(outcome)

    assert asdict(actual.record) == asdict(expected.record)
    assert actual.trace_record == expected.trace_record
    assert (retriever.calls, answer_client.calls, judge_client.calls) == calls_before
    assert assistant.export_session_state() == memory_before
    assert outcome == outcome_before


def test_completed_case_scoring_is_deterministic_defensive_and_strictly_serializable(
    monkeypatch,
) -> None:
    _, outcome, *_ = _capture_generated_outcome(monkeypatch)

    first = score_completed_case(outcome)
    second = score_completed_case(outcome)

    assert asdict(first.record) == asdict(second.record)
    assert first.trace_record == second.trace_record
    restored = eval_record_from_artifact(eval_record_to_artifact(first.record))
    assert asdict(restored) == asdict(first.record)

    first.record.execution["service"]["status"] = "caller-mutation"
    first.record.canonical_metrics["hit_at_5"]["value"] = 0
    first.trace_record["metadata"]["fixture"]["labels"].append("caller-mutation")
    first.trace_record["results"][0]["chunk_metadata"]["snapshot_id"] = "mutated"

    third = score_completed_case(outcome)
    assert asdict(third.record) == asdict(second.record)
    assert third.trace_record == second.trace_record
    assert outcome.trace_metadata == {"fixture": {"labels": ["d4b"]}}
    assert outcome.results[0].chunk.metadata["snapshot_id"] == "fixture-v1"


def test_completed_case_scoring_uses_only_current_run_usage_and_never_raw_responses(
    monkeypatch,
) -> None:
    _, outcome, *_ = _capture_generated_outcome(monkeypatch)
    assert outcome.judge_result is not None
    replay_outcome = replace(
        outcome,
        assistant_usage=ModelUsageDelta(),
        normalizer_usage=ModelUsageDelta(),
        judge_usage=ModelUsageDelta(),
        judge_result=replace(
            outcome.judge_result,
            raw_response="SECRET_RAW_JUDGE_RESPONSE_D4B",
        ),
    )

    evaluated = score_completed_case(replay_outcome)
    serialized = json.dumps(
        {"record": asdict(evaluated.record), "trace": evaluated.trace_record},
        ensure_ascii=False,
    )

    assert evaluated.record.assistant_llm_calls == 0
    assert evaluated.record.normalizer_llm_calls == 0
    assert evaluated.record.judge_llm_calls == 0
    assert evaluated.record.llm_failed_calls == 0
    assert evaluated.record.total_tokens == 0
    assert evaluated.record.llm_latency_ms == 0.0
    assert "SECRET_RAW_JUDGE_RESPONSE_D4B" not in serialized


def test_gold_changes_only_scoring_not_completed_execution_facts(monkeypatch) -> None:
    _, outcome, retriever, assistant, answer_client, judge_client = (
        _capture_generated_outcome(monkeypatch)
    )
    calls_before = (retriever.calls, answer_client.calls, judge_client.calls)
    memory_before = assistant.export_session_state()
    hit = score_completed_case(outcome)
    miss = score_completed_case(
        replace(
            outcome,
            case=replace(
                outcome.case,
                expected_law="另一合成规则",
                expected_articles=["第九条"],
                keywords=["未出现的关键词"],
            ),
        )
    )

    assert hit.record.hit_at_5 == 1
    assert miss.record.hit_at_5 == 0
    assert hit.record.keyword_coverage == 1.0
    assert miss.record.keyword_coverage == 0.0
    assert hit.record.answer == miss.record.answer
    assert hit.record.sources == miss.record.sources
    assert hit.record.execution == miss.record.execution
    assert hit.trace_record["results"] == miss.trace_record["results"]
    assert (retriever.calls, answer_client.calls, judge_client.calls) == calls_before
    assert assistant.export_session_state() == memory_before


@pytest.mark.parametrize(
    "usage",
    [
        {
            "calls": True,
            "failed_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "token_usage_calls": 0,
            "latency_ms": 0.0,
        },
        {
            "calls": 1,
            "failed_calls": 2,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "token_usage_calls": 0,
            "latency_ms": 0.0,
        },
        {
            "calls": 1,
            "failed_calls": 0,
            "input_tokens": 3,
            "output_tokens": 4,
            "total_tokens": 1,
            "token_usage_calls": 1,
            "latency_ms": 0.0,
        },
        {
            "calls": 0,
            "failed_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "token_usage_calls": 0,
            "latency_ms": float("nan"),
        },
    ],
)
def test_model_usage_delta_rejects_ambiguous_or_impossible_values(usage) -> None:
    with pytest.raises(ValueError):
        ModelUsageDelta.from_mapping(usage)


def test_configured_judge_cannot_silently_disappear_from_completed_outcome(
    monkeypatch,
) -> None:
    _, outcome, *_ = _capture_generated_outcome(monkeypatch)

    with pytest.raises(ValueError, match="configured judge"):
        score_completed_case(replace(outcome, judge_result=None))


def test_completed_outcome_rejects_contradictory_execution_facts(monkeypatch) -> None:
    _, outcome, *_ = _capture_generated_outcome(monkeypatch)
    assert outcome.verification is not None
    assert outcome.structured_answer is not None

    invalid = (
        replace(outcome, generate=False),
        replace(outcome, error="evaluation service failed"),
        replace(outcome, verification=None, structured_answer=None),
        replace(outcome, assistant_usage=ModelUsageDelta(calls=-1)),
        replace(outcome, case=replace(outcome.case, case_id="")),
        replace(outcome, results=(replace(outcome.results[0], rank=-2),)),
        replace(
            outcome,
            evidence_check=replace(outcome.evidence_check, sufficient=2),
        ),
        replace(
            outcome,
            verification=replace(outcome.verification, schema_valid=1),
        ),
        replace(
            outcome,
            structured_answer=replace(
                outcome.structured_answer,
                answer_mode="unsupported_mode",
            ),
        ),
        replace(
            outcome,
            pre_fallback_answer=outcome.structured_answer,
            pre_fallback_verification=outcome.verification,
        ),
        replace(
            outcome,
            generation_kind="generation_error",
            generation_error="secret_generation_code",
            judge_result=None,
            judge_usage=ModelUsageDelta(),
        ),
    )
    for value in invalid:
        with pytest.raises(ValueError):
            score_completed_case(value)


def test_completed_outcome_rejects_unsafe_or_inconsistent_judge_facts(
    monkeypatch,
) -> None:
    _, outcome, *_ = _capture_generated_outcome(monkeypatch)
    leaking_error = JudgeResult(
        faithfulness=None,
        relevance=None,
        completeness=None,
        passed=None,
        comment="",
        source="error",
        error="unsafe judge detail must not escape",
        status="error",
        error_code="SECRET_REASON",
    )
    inconsistent_success = JudgeResult(
        faithfulness=0.1,
        relevance=0.1,
        completeness=0.1,
        passed=True,
        comment="bad fixture",
        status="succeeded",
    )

    for judge_result in (leaking_error, inconsistent_success):
        with pytest.raises(ValueError):
            score_completed_case(replace(outcome, judge_result=judge_result))


def test_authoritative_generation_kind_cannot_be_spoofed_by_answer_adapter_source(
    monkeypatch,
) -> None:
    _, outcome, *_ = _capture_generated_outcome(monkeypatch)
    assert outcome.structured_answer is not None
    model_returned_object = replace(
        outcome,
        structured_answer=replace(
            outcome.structured_answer,
            adapter_source="programmatic",
        ),
        generation_kind="model",
    )

    evaluated = score_completed_case(model_returned_object)

    assert evaluated.record.execution["generation"] == {
        "status": "succeeded",
        "reason": None,
    }
    assert evaluated.record.generation_attempt["attempted"] is True
    assert evaluated.record.generation_attempt["status"] == "accepted"


def test_legacy_wrapper_normalizes_partial_telemetry_failure_to_service_error() -> None:
    retriever = _CountingRetriever()
    result = retriever.retrieve("fixture")[0]

    class _ExplodingAdaptiveTrace:
        analysis = analyze_query(_case().question)

        def to_trace(self):
            raise RuntimeError("SECRET_TELEMETRY_FAILURE")

    class _PartialTelemetryAssistant:
        llm = None
        last_adaptive_result = _ExplodingAdaptiveTrace()
        last_evidence_check = None
        last_verification = None
        last_structured_answer = None
        last_pre_fallback_answer = None
        last_pre_fallback_verification = None
        last_generation_error = None
        last_generation_kind = "model"

        def reset_memory(self) -> None:
            return None

        def answer(self, question: str, *, generate: bool = True):
            return "不应进入服务错误产物的部分答案", [result]

    evaluated = evaluate_case(
        case=_case(),
        retriever=retriever,
        chunk_strategy="article",
        model="partial-telemetry-fixture",
        generate=True,
        assistant=_PartialTelemetryAssistant(),
    )
    serialized = json.dumps(
        {"record": asdict(evaluated.record), "trace": evaluated.trace_record},
        ensure_ascii=False,
    )

    assert evaluated.record.execution["service"] == {
        "status": "error",
        "reason": "service_error",
    }
    assert evaluated.record.answer == ""
    assert evaluated.trace_record["results"] == []
    assert "部分答案" not in serialized
    assert "SECRET_TELEMETRY_FAILURE" not in serialized
