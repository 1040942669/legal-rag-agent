import json
from copy import deepcopy
from dataclasses import replace

import pytest

from legal_rag.chat import LegalChatAssistant
from legal_rag.chat_artifacts import generated_turn_from_artifact, generated_turn_to_artifact
from legal_rag.models import Chunk, SearchResult
from legal_rag.retrieval_contracts import RetrievalBoundaryViolation
from legal_rag.semantic import SemanticAssessment, SemanticPolicy, SegmentAssessment


class FakeRetriever:
    name = "bm25"

    def retrieve(self, question, top_k=5):
        return [SearchResult(Chunk("a", "合成依据正文", [], [], [], [], "synthetic"), 100.0, 1, "bm25")]


class FakeGenerator:
    def complete(self, prompt):
        return json.dumps({"answer_text": "合成结论[S1]。", "answer_mode": "evidence_answer",
                           "claims": [{"claim_id": "c1", "text": "合成结论", "source_ids": ["S1"]}],
                           "limitations": ["仅限所给资料。"], "clarification_question": None}, ensure_ascii=False)


class FakeChecker:
    def __init__(self, policy, status="supported"):
        self.policy = policy
        self.status = status
        self.calls = 0

    def assess(self, request):
        self.calls += 1
        return SemanticAssessment(request.fingerprint, request.policy.fingerprint,
                                  self.policy.checker_id, self.policy.checker_revision,
                                  self.policy.prompt_version,
                                  tuple(SegmentAssessment(segment.segment_id, self.status, ("S1",))
                                        for segment in request.segments))


def staged(*, status="supported", checker_present=True, required=True):
    policy = SemanticPolicy("fake", "r1", "p1", "a" * 64, required)
    checker = FakeChecker(policy, status) if checker_present else None
    assistant = LegalChatAssistant(FakeRetriever(), model="fake", completion_client=FakeGenerator(),
                                   semantic_policy=policy, semantic_checker=checker)
    retrieved = assistant.retrieve_turn(assistant.prepare_question("合成一般问题"), max_followup_rounds=0)
    generated = assistant.generate_turn(retrieved, generate=True)
    return assistant, checker, generated


def test_assessment_is_explicit_and_reverification_never_repeats_provider_call():
    assistant, checker, generated = staged()
    assert checker.calls == 0
    assessed = assistant.assess_turn(generated)
    assert checker.calls == 1
    assert assistant.assess_turn(assessed) == assessed
    verified = assistant.verify_turn(assessed)
    assert verified.verification.passed and verified.verification.semantic_support_status == "supported"
    assert assistant.verify_turn(assessed) == verified
    assistant.commit_turn(verified)
    assert checker.calls == 1


@pytest.mark.parametrize("status", ["unsupported", "uncertain", "not_checked", "error"])
def test_required_failure_preserved_but_safe_fallback_is_publishable(status):
    assistant, checker, generated = staged(status=status)
    verified = assistant.verify_turn(assistant.assess_turn(generated))
    assert checker.calls == 1
    assert verified.pre_fallback_verification is not None
    assert not verified.pre_fallback_verification.passed
    assert verified.pre_fallback_verification.semantic_support_status == status
    assert verified.final_answer.answer_mode == "insufficient_evidence"
    assert verified.verification.passed and verified.verification.semantic_support_status == "not_checked"
    assistant.commit_turn(verified)
    assert checker.calls == 1


def test_required_absent_checker_does_not_claim_supported():
    assistant, _, generated = staged(checker_present=False)
    verified = assistant.verify_turn(assistant.assess_turn(generated))
    assert not verified.pre_fallback_verification.passed
    assert verified.pre_fallback_verification.semantic_support_status == "not_checked"
    assert verified.verification.semantic_support_status == "not_checked"


def test_changed_policy_or_mutated_evidence_cannot_reuse_assessment():
    assistant, _, generated = staged()
    assessed = assistant.assess_turn(generated)
    altered = deepcopy(assessed)
    altered.retrieved.results[0].chunk.metadata["mutated"] = True
    with pytest.raises(RetrievalBoundaryViolation):
        assistant.verify_turn(altered)
    # Even matching mutations of both unbound snapshots cannot reuse a checker
    # assessment. A bound PostgreSQL corpus rejects the mutation earlier.
    altered.retrieved.adaptive_result.results[0].chunk.metadata["mutated"] = True
    verified = assistant.verify_turn(altered)
    assert not verified.pre_fallback_verification.passed
    assert "semantic_input_mismatch" in verified.pre_fallback_verification.failure_reasons


