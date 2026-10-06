from dataclasses import replace
from datetime import date

import pytest

from legal_rag.models import AnswerClaim, Chunk, SearchResult, StructuredAnswer, VerificationContext
from legal_rag.semantic import (
    CompletionSemanticChecker,
    SegmentAssessment,
    SemanticAssessment,
    SemanticPolicy,
    build_semantic_input,
    evaluate_semantic_assessment,
)
from legal_rag.verifier import verify_answer
from legal_rag.retrieval_contracts import (
    RetrievalBoundary, RetrievalProvenance, RetrievedArticleProvenance, chunk_payload_fingerprint,
)


def fixture_input(*, required=True):
    policy = SemanticPolicy("test-checker", "revision-1", "prompt-1", "a" * 64, required)
    answer = StructuredAnswer(
        "第一项断言[S1]。未在 claims 声明的第二项断言[S1]。",
        "evidence_answer",
        [AnswerClaim("c1", "第一项断言", ["S1"])],
        ["只在规定条件成立时适用。"],
        None,
    )
    chunk = Chunk("chunk-a", "合成证据正文", ["甲法"], ["第一条"], [], [], "synthetic")
    results = [SearchResult(chunk, 1.0, 1, "bm25")]
    request = build_semantic_input(
        "合成问题", answer, results, policy,
        context=VerificationContext("snapshot-a", ["scope-a"]),
        boundary_fingerprint="b" * 64,
    )
    return policy, answer, results, request


def test_bound_provenance_dates_have_strict_stable_semantic_payload_identity():
    policy, answer, results, _ = fixture_input()
    boundary = RetrievalBoundary("scope-a", "snapshot-a", "a" * 64, effective_on=date(2026, 10, 5))
    article = RetrievedArticleProvenance("article-a", "law-a", "version-a", "第一条", "甲法",
                                        date(2021, 1, 1), date(2030, 1, 1), "synthetic.txt", 1, "verified")
    provenance = RetrievalProvenance(boundary, "scope-a", "snapshot-a", "a" * 64,
                                    results[0].chunk.chunk_id, "c" * 64,
                                    chunk_payload_fingerprint(results[0].chunk), 0, None, (article,))
    bound = [replace(results[0], provenance=provenance)]
    request = build_semantic_input("合成问题", answer, bound, policy,
                                   boundary_fingerprint=boundary.fingerprint)
    from_iso = replace(article, valid_from="2021-01-01", valid_to="2030-01-01")
    same = build_semantic_input("合成问题", answer, [replace(bound[0], provenance=replace(provenance, articles=(from_iso,)))],
                               policy, boundary_fingerprint=boundary.fingerprint)
    assert request.fingerprint == same.fingerprint
    changed = build_semantic_input("合成问题", answer,
                                  [replace(bound[0], provenance=replace(provenance, articles=(replace(article, valid_from=date(2022, 1, 1)),)))],
                                  policy, boundary_fingerprint=boundary.fingerprint)
    assert changed.fingerprint != request.fingerprint
    assert not evaluate_semantic_assessment(changed, assessment_for(request)).passed


def assessment_for(request, *, status="supported"):
    return SemanticAssessment(
        request.fingerprint, request.policy.fingerprint,
        request.policy.checker_id, request.policy.checker_revision,
        request.policy.prompt_version,
        tuple(SegmentAssessment(segment.segment_id, status, ("S1",), ())
              for segment in request.segments),
    )


def test_complete_visible_output_not_only_declared_claims_is_bound():
    _, _, _, request = fixture_input()
    assert len(request.segments) == 3
    assert any("第二项断言" in segment.text for segment in request.segments)
    assert any(segment.field_name == "limitations[0]" for segment in request.segments)
    gate = evaluate_semantic_assessment(request, assessment_for(request))
    assert gate.passed and gate.status == "supported" and gate.coverage_complete


@pytest.mark.parametrize("mutation", ["draft", "question", "evidence", "scope", "boundary", "checker"])
def test_assessment_cannot_be_reused_after_any_bound_identity_changes(mutation):
    policy, answer, results, request = fixture_input()
    context = VerificationContext("snapshot-a", ["scope-a"])
    question = "合成问题"
    boundary = "b" * 64
    if mutation == "draft":
        answer = replace(answer, limitations=["另一项限制"])
    elif mutation == "question":
        question = "不同问题"
    elif mutation == "evidence":
        results = [replace(results[0], chunk=replace(results[0].chunk, text="不同正文"))]
    elif mutation == "scope":
        context = VerificationContext("snapshot-b", ["scope-a"])
    elif mutation == "boundary":
        boundary = "c" * 64
    else:
        policy = replace(policy, checker_revision="revision-2")
    changed = build_semantic_input(question, answer, results, policy, context=context,
                                   boundary_fingerprint=boundary)
    gate = evaluate_semantic_assessment(changed, assessment_for(request))
    assert not gate.passed and gate.status == "error"
    assert "semantic_input_mismatch" in gate.failure_reasons


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown_segment", "unknown_source", "empty_sources"])
def test_incomplete_or_unbound_decisions_fail_closed(mutation):
    _, _, _, request = fixture_input()
    assessment = assessment_for(request)
    decisions = list(assessment.decisions)
    if mutation == "missing":
        decisions.pop()
    elif mutation == "duplicate":
        decisions.append(decisions[0])
    elif mutation == "unknown_segment":
        decisions[0] = replace(decisions[0], segment_id="not-a-segment")
    elif mutation == "unknown_source":
        decisions[0] = replace(decisions[0], source_ids=("S99",))
    else:
        decisions[0] = replace(decisions[0], source_ids=())
    gate = evaluate_semantic_assessment(request, replace(assessment, decisions=tuple(decisions)))
    assert not gate.passed and gate.status == "error"


