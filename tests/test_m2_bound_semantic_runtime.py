"""Offline contracts for the explicit M2 semantic gate, never legal-quality proof."""

import json
import threading
from copy import deepcopy
from dataclasses import replace

import pytest

from legal_rag.experiment_adapter import LegalEvaluationRuntimeFactory
from legal_rag.experiment_runtime import (
    ExactStageCache, ExperimentContractError, build_experiment_manifest,
    build_stage_cache_key, validate_experiment_manifest,
)
from legal_rag.semantic import SemanticPolicy
from test_m2_experiment_adapter import (
    _AssistantHarness, _ProviderFreeRetriever, _case, _completed_result,
    _manifest, _run, _spec,
)
from legal_rag.llm import CompletionUsage
from legal_rag.experiment_runner import (
    AttemptControls, ProviderController, RunnerContractError, plan_work_units,
    validate_persisted_runner_attempt,
)
from legal_rag.experiment_runtime import EXTERNAL_CALL_KINDS
from legal_rag.provider_errors import ProviderCallError


class SemanticClient:
    hidden_retries_disabled = True
    request_timeout = None

    def __init__(self, *, status="supported", payload="valid"):
        self.status, self.payload = status, payload
        self.calls = 0
        self.usage = CompletionUsage()

    def complete(self, prompt):
        self.calls += 1
        self.usage.calls += 1
        self.usage.record_tokens(input_tokens=19, output_tokens=11, total_tokens=30)
        self.usage.latency_ms += 2.0
        if self.payload != "valid":
            return self.payload
        request = json.loads(prompt.split("\n", 1)[1])
        return json.dumps({"decisions": [
            {"segment_id": segment["segment_id"], "status": self.status,
             "source_ids": ["S1"], "reason_codes": []}
            for segment in request["segments"]
        ]})


class SemanticHarness(_AssistantHarness):
    def __init__(self, policy, *, status="supported", payload="valid"):
        super().__init__(_ProviderFreeRetriever())
        self.policy, self.status, self.payload = policy, status, payload
        self.semantic_clients = []

    def build(self):
        assistant = super().build()
        assistant.semantic_policy = self.policy
        return assistant

    def semantic_client(self):
        client = SemanticClient(status=self.status, payload=self.payload)
        self.semantic_clients.append(client)
        return client


def _rebuild(manifest, *, summary=None, contracts=None, version=3):
    return build_experiment_manifest(
        experiment_id=manifest["experiment_id"], execution_mode=manifest["execution_mode"],
        default_cache_mode=manifest["cache_policy"]["default_mode"], code=manifest["code"],
        config_summary=summary or manifest["config"]["summary"], corpus=manifest["corpus"],
        dataset=manifest["dataset"], contracts=contracts or manifest["contracts"],
        runtime=manifest["runtime"], environment=manifest["environment"],
        created_at=manifest["created_at"], manifest_schema_version=version,
    )


def _semantic_manifest(case, policy):
    manifest = _manifest([case], experiment_id="m2-semantic-offline-fake")
    summary = deepcopy(manifest["config"]["summary"])
    summary["semantic_policy"] = policy.to_dict()
    summary["semantic_max_calls_per_case"] = 1
    summary["provider_timeouts"]["semantic"] = None
    contracts = deepcopy(manifest["contracts"])
    contracts["verification"]["rules_version"] = "general-bound-v2"
    return _rebuild(manifest, summary=summary, contracts=contracts)


def _factory(tmp_path, policy, *, status="supported", payload="valid", manifest=None):
    case = _case(question="合成一般问题")
    manifest = manifest or _semantic_manifest(case, policy)
    harness = SemanticHarness(policy, status=status, payload=payload)
    spec = replace(_spec(manifest, ExactStageCache(tmp_path / "cache"), harness),
                   semantic_policy=policy, semantic_client_factory=harness.semantic_client)
    return case, manifest, harness, LegalEvaluationRuntimeFactory(spec)


