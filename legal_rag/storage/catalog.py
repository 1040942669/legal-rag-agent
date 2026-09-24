from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Literal, Mapping, Sequence

from ..data import normalize_article_number
from .contracts import sha256_json


if TYPE_CHECKING:
    from sqlalchemy import Connection, Engine


MAX_BOUNDARY_VALUES = 1_000
SERVICEABLE_SNAPSHOT_STATUSES = frozenset({"validated", "active"})


class CatalogContractError(ValueError):
    """A structured lookup or activation request is malformed or ambiguous."""


class CatalogUnavailableError(RuntimeError):
    """The requested scope/snapshot catalog is not available for reads."""


class CatalogDataError(RuntimeError):
    """Persisted catalog rows violate an immutable storage invariant."""


class SnapshotActivationConflictError(RuntimeError):
    """A snapshot activation compare-and-swap precondition did not hold."""


def _load_database_dependencies() -> None:
    """Load the optional database stack only when a repository is constructed."""

    global active_snapshot_pointers
    global and_
    global chunk_articles
    global chunks
    global corpus_snapshots
    global embedding_imports
    global exists
    global func
    global insert
    global law_articles
    global law_versions
    global select
    global snapshot_activation_events
    global snapshot_chunks
    global update

    try:
        from sqlalchemy import and_ as sqlalchemy_and
        from sqlalchemy import exists as sqlalchemy_exists
        from sqlalchemy import func as sqlalchemy_func
        from sqlalchemy import insert as sqlalchemy_insert
        from sqlalchemy import select as sqlalchemy_select
        from sqlalchemy import update as sqlalchemy_update

        from .schema import active_snapshot_pointers as schema_active_pointers
        from .schema import chunk_articles as schema_chunk_articles
        from .schema import chunks as schema_chunks
        from .schema import corpus_snapshots as schema_corpus_snapshots
        from .schema import embedding_imports as schema_embedding_imports
        from .schema import law_articles as schema_law_articles
        from .schema import law_versions as schema_law_versions
        from .schema import snapshot_activation_events as schema_activation_events
        from .schema import snapshot_chunks as schema_snapshot_chunks
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "PostgresLegalCatalogRepository requires the 'database' extra"
        ) from exc

    and_ = sqlalchemy_and
    exists = sqlalchemy_exists
    func = sqlalchemy_func
    insert = sqlalchemy_insert
    select = sqlalchemy_select
    update = sqlalchemy_update
    active_snapshot_pointers = schema_active_pointers
    chunk_articles = schema_chunk_articles
    chunks = schema_chunks
    corpus_snapshots = schema_corpus_snapshots
    embedding_imports = schema_embedding_imports
    law_articles = schema_law_articles
    law_versions = schema_law_versions
    snapshot_activation_events = schema_activation_events
    snapshot_chunks = schema_snapshot_chunks


def _canonical_text(
    value: Any,
    field_name: str,
    *,
    max_length: int | None = None,
) -> str:
    if not isinstance(value, str) or not value:
        raise CatalogContractError(f"{field_name} must be a non-empty string")
    if value != value.strip():
        raise CatalogContractError(
            f"{field_name} must not contain surrounding whitespace"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise CatalogContractError(f"{field_name} must not contain control characters")
    if max_length is not None and len(value) > max_length:
        raise CatalogContractError(f"{field_name} exceeds maximum length {max_length}")
    return value


def _canonical_optional_text(
    value: Any,
    field_name: str,
    *,
    max_length: int | None = None,
) -> str | None:
    if value is None:
        return None
    return _canonical_text(value, field_name, max_length=max_length)


def _canonical_id(value: Any, field_name: str, *, max_length: int = 255) -> str:
    return _canonical_text(value, field_name, max_length=max_length)


def _canonical_sha256(value: Any, field_name: str) -> str:
    resolved = _canonical_id(value, field_name, max_length=64)
    if len(resolved) != 64:
        raise CatalogContractError(f"{field_name} must be a 64-character SHA-256 hex")
    try:
        int(resolved, 16)
    except ValueError as exc:
        raise CatalogContractError(
            f"{field_name} must be a 64-character SHA-256 hex"
        ) from exc
    return resolved


def _canonical_activation_precondition(
    *,
    snapshot_id: str | None,
    revision: int | None,
    activation_id: str | None,
) -> tuple[str, int, str] | None:
    values = (snapshot_id, revision, activation_id)
    if values == (None, None, None):
        return None
    if any(value is None for value in values):
        raise CatalogContractError(
            "expected current snapshot_id, revision, and activation_id "
            "must be supplied together"
        )
    resolved_snapshot_id = _canonical_id(
        snapshot_id,
        "expected_current_snapshot_id",
    )
    if type(revision) is not int or revision <= 0:
        raise CatalogContractError(
            "expected_current_revision must be a positive integer"
        )
    resolved_activation_id = _canonical_id(
        activation_id,
        "expected_current_activation_id",
        max_length=64,
    )
    return resolved_snapshot_id, revision, resolved_activation_id


def _canonical_values(
    value: Sequence[str] | None,
    field_name: str,
) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise CatalogContractError(f"{field_name} must be a sequence of strings")
    if len(value) > MAX_BOUNDARY_VALUES:
        raise CatalogContractError(
            f"{field_name} exceeds maximum item count {MAX_BOUNDARY_VALUES}"
        )
    resolved = tuple(_canonical_id(item, f"{field_name} item") for item in value)
    if len(set(resolved)) != len(resolved):
        raise CatalogContractError(f"{field_name} must not contain duplicates")
    return tuple(sorted(resolved))


def _canonical_date(value: date | str | None, field_name: str) -> date | None:
    if value is None:
        return None
    if type(value) is date:
        return value
    if not isinstance(value, str):
        raise CatalogContractError(f"{field_name} must be an ISO date or null")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CatalogContractError(f"{field_name} must be an ISO date") from exc


def _canonical_article_number(value: Any) -> str:
    if not isinstance(value, str):
        raise CatalogContractError("article_number must be a string")
    if value != value.strip():
        raise CatalogContractError(
            "article_number must not contain surrounding whitespace"
        )
    resolved = normalize_article_number(value)
    if not resolved:
        raise CatalogContractError("article_number must be a non-empty string")
    if any(ord(character) < 32 or ord(character) == 127 for character in resolved):
        raise CatalogContractError("article_number must not contain control characters")
    if len(resolved) > 128:
        raise CatalogContractError("article_number exceeds maximum length 128")
    return resolved


def _law_version_metadata_hash(
    *,
    law_id: str,
    version_id: str,
    title: str,
    valid_from: date | None,
    valid_to: date | None,
    verification_status: str,
    source_ref: str,
) -> str:
    return sha256_json(
        {
            "law_id": law_id,
            "version_id": version_id,
            "title": title,
            "valid_from": valid_from.isoformat() if valid_from else None,
            "valid_to": valid_to.isoformat() if valid_to else None,
            "verification_status": verification_status,
            "source_ref": source_ref,
        }
    )


@dataclass(frozen=True, slots=True)
class ArticleLookupBoundary:
    """A profile-free authorization and snapshot boundary for exact lookup."""

    scope_id: str
    snapshot_id: str
    law_ids: tuple[str, ...] | None = None
    version_ids: tuple[str, ...] | None = None
    article_ids: tuple[str, ...] | None = None
    pointer_revision: int | None = None
    activation_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope_id", _canonical_id(self.scope_id, "scope_id"))
        object.__setattr__(
            self,
            "snapshot_id",
            _canonical_id(self.snapshot_id, "snapshot_id"),
        )
        object.__setattr__(self, "law_ids", _canonical_values(self.law_ids, "law_ids"))
        object.__setattr__(
            self,
            "version_ids",
            _canonical_values(self.version_ids, "version_ids"),
        )
        object.__setattr__(
            self,
            "article_ids",
            _canonical_values(self.article_ids, "article_ids"),
        )
        if (self.pointer_revision is None) != (self.activation_id is None):
            raise CatalogContractError(
                "pointer_revision and activation_id must be supplied together"
            )
        if self.pointer_revision is not None:
            if type(self.pointer_revision) is not int or self.pointer_revision <= 0:
                raise CatalogContractError(
                    "pointer_revision must be a positive integer"
                )
            object.__setattr__(
                self,
                "activation_id",
                _canonical_id(self.activation_id, "activation_id", max_length=64),
            )

    @property
    def denies_all(self) -> bool:
        return any(
            values == ()
            for values in (self.law_ids, self.version_ids, self.article_ids)
        )

    def trace_payload(self) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "snapshot_id": self.snapshot_id,
            "law_ids": list(self.law_ids) if self.law_ids is not None else None,
            "version_ids": (
                list(self.version_ids) if self.version_ids is not None else None
            ),
            "article_ids": (
                list(self.article_ids) if self.article_ids is not None else None
            ),
            "pointer_revision": self.pointer_revision,
            "activation_id": self.activation_id,
        }

    @property
    def fingerprint(self) -> str:
        return sha256_json(
            {
                "schema_version": 1,
                "kind": "postgres_exact_article_lookup_boundary",
                **self.trace_payload(),
            }
        )

    def allows_identity(
        self,
        *,
        law_id: str,
        version_id: str,
        article_id: str,
    ) -> bool:
        if self.denies_all:
            return False
        selectors = (
            (self.law_ids, law_id),
            (self.version_ids, version_id),
            (self.article_ids, article_id),
        )
        return not any(
            allowed is not None and actual not in allowed
            for allowed, actual in selectors
        )


