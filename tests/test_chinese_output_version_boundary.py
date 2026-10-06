"""Public M2 output decoding must preserve the frozen evidence-rule identity."""

from __future__ import annotations

from copy import deepcopy

import pytest

from legal_rag.evaluation_scoring import CompletedCaseOutcome, ModelUsageDelta, score_completed_case
from legal_rag.evidence import check_evidence_sufficiency
from legal_rag.bm25_settings import BM25Settings
from legal_rag.experiment_adapter import (
    OBSERVATION_STAGES,
    EvaluationRuntimeSpec,
    LegalEvaluationRuntimeFactory,
    _evaluation_output,
    decode_evaluation_output,
)
from legal_rag.experiment_runtime import (
    ExactStageCache, ExperimentContractError, build_experiment_manifest,
)
from legal_rag.query import analyze_query
from legal_rag.retrieval import BM25Retriever
from test_m2_experiment_adapter import _ProviderFreeRetriever, _case, _manifest


def _public_output(*, frozen_version: str | None, executed_version: str):
    """Construct only synthetic completed facts; execute no runtime or provider."""
    case = _case()
    manifest = _manifest(
        [case], generate=False, evidence_rules_version=frozen_version
    )
    results = tuple(_ProviderFreeRetriever().retrieve(case.question, top_k=3))
    evidence = check_evidence_sufficiency(
        case.question, list(results), rules_version=executed_version
    )
    outcome = CompletedCaseOutcome(
        case=case,
        model=manifest["contracts"]["generation"]["model"],
        retriever=manifest["contracts"]["retrieval"]["kind"],
        chunk_strategy="article",
        top_k=3,
        generate=False,
        results=results,
        answer="",
        analysis=analyze_query(case.question, evidence_rules_version=executed_version),
        adaptive_trace={"enabled": False},
        evidence_check=evidence,
        verification=None,
        structured_answer=None,
        pre_fallback_answer=None,
        pre_fallback_verification=None,
        generation_kind="retrieval_only",
        generation_error=None,
        judge_configured=False,
        judge_result=None,
        error="",
        latency_ms=0,
        assistant_usage=ModelUsageDelta(),
        normalizer_usage=ModelUsageDelta(),
        judge_usage=ModelUsageDelta(),
    )
    stages = {
        stage: {
            "status": "succeeded" if stage in {"query_analysis", "retrieval"} else "not_run",
            "cache_key": "a" * 64 if stage in {"query_analysis", "retrieval"} else None,
            "payload_sha256": "b" * 64 if stage in {"query_analysis", "retrieval"} else None,
        }
        for stage in OBSERVATION_STAGES
    }
    output = _evaluation_output(
        outcome, score_completed_case(outcome), manifest=manifest, stage_identity=stages
    )
    return manifest, case, stages, output


@pytest.mark.parametrize("version", ["general-reference-v2", "general-reference-v3"])
def test_public_decoder_accepts_matching_frozen_evidence_version(version: str) -> None:
    manifest, case, stages, output = _public_output(
        frozen_version=version, executed_version=version
    )
    decoded = decode_evaluation_output(
        output, expected_manifest=manifest, expected_case=case,
        expected_stage_identity=stages,
    )
    assert decoded.trace_record["evidence"]["rules_version"] == version


@pytest.mark.parametrize(
    ("frozen", "executed"),
    [("general-reference-v3", "general-reference-v2"),
     ("general-reference-v2", "general-reference-v3")],
)
def test_public_decoder_rejects_self_consistent_cross_version_output(
    frozen: str, executed: str
) -> None:
    manifest, case, stages, output = _public_output(
        frozen_version=frozen, executed_version=executed
    )
    # Facts, record, trace and all envelope hashes agree. Only the actual
    # evidence version disagrees with the separately frozen manifest.
    with pytest.raises(ExperimentContractError, match="evidence rules differ"):
        decode_evaluation_output(
            output, expected_manifest=manifest, expected_case=case,
            expected_stage_identity=stages,
        )


def test_missing_historical_query_contract_does_not_upgrade_to_v3() -> None:
    manifest, case, stages, matching = _public_output(
        frozen_version=None, executed_version="general-reference-v2"
    )
    assert decode_evaluation_output(
        matching, expected_manifest=manifest, expected_case=case,
        expected_stage_identity=stages,
    ).record.case_id == case.case_id
    manifest, case, stages, upgraded = _public_output(
        frozen_version=None, executed_version="general-reference-v3"
    )
    with pytest.raises(ExperimentContractError, match="evidence rules differ"):
        decode_evaluation_output(
            upgraded, expected_manifest=manifest, expected_case=case,
            expected_stage_identity=stages,
        )


def _implicit_bm25_spec(*, freeze_settings: bool = False, **parameters) -> EvaluationRuntimeSpec:
    original = _manifest([_case()], generate=False)
    contracts = deepcopy(original["contracts"])
    contracts["retrieval"]["kind"] = "bm25"
    summary = deepcopy(original["config"]["summary"])
    if freeze_settings:
        settings = BM25Settings(**parameters).to_dict()
        contracts["retrieval"]["parameters"]["bm25_settings"] = settings
        contracts["retrieval"]["query_analysis"] = {
            "version": "rules-v1",
            "reference_rules_version": "legal-reference-v3",
            "evidence_rules_version": "general-reference-v3",
        }
        summary.update(bm25_settings=settings, evidence_rules_version="general-reference-v3")
    manifest = build_experiment_manifest(
        experiment_id="synthetic-implicit-bm25-contract",
        execution_mode=original["execution_mode"],
        default_cache_mode="fresh",
        code=original["code"],
        config_summary=summary,
        corpus=original["corpus"],
        dataset=original["dataset"],
        contracts=contracts,
        runtime=original["runtime"],
        environment=original["environment"],
        created_at=original["created_at"],
    )
    return EvaluationRuntimeSpec(
        manifest=manifest,
        cache=ExactStageCache(".tmp/never-executed-bm25-contract"),
        retriever=BM25Retriever([], **parameters),
        assistant_factory=lambda: None,
        model=contracts["generation"]["model"],
        chunk_strategy="article",
        generate=False,
    )


def test_implicit_historical_bm25_contract_accepts_original_defaults() -> None:
    spec = _implicit_bm25_spec()
    retriever = spec.retriever
    assert (retriever.lexical_profile, retriever.k1, retriever.b,
            retriever.law_boost, retriever.article_boost,
            retriever.deprecated_penalty) == ("legacy-v1", 1.5, .75, 40, 80, 1.0)
    # Constructing the factory validates wiring only. No assistant, stage,
    # cache write, corpus, or model provider is executed by this test.
    LegalEvaluationRuntimeFactory(spec)


@pytest.mark.parametrize("parameters", [
    {"lexical_profile": "generic-v3"},
    {"k1": .7},
    {"b": .25},
    {"law_boost": 0},
    {"article_boost": 0},
    {"deprecated_penalty": .5},
])
def test_implicit_historical_bm25_contract_rejects_unfrozen_parameters(parameters) -> None:
    with pytest.raises(ExperimentContractError):
        LegalEvaluationRuntimeFactory(_implicit_bm25_spec(**parameters))


@pytest.mark.parametrize("profile", ["legacy-v1", "generic-v3"])
def test_explicit_frozen_old_engine_settings_can_keep_nondefault_parameters(profile: str) -> None:
    spec = _implicit_bm25_spec(
        freeze_settings=True, lexical_profile=profile, k1=.7, b=.25,
        law_boost=0, article_boost=0, deprecated_penalty=.5,
    )
    LegalEvaluationRuntimeFactory(spec)