@pytest.mark.parametrize("status", ["unsupported", "uncertain", "not_checked", "error"])
def test_required_assessment_never_passes_unknown_or_negative(status):
    _, _, _, request = fixture_input()
    gate = evaluate_semantic_assessment(request, assessment_for(request, status=status))
    assert not gate.passed and gate.status == status


def test_absent_checker_preserves_unknown_and_optional_generation():
    _, _, _, required = fixture_input()
    assert not evaluate_semantic_assessment(required, None).passed
    _, _, _, optional = fixture_input(required=False)
    gate = evaluate_semantic_assessment(optional, None)
    assert gate.passed and gate.status == "not_checked"
    assert gate.assessment_fingerprint is None


def test_assessment_strict_roundtrip_rejects_extra_keys_and_duplicates():
    _, _, _, request = fixture_input()
    assessment = assessment_for(request)
    assert SemanticAssessment.from_dict(assessment.to_dict()) == assessment
    with pytest.raises(ValueError):
        SemanticAssessment.from_dict({**assessment.to_dict(), "supported": True})
    with pytest.raises(ValueError):
        SemanticAssessment.from_json('{"schema_version":1,"schema_version":1}')


def test_provider_adapter_sends_all_segments_but_never_trusts_provider_identity():
    _, _, _, request = fixture_input()

    class FakeClient:
        def __init__(self):
            self.prompts = []

        def complete(self, prompt):
            import json
            self.prompts.append(prompt)
            return json.dumps({"decisions": [decision.to_dict()
                              for decision in assessment_for(request).decisions]})

    client = FakeClient()
    checker = CompletionSemanticChecker(request.policy, client)
    result = checker.assess(request)
    assert evaluate_semantic_assessment(request, result).passed
    assert len(client.prompts) == 1 and "第二项断言" in client.prompts[0]
    assert result.input_fingerprint == request.fingerprint


def test_source_catalog_is_only_actual_cited_evidence():
    policy, answer, results, request = fixture_input()
    uncited = replace(results[0], rank=2, chunk=replace(results[0].chunk, chunk_id="b", text="未引用正文"))
    expanded = build_semantic_input("合成问题", answer, [*results, uncited], policy,
                                   context=VerificationContext("snapshot-a", ["scope-a"]),
                                   boundary_fingerprint="b" * 64)
    assert expanded.evidence == request.evidence
    assert expanded.fingerprint == request.fingerprint
    with pytest.raises(ValueError):
        build_semantic_input("合成问题", replace(answer, answer_text="未知来源[S99]。"), results, policy)


def test_verifier_requires_bound_assessment_not_free_status():
    policy, answer, results, _ = fixture_input()
    request = build_semantic_input("合成问题", answer, results, policy)
    supported = verify_answer(answer, results, semantic_policy=policy,
                              semantic_question="合成问题", semantic_assessment=assessment_for(request))
    assert supported.passed and supported.structural_passed
    assert supported.semantic_support_status == "supported"
    absent = verify_answer(answer, results, semantic_policy=policy, semantic_question="合成问题")
    assert not absent.passed and absent.structural_passed
    assert "semantic_required_not_checked" in absent.failure_reasons
    negative = verify_answer(answer, results, semantic_policy=policy, semantic_question="合成问题",
                             semantic_assessment=assessment_for(request, status="unsupported"))
    assert not negative.passed and negative.semantic_support_status == "unsupported"
    unbound = verify_answer(answer, results, semantic_support_status="supported")
    assert not unbound.passed and unbound.semantic_support_status != "supported"


def test_legacy_status_is_explicit_historical_contract_only():
    _, answer, results, _ = fixture_input()
    legacy = verify_answer(answer, results, semantic_support_status="supported", verification_rules_version="m1")
    assert legacy.semantic_support_status == "supported"
    with pytest.raises(ValueError):
        verify_answer(answer, results, verification_rules_version="unknown")
