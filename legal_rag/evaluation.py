from __future__ import annotations

import csv
import json
import time
from collections import defaultdict
from dataclasses import fields
from pathlib import Path
from typing import Any

from .adaptive import retrieve_adaptive
from .chat import LegalChatAssistant
from .evidence import check_evidence_sufficiency
from .failure_analysis import label_retrieval_failure
from .models import EvalCase, EvalRecord, SearchResult
from .query import analyze_query
from .query_understanding import CompletionClient
from .retrieval import Retriever, format_sources
from .tracing import JsonlTraceWriter, build_retrieval_trace_record
from .verifier import verify_answer


def load_eval_cases(path: str | Path) -> list[EvalCase]:
    cases: list[EvalCase] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            item = json.loads(line)
            cases.append(
                EvalCase(
                    case_id=item["id"],
                    question=item["question"],
                    case_type=item.get("type", "unknown"),
                    expected_law=item.get("expected_law", ""),
                    expected_articles=item.get("expected_articles", []),
                    keywords=item.get("keywords", []),
                )
            )
    return cases


def evaluate(
    *,
    cases: list[EvalCase],
    retriever: Retriever,
    chunk_strategy: str,
    model: str = "retrieval-only",
    generate: bool = False,
    top_k: int = 5,
    assistant: LegalChatAssistant | None = None,
    trace_writer: JsonlTraceWriter | None = None,
    trace_metadata: dict[str, Any] | None = None,
    adaptive_enabled: bool = False,
    adaptive_use_llm: bool = False,
    adaptive_llm_client: CompletionClient | None = None,
    adaptive_max_queries: int = 3,
    adaptive_per_plan_top_k: int | None = None,
    normalizer_retries: int = 0,
) -> list[EvalRecord]:
    records: list[EvalRecord] = []
    for case in cases:
        analysis = analyze_query(case.question)
        adaptive_trace: dict[str, Any] = {"enabled": adaptive_enabled, "used": False}
        started = time.perf_counter()
        answer = ""
        error = ""
        evidence_check = None
        verification = None
        try:
            if generate:
                if assistant is None:
                    raise RuntimeError("assistant is required when generate=True")
                answer, results = assistant.answer(case.question, generate=True)
                if assistant.last_adaptive_result:
                    analysis = assistant.last_adaptive_result.analysis
                    adaptive_trace = assistant.last_adaptive_result.to_trace()
                    evidence_check = assistant.last_evidence_check
                    verification = assistant.last_verification
            else:
                adaptive_result = retrieve_adaptive(
                    case.question,
                    retriever,
                    top_k=top_k,
                    enabled=adaptive_enabled,
                    use_llm=adaptive_use_llm,
                    llm_client=adaptive_llm_client,
                    max_queries=adaptive_max_queries,
                    per_plan_top_k=adaptive_per_plan_top_k,
                    normalizer_retries=normalizer_retries,
                )
                results = adaptive_result.results
                analysis = adaptive_result.analysis
                adaptive_trace = adaptive_result.to_trace() if adaptive_enabled else adaptive_trace
                evidence_check = adaptive_result.evidence_check
                answer = "\n".join(result.chunk.text for result in results)
                verification = verify_answer(
                    answer,
                    results,
                    evidence_check=evidence_check,
                    risk_flags=analysis.risk_flags,
                )
        except Exception as exc:  # Keep evaluation running across model failures.
            results = []
            error = str(exc)
        if evidence_check is None:
            evidence_check = check_evidence_sufficiency(case.question, results, analysis=analysis)
        if verification is None:
            verification = verify_answer(
                answer,
                results,
                evidence_check=evidence_check,
                risk_flags=analysis.risk_flags,
            )
        latency_ms = int((time.perf_counter() - started) * 1000)
        failure = label_retrieval_failure(results, case, top_k=top_k)
        if trace_writer:
            trace_writer.write(
                build_retrieval_trace_record(
                    case_id=case.case_id,
                    query=case.question,
                    retriever=getattr(retriever, "name", "unknown"),
                    top_k=top_k,
                    results=results,
                    latency_ms=latency_ms,
                    analyzer=analysis.to_dict(),
                    adaptive=adaptive_trace,
                    evidence=evidence_check.to_dict(),
                    verifier=verification.to_dict(),
                    failure=failure.to_dict(),
                    metadata=trace_metadata,
                )
            )
        records.append(
            EvalRecord(
                case_id=case.case_id,
                case_type=case.case_type,
                model=model,
                retriever=getattr(retriever, "name", "unknown"),
                chunk_strategy=chunk_strategy,
                hit_at_3=hit_at_k(results, case, 3),
                hit_at_5=hit_at_k(results, case, 5),
                mrr=mean_reciprocal_rank(results, case),
                target_coverage=target_coverage(results, case, top_k),
                keyword_coverage=keyword_coverage(answer, case.keywords),
                citation_hit=citation_hit(results, case),
                sufficiency_pass=int(evidence_check.sufficient),
                citation_valid=int(verification.citation_valid),
                verifier_pass=int(verification.passed),
                refusal_correctness=int(verification.refusal_correct),
                latency_ms=latency_ms,
                answer=answer[:1200],
                sources=format_sources(results),
                error=error,
                failure_label=failure.label,
                failure_reason=failure.reason,
            )
        )
    return records


