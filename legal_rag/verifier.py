from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

from .models import (
    ANSWER_MODES,
    SEMANTIC_SUPPORT_STATUSES,
    AnswerClaim,
    EvidenceCheck,
    SearchResult,
    StructuredAnswer,
    VerificationContext,
    VerificationResult,
)


CITATION_RE = re.compile(r"\[S([1-9]\d*)\]")
CITATION_LIKE_RE = re.compile(r"\[\s*[sS][^\]\r\n]*\]")
SOURCE_ID_RE = re.compile(r"S([1-9]\d*)$")
MAX_STRUCTURED_ANSWER_CHARS = 65_536
STANDARD_DISCLAIMER = "仅供课程学习和法律文本检索参考，不构成法律意见。"
LEGAL_CLAIM_WORDS = ["应当", "不得", "可以", "必须", "承担", "规定", "禁止", "权利", "义务"]
HIGH_RISK_FLAGS = {"case_strategy", "illegal_help", "medical_financial_advice", "non_legal"}
STRUCTURED_ANSWER_FIELDS = {
    "answer_text",
    "answer_mode",
    "claims",
    "limitations",
    "clarification_question",
}
EXPLICIT_REFUSAL_PATTERNS = (
    re.compile(r"(?:^|[。！？\n])\s*(?:抱歉[，,]\s*)?(?:我|本助手|本系统)(?:不能|无法|不予)(?:为(?:你|您|用户))?(?:直接)?(?:提供|回答|协助|给出)"),
    re.compile(r"(?:^|[。！？\n])\s*抱歉[，,]\s*(?:不能|无法|不予)(?:回答|提供|协助)"),
    re.compile(r"^\s*(?:不能|无法|不予)(?:直接)?(?:回答(?:这个|该)(?:问题|请求)|提供(?:违法|非法|具体操作)|协助(?:规避执法|违法))"),
    re.compile(r"(?:^|[。！？\n])\s*(?:我|本助手|本系统)拒绝(?:提供|回答|协助)"),
    re.compile(r"(?:这个问题|这个请求|该请求)(?:不属于|超出)当前.+(?:范围|能力)"),
)
QUOTED_TEXT_RE = re.compile(
    r"“([^”]*)”|\"([^\"]*)\"|‘([^’]*)’|'([^']*)'|「([^」]*)」|『([^』]*)』"
)
QUOTED_ATTRIBUTION_BEFORE_RE = re.compile(
    r"(?:示例|提示语|引述|引用|原文|当事人|用户|第三方|对方|证人|他说|她说|其表示)"
    r"(?:中|称|表示|写道|写明|写着|为)?\s*[，,:：]?\s*$"
)
QUOTED_ATTRIBUTION_AFTER_RE = re.compile(
    r"^\s*(?:是|属于|作为|仅为|只是)?\s*(?:一段)?\s*"
    r"(?:示例|提示语|引述|引用|原文|当事人陈述|用户陈述)"
)
INSUFFICIENT_MARKERS = (
    "资料不足",
    "证据不足",
    "依据不足",
    "当前检索资料不足",
    "无法仅根据当前",
    "无法给出可靠结论",
)
CLARIFICATION_MARKERS = ("请补充", "需要补充", "请明确", "需要澄清")
INSUFFICIENT_RESPONSE_TEXTS = frozenset(
    {
        "当前检索资料不足，无法给出可靠结论。",
        (
            "我无法仅根据当前检索资料给出可靠结论。\n\n"
            "可以补充更具体的法律名称、条文编号或事实背景后再检索。"
        ),
    }
)
CLARIFICATION_PREAMBLE = "当前信息不足，需要补充关键事实后再检索。"
SAFE_CLARIFICATION_QUESTION = "请补充作答所需的关键事实。"
SAFE_CLARIFICATION_RESPONSE = (
    f"{CLARIFICATION_PREAMBLE}\n\n{SAFE_CLARIFICATION_QUESTION}"
)
OUT_OF_SCOPE_RESPONSE_TEXTS = frozenset(
    {
        (
            "我不能提供违法帮助、规避执法或相关操作方案。\n\n"
            "我可以帮助检索相关法律条文或解释公开法律文本。"
        ),
        (
            "我不能直接给出具体案件策略、胜诉判断或个性化法律意见。\n\n"
            "我可以帮助检索相关法律条文或解释公开法律文本。"
        ),
        (
            "我不能提供医疗、金融或投资等专业建议。\n\n"
            "我可以帮助检索相关法律条文或解释公开法律文本。"
        ),
        (
            "这个问题不属于当前中国现行法律文本检索范围。\n\n"
            "我可以帮助检索相关法律条文或解释公开法律文本。"
        ),
        "这个请求超出当前法律文本学习助手的范围，我不能提供具体操作方案。",
    }
)


