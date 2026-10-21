"""Depth-N retrieval audit for candidate generation quality.

``evaluate`` measures the answer pipeline at its serving ``top_k``.  This audit
asks a different question: how deep does a retriever have to go before every
gold article shows up, and is the gold itself unambiguous in the corpus?  It
calls ``retriever.retrieve(question, top_k=depth)`` directly, without adaptive
planning, so the numbers isolate the retriever.

Note that fusion and reranking retrievers size their candidate pools from
``top_k``; their depth-N ranking is therefore not always a prefix-compatible
extension of the depth-5 ranking that ``evaluate`` sees.
"""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .evaluation import bootstrap_ci, percentile
from .evaluation_scoring import result_matches
from .models import Chunk, EvalCase, SearchResult
from .retrieval import Retriever


DEFAULT_AUDIT_KS = (1, 3, 5, 10, 20)
GOLD_UNIQUE = "unique"
GOLD_AMBIGUOUS = "ambiguous"
GOLD_MISSING = "missing"
GOLD_UNKNOWN = "unknown"


@dataclass(frozen=True)
class AuditCaseResult:
    case_id: str
    case_type: str
    gold_status: str
    gold_article_count: int
    first_hit_rank: int | None
    covered_at: dict[int, int]
    returned_count: int
    latency_ms: float
    error: str | None = None
    top_results: list[str] = field(default_factory=list)


def article_identity_index(chunks: Sequence[Chunk]) -> dict[tuple[str, str], set[str]]:
    """Map (law, article number) to the distinct article ids that carry it.

    Only single-article chunks are attributable; multi-article windows such as
    ``neighbor`` cannot say which law name belongs to which number.
    """

    index: dict[tuple[str, str], set[str]] = defaultdict(set)
    for chunk in chunks:
        if len(chunk.law_names) != 1 or len(chunk.article_numbers) != 1:
            continue
        article_ids = chunk.metadata.get("article_ids")
        if not article_ids:
            article_id = chunk.metadata.get("article_id")
            article_ids = [article_id] if article_id else []
        if len(article_ids) != 1:
            continue
        index[(chunk.law_names[0], chunk.article_numbers[0])].add(str(article_ids[0]))
    return dict(index)


def gold_status(
    case: EvalCase, identity_index: dict[tuple[str, str], set[str]] | None
) -> str:
    if identity_index is None or not case.expected_law or not case.expected_articles:
        return GOLD_UNKNOWN
    counts = [
        len(identity_index.get((case.expected_law, article), ()))
        for article in case.expected_articles
    ]
    if any(count == 0 for count in counts):
        return GOLD_MISSING
    if any(count > 1 for count in counts):
        return GOLD_AMBIGUOUS
    return GOLD_UNIQUE


def has_retrieval_gold(case: EvalCase) -> bool:
    return bool(case.expected_law or case.expected_articles)


def audit_case(
    case: EvalCase,
    retriever: Retriever,
    *,
    depth: int,
    ks: Sequence[int] = DEFAULT_AUDIT_KS,
    identity_index: dict[tuple[str, str], set[str]] | None = None,
) -> AuditCaseResult:
    started = time.perf_counter()
    error: str | None = None
    try:
        results = retriever.retrieve(case.question, top_k=depth)
    except Exception as exc:  # One failing case must not hide the rest.
        results = []
        error = type(exc).__name__
    latency_ms = (time.perf_counter() - started) * 1000
    matching_ranks = [result.rank for result in results if result_matches(result, case)]
    return AuditCaseResult(
        case_id=case.case_id,
        case_type=case.case_type,
        gold_status=gold_status(case, identity_index),
        gold_article_count=max(len(case.expected_articles), 1),
        first_hit_rank=min(matching_ranks) if matching_ranks else None,
        covered_at={k: covered_gold_articles(results, case, k) for k in ks},
        returned_count=len(results),
        latency_ms=round(latency_ms, 3),
        error=error,
        top_results=[_result_label(result) for result in results[:5]],
    )


def covered_gold_articles(results: Sequence[SearchResult], case: EvalCase, k: int) -> int:
    """Count gold articles found in the top-k, each counted at most once."""

    top = [result for result in results if result.rank <= k]
    if not case.expected_articles:
        return int(any(result_matches(result, case) for result in top))
    covered = 0
    for article in case.expected_articles:
        if any(
            (not case.expected_law or case.expected_law in result.chunk.law_names)
            and article in result.chunk.article_numbers
            for result in top
        ):
            covered += 1
    return covered


def audit_retrieval(
    cases: Sequence[EvalCase],
    retriever: Retriever,
    *,
    depth: int = 20,
    ks: Sequence[int] = DEFAULT_AUDIT_KS,
    chunks: Sequence[Chunk] | None = None,
) -> tuple[list[AuditCaseResult], dict[str, Any]]:
    resolved_ks = tuple(sorted({k for k in ks if 1 <= k <= depth} | {depth}))
    identity_index = article_identity_index(chunks) if chunks is not None else None
    gold_cases = [case for case in cases if has_retrieval_gold(case)]
    rows = [
        audit_case(
            case,
            retriever,
            depth=depth,
            ks=resolved_ks,
            identity_index=identity_index,
        )
        for case in gold_cases
    ]
    summary = summarize_audit(rows, ks=resolved_ks, depth=depth)
    summary["skipped_without_gold"] = len(cases) - len(gold_cases)
    return rows, summary


