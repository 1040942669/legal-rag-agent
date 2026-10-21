import unittest

from legal_rag.models import Chunk
from legal_rag.query_parser import (
    LawAliasIndex,
    canonical_article_label,
    chunk_citation_keys,
    law_title_aliases,
    parse_query,
)
from legal_rag.retrieval import EXACT_CITATION_BOOST, BM25Retriever


INTERPRETATION = "最高人民法院关于适用《中华人民共和国民事诉讼法》的解释(2022)"
LAWS = [
    "中华人民共和国保险法",
    "中华人民共和国社会保险法",
    "中华人民共和国刑法",
    "中华人民共和国民事诉讼法",
    "中华人民共和国能源法",
    "中华人民共和国节约能源法",
    "中华人民共和国劳动合同法",
    INTERPRETATION,
]


def make_chunk(chunk_id: str, text: str, law: str | list[str], *articles: str) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        text=text,
        law_names=[law] if isinstance(law, str) else law,
        article_numbers=list(articles),
        source_files=["test.txt"],
        line_nos=[1],
        strategy="article",
    )


class LawAliasIndexTest(unittest.TestCase):
    def test_aliases_cover_short_yearless_and_nested_spellings(self) -> None:
        self.assertEqual(law_title_aliases("中华人民共和国刑法"), ["中华人民共和国刑法", "刑法"])
        aliases = law_title_aliases(INTERPRETATION)
        self.assertEqual(aliases[0], INTERPRETATION)
        self.assertIn("最高人民法院关于适用《中华人民共和国民事诉讼法》的解释", aliases)
        self.assertIn("最高人民法院关于适用〈中华人民共和国民事诉讼法〉的解释", aliases)
        self.assertEqual(law_title_aliases("中华人民共和国\u3000刑法")[0], "中华人民共和国刑法")

    def test_full_title_owns_its_spelling_and_shared_short_names_are_dropped(self) -> None:
        index = LawAliasIndex(["中华人民共和国测试法", "测试法", "甲国测试法", "中华人民共和国甲国测试法"])
        self.assertEqual(index.resolve("测试法"), "测试法")
        self.assertEqual(index.resolve("中华人民共和国测试法"), "中华人民共和国测试法")
        self.assertEqual(index.resolve("甲国测试法"), "甲国测试法")
        versions = LawAliasIndex(["中华人民共和国乙法", "中华人民共和国乙法(2020)"])
        self.assertEqual(versions.resolve("中华人民共和国乙法"), "中华人民共和国乙法")
        self.assertEqual(versions.resolve("乙法(2020)"), "中华人民共和国乙法(2020)")
        # 乙法 abbreviates both versions, so it names neither.
        self.assertIsNone(versions.resolve("乙法"))

    def test_ambiguous_derived_alias_is_not_resolved(self) -> None:
        index = LawAliasIndex(["中华人民共和国丙法(2020)", "中华人民共和国丙法(2023)"])
        self.assertIsNone(index.resolve("丙法"))
        self.assertIn("丙法", index.ambiguous_aliases)


class ParseQueryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.index = LawAliasIndex(LAWS)

    def test_near_name_laws_resolve_exactly(self) -> None:
        cases = {
            "保险法第九十八条规定了什么？": "中华人民共和国保险法",
            "社会保险法第九十八条规定了什么？": "中华人民共和国社会保险法",
            "能源法第七十五条": "中华人民共和国能源法",
            "节约能源法第七十五条": "中华人民共和国节约能源法",
        }
        for question, law in cases.items():
            with self.subTest(question=question):
                self.assertEqual(parse_query(question, self.index).law_names, [law])

    def test_article_numbers_in_any_spelling_become_corpus_labels(self) -> None:
        parsed = parse_query("刑法第264条、第２６６条和第二百六十七条", self.index)
        self.assertEqual(
            parsed.article_labels, ["第二百六十四条", "第二百六十六条", "第二百六十七条"]
        )
        self.assertEqual(
            parse_query("刑法第264、266条", self.index).article_labels,
            ["第二百六十四条", "第二百六十六条"],
        )
        self.assertEqual(
            parse_query("刑法第 10 至 12 条", self.index).article_labels,
            ["第十条", "第十二条"],
        )
        self.assertEqual(
            parse_query("刑法第一百三十三条之一", self.index).article_labels,
            ["第一百三十三条之一"],
        )

    def test_invalid_numerals_are_skipped(self) -> None:
        for question in ("刑法第零条", "刑法第一二条", "刑法第10000条", "刑法第百条"):
            with self.subTest(question=question):
                self.assertEqual(parse_query(question, self.index).articles, ())

    def test_article_belongs_to_nearest_law_in_the_same_sentence(self) -> None:
        parsed = parse_query("节约能源法第七十五条与能源法第3条有何不同", self.index)
        self.assertEqual(
            parsed.citations,
            [
                ("中华人民共和国节约能源法", "第七十五条"),
                ("中华人民共和国能源法", "第三条"),
            ],
        )
        sole = parse_query("第二百六十四条在刑法里。另外第五条呢", self.index)
        self.assertEqual(
            sole.citations,
            [("中华人民共和国刑法", "第二百六十四条"), ("中华人民共和国刑法", "第五条")],
        )
        split = parse_query("保险法和刑法有何区别。第五条怎么规定", self.index)
        self.assertEqual(split.citations, [])
        self.assertEqual(split.article_labels, ["第五条"])

    def test_bracketed_title_must_be_named_whole(self) -> None:
        parsed = parse_query("《劳动合同法实施条例》第三条", self.index)
        self.assertEqual(parsed.law_names, [])
        self.assertEqual(parsed.unresolved_titles, ("劳动合同法实施条例",))
        self.assertEqual(
            parse_query("《劳动合同法》第三条", self.index).citations,
            [("中华人民共和国劳动合同法", "第三条")],
        )

    def test_interpretation_title_wins_over_the_law_it_quotes(self) -> None:
        for question in (
            f"{INTERPRETATION}第5条规定了什么？",
            "《最高人民法院关于适用〈中华人民共和国民事诉讼法〉的解释》第五条",
        ):
            with self.subTest(question=question):
                parsed = parse_query(question, self.index)
                self.assertEqual(parsed.citations, [(INTERPRETATION, "第五条")])
                self.assertEqual(parsed.unresolved_titles, ())

    def test_rewritten_text_uses_corpus_spelling(self) -> None:
        parsed = parse_query("刑法第264、266条怎么规定？", self.index)
        self.assertEqual(parsed.rewritten_text(), "刑法第二百六十四条 第二百六十六条怎么规定?")
        self.assertEqual(parse_query("盗窃怎么处罚").rewritten_text(), "盗窃怎么处罚")


