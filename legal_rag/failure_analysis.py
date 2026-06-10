from __future__ import annotations

from dataclasses import asdict, dataclass

from .models import EvalCase, SearchResult


@dataclass(frozen=True)
class FailureAnalysis:
    label: str
    reason: str
    expected_law: str
    expected_articles: list[str]
    best_rank: int | None
    retrieved_laws: list[str]
    retrieved_articles: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def label_retrieval_failure(
    results: list[SearchResult],
    case: EvalCase,
    *,
    top_k: int = 5,
) -> FailureAnalysis:
    expected_law = case.expected_law
    expected_articles = case.expected_articles
    retrieved_laws = unique([law for result in results for law in result.chunk.law_names])
    retrieved_articles = unique(
        [article for result in results for article in result.chunk.article_numbers]
    )

    if not expected_law and not expected_articles:
        return FailureAnalysis(
            label="not_applicable",
            reason="No expected law or article was provided for this case.",
            expected_law=expected_law,
            expected_articles=expected_articles,
            best_rank=None,
            retrieved_laws=retrieved_laws,
            retrieved_articles=retrieved_articles,
        )

    best_rank = best_matching_rank(results, case)
    if best_rank is not None and best_rank <= top_k:
        return FailureAnalysis(
            label="hit",
            reason=f"Expected target found at rank {best_rank}.",
            expected_law=expected_law,
            expected_articles=expected_articles,
            best_rank=best_rank,
            retrieved_laws=retrieved_laws,
            retrieved_articles=retrieved_articles,
        )
    if best_rank is not None:
        return FailureAnalysis(
            label="low_rank",
            reason=f"Expected target found at rank {best_rank}, outside top {top_k}.",
            expected_law=expected_law,
            expected_articles=expected_articles,
            best_rank=best_rank,
            retrieved_laws=retrieved_laws,
            retrieved_articles=retrieved_articles,
        )
    if not results:
        return FailureAnalysis(
            label="miss",
            reason="Retriever returned no results.",
            expected_law=expected_law,
            expected_articles=expected_articles,
            best_rank=None,
            retrieved_laws=retrieved_laws,
            retrieved_articles=retrieved_articles,
        )

    law_seen = not expected_law or expected_law in retrieved_laws
    expected_articles_seen = [
        article for article in expected_articles if article in retrieved_articles
    ]
    if expected_law and not law_seen:
        label = "wrong_law"
        reason = f"Expected law `{expected_law}` was not retrieved in top {len(results)}."
    elif expected_articles and not expected_articles_seen:
        label = "wrong_article"
        reason = "Expected law may be present, but none of the expected articles were retrieved."
    elif law_seen and expected_articles and expected_articles_seen:
        label = "metadata_gap"
        reason = "Expected law/article tokens appeared separately, but no single chunk metadata matched both."
    else:
        label = "miss"
        reason = "Expected target was not found in retrieved chunks."

    return FailureAnalysis(
        label=label,
        reason=reason,
        expected_law=expected_law,
        expected_articles=expected_articles,
        best_rank=None,
        retrieved_laws=retrieved_laws,
        retrieved_articles=retrieved_articles,
    )


def best_matching_rank(results: list[SearchResult], case: EvalCase) -> int | None:
    for result in results:
        if result_matches_case(result, case):
            return result.rank
    return None


def result_matches_case(result: SearchResult, case: EvalCase) -> bool:
    law_ok = not case.expected_law or case.expected_law in result.chunk.law_names
    article_ok = not case.expected_articles or any(
        article in result.chunk.article_numbers for article in case.expected_articles
    )
    return law_ok and article_ok


def unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