def test_semantic_gate_runs_once_is_independently_counted_and_replays_without_calls(tmp_path):
    policy = SemanticPolicy("offline-checker", "revision-1", "prompt-1", "a" * 64, True)
    case, manifest, harness, factory = _factory(tmp_path, policy)
    store, summary = _run(tmp_path / "fresh", manifest, factory, cache_mode="fresh")
    assert summary.status == "succeeded"
    result = _completed_result(store, case.case_id)
    assert result["runner_schema_version"] == 3
    assert result["call_ledger"]["actual"]["semantic"]["attempted"] == 1
    assert result["call_ledger"]["actual"]["judge"]["attempted"] == 0
    assert result["model_usage"]["semantic"]["total_tokens"] == 30
    assert result["model_usage"]["assistant"]["total_tokens"] == 18
    assert result["stage_observations"]["generation"]["external_calls"]["semantic"] == 1
    facts = result["output"]["scoring_facts"]
    assert facts["verification"]["payload"]["semantic_support_status"] == "supported"
    assert facts["trace_metadata"]["semantic_gate"]["policy_fingerprint"] == policy.fingerprint
    assert sum(client.calls for client in harness.semantic_clients) == 1
    replay, replay_summary = _run(tmp_path / "replay", manifest, factory, cache_mode="replay")
    assert replay_summary.status == "succeeded"
    replay_result = _completed_result(replay, case.case_id)
    assert replay_result["call_ledger"]["actual"]["semantic"]["attempted"] == 0
    assert replay_result["call_ledger"]["source"]["semantic"] == 1
    assert replay_result["model_usage"]["semantic"]["calls"] == 0
    assert sum(client.calls for client in harness.semantic_clients) == 1


@pytest.mark.parametrize("status,payload,expected", [
    ("unsupported", "valid", "unsupported"), ("uncertain", "valid", "uncertain"),
    ("supported", "not-json", "error"),
])
def test_failed_semantic_gate_preserves_draft_failure_not_fake_full_pass(tmp_path, status, payload, expected):
    policy = SemanticPolicy("offline-checker", "revision-1", "prompt-1", "a" * 64, True)
    case, manifest, harness, factory = _factory(tmp_path, policy, status=status, payload=payload)
    store, summary = _run(tmp_path / "run", manifest, factory, cache_mode="fresh")
    assert summary.status == "succeeded"
    result = _completed_result(store, case.case_id)
    facts = result["output"]["scoring_facts"]
    assert not facts["pre_fallback_verification"]["payload"]["passed"]
    assert facts["pre_fallback_verification"]["payload"]["semantic_support_status"] == expected
    assert facts["verification"]["payload"]["semantic_support_status"] == "not_checked"
    assert result["model_usage"]["semantic"]["calls"] == 1
    assert sum(client.calls for client in harness.semantic_clients) == 1


def test_frozen_semantic_policy_changes_only_downstream_stage_identity():
    policy = SemanticPolicy("offline-checker", "revision-1", "prompt-1", "a" * 64, True)
    first = _semantic_manifest(_case(), policy)
    second_policy = replace(policy, checker_revision="revision-2")
    second = _semantic_manifest(_case(), second_policy)
    assert first["stage_contracts"]["retrieval"] == second["stage_contracts"]["retrieval"]
    for stage, inputs in (
        ("generation", {"context_history_hash": "a", "evidence_hash": "b"}),
        ("verification", {"draft_hash": "a", "evidence_hash": "b", "scope_hash": "c"}),
    ):
        assert build_stage_cache_key(first, stage, inputs) != build_stage_cache_key(second, stage, inputs)