def parse_structured_answer(answer: str | StructuredAnswer) -> StructuredAnswer:
    """Parse the M1 answer envelope, with an explicit legacy-text fallback.

    A legacy string remains usable for display and citation inspection, but
    ``schema_valid`` is false. This prevents a best-effort adapter from being
    reported as if the model satisfied the structured-output contract.
    """

    if isinstance(answer, StructuredAnswer):
        errors = validate_structured_answer(answer)
        if not errors and answer.schema_valid:
            return answer
        safe_claims = []
        if isinstance(answer.claims, list):
            safe_claims = [
                claim
                for claim in answer.claims
                if isinstance(claim, AnswerClaim)
                and isinstance(claim.claim_id, str)
                and isinstance(claim.text, str)
                and isinstance(claim.source_ids, list)
                and all(isinstance(source_id, str) for source_id in claim.source_ids)
            ]
        return StructuredAnswer(
            answer_text=answer.answer_text if isinstance(answer.answer_text, str) else "",
            answer_mode=answer.answer_mode if isinstance(answer.answer_mode, str) else "invalid",
            claims=safe_claims,
            limitations=(
                [item for item in answer.limitations if isinstance(item, str)]
                if isinstance(answer.limitations, list)
                else []
            ),
            clarification_question=(
                answer.clarification_question
                if isinstance(answer.clarification_question, str)
                else None
            ),
            schema_valid=False,
            adapter_source=(
                answer.adapter_source if isinstance(answer.adapter_source, str) else "invalid"
            ),
            parse_errors=_unique(
                (
                    [item for item in answer.parse_errors if isinstance(item, str)]
                    if isinstance(answer.parse_errors, list)
                    else []
                )
                + errors
            ),
        )

    if not isinstance(answer, str):
        return _invalid_answer(["input_must_be_string_or_structured_answer"])
    if len(answer) > MAX_STRUCTURED_ANSWER_CHARS:
        return _invalid_answer(["input_too_large"])

    raw = answer.strip()
    candidate = _strip_json_fence(raw)
    if candidate.startswith("{"):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            return _legacy_answer(raw, [f"invalid_json:{exc.msg}"])
        except RecursionError:
            return _invalid_answer(["invalid_json:nesting_too_deep"])
        except (ValueError, OverflowError):
            # CPython can reject otherwise syntactically valid JSON when a
            # numeric token exceeds interpreter resource limits. Treat every
            # such model payload as invalid input instead of letting it abort
            # the request path.
            return _invalid_answer(["invalid_json:resource_limit"])
        except TypeError:
            return _invalid_answer(["invalid_json:unsupported_input_type"])
        parsed, errors = _answer_from_payload(payload)
        if parsed is not None and not errors:
            return parsed
        fallback_text = raw
        if isinstance(payload, dict) and isinstance(payload.get("answer_text"), str):
            fallback_text = payload["answer_text"]
        return _legacy_answer(fallback_text, errors or ["invalid_schema"])
    return _legacy_answer(raw, ["legacy_text_not_structured"])


