from __future__ import annotations

import hashlib
import hmac
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from legal_rag.evaluation_governance import (
    GovernanceError,
    ReviewKey,
    ReviewTrustPolicy,
    build_cross_pool_duplicate_report,
    canonical_bytes,
    content_identity,
    hash_payload,
    prepare_review_packet,
    question_fingerprints,
    strict_json_loads,
    validate_admission,
    validate_review_packet,
    validate_case_payload,
    write_json_exclusive,
)

ROOT = Path(__file__).resolve().parents[1]


def fixture_materials():
    """Explicit fake credentials, fictional corpus, never a human review."""
    now = datetime.now(timezone.utc)
    frozen = (now - timedelta(minutes=1)).isoformat()
    cases = {
        "case_schema_version": 1,
        "cases": [{"case_id": "fixture-a", "question": "虚构甲法第一条说明什么？",
                   "stratum": "explicit", "expected_behavior": "evidence_answer",
                   "targets": [{"law_title": "虚构甲法", "article_number": "第一条",
                                "law_id": "law-a", "version_id": "version-a"}]}],
    }
    pools = {"fixture-development": ["完全不同的合成练习"]}
    duplicates = build_cross_pool_duplicate_report(cases, pools)
    key = ReviewKey("fixture-key", b"fictional-review-key-for-tests-only", "fixture-reviewer",
                    "fixture-curator", test_only=True)
    policy = ReviewTrustPolicy((key,), {name: hash_payload(values) for name, values in pools.items()},
                               allow_test_credentials=True)
    protocol = {
        "protocol_schema_version": 1, "protocol_id": "fixture-protocol",
        "dataset": {"dataset_id": "fictional-holdout-simulation", "role": "sealed_holdout",
                    "case_file_sha256": hashlib.sha256(canonical_bytes(cases)).hexdigest(),
                    "case_set_sha256": hash_payload(cases), "content_identity_sha256": content_identity(cases),
                    "question_sha256": question_fingerprints(cases),
                    "case_count": 1},
        "corpus": {"snapshot_sha256": "a" * 64, "source_sha256": "b" * 64,
                   "license_sha256": "c" * 64, "as_of": "2025-01-01", "boundary": None},
        "review": {"status": "approved", "human_review_complete": True,
                   "legal_currentness_reviewed": True, "license_review_complete": True},
        "duplicate_report_sha256": hash_payload(duplicates),
        "exposure": "sealed_unexposed", "frozen_at": frozen,
        "developer_ids": ["fixture-developer"],
        "candidates": [{"candidate_id": "candidate-a", "implementation_sha256": "d" * 64,
                        "config_sha256": "e" * 64, "implementation_files": {}}],
        "metrics": ["typed_hit_at_k", "typed_target_coverage", "typed_all_required"],
        "strata": ["explicit"],
        "thresholds": {"promotion_requested": False, "minimum_paired_delta": 0.0,
                       "maximum_stratum_regressions": 0},
        "budget": {"external_calls": 0}, "top_k": 5,
    }
    receipt = {
        "receipt_schema_version": 1, "key_id": key.key_id,
        "payload": {"purpose": "legal_holdout_admission", "protocol_sha256": hash_payload(protocol),
                    "reviewer_id": key.reviewer_id, "curator_id": key.curator_id,
                    "independent": True, "reviewed_at": frozen, "expires_at": (now + timedelta(days=1)).isoformat(),
                    "test_only": True},
    }
    receipt["mac_sha256"] = hmac.new(key.secret, canonical_bytes(receipt["payload"]), hashlib.sha256).hexdigest()
    return cases, protocol, receipt, duplicates, policy


def test_real_legacy_review_packet_is_pending_and_cannot_self_approve():
    packet = prepare_review_packet("legal-eval-v3", repository_root=ROOT)
    validated = validate_review_packet(packet)
    assert len(validated["cases"]) == 120
    assert validated["human_review_complete"] is False
    assert validated["holdout_admitted"] is False
    assert all(row["review_state"] == "pending" and row["reviewer_id"] is None for row in validated["cases"])
    packet["human_review_complete"] = True
    with pytest.raises(GovernanceError):
        validate_review_packet(packet)


def test_trusted_mac_simulation_is_not_a_real_legal_holdout_admission():
    _, protocol, receipt, duplicates, policy = fixture_materials()
    admitted = validate_admission(protocol, receipt, duplicates, policy)
    assert admitted.execution_allowed
    assert admitted.simulation is True
    assert admitted.legal_holdout_admitted is False


