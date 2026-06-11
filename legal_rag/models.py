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
class NormalizedQuery:
    original_query: str
    legal_questions: list[str]
    missing_facts: list[str]
    law_hints: list[str]
    article_hints: list[str]
    keywords: list[str]
    risk_flags: list[str]
    confidence: float
    source: str = "rules"
    errors: list[str] = field(default_factory=list)
    raw_response: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_query": self.original_query,
            "legal_questions": self.legal_questions,
            "missing_facts": self.missing_facts,
            "law_hints": self.law_hints,
            "article_hints": self.article_hints,
            "keywords": self.keywords,
            "risk_flags": self.risk_flags,
            "confidence": self.confidence,
            "source": self.source,
            "errors": self.errors,
            "raw_response": self.raw_response,
        }


@dataclass(frozen=True)
class RetrievalPlan:
    plan_id: str
    query: str
    law_hints: list[str]
    article_hints: list[str]
    keywords: list[str]
    top_k: int
    rationale: str
    source_question_index: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "query": self.query,
            "law_hints": self.law_hints,
            "article_hints": self.article_hints,
            "keywords": self.keywords,
            "top_k": self.top_k,
            "rationale": self.rationale,
            "source_question_index": self.source_question_index,
        }


@dataclass(frozen=True)
class EvidenceCheck:
    sufficient: bool
    missing_facts: list[str]
    missing_law_support: list[str]
    low_coverage: list[str]
    followup_queries: list[str]
    stop_reason: str
    checked_result_count: int
    covered_laws: list[str] = field(default_factory=list)
    covered_articles: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sufficient": self.sufficient,
            "missing_facts": self.missing_facts,
            "missing_law_support": self.missing_law_support,
            "low_coverage": self.low_coverage,
            "followup_queries": self.followup_queries,
            "stop_reason": self.stop_reason,
            "checked_result_count": self.checked_result_count,
            "covered_laws": self.covered_laws,
            "covered_articles": self.covered_articles,
        }


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    citation_valid: bool
    missing_citations: list[str]
    unsupported_claims: list[str]
    disclaimer_present: bool
    refusal_correct: bool
    failure_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "citation_valid": self.citation_valid,
            "missing_citations": self.missing_citations,
            "unsupported_claims": self.unsupported_claims,
            "disclaimer_present": self.disclaimer_present,
            "refusal_correct": self.refusal_correct,
            "failure_reasons": self.failure_reasons,
        }


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
    sufficiency_pass: int
    citation_valid: int
    verifier_pass: int
    refusal_correctness: int
    latency_ms: int
    answer: str
    sources: str
    error: str = ""
    failure_label: str = ""
    failure_reason: str = ""
