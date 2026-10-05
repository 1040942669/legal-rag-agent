from decimal import Decimal

import pytest

from legal_rag.services.execution_policy import GenerationPolicy, ServiceExecutionPolicy
from legal_rag.retrieval_outcomes import RetrievalOutcome


def test_default_policy_is_provider_disabled_and_legacy() -> None:
    policy = ServiceExecutionPolicy()
    assert policy.generation.enabled is False
    assert policy.lexical_profile == "legacy-v1"
    assert ServiceExecutionPolicy.from_dict(policy.to_dict()) == policy
    assert len(policy.fingerprint) == 64


def test_price_is_decimal_and_missing_ack_or_scope_is_rejected() -> None:
    with pytest.raises(ValueError):
        GenerationPolicy(enabled=True)
    policy = GenerationPolicy(
        enabled=True, model="Qwen/test", allowed_scope_ids=("public",),
        pricing_acknowledged=True, egress_acknowledged=True,
        price_revision="operator-confirmed-test", input_rate="0.4", output_rate="3.2",
        budget="1", max_prompt_bytes=1000, max_output_tokens=100,
    )
    assert policy.input_rate == Decimal("0.4")
    assert policy.reservation("中文") == Decimal("0.0004248")
    assert policy.to_dict()["budget"] == "1"


@pytest.mark.parametrize("changes", [
    {"budget": float("nan")}, {"input_rate": -1}, {"max_output_tokens": True},
    {"base_url": "http://untrusted.test"}, {"api_key_env": "bad name"},
])
def test_invalid_generation_policy_fails_closed(changes) -> None:
    with pytest.raises(ValueError):
        GenerationPolicy(**changes)


def test_route_outcome_cannot_claim_found_without_all_pairs() -> None:
    with pytest.raises(ValueError):
        RetrievalOutcome(results=(), route="exact_reference", status="found",
                         requested_pairs=(("甲法", "第一条"),), resolved_pairs=())


def test_policy_unknown_fields_or_historical_paid_upgrade_rejected() -> None:
    with pytest.raises(ValueError):
        ServiceExecutionPolicy.from_dict({"schema_version": 1, "api_key": "not-a-key"})
    assert ServiceExecutionPolicy.historical().generation.enabled is False
    assert ServiceExecutionPolicy.historical().exact_reference_routing is False


def test_semantic_policy_freeze_is_detached_and_has_no_free_enabled_dict() -> None:
    from legal_rag.semantic import SemanticPolicy
    checker = SemanticPolicy("checker", "r1", "p1", "a" * 64, True)
    with pytest.raises(ValueError):
        ServiceExecutionPolicy(semantic_policy=checker.to_dict())
    generation = GenerationPolicy(enabled=True, model="Qwen/test", allowed_scope_ids=("scope",),
        egress_acknowledged=True, pricing_acknowledged=True, price_revision="r1", input_rate="0.4", output_rate="3.2", budget="1")
    source = checker.to_dict()
    policy = ServiceExecutionPolicy(generation=generation, semantic_policy=source)
    source["checker_revision"] = "mutated"
    assert policy.semantic_policy == checker
    assert ServiceExecutionPolicy.from_dict(policy.to_dict()) == policy


def test_route_artifact_rejects_string_instead_of_array():
    outcome = RetrievalOutcome((), "exact_reference", "needs_disambiguation", ("unresolved_reference",))
    value = outcome.to_dict()
    value["requested_pairs"] = ["甲法"]
    with pytest.raises(ValueError):
        RetrievalOutcome.from_dict(value, results=())


def test_environment_policy_rejects_duplicate_authority_fields(monkeypatch):
    monkeypatch.setenv("LEGAL_RAG_EXECUTION_POLICY_JSON", '{"schema_version":1,"schema_version":1}')
    with pytest.raises(ValueError):
        ServiceExecutionPolicy.from_environment()


def test_policy_hash_matches_storage_canonical_unicode_hash():
    from legal_rag.services.run_service import canonical_json_sha256
    policy = ServiceExecutionPolicy(generation=GenerationPolicy(price_revision="人工确认版本"))
    assert policy.fingerprint == canonical_json_sha256(policy.to_dict())