@dataclass(frozen=True, slots=True)
class ArticleLookupRequest:
    boundary: ArticleLookupBoundary
    law_title: str
    article_number: str
    law_id: str | None = None
    version_id: str | None = None
    effective_on: date | str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.boundary, ArticleLookupBoundary):
            raise CatalogContractError("boundary must be an ArticleLookupBoundary")
        object.__setattr__(
            self,
            "law_title",
            _canonical_text(self.law_title, "law_title"),
        )
        object.__setattr__(
            self,
            "article_number",
            _canonical_article_number(self.article_number),
        )
        object.__setattr__(
            self,
            "law_id",
            _canonical_optional_text(self.law_id, "law_id", max_length=255),
        )
        object.__setattr__(
            self,
            "version_id",
            _canonical_optional_text(self.version_id, "version_id", max_length=255),
        )
        object.__setattr__(
            self,
            "effective_on",
            _canonical_date(self.effective_on, "effective_on"),
        )
        if (
            self.law_id is not None
            and self.boundary.law_ids is not None
            and self.law_id not in self.boundary.law_ids
        ):
            raise CatalogContractError("law_id is outside the lookup boundary")
        if (
            self.version_id is not None
            and self.boundary.version_ids is not None
            and self.version_id not in self.boundary.version_ids
        ):
            raise CatalogContractError("version_id is outside the lookup boundary")

    def trace_payload(self) -> dict[str, Any]:
        return {
            "boundary_fingerprint": self.boundary.fingerprint,
            "law_title": self.law_title,
            "article_number": self.article_number,
            "law_id": self.law_id,
            "version_id": self.version_id,
            "effective_on": (
                self.effective_on.isoformat() if self.effective_on is not None else None
            ),
        }

    @property
    def fingerprint(self) -> str:
        return sha256_json(
            {
                "schema_version": 1,
                "kind": "postgres_exact_article_lookup_request",
                **self.trace_payload(),
            }
        )


@dataclass(frozen=True, slots=True)
class ArticleVersionCandidate:
    law_id: str
    version_id: str
    article_id: str
    title: str
    article_number: str
    valid_from: date | str | None
    valid_to: date | str | None
    verification_status: str
    source_ref: str
    stored_law_version_content_hash: str
    law_version_metadata_hash: str
    article_content_hash: str

    def __post_init__(self) -> None:
        for field_name in ("law_id", "version_id", "article_id"):
            object.__setattr__(
                self,
                field_name,
                _canonical_id(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "title", _canonical_text(self.title, "title"))
        object.__setattr__(
            self,
            "article_number",
            _canonical_article_number(self.article_number),
        )
        object.__setattr__(
            self,
            "valid_from",
            _canonical_date(self.valid_from, "valid_from"),
        )
        object.__setattr__(
            self,
            "valid_to",
            _canonical_date(self.valid_to, "valid_to"),
        )
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and self.valid_from >= self.valid_to
        ):
            raise CatalogContractError("candidate validity interval must be half-open")
        if self.verification_status not in {"unknown", "verified", "unverified"}:
            raise CatalogContractError(
                "verification_status must be unknown, verified, or unverified"
            )
        object.__setattr__(
            self,
            "source_ref",
            _canonical_text(self.source_ref, "source_ref"),
        )
        object.__setattr__(
            self,
            "stored_law_version_content_hash",
            _canonical_sha256(
                self.stored_law_version_content_hash,
                "stored_law_version_content_hash",
            ),
        )
        object.__setattr__(
            self,
            "law_version_metadata_hash",
            _canonical_sha256(
                self.law_version_metadata_hash,
                "law_version_metadata_hash",
            ),
        )
        object.__setattr__(
            self,
            "article_content_hash",
            _canonical_sha256(self.article_content_hash, "article_content_hash"),
        )
        if self.law_version_metadata_hash != _law_version_metadata_hash(
            law_id=self.law_id,
            version_id=self.version_id,
            title=self.title,
            valid_from=self.valid_from,
            valid_to=self.valid_to,
            verification_status=self.verification_status,
            source_ref=self.source_ref,
        ):
            raise CatalogContractError("law version metadata hash does not match")

    @property
    def sort_key(self) -> tuple[str, str, str]:
        return (self.law_id, self.version_id, self.article_id)


@dataclass(frozen=True, slots=True)
class ArticleSnapshotMembership:
    chunk_id: str
    snapshot_ordinal: int
    article_ordinal: int
    chunk_content_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "chunk_id", _canonical_id(self.chunk_id, "chunk_id"))
        if type(self.snapshot_ordinal) is not int or self.snapshot_ordinal < 0:
            raise CatalogContractError(
                "snapshot_ordinal must be a non-negative integer"
            )
        if type(self.article_ordinal) is not int or self.article_ordinal < 0:
            raise CatalogContractError("article_ordinal must be a non-negative integer")
        object.__setattr__(
            self,
            "chunk_content_hash",
            _canonical_sha256(self.chunk_content_hash, "chunk_content_hash"),
        )

    def trace_payload(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "snapshot_ordinal": self.snapshot_ordinal,
            "article_ordinal": self.article_ordinal,
            "chunk_content_hash": self.chunk_content_hash,
        }