def test_old_manifest_cannot_acquire_semantic_authority_or_execute_modern_factory(tmp_path):
    policy = SemanticPolicy("offline-checker", "revision-1", "prompt-1", "a" * 64, True)
    modern = _semantic_manifest(_case(), policy)
    with pytest.raises(ExperimentContractError, match="semantic"):
        _rebuild(modern, version=2)
    historical_summary = deepcopy(modern["config"]["summary"])
    historical_summary.pop("semantic_policy")
    historical_summary.pop("semantic_max_calls_per_case")
    historical_summary["provider_timeouts"].pop("semantic")
    historical = _rebuild(modern, summary=historical_summary, version=2)
    assert validate_experiment_manifest(historical) == historical
    harness = _AssistantHarness(_ProviderFreeRetriever())
    with pytest.raises(ExperimentContractError, match="historical"):
        LegalEvaluationRuntimeFactory(_spec(historical, ExactStageCache(tmp_path / "cache"), harness))


def test_runtime_policy_must_equal_manifest_before_any_client_is_constructed(tmp_path):
    policy = SemanticPolicy("offline-checker", "revision-1", "prompt-1", "a" * 64, True)
    manifest = _semantic_manifest(_case(), policy)
    harness = SemanticHarness(policy)
    spec = replace(_spec(manifest, ExactStageCache(tmp_path / "cache"), harness),
                   semantic_policy=replace(policy, required=False),
                   semantic_client_factory=harness.semantic_client)
    with pytest.raises(ExperimentContractError, match="semantic"):
        LegalEvaluationRuntimeFactory(spec)
    assert not harness.clients and not harness.semantic_clients


def test_semantic_client_without_frozen_policy_is_rejected(tmp_path):
    manifest = _manifest([_case()])
    harness = _AssistantHarness(_ProviderFreeRetriever())
    spec = replace(_spec(manifest, ExactStageCache(tmp_path / "cache"), harness),
                   semantic_client_factory=SemanticClient)
    with pytest.raises(ExperimentContractError, match="semantic"):
        LegalEvaluationRuntimeFactory(spec)


def test_implementation_fingerprints_cover_new_mechanisms_and_strict_codecs():
    from legal_rag.experiment_lifecycle import _IMPLEMENTATION_FILES
    for component in ("retrieval", "generation", "verification"):
        assert "legal_rag/models.py" in _IMPLEMENTATION_FILES[component]
        assert "legal_rag/retrieval_contracts.py" in _IMPLEMENTATION_FILES[component]
    for component in ("retrieval", "verification"):
        assert "legal_rag/legal_references.py" in _IMPLEMENTATION_FILES[component]
        assert "legal_rag/reference_evidence.py" in _IMPLEMENTATION_FILES[component]
    for component in ("generation", "verification"):
        assert "legal_rag/semantic.py" in _IMPLEMENTATION_FILES[component]
        assert "legal_rag/evaluation_artifacts.py" in _IMPLEMENTATION_FILES[component]


def test_controls_never_grant_semantic_authority_implicitly():
    controls = AttemptControls(ProviderController({kind: 1 for kind in EXTERNAL_CALL_KINDS}),
                               threading.Event(), cache_mode="fresh", external_calls_allowed=True)
    dispatched = []
    with pytest.raises(ExperimentContractError, match="semantic"):
        controls.call("generation", "semantic", lambda: dispatched.append(True))
    assert not dispatched
    assert controls.ledger()["semantic"]["attempted"] == 0


def test_semantic_dispatch_budget_is_independent_and_checked_before_provider():
    controls = AttemptControls(ProviderController({kind: 1 for kind in EXTERNAL_CALL_KINDS}),
                               threading.Event(), cache_mode="fresh", external_calls_allowed=True,
                               semantic_max_calls=1)
    dispatched = []
    assert controls.call("generation", "semantic", lambda: dispatched.append(True)) is None
    with pytest.raises(ExperimentContractError, match="budget"):
        controls.call("generation", "semantic", lambda: dispatched.append(True))
    assert dispatched == [True]
    assert controls.ledger()["semantic"]["attempted"] == 1
    with pytest.raises(ExperimentContractError, match="generation"):
        controls.call("verification", "semantic", lambda: dispatched.append(True))


