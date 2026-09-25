from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import replace
from numbers import Real
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .embeddings import (
    EmbeddingModelConfig,
    build_encoder,
    load_embedding_cache,
    validate_cache_matches_chunks,
)
from .embedding_contracts import (
    EmbeddingVectorContractError,
    canonicalize_embedding_vector,
)
from .models import Chunk, SearchResult
from .provider_errors import ProviderCallError, raise_sanitized_provider_error
from .retrieval_contracts import (
    RetrievalBoundary,
    RetrievalBoundaryViolation,
    RetrievalContractError,
    article_provenance_from_mapping,
    chunk_payload_fingerprint,
    validate_retrieval_top_k,
)


class Retriever(Protocol):
    name: str

    def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]: ...


def retrieval_boundary(retriever: Retriever) -> RetrievalBoundary | None:
    """Return a bound retriever's complete immutable request boundary."""

    value = getattr(retriever, "retrieval_boundary", None)
    if value is None:
        if getattr(retriever, "boundary_fingerprint", None) is not None:
            raise ValueError("bound retriever must expose a complete RetrievalBoundary")
        return None
    if not isinstance(value, RetrievalBoundary):
        raise ValueError("retriever retrieval_boundary must be immutable and typed")
    return value


def retrieval_boundary_fingerprint(retriever: Retriever) -> str | None:
    boundary = retrieval_boundary(retriever)
    return boundary.fingerprint if boundary is not None else None


def assert_results_match_boundary(
    results: Sequence[SearchResult],
    boundary: RetrievalBoundary | None,
    *,
    stage: str,
) -> None:
    """Validate typed provenance and its compatibility metadata fail-closed."""

    if boundary is None:
        return
    seen_ranks: set[int] = set()
    for result in results:
        if not isinstance(result, SearchResult):
            raise RetrievalBoundaryViolation(
                f"{stage} returned a non-SearchResult value"
            )
        score = result.score
        rank = result.rank
        structural_valid = (
            not isinstance(score, bool)
            and isinstance(score, Real)
            and math.isfinite(float(score))
            and type(rank) is int
            and rank > 0
            and rank not in seen_ranks
            and isinstance(result.retriever, str)
            and bool(result.retriever.strip())
            and isinstance(result.trace, Mapping)
        )
        if not structural_valid:
            raise RetrievalBoundaryViolation(
                f"{stage} returned an invalid bound SearchResult"
            )
        seen_ranks.add(rank)
        provenance = result.provenance
        metadata = result.chunk.metadata
        try:
            if not isinstance(metadata, Mapping):
                raise RetrievalContractError("chunk metadata must be an object")
            metadata_articles = tuple(
                article_provenance_from_mapping(item)
                for item in metadata.get("article_refs", ())
            )
        except (RetrievalContractError, TypeError):
            metadata_articles = ()
        expected_law_ids = (
            _unique_values(article.law_id for article in provenance.articles)
            if provenance is not None
            else []
        )
        expected_version_ids = (
            _unique_values(article.version_id for article in provenance.articles)
            if provenance is not None
            else []
        )
        expected_article_ids = (
            [article.article_id for article in provenance.articles]
            if provenance is not None
            else []
        )
        expected_law_names = (
            _unique_values(article.title for article in provenance.articles)
            if provenance is not None
            else []
        )
        expected_article_numbers = (
            _unique_values(
                article.article_number
                for article in provenance.articles
                if article.article_number
            )
            if provenance is not None
            else []
        )
        expected_source_files = (
            _unique_values(article.source_ref for article in provenance.articles)
            if provenance is not None
            else []
        )
        expected_line_nos = (
            [article.source_line for article in provenance.articles]
            if provenance is not None
            else []
        )
        valid = (
            provenance is not None
            and provenance.boundary == boundary
            and boundary.allows(provenance)
            and provenance.chunk_id == result.chunk.chunk_id
            and provenance.chunk_payload_hash == chunk_payload_fingerprint(result.chunk)
            and isinstance(metadata, Mapping)
            and metadata.get("boundary_fingerprint") == boundary.fingerprint
            and metadata.get("scope_id") == provenance.scope_id
            and metadata.get("snapshot_id") == provenance.snapshot_id
            and metadata.get("profile_id") == provenance.profile_id
            and metadata.get("access_scope_ids") == [provenance.scope_id]
            and metadata.get("law_ids") == expected_law_ids
            and metadata.get("version_ids") == expected_version_ids
            and metadata.get("article_ids") == expected_article_ids
            and metadata_articles == provenance.articles
            and result.chunk.law_names == expected_law_names
            and result.chunk.article_numbers == expected_article_numbers
            and result.chunk.source_files == expected_source_files
            and result.chunk.line_nos == expected_line_nos
            and result.trace.get("boundary_fingerprint") == boundary.fingerprint
        )
        if not valid:
            raise RetrievalBoundaryViolation(
                f"{stage} returned results outside the bound retrieval boundary"
            )


