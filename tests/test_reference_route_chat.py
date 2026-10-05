from copy import deepcopy
from dataclasses import replace

import pytest

from legal_rag.chat import LegalChatAssistant
from legal_rag.chat_artifacts import retrieved_turn_from_artifact, retrieved_turn_to_artifact
from legal_rag.models import Chunk, SearchResult, VerificationContext
from legal_rag.retrieval_contracts import RetrievalBoundaryViolation
from legal_rag.retrieval_outcomes import RetrievalOutcome


def result(*, law="甲法", article="第十条", scope="scope-a"):
    return SearchResult(Chunk("synthetic-a", "合成条文正文", [law], [article], [], [], "article",
                              {"scope_id": scope, "snapshot_id": "snapshot-a"}), 1.0, 1, "exact_reference")


class Router:
    name = "synthetic_router"
    known_law_hints = ("甲法", "乙法")

    def __init__(self, *, status="found", results=None, lexical=False):
        self.results = tuple(results if results is not None else [result()] if status == "found" else [])
        self.status, self.lexical = status, lexical
        self.exact_calls, self.plain_calls = 0, 0

    def retrieve_outcome(self, query, top_k=5):
        self.exact_calls += 1
        pairs = () if self.lexical else (("甲法", "第十条"),)
        return RetrievalOutcome(self.results[:top_k], "lexical" if self.lexical else "exact_reference",
                                self.status, ("synthetic_route",), pairs,
                                pairs if self.status == "found" else ())

    def retrieve(self, query, top_k=5):
        self.plain_calls += 1
        # An exact miss must never reach this superficially plausible fuzzy result.
        return list(self.results[:top_k] or (result(),))


class Generator:
    def __init__(self):
        self.calls = 0

    def complete(self, prompt):
        self.calls += 1
        raise AssertionError("terminal reference route must not generate")


def staged(router, **kwargs):
    generator = Generator()
    assistant = LegalChatAssistant(router, model="synthetic", completion_client=generator, **kwargs)
    retrieved = assistant.retrieve_turn(assistant.prepare_question("请解释《甲法》第十条"))
    return assistant, generator, retrieved


def test_exact_found_uses_one_routed_request_and_preserves_route_facts():
    router = Router()
    assistant, _, retrieved = staged(router, adaptive_enabled=True, adaptive_use_llm=True)
    assert router.exact_calls == 1 and router.plain_calls == 0
    assert retrieved.route_outcome.status == "found"
    assert retrieved.route_outcome.results == retrieved.results
    assert retrieved.evidence_check.sufficient
    assert not retrieved.adaptive_result.adaptive_used
    assert assistant.verify_turn(assistant.generate_turn(retrieved, generate=False)).verification is None


def test_direct_terminal_mutation_cannot_bypass_the_shared_artifact_contract():
    assistant, generator, retrieved = staged(Router(status="not_found"))
    altered = replace(retrieved, terminal_answer=replace(
        retrieved.terminal_answer, limitations=["合成断言保证任何人无条件获得赔偿。"],
    ))
    with pytest.raises((ValueError, RuntimeError), match="contract"):
        assistant.generate_turn(altered, generate=True)
    assert generator.calls == 0


def test_explicit_current_request_is_not_rewritten_with_cancelled_history():
    assistant = LegalChatAssistant(Router(), model="synthetic", completion_client=Generator())
    assistant.memory.add_turn("解释《甲法》第十条。", "上一轮合成回答。")
    current = "上一条不用解释了，现在只解释《乙法》第五条。"
    prepared = assistant.prepare_question(current)
    assert prepared.original_question == current
    assert prepared.standalone_question == current
    from legal_rag.legal_references import parse_legal_references
    assert {(r.law_title, r.article_number) for r in parse_legal_references(
        prepared.standalone_question, known_law_titles=("甲法", "乙法"),
    ).requirements} == {("乙法", "第五条")}


@pytest.mark.parametrize("current", ["上一条不要解释《乙法》第五条。", "上一条不是《乙法》第五条。"])
def test_current_excluded_references_are_not_replaced_with_historical_pairs(current):
    assistant = LegalChatAssistant(Router(), model="synthetic", completion_client=Generator())
    assistant.memory.add_turn("解释《甲法》第十条。", "上一轮合成回答。")
    assert assistant.prepare_question(current).standalone_question == current


