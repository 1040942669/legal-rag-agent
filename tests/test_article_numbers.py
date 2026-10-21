import unittest

from legal_rag.article_numbers import (
    article_number_to_int,
    canonical_article_label,
    canonical_article_number,
    chinese_number_to_int,
    format_article_label,
    format_article_number,
    int_to_chinese_number,
    parse_article_label,
)


class ArticleNumberTest(unittest.TestCase):
    CORPUS_SPELLINGS = {
        1: "一",
        10: "十",
        15: "十五",
        20: "二十",
        105: "一百零五",
        110: "一百一十",
        264: "二百六十四",
        1001: "一千零一",
        1010: "一千零一十",
        1100: "一千一百",
        1260: "一千二百六十",
    }

    def test_round_trip_matches_corpus_spelling(self) -> None:
        for value, spelling in self.CORPUS_SPELLINGS.items():
            with self.subTest(value=value):
                self.assertEqual(int_to_chinese_number(value), spelling)
                self.assertEqual(chinese_number_to_int(spelling), value)

    def test_round_trip_covers_full_range(self) -> None:
        for value in range(1, 2000):
            self.assertEqual(chinese_number_to_int(int_to_chinese_number(value)), value)

    def test_alternative_spellings(self) -> None:
        self.assertEqual(chinese_number_to_int("264"), 264)
        self.assertEqual(chinese_number_to_int("２６４"), 264)
        self.assertEqual(chinese_number_to_int("一十五"), 15)
        self.assertEqual(chinese_number_to_int("贰佰陆拾肆"), 264)
        self.assertEqual(chinese_number_to_int("两百"), 200)

    def test_rejects_malformed_numbers(self) -> None:
        for text in ("", "二三", "百", "十百", "零十", "第五", "abc"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    chinese_number_to_int(text)

    def test_article_number_helpers(self) -> None:
        self.assertEqual(article_number_to_int("第二百六十四条"), 264)
        self.assertEqual(article_number_to_int("第264条"), 264)
        self.assertEqual(article_number_to_int("第 264 条"), 264)
        self.assertEqual(format_article_number(1001), "第一千零一条")
        self.assertEqual(canonical_article_number("第110条"), "第一百一十条")
        for text in ("二百六十四条", "第条", "第零条", "第一款"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    article_number_to_int(text)

    def test_amended_zhi_labels(self) -> None:
        self.assertEqual(parse_article_label("第一百三十三条之一"), (133, 1))
        self.assertEqual(parse_article_label("第133条之2"), (133, 2))
        self.assertEqual(parse_article_label("第二百六十四条"), (264, 0))
        self.assertEqual(format_article_label(133, 1), "第一百三十三条之一")
        self.assertEqual(format_article_label(264), "第二百六十四条")
        self.assertEqual(canonical_article_number("第133条之一"), "第一百三十三条之一")
        self.assertEqual(canonical_article_label("第264条"), "第二百六十四条")
        self.assertEqual(canonical_article_label(" 序言 "), "序言")
        for text in ("第一百三十三条之", "第一百三十三条之零", "第一百三十三条之一之二"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_article_label(text)
        # The integer form is only defined for plain articles.
        with self.assertRaises(ValueError):
            article_number_to_int("第一百三十三条之一")

    def test_format_rejects_out_of_range(self) -> None:
        for value in (0, -1, 10000):
            with self.assertRaises(ValueError):
                int_to_chinese_number(value)
        with self.assertRaises(TypeError):
            int_to_chinese_number(True)


if __name__ == "__main__":
    unittest.main()
