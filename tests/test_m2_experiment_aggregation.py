from __future__ import annotations

import csv
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from legal_rag.chat import LegalChatAssistant
from legal_rag.experiment_adapter import (
    EvaluationRuntimeSpec,
    LegalEvaluationRuntimeFactory,
    build_manifest_cases,
)
from legal_rag.experiment_aggregation import (
    AGGREGATION_SCHEMA_VERSION,
    AggregationContractError,
    aggregate_experiment,
    aggregate_experiment_artifacts,
    aggregation_csv_rows,
    aggregation_json_bytes,
    aggregation_jsonl_rows,
    render_aggregation_csv,
    render_aggregation_json,
    render_aggregation_jsonl,
    render_aggregation_markdown,
    validate_aggregation_bundle,
)
from legal_rag.experiment_runner import (
    CaseExecutionError,
    ExperimentRunner,
    RunnerControls,
    WorkUnit,
)
from legal_rag.experiment_runtime import (
    EXTERNAL_CALL_KINDS,
    ExactStageCache,
    build_experiment_manifest,
    canonical_hash,
)
from legal_rag.experiment_store import ExperimentStore
from legal_rag.llm import CompletionUsage
from legal_rag.models import Chunk, EvalCase, SearchResult
from legal_rag.provider_errors import ProviderCallError


class _ProviderFreeRetriever:
    name = "aggregation-provider-free"
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
                    metadata={"snapshot_id": "aggregation-fixture-v1"},
                ),
                score=1.0,
                rank=1,
                retriever=self.name,
                trace={"fixture": {"rank": 1}},
            )
        ][:top_k]


class _AnswerClient:
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


class _ScriptedAnswerClient(_AnswerClient):
    def __init__(self, script: list[str]) -> None:
        super().__init__()
        self.script = script

    def complete(self, prompt: str) -> str:
        outcome = self.script.pop(0)
        if outcome == "success":
            return super().complete(prompt)
        self.calls += 1
        self.usage.calls += 1
        self.usage.failed_calls += 1
        self.usage.record_tokens(
            input_tokens=13,
            output_tokens=2,
            total_tokens=15,
        )
        self.usage.latency_ms += 2.25
        raise ProviderCallError(
            outcome,
            provider="test_provider",
            operation="completion",
        )


class _UnknownTokenUsageAnswerClient(_AnswerClient):
    def complete(self, prompt: str) -> str:
        response = super().complete(prompt)
        self.usage.input_tokens = 0
        self.usage.output_tokens = 0
        self.usage.total_tokens = 0
        self.usage.token_usage_calls = 0
        return response


class _AssistantHarness:
    def __init__(self, client_factory=None) -> None:
        self.retriever = _ProviderFreeRetriever()
        self.clients: list[_AnswerClient] = []
        self.client_factory = client_factory or _AnswerClient

    def build(self) -> LegalChatAssistant:
        assistant = LegalChatAssistant(
            self.retriever,
            model="aggregation-generation-fixture",
            top_k=3,
            adaptive_enabled=False,
            adaptive_use_llm=False,
            condense_with_llm=False,
        )
        client = self.client_factory()
        assistant.llm = client
        self.clients.append(client)
        return assistant


def _case(
    case_id: str,
    *,
    session_group: str | None = None,
    turn_index: int = 0,
) -> EvalCase:
    return EvalCase(
        case_id=case_id,
        question=f"《合成规则》第一条对 {case_id} 有什么要求？",
        case_type="synthetic",
        expected_law="合成规则",
        expected_articles=["第一条"],
        keywords=["合成事项"],
        expected_behavior="evidence_answer",
        session_group=session_group,
        turn_index=turn_index,
        schema_version=2,
    )


def _no_gold_case(case_id: str) -> EvalCase:
    return EvalCase(
        case_id=case_id,
        question="请执行一个超出合成规则范围的动作。",
        case_type="refusal",
        expected_law="",
        expected_articles=[],
        keywords=[],
        expected_behavior="out_of_scope",
        schema_version=2,
    )


