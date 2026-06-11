from __future__ import annotations

import re

from .models import EvidenceCheck, SearchResult, VerificationResult


CITATION_RE = re.compile(r"\[S(\d+)\]")
LEGAL_CLAIM_WORDS = ["应当", "不得", "可以", "必须", "承担", "规定", "禁止", "权利", "义务"]
REFUSAL_WORDS = ["无法", "不能", "不提供", "拒绝", "资料不足", "不构成法律意见"]
HIGH_RISK_FLAGS = {"case_strategy", "illegal_help", "medical_financial_advice", "non_legal"}


def verify_answer(
    answer: str,
    results: list[SearchResult],
    *,
    evidence_check: EvidenceCheck | None = None,
    risk_flags: list[str] | None = None,
    disclaimer: str = "",
) -> VerificationResult:
    valid_ranks = {str(result.rank) for result in results}
    cited_ranks = CITATION_RE.findall(answer)
    missing_citations = [rank for rank in cited_ranks if rank not in valid_ranks]
    citation_valid = bool(cited_ranks) and not missing_citations if results else not cited_ranks
    disclaimer_present = not disclaimer or disclaimer in answer
    refusal_required = bool(set(risk_flags or []) & HIGH_RISK_FLAGS)
    refusal_correct = not refusal_required or contains_refusal(answer)
    unsupported_claims = unsupported_legal_claims(answer, results)

    failure_reasons: list[str] = []
    if not citation_valid:
        failure_reasons.append("citation_invalid")
    if unsupported_claims:
        failure_reasons.append("unsupported_claim")
    if not disclaimer_present:
        failure_reasons.append("missing_disclaimer")
    if not refusal_correct:
        failure_reasons.append("refusal_required")
    if evidence_check and not evidence_check.sufficient and not contains_refusal(answer):
        failure_reasons.append("insufficient_evidence_not_disclosed")

    return VerificationResult(
        passed=not failure_reasons,
        citation_valid=citation_valid,
        missing_citations=missing_citations,
        unsupported_claims=unsupported_claims,
        disclaimer_present=disclaimer_present,
        refusal_correct=refusal_correct,
        failure_reasons=failure_reasons,
    )


def build_verifier_fallback_answer(
    answer: str,
    verification: VerificationResult,
    *,
    low_confidence_answer: str,
) -> str:
    if "citation_invalid" in verification.failure_reasons:
        return low_confidence_answer + "\n\n原回答存在无效引用，已降级为资料不足回答。"
    if "unsupported_claim" in verification.failure_reasons:
        return low_confidence_answer + "\n\n原回答包含当前资料无法支撑的结论，已降级处理。"
    if "refusal_required" in verification.failure_reasons:
        return "这个问题涉及个案策略、违法帮助或其他越界建议，我不能提供具体操作方案。"
    return answer


def contains_refusal(answer: str) -> bool:
    return any(word in answer for word in REFUSAL_WORDS)


def unsupported_legal_claims(answer: str, results: list[SearchResult]) -> list[str]:
    if not results:
        return []
    evidence_text = "\n".join(result.chunk.text for result in results)
    unsupported: list[str] = []
    for sentence in split_sentences(answer):
        if not any(word in sentence for word in LEGAL_CLAIM_WORDS):
            continue
        if CITATION_RE.search(sentence):
            continue
        terms = significant_terms(sentence)
        if terms and not any(term in evidence_text for term in terms[:3]):
            unsupported.append(sentence[:120])
    return unsupported[:3]


def split_sentences(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"[。！？\n]+", text) if item.strip()]


def significant_terms(text: str) -> list[str]:
    terms = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    return [term for term in terms if term not in {"根据", "因此", "资料", "法律", "规定"}]