class BM25Retriever:
    name = "bm25"

    def __init__(
        self,
        chunks: list[Chunk],
        *,
        k1: float = 1.5,
        b: float = 0.75,
        law_boost: float = 40.0,
        article_boost: float = 80.0,
        deprecated_penalty: float = 1.0,
    ) -> None:
        self.chunks = chunks
        self.k1 = k1
        self.b = b
        self.law_boost = law_boost
        self.article_boost = article_boost
        self.deprecated_penalty = deprecated_penalty
        self.known_law_hints = build_known_law_hints(chunks)
        self.doc_tokens = [tokenize(chunk.text) for chunk in chunks]
        self.doc_lengths = [len(tokens) for tokens in self.doc_tokens]
        self.avgdl = sum(self.doc_lengths) / max(len(self.doc_lengths), 1)
        self.term_freqs = [Counter(tokens) for tokens in self.doc_tokens]
        self.doc_freqs = Counter()
        for tokens in self.doc_tokens:
            self.doc_freqs.update(set(tokens))
        self.total_docs = len(chunks)

    def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
        resolved_top_k = validate_retrieval_top_k(top_k)
        query_terms = tokenize(query)
        law_hints = extract_law_hints(query, known_hints=self.known_law_hints)
        article_hints = extract_article_terms(query)
        scores: list[tuple[int, float, dict]] = []
        for index, freqs in enumerate(self.term_freqs):
            bm25_score = 0.0
            doc_len = self.doc_lengths[index] or 1
            for term in query_terms:
                tf = freqs.get(term, 0)
                if tf == 0:
                    continue
                df = self.doc_freqs.get(term, 0)
                idf = math.log((self.total_docs - df + 0.5) / (df + 0.5) + 1)
                numerator = tf * (self.k1 + 1)
                denominator = tf + self.k1 * (
                    1 - self.b + self.b * doc_len / max(self.avgdl, 1)
                )
                bm25_score += idf * numerator / denominator
            boost = metadata_boost(
                self.chunks[index],
                law_hints,
                article_hints,
                law_boost=self.law_boost,
                article_boost=self.article_boost,
            )
            multiplier = deprecated_multiplier(
                self.chunks[index], law_hints, penalty=self.deprecated_penalty
            )
            score = (bm25_score + boost) * multiplier
            if score > 0:
                trace = {
                    "bm25_score": bm25_score,
                    "metadata_boost": boost,
                    "law_hints": law_hints,
                    "article_hints": article_hints,
                    "bm25_k1": self.k1,
                    "bm25_b": self.b,
                    "bm25_law_boost": self.law_boost,
                    "bm25_article_boost": self.article_boost,
                }
                if multiplier != 1.0:
                    trace["deprecated_penalty"] = multiplier
                scores.append((index, score, trace))

        scores.sort(key=lambda item: item[1], reverse=True)
        return [
            SearchResult(
                chunk=self.chunks[index],
                score=score,
                rank=rank,
                retriever=self.name,
                trace=trace,
            )
            for rank, (index, score, trace) in enumerate(
                scores[:resolved_top_k], start=1
            )
        ]