def hit_at_k(results: list[SearchResult], case: EvalCase, k: int) -> int:
    if not case.expected_law and not case.expected_articles:
        return 0
    return int(any(result_matches(result, case) for result in results[:k]))


def mean_reciprocal_rank(results: list[SearchResult], case: EvalCase) -> float:
    for result in results:
        if result_matches(result, case):
            return round(1 / result.rank, 4)
    return 0.0


def citation_hit(results: list[SearchResult], case: EvalCase) -> int:
    if not case.expected_law and not case.expected_articles:
        return 0
    return int(any(result_matches(result, case) for result in results))


def target_coverage(results: list[SearchResult], case: EvalCase, k: int = 5) -> float:
    if not case.expected_articles:
        return 0.0
    found = set()
    for result in results[:k]:
        if case.expected_law and case.expected_law not in result.chunk.law_names:
            continue
        for article in case.expected_articles:
            if article in result.chunk.article_numbers:
                found.add(article)
    return round(len(found) / len(case.expected_articles), 4)


def result_matches(result: SearchResult, case: EvalCase) -> bool:
    if not case.expected_law and not case.expected_articles:
        return False
    law_ok = not case.expected_law or case.expected_law in result.chunk.law_names
    article_ok = not case.expected_articles or any(
        article in result.chunk.article_numbers for article in case.expected_articles
    )
    return law_ok and article_ok


def keyword_coverage(answer: str, keywords: list[str]) -> float:
    if not keywords:
        return 0.0
    matched = sum(1 for keyword in keywords if keyword and keyword in answer)
    return round(matched / len(keywords), 4)


def write_eval_outputs(
    records: list[EvalRecord],
    output_dir: str | Path,
    prefix: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{prefix}.csv"
    report_path = out_dir / f"{prefix}.md"

    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[field.name for field in fields(EvalRecord)])
        writer.writeheader()
        for record in records:
            writer.writerow(record.__dict__)

    report_path.write_text(render_eval_report(records, metadata=metadata), encoding="utf-8")
    return csv_path, report_path


