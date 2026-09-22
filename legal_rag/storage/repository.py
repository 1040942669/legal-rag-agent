from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy import Connection, Engine, func, insert, select, text, update

from .contracts import StorageImportBundle, validate_storage_import_bundle
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


class ImportConflictError(RuntimeError):
    """An existing persistent identity points to different immutable content."""


@dataclass(frozen=True)
class ImportResult:
    snapshot_id: str
    bundle_hash: str
    imported: bool
    snapshot_created: bool
    law_version_count: int
    article_count: int
    chunk_count: int
    embedding_count: int
    status: str


def _existing_mapping(
    connection: Connection, table, key_column, key_value: str
) -> Mapping[str, Any] | None:
    return (
        connection.execute(select(table).where(key_column == key_value))
        .mappings()
        .one_or_none()
    )


def _stored_vector_hash(value: Any) -> str:
    try:
        import numpy as np
    except ModuleNotFoundError as exc:  # pragma: no cover - project requires numpy.
        raise RuntimeError("verifying stored embeddings requires numpy") from exc
    stored_vector = np.ascontiguousarray(value, dtype="<f4")
    return hashlib.sha256(stored_vector.tobytes(order="C")).hexdigest()


def _ensure_hashed_row(
    connection: Connection,
    table,
    key_column,
    values: dict[str, Any],
    *,
    identity_name: str,
    insert_missing: bool = True,
) -> None:
    existing = _existing_mapping(connection, table, key_column, values[key_column.name])
    if existing is None:
        if not insert_missing:
            raise ImportConflictError(
                f"{identity_name} {values[key_column.name]!r} is missing"
            )
        connection.execute(insert(table).values(**values))
        return
    for field_name, expected in values.items():
        if existing[field_name] != expected:
            raise ImportConflictError(
                f"{identity_name} {values[key_column.name]!r} already exists with "
                f"different immutable content in {field_name}"
            )


