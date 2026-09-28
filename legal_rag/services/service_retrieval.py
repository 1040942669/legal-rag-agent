from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine, select

from legal_rag.chat import LegalChatAssistant
from legal_rag.embedding_contracts import EmbeddingProfileIdentity
from legal_rag.models import VerificationContext
from legal_rag.retrieval import BM25Retriever
from legal_rag.retrieval_contracts import RetrievalBoundary
from legal_rag.storage.retrieval import (
    BoundaryBoundRetriever,
    PostgresExactRetrievalRepository,
)
from legal_rag.storage.schema import embedding_profiles

from .run_executor import ExecutionFailure, RunExecutionInput


class _ProviderDisabledCompletionClient:
    def complete(self, prompt: str):
        del prompt
        raise RuntimeError("generation is disabled for the provider-free service profile")


@dataclass(frozen=True, slots=True)
class PostgresAssistantFactory:
    """Create one database-bound, provider-free assistant for each M4 run.

    The factory reloads the immutable snapshot/profile named on the run rather
    than following the current active pointer.  PostgreSQL applies every hard
    boundary before BM25 sees the corpus.  Generation remains disabled by the
    corresponding ``LegalChatRunExecutor(generate=False)`` configuration.
    """

    engine: Engine
    memory_token_limit: int = 2_000

    def __post_init__(self) -> None:
        if self.engine.dialect.name != "postgresql":
            raise ValueError("PostgresAssistantFactory requires PostgreSQL")
        if (
            isinstance(self.memory_token_limit, bool)
            or not isinstance(self.memory_token_limit, int)
            or self.memory_token_limit < 1
        ):
            raise ValueError("memory_token_limit must be a positive integer")

    def __call__(self, execution: RunExecutionInput) -> LegalChatAssistant:
        boundary = RetrievalBoundary(
            scope_id=execution.scope_id,
            snapshot_id=execution.snapshot_id,
            profile_id=execution.profile_id,
        )
        if boundary.fingerprint != execution.boundary_fingerprint:
            raise ExecutionFailure(
                code="retrieval_boundary_mismatch",
                stage="assistant_factory",
            )
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(
                        embedding_profiles.c.provider,
                        embedding_profiles.c.model,
                        embedding_profiles.c.revision,
                        embedding_profiles.c.dimensions,
                        embedding_profiles.c.normalization,
                        embedding_profiles.c.query_prefix,
                        embedding_profiles.c.document_prefix,
                        embedding_profiles.c.embed_with_metadata,
                    ).where(embedding_profiles.c.profile_id == execution.profile_id)
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise ExecutionFailure(
                code="embedding_profile_unavailable",
                stage="assistant_factory",
            )
        profile = EmbeddingProfileIdentity(
            provider=str(row["provider"]),
            model=str(row["model"]),
            revision=str(row["revision"]),
            dimensions=int(row["dimensions"]),
            normalization=bool(row["normalization"]),
            query_prefix=str(row["query_prefix"]),
            document_prefix=str(row["document_prefix"]),
            embed_with_metadata=bool(row["embed_with_metadata"]),
        )
        if profile.profile_id != execution.profile_id:
            raise ExecutionFailure(
                code="embedding_profile_mismatch",
                stage="assistant_factory",
            )
        repository = PostgresExactRetrievalRepository(self.engine)
        corpus = repository.load_bound_corpus(
            filters=boundary,
            expected_profile=profile,
        )
        retriever = BoundaryBoundRetriever(
            BM25Retriever(corpus.chunks),
            corpus=corpus,
        )
        top_k = execution.request_options.get("top_k", 5)
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 20:
            raise ExecutionFailure(code="invalid_top_k", stage="assistant_factory")
        return LegalChatAssistant(
            retriever,
            model="service-provider-disabled",
            top_k=top_k,
            memory_token_limit=self.memory_token_limit,
            verification_context=VerificationContext(
                snapshot_id=execution.snapshot_id,
                allowed_scope_ids=[execution.scope_id],
            ),
            completion_client=_ProviderDisabledCompletionClient(),
        )


__all__ = ["PostgresAssistantFactory"]
