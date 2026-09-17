from __future__ import annotations

import csv
import json
import random
import time
from collections import defaultdict
from dataclasses import fields
from pathlib import Path
from typing import Any

from .adaptive import retrieve_adaptive
from .chat import LegalChatAssistant
from .evidence import check_evidence_sufficiency
from .failure_analysis import label_retrieval_failure
from .judge import judge_answer
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
    judge_client: CompletionClient | None = None,
) -> list[EvalRecord]:
    records: list[EvalRecord] = []
    for case in cases:
        if assistant is not None:
            # Each case must be answered in isolation; otherwise conversation
            # memory leaks across cases and contaminates model comparisons.
            assistant.reset_memory()
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
        judge_result = None
        if judge_client is not None and generate and not error:
            judge_result = judge_answer(
                judge_client,
                question=case.question,
                answer=answer,
                results=results,
            )
        judge_succeeded = judge_result is not None and judge_result.source != "error"
        answer_metrics_available = generate and not error
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
                citation_valid=int(verification.citation_valid) if answer_metrics_available else -1,
                verifier_pass=int(verification.passed) if answer_metrics_available else -1,
                refusal_correctness=(
                    int(verification.refusal_correct) if answer_metrics_available else -1
                ),
                latency_ms=latency_ms,
                answer=answer[:1200],
                sources=format_sources(results),
                error=error,
                failure_label=failure.label,
                failure_reason=failure.reason,
                judge_faithfulness=judge_result.faithfulness if judge_succeeded else -1.0,
                judge_relevance=judge_result.relevance if judge_succeeded else -1.0,
                judge_completeness=judge_result.completeness if judge_succeeded else -1.0,
                judge_pass=int(judge_result.passed) if judge_succeeded else -1,
                judge_comment=judge_result.comment if judge_succeeded else "",
                judge_error=judge_result.error if judge_result and not judge_succeeded else "",
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


def bootstrap_ci(
    values: list[float],
    *,
    n_resamples: int = 2000,
    confidence: float = 0.95,
    seed: int = 42,
) -> tuple[float, float]:
    """Percentile bootstrap confidence interval for the mean of `values`."""
    if n_resamples < 1:
        raise ValueError("n_resamples must be at least 1")
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be between 0 and 1")
    if not values:
        return (0.0, 0.0)
    if len(values) == 1:
        return (values[0], values[0])
    rng = random.Random(seed)
    n = len(values)
    means = sorted(
        sum(rng.choice(values) for _ in range(n)) / n for _ in range(n_resamples)
    )
    alpha = (1.0 - confidence) / 2
    lower = means[int(alpha * n_resamples)]
    upper = means[min(int((1.0 - alpha) * n_resamples), n_resamples - 1)]
    return (round(lower, 4), round(upper, 4))


def scored_records(records: list[EvalRecord]) -> list[EvalRecord]:
    """Records with a retrieval target (refusal cases have none and would dilute hit metrics)."""
    return [record for record in records if record.failure_label != "not_applicable"]


