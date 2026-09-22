from __future__ import annotations

import json
import time
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

import legal_rag.evaluation as evaluation_module
import legal_rag.experiment_adapter as adapter_module
from legal_rag.chat import (
    GeneratedTurn,
    LegalChatAssistant,
    PreparedQuestion,
    RetrievedTurn,
    VerifiedTurn,
)
from legal_rag.evaluation_artifacts import (
    eval_case_to_artifact,
    eval_record_to_artifact,
)
from legal_rag.evaluation_scoring import score_completed_case
from legal_rag.experiment_adapter import (
    EvaluationRuntimeSpec,
    LegalEvaluationRuntimeFactory,
    build_manifest_case,
    build_manifest_cases,
    decode_evaluation_output,
)
from legal_rag.experiment_runner import (
    OBSERVATION_STAGES,
    ExperimentRunner,
    RunnerControls,
    plan_work_units,
)
from legal_rag.experiment_runtime import (
    EXTERNAL_CALL_KINDS,
    ExactStageCache,
    ExperimentContractError,
    build_experiment_manifest,
    canonical_hash,
)
from legal_rag.experiment_store import ExperimentStore
from legal_rag.llm import CompletionUsage
from legal_rag.models import Chunk, EvalCase, SearchResult
from legal_rag.provider_errors import ProviderCallError
from legal_rag.retrieval import BM25Retriever


class _ProviderFreeRetriever:
    name = "m2-adapter-provider-free"
    provider_free = True
    uses_query_embedding = False

    def __init__(self) -> None:
        self.queries: list[str] = []

    def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
        self.queries.append(query)
        return [
            SearchResult(
                chunk=Chunk(
                    chunk_id="synthetic-rule-1",
                    text="合成规则第一条规定，合成主体应当遵守合成事项。",
                    law_names=["合成规则"],
                    article_numbers=["第一条"],
                    source_files=["synthetic.txt"],
                    line_nos=[1],
                    strategy="article",
                    metadata={"snapshot_id": "m2-adapter-fixture-v1"},
                ),
                score=1.0,
                rank=1,
                retriever=self.name,
                trace={"fixture": {"rank": 1}},
            )
        ][:top_k]


class _CountingAnswerClient:
    hidden_retries_disabled = True

    def __init__(self) -> None:
        self.calls = 0
        self.usage = CompletionUsage()

    def complete(self, prompt: str) -> str:
        self.calls += 1
        self.usage.calls += 1
        self.usage.record_tokens(
            input_tokens=11,
            output_tokens=7,
            total_tokens=18,
        )
        self.usage.latency_ms += 1.5
        return json.dumps(
            {
                "answer_text": "合成规则第一条要求遵守合成事项 [S1]。",
                "answer_mode": "evidence_answer",
                "claims": [
                    {
                        "claim_id": "C1",
                        "text": "合成规则第一条要求遵守合成事项",
                        "source_ids": ["S1"],
                    }
                ],
                "limitations": [],
                "clarification_question": None,
            },
            ensure_ascii=False,
        )


class _SequencedAnswerClient(_CountingAnswerClient):
    def __init__(self, answers: list[str]) -> None:
        super().__init__()
        self.answers = answers

    def complete(self, prompt: str) -> str:
        payload = json.loads(super().complete(prompt))
        answer = self.answers.pop(0)
        payload["answer_text"] = f"{answer} [S1]。"
        payload["claims"][0]["text"] = answer
        return json.dumps(payload, ensure_ascii=False)


class _SequencedJudgeClient:
    hidden_retries_disabled = True

    def __init__(self, outcomes: list[str]) -> None:
        self.outcomes = outcomes
        self.calls = 0
        self.usage = CompletionUsage()

    def complete(self, prompt: str) -> str:
        self.calls += 1
        self.usage.calls += 1
        outcome = self.outcomes.pop(0)
        if outcome == "timeout":
            self.usage.failed_calls += 1
            raise ProviderCallError(
                "timeout",
                provider="test_provider",
                operation="judge",
            )
        self.usage.record_tokens(
            input_tokens=5,
            output_tokens=4,
            total_tokens=9,
        )
        return json.dumps(
            {
                "faithfulness": 0.9,
                "relevance": 0.8,
                "completeness": 0.7,
                "passed": True,
                "comment": "ok",
            }
        )


class _TrackingAssistant(LegalChatAssistant):
    def __init__(self, retriever: _ProviderFreeRetriever) -> None:
        super().__init__(
            retriever,
            model="m2-adapter-offline-fixture",
            top_k=3,
            adaptive_enabled=False,
            adaptive_use_llm=False,
            condense_with_llm=False,
        )
        self.events: list[str] = []
        self.prepared_memory: list[str] = []

    def answer(self, question: str, *, generate: bool = True):
        raise AssertionError("the adapter must not call the legacy answer wrapper")

    def prepare_question(self, question: str) -> PreparedQuestion:
        self.events.append("prepare")
        prepared = super().prepare_question(question)
        self.prepared_memory.append(prepared.memory_text)
        return prepared

    def retrieve_turn(self, prepared: PreparedQuestion) -> RetrievedTurn:
        self.events.append("retrieve")
        return super().retrieve_turn(prepared)

    def generate_turn(
        self,
        retrieved: RetrievedTurn,
        *,
        generate: bool,
    ) -> GeneratedTurn:
        self.events.append("generate")
        return super().generate_turn(retrieved, generate=generate)

    def verify_turn(self, generated: GeneratedTurn) -> VerifiedTurn:
        self.events.append("verify")
        return super().verify_turn(generated)

    def commit_turn(self, verified: VerifiedTurn) -> None:
        self.events.append("commit")
        super().commit_turn(verified)


class _AssistantHarness:
    def __init__(self, retriever: _ProviderFreeRetriever) -> None:
        self.retriever = retriever
        self.assistants: list[_TrackingAssistant] = []
        self.clients: list[_CountingAnswerClient] = []

    def build(self) -> _TrackingAssistant:
        assistant = _TrackingAssistant(self.retriever)
        client = _CountingAnswerClient()
        assistant.llm = client
        self.assistants.append(assistant)
        self.clients.append(client)
        return assistant


class _FailingCompletionClient:
    hidden_retries_disabled = True

    def __init__(self) -> None:
        self.calls = 0
        self.usage = CompletionUsage()

    def complete(self, prompt: str) -> str:
        self.calls += 1
        self.usage.calls += 1
        self.usage.failed_calls += 1
        raise TimeoutError("controlled fixture timeout")


class _InvalidJudgeClient:
    hidden_retries_disabled = True

    def __init__(self) -> None:
        self.calls = 0
        self.usage = CompletionUsage()

    def complete(self, prompt: str) -> str:
        self.calls += 1
        self.usage.calls += 1
        return "not-json"


class _BadLedgerClient(_CountingAnswerClient):
    def complete(self, prompt: str) -> str:
        self.calls += 1
        response = super().complete(prompt)
        self.usage.calls += 1
        return response


class _ScriptedProviderClient:
    hidden_retries_disabled = True

    def __init__(self, script: list[str]) -> None:
        self.script = script
        self.calls = 0
        self.usage = CompletionUsage()

    def complete(self, prompt: str) -> str:
        self.calls += 1
        self.usage.calls += 1
        outcome = self.script.pop(0)
        if outcome != "success":
            self.usage.failed_calls += 1
            raise ProviderCallError(
                outcome,
                provider="test_provider",
                operation="completion",
            ) from RuntimeError("SECRET_PROVIDER_BODY must never be persisted")
        self.usage.record_tokens(
            input_tokens=11,
            output_tokens=7,
            total_tokens=18,
        )
        self.usage.latency_ms += 1.5
        return json.dumps(
            {
                "answer_text": "合成规则第一条要求遵守合成事项 [S1]。",
                "answer_mode": "evidence_answer",
                "claims": [
                    {
                        "claim_id": "C1",
                        "text": "合成规则第一条要求遵守合成事项",
                        "source_ids": ["S1"],
                    }
                ],
                "limitations": [],
                "clarification_question": None,
            },
            ensure_ascii=False,
        )


class _TokenReportingScriptedProviderClient(_ScriptedProviderClient):
    def complete(self, prompt: str) -> str:
        if self.script and self.script[0] != "success":
            self.calls += 1
            self.usage.calls += 1
            self.usage.failed_calls += 1
            self.usage.record_tokens(
                input_tokens=13,
                output_tokens=2,
                total_tokens=15,
            )
            self.usage.latency_ms += 2.25
            outcome = self.script.pop(0)
            raise ProviderCallError(
                outcome,
                provider="test_provider",
                operation="completion",
            )
        return super().complete(prompt)


class _ClientHarness(_AssistantHarness):
    def __init__(
        self,
        retriever: _ProviderFreeRetriever,
        client_factory,
        *,
        assistant_type=_TrackingAssistant,
    ) -> None:
        super().__init__(retriever)
        self.client_factory = client_factory
        self.assistant_type = assistant_type

    def build(self) -> _TrackingAssistant:
        assistant = self.assistant_type(self.retriever)
        client = self.client_factory()
        assistant.llm = client
        self.assistants.append(assistant)
        self.clients.append(client)
        return assistant


