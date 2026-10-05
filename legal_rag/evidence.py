from __future__ import annotations

import math
from dataclasses import replace
from numbers import Real
from typing import Iterable, Sequence

from .legal_references import MAX_REFERENCE_QUERY_CHARS, parse_legal_references
from .models import EvidenceCheck, NormalizedQuery, RetrievalPlan, SearchResult
from .query import QueryAnalysis, analyze_query, extract_law_names
from .reference_evidence import check_reference_evidence


LOW_SCORE_THRESHOLD = 0.01
GENERAL_EVIDENCE_RULES_VERSION = "general-reference-v2"
HISTORICAL_EVIDENCE_RULES_VERSION = "legacy-hints-and-return-v1"
EVIDENCE_RULES_VERSIONS = frozenset({GENERAL_EVIDENCE_RULES_VERSION, HISTORICAL_EVIDENCE_RULES_VERSION})


def check_evidence_sufficiency(
    query: str,
    results: list[SearchResult],
    *,
    analysis: QueryAnalysis | None = None,
    normalized_query: NormalizedQuery | None = None,
    plans: list[RetrievalPlan] | None = None,
    min_results: int = 1,
    max_followup_queries: int = 2,
    rules_version: str = GENERAL_EVIDENCE_RULES_VERSION,
    known_law_titles: Iterable[str] = (),
    snapshot_id: str | None = None,
    allowed_scope_ids: Sequence[str] | None = None,
) -> EvidenceCheck:
    """Candidate/reference mechanics, never a semantic-support certificate.

    Requirements come from the original question, not inferred normalizer or
    planner hints. The legacy rules are explicit historical replay only.
    Callers still own provenance validation and authorized candidate filtering.
    """
    if rules_version not in EVIDENCE_RULES_VERSIONS:
        raise ValueError("unsupported evidence rules version")
    if rules_version == HISTORICAL_EVIDENCE_RULES_VERSION:
        return replace(_check_historical_evidence_sufficiency(
            query, results, analysis=analysis, normalized_query=normalized_query,
            plans=plans, min_results=min_results, max_followup_queries=max_followup_queries),
            rules_version=HISTORICAL_EVIDENCE_RULES_VERSION, mechanical_check=None)
    if not isinstance(query, str):
        raise ValueError("evidence query must be text")
    covered_laws = unique(law for result in results for law in result.chunk.law_names if law)
    covered_articles = unique(article for result in results for article in result.chunk.article_numbers if article)
    missing_facts = unique(normalized_query.missing_facts if normalized_query else [])
    if len(query) > MAX_REFERENCE_QUERY_CHARS:
        return _unparsed_evidence_check(results, missing_facts, covered_laws, covered_articles,
                                        "reference_query_limit_exceeded")
    # Only literal occurrences in the original query may use these title aliases.
    # The supplied QueryAnalysis is intentionally not trusted for requirements.
    try:
        reference_analysis = parse_legal_references(query, known_law_titles=(
            *known_law_titles, *covered_laws, *extract_law_names(query)))
    except (TypeError, ValueError):
        return _unparsed_evidence_check(results, missing_facts, covered_laws, covered_articles,
                                        "reference_parse_invalid")
    mechanical = check_reference_evidence(reference_analysis, results,
                                           snapshot_id=snapshot_id, allowed_scope_ids=allowed_scope_ids)
    missing_law_support = [reason for reason in mechanical.reasons if reason.startswith((
        "missing_law:", "missing_reference_pair:", "unresolved_reference:"))
        or reason == "no_retrieved_evidence"]
    # Keep the legacy public summary key, alongside the precise owned-pair key.
    missing_law_support.extend("missing_article:" + pair.article_number for pair in mechanical.missing_pairs)
    low_coverage = [reason for reason in mechanical.reasons if reason in {
        "invalid_scores", "low_scores", "evidence_scope_invalid"}]
    if mechanical.checked_result_count < min_results:
        low_coverage.append("too_few_results")
    if normalized_query and len(normalized_query.legal_questions) > mechanical.checked_result_count \
            and mechanical.checked_result_count < min_results:
        low_coverage.append("not_enough_results_for_legal_questions")
    sufficient = mechanical.sufficient and not missing_facts and not low_coverage
    clarify = bool(missing_facts or reference_analysis.unresolved)
    # A follow-up cannot resolve ambiguous ownership by silently guessing it.
    followup_queries = [] if clarify else unique(
        [f"《{pair.law_title}》{pair.article_number} {query}" for pair in mechanical.missing_pairs]
        + [f"《{law}》 {query}" for law in mechanical.missing_laws]
        + ([query] if not mechanical.candidate_available else []))[:max_followup_queries]
    return EvidenceCheck(
        sufficient=sufficient, missing_facts=missing_facts,
        missing_law_support=unique(missing_law_support), low_coverage=unique(low_coverage),
        followup_queries=followup_queries,
        stop_reason="needs_clarification" if clarify else "sufficient" if sufficient else "needs_followup",
        checked_result_count=mechanical.checked_result_count,
        covered_laws=covered_laws, covered_articles=covered_articles,
        rules_version=GENERAL_EVIDENCE_RULES_VERSION, mechanical_check=mechanical.to_dict())


