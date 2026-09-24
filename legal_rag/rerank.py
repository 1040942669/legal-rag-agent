from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace
from typing import Any, Protocol

from .models import SearchResult
from .retrieval import (
    Retriever,
    assert_results_match_boundary,
    retrieval_boundary,
)
from .retrieval_contracts import (
    RetrievalBoundary,
    RetrievalBoundaryViolation,
    RetrievalContractError,
    validate_retrieval_top_k,
)


@dataclass(frozen=True)
class RerankerConfig:
    key: str
    provider: str
    model_name: str
    batch_size: int = 8
    max_length: int = 512
    device: str = "auto"
    include_metadata: bool = True


@dataclass
class RerankerStats:
    calls: int = 0
    failed_calls: int = 0
    documents: int = 0
    total_ms: float = 0.0

    def to_dict(self) -> dict[str, int | float]:
        return {
            "rerank_calls": self.calls,
            "rerank_failed_calls": self.failed_calls,
            "rerank_documents": self.documents,
            "rerank_total_ms": round(self.total_ms, 3),
        }


class Reranker(Protocol):
    name: str

    def score(self, query: str, results: list[SearchResult]) -> list[float]: ...


class CrossEncoderReranker:
    """Lazy sentence-transformers CrossEncoder adapter.

    Loading stays lazy so `--reranker none` and CLI help never download or
    allocate a model. The adapter deliberately exposes only pair scores; the
    wrapper owns ranking and trace preservation.
    """

    def __init__(
        self,
        config: RerankerConfig,
        *,
        model_factory: Any | None = None,
    ) -> None:
        self.config = config
        self.name = config.key
        self._model_factory = model_factory
        self._model: Any | None = None

    def score(self, query: str, results: list[SearchResult]) -> list[float]:
        if not results:
            return []
        model = self._load_model()
        pairs = [
            (
                query,
                reranker_document_text(result)
                if self.config.include_metadata
                else result.chunk.text,
            )
            for result in results
        ]
        predictions = model.predict(
            pairs,
            batch_size=self.config.batch_size,
            show_progress_bar=False,
        )
        raw_scores = (
            predictions.tolist() if hasattr(predictions, "tolist") else predictions
        )
        if not isinstance(raw_scores, (list, tuple)):
            raw_scores = [raw_scores]
        scores: list[float] = []
        for value in raw_scores:
            if isinstance(value, (list, tuple)):
                if len(value) != 1:
                    raise ValueError(
                        "Reranker returned multi-label scores; configure a single-score cross-encoder."
                    )
                value = value[0]
            scores.append(float(value))
        if len(scores) != len(results):
            raise ValueError(
                f"Reranker returned {len(scores)} scores for {len(results)} candidates."
            )
        return scores

    def _load_model(self):
        if self._model is not None:
            return self._model
        if self._model_factory is None:
            try:
                from sentence_transformers import CrossEncoder  # type: ignore
            except ModuleNotFoundError as exc:
                raise RuntimeError(
                    "Could not import sentence-transformers CrossEncoder "
                    f"(missing module: {exc.name or 'unknown'}). Run `uv sync` and verify the "
                    "Torch/Transformers environment."
                ) from exc
            self._model_factory = CrossEncoder
        kwargs: dict[str, Any] = {"max_length": self.config.max_length}
        if self.config.device and self.config.device != "auto":
            kwargs["device"] = self.config.device
        self._model = self._model_factory(self.config.model_name, **kwargs)
        return self._model