def render_eval_report(records: list[EvalRecord], *, metadata: dict[str, Any] | None = None) -> str:
    if not records:
        return "# 评估报告\n\n没有评估记录。\n"
    avg_hit3 = sum(record.hit_at_3 for record in records) / len(records)
    avg_hit5 = sum(record.hit_at_5 for record in records) / len(records)
    avg_mrr = sum(record.mrr for record in records) / len(records)
    avg_target_coverage = sum(record.target_coverage for record in records) / len(records)
    avg_latency = sum(record.latency_ms for record in records) / len(records)
    avg_keyword = sum(record.keyword_coverage for record in records) / len(records)
    avg_sufficiency = sum(record.sufficiency_pass for record in records) / len(records)
    avg_citation_valid = sum(record.citation_valid for record in records) / len(records)
    avg_verifier = sum(record.verifier_pass for record in records) / len(records)
    avg_refusal = sum(record.refusal_correctness for record in records) / len(records)
    first = records[0]

    lines = [
        "# 评估报告",
        "",
        "## 实验配置",
        f"- 模型: `{first.model}`",
        f"- 检索器: `{first.retriever}`",
        f"- Chunk 策略: `{first.chunk_strategy}`",
        f"- 样例数: {len(records)}",
    ]
    if metadata:
        for label, value in report_metadata_items(metadata):
            lines.append(f"- {label}: {value}")
    lines.extend([
        "",
        "## 汇总指标",
        f"- Hit@3: {avg_hit3:.3f}",
        f"- Hit@5: {avg_hit5:.3f}",
        f"- MRR: {avg_mrr:.3f}",
        f"- 目标条文覆盖率: {avg_target_coverage:.3f}",
        f"- 关键词覆盖率: {avg_keyword:.3f}",
        f"- Evidence sufficiency pass: {avg_sufficiency:.3f}",
        f"- Citation validity: {avg_citation_valid:.3f}",
        f"- Verifier pass: {avg_verifier:.3f}",
        f"- Refusal correctness: {avg_refusal:.3f}",
        f"- 平均延迟: {avg_latency:.1f} ms",
        "",
        "## 分组指标",
    ])
    for case_type, group in grouped_records(records).items():
        group_hit3 = sum(record.hit_at_3 for record in group) / len(group)
        group_hit5 = sum(record.hit_at_5 for record in group) / len(group)
        group_mrr = sum(record.mrr for record in group) / len(group)
        group_target = sum(record.target_coverage for record in group) / len(group)
        group_sufficiency = sum(record.sufficiency_pass for record in group) / len(group)
        group_verifier = sum(record.verifier_pass for record in group) / len(group)
        group_latency = sum(record.latency_ms for record in group) / len(group)
        lines.append(
            f"- `{case_type}` n={len(group)} Hit@3={group_hit3:.3f} "
            f"Hit@5={group_hit5:.3f} MRR={group_mrr:.3f} "
            f"TargetCoverage={group_target:.3f} Sufficiency={group_sufficiency:.3f} "
            f"Verifier={group_verifier:.3f} latency={group_latency:.1f}ms"
        )
    failure_counts = defaultdict(int)
    for record in records:
        if record.failure_label:
            failure_counts[record.failure_label] += 1
    lines.extend([
        "",
        "## 失败归因",
    ])
    for label, count in sorted(failure_counts.items()):
        lines.append(f"- `{label}`: {count}")
    lines.extend([
        "",
        "## 失败样例",
    ])
    failures = [
        record
        for record in records
        if record.case_type != "refusal"
        and record.failure_label not in {"", "hit", "not_applicable"}
        and not record.error
    ]
    if not failures:
        lines.append("- 未发现 Hit@5 失败样例。")
    for record in failures[:10]:
        lines.append(
            f"- `{record.case_id}` `{record.failure_label}` {record.failure_reason} "
            f"sources: {record.sources.splitlines()[:2]}"
        )
    errors = [record for record in records if record.error]
    if errors:
        lines.extend(["", "## 运行错误"])
        for record in errors[:10]:
            lines.append(f"- `{record.case_id}` {record.error}")
    lines.append("")
    return "\n".join(lines)


def report_metadata_items(metadata: dict[str, Any]) -> list[tuple[str, str]]:
    ordered_keys = [
        ("run_id", "Run ID"),
        ("config_path", "配置文件"),
        ("case_path", "评测集"),
        ("top_k", "Top K"),
        ("chunk_count", "Chunk 数"),
        ("adaptive_enabled", "Adaptive enabled"),
        ("adaptive_use_llm", "Adaptive LLM normalizer"),
        ("adaptive_max_queries", "Adaptive max queries"),
        ("adaptive_per_plan_top_k", "Adaptive per-plan top K"),
        ("adaptive_normalizer_retries", "Adaptive normalizer retries"),
        ("bm25_k1", "BM25 k1"),
        ("bm25_b", "BM25 b"),
        ("bm25_law_boost", "BM25 law boost"),
        ("bm25_article_boost", "BM25 article boost"),
        ("index_manifest_path", "Index manifest"),
        ("diagnostics_path", "Chunk diagnostics"),
        ("trace_path", "Trace JSONL"),
        ("embedding_key", "Embedding key"),
        ("embedding_cache_dir", "Embedding cache"),
    ]
    items: list[tuple[str, str]] = []
    for key, label in ordered_keys:
        value = metadata.get(key)
        if value in {None, ""}:
            continue
        if isinstance(value, (int, float)):
            rendered = str(value)
        else:
            rendered = f"`{value}`"
        items.append((label, rendered))
    return items


def grouped_records(records: list[EvalRecord]) -> dict[str, list[EvalRecord]]:
    groups: dict[str, list[EvalRecord]] = defaultdict(list)
    for record in records:
        groups[record.case_type].append(record)
    return dict(sorted(groups.items()))