class _SlowVerifyAssistant(_TrackingAssistant):
    def verify_turn(self, generated: GeneratedTurn) -> VerifiedTurn:
        time.sleep(0.02)
        return super().verify_turn(generated)


class _AdaptiveMismatchAssistant(_TrackingAssistant):
    def __init__(self, retriever: _ProviderFreeRetriever) -> None:
        super().__init__(retriever)
        self.adaptive_enabled = True


class _AdaptiveProviderAssistant(_TrackingAssistant):
    def __init__(self, retriever: _ProviderFreeRetriever) -> None:
        super().__init__(retriever)
        self.adaptive_enabled = True
        self.adaptive_use_llm = True
        self.normalizer_retries = 1


class _CondensingAssistant(_TrackingAssistant):
    def __init__(self, retriever: _ProviderFreeRetriever) -> None:
        super().__init__(retriever)
        self.condense_with_llm = True


class _ProviderCallingVerifyAssistant(_TrackingAssistant):
    def verify_turn(self, generated: GeneratedTurn) -> VerifiedTurn:
        try:
            self.llm.complete("forbidden verifier request")
        except ExperimentContractError:
            pass
        return super().verify_turn(generated)


class _UnmarkedRetriever(_ProviderFreeRetriever):
    provider_free = False


class _ThreadSafeRetriever(_ProviderFreeRetriever):
    thread_safe = True


class _EmbeddingRetriever(_ProviderFreeRetriever):
    uses_query_embedding = True


class _UnsafeBM25Subclass(BM25Retriever):
    name = "m2-adapter-provider-free"

    def __init__(self) -> None:
        pass

    def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
        return []


def _case(
    case_id: str = "adapter-case",
    *,
    question: str = "《合成规则》第一条规定什么？",
    session_group: str | None = None,
    turn_index: int = 0,
) -> EvalCase:
    return EvalCase(
        case_id=case_id,
        question=question,
        case_type="synthetic",
        expected_law="合成规则",
        expected_articles=["第一条"],
        keywords=["合成事项"],
        expected_behavior="evidence_answer",
        session_group=session_group,
        turn_index=turn_index,
        schema_version=2,
    )


def _manifest(
    cases: list[EvalCase],
    *,
    experiment_id: str = "m2-adapter-test",
    generate: bool = True,
    raw_cases: list[dict[str, Any]] | None = None,
    concurrency: int = 1,
    judge_enabled: bool = False,
    execution_mode: str | None = None,
    max_retries: int = 0,
    provider_timeouts: Mapping[str, float | None] | None = None,
    adaptive_enabled: bool = False,
    adaptive_use_llm: bool = False,
    normalizer_retries: int = 0,
    condense_with_llm: bool = False,
) -> dict[str, Any]:
    manifest_cases = raw_cases if raw_cases is not None else build_manifest_cases(cases)
    provider_limits = {kind: 1 for kind in EXTERNAL_CALL_KINDS}
    return build_experiment_manifest(
        experiment_id=experiment_id,
        execution_mode=execution_mode
        or ("full-regression" if generate else "retrieval"),
        default_cache_mode="fresh",
        code={
            "commit": "a" * 40,
            "dirty": False,
            "stage_implementation_fingerprints": {
                "chunking": "adapter-chunking-v1",
                "query_analysis": "adapter-query-analysis-v1",
                "embedding": "adapter-embedding-v1",
                "retrieval": "adapter-retrieval-v1",
                "rerank": "adapter-rerank-v1",
                "generation": "adapter-generation-v1",
                "verification": "adapter-verification-v1",
                "judge": "adapter-judge-v1",
                "aggregation": "adapter-aggregation-v1",
            },
        },
        config_summary={
            "profile": "offline",
            "top_k": 3,
            "memory_token_limit": 2000,
            "generate": generate,
            "adaptive_enabled": adaptive_enabled,
            "adaptive_use_llm": adaptive_use_llm,
            "adaptive_max_queries": 3,
            "adaptive_per_plan_top_k": None,
            "adaptive_normalizer_retries": normalizer_retries,
            "condense_with_llm": condense_with_llm,
            "provider_timeouts": dict(
                provider_timeouts
                if provider_timeouts is not None
                else {"assistant": None, "judge": None, "adaptive": None}
            ),
        },
        corpus={
            "snapshot_hash": canonical_hash("m2-adapter-corpus"),
            "index_hash": canonical_hash("m2-adapter-index"),
        },
        dataset={
            "dataset_id": "synthetic-m2-adapter",
            "role": "test_fixture",
            "case_file_hash": canonical_hash(manifest_cases),
            "case_schema_version": 2,
            "case_count": len(manifest_cases),
            "case_set_hash": canonical_hash(manifest_cases),
            "cases": manifest_cases,
        },
        contracts={
            "chunking": {"strategy": "article", "version": "article-v1"},
            "embedding": {
                "model": "provider-free-fixture",
                "revision": "embedding-v1",
                "dimension": 3,
                "normalized": True,
                "query_text_version": "query-v1",
                "document_text_version": "document-v1",
            },
            "retrieval": {
                "kind": "m2-adapter-provider-free",
                "parameters": {"top_k": 3},
                "filters": {},
                "scope": {
                    "configured": False,
                    "snapshot_id": None,
                    "allowed_scope_ids": None,
                },
            },
            "rerank": {"enabled": False, "config": {}},
            "generation": {
                "model": "m2-adapter-offline-fixture",
                "revision": "generation-v1",
                "prompt_version": "prompt-v1",
                "parameters": {"temperature": 0},
            },
            "verification": {"schema_version": 2, "rules_version": "m1"},
            "judge": (
                {
                    "enabled": True,
                    "model": "m2-adapter-judge-fixture",
                    "revision": "judge-v1",
                    "prompt_version": "judge-prompt-v1",
                    "rules_version": "judge-rules-v1",
                }
                if judge_enabled
                else {"enabled": False}
            ),
        },
        runtime={
            "random_seed": 42,
            "concurrency": concurrency,
            "max_retries": max_retries,
            "timing_scope": "runner_stage_wall_clock",
            "provider_limits": provider_limits,
            "retry_backoff_ms": 0,
        },
        environment={"python": "3.12", "platform": "test"},
        created_at="2026-09-20T00:00:00Z",
    )


def _spec(
    manifest: dict[str, Any],
    cache: ExactStageCache,
    harness: _AssistantHarness,
    *,
    generate: bool = True,
) -> EvaluationRuntimeSpec:
    return EvaluationRuntimeSpec(
        manifest=manifest,
        cache=cache,
        retriever=harness.retriever,
        assistant_factory=harness.build,
        model="m2-adapter-offline-fixture",
        chunk_strategy="article",
        generate=generate,
        retriever_provider_free=True,
        trace_metadata={"fixture": {"name": "m2-adapter"}},
    )


def _run(
    root: Path,
    manifest: dict[str, Any],
    factory: LegalEvaluationRuntimeFactory,
    *,
    cache_mode: str,
    stop_after_completed: int | None = None,
):
    store = ExperimentStore.create(root, manifest)
    runner = ExperimentRunner(
        store=store,
        requested_manifest=manifest,
        runtime_factory=factory,
        cache_mode=cache_mode,
        execution_environment={"python": "3.12", "platform": "test"},
    )
    summary = runner.run(stop_after_completed=stop_after_completed)
    return store, summary


def _completed_result(store: ExperimentStore, case_id: str) -> dict[str, Any]:
    return store.load_completed(case_id)["result"]


def _total_client_calls(harness: _AssistantHarness) -> int:
    return sum(client.calls for client in harness.clients)


def _actual_provider_attempts(result: dict[str, Any]) -> int:
    return sum(item["attempted"] for item in result["call_ledger"]["actual"].values())


def test_manifest_case_builder_binds_the_complete_evaluation_case() -> None:
    case = _case()

    payload = build_manifest_case(case, ordinal=0)

    assert payload == build_manifest_cases([case])[0]
    assert payload["case_hash"] == canonical_hash(eval_case_to_artifact(case))
    assert payload["evaluation_case"] == eval_case_to_artifact(case)
    assert payload["session_group"] is None
    assert payload["turn_index"] == 0


@pytest.mark.parametrize(
    ("field_name", "field_value", "message"),
    (
        ("model", "wrong-model", "generation.model"),
        ("chunk_strategy", "wrong-strategy", "chunking.strategy"),
        ("generate", False, "config.summary.generate"),
    ),
)
def test_runtime_spec_cannot_drift_from_manifest_identity(
    tmp_path: Path,
    field_name: str,
    field_value: Any,
    message: str,
) -> None:
    case = _case()
    manifest = _manifest([case], experiment_id=f"m2-adapter-drift-{field_name}")
    harness = _AssistantHarness(_ProviderFreeRetriever())
    spec = _spec(manifest, ExactStageCache(tmp_path / "cache"), harness)

    with pytest.raises(ExperimentContractError, match=message):
        LegalEvaluationRuntimeFactory(replace(spec, **{field_name: field_value}))

    assert harness.assistants == []
    assert harness.clients == []


