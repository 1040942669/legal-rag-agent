import unittest

from legal_rag.corpus_integrity import (
    audit_article_integrity,
    integrity_issue_count,
    is_reference_fragment,
    render_integrity_markdown,
)
from legal_rag.data import is_structure_heading
from legal_rag.models import LawArticle


def make_article(
    number: str,
    raw_text: str,
    *,
    body: str | None = None,
    status: str = "from_filename",
    law: str = "中华人民共和国测试法",
    line_no: int = 1,
) -> LawArticle:
    return LawArticle(
        article_id=f"{number}-{line_no}-{len(raw_text)}",
        law_name=law,
        article_number=number,
        body=body if body is not None else raw_text[len(number) :].lstrip("　 "),
        raw_text=raw_text,
        source_file="中华人民共和国测试法.txt",
        line_no=line_no,
        parse_status=status,
    )


class CorpusIntegrityTest(unittest.TestCase):
    def test_structure_heading_detection(self) -> None:
        for line in (
            "第一章　总则",
            "第二节　一般规定",
            "第一分编　通则",
            "目　　录",
            "附　　则",
            "## 第八章　附则",
        ):
            with self.subTest(line=line):
                self.assertTrue(is_structure_heading(line))
        for line in (
            "第一条　为了测试，制定本法。",
            "第一章规定的事项，由国务院另行规定。",
            "依照本法第三章的规定处理。",
            "",
        ):
            with self.subTest(line=line):
                self.assertFalse(is_structure_heading(line))

    def test_reference_fragment_detection(self) -> None:
        genuine = make_article("第一条", "第一条　为了测试，制定本法。")
        no_separator_header = make_article(
            "第一百二十八条", "第一百二十八　条侦查人员应当进行勘验。"
        )
        fragment = make_article("第一条", "第一条的规定定罪处罚。", body="的规定定罪处罚。")
        clause_fragment = make_article(
            "第五十三条", "第五十三条第（二）项规定的情形。", body="第（二）项规定的情形。"
        )
        with_law = make_article(
            "第一条", "《中华人民共和国测试法》第一条规定，的确如此。", status="with_law"
        )
        self.assertFalse(is_reference_fragment(genuine))
        self.assertFalse(is_reference_fragment(no_separator_header))
        self.assertTrue(is_reference_fragment(fragment))
        self.assertTrue(is_reference_fragment(clause_fragment))
        self.assertFalse(is_reference_fragment(with_law))

    def test_audit_counts_collisions_truncation_and_glued_headings(self) -> None:
        articles = [
            make_article("第一条", "第一条　为了测试，制定本法。", line_no=1),
            make_article("第二条", "第二条　违反本法", line_no=2),
            make_article("第一条", "第一条的规定处理。", body="的规定处理。", line_no=2),
            make_article(
                "第三条", "第三条　本法自公布之日起施行。\n第二章　附则", line_no=3
            ),
        ]
        report = audit_article_integrity(articles)
        self.assertEqual(report["article_count"], 4)
        self.assertEqual(report["identity_collision_groups"], 1)
        self.assertEqual(report["identity_collision_articles"], 2)
        self.assertEqual(report["reference_fragment_count"], 1)
        self.assertEqual(report["unterminated_article_count"], 2)
        self.assertEqual(report["glued_heading_article_count"], 1)
        self.assertEqual(integrity_issue_count(report), 3)
        markdown = "\n".join(render_integrity_markdown(report))
        self.assertIn("同法同条号冲突: 1 组 / 2 条", markdown)
        self.assertIn("疑似引用碎片样例", markdown)

    def test_clean_corpus_has_no_issues(self) -> None:
        articles = [
            make_article("第一条", "第一条　为了测试，制定本法。"),
            make_article("第二条", "第二条　违反本法第一条的规定的，依照本法处理。"),
        ]
        report = audit_article_integrity(articles)
        self.assertEqual(integrity_issue_count(report), 0)
        self.assertEqual(report["unterminated_article_count"], 0)


if __name__ == "__main__":
    unittest.main()
