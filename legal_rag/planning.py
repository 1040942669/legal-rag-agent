from __future__ import annotations

from .models import NormalizedQuery, RetrievalPlan
from .query_understanding import extract_keywords, suggest_law_hints


def build_retrieval_plans(
    normalized: NormalizedQuery,
    *,
    max_queries: int = 3,
    per_plan_top_k: int = 5,
) -> tuple[list[RetrievalPlan], dict]:
    questions = normalized.legal_questions or [normalized.original_query]
    truncated = max(0, len(questions) - max_queries)
    selected = questions[:max_queries]
    plans: list[RetrievalPlan] = []

    for index, question in enumerate(selected, start=1):
        question_law_hints = unique([*suggest_law_hints(question), *normalized.law_hints])
        question_keywords = unique([*extract_keywords(question), *normalized.keywords])
        query = render_plan_query(
            question=question,
            law_hints=question_law_hints,
            article_hints=normalized.article_hints,
            keywords=question_keywords,
        )
        plans.append(
            RetrievalPlan(
                plan_id=f"q{index}",
                query=query,
                law_hints=question_law_hints,
                article_hints=normalized.article_hints,
                keywords=question_keywords,
                top_k=max(1, per_plan_top_k),
                rationale=build_rationale(normalized, question_law_hints, question_keywords),
                source_question_index=index - 1,
            )
        )

    if not plans:
        plans.append(
            RetrievalPlan(
                plan_id="q1",
                query=normalized.original_query,
                law_hints=normalized.law_hints,
                article_hints=normalized.article_hints,
                keywords=normalized.keywords,
                top_k=max(1, per_plan_top_k),
                rationale="fallback original query",
                source_question_index=0,
            )
        )

    return plans, {
        "max_queries": max_queries,
        "per_plan_top_k": per_plan_top_k,
        "input_question_count": len(questions),
        "truncated_count": truncated,
        "normalizer_source": normalized.source,
    }


def render_plan_query(
    *,
    question: str,
    law_hints: list[str],
    article_hints: list[str],
    keywords: list[str],
) -> str:
    parts = [question.strip()]
    if law_hints:
        parts.append(" ".join(f"《{law}》" for law in law_hints[:3]))
    if article_hints:
        parts.append(" ".join(article_hints[:5]))
    if keywords:
        parts.append("关键词: " + " ".join(keywords[:8]))
    return " ".join(part for part in parts if part).strip()


def build_rationale(
    normalized: NormalizedQuery,
    law_hints: list[str],
    keywords: list[str],
) -> str:
    parts = [f"source={normalized.source}", f"confidence={normalized.confidence:.2f}"]
    if law_hints:
        parts.append("law_hints=" + ",".join(law_hints[:3]))
    if keywords:
        parts.append("keywords=" + ",".join(keywords[:5]))
    return "; ".join(parts)


def unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = value.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result
