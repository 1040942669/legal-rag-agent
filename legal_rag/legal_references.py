"""Bounded, scene-independent reference relations, not legal applicability.

Only explicit titles or caller-supplied corpus titles establish a law identity.
Grammar exclusions are recorded; uncertain ownership is never a guessed pair.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


REFERENCE_RULES_VERSION = "legal-reference-v3"
LEGACY_REFERENCE_RULES_VERSION = "legal-reference-v2"
REFERENCE_SCHEMA_VERSION = 1
MAX_REFERENCE_QUERY_CHARS = 16_384
MAX_LAW_TITLE_CHARS = 255
MAX_LAW_TITLE_BRACKET_DEPTH = 8
_NUM = r"[零〇一二两三四五六七八九十百千万壹贰叁肆伍陆柒捌玖拾佰仟\d]+"
_ARTICLE = re.compile(rf"第\s*({_NUM})\s*条(?:之({_NUM}))?")
# Recognize the shape first, validate numeral grammar separately. Otherwise
# an unsupported explicit label disappears and becomes a law-only request.
_ARTICLE_LOOKING = re.compile(r"第[^条，,。；;!?！？\n《》]*条")
_NUMERAL_FRAGMENT = re.compile(rf"之[+\-]?(?:{_NUM}|[A-Za-z_亿億兆.])+")
_LIST = re.compile(rf"第\s*({_NUM})(?:\s*[、,，和及与]\s*(?:第\s*)?{_NUM})+\s*条")
_RANGE = re.compile(rf"第\s*{_NUM}\s*(?:至|到|[-~～])\s*(?:第\s*)?{_NUM}\s*条")
_STOP = re.compile(r"[。；;!?！？\n]")
_DIGITS = dict(zip("零〇一二两三四五六七八九壹贰叁肆伍陆柒捌玖", (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9, 1, 2, 3, 4, 5, 6, 7, 8, 9)))
_UNITS = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000, "万": 10000}
_FORMAL_NUMERALS = str.maketrans("〇两壹贰叁肆伍陆柒捌玖拾佰仟", "零二一二三四五六七八九十百千")
_CLAUSE = re.compile(r"[^，,。；;!?！？\n]+")
_REQUEST = re.compile(r"解释|查询|检索|核查|核对|判断|确认|验证|比较|对比|说明|是否|什么|对吗|正确吗|有效吗|适用吗")
_REFERENCE_ANAPHORA = re.compile(r"(?:这(?:种|个|些|条|项)?|那(?:种|个|些|条|项)?|该|上述|前述|前面|其)(?:[一二两三几多各所有]*)(?:引用|法条|条款|条文|规定|法律|依据|说法|主张)")
_HISTORICAL_MARKER = re.compile(r"此前|先前|以前|之前|历史|曾经|曾|过去")
_REPORTED_PREFIX = re.compile(r"(?:对方|他人|有人|他|她|" + _HISTORICAL_MARKER.pattern + r")[^，,。；;!?！？\n]*(?:引用|提到|援引|说过|主张)")
_UNCERTAIN_NEGATION = re.compile(r"不|没|未|无|非|别|勿")
_LAW_CONNECTOR = re.compile(r"(?:以及|或者|和|与|及|或|、|,)")
# This is a declared, bounded command syntax, not a discourse/intent model.
# No reporting verbs or scene vocabulary establish a user's selection.
_COMMAND = re.compile(r"解释|查询|检索|核查|核对|判断|确认|验证|比较|对比|说明")
_COMMAND_PREFIX = re.compile(r"(?:请|我(?:现在)?(?:只)?(?:要求|想|要)?|现在(?:只)?|只|并|再)*")
_REFERENCE_TRIVIA = re.compile(r"(?:\s|的|中|和|与|及|以及|或|或者|、|[,，（()）])*")
_QUESTION_PREFIX = re.compile(r"(?:\s|是|有|的|中|请|规定(?:了)?|分别|" + _LAW_CONNECTOR.pattern + r"|[（()）])*")


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _title_brackets(value: str) -> bool:
    """Validate balanced literal brackets; report a whole enclosing wrapper.

    An inner quoted law is part of the full title identity, not an alias for
    the containing document. No title vocabulary is needed for this boundary.
    """
    opened: list[int] = []
    first_close = None
    for index, character in enumerate(value):
        if character == "《":
            opened.append(index)
            if len(opened) > MAX_LAW_TITLE_BRACKET_DEPTH:
                raise ValueError("law title bracket depth exceeds supported bound")
        elif character == "》":
            if not opened or index == opened[-1] + 1:
                raise ValueError("law title brackets are invalid")
            opened.pop()
            if not opened and first_close is None:
                first_close = index
    if opened:
        raise ValueError("law title brackets are invalid")
    return value.startswith("《") and first_close == len(value) - 1


def canonical_law_title(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("law title must be text")
    result = "".join(value.split())
    if "\x00" in result or len(result) > MAX_LAW_TITLE_CHARS + 2 * MAX_LAW_TITLE_BRACKET_DEPTH + 7:
        raise ValueError("invalid canonical law title")
    # Remove only complete enclosing syntax, never the inner quoted identity.
    # Repeat to keep the canonical identity idempotent for codecs and aliases.
    enclosing = _title_brackets(result)
    while enclosing or result.startswith("中华人民共和国"):
        result = result[1:-1] if enclosing else result.removeprefix("中华人民共和国")
        enclosing = _title_brackets(result)
    if not result or len(result) > MAX_LAW_TITLE_CHARS:
        raise ValueError("invalid canonical law title")
    return result


def _quoted_title_items(query: str):
    """Yield only outermost spans, including opaque invalid spans as barriers."""
    cursor = 0
    while cursor < len(query):
        if query[cursor] not in "《》":
            cursor += 1
            continue
        start = cursor
        if query[cursor] == "》":
            cursor += 1
            yield start, cursor, None, "invalid_title_brackets"
            continue
        depth = 1
        cursor += 1
        while cursor < len(query) and query[cursor] != "\n" and depth:
            depth += (query[cursor] == "《") - (query[cursor] == "》")
            cursor += 1
        # An extra closing bracket invalidates the whole preceding spelling;
        # it cannot leave a valid inner law behind to own the following label.
        while cursor < len(query) and query[cursor] == "》":
            cursor += 1
        try:
            title = canonical_law_title(query[start:cursor])
            issue = None
        except ValueError:
            title, issue = None, "unsupported_title_shape"
        yield start, cursor, title, issue


def _number(text: str) -> int:
    if not text or len(text) > 32:
        raise ValueError("article numeral outside supported bound")
    if text.isascii() and text.isdigit():
        number = int(text)
    elif all(character in _DIGITS for character in text):
        number = int("".join(str(_DIGITS[character]) for character in text))
    else:
        section = total = digit = 0
        for character in text:
            if character in _DIGITS:
                digit = _DIGITS[character]
            elif character in _UNITS:
                unit = _UNITS[character]
                if unit == 10000:
                    total += (section + digit) * unit
                    section = digit = 0
                else:
                    section += (digit or 1) * unit
                    digit = 0
            else:
                raise ValueError("unsupported article numeral")
        number = total + section + digit
    if number <= 0 or number >= 100_000_000:
        raise ValueError("article numeral outside supported bound")
    if any(character in _UNITS for character in text):
        normalized = text.translate(_FORMAL_NUMERALS)
        canonical = _chinese(number)
        # Chinese unit notation must be a well-formed spelling of that number.
        # This rejects repeated/out-of-order units and silently dropped digits.
        accepted = {canonical, "一" + canonical if canonical.startswith("十") else canonical}
        if normalized not in accepted:
            raise ValueError("invalid article numeral grammar")
    return number


def _chinese(number: int, *, leading: bool = True) -> str:
    if number >= 10000:
        high, low = divmod(number, 10000)
        return _chinese(high, leading=leading) + "万" + ("零" if 0 < low < 1000 else "") + (_chinese(low, leading=False) if low else "")
    digits = "零一二三四五六七八九"
    result = ""
    pending_zero = False
    for unit, suffix in ((1000, "千"), (100, "百"), (10, "十"), (1, "")):
        value, number = divmod(number, unit)
        if value:
            if pending_zero:
                result += "零"
            result += digits[value] + suffix
            pending_zero = False
        elif result and number:
            pending_zero = True
    return result[1:] if leading and result.startswith("一十") else result


def canonical_article_number(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("article number must be text")
    compact = "".join(value.split())
    match = _ARTICLE.fullmatch(compact)
    if match is None:
        raise ValueError("invalid article label")
    suffix = "之" + _chinese(_number(match.group(2))) if match.group(2) else ""
    return "第" + _chinese(_number(match.group(1))) + "条" + suffix


def _span(value: Any) -> tuple[int, int]:
    if not isinstance(value, (tuple, list)) or len(value) != 2 or any(type(item) is not int for item in value):
        raise ValueError("reference span must contain two integers")
    resolved = tuple(value)
    if resolved[0] < 0 or resolved[1] <= resolved[0]:
        raise ValueError("reference span is invalid")
    return resolved  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class LawArticleRequirement:
    law_title: str
    article_number: str
    span: tuple[int, int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "law_title", canonical_law_title(self.law_title))
        object.__setattr__(self, "article_number", canonical_article_number(self.article_number))
        object.__setattr__(self, "span", _span(self.span))

    def to_dict(self) -> dict[str, Any]:
        return {"law_title": self.law_title, "article_number": self.article_number, "span": list(self.span)}


@dataclass(frozen=True, slots=True)
class ReferenceMention:
    law_title: str | None
    article_number: str | None
    span: tuple[int, int]
    disposition: str
    reason: str

    def __post_init__(self) -> None:
        if self.law_title is not None:
            object.__setattr__(self, "law_title", canonical_law_title(self.law_title))
        if self.article_number is not None:
            object.__setattr__(self, "article_number", canonical_article_number(self.article_number))
        object.__setattr__(self, "span", _span(self.span))
        if self.disposition not in {"requested", "unresolved", "excluded", "mentioned"}:
            raise ValueError("invalid reference disposition")
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("reference reason must be nonempty")

    def to_dict(self) -> dict[str, Any]:
        return {"law_title": self.law_title, "article_number": self.article_number,
                "span": list(self.span), "disposition": self.disposition, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class ReferenceAnalysis:
    query: str
    requirements: tuple[LawArticleRequirement, ...]
    unresolved: tuple[ReferenceMention, ...]
    excluded: tuple[ReferenceMention, ...]
    required_law_titles: tuple[str, ...]
    mentions: tuple[ReferenceMention, ...]
    known_law_titles: tuple[str, ...] = ()
    rules_version: str = REFERENCE_RULES_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.rules_version, str) or self.rules_version not in {REFERENCE_RULES_VERSION, LEGACY_REFERENCE_RULES_VERSION}:
            raise ValueError("reference rules are unsupported")
        if not isinstance(self.query, str) or len(self.query) > MAX_REFERENCE_QUERY_CHARS:
            raise ValueError("reference query must be bounded text")
        for field, kind in (("requirements", LawArticleRequirement), ("unresolved", ReferenceMention),
                            ("excluded", ReferenceMention), ("mentions", ReferenceMention)):
            items = getattr(self, field)
            if not isinstance(items, tuple) or any(not isinstance(item, kind) for item in items):
                raise ValueError(f"{field} must be an immutable typed tuple")
            if any(item.span[1] > len(self.query) for item in items):
                raise ValueError("reference span exceeds original query")
        for field in ("required_law_titles", "known_law_titles"):
            items = getattr(self, field)
            if not isinstance(items, tuple) or any(canonical_law_title(item) != item for item in items):
                raise ValueError(f"{field} must contain canonical immutable titles")

    def _payload(self) -> dict[str, Any]:
        return {"schema_version": REFERENCE_SCHEMA_VERSION, "rules_version": self.rules_version,
                "query": self.query, "query_hash": hashlib.sha256(self.query.encode("utf-8")).hexdigest(),
                "requirements": [item.to_dict() for item in self.requirements],
                "unresolved": [item.to_dict() for item in self.unresolved],
                "excluded": [item.to_dict() for item in self.excluded],
                "required_law_titles": list(self.required_law_titles),
                "mentions": [item.to_dict() for item in self.mentions],
                "known_law_titles": list(self.known_law_titles)}

    @property
    def fingerprint(self) -> str:
        return _hash(self._payload())

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "fingerprint": self.fingerprint}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ReferenceAnalysis:
        if not isinstance(payload, Mapping) or set(payload) != {
            "schema_version", "rules_version", "query", "query_hash", "requirements", "unresolved",
            "excluded", "required_law_titles", "mentions", "known_law_titles", "fingerprint"}:
            raise ValueError("reference artifact fields are invalid")
        if type(payload["schema_version"]) is not int or payload["schema_version"] != REFERENCE_SCHEMA_VERSION:
            raise ValueError("reference schema is unsupported")
        if not isinstance(payload["rules_version"], str) or payload["rules_version"] not in {REFERENCE_RULES_VERSION, LEGACY_REFERENCE_RULES_VERSION}:
            raise ValueError("reference rules are unsupported")
        if not isinstance(payload["known_law_titles"], list):
            raise ValueError("reference known titles must be an artifact array")
        if _hash({key: value for key, value in payload.items() if key != "fingerprint"}) != payload["fingerprint"]:
            raise ValueError("reference artifact fingerprint is invalid")
        # Reparse the bound original text instead of trusting serialized pairs.
        restored = parse_legal_references(payload["query"], known_law_titles=payload["known_law_titles"],
                                          rules_version=payload["rules_version"])
        if restored.to_dict() != dict(payload):
            raise ValueError("reference artifact does not match its original input")
        return restored


def _context(query: str, span: tuple[int, int]) -> tuple[str, str]:
    before = query[:span[0]]
    start = 0
    right = len(before)
    for separator in reversed(list(re.finditer(r"[，,。；;!?！？\n]", before))):
        # A comma inside a pure reference expression is a connector, not a
        # new selection scope. Masked earlier references retain the command
        # or opaque background prefix that owns the complete list.
        if separator.group(0) in ",，" and _REFERENCE_TRIVIA.fullmatch(before[separator.end():right]):
            right = separator.start()
            continue
        start = separator.end()
        break
    after = query[span[1]:]
    end = re.search(r"[，,。；;!?！？\n]", after)
    return query[start:span[0]], after[:end.start()] if end else after


def _disposition(query: str, span: tuple[int, int], syntax_query: str) -> tuple[str, str]:
    """Select only an explicit address or a bounded positive operation.

    Reference spellings are masked before examining operation syntax, so a
    request word in a document title or a previous pair is not an operator.
    Unknown surrounding prose is a mention, not an implicit hard request.
    """
    before, after = _context(syntax_query, span)
    before, after = before.strip(), after.strip()
    if re.search(r"(?:不是|并非)\s*(?:不要|无需|不用|不必|不)[^，,。；;!?！？\n]*$", before):
        return "unresolved", "nested_reference_negation"
    if re.search(r"(?:不是|而不是|并非|不要(?:查询|检索|解释|看)?|无需(?:查询|检索|解释)?|不用(?:查询|检索|解释)?|不(?:查询|检索|解释|讨论|看|按|依据))\s*$", before):
        return "excluded", "excluded_reference"
    if re.match(r"\s*(?:除外|不必解释|无需解释|不要解释)", after):
        return "excluded", "excluded_reference"
    command = _COMMAND.search(before)
    suffix_command = _COMMAND.search(after)
    negated_suffix_operation = (suffix_command is not None
                               and _UNCERTAIN_NEGATION.search(after[:suffix_command.start()]) is not None)
    if command is not None:
        if not _COMMAND_PREFIX.fullmatch(before[:command.start()].strip()):
            return "unresolved", "unresolved_reference_selection"
        if _UNCERTAIN_NEGATION.search(before[command.end():]) or negated_suffix_operation:
            return "unresolved", "conflicting_reference_selection"
        return "requested", "explicit_operation_selection"
    # Negating a following operation does not become positive merely because
    # its spelling contains a request word. It is kept without requiring it.
    if negated_suffix_operation:
        return "mentioned", "unselected_negated_operation"
    if _UNCERTAIN_NEGATION.search(before):
        return "unresolved", "unresolved_reference_selection"
    if _REFERENCE_TRIVIA.fullmatch(before) or re.fullmatch(r"(?:按|依据|根据)\s*", before):
        requested = _REQUEST.search(after)
        if requested and _QUESTION_PREFIX.fullmatch(after[:requested.start()]):
            return "requested", "explicit_question_selection"
        if _REFERENCE_TRIVIA.fullmatch(after):
            return "requested", "direct_reference_address"
    if _REQUEST.search(before) or _REQUEST.search(after):
        return "unresolved", "unresolved_reference_selection"
    return "mentioned", "reference_not_selected"


def _selection_syntax(query, laws, article_items):
    characters = list(query)
    for start, end in [(start, end) for start, end, _ in laws] + [
            (start, end) for start, end, _, _ in article_items]:
        characters[start:end] = " " * (end - start)
    return "".join(characters)


def _unresolved_anaphoric_requests(query, laws):
    """Do not infer discourse selection from arbitrary reports or history."""
    unresolved = []
    for clause in _CLAUSE.finditer(query):
        if (_REFERENCE_ANAPHORA.search(clause.group(0))
            and not any(clause.start() <= start < clause.end() for start, _, _ in laws)):
            unresolved.append(ReferenceMention(None, None, clause.span(), "unresolved",
                                               "unresolved_reference_anaphora"))
    return unresolved




def _bare_title_start_supported(query: str, start: int) -> bool:
    if start == 0 or not (query[start - 1].isalnum() or query[start - 1] == "_"):
        return True
    before = query[:start]
    requested = any(match.end() == len(before) for match in _REQUEST.finditer(before))
    reported = _REPORTED_PREFIX.search(before)
    return bool(requested
                or reported and reported.end() == len(before)
                or re.search(r"(?:按|依据|根据|不是|并非)\s*$", before)
                or re.search(_LAW_CONNECTOR.pattern + r"\s*$", before))


def _opaque_title_qualifier(query: str, end: int) -> bool:
    following = query[end:].lstrip()
    brackets = {"（": "）", "(": ")", "【": "】", "[": "]"}
    if not following or following[0] not in brackets:
        return False
    close = following.find(brackets[following[0]], 1)
    if close < 0:
        return True
    # A wrapper containing exactly an article label is syntax, not a title
    # qualifier. Other bracket content is opaque; it cannot select an existing
    # unqualified law/version merely because the known title is its prefix.
    try:
        canonical_article_number(following[1:close])
    except ValueError:
        return True
    return False


def _bare_title_follow_supported(query: str, end: int, known: tuple[str, ...]) -> bool:
    following = query[end:].lstrip()
    if _opaque_title_qualifier(query, end):
        return False
    if not following or following[0] in "，,。；;!?！？\n《》、（）()":
        return True
    if re.match(r"中?的?\s*第", following) or _REQUEST.match(following):
        return True
    # A conjunction only connects a new explicit/canonical title, not an
    # arbitrary suffix that could be part of a different law's full name.
    connector = _LAW_CONNECTOR.match(following)
    if connector:
        remainder = following[connector.end():]
        return remainder.startswith("《") or any(remainder.startswith(title) for title in known)
    return False


def _article_looking_items(query: str, protected: list[tuple[int, int]]):
    for match in _ARTICLE_LOOKING.finditer(query):
        if any(match.start() < end and match.end() > start for start, end in protected):
            continue
        end = match.end()
        while query[end:].startswith("之"):
            tail = query[end:]
            numeral = _NUMERAL_FRAGMENT.match(tail)
            if numeral is not None:
                end += numeral.end()
                # Non-CJK identifier letters cannot silently trail a valid
                # suffix numeral and turn an opaque label into a valid key.
                if end < len(query) and query[end].isalnum() and not "\u4e00" <= query[end] <= "\u9fff":
                    opaque = re.match(r"[^\s，,。；;!?！？\n《》]*", query[end:])
                    end += opaque.end() if opaque else 0
            else:
                opaque = re.match(r"之[^\s，,。；;!?！？\n《》]*", tail)
                end += opaque.end() if opaque else 1
        label = query[match.start():end]
        try:
            article = canonical_article_number(label)
            issue = None
        except ValueError:
            article = None
            issue = "invalid_article_numeral" if _ARTICLE.fullmatch(label) else "unsupported_article_label"
        yield match.start(), end, article, issue


def parse_legal_references(query: str, known_law_titles: Iterable[str] = (), *,
                           rules_version: str = REFERENCE_RULES_VERSION) -> ReferenceAnalysis:
    if rules_version == LEGACY_REFERENCE_RULES_VERSION:
        from .legacy_reference_v2 import parse_legal_references as legacy_parse
        old = legacy_parse(query, known_law_titles)
        def mention(item):
            return ReferenceMention(item.law_title, item.article_number, item.span, item.disposition, item.reason)
        return ReferenceAnalysis(old.query,
            tuple(LawArticleRequirement(item.law_title, item.article_number, item.span) for item in old.requirements),
            tuple(mention(item) for item in old.unresolved), tuple(mention(item) for item in old.excluded),
            old.required_law_titles, tuple(mention(item) for item in old.mentions), old.known_law_titles,
            LEGACY_REFERENCE_RULES_VERSION)
    if rules_version != REFERENCE_RULES_VERSION:
        raise ValueError("reference rules are unsupported")
    if not isinstance(query, str) or len(query) > MAX_REFERENCE_QUERY_CHARS:
        raise ValueError("reference query must be bounded text")
    if isinstance(known_law_titles, (str, bytes)) or known_law_titles is None:
        raise ValueError("known titles must be a collection")
    known = tuple(sorted({canonical_law_title(title) for title in known_law_titles}))
    laws: list[tuple[int, int, str]] = []
    unresolved_titles = []
    quoted = list(_quoted_title_items(query))
    aliases = sorted({alias for title in known for alias in (title, "中华人民共和国" + title)}, key=lambda x: (-len(x), x))
    # A known full unquoted title can contain a quoted title. Select the full
    # literal identity before considering the contained quote, but never escape
    # a surrounding outer or malformed quoted span. Longest aliases win.
    for alias in (item for item in aliases if "《" in item):
        for match in re.finditer(re.escape(alias), query):
            if any(start <= match.start() and match.end() <= end for start, end, _, _ in quoted):
                continue
            if any(match.start() < end and match.end() > start for start, end, _ in laws) or any(
                    match.start() < item.span[1] and match.end() > item.span[0] for item in unresolved_titles):
                continue
            if any(issue and match.start() < end and match.end() > start for start, end, _, issue in quoted):
                continue
            if not _bare_title_start_supported(query, match.start()) or not _bare_title_follow_supported(query, match.end(), known):
                unresolved_titles.append(ReferenceMention(None, None, match.span(), "unresolved", "unresolved_bare_title_boundary"))
                continue
            laws.append((match.start(), match.end(), canonical_law_title(alias)))
    for start, end, title, issue in quoted:
        if any(start < law_end and end > law_start for law_start, law_end, _ in laws) or any(
                start < item.span[1] and end > item.span[0] for item in unresolved_titles):
            continue
        if issue:
            unresolved_titles.append(ReferenceMention(None, None, (start, end), "unresolved", issue))
            continue
        if _opaque_title_qualifier(query, end):
            unresolved_titles.append(ReferenceMention(None, None, (start, end), "unresolved", "unresolved_title_qualifier"))
            continue
        following = query[end:].lstrip()
        # Only a directly adjacent, bounded question particle may precede the
        # existing request grammar. Searching farther into an opaque suffix
        # would reinterpret a different document as its quoted inner law.
        ordinary_law_question = (_bare_title_start_supported(query, start)
                                 and following.startswith("有") and _REQUEST.match(following[1:]))
        if not _bare_title_follow_supported(query, end, known) and not ordinary_law_question:
            unresolved_titles.append(ReferenceMention(None, None, (start, end), "unresolved", "unresolved_quoted_title_boundary"))
            continue
        laws.append((start, end, title))
    for alias in aliases:
        for match in re.finditer(re.escape(alias), query):
            if any(match.start() < end and match.end() > start for start, end, _ in laws) or any(
                    match.start() < item.span[1] and match.end() > item.span[0] for item in unresolved_titles):
                continue
            if not _bare_title_start_supported(query, match.start()) or not _bare_title_follow_supported(query, match.end(), known):
                unresolved_titles.append(ReferenceMention(None, None, match.span(), "unresolved", "unresolved_bare_title_boundary"))
                continue
            laws.append((match.start(), match.end(), canonical_law_title(alias)))
    laws.sort()
    unresolved_titles.sort(key=lambda item: item.span)
    article_items: list[tuple[int, int, str | None, str | None]] = []
    protected: list[tuple[int, int]] = [(start, end) for start, end, _, _ in quoted]
    protected.extend((start, end) for start, end, _ in laws)
    protected.extend(item.span for item in unresolved_titles)
    for match in _RANGE.finditer(query):
        if any(match.start() < end and match.end() > start for start, end in protected):
            continue
        protected.append(match.span())
        article_items.append((match.start(), match.end(), None, "unsupported_article_range"))
    for match in _LIST.finditer(query):
        if any(match.start() < end and match.end() > start for start, end in protected):
            continue
        protected.append(match.span())
        for numeral in re.findall(_NUM, match.group(0)):
            try:
                article = canonical_article_number("第" + numeral + "条")
                issue = None
            except ValueError:
                article, issue = None, "invalid_article_numeral"
            article_items.append((match.start(), match.end(), article, issue))
    article_items.extend(_article_looking_items(query, protected))
    article_items.sort(key=lambda item: item[:2])
    requirements: list[LawArticleRequirement] = []
    unresolved: list[ReferenceMention] = list(unresolved_titles)
    excluded: list[ReferenceMention] = []
    mentions: list[ReferenceMention] = []
    law_dispositions: dict[tuple[int, int], tuple[str, str]] = {}
    used_laws: set[tuple[int, int]] = set()
    active_laws: list[str] = []
    syntax_query = _selection_syntax(query, laws, article_items)
    for start, end, law in laws:
        law_dispositions[(start, end)] = _disposition(query, (start, end), syntax_query)
    unresolved.extend(_unresolved_anaphoric_requests(query, laws))
    for start, end, law in laws:
        disposition, reason = law_dispositions[(start, end)]
        mentions.append(ReferenceMention(law, None, (start, end), disposition, reason))
    for start, end, article, issue in article_items:
        prior = [law for law in laws if law[1] <= start and not _STOP.search(query[law[1]:start])]
        owner = prior[-1] if prior else None
        opaque_titles = [item for item in unresolved_titles if item.span[1] <= start
                         and not _STOP.search(query[item.span[1]:start])]
        if opaque_titles and (owner is None or owner[1] <= opaque_titles[-1].span[0]):
            # A failed title boundary is a barrier, not permission to borrow
            # a more distant recognized title from an earlier reference.
            unresolved.append(ReferenceMention(None, article, (opaque_titles[-1].span[0], end),
                                               "unresolved", "unresolved_bare_title_boundary"))
            continue
        if owner is None:
            following = [law for law in laws if law[0] >= end and re.fullmatch(r"\s*[（(]?\s*", query[end:law[0]])]
            owner = following[0] if following else None
        if owner is None:
            reason = issue or "unpaired_article"
            unresolved.append(ReferenceMention(None, article, (start, end), "unresolved", reason))
            continue
        owner_start, owner_end, law_title = owner
        span = (min(owner_start, start), max(owner_end, end))
        # Identity may be inherited from an earlier explicit law, but each
        # article's local selection must still be evaluated. Using the whole
        # owner-to-article span would hide intervening negation or reports.
        disposition, reason = _disposition(query, (start, end), syntax_query)
        inherited = law_dispositions[(owner_start, owner_end)]
        if inherited[0] in {"excluded", "unresolved", "mentioned"}:
            if disposition == "requested" and reason == "explicit_operation_selection":
                # A later local operation cannot silently inherit either an
                # earlier exclusion or an unselected discourse reference.
                disposition, reason = "unresolved", "conflicting_inherited_selection"
            elif disposition not in {"excluded", "unresolved"}:
                disposition, reason = inherited
        used_laws.add((owner_start, owner_end))
        if disposition == "excluded":
            excluded.append(ReferenceMention(law_title, article, span, disposition, reason))
            continue
        if disposition == "mentioned":
            mentions.append(ReferenceMention(law_title, article, span, disposition, reason))
            continue
        if disposition == "unresolved" or issue:
            unresolved.append(ReferenceMention(law_title, article, span, "unresolved", issue or reason))
            continue
        group = [law for law in prior if law_dispositions[(law[0], law[1])][0] == "requested"]
        if len(group) > 1:
            previous = group[-2]
            between = query[previous[1]:owner_start]
            has_prior_article = any(a_start >= previous[1] and a_end <= owner_start for a_start, a_end, _, _ in article_items)
            if not has_prior_article and re.fullmatch(r"\s*" + _LAW_CONNECTOR.pattern + r"\s*", between):
                unresolved.append(ReferenceMention(None, article, span, "unresolved", "ambiguous_law_group"))
                active_laws.extend(item[2] for item in group)
                continue
        active_laws.append(law_title)
        requirement = LawArticleRequirement(law_title, article, span)
        if (law_title, article) not in {(item.law_title, item.article_number) for item in requirements}:
            requirements.append(requirement)
    for start, end, law in laws:
        if (start, end) not in used_laws:
            disposition, reason = law_dispositions[(start, end)]
            if disposition == "excluded":
                excluded.append(ReferenceMention(law, None, (start, end), disposition, reason))
            elif disposition == "unresolved":
                unresolved.append(ReferenceMention(law, None, (start, end), disposition, reason))
            elif disposition == "requested":
                active_laws.append(law)
    mentions.extend(unresolved)
    mentions.extend(excluded)
    mentions.extend(ReferenceMention(item.law_title, item.article_number, item.span, "requested", "explicit_pair")
                    for item in requirements)
    return ReferenceAnalysis(query, tuple(requirements), tuple(unresolved), tuple(excluded),
                             tuple(dict.fromkeys(active_laws)), tuple(mentions), known)
