from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import fields, replace
from typing import Any

from .adaptive import AdaptiveRetrievalResult
from .chat import (
    ASSISTANT_SESSION_STATE_SCHEMA_VERSION,
    STRUCTURED_ANSWER_PARSER_VERSION,
    ConversationMemory,
    GeneratedTurn,
    LegalChatAssistant,
    PreparedQuestion,
    RetrievedTurn,
    VerifiedTurn,
    append_disclaimer,
    build_limited_structured_answer,
    build_risk_refusal_answer,
    expected_limited_answer_mode,
    programmatic_answer,
    render_retrieval_only_answer,
    safe_terminal_answer,
    should_refuse_before_retrieval,
)
from .evaluation_artifacts import (
    _retrieval_boundary_from_payload,
    _retrieval_boundary_to_payload,
    search_result_from_artifact,
    search_result_to_artifact,
)
from .experiment_runtime import canonical_hash, canonical_json_bytes
from .json_utils import validate_json_unicode
from .models import (
    ANSWER_MODES,
    SEMANTIC_SUPPORT_STATUSES,
    AnswerClaim,
    EvidenceCheck,
    NormalizedQuery,
    RetrievalPlan,
    SearchResult,
    StructuredAnswer,
    VerificationResult,
)
from .query import QueryAnalysis, analyze_query
from .retrieval import assert_results_match_boundary
from .retrieval_contracts import RetrievalBoundary
from .verifier import parse_structured_answer


CHAT_STAGE_ARTIFACT_SCHEMA_VERSION = 1
_ARTIFACT_FIELDS = {"artifact_schema_version", "artifact_kind", "payload"}
_SHA256_HEX_LENGTH = 64
_SOURCE_ID_PATTERN = re.compile(r"S[1-9][0-9]*")


def _json_copy(value: Any) -> Any:
    try:
        return json.loads(canonical_json_bytes(value).decode("utf-8"))
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("chat stage artifact must contain strict JSON values") from exc


