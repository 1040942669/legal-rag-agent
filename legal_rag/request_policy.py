"""One bounded routing decision. Word signals do not establish user intent.

This module performs no classification, model calls or safety certification.
Execution authority and evidence continue to come from their existing contracts.
"""
from __future__ import annotations

MODERN_EVIDENCE_RULES = "general-reference-v3"
LEGACY_EVIDENCE_RULES = frozenset({"general-reference-v2", "legacy-hints-and-return-v1"})
LEGACY_BLOCKING_FLAGS = frozenset({"case_strategy", "illegal_help", "medical_financial_advice", "non_legal"})
ADVISORY_FLAG_NAMES = {
    "case_strategy": "case_strategy_topic",
    "illegal_help": "sensitive_topic",
    "medical_financial_advice": "professional_topic",
    "non_legal": "domain_topic",
}
ADVISORY_FLAGS = frozenset(ADVISORY_FLAG_NAMES.values())


def modern_request_rules(evidence_rules_version: str) -> bool:
    if evidence_rules_version == MODERN_EVIDENCE_RULES:
        return True
    if evidence_rules_version in LEGACY_EVIDENCE_RULES:
        return False
    raise ValueError("unsupported request evidence rules")


def advisory_risk_flags(flags: list[str], *, evidence_rules_version: str) -> list[str]:
    if not modern_request_rules(evidence_rules_version):
        return list(flags)
    return list(dict.fromkeys(ADVISORY_FLAG_NAMES.get(flag, flag) for flag in flags))


def request_answer_mode(risk_flags: list[str], *, evidence_rules_version: str,
                        free_generation: bool) -> str | None:
    if type(free_generation) is not bool:
        raise ValueError("free_generation must be boolean")
    modern = modern_request_rules(evidence_rules_version)
    signals = set(risk_flags)
    if not modern:
        return "out_of_scope" if signals & LEGACY_BLOCKING_FLAGS else None
    # Accept old names defensively as advisory too; a normalizer cannot turn
    # an unknown purpose into free-generation permission through label choice.
    if free_generation and signals & (ADVISORY_FLAGS | LEGACY_BLOCKING_FLAGS):
        return "needs_clarification"
    # None means no lexical routing restriction, not safety/legality confirmed.
    return None