def test_runner_schema_two_is_only_zero_filled_read_only_history(tmp_path):
    case = _case()
    manifest = _manifest([case])
    harness = _AssistantHarness(_ProviderFreeRetriever())
    factory = LegalEvaluationRuntimeFactory(_spec(manifest, ExactStageCache(tmp_path / "cache"), harness))
    store, _ = _run(tmp_path / "run", manifest, factory, cache_mode="fresh")
    original = store.load_attempts(case.case_id)[0]
    historical = deepcopy(original)
    result = historical["result"]
    result["runner_schema_version"] = 2
    result["model_usage"].pop("semantic")
    result["call_ledger"]["actual"].pop("semantic")
    result["call_ledger"]["source"].pop("semantic")
    for observation in result["stage_observations"].values():
        for field in ("external_calls", "failed_external_calls", "source_external_calls"):
            observation[field].pop("semantic")
    frozen = deepcopy(historical)
    unit = plan_work_units(manifest)[0]
    view = validate_persisted_runner_attempt(historical, case=unit.cases[0], unit=unit, state_before=None)
    assert view.model_usage["semantic"]["calls"] == 0
    assert view.call_ledger["actual"]["semantic"]["attempted"] == 0
    assert historical == frozen
    # Modern records cannot omit the new role or hide a dispatched checker.
    broken = deepcopy(original)
    broken["result"]["model_usage"].pop("semantic")
    with pytest.raises(RunnerContractError, match="roles"):
        validate_persisted_runner_attempt(broken, case=unit.cases[0], unit=unit, state_before=None)


@pytest.mark.parametrize("field,value", [("semantic_max_calls_per_case", 0), ("semantic_max_calls_per_case", True)])
def test_manifest_rejects_invalid_semantic_call_budget(field, value):
    policy = SemanticPolicy("offline-checker", "revision-1", "prompt-1", "a" * 64, True)
    manifest = _semantic_manifest(_case(), policy)
    summary = deepcopy(manifest["config"]["summary"])
    summary[field] = value
    with pytest.raises(ExperimentContractError, match="semantic_max_calls"):
        _rebuild(manifest, summary=summary)


def test_checker_unavailable_is_zero_call_not_checked_and_retains_original_failure(tmp_path):
    policy = SemanticPolicy("offline-checker", "revision-1", "prompt-1", "a" * 64, True)
    case = _case(question="合成一般问题")
    manifest = _semantic_manifest(case, policy)
    harness = SemanticHarness(policy)
    spec = replace(_spec(manifest, ExactStageCache(tmp_path / "cache"), harness), semantic_policy=policy)
    store, summary = _run(tmp_path / "run", manifest, LegalEvaluationRuntimeFactory(spec), cache_mode="fresh")
    assert summary.status == "succeeded"
    result = _completed_result(store, case.case_id)
    facts = result["output"]["scoring_facts"]
    assert facts["pre_fallback_verification"]["payload"]["semantic_support_status"] == "not_checked"
    assert not facts["pre_fallback_verification"]["payload"]["passed"]
    assert facts["trace_metadata"]["semantic_gate"]["not_checked_reason"] == "checker_unavailable"
    assert result["call_ledger"]["actual"]["semantic"]["attempted"] == 0