def verify_answer(
    answer: str | StructuredAnswer,
    results: list[SearchResult],
    *,
    evidence_check: EvidenceCheck | None = None,
    risk_flags: list[str] | None = None,
    disclaimer: str = "",
    expected_answer_mode: str | None = None,
    context: VerificationContext | None = None,
    semantic_support_status: str | None = None,
) -> VerificationResult:
    structured = parse_structured_answer(answer)
    catalog_source_ids = [
        f"S{result.rank}"
        for result in results
        if isinstance(result.rank, int) and not isinstance(result.rank, bool) and result.rank > 0
    ]
    duplicate_source_ids = sorted(
        {
            source_id
            for source_id in catalog_source_ids
            if catalog_source_ids.count(source_id) > 1
        }
    )
    evidence_catalog_valid = (
        len(catalog_source_ids) == len(results) and not duplicate_source_ids
    )
    valid_source_ids = set(catalog_source_ids)
    visible_source_ids = [f"S{rank}" for rank in CITATION_RE.findall(structured.answer_text)]
    malformed_citation_tokens = _unique(
        [
            token
            for token in CITATION_LIKE_RE.findall(structured.answer_text)
            if CITATION_RE.fullmatch(token) is None
        ]
    )
    claim_source_ids = [
        source_id
        for claim in structured.claims
        for source_id in claim.source_ids
    ]
    visible_source_ids = _unique(visible_source_ids)
    claim_source_ids = _unique(claim_source_ids)
    cited_source_ids = _unique([*visible_source_ids, *claim_source_ids])
    missing_source_ids = [source_id for source_id in cited_source_ids if source_id not in valid_source_ids]
    citation_alignment_valid = _claims_align_with_visible_text(
        structured,
        disclaimer=disclaimer,
    )

    citation_required = structured.answer_mode == "evidence_answer"
    citation_ids_valid = (
        evidence_catalog_valid
        and not missing_source_ids
        and not malformed_citation_tokens
        and citation_alignment_valid
    )
    if citation_required:
        citation_ids_valid = (
            citation_ids_valid
            and bool(results)
            and bool(visible_source_ids)
            and bool(claim_source_ids)
        )

    invalid_scope_citations = find_out_of_scope_citations(
        cited_source_ids,
        results,
        context,
    )
    scope_check_configured = context is not None and (
        context.snapshot_id is not None or context.allowed_scope_ids is not None
    )
    evidence_scope_valid = (
        None
        if not scope_check_configured
        else evidence_catalog_valid and not invalid_scope_citations
    )
    disclaimer_present = not disclaimer or structured.answer_text.rstrip().endswith(disclaimer)

    expected_mode = _resolve_expected_mode(
        expected_answer_mode=expected_answer_mode,
        evidence_check=evidence_check,
        risk_flags=risk_flags,
    )
    refusal_present = contains_refusal(structured.answer_text)
    refusal_required = expected_mode == "out_of_scope"
    expected_mode_valid = expected_answer_mode is None or expected_answer_mode in ANSWER_MODES
    response_mode_valid = expected_mode_valid and validate_response_mode(
        structured,
        expected_mode=expected_mode,
        refusal_present=refusal_present,
        cited_source_ids=cited_source_ids,
        disclaimer=disclaimer,
    )
    refusal_correct = response_mode_valid if refusal_required else None

    unsupported_claims = unsupported_legal_claims(structured, results)
    if structured.answer_mode != "evidence_answer" or not structured.claims:
        semantic_status = "not_checked"
    elif semantic_support_status is None:
        semantic_status = "uncertain" if unsupported_claims else "not_checked"
    elif semantic_support_status in SEMANTIC_SUPPORT_STATUSES:
        semantic_status = semantic_support_status
    else:
        semantic_status = "not_checked"

    failure_reasons: list[str] = []
    if not structured.schema_valid:
        failure_reasons.append("schema_invalid")
    if not evidence_catalog_valid:
        failure_reasons.append("evidence_catalog_invalid")
    if not citation_ids_valid:
        failure_reasons.append("citation_ids_invalid")
    if evidence_scope_valid is False:
        failure_reasons.append("evidence_scope_invalid")
    if not disclaimer_present:
        failure_reasons.append("missing_disclaimer")
    if not response_mode_valid:
        failure_reasons.append(
            "response_mode_invalid" if expected_mode_valid else "expected_answer_mode_invalid"
        )

    return VerificationResult(
        passed=not failure_reasons,
        schema_valid=structured.schema_valid,
        evidence_catalog_valid=evidence_catalog_valid,
        citation_ids_valid=citation_ids_valid,
        citation_alignment_valid=citation_alignment_valid,
        evidence_scope_valid=evidence_scope_valid,
        disclaimer_present=disclaimer_present,
        response_mode_valid=response_mode_valid,
        semantic_support_status=semantic_status,
        expected_answer_mode=expected_mode,
        actual_answer_mode=structured.answer_mode,
        refusal_required=refusal_required,
        refusal_present=refusal_present,
        answer_source_format=structured.adapter_source,
        schema_errors=structured.parse_errors,
        required_checks=[
            "schema",
            "evidence_catalog",
            "citation_ids",
            *(["evidence_scope"] if scope_check_configured else []),
            *(["disclaimer"] if disclaimer else []),
            "response_mode",
        ],
        duplicate_source_ids=duplicate_source_ids,
        missing_source_ids=missing_source_ids,
        malformed_citation_tokens=malformed_citation_tokens,
        invalid_scope_citations=invalid_scope_citations,
        cited_source_ids=cited_source_ids,
        visible_source_ids=visible_source_ids,
        claim_source_ids=claim_source_ids,
        unsupported_claims=unsupported_claims,
        refusal_correct=refusal_correct,
        failure_reasons=failure_reasons,
    )