def test_fresh_runtime_uses_real_stages_once_without_legacy_wrapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case()
    manifest = _manifest([case])
    retriever = _ProviderFreeRetriever()
    harness = _AssistantHarness(retriever)
    cache = ExactStageCache(tmp_path / "cache")

    def forbidden(*args, **kwargs):
        raise AssertionError("the adapter must not call evaluate_case")

    monkeypatch.setattr(evaluation_module, "evaluate_case", forbidden)
    factory = LegalEvaluationRuntimeFactory(_spec(manifest, cache, harness))

    store, summary = _run(
        tmp_path / "fresh-artifacts",
        manifest,
        factory,
        cache_mode="fresh",
    )

    assert summary.status == "succeeded"
    assert retriever.queries == [case.question]
    assert _total_client_calls(harness) == 1
    assert len(harness.assistants) == 1
    assistant = harness.assistants[0]
    assert assistant.events.count("prepare") == 1
    assert assistant.events.count("retrieve") == 1
    assert assistant.events.count("generate") == 1
    assert assistant.events.count("commit") == 1
    assert assistant.events.count("verify") >= 2
    assert len(assistant.memory.messages) == 2

    result = _completed_result(store, case.case_id)
    evaluated = decode_evaluation_output(result["output"])
    assert evaluated.record.case_id == case.case_id
    assert evaluated.record.answer
    assert result["error"] is None
    assert _actual_provider_attempts(result) == 1
    assert (
        result["stage_observations"]["generation"]["source_external_calls"][
            "generation"
        ]
        == 1
    )


def test_every_runner_observation_is_explicit_for_the_real_adapter(
    tmp_path: Path,
) -> None:
    case = _case()
    manifest = _manifest([case], experiment_id="m2-adapter-observations")
    harness = _AssistantHarness(_ProviderFreeRetriever())
    factory = LegalEvaluationRuntimeFactory(
        _spec(manifest, ExactStageCache(tmp_path / "cache"), harness)
    )

    store, summary = _run(
        tmp_path / "artifacts",
        manifest,
        factory,
        cache_mode="fresh",
    )

    assert summary.status == "succeeded"
    observations = _completed_result(store, case.case_id)["stage_observations"]
    assert set(observations) == set(OBSERVATION_STAGES)
    assert observations["query_analysis"]["status"] == "succeeded"
    assert observations["retrieval"]["status"] == "succeeded"
    assert observations["generation"]["status"] == "succeeded"
    assert observations["verification"]["status"] == "succeeded"
    assert observations["query_embedding"]["status"] == "not_run"
    assert observations["rerank"]["status"] == "not_run"
    assert observations["judge"]["status"] == "not_run"
    assert all(
        observation["status"] != "not_run" or observation["unavailable_reason"]
        for observation in observations.values()
    )


def test_evaluation_output_round_trips_and_rejects_tampering(tmp_path: Path) -> None:
    case = _case()
    manifest = _manifest([case], experiment_id="m2-adapter-output")
    harness = _AssistantHarness(_ProviderFreeRetriever())
    factory = LegalEvaluationRuntimeFactory(
        _spec(manifest, ExactStageCache(tmp_path / "cache"), harness)
    )
    store, summary = _run(
        tmp_path / "artifacts",
        manifest,
        factory,
        cache_mode="fresh",
    )
    assert summary.status == "succeeded"
    output = _completed_result(store, case.case_id)["output"]

    first = decode_evaluation_output(output)
    second = decode_evaluation_output(deepcopy(output))

    assert asdict(first.record) == asdict(second.record)
    assert first.trace_record == second.trace_record

    unknown = deepcopy(output)
    unknown["unknown"] = True
    with pytest.raises((ValueError, ExperimentContractError)):
        decode_evaluation_output(unknown)

    missing = deepcopy(output)
    missing.pop(next(iter(missing)))
    with pytest.raises((ValueError, ExperimentContractError)):
        decode_evaluation_output(missing)

    changed_facts = deepcopy(output)
    changed_facts["scoring_facts"]["answer"] = "tampered answer"
    with pytest.raises(
        ExperimentContractError,
        match="scoring facts checksum",
    ):
        decode_evaluation_output(changed_facts)

    changed_record = deepcopy(output)
    changed_record["record"]["evaluation_record"]["answer"] = "tampered answer"
    with pytest.raises(ExperimentContractError, match="record disagrees"):
        decode_evaluation_output(changed_record)

    changed_trace = deepcopy(output)
    changed_trace["trace_record"]["query"] = "tampered question"
    with pytest.raises(ExperimentContractError, match="trace disagrees"):
        decode_evaluation_output(changed_trace)

    changed_checksum = deepcopy(output)
    changed_checksum["result_sha256"] = "0" * 64
    with pytest.raises(ExperimentContractError, match="output checksum"):
        decode_evaluation_output(changed_checksum)


def test_cache_and_replay_use_no_provider_but_reverify_and_commit(
    tmp_path: Path,
) -> None:
    case = _case()
    manifest = _manifest([case], experiment_id="m2-adapter-cache-replay")
    cache = ExactStageCache(tmp_path / "cache")
    runs: dict[str, tuple[_AssistantHarness, dict[str, Any]]] = {}

    for cache_mode in ("fresh", "cache", "replay"):
        harness = _AssistantHarness(_ProviderFreeRetriever())
        factory = LegalEvaluationRuntimeFactory(_spec(manifest, cache, harness))
        store, summary = _run(
            tmp_path / f"{cache_mode}-artifacts",
            manifest,
            factory,
            cache_mode=cache_mode,
        )
        assert summary.status == "succeeded"
        result = _completed_result(store, case.case_id)
        runs[cache_mode] = (harness, result)

    fresh_harness, fresh_result = runs["fresh"]
    assert _total_client_calls(fresh_harness) == 1
    assert _actual_provider_attempts(fresh_result) == 1

    for cache_mode in ("cache", "replay"):
        harness, result = runs[cache_mode]
        assistant = harness.assistants[0]
        assert _total_client_calls(harness) == 0
        assert harness.retriever.queries == []
        assert _actual_provider_attempts(result) == 0
        assert assistant.events.count("commit") == 1
        assert assistant.events.count("verify") >= 2
        assert len(assistant.memory.messages) == 2
        for stage in (
            "query_analysis",
            "retrieval",
            "generation",
            "verification",
        ):
            assert result["stage_observations"][stage]["origin"] == cache_mode
        assert (
            result["stage_observations"]["generation"]["source_external_calls"][
                "generation"
            ]
            == 1
        )
        assert decode_evaluation_output(result["output"]).record.answer


def test_two_turn_session_resumes_real_conversation_memory_after_interruption(
    tmp_path: Path,
) -> None:
    cases = [
        _case("turn-1", session_group="session-a", turn_index=0),
        _case(
            "turn-2",
            question="那具体应当遵守什么？",
            session_group="session-a",
            turn_index=1,
        ),
    ]
    manifest = _manifest(cases, experiment_id="m2-adapter-session-resume")
    cache = ExactStageCache(tmp_path / "cache")
    first_harness = _AssistantHarness(_ProviderFreeRetriever())
    first_factory = LegalEvaluationRuntimeFactory(_spec(manifest, cache, first_harness))
    artifact_root = tmp_path / "artifacts"

    first_store, stopped = _run(
        artifact_root,
        manifest,
        first_factory,
        cache_mode="fresh",
        stop_after_completed=1,
    )

    assert stopped.status == "interrupted"
    first_result = _completed_result(first_store, "turn-1")
    first_state = first_result["session_checkpoint"]["state_after"]
    assert len(first_state["memory"]["messages"]) == 2

    resumed_harness = _AssistantHarness(_ProviderFreeRetriever())
    resumed_factory = LegalEvaluationRuntimeFactory(
        _spec(manifest, cache, resumed_harness)
    )
    resumed_store, resumed = _run(
        artifact_root,
        manifest,
        resumed_factory,
        cache_mode="fresh",
    )

    assert resumed.status == "succeeded"
    assert resumed.skipped_case_ids == ("turn-1",)
    assert len(resumed_harness.assistants) == 1
    assistant = resumed_harness.assistants[0]
    assert len(assistant.memory.messages) == 4
    assert assistant.prepared_memory
    assert cases[0].question in assistant.prepared_memory[0]
    assert "合成规则第一条要求遵守合成事项" in assistant.prepared_memory[0]

    second_result = _completed_result(resumed_store, "turn-2")
    assert second_result["session_checkpoint"]["state_before_sha256"] == canonical_hash(
        first_state
    )
    assert (
        len(second_result["session_checkpoint"]["state_after"]["memory"]["messages"])
        == 4
    )


def test_retrieval_only_persists_empty_answer_without_committing_memory(
    tmp_path: Path,
) -> None:
    case = _case()
    manifest = _manifest(
        [case],
        experiment_id="m2-adapter-retrieval-only",
        generate=False,
    )
    harness = _AssistantHarness(_ProviderFreeRetriever())
    factory = LegalEvaluationRuntimeFactory(
        _spec(
            manifest,
            ExactStageCache(tmp_path / "cache"),
            harness,
            generate=False,
        )
    )

    store, summary = _run(
        tmp_path / "artifacts",
        manifest,
        factory,
        cache_mode="fresh",
    )

    assert summary.status == "succeeded"
    assert _total_client_calls(harness) == 0
    assistant = harness.assistants[0]
    assert assistant.events.count("commit") == 0
    assert assistant.memory.messages == []
    result = _completed_result(store, case.case_id)
    evaluated = decode_evaluation_output(result["output"])
    assert evaluated.record.answer == ""
    assert evaluated.trace_record["final_response"] == {
        "value": None,
        "unavailable_reason": "retrieval_only",
    }
    assert result["stage_observations"]["generation"]["status"] == "not_run"
    assert result["stage_observations"]["verification"]["status"] == "not_run"


