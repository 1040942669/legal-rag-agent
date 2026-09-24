from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence


MAX_FILTER_VALUES = 1_000
MAX_RETRIEVAL_TOP_K = 10_000


class RetrievalContractError(ValueError):
    """The caller supplied an ambiguous or unsafe retrieval request."""


class RetrievalBoundaryViolation(RuntimeError):
    """A result attempted to cross an immutable retrieval boundary."""


def validate_retrieval_top_k(value: Any) -> int:
    if type(value) is not int or value <= 0:
        raise RetrievalContractError("top_k must be a positive integer")
    if value > MAX_RETRIEVAL_TOP_K:
        raise RetrievalContractError(
            f"top_k exceeds retrieval maximum {MAX_RETRIEVAL_TOP_K}"
        )
    return value


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise RetrievalContractError(
            f"retrieval value is not canonical JSON: {exc}"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_id(value: Any, field_name: str, *, max_length: int = 255) -> str:
    if not isinstance(value, str) or not value:
        raise RetrievalContractError(f"{field_name} must be a non-empty string")
    if value != value.strip():
        raise RetrievalContractError(
            f"{field_name} must not contain surrounding whitespace"
        )
    if len(value) > max_length:
        raise RetrievalContractError(
            f"{field_name} exceeds maximum length {max_length}"
        )
    return value


def _canonical_sha256(value: Any, field_name: str) -> str:
    resolved = _canonical_id(value, field_name, max_length=64)
    if len(resolved) != 64:
        raise RetrievalContractError(f"{field_name} must be a 64-character SHA-256 hex")
    try:
        int(resolved, 16)
    except ValueError as exc:
        raise RetrievalContractError(
            f"{field_name} must be a 64-character SHA-256 hex"
        ) from exc
    return resolved


def _canonical_values(
    value: Sequence[str] | None,
    field_name: str,
    *,
    max_length: int = 255,
) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise RetrievalContractError(f"{field_name} must be a sequence of strings")
    if len(value) > MAX_FILTER_VALUES:
        raise RetrievalContractError(
            f"{field_name} exceeds maximum item count {MAX_FILTER_VALUES}"
        )
    resolved = tuple(
        _canonical_id(item, f"{field_name} item", max_length=max_length)
        for item in value
    )
    if len(set(resolved)) != len(resolved):
        raise RetrievalContractError(f"{field_name} must not contain duplicates")
    return tuple(sorted(resolved))


def _canonical_date(value: date | str | None, field_name: str) -> date | None:
    if value is None:
        return None
    if type(value) is date:
        return value
    if not isinstance(value, str):
        raise RetrievalContractError(f"{field_name} must be an ISO date or null")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise RetrievalContractError(f"{field_name} must be an ISO date") from exc


@dataclass(frozen=True, slots=True)
class RetrievalBoundary:
    """Canonical, immutable request boundary for one retrieval operation."""

    scope_id: str
    snapshot_id: str
    profile_id: str
    law_ids: tuple[str, ...] | None = None
    version_ids: tuple[str, ...] | None = None
    article_ids: tuple[str, ...] | None = None
    article_numbers: tuple[str, ...] | None = None
    effective_on: date | str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope_id", _canonical_id(self.scope_id, "scope_id"))
        object.__setattr__(
            self, "snapshot_id", _canonical_id(self.snapshot_id, "snapshot_id")
        )
        object.__setattr__(
            self, "profile_id", _canonical_sha256(self.profile_id, "profile_id")
        )
        object.__setattr__(self, "law_ids", _canonical_values(self.law_ids, "law_ids"))
        object.__setattr__(
            self, "version_ids", _canonical_values(self.version_ids, "version_ids")
        )
        object.__setattr__(
            self, "article_ids", _canonical_values(self.article_ids, "article_ids")
        )
        object.__setattr__(
            self,
            "article_numbers",
            _canonical_values(self.article_numbers, "article_numbers", max_length=128),
        )
        object.__setattr__(
            self, "effective_on", _canonical_date(self.effective_on, "effective_on")
        )

    @property
    def denies_all(self) -> bool:
        return any(
            values == ()
            for values in (
                self.law_ids,
                self.version_ids,
                self.article_ids,
                self.article_numbers,
            )
        )

    def trace_payload(self) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "snapshot_id": self.snapshot_id,
            "profile_id": self.profile_id,
            "law_ids": list(self.law_ids) if self.law_ids is not None else None,
            "version_ids": (
                list(self.version_ids) if self.version_ids is not None else None
            ),
            "article_ids": (
                list(self.article_ids) if self.article_ids is not None else None
            ),
            "article_numbers": (
                list(self.article_numbers) if self.article_numbers is not None else None
            ),
            "effective_on": (
                self.effective_on.isoformat() if self.effective_on is not None else None
            ),
        }

    @property
    def fingerprint(self) -> str:
        return _sha256_json(
            {
                "schema_version": 1,
                "kind": "postgres_exact_retrieval_boundary",
                **self.trace_payload(),
            }
        )

    @property
    def boundary_fingerprint(self) -> str:
        """Compatibility alias for traces; authorization uses the full object."""

        return self.fingerprint

    def allows(self, provenance: RetrievalProvenance) -> bool:
        if self.denies_all or provenance.boundary != self:
            return False
        if (
            provenance.scope_id != self.scope_id
            or provenance.snapshot_id != self.snapshot_id
            or provenance.profile_id != self.profile_id
            or not provenance.articles
        ):
            return False
        return all(self.allows_article(article) for article in provenance.articles)

    def allows_article(self, article: RetrievedArticleProvenance) -> bool:
        selectors = (
            (self.law_ids, article.law_id),
            (self.version_ids, article.version_id),
            (self.article_ids, article.article_id),
            (self.article_numbers, article.article_number),
        )
        if any(
            allowed is not None and actual not in allowed
            for allowed, actual in selectors
        ):
            return False
        if self.effective_on is None:
            return True
        if article.valid_from is None or article.valid_from > self.effective_on:
            return False
        return article.valid_to is None or self.effective_on < article.valid_to