def build_verifier_fallback_answer(
    answer: str,
    verification: VerificationResult,
    *,
    low_confidence_answer: str,
) -> str:
    if "citation_ids_invalid" in verification.failure_reasons:
        return low_confidence_answer + "\n\n原回答存在无效引用，已降级为资料不足回答。"
    if "evidence_scope_invalid" in verification.failure_reasons:
        return low_confidence_answer + "\n\n原回答引用了本次快照或权限范围外的资料，已拒绝使用。"
    if "schema_invalid" in verification.failure_reasons:
        return low_confidence_answer + "\n\n原回答未满足结构化输出要求，已降级处理。"
    if "response_mode_invalid" in verification.failure_reasons:
        return low_confidence_answer + "\n\n原回答行为模式与本次请求不一致，已降级处理。"
    if "missing_disclaimer" in verification.failure_reasons:
        return low_confidence_answer + "\n\n原回答缺少必要的使用边界说明，已降级处理。"
    if not verification.passed:
        return low_confidence_answer + "\n\n原回答未通过必要检查，已降级处理。"
    return answer


def contains_refusal(answer: str) -> bool:
    text = _text_for_refusal_detection(answer)
    return any(pattern.search(text) for pattern in EXPLICIT_REFUSAL_PATTERNS)


def validate_response_mode(
    answer: StructuredAnswer,
    *,
    expected_mode: str | None,
    refusal_present: bool,
    cited_source_ids: list[str],
    disclaimer: str = "",
) -> bool:
    if not isinstance(answer.answer_mode, str) or answer.answer_mode not in ANSWER_MODES:
        return False
    if expected_mode is not None and answer.answer_mode != expected_mode:
        return False
    if answer.answer_mode == "evidence_answer":
        return bool(answer.claims) and not answer.clarification_question and not refusal_present
    if answer.answer_mode == "insufficient_evidence":
        return (
            not answer.claims
            and not cited_source_ids
            and not answer.clarification_question
            and bounded_insufficient_response(answer.answer_text, disclaimer=disclaimer)
            and not refusal_present
        )
    if answer.answer_mode == "needs_clarification":
        question = answer.clarification_question or ""
        return (
            bool(question.strip())
            and bounded_clarification_response(
                answer.answer_text,
                question.strip(),
                disclaimer=disclaimer,
            )
            and not answer.claims
            and not cited_source_ids
            and not refusal_present
        )
    return (
        not answer.claims
        and not cited_source_ids
        and not answer.clarification_question
        and refusal_present
        and bounded_out_of_scope_response(answer.answer_text, disclaimer=disclaimer)
    )


def bounded_insufficient_response(answer_text: str, *, disclaimer: str = "") -> bool:
    body = _without_exact_trailing_disclaimer(answer_text, disclaimer).strip()
    return body in INSUFFICIENT_RESPONSE_TEXTS


def bounded_clarification_response(
    answer_text: str,
    question: str,
    *,
    disclaimer: str = "",
) -> bool:
    if question != SAFE_CLARIFICATION_QUESTION:
        return False
    body = _without_exact_trailing_disclaimer(answer_text, disclaimer).strip()
    return body == SAFE_CLARIFICATION_RESPONSE


