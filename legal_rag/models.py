from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


ANSWER_MODES = frozenset(
    {
        "evidence_answer",
        "insufficient_evidence",
        "needs_clarification",
        "out_of_scope",
    }
)
SEMANTIC_SUPPORT_STATUSES = frozenset(
    {"supported", "unsupported", "uncertain", "not_checked"}
)
EVALUATION_METRICS_SCHEMA_VERSION = 2


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
class AnswerClaim:
    claim_id: str
    text: str
    source_ids: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "text": self.text,
            "source_ids": self.source_ids,
        }


@dataclass(frozen=True)
class StructuredAnswer:
    """Canonical answer envelope used by the M1 verifier.

    ``schema_valid`` and ``adapter_source`` describe how confidently the
    envelope was obtained.  They are intentionally omitted from ``to_dict``
    so the public generation contract remains the five fields documented in
    the refactor plan.
    """

    answer_text: str
    answer_mode: str
    claims: list[AnswerClaim]
    limitations: list[str]
    clarification_question: str | None
    schema_valid: bool = True
    adapter_source: str = "structured_object"
    parse_errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer_text": self.answer_text,
            "answer_mode": self.answer_mode,
            "claims": [claim.to_dict() for claim in self.claims],
            "limitations": self.limitations,
            "clarification_question": self.clarification_question,
        }


@dataclass(frozen=True)
class VerificationContext:
    """Evidence boundary for one verification run.

    When either boundary is configured, cited evidence must carry the
    corresponding metadata.  A missing value is rejected instead of being
    silently treated as in-scope.
    """

    snapshot_id: str | None = None
    allowed_scope_ids: list[str] | None = None


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    schema_valid: bool
    evidence_catalog_valid: bool
    citation_ids_valid: bool
    citation_alignment_valid: bool
    evidence_scope_valid: bool | None
    disclaimer_present: bool
    response_mode_valid: bool
    semantic_support_status: str
    expected_answer_mode: str | None
    actual_answer_mode: str
    refusal_required: bool
    refusal_present: bool
    answer_source_format: str
    schema_errors: list[str]
    required_checks: list[str]
    duplicate_source_ids: list[str]
    missing_source_ids: list[str]
    malformed_citation_tokens: list[str]
    invalid_scope_citations: list[str]
    cited_source_ids: list[str]
    visible_source_ids: list[str]
    claim_source_ids: list[str]
    unsupported_claims: list[str]
    refusal_correct: bool | None
    failure_reasons: list[str] = field(default_factory=list)

    @property
    def citation_valid(self) -> bool:
        """Compatibility alias for pre-M1 callers and legacy CSV output."""

        return self.citation_ids_valid and self.evidence_scope_valid is not False

    @property
    def missing_citations(self) -> list[str]:
        """Legacy rank-only view used by the pre-M1 verifier tests."""

        return [source_id.removeprefix("S") for source_id in self.missing_source_ids]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "schema_valid": self.schema_valid,
            "evidence_catalog_valid": self.evidence_catalog_valid,
            "citation_ids_valid": self.citation_ids_valid,
            "citation_alignment_valid": self.citation_alignment_valid,
            "evidence_scope_valid": self.evidence_scope_valid,
            "citation_valid": self.citation_valid,
            "disclaimer_present": self.disclaimer_present,
            "response_mode_valid": self.response_mode_valid,
            "semantic_support_status": self.semantic_support_status,
            "expected_answer_mode": self.expected_answer_mode,
            "actual_answer_mode": self.actual_answer_mode,
            "refusal_required": self.refusal_required,
            "refusal_present": self.refusal_present,
            "answer_source_format": self.answer_source_format,
            "schema_errors": self.schema_errors,
            "required_checks": self.required_checks,
            "duplicate_source_ids": self.duplicate_source_ids,
            "missing_source_ids": self.missing_source_ids,
            "malformed_citation_tokens": self.malformed_citation_tokens,
            "missing_citations": self.missing_citations,
            "invalid_scope_citations": self.invalid_scope_citations,
            "cited_source_ids": self.cited_source_ids,
            "visible_source_ids": self.visible_source_ids,
            "claim_source_ids": self.claim_source_ids,
            "unsupported_claims": self.unsupported_claims,
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
    expected_behavior: str = ""
    session_group: str | None = None
    turn_index: int = 0
    schema_version: int = 1

    @property
    def resolved_expected_behavior(self) -> str:
        """Return the explicit behavior target, or the legacy case mapping."""

        if self.expected_behavior:
            return self.expected_behavior
        if self.case_type == "refusal":
            return "out_of_scope"
        return "evidence_answer"


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
    judge_faithfulness: float = -1.0
    judge_relevance: float = -1.0
    judge_completeness: float = -1.0
    judge_pass: int = -1
    judge_comment: str = ""
    judge_error: str = ""
    assistant_llm_calls: int = 0
    normalizer_llm_calls: int = 0
    judge_llm_calls: int = 0
    llm_failed_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    token_usage_calls: int = 0
    llm_latency_ms: float = 0.0
    # Manually constructed/pre-M1 records remain legacy until evaluate()
    # explicitly populates the v2 canonical metrics.
    metrics_schema_version: int = 1
    expected_behavior: str = ""
    observed_answer_mode: str | None = None
    execution: dict[str, Any] = field(default_factory=dict)
    canonical_metrics: dict[str, dict[str, Any]] = field(default_factory=dict)
    generation_attempt: dict[str, Any] = field(default_factory=dict)

    def to_canonical_dict(self) -> dict[str, Any]:
        """Return the unambiguous v2 evaluation payload.

        Legacy scalar fields remain on the dataclass for existing CSV readers.
        New consumers should use this representation, where unavailable values
        are always ``null`` together with an explicit reason.
        """

        return {
            "metrics_schema_version": self.metrics_schema_version,
            "case_id": self.case_id,
            "case_type": self.case_type,
            "expected_behavior": self.expected_behavior,
            "observed_answer_mode": self.observed_answer_mode,
            "execution": self.execution,
            "metrics": self.canonical_metrics,
            "generation_attempt": self.generation_attempt,
        }
