"""Immutable per-request route facts; text remains in typed search results."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from .models import SearchResult

Pair = tuple[str, str]


@dataclass(frozen=True, slots=True)
class RetrievalOutcome:
    results: tuple[SearchResult, ...]
    route: str
    status: str
    reason_codes: tuple[str, ...] = ()
    requested_pairs: tuple[Pair, ...] = ()
    resolved_pairs: tuple[Pair, ...] = ()
    contract_version: str = "reference-route-v1"

    def __post_init__(self) -> None:
        if self.route not in {"exact_reference", "lexical"}:
            raise ValueError("invalid retrieval route")
        if self.status not in {"found", "not_found", "needs_disambiguation"}:
            raise ValueError("invalid route status")
        if self.contract_version != "reference-route-v1":
            raise ValueError("unknown route contract")
        if not isinstance(self.results, tuple) or not all(
            isinstance(item, SearchResult) for item in self.results
        ):
            raise ValueError("route results must be a typed tuple")
        for name in ("requested_pairs", "resolved_pairs"):
            values = getattr(self, name)
            if not isinstance(values, tuple) or len(values) > 16 or any(
                not isinstance(pair, tuple) or len(pair) != 2
                or any(not isinstance(part, str) or not part.strip() for part in pair)
                for pair in values
            ) or len(set(values)) != len(values):
                raise ValueError("invalid bounded reference pairs")
        if not set(self.resolved_pairs).issubset(self.requested_pairs):
            raise ValueError("route resolved an unrequested pair")
        if self.route == "lexical" and (self.requested_pairs or self.resolved_pairs):
            raise ValueError("lexical route cannot claim exact reference coverage")
        if self.status == "found" and not self.results:
            raise ValueError("found route requires results")
        if self.route == "exact_reference" and self.status == "found" and (
            not self.results or not self.requested_pairs
            or set(self.resolved_pairs) != set(self.requested_pairs)
        ):
            raise ValueError("found route must cover every requested pair")
        if not isinstance(self.reason_codes, tuple) or any(
            not isinstance(reason, str) or not reason or len(reason) > 64
            or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in reason)
            for reason in self.reason_codes
        ):
            raise ValueError("invalid route reason codes")
        object.__setattr__(self, "results", deepcopy(self.results))

    def to_dict(self) -> dict:
        return {
            "contract_version": self.contract_version, "route": self.route,
            "status": self.status, "reason_codes": list(self.reason_codes),
            "requested_pairs": [list(pair) for pair in self.requested_pairs],
            "resolved_pairs": [list(pair) for pair in self.resolved_pairs],
        }

    @classmethod
    def from_dict(cls, value: dict, *, results: tuple[SearchResult, ...]) -> RetrievalOutcome:
        fields = {"contract_version", "route", "status", "reason_codes", "requested_pairs", "resolved_pairs"}
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("invalid route artifact")
        if not isinstance(value["reason_codes"], list) or any(
            not isinstance(value[name], list) or any(not isinstance(pair, list) for pair in value[name])
            for name in ("requested_pairs", "resolved_pairs")
        ):
            raise ValueError("route artifact arrays are invalid")
        return cls(results=results, route=value["route"], status=value["status"],
                   reason_codes=tuple(value["reason_codes"]),
                   requested_pairs=tuple(tuple(pair) for pair in value["requested_pairs"]),
                   resolved_pairs=tuple(tuple(pair) for pair in value["resolved_pairs"]),
                   contract_version=value["contract_version"])