def bounded_out_of_scope_response(answer_text: str, *, disclaimer: str = "") -> bool:
    body = _without_exact_trailing_disclaimer(answer_text, disclaimer).strip()
    return body in OUT_OF_SCOPE_RESPONSE_TEXTS


def collect_source_ids(answer: StructuredAnswer) -> list[str]:
    source_ids = [f"S{rank}" for rank in CITATION_RE.findall(answer.answer_text)]
    for claim in answer.claims:
        source_ids.extend(claim.source_ids)
    return _unique(source_ids)


def find_out_of_scope_citations(
    cited_source_ids: list[str],
    results: list[SearchResult],
    context: VerificationContext | None,
) -> list[str]:
    if context is None:
        return []
    by_source_id = {f"S{result.rank}": result for result in results}
    invalid: list[str] = []
    for source_id in cited_source_ids:
        result = by_source_id.get(source_id)
        if result is None:
            continue
        if not evidence_result_in_context(result, context):
            invalid.append(source_id)
    return _unique(invalid)


def filter_results_to_context(
    results: list[SearchResult],
    context: VerificationContext | None,
) -> tuple[list[SearchResult], list[str], dict[str, str]]:
    """Remove evidence that must not be exposed to generation or callers.

    Returned evidence is re-ranked only after filtering, so the model and the
    verifier share one contiguous, request-local source catalog.
    """

    allowed: list[SearchResult] = []
    rejected_source_ids: list[str] = []
    original_source_ids = [f"S{result.rank}" for result in results]
    ambiguous_source_ids = {
        source_id
        for source_id in original_source_ids
        if original_source_ids.count(source_id) > 1
    }
    for result in results:
        original_source_id = f"S{result.rank}"
        rank_valid = (
            isinstance(result.rank, int)
            and not isinstance(result.rank, bool)
            and result.rank > 0
            and original_source_id not in ambiguous_source_ids
        )
        if rank_valid and (context is None or evidence_result_in_context(result, context)):
            allowed.append(result)
        else:
            rejected_source_ids.append(original_source_id)
    reranked: list[SearchResult] = []
    source_id_map: dict[str, str] = {}
    for rank, result in enumerate(allowed, start=1):
        original_source_id = f"S{result.rank}"
        filtered_source_id = f"S{rank}"
        source_id_map[original_source_id] = filtered_source_id
        reranked.append(
            replace(
                result,
                rank=rank,
                trace={
                    **result.trace,
                    "evidence_filter": {
                        "original_source_id": original_source_id,
                        "filtered_source_id": filtered_source_id,
                        "allowed": True,
                    },
                },
            )
        )
    return reranked, _unique(rejected_source_ids), source_id_map


def evidence_result_in_context(
    result: SearchResult,
    context: VerificationContext,
) -> bool:
    metadata = result.chunk.metadata
    if not isinstance(metadata, dict):
        return False
    if context.snapshot_id is not None:
        if not _is_canonical_boundary_id(context.snapshot_id):
            return False
        metadata_snapshot_id = metadata.get("snapshot_id")
        if (
            not _is_canonical_boundary_id(metadata_snapshot_id)
            or metadata_snapshot_id != context.snapshot_id
        ):
            return False
    if context.allowed_scope_ids is None:
        return True
    if not isinstance(context.allowed_scope_ids, list) or not all(
        _is_canonical_boundary_id(scope_id)
        for scope_id in context.allowed_scope_ids
    ):
        return False
    raw_scope_ids = metadata.get("access_scope_ids")
    if raw_scope_ids is None:
        scope_id = metadata.get("scope_id")
        raw_scope_ids = [scope_id] if _is_canonical_boundary_id(scope_id) else []
    if not isinstance(raw_scope_ids, list) or not all(
        _is_canonical_boundary_id(scope_id) for scope_id in raw_scope_ids
    ):
        return False
    return bool(
        set(raw_scope_ids) & set(context.allowed_scope_ids)
    )


