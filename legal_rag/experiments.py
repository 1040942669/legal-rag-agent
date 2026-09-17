from __future__ import annotations

import csv
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .evaluation import bootstrap_ci, percentile, scored_records
from .manifest import utc_now
from .models import EvalRecord

EMBEDDING_RETRIEVERS = {"dense", "rrf", "hybrid", "llamaindex_dense"}


@dataclass(frozen=True)
class ExperimentSpec:
    chunk_strategy: str
    retriever: str
    embedding: str
    adaptive: bool
    reranker: str

    @property
    def experiment_id(self) -> str:
        adaptive_mode = "adaptive" if self.adaptive else "direct"
        return (
            f"{self.chunk_strategy}__{self.retriever}__{self.embedding}__"
            f"{adaptive_mode}__{self.reranker}"
        )


def comma_values(value: str | None, *, default: list[str] | None = None) -> list[str]:
    values = [item.strip() for item in (value or "").split(",") if item.strip()]
    return values or list(default or [])


def build_experiment_specs(
    *,
    chunk_strategies: list[str],
    retrievers: list[str],
    embeddings: list[str],
    adaptive_modes: list[str],
    rerankers: list[str],
) -> list[ExperimentSpec]:
    if not chunk_strategies or not retrievers:
        raise ValueError("Experiment matrix needs at least one chunk strategy and retriever.")
    invalid_modes = sorted(set(adaptive_modes) - {"direct", "adaptive"})
    if invalid_modes:
        raise ValueError(f"Unknown adaptive mode(s): {', '.join(invalid_modes)}")
    if not adaptive_modes:
        adaptive_modes = ["direct"]
    if not rerankers:
        rerankers = ["none"]

    specs: list[ExperimentSpec] = []
    seen: set[ExperimentSpec] = set()
    for chunk_strategy in chunk_strategies:
        for retriever in retrievers:
            embedding_values = embeddings if retriever in EMBEDDING_RETRIEVERS else ["none"]
            if retriever in EMBEDDING_RETRIEVERS and not embedding_values:
                raise ValueError(f"Retriever {retriever!r} requires at least one embedding key.")
            for embedding in embedding_values:
                for adaptive_mode in adaptive_modes:
                    for reranker in rerankers:
                        spec = ExperimentSpec(
                            chunk_strategy=chunk_strategy,
                            retriever=retriever,
                            embedding=embedding,
                            adaptive=adaptive_mode == "adaptive",
                            reranker=reranker,
                        )
                        if spec not in seen:
                            seen.add(spec)
                            specs.append(spec)
    return specs


def summarize_experiment(
    spec: ExperimentSpec,
    records: list[EvalRecord],
    *,
    top_k: int,
    build_seconds: float,
    build_peak_mb: float,
    runtime_metrics: dict[str, int | float] | None = None,
    input_cost_per_million: float | None = None,
    output_cost_per_million: float | None = None,
) -> dict[str, Any]:
    successful = [record for record in records if not record.error]
    scored = scored_records(successful)
    latency = [float(record.latency_ms) for record in successful]
    failure_counts = Counter(record.failure_label for record in successful if record.failure_label)
    input_tokens = sum(record.input_tokens for record in records)
    output_tokens = sum(record.output_tokens for record in records)
    token_usage_calls = sum(record.token_usage_calls for record in records)
    estimated_cost: float | None = None
    if token_usage_calls and (input_cost_per_million is not None or output_cost_per_million is not None):
        estimated_cost = (
            input_tokens * float(input_cost_per_million or 0.0)
            + output_tokens * float(output_cost_per_million or 0.0)
        ) / 1_000_000
    runtime = runtime_metrics or {}
    error_messages = list(dict.fromkeys(record.error for record in records if record.error))
    if not records:
        status = "empty"
    elif not successful:
        status = "failed"
    elif len(successful) < len(records):
        status = "partial"
    else:
        status = "ok"
    hit5_ci = bootstrap_ci([float(record.hit_at_5) for record in scored])
    mrr_ci = bootstrap_ci([record.mrr for record in scored])
    return {
        "experiment_id": spec.experiment_id,
        "status": status,
        "error": " | ".join(error_messages[:3]),
        "chunk_strategy": spec.chunk_strategy,
        "retriever": spec.retriever,
        "embedding": spec.embedding,
        "adaptive": spec.adaptive,
        "reranker": spec.reranker,
        "top_k": top_k,
        "case_count": len(records),
        "successful_case_count": len(successful),
        "scored_case_count": len(scored),
        "hit_at_3": mean(scored, "hit_at_3"),
        "hit_at_5": mean(scored, "hit_at_5"),
        "hit_at_5_ci_low": hit5_ci[0] if scored else None,
        "hit_at_5_ci_high": hit5_ci[1] if scored else None,
        "mrr": mean(scored, "mrr"),
        "mrr_ci_low": mrr_ci[0] if scored else None,
        "mrr_ci_high": mrr_ci[1] if scored else None,
        "target_coverage": mean(scored, "target_coverage"),
        "sufficiency_pass": mean(successful, "sufficiency_pass"),
        "avg_latency_ms": round(sum(latency) / len(latency), 3) if latency else 0.0,
        "p50_latency_ms": round(percentile(latency, 0.50), 3),
        "p95_latency_ms": round(percentile(latency, 0.95), 3),
        "retriever_build_seconds": round(build_seconds, 3),
        "retriever_build_peak_mb": round(build_peak_mb, 3),
        "rerank_calls": int(runtime.get("rerank_calls", 0)),
        "rerank_failed_calls": int(runtime.get("rerank_failed_calls", 0)),
        "rerank_documents": int(runtime.get("rerank_documents", 0)),
        "rerank_total_ms": round(float(runtime.get("rerank_total_ms", 0.0)), 3),
        "assistant_llm_calls": sum(record.assistant_llm_calls for record in records),
        "normalizer_llm_calls": sum(record.normalizer_llm_calls for record in records),
        "judge_llm_calls": sum(record.judge_llm_calls for record in records),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": sum(record.total_tokens for record in records),
        "token_usage_calls": token_usage_calls,
        "estimated_cost_usd": round(estimated_cost, 8) if estimated_cost is not None else None,
        "eval_error_count": sum(bool(record.error) for record in records),
        "failure_counts": json.dumps(failure_counts, ensure_ascii=False, sort_keys=True),
    }