@pytest.mark.parametrize("prefix", ["", "第三百条；"])
def test_reference_overflow_is_bounded_clarification_without_generation(prefix):
    class OverflowRouter(Router):
        def retrieve_outcome(self, query, top_k=5):
            self.exact_calls += 1
            return RetrievalOutcome((), "exact_reference", "needs_disambiguation", ("too_many_references",))

    router, generator = OverflowRouter(), Generator()
    assistant = LegalChatAssistant(router, model="synthetic", completion_client=generator)
    question = prefix + "请解释" + "、".join(f"《甲法》第{number}条" for number in range(1, 18))
    retrieved = assistant.retrieve_turn(assistant.prepare_question(question))
    assert not retrieved.results and not retrieved.evidence_check.sufficient
    assert retrieved.terminal_answer.answer_mode == "needs_clarification"
    restored = retrieved_turn_from_artifact(retrieved_turn_to_artifact(retrieved), prepared=retrieved.prepared)
    assistant.commit_turn(assistant.verify_turn(assistant.generate_turn(restored, generate=True)))
    assert router.exact_calls == 1 and router.plain_calls == generator.calls == 0


def test_small_query_cannot_forge_the_special_overflow_identity():
    from legal_rag.chat import validate_reference_route
    outcome = RetrievalOutcome((), "exact_reference", "needs_disambiguation", ("too_many_references",))
    with pytest.raises(ValueError, match="requirements"):
        validate_reference_route(outcome, "请解释《甲法》第十条")


@pytest.mark.parametrize("status,mode", [("not_found", "insufficient_evidence"),
                                         ("needs_disambiguation", "needs_clarification")])
def test_exact_nonfound_cannot_fuzzy_followup_or_generate(status, mode):
    router = Router(status=status)
    assistant, generator, retrieved = staged(router)
    assert router.exact_calls == 1 and router.plain_calls == 0
    assert retrieved.route_outcome.status == status
    assert not retrieved.evidence_check.sufficient and not retrieved.evidence_check.followup_queries
    assert retrieved.terminal_answer.answer_mode == mode
    verified = assistant.verify_turn(assistant.generate_turn(retrieved, generate=True))
    assert verified.verification.passed and verified.final_answer.answer_mode == mode
    assert generator.calls == 0
    with pytest.raises(ValueError, match="exact"):
        assistant.merge_followup_turn(retrieved, [result()], queries=["补检索"])


def test_exact_version_ambiguity_wins_even_when_partial_chunks_cover_requested_pair():
    _, _, retrieved = staged(Router(status="needs_disambiguation", results=[result()]))
    assert not retrieved.evidence_check.sufficient
    assert retrieved.terminal_answer.answer_mode == "needs_clarification"


def test_scope_filter_cannot_leave_exact_found_or_resolved_coverage():
    router = Router(results=[result(scope="scope-b")])
    _, _, retrieved = staged(router, verification_context=VerificationContext(
        snapshot_id="snapshot-a", allowed_scope_ids=["scope-a"]))
    assert retrieved.route_outcome.status == "not_found"
    assert retrieved.route_outcome.resolved_pairs == () and retrieved.route_outcome.results == ()
    assert not retrieved.evidence_check.sufficient and not retrieved.evidence_check.followup_queries


def test_exact_claimed_coverage_must_match_actual_owned_chunks():
    with pytest.raises((ValueError, RetrievalBoundaryViolation)):
        staged(Router(results=[result(law="乙法", article="第二十条")]))


def test_route_snapshot_mutation_cannot_pass_stage_validation():
    assistant, _, retrieved = staged(Router())
    changed = deepcopy(retrieved)
    changed.route_outcome.results[0].chunk.metadata["route_only_drift"] = True
    with pytest.raises(RetrievalBoundaryViolation):
        assistant.generate_turn(changed, generate=False)


def test_routed_artifact_is_versioned_roundtrips_and_rejects_downgrade():
    _, _, retrieved = staged(Router())
    artifact = retrieved_turn_to_artifact(retrieved)
    assert artifact["artifact_schema_version"] == 2
    restored = retrieved_turn_from_artifact(artifact, prepared=retrieved.prepared)
    assert restored == retrieved
    downgraded = deepcopy(artifact)
    downgraded["artifact_schema_version"] = 1
    with pytest.raises(ValueError):
        retrieved_turn_from_artifact(downgraded, prepared=retrieved.prepared)


