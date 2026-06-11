import unittest

from legal_rag.adaptive import retrieve_adaptive
from legal_rag.chat import LEGAL_DISCLAIMER, LegalChatAssistant, build_qa_prompt
from legal_rag.evidence import check_evidence_sufficiency
from legal_rag.models import Chunk, SearchResult
from legal_rag.verifier import verify_answer


class Phase3Test(unittest.TestCase):
    def make_chunk(self, chunk_id: str, article: str = "第九条") -> Chunk:
        return Chunk(
            chunk_id=chunk_id,
            text="民事主体从事民事活动，应当有利于节约资源、保护生态环境。",
            law_names=["中华人民共和国民法典"],
            article_numbers=[article],
            source_files=["fixture.txt"],
            line_nos=[1],
            strategy="article",
        )

    def test_evidence_checker_detects_missing_article_and_followup_query(self) -> None:
        result = SearchResult(
            chunk=self.make_chunk("a", "第九条"),
            score=1.0,
            rank=1,
            retriever="bm25",
        )
        check = check_evidence_sufficiency(
            "《中华人民共和国民法典》第十条规定了什么？",
            [result],
        )

        self.assertFalse(check.sufficient)
        self.assertIn("missing_article:第十条", check.missing_law_support)
        self.assertTrue(check.followup_queries)

    def test_bounded_followup_runs_once_and_stops(self) -> None:
        chunk_9 = self.make_chunk("a", "第九条")
        chunk_10 = self.make_chunk("b", "第十条")

        class FollowupRetriever:
            name = "bm25"

            def __init__(self) -> None:
                self.calls = 0

            def retrieve(self, query: str, top_k: int = 5):
                self.calls += 1
                if self.calls == 1:
                    return [SearchResult(chunk=chunk_9, score=1.0, rank=1, retriever="bm25")]
                return [SearchResult(chunk=chunk_10, score=2.0, rank=1, retriever="bm25")]

        retriever = FollowupRetriever()
        result = retrieve_adaptive(
            "《中华人民共和国民法典》第十条规定了什么？",
            retriever,
            enabled=False,
            top_k=2,
            max_followup_rounds=1,
        )

        self.assertEqual(retriever.calls, 2)
        self.assertTrue(result.evidence_check.sufficient)
        self.assertEqual(result.followup_trace["rounds_used"], 1)

    def test_verifier_rejects_missing_citation_and_disclaimer(self) -> None:
        result = SearchResult(
            chunk=self.make_chunk("a"),
            score=1.0,
            rank=1,
            retriever="bm25",
        )
        verification = verify_answer(
            "根据资料，民事活动应当保护生态环境 [S2]。",
            [result],
            disclaimer=LEGAL_DISCLAIMER,
        )

        self.assertFalse(verification.passed)
        self.assertIn("2", verification.missing_citations)
        self.assertFalse(verification.disclaimer_present)

    def test_chat_refuses_case_strategy_before_retrieval(self) -> None:
        class FailingRetriever:
            name = "bm25"

            def retrieve(self, query: str, top_k: int = 5):
                raise AssertionError("retriever should not be called")

        assistant = LegalChatAssistant(FailingRetriever(), model="fake")
        answer, results = assistant.answer("这个案子怎么起诉才能胜诉？", generate=False)

        self.assertEqual(results, [])
        self.assertIn("具体案件策略", answer)
        self.assertIn(LEGAL_DISCLAIMER, answer)

    def test_prompt_keeps_citation_and_disclaimer_contract(self) -> None:
        result = SearchResult(
            chunk=self.make_chunk("a"),
            score=1.0,
            rank=1,
            retriever="bm25",
        )
        prompt = build_qa_prompt(
            question="民法典第九条",
            original_question="民法典第九条",
            memory="无",
            results=[result],
        )

        self.assertIn("必须引用资料编号，例如 [S1]", prompt)
        self.assertIn(LEGAL_DISCLAIMER, prompt)
        self.assertIn("[S1] 来源:", prompt)


if __name__ == "__main__":
    unittest.main()
