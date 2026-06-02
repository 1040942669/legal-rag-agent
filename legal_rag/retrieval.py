from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Protocol

from .embeddings import (
    EmbeddingModelConfig,
    SentenceTransformerEncoder,
    load_embedding_cache,
    validate_cache_matches_chunks,
)
from .models import Chunk, SearchResult


class Retriever(Protocol):
    name: str

    def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
        ...


class BM25Retriever:
    name = "bm25"

    def __init__(self, chunks: list[Chunk], *, k1: float = 1.5, b: float = 0.75) -> None:
        self.chunks = chunks
        self.k1 = k1
        self.b = b
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
        law_hints = extract_law_hints(query)
        article_hints = extract_article_terms(query)
        scores: list[tuple[int, float]] = []
        for index, freqs in enumerate(self.term_freqs):
            score = 0.0
            doc_len = self.doc_lengths[index] or 1
            for term in query_terms:
                tf = freqs.get(term, 0)
                if tf == 0:
                    continue
                df = self.doc_freqs.get(term, 0)
                idf = math.log((self.total_docs - df + 0.5) / (df + 0.5) + 1)
                numerator = tf * (self.k1 + 1)
                denominator = tf + self.k1 * (1 - self.b + self.b * doc_len / max(self.avgdl, 1))
                score += idf * numerator / denominator
            score += metadata_boost(self.chunks[index], law_hints, article_hints)
            if score > 0:
                scores.append((index, score))

        scores.sort(key=lambda item: item[1], reverse=True)
        return [
            SearchResult(chunk=self.chunks[index], score=score, rank=rank, retriever=self.name)
            for rank, (index, score) in enumerate(scores[:top_k], start=1)
        ]


class DenseRetriever:
    name = "dense"

    def __init__(self, chunks: list[Chunk], model_name: str = "BAAI/bge-small-zh-v1.5") -> None:
        self.chunks = chunks
        self.model_name = model_name
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Dense retrieval requires sentence-transformers. Install project dependencies first."
            ) from exc
        self.model = SentenceTransformer(model_name)
        self.embeddings = self.model.encode([chunk.text for chunk in chunks], normalize_embeddings=True)

    def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
        query_embedding = self.model.encode([query], normalize_embeddings=True)[0]
        scores = self.embeddings @ query_embedding
        ranked = sorted(enumerate(scores), key=lambda item: float(item[1]), reverse=True)[:top_k]
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
    ) -> None:
        self.chunks = chunks
        self.model_config = model_config
        self.cache = load_embedding_cache(cache_dir)
        validate_cache_matches_chunks(self.cache, chunks)
        self.encoder = SentenceTransformerEncoder(model_config, device=device)

    def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
        query_embedding = self.encoder.encode_query(query)
        scores = self.cache.vectors @ query_embedding
        ranked = sorted(enumerate(scores), key=lambda item: float(item[1]), reverse=True)[:top_k]
        return [
            SearchResult(
                chunk=self.chunks[index],
                score=float(score),
                rank=rank,
                retriever=self.name,
            )
            for rank, (index, score) in enumerate(ranked, start=1)
        ]


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

        for result in bm25_results:
            fused_scores[result.chunk.chunk_id] += self.bm25_weight * reciprocal_rank(
                result.rank, k=self.rrf_k
            )
            chunk_by_id[result.chunk.chunk_id] = result.chunk
        for result in dense_results:
            fused_scores[result.chunk.chunk_id] += self.dense_weight * reciprocal_rank(
                result.rank, k=self.rrf_k
            )
            chunk_by_id[result.chunk.chunk_id] = result.chunk

        ranked = sorted(fused_scores.items(), key=lambda item: item[1], reverse=True)[:top_k]
        return [
            SearchResult(
                chunk=chunk_by_id[chunk_id],
                score=score,
                rank=rank,
                retriever=self.name,
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
) -> Retriever:
    if kind == "bm25":
        return BM25Retriever(chunks)
    if kind == "dense":
        if embedding_cache_dir and embedding_model_config:
            return CachedDenseRetriever(
                chunks,
                cache_dir=embedding_cache_dir,
                model_config=embedding_model_config,
                device=device,
            )
        return DenseRetriever(chunks, model_name=embedding_model)
    if kind in {"rrf", "hybrid"}:
        bm25 = BM25Retriever(chunks)
        if embedding_cache_dir and embedding_model_config:
            dense = CachedDenseRetriever(
                chunks,
                cache_dir=embedding_cache_dir,
                model_config=embedding_model_config,
                device=device,
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


def extract_law_hints(text: str) -> list[str]:
    explicit_titles = re.findall(r"《([^》]+)》", text)
    common_hints = [
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
    hints = explicit_titles[:]
    hints.extend(hint for hint in common_hints if hint in text)
    return list(dict.fromkeys(hints))


def metadata_boost(chunk: Chunk, law_hints: list[str], article_hints: list[str]) -> float:
    boost = 0.0
    if law_hints and any(hint in law for hint in law_hints for law in chunk.law_names):
        boost += 40.0
    if article_hints and any(article == hint for hint in article_hints for article in chunk.article_numbers):
        boost += 80.0
    return boost


def format_sources(results: list[SearchResult]) -> str:
    lines = []
    for result in results:
        chunk = result.chunk
        law = "、".join(chunk.law_names) or "未知法律"
        articles = "、".join(chunk.article_numbers) or "未知条文"
        source_file = chunk.source_files[0] if chunk.source_files else ""
        lines.append(
            f"[S{result.rank}] {law} {articles} score={result.score:.4f} source={source_file}"
        )
    return "\n".join(lines)
