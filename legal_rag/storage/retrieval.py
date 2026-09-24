from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence

from sqlalchemy import Engine, and_, exists, select

from ..embedding_contracts import (
    EmbeddingProfileIdentity,
    EmbeddingVectorContractError,
    canonicalize_embedding_vector,
)
from ..models import Chunk, SearchResult
from ..provider_errors import ProviderCallError, raise_sanitized_provider_error
from ..retrieval_contracts import (
    RetrievalBoundary,
    RetrievalContractError,
    RetrievalProvenance,
    article_provenance_from_mapping,
    chunk_payload_fingerprint,
    validate_retrieval_top_k,
)
from .contracts import sha256_json
from .schema import (
    chunk_articles,
    chunk_embeddings,
    chunks,
    corpus_snapshots,
    embedding_imports,
    embedding_profiles,
    law_articles,
    law_versions,
    snapshot_chunks,
)


class RetrievalUnavailableError(RuntimeError):
    """The requested immutable snapshot/profile is not ready to serve."""


class RetrievalDataError(RuntimeError):
    """Persisted rows violate the storage contract required for retrieval."""


SERVICEABLE_SNAPSHOT_STATUSES = frozenset({"validated", "active"})


RetrievalFilters = RetrievalBoundary


class QueryEncoder(Protocol):
    profile: EmbeddingProfileIdentity

    def encode_query(self, text: str) -> Any: ...


def _validate_top_k(top_k: int) -> int:
    return validate_retrieval_top_k(top_k)


def _canonical_query_vector(value: Any, profile: EmbeddingProfileIdentity) -> Any:
    try:
        return canonicalize_embedding_vector(
            value,
            expected_dimension=profile.dimensions,
            normalized=profile.normalization,
            label="query vector",
        )
    except EmbeddingVectorContractError as exc:
        raise RetrievalContractError(str(exc)) from exc


