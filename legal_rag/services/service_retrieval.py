from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

from sqlalchemy import Engine, select

from legal_rag.chat import LegalChatAssistant
from legal_rag.embedding_contracts import EmbeddingProfileIdentity
from legal_rag.evidence import reference_rules_for_evidence
from legal_rag.models import VerificationContext
from legal_rag.llm import SiliconFlowClient
from legal_rag.bm25_settings import build_bm25_retriever
from legal_rag.retrieval_contracts import RetrievalBoundary
from legal_rag.storage.retrieval import (
    BoundaryBoundRetriever,
    PostgresExactRetrievalRepository,
)
from legal_rag.storage.schema import embedding_profiles
from legal_rag.storage.catalog import PostgresLegalCatalogRepository

from .run_executor import ExecutionFailure, RunExecutionInput
from .execution_policy import GenerationPolicy, ServiceExecutionPolicy
from .exact_retrieval import ExactReferenceRetriever


class _ProviderDisabledCompletionClient:
    def complete(self, prompt: str):
        del prompt
        raise RuntimeError("generation is disabled for the provider-free service profile")


@dataclass(frozen=True, slots=True)
class PostgresAssistantFactory:
    """Create one database-bound assistant from the run's frozen server policy.

    The factory reloads the immutable snapshot/profile named on the run rather
    than following the current active pointer.  PostgreSQL applies every hard
    boundary before BM25 sees the corpus. Generation defaults to disabled; an
    enabled policy creates a lazy adapter (or an explicitly trusted test fake).
    The graph runner, not this factory, supplies lease/epoch authority and the
    shared monetary invoker before generation or semantic checking can execute.
    """

    engine: Engine
    memory_token_limit: int = 2_000
    completion_client_factory: Callable[[GenerationPolicy], Any] | None = None

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
        policy = ServiceExecutionPolicy.from_dict(dict(execution.execution_policy)) if execution.execution_policy is not None else ServiceExecutionPolicy.historical()
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
            build_bm25_retriever(corpus.chunks, policy.resolved_bm25_settings),
            corpus=corpus,
        )
        if policy.exact_reference_routing:
            retriever = ExactReferenceRetriever(
                corpus=corpus, lexical=retriever,
                catalog=PostgresLegalCatalogRepository(self.engine),
                pointer_revision=execution.snapshot_revision, activation_id=execution.activation_id,
                reference_rules_version=reference_rules_for_evidence(policy.evidence_rules_version),
            )
        top_k = execution.request_options.get("top_k", 5)
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 20:
            raise ExecutionFailure(code="invalid_top_k", stage="assistant_factory")
        generation = policy.generation
        client = _ProviderDisabledCompletionClient()
        if generation.enabled:
            if execution.scope_id not in generation.allowed_scope_ids:
                raise ExecutionFailure(code="egress_scope_not_authorized", stage="assistant_factory")
            client = self.completion_client_factory(generation) if self.completion_client_factory else SiliconFlowClient(
                model=generation.model, base_url=generation.base_url, api_key_env=generation.api_key_env,
                request_timeout=30, max_tokens=generation.max_output_tokens, enable_thinking=False,
                response_format="json_object", follow_redirects=False, load_environment_file=False,
            )
        return LegalChatAssistant(
            retriever,
            model=generation.model if generation.enabled else "service-provider-disabled",
            top_k=top_k,
            memory_token_limit=self.memory_token_limit,
            verification_context=VerificationContext(
                snapshot_id=execution.snapshot_id,
                allowed_scope_ids=[execution.scope_id],
            ),
            completion_client=client,
            semantic_policy=policy.semantic_policy,
            evidence_rules_version=policy.evidence_rules_version,
        )


__all__ = ["PostgresAssistantFactory"]
