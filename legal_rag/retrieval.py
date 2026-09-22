from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Protocol

from .embeddings import (
    EmbeddingModelConfig,
    build_encoder,
    load_embedding_cache,
    validate_cache_matches_chunks,
)
from .models import Chunk, SearchResult
from .provider_errors import ProviderCallError, raise_sanitized_provider_error


class Retriever(Protocol):
    name: str

    def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]: ...


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
            for rank, (index, score, trace) in enumerate(scores[:top_k], start=1)
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
        query_embedding = self.model.encode([query], normalize_embeddings=True)[0]
        scores = self.embeddings @ query_embedding
        ranked = sorted(
            enumerate(scores), key=lambda item: float(item[1]), reverse=True
        )[:top_k]
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
        expected_dimension = int(self.cache.vectors.shape[1])
        try:
            query_embedding = self.encoder.encode_query(query)
            query_vector = _validated_query_embedding(
                query_embedding,
                expected_dimension=expected_dimension,
                provider=self.model_config.provider,
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
        )[:top_k]
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
) -> Any:
    """Validate encoder output before NumPy can fail or rank unsafe scores."""

    try:
        import numpy as np  # type: ignore

        vector = np.asarray(value)
        valid = (
            vector.ndim == 1
            and int(vector.shape[0]) == expected_dimension
            and vector.dtype.kind in {"f", "i", "u"}
            and bool(np.isfinite(vector).all())
        )
    except (TypeError, ValueError, OverflowError):
        valid = False
        vector = None
    if valid:
        return vector
    if provider == "siliconflow":
        raise ProviderCallError(
            "invalid_response",
            provider="siliconflow",
            operation="embedding",
            cause_type="InvalidQueryVector",
        )
    raise ValueError("query embedding vector is invalid")


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
        self.bm25 = bm25
        self.dense = dense
        self.rrf_k = rrf_k
        self.bm25_weight = bm25_weight
        self.dense_weight = dense_weight

    def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
        bm25_results = self.bm25.retrieve(query, top_k=max(top_k * 3, 10))
        dense_results = self.dense.retrieve(query, top_k=max(top_k * 3, 10))
        fused_scores: dict[str, float] = defaultdict(float)
        chunk_by_id: dict[str, Chunk] = {}
        trace_by_id: dict[str, dict] = defaultdict(dict)

        for result in bm25_results:
            contribution = self.bm25_weight * reciprocal_rank(result.rank, k=self.rrf_k)
            chunk_id = result.chunk.chunk_id
            fused_scores[chunk_id] += contribution
            chunk_by_id[chunk_id] = result.chunk
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
            chunk_by_id[chunk_id] = result.chunk
            trace_by_id[chunk_id].update(
                {
                    "dense_rank": result.rank,
                    "dense_score": result.score,
                    "dense_rrf_score": contribution,
                    "dense_trace": result.trace,
                }
            )

        ranked = sorted(fused_scores.items(), key=lambda item: item[1], reverse=True)[
            :top_k
        ]
        return [
            SearchResult(
                chunk=chunk_by_id[chunk_id],
                score=score,
                rank=rank,
                retriever=self.name,
                trace={
                    **trace_by_id[chunk_id],
                    "fused_score": score,
                    "rrf_k": self.rrf_k,
                    "bm25_weight": self.bm25_weight,
                    "dense_weight": self.dense_weight,
                },
            )
            for rank, (chunk_id, score) in enumerate(ranked, start=1)
        ]


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