def _is_canonical_boundary_id(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def unsupported_legal_claims(
    answer: str | StructuredAnswer,
    results: list[SearchResult],
) -> list[str]:
    """Return heuristic warnings without claiming semantic verification.

    The old implementation skipped every cited sentence. A citation proves
    only that an ID exists, not that its text entails the claim. This helper
    inspects cited and uncited claims alike, while its caller maps a warning
    only to ``uncertain`` unless a real semantic checker is supplied.
    """

    if not results:
        return []
    structured = parse_structured_answer(answer)
    if structured.answer_mode != "evidence_answer" or not structured.claims:
        return []
    evidence_text = "\n".join(result.chunk.text for result in results)
    claim_texts = _unique(
        [
            *[claim.text for claim in structured.claims],
            *split_sentences(structured.answer_text),
        ]
    )
    unsupported: list[str] = []
    for sentence in claim_texts:
        if not any(word in sentence for word in LEGAL_CLAIM_WORDS):
            continue
        terms = significant_terms(CITATION_RE.sub("", sentence))
        if terms and not any(term in evidence_text for term in terms[:3]):
            unsupported.append(sentence[:120])
    return unsupported[:3]


def validate_structured_answer(answer: StructuredAnswer) -> list[str]:
    errors: list[str] = []
    if not isinstance(answer.answer_text, str) or not answer.answer_text.strip():
        errors.append("answer_text_must_be_non_empty_string")
    if not isinstance(answer.answer_mode, str) or answer.answer_mode not in ANSWER_MODES:
        errors.append("answer_mode_invalid")
    if not isinstance(answer.claims, list):
        errors.append("claims_must_be_list")
    else:
        seen_claim_ids: set[str] = set()
        for index, claim in enumerate(answer.claims):
            if not isinstance(claim, AnswerClaim):
                errors.append(f"claims[{index}]_invalid")
                continue
            claim_id_valid = (
                isinstance(claim.claim_id, str)
                and bool(claim.claim_id)
                and claim.claim_id == claim.claim_id.strip()
            )
            claim_text_valid = isinstance(claim.text, str) and bool(claim.text.strip())
            if not claim_id_valid:
                errors.append(f"claims[{index}].claim_id_invalid")
            elif claim.claim_id in seen_claim_ids:
                errors.append(f"claims[{index}].claim_id_duplicate")
            else:
                seen_claim_ids.add(claim.claim_id)
            if not claim_text_valid:
                errors.append(f"claims[{index}].text_invalid")
            if not isinstance(claim.source_ids, list) or not claim.source_ids or not all(
                isinstance(source_id, str) and SOURCE_ID_RE.fullmatch(source_id)
                for source_id in claim.source_ids
            ):
                errors.append(f"claims[{index}].source_ids_invalid")
    if not isinstance(answer.limitations, list) or not all(
        isinstance(item, str) for item in answer.limitations
    ):
        errors.append("limitations_must_be_string_list")
    if answer.clarification_question is not None and not isinstance(
        answer.clarification_question, str
    ):
        errors.append("clarification_question_must_be_string_or_null")
    return errors


def split_sentences(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"[。！？\n]+", text) if item.strip()]


def significant_terms(text: str) -> list[str]:
    terms = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    return [term for term in terms if term not in {"根据", "因此", "资料", "法律", "规定"}]


def _answer_from_payload(payload: Any) -> tuple[StructuredAnswer | None, list[str]]:
    if not isinstance(payload, dict):
        return None, ["root_must_be_object"]
    errors: list[str] = []
    missing = STRUCTURED_ANSWER_FIELDS - set(payload)
    unknown = set(payload) - STRUCTURED_ANSWER_FIELDS
    if missing:
        errors.append("missing_fields:" + ",".join(sorted(missing)))
    if unknown:
        errors.append("unknown_fields:" + ",".join(sorted(unknown)))
    if not isinstance(payload.get("answer_text"), str) or not payload.get("answer_text", "").strip():
        errors.append("answer_text_must_be_non_empty_string")
    if not isinstance(payload.get("answer_mode"), str) or payload.get("answer_mode") not in ANSWER_MODES:
        errors.append("answer_mode_invalid")
    raw_claims = payload.get("claims")
    claims: list[AnswerClaim] = []
    if not isinstance(raw_claims, list):
        errors.append("claims_must_be_list")
    else:
        seen_claim_ids: set[str] = set()
        for index, raw_claim in enumerate(raw_claims):
            claim, claim_errors = _claim_from_payload(raw_claim, index)
            errors.extend(claim_errors)
            if claim is not None:
                if claim.claim_id in seen_claim_ids:
                    errors.append(f"claims[{index}].claim_id_duplicate")
                seen_claim_ids.add(claim.claim_id)
                claims.append(claim)
    limitations = payload.get("limitations")
    if not isinstance(limitations, list) or not all(isinstance(item, str) for item in limitations):
        errors.append("limitations_must_be_string_list")
        limitations = []
    clarification = payload.get("clarification_question")
    if clarification is not None and not isinstance(clarification, str):
        errors.append("clarification_question_must_be_string_or_null")
        clarification = None
    if errors:
        return None, errors
    return (
        StructuredAnswer(
            answer_text=payload["answer_text"],
            answer_mode=payload["answer_mode"],
            claims=claims,
            limitations=limitations,
            clarification_question=clarification,
            adapter_source="structured_json",
        ),
        [],
    )


