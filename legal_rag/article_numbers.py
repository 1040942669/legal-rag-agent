"""Conversion between Chinese statute article numbers and integers.

The corpus writes article numbers as ``第一百一十条``/``第一千零一条``.  Users
also type ``第110条``.  These helpers give every caller one canonical mapping
instead of ad-hoc regular expressions.
"""

from __future__ import annotations

import re
import unicodedata


CHINESE_DIGITS = "零一二三四五六七八九"
_DIGIT_VALUES = {char: index for index, char in enumerate(CHINESE_DIGITS)}
_DIGIT_VALUES.update(
    {
        "〇": 0,
        "两": 2,
        "壹": 1,
        "贰": 2,
        "叁": 3,
        "肆": 4,
        "伍": 5,
        "陆": 6,
        "柒": 7,
        "捌": 8,
        "玖": 9,
    }
)
_UNIT_VALUES = {
    "十": 10,
    "拾": 10,
    "百": 100,
    "佰": 100,
    "千": 1000,
    "仟": 1000,
}
MAX_ARTICLE_NUMBER = 9999
ARTICLE_NUMBER_TEXT_RE = re.compile(r"^第\s*(?P<number>[^\s条]+)\s*条$")


def chinese_number_to_int(text: str) -> int:
    """Parse ``二百六十四``/``264``/``十``/``一千零一`` into an integer."""

    value = unicodedata.normalize("NFKC", text).strip()
    if not value:
        raise ValueError("number text must not be empty")
    if value.isdigit() and value.isascii():
        return int(value)
    total = 0
    pending: int | None = None
    for char in value:
        if char in _DIGIT_VALUES:
            if pending:
                raise ValueError(f"consecutive digits in Chinese number: {text!r}")
            pending = _DIGIT_VALUES[char]
            continue
        unit = _UNIT_VALUES.get(char)
        if unit is None:
            raise ValueError(f"unsupported character in Chinese number: {char!r}")
        if pending is None:
            # Only a leading 十 may omit its digit: 十五 == 一十五.
            if unit != 10 or total != 0:
                raise ValueError(f"unit without digit in Chinese number: {text!r}")
            pending = 1
        elif pending == 0:
            raise ValueError(f"零 cannot precede a unit: {text!r}")
        total += pending * unit
        pending = None
    return total + (pending or 0)


def int_to_chinese_number(value: int) -> str:
    """Format ``110`` as ``一百一十`` and ``1001`` as ``一千零一``."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("article number must be an integer")
    if not 1 <= value <= MAX_ARTICLE_NUMBER:
        raise ValueError(f"article number out of range: {value}")
    if value < 10:
        return CHINESE_DIGITS[value]
    if value < 20:
        return "十" + (CHINESE_DIGITS[value - 10] if value > 10 else "")
    parts: list[str] = []
    zero_pending = False
    for unit_value, unit_char in ((1000, "千"), (100, "百"), (10, "十"), (1, "")):
        digit = value // unit_value % 10
        if digit == 0:
            if parts:
                zero_pending = True
            continue
        if zero_pending:
            parts.append("零")
            zero_pending = False
        parts.append(CHINESE_DIGITS[digit] + unit_char)
    return "".join(parts)


def article_number_to_int(article_number: str) -> int:
    """Parse ``第二百六十四条`` or ``第264条`` into ``264``."""

    match = ARTICLE_NUMBER_TEXT_RE.match(article_number.strip())
    if match is None:
        raise ValueError(f"not an article number: {article_number!r}")
    number = chinese_number_to_int(match.group("number"))
    if not 1 <= number <= MAX_ARTICLE_NUMBER:
        raise ValueError(f"article number out of range: {article_number!r}")
    return number


def format_article_number(value: int) -> str:
    """Render the corpus spelling, e.g. ``264`` -> ``第二百六十四条``."""

    return f"第{int_to_chinese_number(value)}条"


def canonical_article_number(article_number: str) -> str:
    """Normalize any supported spelling to the corpus spelling."""

    return format_article_number(article_number_to_int(article_number))