class CitationKeyTest(unittest.TestCase):
    def test_keys_use_canonical_labels_and_skip_mixed_law_chunks(self) -> None:
        self.assertEqual(canonical_article_label("第264条"), "第二百六十四条")
        self.assertEqual(canonical_article_label("第一百三十三条之一"), "第一百三十三条之一")
        single = make_chunk("a", "text", "中华人民共和国刑法", "第264条", "第二百六十五条")
        self.assertEqual(
            chunk_citation_keys(single),
            {
                ("中华人民共和国刑法", "第二百六十四条"),
                ("中华人民共和国刑法", "第二百六十五条"),
            },
        )
        mixed = make_chunk("b", "text", ["中华人民共和国刑法", "中华人民共和国保险法"], "第一条")
        self.assertEqual(chunk_citation_keys(mixed), frozenset())


class BM25CitationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.chunks = [
            make_chunk(
                "insurance-98",
                "第九十八条 保险公司应当按照规定提取各项责任准备金。",
                "中华人民共和国保险法",
                "第九十八条",
            ),
            make_chunk(
                "social-98",
                "第九十八条 社会保险经办机构应当按时足额支付社会保险待遇。",
                "中华人民共和国社会保险法",
                "第九十八条",
            ),
            make_chunk(
                "criminal-264",
                "第二百六十四条 盗窃公私财物，数额较大的，处三年以下有期徒刑。",
                "中华人民共和国刑法",
                "第二百六十四条",
            ),
            make_chunk(
                "criminal-266",
                "第二百六十六条 诈骗公私财物，数额较大的，依照本法第二百六十四条处罚。",
                "中华人民共和国刑法",
                "第二百六十六条",
            ),
        ]
        self.retriever = BM25Retriever(self.chunks)

    def test_arabic_article_number_hits_the_exact_article_first(self) -> None:
        results = self.retriever.retrieve("刑法第264条规定了什么？", top_k=3)
        self.assertEqual(results[0].chunk.chunk_id, "criminal-264")
        self.assertEqual(results[0].trace["exact_citation_boost"], EXACT_CITATION_BOOST)
        self.assertEqual(results[0].trace["article_hints"], ["第二百六十四条"])
        self.assertNotIn("exact_citation_boost", results[1].trace)
        self.assertGreater(results[0].score, results[1].score)

    def test_short_name_does_not_boost_a_longer_law_that_contains_it(self) -> None:
        results = self.retriever.retrieve("保险法第九十八条规定了什么？", top_k=2)
        self.assertEqual(results[0].chunk.chunk_id, "insurance-98")
        social = next(item for item in results if item.chunk.chunk_id == "social-98")
        self.assertEqual(social.trace["law_hints"], ["中华人民共和国保险法"])
        self.assertNotIn("exact_citation_boost", social.trace)
        # Same article number, other law: article boost only, no law boost.
        self.assertEqual(social.trace["metadata_boost"], 80.0)

    def test_exact_matches_follow_citation_order(self) -> None:
        for question, expected in (
            ("刑法第266条和第264条有什么区别", ["criminal-266", "criminal-264"]),
            ("刑法第264条和第266条有什么区别", ["criminal-264", "criminal-266"]),
        ):
            with self.subTest(question=question):
                results = self.retriever.retrieve(question, top_k=2)
                self.assertEqual([item.chunk.chunk_id for item in results], expected)

    def test_unknown_title_is_traced_and_not_boosted(self) -> None:
        results = self.retriever.retrieve("《保险法实施细则》第九十八条", top_k=2)
        for result in results:
            self.assertEqual(result.trace["law_hints"], [])
            self.assertEqual(result.trace["unresolved_law_titles"], ["保险法实施细则"])
            self.assertNotIn("exact_citation_boost", result.trace)


if __name__ == "__main__":
    unittest.main()
