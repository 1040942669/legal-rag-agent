from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from legal_rag import evaluation
from legal_rag.chat import LEGAL_DISCLAIMER, LegalChatAssistant
from legal_rag.judge import judge_answer
from legal_rag.models import (
    AnswerClaim,
    Chunk,
    EvalCase,
    EvalRecord,
    SearchResult,
    StructuredAnswer,
    VerificationContext,
)
from legal_rag.tracing import JsonlTraceWriter


def make_result(marker: str = "A") -> SearchResult:
    return SearchResult(
        chunk=Chunk(
            chunk_id=f"synthetic-{marker.lower()}",
            text=f"合成资料 {marker} 只说明合成事项 {marker}。",
            law_names=[f"合成测试法{marker}"],
            article_numbers=["第一条"],
            source_files=["synthetic.txt"],
            line_nos=[1],
            strategy="article",
        ),
        score=1.0,
        rank=1,
        retriever="deterministic",
    )


def make_case(
    marker: str,
    *,
    expected_behavior: str = "evidence_answer",
    case_type: str = "article_lookup",
) -> EvalCase:
    return EvalCase(
        case_id=f"case-{marker.lower()}",
        question=f"合成问题 {marker}",
        case_type=case_type,
        expected_law="",
        expected_articles=[],
        keywords=[f"合成事项 {marker}"],
        expected_behavior=expected_behavior,
        session_group=None,
        turn_index=0,
        schema_version=2,
    )


class DeterministicRetriever:
    name = "deterministic"

    def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
        marker = "B" if " B" in query else "A"
        return [make_result(marker)]


class DeterministicAnswerClient:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        marker = "B" if "合成问题 B" in prompt else "A"
        return json.dumps(
            {
                "answer_text": f"合成资料 {marker} 说明合成事项 {marker} [S1]。",
                "answer_mode": "evidence_answer",
                "claims": [
                    {
                        "claim_id": "C1",
                        "text": f"合成资料 {marker} 说明合成事项 {marker}",
                        "source_ids": ["S1"],
                    }
                ],
                "limitations": [],
                "clarification_question": None,
            },
            ensure_ascii=False,
        )


class InvalidCitationAnswerClient:
    def complete(self, prompt: str) -> str:
        return json.dumps(
            {
                "answer_text": "这个合成结论引用了不存在的来源 [S999]。",
                "answer_mode": "evidence_answer",
                "claims": [
                    {
                        "claim_id": "C1",
                        "text": "这个合成结论引用了不存在的来源",
                        "source_ids": ["S999"],
                    }
                ],
                "limitations": [],
                "clarification_question": None,
            },
            ensure_ascii=False,
        )


class SecretRejectedDraftClient:
    def complete(self, prompt: str) -> str:
        return json.dumps(
            {
                "answer_text": "机密草稿令牌SECRET_DRAFT_123必须立即执行 [S999]。",
                "answer_mode": "evidence_answer",
                "claims": [
                    {
                        "claim_id": "C1",
                        "text": "机密草稿令牌SECRET_DRAFT_123必须立即执行",
                        "source_ids": ["S999"],
                    }
                ],
                "limitations": [],
                "clarification_question": None,
            },
            ensure_ascii=False,
        )


class NoGenerationAssistant:
    llm = None
    last_adaptive_result = None
    last_evidence_check = None
    last_verification = None
    last_structured_answer = None

    def __init__(self) -> None:
        self.answer_calls = 0
        self.reset_calls = 0

    def reset_memory(self) -> None:
        self.reset_calls += 1

    def answer(self, question: str, *, generate: bool = True):
        self.answer_calls += 1
        raise AssertionError("retrieval-only must not enter answer generation")


class ForbiddenJudgeClient:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        raise AssertionError("retrieval-only must not call the judge")


class TimeoutJudgeClient:
    def complete(self, prompt: str) -> str:
        # Empty provider messages are common for socket timeouts. Classification
        # must use the exception type, not truthiness of the message string.
        raise TimeoutError()


class InvalidJsonJudgeClient:
    def complete(self, prompt: str) -> str:
        return "这不是 JSON"