@pytest.mark.parametrize("change", ["mac", "key", "test", "purpose", "reviewer", "independence", "expired"])
def test_forged_wrong_or_test_credentials_fail_closed(change):
    _, protocol, receipt, duplicates, policy = fixture_materials()
    if change == "mac":
        receipt["mac_sha256"] = "f" * 64
    elif change == "key":
        receipt["key_id"] = "self-appointed-key"
    elif change == "test":
        policy = ReviewTrustPolicy(policy.keys, policy.development_pools)
    else:
        fields = {"purpose": ("purpose", "other-purpose"), "reviewer": ("reviewer_id", "developer"),
                  "independence": ("independent", False), "expired": ("expires_at", "2000-01-01T00:00:00Z")}
        field, value = fields[change]
        receipt["payload"][field] = value
        receipt["mac_sha256"] = hmac.new(policy.keys[0].secret, canonical_bytes(receipt["payload"]), hashlib.sha256).hexdigest()
    with pytest.raises(GovernanceError):
        validate_admission(protocol, receipt, duplicates, policy)


@pytest.mark.parametrize("field", ["dataset", "corpus", "review", "candidates", "thresholds", "frozen_at"])
def test_every_protocol_binding_is_authenticated(field):
    _, protocol, receipt, duplicates, policy = fixture_materials()
    protocol = deepcopy(protocol)
    if field == "frozen_at":
        protocol[field] = "2000-01-01T00:00:00Z"
    elif field == "candidates":
        protocol[field][0]["implementation_sha256"] = "f" * 64
    elif field == "thresholds":
        protocol[field]["minimum_paired_delta"] = 0.1
    else:
        protocol[field][next(iter(protocol[field]))] = "tampered"
    with pytest.raises(GovernanceError):
        validate_admission(protocol, receipt, duplicates, policy)


def test_cross_pool_duplicate_report_detects_normalization_and_pool_omission():
    cases, protocol, receipt, _, policy = fixture_materials()
    question = cases["cases"][0]["question"]
    duplicate = build_cross_pool_duplicate_report(cases, {"fixture-development": [question]})
    assert duplicate["unresolved_count"] == 1
    with pytest.raises(GovernanceError):
        validate_admission(protocol, receipt, duplicate, policy)
    duplicate = build_cross_pool_duplicate_report(cases, {})
    assert duplicate["development_pools"] == {}
    with pytest.raises(GovernanceError):
        validate_admission(protocol, receipt, duplicate, policy)


def test_content_identity_ignores_case_ids_order_and_gold():
    cases, *_ = fixture_materials()
    changed = deepcopy(cases)
    changed["cases"][0]["case_id"] = "other-run-other-id"
    changed["cases"][0]["targets"][0]["law_id"] = "other-gold-id"
    assert content_identity(changed) == content_identity(cases)


def test_governed_case_uses_existing_needs_clarification_mode():
    cases, *_ = fixture_materials()
    cases["cases"][0]["expected_behavior"] = "needs_clarification"
    cases["cases"][0]["targets"] = []
    assert validate_case_payload(cases)["cases"][0]["expected_behavior"] == "needs_clarification"


def test_answer_missing_gold_and_alias_duplicate_targets_are_rejected():
    cases, *_ = fixture_materials()
    missing = deepcopy(cases)
    missing["cases"][0]["targets"] = []
    with pytest.raises(GovernanceError, match="answer_missing_typed_gold"):
        validate_case_payload(missing)
    alias = deepcopy(cases["cases"][0]["targets"][0])
    alias["law_title"] = "中华人民共和国虚构甲法"
    alias["article_number"] = "第1条"
    cases["cases"][0]["targets"].append(alias)
    with pytest.raises(GovernanceError, match="duplicate_targets"):
        validate_case_payload(cases)


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}',
                                 b'{"a":1e999}', b'{"a":"\\ud800"}', b'\xef\xbb\xbf{}', b'\xff'])
def test_strict_utf8_and_json_reject_ambiguous_inputs(raw):
    with pytest.raises(GovernanceError):
        strict_json_loads(raw)


def test_exclusive_safe_output_refuses_overwrite_escape_and_symlink(tmp_path):
    output = tmp_path / "packet.json"
    write_json_exclusive(output, {"中文": "可读"}, root=tmp_path)
    assert output.read_bytes().startswith(b'{')
    with pytest.raises(GovernanceError):
        write_json_exclusive(output, {}, root=tmp_path)
    with pytest.raises(GovernanceError):
        write_json_exclusive(tmp_path.parent / "escape.json", {}, root=tmp_path)
    link = tmp_path / "linked"
    try:
        link.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        return  # The path rejection below is additionally tested with mocked reparse metadata.
    with pytest.raises(GovernanceError):
        write_json_exclusive(link / "bad.json", {}, root=tmp_path)


def test_reparse_parent_rejected_even_when_host_cannot_create_symlink(tmp_path, monkeypatch):
    import legal_rag.evaluation_governance as governance
    parent = tmp_path / "reparse-parent"
    parent.mkdir()
    original = governance._is_reparse
    monkeypatch.setattr(governance, "_is_reparse", lambda path: path == parent or original(path))
    with pytest.raises(GovernanceError, match="unsafe_reparse_path"):
        write_json_exclusive(parent / "packet.json", {}, root=tmp_path)


