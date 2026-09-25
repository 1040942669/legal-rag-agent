from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Mapping, Sequence

from ..embeddings import (
    EmbeddingCache,
    EmbeddingModelConfig,
    validate_cache_matches_chunks,
)
from ..embedding_contracts import (
    EmbeddingProfileIdentity,
    EmbeddingVectorContractError,
    canonicalize_embedding_matrix,
    canonicalize_embedding_vector,
)
from ..models import Chunk, LawArticle


class StorageContractError(ValueError):
    """Raised before a malformed or ambiguous corpus reaches the database."""


PGVECTOR_VECTOR_MAX_DIMENSIONS = 16_000


def _required_text(
    value: str, field_name: str, *, max_length: int | None = None
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StorageContractError(f"{field_name} must be a non-empty string")
    if value != value.strip():
        raise StorageContractError(
            f"{field_name} must not contain surrounding whitespace"
        )
    if max_length is not None and len(value) > max_length:
        raise StorageContractError(f"{field_name} exceeds maximum length {max_length}")
    return value


def _parse_date(value: date | str | None, field_name: str) -> date | None:
    if value is None:
        return None
    if type(value) is date:
        return value
    if not isinstance(value, str):
        raise StorageContractError(f"{field_name} must be an ISO date or null")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise StorageContractError(f"{field_name} must be an ISO date") from exc


def canonical_json(value: Any) -> str:
    """Return a UTF-8-safe, deterministic JSON representation.

    ``allow_nan=False`` matters here: JSON containing NaN or infinity is not a
    stable cross-language storage contract.
    """

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise StorageContractError(f"value is not canonical JSON: {exc}") from exc


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


@dataclass(frozen=True)
class LawVersionSpec:
    """Explicit law identity supplied by a trusted source manifest.

    Titles are display labels, not persistent identities.  Unknown effective
    dates stay null and are never inferred from a filename or deprecation flag.
    """

    law_id: str
    version_id: str
    title: str
    verification_status: str
    source_ref: str
    valid_from: date | str | None = None
    valid_to: date | str | None = None

    def __post_init__(self) -> None:
        for field_name in ("law_id", "version_id", "title", "source_ref"):
            max_length = 255 if field_name in {"law_id", "version_id"} else None
            object.__setattr__(
                self,
                field_name,
                _required_text(
                    getattr(self, field_name), field_name, max_length=max_length
                ),
            )
        if self.verification_status not in {"unknown", "verified", "unverified"}:
            raise StorageContractError(
                "verification_status must be unknown, verified, or unverified"
            )
        valid_from = _parse_date(self.valid_from, "valid_from")
        valid_to = _parse_date(self.valid_to, "valid_to")
        if valid_from is not None and valid_to is not None and valid_from >= valid_to:
            raise StorageContractError(
                "valid_from must be earlier than valid_to for [valid_from, valid_to)"
            )
        object.__setattr__(self, "valid_from", valid_from)
        object.__setattr__(self, "valid_to", valid_to)


@dataclass(frozen=True)
class SnapshotRecord:
    snapshot_id: str
    scope_id: str
    source_manifest_json: str
    source_manifest_hash: str
    status: str = "validated"


@dataclass(frozen=True)
class LawVersionRecord:
    law_id: str
    version_id: str
    title: str
    valid_from: date | None
    valid_to: date | None
    verification_status: str
    source_ref: str
    content_hash: str


@dataclass(frozen=True)
class ArticleRecord:
    article_id: str
    law_id: str
    version_id: str
    article_number: str
    body: str
    raw_text: str
    source_ref: str
    source_line: int
    parse_status: str
    content_hash: str


@dataclass(frozen=True)
class ChunkRecord:
    chunk_id: str
    text: str
    strategy: str
    metadata_json: str
    content_hash: str
    recipe_hash: str


@dataclass(frozen=True)
class ChunkArticleRecord:
    chunk_id: str
    article_id: str
    ordinal: int


@dataclass(frozen=True)
class SnapshotChunkRecord:
    snapshot_id: str
    chunk_id: str
    ordinal: int


@dataclass(frozen=True)
class EmbeddingProfileRecord:
    profile_id: str
    provider: str
    model: str
    revision: str
    dimensions: int
    normalization: bool
    query_prefix: str
    document_prefix: str
    embed_with_metadata: bool
    recipe_hash: str

    def to_identity(self) -> EmbeddingProfileIdentity:
        try:
            identity = EmbeddingProfileIdentity(
                provider=self.provider,
                model=self.model,
                revision=self.revision,
                dimensions=self.dimensions,
                normalization=self.normalization,
                query_prefix=self.query_prefix,
                document_prefix=self.document_prefix,
                embed_with_metadata=self.embed_with_metadata,
            )
        except ValueError as exc:
            raise StorageContractError(f"embedding profile is invalid: {exc}") from exc
        if identity.recipe_hash != self.recipe_hash:
            raise StorageContractError("embedding profile recipe hash mismatch")
        if identity.profile_id != self.profile_id:
            raise StorageContractError("embedding profile identity mismatch")
        return identity


@dataclass(frozen=True)
class ChunkEmbeddingRecord:
    chunk_id: str
    profile_id: str
    embedding: tuple[float, ...]
    embedding_hash: str


@dataclass(frozen=True)
class StorageImportBundle:
    snapshot: SnapshotRecord
    law_versions: tuple[LawVersionRecord, ...]
    articles: tuple[ArticleRecord, ...]
    chunks: tuple[ChunkRecord, ...]
    chunk_articles: tuple[ChunkArticleRecord, ...]
    snapshot_chunks: tuple[SnapshotChunkRecord, ...]
    embedding_profile: EmbeddingProfileRecord
    embeddings: tuple[ChunkEmbeddingRecord, ...]
    corpus_hash: str
    bundle_hash: str


def _unique_by_id(items: Sequence[Any], field_name: str, label: str) -> None:
    seen: set[str] = set()
    for item in items:
        item_id = getattr(item, field_name)
        if item_id in seen:
            raise StorageContractError(f"duplicate {label}: {item_id}")
        seen.add(item_id)


def _law_records(
    laws: Sequence[LawVersionSpec], articles: Sequence[ArticleRecord]
) -> tuple[LawVersionRecord, ...]:
    _unique_by_id(laws, "version_id", "version_id")
    records: list[LawVersionRecord] = []
    for law in laws:
        law_articles = [
            article for article in articles if article.version_id == law.version_id
        ]
        payload = {
            "law_id": law.law_id,
            "version_id": law.version_id,
            "title": law.title,
            "valid_from": law.valid_from.isoformat() if law.valid_from else None,
            "valid_to": law.valid_to.isoformat() if law.valid_to else None,
            "verification_status": law.verification_status,
            "source_ref": law.source_ref,
            "articles": [
                {
                    "article_id": article.article_id,
                    "article_number": article.article_number,
                    "content_hash": article.content_hash,
                }
                for article in law_articles
            ],
        }
        records.append(
            LawVersionRecord(
                law_id=law.law_id,
                version_id=law.version_id,
                title=law.title,
                valid_from=law.valid_from,
                valid_to=law.valid_to,
                verification_status=law.verification_status,
                source_ref=law.source_ref,
                content_hash=sha256_json(payload),
            )
        )
    return tuple(records)


def _article_records(
    articles: Sequence[LawArticle],
    laws: Sequence[LawVersionSpec],
    article_version_ids: Mapping[str, str] | None,
) -> tuple[ArticleRecord, ...]:
    _unique_by_id(articles, "article_id", "article_id")
    article_ids = {article.article_id for article in articles}
    unexpected_mapping_ids = set(article_version_ids or {}) - article_ids
    if unexpected_mapping_ids:
        raise StorageContractError(
            "article_version_ids contains unknown article_id values: "
            + ", ".join(sorted(unexpected_mapping_ids))
        )
    laws_by_title: dict[str, list[LawVersionSpec]] = {}
    laws_by_version = {law.version_id: law for law in laws}
    for law in laws:
        laws_by_title.setdefault(law.title, []).append(law)

    records: list[ArticleRecord] = []
    for article in articles:
        _required_text(article.article_id, "article_id", max_length=255)
        source_ref = _required_text(article.source_file, "source_ref")
        if len(article.article_number) > 128:
            raise StorageContractError("article_number exceeds maximum length 128")
        if len(article.parse_status) > 64:
            raise StorageContractError("parse_status exceeds maximum length 64")
        explicit_version = (article_version_ids or {}).get(article.article_id)
        if explicit_version is not None:
            law = laws_by_version.get(explicit_version)
            if law is None:
                raise StorageContractError(
                    f"article {article.article_id} references unknown version_id "
                    f"{explicit_version}"
                )
            if law.title != article.law_name:
                raise StorageContractError(
                    f"article {article.article_id} title does not match version_id "
                    f"{explicit_version}"
                )
        else:
            candidates = laws_by_title.get(article.law_name, [])
            if len(candidates) != 1:
                raise StorageContractError(
                    f"article {article.article_id} cannot resolve one explicit law version"
                )
            law = candidates[0]
        if type(article.line_no) is not int or article.line_no <= 0:
            raise StorageContractError(
                f"article {article.article_id} source line must be positive"
            )
        payload = {
            "article_id": article.article_id,
            "law_id": law.law_id,
            "version_id": law.version_id,
            "article_number": article.article_number,
            "body": article.body,
            "raw_text": article.raw_text,
            "source_ref": source_ref,
            "source_line": article.line_no,
            "parse_status": article.parse_status,
        }
        records.append(ArticleRecord(content_hash=sha256_json(payload), **payload))
    seen_number_keys: set[tuple[str, str]] = set()
    for record in records:
        if not record.article_number:
            continue
        key = (record.version_id, record.article_number)
        if key in seen_number_keys:
            raise StorageContractError(
                f"duplicate article_number {record.article_number!r} in version "
                f"{record.version_id!r}"
            )
        seen_number_keys.add(key)
    return tuple(records)


def _chunk_article_ids(chunk: Chunk) -> tuple[str, ...]:
    article_ids = chunk.metadata.get("article_ids")
    if article_ids is None and chunk.metadata.get("article_id") is not None:
        article_ids = [chunk.metadata["article_id"]]
    if not isinstance(article_ids, (list, tuple)) or not article_ids:
        raise StorageContractError(
            f"chunk {chunk.chunk_id} lacks an explicit ordered article relation"
        )
    if any(not isinstance(item, str) or not item for item in article_ids):
        raise StorageContractError(
            f"chunk {chunk.chunk_id} has an invalid article relation"
        )
    if len(set(article_ids)) != len(article_ids):
        raise StorageContractError(
            f"chunk {chunk.chunk_id} repeats an article relation"
        )
    return tuple(article_ids)


def _profile_record(
    model_config: EmbeddingModelConfig,
    *,
    model_revision: str,
    actual_dimensions: int,
) -> EmbeddingProfileRecord:
    revision = _required_text(model_revision, "model_revision")
    configured_revision = _required_text(model_config.revision, "model_config.revision")
    if configured_revision != revision:
        raise StorageContractError(
            "model_revision does not match model_config.revision"
        )
    if model_config.dimensions is None:
        raise StorageContractError(
            "model_config.dimensions must be explicit for a storage profile"
        )
    if model_config.dimensions != actual_dimensions:
        raise StorageContractError(
            "embedding dimension does not match model_config.dimensions"
        )
    if actual_dimensions > PGVECTOR_VECTOR_MAX_DIMENSIONS:
        raise StorageContractError(
            f"embedding dimension {actual_dimensions} exceeds pgvector vector "
            f"storage limit {PGVECTOR_VECTOR_MAX_DIMENSIONS}; choose an explicit "
            "supported representation instead of truncating"
        )
    identity = EmbeddingProfileIdentity(
        provider=model_config.provider,
        model=model_config.model_name,
        revision=revision,
        dimensions=actual_dimensions,
        normalization=model_config.normalize,
        query_prefix=model_config.query_prefix,
        document_prefix=model_config.document_prefix,
        embed_with_metadata=model_config.embed_with_metadata,
    )
    return EmbeddingProfileRecord(
        profile_id=identity.profile_id,
        provider=_required_text(model_config.provider, "provider", max_length=128),
        model=_required_text(model_config.model_name, "model_name"),
        revision=revision,
        dimensions=actual_dimensions,
        normalization=model_config.normalize,
        query_prefix=model_config.query_prefix,
        document_prefix=model_config.document_prefix,
        embed_with_metadata=model_config.embed_with_metadata,
        recipe_hash=identity.recipe_hash,
    )


def _embedding_records(
    cache: EmbeddingCache, profile: EmbeddingProfileRecord
) -> tuple[ChunkEmbeddingRecord, ...]:
    try:
        import numpy as np
    except ModuleNotFoundError as exc:  # pragma: no cover - project requires numpy.
        raise RuntimeError("building a storage bundle requires numpy") from exc

    try:
        matrix = canonicalize_embedding_matrix(
            cache.vectors,
            expected_dimension=profile.dimensions,
            normalized=profile.normalization,
            label="embedding cache",
            require_float32=True,
        )
    except EmbeddingVectorContractError as exc:
        raise StorageContractError(str(exc)) from exc
    records: list[ChunkEmbeddingRecord] = []
    for chunk_id, row in zip(cache.chunk_ids, matrix, strict=True):
        canonical_row = np.ascontiguousarray(row, dtype="<f4")
        records.append(
            ChunkEmbeddingRecord(
                chunk_id=chunk_id,
                profile_id=profile.profile_id,
                embedding=tuple(float(item) for item in canonical_row.tolist()),
                embedding_hash=hashlib.sha256(
                    canonical_row.tobytes(order="C")
                ).hexdigest(),
            )
        )
    return tuple(records)


def build_storage_import_bundle(
    *,
    snapshot_id: str,
    scope_id: str,
    source_manifest: Mapping[str, Any],
    laws: Sequence[LawVersionSpec],
    articles: Sequence[LawArticle],
    chunks: Sequence[Chunk],
    embedding_cache: EmbeddingCache,
    model_config: EmbeddingModelConfig,
    model_revision: str,
    chunk_recipe: Mapping[str, Any],
    article_version_ids: Mapping[str, str] | None = None,
) -> StorageImportBundle:
    """Validate and freeze a complete, database-ready corpus import.

    The function is deliberately pure and performs no model or database calls.
    Any ambiguity is rejected before the repository starts a transaction.
    """

    resolved_snapshot_id = _required_text(snapshot_id, "snapshot_id", max_length=255)
    resolved_scope_id = _required_text(scope_id, "scope_id", max_length=255)
    if not laws:
        raise StorageContractError("at least one explicit law version is required")
    if not articles:
        raise StorageContractError("at least one article is required")
    if not chunks:
        raise StorageContractError("at least one chunk is required")
    if model_config.trust_remote_code:
        raise StorageContractError(
            "version-bound storage does not allow trust_remote_code"
        )
    _unique_by_id(chunks, "chunk_id", "chunk_id")

    source_manifest_json = canonical_json(source_manifest)
    chunk_recipe_json = canonical_json(chunk_recipe)
    chunk_recipe_hash = sha256_text(chunk_recipe_json)

    # Reuse the existing strict cache contract before adding M3-specific fields.
    try:
        validate_cache_matches_chunks(
            embedding_cache, list(chunks), model_config=model_config
        )
    except ValueError as exc:
        raise StorageContractError(str(exc)) from exc
    if embedding_cache.metadata.get("revision") != model_revision:
        raise StorageContractError(
            "embedding cache revision does not match model_revision"
        )

    shape = getattr(embedding_cache.vectors, "shape", ())
    if len(shape) != 2:
        raise StorageContractError("embedding cache must be a 2D matrix")
    profile = _profile_record(
        model_config,
        model_revision=model_revision,
        actual_dimensions=int(shape[1]),
    )
    article_records = _article_records(articles, laws, article_version_ids)
    law_records = _law_records(laws, article_records)
    known_article_ids = {article.article_id for article in article_records}

    chunk_records: list[ChunkRecord] = []
    chunk_articles: list[ChunkArticleRecord] = []
    snapshot_chunks: list[SnapshotChunkRecord] = []
    for chunk_ordinal, chunk in enumerate(chunks):
        metadata_json = canonical_json(chunk.metadata)
        content_hash = sha256_json(
            {
                "chunk_id": chunk.chunk_id,
                "text": chunk.text,
                "strategy": chunk.strategy,
                "metadata": json.loads(metadata_json),
                "recipe_hash": chunk_recipe_hash,
            }
        )
        chunk_records.append(
            ChunkRecord(
                chunk_id=_required_text(chunk.chunk_id, "chunk_id", max_length=255),
                text=chunk.text,
                strategy=_required_text(
                    chunk.strategy, "chunk strategy", max_length=64
                ),
                metadata_json=metadata_json,
                content_hash=content_hash,
                recipe_hash=chunk_recipe_hash,
            )
        )
        for relation_ordinal, article_id in enumerate(_chunk_article_ids(chunk)):
            if article_id not in known_article_ids:
                raise StorageContractError(
                    f"chunk {chunk.chunk_id} references unknown article_id {article_id}"
                )
            chunk_articles.append(
                ChunkArticleRecord(
                    chunk_id=chunk.chunk_id,
                    article_id=article_id,
                    ordinal=relation_ordinal,
                )
            )
        snapshot_chunks.append(
            SnapshotChunkRecord(
                snapshot_id=resolved_snapshot_id,
                chunk_id=chunk.chunk_id,
                ordinal=chunk_ordinal,
            )
        )

    embeddings = _embedding_records(embedding_cache, profile)
    snapshot = SnapshotRecord(
        snapshot_id=resolved_snapshot_id,
        scope_id=resolved_scope_id,
        source_manifest_json=source_manifest_json,
        source_manifest_hash=sha256_text(source_manifest_json),
    )
    corpus_payload = {
        "snapshot": asdict(snapshot),
        "law_versions": [
            {
                **asdict(item),
                "valid_from": item.valid_from.isoformat() if item.valid_from else None,
                "valid_to": item.valid_to.isoformat() if item.valid_to else None,
            }
            for item in law_records
        ],
        "articles": [asdict(item) for item in article_records],
        "chunks": [asdict(item) for item in chunk_records],
        "chunk_articles": [asdict(item) for item in chunk_articles],
        "snapshot_chunks": [asdict(item) for item in snapshot_chunks],
    }
    corpus_hash = sha256_json(corpus_payload)
    bundle_payload = {
        "corpus_hash": corpus_hash,
        "embedding_profile": asdict(profile),
        "embeddings": [
            {
                "chunk_id": item.chunk_id,
                "profile_id": item.profile_id,
                "embedding_hash": item.embedding_hash,
            }
            for item in embeddings
        ],
    }
    bundle = StorageImportBundle(
        snapshot=snapshot,
        law_versions=law_records,
        articles=article_records,
        chunks=tuple(chunk_records),
        chunk_articles=tuple(chunk_articles),
        snapshot_chunks=tuple(snapshot_chunks),
        embedding_profile=profile,
        embeddings=embeddings,
        corpus_hash=corpus_hash,
        bundle_hash=sha256_json(bundle_payload),
    )
    validate_storage_import_bundle(bundle)
    return bundle


def validate_storage_import_bundle(bundle: StorageImportBundle) -> None:
    """Recompute every identity and relationship in an import bundle.

    ``frozen=True`` prevents in-place assignment but does not make construction
    trusted: callers can still use ``dataclasses.replace``.  Repositories call
    this verifier before opening a write transaction.
    """

    snapshot = bundle.snapshot
    for value, field_name in (
        (snapshot.snapshot_id, "snapshot_id"),
        (snapshot.scope_id, "scope_id"),
    ):
        _required_text(value, field_name, max_length=255)
    try:
        source_manifest = json.loads(snapshot.source_manifest_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StorageContractError("source manifest JSON is invalid") from exc
    if canonical_json(source_manifest) != snapshot.source_manifest_json:
        raise StorageContractError("source manifest JSON is not canonical")
    if sha256_text(snapshot.source_manifest_json) != snapshot.source_manifest_hash:
        raise StorageContractError("source manifest hash mismatch")
    if snapshot.status != "validated":
        raise StorageContractError("import bundle snapshot must be validated")

    _unique_by_id(bundle.law_versions, "version_id", "version_id")
    _unique_by_id(bundle.articles, "article_id", "article_id")
    _unique_by_id(bundle.chunks, "chunk_id", "chunk_id")
    law_by_version = {item.version_id: item for item in bundle.law_versions}
    article_by_id = {item.article_id: item for item in bundle.articles}
    chunk_by_id = {item.chunk_id: item for item in bundle.chunks}

    seen_number_keys: set[tuple[str, str]] = set()
    for article in bundle.articles:
        for value, field_name in (
            (article.article_id, "article_id"),
            (article.law_id, "law_id"),
            (article.version_id, "version_id"),
            (article.source_ref, "source_ref"),
        ):
            max_length = (
                255 if field_name in {"article_id", "law_id", "version_id"} else None
            )
            _required_text(value, field_name, max_length=max_length)
        if len(article.article_number) > 128:
            raise StorageContractError("article_number exceeds maximum length 128")
        if len(article.parse_status) > 64:
            raise StorageContractError("parse_status exceeds maximum length 64")
        law = law_by_version.get(article.version_id)
        if law is None or law.law_id != article.law_id:
            raise StorageContractError(
                f"article {article.article_id} has an invalid law/version relation"
            )
        if article.article_number:
            number_key = (article.version_id, article.article_number)
            if number_key in seen_number_keys:
                raise StorageContractError(
                    f"duplicate article_number {article.article_number!r} in version "
                    f"{article.version_id!r}"
                )
            seen_number_keys.add(number_key)
        article_payload = {
            "article_id": article.article_id,
            "law_id": article.law_id,
            "version_id": article.version_id,
            "article_number": article.article_number,
            "body": article.body,
            "raw_text": article.raw_text,
            "source_ref": article.source_ref,
            "source_line": article.source_line,
            "parse_status": article.parse_status,
        }
        if sha256_json(article_payload) != article.content_hash:
            raise StorageContractError(
                f"article {article.article_id} content hash mismatch"
            )

    for law in bundle.law_versions:
        for value, field_name in (
            (law.law_id, "law_id"),
            (law.version_id, "version_id"),
            (law.title, "title"),
            (law.source_ref, "source_ref"),
        ):
            max_length = 255 if field_name in {"law_id", "version_id"} else None
            _required_text(value, field_name, max_length=max_length)
        if law.valid_from is not None and type(law.valid_from) is not date:
            raise StorageContractError("valid_from must be a date, not a datetime")
        if law.valid_to is not None and type(law.valid_to) is not date:
            raise StorageContractError("valid_to must be a date, not a datetime")
        if (
            law.valid_from is not None
            and law.valid_to is not None
            and law.valid_from >= law.valid_to
        ):
            raise StorageContractError("law validity interval is not half-open")
        law_articles_for_hash = [
            article
            for article in bundle.articles
            if article.version_id == law.version_id
        ]
        law_payload = {
            "law_id": law.law_id,
            "version_id": law.version_id,
            "title": law.title,
            "valid_from": law.valid_from.isoformat() if law.valid_from else None,
            "valid_to": law.valid_to.isoformat() if law.valid_to else None,
            "verification_status": law.verification_status,
            "source_ref": law.source_ref,
            "articles": [
                {
                    "article_id": article.article_id,
                    "article_number": article.article_number,
                    "content_hash": article.content_hash,
                }
                for article in law_articles_for_hash
            ],
        }
        if sha256_json(law_payload) != law.content_hash:
            raise StorageContractError(
                f"law version {law.version_id} content hash mismatch"
            )

    relations_by_chunk: dict[str, list[ChunkArticleRecord]] = {
        chunk_id: [] for chunk_id in chunk_by_id
    }
    seen_relations: set[tuple[str, str]] = set()
    for relation in bundle.chunk_articles:
        if relation.chunk_id not in chunk_by_id:
            raise StorageContractError("chunk relation references unknown chunk_id")
        if relation.article_id not in article_by_id:
            raise StorageContractError("chunk relation references unknown article_id")
        key = (relation.chunk_id, relation.article_id)
        if key in seen_relations:
            raise StorageContractError("duplicate chunk/article relation")
        seen_relations.add(key)
        relations_by_chunk[relation.chunk_id].append(relation)

    for chunk in bundle.chunks:
        _required_text(chunk.chunk_id, "chunk_id", max_length=255)
        _required_text(chunk.strategy, "chunk strategy", max_length=64)
        try:
            metadata = json.loads(chunk.metadata_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise StorageContractError(
                f"chunk {chunk.chunk_id} metadata JSON is invalid"
            ) from exc
        if canonical_json(metadata) != chunk.metadata_json:
            raise StorageContractError(
                f"chunk {chunk.chunk_id} metadata JSON is not canonical"
            )
        if (
            sha256_json(
                {
                    "chunk_id": chunk.chunk_id,
                    "text": chunk.text,
                    "strategy": chunk.strategy,
                    "metadata": metadata,
                    "recipe_hash": chunk.recipe_hash,
                }
            )
            != chunk.content_hash
        ):
            raise StorageContractError(f"chunk {chunk.chunk_id} content hash mismatch")
        relations = sorted(
            relations_by_chunk[chunk.chunk_id], key=lambda item: item.ordinal
        )
        if not relations or [item.ordinal for item in relations] != list(
            range(len(relations))
        ):
            raise StorageContractError(
                f"chunk {chunk.chunk_id} article relation order is invalid"
            )
        metadata_article_ids = metadata.get("article_ids")
        if metadata_article_ids is None and metadata.get("article_id") is not None:
            metadata_article_ids = [metadata["article_id"]]
        if [item.article_id for item in relations] != metadata_article_ids:
            raise StorageContractError(
                f"chunk {chunk.chunk_id} article relations do not match metadata"
            )

    expected_members = [
        (snapshot.snapshot_id, chunk.chunk_id, ordinal)
        for ordinal, chunk in enumerate(bundle.chunks)
    ]
    actual_members = [
        (item.snapshot_id, item.chunk_id, item.ordinal)
        for item in bundle.snapshot_chunks
    ]
    if actual_members != expected_members:
        raise StorageContractError("snapshot chunk membership or order mismatch")

    profile = bundle.embedding_profile
    if profile.dimensions <= 0 or profile.dimensions > PGVECTOR_VECTOR_MAX_DIMENSIONS:
        raise StorageContractError(
            f"embedding profile dimensions must be between 1 and "
            f"{PGVECTOR_VECTOR_MAX_DIMENSIONS}"
        )
    profile.to_identity()

    embeddings_by_chunk: dict[str, ChunkEmbeddingRecord] = {}
    for embedding in bundle.embeddings:
        if embedding.chunk_id not in chunk_by_id:
            raise StorageContractError("embedding references unknown chunk_id")
        if embedding.chunk_id in embeddings_by_chunk:
            raise StorageContractError("duplicate chunk embedding")
        if embedding.profile_id != profile.profile_id:
            raise StorageContractError("embedding profile relation mismatch")
        try:
            canonical_row = canonicalize_embedding_vector(
                embedding.embedding,
                expected_dimension=profile.dimensions,
                normalized=profile.normalization,
                label=f"embedding {embedding.chunk_id}",
            )
        except EmbeddingVectorContractError as exc:
            raise StorageContractError(str(exc)) from exc
        if hashlib.sha256(canonical_row.tobytes(order="C")).hexdigest() != (
            embedding.embedding_hash
        ):
            raise StorageContractError(f"embedding {embedding.chunk_id} hash mismatch")
        embeddings_by_chunk[embedding.chunk_id] = embedding
    if set(embeddings_by_chunk) != set(chunk_by_id):
        raise StorageContractError("embedding coverage does not match chunks")

    law_payloads = [
        {
            **asdict(item),
            "valid_from": item.valid_from.isoformat() if item.valid_from else None,
            "valid_to": item.valid_to.isoformat() if item.valid_to else None,
        }
        for item in bundle.law_versions
    ]
    corpus_payload = {
        "snapshot": asdict(snapshot),
        "law_versions": law_payloads,
        "articles": [asdict(item) for item in bundle.articles],
        "chunks": [asdict(item) for item in bundle.chunks],
        "chunk_articles": [asdict(item) for item in bundle.chunk_articles],
        "snapshot_chunks": [asdict(item) for item in bundle.snapshot_chunks],
    }
    if sha256_json(corpus_payload) != bundle.corpus_hash:
        raise StorageContractError("corpus hash mismatch")
    bundle_payload = {
        "corpus_hash": bundle.corpus_hash,
        "embedding_profile": asdict(profile),
        "embeddings": [
            {
                "chunk_id": item.chunk_id,
                "profile_id": item.profile_id,
                "embedding_hash": item.embedding_hash,
            }
            for item in bundle.embeddings
        ],
    }
    if sha256_json(bundle_payload) != bundle.bundle_hash:
        raise StorageContractError("bundle hash mismatch")
