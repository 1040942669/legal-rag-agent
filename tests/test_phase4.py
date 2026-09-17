from __future__ import annotations

import unittest
from pathlib import Path

from legal_rag.chat import estimate_tokens, extract_reference_hints
from legal_rag.evaluation import bootstrap_ci, evaluate, load_eval_cases, render_eval_report
from legal_rag.indexing import mark_deprecated_chunks
from legal_rag.judge import build_judge_prompt, extract_json_object, judge_answer
from legal_rag.models import Chunk, EvalCase, EvalRecord, SearchResult
from legal_rag.query import analyze_query
from legal_rag.retrieval import (
    BM25Retriever,
    build_known_law_hints,
    deprecated_multiplier,
    extract_law_hints,
)


def make_chunk(chunk_id: str, text: str, laws: list[str], articles: list[str], **metadata) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        text=text,
        law_names=laws,
        article_numbers=articles,
        source_files=["test.txt"],
        line_nos=[1],
        strategy="article",
        metadata=metadata,
    )


class DeprecatedLawTest(unittest.TestCase):
    def test_mark_deprecated_chunks(self):
        chunks = [
            make_chunk("a", "合同自由原则", ["中华人民共和国合同法"], ["第四条"]),
            make_chunk("b", "民事主体合法权益", ["中华人民共和国民法典"], ["第三条"]),
        ]
        marked = mark_deprecated_chunks(chunks, ["中华人民共和国合同法"])
        self.assertTrue(marked[0].metadata.get("deprecated"))
        self.assertNotIn("deprecated", marked[1].metadata)

    def test_deprecated_multiplier_respects_explicit_reference(self):
        chunk = make_chunk(
            "a", "text", ["中华人民共和国合同法"], ["第四条"], deprecated=True
        )
        self.assertEqual(deprecated_multiplier(chunk, [], penalty=0.5), 0.5)
        self.assertEqual(deprecated_multiplier(chunk, ["合同法"], penalty=0.5), 1.0)
        fresh = make_chunk("b", "text", ["中华人民共和国民法典"], ["第三条"])
        self.assertEqual(deprecated_multiplier(fresh, [], penalty=0.5), 1.0)

    def test_bm25_penalizes_deprecated_chunk(self):
        deprecated = make_chunk(
            "old",
            "依法成立的合同，对当事人具有法律约束力。",
            ["中华人民共和国合同法"],
            ["第八条"],
            deprecated=True,
        )
        current = make_chunk(
            "new",
            "依法成立的合同，对当事人具有法律约束力。",
            ["中华人民共和国民法典"],
            ["第四百六十五条"],
        )
        retriever = BM25Retriever([deprecated, current], deprecated_penalty=0.5)
        results = retriever.retrieve("依法成立的合同有法律约束力吗", top_k=2)
        self.assertEqual(results[0].chunk.chunk_id, "new")
        # 显式引用废止法律时不降权
        explicit = retriever.retrieve("《合同法》第八条规定了什么", top_k=2)
        self.assertEqual(explicit[0].chunk.chunk_id, "old")


class LawHintTest(unittest.TestCase):
    def test_build_known_law_hints_includes_short_names(self):
        chunks = [make_chunk("a", "text", ["中华人民共和国专利法"], ["第二条"])]
        hints = build_known_law_hints(chunks)
        self.assertIn("中华人民共和国专利法", hints)
        self.assertIn("专利法", hints)

    def test_extract_law_hints_suppresses_substring_matches(self):
        known = ["中华人民共和国社会保险法", "社会保险法", "保险法"]
        hints = extract_law_hints("社会保险法对养老保险怎么规定", known_hints=known)
        self.assertIn("社会保险法", hints)
        self.assertNotIn("保险法", hints)

    def test_extract_law_hints_falls_back_without_known_list(self):
        hints = extract_law_hints("民法典第八条规定了什么")
        self.assertIn("民法典", hints)


class TokenEstimateTest(unittest.TestCase):
    def test_chinese_text_counts_chars(self):
        text = "民法典第八条"
        self.assertGreaterEqual(estimate_tokens(text), 6)

    def test_reference_hints_extracted_from_answer(self):
        answer = "根据《中华人民共和国民法典》第八条，民事主体从事民事活动不得违反法律。"
        hints = extract_reference_hints(answer)
        self.assertIn("中华人民共和国民法典", hints)
        self.assertIn("第八条", hints)


