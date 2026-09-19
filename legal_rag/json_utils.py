from __future__ import annotations

from typing import Any


class DuplicateJsonKeyError(ValueError):
    """Raised when an untrusted JSON object repeats a field name."""


def reject_duplicate_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a JSON object while rejecting ambiguous last-key-wins input."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result
