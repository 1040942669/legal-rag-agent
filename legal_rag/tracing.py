from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import SearchResult


class JsonlTraceWriter:
    def __init__(self, path: str | Path, *, run_id: str) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: dict[str, Any]) -> None:
        payload = {"run_id": self.run_id, **record}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def search_result_to_trace(result: SearchResult) -> dict[str, Any]:
    chunk = result.chunk
    return {
        "rank": result.rank,
        "score": result.score,
        "retriever": result.retriever,
        "chunk_id": chunk.chunk_id,
        "law_names": chunk.law_names,
        "article_numbers": chunk.article_numbers,
        "source_files": chunk.source_files,
        "line_nos": chunk.line_nos,
        "strategy": chunk.strategy,
        "chunk_metadata": chunk.metadata,
        "ranking_trace": result.trace,
    }


def build_retrieval_trace_record(
    *,
    query: str,
    retriever: str,
    top_k: int,
    results: list[SearchResult],
    latency_ms: int,
    case_id: str | None = None,
    analyzer: dict[str, Any] | None = None,
    adaptive: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
    verifier: dict[str, Any] | None = None,
    failure: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "query": query,
        "retriever": retriever,
        "top_k": top_k,
        "latency_ms": latency_ms,
        "analyzer": analyzer or {},
        "adaptive": adaptive or {},
        "evidence": evidence or {},
        "verifier": verifier or {},
        "failure": failure or {},
        "results": [search_result_to_trace(result) for result in results],
        "metadata": metadata or {},
    }