class DenseRetriever:
    name = "dense"

    def __init__(
        self, chunks: list[Chunk], model_name: str = "BAAI/bge-small-zh-v1.5"
    ) -> None:
        self.chunks = chunks
        self.model_name = model_name
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Dense retrieval requires sentence-transformers. Install project dependencies first."
            ) from exc
        self.model = SentenceTransformer(model_name)
        self.embeddings = self.model.encode(
            [chunk.text for chunk in chunks], normalize_embeddings=True
        )

    def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
        resolved_top_k = validate_retrieval_top_k(top_k)
        query_embedding = self.model.encode([query], normalize_embeddings=True)[0]
        scores = self.embeddings @ query_embedding
        ranked = sorted(
            enumerate(scores), key=lambda item: float(item[1]), reverse=True
        )[:resolved_top_k]
        return [
            SearchResult(
                chunk=self.chunks[index],
                score=float(score),
                rank=rank,
                retriever=self.name,
            )
            for rank, (index, score) in enumerate(ranked, start=1)
        ]


class CachedDenseRetriever:
    name = "dense"

    def __init__(
        self,
        chunks: list[Chunk],
        *,
        cache_dir: str | Path,
        model_config: EmbeddingModelConfig,
        device: str = "auto",
        deprecated_penalty: float = 1.0,
    ) -> None:
        self.chunks = chunks
        self.model_config = model_config
        self.cache = load_embedding_cache(cache_dir)
        validate_cache_matches_chunks(self.cache, chunks, model_config=model_config)
        cache_dimension = int(self.cache.vectors.shape[1])
        self.encoder = build_encoder(
            model_config,
            device=device,
            expected_dimension=cache_dimension,
        )
        self.deprecated_penalty = deprecated_penalty
        self.known_law_hints = build_known_law_hints(chunks)
        self.deprecated_indexes = [
            index
            for index, chunk in enumerate(chunks)
            if chunk.metadata.get("deprecated")
        ]

    def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
        resolved_top_k = validate_retrieval_top_k(top_k)
        expected_dimension = int(self.cache.vectors.shape[1])
        try:
            query_embedding = self.encoder.encode_query(query)
            query_vector = _validated_query_embedding(
                query_embedding,
                expected_dimension=expected_dimension,
                provider=self.model_config.provider,
                normalized=self.model_config.normalize,
            )
        except ProviderCallError as exc:
            error = exc
        else:
            scores = self.cache.vectors @ query_vector
            error = None
        if error is not None:
            query = "<redacted>"
            query_embedding = []
            raise_sanitized_provider_error(error)
        if self.deprecated_penalty != 1.0 and self.deprecated_indexes:
            law_hints = extract_law_hints(query, known_hints=self.known_law_hints)
            scores = scores.copy()
            for index in self.deprecated_indexes:
                multiplier = deprecated_multiplier(
                    self.chunks[index], law_hints, penalty=self.deprecated_penalty
                )
                if multiplier != 1.0 and scores[index] > 0:
                    scores[index] = scores[index] * multiplier
        ranked = sorted(
            enumerate(scores), key=lambda item: float(item[1]), reverse=True
        )[:resolved_top_k]
        return [
            SearchResult(
                chunk=self.chunks[index],
                score=float(score),
                rank=rank,
                retriever=self.name,
            )
            for rank, (index, score) in enumerate(ranked, start=1)
        ]


def _validated_query_embedding(
    value: Any,
    *,
    expected_dimension: int,
    provider: str,
    normalized: bool,
) -> Any:
    """Validate encoder output before NumPy can fail or rank unsafe scores."""

    try:
        return canonicalize_embedding_vector(
            value,
            expected_dimension=expected_dimension,
            normalized=normalized,
            label="query embedding vector",
        )
    except EmbeddingVectorContractError as exc:
        error = exc
    if provider == "siliconflow":
        raise ProviderCallError(
            "invalid_response",
            provider="siliconflow",
            operation="embedding",
            cause_type="InvalidQueryVector",
        ) from error
    raise ValueError(str(error)) from error