def test_manifest_case_mismatch_is_rejected_before_assistant_construction(
    tmp_path: Path,
) -> None:
    case = _case()
    mismatched = build_manifest_case(case, ordinal=0)
    mismatched["case_id"] = "tampered-case-id"
    manifest = _manifest(
        [case],
        experiment_id="m2-adapter-manifest-mismatch",
        raw_cases=[mismatched],
    )
    retriever = _ProviderFreeRetriever()
    harness = _AssistantHarness(retriever)
    spec = _spec(manifest, ExactStageCache(tmp_path / "cache"), harness)

    with pytest.raises(ExperimentContractError, match="identity"):
        LegalEvaluationRuntimeFactory(spec)

    assert harness.assistants == []
    assert harness.clients == []
    assert retriever.queries == []


def test_replay_cache_miss_fails_without_committing_memory(tmp_path: Path) -> None:
    case = _case()
    manifest = _manifest([case], experiment_id="m2-adapter-cache-miss")
    harness = _AssistantHarness(_ProviderFreeRetriever())
    factory = LegalEvaluationRuntimeFactory(
        _spec(manifest, ExactStageCache(tmp_path / "empty-cache"), harness)
    )

    store, summary = _run(
        tmp_path / "artifacts",
        manifest,
        factory,
        cache_mode="replay",
    )

    assert summary.status == "completed_with_failures"
    assert _total_client_calls(harness) == 0
    assert harness.retriever.queries == []
    assert len(harness.assistants) == 1
    assert harness.assistants[0].events.count("commit") == 0
    assert harness.assistants[0].memory.messages == []
    attempt = store.load_attempts(case.case_id)[0]
    assert attempt["status"] == "failed"
    assert attempt["result"]["output"] is None


def test_corrupt_cached_stage_fails_without_committing_memory(tmp_path: Path) -> None:
    case = _case()
    manifest = _manifest([case], experiment_id="m2-adapter-cache-corruption")
    cache_root = tmp_path / "cache"
    cache = ExactStageCache(cache_root)
    fresh_harness = _AssistantHarness(_ProviderFreeRetriever())
    fresh_factory = LegalEvaluationRuntimeFactory(_spec(manifest, cache, fresh_harness))
    _, fresh = _run(
        tmp_path / "fresh-artifacts",
        manifest,
        fresh_factory,
        cache_mode="fresh",
    )
    assert fresh.status == "succeeded"

    generation_entries = list((cache_root / "generation").glob("*.json"))
    assert len(generation_entries) == 1
    generation_entries[0].write_text("{not-valid-json", encoding="utf-8")

    replay_harness = _AssistantHarness(_ProviderFreeRetriever())
    replay_factory = LegalEvaluationRuntimeFactory(
        _spec(manifest, cache, replay_harness)
    )
    store, replay = _run(
        tmp_path / "replay-artifacts",
        manifest,
        replay_factory,
        cache_mode="replay",
    )

    assert replay.status == "completed_with_failures"
    assert _total_client_calls(replay_harness) == 0
    assert len(replay_harness.assistants) == 1
    assistant = replay_harness.assistants[0]
    assert assistant.events.count("commit") == 0
    assert assistant.memory.messages == []
    attempt = store.load_attempts(case.case_id)[0]
    assert attempt["status"] == "failed"
    assert attempt["result"]["output"] is None


def test_provider_timeout_is_a_failed_attempt_and_is_not_cached(
    tmp_path: Path,
) -> None:
    case = _case()
    manifest = _manifest([case], experiment_id="m2-adapter-generation-timeout")
    cache = ExactStageCache(tmp_path / "cache")
    harness = _ClientHarness(
        _ProviderFreeRetriever(),
        _FailingCompletionClient,
    )
    store, summary = _run(
        tmp_path / "artifacts",
        manifest,
        LegalEvaluationRuntimeFactory(_spec(manifest, cache, harness)),
        cache_mode="fresh",
    )

    assert summary.status == "completed_with_failures"
    attempt = store.load_attempts(case.case_id)[0]
    result = attempt["result"]
    assert attempt["status"] == "failed"
    assert result["output"] is None
    assert result["error"] == {
        "code": "provider_timeout",
        "retryable": True,
    }
    assert set(result["stage_observations"]) == set(OBSERVATION_STAGES)
    observation = result["stage_observations"]["generation"]
    assert observation["status"] == "error"
    assert observation["error_code"] == "provider_timeout"
    assert observation["origin"] == "fresh"
    assert observation["source_external_calls"]["generation"] == 0
    assert result["stage_observations"]["verification"]["unavailable_reason"] == (
        "upstream_provider_failure"
    )
    actual = result["call_ledger"]["actual"]["generation"]
    assert actual["attempted"] == 1
    assert actual["failed"] == 1
    assert list((tmp_path / "cache" / "generation").glob("*.json")) == []
    assert harness.assistants[0].events.count("commit") == 0
    assert harness.assistants[0].memory.messages == []


@pytest.mark.parametrize(
    "error_code",
    ["timeout", "rate_limited", "network", "unavailable"],
)
def test_retryable_provider_failure_uses_a_fresh_runtime_then_succeeds(
    tmp_path: Path,
    error_code: str,
) -> None:
    case = _case(session_group=f"retry-{error_code}", turn_index=0)
    manifest = _manifest(
        [case],
        experiment_id=f"m2-adapter-provider-retry-{error_code}",
        max_retries=1,
    )
    script = [error_code, "success"]
    harness = _ClientHarness(
        _ProviderFreeRetriever(),
        lambda: _ScriptedProviderClient(script),
    )
    cache = ExactStageCache(tmp_path / "cache")

    store, summary = _run(
        tmp_path / "artifacts",
        manifest,
        LegalEvaluationRuntimeFactory(_spec(manifest, cache, harness)),
        cache_mode="fresh",
    )

    attempts = store.load_attempts(case.case_id)
    assert summary.status == "succeeded"
    assert [attempt["status"] for attempt in attempts] == ["failed", "succeeded"]
    assert [attempt["cache_mode"] for attempt in attempts] == ["fresh", "cache"]
    assert len(harness.assistants) == 2
    assert len({id(assistant) for assistant in harness.assistants}) == 2
    assert len({id(client) for client in harness.clients}) == 2
    assert attempts[0]["result"]["error"] == {
        "code": f"provider_{error_code}",
        "retryable": True,
    }
    assert (
        attempts[0]["result"]["session_checkpoint"]["state_before_sha256"]
        == attempts[1]["result"]["session_checkpoint"]["state_before_sha256"]
    )
    assert attempts[0]["result"]["session_checkpoint"]["state_after"] is None
    assert attempts[1]["result"]["session_checkpoint"]["state_after"] is not None
    failed_generation = attempts[0]["result"]["stage_observations"]["generation"]
    assert failed_generation["status"] == "error"
    assert failed_generation["origin"] == "fresh"
    assert failed_generation["error_code"] == f"provider_{error_code}"
    failed_ledger = attempts[0]["result"]["call_ledger"]["actual"]["generation"]
    assert failed_ledger["attempted"] == 1
    assert failed_ledger["succeeded"] == 0
    assert failed_ledger["failed"] == 1
    assert "SECRET_PROVIDER_BODY" not in json.dumps(attempts[0], ensure_ascii=False)
    assert harness.assistants[0].events.count("commit") == 0
    assert harness.assistants[0].memory.messages == []
    assert harness.assistants[1].events.count("commit") == 1
    assert len(harness.assistants[1].memory.messages) == 2
    completed = _completed_result(store, case.case_id)
    assert completed["output"] is not None
    assert completed["stage_observations"]["query_analysis"]["origin"] == "cache"
    assert completed["stage_observations"]["retrieval"]["origin"] == "cache"
    assert completed["stage_observations"]["generation"]["origin"] == "fresh"
    assert completed["call_ledger"]["actual"]["generation"]["attempted"] == 1
    assert len(list((tmp_path / "cache" / "generation").glob("*.json"))) == 1


