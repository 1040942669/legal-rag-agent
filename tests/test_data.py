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
        chapter_lines = [article for article in articles if "第二章" in article.raw_text]
        self.assertTrue(chapter_lines)
        self.assertEqual(chapter_lines[0].parse_status, "unmatched")

    def test_normalize_space_before_tiao(self) -> None:
        text = "第一百二十八\u3000条  本条测试空格。"
        articles = self.parse_text(text, "测试法.txt")
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].article_number, "第一百二十八条")
        self.assertIn("本条测试", articles[0].body)


if __name__ == "__main__":
    unittest.main()