@dataclass(frozen=True, slots=True)
class ArticleLookupProvenance:
    boundary: ArticleLookupBoundary
    request_fingerprint: str
    scope_id: str
    snapshot_id: str
    snapshot_corpus_hash: str
    law_id: str
    version_id: str
    article_id: str
    law_version_source_ref: str
    stored_law_version_content_hash: str
    law_version_metadata_hash: str
    article_content_hash: str
    article_payload_hash: str
    memberships: tuple[ArticleSnapshotMembership, ...]
    membership_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.boundary, ArticleLookupBoundary):
            raise CatalogContractError("provenance boundary is invalid")
        object.__setattr__(
            self,
            "request_fingerprint",
            _canonical_sha256(self.request_fingerprint, "request_fingerprint"),
        )
        object.__setattr__(self, "scope_id", _canonical_id(self.scope_id, "scope_id"))
        object.__setattr__(
            self,
            "snapshot_id",
            _canonical_id(self.snapshot_id, "snapshot_id"),
        )
        object.__setattr__(
            self,
            "snapshot_corpus_hash",
            _canonical_sha256(self.snapshot_corpus_hash, "snapshot_corpus_hash"),
        )
        for field_name in ("law_id", "version_id", "article_id"):
            object.__setattr__(
                self,
                field_name,
                _canonical_id(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "law_version_source_ref",
            _canonical_text(self.law_version_source_ref, "law_version_source_ref"),
        )
        object.__setattr__(
            self,
            "stored_law_version_content_hash",
            _canonical_sha256(
                self.stored_law_version_content_hash,
                "stored_law_version_content_hash",
            ),
        )
        object.__setattr__(
            self,
            "law_version_metadata_hash",
            _canonical_sha256(
                self.law_version_metadata_hash,
                "law_version_metadata_hash",
            ),
        )
        object.__setattr__(
            self,
            "article_content_hash",
            _canonical_sha256(self.article_content_hash, "article_content_hash"),
        )
        object.__setattr__(
            self,
            "article_payload_hash",
            _canonical_sha256(self.article_payload_hash, "article_payload_hash"),
        )
        if self.scope_id != self.boundary.scope_id:
            raise CatalogContractError("provenance scope does not match boundary")
        if self.snapshot_id != self.boundary.snapshot_id:
            raise CatalogContractError("provenance snapshot does not match boundary")
        if not self.boundary.allows_identity(
            law_id=self.law_id,
            version_id=self.version_id,
            article_id=self.article_id,
        ):
            raise CatalogContractError("provenance identity is outside boundary")
        if not isinstance(self.memberships, tuple) or not self.memberships:
            raise CatalogContractError(
                "provenance memberships must be a non-empty tuple"
            )
        if not all(
            isinstance(item, ArticleSnapshotMembership) for item in self.memberships
        ):
            raise CatalogContractError("provenance memberships are invalid")
        if len({item.chunk_id for item in self.memberships}) != len(self.memberships):
            raise CatalogContractError("provenance memberships must be unique")
        if len({item.snapshot_ordinal for item in self.memberships}) != len(
            self.memberships
        ):
            raise CatalogContractError("provenance snapshot ordinals must be unique")
        ordered = tuple(
            sorted(
                self.memberships,
                key=lambda item: (item.snapshot_ordinal, item.chunk_id),
            )
        )
        object.__setattr__(self, "memberships", ordered)
        object.__setattr__(
            self,
            "membership_fingerprint",
            _canonical_sha256(
                self.membership_fingerprint,
                "membership_fingerprint",
            ),
        )
        expected_membership_fingerprint = sha256_json(
            {
                "schema_version": 1,
                "scope_id": self.scope_id,
                "snapshot_id": self.snapshot_id,
                "article_id": self.article_id,
                "memberships": [item.trace_payload() for item in ordered],
            }
        )
        if self.membership_fingerprint != expected_membership_fingerprint:
            raise CatalogContractError("membership fingerprint does not match")


@dataclass(frozen=True, slots=True)
class ArticleLookupMatch:
    evidence_id: str
    rank: int
    raw_score: float
    score_kind: str
    title: str
    article_number: str
    body: str
    raw_text: str
    source_ref: str
    source_line: int
    parse_status: str
    law_id: str
    version_id: str
    article_id: str
    valid_from: date | str | None
    valid_to: date | str | None
    verification_status: str
    provenance: ArticleLookupProvenance

    def __post_init__(self) -> None:
        for field_name in ("evidence_id", "law_id", "version_id", "article_id"):
            object.__setattr__(
                self,
                field_name,
                _canonical_id(getattr(self, field_name), field_name),
            )
        if self.evidence_id != self.article_id:
            raise CatalogContractError("evidence_id must equal article_id")
        if type(self.rank) is not int or self.rank != 1:
            raise CatalogContractError("exact lookup rank must equal 1")
        if type(self.raw_score) not in {float, int} or float(self.raw_score) != 1.0:
            raise CatalogContractError("exact lookup raw_score must equal 1.0")
        object.__setattr__(self, "raw_score", 1.0)
        if self.score_kind != "exact_key_match":
            raise CatalogContractError(
                "exact lookup score_kind must be exact_key_match"
            )
        object.__setattr__(self, "title", _canonical_text(self.title, "title"))
        object.__setattr__(
            self,
            "article_number",
            _canonical_article_number(self.article_number),
        )
        if not isinstance(self.body, str):
            raise CatalogContractError("body must be a string")
        if not isinstance(self.raw_text, str):
            raise CatalogContractError("raw_text must be a string")
        object.__setattr__(
            self,
            "source_ref",
            _canonical_text(self.source_ref, "source_ref"),
        )
        if type(self.source_line) is not int or self.source_line <= 0:
            raise CatalogContractError("source_line must be a positive integer")
        object.__setattr__(
            self,
            "parse_status",
            _canonical_text(self.parse_status, "parse_status", max_length=64),
        )
        object.__setattr__(
            self,
            "valid_from",
            _canonical_date(self.valid_from, "valid_from"),
        )
        object.__setattr__(
            self,
            "valid_to",
            _canonical_date(self.valid_to, "valid_to"),
        )
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and self.valid_from >= self.valid_to
        ):
            raise CatalogContractError("article validity interval must be half-open")
        if self.verification_status not in {"unknown", "verified", "unverified"}:
            raise CatalogContractError(
                "verification_status must be unknown, verified, or unverified"
            )
        if not isinstance(self.provenance, ArticleLookupProvenance):
            raise CatalogContractError("provenance is invalid")
        for field_name in ("law_id", "version_id", "article_id"):
            if getattr(self, field_name) != getattr(self.provenance, field_name):
                raise CatalogContractError(
                    f"match {field_name} does not match provenance"
                )
        payload_hash = sha256_json(
            {
                "article_id": self.article_id,
                "law_id": self.law_id,
                "version_id": self.version_id,
                "article_number": self.article_number,
                "body": self.body,
                "raw_text": self.raw_text,
                "source_ref": self.source_ref,
                "source_line": self.source_line,
                "parse_status": self.parse_status,
            }
        )
        if payload_hash != self.provenance.article_payload_hash:
            raise CatalogContractError("article payload hash does not match provenance")
        if payload_hash != self.provenance.article_content_hash:
            raise CatalogContractError("article content hash does not match provenance")
        version_metadata_hash = _law_version_metadata_hash(
            law_id=self.law_id,
            version_id=self.version_id,
            title=self.title,
            valid_from=self.valid_from,
            valid_to=self.valid_to,
            verification_status=self.verification_status,
            source_ref=self.provenance.law_version_source_ref,
        )
        if version_metadata_hash != self.provenance.law_version_metadata_hash:
            raise CatalogContractError(
                "law version metadata hash does not match provenance"
            )


ArticleLookupStatus = Literal["found", "not_found", "needs_disambiguation"]

_ARTICLE_LOOKUP_REASONS_BY_STATUS: dict[ArticleLookupStatus, frozenset[str]] = {
    "found": frozenset({"exact_match"}),
    "not_found": frozenset(
        {
            "boundary_denies_all",
            "no_exact_match",
            "version_not_effective",
            "no_effective_version",
        }
    ),
    "needs_disambiguation": frozenset(
        {
            "multiple_law_identities",
            "multiple_exact_versions",
            "overlapping_validity",
            "validity_unknown",
        }
    ),
}