def test_failed_attempt_persists_reported_tokens_before_retry(
    tmp_path: Path,
) -> None:
    case = _case(case_id="adapter-tokenized-retry")
    manifest = _manifest(
        [case],
        experiment_id="m2-adapter-tokenized-retry",
        max_retries=1,
    )
    script = ["timeout", "success"]
    harness = _ClientHarness(
        _ProviderFreeRetriever(),
        lambda: _TokenReportingScriptedProviderClient(script),
    )
    store, summary = _run(
        tmp_path / "artifacts",
        manifest,
        LegalEvaluationRuntimeFactory(
            _spec(manifest, ExactStageCache(tmp_path / "cache"), harness)
        ),
        cache_mode="fresh",
    )

    assert summary.status == "succeeded"
    failed, succeeded = store.load_attempts(case.case_id)
    assert failed["status"] == "failed"
    assert failed["result"]["model_usage"]["assistant"] == {
        "calls": 1,
        "failed_calls": 1,
        "input_tokens": 13,
        "output_tokens": 2,
        "total_tokens": 15,
        "token_usage_calls": 1,
        "latency_ms": 2.25,
    }
    assert failed["result"]["model_usage"]["normalizer"]["calls"] == 0
    assert failed["result"]["model_usage"]["judge"]["calls"] == 0
    failed_calls = failed["result"]["call_ledger"]["actual"]["generation"]
    assert failed_calls["attempted"] == 1
    assert failed_calls["succeeded"] == 0
    assert failed_calls["failed"] == 1
    assert succeeded["status"] == "succeeded"
    assert succeeded["result"]["model_usage"]["assistant"] == {
        "calls": 1,
        "failed_calls": 0,
        "input_tokens": 11,
        "output_tokens": 7,
        "total_tokens": 18,
        "token_usage_calls": 1,
        "latency_ms": 1.5,
    }
    output_usage = succeeded["result"]["output"]["scoring_facts"]["assistant_usage"]
    assert succeeded["result"]["model_usage"]["assistant"] == output_usage

    def forbidden_factory(*args, **kwargs):
        raise AssertionError("completed retry history must resume without a runtime")

    reopened = ExperimentStore.open(tmp_path / "artifacts", manifest["experiment_id"])
    resumed = ExperimentRunner(
        store=reopened,
        requested_manifest=manifest,
        runtime_factory=forbidden_factory,
        cache_mode="fresh",
        execution_environment={"python": "3.12", "platform": "test"},
    ).run()
    assert resumed.status == "succeeded"
    assert resumed.skipped_case_ids == (case.case_id,)


@pytest.mark.parametrize(
    "error_code",
    ["auth", "invalid_request", "configuration", "invalid_response", "unknown"],
)
def test_nonretryable_provider_failure_does_not_consume_retry_budget(
    tmp_path: Path,
    error_code: str,
) -> None:
    case = _case(case_id=f"adapter-{error_code}")
    manifest = _manifest(
        [case],
        experiment_id=f"m2-adapter-provider-{error_code}",
        max_retries=3,
    )
    script = [error_code, "success"]
    harness = _ClientHarness(
        _ProviderFreeRetriever(),
        lambda: _ScriptedProviderClient(script),
    )

    store, summary = _run(
        tmp_path / "artifacts",
        manifest,
        LegalEvaluationRuntimeFactory(
            _spec(manifest, ExactStageCache(tmp_path / "cache"), harness)
        ),
        cache_mode="cache",
    )

    attempts = store.load_attempts(case.case_id)
    assert summary.status == "completed_with_failures"
    assert len(attempts) == 1
    assert len(harness.assistants) == 1
    assert script == ["success"]
    assert attempts[0]["result"]["error"] == {
        "code": f"provider_{error_code}",
        "retryable": False,
    }
    assert attempts[0]["result"]["call_ledger"]["actual"]["generation"]["failed"] == 1
    assert harness.assistants[0].events.count("commit") == 0
    assert list((tmp_path / "cache" / "generation").glob("*.json")) == []


def test_session_condense_provider_failure_is_attributed_to_query_analysis(
    tmp_path: Path,
) -> None:
    first = _case(case_id="condense-0", session_group="condense", turn_index=0)
    second = _case(
        case_id="condense-1",
        question="这个具体是什么意思？",
        session_group="condense",
        turn_index=1,
    )
    manifest = _manifest(
        [first, second],
        experiment_id="m2-adapter-condense-timeout",
        condense_with_llm=True,
    )
    script = ["success", "timeout"]
    harness = _ClientHarness(
        _ProviderFreeRetriever(),
        lambda: _ScriptedProviderClient(script),
        assistant_type=_CondensingAssistant,
    )
    cache = ExactStageCache(tmp_path / "cache")

    store, summary = _run(
        tmp_path / "artifacts",
        manifest,
        LegalEvaluationRuntimeFactory(_spec(manifest, cache, harness)),
        cache_mode="cache",
    )

    failed = store.load_attempts(second.case_id)[0]["result"]
    assert summary.status == "completed_with_failures"
    assert summary.inventory.succeeded == (first.case_id,)
    assert failed["error"] == {"code": "provider_timeout", "retryable": True}
    query_analysis = failed["stage_observations"]["query_analysis"]
    assert query_analysis["status"] == "error"
    assert query_analysis["origin"] == "fresh"
    assert query_analysis["error_code"] == "provider_timeout"
    assert failed["call_ledger"]["actual"]["normalizer"]["attempted"] == 1
    assert failed["call_ledger"]["actual"]["normalizer"]["failed"] == 1
    assert failed["stage_observations"]["retrieval"]["unavailable_reason"] == (
        "upstream_provider_failure"
    )
    assert len(list((tmp_path / "cache" / "query_analysis").glob("*.json"))) == 1
    assert len(harness.assistants) == 1
    assert harness.assistants[0].events.count("commit") == 1
    assert len(harness.assistants[0].memory.messages) == 2


def test_retrieval_only_adaptive_provider_failure_is_attributed_to_retrieval(
    tmp_path: Path,
) -> None:
    case = _case(
        question="商家不退押金，另外还把我的照片拿去宣传，这些分别有什么依据？"
    )
    manifest = _manifest(
        [case],
        experiment_id="m2-adapter-adaptive-timeout",
        generate=False,
        execution_mode="retrieval",
        adaptive_enabled=True,
        adaptive_use_llm=True,
        normalizer_retries=1,
    )
    harness = _ClientHarness(
        _ProviderFreeRetriever(),
        _CountingAnswerClient,
        assistant_type=_AdaptiveProviderAssistant,
    )
    adaptive_script = ["timeout", "success"]
    adaptive_clients: list[_ScriptedProviderClient] = []

    def build_adaptive() -> _ScriptedProviderClient:
        client = _ScriptedProviderClient(adaptive_script)
        adaptive_clients.append(client)
        return client

    cache = ExactStageCache(tmp_path / "cache")
    spec = replace(
        _spec(manifest, cache, harness, generate=False),
        adaptive_llm_client_factory=build_adaptive,
    )
    store, summary = _run(
        tmp_path / "artifacts",
        manifest,
        LegalEvaluationRuntimeFactory(spec),
        cache_mode="cache",
    )

    result = store.load_attempts(case.case_id)[0]["result"]
    assert summary.status == "completed_with_failures"
    assert adaptive_script == ["success"]
    assert len(adaptive_clients) == 1
    assert result["error"] == {"code": "provider_timeout", "retryable": True}
    assert result["stage_observations"]["query_analysis"]["status"] == "succeeded"
    retrieval = result["stage_observations"]["retrieval"]
    assert retrieval["status"] == "error"
    assert retrieval["origin"] == "fresh"
    assert retrieval["error_code"] == "provider_timeout"
    assert result["call_ledger"]["actual"]["normalizer"]["attempted"] == 1
    assert result["call_ledger"]["actual"]["normalizer"]["failed"] == 1
    assert result["stage_observations"]["generation"]["unavailable_reason"] == (
        "upstream_provider_failure"
    )
    assert list((tmp_path / "cache" / "retrieval").glob("*.json")) == []


def test_judge_degradation_is_explicit_across_cache_modes(tmp_path: Path) -> None:
    case = _case()
    manifest = _manifest(
        [case],
        experiment_id="m2-adapter-judge-degraded",
        judge_enabled=True,
    )
    cache = ExactStageCache(tmp_path / "cache")

    for cache_mode in ("fresh", "cache", "replay"):
        harness = _AssistantHarness(_ProviderFreeRetriever())
        judge_clients: list[_InvalidJudgeClient] = []

        def build_judge() -> _InvalidJudgeClient:
            client = _InvalidJudgeClient()
            judge_clients.append(client)
            return client

        spec = replace(
            _spec(manifest, cache, harness),
            judge_client_factory=build_judge,
        )
        store, summary = _run(
            tmp_path / f"{cache_mode}-artifacts",
            manifest,
            LegalEvaluationRuntimeFactory(spec),
            cache_mode=cache_mode,
        )

        assert summary.status == "succeeded"
        result = _completed_result(store, case.case_id)
        observation = result["stage_observations"]["judge"]
        assert observation["status"] == "error"
        assert observation["error_code"] == "invalid_json"
        assert observation["origin"] == cache_mode
        assert observation["source_external_calls"]["judge"] == 1
        assert result["output"]["identity"]["stages"]["judge"]["status"] == ("error")
        assert sum(client.calls for client in judge_clients) == (
            1 if cache_mode == "fresh" else 0
        )