def test_pending_review_and_repeated_development_exposure_refuse_before_mac():
    _, protocol, receipt, duplicates, policy = fixture_materials()
    protocol["review"]["status"] = "pending"
    with pytest.raises(GovernanceError, match="human_review_pending"):
        validate_admission(protocol, receipt, duplicates, policy)
    protocol["review"]["status"] = "approved"
    protocol["exposure"] = "repeated_development"
    with pytest.raises(GovernanceError, match="already_exposed"):
        validate_admission(protocol, receipt, duplicates, policy)


def test_duplicate_pool_binding_remains_required_with_a_valid_new_mac():
    _, protocol, receipt, duplicates, policy = fixture_materials()
    duplicates["development_pools"] = {}
    protocol["duplicate_report_sha256"] = hash_payload(duplicates)
    receipt["payload"]["protocol_sha256"] = hash_payload(protocol)
    receipt["mac_sha256"] = hmac.new(policy.keys[0].secret, canonical_bytes(receipt["payload"]), hashlib.sha256).hexdigest()
    with pytest.raises(GovernanceError, match="development_pool_omitted"):
        validate_admission(protocol, receipt, duplicates, policy)


def _resigned(protocol, receipt, policy):
    receipt = deepcopy(receipt)
    receipt["payload"]["protocol_sha256"] = hash_payload(protocol)
    receipt["mac_sha256"] = hmac.new(policy.keys[0].secret, canonical_bytes(receipt["payload"]), hashlib.sha256).hexdigest()
    return receipt


def test_pending_packet_cannot_relabel_legacy_data_as_sealed_unexposed():
    packet = prepare_review_packet("legal-eval-v3", repository_root=ROOT)
    packet["source_exposure"]["status"] = "sealed_unexposed"
    with pytest.raises(GovernanceError, match="invalid_packet_exposure"):
        validate_review_packet(packet)


@pytest.mark.parametrize("dataset_id", ["legal-eval-v3-generation-30", "synthetic-offline-v1"])
def test_review_packet_retains_the_existing_registry_role_and_exposure(dataset_id):
    packet = prepare_review_packet(dataset_id, repository_root=ROOT)
    assert validate_review_packet(packet)["holdout_admitted"] is False
    assert packet["source_exposure"]["status"] == ("synthetic" if dataset_id.startswith("synthetic") else "repeated_development")


def test_simulation_duplicate_report_still_requires_a_boolean_family_review_state():
    _, protocol, receipt, duplicates, policy = fixture_materials()
    duplicates["family_review_complete"] = "not-an-actual-boolean"
    protocol["duplicate_report_sha256"] = hash_payload(duplicates)
    with pytest.raises(GovernanceError, match="invalid_duplicate_review_state"):
        validate_admission(protocol, _resigned(protocol, receipt, policy), duplicates, policy)


def test_even_signed_production_shape_refuses_missing_family_and_execution_evidence():
    """Injected fictional issuer, negative tests only, not an actual review."""
    _, protocol, receipt, duplicates, original_policy = fixture_materials()
    fake = original_policy.keys[0]
    policy = ReviewTrustPolicy((ReviewKey(fake.key_id, fake.secret, fake.reviewer_id, fake.curator_id, test_only=False),),
                               original_policy.development_pools)
    receipt["payload"]["test_only"] = False
    with pytest.raises(GovernanceError, match="family_review_pending"):
        validate_admission(protocol, _resigned(protocol, receipt, policy), duplicates, policy)
    duplicates["family_review_complete"] = True
    protocol["duplicate_report_sha256"] = hash_payload(duplicates)
    with pytest.raises(GovernanceError, match="production_execution_proof_incomplete"):
        validate_admission(protocol, _resigned(protocol, receipt, policy), duplicates, policy)


@pytest.mark.parametrize("condition", ["developer-reviewer", "near-duplicate", "weakened-threshold", "incomplete-review"])
def test_valid_mac_never_overrides_independent_review_and_duplicate_gates(condition):
    _, protocol, receipt, duplicates, policy = fixture_materials()
    if condition == "developer-reviewer":
        protocol["developer_ids"].append(policy.keys[0].reviewer_id)
        expected = "review_not_independent"
    elif condition == "near-duplicate":
        duplicates["near_duplicate_pairs"] = [{"fixture": "unresolved"}]
        duplicates["unresolved_count"] = 1
        expected = "near_duplicate_unresolved"
    elif condition == "weakened-threshold":
        duplicates["threshold"] = 0.95
        expected = "weakened_duplicate_threshold"
    else:
        protocol["review"]["legal_currentness_reviewed"] = False
        expected = "human_review_pending"
    protocol["duplicate_report_sha256"] = hash_payload(duplicates)
    with pytest.raises(GovernanceError, match=expected):
        validate_admission(protocol, _resigned(protocol, receipt, policy), duplicates, policy)