@dataclass(frozen=True, slots=True)
class ArticleLookupResult:
    status: ArticleLookupStatus
    reason: str
    request: ArticleLookupRequest
    match: ArticleLookupMatch | None
    candidates: tuple[ArticleVersionCandidate, ...]

    def __post_init__(self) -> None:
        if self.status not in {"found", "not_found", "needs_disambiguation"}:
            raise CatalogContractError("lookup result status is invalid")
        object.__setattr__(self, "reason", _canonical_id(self.reason, "reason"))
        if not isinstance(self.request, ArticleLookupRequest):
            raise CatalogContractError("lookup result request is invalid")
        if self.reason not in _ARTICLE_LOOKUP_REASONS_BY_STATUS[self.status]:
            raise CatalogContractError(
                f"{self.reason!r} is not a valid reason for {self.status}"
            )
        if not isinstance(self.candidates, tuple) or not all(
            isinstance(item, ArticleVersionCandidate) for item in self.candidates
        ):
            raise CatalogContractError("lookup result candidates are invalid")
        if self.status == "found":
            if not isinstance(self.match, ArticleLookupMatch):
                raise CatalogContractError("found lookup result requires one match")
            if self.candidates:
                raise CatalogContractError(
                    "found lookup result must not include candidates"
                )
            self._validate_match(self.match)
            return
        if self.match is not None:
            raise CatalogContractError(
                f"{self.status} lookup result must not include a match"
            )
        if self.status == "not_found" and self.candidates:
            raise CatalogContractError(
                "not_found lookup result must not include candidates"
            )
        if self.status == "not_found":
            self._validate_not_found_reason()
            return
        if self.status == "needs_disambiguation":
            if not self.candidates:
                raise CatalogContractError(
                    "needs_disambiguation lookup result requires candidates"
                )
            self._validate_candidates()
            self._validate_disambiguation_reason()

    def _validate_match(self, match: ArticleLookupMatch) -> None:
        request = self.request
        if match.provenance.boundary != request.boundary:
            raise CatalogContractError("match boundary does not match request")
        if match.provenance.request_fingerprint != request.fingerprint:
            raise CatalogContractError(
                "match request fingerprint does not match request"
            )
        if match.title != request.law_title:
            raise CatalogContractError("match title does not match request")
        if match.article_number != request.article_number:
            raise CatalogContractError("match article_number does not match request")
        if request.law_id is not None and match.law_id != request.law_id:
            raise CatalogContractError("match law_id does not match request")
        if request.version_id is not None and match.version_id != request.version_id:
            raise CatalogContractError("match version_id does not match request")
        if request.effective_on is not None:
            if (
                match.verification_status != "verified"
                or match.valid_from is None
                or match.valid_from > request.effective_on
                or (
                    match.valid_to is not None
                    and request.effective_on >= match.valid_to
                )
            ):
                raise CatalogContractError(
                    "match does not prove the requested effective date"
                )

    def _validate_candidates(self) -> None:
        request = self.request
        keys: set[tuple[str, str, str]] = set()
        for candidate in self.candidates:
            if candidate.sort_key in keys:
                raise CatalogContractError("lookup candidates must be unique")
            keys.add(candidate.sort_key)
            if candidate.title != request.law_title:
                raise CatalogContractError("candidate title does not match request")
            if candidate.article_number != request.article_number:
                raise CatalogContractError(
                    "candidate article_number does not match request"
                )
            if request.law_id is not None and candidate.law_id != request.law_id:
                raise CatalogContractError("candidate law_id does not match request")
            if (
                request.version_id is not None
                and candidate.version_id != request.version_id
            ):
                raise CatalogContractError(
                    "candidate version_id does not match request"
                )
            if not request.boundary.allows_identity(
                law_id=candidate.law_id,
                version_id=candidate.version_id,
                article_id=candidate.article_id,
            ):
                raise CatalogContractError("candidate is outside lookup boundary")

    def _validate_not_found_reason(self) -> None:
        request = self.request
        boundary_denies_all = request.boundary.denies_all
        if (self.reason == "boundary_denies_all") != boundary_denies_all:
            raise CatalogContractError(
                "boundary_denies_all reason must exactly match a deny-all boundary"
            )
        if self.reason == "version_not_effective" and (
            request.version_id is None or request.effective_on is None
        ):
            raise CatalogContractError(
                "version_not_effective requires an explicit version and effective date"
            )
        if self.reason == "no_effective_version" and (
            request.version_id is not None or request.effective_on is None
        ):
            raise CatalogContractError(
                "no_effective_version requires an effective date without an explicit "
                "version"
            )

    def _validate_disambiguation_reason(self) -> None:
        request = self.request
        law_ids = {candidate.law_id for candidate in self.candidates}
        version_ids = {candidate.version_id for candidate in self.candidates}

        if self.reason == "multiple_law_identities":
            if request.law_id is not None or len(law_ids) < 2:
                raise CatalogContractError(
                    "multiple_law_identities requires at least two unpinned law "
                    "identities"
                )
            return

        if self.reason == "multiple_exact_versions":
            if (
                request.effective_on is not None
                or request.version_id is not None
                or len(law_ids) != 1
                or len(version_ids) < 2
                or len(version_ids) != len(self.candidates)
            ):
                raise CatalogContractError(
                    "multiple_exact_versions requires at least two distinct versions "
                    "of one law without an effective date or explicit version"
                )
            return

        if request.effective_on is None:
            raise CatalogContractError(
                f"{self.reason} requires an explicit effective date"
            )
        if len(law_ids) != 1:
            raise CatalogContractError(
                f"{self.reason} requires candidates from exactly one law identity"
            )
        if len(version_ids) != len(self.candidates):
            raise CatalogContractError(
                f"{self.reason} requires at most one candidate per version"
            )

        if self.reason == "overlapping_validity":
            if (
                request.version_id is not None
                or len(self.candidates) < 2
                or not all(
                    self._candidate_is_effective_on(
                        candidate,
                        request.effective_on,
                    )
                    for candidate in self.candidates
                )
            ):
                raise CatalogContractError(
                    "overlapping_validity requires at least two distinct verified "
                    "versions effective on the requested date"
                )
            return

        uncertain = tuple(
            candidate
            for candidate in self.candidates
            if candidate.verification_status != "verified"
            or candidate.valid_from is None
        )
        if not uncertain:
            raise CatalogContractError(
                "validity_unknown requires at least one candidate with unknown validity"
            )
        if any(
            candidate not in uncertain
            and not self._candidate_is_effective_on(candidate, request.effective_on)
            for candidate in self.candidates
        ):
            raise CatalogContractError(
                "validity_unknown may include only uncertain or currently effective "
                "verified candidates"
            )

    @staticmethod
    def _candidate_is_effective_on(
        candidate: ArticleVersionCandidate,
        effective_on: date,
    ) -> bool:
        return (
            candidate.verification_status == "verified"
            and candidate.valid_from is not None
            and candidate.valid_from <= effective_on
            and (candidate.valid_to is None or effective_on < candidate.valid_to)
        )

    @classmethod
    def found(
        cls,
        request: ArticleLookupRequest,
        match: ArticleLookupMatch,
        *,
        reason: str = "exact_match",
    ) -> ArticleLookupResult:
        return cls(
            status="found",
            reason=reason,
            request=request,
            match=match,
            candidates=(),
        )

    @classmethod
    def not_found(
        cls,
        request: ArticleLookupRequest,
        *,
        reason: str,
    ) -> ArticleLookupResult:
        return cls(
            status="not_found",
            reason=reason,
            request=request,
            match=None,
            candidates=(),
        )

    @classmethod
    def needs_disambiguation(
        cls,
        request: ArticleLookupRequest,
        *,
        reason: str,
        candidates: Sequence[ArticleVersionCandidate],
    ) -> ArticleLookupResult:
        ordered = tuple(sorted(candidates, key=lambda item: item.sort_key))
        return cls(
            status="needs_disambiguation",
            reason=reason,
            request=request,
            match=None,
            candidates=ordered,
        )


@dataclass(frozen=True, slots=True)
class SnapshotActivationEvent:
    activation_id: str
    scope_id: str
    revision: int
    operation: str
    previous_snapshot_id: str | None
    target_snapshot_id: str
    previous_activation_id: str | None
    actor: str | None
    reason: str | None
    occurred_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "activation_id",
            _canonical_id(self.activation_id, "activation_id", max_length=64),
        )
        object.__setattr__(self, "scope_id", _canonical_id(self.scope_id, "scope_id"))
        if type(self.revision) is not int or self.revision <= 0:
            raise CatalogDataError("activation revision must be a positive integer")
        if self.operation not in {
            "initial_activate",
            "replace",
            "rollback",
            "migration_bootstrap",
        }:
            raise CatalogDataError("activation operation is invalid")
        object.__setattr__(
            self,
            "previous_snapshot_id",
            _canonical_optional_text(
                self.previous_snapshot_id,
                "previous_snapshot_id",
                max_length=255,
            ),
        )
        object.__setattr__(
            self,
            "target_snapshot_id",
            _canonical_id(self.target_snapshot_id, "target_snapshot_id"),
        )
        object.__setattr__(
            self,
            "previous_activation_id",
            _canonical_optional_text(
                self.previous_activation_id,
                "previous_activation_id",
                max_length=64,
            ),
        )
        object.__setattr__(
            self,
            "actor",
            _canonical_optional_text(self.actor, "actor", max_length=128),
        )
        object.__setattr__(
            self,
            "reason",
            _canonical_optional_text(self.reason, "reason"),
        )
        if self.revision == 1:
            if self.operation not in {"initial_activate", "migration_bootstrap"}:
                raise CatalogDataError(
                    "revision 1 activation must be initial_activate or migration_bootstrap"
                )
            if (
                self.previous_snapshot_id is not None
                or self.previous_activation_id is not None
            ):
                raise CatalogDataError(
                    "revision 1 activation must not have a predecessor"
                )
        else:
            if self.operation not in {"replace", "rollback"}:
                raise CatalogDataError(
                    "revision > 1 activation must be replace or rollback"
                )
            if self.previous_snapshot_id is None or self.previous_activation_id is None:
                raise CatalogDataError("revision > 1 activation requires a predecessor")
        if self.previous_snapshot_id == self.target_snapshot_id:
            raise CatalogDataError(
                "activation target must differ from the previous snapshot"
            )
        if (
            not isinstance(self.occurred_at, datetime)
            or self.occurred_at.tzinfo is None
            or self.occurred_at.utcoffset() is None
        ):
            raise CatalogDataError(
                "activation occurred_at must be a timezone-aware datetime"
            )