class BootstrapTest(unittest.TestCase):
    def test_bootstrap_ci_deterministic_and_bounded(self):
        values = [1.0, 0.0, 1.0, 1.0, 0.0, 1.0, 1.0, 1.0, 0.0, 1.0]
        first = bootstrap_ci(values)
        second = bootstrap_ci(values)
        self.assertEqual(first, second)
        self.assertLessEqual(first[0], sum(values) / len(values))
        self.assertGreaterEqual(first[1], sum(values) / len(values))

    def test_bootstrap_ci_single_value(self):
        self.assertEqual(bootstrap_ci([0.5]), (0.5, 0.5))

    def test_bootstrap_ci_rejects_invalid_parameters(self):
        with self.assertRaises(ValueError):
            bootstrap_ci([0.5], n_resamples=0)
        with self.assertRaises(ValueError):
            bootstrap_ci([0.5], confidence=1.0)


class StubJudgeClient:
    def __init__(self, response: str):
        self.response = response
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response


class JudgeTest(unittest.TestCase):
    def make_results(self) -> list[SearchResult]:
        chunk = make_chunk("a", "民事主体从事民事活动，不得违反法律。", ["中华人民共和国民法典"], ["第八条"])
        return [SearchResult(chunk=chunk, score=1.0, rank=1, retriever="bm25")]

    def test_judge_parses_strict_json(self):
        client = StubJudgeClient(
            '{"faithfulness": 0.9, "relevance": 0.85, "completeness": 0.8, "passed": true, "comment": "ok"}'
        )
        result = judge_answer(client, question="q", answer="a", results=self.make_results())
        self.assertTrue(result.passed)
        self.assertAlmostEqual(result.faithfulness, 0.9)
        self.assertEqual(result.source, "llm")

    def test_judge_parses_fenced_json(self):
        client = StubJudgeClient(
            '```json\n{"faithfulness": 0.5, "relevance": 0.4, "completeness": 0.3, "passed": false, "comment": "weak"}\n```'
        )
        result = judge_answer(client, question="q", answer="a", results=self.make_results())
        self.assertFalse(result.passed)
        self.assertAlmostEqual(result.relevance, 0.4)

    def test_judge_handles_invalid_json(self):
        client = StubJudgeClient("我觉得回答不错")
        result = judge_answer(client, question="q", answer="a", results=self.make_results())
        self.assertEqual(result.source, "error")
        self.assertFalse(result.passed)

    def test_judge_handles_client_exception(self):
        class FailingClient:
            def complete(self, prompt: str) -> str:
                raise ValueError("transport failed")

        result = judge_answer(
            FailingClient(), question="q", answer="a", results=self.make_results()
        )
        self.assertEqual(result.source, "error")
        self.assertIn("transport failed", result.error)

    def test_judge_recomputes_inconsistent_pass_flag(self):
        client = StubJudgeClient(
            '{"faithfulness": 0.9, "relevance": 0.8, "completeness": 0.7, "passed": false, "comment": "ok"}'
        )
        result = judge_answer(client, question="q", answer="a", results=self.make_results())
        self.assertTrue(result.passed)

    def test_judge_rejects_non_boolean_pass_and_invalid_scores(self):
        non_boolean = StubJudgeClient(
            '{"faithfulness": 0.9, "relevance": 0.8, "completeness": 0.7, "passed": "false"}'
        )
        result = judge_answer(non_boolean, question="q", answer="a", results=self.make_results())
        self.assertEqual(result.source, "error")

        out_of_range = StubJudgeClient(
            '{"faithfulness": 1.2, "relevance": 0.8, "completeness": 0.7, "passed": true}'
        )
        result = judge_answer(out_of_range, question="q", answer="a", results=self.make_results())
        self.assertEqual(result.source, "error")

    def test_extract_json_object_plain(self):
        self.assertEqual(extract_json_object('前缀 {"a": 1} 后缀'), {"a": 1})
        self.assertEqual(extract_json_object('{"a": 1}\n{"b": 2}'), {"a": 1})
        self.assertIsNone(extract_json_object("not json"))

    def test_prompt_contains_sources(self):
        prompt = build_judge_prompt("问题", "回答", self.make_results())
        self.assertIn("[S1]", prompt)
        self.assertIn("民法典", prompt)


def make_eval_record(**overrides) -> EvalRecord:
    values = {
        "case_id": "case",
        "case_type": "article_lookup",
        "model": "model",
        "retriever": "bm25",
        "chunk_strategy": "article",
        "hit_at_3": 1,
        "hit_at_5": 1,
        "mrr": 1.0,
        "target_coverage": 1.0,
        "keyword_coverage": 1.0,
        "citation_hit": 1,
        "sufficiency_pass": 1,
        "citation_valid": 1,
        "verifier_pass": 1,
        "refusal_correctness": 1,
        "latency_ms": 1,
        "answer": "answer",
        "sources": "[S1] source",
        "failure_label": "hit",
    }
    values.update(overrides)
    return EvalRecord(**values)


