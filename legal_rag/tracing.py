from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import SearchResult, VerificationResult


_SAFE_VERIFICATION_TRACE_FIELDS = (
    "passed",
    "schema_valid",
    "evidence_catalog_valid",
    "citation_ids_valid",
    "citation_alignment_valid",
    "evidence_scope_valid",
    "citation_valid",
    "disclaimer_present",
    "response_mode_valid",
    "semantic_support_status",
    "expected_answer_mode",
    "actual_answer_mode",
    "refusal_required",
    "refusal_present",
    "answer_source_format",
    "required_checks",
    "refusal_correct",
    "failure_reasons",
)
_VERIFICATION_TRACE_COUNT_FIELDS = {
    "schema_errors": "schema_error_count",
    "duplicate_source_ids": "duplicate_source_id_count",
    "missing_source_ids": "missing_source_id_count",
    "malformed_citation_tokens": "malformed_citation_token_count",
    "invalid_scope_citations": "invalid_scope_citation_count",
    "cited_source_ids": "cited_source_id_count",
    "visible_source_ids": "visible_source_id_count",
    "claim_source_ids": "claim_source_id_count",
    "unsupported_claims": "unsupported_claim_count",
}


class JsonlTraceWriter:
    def __init__(self, path: str | Path, *, run_id: str) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: dict[str, Any]) -> None:
        payload = {"run_id": self.run_id, **record}
        serialized = json.dumps(payload, ensure_ascii=False)
        try:
            serialized.encode("utf-8")
        except UnicodeEncodeError:
            serialized = json.dumps(payload, ensure_ascii=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(serialized + "\n")


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


def verification_result_to_trace(result: VerificationResult | None) -> dict[str, Any] | None:
    """Serialize verifier diagnostics without copying untrusted draft fragments."""

    if result is None:
        return None
    raw = result.to_dict()
    payload = {
        field: raw[field]
        for field in _SAFE_VERIFICATION_TRACE_FIELDS
        if field in raw
    }
    for source_field, count_field in _VERIFICATION_TRACE_COUNT_FIELDS.items():
        value = raw.get(source_field, [])
        payload[count_field] = len(value) if isinstance(value, list) else 0
    return payload


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
    execution: dict[str, Any] | None = None,
    generation_attempt: dict[str, Any] | None = None,
    final_response: dict[str, Any] | None = None,
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
        "execution": execution or {},
        "generation_attempt": generation_attempt or {},
        "final_response": final_response or {},
        # Compatibility key for pre-M1 trace readers. Retrieval-only callers
        # leave it empty instead of fabricating a verification result.
        "verifier": verifier or {},
        "failure": failure or {},
        "results": [search_result_to_trace(result) for result in results],
        "metadata": metadata or {},
    }