@dataclass(frozen=True, slots=True)
class ActiveSnapshotSelection:
    scope_id: str
    snapshot_id: str
    revision: int
    activation_id: str
    corpus_hash: str
    activated_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope_id", _canonical_id(self.scope_id, "scope_id"))
        object.__setattr__(
            self,
            "snapshot_id",
            _canonical_id(self.snapshot_id, "snapshot_id"),
        )
        if type(self.revision) is not int or self.revision <= 0:
            raise CatalogDataError("active snapshot revision must be positive")
        object.__setattr__(
            self,
            "activation_id",
            _canonical_id(self.activation_id, "activation_id", max_length=64),
        )
        object.__setattr__(
            self,
            "corpus_hash",
            _canonical_sha256(self.corpus_hash, "corpus_hash"),
        )
        if (
            not isinstance(self.activated_at, datetime)
            or self.activated_at.tzinfo is None
            or self.activated_at.utcoffset() is None
        ):
            raise CatalogDataError(
                "active snapshot activated_at must be a timezone-aware datetime"
            )

    @property
    def boundary(self) -> ArticleLookupBoundary:
        return ArticleLookupBoundary(
            scope_id=self.scope_id,
            snapshot_id=self.snapshot_id,
            pointer_revision=self.revision,
            activation_id=self.activation_id,
        )


@dataclass(frozen=True, slots=True)
class SnapshotActivationResult:
    selection: ActiveSnapshotSelection
    event: SnapshotActivationEvent
    changed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.selection, ActiveSnapshotSelection):
            raise CatalogDataError("activation selection is invalid")
        if not isinstance(self.event, SnapshotActivationEvent):
            raise CatalogDataError("activation event is invalid")
        if type(self.changed) is not bool:
            raise CatalogDataError("activation changed flag must be boolean")
        if (
            self.selection.scope_id != self.event.scope_id
            or self.selection.snapshot_id != self.event.target_snapshot_id
            or self.selection.revision != self.event.revision
            or self.selection.activation_id != self.event.activation_id
        ):
            raise CatalogDataError("activation result event and selection disagree")
        if self.selection.activated_at != self.event.occurred_at:
            raise CatalogDataError("activation result timestamps disagree")


@dataclass(frozen=True, slots=True)
class _ArticleRowGroup:
    row: Mapping[str, Any]
    memberships: tuple[ArticleSnapshotMembership, ...]
    candidate: ArticleVersionCandidate


