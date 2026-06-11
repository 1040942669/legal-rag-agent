from __future__ import annotations

from .models import EvidenceCheck, NormalizedQuery, RetrievalPlan, SearchResult
from .query import QueryAnalysis, analyze_query


LOW_SCORE_THRESHOLD = 0.01


def check_evidence_sufficiency(
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
    for law in required_laws:
        if not any(law in covered or covered in law for covered in covered_laws):
            missing_law_support.append(f"missing_law:{law}")
    for article in required_articles:
        if article not in covered_articles:
            missing_law_support.append(f"missing_article:{article}")

    if normalized_query:
        missing_facts.extend(normalized_query.missing_facts)
        if len(normalized_query.legal_questions) > len(results) and len(results) < min_results:
            low_coverage.append("not_enough_results_for_legal_questions")

    if len(results) < min_results:
        low_coverage.append("too_few_results")
    if results and max(result.score for result in results) <= LOW_SCORE_THRESHOLD:
        low_coverage.append("low_scores")

    followup_queries = build_followup_queries(
        query,
        missing_law_support=missing_law_support,
        missing_facts=missing_facts,
        max_queries=max_followup_queries,
    )
    sufficient = not missing_law_support and not low_coverage and bool(results)
    return EvidenceCheck(
        sufficient=sufficient,
        missing_facts=unique(missing_facts),
        missing_law_support=unique(missing_law_support),
        low_coverage=unique(low_coverage),
        followup_queries=followup_queries,
        stop_reason="sufficient" if sufficient else "needs_followup",
        checked_result_count=len(results),
        covered_laws=covered_laws,
        covered_articles=covered_articles,
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
    return EvidenceCheck(
        sufficient=check.sufficient,
        missing_facts=check.missing_facts,
        missing_law_support=check.missing_law_support,
        low_coverage=check.low_coverage,
        followup_queries=check.followup_queries,
        stop_reason=stop_reason,
        checked_result_count=check.checked_result_count,
        covered_laws=check.covered_laws,
        covered_articles=check.covered_articles,
    )


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
    missing_facts: list[str],
    max_queries: int,
) -> list[str]:
    queries: list[str] = []
    for item in missing_law_support:
        if item.startswith("missing_law:"):
            queries.append(f"{item.removeprefix('missing_law:')} {query}")
        elif item.startswith("missing_article:"):
            queries.append(f"{query} {item.removeprefix('missing_article:')}")
    for fact in missing_facts:
        queries.append(f"{query} {fact}")
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