def _unique_values(values) -> list[Any]:
    resolved: list[Any] = []
    seen: set[Any] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        resolved.append(value)
    return resolved


class RRFHybridRetriever:
    name = "rrf"

    def __init__(
        self,
        bm25: Retriever,
        dense: Retriever,
        *,
        rrf_k: int = 60,
        bm25_weight: float = 1.0,
        dense_weight: float = 1.0,
    ) -> None:
        if type(rrf_k) is not int or rrf_k < 0:
            raise ValueError("rrf_k must be a non-negative integer")
        for name, value in (
            ("bm25_weight", bm25_weight),
            ("dense_weight", dense_weight),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, Real)
                or not math.isfinite(float(value))
                or value < 0
            ):
                raise ValueError(f"{name} must be a finite non-negative number")
        if bm25_weight == 0 and dense_weight == 0:
            raise ValueError("at least one RRF weight must be positive")
        bm25_boundary = retrieval_boundary(bm25)
        dense_boundary = retrieval_boundary(dense)
        if (bm25_boundary is None) != (dense_boundary is None):
            raise ValueError(
                "RRF retrievers must both be unbound or share one bound boundary"
            )
        if bm25_boundary is not None and bm25_boundary != dense_boundary:
            raise ValueError("RRF retrievers use different retrieval boundaries")
        self._bm25 = bm25
        self._dense = dense
        self._boundary = bm25_boundary
        self.rrf_k = rrf_k
        self.bm25_weight = bm25_weight
        self.dense_weight = dense_weight

    @property
    def retrieval_boundary(self) -> RetrievalBoundary | None:
        return self._boundary

    @property
    def bm25(self) -> Retriever:
        """Read-only compatibility view of the lexical leaf retriever."""

        return self._bm25

    @property
    def dense(self) -> Retriever:
        """Read-only compatibility view of the dense leaf retriever."""

        return self._dense

    @property
    def boundary_fingerprint(self) -> str | None:
        return self._boundary.fingerprint if self._boundary is not None else None

    def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
        resolved_top_k = validate_retrieval_top_k(top_k)
        if (
            retrieval_boundary(self._bm25) != self._boundary
            or retrieval_boundary(self._dense) != self._boundary
        ):
            raise RetrievalBoundaryViolation(
                "RRF leaf retrieval boundary changed after construction"
            )
        candidate_top_k = min(max(resolved_top_k * 3, 10), 10_000)
        bm25_results = self._bm25.retrieve(query, top_k=candidate_top_k)
        dense_results = self._dense.retrieve(query, top_k=candidate_top_k)
        boundary = self._boundary
        assert_results_match_boundary(
            bm25_results, boundary, stage="RRF BM25 retrieval"
        )
        assert_results_match_boundary(
            dense_results, boundary, stage="RRF dense retrieval"
        )
        fused_scores: dict[str, float] = defaultdict(float)
        result_by_id: dict[str, SearchResult] = {}
        trace_by_id: dict[str, dict] = defaultdict(dict)

        def remember(result: SearchResult) -> None:
            chunk_id = result.chunk.chunk_id
            existing = result_by_id.get(chunk_id)
            if existing is not None and (
                existing.chunk != result.chunk
                or existing.provenance != result.provenance
            ):
                raise RetrievalBoundaryViolation(
                    "RRF leaves returned conflicting payloads for one chunk ID"
                )
            result_by_id.setdefault(chunk_id, result)

        for result in bm25_results:
            contribution = self.bm25_weight * reciprocal_rank(result.rank, k=self.rrf_k)
            chunk_id = result.chunk.chunk_id
            fused_scores[chunk_id] += contribution
            remember(result)
            trace_by_id[chunk_id].update(
                {
                    "bm25_rank": result.rank,
                    "bm25_score": result.score,
                    "bm25_rrf_score": contribution,
                    "bm25_trace": result.trace,
                }
            )
        for result in dense_results:
            contribution = self.dense_weight * reciprocal_rank(
                result.rank, k=self.rrf_k
            )
            chunk_id = result.chunk.chunk_id
            fused_scores[chunk_id] += contribution
            remember(result)
            trace_by_id[chunk_id].update(
                {
                    "dense_rank": result.rank,
                    "dense_score": result.score,
                    "dense_rrf_score": contribution,
                    "dense_trace": result.trace,
                }
            )

        ranked = sorted(fused_scores.items(), key=lambda item: (-item[1], item[0]))[
            :resolved_top_k
        ]
        results = [
            replace(
                result_by_id[chunk_id],
                score=score,
                rank=rank,
                retriever=self.name,
                trace={
                    **trace_by_id[chunk_id],
                    "fused_score": score,
                    "rrf_k": self.rrf_k,
                    "bm25_weight": self.bm25_weight,
                    "dense_weight": self.dense_weight,
                    **(
                        {"boundary_fingerprint": boundary.fingerprint}
                        if boundary is not None
                        else {}
                    ),
                },
            )
            for rank, (chunk_id, score) in enumerate(ranked, start=1)
        ]
        assert_results_match_boundary(results, boundary, stage="RRF fusion")
        return results