def _manifest(
    cases: list[EvalCase],
    *,
    experiment_id: str,
    generate: bool = True,
    max_retries: int = 0,
) -> dict[str, Any]:
    manifest_cases = build_manifest_cases(cases)
    return build_experiment_manifest(
        experiment_id=experiment_id,
        execution_mode="full-regression" if generate else "retrieval",
        default_cache_mode="fresh",
        code={
            "commit": "a" * 40,
            "dirty": False,
            "stage_implementation_fingerprints": {
                "chunking": "aggregation-chunking-v1",
                "query_analysis": "aggregation-query-analysis-v1",
                "embedding": "aggregation-embedding-v1",
                "retrieval": "aggregation-retrieval-v1",
                "rerank": "aggregation-rerank-v1",
                "generation": "aggregation-generation-v1",
                "verification": "aggregation-verification-v1",
                "judge": "aggregation-judge-v1",
                "aggregation": "aggregation-runtime-v1",
            },
        },
        config_summary={
            "profile": "offline",
            "top_k": 3,
            "memory_token_limit": 2000,
            "generate": generate,
            "adaptive_enabled": False,
            "adaptive_use_llm": False,
            "adaptive_max_queries": 3,
            "adaptive_per_plan_top_k": None,
            "adaptive_normalizer_retries": 0,
            "condense_with_llm": False,
            "provider_timeouts": {
                "assistant": None,
                "judge": None,
                "adaptive": None,
            },
        },
        corpus={
            "snapshot_hash": canonical_hash("aggregation-corpus"),
            "index_hash": canonical_hash("aggregation-index"),
        },
        dataset={
            "dataset_id": "synthetic-aggregation",
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
                "kind": "aggregation-provider-free",
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
                "model": "aggregation-generation-fixture",
                "revision": "generation-v1",
                "prompt_version": "prompt-v1",
                "parameters": {"temperature": 0},
            },
            "verification": {"schema_version": 2, "rules_version": "m1"},
            "judge": {"enabled": False},
        },
        runtime={
            "random_seed": 42,
            "concurrency": 1,
            "max_retries": max_retries,
            "timing_scope": "runner_stage_wall_clock",
            "provider_limits": {kind: 1 for kind in EXTERNAL_CALL_KINDS},
            "retry_backoff_ms": 0,
        },
        environment={"python": "3.12", "platform": "test"},
        created_at="2026-09-20T00:00:00Z",
    )


def _run_valid(
    artifact_root: Path,
    cache_root: Path,
    manifest: dict[str, Any],
    *,
    cache_mode: str = "fresh",
    harness: _AssistantHarness | None = None,
    trace_metadata: dict[str, Any] | None = None,
    execution_environment: dict[str, Any] | None = None,
) -> tuple[ExperimentStore, _AssistantHarness]:
    harness = harness or _AssistantHarness()
    factory = LegalEvaluationRuntimeFactory(
        EvaluationRuntimeSpec(
            manifest=manifest,
            cache=ExactStageCache(cache_root),
            retriever=harness.retriever,
            assistant_factory=harness.build,
            model="aggregation-generation-fixture",
            chunk_strategy="article",
            generate=manifest["config"]["summary"]["generate"],
            retriever_provider_free=True,
            trace_metadata=(
                trace_metadata
                if trace_metadata is not None
                else {"fixture": {"name": "aggregation"}}
            ),
        )
    )
    store = ExperimentStore.create(artifact_root, manifest)
    runner = ExperimentRunner(
        store=store,
        requested_manifest=manifest,
        runtime_factory=factory,
        cache_mode=cache_mode,
        execution_environment=(
            execution_environment
            if execution_environment is not None
            else {
                "python": "3.12",
                "platform": "test",
                "worker": f"fixture-{cache_mode}",
            }
        ),
        timestamp=lambda: "2026-09-20T00:00:01Z",
    )
    runner.run()
    return store, harness


class _FailureRuntime:
    def execute(self, case, controls):
        if case["case_id"] == "interrupted-case":
            raise CaseExecutionError(
                "fixture_interrupted",
                retryable=False,
                interrupted=True,
            )
        if case["case_id"] == "failed-case":
            raise CaseExecutionError("fixture_terminal", retryable=False)
        raise CaseExecutionError("fixture_retryable", retryable=True)


class _FailureFactory:
    def __call__(
        self,
        work_unit: WorkUnit,
        resume_state,
        controls: RunnerControls,
    ) -> _FailureRuntime:
        return _FailureRuntime()


