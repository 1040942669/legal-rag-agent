import json
import unittest
from pathlib import Path
from uuid import uuid4

from legal_rag.citation_cases import (
    build_citation_cases,
    select_citation_targets,
    short_law_name,
    write_citation_cases,
)
from legal_rag.cli import main
from legal_rag.evaluation import load_eval_cases
from legal_rag.models import Chunk, EvalCase, LawArticle, SearchResult
from legal_rag.retrieval_audit import (
    GOLD_AMBIGUOUS,
    GOLD_MISSING,
    GOLD_UNIQUE,
    article_identity_index,
    audit_retrieval,
    render_audit_markdown,
)


def make_chunk(chunk_id: str, law: str, article: str) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        text=f"{law}{article}",
        law_names=[law],
        article_numbers=[article],
        source_files=[f"{law}.txt"],
        line_nos=[1],
        strategy="article",
        metadata={"article_ids": [f"a-{chunk_id}"], "article_count": 1},
    )


def make_case(case_id: str, law: str, articles: list[str], case_type: str = "lookup") -> EvalCase:
    return EvalCase(
        case_id=case_id,
        question=case_id,
        case_type=case_type,
        expected_law=law,
        expected_articles=articles,
        keywords=[],
    )


class FixedRetriever:
    name = "fixed"

    def __init__(self, ranking: dict[str, list[Chunk]]) -> None:
        self.ranking = ranking
        self.calls: list[tuple[str, int]] = []

    def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
        self.calls.append((query, top_k))
        if query == "boom":
            raise RuntimeError("provider down")
        return [
            SearchResult(chunk=chunk, score=1.0 / rank, rank=rank, retriever=self.name)
            for rank, chunk in enumerate(self.ranking.get(query, [])[:top_k], start=1)
        ]


class RetrievalAuditTest(unittest.TestCase):
    LAW = "中华人民共和国测试法"

    def setUp(self) -> None:
        self.a1 = make_chunk("1", self.LAW, "第一条")
        self.a2 = make_chunk("2", self.LAW, "第二条")
        self.a2_fragment = make_chunk("2f", self.LAW, "第二条")
        self.other = make_chunk("9", "中华人民共和国其他法", "第一条")
        self.noise = [make_chunk(f"n{i}", "中华人民共和国噪声法", f"第{i}条") for i in range(3, 12)]
        self.chunks = [self.a1, self.a2, self.a2_fragment, self.other, *self.noise]

    def test_identity_index_flags_ambiguous_and_missing_gold(self) -> None:
        index = article_identity_index(self.chunks)
        self.assertEqual(len(index[(self.LAW, "第二条")]), 2)
        retriever = FixedRetriever({})
        cases = [
            make_case("unique", self.LAW, ["第一条"]),
            make_case("ambiguous", self.LAW, ["第二条"]),
            make_case("missing", self.LAW, ["第三条"]),
        ]
        rows, summary = audit_retrieval(cases, retriever, depth=5, chunks=self.chunks)
        statuses = {row.case_id: row.gold_status for row in rows}
        self.assertEqual(statuses["unique"], GOLD_UNIQUE)
        self.assertEqual(statuses["ambiguous"], GOLD_AMBIGUOUS)
        self.assertEqual(statuses["missing"], GOLD_MISSING)
        self.assertEqual(summary["gold_status_counts"], {"ambiguous": 1, "missing": 1, "unique": 1})

    def test_hits_recall_and_depth(self) -> None:
        ranking = {
            "late": [*self.noise[:7], self.a1],
            "multi": [self.a1, *self.noise[:4], self.a2],
            "wrong_law": [self.other],
            "boom": [],
        }
        retriever = FixedRetriever(ranking)
        cases = [
            make_case("late", self.LAW, ["第一条"], "lookup"),
            make_case("multi", self.LAW, ["第一条", "第二条"], "multi"),
            make_case("wrong_law", self.LAW, ["第一条"], "lookup"),
            make_case("boom", self.LAW, ["第一条"], "lookup"),
            EvalCase(
                case_id="refusal",
                question="refusal",
                case_type="refusal",
                expected_law="",
                expected_articles=[],
                keywords=[],
            ),
        ]
        rows, summary = audit_retrieval(cases, retriever, depth=10, ks=(1, 5, 10))
        by_id = {row.case_id: row for row in rows}
        self.assertEqual({top_k for _, top_k in retriever.calls}, {10})
        self.assertEqual(by_id["late"].first_hit_rank, 8)
        self.assertEqual(by_id["multi"].first_hit_rank, 1)
        self.assertEqual(by_id["multi"].covered_at, {1: 1, 5: 1, 10: 2})
        self.assertIsNone(by_id["wrong_law"].first_hit_rank)
        self.assertEqual(by_id["boom"].error, "RuntimeError")
        self.assertEqual(summary["case_count"], 4)
        self.assertEqual(summary["skipped_without_gold"], 1)
        self.assertEqual(summary["error_count"], 1)
        self.assertEqual(summary["hit_at"], {"1": 0.25, "5": 0.25, "10": 0.5})
        self.assertEqual(summary["article_recall_at"]["5"], 0.125)
        self.assertEqual(summary["article_recall_at"]["10"], 0.5)
        self.assertEqual(summary["mrr_at_10"], round((1 / 8 + 1) / 4, 4))
        self.assertEqual(summary["by_type"]["multi"]["hit_at_5"], 1.0)
        markdown = render_audit_markdown(summary, {"retriever": "fixed"})
        self.assertIn("| 10 | 0.5000", markdown)
        self.assertIn("MRR_AT_10", markdown)