def test_lexical_query_keeps_existing_adaptive_path_and_does_not_double_retrieve():
    router = Router(lexical=True)
    assistant = LegalChatAssistant(router, model="synthetic", completion_client=Generator())
    retrieved = assistant.retrieve_turn(assistant.prepare_question("一般法律文本学习问题"), max_followup_rounds=0)
    assert router.plain_calls == 1 and router.exact_calls == 0
    assert retrieved.route_outcome.route == "lexical"
    assert retrieved.route_outcome.results == retrieved.results


def test_route_artifact_cannot_claim_ambiguous_result_was_publishable():
    _, _, retrieved = staged(Router(status="needs_disambiguation", results=[result()]))
    # Keep all non-route fields coherent; the route authority must be checked too.
    checked = replace(retrieved.evidence_check, sufficient=True, missing_facts=[], missing_law_support=[])
    forged = replace(retrieved, evidence_check=checked,
                     adaptive_result=replace(retrieved.adaptive_result, evidence_check=checked),
                     terminal_kind=None, terminal_answer=None, terminal_expected_answer_mode=None)
    with pytest.raises(ValueError):
        retrieved_turn_to_artifact(forged)


def test_unpaired_article_requires_clarification_without_generation():
    class Plain:
        name = "synthetic"
        def retrieve(self, query, top_k=5):
            return [result()]
    generator = Generator()
    assistant = LegalChatAssistant(Plain(), model="synthetic", completion_client=generator)
    _, _ = assistant.answer("第十条规定什么？", generate=True)
    assert assistant.last_structured_answer.answer_mode == "needs_clarification"
    assert generator.calls == 0


def test_legacy_artifact_is_viewable_but_not_executable_as_modern_evidence():
    assistant, _, retrieved = staged(Router())
    old = deepcopy(retrieved_turn_to_artifact(retrieved))
    old["artifact_schema_version"] = 1
    old["payload"].pop("route_outcome")
    for payload in (old["payload"]["evidence_check"], old["payload"]["adaptive_result"]["evidence_check"]):
        payload.pop("rules_version")
        payload.pop("mechanical_check")
    viewed = retrieved_turn_from_artifact(old, prepared=retrieved.prepared)
    assert viewed.evidence_check.rules_version == "legacy-hints-and-return-v1"
    assert viewed.evidence_check.mechanical_check is None
    # A historical view cannot implicitly acquire modern execution authority.
    with pytest.raises((ValueError, RetrievalBoundaryViolation), match="rules"):
        assistant.generate_turn(viewed, generate=False)


def test_legacy_evidence_serialization_retains_old_fields_for_hash_compatibility():
    _, _, retrieved = staged(Router())
    evidence = replace(retrieved.evidence_check, rules_version="legacy-hints-and-return-v1", mechanical_check=None)
    historical = replace(retrieved, route_outcome=None, evidence_check=evidence,
                         adaptive_result=replace(retrieved.adaptive_result, evidence_check=evidence))
    artifact = retrieved_turn_to_artifact(historical)
    assert artifact["artifact_schema_version"] == 1
    assert "rules_version" not in artifact["payload"]["evidence_check"]
    assert "mechanical_check" not in artifact["payload"]["adaptive_result"]["evidence_check"]
    assert retrieved_turn_from_artifact(artifact, prepared=historical.prepared) == historical


def test_modern_sufficient_artifact_requires_actual_mechanical_assessment():
    _, _, retrieved = staged(Router())
    evidence = replace(retrieved.evidence_check, mechanical_check=None)
    forged = replace(retrieved, evidence_check=evidence,
                     adaptive_result=replace(retrieved.adaptive_result, evidence_check=evidence))
    with pytest.raises(ValueError, match="mechanical"):
        retrieved_turn_to_artifact(forged)


def test_coherently_changed_score_cannot_reuse_old_sufficient_stage():
    assistant, _, retrieved = staged(Router())
    changed_results = (replace(retrieved.results[0], score=-1.0),)
    forged = replace(retrieved, results=changed_results,
                     adaptive_result=replace(retrieved.adaptive_result, results=list(changed_results)),
                     route_outcome=replace(retrieved.route_outcome, results=changed_results))
    with pytest.raises(RetrievalBoundaryViolation, match="mechanical"):
        assistant.generate_turn(forged, generate=False)