def _unparsed_evidence_check(results, missing_facts, covered_laws, covered_articles, reason):
    # Unknown parsing stays unknown; do not fabricate an analysis fingerprint.
    return EvidenceCheck(False, missing_facts, [], [reason], [], "needs_clarification",
                         len(results), covered_laws, covered_articles,
                         GENERAL_EVIDENCE_RULES_VERSION, None)


def _check_historical_evidence_sufficiency(
    query: str,
    results: list[SearchResult],
    *,
    analysis: QueryAnalysis | None = None,
    normalized_query: NormalizedQuery | None = None,
    plans: list[RetrievalPlan] | None = None,
    min_results: int = 1,
    max_followup_queries: int = 2,
) -> EvidenceCheck:
    if analysis is None:
        analysis = analyze_query(query)
    missing_facts: list[str] = []
    missing_law_support: list[str] = []
    low_coverage: list[str] = []

    if not results:
        missing_law_support.append("no_retrieved_evidence")

    covered_laws = unique(
        law
        for result in results
        for law in result.chunk.law_names
        if law
    )
    covered_articles = unique(
        article
        for result in results
        for article in result.chunk.article_numbers
        if article
    )

    required_laws = required_law_hints(analysis, normalized_query, plans)
    required_articles = required_article_hints(analysis, normalized_query, plans)
    canonical_covered_laws = {_canonical_law_name(law) for law in covered_laws}
    for law in required_laws:
        if not _canonical_law_name(law) or _canonical_law_name(law) not in canonical_covered_laws:
            missing_law_support.append(f"missing_law:{law}")
    for article in required_articles:
        if article not in covered_articles:
            missing_law_support.append(f"missing_article:{article}")

    # Only an explicit, unambiguous single-law query establishes article ownership.
    # The union of unrelated laws and article numbers cannot prove that pair.
    explicit_laws = unique(_canonical_law_name(law) for law in analysis.law_names)
    if len(explicit_laws) == 1:
        for article in analysis.article_numbers:
            if not any(_covers_law_article(result, explicit_laws[0], article) for result in results):
                missing_law_support.append(f"missing_article:{article}")

    if normalized_query:
        missing_facts.extend(normalized_query.missing_facts)
        if len(normalized_query.legal_questions) > len(results) and len(results) < min_results:
            low_coverage.append("not_enough_results_for_legal_questions")

    if len(results) < min_results:
        low_coverage.append("too_few_results")
    if any(not _finite_score(result.score) for result in results):
        low_coverage.append("invalid_scores")
    elif results and max(result.score for result in results) <= LOW_SCORE_THRESHOLD:
        low_coverage.append("low_scores")
    if _missing_goods_return_anchor(query, results):
        low_coverage.append("missing_goods_return_anchor")

    followup_queries = build_followup_queries(
        query,
        missing_law_support=missing_law_support,
        max_queries=max_followup_queries,
    )
    sufficient = not missing_law_support and not missing_facts and not low_coverage and bool(results)
    if missing_facts:
        stop_reason = "needs_clarification"
    elif sufficient:
        stop_reason = "sufficient"
    else:
        stop_reason = "needs_followup"
    return EvidenceCheck(
        sufficient=sufficient,
        missing_facts=unique(missing_facts),
        missing_law_support=unique(missing_law_support),
        low_coverage=unique(low_coverage),
        followup_queries=followup_queries,
        stop_reason=stop_reason,
        checked_result_count=len(results),
        covered_laws=covered_laws,
        covered_articles=covered_articles,
    )