@dataclass(frozen=True, slots=True)
class RetrievedArticleProvenance:
    article_id: str
    law_id: str
    version_id: str
    article_number: str
    title: str
    valid_from: date | str | None
    valid_to: date | str | None
    source_ref: str
    source_line: int
    verification_status: str

    def __post_init__(self) -> None:
        for field_name in (
            "article_id",
            "law_id",
            "version_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _canonical_id(getattr(self, field_name), field_name),
            )
        for field_name in ("title", "source_ref"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise RetrievalContractError(f"{field_name} must be a non-empty string")
            if value != value.strip():
                raise RetrievalContractError(
                    f"{field_name} must not contain surrounding whitespace"
                )
        if not isinstance(self.article_number, str) or len(self.article_number) > 128:
            raise RetrievalContractError(
                "article_number must be a string of at most 128 characters"
            )
        if self.verification_status not in {"unknown", "verified", "unverified"}:
            raise RetrievalContractError(
                "verification_status must be unknown, verified, or unverified"
            )
        if type(self.source_line) is not int or self.source_line <= 0:
            raise RetrievalContractError("source_line must be a positive integer")
        object.__setattr__(
            self, "valid_from", _canonical_date(self.valid_from, "valid_from")
        )
        object.__setattr__(self, "valid_to", _canonical_date(self.valid_to, "valid_to"))
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and self.valid_from >= self.valid_to
        ):
            raise RetrievalContractError("article validity interval must be half-open")

    def to_metadata(self) -> dict[str, Any]:
        return {
            "article_id": self.article_id,
            "law_id": self.law_id,
            "version_id": self.version_id,
            "article_number": self.article_number,
            "source_ref": self.source_ref,
            "source_line": self.source_line,
            "title": self.title,
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "verification_status": self.verification_status,
        }


@dataclass(frozen=True, slots=True)
class RetrievalProvenance:
    boundary: RetrievalBoundary
    scope_id: str
    snapshot_id: str
    profile_id: str
    chunk_id: str
    chunk_content_hash: str
    chunk_payload_hash: str
    snapshot_ordinal: int
    embedding_hash: str | None
    articles: tuple[RetrievedArticleProvenance, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.boundary, RetrievalBoundary):
            raise RetrievalContractError("provenance boundary is invalid")
        object.__setattr__(self, "scope_id", _canonical_id(self.scope_id, "scope_id"))
        object.__setattr__(
            self, "snapshot_id", _canonical_id(self.snapshot_id, "snapshot_id")
        )
        object.__setattr__(
            self, "profile_id", _canonical_sha256(self.profile_id, "profile_id")
        )
        object.__setattr__(self, "chunk_id", _canonical_id(self.chunk_id, "chunk_id"))
        object.__setattr__(
            self,
            "chunk_content_hash",
            _canonical_sha256(self.chunk_content_hash, "chunk_content_hash"),
        )
        object.__setattr__(
            self,
            "chunk_payload_hash",
            _canonical_sha256(self.chunk_payload_hash, "chunk_payload_hash"),
        )
        if type(self.snapshot_ordinal) is not int or self.snapshot_ordinal < 0:
            raise RetrievalContractError(
                "snapshot_ordinal must be a non-negative integer"
            )
        if self.embedding_hash is not None:
            object.__setattr__(
                self,
                "embedding_hash",
                _canonical_sha256(self.embedding_hash, "embedding_hash"),
            )
        if not isinstance(self.articles, tuple) or not self.articles:
            raise RetrievalContractError(
                "provenance articles must be a non-empty tuple"
            )
        if not all(
            isinstance(item, RetrievedArticleProvenance) for item in self.articles
        ):
            raise RetrievalContractError("provenance articles are invalid")


def article_provenance_from_mapping(value: Any) -> RetrievedArticleProvenance:
    if not isinstance(value, Mapping):
        raise RetrievalContractError("article provenance must be an object")
    required = {
        "article_id",
        "law_id",
        "version_id",
        "article_number",
        "title",
        "source_ref",
        "source_line",
        "valid_from",
        "valid_to",
        "verification_status",
    }
    if not required.issubset(value):
        raise RetrievalContractError("article provenance fields are incomplete")
    return RetrievedArticleProvenance(
        article_id=value["article_id"],
        law_id=value["law_id"],
        version_id=value["version_id"],
        article_number=value["article_number"],
        title=value["title"],
        valid_from=value["valid_from"],
        valid_to=value["valid_to"],
        source_ref=value["source_ref"],
        source_line=value["source_line"],
        verification_status=value["verification_status"],
    )


def chunk_payload_fingerprint(chunk: Any) -> str:
    """Fingerprint the hydrated payload that crosses retrieval boundaries."""

    try:
        payload = {
            "chunk_id": chunk.chunk_id,
            "text": chunk.text,
            "law_names": list(chunk.law_names),
            "article_numbers": list(chunk.article_numbers),
            "source_files": list(chunk.source_files),
            "line_nos": list(chunk.line_nos),
            "strategy": chunk.strategy,
            "metadata": chunk.metadata,
        }
    except AttributeError as exc:
        raise RetrievalContractError("retrieval chunk payload is invalid") from exc
    return _sha256_json(payload)