def failed_experiment(spec: ExperimentSpec, error: Exception | str, *, top_k: int) -> dict[str, Any]:
    message = str(error)
    return {
        "experiment_id": spec.experiment_id,
        "status": "failed",
        "error": message,
        "chunk_strategy": spec.chunk_strategy,
        "retriever": spec.retriever,
        "embedding": spec.embedding,
        "adaptive": spec.adaptive,
        "reranker": spec.reranker,
        "top_k": top_k,
    }


def write_experiment_matrix(
    rows: list[dict[str, Any]],
    output_dir: str | Path,
    prefix: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> tuple[Path, Path, Path]:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{prefix}.csv"
    json_path = out_dir / f"{prefix}.json"
    report_path = out_dir / f"{prefix}.md"
    fieldnames = ordered_fieldnames(rows)
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    payload = {
        "schema_version": 1,
        "created_at": utc_now(),
        "metadata": metadata or {},
        "experiments": rows,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(render_experiment_matrix(rows, metadata=metadata), encoding="utf-8")
    return csv_path, json_path, report_path


def render_experiment_matrix(
    rows: list[dict[str, Any]],
    *,
    metadata: dict[str, Any] | None = None,
) -> str:
    lines = ["# 实验矩阵报告", ""]
    if metadata:
        lines.extend(
            f"- {key}: `{value}`"
            for key, value in metadata.items()
            if value is not None and value != ""
        )
        lines.append("")
    lines.extend(
        [
            "| 状态 | Chunk | Retriever | Embedding | Adaptive | Reranker | Hit@5 | MRR | P95 ms | Build s | Rerank ms |",
            "| --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in rows:
        lines.append(
            "| {status} | {chunk_strategy} | {retriever} | {embedding} | {adaptive} | "
            "{reranker} | {hit_at_5} | {mrr} | {p95_latency_ms} | "
            "{retriever_build_seconds} | {rerank_total_ms} |".format(
                status=escape_cell(row.get("status", "")),
                chunk_strategy=escape_cell(row.get("chunk_strategy", "")),
                retriever=escape_cell(row.get("retriever", "")),
                embedding=escape_cell(row.get("embedding", "")),
                adaptive=escape_cell(row.get("adaptive", "")),
                reranker=escape_cell(row.get("reranker", "")),
                hit_at_5=format_ci(row, "hit_at_5"),
                mrr=format_metric(row.get("mrr")),
                p95_latency_ms=format_metric(row.get("p95_latency_ms"), digits=1),
                retriever_build_seconds=format_metric(row.get("retriever_build_seconds"), digits=3),
                rerank_total_ms=format_metric(row.get("rerank_total_ms"), digits=1),
            )
        )
    status_counts = Counter(str(row.get("status", "unknown")) for row in rows)
    lines.extend(["", "## 运行结论"])
    for status, count in sorted(status_counts.items()):
        lines.append(f"- `{status}`: {count}")
    for row in rows:
        if row.get("status") not in {"failed", "partial"}:
            continue
        lines.append(f"- `{row['experiment_id']}`: {row.get('error', '')}")
    lines.append("")
    return "\n".join(lines)


def mean(records: list[EvalRecord], field_name: str) -> float:
    if not records:
        return 0.0
    return round(sum(float(getattr(record, field_name)) for record in records) / len(records), 4)


def ordered_fieldnames(rows: list[dict[str, Any]]) -> list[str]:
    preferred = [
        "experiment_id",
        "status",
        "error",
        "chunk_strategy",
        "retriever",
        "embedding",
        "adaptive",
        "reranker",
        "top_k",
        "case_count",
        "successful_case_count",
        "scored_case_count",
        "hit_at_3",
        "hit_at_5",
        "hit_at_5_ci_low",
        "hit_at_5_ci_high",
        "mrr",
        "mrr_ci_low",
        "mrr_ci_high",
        "target_coverage",
        "sufficiency_pass",
        "avg_latency_ms",
        "p50_latency_ms",
        "p95_latency_ms",
        "retriever_build_seconds",
        "retriever_build_peak_mb",
        "rerank_calls",
        "rerank_failed_calls",
        "rerank_documents",
        "rerank_total_ms",
        "assistant_llm_calls",
        "normalizer_llm_calls",
        "judge_llm_calls",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "token_usage_calls",
        "estimated_cost_usd",
        "eval_error_count",
        "failure_counts",
    ]
    present = {key for row in rows for key in row}
    return [key for key in preferred if key in present] + sorted(present - set(preferred))


def escape_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def format_metric(value: Any, *, digits: int = 3) -> str:
    if value is None or value == "":
        return "N/A"
    return f"{float(value):.{digits}f}"


def format_ci(row: dict[str, Any], metric: str, *, digits: int = 3) -> str:
    value = row.get(metric)
    low = row.get(f"{metric}_ci_low")
    high = row.get(f"{metric}_ci_high")
    if value is None or value == "":
        return "N/A"
    rendered = format_metric(value, digits=digits)
    if low is None or high is None:
        return rendered
    return f"{rendered} [{float(low):.{digits}f}, {float(high):.{digits}f}]"
