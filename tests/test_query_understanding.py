import json
import unittest

from legal_rag.adaptive import retrieve_adaptive, retrieve_and_merge_plans
from legal_rag.models import Chunk, NormalizedQuery, RetrievalPlan, SearchResult
from legal_rag.planning import build_retrieval_plans
from legal_rag.query import analyze_query, should_use_adaptive
from legal_rag.query_understanding import (
    normalize_query,
    parse_normalized_query_json,
)


class QueryUnderstandingTest(unittest.TestCase):
    def make_chunk(self, chunk_id: str, text: str) -> Chunk:
        return Chunk(
            chunk_id=chunk_id,
            text=text,
            law_names=["中华人民共和国民法典"],
            article_numbers=["第九条"],
            source_files=["fixture.txt"],
            line_nos=[1],
            strategy="article",
        )

    def test_parse_normalized_query_json_contract(self) -> None:
        raw = json.dumps(
            {
                "legal_questions": ["网购押金退还适用哪些规定？"],
                "missing_facts": ["押金规则是否已明示"],
                "law_hints": ["中华人民共和国电子商务法"],
                "article_hints": ["第二十一条"],
                "keywords": ["押金", "退还"],
                "risk_flags": [],
                "confidence": 0.82,
            },
            ensure_ascii=False,
        )

        normalized = parse_normalized_query_json(raw, original_query="押金不退怎么办")

        self.assertEqual(normalized.legal_questions, ["网购押金退还适用哪些规定？"])
        self.assertEqual(normalized.law_hints, ["中华人民共和国电子商务法"])
        self.assertEqual(normalized.confidence, 0.82)

    def test_parse_normalized_query_json_rejects_missing_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "Missing normalized query field"):
            parse_normalized_query_json(
                '{"legal_questions":["问题"]}',
                original_query="问题",
            )

    def test_adaptive_trigger_only_for_complex_inputs(self) -> None:
        clear = analyze_query("《中华人民共和国民法典》第九条规定了什么？")
        complex_query = analyze_query("商家不退押金，另外还把我照片拿去宣传，气死了！")

        self.assertFalse(should_use_adaptive(clear))
        self.assertTrue(should_use_adaptive(complex_query))
        self.assertIn("multi_intent", complex_query.adaptive_reasons)
        self.assertIn("emotional", complex_query.adaptive_reasons)

    def test_llm_normalizer_falls_back_on_invalid_json(self) -> None:
        class BadClient:
            def __init__(self) -> None:
                self.calls = 0

            def complete(self, prompt: str) -> str:
                self.calls += 1
                return "not json"

        client = BadClient()
        analysis = analyze_query("这个事情有没有依据？")
        normalized = normalize_query(
            analysis.original_query,
            analysis=analysis,
            llm_client=client,
            use_llm=True,
            max_retries=1,
        )

        self.assertEqual(client.calls, 2)
        self.assertEqual(normalized.source, "rules:llm_error")
        self.assertTrue(normalized.errors)
        self.assertTrue(normalized.legal_questions)

    def test_planner_limits_queries_and_adds_hints(self) -> None:
        normalized = NormalizedQuery(
            original_query="多意图问题",
            legal_questions=["押金退还", "肖像宣传", "劳动工资", "饮酒驾驶"],
            missing_facts=[],
            law_hints=[],
            article_hints=[],
            keywords=[],
            risk_flags=[],
            confidence=0.6,
        )

        plans, trace = build_retrieval_plans(normalized, max_queries=2, per_plan_top_k=4)

        self.assertEqual(len(plans), 2)
        self.assertEqual(trace["truncated_count"], 2)
        self.assertEqual(plans[0].top_k, 4)
        self.assertIn("押金", plans[0].query)

    def test_multi_query_merge_dedupes_chunks_and_preserves_source_trace(self) -> None:
        chunk_a = self.make_chunk("a", "押金退还依据")
        chunk_b = self.make_chunk("b", "肖像权依据")

        class StaticRetriever:
            name = "bm25"

            def retrieve(self, query: str, top_k: int = 5):
                if "押金" in query:
                    return [
                        SearchResult(chunk=chunk_a, score=2.0, rank=1, retriever="bm25"),
                        SearchResult(chunk=chunk_b, score=1.0, rank=2, retriever="bm25"),
                    ]
                return [
                    SearchResult(chunk=chunk_b, score=3.0, rank=1, retriever="bm25"),
                    SearchResult(chunk=chunk_a, score=0.5, rank=2, retriever="bm25"),
                ]

        plans = [
            RetrievalPlan("q1", "押金", [], [], ["押金"], 2, "test"),
            RetrievalPlan("q2", "肖像", [], [], ["肖像"], 2, "test"),
        ]

        merged, trace = retrieve_and_merge_plans(StaticRetriever(), plans, final_top_k=2)

        self.assertEqual(len(merged), 2)
        self.assertEqual(trace["deduped_count"], 2)
        self.assertEqual(merged[0].rank, 1)
        self.assertIn("adaptive", merged[0].trace)
        self.assertGreaterEqual(len(merged[0].trace["adaptive"]["source_plans"]), 1)

    def test_retrieve_adaptive_skips_clear_query_and_uses_complex_query(self) -> None:
        chunk = self.make_chunk("a", "生态环境")

        class CountingRetriever:
            name = "bm25"

            def __init__(self) -> None:
                self.queries: list[str] = []

            def retrieve(self, query: str, top_k: int = 5):
                self.queries.append(query)
                return [SearchResult(chunk=chunk, score=1.0, rank=1, retriever="bm25")]

        clear_retriever = CountingRetriever()
        clear = retrieve_adaptive(
            "《中华人民共和国民法典》第九条规定了什么？",
            clear_retriever,
            enabled=True,
        )
        complex_retriever = CountingRetriever()
        complex_result = retrieve_adaptive(
            "这个押金不退，另外还把我照片拿去宣传，气死了！",
            complex_retriever,
            enabled=True,
            max_queries=2,
        )

        self.assertFalse(clear.adaptive_used)
        self.assertEqual(len(clear_retriever.queries), 1)
        self.assertTrue(complex_result.adaptive_used)
        self.assertGreaterEqual(len(complex_retriever.queries), 1)
        self.assertTrue(complex_result.plans)


if __name__ == "__main__":
    unittest.main()
