"""Synthetic reproduction of the authentic provider-free M2 schema-2 shape."""

from copy import deepcopy

import pytest

from legal_rag.experiment_adapter import LegalEvaluationRuntimeFactory, decode_evaluation_output
from legal_rag.experiment_runtime import ExactStageCache, ExperimentContractError, canonical_hash
from test_m2_experiment_adapter import _AssistantHarness, _ProviderFreeRetriever, _case, _manifest, _run, _spec
from test_m2_bound_semantic_runtime import _rebuild


def _checksums(output):
    output["scoring_facts_sha256"] = canonical_hash(output["scoring_facts"])
    output["result_sha256"] = canonical_hash({
        "identity": output["identity"], "scoring_facts_sha256": output["scoring_facts_sha256"],
        "record": output["record"], "trace_record": output["trace_record"],
    })


def _historical_shape(tmp_path):
    case = _case(question="合成一般问题")
    modern = _manifest([case], generate=False)
    harness = _AssistantHarness(_ProviderFreeRetriever())
    factory = LegalEvaluationRuntimeFactory(_spec(modern, ExactStageCache(tmp_path / "cache"), harness, generate=False))
    store, _ = _run(tmp_path / "run", modern, factory, cache_mode="fresh")
    output = deepcopy(store.load_completed(case.case_id)["result"]["output"])
    contracts = deepcopy(modern["contracts"])
    contracts["verification"]["rules_version"] = "m1"
    historical = _rebuild(modern, contracts=contracts, version=2)
    output["identity"]["manifest_hash"] = historical["identity"]["manifest_hash"]
    evidence = output["scoring_facts"]["evidence_check"]
    evidence["artifact_schema_version"] = 1
    evidence["payload"].pop("rules_version")
    evidence["payload"].pop("mechanical_check")
    output["trace_record"]["evidence"].pop("rules_version")
    output["trace_record"]["evidence"].pop("mechanical_check")
    _checksums(output)
    return modern, historical, output


def test_authentic_historical_shape_decodes_exact_original_metrics_and_trace(tmp_path):
    _, historical, output = _historical_shape(tmp_path)
    frozen = deepcopy(output)
    evaluated = decode_evaluation_output(output, expected_manifest=historical)
    assert evaluated.trace_record == frozen["trace_record"]
    from legal_rag.evaluation_artifacts import eval_record_to_artifact
    assert eval_record_to_artifact(evaluated.record) == frozen["record"]
    assert output == frozen


def test_history_projection_does_not_ignore_changed_old_contract_fields(tmp_path):
    _, historical, output = _historical_shape(tmp_path)
    output["trace_record"]["evidence"]["sufficient"] = not output["trace_record"]["evidence"]["sufficient"]
    _checksums(output)
    with pytest.raises(ExperimentContractError, match="trace"):
        decode_evaluation_output(output, expected_manifest=historical)


def test_old_evidence_cannot_be_relabelled_as_modern_execution(tmp_path):
    modern, _, output = _historical_shape(tmp_path)
    output["identity"]["manifest_hash"] = modern["identity"]["manifest_hash"]
    _checksums(output)
    with pytest.raises(ExperimentContractError, match="evidence"):
        decode_evaluation_output(output, expected_manifest=modern)
