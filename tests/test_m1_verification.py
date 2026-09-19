import json
import unittest

from legal_rag.chat import (
    LEGAL_DISCLAIMER,
    LegalChatAssistant,
    build_risk_refusal_answer,
    programmatic_answer,
)
from legal_rag.evidence import check_evidence_sufficiency
from legal_rag.models import (
    AnswerClaim,
    Chunk,
    NormalizedQuery,
    SearchResult,
    StructuredAnswer,
    VerificationContext,
)
from legal_rag.verifier import (
    build_verifier_fallback_answer,
    contains_refusal,
    parse_structured_answer,
    verify_answer,
)


class M1VerificationTest(unittest.TestCase):
    def make_result(
        self,
        *,
        rank: int = 1,
        text: str = "合成示例条文：经营者应当依法保护消费者权益。",
        snapshot_id: str = "snapshot-a",
        scope_id: str = "public",
    ) -> SearchResult:
        return SearchResult(
            chunk=Chunk(
                chunk_id=f"chunk-{rank}",
                text=text,
                law_names=["合成示例法"],
                article_numbers=["第一条"],
                source_files=["synthetic.txt"],
                line_nos=[1],
                strategy="article",
                metadata={"snapshot_id": snapshot_id, "scope_id": scope_id},
            ),
            score=1.0,
            rank=rank,
            retriever="fixture",
        )

    def make_answer(
        self,
        text: str,
        *,
        mode: str = "evidence_answer",
        source_ids: list[str] | None = None,
        limitations: list[str] | None = None,
    ) -> StructuredAnswer:
        claims = []
        if source_ids is not None:
            claims = [AnswerClaim(claim_id="C1", text=text, source_ids=source_ids)]
        return StructuredAnswer(
            answer_text=text,
            answer_mode=mode,
            claims=claims,
            limitations=limitations or [],
            clarification_question=None,
        )

    def test_m1_t01_disclaimer_is_not_a_refusal(self) -> None:
        answer = self.make_answer(
            f"经营者应当依法保护消费者权益 [S1]。\n\n{LEGAL_DISCLAIMER}",
            source_ids=["S1"],
        )

        verification = verify_answer(
            answer,
            [self.make_result()],
            expected_answer_mode="evidence_answer",
            disclaimer=LEGAL_DISCLAIMER,
        )

        self.assertFalse(contains_refusal(answer.answer_text))
        self.assertFalse(verification.refusal_present)
        self.assertIsNone(verification.refusal_correct)
        self.assertTrue(verification.response_mode_valid)

    def test_disclaimer_must_be_the_final_boundary_statement(self) -> None:
        answer = self.make_answer(
            f"{LEGAL_DISCLAIMER}\n\n经营者应当保护消费者权益 [S1]。",
            source_ids=["S1"],
        )

        verification = verify_answer(
            answer,
            [self.make_result()],
            expected_answer_mode="evidence_answer",
            disclaimer=LEGAL_DISCLAIMER,
        )

        self.assertFalse(verification.disclaimer_present)
        self.assertIn("missing_disclaimer", verification.failure_reasons)

    def test_m1_t02_legal_prohibition_words_are_not_a_refusal(self) -> None:
        answer = self.make_answer(
            f"合成示例法规定经营者不得伪造记录，也不能隐瞒重要事实 [S1]。\n\n{LEGAL_DISCLAIMER}",
            source_ids=["S1"],
        )

        verification = verify_answer(
            answer,
            [self.make_result()],
            expected_answer_mode="evidence_answer",
            disclaimer=LEGAL_DISCLAIMER,
        )

        self.assertFalse(contains_refusal(answer.answer_text))
        self.assertFalse(verification.refusal_present)
        self.assertTrue(verification.response_mode_valid)

    def test_refusal_detection_uses_explicit_assistant_behavior(self) -> None:
        self.assertFalse(contains_refusal("该系统不能提供违法操作方案，这是规则说明。"))
        self.assertFalse(contains_refusal("“我不能提供操作方案”是示例提示语。"))
        self.assertFalse(contains_refusal("该案件不属于当前法院管辖范围。"))
        self.assertFalse(contains_refusal("当事人表示，我不能提供该文件。"))
        self.assertTrue(contains_refusal("抱歉，无法回答这一问题。"))
        self.assertTrue(contains_refusal("我无法为你提供具体策略。"))
        self.assertTrue(contains_refusal("不能提供违法操作方案。"))
        self.assertTrue(contains_refusal("无法回答这个问题。"))
        self.assertTrue(contains_refusal("不能协助规避执法。"))
        self.assertTrue(
            contains_refusal(
                f"我不能提供违法操作方案。{LEGAL_DISCLAIMER}"
            )
        )

    def test_each_programmatic_risk_route_has_a_verified_refusal_mode(self) -> None:
        for risk_flag in (
            "illegal_help",
            "case_strategy",
            "medical_financial_advice",
            "non_legal",
        ):
            with self.subTest(risk_flag=risk_flag):
                answer = programmatic_answer(
                    build_risk_refusal_answer([risk_flag]),
                    answer_mode="out_of_scope",
                    limitations=["请求超出允许范围。"],
                )
                verification = verify_answer(
                    answer,
                    [],
                    risk_flags=[risk_flag],
                    expected_answer_mode="out_of_scope",
                    disclaimer=LEGAL_DISCLAIMER,
                )

                self.assertTrue(verification.refusal_present)
                self.assertTrue(verification.refusal_correct)
                self.assertTrue(verification.response_mode_valid)
                self.assertTrue(verification.passed)

    def test_programmatic_mode_does_not_self_certify_refusal(self) -> None:
        answer = programmatic_answer(
            "下面给出具体操作步骤。",
            answer_mode="out_of_scope",
        )

        verification = verify_answer(
            answer,
            [],
            expected_answer_mode="out_of_scope",
            disclaimer=LEGAL_DISCLAIMER,
        )

        self.assertFalse(verification.refusal_present)
        self.assertFalse(verification.refusal_correct)
        self.assertFalse(verification.response_mode_valid)
        self.assertFalse(verification.passed)

    def test_refusal_prefix_cannot_hide_actionable_suffix(self) -> None:
        unsafe_texts = (
            "我不能提供违法操作方案。第一步，先伪造证据，第二步删除记录。",
            "不能提供具体操作方案但请先转移财产",
            "不能提供违法操作方案但应先销毁证据",
        )

        for unsafe_text in unsafe_texts:
            with self.subTest(unsafe_text=unsafe_text):
                answer = programmatic_answer(
                    unsafe_text,
                    answer_mode="out_of_scope",
                )
                verification = verify_answer(
                    answer,
                    [],
                    expected_answer_mode="out_of_scope",
                    disclaimer=LEGAL_DISCLAIMER,
                )

                self.assertTrue(verification.refusal_present)
                self.assertFalse(verification.refusal_correct)
                self.assertFalse(verification.response_mode_valid)
                self.assertFalse(verification.passed)

    def test_refusal_phrase_with_actionable_claim_is_not_correct_refusal(self) -> None:
        answer = StructuredAnswer(
            answer_text=(
                "我不能提供案件策略，但请按以下步骤操作 [S1]。"
                f"\n\n{LEGAL_DISCLAIMER}"
            ),
            answer_mode="out_of_scope",
            claims=[
                AnswerClaim(
                    claim_id="C1",
                    text="请按以下步骤操作",
                    source_ids=["S1"],
                )
            ],
            limitations=[],
            clarification_question=None,
        )

        verification = verify_answer(
            answer,
            [self.make_result()],
            expected_answer_mode="out_of_scope",
            disclaimer=LEGAL_DISCLAIMER,
        )

        self.assertTrue(verification.refusal_present)
        self.assertFalse(verification.refusal_correct)
        self.assertFalse(verification.response_mode_valid)
        self.assertFalse(verification.passed)

    def test_m1_t03_unknown_source_id_fails_and_falls_back(self) -> None:
        answer = self.make_answer(
            f"经营者应当依法保护消费者权益 [S999]。\n\n{LEGAL_DISCLAIMER}",
            source_ids=["S999"],
        )
        verification = verify_answer(
            answer,
            [self.make_result()],
            expected_answer_mode="evidence_answer",
            disclaimer=LEGAL_DISCLAIMER,
        )

        self.assertFalse(verification.citation_ids_valid)
        self.assertIn("S999", verification.missing_source_ids)
        self.assertIn("999", verification.missing_citations)
        self.assertIn("citation_ids_invalid", verification.failure_reasons)
        fallback = build_verifier_fallback_answer(
            answer.answer_text,
            verification,
            low_confidence_answer=f"当前资料不足。\n\n{LEGAL_DISCLAIMER}",
        )
        self.assertIn("资料不足", fallback)
        self.assertNotIn("[S999]", fallback)

    def test_fallback_is_default_deny_for_any_failed_verification(self) -> None:
        original = f"UNSAFE [S1]。\n\n{LEGAL_DISCLAIMER}"
        answer = self.make_answer(original, source_ids=["S1"])
        verification = verify_answer(
            answer,
            [self.make_result()],
            expected_answer_mode="typo",
            disclaimer=LEGAL_DISCLAIMER,
        )

        fallback = build_verifier_fallback_answer(
            original,
            verification,
            low_confidence_answer="当前资料不足。",
        )

        self.assertFalse(verification.passed)
        self.assertNotEqual(fallback, original)
        self.assertNotIn("UNSAFE", fallback)

    def test_m1_t04_real_id_does_not_imply_semantic_support(self) -> None:
        answer = self.make_answer(
            f"雇主必须为所有远程员工提供住房 [S1]。\n\n{LEGAL_DISCLAIMER}",
            source_ids=["S1"],
        )

        verification = verify_answer(
            answer,
            [self.make_result(text="合成示例条文：经营者应当保存交易记录。")],
            expected_answer_mode="evidence_answer",
            disclaimer=LEGAL_DISCLAIMER,
        )

        self.assertTrue(verification.citation_ids_valid)
        self.assertNotEqual(verification.semantic_support_status, "supported")
        self.assertIn(verification.semantic_support_status, {"uncertain", "not_checked"})

    def test_visible_legal_claim_omitted_from_claims_is_still_scanned(self) -> None:
        answer = StructuredAnswer(
            answer_text=(
                "经营者应当保存交易记录 [S1]。"
                "雇主必须为远程员工提供住房 [S1]。"
                f"\n\n{LEGAL_DISCLAIMER}"
            ),
            answer_mode="evidence_answer",
            claims=[
                AnswerClaim(
                    claim_id="C1",
                    text="经营者应当保存交易记录",
                    source_ids=["S1"],
                )
            ],
            limitations=[],
            clarification_question=None,
        )

        verification = verify_answer(
            answer,
            [self.make_result(text="经营者应当保存交易记录。")],
            expected_answer_mode="evidence_answer",
            disclaimer=LEGAL_DISCLAIMER,
        )

        self.assertEqual(verification.semantic_support_status, "uncertain")
        self.assertTrue(
            any("远程员工提供住房" in claim for claim in verification.unsupported_claims)
        )

    def test_m1_t05_insufficient_evidence_without_sources_is_valid_mode(self) -> None:
        answer = self.make_answer(
            f"当前检索资料不足，无法给出可靠结论。\n\n{LEGAL_DISCLAIMER}",
            mode="insufficient_evidence",
            source_ids=None,
        )

        verification = verify_answer(
            answer,
            [],
            expected_answer_mode="insufficient_evidence",
            disclaimer=LEGAL_DISCLAIMER,
        )

        self.assertTrue(verification.citation_ids_valid)
        self.assertEqual(verification.cited_source_ids, [])
        self.assertTrue(verification.response_mode_valid)
        self.assertEqual(verification.semantic_support_status, "not_checked")
        self.assertTrue(verification.passed)

    def test_limited_mode_boilerplate_is_not_semantically_scored(self) -> None:
        answer = self.make_answer(
            f"当前资料不足，可以补充事实后再检索。\n\n{LEGAL_DISCLAIMER}",
            mode="insufficient_evidence",
            limitations=["当前资料不足。"],
        )

        verification = verify_answer(
            answer,
            [self.make_result(text="完全无关的合成证据。")],
            expected_answer_mode="insufficient_evidence",
            disclaimer=LEGAL_DISCLAIMER,
            semantic_support_status="supported",
        )

        self.assertEqual(verification.unsupported_claims, [])
        self.assertEqual(verification.semantic_support_status, "not_checked")

    def test_m1_t10_cross_snapshot_and_scope_sources_are_rejected(self) -> None:
        answer = self.make_answer(
            f"经营者应当依法保护消费者权益 [S1] [S2]。\n\n{LEGAL_DISCLAIMER}",
            source_ids=["S1", "S2"],
        )
        results = [
            self.make_result(rank=1, snapshot_id="snapshot-b", scope_id="public"),
            self.make_result(rank=2, snapshot_id="snapshot-a", scope_id="tenant-b"),
        ]

        verification = verify_answer(
            answer,
            results,
            expected_answer_mode="evidence_answer",
            context=VerificationContext(
                snapshot_id="snapshot-a",
                allowed_scope_ids=["public", "tenant-a"],
            ),
            disclaimer=LEGAL_DISCLAIMER,
        )

        self.assertTrue(verification.citation_ids_valid)
        self.assertFalse(verification.evidence_scope_valid)
        self.assertCountEqual(verification.invalid_scope_citations, ["S1", "S2"])
        self.assertIn("evidence_scope_invalid", verification.failure_reasons)

    def test_scope_check_fails_closed_when_metadata_is_missing(self) -> None:
        result = SearchResult(
            chunk=Chunk(
                chunk_id="missing-scope",
                text="合成示例条文。",
                law_names=["合成示例法"],
                article_numbers=["第一条"],
                source_files=["synthetic.txt"],
                line_nos=[1],
                strategy="article",
            ),
            score=1.0,
            rank=1,
            retriever="fixture",
        )
        answer = self.make_answer(
            f"合成示例结论 [S1]。\n\n{LEGAL_DISCLAIMER}",
            source_ids=["S1"],
        )

        verification = verify_answer(
            answer,
            [result],
            expected_answer_mode="evidence_answer",
            context=VerificationContext(
                snapshot_id="snapshot-a",
                allowed_scope_ids=["public"],
            ),
            disclaimer=LEGAL_DISCLAIMER,
        )

        self.assertFalse(verification.evidence_scope_valid)
        self.assertEqual(verification.invalid_scope_citations, ["S1"])

    def test_scope_check_fails_closed_on_malformed_metadata(self) -> None:
        malformed_metadata_values = (
            {"snapshot_id": "snapshot-a", "access_scope_ids": [["public"]]},
            [],
        )
        answer = self.make_answer(
            f"合成示例结论 [S1]。\n\n{LEGAL_DISCLAIMER}",
            source_ids=["S1"],
        )
        context = VerificationContext(
            snapshot_id="snapshot-a",
            allowed_scope_ids=["public"],
        )

        for metadata in malformed_metadata_values:
            with self.subTest(metadata=metadata):
                result = SearchResult(
                    chunk=Chunk(
                        chunk_id="malformed-scope",
                        text="合成示例条文。",
                        law_names=["合成示例法"],
                        article_numbers=["第一条"],
                        source_files=["synthetic.txt"],
                        line_nos=[1],
                        strategy="article",
                        metadata=metadata,  # type: ignore[arg-type]
                    ),
                    score=1.0,
                    rank=1,
                    retriever="fixture",
                )
                verification = verify_answer(
                    answer,
                    [result],
                    expected_answer_mode="evidence_answer",
                    context=context,
                    disclaimer=LEGAL_DISCLAIMER,
                )

                self.assertFalse(verification.evidence_scope_valid)
                self.assertFalse(verification.passed)

    def test_duplicate_or_nonpositive_result_ranks_fail_catalog_validation(self) -> None:
        answer = self.make_answer(
            f"合成示例结论 [S1]。\n\n{LEGAL_DISCLAIMER}",
            source_ids=["S1"],
        )
        duplicate_results = [
            self.make_result(rank=1, snapshot_id="snapshot-b", scope_id="public"),
            self.make_result(rank=1, snapshot_id="snapshot-a", scope_id="public"),
        ]
        invalid_rank_result = self.make_result(rank=0)
        context = VerificationContext(
            snapshot_id="snapshot-a",
            allowed_scope_ids=["public"],
        )

        for results in (duplicate_results, [invalid_rank_result]):
            with self.subTest(ranks=[result.rank for result in results]):
                verification = verify_answer(
                    answer,
                    results,
                    expected_answer_mode="evidence_answer",
                    context=context,
                    disclaimer=LEGAL_DISCLAIMER,
                )

                self.assertFalse(verification.evidence_catalog_valid)
                self.assertFalse(verification.citation_ids_valid)
                self.assertIn("evidence_catalog_invalid", verification.failure_reasons)
                self.assertFalse(verification.passed)

    def test_answer_mode_invariants_reject_claims_in_limited_answer(self) -> None:
        answer = self.make_answer(
            f"当前资料不足，但经营者必须退款 [S1]。\n\n{LEGAL_DISCLAIMER}",
            mode="insufficient_evidence",
            source_ids=["S1"],
        )

        verification = verify_answer(
            answer,
            [self.make_result()],
            expected_answer_mode="insufficient_evidence",
            disclaimer=LEGAL_DISCLAIMER,
        )

        self.assertFalse(verification.response_mode_valid)
        self.assertFalse(verification.passed)

    def test_answer_modes_reject_hidden_or_incoherent_clarification(self) -> None:
        hidden_question = StructuredAnswer(
            answer_text=f"请稍后。\n\n{LEGAL_DISCLAIMER}",
            answer_mode="needs_clarification",
            claims=[],
            limitations=[],
            clarification_question="请说明合同日期？",
        )
        insufficient_with_question = StructuredAnswer(
            answer_text=f"当前资料不足。\n\n{LEGAL_DISCLAIMER}",
            answer_mode="insufficient_evidence",
            claims=[],
            limitations=[],
            clarification_question="请说明合同日期？",
        )
        hidden_insufficiency = StructuredAnswer(
            answer_text=f"请稍后。\n\n{LEGAL_DISCLAIMER}",
            answer_mode="insufficient_evidence",
            claims=[],
            limitations=["资料不足。"],
            clarification_question=None,
        )

        hidden_verification = verify_answer(
            hidden_question,
            [],
            expected_answer_mode="needs_clarification",
            disclaimer=LEGAL_DISCLAIMER,
        )
        insufficient_verification = verify_answer(
            insufficient_with_question,
            [],
            expected_answer_mode="insufficient_evidence",
            disclaimer=LEGAL_DISCLAIMER,
        )
        hidden_insufficiency_verification = verify_answer(
            hidden_insufficiency,
            [],
            expected_answer_mode="insufficient_evidence",
            disclaimer=LEGAL_DISCLAIMER,
        )

        self.assertFalse(hidden_verification.response_mode_valid)
        self.assertFalse(insufficient_verification.response_mode_valid)
        self.assertFalse(hidden_insufficiency_verification.response_mode_valid)

    def test_global_citation_cannot_cover_a_claim_without_source_ids(self) -> None:
        answer = StructuredAnswer(
            answer_text=f"背景资料见 [S1]。雇主必须提供住房。\n\n{LEGAL_DISCLAIMER}",
            answer_mode="evidence_answer",
            claims=[AnswerClaim(claim_id="C1", text="雇主必须提供住房", source_ids=[])],
            limitations=[],
            clarification_question=None,
        )

        verification = verify_answer(
            answer,
            [self.make_result()],
            expected_answer_mode="evidence_answer",
            disclaimer=LEGAL_DISCLAIMER,
        )

        self.assertFalse(verification.schema_valid)
        self.assertIn("claims[0].source_ids_invalid", verification.schema_errors)
        self.assertFalse(verification.passed)

    def test_hidden_claim_source_cannot_replace_user_visible_citation(self) -> None:
        answer = StructuredAnswer(
            answer_text=f"经营者应当保护消费者权益。\n\n{LEGAL_DISCLAIMER}",
            answer_mode="evidence_answer",
            claims=[
                AnswerClaim(
                    claim_id="C1",
                    text="经营者应当保护消费者权益",
                    source_ids=["S1"],
                )
            ],
            limitations=[],
            clarification_question=None,
        )

        verification = verify_answer(
            answer,
            [self.make_result()],
            expected_answer_mode="evidence_answer",
            disclaimer=LEGAL_DISCLAIMER,
        )

        self.assertFalse(verification.citation_alignment_valid)
        self.assertFalse(verification.citation_ids_valid)
        self.assertFalse(verification.passed)

    def test_missing_user_facts_require_clarification_not_more_retrieval(self) -> None:
        normalized = NormalizedQuery(
            original_query="这个能不能解除？",
            legal_questions=["合同是否可以解除"],
            missing_facts=["是否已经履行催告程序"],
            law_hints=[],
            article_hints=[],
            keywords=["合同解除"],
            risk_flags=[],
            confidence=0.8,
        )

        check = check_evidence_sufficiency(
            normalized.original_query,
            [self.make_result()],
            normalized_query=normalized,
        )

        self.assertFalse(check.sufficient)
        self.assertEqual(check.stop_reason, "needs_clarification")
        self.assertEqual(check.followup_queries, [])

        clarification = StructuredAnswer(
            answer_text=f"请补充是否已经履行催告程序？\n\n{LEGAL_DISCLAIMER}",
            answer_mode="needs_clarification",
            claims=[],
            limitations=["缺少是否履行催告程序的事实。"],
            clarification_question="是否已经履行催告程序？",
        )
        verification = verify_answer(
            clarification,
            [self.make_result()],
            evidence_check=check,
            disclaimer=LEGAL_DISCLAIMER,
        )

        self.assertEqual(verification.expected_answer_mode, "needs_clarification")
        self.assertTrue(verification.response_mode_valid)
        self.assertTrue(verification.passed)

    def test_structured_json_adapter_rejects_unknown_field_types(self) -> None:
        raw = json.dumps(
            {
                "answer_text": "当前资料不足。",
                "answer_mode": "insufficient_evidence",
                "claims": "not-a-list",
                "limitations": [],
                "clarification_question": None,
            },
            ensure_ascii=False,
        )

        parsed = parse_structured_answer(raw)

        self.assertFalse(parsed.schema_valid)
        self.assertTrue(parsed.parse_errors)

    def test_all_malformed_json_root_types_fail_closed(self) -> None:
        baseline = {
            "answer_text": "当前资料不足。",
            "answer_mode": "insufficient_evidence",
            "claims": [],
            "limitations": ["当前资料不足。"],
            "clarification_question": None,
        }
        malformed_values = {
            "answer_text": 42,
            "answer_mode": [],
            "claims": {},
            "limitations": "not-a-list",
            "clarification_question": [],
        }

        for field_name, malformed_value in malformed_values.items():
            with self.subTest(field_name=field_name):
                payload = {**baseline, field_name: malformed_value}
                parsed = parse_structured_answer(json.dumps(payload, ensure_ascii=False))
                verification = verify_answer(
                    parsed,
                    [],
                    expected_answer_mode="insufficient_evidence",
                    disclaimer=LEGAL_DISCLAIMER,
                )

                self.assertFalse(parsed.schema_valid)
                self.assertFalse(verification.passed)
                self.assertIn("schema_invalid", verification.failure_reasons)

    def test_legacy_text_adapter_is_explicitly_less_verifiable(self) -> None:
        parsed = parse_structured_answer(
            f"经营者应当保护消费者权益 [S1]。\n\n{LEGAL_DISCLAIMER}"
        )

        self.assertEqual(parsed.answer_mode, "evidence_answer")
        self.assertFalse(parsed.schema_valid)
        self.assertEqual(parsed.adapter_source, "legacy_text")

    def test_malformed_structured_object_degrades_without_crashing(self) -> None:
        malformed = StructuredAnswer(
            answer_text=f"当前资料不足。\n\n{LEGAL_DISCLAIMER}",
            answer_mode="insufficient_evidence",
            claims="not-a-list",  # type: ignore[arg-type]
            limitations=["当前资料不足。"],
            clarification_question=None,
        )

        verification = verify_answer(
            malformed,
            [],
            expected_answer_mode="insufficient_evidence",
            disclaimer=LEGAL_DISCLAIMER,
        )

        self.assertFalse(verification.schema_valid)
        self.assertIn("claims_must_be_list", verification.schema_errors)
        self.assertFalse(verification.passed)

    def test_unhashable_direct_object_fields_fail_closed(self) -> None:
        malformed = StructuredAnswer(
            answer_text=42,  # type: ignore[arg-type]
            answer_mode=[],  # type: ignore[arg-type]
            claims=[
                AnswerClaim(
                    claim_id=[],  # type: ignore[arg-type]
                    text={},  # type: ignore[arg-type]
                    source_ids=[[]],  # type: ignore[list-item]
                )
            ],
            limitations="bad",  # type: ignore[arg-type]
            clarification_question=[],  # type: ignore[arg-type]
        )

        verification = verify_answer(malformed, [])

        self.assertFalse(verification.schema_valid)
        self.assertFalse(verification.passed)
        self.assertIn("schema_invalid", verification.failure_reasons)

    def test_programmatic_no_evidence_response_is_verified(self) -> None:
        class EmptyRetriever:
            name = "fixture"

            def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
                return []

        assistant = LegalChatAssistant(EmptyRetriever(), model="fake")

        answer, results = assistant.answer("第一条规定了什么？", generate=False)

        self.assertEqual(results, [])
        self.assertIn("资料", answer)
        self.assertIsNotNone(assistant.last_structured_answer)
        self.assertEqual(
            assistant.last_structured_answer.answer_mode,
            "insufficient_evidence",
        )
        self.assertIsNotNone(assistant.last_verification)
        self.assertTrue(assistant.last_verification.passed)

    def test_user_source_like_token_is_sanitized_in_no_evidence_response(self) -> None:
        class EmptyRetriever:
            name = "fixture"

            def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
                return []

        assistant = LegalChatAssistant(EmptyRetriever(), model="fake")

        answer, _ = assistant.answer("《[S999]测试法》第一条规定什么？", generate=False)

        self.assertNotIn("[S999]", answer)
        self.assertTrue(assistant.last_verification.passed)
        self.assertTrue(answer.endswith(LEGAL_DISCLAIMER))

    def test_invalid_generated_source_is_reverified_after_fallback(self) -> None:
        result = self.make_result()

        class StaticRetriever:
            name = "fixture"

            def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
                return [result]

        class InvalidCitationModel:
            def complete(self, prompt: str) -> str:
                return json.dumps(
                    {
                        "answer_text": f"这是具体结论 [S999]。\n\n{LEGAL_DISCLAIMER}",
                        "answer_mode": "evidence_answer",
                        "claims": [
                            {
                                "claim_id": "C1",
                                "text": "这是具体结论",
                                "source_ids": ["S999"],
                            }
                        ],
                        "limitations": [],
                        "clarification_question": None,
                    },
                    ensure_ascii=False,
                )

        assistant = LegalChatAssistant(StaticRetriever(), model="fake")
        assistant.llm = InvalidCitationModel()

        answer, _ = assistant.answer("第一条规定了什么？", generate=True)

        self.assertNotIn("S999", answer)
        self.assertIsNotNone(assistant.last_pre_fallback_verification)
        self.assertIsNotNone(assistant.last_pre_fallback_answer)
        self.assertIn("S999", assistant.last_pre_fallback_answer.answer_text)
        self.assertFalse(assistant.last_pre_fallback_verification.citation_ids_valid)
        self.assertEqual(
            assistant.last_structured_answer.answer_mode,
            "insufficient_evidence",
        )
        self.assertTrue(assistant.last_verification.passed)
        self.assertTrue(answer.endswith(LEGAL_DISCLAIMER))

    def test_runtime_error_text_is_not_exposed_or_treated_as_an_answer(self) -> None:
        result = self.make_result()

        class StaticRetriever:
            name = "fixture"

            def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
                return [result]

        class FailingModel:
            def complete(self, prompt: str) -> str:
                raise RuntimeError("PROVIDER_SECRET [S999]")

        assistant = LegalChatAssistant(StaticRetriever(), model="fake")
        assistant.llm = FailingModel()

        answer, _ = assistant.answer("第一条规定了什么？", generate=True)

        self.assertNotIn("PROVIDER_SECRET", answer)
        self.assertNotIn("S999", answer)
        self.assertEqual(assistant.last_generation_error, "generation_error")
        self.assertEqual(assistant.last_structured_answer.answer_mode, "insufficient_evidence")
        self.assertTrue(assistant.last_verification.passed)

    def test_non_runtime_provider_error_also_uses_verified_safe_terminal(self) -> None:
        result = self.make_result()

        class StaticRetriever:
            name = "fixture"

            def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
                return [result]

        class FailingModel:
            def complete(self, prompt: str) -> str:
                raise ValueError("UNTRUSTED_PROVIDER_DETAIL [S999]")

        assistant = LegalChatAssistant(StaticRetriever(), model="fake")
        assistant.llm = FailingModel()

        answer, _ = assistant.answer("第一条规定了什么？", generate=True)

        self.assertNotIn("UNTRUSTED_PROVIDER_DETAIL", answer)
        self.assertNotIn("[S999]", answer)
        self.assertEqual(assistant.last_generation_error, "generation_error")
        self.assertTrue(assistant.last_verification.passed)

    def test_hidden_claim_citation_is_limited_before_user_delivery(self) -> None:
        result = self.make_result()

        class StaticRetriever:
            name = "fixture"

            def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
                return [result]

        class HiddenCitationModel:
            def complete(self, prompt: str) -> str:
                return json.dumps(
                    {
                        "answer_text": f"经营者应当保护消费者权益。\n\n{LEGAL_DISCLAIMER}",
                        "answer_mode": "evidence_answer",
                        "claims": [
                            {
                                "claim_id": "C1",
                                "text": "经营者应当保护消费者权益",
                                "source_ids": ["S1"],
                            }
                        ],
                        "limitations": [],
                        "clarification_question": None,
                    },
                    ensure_ascii=False,
                )

        assistant = LegalChatAssistant(StaticRetriever(), model="fake")
        assistant.llm = HiddenCitationModel()

        answer, _ = assistant.answer("第一条规定了什么？", generate=True)

        self.assertNotIn("经营者应当保护消费者权益。", answer)
        self.assertFalse(assistant.last_pre_fallback_verification.citation_alignment_valid)
        self.assertEqual(assistant.last_structured_answer.answer_mode, "insufficient_evidence")
        self.assertTrue(assistant.last_verification.passed)

    def test_cross_snapshot_generation_is_rejected_without_leaking_evidence(self) -> None:
        result = self.make_result(
            text="跨快照私有合成内容，不得出现在降级答案中。",
            snapshot_id="snapshot-b",
            scope_id="tenant-b",
        )

        class StaticRetriever:
            name = "fixture"

            def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
                return [result]

        class ScopedAnswerModel:
            def __init__(self) -> None:
                self.calls = 0

            def complete(self, prompt: str) -> str:
                self.calls += 1
                raise AssertionError("out-of-scope evidence must not reach the model")

        model = ScopedAnswerModel()
        assistant = LegalChatAssistant(
            StaticRetriever(),
            model="fake",
            verification_context=VerificationContext(
                snapshot_id="snapshot-a",
                allowed_scope_ids=["tenant-a"],
            ),
        )
        assistant.llm = model

        answer, _ = assistant.answer("第一条规定了什么？", generate=True)

        self.assertNotIn("跨快照私有合成内容", answer)
        self.assertEqual(model.calls, 0)
        self.assertEqual(assistant.last_rejected_original_source_ids, ["S1"])
        self.assertEqual(assistant.last_evidence_source_id_map, {})
        self.assertIsNone(assistant.last_pre_fallback_verification)
        self.assertEqual(assistant.last_structured_answer.answer_mode, "insufficient_evidence")
        self.assertTrue(assistant.last_verification.passed)

    def test_mixed_scope_prompt_contains_only_allowed_reranked_evidence(self) -> None:
        private_result = self.make_result(
            rank=1,
            text="禁止发送给模型的私有合成内容。",
            snapshot_id="snapshot-b",
            scope_id="tenant-b",
        )
        allowed_result = self.make_result(
            rank=2,
            text="允许使用的公开合成内容。",
            snapshot_id="snapshot-a",
            scope_id="public",
        )

        class MixedRetriever:
            name = "fixture"

            def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
                return [private_result, allowed_result]

        class CapturingModel:
            def __init__(self) -> None:
                self.prompt = ""

            def complete(self, prompt: str) -> str:
                self.prompt = prompt
                return json.dumps(
                    {
                        "answer_text": f"允许使用的公开合成内容 [S1]。\n\n{LEGAL_DISCLAIMER}",
                        "answer_mode": "evidence_answer",
                        "claims": [
                            {
                                "claim_id": "C1",
                                "text": "允许使用的公开合成内容",
                                "source_ids": ["S1"],
                            }
                        ],
                        "limitations": [],
                        "clarification_question": None,
                    },
                    ensure_ascii=False,
                )

        model = CapturingModel()
        assistant = LegalChatAssistant(
            MixedRetriever(),
            model="fake",
            verification_context=VerificationContext(
                snapshot_id="snapshot-a",
                allowed_scope_ids=["public"],
            ),
        )
        assistant.llm = model

        answer, results = assistant.answer("请解释这份材料。", generate=True)

        self.assertNotIn("禁止发送给模型", model.prompt)
        self.assertIn("允许使用的公开合成内容", model.prompt)
        self.assertEqual([result.rank for result in results], [1])
        self.assertEqual(assistant.last_rejected_original_source_ids, ["S1"])
        self.assertEqual(assistant.last_evidence_source_id_map, {"S2": "S1"})
        self.assertIn("[S1]", answer)
        self.assertTrue(assistant.last_verification.passed)


if __name__ == "__main__":
    unittest.main()