class RerankingRetriever:
    def __init__(
        self,
        base: Retriever,
        reranker: Reranker,
        *,
        candidate_top_n: int = 20,
    ) -> None:
        if type(candidate_top_n) is not int or candidate_top_n < 1:
            raise ValueError("candidate_top_n must be at least 1")
        self._base = base
        self.reranker = reranker
        self.candidate_top_n = candidate_top_n
        self.name = f"{getattr(base, 'name', 'retriever')}+rerank:{reranker.name}"
        self.stats = RerankerStats()
        self._boundary = retrieval_boundary(base)

    @property
    def retrieval_boundary(self) -> RetrievalBoundary | None:
        return self._boundary

    @property
    def base(self) -> Retriever:
        """Read-only compatibility view of the captured base retriever."""

        return self._base

    @property
    def boundary_fingerprint(self) -> str | None:
        return self._boundary.fingerprint if self._boundary is not None else None

    def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
        resolved_top_k = validate_retrieval_top_k(top_k)
        candidate_top_n = validate_retrieval_top_k(
            max(resolved_top_k, self.candidate_top_n)
        )
        if retrieval_boundary(self._base) != self._boundary:
            raise RetrievalBoundaryViolation(
                "reranker base retrieval boundary changed after construction"
            )
        candidates = self._base.retrieve(query, top_k=candidate_top_n)
        boundary = self._boundary
        assert_results_match_boundary(
            candidates, boundary, stage="reranker candidate retrieval"
        )
        if not candidates:
            return []

        started = time.perf_counter()
        self.stats.calls += 1
        self.stats.documents += len(candidates)
        try:
            scores = self.reranker.score(query, candidates)
            if not isinstance(scores, (list, tuple)) or len(scores) != len(candidates):
                raise RetrievalContractError(
                    "reranker must return one score for every candidate"
                )
            normalized_scores: list[float] = []
            for score in scores:
                if isinstance(score, bool):
                    raise RetrievalContractError(
                        "reranker scores must be finite real numbers"
                    )
                try:
                    normalized_score = float(score)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise RetrievalContractError(
                        "reranker scores must be finite real numbers"
                    ) from exc
                if not math.isfinite(normalized_score):
                    raise RetrievalContractError(
                        "reranker scores must be finite real numbers"
                    )
                normalized_scores.append(normalized_score)
        except Exception:
            self.stats.failed_calls += 1
            raise
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            self.stats.total_ms += elapsed_ms

        ranked = sorted(
            zip(candidates, normalized_scores, strict=True),
            key=lambda item: (-item[1], item[0].rank, item[0].chunk.chunk_id),
        )[:resolved_top_k]
        results = [
            replace(
                result,
                score=score,
                rank=rank,
                retriever=self.name,
                trace={
                    **result.trace,
                    "base_retriever": result.retriever,
                    "base_rank": result.rank,
                    "base_score": result.score,
                    "reranker": self.reranker.name,
                    "rerank_score": score,
                    "rerank_candidate_count": len(candidates),
                    "rerank_ms": round(elapsed_ms, 3),
                },
            )
            for rank, (result, score) in enumerate(ranked, start=1)
        ]
        assert_results_match_boundary(results, boundary, stage="reranker output")
        return results


def resolve_reranker_config(
    config: dict, reranker_key: str | None = None
) -> RerankerConfig | None:
    reranking = config.get("reranking", {})
    key = reranker_key if reranker_key is not None else reranking.get("default", "none")
    if not key or str(key).lower() == "none":
        return None
    model_map = reranking.get("models", {})
    if key not in model_map:
        available = ", ".join(["none", *sorted(model_map)])
        raise ValueError(f"Unknown reranker key `{key}`. Available: {available}")
    item = model_map[key]
    return RerankerConfig(
        key=key,
        provider=item.get("provider", "sentence_transformers_cross_encoder"),
        model_name=item["model_name"],
        batch_size=int(item.get("batch_size", 8)),
        max_length=int(item.get("max_length", 512)),
        device=item.get("device", "auto"),
        include_metadata=bool(item.get("include_metadata", True)),
    )


def build_reranker(config: RerankerConfig) -> Reranker:
    if config.provider == "sentence_transformers_cross_encoder":
        return CrossEncoderReranker(config)
    raise ValueError(f"Unsupported reranker provider: {config.provider}")


def wrap_with_reranker(
    retriever: Retriever,
    config: dict,
    *,
    reranker_key: str | None = None,
    candidate_top_n: int | None = None,
) -> Retriever:
    reranker_config = resolve_reranker_config(config, reranker_key)
    if reranker_config is None:
        return retriever
    configured_top_n = int(config.get("reranking", {}).get("candidate_top_n", 20))
    return RerankingRetriever(
        retriever,
        build_reranker(reranker_config),
        candidate_top_n=(
            candidate_top_n if candidate_top_n is not None else configured_top_n
        ),
    )


def retriever_runtime_metrics(retriever: Retriever) -> dict[str, int | float]:
    stats = getattr(retriever, "stats", None)
    if isinstance(stats, RerankerStats):
        return stats.to_dict()
    return {
        "rerank_calls": 0,
        "rerank_failed_calls": 0,
        "rerank_documents": 0,
        "rerank_total_ms": 0.0,
    }


def reranker_document_text(result: SearchResult) -> str:
    chunk = result.chunk
    law = "、".join(chunk.law_names)
    article = "、".join(chunk.article_numbers)
    header = " ".join(part for part in (law, article) if part)
    return f"{header}\n{chunk.text}" if header else chunk.text