class EvaluationContractTest(unittest.TestCase):
    def test_v3_case_sets_have_stable_sizes_and_unique_ids(self):
        root = Path(__file__).resolve().parents[1]
        full = load_eval_cases(root / "eval_cases" / "legal_eval_cases_v3.jsonl")
        subset = load_eval_cases(root / "eval_cases" / "legal_eval_cases_v3_gen_subset.jsonl")

        self.assertEqual(len(full), 120)
        self.assertEqual(len(subset), 30)
        self.assertEqual(len({case.case_id for case in full}), len(full))
        self.assertEqual(len({case.case_id for case in subset}), len(subset))
        self.assertTrue({case.case_id for case in subset}.issubset({case.case_id for case in full}))
        self.assertEqual(sum(case.case_type == "refusal" for case in full), 12)
        refusal_flags = {"case_strategy", "illegal_help", "medical_financial_advice", "non_legal"}
        refusal_cases = [case for case in full if case.case_type == "refusal"]
        self.assertTrue(
            all(set(analyze_query(case.question).risk_flags) & refusal_flags for case in refusal_cases)
        )

    def test_retrieval_only_marks_answer_metrics_not_applicable(self):
        result = SearchResult(
            chunk=make_chunk("a", "民事活动不得违反法律。", ["中华人民共和国民法典"], ["第八条"]),
            score=1.0,
            rank=1,
            retriever="static",
        )

        class StaticRetriever:
            name = "static"

            def retrieve(self, query: str, top_k: int = 5):
                return [result]

        case = EvalCase(
            case_id="retrieval-only",
            question="民法典第八条规定了什么？",
            case_type="article_lookup",
            expected_law="中华人民共和国民法典",
            expected_articles=["第八条"],
            keywords=["不得违反法律"],
        )
        records = evaluate(
            cases=[case],
            retriever=StaticRetriever(),
            chunk_strategy="article",
            generate=False,
        )

        self.assertEqual(records[0].citation_valid, -1)
        self.assertEqual(records[0].verifier_pass, -1)
        self.assertEqual(records[0].refusal_correctness, -1)
        self.assertIn("Citation validity: N/A (retrieval-only)", render_eval_report(records))

    def test_judge_errors_are_reported_but_excluded_from_quality_means(self):
        successful = make_eval_record(
            case_id="ok",
            judge_faithfulness=0.9,
            judge_relevance=0.8,
            judge_completeness=0.7,
            judge_pass=1,
        )
        failed = make_eval_record(case_id="error", judge_error="timeout")

        report = render_eval_report([successful, failed])

        self.assertIn("成功 n=1, 失败 n=1", report)
        self.assertIn("Faithfulness: 0.900", report)
        self.assertIn("已从质量均值中排除", report)

    def test_evaluate_does_not_count_judge_transport_failure_as_model_failure(self):
        result = SearchResult(
            chunk=make_chunk("a", "民事活动不得违反法律。", ["中华人民共和国民法典"], ["第八条"]),
            score=1.0,
            rank=1,
            retriever="static",
        )

        class StaticRetriever:
            name = "static"

            def retrieve(self, query: str, top_k: int = 5):
                return [result]

        class StaticAssistant:
            last_adaptive_result = None
            last_evidence_check = None
            last_verification = None

            def reset_memory(self):
                return None

            def answer(self, question: str, *, generate: bool = True):
                return "根据资料，民事活动不得违反法律 [S1]。", [result]

        case = EvalCase(
            case_id="judge-error",
            question="民法典第八条规定了什么？",
            case_type="article_lookup",
            expected_law="中华人民共和国民法典",
            expected_articles=["第八条"],
            keywords=["不得违反法律"],
        )
        records = evaluate(
            cases=[case],
            retriever=StaticRetriever(),
            chunk_strategy="article",
            model="fake",
            generate=True,
            assistant=StaticAssistant(),
            judge_client=StubJudgeClient("not json"),
        )

        self.assertEqual(records[0].judge_pass, -1)
        self.assertEqual(records[0].judge_faithfulness, -1.0)
        self.assertIn("not valid JSON", records[0].judge_error)


if __name__ == "__main__":
    unittest.main()
