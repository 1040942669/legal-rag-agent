"""Generate deterministic citation-lookup evaluation cases from the corpus.

Each sampled article is asked three ways: full title in 书名号, short title
without 中华人民共和国, and short title with an Arabic article number.  The v3
set only uses Chinese numerals and mostly full titles, so it cannot catch a
retriever that fails on ``刑法第264条``.
"""

from __future__ import annotations

import json
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Sequence

from .article_numbers import int_to_chinese_number, parse_article_label
from .data import stable_id
from .models import LawArticle


PRC_PREFIX = "中华人民共和国"
QUESTION_SUFFIX = "规定了什么？"
CITATION_CASE_TYPES = ("citation_full", "citation_short", "citation_arabic")


def short_law_name(law_name: str) -> str | None:
    if not law_name.startswith(PRC_PREFIX):
        return None
    short = law_name.removeprefix(PRC_PREFIX).strip()
    return short if len(short) >= 2 else None


def select_citation_targets(
    articles: Iterable[LawArticle], *, per_law: int = 1, seed: int = 20261021
) -> list[LawArticle]:
    """Pick ``per_law`` articles per law whose (law, number) identity is unique."""

    if per_law < 1:
        raise ValueError("per_law must be at least 1")
    numbered = [article for article in articles if article.article_number]
    identity_counts = Counter(
        (article.law_name, article.article_number) for article in numbered
    )
    by_law: dict[str, list[LawArticle]] = defaultdict(list)
    for article in numbered:
        if identity_counts[(article.law_name, article.article_number)] == 1:
            by_law[article.law_name].append(article)
    selected: list[LawArticle] = []
    for law_name in sorted(by_law):
        candidates = sorted(
            by_law[law_name], key=lambda item: parse_article_label(item.article_number)
        )
        # Seed per law so adding a law to the corpus does not reshuffle others.
        rng = random.Random(f"{seed}:{law_name}")
        picked = rng.sample(candidates, min(per_law, len(candidates)))
        selected.extend(
            sorted(picked, key=lambda item: parse_article_label(item.article_number))
        )
    return selected


def build_citation_cases(targets: Sequence[LawArticle]) -> list[dict]:
    cases: list[dict] = []
    for article in targets:
        law = article.law_name
        number = article.article_number
        short = short_law_name(law)
        full_title = law if "《" in law else f"《{law}》"
        base, suffix = parse_article_label(number)
        arabic = f"第{base}条" + (f"之{int_to_chinese_number(suffix)}" if suffix else "")
        variants = [("citation_full", f"{full_title}{number}")]
        if short:
            variants.append(("citation_short", f"{short}{number}"))
        variants.append(("citation_arabic", f"{short or law}{arabic}"))
        base_id = stable_id(law, number)[:12]
        for case_type, prefix in variants:
            cases.append(
                {
                    "id": f"cite_{base_id}_{case_type.removeprefix('citation_')}",
                    "type": case_type,
                    "question": f"{prefix}{QUESTION_SUFFIX}",
                    "expected_law": law,
                    "expected_articles": [number],
                    "keywords": [],
                    "expected_behavior": "evidence_answer",
                }
            )
    return cases


def write_citation_cases(cases: Sequence[dict], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False) + "\n")
    return path