class PostgresLegalCatalogRepository:
    """Structured article lookup and atomic active-snapshot administration."""

    def __init__(self, engine: Engine) -> None:
        _load_database_dependencies()
        if engine.dialect.name != "postgresql":
            raise ValueError("PostgresLegalCatalogRepository requires PostgreSQL")
        self.engine = engine

    def lookup_article(self, request: ArticleLookupRequest) -> ArticleLookupResult:
        if not isinstance(request, ArticleLookupRequest):
            raise CatalogContractError("request must be an ArticleLookupRequest")
        with self.engine.connect().execution_options(
            isolation_level="REPEATABLE READ"
        ) as connection:
            with connection.begin():
                return self._lookup_article(connection, request)

    def lookup_active_article(
        self,
        *,
        scope_id: str,
        law_title: str,
        article_number: str,
        law_id: str | None = None,
        version_id: str | None = None,
        effective_on: date | str | None = None,
    ) -> ArticleLookupResult:
        resolved_scope_id = _canonical_id(scope_id, "scope_id")
        with self.engine.connect().execution_options(
            isolation_level="REPEATABLE READ"
        ) as connection:
            with connection.begin():
                selection, _ = self._active_snapshot(connection, resolved_scope_id)
                request = ArticleLookupRequest(
                    boundary=selection.boundary,
                    law_title=law_title,
                    article_number=article_number,
                    law_id=law_id,
                    version_id=version_id,
                    effective_on=effective_on,
                )
                return self._lookup_article(connection, request)

    def get_active_snapshot(self, scope_id: str) -> ActiveSnapshotSelection:
        resolved_scope_id = _canonical_id(scope_id, "scope_id")
        with self.engine.connect() as connection:
            selection, _ = self._active_snapshot(connection, resolved_scope_id)
        return selection

    def activate_snapshot(
        self,
        *,
        scope_id: str,
        snapshot_id: str,
        expected_current_snapshot_id: str | None,
        expected_current_revision: int | None = None,
        expected_current_activation_id: str | None = None,
        required_profile_id: str | None = None,
        actor: str | None = None,
        reason: str | None = None,
    ) -> SnapshotActivationResult:
        return self._change_active_snapshot(
            scope_id=scope_id,
            snapshot_id=snapshot_id,
            expected_current_snapshot_id=expected_current_snapshot_id,
            expected_current_revision=expected_current_revision,
            expected_current_activation_id=expected_current_activation_id,
            required_profile_id=required_profile_id,
            actor=actor,
            reason=reason,
            rollback=False,
        )

    def rollback_snapshot(
        self,
        *,
        scope_id: str,
        target_snapshot_id: str,
        expected_current_snapshot_id: str,
        expected_current_revision: int,
        expected_current_activation_id: str,
        required_profile_id: str,
        actor: str | None = None,
        reason: str | None = None,
    ) -> SnapshotActivationResult:
        return self._change_active_snapshot(
            scope_id=scope_id,
            snapshot_id=target_snapshot_id,
            expected_current_snapshot_id=expected_current_snapshot_id,
            expected_current_revision=expected_current_revision,
            expected_current_activation_id=expected_current_activation_id,
            required_profile_id=required_profile_id,
            actor=actor,
            reason=reason,
            rollback=True,
        )

    def list_activation_history(
        self,
        scope_id: str,
        *,
        before_revision: int | None = None,
        limit: int = 100,
    ) -> tuple[SnapshotActivationEvent, ...]:
        resolved_scope_id = _canonical_id(scope_id, "scope_id")
        if type(limit) is not int or limit <= 0 or limit > 1_000:
            raise CatalogContractError("limit must be an integer from 1 to 1000")
        if before_revision is not None and (
            type(before_revision) is not int or before_revision <= 0
        ):
            raise CatalogContractError("before_revision must be a positive integer")
        statement = select(snapshot_activation_events).where(
            snapshot_activation_events.c.scope_id == resolved_scope_id
        )
        if before_revision is not None:
            statement = statement.where(
                snapshot_activation_events.c.revision < before_revision
            )
        statement = statement.order_by(
            snapshot_activation_events.c.revision.desc()
        ).limit(limit)
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return tuple(self._event_from_row(row) for row in rows)

    def _lookup_article(
        self,
        connection: Connection,
        request: ArticleLookupRequest,
    ) -> ArticleLookupResult:
        if request.boundary.denies_all:
            return ArticleLookupResult.not_found(
                request,
                reason="boundary_denies_all",
            )
        snapshot = self._validated_snapshot(connection, request.boundary)
        groups = self._article_groups(connection, request)
        if not groups:
            return ArticleLookupResult.not_found(request, reason="no_exact_match")
        selected, status_reason, disambiguation_groups = self._resolve_article_group(
            request,
            groups,
        )
        if selected is None:
            status, reason = status_reason
            if status == "not_found":
                return ArticleLookupResult.not_found(request, reason=reason)
            return ArticleLookupResult.needs_disambiguation(
                request,
                reason=reason,
                candidates=tuple(group.candidate for group in disambiguation_groups),
            )
        match = self._match_from_group(
            request,
            selected,
            snapshot_corpus_hash=snapshot["corpus_hash"],
        )
        return ArticleLookupResult.found(request, match)

    def _validated_snapshot(
        self,
        connection: Connection,
        boundary: ArticleLookupBoundary,
    ) -> Mapping[str, Any]:
        row = (
            connection.execute(
                select(corpus_snapshots).where(
                    corpus_snapshots.c.scope_id == boundary.scope_id,
                    corpus_snapshots.c.snapshot_id == boundary.snapshot_id,
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None or row["status"] not in SERVICEABLE_SNAPSHOT_STATUSES:
            raise CatalogUnavailableError(
                "the requested scope/snapshot catalog is not serviceable"
            )
        if boundary.pointer_revision is not None:
            activation_exists = connection.scalar(
                select(
                    exists().where(
                        snapshot_activation_events.c.scope_id == boundary.scope_id,
                        snapshot_activation_events.c.revision
                        == boundary.pointer_revision,
                        snapshot_activation_events.c.activation_id
                        == boundary.activation_id,
                        snapshot_activation_events.c.target_snapshot_id
                        == boundary.snapshot_id,
                    )
                )
            )
            if not activation_exists:
                raise CatalogUnavailableError(
                    "the pinned active snapshot event is not available"
                )
        return row

    def _article_groups(
        self,
        connection: Connection,
        request: ArticleLookupRequest,
    ) -> tuple[_ArticleRowGroup, ...]:
        boundary = request.boundary
        statement = (
            select(
                law_versions.c.law_id,
                law_versions.c.version_id,
                law_versions.c.title,
                law_versions.c.valid_from,
                law_versions.c.valid_to,
                law_versions.c.verification_status,
                law_versions.c.source_ref.label("law_version_source_ref"),
                law_versions.c.content_hash.label("law_version_content_hash"),
                law_articles.c.article_id,
                law_articles.c.article_number,
                law_articles.c.body,
                law_articles.c.raw_text,
                law_articles.c.source_ref,
                law_articles.c.source_line,
                law_articles.c.parse_status,
                law_articles.c.content_hash.label("article_content_hash"),
                chunk_articles.c.chunk_id,
                chunk_articles.c.ordinal.label("article_ordinal"),
                snapshot_chunks.c.ordinal.label("snapshot_ordinal"),
                chunks.c.text.label("chunk_text"),
                chunks.c.strategy.label("chunk_strategy"),
                chunks.c.metadata.label("chunk_metadata"),
                chunks.c.content_hash.label("chunk_content_hash"),
                chunks.c.recipe_hash.label("chunk_recipe_hash"),
            )
            .select_from(
                law_versions.join(
                    law_articles,
                    and_(
                        law_articles.c.version_id == law_versions.c.version_id,
                        law_articles.c.law_id == law_versions.c.law_id,
                    ),
                )
                .join(
                    chunk_articles,
                    chunk_articles.c.article_id == law_articles.c.article_id,
                )
                .join(
                    chunks,
                    chunks.c.chunk_id == chunk_articles.c.chunk_id,
                )
                .join(
                    snapshot_chunks,
                    snapshot_chunks.c.chunk_id == chunks.c.chunk_id,
                )
            )
            .where(
                snapshot_chunks.c.snapshot_id == boundary.snapshot_id,
                law_versions.c.title == request.law_title,
                law_articles.c.article_number == request.article_number,
            )
            .order_by(
                law_versions.c.law_id,
                law_versions.c.version_id,
                law_articles.c.article_id,
                snapshot_chunks.c.ordinal,
                chunk_articles.c.chunk_id,
            )
        )
        if request.law_id is not None:
            statement = statement.where(law_versions.c.law_id == request.law_id)
        if request.version_id is not None:
            statement = statement.where(law_versions.c.version_id == request.version_id)
        if boundary.law_ids is not None:
            statement = statement.where(law_versions.c.law_id.in_(boundary.law_ids))
        if boundary.version_ids is not None:
            statement = statement.where(
                law_versions.c.version_id.in_(boundary.version_ids)
            )
        if boundary.article_ids is not None:
            statement = statement.where(
                law_articles.c.article_id.in_(boundary.article_ids)
            )
        rows = connection.execute(statement).mappings().all()
        grouped: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["article_id"], []).append(row)

        results: list[_ArticleRowGroup] = []
        for article_id, article_rows in grouped.items():
            first = article_rows[0]
            immutable_fields = (
                "law_id",
                "version_id",
                "title",
                "valid_from",
                "valid_to",
                "verification_status",
                "law_version_source_ref",
                "law_version_content_hash",
                "article_number",
                "body",
                "raw_text",
                "source_ref",
                "source_line",
                "parse_status",
                "article_content_hash",
            )
            if any(
                any(row[field] != first[field] for field in immutable_fields)
                for row in article_rows[1:]
            ):
                raise CatalogDataError(
                    f"article {article_id!r} has inconsistent membership rows"
                )
            article_payload = {
                "article_id": first["article_id"],
                "law_id": first["law_id"],
                "version_id": first["version_id"],
                "article_number": first["article_number"],
                "body": first["body"],
                "raw_text": first["raw_text"],
                "source_ref": first["source_ref"],
                "source_line": first["source_line"],
                "parse_status": first["parse_status"],
            }
            payload_hash = sha256_json(article_payload)
            if payload_hash != first["article_content_hash"]:
                raise CatalogDataError(
                    f"article {article_id!r} content hash does not match stored payload"
                )
            version_metadata_hash = _law_version_metadata_hash(
                law_id=first["law_id"],
                version_id=first["version_id"],
                title=first["title"],
                valid_from=first["valid_from"],
                valid_to=first["valid_to"],
                verification_status=first["verification_status"],
                source_ref=first["law_version_source_ref"],
            )
            memberships_list: list[ArticleSnapshotMembership] = []
            for row in article_rows:
                metadata = row["chunk_metadata"]
                if not isinstance(metadata, Mapping):
                    raise CatalogDataError(
                        f"chunk {row['chunk_id']!r} metadata is not an object"
                    )
                metadata_article_ids = metadata.get("article_ids")
                if (
                    metadata_article_ids is None
                    and metadata.get("article_id") is not None
                ):
                    metadata_article_ids = [metadata["article_id"]]
                if (
                    not isinstance(metadata_article_ids, (list, tuple))
                    or row["article_ordinal"] >= len(metadata_article_ids)
                    or metadata_article_ids[row["article_ordinal"]]
                    != first["article_id"]
                ):
                    raise CatalogDataError(
                        f"chunk {row['chunk_id']!r} article relation does not match "
                        "its immutable metadata"
                    )
                chunk_content_hash = sha256_json(
                    {
                        "chunk_id": row["chunk_id"],
                        "text": row["chunk_text"],
                        "strategy": row["chunk_strategy"],
                        "metadata": row["chunk_metadata"],
                        "recipe_hash": row["chunk_recipe_hash"],
                    }
                )
                if chunk_content_hash != row["chunk_content_hash"]:
                    raise CatalogDataError(
                        f"chunk {row['chunk_id']!r} content hash does not match "
                        "stored payload"
                    )
                memberships_list.append(
                    ArticleSnapshotMembership(
                        chunk_id=row["chunk_id"],
                        snapshot_ordinal=row["snapshot_ordinal"],
                        article_ordinal=row["article_ordinal"],
                        chunk_content_hash=row["chunk_content_hash"],
                    )
                )
            memberships = tuple(memberships_list)
            candidate = ArticleVersionCandidate(
                law_id=first["law_id"],
                version_id=first["version_id"],
                article_id=first["article_id"],
                title=first["title"],
                article_number=first["article_number"],
                valid_from=first["valid_from"],
                valid_to=first["valid_to"],
                verification_status=first["verification_status"],
                source_ref=first["law_version_source_ref"],
                stored_law_version_content_hash=first["law_version_content_hash"],
                law_version_metadata_hash=version_metadata_hash,
                article_content_hash=first["article_content_hash"],
            )
            results.append(
                _ArticleRowGroup(
                    row=first,
                    memberships=memberships,
                    candidate=candidate,
                )
            )
        return tuple(sorted(results, key=lambda item: item.candidate.sort_key))

    def _resolve_article_group(
        self,
        request: ArticleLookupRequest,
        groups: tuple[_ArticleRowGroup, ...],
    ) -> tuple[
        _ArticleRowGroup | None,
        tuple[Literal["not_found", "needs_disambiguation"], str],
        tuple[_ArticleRowGroup, ...],
    ]:
        if (
            request.law_id is None
            and len({group.candidate.law_id for group in groups}) > 1
        ):
            return (
                None,
                ("needs_disambiguation", "multiple_law_identities"),
                groups,
            )
        if request.effective_on is None:
            if len(groups) == 1:
                return groups[0], ("not_found", "unreachable"), ()
            return (
                None,
                ("needs_disambiguation", "multiple_exact_versions"),
                groups,
            )

        uncertain = tuple(
            group
            for group in groups
            if group.candidate.verification_status != "verified"
            or group.candidate.valid_from is None
        )
        effective = tuple(
            group
            for group in groups
            if group not in uncertain
            and group.candidate.valid_from is not None
            and group.candidate.valid_from <= request.effective_on
            and (
                group.candidate.valid_to is None
                or request.effective_on < group.candidate.valid_to
            )
        )
        if uncertain:
            relevant = tuple(
                sorted(
                    (*uncertain, *effective),
                    key=lambda group: group.candidate.sort_key,
                )
            )
            return None, ("needs_disambiguation", "validity_unknown"), relevant
        if len(effective) == 1:
            return effective[0], ("not_found", "unreachable"), ()
        if len(effective) > 1:
            return (
                None,
                ("needs_disambiguation", "overlapping_validity"),
                effective,
            )
        reason = (
            "version_not_effective"
            if request.version_id is not None
            else "no_effective_version"
        )
        return None, ("not_found", reason), ()

    def _match_from_group(
        self,
        request: ArticleLookupRequest,
        group: _ArticleRowGroup,
        *,
        snapshot_corpus_hash: str,
    ) -> ArticleLookupMatch:
        row = group.row
        ordered_memberships = tuple(
            sorted(
                group.memberships,
                key=lambda item: (item.snapshot_ordinal, item.chunk_id),
            )
        )
        provenance = ArticleLookupProvenance(
            boundary=request.boundary,
            request_fingerprint=request.fingerprint,
            scope_id=request.boundary.scope_id,
            snapshot_id=request.boundary.snapshot_id,
            snapshot_corpus_hash=snapshot_corpus_hash,
            law_id=row["law_id"],
            version_id=row["version_id"],
            article_id=row["article_id"],
            law_version_source_ref=row["law_version_source_ref"],
            stored_law_version_content_hash=row["law_version_content_hash"],
            law_version_metadata_hash=_law_version_metadata_hash(
                law_id=row["law_id"],
                version_id=row["version_id"],
                title=row["title"],
                valid_from=row["valid_from"],
                valid_to=row["valid_to"],
                verification_status=row["verification_status"],
                source_ref=row["law_version_source_ref"],
            ),
            article_content_hash=row["article_content_hash"],
            article_payload_hash=row["article_content_hash"],
            memberships=ordered_memberships,
            membership_fingerprint=sha256_json(
                {
                    "schema_version": 1,
                    "scope_id": request.boundary.scope_id,
                    "snapshot_id": request.boundary.snapshot_id,
                    "article_id": row["article_id"],
                    "memberships": [
                        item.trace_payload() for item in ordered_memberships
                    ],
                }
            ),
        )
        return ArticleLookupMatch(
            evidence_id=row["article_id"],
            rank=1,
            raw_score=1.0,
            score_kind="exact_key_match",
            title=row["title"],
            article_number=row["article_number"],
            body=row["body"],
            raw_text=row["raw_text"],
            source_ref=row["source_ref"],
            source_line=row["source_line"],
            parse_status=row["parse_status"],
            law_id=row["law_id"],
            version_id=row["version_id"],
            article_id=row["article_id"],
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            verification_status=row["verification_status"],
            provenance=provenance,
        )

    def _active_snapshot(
        self,
        connection: Connection,
        scope_id: str,
        *,
        for_update: bool = False,
    ) -> tuple[ActiveSnapshotSelection, SnapshotActivationEvent]:
        statement = (
            select(
                active_snapshot_pointers.c.scope_id,
                active_snapshot_pointers.c.snapshot_id,
                active_snapshot_pointers.c.revision,
                active_snapshot_pointers.c.activation_id,
                active_snapshot_pointers.c.updated_at,
                corpus_snapshots.c.corpus_hash,
                corpus_snapshots.c.status,
                corpus_snapshots.c.activated_at,
                snapshot_activation_events.c.operation,
                snapshot_activation_events.c.previous_snapshot_id,
                snapshot_activation_events.c.previous_activation_id,
                snapshot_activation_events.c.actor,
                snapshot_activation_events.c.reason,
                snapshot_activation_events.c.occurred_at,
            )
            .select_from(
                active_snapshot_pointers.join(
                    corpus_snapshots,
                    and_(
                        corpus_snapshots.c.scope_id
                        == active_snapshot_pointers.c.scope_id,
                        corpus_snapshots.c.snapshot_id
                        == active_snapshot_pointers.c.snapshot_id,
                    ),
                ).join(
                    snapshot_activation_events,
                    and_(
                        snapshot_activation_events.c.scope_id
                        == active_snapshot_pointers.c.scope_id,
                        snapshot_activation_events.c.revision
                        == active_snapshot_pointers.c.revision,
                        snapshot_activation_events.c.activation_id
                        == active_snapshot_pointers.c.activation_id,
                        snapshot_activation_events.c.target_snapshot_id
                        == active_snapshot_pointers.c.snapshot_id,
                    ),
                )
            )
            .where(active_snapshot_pointers.c.scope_id == scope_id)
        )
        if for_update:
            statement = statement.with_for_update(of=active_snapshot_pointers)
        row = connection.execute(statement).mappings().one_or_none()
        if row is None:
            pointer_exists = connection.scalar(
                select(exists().where(active_snapshot_pointers.c.scope_id == scope_id))
            )
            if pointer_exists:
                raise CatalogDataError("active snapshot pointer is internally invalid")
            raise CatalogUnavailableError("the scope has no active snapshot")
        if row["status"] != "active":
            raise CatalogDataError("active pointer target is not active")
        if (
            row["activated_at"] != row["updated_at"]
            or row["activated_at"] != row["occurred_at"]
        ):
            raise CatalogDataError("active snapshot timestamps disagree")
        selection = ActiveSnapshotSelection(
            scope_id=row["scope_id"],
            snapshot_id=row["snapshot_id"],
            revision=row["revision"],
            activation_id=row["activation_id"],
            corpus_hash=row["corpus_hash"],
            activated_at=row["activated_at"],
        )
        event = SnapshotActivationEvent(
            activation_id=row["activation_id"],
            scope_id=row["scope_id"],
            revision=row["revision"],
            operation=row["operation"],
            previous_snapshot_id=row["previous_snapshot_id"],
            target_snapshot_id=row["snapshot_id"],
            previous_activation_id=row["previous_activation_id"],
            actor=row["actor"],
            reason=row["reason"],
            occurred_at=row["occurred_at"],
        )
        return selection, event

    def _change_active_snapshot(
        self,
        *,
        scope_id: str,
        snapshot_id: str,
        expected_current_snapshot_id: str | None,
        expected_current_revision: int | None,
        expected_current_activation_id: str | None,
        required_profile_id: str | None,
        actor: str | None,
        reason: str | None,
        rollback: bool,
    ) -> SnapshotActivationResult:
        resolved_scope_id = _canonical_id(scope_id, "scope_id")
        resolved_snapshot_id = _canonical_id(snapshot_id, "snapshot_id")
        resolved_expected = _canonical_activation_precondition(
            snapshot_id=expected_current_snapshot_id,
            revision=expected_current_revision,
            activation_id=expected_current_activation_id,
        )
        if rollback and required_profile_id is None:
            raise CatalogContractError(
                "rollback requires a non-empty required_profile_id"
            )
        resolved_profile = (
            _canonical_sha256(required_profile_id, "required_profile_id")
            if required_profile_id is not None
            else None
        )
        resolved_actor = _canonical_optional_text(actor, "actor", max_length=128)
        resolved_reason = _canonical_optional_text(reason, "reason")

        with self.engine.begin() as connection:
            connection.execute(
                select(
                    func.pg_advisory_xact_lock(
                        func.hashtextextended(
                            f"legal_rag_snapshot_activation_v1:{resolved_scope_id}",
                            0,
                        )
                    )
                )
            )
            try:
                current, current_event = self._active_snapshot(
                    connection,
                    resolved_scope_id,
                    for_update=True,
                )
            except CatalogUnavailableError:
                current = None
                current_event = None

            actual_current = (
                (
                    current.snapshot_id,
                    current.revision,
                    current.activation_id,
                )
                if current is not None
                else None
            )
            if resolved_expected != actual_current:
                raise SnapshotActivationConflictError(
                    "expected current snapshot does not match the active pointer"
                )
            if rollback and current is None:
                raise SnapshotActivationConflictError(
                    "rollback requires an existing active snapshot"
                )
            same_target = (
                current is not None and current.snapshot_id == resolved_snapshot_id
            )

            lock_ids = sorted(
                {
                    resolved_snapshot_id,
                    *((current.snapshot_id,) if current is not None else ()),
                }
            )
            locked_rows = (
                connection.execute(
                    select(corpus_snapshots)
                    .where(
                        corpus_snapshots.c.scope_id == resolved_scope_id,
                        corpus_snapshots.c.snapshot_id.in_(lock_ids),
                    )
                    .order_by(corpus_snapshots.c.snapshot_id)
                    .with_for_update()
                )
                .mappings()
                .all()
            )
            rows_by_id = {row["snapshot_id"]: row for row in locked_rows}
            target = rows_by_id.get(resolved_snapshot_id)
            if target is None:
                raise CatalogUnavailableError(
                    "the target snapshot is not available in the requested scope"
                )
            expected_target_status = "active" if same_target else "validated"
            if (
                target["status"] != expected_target_status
                or target["validated_at"] is None
            ):
                raise CatalogUnavailableError(
                    "the target snapshot is not ready for activation"
                )
            member_exists = connection.scalar(
                select(
                    exists().where(
                        snapshot_chunks.c.snapshot_id == resolved_snapshot_id
                    )
                )
            )
            if not member_exists:
                raise CatalogUnavailableError(
                    "the target snapshot has no validated corpus members"
                )
            import_condition = [
                embedding_imports.c.snapshot_id == resolved_snapshot_id,
                embedding_imports.c.status == "validated",
            ]
            if resolved_profile is not None:
                import_condition.append(
                    embedding_imports.c.profile_id == resolved_profile
                )
            import_exists = connection.scalar(select(exists().where(*import_condition)))
            if not import_exists:
                detail = (
                    " for the required profile" if resolved_profile is not None else ""
                )
                raise CatalogUnavailableError(
                    "the target snapshot has no validated embedding import" + detail
                )
            if same_target:
                if current is None or current_event is None:  # pragma: no cover
                    raise CatalogDataError("active snapshot event is missing")
                return SnapshotActivationResult(
                    selection=current,
                    event=current_event,
                    changed=False,
                )
            if rollback:
                was_active = connection.scalar(
                    select(
                        exists().where(
                            snapshot_activation_events.c.scope_id == resolved_scope_id,
                            snapshot_activation_events.c.target_snapshot_id
                            == resolved_snapshot_id,
                        )
                    )
                )
                if not was_active:
                    raise SnapshotActivationConflictError(
                        "rollback target has no prior activation event"
                    )

            occurred_at = connection.scalar(select(func.transaction_timestamp()))
            if not isinstance(occurred_at, datetime):
                raise CatalogDataError("database did not return a transaction time")
            revision = 1 if current is None else current.revision + 1
            activation_id = uuid.uuid4().hex
            operation = (
                "initial_activate"
                if current is None
                else ("rollback" if rollback else "replace")
            )
            previous_snapshot_id = current.snapshot_id if current is not None else None
            previous_activation_id = (
                current.activation_id if current is not None else None
            )
            event_values = {
                "activation_id": activation_id,
                "scope_id": resolved_scope_id,
                "revision": revision,
                "operation": operation,
                "previous_snapshot_id": previous_snapshot_id,
                "target_snapshot_id": resolved_snapshot_id,
                "previous_activation_id": previous_activation_id,
                "actor": resolved_actor,
                "reason": resolved_reason,
                "occurred_at": occurred_at,
            }
            connection.execute(
                insert(snapshot_activation_events).values(**event_values)
            )

            if current is not None:
                old_update = connection.execute(
                    update(corpus_snapshots)
                    .where(
                        corpus_snapshots.c.scope_id == resolved_scope_id,
                        corpus_snapshots.c.snapshot_id == current.snapshot_id,
                        corpus_snapshots.c.status == "active",
                    )
                    .values(status="validated")
                )
                if old_update.rowcount != 1:
                    raise SnapshotActivationConflictError(
                        "the active snapshot changed during replacement"
                    )
            target_update = connection.execute(
                update(corpus_snapshots)
                .where(
                    corpus_snapshots.c.scope_id == resolved_scope_id,
                    corpus_snapshots.c.snapshot_id == resolved_snapshot_id,
                    corpus_snapshots.c.status == "validated",
                )
                .values(status="active", activated_at=occurred_at)
            )
            if target_update.rowcount != 1:
                raise SnapshotActivationConflictError(
                    "the target snapshot changed during activation"
                )

            if current is None:
                connection.execute(
                    insert(active_snapshot_pointers).values(
                        scope_id=resolved_scope_id,
                        snapshot_id=resolved_snapshot_id,
                        revision=revision,
                        activation_id=activation_id,
                        updated_at=occurred_at,
                    )
                )
            else:
                pointer_update = connection.execute(
                    update(active_snapshot_pointers)
                    .where(
                        active_snapshot_pointers.c.scope_id == resolved_scope_id,
                        active_snapshot_pointers.c.snapshot_id == current.snapshot_id,
                        active_snapshot_pointers.c.revision == current.revision,
                        active_snapshot_pointers.c.activation_id
                        == current.activation_id,
                    )
                    .values(
                        snapshot_id=resolved_snapshot_id,
                        revision=revision,
                        activation_id=activation_id,
                        updated_at=occurred_at,
                    )
                )
                if pointer_update.rowcount != 1:
                    raise SnapshotActivationConflictError(
                        "the active pointer changed during replacement"
                    )

            event = SnapshotActivationEvent(**event_values)
            selection = ActiveSnapshotSelection(
                scope_id=resolved_scope_id,
                snapshot_id=resolved_snapshot_id,
                revision=revision,
                activation_id=activation_id,
                corpus_hash=target["corpus_hash"],
                activated_at=occurred_at,
            )
            return SnapshotActivationResult(
                selection=selection,
                event=event,
                changed=True,
            )

    @staticmethod
    def _event_from_row(row: Mapping[str, Any]) -> SnapshotActivationEvent:
        return SnapshotActivationEvent(
            activation_id=row["activation_id"],
            scope_id=row["scope_id"],
            revision=row["revision"],
            operation=row["operation"],
            previous_snapshot_id=row["previous_snapshot_id"],
            target_snapshot_id=row["target_snapshot_id"],
            previous_activation_id=row["previous_activation_id"],
            actor=row["actor"],
            reason=row["reason"],
            occurred_at=row["occurred_at"],
        )