def test_semantic_provider_failure_keeps_independent_failed_usage_and_no_partial_cache(tmp_path):
    policy = SemanticPolicy("offline-checker", "revision-1", "prompt-1", "a" * 64, True)
    case = _case(question="合成一般问题")
    manifest = _semantic_manifest(case, policy)
    harness = SemanticHarness(policy)

    class TimeoutSemanticClient(SemanticClient):
        def complete(self, prompt):
            self.calls += 1
            self.usage.calls += 1
            self.usage.failed_calls += 1
            self.usage.record_tokens(input_tokens=13, output_tokens=2, total_tokens=15)
            raise ProviderCallError("timeout", provider="offline_fixture", operation="semantic")

    cache = ExactStageCache(tmp_path / "cache")
    spec = replace(_spec(manifest, cache, harness), semantic_policy=policy,
                   semantic_client_factory=TimeoutSemanticClient)
    store, summary = _run(tmp_path / "run", manifest, LegalEvaluationRuntimeFactory(spec), cache_mode="fresh")
    assert summary.status == "completed_with_failures"
    attempt = store.load_attempts(case.case_id)[0]
    result = attempt["result"]
    assert result["output"] is None
    assert result["error"] == {"code": "provider_timeout", "retryable": True}
    assert result["model_usage"]["semantic"]["calls"] == 1
    assert result["model_usage"]["semantic"]["failed_calls"] == 1
    assert result["model_usage"]["semantic"]["total_tokens"] == 15
    assert result["model_usage"]["judge"]["calls"] == 0
    assert result["model_usage"]["assistant"]["calls"] == 1
    assert result["stage_observations"]["generation"]["failed_external_calls"]["semantic"] == 1
    assert not list((tmp_path / "cache" / "generation").glob("*.json"))
    assert not harness.assistants[0].memory.messages


@pytest.mark.parametrize("drift", ["policy", "checker"])
def test_assistant_cannot_smuggle_an_uncontrolled_checker_or_policy(tmp_path, drift):
    policy = SemanticPolicy("offline-checker", "revision-1", "prompt-1", "a" * 64, True)
    case, manifest, harness, factory = _factory(tmp_path, policy)
    original_build = harness.build

    def changed_assistant():
        assistant = original_build()
        if drift == "policy":
            assistant.semantic_policy = replace(policy, checker_revision="drift")
        else:
            assistant.semantic_checker = object()
        return assistant

    changed = LegalEvaluationRuntimeFactory(replace(factory.spec, assistant_factory=changed_assistant))
    unit = plan_work_units(manifest)[0]
    from legal_rag.experiment_runner import RunnerControls
    with pytest.raises(ExperimentContractError, match="semantic"):
        changed(unit, None, RunnerControls("fresh"))
    assert sum(client.calls for client in harness.clients) == 0
    assert not harness.semantic_clients


def test_semantic_trace_usage_cannot_disagree_with_attempt_ledger(tmp_path):
    policy = SemanticPolicy("offline-checker", "revision-1", "prompt-1", "a" * 64, True)
    case, manifest, _, factory = _factory(tmp_path, policy)
    store, _ = _run(tmp_path / "run", manifest, factory, cache_mode="fresh")
    attempt = store.load_attempts(case.case_id)[0]
    unit = plan_work_units(manifest)[0]
    view = validate_persisted_runner_attempt(attempt, case=unit.cases[0], unit=unit, state_before=None)
    output = deepcopy(view.output)
    from legal_rag.experiment_aggregation import _output_usage_matches_runner
    assert _output_usage_matches_runner(output, view)
    output["scoring_facts"]["trace_metadata"]["semantic_gate"]["actual_usage"]["calls"] = 0
    assert not _output_usage_matches_runner(output, view)


def test_rechecksummed_semantic_policy_drift_is_rejected_by_strict_output_decoder(tmp_path):
    policy = SemanticPolicy("offline-checker", "revision-1", "prompt-1", "a" * 64, True)
    case, manifest, _, factory = _factory(tmp_path, policy)
    store, _ = _run(tmp_path / "run", manifest, factory, cache_mode="fresh")
    output = deepcopy(_completed_result(store, case.case_id)["output"])
    from legal_rag.experiment_adapter import _evaluation_output, _outcome_from_facts, decode_evaluation_output
    from legal_rag.evaluation_scoring import score_completed_case
    output["scoring_facts"]["trace_metadata"]["semantic_gate"]["policy_fingerprint"] = "b" * 64
    outcome = _outcome_from_facts(output["scoring_facts"])
    altered = _evaluation_output(outcome, score_completed_case(outcome), manifest=manifest,
                                 stage_identity=output["identity"]["stages"])
    with pytest.raises(ExperimentContractError, match="semantic"):
        decode_evaluation_output(altered, expected_manifest=manifest)