def _canonical_law_name(value: str) -> str:
    """Normalize only spelling wrappers, never related or inferred law names."""
    name = "".join(value.split())
    if name.startswith("《") and name.endswith("》"):
        name = name[1:-1]
    return name.removeprefix("中华人民共和国")


def _covers_law_article(result: SearchResult, law: str, article: str) -> bool:
    if result.provenance is not None:
        return any(
            _canonical_law_name(entry.title) == law and entry.article_number == article
            for entry in result.provenance.articles
        )
    # Without typed per-article provenance, a mixed-law chunk is ambiguous.
    chunk_laws = {_canonical_law_name(name) for name in result.chunk.law_names if name}
    return chunk_laws == {law} and article in result.chunk.article_numbers


def _finite_score(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, Real):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return False


def _missing_goods_return_anchor(query: str, results: list[SearchResult]) -> bool:
    """Candidate-only lexical necessity, not a semantic or eligibility verdict."""
    if not results or not all(
        result.retriever == "bm25"
        and result.trace.get("lexical_profile") == "local-lexical-v2"
        for result in results
    ):
        return False
    from .retrieval import lexical_expansion_terms

    if "退货" not in lexical_expansion_terms(query):
        return False
    return not any(
        "退货" in result.chunk.text
        and any(goods in result.chunk.text for goods in ("商品", "货物", "物品"))
        for result in results
    )


def build_low_confidence_answer(check: EvidenceCheck) -> str:
    reasons = []
    if check.missing_law_support:
        reasons.append("缺少明确法律或条文依据: " + "、".join(check.missing_law_support))
    if check.missing_facts:
        reasons.append("还缺少事实信息: " + "、".join(check.missing_facts))
    if check.low_coverage:
        reasons.append("检索覆盖不足: " + "、".join(check.low_coverage))
    detail = "\n".join(f"- {reason}" for reason in reasons) or "- 当前资料不足。"
    return (
        "我无法仅根据当前检索资料给出可靠结论。\n\n"
        f"{detail}\n\n"
        "可以补充更具体的法律名称、条文编号或事实背景后再检索。"
    )


def with_stop_reason(check: EvidenceCheck, stop_reason: str) -> EvidenceCheck:
    return replace(check, stop_reason=stop_reason)


def required_law_hints(
    analysis: QueryAnalysis | None,
    normalized_query: NormalizedQuery | None,
    plans: list[RetrievalPlan] | None,
) -> list[str]:
    hints: list[str] = []
    if analysis:
        hints.extend(analysis.law_names)
    if normalized_query:
        hints.extend(normalized_query.law_hints)
    for plan in plans or []:
        hints.extend(plan.law_hints)
    return unique(hints)


def required_article_hints(
    analysis: QueryAnalysis | None,
    normalized_query: NormalizedQuery | None,
    plans: list[RetrievalPlan] | None,
) -> list[str]:
    hints: list[str] = []
    if analysis:
        hints.extend(analysis.article_numbers)
    if normalized_query:
        hints.extend(normalized_query.article_hints)
    for plan in plans or []:
        hints.extend(plan.article_hints)
    return unique(hints)


def build_followup_queries(
    query: str,
    *,
    missing_law_support: list[str],
    max_queries: int,
) -> list[str]:
    queries: list[str] = []
    for item in missing_law_support:
        if item.startswith("missing_law:"):
            queries.append(f"{item.removeprefix('missing_law:')} {query}")
        elif item.startswith("missing_article:"):
            queries.append(f"{query} {item.removeprefix('missing_article:')}")
    if not queries and missing_law_support:
        queries.append(query)
    return unique(queries)[:max_queries]


def unique(values) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