def test_judge_provider_failure_is_not_cached_or_downgraded_to_judge_result(
    tmp_path: Path,
) -> None:
    case = _case()
    manifest = _manifest(
        [case],
        experiment_id="m2-adapter-judge-provider-timeout",
        judge_enabled=True,
    )
    harness = _AssistantHarness(_ProviderFreeRetriever())
    judge_script = ["timeout"]
    judge_clients: list[_ScriptedProviderClient] = []

    def build_judge() -> _ScriptedProviderClient:
        client = _ScriptedProviderClient(judge_script)
        judge_clients.append(client)
        return client

    cache = ExactStageCache(tmp_path / "cache")
    spec = replace(
        _spec(manifest, cache, harness),
        judge_client_factory=build_judge,
    )
    store, summary = _run(
        tmp_path / "artifacts",
        manifest,
        LegalEvaluationRuntimeFactory(spec),
        cache_mode="cache",
    )

    attempt = store.load_attempts(case.case_id)[0]
    result = attempt["result"]
    assert summary.status == "completed_with_failures"
    assert attempt["status"] == "failed"
    assert result["error"] == {
        "code": "provider_timeout",
        "retryable": True,
    }
    assert result["output"] is None
    assert result["stage_observations"]["generation"]["status"] == "succeeded"
    judge = result["stage_observations"]["judge"]
    assert judge["status"] == "error"
    assert judge["origin"] == "fresh"
    assert judge["error_code"] == "provider_timeout"
    assert judge["source_external_calls"]["judge"] == 0
    assert result["call_ledger"]["actual"]["judge"]["attempted"] == 1
    assert result["call_ledger"]["actual"]["judge"]["failed"] == 1
    assert list((tmp_path / "cache" / "judge").glob("*.json")) == []
    assert len(judge_clients) == 1
    assert harness.assistants[0].events.count("commit") == 0


def test_fresh_retry_reuses_successful_upstream_stage_before_retrying_judge(
    tmp_path: Path,
) -> None:
    case = _case()
    manifest = _manifest(
        [case],
        experiment_id="m2-adapter-fresh-retry-cache",
        judge_enabled=True,
        max_retries=1,
    )
    answers = ["第一次生成的答案", "不应执行的第二次生成答案"]
    harness = _ClientHarness(
        _ProviderFreeRetriever(),
        lambda: _SequencedAnswerClient(answers),
    )
    judge_outcomes = ["timeout", "success"]
    judge_clients: list[_SequencedJudgeClient] = []

    def build_judge() -> _SequencedJudgeClient:
        client = _SequencedJudgeClient(judge_outcomes)
        judge_clients.append(client)
        return client

    cache = ExactStageCache(tmp_path / "cache")
    spec = replace(
        _spec(manifest, cache, harness),
        judge_client_factory=build_judge,
    )
    store, summary = _run(
        tmp_path / "artifacts",
        manifest,
        LegalEvaluationRuntimeFactory(spec),
        cache_mode="fresh",
    )

    attempts = store.load_attempts(case.case_id)
    assert summary.status == "succeeded"
    assert [attempt["status"] for attempt in attempts] == ["failed", "succeeded"]
    assert [attempt["cache_mode"] for attempt in attempts] == ["fresh", "cache"]
    assert attempts[0]["result"]["error"] == {
        "code": "provider_timeout",
        "retryable": True,
    }
    assert attempts[0]["result"]["stage_observations"]["generation"]["origin"] == (
        "fresh"
    )
    retried_generation = attempts[1]["result"]["stage_observations"]["generation"]
    assert retried_generation["origin"] == "cache"
    assert retried_generation["external_calls"]["generation"] == 0
    assert retried_generation["source_external_calls"]["generation"] == 1
    assert sum(client.calls for client in harness.clients) == 1
    assert answers == ["不应执行的第二次生成答案"]
    assert sum(client.calls for client in judge_clients) == 2
    assert judge_outcomes == []
    assert (
        "第一次生成的答案"
        in decode_evaluation_output(
            _completed_result(store, case.case_id)["output"]
        ).record.answer
    )
    assert len(list((tmp_path / "cache" / "generation").glob("*.json"))) == 1

    replay_harness = _AssistantHarness(_ProviderFreeRetriever())
    replay_judge_outcomes = ["must-not-run"]
    replay_judge_clients: list[_SequencedJudgeClient] = []

    def build_replay_judge() -> _SequencedJudgeClient:
        client = _SequencedJudgeClient(replay_judge_outcomes)
        replay_judge_clients.append(client)
        return client

    replay_spec = replace(
        _spec(manifest, cache, replay_harness),
        judge_client_factory=build_replay_judge,
    )
    replay_store, replay_summary = _run(
        tmp_path / "replay-artifacts",
        manifest,
        LegalEvaluationRuntimeFactory(replay_spec),
        cache_mode="replay",
    )
    assert replay_summary.status == "succeeded"
    assert _total_client_calls(replay_harness) == 0
    assert sum(client.calls for client in replay_judge_clients) == 0
    assert replay_judge_outcomes == ["must-not-run"]
    replay_result = _completed_result(replay_store, case.case_id)
    assert (
        "第一次生成的答案"
        in decode_evaluation_output(replay_result["output"]).record.answer
    )
    assert all(
        item["attempted"] == 0
        for item in replay_result["call_ledger"]["actual"].values()
    )


def test_output_identity_cross_checks_manifest_case_stages_schema_and_raw_data(
    tmp_path: Path,
) -> None:
    case = _case()
    manifest = _manifest([case], experiment_id="m2-adapter-bound-output")
    harness = _AssistantHarness(_ProviderFreeRetriever())
    store, summary = _run(
        tmp_path / "artifacts",
        manifest,
        LegalEvaluationRuntimeFactory(
            _spec(manifest, ExactStageCache(tmp_path / "cache"), harness)
        ),
        cache_mode="fresh",
    )
    assert summary.status == "succeeded"
    output = _completed_result(store, case.case_id)["output"]
    stages = output["identity"]["stages"]

    decoded = decode_evaluation_output(
        output,
        expected_manifest=manifest,
        expected_case=case,
        expected_stage_identity=stages,
    )
    assert decoded.record.case_id == case.case_id
    assert output["evaluation_output_schema_version"] == 2
    assert output["identity"]["manifest_hash"] == manifest["identity"]["manifest_hash"]
    assert output["identity"]["case_artifact_sha256"] == canonical_hash(
        eval_case_to_artifact(case)
    )
    for stage in OBSERVATION_STAGES:
        assert set(stages[stage]) == {"status", "cache_key", "payload_sha256"}

    other_manifest = _manifest([case], experiment_id="other-experiment")
    with pytest.raises(ExperimentContractError, match="manifest identity"):
        decode_evaluation_output(output, expected_manifest=other_manifest)
    with pytest.raises(ExperimentContractError, match="case identity"):
        decode_evaluation_output(
            output,
            expected_case=replace(case, expected_law="另一规则"),
        )
    wrong_stages = deepcopy(stages)
    wrong_stages["generation"]["cache_key"] = "0" * 64
    with pytest.raises(ExperimentContractError, match="stage identity"):
        decode_evaluation_output(output, expected_stage_identity=wrong_stages)

    inconsistent_stages = deepcopy(output)
    inconsistent_stages["identity"]["stages"]["generation"]["status"] = "error"
    inconsistent_stages["result_sha256"] = canonical_hash(
        {
            "identity": inconsistent_stages["identity"],
            "scoring_facts_sha256": inconsistent_stages["scoring_facts_sha256"],
            "record": inconsistent_stages["record"],
            "trace_record": inconsistent_stages["trace_record"],
        }
    )
    with pytest.raises(ExperimentContractError, match="execution facts"):
        decode_evaluation_output(inconsistent_stages)

    forged_model = deepcopy(output)
    forged_model["scoring_facts"]["model"] = "forged-model"
    forged_outcome = adapter_module._outcome_from_facts(forged_model["scoring_facts"])
    forged_evaluated = score_completed_case(forged_outcome)
    forged_model["scoring_facts_sha256"] = canonical_hash(forged_model["scoring_facts"])
    forged_model["record"] = eval_record_to_artifact(forged_evaluated.record)
    forged_model["trace_record"] = forged_evaluated.trace_record
    forged_model["result_sha256"] = canonical_hash(
        {
            "identity": forged_model["identity"],
            "scoring_facts_sha256": forged_model["scoring_facts_sha256"],
            "record": forged_model["record"],
            "trace_record": forged_model["trace_record"],
        }
    )
    with pytest.raises(ExperimentContractError, match="manifest contract"):
        decode_evaluation_output(forged_model, expected_manifest=manifest)

    boolean_schema = deepcopy(output)
    boolean_schema["evaluation_output_schema_version"] = True
    with pytest.raises(ExperimentContractError, match="schema"):
        decode_evaluation_output(boolean_schema)
    nested_raw = deepcopy(output)
    nested_raw["trace_record"]["metadata"]["raw_response"] = "secret"
    with pytest.raises(ExperimentContractError, match="raw_response"):
        decode_evaluation_output(nested_raw)

    empty_artifact: dict[str, Any] = {}
    boolean_envelope = {
        "adapter_artifact_schema_version": True,
        "artifact_kind": "fixture",
        "artifact": empty_artifact,
        "artifact_sha256": canonical_hash(empty_artifact),
    }
    with pytest.raises(ExperimentContractError, match="schema"):
        adapter_module._artifact_from_envelope(boolean_envelope, "fixture")


def test_factory_rejects_resume_for_independent_work_unit_before_construction(
    tmp_path: Path,
) -> None:
    case = _case()
    manifest = _manifest([case], experiment_id="m2-adapter-independent-resume")
    harness = _AssistantHarness(_ProviderFreeRetriever())
    factory = LegalEvaluationRuntimeFactory(
        _spec(manifest, ExactStageCache(tmp_path / "cache"), harness)
    )
    unit = plan_work_units(manifest)[0]

    with pytest.raises(ExperimentContractError, match="independent"):
        factory(
            unit,
            {
                "schema_version": 1,
                "memory": {
                    "schema_version": 1,
                    "token_limit": 2000,
                    "messages": [],
                },
            },
            RunnerControls(cache_mode="fresh"),
        )

    assert harness.assistants == []