def _exact_mapping(
    name: str,
    value: Any,
    expected_fields: set[str] | frozenset[str],
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(expected_fields):
        raise ValueError(f"{name} fields are invalid")
    copied = _json_copy(dict(value))
    if not isinstance(copied, dict):  # pragma: no cover - guarded by Mapping
        raise ValueError(f"{name} must be an object")
    return copied


def _object(name: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    copied = _json_copy(dict(value))
    if not isinstance(copied, dict):  # pragma: no cover - guarded by Mapping
        raise ValueError(f"{name} must be an object")
    return copied


def _string(name: str, value: Any, *, non_empty: bool = False) -> str:
    if not isinstance(value, str) or (non_empty and not value.strip()):
        qualifier = "a non-empty string" if non_empty else "a string"
        raise ValueError(f"{name} must be {qualifier}")
    validate_json_unicode(value)
    return value


def _optional_string(name: str, value: Any) -> str | None:
    if value is None:
        return None
    return _string(name, value)


def _boolean(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _optional_boolean(name: str, value: Any) -> bool | None:
    if value is None:
        return None
    return _boolean(name, value)


def _integer(name: str, value: Any, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")
    return value


def _number(
    name: str,
    value: Any,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    if minimum is not None and result < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and result > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return result


def _string_list(name: str, value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    return [_string(f"{name}[{index}]", item) for index, item in enumerate(value)]


def _artifact(kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    return _json_copy(
        {
            "artifact_schema_version": CHAT_STAGE_ARTIFACT_SCHEMA_VERSION,
            "artifact_kind": kind,
            "payload": dict(payload),
        }
    )


def _artifact_payload(kind: str, artifact: Mapping[str, Any]) -> dict[str, Any]:
    envelope = _exact_mapping(f"{kind} artifact", artifact, _ARTIFACT_FIELDS)
    schema_version = envelope["artifact_schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != CHAT_STAGE_ARTIFACT_SCHEMA_VERSION
    ):
        raise ValueError(f"{kind} artifact schema is unsupported")
    if envelope["artifact_kind"] != kind:
        raise ValueError(f"{kind} artifact kind is invalid")
    return _object(f"{kind} artifact payload", envelope["payload"])


def _digest(name: str, value: Any) -> str:
    digest = _string(name, value)
    if len(digest) != _SHA256_HEX_LENGTH or any(
        char not in "0123456789abcdef" for char in digest
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return digest


def _source_id(name: str, value: Any) -> str:
    source_id = _string(name, value, non_empty=True)
    if _SOURCE_ID_PATTERN.fullmatch(source_id) is None:
        raise ValueError(f"{name} must be a canonical source ID")
    return source_id


def _optional_answer_mode(name: str, value: Any) -> str | None:
    answer_mode = _optional_string(name, value)
    if answer_mode is not None and answer_mode not in ANSWER_MODES:
        raise ValueError(f"{name} is unsupported")
    return answer_mode


def _string_mapping(name: str, value: Any) -> dict[str, str]:
    payload = _object(name, value)
    restored: dict[str, str] = {}
    for key, item in payload.items():
        restored[_string(f"{name} key", key, non_empty=True)] = _string(
            f"{name}.{key}", item, non_empty=True
        )
    return restored


def _ordered_unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _query_analysis_payload(analysis: QueryAnalysis) -> dict[str, Any]:
    if not isinstance(analysis, QueryAnalysis):
        raise ValueError("analysis must be a QueryAnalysis")
    payload = {
        field.name: getattr(analysis, field.name) for field in fields(QueryAnalysis)
    }
    return _query_analysis_from_payload(payload).to_dict()


def _query_analysis_from_payload(value: Any) -> QueryAnalysis:
    payload = _exact_mapping(
        "query analysis",
        value,
        {field.name for field in fields(QueryAnalysis)},
    )
    return QueryAnalysis(
        original_query=_string("analysis.original_query", payload["original_query"]),
        normalized_query=_string(
            "analysis.normalized_query", payload["normalized_query"]
        ),
        law_names=_string_list("analysis.law_names", payload["law_names"]),
        article_numbers=_string_list(
            "analysis.article_numbers", payload["article_numbers"]
        ),
        case_type_hints=_string_list(
            "analysis.case_type_hints", payload["case_type_hints"]
        ),
        risk_flags=_string_list("analysis.risk_flags", payload["risk_flags"]),
        complexity_flags=_string_list(
            "analysis.complexity_flags", payload["complexity_flags"]
        ),
        adaptive_reasons=_string_list(
            "analysis.adaptive_reasons", payload["adaptive_reasons"]
        ),
        char_length=_integer("analysis.char_length", payload["char_length"]),
        token_count=_integer("analysis.token_count", payload["token_count"]),
        confidence=_number(
            "analysis.confidence", payload["confidence"], minimum=0.0, maximum=1.0
        ),
    )


def query_analysis_to_artifact(analysis: QueryAnalysis) -> dict[str, Any]:
    return _artifact("query_analysis", _query_analysis_payload(analysis))


def query_analysis_from_artifact(artifact: Mapping[str, Any]) -> QueryAnalysis:
    return _query_analysis_from_payload(_artifact_payload("query_analysis", artifact))


def _normalized_query_payload(query: NormalizedQuery) -> dict[str, Any]:
    if not isinstance(query, NormalizedQuery):
        raise ValueError("normalized query must be a NormalizedQuery")
    payload = {
        field.name: getattr(query, field.name) for field in fields(NormalizedQuery)
    }
    return _json_copy(_normalized_query_from_payload(payload).to_dict())


def _normalized_query_from_payload(value: Any) -> NormalizedQuery:
    payload = _exact_mapping(
        "normalized query",
        value,
        {field.name for field in fields(NormalizedQuery)},
    )
    return NormalizedQuery(
        original_query=_string("normalized.original_query", payload["original_query"]),
        legal_questions=_string_list(
            "normalized.legal_questions", payload["legal_questions"]
        ),
        missing_facts=_string_list(
            "normalized.missing_facts", payload["missing_facts"]
        ),
        law_hints=_string_list("normalized.law_hints", payload["law_hints"]),
        article_hints=_string_list(
            "normalized.article_hints", payload["article_hints"]
        ),
        keywords=_string_list("normalized.keywords", payload["keywords"]),
        risk_flags=_string_list("normalized.risk_flags", payload["risk_flags"]),
        confidence=_number(
            "normalized.confidence", payload["confidence"], minimum=0.0, maximum=1.0
        ),
        source=_string("normalized.source", payload["source"], non_empty=True),
        errors=_string_list("normalized.errors", payload["errors"]),
        raw_response=_string("normalized.raw_response", payload["raw_response"]),
    )


def _retrieval_plan_payload(plan: RetrievalPlan) -> dict[str, Any]:
    if not isinstance(plan, RetrievalPlan):
        raise ValueError("retrieval plan must be a RetrievalPlan")
    payload = {field.name: getattr(plan, field.name) for field in fields(RetrievalPlan)}
    return _json_copy(_retrieval_plan_from_payload(payload).to_dict())


def _retrieval_plan_from_payload(value: Any) -> RetrievalPlan:
    payload = _exact_mapping(
        "retrieval plan",
        value,
        {field.name for field in fields(RetrievalPlan)},
    )
    return RetrievalPlan(
        plan_id=_string("plan.plan_id", payload["plan_id"], non_empty=True),
        query=_string("plan.query", payload["query"], non_empty=True),
        law_hints=_string_list("plan.law_hints", payload["law_hints"]),
        article_hints=_string_list("plan.article_hints", payload["article_hints"]),
        keywords=_string_list("plan.keywords", payload["keywords"]),
        top_k=_integer("plan.top_k", payload["top_k"], minimum=1),
        rationale=_string("plan.rationale", payload["rationale"]),
        source_question_index=_integer(
            "plan.source_question_index", payload["source_question_index"]
        ),
    )


def _evidence_check_payload(check: EvidenceCheck) -> dict[str, Any]:
    if not isinstance(check, EvidenceCheck):
        raise ValueError("evidence check must be an EvidenceCheck")
    payload = {
        field.name: getattr(check, field.name) for field in fields(EvidenceCheck)
    }
    restored = _evidence_check_from_payload(payload)
    return _json_copy(
        {field.name: getattr(restored, field.name) for field in fields(EvidenceCheck)}
    )


def _evidence_check_from_payload(value: Any) -> EvidenceCheck:
    payload = _exact_mapping(
        "evidence check",
        value,
        {field.name for field in fields(EvidenceCheck)},
    )
    return EvidenceCheck(
        sufficient=_boolean("evidence.sufficient", payload["sufficient"]),
        missing_facts=_string_list("evidence.missing_facts", payload["missing_facts"]),
        missing_law_support=_string_list(
            "evidence.missing_law_support", payload["missing_law_support"]
        ),
        low_coverage=_string_list("evidence.low_coverage", payload["low_coverage"]),
        followup_queries=_string_list(
            "evidence.followup_queries", payload["followup_queries"]
        ),
        stop_reason=_string(
            "evidence.stop_reason", payload["stop_reason"], non_empty=True
        ),
        checked_result_count=_integer(
            "evidence.checked_result_count", payload["checked_result_count"]
        ),
        covered_laws=_string_list("evidence.covered_laws", payload["covered_laws"]),
        covered_articles=_string_list(
            "evidence.covered_articles", payload["covered_articles"]
        ),
    )


def normalized_query_to_artifact(query: NormalizedQuery) -> dict[str, Any]:
    return _artifact("normalized_query", _normalized_query_payload(query))


def normalized_query_from_artifact(artifact: Mapping[str, Any]) -> NormalizedQuery:
    return _normalized_query_from_payload(
        _artifact_payload("normalized_query", artifact)
    )


def retrieval_plan_to_artifact(plan: RetrievalPlan) -> dict[str, Any]:
    return _artifact("retrieval_plan", _retrieval_plan_payload(plan))


def retrieval_plan_from_artifact(artifact: Mapping[str, Any]) -> RetrievalPlan:
    return _retrieval_plan_from_payload(_artifact_payload("retrieval_plan", artifact))


def evidence_check_to_artifact(check: EvidenceCheck) -> dict[str, Any]:
    return _artifact("evidence_check", _evidence_check_payload(check))


def evidence_check_from_artifact(artifact: Mapping[str, Any]) -> EvidenceCheck:
    return _evidence_check_from_payload(_artifact_payload("evidence_check", artifact))


def _structured_answer_payload(answer: StructuredAnswer) -> dict[str, Any]:
    if not isinstance(answer, StructuredAnswer):
        raise ValueError("answer must be a StructuredAnswer")
    payload = {
        "answer_text": answer.answer_text,
        "answer_mode": answer.answer_mode,
        "claims": [
            {
                "claim_id": claim.claim_id,
                "text": claim.text,
                "source_ids": claim.source_ids,
            }
            if isinstance(claim, AnswerClaim)
            else claim
            for claim in answer.claims
        ],
        "limitations": answer.limitations,
        "clarification_question": answer.clarification_question,
        "schema_valid": answer.schema_valid,
        "adapter_source": answer.adapter_source,
        "parse_errors": answer.parse_errors,
    }
    restored = _structured_answer_from_payload(payload)
    return {
        "answer_text": restored.answer_text,
        "answer_mode": restored.answer_mode,
        "claims": [claim.to_dict() for claim in restored.claims],
        "limitations": list(restored.limitations),
        "clarification_question": restored.clarification_question,
        "schema_valid": restored.schema_valid,
        "adapter_source": restored.adapter_source,
        "parse_errors": list(restored.parse_errors),
    }


def _structured_answer_from_payload(value: Any) -> StructuredAnswer:
    payload = _exact_mapping(
        "structured answer",
        value,
        {
            "answer_text",
            "answer_mode",
            "claims",
            "limitations",
            "clarification_question",
            "schema_valid",
            "adapter_source",
            "parse_errors",
        },
    )
    raw_claims = payload["claims"]
    if not isinstance(raw_claims, list):
        raise ValueError("answer.claims must be a list")
    claims: list[AnswerClaim] = []
    for index, raw_claim in enumerate(raw_claims):
        claim = _exact_mapping(
            f"answer.claims[{index}]",
            raw_claim,
            {"claim_id", "text", "source_ids"},
        )
        claims.append(
            AnswerClaim(
                claim_id=_string(f"answer.claims[{index}].claim_id", claim["claim_id"]),
                text=_string(f"answer.claims[{index}].text", claim["text"]),
                source_ids=_string_list(
                    f"answer.claims[{index}].source_ids", claim["source_ids"]
                ),
            )
        )
    return StructuredAnswer(
        answer_text=_string("answer.answer_text", payload["answer_text"]),
        answer_mode=_string("answer.answer_mode", payload["answer_mode"]),
        claims=claims,
        limitations=_string_list("answer.limitations", payload["limitations"]),
        clarification_question=_optional_string(
            "answer.clarification_question", payload["clarification_question"]
        ),
        schema_valid=_boolean("answer.schema_valid", payload["schema_valid"]),
        adapter_source=_string("answer.adapter_source", payload["adapter_source"]),
        parse_errors=_string_list("answer.parse_errors", payload["parse_errors"]),
    )


def structured_answer_to_artifact(answer: StructuredAnswer) -> dict[str, Any]:
    return _artifact("structured_answer", _structured_answer_payload(answer))


def structured_answer_from_artifact(
    artifact: Mapping[str, Any],
) -> StructuredAnswer:
    return _structured_answer_from_payload(
        _artifact_payload("structured_answer", artifact)
    )


def _verification_result_payload(result: VerificationResult) -> dict[str, Any]:
    if not isinstance(result, VerificationResult):
        raise ValueError("verification must be a VerificationResult")
    payload = {
        field.name: getattr(result, field.name) for field in fields(VerificationResult)
    }
    restored = _verification_result_from_payload(payload)
    return _json_copy(
        {
            field.name: getattr(restored, field.name)
            for field in fields(VerificationResult)
        }
    )


def _verification_result_from_payload(value: Any) -> VerificationResult:
    expected_fields = {field.name for field in fields(VerificationResult)}
    payload = _exact_mapping("verification result", value, expected_fields)
    semantic_support_status = _string(
        "verification.semantic_support_status",
        payload["semantic_support_status"],
        non_empty=True,
    )
    if semantic_support_status not in SEMANTIC_SUPPORT_STATUSES:
        raise ValueError("verification.semantic_support_status is unsupported")
    expected_answer_mode = _optional_answer_mode(
        "verification.expected_answer_mode", payload["expected_answer_mode"]
    )
    return VerificationResult(
        passed=_boolean("verification.passed", payload["passed"]),
        schema_valid=_boolean("verification.schema_valid", payload["schema_valid"]),
        evidence_catalog_valid=_boolean(
            "verification.evidence_catalog_valid",
            payload["evidence_catalog_valid"],
        ),
        citation_ids_valid=_boolean(
            "verification.citation_ids_valid", payload["citation_ids_valid"]
        ),
        citation_alignment_valid=_boolean(
            "verification.citation_alignment_valid",
            payload["citation_alignment_valid"],
        ),
        evidence_scope_valid=_optional_boolean(
            "verification.evidence_scope_valid", payload["evidence_scope_valid"]
        ),
        disclaimer_present=_boolean(
            "verification.disclaimer_present", payload["disclaimer_present"]
        ),
        response_mode_valid=_boolean(
            "verification.response_mode_valid", payload["response_mode_valid"]
        ),
        semantic_support_status=semantic_support_status,
        expected_answer_mode=expected_answer_mode,
        actual_answer_mode=_string(
            "verification.actual_answer_mode",
            payload["actual_answer_mode"],
        ),
        refusal_required=_boolean(
            "verification.refusal_required", payload["refusal_required"]
        ),
        refusal_present=_boolean(
            "verification.refusal_present", payload["refusal_present"]
        ),
        answer_source_format=_string(
            "verification.answer_source_format",
            payload["answer_source_format"],
        ),
        schema_errors=_string_list(
            "verification.schema_errors", payload["schema_errors"]
        ),
        required_checks=_string_list(
            "verification.required_checks", payload["required_checks"]
        ),
        duplicate_source_ids=_string_list(
            "verification.duplicate_source_ids",
            payload["duplicate_source_ids"],
        ),
        missing_source_ids=_string_list(
            "verification.missing_source_ids", payload["missing_source_ids"]
        ),
        malformed_citation_tokens=_string_list(
            "verification.malformed_citation_tokens",
            payload["malformed_citation_tokens"],
        ),
        invalid_scope_citations=_string_list(
            "verification.invalid_scope_citations",
            payload["invalid_scope_citations"],
        ),
        cited_source_ids=_string_list(
            "verification.cited_source_ids", payload["cited_source_ids"]
        ),
        visible_source_ids=_string_list(
            "verification.visible_source_ids", payload["visible_source_ids"]
        ),
        claim_source_ids=_string_list(
            "verification.claim_source_ids", payload["claim_source_ids"]
        ),
        unsupported_claims=_string_list(
            "verification.unsupported_claims", payload["unsupported_claims"]
        ),
        refusal_correct=_optional_boolean(
            "verification.refusal_correct", payload["refusal_correct"]
        ),
        failure_reasons=_string_list(
            "verification.failure_reasons", payload["failure_reasons"]
        ),
    )


def verification_result_to_artifact(
    result: VerificationResult,
) -> dict[str, Any]:
    return _artifact("verification_result", _verification_result_payload(result))


def verification_result_from_artifact(
    artifact: Mapping[str, Any],
) -> VerificationResult:
    return _verification_result_from_payload(
        _artifact_payload("verification_result", artifact)
    )


_ADAPTIVE_PAYLOAD_FIELDS = {
    "results",
    "analysis",
    "normalized_query",
    "plans",
    "planner_trace",
    "merge_trace",
    "evidence_check",
    "followup_trace",
    "adaptive_used",
    "adaptive_enabled",
    "trigger_reasons",
}


def _adaptive_retrieval_payload(
    adaptive: AdaptiveRetrievalResult,
) -> dict[str, Any]:
    if not isinstance(adaptive, AdaptiveRetrievalResult):
        raise ValueError("adaptive result must be an AdaptiveRetrievalResult")
    if not isinstance(adaptive.evidence_check, EvidenceCheck):
        raise ValueError("adaptive result must contain an EvidenceCheck")
    payload = {
        "results": [search_result_to_artifact(result) for result in adaptive.results],
        "analysis": _query_analysis_payload(adaptive.analysis),
        "normalized_query": (
            _normalized_query_payload(adaptive.normalized_query)
            if adaptive.normalized_query is not None
            else None
        ),
        "plans": [_retrieval_plan_payload(plan) for plan in adaptive.plans],
        "planner_trace": _object("adaptive.planner_trace", adaptive.planner_trace),
        "merge_trace": _object("adaptive.merge_trace", adaptive.merge_trace),
        "evidence_check": _evidence_check_payload(adaptive.evidence_check),
        "followup_trace": _object("adaptive.followup_trace", adaptive.followup_trace),
        "adaptive_used": _boolean("adaptive.adaptive_used", adaptive.adaptive_used),
        "adaptive_enabled": _boolean(
            "adaptive.adaptive_enabled", adaptive.adaptive_enabled
        ),
        "trigger_reasons": _string_list(
            "adaptive.trigger_reasons", adaptive.trigger_reasons
        ),
    }
    return _json_copy(payload)


def _adaptive_retrieval_from_payload(
    value: Any,
) -> AdaptiveRetrievalResult:
    payload = _exact_mapping(
        "adaptive retrieval result", value, _ADAPTIVE_PAYLOAD_FIELDS
    )
    normalized_payload = payload["normalized_query"]
    normalized_query = (
        None
        if normalized_payload is None
        else _normalized_query_from_payload(normalized_payload)
    )
    raw_plans = payload["plans"]
    if not isinstance(raw_plans, list):
        raise ValueError("adaptive.plans must be a list")
    raw_results = payload["results"]
    if not isinstance(raw_results, list):
        raise ValueError("adaptive.results must be a list")
    evidence_check = _evidence_check_from_payload(payload["evidence_check"])
    return AdaptiveRetrievalResult(
        results=[search_result_from_artifact(result) for result in raw_results],
        analysis=_query_analysis_from_payload(payload["analysis"]),
        normalized_query=normalized_query,
        plans=[_retrieval_plan_from_payload(plan) for plan in raw_plans],
        planner_trace=_object("adaptive.planner_trace", payload["planner_trace"]),
        merge_trace=_object("adaptive.merge_trace", payload["merge_trace"]),
        evidence_check=evidence_check,
        followup_trace=_object("adaptive.followup_trace", payload["followup_trace"]),
        adaptive_used=_boolean("adaptive.adaptive_used", payload["adaptive_used"]),
        adaptive_enabled=_boolean(
            "adaptive.adaptive_enabled", payload["adaptive_enabled"]
        ),
        trigger_reasons=_string_list(
            "adaptive.trigger_reasons", payload["trigger_reasons"]
        ),
    )


def _prepared_semantic_payload(prepared: PreparedQuestion) -> dict[str, Any]:
    if not isinstance(prepared, PreparedQuestion):
        raise ValueError("prepared must be a PreparedQuestion")
    analysis = _query_analysis_payload(prepared.analysis)
    standalone_question = _string(
        "prepared.standalone_question", prepared.standalone_question
    )
    if analysis["original_query"] != standalone_question:
        raise ValueError("prepared analysis is not bound to its standalone question")
    if prepared.analysis != analyze_query(standalone_question):
        raise ValueError("prepared analysis does not match its standalone question")
    session_state = _exact_mapping(
        "prepared.session_state_before",
        prepared.session_state_before,
        {"schema_version", "memory"},
    )
    schema_version = session_state["schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != ASSISTANT_SESSION_STATE_SCHEMA_VERSION
    ):
        raise ValueError("prepared session state schema is unsupported")
    memory_token_limit = _integer(
        "prepared.memory_token_limit", prepared.memory_token_limit, minimum=1
    )
    memory = ConversationMemory(token_limit=memory_token_limit)
    memory_state = _object(
        "prepared.session_state_before.memory", session_state["memory"]
    )
    restored_messages = memory._validated_state_messages(memory_state)
    if tuple(restored_messages) != tuple(prepared.memory_messages_before):
        raise ValueError("prepared memory messages do not match its session state")
    memory_text = _string("prepared.memory_text", prepared.memory_text)
    if memory._render_messages(restored_messages) != memory_text:
        raise ValueError("prepared memory text does not match its session state")
    _integer("prepared.memory_revision", prepared.memory_revision)
    _integer("prepared.session_revision", prepared.session_revision)
    return {
        "original_question": _string(
            "prepared.original_question", prepared.original_question
        ),
        "standalone_question": standalone_question,
        "analysis": analysis,
        "memory_text": memory_text,
        "session_state_before": session_state,
    }


def prepared_question_to_artifact(prepared: PreparedQuestion) -> dict[str, Any]:
    return _artifact("prepared_question", _prepared_semantic_payload(prepared))


def prepared_question_from_artifact(
    artifact: Mapping[str, Any],
    *,
    assistant: LegalChatAssistant,
) -> PreparedQuestion:
    if not isinstance(assistant, LegalChatAssistant):
        raise ValueError("assistant must be a LegalChatAssistant")
    payload = _exact_mapping(
        "prepared question",
        _artifact_payload("prepared_question", artifact),
        {
            "original_question",
            "standalone_question",
            "analysis",
            "memory_text",
            "session_state_before",
        },
    )
    original_question = _string(
        "prepared.original_question", payload["original_question"]
    )
    standalone_question = _string(
        "prepared.standalone_question", payload["standalone_question"]
    )
    analysis = _query_analysis_from_payload(payload["analysis"])
    if analysis.original_query != standalone_question:
        raise ValueError("prepared analysis is not bound to its standalone question")
    return assistant.bind_prepared_question(
        original_question=original_question,
        standalone_question=standalone_question,
        analysis=analysis,
        memory_text=_string("prepared.memory_text", payload["memory_text"]),
        session_state_before=_object(
            "prepared.session_state_before", payload["session_state_before"]
        ),
    )


_LEGACY_RETRIEVED_PAYLOAD_FIELDS = {
    "prepared_sha256",
    "adaptive_result",
    "results",
    "evidence_check",
    "rejected_source_ids",
    "source_id_map",
    "terminal_kind",
    "terminal_answer",
    "terminal_expected_answer_mode",
}
_RETRIEVED_PAYLOAD_FIELDS = _LEGACY_RETRIEVED_PAYLOAD_FIELDS | {"retrieval_boundary"}


def _validate_scope_filtered_retrieval_mapping(
    *,
    adaptive: AdaptiveRetrievalResult,
    results: tuple[SearchResult, ...],
    source_id_map: dict[str, str],
    rejected_source_ids: list[str],
) -> None:
    if tuple(adaptive.results) != results:
        raise ValueError(
            "scope-filtered adaptive evidence must match retrieved results"
        )
    expected_filtered_ids = [f"S{index}" for index in range(1, len(results) + 1)]
    if sorted(source_id_map.values()) != sorted(expected_filtered_ids):
        raise ValueError("scope-filtered source map is not contiguous")
    if len(set(source_id_map.values())) != len(source_id_map):
        raise ValueError("scope-filtered source map values must be unique")
    if set(source_id_map).intersection(rejected_source_ids):
        raise ValueError("retrieved source map and rejected evidence overlap")
    for index, result in enumerate(results, start=1):
        filter_trace = _exact_mapping(
            f"retrieved.results[{index - 1}].trace.evidence_filter",
            result.trace.get("evidence_filter"),
            {"original_source_id", "filtered_source_id", "allowed"},
        )
        original_source_id = _string(
            "evidence_filter.original_source_id",
            filter_trace["original_source_id"],
            non_empty=True,
        )
        filtered_source_id = _string(
            "evidence_filter.filtered_source_id",
            filter_trace["filtered_source_id"],
            non_empty=True,
        )
        if (
            filter_trace["allowed"] is not True
            or result.rank != index
            or filtered_source_id != f"S{index}"
            or source_id_map.get(original_source_id) != filtered_source_id
        ):
            raise ValueError("scope-filtered result trace is invalid")
    merge_filter = _exact_mapping(
        "adaptive.merge_trace.evidence_filter",
        adaptive.merge_trace.get("evidence_filter"),
        {"rejected_original_source_ids", "source_id_map", "returned_count"},
    )
    if (
        _string_list(
            "adaptive.merge_trace.evidence_filter.rejected_original_source_ids",
            merge_filter["rejected_original_source_ids"],
        )
        != rejected_source_ids
        or _string_mapping(
            "adaptive.merge_trace.evidence_filter.source_id_map",
            merge_filter["source_id_map"],
        )
        != source_id_map
        or _integer(
            "adaptive.merge_trace.evidence_filter.returned_count",
            merge_filter["returned_count"],
        )
        != len(results)
    ):
        raise ValueError("adaptive scope-filter trace does not match retrieval")


def _validate_unfiltered_retrieval_mapping(
    *,
    adaptive: AdaptiveRetrievalResult,
    results: tuple[SearchResult, ...],
    source_id_map: dict[str, str],
    rejected_source_ids: list[str],
) -> None:
    original_source_ids = [f"S{result.rank}" for result in adaptive.results]
    ambiguous_source_ids = {
        source_id
        for source_id in original_source_ids
        if original_source_ids.count(source_id) > 1
    }
    if ambiguous_source_ids.intersection(source_id_map):
        raise ValueError("ambiguous adaptive evidence may not be source-mapped")
    if set(source_id_map).difference(original_source_ids):
        raise ValueError("retrieved source map references unknown adaptive evidence")
    if set(source_id_map).intersection(rejected_source_ids):
        raise ValueError("retrieved source map and rejected evidence overlap")
    expected_results = []
    expected_rejected: list[str] = []
    for result, original_source_id in zip(
        adaptive.results, original_source_ids, strict=True
    ):
        if original_source_id not in source_id_map:
            expected_rejected.append(original_source_id)
            continue
        filtered_source_id = f"S{len(expected_results) + 1}"
        if source_id_map[original_source_id] != filtered_source_id:
            raise ValueError("retrieved source map is not contiguous")
        expected_results.append(
            replace(
                result,
                rank=len(expected_results) + 1,
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
    if _ordered_unique(expected_rejected) != rejected_source_ids:
        raise ValueError("retrieved rejected evidence does not match adaptive evidence")
    if tuple(expected_results) != results:
        raise ValueError("retrieved results do not match the evidence filter mapping")


def _validate_retrieved_relationships(turn: RetrievedTurn) -> None:
    adaptive = turn.adaptive_result
    evidence = turn.evidence_check
    results = tuple(turn.results)
    boundary = turn.retrieval_boundary
    if boundary is not None and not isinstance(boundary, RetrievalBoundary):
        raise ValueError("retrieved boundary is invalid")
    try:
        assert_results_match_boundary(
            results, boundary, stage="retrieved turn artifact"
        )
    except (TypeError, ValueError, RuntimeError) as exc:
        raise ValueError("retrieved results violate their captured boundary") from exc
    if boundary is None and any(result.provenance is not None for result in results):
        raise ValueError("retrieved provenance requires a captured boundary")
    terminal_values = (
        turn.terminal_kind,
        turn.terminal_answer,
        turn.terminal_expected_answer_mode,
    )
    refusal_required = should_refuse_before_retrieval(turn.prepared.analysis.risk_flags)
    if adaptive is None:
        expected_refusal = programmatic_answer(
            build_risk_refusal_answer(turn.prepared.analysis.risk_flags),
            answer_mode="out_of_scope",
            limitations=["请求超出法律文本学习助手允许的帮助范围。"],
        )
        if (
            not refusal_required
            or results
            or evidence is not None
            or turn.rejected_source_ids
            or turn.source_id_map
            or turn.terminal_kind != "pre_retrieval_refusal"
            or turn.terminal_answer != expected_refusal
            or turn.terminal_expected_answer_mode != "out_of_scope"
        ):
            raise ValueError("pre-retrieval refusal contract is invalid")
        return
    if refusal_required:
        raise ValueError("retrieval may not bypass a required pre-retrieval refusal")
    if adaptive.analysis != turn.prepared.analysis:
        raise ValueError("adaptive analysis is not bound to the prepared question")
    if not isinstance(evidence, EvidenceCheck) or adaptive.evidence_check != evidence:
        raise ValueError("adaptive evidence is not bound to the retrieved turn")
    source_id_map = _string_mapping("retrieved.source_id_map", turn.source_id_map)
    rejected_source_ids = _string_list(
        "retrieved.rejected_source_ids", list(turn.rejected_source_ids)
    )
    for original_source_id, filtered_source_id in source_id_map.items():
        _source_id("retrieved.source_id_map key", original_source_id)
        _source_id("retrieved.source_id_map value", filtered_source_id)
    for index, source_id in enumerate(rejected_source_ids):
        _source_id(f"retrieved.rejected_source_ids[{index}]", source_id)
    if _ordered_unique(rejected_source_ids) != rejected_source_ids:
        raise ValueError("retrieved rejected source IDs must be ordered-unique")
    validator = (
        _validate_scope_filtered_retrieval_mapping
        if rejected_source_ids
        else _validate_unfiltered_retrieval_mapping
    )
    validator(
        adaptive=adaptive,
        results=results,
        source_id_map=source_id_map,
        rejected_source_ids=rejected_source_ids,
    )
    if not results or not evidence.sufficient:
        expected_mode = expected_limited_answer_mode(evidence)
        if (
            turn.terminal_kind != "evidence_limited"
            or turn.terminal_answer != build_limited_structured_answer(evidence)
            or turn.terminal_expected_answer_mode != expected_mode
        ):
            raise ValueError("limited-evidence retrieval contract is invalid")
    elif any(value is not None for value in terminal_values):
        raise ValueError("successful retrieval contains a terminal contract")


def retrieved_turn_to_artifact(turn: RetrievedTurn) -> dict[str, Any]:
    if not isinstance(turn, RetrievedTurn):
        raise ValueError("turn must be a RetrievedTurn")
    _validate_retrieved_relationships(turn)
    prepared_artifact = prepared_question_to_artifact(turn.prepared)
    results = tuple(turn.results)
    result_artifacts = [search_result_to_artifact(result) for result in results]
    adaptive_payload = (
        _adaptive_retrieval_payload(turn.adaptive_result)
        if turn.adaptive_result is not None
        else None
    )
    evidence_payload = (
        _evidence_check_payload(turn.evidence_check)
        if turn.evidence_check is not None
        else None
    )
    payload = {
        "prepared_sha256": canonical_hash(prepared_artifact),
        "adaptive_result": adaptive_payload,
        "results": result_artifacts,
        "evidence_check": evidence_payload,
        "retrieval_boundary": (
            _retrieval_boundary_to_payload(turn.retrieval_boundary)
            if turn.retrieval_boundary is not None
            else None
        ),
        "rejected_source_ids": _string_list(
            "retrieved.rejected_source_ids", list(turn.rejected_source_ids)
        ),
        "source_id_map": _string_mapping("retrieved.source_id_map", turn.source_id_map),
        "terminal_kind": _optional_string(
            "retrieved.terminal_kind", turn.terminal_kind
        ),
        "terminal_answer": (
            _structured_answer_payload(turn.terminal_answer)
            if turn.terminal_answer is not None
            else None
        ),
        "terminal_expected_answer_mode": _optional_answer_mode(
            "retrieved.terminal_expected_answer_mode",
            turn.terminal_expected_answer_mode,
        ),
    }
    return _artifact("retrieved_turn", payload)


def retrieved_turn_from_artifact(
    artifact: Mapping[str, Any],
    *,
    prepared: PreparedQuestion,
) -> RetrievedTurn:
    if not isinstance(prepared, PreparedQuestion):
        raise ValueError("prepared must be a PreparedQuestion")
    prepared_copy = deepcopy(prepared)
    raw_payload = _artifact_payload("retrieved_turn", artifact)
    payload_fields = set(raw_payload)
    if payload_fields == _RETRIEVED_PAYLOAD_FIELDS:
        payload = _exact_mapping(
            "retrieved turn", raw_payload, _RETRIEVED_PAYLOAD_FIELDS
        )
        serialized_boundary = (
            None
            if payload["retrieval_boundary"] is None
            else _retrieval_boundary_from_payload(payload["retrieval_boundary"])
        )
    elif payload_fields == _LEGACY_RETRIEVED_PAYLOAD_FIELDS:
        payload = _exact_mapping(
            "legacy retrieved turn", raw_payload, _LEGACY_RETRIEVED_PAYLOAD_FIELDS
        )
        serialized_boundary = None
    else:
        raise ValueError("retrieved turn fields are invalid")
    expected_prepared_hash = canonical_hash(
        prepared_question_to_artifact(prepared_copy)
    )
    prepared_hash = _digest("retrieved.prepared_sha256", payload["prepared_sha256"])
    if prepared_hash != expected_prepared_hash:
        raise ValueError("retrieved turn prepared artifact hash does not match")
    raw_results = payload["results"]
    if not isinstance(raw_results, list):
        raise ValueError("retrieved.results must be a list")
    results = tuple(search_result_from_artifact(result) for result in raw_results)
    raw_adaptive = payload["adaptive_result"]
    adaptive = (
        None if raw_adaptive is None else _adaptive_retrieval_from_payload(raw_adaptive)
    )
    raw_evidence = payload["evidence_check"]
    evidence = (
        None if raw_evidence is None else _evidence_check_from_payload(raw_evidence)
    )
    if adaptive is not None:
        if adaptive.analysis != prepared_copy.analysis:
            raise ValueError("adaptive analysis is not bound to the prepared question")
        if adaptive.evidence_check != evidence:
            raise ValueError("adaptive evidence is not bound to the retrieved turn")
    elif evidence is not None:
        raise ValueError("retrieved evidence requires an adaptive result")
    terminal_payload = payload["terminal_answer"]
    terminal_answer = (
        None
        if terminal_payload is None
        else _structured_answer_from_payload(terminal_payload)
    )
    restored = RetrievedTurn(
        prepared=prepared_copy,
        adaptive_result=adaptive,
        results=results,
        evidence_check=evidence,
        retrieval_boundary=(
            serialized_boundary
            if payload_fields == _RETRIEVED_PAYLOAD_FIELDS
            else _retrieval_boundary_from_results(results)
        ),
        rejected_source_ids=tuple(
            _string_list(
                "retrieved.rejected_source_ids", payload["rejected_source_ids"]
            )
        ),
        source_id_map=_string_mapping(
            "retrieved.source_id_map", payload["source_id_map"]
        ),
        terminal_kind=_optional_string(
            "retrieved.terminal_kind", payload["terminal_kind"]
        ),
        terminal_answer=terminal_answer,
        terminal_expected_answer_mode=_optional_answer_mode(
            "retrieved.terminal_expected_answer_mode",
            payload["terminal_expected_answer_mode"],
        ),
    )
    _validate_retrieved_relationships(restored)
    return restored


def _retrieval_boundary_from_results(
    results: tuple[SearchResult, ...],
) -> RetrievalBoundary | None:
    boundaries = {
        result.provenance.boundary
        for result in results
        if result.provenance is not None
    }
    if not boundaries:
        return None
    if len(boundaries) != 1 or any(result.provenance is None for result in results):
        raise ValueError("retrieved results contain mixed retrieval boundaries")
    return next(iter(boundaries))


def _strip_search_result_provenance(value: Any) -> None:
    """Convert current nested SearchResult artifacts to their M2 shape."""

    if isinstance(value, dict):
        result_payload = value.get("search_result")
        if set(value) == {"artifact_schema_version", "search_result"} and isinstance(
            result_payload, dict
        ):
            result_payload.pop("provenance", None)
        for nested in value.values():
            _strip_search_result_provenance(nested)
    elif isinstance(value, list):
        for nested in value:
            _strip_search_result_provenance(nested)


def _retrieved_artifact_hash_candidates(turn: RetrievedTurn) -> set[str]:
    current = retrieved_turn_to_artifact(turn)
    candidates = {canonical_hash(current)}
    adaptive_results = (
        () if turn.adaptive_result is None else tuple(turn.adaptive_result.results)
    )
    if turn.retrieval_boundary is not None or any(
        result.provenance is not None
        for result in (*tuple(turn.results), *adaptive_results)
    ):
        return candidates
    without_boundary = deepcopy(current)
    without_boundary["payload"].pop("retrieval_boundary", None)
    m2_legacy = deepcopy(without_boundary)
    _strip_search_result_provenance(m2_legacy)
    candidates.update({canonical_hash(without_boundary), canonical_hash(m2_legacy)})
    return candidates


_GENERATED_PAYLOAD_FIELDS = {
    "retrieved_sha256",
    "kind",
    "answer_text",
    "answer",
    "generation_error",
    "expected_answer_mode",
    "raw_response",
    "parser_version",
}


def _raw_response_payload(
    raw_response: str | StructuredAnswer | None,
) -> dict[str, Any]:
    if raw_response is None:
        return {"kind": "none", "value": None}
    if isinstance(raw_response, str):
        return {
            "kind": "text",
            "value": _string("generated.raw_response.value", raw_response),
        }
    if isinstance(raw_response, StructuredAnswer):
        return {
            "kind": "structured_answer",
            "value": _structured_answer_payload(raw_response),
        }
    raise ValueError("generated raw_response has an unsupported type")


def _raw_response_from_payload(
    value: Any,
) -> str | StructuredAnswer | None:
    payload = _exact_mapping("generated.raw_response", value, {"kind", "value"})
    kind = _string("generated.raw_response.kind", payload["kind"], non_empty=True)
    raw_value = payload["value"]
    if kind == "none":
        if raw_value is not None:
            raise ValueError("generated raw_response none value must be null")
        return None
    if kind == "text":
        return _string("generated.raw_response.value", raw_value)
    if kind == "structured_answer":
        return _structured_answer_from_payload(raw_value)
    raise ValueError("generated raw_response kind is unsupported")


def _validate_generated_relationships(turn: GeneratedTurn) -> None:
    retrieved = turn.retrieved
    terminal_kinds = {"pre_retrieval_refusal", "evidence_limited"}
    if turn.kind == "retrieval_only":
        expected_text = append_disclaimer(
            render_retrieval_only_answer(list(retrieved.results))
        )
        if (
            any(
                value is not None
                for value in (
                    retrieved.terminal_kind,
                    retrieved.terminal_answer,
                    retrieved.terminal_expected_answer_mode,
                    turn.answer,
                    turn.generation_error,
                    turn.expected_answer_mode,
                    turn.raw_response,
                    turn.parser_version,
                )
            )
            or turn.answer_text != expected_text
        ):
            raise ValueError("retrieval-only generated turn contract is invalid")
        return
    if turn.answer is None or turn.answer_text != turn.answer.answer_text:
        raise ValueError("generated answer contract is incomplete")
    if turn.kind in terminal_kinds:
        if (
            retrieved.terminal_kind != turn.kind
            or retrieved.terminal_answer != turn.answer
            or retrieved.terminal_expected_answer_mode != turn.expected_answer_mode
            or turn.generation_error is not None
            or turn.raw_response is not None
            or turn.parser_version is not None
        ):
            raise ValueError("terminal generated turn contract is invalid")
        return
    if any(
        value is not None
        for value in (
            retrieved.terminal_kind,
            retrieved.terminal_answer,
            retrieved.terminal_expected_answer_mode,
        )
    ):
        raise ValueError("non-terminal generated turn contains a terminal contract")
    if turn.kind == "generation_error":
        expected_error_answer = programmatic_answer(
            "当前无法连接生成模型，因此无法给出可靠结论。",
            answer_mode="insufficient_evidence",
            limitations=["生成模型当前不可用。"],
        )
        if (
            retrieved.adaptive_result is None
            or retrieved.evidence_check is None
            or turn.answer != expected_error_answer
            or turn.generation_error != "generation_error"
            or turn.expected_answer_mode != "insufficient_evidence"
            or turn.raw_response is not None
            or turn.parser_version is not None
        ):
            raise ValueError("generation-error turn contract is invalid")
        return
    if turn.kind == "model":
        if (
            retrieved.adaptive_result is None
            or retrieved.evidence_check is None
            or turn.generation_error is not None
            or turn.expected_answer_mode != "evidence_answer"
            or turn.parser_version != STRUCTURED_ANSWER_PARSER_VERSION
        ):
            raise ValueError("model generated turn contract is invalid")
        reparsed = parse_structured_answer(turn.raw_response)
        reparsed = StructuredAnswer(
            answer_text=append_disclaimer(reparsed.answer_text),
            answer_mode=reparsed.answer_mode,
            claims=reparsed.claims,
            limitations=reparsed.limitations,
            clarification_question=reparsed.clarification_question,
            schema_valid=reparsed.schema_valid,
            adapter_source=reparsed.adapter_source,
            parse_errors=reparsed.parse_errors,
        )
        if reparsed != turn.answer:
            raise ValueError("model answer does not match its raw response")
        return
    raise ValueError(f"generated turn kind is unsupported: {turn.kind!r}")


def generated_turn_to_artifact(turn: GeneratedTurn) -> dict[str, Any]:
    if not isinstance(turn, GeneratedTurn):
        raise ValueError("turn must be a GeneratedTurn")
    _validate_generated_relationships(turn)
    retrieved_artifact = retrieved_turn_to_artifact(turn.retrieved)
    payload = {
        "retrieved_sha256": canonical_hash(retrieved_artifact),
        "kind": _string("generated.kind", turn.kind, non_empty=True),
        "answer_text": _string("generated.answer_text", turn.answer_text),
        "answer": (
            _structured_answer_payload(turn.answer) if turn.answer is not None else None
        ),
        "generation_error": _optional_string(
            "generated.generation_error", turn.generation_error
        ),
        "expected_answer_mode": _optional_answer_mode(
            "generated.expected_answer_mode", turn.expected_answer_mode
        ),
        "raw_response": _raw_response_payload(turn.raw_response),
        "parser_version": _optional_string(
            "generated.parser_version", turn.parser_version
        ),
    }
    return _artifact("generated_turn", payload)


def generated_turn_from_artifact(
    artifact: Mapping[str, Any],
    *,
    retrieved: RetrievedTurn,
) -> GeneratedTurn:
    if not isinstance(retrieved, RetrievedTurn):
        raise ValueError("retrieved must be a RetrievedTurn")
    retrieved_copy = deepcopy(retrieved)
    payload = _exact_mapping(
        "generated turn",
        _artifact_payload("generated_turn", artifact),
        _GENERATED_PAYLOAD_FIELDS,
    )
    retrieved_hash = _digest("generated.retrieved_sha256", payload["retrieved_sha256"])
    if retrieved_hash not in _retrieved_artifact_hash_candidates(retrieved_copy):
        raise ValueError("generated turn retrieved artifact hash does not match")
    raw_answer = payload["answer"]
    answer = None if raw_answer is None else _structured_answer_from_payload(raw_answer)
    answer_text = _string("generated.answer_text", payload["answer_text"])
    if answer is not None and answer_text != answer.answer_text:
        raise ValueError("generated answer text does not match its envelope")
    restored = GeneratedTurn(
        retrieved=retrieved_copy,
        kind=_string("generated.kind", payload["kind"], non_empty=True),
        answer_text=answer_text,
        answer=answer,
        generation_error=_optional_string(
            "generated.generation_error", payload["generation_error"]
        ),
        expected_answer_mode=_optional_answer_mode(
            "generated.expected_answer_mode", payload["expected_answer_mode"]
        ),
        raw_response=_raw_response_from_payload(payload["raw_response"]),
        parser_version=_optional_string(
            "generated.parser_version", payload["parser_version"]
        ),
    )
    _validate_generated_relationships(restored)
    return restored


_VERIFIED_PAYLOAD_FIELDS = {
    "generated_sha256",
    "answer_text",
    "final_answer",
    "verification",
    "pre_fallback_answer",
    "pre_fallback_verification",
}


def _validate_verified_relationships(turn: VerifiedTurn) -> None:
    if turn.final_answer is None:
        if (
            turn.generated.kind != "retrieval_only"
            or turn.answer_text != turn.generated.answer_text
        ):
            raise ValueError("verified retrieval-only answer text is invalid")
        if any(
            value is not None
            for value in (
                turn.verification,
                turn.pre_fallback_answer,
                turn.pre_fallback_verification,
            )
        ):
            raise ValueError("verified result without a final answer has extra data")
        return
    if turn.answer_text != turn.final_answer.answer_text:
        raise ValueError("verified answer text does not match its final answer")
    if turn.verification is None or not turn.verification.passed:
        raise ValueError("verified final answer requires a passing verification")
    if (
        turn.verification.actual_answer_mode != turn.final_answer.answer_mode
        or turn.verification.answer_source_format != turn.final_answer.adapter_source
        or turn.verification.schema_valid != turn.final_answer.schema_valid
    ):
        raise ValueError("verified final answer is not bound to its verification")
    if (turn.pre_fallback_answer is None) != (turn.pre_fallback_verification is None):
        raise ValueError("verified pre-fallback fields must be present together")
    if turn.pre_fallback_verification is not None:
        if (
            turn.generated.kind != "model"
            or turn.pre_fallback_answer != turn.generated.answer
            or turn.pre_fallback_verification.passed
            or turn.pre_fallback_verification.actual_answer_mode
            != turn.pre_fallback_answer.answer_mode
            or turn.pre_fallback_verification.answer_source_format
            != turn.pre_fallback_answer.adapter_source
        ):
            raise ValueError("verified pre-fallback data is invalid")
    elif turn.generated.kind == "generation_error":
        if turn.final_answer != safe_terminal_answer("insufficient_evidence"):
            raise ValueError("generation-error final answer is invalid")
    elif turn.final_answer != turn.generated.answer:
        raise ValueError("verified final answer is not bound to its generated answer")


def verified_turn_to_artifact(turn: VerifiedTurn) -> dict[str, Any]:
    if not isinstance(turn, VerifiedTurn):
        raise ValueError("turn must be a VerifiedTurn")
    _validate_verified_relationships(turn)
    generated_artifact = generated_turn_to_artifact(turn.generated)
    payload = {
        "generated_sha256": canonical_hash(generated_artifact),
        "answer_text": _string("verified.answer_text", turn.answer_text),
        "final_answer": (
            _structured_answer_payload(turn.final_answer)
            if turn.final_answer is not None
            else None
        ),
        "verification": (
            _verification_result_payload(turn.verification)
            if turn.verification is not None
            else None
        ),
        "pre_fallback_answer": (
            _structured_answer_payload(turn.pre_fallback_answer)
            if turn.pre_fallback_answer is not None
            else None
        ),
        "pre_fallback_verification": (
            _verification_result_payload(turn.pre_fallback_verification)
            if turn.pre_fallback_verification is not None
            else None
        ),
    }
    return _artifact("verified_turn", payload)


def _generated_artifact_hash_candidates(turn: GeneratedTurn) -> set[str]:
    current = generated_turn_to_artifact(turn)
    candidates: set[str] = set()
    for retrieved_hash in _retrieved_artifact_hash_candidates(turn.retrieved):
        candidate = deepcopy(current)
        candidate["payload"]["retrieved_sha256"] = retrieved_hash
        candidates.add(canonical_hash(candidate))
    return candidates


def verified_turn_from_artifact(
    artifact: Mapping[str, Any],
    *,
    generated: GeneratedTurn,
) -> VerifiedTurn:
    if not isinstance(generated, GeneratedTurn):
        raise ValueError("generated must be a GeneratedTurn")
    generated_copy = deepcopy(generated)
    payload = _exact_mapping(
        "verified turn",
        _artifact_payload("verified_turn", artifact),
        _VERIFIED_PAYLOAD_FIELDS,
    )
    generated_hash = _digest("verified.generated_sha256", payload["generated_sha256"])
    if generated_hash not in _generated_artifact_hash_candidates(generated_copy):
        raise ValueError("verified turn generated artifact hash does not match")
    final_answer = (
        None
        if payload["final_answer"] is None
        else _structured_answer_from_payload(payload["final_answer"])
    )
    verification = (
        None
        if payload["verification"] is None
        else _verification_result_from_payload(payload["verification"])
    )
    pre_fallback_answer = (
        None
        if payload["pre_fallback_answer"] is None
        else _structured_answer_from_payload(payload["pre_fallback_answer"])
    )
    pre_fallback_verification = (
        None
        if payload["pre_fallback_verification"] is None
        else _verification_result_from_payload(payload["pre_fallback_verification"])
    )
    restored = VerifiedTurn(
        generated=generated_copy,
        answer_text=_string("verified.answer_text", payload["answer_text"]),
        final_answer=final_answer,
        verification=verification,
        pre_fallback_answer=pre_fallback_answer,
        pre_fallback_verification=pre_fallback_verification,
    )
    _validate_verified_relationships(restored)
    return restored
