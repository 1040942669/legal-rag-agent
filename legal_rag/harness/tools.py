from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


READ_ONLY_TOOL_NAMES = frozenset(
    {"search_laws", "get_article", "get_neighbors", "inspect_evidence_metadata"}
)
_SERVER_BOUND_FIELDS = frozenset(
    {
        "authorization",
        "database_url",
        "profile_id",
        "scope_id",
        "snapshot_id",
        "sql",
        "user_id",
    }
)


class ToolPolicyError(ValueError):
    """A model-proposed tool request exceeds the read-only policy."""


@dataclass(frozen=True, slots=True)
class ToolContext:
    user_id: str
    scope_id: str
    snapshot_id: str
    profile_id: str

    def __post_init__(self) -> None:
        for name in ("user_id", "scope_id", "snapshot_id", "profile_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ToolPolicyError(f"{name} must be a non-empty identifier")


@dataclass(frozen=True, slots=True)
class ToolResult:
    ok: bool
    data: Mapping[str, Any]
    error_code: str | None
    retryable: bool
    source_refs: tuple[str, ...]
    duration_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.ok, bool) or not isinstance(self.retryable, bool):
            raise ToolPolicyError("tool result flags must be booleans")
        if not isinstance(self.data, Mapping):
            raise ToolPolicyError("tool result data must be an object")
        if self.error_code is not None and not isinstance(self.error_code, str):
            raise ToolPolicyError("tool result error_code must be a string or null")
        if type(self.duration_ms) is not int or self.duration_ms < 0:
            raise ToolPolicyError("tool result duration_ms must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "data": dict(self.data),
            "error_code": self.error_code,
            "retryable": self.retryable,
            "source_refs": list(self.source_refs),
            "duration_ms": self.duration_ms,
        }


ToolHandler = Callable[[Mapping[str, Any], ToolContext], ToolResult]


class ReadOnlyToolRegistry:
    """Closed registry whose authorization boundary is never model-controlled."""

    def __init__(self, handlers: Mapping[str, ToolHandler]) -> None:
        unknown = set(handlers) - READ_ONLY_TOOL_NAMES
        if unknown:
            raise ToolPolicyError(f"unapproved tool handlers: {sorted(unknown)}")
        if not all(callable(handler) for handler in handlers.values()):
            raise ToolPolicyError("every tool handler must be callable")
        self._handlers = MappingProxyType(dict(handlers))

    @property
    def names(self) -> frozenset[str]:
        return READ_ONLY_TOOL_NAMES

    def invoke(
        self,
        request: Mapping[str, Any],
        *,
        context: ToolContext,
    ) -> ToolResult:
        if not isinstance(request, Mapping) or set(request) != {"name", "arguments"}:
            raise ToolPolicyError("tool request fields are invalid")
        name = request["name"]
        arguments = request["arguments"]
        if not isinstance(name, str) or name not in READ_ONLY_TOOL_NAMES:
            raise ToolPolicyError("tool is not in the read-only allowlist")
        if not isinstance(arguments, Mapping):
            raise ToolPolicyError("tool arguments must be an object")
        normalized = _validate_arguments(name, arguments)
        handler = self._handlers.get(name)
        if handler is None:
            raise ToolPolicyError("tool is allowed but unavailable")
        result = handler(MappingProxyType(normalized), context)
        if not isinstance(result, ToolResult):
            raise ToolPolicyError("tool handler returned an invalid result")
        return result


def _validate_arguments(name: str, value: Mapping[str, Any]) -> dict[str, Any]:
    forbidden = sorted(_SERVER_BOUND_FIELDS.intersection(value))
    if forbidden:
        raise ToolPolicyError(
            "tool arguments cannot override server bindings: " + ", ".join(forbidden)
        )
    arguments = dict(value)
    if name == "search_laws":
        _closed(arguments, required={"query"}, optional={"top_k"})
        _text(arguments["query"], "query", maximum=8_000)
        top_k = arguments.get("top_k", 5)
        if type(top_k) is not int or not 1 <= top_k <= 20:
            raise ToolPolicyError("top_k must be between 1 and 20")
        arguments["top_k"] = top_k
    elif name == "get_article":
        _closed(arguments, required={"article_id"})
        _text(arguments["article_id"], "article_id", maximum=255)
    elif name == "get_neighbors":
        _closed(
            arguments,
            required={"article_id"},
            optional={"before", "after"},
        )
        _text(arguments["article_id"], "article_id", maximum=255)
        for key in ("before", "after"):
            count = arguments.get(key, 1)
            if type(count) is not int or not 0 <= count <= 10:
                raise ToolPolicyError(f"{key} must be between 0 and 10")
            arguments[key] = count
    else:
        _closed(arguments, required={"evidence_ids"})
        identifiers = arguments["evidence_ids"]
        if (
            not isinstance(identifiers, list)
            or not identifiers
            or len(identifiers) > 100
        ):
            raise ToolPolicyError("evidence_ids must be a non-empty bounded list")
        for identifier in identifiers:
            _text(identifier, "evidence_id", maximum=255)
    return arguments


def _closed(
    value: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    allowed = required | (optional or set())
    if not required.issubset(value) or set(value) - allowed:
        raise ToolPolicyError("tool argument fields are invalid")


def _text(value: Any, name: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ToolPolicyError(f"{name} is invalid")
    return value


__all__ = [
    "READ_ONLY_TOOL_NAMES",
    "ReadOnlyToolRegistry",
    "ToolContext",
    "ToolPolicyError",
    "ToolResult",
]