def _claim_from_payload(payload: Any, index: int) -> tuple[AnswerClaim | None, list[str]]:
    if not isinstance(payload, dict):
        return None, [f"claims[{index}]_must_be_object"]
    expected = {"claim_id", "text", "source_ids"}
    errors: list[str] = []
    if set(payload) != expected:
        errors.append(f"claims[{index}]_fields_invalid")
    claim_id = payload.get("claim_id")
    text = payload.get("text")
    source_ids = payload.get("source_ids")
    if (
        not isinstance(claim_id, str)
        or not claim_id
        or claim_id != claim_id.strip()
    ):
        errors.append(f"claims[{index}].claim_id_invalid")
    if not isinstance(text, str) or not text.strip():
        errors.append(f"claims[{index}].text_invalid")
    if not isinstance(source_ids, list) or not source_ids or not all(
        isinstance(source_id, str) and SOURCE_ID_RE.fullmatch(source_id)
        for source_id in source_ids
    ):
        errors.append(f"claims[{index}].source_ids_invalid")
    if errors:
        return None, errors
    return AnswerClaim(claim_id=claim_id, text=text, source_ids=source_ids), []


def _legacy_answer(text: str, errors: list[str]) -> StructuredAnswer:
    mode = infer_legacy_answer_mode(text)
    claims: list[AnswerClaim] = []
    for sentence in split_sentences(text):
        source_ids = [f"S{rank}" for rank in CITATION_RE.findall(sentence)]
        if source_ids or any(word in sentence for word in LEGAL_CLAIM_WORDS):
            claims.append(
                AnswerClaim(
                    claim_id=f"C{len(claims) + 1}",
                    text=sentence,
                    source_ids=_unique(source_ids),
                )
            )
    clarification = text.strip() if mode == "needs_clarification" else None
    limitations = [text.strip()] if mode == "insufficient_evidence" and text.strip() else []
    return StructuredAnswer(
        answer_text=text,
        answer_mode=mode,
        claims=claims,
        limitations=limitations,
        clarification_question=clarification,
        schema_valid=False,
        adapter_source="legacy_text",
        parse_errors=errors,
    )


def _invalid_answer(errors: list[str]) -> StructuredAnswer:
    return StructuredAnswer(
        answer_text="",
        answer_mode="invalid",
        claims=[],
        limitations=[],
        clarification_question=None,
        schema_valid=False,
        adapter_source="invalid",
        parse_errors=errors,
    )


def infer_legacy_answer_mode(text: str) -> str:
    if contains_refusal(text):
        return "out_of_scope"
    if any(marker in text for marker in INSUFFICIENT_MARKERS):
        return "insufficient_evidence"
    if any(marker in text for marker in CLARIFICATION_MARKERS):
        return "needs_clarification"
    return "evidence_answer"


def _resolve_expected_mode(
    *,
    expected_answer_mode: str | None,
    evidence_check: EvidenceCheck | None,
    risk_flags: list[str] | None,
) -> str | None:
    if expected_answer_mode is not None:
        return expected_answer_mode if expected_answer_mode in ANSWER_MODES else None
    if set(risk_flags or []) & HIGH_RISK_FLAGS:
        return "out_of_scope"
    if evidence_check is not None and (
        evidence_check.missing_facts
        or evidence_check.stop_reason == "needs_clarification"
    ):
        return "needs_clarification"
    if evidence_check is not None and not evidence_check.sufficient:
        return "insufficient_evidence"
    return None