def build_retriever(
    kind: str,
    chunks: list[Chunk],
    *,
    embedding_model: str = "BAAI/bge-small-zh-v1.5",
    embedding_model_config: EmbeddingModelConfig | None = None,
    embedding_cache_dir: str | Path | None = None,
    device: str = "auto",
    rrf_k: int = 60,
    rrf_bm25_weight: float = 1.0,
    rrf_dense_weight: float = 1.0,
    bm25_k1: float = 1.5,
    bm25_b: float = 0.75,
    bm25_law_boost: float = 40.0,
    bm25_article_boost: float = 80.0,
    deprecated_penalty: float = 1.0,
) -> Retriever:
    if kind == "bm25":
        return BM25Retriever(
            chunks,
            k1=bm25_k1,
            b=bm25_b,
            law_boost=bm25_law_boost,
            article_boost=bm25_article_boost,
            deprecated_penalty=deprecated_penalty,
        )
    if kind == "dense":
        if embedding_cache_dir and embedding_model_config:
            return CachedDenseRetriever(
                chunks,
                cache_dir=embedding_cache_dir,
                model_config=embedding_model_config,
                device=device,
                deprecated_penalty=deprecated_penalty,
            )
        return DenseRetriever(chunks, model_name=embedding_model)
    if kind in {"rrf", "hybrid"}:
        bm25 = BM25Retriever(
            chunks,
            k1=bm25_k1,
            b=bm25_b,
            law_boost=bm25_law_boost,
            article_boost=bm25_article_boost,
            deprecated_penalty=deprecated_penalty,
        )
        if embedding_cache_dir and embedding_model_config:
            dense = CachedDenseRetriever(
                chunks,
                cache_dir=embedding_cache_dir,
                model_config=embedding_model_config,
                device=device,
                deprecated_penalty=deprecated_penalty,
            )
        else:
            dense = DenseRetriever(chunks, model_name=embedding_model)
        return RRFHybridRetriever(
            bm25,
            dense,
            rrf_k=rrf_k,
            bm25_weight=rrf_bm25_weight,
            dense_weight=rrf_dense_weight,
        )
    raise ValueError(f"Unknown retriever: {kind}")


def reciprocal_rank(rank: int, k: int = 60) -> float:
    return 1.0 / (k + rank)