def test_retrieval_only_and_programmatic_terminal_do_not_run_checker():
    assistant, checker, generated = staged()
    only = assistant.generate_turn(generated.retrieved, generate=False)
    assert assistant.assess_turn(only) == only
    assert assistant.verify_turn(only).verification is None
    assert checker.calls == 0


def test_bound_assessment_artifact_roundtrip_and_no_schema_downgrade():
    assistant, checker, generated = staged()
    assessed = assistant.assess_turn(generated)
    artifact = generated_turn_to_artifact(assessed)
    assert artifact["artifact_schema_version"] == 2
    restored = generated_turn_from_artifact(artifact, retrieved=assessed.retrieved)
    assert restored == assessed
    assert assistant.verify_turn(restored).verification.semantic_support_status == "supported"
    assert checker.calls == 1
    downgraded = deepcopy(artifact)
    downgraded["artifact_schema_version"] = 1
    with pytest.raises(ValueError):
        generated_turn_from_artifact(downgraded, retrieved=assessed.retrieved)


def test_old_structural_artifact_cannot_gain_new_required_semantic_pass():
    assistant, checker, generated = staged()
    from dataclasses import replace
    historical = replace(generated, semantic_policy_fingerprint=None)
    artifact = generated_turn_to_artifact(historical)
    assert artifact["artifact_schema_version"] == 1
    restored = generated_turn_from_artifact(artifact, retrieved=historical.retrieved)
    verified = assistant.verify_turn(restored)
    assert verified.pre_fallback_verification is not None and not verified.pre_fallback_verification.passed
    assert "semantic_policy_mismatch" in verified.pre_fallback_verification.failure_reasons
    assert checker.calls == 0


def test_legacy_verification_is_explicit_and_cannot_enable_semantic_policy():
    policy = SemanticPolicy("fake", "r1", "p1", "a" * 64, True)
    with pytest.raises(ValueError, match="legacy"):
        LegalChatAssistant(FakeRetriever(), model="fake", completion_client=FakeGenerator(),
                           semantic_policy=policy, verification_rules_version="m1",
                           evidence_rules_version="legacy-hints-and-return-v1")
    assistant = LegalChatAssistant(FakeRetriever(), model="fake", completion_client=FakeGenerator(),
                                   verification_rules_version="m1", evidence_rules_version="legacy-hints-and-return-v1")
    assert assistant.verification_rules_version == "m1"


def test_nonmodel_turn_cannot_carry_semantic_authority_even_without_artifact_decoder():
    assistant, _, generated = staged()
    only = assistant.generate_turn(generated.retrieved, generate=False)
    with pytest.raises((ValueError, RuntimeError), match="semantic"):
        assistant.verify_turn(replace(only, semantic_policy_fingerprint=assistant.semantic_policy.fingerprint))


@pytest.mark.parametrize("error", ["checker_error", "checker_unavailable", "input_invalid"])
def test_supported_assessment_cannot_override_an_explicit_checker_failure(error):
    assistant, checker, generated = staged()
    assessed = replace(assistant.assess_turn(generated), semantic_error=error)
    with pytest.raises((ValueError, RuntimeError), match="semantic"):
        assistant.verify_turn(assessed)
    with pytest.raises(ValueError, match="semantic"):
        generated_turn_to_artifact(assessed)
    assert checker.calls == 1


def test_checker_exception_produces_consistent_error_assessment_and_safe_fallback():
    assistant, checker, generated = staged()

    def fail(request):
        checker.calls += 1
        raise RuntimeError("synthetic checker failure")

    checker.assess = fail
    assessed = assistant.assess_turn(generated)
    assert assessed.semantic_error == "checker_error"
    assert assessed.semantic_assessment.decisions
    assert all(decision.status == "error" for decision in assessed.semantic_assessment.decisions)
    restored = generated_turn_from_artifact(generated_turn_to_artifact(assessed), retrieved=assessed.retrieved)
    verified = assistant.verify_turn(restored)
    assert verified.pre_fallback_verification.semantic_support_status == "error"
    assert not verified.pre_fallback_verification.passed
    assistant.commit_turn(verified)
    assert checker.calls == 1