class PostgresCorpusRepository:
    def __init__(self, engine: Engine) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("PostgresCorpusRepository requires PostgreSQL")
        self.engine = engine

    def import_bundle(self, bundle: StorageImportBundle) -> ImportResult:
        """Import a validated bundle in one transaction.

        Existing immutable rows are reused only when their hashes match.  No
        update-on-conflict path can silently replace legal text or vectors.
        """

        validate_storage_import_bundle(bundle)
        with self.engine.begin() as connection:
            # Corpus imports are rare administrative operations.  A transaction-
            # scoped advisory lock makes concurrent retries deterministic instead
            # of exposing check-then-insert uniqueness races.
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_name, 0))"),
                {"lock_name": "legal_rag_corpus_import_v1"},
            )
            existing_snapshot = _existing_mapping(
                connection,
                corpus_snapshots,
                corpus_snapshots.c.snapshot_id,
                bundle.snapshot.snapshot_id,
            )
            snapshot_created = existing_snapshot is None
            if existing_snapshot is not None:
                expected_snapshot_fields = {
                    "scope_id": bundle.snapshot.scope_id,
                    "source_manifest": json.loads(bundle.snapshot.source_manifest_json),
                    "source_manifest_hash": bundle.snapshot.source_manifest_hash,
                    "corpus_hash": bundle.corpus_hash,
                }
                mismatched_snapshot_fields = [
                    field_name
                    for field_name, expected in expected_snapshot_fields.items()
                    if existing_snapshot[field_name] != expected
                ]
                if mismatched_snapshot_fields:
                    raise ImportConflictError(
                        f"snapshot_id {bundle.snapshot.snapshot_id!r} already exists "
                        "with different immutable fields: "
                        + ", ".join(mismatched_snapshot_fields)
                    )
                if existing_snapshot["status"] not in {"validated", "active"}:
                    raise ImportConflictError(
                        f"snapshot_id {bundle.snapshot.snapshot_id!r} is not serviceable"
                    )
                self._verify_existing_corpus(connection, bundle)
            else:
                self._ensure_corpus_rows(connection, bundle)
                connection.execute(
                    insert(corpus_snapshots).values(
                        snapshot_id=bundle.snapshot.snapshot_id,
                        scope_id=bundle.snapshot.scope_id,
                        source_manifest=json.loads(
                            bundle.snapshot.source_manifest_json
                        ),
                        source_manifest_hash=bundle.snapshot.source_manifest_hash,
                        corpus_hash=bundle.corpus_hash,
                        status="building",
                    )
                )
                connection.execute(
                    insert(snapshot_chunks),
                    [
                        {
                            "snapshot_id": item.snapshot_id,
                            "chunk_id": item.chunk_id,
                            "ordinal": item.ordinal,
                        }
                        for item in bundle.snapshot_chunks
                    ],
                )

            existing_import = (
                connection.execute(
                    select(embedding_imports).where(
                        embedding_imports.c.snapshot_id == bundle.snapshot.snapshot_id,
                        embedding_imports.c.profile_id
                        == bundle.embedding_profile.profile_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing_import is not None:
                if existing_import["bundle_hash"] != bundle.bundle_hash:
                    raise ImportConflictError(
                        "snapshot/profile import already exists with a different "
                        "bundle hash"
                    )
                self._verify_embedding_batch(connection, bundle)
                return self._result(
                    bundle,
                    imported=False,
                    snapshot_created=False,
                    status=existing_snapshot["status"],
                )

            self._ensure_profile(connection, bundle)
            self._ensure_embeddings(connection, bundle)
            connection.execute(
                insert(embedding_imports).values(
                    snapshot_id=bundle.snapshot.snapshot_id,
                    profile_id=bundle.embedding_profile.profile_id,
                    bundle_hash=bundle.bundle_hash,
                    status="validated",
                )
            )
            connection.execute(
                update(corpus_snapshots)
                .where(corpus_snapshots.c.snapshot_id == bundle.snapshot.snapshot_id)
                .where(corpus_snapshots.c.status == "building")
                .values(status="validated", validated_at=func.now())
            )
            final_status = (
                "validated" if snapshot_created else existing_snapshot["status"]
            )
            return self._result(
                bundle,
                imported=True,
                snapshot_created=snapshot_created,
                status=final_status,
            )

    def _ensure_corpus_rows(
        self,
        connection: Connection,
        bundle: StorageImportBundle,
        *,
        insert_missing: bool = True,
    ) -> None:
        for law in bundle.law_versions:
            _ensure_hashed_row(
                connection,
                law_versions,
                law_versions.c.version_id,
                {
                    "version_id": law.version_id,
                    "law_id": law.law_id,
                    "title": law.title,
                    "valid_from": law.valid_from,
                    "valid_to": law.valid_to,
                    "verification_status": law.verification_status,
                    "source_ref": law.source_ref,
                    "content_hash": law.content_hash,
                },
                identity_name="version_id",
                insert_missing=insert_missing,
            )

        for article in bundle.articles:
            _ensure_hashed_row(
                connection,
                law_articles,
                law_articles.c.article_id,
                {
                    "article_id": article.article_id,
                    "law_id": article.law_id,
                    "version_id": article.version_id,
                    "article_number": article.article_number,
                    "body": article.body,
                    "raw_text": article.raw_text,
                    "source_ref": article.source_ref,
                    "source_line": article.source_line,
                    "parse_status": article.parse_status,
                    "content_hash": article.content_hash,
                },
                identity_name="article_id",
                insert_missing=insert_missing,
            )

        for chunk in bundle.chunks:
            _ensure_hashed_row(
                connection,
                chunks,
                chunks.c.chunk_id,
                {
                    "chunk_id": chunk.chunk_id,
                    "text": chunk.text,
                    "strategy": chunk.strategy,
                    "metadata": json.loads(chunk.metadata_json),
                    "content_hash": chunk.content_hash,
                    "recipe_hash": chunk.recipe_hash,
                },
                identity_name="chunk_id",
                insert_missing=insert_missing,
            )

        self._ensure_chunk_articles(connection, bundle, insert_missing=insert_missing)

    def _ensure_profile(
        self,
        connection: Connection,
        bundle: StorageImportBundle,
        *,
        insert_missing: bool = True,
    ) -> None:
        profile = bundle.embedding_profile
        values = {
            "profile_id": profile.profile_id,
            "provider": profile.provider,
            "model": profile.model,
            "revision": profile.revision,
            "dimensions": profile.dimensions,
            "normalization": profile.normalization,
            "query_prefix": profile.query_prefix,
            "document_prefix": profile.document_prefix,
            "embed_with_metadata": profile.embed_with_metadata,
            "recipe_hash": profile.recipe_hash,
        }
        existing = _existing_mapping(
            connection,
            embedding_profiles,
            embedding_profiles.c.profile_id,
            profile.profile_id,
        )
        if existing is None:
            if not insert_missing:
                raise ImportConflictError(
                    f"profile_id {profile.profile_id!r} is missing"
                )
            connection.execute(insert(embedding_profiles).values(**values))
            return
        for key, value in values.items():
            if existing[key] != value:
                raise ImportConflictError(
                    f"profile_id {profile.profile_id!r} has conflicting {key}"
                )

    def _ensure_chunk_articles(
        self,
        connection: Connection,
        bundle: StorageImportBundle,
        *,
        insert_missing: bool = True,
    ) -> None:
        expected_by_chunk: dict[str, list[tuple[str, int]]] = {}
        for relation in bundle.chunk_articles:
            expected_by_chunk.setdefault(relation.chunk_id, []).append(
                (relation.article_id, relation.ordinal)
            )
        for chunk_id, expected in expected_by_chunk.items():
            existing = connection.execute(
                select(chunk_articles.c.article_id, chunk_articles.c.ordinal)
                .where(chunk_articles.c.chunk_id == chunk_id)
                .order_by(chunk_articles.c.ordinal)
            ).all()
            if not existing:
                if not insert_missing:
                    raise ImportConflictError(
                        f"chunk {chunk_id!r} has missing article relations"
                    )
                connection.execute(
                    insert(chunk_articles),
                    [
                        {
                            "chunk_id": chunk_id,
                            "article_id": article_id,
                            "ordinal": ordinal,
                        }
                        for article_id, ordinal in expected
                    ],
                )
            elif list(existing) != expected:
                raise ImportConflictError(
                    f"chunk {chunk_id!r} has conflicting article relations or order"
                )

    def _ensure_embeddings(
        self, connection: Connection, bundle: StorageImportBundle
    ) -> None:
        dimension = bundle.embedding_profile.dimensions
        for embedding in bundle.embeddings:
            existing = (
                connection.execute(
                    select(chunk_embeddings).where(
                        chunk_embeddings.c.chunk_id == embedding.chunk_id,
                        chunk_embeddings.c.profile_id == embedding.profile_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is None:
                connection.execute(
                    insert(chunk_embeddings).values(
                        chunk_id=embedding.chunk_id,
                        profile_id=embedding.profile_id,
                        embedding=list(embedding.embedding),
                        embedding_dimension=dimension,
                        embedding_hash=embedding.embedding_hash,
                    )
                )
            elif (
                existing["embedding_hash"] != embedding.embedding_hash
                or existing["embedding_dimension"] != dimension
                or _stored_vector_hash(existing["embedding"])
                != embedding.embedding_hash
            ):
                raise ImportConflictError(
                    f"embedding for chunk {embedding.chunk_id!r} and profile "
                    f"{embedding.profile_id!r} has conflicting immutable content"
                )

    def _verify_existing_corpus(
        self, connection: Connection, bundle: StorageImportBundle
    ) -> None:
        self._ensure_corpus_rows(connection, bundle, insert_missing=False)

        stored_members = connection.execute(
            select(snapshot_chunks.c.chunk_id, snapshot_chunks.c.ordinal)
            .where(snapshot_chunks.c.snapshot_id == bundle.snapshot.snapshot_id)
            .order_by(snapshot_chunks.c.ordinal)
        ).all()
        expected_members = [
            (item.chunk_id, item.ordinal) for item in bundle.snapshot_chunks
        ]
        if stored_members != expected_members:
            raise ImportConflictError(
                f"snapshot_id {bundle.snapshot.snapshot_id!r} has incomplete members"
            )

    def _verify_embedding_batch(
        self, connection: Connection, bundle: StorageImportBundle
    ) -> None:
        self._ensure_profile(connection, bundle, insert_missing=False)
        stored_embeddings = connection.execute(
            select(
                chunk_embeddings.c.chunk_id,
                chunk_embeddings.c.embedding,
                chunk_embeddings.c.embedding_hash,
                chunk_embeddings.c.embedding_dimension,
            ).where(
                chunk_embeddings.c.profile_id == bundle.embedding_profile.profile_id,
                chunk_embeddings.c.chunk_id.in_(
                    [item.chunk_id for item in bundle.embeddings]
                ),
            )
        ).all()
        actual_embeddings = {}
        for row in stored_embeddings:
            actual_embeddings[row.chunk_id] = (
                row.embedding_hash,
                row.embedding_dimension,
                _stored_vector_hash(row.embedding),
            )
        expected_embeddings = {
            item.chunk_id: (
                item.embedding_hash,
                bundle.embedding_profile.dimensions,
                item.embedding_hash,
            )
            for item in bundle.embeddings
        }
        if actual_embeddings != expected_embeddings:
            raise ImportConflictError(
                f"snapshot_id {bundle.snapshot.snapshot_id!r} has incomplete or "
                "conflicting embeddings"
            )

    @staticmethod
    def _result(
        bundle: StorageImportBundle,
        *,
        imported: bool,
        snapshot_created: bool,
        status: str,
    ) -> ImportResult:
        return ImportResult(
            snapshot_id=bundle.snapshot.snapshot_id,
            bundle_hash=bundle.bundle_hash,
            imported=imported,
            snapshot_created=snapshot_created,
            law_version_count=len(bundle.law_versions),
            article_count=len(bundle.articles),
            chunk_count=len(bundle.chunks),
            embedding_count=len(bundle.embeddings),
            status=status,
        )

    def table_counts(self) -> dict[str, int]:
        tables = {
            "corpus_snapshots": corpus_snapshots,
            "law_versions": law_versions,
            "law_articles": law_articles,
            "chunks": chunks,
            "chunk_articles": chunk_articles,
            "snapshot_chunks": snapshot_chunks,
            "embedding_profiles": embedding_profiles,
            "chunk_embeddings": chunk_embeddings,
            "embedding_imports": embedding_imports,
        }
        with self.engine.connect() as connection:
            return {
                name: int(
                    connection.scalar(select(func.count()).select_from(table)) or 0
                )
                for name, table in tables.items()
            }