def available_mean(records: list[EvalRecord], field_name: str) -> float | None:
    values = [float(getattr(record, field_name)) for record in records]
    available = [value for value in values if value >= 0.0]
    if not available:
        return None
    return sum(available) / len(available)


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
    avg_citation_valid = available_mean(records, "citation_valid")
    avg_verifier = available_mean(records, "verifier_pass")
    avg_refusal = available_mean(records, "refusal_correctness")
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
        f"- Citation validity: {avg_citation_valid:.3f}" if avg_citation_valid is not None else "- Citation validity: N/A (retrieval-only)",
        f"- Verifier pass: {avg_verifier:.3f}" if avg_verifier is not None else "- Verifier pass: N/A (retrieval-only)",
        f"- Refusal correctness: {avg_refusal:.3f}" if avg_refusal is not None else "- Refusal correctness: N/A (retrieval-only)",
        f"- 平均延迟: {avg_latency:.1f} ms",
    ])
    scored = scored_records(records)
    if scored:
        scored_hit3 = [float(record.hit_at_3) for record in scored]
        scored_hit5 = [float(record.hit_at_5) for record in scored]
        scored_mrr = [record.mrr for record in scored]
        hit3_ci = bootstrap_ci(scored_hit3)
        hit5_ci = bootstrap_ci(scored_hit5)
        mrr_ci = bootstrap_ci(scored_mrr)
        lines.extend([
            "",
            f"## 有目标样例指标 (n={len(scored)}, 排除拒答类, bootstrap 95% CI)",
            f"- Hit@3: {sum(scored_hit3) / len(scored):.3f} [{hit3_ci[0]:.3f}, {hit3_ci[1]:.3f}]",
            f"- Hit@5: {sum(scored_hit5) / len(scored):.3f} [{hit5_ci[0]:.3f}, {hit5_ci[1]:.3f}]",
            f"- MRR: {sum(scored_mrr) / len(scored):.3f} [{mrr_ci[0]:.3f}, {mrr_ci[1]:.3f}]",
        ])
    judged = [record for record in records if record.judge_pass >= 0]
    judge_errors = [record for record in records if record.judge_error]
    if judged or judge_errors:
        lines.extend([
            "",
            f"## LLM Judge 指标 (成功 n={len(judged)}, 失败 n={len(judge_errors)})",
        ])
    if judged:
        avg_faithfulness = sum(record.judge_faithfulness for record in judged) / len(judged)
        avg_relevance = sum(record.judge_relevance for record in judged) / len(judged)
        avg_completeness = sum(record.judge_completeness for record in judged) / len(judged)
        pass_rate = sum(record.judge_pass for record in judged) / len(judged)
        lines.extend([
            f"- Faithfulness: {avg_faithfulness:.3f}",
            f"- Relevance: {avg_relevance:.3f}",
            f"- Completeness: {avg_completeness:.3f}",
            f"- Judge pass rate: {pass_rate:.3f}",
        ])
    if judge_errors:
        lines.append("- Judge 调用或格式错误已从质量均值中排除。")
    models = sorted({record.model for record in records})
    if len(models) > 1:
        lines.extend([
            "",
            "## 按模型分组",
        ])
        for model_name in models:
            group = [record for record in records if record.model == model_name]
            group_scored = scored_records(group)
            hit5 = (
                sum(record.hit_at_5 for record in group_scored) / len(group_scored)
                if group_scored
                else 0.0
            )
            keyword = sum(record.keyword_coverage for record in group) / len(group)
            verifier = available_mean(group, "verifier_pass")
            verifier_text = f"{verifier:.3f}" if verifier is not None else "N/A"
            latency = sum(record.latency_ms for record in group) / len(group)
            line = (
                f"- `{model_name}` n={len(group)} Hit@5(scored)={hit5:.3f} "
                f"KeywordCov={keyword:.3f} VerifierPass={verifier_text} "
            )
            line += f"latency={latency:.1f}ms"
            judged_group = [record for record in group if record.judge_pass >= 0]
            if judged_group:
                faith = sum(record.judge_faithfulness for record in judged_group) / len(judged_group)
                jpass = sum(record.judge_pass for record in judged_group) / len(judged_group)
                line += f" JudgeFaith={faith:.3f} JudgePass={jpass:.3f}"
            judge_error_count = sum(bool(record.judge_error) for record in group)
            if judge_error_count:
                line += f" JudgeErrors={judge_error_count}"
            lines.append(line)
    lines.extend([
        "",
        "## 分组指标",
    ])
    for case_type, group in grouped_records(records).items():
        group_hit3 = sum(record.hit_at_3 for record in group) / len(group)
        group_hit5 = sum(record.hit_at_5 for record in group) / len(group)
        group_mrr = sum(record.mrr for record in group) / len(group)
        group_target = sum(record.target_coverage for record in group) / len(group)
        group_sufficiency = sum(record.sufficiency_pass for record in group) / len(group)
        group_verifier = available_mean(group, "verifier_pass")
        group_verifier_text = f"{group_verifier:.3f}" if group_verifier is not None else "N/A"
        group_latency = sum(record.latency_ms for record in group) / len(group)
        lines.append(
            f"- `{case_type}` n={len(group)} Hit@3={group_hit3:.3f} "
            f"Hit@5={group_hit5:.3f} MRR={group_mrr:.3f} "
            f"TargetCoverage={group_target:.3f} Sufficiency={group_sufficiency:.3f} "
            f"Verifier={group_verifier_text} latency={group_latency:.1f}ms"
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
        ("index_size_mb", "索引体积 (MB)"),
        ("retriever_build_seconds", "检索器构建耗时 (s)"),
        ("retriever_build_peak_mb", "检索器构建峰值内存 (MB)"),
        ("judge_model", "Judge 模型"),
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