def test_factory_rejects_reused_memory_before_restoring_checkpoint(
    tmp_path: Path,
) -> None:
    case = _case(session_group="shared-memory", turn_index=0)
    manifest = _manifest([case], experiment_id="m2-adapter-shared-memory")
    retriever = _ProviderFreeRetriever()
    assistants: list[_TrackingAssistant] = []
    shared_memory = None

    def build_assistant() -> _TrackingAssistant:
        nonlocal shared_memory
        assistant = _TrackingAssistant(retriever)
        if shared_memory is None:
            shared_memory = assistant.memory
        else:
            assistant.memory = shared_memory
        assistant.llm = _CountingAnswerClient()
        assistants.append(assistant)
        return assistant

    spec = replace(
        _spec(
            manifest,
            ExactStageCache(tmp_path / "cache"),
            _AssistantHarness(retriever),
        ),
        assistant_factory=build_assistant,
    )
    factory = LegalEvaluationRuntimeFactory(spec)
    unit = plan_work_units(manifest)[0]
    factory(unit, None, RunnerControls(cache_mode="fresh"))
    checkpoint_assistant = _TrackingAssistant(retriever)
    checkpoint_assistant.memory.add_turn("历史问题", "历史回答")
    checkpoint = checkpoint_assistant.export_session_state()

    with pytest.raises(ExperimentContractError, match="reuse"):
        factory(unit, checkpoint, RunnerControls(cache_mode="fresh"))

    assert shared_memory is not None
    assert shared_memory.messages == []
    assert len(assistants) == 2


def test_custom_retriever_requires_object_markers_and_thread_safety(
    tmp_path: Path,
) -> None:
    case = _case()
    manifest = _manifest([case], experiment_id="m2-adapter-retriever-marker")
    unmarked_harness = _AssistantHarness(_UnmarkedRetriever())
    with pytest.raises(ExperimentContractError, match="provider_free"):
        LegalEvaluationRuntimeFactory(
            _spec(
                manifest,
                ExactStageCache(tmp_path / "unmarked-cache"),
                unmarked_harness,
            )
        )

    embedding_harness = _AssistantHarness(_EmbeddingRetriever())
    with pytest.raises(ExperimentContractError, match="uses_query_embedding"):
        LegalEvaluationRuntimeFactory(
            _spec(
                manifest,
                ExactStageCache(tmp_path / "embedding-cache"),
                embedding_harness,
            )
        )

    subclass_harness = _AssistantHarness(_UnsafeBM25Subclass())
    with pytest.raises(ExperimentContractError, match="provider_free"):
        LegalEvaluationRuntimeFactory(
            _spec(
                manifest,
                ExactStageCache(tmp_path / "bm25-subclass-cache"),
                subclass_harness,
            )
        )

    concurrent_manifest = _manifest(
        [case],
        experiment_id="m2-adapter-retriever-concurrency",
        concurrency=2,
    )
    unsafe_harness = _AssistantHarness(_ProviderFreeRetriever())
    with pytest.raises(ExperimentContractError, match="thread_safe"):
        LegalEvaluationRuntimeFactory(
            _spec(
                concurrent_manifest,
                ExactStageCache(tmp_path / "unsafe-cache"),
                unsafe_harness,
            )
        )

    safe_harness = _AssistantHarness(_ThreadSafeRetriever())
    LegalEvaluationRuntimeFactory(
        _spec(
            concurrent_manifest,
            ExactStageCache(tmp_path / "safe-cache"),
            safe_harness,
        )
    )


def test_retrieval_only_rejects_session_cases_before_assistant_construction(
    tmp_path: Path,
) -> None:
    case = _case(session_group="retrieval-session", turn_index=0)
    manifest = _manifest(
        [case],
        experiment_id="m2-adapter-retrieval-session",
        generate=False,
    )
    harness = _AssistantHarness(_ProviderFreeRetriever())

    with pytest.raises(ExperimentContractError, match="does not support session"):
        LegalEvaluationRuntimeFactory(
            _spec(
                manifest,
                ExactStageCache(tmp_path / "cache"),
                harness,
                generate=False,
            )
        )

    assert harness.assistants == []


def test_retrieval_only_rejects_answer_judge_before_assistant_construction(
    tmp_path: Path,
) -> None:
    case = _case()
    manifest = _manifest(
        [case],
        experiment_id="m2-adapter-retrieval-judge",
        generate=False,
        judge_enabled=True,
    )
    harness = _AssistantHarness(_ProviderFreeRetriever())

    with pytest.raises(ExperimentContractError, match="cannot enable.*judge"):
        LegalEvaluationRuntimeFactory(
            replace(
                _spec(
                    manifest,
                    ExactStageCache(tmp_path / "cache"),
                    harness,
                    generate=False,
                ),
                judge_client_factory=_InvalidJudgeClient,
            )
        )

    assert harness.assistants == []


@pytest.mark.parametrize(
    ("generate", "execution_mode"),
    ((True, "retrieval"), (False, "full-regression")),
)
def test_execution_mode_must_match_generate(
    tmp_path: Path,
    generate: bool,
    execution_mode: str,
) -> None:
    case = _case()
    manifest = _manifest(
        [case],
        experiment_id=f"m2-adapter-mode-{generate}",
        generate=generate,
        execution_mode=execution_mode,
    )
    harness = _AssistantHarness(_ProviderFreeRetriever())

    with pytest.raises(ExperimentContractError, match="execution_mode"):
        LegalEvaluationRuntimeFactory(
            _spec(
                manifest,
                ExactStageCache(tmp_path / "cache"),
                harness,
                generate=generate,
            )
        )


def test_generated_evaluation_rejects_separate_adaptive_client_factory(
    tmp_path: Path,
) -> None:
    case = _case()
    manifest = _manifest([case], experiment_id="m2-adapter-adaptive-client")
    harness = _AssistantHarness(_ProviderFreeRetriever())
    spec = replace(
        _spec(manifest, ExactStageCache(tmp_path / "cache"), harness),
        adaptive_llm_client_factory=_CountingAnswerClient,
    )

    with pytest.raises(ExperimentContractError, match="separate adaptive"):
        LegalEvaluationRuntimeFactory(spec)

    assert harness.assistants == []


def test_assistant_runtime_must_match_manifest_summary(tmp_path: Path) -> None:
    case = _case()
    manifest = _manifest([case], experiment_id="m2-adapter-runtime-drift")
    harness = _ClientHarness(
        _ProviderFreeRetriever(),
        _CountingAnswerClient,
        assistant_type=_AdaptiveMismatchAssistant,
    )
    factory = LegalEvaluationRuntimeFactory(
        _spec(manifest, ExactStageCache(tmp_path / "cache"), harness)
    )

    with pytest.raises(ExperimentContractError, match="manifest config.summary"):
        factory(
            plan_work_units(manifest)[0],
            None,
            RunnerControls(cache_mode="fresh"),
        )


def test_provider_timeout_is_bound_to_manifest_identity(tmp_path: Path) -> None:
    case = _case()

    def build_client() -> _CountingAnswerClient:
        client = _CountingAnswerClient()
        client.request_timeout = 7.5  # type: ignore[attr-defined]
        return client

    matching = _manifest(
        [case],
        experiment_id="m2-adapter-timeout-match",
        provider_timeouts={"assistant": 7.5, "judge": None, "adaptive": None},
    )
    matching_harness = _ClientHarness(_ProviderFreeRetriever(), build_client)
    matching_factory = LegalEvaluationRuntimeFactory(
        _spec(
            matching,
            ExactStageCache(tmp_path / "matching-cache"),
            matching_harness,
        )
    )
    runtime = matching_factory(
        plan_work_units(matching)[0],
        None,
        RunnerControls(cache_mode="fresh"),
    )
    assert runtime is not None

    mismatched = _manifest(
        [case],
        experiment_id="m2-adapter-timeout-mismatch",
        provider_timeouts={"assistant": 8.0, "judge": None, "adaptive": None},
    )
    mismatched_harness = _ClientHarness(_ProviderFreeRetriever(), build_client)
    mismatched_factory = LegalEvaluationRuntimeFactory(
        _spec(
            mismatched,
            ExactStageCache(tmp_path / "mismatched-cache"),
            mismatched_harness,
        )
    )
    with pytest.raises(ExperimentContractError, match="request timeouts"):
        mismatched_factory(
            plan_work_units(mismatched)[0],
            None,
            RunnerControls(cache_mode="fresh"),
        )