def _strip_json_fence(text: str) -> str:
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else text


def _text_for_refusal_detection(text: str) -> str:
    """Ignore clearly attributed examples while preserving visible refusals.

    Limited-mode validation deliberately does not call this helper because
    quoted text remains visible to the user and must be validated in full.
    """

    body = _without_exact_trailing_disclaimer(text, STANDARD_DISCLAIMER)

    def replace_quote(match: re.Match[str]) -> str:
        quoted = next(group for group in match.groups() if group is not None)
        before = body[: match.start()]
        after = body[match.end() :]
        clearly_attributed = bool(
            QUOTED_ATTRIBUTION_BEFORE_RE.search(before)
            or QUOTED_ATTRIBUTION_AFTER_RE.search(after)
        )
        return "" if clearly_attributed else quoted

    visible_behavior = QUOTED_TEXT_RE.sub(replace_quote, body)
    visible_behavior = re.sub(r"[*_`]+", "", visible_behavior)
    visible_behavior = re.sub(
        r"(?m)^\s*(?:(?:[-+>#•]|\d+[.)、])\s*)+",
        "",
        visible_behavior,
    )
    visible_behavior = re.sub(
        r"(?m)(^|[。！？!?\n])\s*[（(【\[]+\s*",
        r"\1",
        visible_behavior,
    )
    # Presentation wrappers must not change behavioral classification.  This
    # removes only non-word decoration at a sentence/line boundary, then a
    # small set of assistant-authored headings.  Attribution words such as
    # "示例" and "当事人表示" are intentionally not headings here.
    visible_behavior = re.sub(
        r"(?m)(^|[。！？!?\n])\s*[^\w\u4e00-\u9fff]*",
        r"\1",
        visible_behavior,
    )
    visible_behavior = re.sub(
        r"(?m)(^|[。！？!?\n])\s*(?:提示|回答|答复|结论|说明)\s*[：:]\s*",
        r"\1",
        visible_behavior,
    )
    visible_behavior = re.sub(
        r"(?m)(^|[。！？!?\n])\s*[^\w\u4e00-\u9fff]*",
        r"\1",
        visible_behavior,
    )
    return visible_behavior


def _without_exact_trailing_disclaimer(text: str, disclaimer: str = "") -> str:
    stripped = text.rstrip()
    exact_disclaimer = disclaimer or STANDARD_DISCLAIMER
    if exact_disclaimer and stripped.endswith(exact_disclaimer):
        return stripped[: -len(exact_disclaimer)].rstrip()
    return stripped


def _claims_align_with_visible_text(
    answer: StructuredAnswer,
    *,
    disclaimer: str = "",
) -> bool:
    """Check claim presence and citation locality, not semantic entailment."""

    if not answer.claims:
        return True
    body = _without_exact_trailing_disclaimer(answer.answer_text, disclaimer)
    segments = [
        segment.strip()
        for segment in re.split(r"[。．｡！？!?；;…]+|(?<!\d)\.(?!\d)|\n+", body)
        if segment.strip()
    ]
    normalized_segments = [
        (
            _normalize_claim_text(segment, disclaimer=disclaimer),
            {f"S{rank}" for rank in CITATION_RE.findall(segment)},
        )
        for segment in segments
    ]
    for claim in answer.claims:
        normalized_claim = _normalize_claim_text(claim.text, disclaimer=disclaimer)
        if not normalized_claim:
            return False
        required_sources = set(claim.source_ids)
        if not any(
            normalized_claim in normalized_segment
            and required_sources.issubset(local_sources)
            for normalized_segment, local_sources in normalized_segments
        ):
            return False
    return True


def _normalize_claim_text(text: str, *, disclaimer: str = "") -> str:
    body = _without_exact_trailing_disclaimer(text, disclaimer)
    without_citations = CITATION_RE.sub("", body)
    return re.sub(r"[\W_]+", "", without_citations).casefold()


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