class SessionTrackingAssistant:
    llm = None
    last_adaptive_result = None
    last_evidence_check = None
    last_verification = None
    last_structured_answer = None
    last_pre_fallback_answer = None
    last_pre_fallback_verification = None
    last_generation_error = None

    def __init__(self) -> None:
        self.reset_calls = 0
        self.history: list[str] = []
        self.history_before_answer: dict[str, list[str]] = {}

    def reset_memory(self) -> None:
        self.reset_calls += 1
        self.history.clear()

    def answer(self, question: str, *, generate: bool = True):
        self.history_before_answer[question] = list(self.history)
        self.history.append(question)
        return f"合成资料支持合成事项 [S1]。\n\n{LEGAL_DISCLAIMER}", [make_result()]


class M1EvaluationTest(unittest.TestCase):
    maxDiff = None

    def run_generated_order(
        self,
        markers: list[str],
    ) -> tuple[dict[str, EvalRecord], DeterministicAnswerClient]:
        retriever = DeterministicRetriever()
        assistant = LegalChatAssistant(retriever, model="deterministic-fake")
        client = DeterministicAnswerClient()
        assistant.llm = client
        records = evaluation.evaluate(
            cases=[make_case(marker) for marker in markers],
            retriever=retriever,
            chunk_strategy="article",
            model="deterministic-fake",
            generate=True,
            assistant=assistant,
        )
        return {record.case_id: record for record in records}, client

    def test_m1_t06_ab_and_ba_are_deterministic_and_case_isolated(self) -> None:
        ab_records, ab_client = self.run_generated_order(["A", "B"])
        ba_records, ba_client = self.run_generated_order(["B", "A"])

        self.assertEqual(set(ab_records), {"case-a", "case-b"})
        self.assertEqual(set(ba_records), {"case-a", "case-b"})
        for case_id in ("case-a", "case-b"):
            with self.subTest(case_id=case_id):
                self.assertEqual(ab_records[case_id].answer, ba_records[case_id].answer)
                self.assertEqual(
                    ab_records[case_id].observed_answer_mode,
                    ba_records[case_id].observed_answer_mode,
                )
                self.assertEqual(
                    ab_records[case_id].canonical_metrics,
                    ba_records[case_id].canonical_metrics,
                )

        all_prompts = ab_client.prompts + ba_client.prompts
        self.assertEqual(len(all_prompts), 4)
        for prompt in all_prompts:
            self.assertIn("最近对话:\n无\n\n用户原始问题:", prompt)
        self.assertNotIn("合成事项 A [S1]", ab_client.prompts[1])
        self.assertNotIn("合成事项 B [S1]", ba_client.prompts[1])

    def test_m1_t07_retrieval_only_skips_generation_verifier_and_judge(self) -> None:
        assistant = NoGenerationAssistant()
        judge = ForbiddenJudgeClient()

        with (
            patch(
                "legal_rag.evaluation.verify_answer",
                wraps=evaluation.verify_answer,
            ) as verifier_spy,
            patch(
                "legal_rag.evaluation.judge_answer",
                wraps=evaluation.judge_answer,
            ) as judge_spy,
        ):
            records = evaluation.evaluate(
                cases=[make_case("A")],
                retriever=DeterministicRetriever(),
                chunk_strategy="article",
                generate=False,
                assistant=assistant,
                judge_client=judge,
            )

        self.assertEqual(len(records), 1)
        self.assertEqual(assistant.answer_calls, 0)
        self.assertEqual(judge.calls, 0)
        verifier_spy.assert_not_called()
        judge_spy.assert_not_called()

    def test_m1_t07_retrieval_only_uses_explicit_na_in_record_and_trace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            trace_path = Path(temp_dir) / "retrieval-only.jsonl"
            writer = JsonlTraceWriter(trace_path, run_id="m1-t07")
            records = evaluation.evaluate(
                cases=[make_case("A")],
                retriever=DeterministicRetriever(),
                chunk_strategy="article",
                generate=False,
                trace_writer=writer,
                judge_client=ForbiddenJudgeClient(),
            )
            trace = json.loads(trace_path.read_text(encoding="utf-8").strip())

        record = records[0]
        self.assertEqual(record.answer, "")
        self.assertEqual(record.keyword_coverage, -1)
        self.assertEqual(record.citation_valid, -1)
        self.assertEqual(record.verifier_pass, -1)
        self.assertEqual(record.refusal_correctness, -1)

        unavailable = {"value": None, "unavailable_reason": "retrieval_only"}
        for metric_name in (
            "answer_text",
            "keyword_coverage",
            "schema_valid",
            "evidence_catalog_valid",
            "source_ids_exist",
            "citation_ids_valid",
            "citation_alignment_valid",
            "evidence_scope_valid",
            "citation_valid",
            "disclaimer_present",
            "response_mode_valid",
            "verifier_pass",
            "semantic_support_status",
            "response_mode_correct",
            "refusal_correctness",
            "over_refusal",
            "judge_faithfulness",
            "judge_relevance",
            "judge_completeness",
            "judge_pass",
        ):
            with self.subTest(metric_name=metric_name):
                self.assertEqual(record.canonical_metrics[metric_name], unavailable)

        self.assertEqual(record.execution["service"]["status"], "succeeded")
        for stage in ("generation", "verification", "judge"):
            with self.subTest(stage=stage):
                self.assertEqual(record.execution[stage]["status"], "not_run")
                self.assertEqual(record.execution[stage]["reason"], "retrieval_only")
        self.assertEqual(
            record.generation_attempt,
            {"attempted": False, "reason": "retrieval_only"},
        )

        self.assertEqual(trace["execution"], record.execution)
        self.assertEqual(
            trace["generation_attempt"],
            {"attempted": False, "reason": "retrieval_only"},
        )
        self.assertEqual(
            trace["final_response"],
            {"value": None, "unavailable_reason": "retrieval_only"},
        )
        self.assertEqual(trace["verifier"], {})

    def test_rejected_generation_attempt_is_distinct_from_delivered_final(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            trace_path = Path(temp_dir) / "fallback.jsonl"
            writer = JsonlTraceWriter(trace_path, run_id="m1-attempt-final")
            assistant = LegalChatAssistant(
                DeterministicRetriever(),
                model="invalid-citation-fake",
            )
            assistant.llm = InvalidCitationAnswerClient()
            record = evaluation.evaluate(
                cases=[make_case("A")],
                retriever=assistant.retriever,
                chunk_strategy="article",
                model="invalid-citation-fake",
                generate=True,
                assistant=assistant,
                trace_writer=writer,
            )[0]
            trace = json.loads(trace_path.read_text(encoding="utf-8").strip())

        self.assertEqual(record.generation_attempt["status"], "rejected")
        self.assertFalse(record.generation_attempt["verification"]["citation_ids_valid"])
        self.assertEqual(record.execution["generation"]["status"], "succeeded")
        self.assertIsNone(record.execution["generation"]["reason"])
        self.assertEqual(record.verifier_pass, 1)
        self.assertEqual(record.observed_answer_mode, "insufficient_evidence")
        self.assertEqual(trace["execution"], record.execution)
        self.assertEqual(trace["generation_attempt"], record.generation_attempt)
        self.assertTrue(trace["final_response"]["value"]["verification"]["passed"])
        self.assertEqual(
            trace["final_response"]["value"]["answer_mode"],
            "insufficient_evidence",
        )

    def test_rejected_generation_draft_text_is_not_copied_into_trace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            trace_path = Path(temp_dir) / "secret-fallback.jsonl"
            writer = JsonlTraceWriter(trace_path, run_id="m1-secret-attempt")
            assistant = LegalChatAssistant(
                DeterministicRetriever(),
                model="secret-invalid-citation-fake",
            )
            assistant.llm = SecretRejectedDraftClient()
            record = evaluation.evaluate(
                cases=[make_case("A")],
                retriever=assistant.retriever,
                chunk_strategy="article",
                model="secret-invalid-citation-fake",
                generate=True,
                assistant=assistant,
                trace_writer=writer,
            )[0]
            trace_text = trace_path.read_text(encoding="utf-8")

        self.assertEqual(record.generation_attempt["status"], "rejected")
        self.assertGreaterEqual(
            record.generation_attempt["verification"]["unsupported_claim_count"],
            1,
        )
        self.assertNotIn("unsupported_claims", record.generation_attempt["verification"])
        self.assertNotIn("SECRET_DRAFT_123", trace_text)
        self.assertNotIn(
            "SECRET_DRAFT_123",
            json.dumps(record.generation_attempt, ensure_ascii=False),
        )

    def test_service_exception_details_are_not_written_to_record_or_report(self) -> None:
        class FailingRetriever:
            name = "failing"

            def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
                raise RuntimeError("SECRET_RETRIEVER_TOKEN=do-not-report")

        record = evaluation.evaluate(
            cases=[make_case("A")],
            retriever=FailingRetriever(),
            chunk_strategy="article",
            model="none",
            generate=False,
        )[0]
        report = evaluation.render_eval_report([record])

        self.assertEqual(record.error, "evaluation service failed")
        self.assertEqual(record.execution["service"]["status"], "error")
        self.assertNotIn("SECRET_RETRIEVER_TOKEN", record.error)
        self.assertNotIn("SECRET_RETRIEVER_TOKEN", report)

    def test_canonical_citation_id_and_scope_metrics_remain_distinct(self) -> None:
        result = SearchResult(
            chunk=Chunk(
                chunk_id="cross-snapshot",
                text="合成资料 A 只说明合成事项 A。",
                law_names=["合成测试法A"],
                article_numbers=["第一条"],
                source_files=["synthetic.txt"],
                line_nos=[1],
                strategy="article",
                metadata={"snapshot_id": "snapshot-b", "access_scope_ids": ["public"]},
            ),
            score=1.0,
            rank=1,
            retriever="deterministic",
        )
        structured = StructuredAnswer(
            answer_text=f"合成资料 A 说明合成事项 A [S1]。\n\n{LEGAL_DISCLAIMER}",
            answer_mode="evidence_answer",
            claims=[
                AnswerClaim(
                    claim_id="C1",
                    text="合成资料 A 说明合成事项 A",
                    source_ids=["S1"],
                )
            ],
            limitations=[],
            clarification_question=None,
        )

        class CrossSnapshotAssistant:
            llm = None
            last_adaptive_result = None
            last_evidence_check = None
            last_pre_fallback_answer = None
            last_pre_fallback_verification = None
            last_generation_error = None

            def reset_memory(self) -> None:
                self.last_structured_answer = None
                self.last_verification = None

            def answer(self, question: str, *, generate: bool = True):
                self.last_structured_answer = structured
                self.last_verification = evaluation.verify_answer(
                    structured,
                    [result],
                    expected_answer_mode="evidence_answer",
                    disclaimer=LEGAL_DISCLAIMER,
                    context=VerificationContext(
                        snapshot_id="snapshot-a",
                        allowed_scope_ids=["public"],
                    ),
                )
                return structured.answer_text, [result]

        record = evaluation.evaluate(
            cases=[make_case("A")],
            retriever=DeterministicRetriever(),
            chunk_strategy="article",
            generate=True,
            assistant=CrossSnapshotAssistant(),
        )[0]

        self.assertEqual(record.canonical_metrics["source_ids_exist"]["value"], True)
        self.assertEqual(record.canonical_metrics["citation_ids_valid"]["value"], True)
        self.assertEqual(record.canonical_metrics["evidence_scope_valid"]["value"], False)
        self.assertEqual(record.canonical_metrics["citation_valid"]["value"], False)
        self.assertEqual(record.citation_valid, 0)

    def test_m1_t08_judge_errors_have_nullable_scores_and_canonical_codes(self) -> None:
        results = [make_result("A")]
        scenarios = (
            (TimeoutJudgeClient(), "timeout"),
            (InvalidJsonJudgeClient(), "invalid_json"),
        )

        for client, error_code in scenarios:
            with self.subTest(error_code=error_code):
                result = judge_answer(
                    client,
                    question="合成问题 A",
                    answer="合成回答 A",
                    results=results,
                )
                self.assertEqual(result.status, "error")
                self.assertEqual(result.error_code, error_code)
                self.assertEqual(result.source, "error")
                self.assertIsNone(result.faithfulness)
                self.assertIsNone(result.relevance)
                self.assertIsNone(result.completeness)
                self.assertIsNone(result.passed)
                self.assertIsInstance(result.error, str)

    def test_m1_t08_judge_errors_are_null_and_excluded_from_quality_denominator(self) -> None:
        records: list[EvalRecord] = []
        for suffix, judge_client in (
            ("timeout", TimeoutJudgeClient()),
            ("invalid-json", InvalidJsonJudgeClient()),
        ):
            assistant = LegalChatAssistant(
                DeterministicRetriever(),
                model="deterministic-fake",
            )
            assistant.llm = DeterministicAnswerClient()
            records.extend(
                evaluation.evaluate(
                    cases=[
                        EvalCase(
                            case_id=f"judge-{suffix}",
                            question="合成问题 A",
                            case_type="article_lookup",
                            expected_law="",
                            expected_articles=[],
                            keywords=["合成事项 A"],
                            expected_behavior="evidence_answer",
                            schema_version=2,
                        )
                    ],
                    retriever=assistant.retriever,
                    chunk_strategy="article",
                    model="deterministic-fake",
                    generate=True,
                    assistant=assistant,
                    judge_client=judge_client,
                )
            )

        expected_codes = {
            "judge-timeout": "timeout",
            "judge-invalid-json": "invalid_json",
        }
        for record in records:
            error_code = expected_codes[record.case_id]
            with self.subTest(case_id=record.case_id):
                self.assertEqual(record.execution["judge"]["status"], "error")
                self.assertEqual(record.execution["judge"]["reason"], error_code)
                for metric_name in (
                    "judge_faithfulness",
                    "judge_relevance",
                    "judge_completeness",
                    "judge_pass",
                ):
                    self.assertEqual(
                        record.canonical_metrics[metric_name],
                        {"value": None, "unavailable_reason": error_code},
                    )

        summary = evaluation.summarize_evaluation(records)
        self.assertEqual(summary["denominators"]["judge_succeeded"], 0)
        self.assertEqual(summary["denominators"]["judge_failed"], 2)
        self.assertEqual(summary["denominators"]["judge_not_run"], 0)

        report = evaluation.render_eval_report(records)
        self.assertIn("成功 n=0, 失败 n=2", report)
        self.assertIn("已从质量均值中排除", report)
        self.assertNotIn("Faithfulness: 0.000", report)

    def test_m1_t08_judge_three_way_counts_and_success_mean_share_one_summary(self) -> None:
        succeeded = self.make_behavior_record(
            "judge-succeeded",
            "evidence_answer",
            "evidence_answer",
        )
        succeeded.execution["judge"] = {"status": "succeeded", "reason": None}
        succeeded.judge_faithfulness = 0.9
        succeeded.judge_relevance = 0.8
        succeeded.judge_completeness = 0.7
        succeeded.judge_pass = 1
        succeeded.canonical_metrics.update(
            {
                "judge_faithfulness": {"value": 0.9, "unavailable_reason": None},
                "judge_relevance": {"value": 0.8, "unavailable_reason": None},
                "judge_completeness": {"value": 0.7, "unavailable_reason": None},
                "judge_pass": {"value": True, "unavailable_reason": None},
            }
        )

        failed = self.make_behavior_record(
            "judge-failed",
            "evidence_answer",
            "evidence_answer",
        )
        failed.execution["judge"] = {"status": "error", "reason": "timeout"}
        failed.judge_error = ""
        failed.canonical_metrics.update(
            {
                metric_name: {"value": None, "unavailable_reason": "timeout"}
                for metric_name in (
                    "judge_faithfulness",
                    "judge_relevance",
                    "judge_completeness",
                    "judge_pass",
                )
            }
        )

        not_run = self.make_behavior_record(
            "judge-not-run",
            "evidence_answer",
            "evidence_answer",
        )
        records = [succeeded, failed, not_run]

        summary = evaluation.summarize_evaluation(records)
        self.assertEqual(summary["denominators"]["judge_succeeded"], 1)
        self.assertEqual(summary["denominators"]["judge_failed"], 1)
        self.assertEqual(summary["denominators"]["judge_not_run"], 1)

        report = evaluation.render_eval_report(records)
        self.assertIn("成功 n=1, 失败 n=1, 未执行 n=1", report)
        self.assertIn("Faithfulness: 0.900", report)
        self.assertIn("Relevance: 0.800", report)
        self.assertIn("Completeness: 0.700", report)
        self.assertNotIn("Faithfulness: -0.", report)

    def test_m1_t09_behavior_denominators_and_over_refusal_are_explicit(self) -> None:
        records = [
            self.make_behavior_record("answer-ok", "evidence_answer", "evidence_answer"),
            self.make_behavior_record("answer-refused", "evidence_answer", "out_of_scope"),
            self.make_behavior_record("refuse-ok", "out_of_scope", "out_of_scope"),
            self.make_behavior_record("refuse-missed", "out_of_scope", "evidence_answer"),
            self.make_behavior_record(
                "clarify-ok",
                "needs_clarification",
                "needs_clarification",
            ),
            self.make_behavior_record(
                "clarify-missed",
                "needs_clarification",
                "evidence_answer",
            ),
        ]

        summary = evaluation.summarize_evaluation(records)
        self.assertEqual(
            summary["denominators"],
            {
                "retrieval_gold": 2,
                "should_answer": 2,
                "should_refuse": 2,
                "should_clarify": 2,
                "service_failures": 0,
                "judge_succeeded": 0,
                "judge_failed": 0,
                "judge_not_run": 6,
            },
        )
        expected_ratio = {"numerator": 1, "denominator": 2, "value": 0.5}
        self.assertEqual(summary["refusal_recall"], expected_ratio)
        self.assertEqual(summary["over_refusal_rate"], expected_ratio)
        self.assertEqual(summary["clarification_recall"], expected_ratio)
        self.assertEqual(
            summary["answer_mode_accuracy"],
            {"numerator": 3, "denominator": 6, "value": 0.5},
        )

        report = evaluation.render_eval_report(records)
        for denominator_name, value in summary["denominators"].items():
            with self.subTest(denominator_name=denominator_name):
                self.assertRegex(
                    report,
                    rf"(?im){denominator_name}\s*[:=]\s*{value}\b",
                )
        self.assertRegex(report, r"(?im)over_refusal_rate[^\n]*1/2[^\n]*0\.500")

    def test_report_subgroups_render_unavailable_v2_metrics_as_na(self) -> None:
        cases = [
            EvalCase(
                case_id="no-gold",
                question="合成问题 A",
                case_type="article_lookup",
                expected_law="",
                expected_articles=[],
                keywords=["合成事项 A"],
                expected_behavior="evidence_answer",
                schema_version=2,
            ),
            EvalCase(
                case_id="refusal-no-gold",
                question="合成问题 B",
                case_type="refusal",
                expected_law="",
                expected_articles=[],
                keywords=["合成事项 B"],
                expected_behavior="out_of_scope",
                schema_version=2,
            ),
        ]
        records: list[EvalRecord] = []
        for case, model in zip(cases, ("retrieval-only-a", "retrieval-only-b"), strict=True):
            records.extend(
                evaluation.evaluate(
                    cases=[case],
                    retriever=DeterministicRetriever(),
                    chunk_strategy="article",
                    model=model,
                    generate=False,
                )
            )

        report = evaluation.render_eval_report(records)

        self.assertRegex(
            report,
            r"`retrieval-only-a` n=1 Hit@5\(scored\)=N/A "
            r"KeywordCov=N/A VerifierPass=N/A",
        )
        self.assertRegex(
            report,
            r"`retrieval-only-b` n=1 Hit@5\(scored\)=N/A "
            r"KeywordCov=N/A VerifierPass=N/A",
        )
        self.assertRegex(
            report,
            r"`refusal` n=1 Hit@3=N/A Hit@5=N/A MRR=N/A "
            r"TargetCoverage=N/A",
        )
        self.assertNotIn("KeywordCov=-1.000", report)

    def test_report_target_coverage_excludes_cases_without_article_gold(self) -> None:
        article_gold = self.make_behavior_record(
            "article-gold",
            "evidence_answer",
            "evidence_answer",
        )
        article_gold.canonical_metrics = {
            "hit_at_3": {"value": True, "unavailable_reason": None},
            "hit_at_5": {"value": True, "unavailable_reason": None},
            "mrr": {"value": 1.0, "unavailable_reason": None},
            "target_coverage": {"value": 1.0, "unavailable_reason": None},
        }
        law_only = self.make_behavior_record(
            "law-only",
            "evidence_answer",
            "evidence_answer",
        )
        law_only.target_coverage = 0.0
        law_only.canonical_metrics = {
            "hit_at_3": {"value": True, "unavailable_reason": None},
            "hit_at_5": {"value": True, "unavailable_reason": None},
            "mrr": {"value": 1.0, "unavailable_reason": None},
            "target_coverage": {
                "value": None,
                "unavailable_reason": "no_article_gold",
            },
        }

        report = evaluation.render_eval_report([article_gold, law_only])

        self.assertIn("目标条文覆盖率 (retrieval gold): 1.000", report)
        self.assertNotIn("目标条文覆盖率 (retrieval gold): 0.500", report)

    def test_model_subgroups_only_average_successful_judge_results(self) -> None:
        records: list[EvalRecord] = []
        for model in ("judge-error-a", "judge-error-b"):
            record = self.make_behavior_record(
                model,
                "evidence_answer",
                "evidence_answer",
            )
            record.model = model
            record.execution["judge"] = {"status": "error", "reason": "timeout"}
            # Deliberately inconsistent legacy values must not override the
            # authoritative v2 stage status and unavailable canonical values.
            record.judge_faithfulness = 1.0
            record.judge_pass = 1
            record.judge_error = ""
            record.canonical_metrics.update(
                {
                    "judge_faithfulness": {
                        "value": None,
                        "unavailable_reason": "timeout",
                    },
                    "judge_pass": {
                        "value": None,
                        "unavailable_reason": "timeout",
                    },
                }
            )
            records.append(record)

        report = evaluation.render_eval_report(records)

        self.assertNotIn("JudgeFaith=", report)
        self.assertEqual(report.count("JudgeErrors=1"), 2)

    def test_write_eval_outputs_refuses_if_either_target_already_exists(self) -> None:
        record = self.make_behavior_record(
            "immutable-output",
            "evidence_answer",
            "evidence_answer",
        )
        for existing_suffix, other_suffix in ((".csv", ".md"), (".md", ".csv")):
            with self.subTest(existing_suffix=existing_suffix):
                with tempfile.TemporaryDirectory() as temp_dir:
                    output_dir = Path(temp_dir)
                    existing_path = output_dir / f"m1{existing_suffix}"
                    other_path = output_dir / f"m1{other_suffix}"
                    existing_path.write_text("do not overwrite", encoding="utf-8")

                    with self.assertRaises(FileExistsError):
                        evaluation.write_eval_outputs([record], output_dir, "m1")

                    self.assertEqual(
                        existing_path.read_text(encoding="utf-8"),
                        "do not overwrite",
                    )
                    self.assertFalse(other_path.exists())

    def test_contiguous_session_group_resets_only_at_group_boundaries(self) -> None:
        assistant = SessionTrackingAssistant()
        cases = [
            self.make_session_case("g1-0", session_group="g1", turn_index=0),
            self.make_session_case("g1-1", session_group="g1", turn_index=1),
            self.make_session_case("g2-0", session_group="g2", turn_index=0),
            self.make_session_case("g2-1", session_group="g2", turn_index=1),
            self.make_session_case("single", session_group=None, turn_index=0),
        ]

        records = evaluation.evaluate(
            cases=cases,
            retriever=DeterministicRetriever(),
            chunk_strategy="article",
            model="session-fake",
            generate=True,
            assistant=assistant,
        )

        self.assertEqual(len(records), 5)
        self.assertEqual(assistant.reset_calls, 3)
        self.assertEqual(assistant.history_before_answer["session g1-0"], [])
        self.assertEqual(
            assistant.history_before_answer["session g1-1"],
            ["session g1-0"],
        )
        self.assertEqual(assistant.history_before_answer["session g2-0"], [])
        self.assertEqual(
            assistant.history_before_answer["session g2-1"],
            ["session g2-0"],
        )
        self.assertEqual(assistant.history_before_answer["session single"], [])

    def test_invalid_session_layouts_are_rejected_before_evaluation(self) -> None:
        invalid_layouts = {
            "reopened_group": [
                self.make_session_case("g-0", session_group="g", turn_index=0),
                self.make_session_case("single", session_group=None, turn_index=0),
                self.make_session_case("g-1", session_group="g", turn_index=1),
            ],
            "skipped_turn": [
                self.make_session_case("g-0", session_group="g", turn_index=0),
                self.make_session_case("g-2", session_group="g", turn_index=2),
            ],
            "single_turn_nonzero": [
                self.make_session_case("single-1", session_group=None, turn_index=1),
            ],
        }

        for scenario, cases in invalid_layouts.items():
            with self.subTest(scenario=scenario):
                with self.assertRaises(ValueError):
                    evaluation.validate_eval_cases(cases)

    def test_load_eval_cases_accepts_expected_answer_mode_alias(self) -> None:
        payload = {
            "id": "alias-case",
            "type": "clarification",
            "question": "需要补充什么信息？",
            "expected_answer_mode": "needs_clarification",
            "schema_version": 2,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            case_path = Path(temp_dir) / "cases.jsonl"
            case_path.write_text(
                json.dumps(payload, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            cases = evaluation.load_eval_cases(case_path)

        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].expected_behavior, "needs_clarification")
        self.assertEqual(cases[0].resolved_expected_behavior, "needs_clarification")

    def test_load_eval_cases_rejects_unknown_behavior_from_either_field(self) -> None:
        for field_name in ("expected_behavior", "expected_answer_mode"):
            with self.subTest(field_name=field_name):
                payload = {
                    "id": f"unknown-{field_name}",
                    "type": "article_lookup",
                    "question": "合成问题",
                    field_name: "unknown_behavior",
                    "schema_version": 2,
                }
                with tempfile.TemporaryDirectory() as temp_dir:
                    case_path = Path(temp_dir) / "cases.jsonl"
                    case_path.write_text(
                        json.dumps(payload, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )

                    with self.assertRaisesRegex(ValueError, "unknown expected_behavior"):
                        evaluation.load_eval_cases(case_path)

    @staticmethod
    def make_behavior_record(
        case_id: str,
        expected_behavior: str,
        observed_answer_mode: str,
    ) -> EvalRecord:
        should_answer = expected_behavior == "evidence_answer"
        return EvalRecord(
            case_id=case_id,
            case_type="article_lookup" if should_answer else "behavior",
            model="deterministic-fake",
            retriever="deterministic",
            chunk_strategy="article",
            hit_at_3=int(should_answer),
            hit_at_5=int(should_answer),
            mrr=float(should_answer),
            target_coverage=float(should_answer),
            keyword_coverage=1.0,
            citation_hit=int(should_answer),
            sufficiency_pass=1,
            citation_valid=1,
            verifier_pass=1,
            # Deliberately uninformative: the v2 summary must derive behavior
            # metrics from expected_behavior and observed_answer_mode instead.
            refusal_correctness=1,
            latency_ms=1,
            answer=f"answer for {case_id}\n\n{LEGAL_DISCLAIMER}",
            sources="[S1] synthetic" if should_answer else "",
            failure_label="hit" if should_answer else "not_applicable",
            metrics_schema_version=2,
            expected_behavior=expected_behavior,
            observed_answer_mode=observed_answer_mode,
            execution={
                "service": {"status": "succeeded", "reason": None},
                "generation": {"status": "succeeded", "reason": None},
                "verification": {"status": "succeeded", "reason": None},
                "judge": {"status": "not_run", "reason": "judge_not_configured"},
            },
            canonical_metrics={},
            generation_attempt={"attempted": True},
        )

    @staticmethod
    def make_session_case(
        case_id: str,
        *,
        session_group: str | None,
        turn_index: int,
    ) -> EvalCase:
        return EvalCase(
            case_id=case_id,
            question=f"session {case_id}",
            case_type="article_lookup",
            expected_law="",
            expected_articles=[],
            keywords=["合成事项"],
            expected_behavior="evidence_answer",
            session_group=session_group,
            turn_index=turn_index,
            schema_version=2,
        )


if __name__ == "__main__":
    unittest.main()
