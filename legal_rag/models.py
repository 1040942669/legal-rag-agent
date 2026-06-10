from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class LawArticle:
    article_id: str
    law_name: str
    article_number: str
    body: str
    raw_text: str
    source_file: str
    line_no: int
    parse_status: str


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    text: str
    law_names: list[str]
    article_numbers: list[str]
    source_files: list[str]
    line_nos: list[int]
    strategy: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SearchResult:
    chunk: Chunk
    score: float
    rank: int
    retriever: str
    trace: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    question: str
    case_type: str
    expected_law: str
    expected_articles: list[str]
    keywords: list[str]


@dataclass
class EvalRecord:
    case_id: str
    case_type: str
    model: str
    retriever: str
    chunk_strategy: str
    hit_at_3: int
    hit_at_5: int
    mrr: float
    target_coverage: float
    keyword_coverage: float
    citation_hit: int
    latency_ms: int
    answer: str
    sources: str
    error: str = ""
    failure_label: str = ""
    failure_reason: str = ""