def _unique(values: Sequence[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[Any] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


@dataclass(frozen=True, slots=True)
class BoundCorpusEntry:
    chunk: Chunk
    provenance: RetrievalProvenance


@dataclass(frozen=True, slots=True)
class BoundCorpus:
    boundary: RetrievalBoundary
    entries: tuple[BoundCorpusEntry, ...]

    @property
    def chunks(self) -> list[Chunk]:
        # Legacy lexical retrievers keep their input ``Chunk`` objects and the
        # nested lists/dicts on ``Chunk`` are mutable.  Never let that legacy
        # view alias the database-authoritative corpus snapshot.
        return [deepcopy(entry.chunk) for entry in self.entries]


class PostgresExactRetrievalRepository:
    """Exact inner-product retrieval over one immutable snapshot/profile."""

    def __init__(self, engine: Engine) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("PostgresExactRetrievalRepository requires PostgreSQL")
        self.engine = engine

    def validate_context(
        self,
        filters: RetrievalFilters,
        *,
        expected_profile: EmbeddingProfileIdentity,
    ) -> EmbeddingProfileIdentity:
        with self.engine.connect() as connection:
            profile = self._validated_context(
                connection, filters, expected_profile=expected_profile
            )
        return profile

    def search_vector(
        self,
        query_vector: Any,
        *,
        top_k: int,
        filters: RetrievalFilters,
        expected_profile: EmbeddingProfileIdentity,
    ) -> list[SearchResult]:
        resolved_top_k = _validate_top_k(top_k)
        if filters.profile_id != expected_profile.profile_id:
            raise RetrievalContractError(
                "expected embedding profile does not match bound profile_id"
            )
        vector = _canonical_query_vector(query_vector, expected_profile)

        with self.engine.connect() as connection:
            profile = self._validated_context(
                connection, filters, expected_profile=expected_profile
            )
            if profile != expected_profile:
                raise RetrievalDataError(
                    "validated embedding profile changed during query"
                )
            if filters.denies_all:
                return []

            distance = chunk_embeddings.c.embedding.max_inner_product(
                vector.tolist()
            ).label("distance")
            relation_exists = self._relation_filter(filters)
            from_clause = (
                chunk_embeddings.join(
                    chunks, chunks.c.chunk_id == chunk_embeddings.c.chunk_id
                )
                .join(
                    snapshot_chunks,
                    snapshot_chunks.c.chunk_id == chunk_embeddings.c.chunk_id,
                )
                .join(
                    corpus_snapshots,
                    corpus_snapshots.c.snapshot_id == snapshot_chunks.c.snapshot_id,
                )
                .join(
                    embedding_imports,
                    and_(
                        embedding_imports.c.snapshot_id
                        == snapshot_chunks.c.snapshot_id,
                        embedding_imports.c.profile_id == chunk_embeddings.c.profile_id,
                    ),
                )
            )
            statement = (
                select(
                    chunks.c.chunk_id,
                    chunks.c.text,
                    chunks.c.strategy,
                    chunks.c.metadata.label("chunk_metadata"),
                    chunks.c.content_hash.label("chunk_content_hash"),
                    chunks.c.recipe_hash.label("chunk_recipe_hash"),
                    snapshot_chunks.c.ordinal.label("snapshot_ordinal"),
                    chunk_embeddings.c.embedding.label("embedding"),
                    chunk_embeddings.c.embedding_hash.label("embedding_hash"),
                    distance,
                )
                .select_from(from_clause)
                .where(
                    snapshot_chunks.c.snapshot_id == filters.snapshot_id,
                    corpus_snapshots.c.scope_id == filters.scope_id,
                    corpus_snapshots.c.status.in_(SERVICEABLE_SNAPSHOT_STATUSES),
                    chunk_embeddings.c.profile_id == filters.profile_id,
                    embedding_imports.c.status == "validated",
                )
                .order_by(
                    distance.asc(),
                    snapshot_chunks.c.ordinal.asc(),
                    chunks.c.chunk_id.asc(),
                )
                .limit(resolved_top_k)
            )
            if relation_exists is not None:
                statement = statement.where(relation_exists)
            rows = connection.execute(statement).mappings().all()
            relation_rows = self._load_relations(
                connection, [str(row["chunk_id"]) for row in rows]
            )

        results: list[SearchResult] = []
        filter_trace = filters.trace_payload()
        boundary_fingerprint = filters.fingerprint
        for rank, row in enumerate(rows, start=1):
            chunk_id = str(row["chunk_id"])
            relations = relation_rows.get(chunk_id)
            if not relations:
                raise RetrievalDataError(
                    f"chunk {chunk_id!r} has no persisted article relations"
                )
            distance_value = float(row["distance"])
            score = -distance_value
            if score == 0.0:
                score = 0.0
            chunk = self._hydrate_chunk(row, relations, filters)
            provenance = self._build_provenance(row, relations, filters, chunk, profile)
            results.append(
                SearchResult(
                    chunk=chunk,
                    score=score,
                    rank=rank,
                    retriever="pgvector_exact",
                    trace={
                        "score_kind": "inner_product",
                        "raw_score": score,
                        "pgvector_distance": distance_value,
                        "pgvector_distance_kind": "negative_inner_product",
                        "pgvector_operator": "<#>",
                        "search_mode": "exact",
                        "snapshot_ordinal": int(row["snapshot_ordinal"]),
                        "tie_break": ["snapshot_ordinal", "chunk_id"],
                        "filters": filter_trace,
                        "boundary_fingerprint": boundary_fingerprint,
                    },
                    provenance=provenance,
                )
            )
        return results

    def load_bound_corpus(
        self,
        *,
        filters: RetrievalFilters,
        expected_profile: EmbeddingProfileIdentity,
    ) -> BoundCorpus:
        """Load a fully scoped corpus plus database-authoritative provenance.

        All hard selectors run in PostgreSQL before the chunk set is exposed.
        This avoids building BM25 over a broader corpus and filtering only
        after RRF fusion.
        """

        if filters.profile_id != expected_profile.profile_id:
            raise RetrievalContractError(
                "expected embedding profile does not match bound profile_id"
            )
        with self.engine.connect() as connection:
            profile = self._validated_context(
                connection, filters, expected_profile=expected_profile
            )
            if filters.denies_all:
                return BoundCorpus(boundary=filters, entries=())
            from_clause = (
                chunk_embeddings.join(
                    chunks, chunks.c.chunk_id == chunk_embeddings.c.chunk_id
                )
                .join(
                    snapshot_chunks,
                    snapshot_chunks.c.chunk_id == chunk_embeddings.c.chunk_id,
                )
                .join(
                    corpus_snapshots,
                    corpus_snapshots.c.snapshot_id == snapshot_chunks.c.snapshot_id,
                )
                .join(
                    embedding_imports,
                    and_(
                        embedding_imports.c.snapshot_id
                        == snapshot_chunks.c.snapshot_id,
                        embedding_imports.c.profile_id == chunk_embeddings.c.profile_id,
                    ),
                )
            )
            statement = (
                select(
                    chunks.c.chunk_id,
                    chunks.c.text,
                    chunks.c.strategy,
                    chunks.c.metadata.label("chunk_metadata"),
                    chunks.c.content_hash.label("chunk_content_hash"),
                    chunks.c.recipe_hash.label("chunk_recipe_hash"),
                    snapshot_chunks.c.ordinal.label("snapshot_ordinal"),
                    chunk_embeddings.c.embedding.label("embedding"),
                    chunk_embeddings.c.embedding_hash.label("embedding_hash"),
                )
                .select_from(from_clause)
                .where(
                    snapshot_chunks.c.snapshot_id == filters.snapshot_id,
                    corpus_snapshots.c.scope_id == filters.scope_id,
                    corpus_snapshots.c.status.in_(SERVICEABLE_SNAPSHOT_STATUSES),
                    chunk_embeddings.c.profile_id == filters.profile_id,
                    embedding_imports.c.status == "validated",
                )
                .order_by(snapshot_chunks.c.ordinal.asc(), chunks.c.chunk_id.asc())
            )
            relation_filter = self._relation_filter(filters)
            if relation_filter is not None:
                statement = statement.where(relation_filter)
            rows = connection.execute(statement).mappings().all()
            relation_rows = self._load_relations(
                connection, [str(row["chunk_id"]) for row in rows]
            )
        loaded: list[BoundCorpusEntry] = []
        for row in rows:
            chunk_id = str(row["chunk_id"])
            relations = relation_rows.get(chunk_id)
            if not relations:
                raise RetrievalDataError(
                    f"chunk {chunk_id!r} has no persisted article relations"
                )
            chunk = self._hydrate_chunk(row, relations, filters)
            loaded.append(
                BoundCorpusEntry(
                    chunk=chunk,
                    provenance=self._build_provenance(
                        row, relations, filters, chunk, profile
                    ),
                )
            )
        return BoundCorpus(boundary=filters, entries=tuple(loaded))

    def load_chunks(
        self,
        *,
        filters: RetrievalFilters,
        expected_profile: EmbeddingProfileIdentity,
    ) -> list[Chunk]:
        """Compatibility view; bound lexical wrappers need ``load_bound_corpus``."""

        return self.load_bound_corpus(
            filters=filters, expected_profile=expected_profile
        ).chunks

    @staticmethod
    def _validated_context(
        connection,
        filters: RetrievalFilters,
        *,
        expected_profile: EmbeddingProfileIdentity,
    ) -> EmbeddingProfileIdentity:
        snapshot = (
            connection.execute(
                select(
                    corpus_snapshots.c.snapshot_id,
                    corpus_snapshots.c.scope_id,
                    corpus_snapshots.c.status,
                ).where(
                    corpus_snapshots.c.snapshot_id == filters.snapshot_id,
                    corpus_snapshots.c.scope_id == filters.scope_id,
                )
            )
            .mappings()
            .one_or_none()
        )
        if snapshot is None or snapshot["status"] not in SERVICEABLE_SNAPSHOT_STATUSES:
            raise RetrievalUnavailableError(
                "requested snapshot is not serviceable in the requested scope"
            )

        profile = (
            connection.execute(
                select(embedding_profiles).where(
                    embedding_profiles.c.profile_id == filters.profile_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if profile is None:
            raise RetrievalUnavailableError(
                "requested embedding profile does not exist"
            )
        if expected_profile.profile_id != filters.profile_id:
            raise RetrievalContractError(
                "expected embedding profile does not match bound profile_id"
            )
        try:
            stored_identity = EmbeddingProfileIdentity(
                provider=profile["provider"],
                model=profile["model"],
                revision=profile["revision"],
                dimensions=profile["dimensions"],
                normalization=profile["normalization"],
                query_prefix=profile["query_prefix"],
                document_prefix=profile["document_prefix"],
                embed_with_metadata=profile["embed_with_metadata"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RetrievalDataError("stored embedding profile is invalid") from exc
        if (
            stored_identity.profile_id != profile["profile_id"]
            or stored_identity.recipe_hash != profile["recipe_hash"]
        ):
            raise RetrievalDataError(
                "stored embedding profile identity is inconsistent"
            )
        if stored_identity != expected_profile:
            raise RetrievalUnavailableError(
                "stored embedding profile does not match the query encoder contract"
            )

        import_status = connection.scalar(
            select(embedding_imports.c.status).where(
                embedding_imports.c.snapshot_id == filters.snapshot_id,
                embedding_imports.c.profile_id == filters.profile_id,
            )
        )
        if import_status != "validated":
            raise RetrievalUnavailableError(
                "requested embedding profile is not validated for the snapshot"
            )
        return stored_identity

    @staticmethod
    def _relation_filter(filters: RetrievalFilters):
        match_conditions = []
        if filters.law_ids is not None:
            match_conditions.append(law_articles.c.law_id.in_(filters.law_ids))
        if filters.version_ids is not None:
            match_conditions.append(law_articles.c.version_id.in_(filters.version_ids))
        if filters.article_ids is not None:
            match_conditions.append(law_articles.c.article_id.in_(filters.article_ids))
        if filters.article_numbers is not None:
            match_conditions.append(
                law_articles.c.article_number.in_(filters.article_numbers)
            )
        if filters.effective_on is not None:
            match_conditions.extend(
                [
                    law_versions.c.valid_from.is_not(None),
                    law_versions.c.valid_from <= filters.effective_on,
                    (
                        law_versions.c.valid_to.is_(None)
                        | (law_versions.c.valid_to > filters.effective_on)
                    ),
                ]
            )
        if not match_conditions:
            return None
        relation_join = chunk_articles.join(
            law_articles,
            law_articles.c.article_id == chunk_articles.c.article_id,
        ).join(
            law_versions,
            and_(
                law_versions.c.version_id == law_articles.c.version_id,
                law_versions.c.law_id == law_articles.c.law_id,
            ),
        )
        same_chunk = chunk_articles.c.chunk_id == chunks.c.chunk_id
        match = and_(*match_conditions)
        matching_relation = exists(
            select(1).select_from(relation_join).where(same_chunk, match)
        )
        # Hard selectors are also disclosure boundaries. A multi-article chunk
        # is returned only when every related article satisfies the combined
        # selector; otherwise the full chunk would expose non-matching text.
        violating_relation = exists(
            select(1).select_from(relation_join).where(same_chunk, match.is_not(True))
        )
        return matching_relation & ~violating_relation

    @staticmethod
    def _load_relations(
        connection, chunk_ids: list[str]
    ) -> dict[str, list[Mapping[str, Any]]]:
        if not chunk_ids:
            return {}
        statement = (
            select(
                chunk_articles.c.chunk_id,
                chunk_articles.c.ordinal,
                law_articles.c.article_id,
                law_articles.c.law_id,
                law_articles.c.version_id,
                law_articles.c.article_number,
                law_articles.c.source_ref,
                law_articles.c.source_line,
                law_versions.c.title,
                law_versions.c.valid_from,
                law_versions.c.valid_to,
                law_versions.c.verification_status,
            )
            .select_from(
                chunk_articles.join(
                    law_articles,
                    law_articles.c.article_id == chunk_articles.c.article_id,
                ).join(
                    law_versions,
                    and_(
                        law_versions.c.version_id == law_articles.c.version_id,
                        law_versions.c.law_id == law_articles.c.law_id,
                    ),
                )
            )
            .where(chunk_articles.c.chunk_id.in_(chunk_ids))
            .order_by(chunk_articles.c.chunk_id, chunk_articles.c.ordinal)
        )
        rows: dict[str, list[Mapping[str, Any]]] = {}
        for row in connection.execute(statement).mappings():
            item = dict(row)
            chunk_id = str(item.pop("chunk_id"))
            item.pop("ordinal")
            item["valid_from"] = (
                item["valid_from"].isoformat()
                if item["valid_from"] is not None
                else None
            )
            item["valid_to"] = (
                item["valid_to"].isoformat() if item["valid_to"] is not None else None
            )
            rows.setdefault(chunk_id, []).append(item)
        return rows

    @staticmethod
    def _hydrate_chunk(
        row: Mapping[str, Any],
        relations: Sequence[Mapping[str, Any]],
        filters: RetrievalFilters,
    ) -> Chunk:
        chunk_id = str(row["chunk_id"])
        raw_metadata = row["chunk_metadata"]
        if not isinstance(raw_metadata, Mapping):
            raise RetrievalDataError(
                f"chunk {chunk_id!r} metadata is not a JSON object"
            )
        persisted_content_hash = sha256_json(
            {
                "chunk_id": chunk_id,
                "text": str(row["text"]),
                "strategy": str(row["strategy"]),
                "metadata": dict(raw_metadata),
                "recipe_hash": str(row["chunk_recipe_hash"]),
            }
        )
        if persisted_content_hash != str(row["chunk_content_hash"]):
            raise RetrievalDataError(
                f"chunk {chunk_id!r} content hash does not match persisted payload"
            )
        article_refs = [dict(item) for item in relations]
        metadata = dict(raw_metadata)
        # Database relations are authoritative. Stored source metadata is
        # never allowed to widen the request boundary.
        metadata.update(
            {
                "snapshot_id": filters.snapshot_id,
                "scope_id": filters.scope_id,
                "access_scope_ids": [filters.scope_id],
                "profile_id": filters.profile_id,
                "law_ids": _unique([item["law_id"] for item in relations]),
                "version_ids": _unique([item["version_id"] for item in relations]),
                "article_ids": [item["article_id"] for item in relations],
                "article_refs": article_refs,
                "boundary_fingerprint": filters.fingerprint,
            }
        )
        return Chunk(
            chunk_id=chunk_id,
            text=str(row["text"]),
            law_names=_unique([item["title"] for item in relations]),
            article_numbers=_unique(
                [item["article_number"] for item in relations if item["article_number"]]
            ),
            source_files=_unique([item["source_ref"] for item in relations]),
            line_nos=[int(item["source_line"]) for item in relations],
            strategy=str(row["strategy"]),
            metadata=metadata,
        )

    @staticmethod
    def _build_provenance(
        row: Mapping[str, Any],
        relations: Sequence[Mapping[str, Any]],
        filters: RetrievalFilters,
        chunk: Chunk,
        profile: EmbeddingProfileIdentity,
    ) -> RetrievalProvenance:
        try:
            canonical_embedding = canonicalize_embedding_vector(
                row["embedding"],
                expected_dimension=profile.dimensions,
                normalized=profile.normalization,
                label=f"persisted embedding for chunk {chunk.chunk_id}",
            )
            actual_embedding_hash = hashlib.sha256(
                canonical_embedding.tobytes(order="C")
            ).hexdigest()
            if actual_embedding_hash != str(row["embedding_hash"]):
                raise RetrievalDataError(
                    f"chunk {chunk.chunk_id!r} embedding hash does not match persisted vector"
                )
            article_provenance = tuple(
                article_provenance_from_mapping(item) for item in relations
            )
            return RetrievalProvenance(
                boundary=filters,
                scope_id=filters.scope_id,
                snapshot_id=filters.snapshot_id,
                profile_id=filters.profile_id,
                chunk_id=chunk.chunk_id,
                chunk_content_hash=str(row["chunk_content_hash"]),
                chunk_payload_hash=chunk_payload_fingerprint(chunk),
                snapshot_ordinal=int(row["snapshot_ordinal"]),
                embedding_hash=str(row["embedding_hash"]),
                articles=article_provenance,
            )
        except RetrievalDataError:
            raise
        except (EmbeddingVectorContractError, KeyError, TypeError, ValueError) as exc:
            raise RetrievalDataError(
                f"chunk {chunk.chunk_id!r} has invalid persisted provenance"
            ) from exc


@dataclass(frozen=True, slots=True, init=False)
class PgVectorExactRetriever:
    """Bind one encoder and immutable filter boundary to the legacy Retriever API."""

    name = "pgvector_exact"
    _repository: PostgresExactRetrievalRepository
    _encoder: QueryEncoder
    _boundary: RetrievalBoundary
    _profile: EmbeddingProfileIdentity

    def __init__(
        self,
        repository: PostgresExactRetrievalRepository,
        *,
        encoder: QueryEncoder,
        filters: RetrievalFilters,
    ) -> None:
        profile = getattr(encoder, "profile", None)
        if not isinstance(profile, EmbeddingProfileIdentity):
            raise RetrievalContractError(
                "query encoder must expose an immutable embedding profile identity"
            )
        if filters.profile_id != profile.profile_id:
            raise RetrievalContractError(
                "bound profile_id does not match the query encoder profile"
            )
        # Validate snapshot, import receipt and the complete encoder/profile
        # contract before the first provider call can occur.
        validated_profile = repository.validate_context(
            filters, expected_profile=profile
        )
        if validated_profile != profile:
            raise RetrievalDataError("validated query encoder profile changed")
        object.__setattr__(self, "_repository", repository)
        object.__setattr__(self, "_encoder", encoder)
        object.__setattr__(self, "_boundary", filters)
        object.__setattr__(self, "_profile", profile)

    @property
    def retrieval_boundary(self) -> RetrievalBoundary:
        return self._boundary

    @property
    def boundary_fingerprint(self) -> str:
        return self._boundary.fingerprint

    @property
    def profile(self) -> EmbeddingProfileIdentity:
        return self._profile

    @property
    def expected_dimension(self) -> int:
        return self._profile.dimensions

    def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
        if not isinstance(query, str) or not query.strip():
            raise RetrievalContractError("query must be a non-empty string")
        resolved_top_k = _validate_top_k(top_k)
        if getattr(self._encoder, "profile", None) != self._profile:
            raise RetrievalContractError(
                "query encoder profile changed after retriever construction"
            )
        try:
            query_vector = self._encoder.encode_query(query)
        except ProviderCallError as exc:
            query = "<redacted>"
            raise_sanitized_provider_error(exc)
        return self._repository.search_vector(
            query_vector,
            top_k=resolved_top_k,
            filters=self._boundary,
            expected_profile=self._profile,
        )


@dataclass(frozen=True, slots=True, init=False)
class BoundaryBoundRetriever:
    """Attach and enforce a database-resolved boundary on a legacy retriever.

    The authoritative ``BoundCorpus`` is produced by PostgreSQL. The wrapped
    lexical retriever may rank those chunks, but it cannot self-report or widen
    their provenance.
    """

    _retriever: Any
    _boundary: RetrievalBoundary
    _entries: Mapping[str, BoundCorpusEntry]
    name: str

    def __init__(self, retriever: Any, *, corpus: BoundCorpus) -> None:
        if not callable(getattr(retriever, "retrieve", None)):
            raise TypeError("bound retriever must provide retrieve(query, top_k)")
        if not isinstance(corpus, BoundCorpus):
            raise TypeError("bound retriever requires a database-resolved BoundCorpus")
        # Materialize a private snapshot.  ``BoundCorpus`` is frozen only at
        # the dataclass shell; its Chunk payloads contain mutable containers.
        private_entries = tuple(deepcopy(corpus.entries))
        entries = {entry.chunk.chunk_id: entry for entry in private_entries}
        if len(entries) != len(corpus.entries):
            raise RetrievalDataError("bound corpus contains duplicate chunk IDs")
        for entry in private_entries:
            provenance = entry.provenance
            if (
                provenance.boundary != corpus.boundary
                or not corpus.boundary.allows(provenance)
                or provenance.chunk_id != entry.chunk.chunk_id
                or provenance.chunk_payload_hash
                != chunk_payload_fingerprint(entry.chunk)
            ):
                raise RetrievalDataError("bound corpus provenance is inconsistent")
        object.__setattr__(self, "_retriever", retriever)
        object.__setattr__(self, "_boundary", corpus.boundary)
        object.__setattr__(self, "_entries", MappingProxyType(entries))
        object.__setattr__(
            self, "name", f"bound_{getattr(retriever, 'name', 'retriever')}"
        )

    @property
    def retrieval_boundary(self) -> RetrievalBoundary:
        return self._boundary

    @property
    def boundary_fingerprint(self) -> str:
        return self._boundary.fingerprint

    def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
        resolved_top_k = _validate_top_k(top_k)
        results = self._retriever.retrieve(query, top_k=resolved_top_k)
        checked: list[SearchResult] = []
        for result in results:
            entry = self._entries.get(result.chunk.chunk_id)
            if (
                entry is None
                or chunk_payload_fingerprint(result.chunk)
                != entry.provenance.chunk_payload_hash
                or result.provenance not in {None, entry.provenance}
            ):
                raise RetrievalDataError(
                    "legacy retriever returned a result outside its bound boundary"
                )
            checked.append(
                replace(
                    result,
                    # The caller receives a disposable snapshot, never the
                    # authoritative object used for later validations.
                    chunk=deepcopy(entry.chunk),
                    retriever=self.name,
                    trace={
                        **deepcopy(result.trace),
                        "base_retriever": result.retriever,
                        "boundary_fingerprint": self.boundary_fingerprint,
                    },
                    provenance=entry.provenance,
                )
            )
        # Import lazily to avoid a module import cycle: the generic retrieval
        # module intentionally knows nothing about the storage adapter.
        from ..retrieval import assert_results_match_boundary

        assert_results_match_boundary(
            checked, self._boundary, stage="boundary-bound retrieval"
        )
        return checked
