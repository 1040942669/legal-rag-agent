from __future__ import annotations

import json
import math
import re
from typing import Protocol

from .json_utils import (
    reject_duplicate_object_pairs,
    reject_non_finite_json_constant,
    validate_json_unicode,
)
from .models import NormalizedQuery
from .provider_errors import should_propagate_controlled_error
from .query import QueryAnalysis, analyze_query, should_use_adaptive


NORMALIZED_QUERY_FIELDS = {
    "legal_questions",
    "missing_facts",
    "law_hints",
    "article_hints",
    "keywords",
    "risk_flags",
    "confidence",
}
MAX_NORMALIZER_RESPONSE_CHARS = 65_536

LAW_KEYWORD_HINTS: list[tuple[list[str], str]] = [
    (["劳动", "用人单位", "工资", "解除合同", "试用期"], "中华人民共和国劳动合同法"),
    (["消费者", "退货", "网购", "商家", "押金"], "中华人民共和国消费者权益保护法"),
    (["个人信息", "隐私", "姓名", "照片", "肖像"], "中华人民共和国民法典"),
    (["网络运营者", "数据", "个人信息", "平台"], "中华人民共和国网络安全法"),
    (["食品", "召回", "生产者", "安全"], "中华人民共和国食品安全法"),
    (["广告", "宣传", "虚假"], "中华人民共和国广告法"),
    (["电子商务", "电商", "平台", "押金"], "中华人民共和国电子商务法"),
    (["驾驶", "饮酒", "道路", "交通"], "中华人民共和国道路交通安全法"),
    (["合同", "民事", "未成年人", "人格权", "生态环境"], "中华人民共和国民法典"),
    (["犯罪", "判刑", "刑事"], "中华人民共和国刑法"),
]

STOP_WORDS = {
    "什么",
    "怎么",
    "哪些",
    "规定",
    "依据",
    "相关",
    "法律",
    "这个",
    "这种",
    "是否",
    "可以",
    "应该",
    "一下",
}


class CompletionClient(Protocol):
    def complete(self, prompt: str) -> str: ...


def normalize_query(
    query: str,
    *,
    analysis: QueryAnalysis | None = None,
    llm_client: CompletionClient | None = None,
    use_llm: bool = False,
    max_retries: int = 0,
) -> NormalizedQuery:
    analysis = analysis or analyze_query(query)
    if not should_use_adaptive(analysis):
        return fallback_normalized_query(query, analysis, source="rules:clear_query")
    if not use_llm or llm_client is None:
        return fallback_normalized_query(query, analysis, source="rules:fallback")

    prompt = build_normalizer_prompt(query, analysis)
    attempts = max(1, min(max_retries + 1, 2))
    errors: list[str] = []
    for _ in range(attempts):
        try:
            raw_response = llm_client.complete(prompt)
        except Exception as exc:  # Normalizer must never block retrieval by default.
            if should_propagate_controlled_error(exc, llm_client):
                raise
            errors.append("normalizer_provider_error")
            continue
        try:
            parsed = parse_normalized_query_json(
                raw_response,
                original_query=query,
                source="llm",
            )
            return enrich_normalized_query(parsed, analysis)
        except (TypeError, ValueError, RecursionError, OverflowError):
            errors.append("normalizer_invalid_response")

    return fallback_normalized_query(
        query,
        analysis,
        source="rules:llm_error",
        errors=errors,
        raw_response="",
    )


def parse_normalized_query_json(
    raw_text: str,
    *,
    original_query: str,
    source: str = "llm",
) -> NormalizedQuery:
    payload = validate_json_unicode(
        json.loads(
            extract_json_object(raw_text),
            object_pairs_hook=reject_duplicate_object_pairs,
            parse_constant=reject_non_finite_json_constant,
        )
    )
    if not isinstance(payload, dict):
        raise ValueError("Normalizer JSON must be an object.")

    missing = sorted(field for field in NORMALIZED_QUERY_FIELDS if field not in payload)
    if missing:
        raise ValueError(f"Missing normalized query field(s): {', '.join(missing)}")
    unknown = sorted(field for field in payload if field not in NORMALIZED_QUERY_FIELDS)
    if unknown:
        raise ValueError(f"Unknown normalized query field(s): {', '.join(unknown)}")

    legal_questions = require_string_list(payload, "legal_questions")
    missing_facts = require_string_list(payload, "missing_facts")
    law_hints = require_string_list(payload, "law_hints")
    article_hints = require_string_list(payload, "article_hints")
    keywords = require_string_list(payload, "keywords")
    risk_flags = require_string_list(payload, "risk_flags")
    confidence = require_confidence(payload["confidence"])

    if not legal_questions:
        raise ValueError("Normalized query must contain at least one legal question.")

    return NormalizedQuery(
        original_query=original_query,
        legal_questions=unique(legal_questions),
        missing_facts=unique(missing_facts),
        law_hints=unique(law_hints),
        article_hints=unique(article_hints),
        keywords=unique(keywords),
        risk_flags=unique(risk_flags),
        confidence=confidence,
        source=source,
        raw_response="",
    )


