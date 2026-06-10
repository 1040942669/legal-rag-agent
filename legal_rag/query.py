from __future__ import annotations

import re
from dataclasses import asdict, dataclass


ARTICLE_NUMBER_RE = re.compile(r"第[零一二三四五六七八九十百千万壹贰叁肆伍陆柒捌玖拾佰仟\d]+条")
EXPLICIT_LAW_RE = re.compile(r"《([^》]+)》")

COMMON_LAW_HINTS = [
    "中华人民共和国民法典",
    "中华人民共和国宪法",
    "中华人民共和国刑法",
    "中华人民共和国民事诉讼法",
    "中华人民共和国刑事诉讼法",
    "中华人民共和国行政诉讼法",
    "中华人民共和国消费者权益保护法",
    "中华人民共和国个人信息保护法",
    "中华人民共和国数据安全法",
    "中华人民共和国网络安全法",
    "中华人民共和国劳动法",
    "中华人民共和国劳动合同法",
    "中华人民共和国食品安全法",
    "中华人民共和国广告法",
    "中华人民共和国电子商务法",
    "中华人民共和国道路交通安全法",
    "民法典",
    "宪法",
    "刑法",
    "消费者权益保护法",
    "个人信息保护法",
    "数据安全法",
    "网络安全法",
    "劳动法",
    "劳动合同法",
    "食品安全法",
    "广告法",
    "电子商务法",
    "道路交通安全法",
]

EMOTIONAL_WORDS = [
    "气死",
    "崩溃",
    "太过分",
    "坑人",
    "骗子",
    "救命",
    "急",
    "愤怒",
]
CASE_STRATEGY_WORDS = [
    "胜诉",
    "败诉",
    "怎么打官司",
    "怎么起诉",
    "赔多少钱",
    "判几年",
    "律师建议",
    "具体怎么办",
]
ILLEGAL_HELP_WORDS = [
    "伪造",
    "洗钱",
    "逃避监管",
    "规避执法",
    "怎么不被发现",
]
VAGUE_WORDS = [
    "这个",
    "这种",
    "该怎么",
    "有没有依据",
    "哪些规定",
    "相关法律",
]
MULTI_INTENT_WORDS = [
    "分别",
    "同时",
    "以及",
    "还有",
    "另外",
    "一方面",
    "另一方面",
]
CONTRADICTION_WORDS = [
    "但是",
    "可是",
    "又说",
    "前后不一致",
    "矛盾",
    "冲突",
]


@dataclass(frozen=True)
class QueryAnalysis:
    original_query: str
    normalized_query: str
    law_names: list[str]
    article_numbers: list[str]
    case_type_hints: list[str]
    risk_flags: list[str]
    complexity_flags: list[str]
    adaptive_reasons: list[str]
    char_length: int
    token_count: int
    confidence: float

    def to_dict(self) -> dict:
        return asdict(self)


def analyze_query(query: str) -> QueryAnalysis:
    normalized = re.sub(r"\s+", " ", query.strip())
    law_names = extract_law_names(normalized)
    article_numbers = extract_article_numbers(normalized)
    risk_flags = detect_risk_flags(normalized)
    complexity_flags = detect_complexity_flags(normalized, law_names, article_numbers, risk_flags)
    case_type_hints = infer_case_type_hints(normalized, law_names, article_numbers, complexity_flags)
    confidence = estimate_confidence(law_names, article_numbers, complexity_flags, risk_flags)
    adaptive_reasons = [
        flag
        for flag in complexity_flags
        if flag in {"vague", "contradictory", "emotional", "too_long", "multi_intent", "low_confidence"}
    ]
    if confidence < 0.45 and "low_confidence" not in adaptive_reasons:
        adaptive_reasons.append("low_confidence")

    return QueryAnalysis(
        original_query=query,
        normalized_query=normalized,
        law_names=law_names,
        article_numbers=article_numbers,
        case_type_hints=case_type_hints,
        risk_flags=risk_flags,
        complexity_flags=complexity_flags,
        adaptive_reasons=adaptive_reasons,
        char_length=len(normalized),
        token_count=count_query_tokens(normalized),
        confidence=confidence,
    )


def extract_article_numbers(text: str) -> list[str]:
    return unique(ARTICLE_NUMBER_RE.findall(text))


def extract_law_names(text: str) -> list[str]:
    names = EXPLICIT_LAW_RE.findall(text)
    for hint in COMMON_LAW_HINTS:
        if hint in text:
            names.append(hint)
    return unique(names)


def detect_risk_flags(text: str) -> list[str]:
    flags: list[str] = []
    if any(word in text for word in EMOTIONAL_WORDS):
        flags.append("emotional")
    if any(word in text for word in CASE_STRATEGY_WORDS):
        flags.append("case_strategy")
    if any(word in text for word in ILLEGAL_HELP_WORDS):
        flags.append("illegal_help")
    if "!" in text or "！" in text:
        flags.append("emotional")
    return unique(flags)


def detect_complexity_flags(
    text: str,
    law_names: list[str],
    article_numbers: list[str],
    risk_flags: list[str],
) -> list[str]:
    flags: list[str] = []
    if len(text) > 160:
        flags.append("too_long")
    if len(article_numbers) > 1 or any(word in text for word in MULTI_INTENT_WORDS):
        flags.append("multi_intent")
    if any(word in text for word in CONTRADICTION_WORDS):
        flags.append("contradictory")
    if "emotional" in risk_flags:
        flags.append("emotional")
    if not law_names and not article_numbers and any(word in text for word in VAGUE_WORDS):
        flags.append("vague")
    if not law_names and not article_numbers and len(text) < 18:
        flags.append("vague")
    if not law_names and not article_numbers and len(text) > 80:
        flags.append("low_confidence")
    return unique(flags)


def infer_case_type_hints(
    text: str,
    law_names: list[str],
    article_numbers: list[str],
    complexity_flags: list[str],
) -> list[str]:
    hints: list[str] = []
    if law_names or article_numbers:
        hints.append("article_lookup")
    if "multi_intent" in complexity_flags:
        hints.append("multi_article")
    if not hints and len(text) >= 18:
        hints.append("semantic_scenario")
    if "vague" in complexity_flags:
        hints.append("needs_clarification")
    return unique(hints or ["unknown"])


def estimate_confidence(
    law_names: list[str],
    article_numbers: list[str],
    complexity_flags: list[str],
    risk_flags: list[str],
) -> float:
    score = 0.35
    if law_names:
        score += 0.25
    if article_numbers:
        score += 0.25
    if "multi_intent" in complexity_flags:
        score -= 0.10
    if "too_long" in complexity_flags:
        score -= 0.10
    if "vague" in complexity_flags:
        score -= 0.15
    if risk_flags:
        score -= 0.05
    return round(min(max(score, 0.0), 1.0), 2)


def count_query_tokens(text: str) -> int:
    chinese_chars = re.findall(r"[\u4e00-\u9fff]", text)
    ascii_terms = re.findall(r"[a-zA-Z0-9_]+", text)
    return len(chinese_chars) + len(ascii_terms)


def unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