def summarize_audit(
    rows: Sequence[AuditCaseResult], *, ks: Sequence[int], depth: int
) -> dict[str, Any]:
    mrr_cutoff = min(10, depth)
    hits = {k: [_hit(row, k) for row in rows] for k in ks}
    recalls = {
        k: [row.covered_at[k] / row.gold_article_count for row in rows] for k in ks
    }
    reciprocal = [
        1.0 / row.first_hit_rank
        if row.first_hit_rank is not None and row.first_hit_rank <= mrr_cutoff
        else 0.0
        for row in rows
    ]
    unique_rows = [row for row in rows if row.gold_status == GOLD_UNIQUE]
    by_type: dict[str, list[AuditCaseResult]] = defaultdict(list)
    for row in rows:
        by_type[row.case_type].append(row)
    latencies = [row.latency_ms for row in rows]
    headline_k = 5 if 5 in ks else ks[0]
    return {
        "case_count": len(rows),
        "depth": depth,
        "ks": list(ks),
        "headline_k": headline_k,
        "error_count": sum(1 for row in rows if row.error),
        "hit_at": {str(k): _mean(values) for k, values in hits.items()},
        "hit_at_ci95": {
            str(k): list(bootstrap_ci([float(value) for value in values]))
            for k, values in hits.items()
            if k in {headline_k, depth}
        },
        f"mrr_at_{mrr_cutoff}": _mean(reciprocal),
        "article_recall_at": {str(k): _mean(values) for k, values in recalls.items()},
        "gold_status_counts": dict(sorted(Counter(row.gold_status for row in rows).items())),
        "unique_gold_hit_at": {
            str(k): _mean([_hit(row, k) for row in unique_rows]) for k in ks
        },
        "by_type": {
            case_type: {
                "count": len(items),
                f"hit_at_{headline_k}": _mean([_hit(row, headline_k) for row in items]),
                f"hit_at_{depth}": _mean([_hit(row, depth) for row in items]),
            }
            for case_type, items in sorted(by_type.items())
        },
        "latency_ms": {
            "mean": _mean(latencies),
            "p50": round(percentile(latencies, 0.5), 3),
            "p95": round(percentile(latencies, 0.95), 3),
        },
    }


def render_audit_markdown(summary: dict[str, Any], metadata: dict[str, Any]) -> str:
    ks = summary["ks"]
    depth = summary["depth"]
    mrr_key = f"mrr_at_{min(10, depth)}"
    lines = [
        "# 检索深度审计",
        "",
        "## 运行信息",
        *[f"- {key}: `{value}`" for key, value in metadata.items()],
        "",
        "## 总体指标",
        f"- 有 gold 的样例: {summary['case_count']}（跳过无 gold 样例 "
        f"{summary.get('skipped_without_gold', 0)}，检索错误 {summary['error_count']}）",
        f"- {mrr_key.upper()}: {summary[mrr_key]:.4f}",
        f"- gold 状态: `{json.dumps(summary['gold_status_counts'], ensure_ascii=False)}`",
        "",
        "| k | Hit@k | 全部目标条文召回@k | 仅 unique gold Hit@k |",
        "|---:|---:|---:|---:|",
    ]
    for k in ks:
        key = str(k)
        ci = summary["hit_at_ci95"].get(key)
        hit = f"{summary['hit_at'][key]:.4f}"
        if ci:
            hit += f" [{ci[0]:.3f}, {ci[1]:.3f}]"
        lines.append(
            f"| {k} | {hit} | {summary['article_recall_at'][key]:.4f} | "
            f"{summary['unique_gold_hit_at'][key]:.4f} |"
        )
    headline = f"hit_at_{summary['headline_k']}"
    lines.extend(
        [
            "",
            "## 分题型",
            "",
            f"| 题型 | 样例数 | {headline} | hit_at_{depth} |",
            "|---|---:|---:|---:|",
        ]
    )
    for case_type, item in summary["by_type"].items():
        lines.append(
            f"| {case_type} | {item['count']} | {item[headline]:.4f} | "
            f"{item[f'hit_at_{depth}']:.4f} |"
        )
    latency = summary["latency_ms"]
    lines.extend(
        [
            "",
            "## 延迟",
            f"- mean / P50 / P95: {latency['mean']:.1f} / {latency['p50']:.1f} / "
            f"{latency['p95']:.1f} ms",
            "",
        ]
    )
    integrity = metadata.get("integrity_summary")
    if integrity:
        lines.extend(["## 语料完整性摘要", f"- `{integrity}`", ""])
    return "\n".join(lines)


def write_audit_outputs(
    rows: Sequence[AuditCaseResult],
    summary: dict[str, Any],
    metadata: dict[str, Any],
    *,
    output_dir: str | Path,
    prefix: str,
    integrity: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"{prefix}.json"
    md_path = directory / f"{prefix}.md"
    payload = {
        "schema_version": 1,
        "metadata": metadata,
        "summary": summary,
        "integrity": integrity,
        "cases": [asdict(row) for row in rows],
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path.write_text(render_audit_markdown(summary, metadata), encoding="utf-8")
    return json_path, md_path


def _hit(row: AuditCaseResult, k: int) -> int:
    return int(row.first_hit_rank is not None and row.first_hit_rank <= k)


def _mean(values: Sequence[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _result_label(result: SearchResult) -> str:
    law = "、".join(result.chunk.law_names) or "?"
    articles = "、".join(result.chunk.article_numbers) or "?"
    return f"{result.rank}:{law}{articles}"
