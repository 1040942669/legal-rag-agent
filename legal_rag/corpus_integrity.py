"""Corpus integrity checks that retrieval metrics cannot see on their own.

Hit@k only checks law name and article number.  If the parser splits a
cross-reference such as ``依照本法第二百六十四条的规定`` into a fake article,
the fragment still "hits" and the referencing article silently loses its tail.
These checks make such damage visible before any retriever is evaluated.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from .data import ARTICLE_NUMBER, is_structure_heading
from .models import LawArticle


ARTICLE_HEADER_PREFIX_RE = re.compile(rf"^[#*>\s]*{ARTICLE_NUMBER}")
# Text that follows an article number inside a sentence, never a real header.
REFERENCE_CONTINUATION_PREFIXES = (
    "的",
    "规定",
    "第",
    "、",
    "，",
    ",",
    "至",
    "和",
    "或者",
    "以及",
    "所",
    "款",
    "项",
    "（",
    "(",
)
TERMINAL_PUNCTUATION = ("。", "；", "！", "？", "”", "’", "」", "』", "）", ")", "》", "…", ";", "!", "?", ".")


def audit_article_integrity(
    articles: Iterable[LawArticle], *, example_limit: int = 10
) -> dict[str, Any]:
    """Summarize parser damage in a loaded article list."""

    items = list(articles)
    groups: dict[tuple[str, str], list[LawArticle]] = defaultdict(list)
    fragments: list[LawArticle] = []
    unterminated: list[LawArticle] = []
    glued_headings: list[LawArticle] = []
    for article in items:
        if article.article_number:
            groups[(article.law_name, article.article_number)].append(article)
            if article.raw_text.rstrip() and not article.raw_text.rstrip().endswith(
                TERMINAL_PUNCTUATION
            ):
                unterminated.append(article)
        if is_reference_fragment(article):
            fragments.append(article)
        if any(is_structure_heading(line) for line in article.raw_text.splitlines()[1:]):
            glued_headings.append(article)

    collisions = {key: group for key, group in groups.items() if len(group) > 1}
    return {
        "article_count": len(items),
        "numbered_article_count": sum(1 for item in items if item.article_number),
        "identity_collision_groups": len(collisions),
        "identity_collision_articles": sum(len(group) for group in collisions.values()),
        "reference_fragment_count": len(fragments),
        "unterminated_article_count": len(unterminated),
        "glued_heading_article_count": len(glued_headings),
        "examples": {
            "identity_collisions": [
                {
                    "law_name": law_name,
                    "article_number": article_number,
                    "texts": [_preview(item.raw_text) for item in group[:4]],
                }
                for (law_name, article_number), group in list(collisions.items())[
                    :example_limit
                ]
            ],
            "reference_fragments": [_describe(item) for item in fragments[:example_limit]],
            "unterminated_articles": [
                _describe(item, tail=True) for item in unterminated[:example_limit]
            ],
            "glued_headings": [
                _describe(item, tail=True) for item in glued_headings[:example_limit]
            ],
        },
    }


def is_reference_fragment(article: LawArticle) -> bool:
    """Detect a fake article cut out of an in-body cross-reference."""

    if article.parse_status != "from_filename" or not article.article_number:
        return False
    header = ARTICLE_HEADER_PREFIX_RE.match(article.raw_text)
    if header is None:
        return False
    # A genuine header is separated from its body (第一条　为了...).
    rest = article.raw_text[header.end() :]
    return bool(rest) and not rest[0].isspace() and rest.startswith(
        REFERENCE_CONTINUATION_PREFIXES
    )


def integrity_issue_count(report: dict[str, Any]) -> int:
    return (
        int(report["identity_collision_groups"])
        + int(report["reference_fragment_count"])
        + int(report["glued_heading_article_count"])
    )


def render_integrity_markdown(report: dict[str, Any]) -> list[str]:
    lines = [
        "## 语料完整性",
        f"- 条文记录数: {report['article_count']}（有条号 {report['numbered_article_count']}）",
        f"- 同法同条号冲突: {report['identity_collision_groups']} 组 / "
        f"{report['identity_collision_articles']} 条",
        f"- 疑似引用碎片: {report['reference_fragment_count']}",
        f"- 未以终止标点结尾的条文: {report['unterminated_article_count']}",
        f"- 正文粘连章节标题: {report['glued_heading_article_count']}",
    ]
    examples = report.get("examples", {})
    for key, title in (
        ("reference_fragments", "疑似引用碎片样例"),
        ("unterminated_articles", "未终止条文样例"),
        ("glued_headings", "粘连标题样例"),
    ):
        rows = examples.get(key) or []
        if not rows:
            continue
        lines.append(f"- {title}:")
        lines.extend(
            f"  - {row['source_file']}:{row['line_no']} {row['law_name']} "
            f"{row['article_number']} `{row['text']}`"
            for row in rows
        )
    return lines


def _describe(article: LawArticle, *, tail: bool = False) -> dict[str, Any]:
    return {
        "law_name": article.law_name,
        "article_number": article.article_number,
        "source_file": Path(article.source_file).name,
        "line_no": article.line_no,
        "text": _preview(article.raw_text, tail=tail),
    }


def _preview(text: str, *, tail: bool = False, limit: int = 60) -> str:
    flat = text.replace("\n", " / ")
    if len(flat) <= limit:
        return flat
    return "..." + flat[-limit:] if tail else flat[:limit] + "..."
