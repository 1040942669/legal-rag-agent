"""One parser for the law names and article numbers a query cites.

Law names resolve by exact alias equality against corpus titles: the full
title, the title without the 中华人民共和国 prefix, and the title without a
trailing year such as ``(2022)``.  Overlapping matches keep the longest alias,
so 保险法 inside 社会保险法 never becomes a second law.  Article numbers are
turned into integers by :mod:`legal_rag.article_numbers`, so 第264条 and
第二百六十四条 are the same reference.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from .article_numbers import (
    MAX_ARTICLE_NUMBER,
    canonical_article_label,
    chinese_number_to_int,
    format_article_label,
)
from .models import Chunk


PRC_PREFIX = "中华人民共和国"
MIN_ALIAS_CHARS = 2
_NUMERAL_CHARS = "0-9零〇一二两三四五六七八九十百千壹贰叁肆伍陆柒捌玖拾佰仟"
# Queries are NFKC-normalized first, so full-width ，～－ arrive as ASCII.
_LIST_SEPARATOR = r"(?:、|,|和|及|与|或|至|到|-|~)"
ARTICLE_REFERENCE_RE = re.compile(
    rf"第\s*(?P<numbers>[{_NUMERAL_CHARS}]+"
    rf"(?:\s*{_LIST_SEPARATOR}\s*第?\s*[{_NUMERAL_CHARS}]+)*)"
    rf"\s*条(?:之(?P<suffix>[{_NUMERAL_CHARS}]+))?"
)
_NUMERAL_RUN_RE = re.compile(rf"[{_NUMERAL_CHARS}]+")
_TRAILING_YEAR_RE = re.compile(r"\(\d{4}\)$")
SENTENCE_BREAK_CHARS = frozenset("。;!?\n")


@dataclass(frozen=True)
class LawMention:
    law_name: str
    alias: str
    start: int
    end: int


@dataclass(frozen=True)
class ArticleReference:
    label: str
    number: int
    start: int
    end: int
    law_name: str | None


@dataclass(frozen=True)
class ParsedQuery:
    """Law and article references found in the NFKC-normalized query text."""

    text: str
    laws: tuple[LawMention, ...]
    articles: tuple[ArticleReference, ...]
    unresolved_titles: tuple[str, ...]

    @property
    def law_names(self) -> list[str]:
        return list(dict.fromkeys(mention.law_name for mention in self.laws))

    @property
    def article_labels(self) -> list[str]:
        return list(dict.fromkeys(article.label for article in self.articles))

    @property
    def citations(self) -> list[tuple[str, str]]:
        """(law name, article label) pairs whose owning law is known."""

        return list(
            dict.fromkeys(
                (article.law_name, article.label)
                for article in self.articles
                if article.law_name is not None
            )
        )

    def rewritten_text(self) -> str:
        """Query text with every article reference spelled the corpus way."""

        labels_by_span: dict[tuple[int, int], list[str]] = defaultdict(list)
        for article in self.articles:
            labels_by_span[(article.start, article.end)].append(article.label)
        parts: list[str] = []
        cursor = 0
        for (start, end), labels in sorted(labels_by_span.items()):
            parts.append(self.text[cursor:start])
            parts.append(" ".join(labels))
            cursor = end
        parts.append(self.text[cursor:])
        return "".join(parts)


def normalize_query_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def normalize_law_title(title: str) -> str:
    """NFKC spelling without whitespace, shared by corpus titles and queries."""

    return "".join(normalize_query_text(title).split())


def law_title_aliases(law_name: str) -> list[str]:
    """Full title first, then the shorter spellings users write."""

    full = normalize_law_title(law_name)
    spellings = [full]
    without_year = _TRAILING_YEAR_RE.sub("", full)
    if without_year != full:
        spellings.append(without_year)
    # A nested title is often typed with 〈〉 inside the outer brackets.
    spellings.extend(
        spelling.replace("《", "〈").replace("》", "〉")
        for spelling in list(spellings)
        if "《" in spelling
    )
    spellings.extend(
        spelling.removeprefix(PRC_PREFIX)
        for spelling in list(spellings)
        if spelling.startswith(PRC_PREFIX)
    )
    return [
        spelling
        for spelling in dict.fromkeys(spellings)
        if len(spelling) >= MIN_ALIAS_CHARS
    ]


def chunk_citation_keys(chunk: Chunk) -> frozenset[tuple[str, str]]:
    """(law name, article label) pairs a chunk answers exactly.

    Chunks that mix laws are skipped: their law and article lists do not say
    which article belongs to which law.
    """

    if len(chunk.law_names) != 1:
        return frozenset()
    law_name = chunk.law_names[0]
    return frozenset(
        (law_name, canonical_article_label(label)) for label in chunk.article_numbers
    )


class LawAliasIndex:
    """Exact alias -> corpus law name lookup."""

    def __init__(self, law_names: Iterable[str]) -> None:
        names = list(dict.fromkeys(name for name in law_names if name.strip()))
        aliases: dict[str, str] = {}
        derived: dict[str, set[str]] = defaultdict(set)
        for name in names:
            spellings = law_title_aliases(name)
            if not spellings:
                # A one-character title would match almost any query.
                continue
            full, *shorter = spellings
            aliases.setdefault(full, name)
            for alias in shorter:
                derived[alias].add(name)
        ambiguous: set[str] = set()
        for alias, owners in derived.items():
            if alias in aliases:
                # A full title always owns its own spelling.
                continue
            if len(owners) == 1:
                aliases[alias] = next(iter(owners))
            else:
                ambiguous.add(alias)
        self._aliases = aliases
        self.ambiguous_aliases = frozenset(ambiguous)
        by_first_char: dict[str, list[str]] = defaultdict(list)
        for alias in aliases:
            by_first_char[alias[0]].append(alias)
        self._by_first_char = {
            char: sorted(group, key=len, reverse=True)
            for char, group in by_first_char.items()
        }

    @classmethod
    def from_chunks(cls, chunks: Iterable[Chunk]) -> "LawAliasIndex":
        return cls(law for chunk in chunks for law in chunk.law_names)

    def __len__(self) -> int:
        return len(self._aliases)

    def resolve(self, title: str) -> str | None:
        return self._aliases.get(normalize_law_title(title))

    def find_mentions(self, text: str) -> list[LawMention]:
        """Non-overlapping law mentions in ``text``, longest alias first."""

        candidates = [
            LawMention(self._aliases[alias], alias, start, start + len(alias))
            for start, char in enumerate(text)
            for alias in self._by_first_char.get(char, ())
            if text.startswith(alias, start)
        ]
        # Inside 《...》 only the whole title names a law: 《劳动合同法实施条例》
        # is not 劳动合同法.
        titles = title_spans(text)
        candidates = [
            mention
            for mention in candidates
            if not any(
                start <= mention.start
                and mention.end <= end
                and (mention.start, mention.end) != (start, end)
                for start, end in titles
            )
        ]
        candidates.sort(key=lambda mention: (mention.start - mention.end, mention.start))
        accepted: list[LawMention] = []
        for mention in candidates:
            if all(
                mention.end <= kept.start or mention.start >= kept.end
                for kept in accepted
            ):
                accepted.append(mention)
        return sorted(accepted, key=lambda mention: mention.start)


def title_spans(text: str) -> list[tuple[int, int]]:
    """Content spans of every balanced 《...》 pair, nested pairs included."""

    spans: list[tuple[int, int]] = []
    opened: list[int] = []
    for index, char in enumerate(text):
        if char == "《":
            opened.append(index + 1)
        elif char == "》" and opened:
            spans.append((opened.pop(), index))
    return spans


def parse_query(text: str, aliases: LawAliasIndex | None = None) -> ParsedQuery:
    normalized = normalize_query_text(text)
    mentions = aliases.find_mentions(normalized) if aliases is not None else []
    distinct_laws = {mention.law_name for mention in mentions}
    sole_law = next(iter(distinct_laws)) if len(distinct_laws) == 1 else None
    articles: list[ArticleReference] = []
    for match in ARTICLE_REFERENCE_RE.finditer(normalized):
        numbers = [
            number
            for number in map(_article_numeral, _NUMERAL_RUN_RE.findall(match["numbers"]))
            if number is not None
        ]
        if not numbers:
            continue
        suffix = 0
        if match["suffix"]:
            suffix = _article_numeral(match["suffix"]) or 0
            if not suffix:
                continue
        owner = _owning_law(normalized, match.start(), mentions) or sole_law
        for position, number in enumerate(numbers):
            # 第十七、十八条之一: the suffix belongs to the last number only.
            last = position == len(numbers) - 1
            label = format_article_label(number, suffix if last else 0)
            articles.append(
                ArticleReference(label, number, match.start(), match.end(), owner)
            )
    covered = [(mention.start, mention.end) for mention in mentions]
    unresolved = [
        normalized[start:end]
        for start, end in title_spans(normalized)
        if not any(start <= low and high <= end for low, high in covered)
        and not any(low <= start and end <= high for low, high in covered)
    ]
    return ParsedQuery(
        text=normalized,
        laws=tuple(mentions),
        articles=tuple(articles),
        unresolved_titles=tuple(dict.fromkeys(unresolved)),
    )


def _article_numeral(text: str) -> int | None:
    try:
        number = chinese_number_to_int(text)
    except ValueError:
        return None
    return number if 1 <= number <= MAX_ARTICLE_NUMBER else None


def _owning_law(text: str, position: int, mentions: list[LawMention]) -> str | None:
    """Nearest law named earlier in the same sentence."""

    owner: LawMention | None = None
    for mention in mentions:
        if mention.end > position:
            break
        owner = mention
    if owner is None or any(char in SENTENCE_BREAK_CHARS for char in text[owner.end : position]):
        return None
    return owner.law_name
