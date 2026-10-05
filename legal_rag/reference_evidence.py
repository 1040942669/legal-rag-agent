"""Mechanical candidate validity and reference ownership, never entailment."""

from __future__ import annotations

import math
from dataclasses import dataclass
from numbers import Real
from typing import Any, Sequence

from .legal_references import (
    LawArticleRequirement, ReferenceAnalysis, canonical_article_number, canonical_law_title,
)
from .models import SearchResult


REFERENCE_EVIDENCE_RULES_VERSION = "reference-evidence-v2"
REFERENCE_EVIDENCE_RULES_VERSIONS = frozenset({"reference-evidence-v1", REFERENCE_EVIDENCE_RULES_VERSION})


@dataclass(frozen=True, slots=True)
class MechanicalEvidenceCheck:
    candidate_available: bool
    scores_valid: bool
    scope_status: str
    reference_coverage_status: str
    missing_pairs: tuple[LawArticleRequirement, ...]
    missing_laws: tuple[str, ...]
    reasons: tuple[str, ...]
    checked_result_count: int
    analysis_fingerprint: str
    rules_version: str = REFERENCE_EVIDENCE_RULES_VERSION

    @property
    def sufficient(self) -> bool:
        return self.candidate_available and self.scores_valid and not self.reasons

    @property
    def semantic_support_status(self) -> str:
        return "not_checked"

    def to_dict(self) -> dict[str, Any]:
        return {"rules_version": self.rules_version,
                "candidate_available": self.candidate_available, "scores_valid": self.scores_valid,
                "scope_status": self.scope_status, "reference_coverage_status": self.reference_coverage_status,
                "missing_pairs": [item.to_dict() for item in self.missing_pairs],
                "missing_laws": list(self.missing_laws), "reasons": list(self.reasons),
                "checked_result_count": self.checked_result_count,
                "analysis_fingerprint": self.analysis_fingerprint,
                "sufficient": self.sufficient, "semantic_support_status": self.semantic_support_status}


def _canonical_laws(result: SearchResult) -> set[str]:
    try:
        if result.provenance is not None:
            return {canonical_law_title(entry.title) for entry in result.provenance.articles}
        return {canonical_law_title(law) for law in result.chunk.law_names}
    except (TypeError, ValueError):
        return set()


def _covers(result: SearchResult, requirement: LawArticleRequirement) -> bool:
    try:
        if result.provenance is not None:
            return any(canonical_law_title(entry.title) == requirement.law_title
                       and canonical_article_number(entry.article_number) == requirement.article_number
                       for entry in result.provenance.articles if entry.article_number)
        return _canonical_laws(result) == {requirement.law_title} and any(
            canonical_article_number(article) == requirement.article_number
            for article in result.chunk.article_numbers)
    except (TypeError, ValueError):
        return False


def _finite(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, Real):
        return False
    try:
        return math.isfinite(value)
    except (ValueError, TypeError, OverflowError):
        return False


def check_reference_evidence(
    analysis: ReferenceAnalysis, results: Sequence[SearchResult], *,
    snapshot_id: str | None = None, allowed_scope_ids: Sequence[str] | None = None,
    rules_version: str = REFERENCE_EVIDENCE_RULES_VERSION,
) -> MechanicalEvidenceCheck:
    if rules_version not in REFERENCE_EVIDENCE_RULES_VERSIONS:
        raise ValueError("unsupported mechanical evidence rules")
    if not isinstance(analysis, ReferenceAnalysis):
        raise ValueError("reference analysis must be typed")
    if isinstance(results, (str, bytes)) or any(not isinstance(result, SearchResult) for result in results):
        raise ValueError("evidence results must be typed")
    if allowed_scope_ids is not None and (isinstance(allowed_scope_ids, (str, bytes))
                                         or any(not isinstance(item, str) or not item for item in allowed_scope_ids)):
        raise ValueError("scope IDs must be a sequence of nonempty text")
    configured = snapshot_id is not None or allowed_scope_ids is not None
    permitted: list[SearchResult] = []
    for result in results:
        provenance = result.provenance
        snapshot = provenance.snapshot_id if provenance else result.chunk.metadata.get("snapshot_id")
        scope = provenance.scope_id if provenance else result.chunk.metadata.get("scope_id")
        consistent = not provenance or all(
            result.chunk.metadata.get(key, expected) == expected
            for key, expected in (("scope_id", scope), ("snapshot_id", snapshot)))
        if consistent and (snapshot_id is None or snapshot == snapshot_id) and (
            allowed_scope_ids is None or scope in allowed_scope_ids):
            permitted.append(result)
    scope_valid = len(permitted) == len(results)
    scope_status = "not_configured" if not configured else "valid" if scope_valid else "invalid"
    reasons: list[str] = []
    if not scope_valid:
        reasons.append("evidence_scope_invalid")
    if not permitted:
        reasons.append("no_retrieved_evidence")
    scores_valid = all(_finite(result.score) for result in results)
    if scores_valid and rules_version == REFERENCE_EVIDENCE_RULES_VERSION:
        # These backends only emit positive matches. Cosine similarities may
        # legitimately be zero/negative; positivity is not a universal cutoff.
        scores_valid = all(result.score > 0 for result in results
                           if result.retriever in {"bm25", "rrf", "hybrid", "exact_reference"})
    if not scores_valid:
        reasons.append("invalid_scores")
    elif rules_version == "reference-evidence-v1" and permitted and max(result.score for result in permitted) <= 0.01:
        reasons.append("low_scores")
    covered_laws = {law for result in permitted for law in _canonical_laws(result)}
    missing_laws = tuple(law for law in analysis.required_law_titles if law not in covered_laws)
    missing_pairs = tuple(item for item in analysis.requirements if not any(_covers(result, item) for result in permitted))
    reasons.extend("missing_law:" + law for law in missing_laws)
    reasons.extend("missing_reference_pair:" + item.law_title + ":" + item.article_number for item in missing_pairs)
    if analysis.unresolved:
        reasons.extend("unresolved_reference:" + item.reason for item in analysis.unresolved)
        coverage = "unresolved"
    elif missing_pairs or missing_laws:
        coverage = "missing"
    elif analysis.requirements or analysis.required_law_titles:
        coverage = "complete"
    else:
        coverage = "not_requested"
    return MechanicalEvidenceCheck(bool(permitted), scores_valid, scope_status, coverage,
                                    missing_pairs, missing_laws, tuple(dict.fromkeys(reasons)),
                                    len(permitted), analysis.fingerprint, rules_version)