class CitationCasesTest(unittest.TestCase):
    def make_article(self, law: str, number: str, index: int) -> LawArticle:
        return LawArticle(
            article_id=f"{law}-{number}-{index}",
            law_name=law,
            article_number=number,
            body="正文。",
            raw_text=f"{number}　正文。",
            source_file=f"{law}.txt",
            line_no=index,
            parse_status="from_filename",
        )

    def test_short_law_name(self) -> None:
        self.assertEqual(short_law_name("中华人民共和国刑法"), "刑法")
        self.assertIsNone(short_law_name("工伤保险条例"))

    def test_selection_skips_ambiguous_identities_and_is_deterministic(self) -> None:
        articles = [
            self.make_article("中华人民共和国刑法", "第二百六十四条", 1),
            self.make_article("中华人民共和国刑法", "第二百六十四条", 2),
            self.make_article("中华人民共和国刑法", "第三条", 3),
            self.make_article("工伤保险条例", "第十四条", 4),
            self.make_article("工伤保险条例", "", 5),
        ]
        first = select_citation_targets(articles, per_law=3, seed=7)
        second = select_citation_targets(list(reversed(articles)), per_law=3, seed=7)
        self.assertEqual([item.article_id for item in first], [item.article_id for item in second])
        self.assertEqual(
            [(item.law_name, item.article_number) for item in first],
            [("中华人民共和国刑法", "第三条"), ("工伤保险条例", "第十四条")],
        )
        with self.assertRaises(ValueError):
            select_citation_targets(articles, per_law=0)

    def test_cases_cover_full_short_and_arabic_spellings(self) -> None:
        cases = build_citation_cases(
            [
                self.make_article("中华人民共和国刑法", "第二百六十四条", 1),
                self.make_article("工伤保险条例", "第十四条", 2),
            ]
        )
        questions = {case["id"].rsplit("_", 1)[1] + ":" + case["expected_law"]: case["question"] for case in cases}
        self.assertEqual(questions["full:中华人民共和国刑法"], "《中华人民共和国刑法》第二百六十四条规定了什么？")
        self.assertEqual(questions["short:中华人民共和国刑法"], "刑法第二百六十四条规定了什么？")
        self.assertEqual(questions["arabic:中华人民共和国刑法"], "刑法第264条规定了什么？")
        self.assertEqual(questions["arabic:工伤保险条例"], "工伤保险条例第14条规定了什么？")
        self.assertNotIn("short:工伤保险条例", questions)
        self.assertEqual(len({case["id"] for case in cases}), len(cases))

        path = Path(".tmp") / "tests" / uuid4().hex / "citation.jsonl"
        write_citation_cases(cases, path)
        loaded = load_eval_cases(path)
        self.assertEqual(len(loaded), 5)
        self.assertEqual(loaded[0].expected_articles, ["第二百六十四条"])


class RetrievalAuditCliTest(unittest.TestCase):
    def test_generate_cases_and_audit_bm25_end_to_end(self) -> None:
        root = Path(".tmp") / "tests" / uuid4().hex
        data_dir = Path(__file__).parent / "fixtures" / "Chinese-Laws"
        cases_path = root / "citation.jsonl"
        self.assertEqual(
            main(
                [
                    "generate-citation-cases",
                    "--data-dir",
                    str(data_dir),
                    "--output",
                    str(cases_path),
                    "--per-law",
                    "2",
                ]
            ),
            0,
        )
        profile_dir = root / "profile"
        profile_dir.mkdir(parents=True)
        (profile_dir / "data_profile.json").write_text("{}", encoding="utf-8")
        self.assertEqual(
            main(
                [
                    "build-index",
                    "--data-dir",
                    str(data_dir),
                    "--profile-path",
                    str(profile_dir / "data_profile.json"),
                    "--index-root",
                    str(root / "indexes"),
                    "--chunk-strategy",
                    "article",
                ]
            ),
            0,
        )
        self.assertEqual(
            main(
                [
                    "retrieval-audit",
                    "--index-dir",
                    str(root / "indexes" / "article"),
                    "--retriever",
                    "bm25",
                    "--cases",
                    str(cases_path),
                    "--depth",
                    "3",
                    "--data-dir",
                    str(data_dir),
                    "--output-dir",
                    str(root / "audit"),
                    "--prefix",
                    "fixture",
                ]
            ),
            0,
        )
        payload = json.loads((root / "audit" / "fixture_citation.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["summary"]["depth"], 3)
        self.assertEqual(payload["summary"]["case_count"], len(load_eval_cases(cases_path)))
        self.assertEqual(payload["integrity"]["identity_collision_groups"], 0)
        self.assertEqual(payload["summary"]["gold_status_counts"], {"unique": payload["summary"]["case_count"]})
        self.assertTrue((root / "audit" / "fixture_citation.md").exists())


if __name__ == "__main__":
    unittest.main()