def tokenize(text: str) -> list[str]:
    normalized = text.lower()
    chinese_chars = re.findall(r"[\u4e00-\u9fff]", normalized)
    chinese_bigrams = [a + b for a, b in zip(chinese_chars, chinese_chars[1:])]
    ascii_terms = re.findall(r"[a-z0-9_]+", normalized)
    law_titles = re.findall(r"《([^》]+)》", text)
    article_terms = re.findall(r"第[^条]{1,30}条", text)
    return chinese_chars + chinese_bigrams + ascii_terms + law_titles + article_terms


def extract_article_terms(text: str) -> list[str]:
    return re.findall(r"第[^条]{1,30}条", text)


FALLBACK_LAW_HINTS = [
    "民法典",
    "宪法",
    "刑法",
    "民事诉讼法",
    "刑事诉讼法",
    "行政诉讼法",
    "消费者权益保护法",
    "数据安全法",
    "网络安全法",
    "劳动法",
    "劳动合同法",
]


def build_known_law_hints(chunks: list[Chunk]) -> list[str]:
    """Collect full and short law names from the corpus so query hints cover
    every law in the dataset instead of a hardcoded subset."""
    hints: set[str] = set()
    for chunk in chunks:
        for law in chunk.law_names:
            name = law.strip()
            if len(name) < 2:
                continue
            hints.add(name)
            short = name.removeprefix("中华人民共和国").strip()
            if len(short) >= 2:
                hints.add(short)
    # Longest first so substring suppression keeps the most specific match.
    return sorted(hints, key=len, reverse=True)


def extract_law_hints(text: str, known_hints: list[str] | None = None) -> list[str]:
    explicit_titles = re.findall(r"《([^》]+)》", text)
    candidates = known_hints if known_hints else FALLBACK_LAW_HINTS
    matched: list[str] = []
    for hint in candidates:
        if hint in text:
            # Skip hints fully contained in an already matched longer hint
            # (e.g. drop 保险法 when 社会保险法 matched).
            if any(hint != kept and hint in kept for kept in matched):
                continue
            matched.append(hint)
    hints = explicit_titles[:]
    hints.extend(matched)
    return list(dict.fromkeys(hints))


def deprecated_multiplier(
    chunk: Chunk, law_hints: list[str], *, penalty: float
) -> float:
    """Score multiplier for deprecated laws. No penalty when the query
    explicitly references the deprecated law."""
    if penalty >= 1.0 or not chunk.metadata.get("deprecated"):
        return 1.0
    if law_hints and any(hint in law for hint in law_hints for law in chunk.law_names):
        return 1.0
    return penalty


def metadata_boost(
    chunk: Chunk,
    law_hints: list[str],
    article_hints: list[str],
    *,
    law_boost: float = 40.0,
    article_boost: float = 80.0,
) -> float:
    boost = 0.0
    if law_hints and any(hint in law for hint in law_hints for law in chunk.law_names):
        boost += law_boost
    if article_hints and any(
        article == hint for hint in article_hints for article in chunk.article_numbers
    ):
        boost += article_boost
    return boost


def format_sources(results: list[SearchResult]) -> str:
    lines = []
    for result in results:
        chunk = result.chunk
        law = "、".join(chunk.law_names) or "未知法律"
        articles = "、".join(chunk.article_numbers) or "未知条文"
        source_file = chunk.source_files[0] if chunk.source_files else ""
        trace_summary = format_trace_summary(result.trace)
        lines.append(
            f"[S{result.rank}] {law} {articles} score={result.score:.4f} source={source_file}"
            f"{trace_summary}"
        )
    return "\n".join(lines)


def format_trace_summary(trace: dict) -> str:
    if not trace:
        return ""
    parts = []
    if "bm25_rank" in trace:
        parts.append(f"bm25#{trace['bm25_rank']}")
    if "dense_rank" in trace:
        parts.append(f"dense#{trace['dense_rank']}")
    if "fused_score" in trace:
        parts.append(f"fused={float(trace['fused_score']):.4f}")
    if not parts and "metadata_boost" in trace:
        parts.append(f"boost={float(trace['metadata_boost']):.2f}")
    return " trace=" + " ".join(parts) if parts else ""
