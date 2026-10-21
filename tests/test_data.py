import unittest
from pathlib import Path
from uuid import uuid4

from legal_rag.data import parse_law_file


class ParseLawFileTest(unittest.TestCase):
    def make_workspace_temp(self) -> Path:
        path = Path(".tmp") / "tests" / uuid4().hex
        path.mkdir(parents=True, exist_ok=True)
        return path

    def parse_text(self, text: str, filename: str = "测试法.txt") -> list:
        path = self.make_workspace_temp() / filename
        path.write_text(text, encoding="utf-8")
        return parse_law_file(path)

    def test_same_line_multiple_from_filename_articles(self) -> None:
        text = (
            "第二十五条 国家推行计划生育，使人口的增长同经济和社会发展计划相适应。"
            "第二十六条 国家保护和改善生活环境和生态环境，防治污染和其他公害。"
        )
        articles = self.parse_text(text, "中华人民共和国宪法.txt")
        self.assertEqual(len(articles), 2)
        self.assertEqual(articles[0].article_number, "第二十五条")
        self.assertIn("计划生育", articles[0].body)
        self.assertNotIn("第二十六条", articles[0].body)
        self.assertEqual(articles[1].article_number, "第二十六条")
        self.assertIn("生态环境", articles[1].body)
        self.assertEqual(articles[0].raw_text.startswith("第二十五条"), True)
        self.assertEqual(articles[1].raw_text.startswith("第二十六条"), True)

    def test_same_line_multiple_with_law_articles(self) -> None:
        text = (
            "《测试法》第一条规定，第一条正文。"
            "《测试法》第二条规定，第二条正文。"
        )
        articles = self.parse_text(text)
        self.assertEqual(len(articles), 2)
        self.assertEqual(articles[0].parse_status, "with_law")
        self.assertEqual(articles[0].article_number, "第一条")
        self.assertEqual(articles[0].body, "第一条正文。")
        self.assertEqual(articles[1].article_number, "第二条")
        self.assertEqual(articles[1].body, "第二条正文。")

    def test_embedded_article_marker_on_continuation_line(self) -> None:
        text = "第八条 正文内容。\n续行内容。第九条 矿藏、水流、森林属于国家所有。"
        articles = self.parse_text(text, "中华人民共和国宪法.txt")
        self.assertEqual(len(articles), 2)
        self.assertEqual(articles[0].article_number, "第八条")
        self.assertIn("续行内容", articles[0].body)
        self.assertEqual(articles[1].article_number, "第九条")
        self.assertIn("矿藏", articles[1].body)

    def test_multiline_continuation_appends_to_latest_article(self) -> None:
        text = "第一条 总则内容。\n本款为续行内容。\n第二条 分则内容。"
        articles = self.parse_text(text)
        self.assertEqual(len(articles), 2)
        self.assertEqual(articles[0].article_number, "第一条")
        self.assertIn("续行内容", articles[0].body)
        self.assertIn("续行内容", articles[0].raw_text)
        self.assertEqual(articles[1].article_number, "第二条")

    def test_chapter_title_not_parsed_as_article(self) -> None:
        text = "第一章\u3000总则\n第二章\u3000公务员的条件、义务与权利\n第二十一条 公务员的条件。"
        articles = self.parse_text(text, "中华人民共和国公务员法.txt")
        parsed_numbers = [article.article_number for article in articles if article.parse_status == "from_filename"]
        self.assertEqual(parsed_numbers, ["第二十一条"])
        # Structure lines are dropped instead of becoming an unmatched record.
        self.assertFalse([article for article in articles if "第二章" in article.raw_text])

    def test_cross_reference_does_not_split_article(self) -> None:
        text = (
            "第五十八条\u3000除依照本法第五十三条第（二）项、第五十五条规定给予的强制许可外，"
            "强制许可的实施应当主要为了供应国内市场。\n"
            "第二百六十五条\u3000以牟利为目的，盗接他人通信线路的，依照本法第二百六十四条的规定定罪处罚。"
        )
        articles = self.parse_text(text, "中华人民共和国测试法.txt")
        self.assertEqual(
            [article.article_number for article in articles], ["第五十八条", "第二百六十五条"]
        )
        self.assertTrue(articles[0].raw_text.endswith("供应国内市场。"))
        self.assertTrue(articles[1].body.endswith("的规定定罪处罚。"))

    def test_amended_zhi_article_is_its_own_article(self) -> None:
        text = (
            "《中华人民共和国刑法》第一百三十三条规定，违反交通运输管理法规，因而发生重大事故的，处三年以下有期徒刑。\n"
            "第一百三十三条之一\u3000在道路上驾驶机动车，醉酒驾驶机动车的，处拘役，并处罚金。\n"
            "《中华人民共和国刑法》第一百三十四条规定，有前款行为，同时构成第一百三十三条之一规定的犯罪的，依照处罚较重的规定定罪处罚。"
        )
        articles = self.parse_text(text, "中华人民共和国刑法.txt")
        self.assertEqual(
            [article.article_number for article in articles],
            ["第一百三十三条", "第一百三十三条之一", "第一百三十四条"],
        )
        self.assertTrue(articles[1].body.startswith("在道路上驾驶机动车"))
        self.assertTrue(articles[2].body.endswith("依照处罚较重的规定定罪处罚。"))

    def test_headings_toc_and_preamble_are_not_glued_into_articles(self) -> None:
        text = (
            "中华人民共和国测试法\n"
            "> （2020年1月1日通过）\n"
            "目\u3000\u3000录\n"
            "第一章\u3000总则\n"
            "第二章\u3000和解\n"
            "第一章\u3000总则\n"
            "第一条\u3000为了测试，制定本法。\n"
            "第二章\u3000和解\n"
            "第二条\u3000当事人可以和解。\n"
            "### 第三章强制措施\n"
            "## 二、回避\n"
            "分则\n"
            "第三条\u3000本法自公布之日起施行。"
        )
        articles = self.parse_text(text, "中华人民共和国测试法.txt")
        self.assertEqual(articles[0].parse_status, "unmatched")
        self.assertEqual(articles[0].raw_text, "中华人民共和国测试法\n> （2020年1月1日通过）")
        self.assertEqual(
            [(article.article_number, article.raw_text) for article in articles[1:]],
            [
                ("第一条", "第一条\u3000为了测试，制定本法。"),
                ("第二条", "第二条\u3000当事人可以和解。"),
                ("第三条", "第三条\u3000本法自公布之日起施行。"),
            ],
        )

    def test_reference_to_chapter_is_kept_as_article_text(self) -> None:
        text = "第一条\u3000总则内容。\n第一章规定的事项，由国务院另行规定"
        articles = self.parse_text(text, "中华人民共和国测试法.txt")
        self.assertEqual(len(articles), 1)
        self.assertIn("第一章规定的事项", articles[0].raw_text)

    def test_inline_trailing_heading_is_stripped(self) -> None:
        text = (
            "第三十二条 中华人民共和国保护在中国境内的外国人的合法权利和利益。第二章 公民的基本权利和义务\n"
            "第三十三条 凡具有中华人民共和国国籍的人都是中华人民共和国公民。"
        )
        articles = self.parse_text(text, "中华人民共和国宪法.txt")
        self.assertEqual([article.article_number for article in articles], ["第三十二条", "第三十三条"])
        self.assertTrue(articles[0].raw_text.endswith("合法权利和利益。"))

    def test_markdown_and_unseparated_headers(self) -> None:
        text = (
            "第一条 **第一条正文。\n"
            "**第二条 **专利纠纷案件由知识产权法院管辖。\n"
            "第一百二十八\u3000条侦查人员应当进行勘验。"
        )
        articles = self.parse_text(text, "中华人民共和国测试法.txt")
        self.assertEqual(
            [article.article_number for article in articles],
            ["第一条", "第二条", "第一百二十八条"],
        )
        # Decoration before a header is not appended to the previous article.
        self.assertEqual(articles[0].raw_text, "第一条 **第一条正文。")
        self.assertIn("侦查人员", articles[2].body)

    def test_parsed_sample_has_no_integrity_issues(self) -> None:
        from legal_rag.corpus_integrity import audit_article_integrity, integrity_issue_count

        text = (
            "目\u3000\u3000录\n第一章\u3000总则\n"
            "第一条\u3000为了测试，制定本法。\n"
            "第二条\u3000违反本法第一条的规定的，依照本法第一条处理。\n"
            "第二章\u3000附则\n"
            "第三条\u3000本法自公布之日起施行。"
        )
        report = audit_article_integrity(self.parse_text(text, "中华人民共和国测试法.txt"))
        self.assertEqual(integrity_issue_count(report), 0)
        self.assertEqual(report["unterminated_article_count"], 0)

    def test_normalize_space_before_tiao(self) -> None:
        text = "第一百二十八\u3000条  本条测试空格。"
        articles = self.parse_text(text, "测试法.txt")
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].article_number, "第一百二十八条")
        self.assertIn("本条测试", articles[0].body)


if __name__ == "__main__":
    unittest.main()