def test_completion_usage_must_match_attempt_ledger_before_commit(
    tmp_path: Path,
) -> None:
    case = _case()
    manifest = _manifest([case], experiment_id="m2-adapter-usage-mismatch")
    cache = ExactStageCache(tmp_path / "cache")
    harness = _ClientHarness(_ProviderFreeRetriever(), _BadLedgerClient)
    store, summary = _run(
        tmp_path / "artifacts",
        manifest,
        LegalEvaluationRuntimeFactory(_spec(manifest, cache, harness)),
        cache_mode="cache",
    )

    assert summary.status == "completed_with_failures"
    assert harness.assistants[0].events.count("commit") == 0
    attempt = store.load_attempts(case.case_id)[0]
    assert attempt["result"]["error"] == {
        "code": "executor_contract_error",
        "retryable": False,
    }
    assert list((tmp_path / "cache" / "generation").glob("*.json")) == []

    clean_harness = _AssistantHarness(_ProviderFreeRetriever())
    clean_store, clean_summary = _run(
        tmp_path / "clean-artifacts",
        manifest,
        LegalEvaluationRuntimeFactory(_spec(manifest, cache, clean_harness)),
        cache_mode="cache",
    )
    assert clean_summary.status == "succeeded"
    assert _total_client_calls(clean_harness) == 1
    assert (
        _completed_result(clean_store, case.case_id)["stage_observations"][
            "generation"
        ]["origin"]
        == "fresh"
    )


def test_stop_before_provider_dispatch_does_not_cache_or_commit_generation(
    tmp_path: Path,
) -> None:
    case = _case()
    manifest = _manifest(
        [case],
        experiment_id="m2-adapter-stop-before-provider",
        max_retries=1,
    )
    runner_ref: dict[str, ExperimentRunner] = {}

    class StopBeforeGenerationAssistant(_TrackingAssistant):
        def generate_turn(
            self,
            retrieved: RetrievedTurn,
            *,
            generate: bool,
        ) -> GeneratedTurn:
            runner_ref["runner"].request_stop()
            return super().generate_turn(retrieved, generate=generate)

    harness = _ClientHarness(
        _ProviderFreeRetriever(),
        _CountingAnswerClient,
        assistant_type=StopBeforeGenerationAssistant,
    )
    cache = ExactStageCache(tmp_path / "cache")
    store = ExperimentStore.create(tmp_path / "artifacts", manifest)
    runner = ExperimentRunner(
        store=store,
        requested_manifest=manifest,
        runtime_factory=LegalEvaluationRuntimeFactory(_spec(manifest, cache, harness)),
        cache_mode="fresh",
        execution_environment={"python": "3.12", "platform": "test"},
    )
    runner_ref["runner"] = runner

    summary = runner.run()

    attempt = store.load_attempts(case.case_id)[0]
    assert summary.status == "interrupted"
    assert attempt["status"] == "interrupted"
    assert attempt["result"]["error"] == {
        "code": "controlled_interrupt",
        "retryable": True,
    }
    assert harness.clients[0].calls == 0
    assert harness.assistants[0].events.count("commit") == 0
    assert harness.assistants[0].memory.messages == []
    observations = attempt["result"]["stage_observations"]
    for stage in ("query_analysis", "retrieval"):
        assert observations[stage]["status"] == "succeeded"
        assert observations[stage]["origin"] == "fresh"
        assert observations[stage]["cache_key"] is not None
    generation = observations["generation"]
    assert generation["status"] == "error"
    assert generation["origin"] == "fresh"
    assert generation["error_code"] == "controlled_interrupt"
    assert generation["cache_key"] is not None
    for stage in ("verification", "judge"):
        assert observations[stage]["status"] == "not_run"
        assert observations[stage]["unavailable_reason"] == (
            "upstream_controlled_interrupt"
        )
    assert observations["query_embedding"]["unavailable_reason"] == (
        "query_embedding_disabled_by_retriever_contract"
    )
    assert observations["rerank"]["unavailable_reason"] == "rerank_disabled"
    assert attempt["result"]["model_usage"] == {
        role: {
            "calls": 0,
            "failed_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "token_usage_calls": 0,
            "latency_ms": 0.0,
        }
        for role in ("assistant", "normalizer", "judge")
    }
    assert len(list((tmp_path / "cache" / "query_analysis").glob("*.json"))) == 1
    assert len(list((tmp_path / "cache" / "retrieval").glob("*.json"))) == 1
    assert list((tmp_path / "cache" / "generation").glob("*.json")) == []
    assert not (store.directory / "completed" / f"{case.case_id}.json").exists()

    resumed_harness = _AssistantHarness(_ProviderFreeRetriever())
    reopened = ExperimentStore.open(tmp_path / "artifacts", manifest["experiment_id"])
    resumed = ExperimentRunner(
        store=reopened,
        requested_manifest=manifest,
        runtime_factory=LegalEvaluationRuntimeFactory(
            _spec(manifest, cache, resumed_harness)
        ),
        cache_mode="fresh",
        execution_environment={"python": "3.12", "platform": "test"},
    ).run()
    assert resumed.status == "succeeded"
    attempts = reopened.load_attempts(case.case_id)
    assert [item["status"] for item in attempts] == ["interrupted", "succeeded"]
    assert [item["cache_mode"] for item in attempts] == ["fresh", "cache"]
    assert (
        attempts[1]["result"]["stage_observations"]["query_analysis"]["origin"]
        == "cache"
    )
    assert attempts[1]["result"]["stage_observations"]["retrieval"]["origin"] == (
        "cache"
    )


def test_completion_client_requires_isolated_completion_usage(
    tmp_path: Path,
) -> None:
    class ClientWithoutCompletionUsage:
        usage: dict[str, Any] = {}

        def complete(self, prompt: str) -> str:
            return "unused"

    case = _case()
    manifest = _manifest([case], experiment_id="m2-adapter-usage-type")
    retriever = _ProviderFreeRetriever()

    def build_assistant() -> _TrackingAssistant:
        assistant = _TrackingAssistant(retriever)
        assistant.llm = ClientWithoutCompletionUsage()
        return assistant

    base_harness = _AssistantHarness(retriever)
    factory = LegalEvaluationRuntimeFactory(
        replace(
            _spec(
                manifest,
                ExactStageCache(tmp_path / "cache"),
                base_harness,
            ),
            assistant_factory=build_assistant,
        )
    )

    with pytest.raises(ExperimentContractError, match="CompletionUsage"):
        factory(
            plan_work_units(manifest)[0],
            None,
            RunnerControls(cache_mode="fresh"),
        )


def test_completion_client_must_disable_hidden_transport_retries(
    tmp_path: Path,
) -> None:
    class ClientWithUnprovenRetries:
        def __init__(self) -> None:
            self.usage = CompletionUsage()

        def complete(self, prompt: str) -> str:
            return "unused"

    case = _case()
    manifest = _manifest([case], experiment_id="m2-adapter-hidden-retries")
    retriever = _ProviderFreeRetriever()

    def build_assistant() -> _TrackingAssistant:
        assistant = _TrackingAssistant(retriever)
        assistant.llm = ClientWithUnprovenRetries()
        return assistant

    base_harness = _AssistantHarness(retriever)
    factory = LegalEvaluationRuntimeFactory(
        replace(
            _spec(
                manifest,
                ExactStageCache(tmp_path / "cache"),
                base_harness,
            ),
            assistant_factory=build_assistant,
        )
    )

    with pytest.raises(ExperimentContractError, match="hidden_retries_disabled"):
        factory(
            plan_work_units(manifest)[0],
            None,
            RunnerControls(cache_mode="fresh"),
        )


def test_current_verifier_recompute_is_in_stage_and_scoring_latency(
    tmp_path: Path,
) -> None:
    case = _case()
    manifest = _manifest([case], experiment_id="m2-adapter-verifier-timing")
    harness = _ClientHarness(
        _ProviderFreeRetriever(),
        _CountingAnswerClient,
        assistant_type=_SlowVerifyAssistant,
    )
    store, summary = _run(
        tmp_path / "artifacts",
        manifest,
        LegalEvaluationRuntimeFactory(
            _spec(manifest, ExactStageCache(tmp_path / "cache"), harness)
        ),
        cache_mode="fresh",
    )

    assert summary.status == "succeeded"
    result = _completed_result(store, case.case_id)
    verification_ms = result["stage_observations"]["verification"]["duration_ms"]
    assert verification_ms >= 35
    assert result["output"]["scoring_facts"]["latency_ms"] >= int(verification_ms)


def test_local_verification_cannot_bypass_provider_controls(
    tmp_path: Path,
) -> None:
    case = _case()
    manifest = _manifest([case], experiment_id="m2-adapter-verifier-provider-guard")
    cache = ExactStageCache(tmp_path / "cache")
    safe_harness = _AssistantHarness(_ProviderFreeRetriever())
    _, seeded = _run(
        tmp_path / "seed-artifacts",
        manifest,
        LegalEvaluationRuntimeFactory(_spec(manifest, cache, safe_harness)),
        cache_mode="fresh",
    )
    assert seeded.status == "succeeded"

    for cache_mode in ("fresh", "cache", "replay"):
        harness = _ClientHarness(
            _ProviderFreeRetriever(),
            _CountingAnswerClient,
            assistant_type=_ProviderCallingVerifyAssistant,
        )
        store, summary = _run(
            tmp_path / f"{cache_mode}-artifacts",
            manifest,
            LegalEvaluationRuntimeFactory(_spec(manifest, cache, harness)),
            cache_mode=cache_mode,
        )

        assert summary.status == "completed_with_failures"
        assert _total_client_calls(harness) == (1 if cache_mode == "fresh" else 0)
        assert harness.assistants[0].memory.messages == []
        attempt = store.load_attempts(case.case_id)[0]
        assert attempt["result"]["error"] == {
            "code": "executor_contract_error",
            "retryable": False,
        }
