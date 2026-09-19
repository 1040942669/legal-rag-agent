from __future__ import annotations

from typing import Any, NoReturn


class DuplicateJsonKeyError(ValueError):
    """Raised when an untrusted JSON object repeats a field name."""


class InvalidJsonUnicodeError(ValueError):
    """Raised when decoded JSON contains an unpaired UTF-16 surrogate."""


class NonFiniteJsonConstantError(ValueError):
    """Raised for Python's non-standard JSON numeric constants."""


def reject_duplicate_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a JSON object while rejecting ambiguous last-key-wins input."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_non_finite_json_constant(value: str) -> NoReturn:
    """Reject Python's non-standard ``NaN`` and infinity JSON extensions."""

    raise NonFiniteJsonConstantError(f"non-standard JSON constant: {value}")


def validate_json_unicode(value: Any) -> Any:
    """Recursively reject decoded strings that cannot be encoded as UTF-8."""

    if isinstance(value, str):
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise InvalidJsonUnicodeError("decoded JSON contains an unpaired surrogate")
        return value
    if isinstance(value, list):
        for item in value:
            validate_json_unicode(item)
        return value
    if isinstance(value, dict):
        for key, item in value.items():
            validate_json_unicode(key)
            validate_json_unicode(item)
        return value
    return value