def fallback_normalized_query(
    query: str,
    analysis: QueryAnalysis,
    *,
    source: str = "rules:fallback",
    errors: list[str] | None = None,
    raw_response: str = "",
) -> NormalizedQuery:
    legal_questions = split_legal_questions(analysis.normalized_query)
    law_hints = unique(
        [*analysis.law_names, *suggest_law_hints(analysis.normalized_query)]
    )
    keywords = extract_keywords(analysis.normalized_query)
    missing_facts = infer_missing_facts(analysis)
    return NormalizedQuery(
        original_query=query,
        legal_questions=legal_questions or [analysis.normalized_query],
        missing_facts=missing_facts,
        law_hints=law_hints,
        article_hints=analysis.article_numbers,
        keywords=keywords,
        risk_flags=analysis.risk_flags,
        confidence=analysis.confidence,
        source=source,
        errors=errors or [],
        raw_response=raw_response,
    )


def enrich_normalized_query(
    normalized: NormalizedQuery, analysis: QueryAnalysis
) -> NormalizedQuery:
    return NormalizedQuery(
        original_query=normalized.original_query,
        legal_questions=unique(normalized.legal_questions),
        missing_facts=unique(
            [*normalized.missing_facts, *infer_missing_facts(analysis)]
        ),
        law_hints=unique(
            [
                *normalized.law_hints,
                *analysis.law_names,
                *suggest_law_hints(analysis.normalized_query),
            ]
        ),
        article_hints=unique([*normalized.article_hints, *analysis.article_numbers]),
        keywords=unique(
            [*normalized.keywords, *extract_keywords(analysis.normalized_query)]
        ),
        risk_flags=unique([*normalized.risk_flags, *analysis.risk_flags]),
        confidence=round(min(max(normalized.confidence, 0.0), 1.0), 2),
        source=normalized.source,
        errors=normalized.errors,
        raw_response=normalized.raw_response,
    )


def build_normalizer_prompt(query: str, analysis: QueryAnalysis) -> str:
    return f"""你是中国现行法律 RAG 的查询理解器，只输出严格 JSON。
任务: 把用户问题整理为有限数量的法律检索问题，不回答问题，不给法律意见。

输出 JSON 字段必须完整:
{{
  "legal_questions": ["用于检索的法律问题"],
  "missing_facts": ["缺失事实"],
  "law_hints": ["候选法律名称"],
  "article_hints": ["候选条文号"],
  "keywords": ["关键词"],
  "risk_flags": ["风险标记"],
  "confidence": 0.0
}}

约束:
1. legal_questions 最多 3 个。
2. law_hints 只写用户问题明确提到或可从场景强相关推断的法律。
3. 不要输出 Markdown、解释文字或代码块。

规则分析:
{json.dumps(analysis.to_dict(), ensure_ascii=False)}

用户问题:
{query}
"""


def extract_json_object(text: str) -> str:
    if not isinstance(text, str):
        raise TypeError("Normalizer response must be a string.")
    if len(text) > MAX_NORMALIZER_RESPONSE_CHARS:
        raise ValueError("Normalizer response exceeds the maximum accepted size.")
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?", "", stripped, flags=re.IGNORECASE).strip()
        stripped = re.sub(r"```$", "", stripped).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Normalizer response does not contain a JSON object.")
    return stripped[start : end + 1]


def require_string_list(payload: dict, key: str) -> list[str]:
    value = payload[key]
    if not isinstance(value, list):
        raise ValueError(f"Field `{key}` must be a list of strings.")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"Field `{key}` must be a list of strings.")
        item = item.strip()
        if item:
            result.append(item)
    return result


def require_confidence(value: object) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise ValueError("Field `confidence` must be a number.")
    return round(min(max(float(value), 0.0), 1.0), 2)


def split_legal_questions(text: str, *, max_questions: int = 5) -> list[str]:
    separators = r"(?:分别|同时|以及|还有|另外|一方面|另一方面|；|;|\n)"
    parts = [
        part.strip(" ，,。？?")
        for part in re.split(separators, text)
        if part.strip(" ，,。？?")
    ]
    if len(parts) <= 1:
        return [text.strip()]
    return unique(parts[:max_questions])


def suggest_law_hints(text: str) -> list[str]:
    hints: list[str] = []
    for keywords, law_name in LAW_KEYWORD_HINTS:
        if any(keyword in text for keyword in keywords):
            hints.append(law_name)
    return unique(hints)


def extract_keywords(text: str, *, max_keywords: int = 12) -> list[str]:
    terms: list[str] = []
    for match in re.findall(r"[\u4e00-\u9fff]{2,8}", text):
        if match in STOP_WORDS or any(stop in match for stop in STOP_WORDS):
            continue
        terms.append(match)
    for match in re.findall(r"[a-zA-Z0-9_]{2,}", text):
        terms.append(match)
    for keywords, _ in LAW_KEYWORD_HINTS:
        terms.extend(keyword for keyword in keywords if keyword in text)
    return unique(terms)[:max_keywords]


def infer_missing_facts(analysis: QueryAnalysis) -> list[str]:
    facts: list[str] = []
    if "vague" in analysis.complexity_flags:
        facts.append("需要补充具体事实或法律关系")
    if "contradictory" in analysis.complexity_flags:
        facts.append("描述中存在矛盾事实")
    if "case_strategy" in analysis.risk_flags:
        facts.append("系统只能检索法律文本，不能判断个案策略")
    return facts


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