def _run_failures(
    artifact_root: Path,
    manifest: dict[str, Any],
) -> ExperimentStore:
    store = ExperimentStore.create(artifact_root, manifest)
    runner = ExperimentRunner(
        store=store,
        requested_manifest=manifest,
        runtime_factory=_FailureFactory(),
        cache_mode="fresh",
        execution_environment={"python": "3.12", "platform": "test"},
        timestamp=lambda: "2026-09-20T00:00:02Z",
    )
    runner.run()
    return store


def _rewrite_completed_attempt(store: ExperimentStore, case_id: str, mutate) -> None:
    attempt_path = store.attempt_paths(case_id)[-1]
    complete_path = attempt_path.parent / "complete.json"
    attempt_envelope = json.loads(attempt_path.read_text(encoding="utf-8"))
    mutate(attempt_envelope["payload"]["result"])
    attempt_envelope["payload_sha256"] = canonical_hash(attempt_envelope["payload"])
    attempt_path.write_text(
        json.dumps(attempt_envelope, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    complete_envelope = json.loads(complete_path.read_text(encoding="utf-8"))
    complete_envelope["payload"]["attempt_artifact_sha256"] = canonical_hash(
        attempt_envelope
    )
    complete_envelope["payload_sha256"] = canonical_hash(complete_envelope["payload"])
    complete_path.write_text(
        json.dumps(complete_envelope, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )


def _snapshot_files(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def test_artifact_only_aggregation_is_read_only_deterministic_and_renderable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest([_case("case-one")], experiment_id="aggregation-complete")
    store, harness = _run_valid(
        tmp_path / "experiments",
        tmp_path / "cache",
        manifest,
    )
    assert sum(client.calls for client in harness.clients) == 1
    before = _snapshot_files(store.directory)

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "aggregation must not construct or execute runtime components"
        )

    monkeypatch.setattr(LegalEvaluationRuntimeFactory, "__init__", forbidden)
    monkeypatch.setattr(LegalChatAssistant, "__init__", forbidden)
    monkeypatch.setattr(_ProviderFreeRetriever, "retrieve", forbidden)
    monkeypatch.setattr(_AnswerClient, "complete", forbidden)

    first = aggregate_experiment(store)
    second = aggregate_experiment_artifacts(
        tmp_path / "experiments", manifest["experiment_id"]
    )

    assert first == second
    assert aggregation_json_bytes(first) == aggregation_json_bytes(second)
    assert render_aggregation_json(first).encode("utf-8") == aggregation_json_bytes(
        first
    )
    assert _snapshot_files(store.directory) == before
    assert first["aggregation_schema_version"] == AGGREGATION_SCHEMA_VERSION
    assert first["identity"]["manifest_hash"] == manifest["identity"]["manifest_hash"]
    assert first["case_inventory"]["counts"]["succeeded"] == 1
    assert first["scoring"]["trusted_succeeded_case_count"] == 1
    core = {key: value for key, value in first.items() if key != "bundle_hash"}
    assert first["bundle_hash"] == canonical_hash(core)
    assert len(first["identity"]["case_results_hash"]) == 64
    assert len(first["identity"]["aggregation_key"]) == 64

    jsonl_rows = aggregation_jsonl_rows(first)
    assert len(jsonl_rows) == 1
    assert json.loads(render_aggregation_jsonl(first))["case"]["case_id"] == "case-one"
    csv_rows = aggregation_csv_rows(first)
    assert csv_rows[0]["status"] == "succeeded"
    parsed_csv = list(csv.DictReader(render_aggregation_csv(first).splitlines()))
    assert parsed_csv[0]["case_id"] == "case-one"
    markdown = render_aggregation_markdown(first)
    assert "Actual external calls" in markdown
    assert "Source provenance" in markdown

    tampered = deepcopy(first)
    tampered["case_inventory"]["counts"]["succeeded"] = 0
    with pytest.raises(AggregationContractError):
        validate_aggregation_bundle(tampered)


def test_retrieval_only_keeps_answer_metrics_na_and_retrieval_metrics_scored(
    tmp_path: Path,
) -> None:
    manifest = _manifest(
        [_case("retrieval-with-gold"), _no_gold_case("retrieval-without-gold")],
        experiment_id="aggregation-retrieval-only",
        generate=False,
    )
    store, harness = _run_valid(
        tmp_path / "experiments",
        tmp_path / "cache",
        manifest,
    )

    bundle = aggregate_experiment(store)

    assert sum(client.calls for client in harness.clients) == 0
    metrics = bundle["scoring"]["canonical_metrics"]
    assert metrics["hit_at_5"]["denominator"] == 1
    assert metrics["hit_at_5"]["value"] == 1.0
    assert metrics["hit_at_5"]["unavailable_count"] == 1
    assert metrics["answer_text"]["denominator"] == 0
    assert metrics["answer_text"]["unavailable_reasons"] == {"retrieval_only": 2}
    assert metrics["verifier_pass"]["denominator"] == 0
    assert metrics["verifier_pass"]["value"] is None
    assert metrics["verifier_pass"]["unavailable_reason"] == "no_eligible_cases"
    assert bundle["attempt_audit"]["actual_calls"]["totals"]["attempted"] == 0


def test_failed_interrupted_exhausted_and_not_run_are_explicit_and_unscored(
    tmp_path: Path,
) -> None:
    cases = [
        _case("failed-case"),
        _case("interrupted-case"),
        _case("exhausted-case", session_group="blocked-session", turn_index=0),
        _case("not-run-case", session_group="blocked-session", turn_index=1),
    ]
    manifest = _manifest(
        cases,
        experiment_id="aggregation-failures",
        max_retries=1,
    )
    store = _run_failures(tmp_path / "experiments", manifest)

    bundle = aggregate_experiment(store)

    assert bundle["case_inventory"]["counts"] == {
        "corrupt": 0,
        "exhausted": 1,
        "failed": 1,
        "interrupted": 1,
        "not_run": 1,
        "pending": 0,
        "succeeded": 0,
    }
    assert [row["status"] for row in bundle["cases"]] == [
        "failed",
        "interrupted",
        "exhausted",
        "not_run",
    ]
    assert bundle["attempt_audit"]["persisted_attempt_count"] == 4
    assert bundle["attempt_audit"]["trusted_attempt_count"] == 4
    assert bundle["attempt_audit"]["failure_codes"] == {
        "fixture_interrupted": 1,
        "fixture_retryable": 2,
        "fixture_terminal": 1,
    }
    assert bundle["scoring"]["trusted_succeeded_case_count"] == 0
    assert bundle["scoring"]["canonical_metrics"]["hit_at_5"]["value"] is None
    assert (
        bundle["scoring"]["canonical_metrics"]["hit_at_5"]["unavailable_reason"]
        == "no_eligible_cases"
    )


def test_pending_and_corrupt_cases_never_become_scoring_records(tmp_path: Path) -> None:
    manifest = _manifest(
        [_case("pending-case"), _case("corrupt-case")],
        experiment_id="aggregation-damaged",
    )
    store, _ = _run_valid(
        tmp_path / "experiments",
        tmp_path / "cache",
        manifest,
    )
    pending_attempt = store.attempt_paths("pending-case")[-1]
    (pending_attempt.parent / "complete.json").unlink()
    corrupt_attempt = store.attempt_paths("corrupt-case")[-1]
    corrupt_attempt.write_text("{", encoding="utf-8")
    before = _snapshot_files(store.directory)

    bundle = aggregate_experiment(store)

    assert [row["status"] for row in bundle["cases"]] == ["pending", "corrupt"]
    assert all(row["evaluation_record"] is None for row in bundle["cases"])
    assert bundle["scoring"]["trusted_succeeded_case_count"] == 0
    assert bundle["attempt_audit"]["persisted_attempt_count"] == 2
    assert bundle["attempt_audit"]["untrusted_attempt_count"] == 1
    assert bundle["attempt_audit"]["unreadable_case_ids"] == ["corrupt-case"]
    assert _snapshot_files(store.directory) == before

    unreadable_tamper = deepcopy(bundle)
    unreadable_tamper["attempt_audit"]["unreadable_case_ids"] = []
    unreadable_tamper["bundle_hash"] = canonical_hash(
        {key: value for key, value in unreadable_tamper.items() if key != "bundle_hash"}
    )
    with pytest.raises(AggregationContractError, match="unreadable case identities"):
        validate_aggregation_bundle(unreadable_tamper)


@pytest.mark.parametrize("tamper_kind", ("case_identity", "stage_identity", "usage"))
def test_cross_layer_tampering_is_corrupt_even_with_valid_store_checksums(
    tmp_path: Path,
    tamper_kind: str,
) -> None:
    case_id = f"tamper-{tamper_kind}"
    manifest = _manifest(
        [_case(case_id)],
        experiment_id=f"aggregation-{tamper_kind.replace('_', '-')}",
    )
    store, _ = _run_valid(
        tmp_path / "experiments",
        tmp_path / "cache",
        manifest,
    )

    def mutate(result: dict[str, Any]) -> None:
        output = result["output"]
        if tamper_kind == "case_identity":
            output["identity"]["case_artifact_sha256"] = "0" * 64
            output["result_sha256"] = canonical_hash(
                {
                    "identity": output["identity"],
                    "scoring_facts_sha256": output["scoring_facts_sha256"],
                    "record": output["record"],
                    "trace_record": output["trace_record"],
                }
            )
            return
        if tamper_kind == "stage_identity":
            result["stage_observations"]["retrieval"]["cache_key"] = "f" * 64
            return
        zero_usage = {
            "calls": 0,
            "failed_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "token_usage_calls": 0,
            "latency_ms": 0.0,
        }
        result["model_usage"]["assistant"] = zero_usage
        result["call_ledger"]["actual"]["generation"] = {
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "duration_ms": 0.0,
            "provider_wait_ms": 0.0,
        }
        result["call_ledger"]["source"]["generation"] = 0
        generation = result["stage_observations"]["generation"]
        generation["external_calls"]["generation"] = 0
        generation["failed_external_calls"]["generation"] = 0
        generation["source_external_calls"]["generation"] = 0

    _rewrite_completed_attempt(store, case_id, mutate)
    assert store.scan().succeeded == (case_id,)

    bundle = aggregate_experiment(store)

    assert bundle["case_inventory"]["counts"]["corrupt"] == 1
    assert bundle["cases"][0]["evaluation_record"] is None
    assert bundle["scoring"]["trusted_succeeded_case_count"] == 0
    assert bundle["attempt_audit"]["trusted_attempt_count"] == 1
    assert bundle["cases"][0]["problems"][-1]["code"] == "invalid_evaluation_output"


def test_failed_then_succeeded_case_scores_once_but_audits_every_attempt(
    tmp_path: Path,
) -> None:
    script = ["timeout", "success"]
    harness = _AssistantHarness(lambda: _ScriptedAnswerClient(script))
    manifest = _manifest(
        [_case("retry-case", session_group="retry-session", turn_index=0)],
        experiment_id="aggregation-retry-success",
        max_retries=1,
    )
    store, _ = _run_valid(
        tmp_path / "experiments",
        tmp_path / "cache",
        manifest,
        harness=harness,
    )

    bundle = aggregate_experiment(store)

    assert script == []
    assert bundle["case_inventory"]["counts"]["succeeded"] == 1
    assert bundle["cases"][0]["attempt_count"] == 2
    assert [attempt["status"] for attempt in bundle["cases"][0]["attempts"]] == [
        "failed",
        "succeeded",
    ]
    assert bundle["scoring"]["trusted_succeeded_case_count"] == 1
    assert bundle["scoring"]["canonical_metrics"]["hit_at_5"]["denominator"] == 1
    audit = bundle["attempt_audit"]
    assert audit["trusted_attempt_count"] == 2
    assert audit["failure_codes"] == {"provider_timeout": 1}
    assert audit["actual_calls"]["by_kind"]["generation"]["attempted"] == 2
    assert audit["actual_calls"]["by_kind"]["generation"]["failed"] == 1
    assert audit["model_usage"]["assistant"]["calls"] == 2
    assert audit["model_usage"]["assistant"]["failed_calls"] == 1
    assert audit["model_usage"]["assistant"]["total_tokens"] == 33
    assert audit["model_usage"]["assistant"]["token_usage_calls"] == 2
    assert audit["cache_modes"] == {"cache": 1, "fresh": 1}
    assert audit["by_cache_mode"]["fresh"]["failure_codes"] == {"provider_timeout": 1}


def test_fresh_cache_and_replay_keep_actual_calls_separate_from_provenance(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "shared-cache"
    artifact_root = tmp_path / "experiments"
    bundles: dict[str, dict[str, Any]] = {}
    harnesses: dict[str, _AssistantHarness] = {}
    for mode in ("fresh", "cache", "replay"):
        manifest = _manifest(
            [_case("shared-case")],
            experiment_id=f"aggregation-{mode}",
        )
        store, harness = _run_valid(
            artifact_root,
            cache_root,
            manifest,
            cache_mode=mode,
        )
        bundles[mode] = aggregate_experiment(store)
        harnesses[mode] = harness

    assert sum(client.calls for client in harnesses["fresh"].clients) == 1
    assert sum(client.calls for client in harnesses["cache"].clients) == 0
    assert sum(client.calls for client in harnesses["replay"].clients) == 0
    assert (
        bundles["fresh"]["attempt_audit"]["actual_calls"]["by_kind"]["generation"][
            "attempted"
        ]
        == 1
    )
    for mode in ("cache", "replay"):
        audit = bundles[mode]["attempt_audit"]
        assert audit["actual_calls"]["by_kind"]["generation"]["attempted"] == 0
        assert audit["source_provenance"]["by_kind"]["generation"] == 1
        assert (
            audit["by_cache_mode"][mode]["actual_calls"]["generation"]["attempted"] == 0
        )
        assert audit["by_cache_mode"][mode]["source_external_calls"]["generation"] == 1
        assert set(audit["timings_by_cache_mode"]) == {mode}
    assert (
        bundles["fresh"]["attempt_audit"]["source_provenance"]["by_kind"]["generation"]
        == 1
    )
    assert (
        bundles["replay"]["scoring"]["canonical_metrics"]["hit_at_5"]["value"]
        == bundles["fresh"]["scoring"]["canonical_metrics"]["hit_at_5"]["value"]
    )


def test_global_unknown_case_path_fails_closed(tmp_path: Path) -> None:
    manifest = _manifest(
        [_case("known-case")],
        experiment_id="aggregation-global-corruption",
    )
    store, _ = _run_valid(
        tmp_path / "experiments",
        tmp_path / "cache",
        manifest,
    )
    unexpected = store.directory / "cases" / "unexpected-case"
    unexpected.mkdir()

    with pytest.raises(
        AggregationContractError,
        match="global experiment artifact corruption",
    ):
        aggregate_experiment(store)


@pytest.mark.parametrize("corrupt_turn", (0, 1))
@pytest.mark.parametrize("corruption_kind", ("unreadable", "semantic"))
def test_session_corruption_preserves_only_the_directional_trust_prefix(
    tmp_path: Path,
    corrupt_turn: int,
    corruption_kind: str,
) -> None:
    cases = [
        _case("session-turn-0", session_group="session-a", turn_index=0),
        _case("session-turn-1", session_group="session-a", turn_index=1),
    ]
    manifest = _manifest(
        cases,
        experiment_id=f"aggregation-session-{corruption_kind}-{corrupt_turn}",
    )
    store, _ = _run_valid(
        tmp_path / "experiments",
        tmp_path / "cache",
        manifest,
    )
    corrupt_case_id = cases[corrupt_turn].case_id
    if corruption_kind == "unreadable":
        store.attempt_paths(corrupt_case_id)[-1].write_text("{", encoding="utf-8")
    else:

        def mutate(result: dict[str, Any]) -> None:
            output = result["output"]
            output["identity"]["case_artifact_sha256"] = "0" * 64
            output["result_sha256"] = canonical_hash(
                {
                    "identity": output["identity"],
                    "scoring_facts_sha256": output["scoring_facts_sha256"],
                    "record": output["record"],
                    "trace_record": output["trace_record"],
                }
            )

        _rewrite_completed_attempt(store, corrupt_case_id, mutate)

    bundle = aggregate_experiment(store)

    expected_statuses = (
        ["corrupt", "corrupt"] if corrupt_turn == 0 else ["succeeded", "corrupt"]
    )
    assert [row["status"] for row in bundle["cases"]] == expected_statuses
    assert bundle["scoring"]["trusted_succeeded_case_count"] == corrupt_turn
    if corrupt_turn == 0:
        assert bundle["cases"][1]["problems"][-1]["code"] == (
            "untrusted_session_predecessor"
        )


def test_case_results_identity_binds_session_checkpoint_and_completion_bytes(
    tmp_path: Path,
) -> None:
    case = _case("checkpoint-case", session_group="checkpoint-session", turn_index=0)
    manifest = _manifest([case], experiment_id="aggregation-proof-identity")
    store, _ = _run_valid(
        tmp_path / "experiments",
        tmp_path / "cache",
        manifest,
    )
    first = aggregate_experiment(store)

    def mutate_checkpoint(result: dict[str, Any]) -> None:
        checkpoint = result["session_checkpoint"]
        checkpoint["state_after"]["proof_probe"] = "changed"
        checkpoint["state_after_sha256"] = canonical_hash(checkpoint["state_after"])

    _rewrite_completed_attempt(store, case.case_id, mutate_checkpoint)
    second = aggregate_experiment(store)

    assert second["status"] == "complete"
    assert (
        first["cases"][0]["attempts"][0]["attempt_payload_sha256"]
        != second["cases"][0]["attempts"][0]["attempt_payload_sha256"]
    )
    assert (
        first["cases"][0]["artifact_evidence_sha256"]
        != second["cases"][0]["artifact_evidence_sha256"]
    )
    assert (
        first["identity"]["case_results_hash"]
        != second["identity"]["case_results_hash"]
    )
    assert first["identity"]["aggregation_key"] != second["identity"]["aggregation_key"]

    complete_path = store.attempt_paths(case.case_id)[-1].parent / "complete.json"
    complete_payload = json.loads(complete_path.read_text(encoding="utf-8"))
    complete_path.write_text(
        json.dumps(complete_payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    third = aggregate_experiment(store)
    assert (
        second["cases"][0]["completion_artifact_sha256"]
        != third["cases"][0]["completion_artifact_sha256"]
    )
    assert (
        second["identity"]["case_results_hash"]
        != third["identity"]["case_results_hash"]
    )


def test_bundle_validator_recomputes_scoring_audit_and_aggregation_key(
    tmp_path: Path,
) -> None:
    manifest = _manifest([_case("validator-case")], experiment_id="aggregation-guard")
    store, _ = _run_valid(
        tmp_path / "experiments",
        tmp_path / "cache",
        manifest,
    )
    bundle = aggregate_experiment(store)

    scoring_tamper = deepcopy(bundle)
    scoring_tamper["scoring"]["canonical_metrics"]["hit_at_5"]["value"] = 0.0
    scoring_tamper["bundle_hash"] = canonical_hash(
        {key: value for key, value in scoring_tamper.items() if key != "bundle_hash"}
    )
    with pytest.raises(AggregationContractError, match="scoring disagrees"):
        validate_aggregation_bundle(scoring_tamper)

    audit_tamper = deepcopy(bundle)
    audit_tamper["attempt_audit"]["actual_calls"]["totals"]["attempted"] += 1
    audit_tamper["bundle_hash"] = canonical_hash(
        {key: value for key, value in audit_tamper.items() if key != "bundle_hash"}
    )
    with pytest.raises(AggregationContractError, match="audit disagrees"):
        validate_aggregation_bundle(audit_tamper)

    key_tamper = deepcopy(bundle)
    key_tamper["identity"]["aggregation_key"] = "0" * 64
    key_tamper["bundle_hash"] = canonical_hash(
        {key: value for key, value in key_tamper.items() if key != "bundle_hash"}
    )
    with pytest.raises(AggregationContractError, match="cache key"):
        validate_aggregation_bundle(key_tamper)

    with pytest.raises(AggregationContractError, match="trusted identity"):
        validate_aggregation_bundle(bundle, expected_bundle_hash="0" * 64)

    environment_tamper = deepcopy(bundle)
    fingerprint = next(iter(environment_tamper["attempt_audit"]["environments"]))
    environment_tamper["attempt_audit"]["environments"][fingerprint][
        "execution_environment"
    ] = {"python": "forged", "platform": "forged"}
    environment_tamper["bundle_hash"] = canonical_hash(
        {
            key: value
            for key, value in environment_tamper.items()
            if key != "bundle_hash"
        }
    )
    with pytest.raises(AggregationContractError, match="nested data"):
        validate_aggregation_bundle(environment_tamper)


def test_csv_is_formula_safe_identity_bound_and_excludes_answer_text(
    tmp_path: Path,
) -> None:
    case_id = '=HYPERLINK("https://attacker.invalid","click")'
    manifest = _manifest([_case(case_id)], experiment_id="aggregation-csv-safe")
    store, _ = _run_valid(
        tmp_path / "experiments",
        tmp_path / "cache",
        manifest,
    )
    bundle = aggregate_experiment(store)

    rows = aggregation_csv_rows(bundle)
    assert rows[0]["case_id"].startswith("'=")
    assert rows[0]["bundle_hash"] == bundle["bundle_hash"]
    assert rows[0]["aggregation_key"] == bundle["identity"]["aggregation_key"]
    assert "metric_answer_text" not in rows[0]
    rendered = render_aggregation_csv(bundle)
    assert "metric_answer_text" not in rendered.splitlines()[0]
    assert "Concurrency" in render_aggregation_markdown(bundle)
    assert "Cache-mode timing" in render_aggregation_markdown(bundle)


def test_derived_outputs_redact_credential_shaped_free_form_metadata(
    tmp_path: Path,
) -> None:
    manifest = _manifest(
        [_case("redaction-case")], experiment_id="aggregation-redaction"
    )
    store, _ = _run_valid(
        tmp_path / "experiments",
        tmp_path / "cache",
        manifest,
        trace_metadata={
            "fixture": {"name": "aggregation"},
            "api_key": "TRACE_SECRET_SENTINEL",
            "bearer_token": "BEARER_SECRET_SENTINEL",
            "X-API-Key": "HEADER_SECRET_SENTINEL",
        },
        execution_environment={
            "python": "3.12",
            "platform": "test",
            "authorization": "ENV_SECRET_SENTINEL",
            "proxy_authorization": "PROXY_SECRET_SENTINEL",
        },
    )

    bundle = aggregate_experiment(store)
    rendered = "\n".join(
        (
            render_aggregation_json(bundle),
            render_aggregation_jsonl(bundle),
            render_aggregation_csv(bundle),
            render_aggregation_markdown(bundle),
        )
    )

    assert "TRACE_SECRET_SENTINEL" not in rendered
    assert "ENV_SECRET_SENTINEL" not in rendered
    assert "BEARER_SECRET_SENTINEL" not in rendered
    assert "HEADER_SECRET_SENTINEL" not in rendered
    assert "PROXY_SECRET_SENTINEL" not in rendered
    assert "[REDACTED]" in rendered


def test_unknown_provider_token_usage_is_never_presented_as_complete_zero(
    tmp_path: Path,
) -> None:
    manifest = _manifest(
        [_case("unknown-token-case")], experiment_id="aggregation-unknown-token"
    )
    harness = _AssistantHarness(_UnknownTokenUsageAnswerClient)
    store, _ = _run_valid(
        tmp_path / "experiments",
        tmp_path / "cache",
        manifest,
        harness=harness,
    )

    bundle = aggregate_experiment(store)
    aggregate_usage = bundle["attempt_audit"]["model_usage"]["assistant"]
    attempt_usage = bundle["cases"][0]["attempts"][0]["model_usage"]["assistant"]

    for usage in (aggregate_usage, attempt_usage):
        assert usage["calls"] == 1
        assert usage["token_usage_calls"] == 0
        assert usage["token_usage_complete"] is False
        assert usage["unreported_token_usage_calls"] == 1
        assert usage["complete_token_totals"]["total_tokens"] is None


def test_ignored_atomic_temp_debris_does_not_change_aggregation_identity(
    tmp_path: Path,
) -> None:
    manifest = _manifest([_case("temp-case")], experiment_id="aggregation-temp")
    store, _ = _run_valid(
        tmp_path / "experiments",
        tmp_path / "cache",
        manifest,
    )
    first = aggregate_experiment(store)
    case_directory = store.attempt_paths("temp-case")[-1].parent
    (case_directory / ".attempt-0001.json.deadbeef.tmp").write_bytes(b"stale")

    second = aggregate_experiment(store)

    assert second == first
    assert second["status"] == "complete"
