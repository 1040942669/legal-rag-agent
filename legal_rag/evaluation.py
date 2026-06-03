from __future__ import annotations

import csv
import json
import time
from collections import defaultdict
from pathlib import Path

from .chat import LegalChatAssistant
from .models import EvalCase, EvalRecord, SearchResult
from .retrieval import Retriever, format_sources


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
) -> list[EvalRecord]:
    records: list[EvalRecord] = []
    for case in cases:
        started = time.perf_counter()
        answer = ""
        error = ""
        try:
            if generate:
                if assistant is None:
                    raise RuntimeError("assistant is required when generate=True")
                answer, results = assistant.answer(case.question, generate=True)
            else:
                results = retriever.retrieve(case.question, top_k=top_k)
                answer = "\n".join(result.chunk.text for result in results)
        except Exception as exc:  # Keep evaluation running across model failures.
            results = []
            error = str(exc)
        latency_ms = int((time.perf_counter() - started) * 1000)
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
                latency_ms=latency_ms,
                answer=answer[:1200],
                sources=format_sources(results),
                error=error,
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


def write_eval_outputs(records: list[EvalRecord], output_dir: str | Path, prefix: str) -> tuple[Path, Path]:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{prefix}.csv"
    report_path = out_dir / f"{prefix}.md"

    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(EvalRecord.__dataclass_fields__.keys()))
        writer.writeheader()
        for record in records:
            writer.writerow(record.__dict__)

    report_path.write_text(render_eval_report(records), encoding="utf-8")
    return csv_path, report_path


def render_eval_report(records: list[EvalRecord]) -> str:
    if not records:
        return "# 评估报告\n\n没有评估记录。\n"
    avg_hit3 = sum(record.hit_at_3 for record in records) / len(records)
    avg_hit5 = sum(record.hit_at_5 for record in records) / len(records)
    avg_mrr = sum(record.mrr for record in records) / len(records)
    avg_target_coverage = sum(record.target_coverage for record in records) / len(records)
    avg_latency = sum(record.latency_ms for record in records) / len(records)
    avg_keyword = sum(record.keyword_coverage for record in records) / len(records)
    first = records[0]

    lines = [
        "# 评估报告",
        "",
        "## 实验配置",
        f"- 模型: `{first.model}`",
        f"- 检索器: `{first.retriever}`",
        f"- Chunk 策略: `{first.chunk_strategy}`",
        f"- 样例数: {len(records)}",
        "",
        "## 汇总指标",
        f"- Hit@3: {avg_hit3:.3f}",
        f"- Hit@5: {avg_hit5:.3f}",
        f"- MRR: {avg_mrr:.3f}",
        f"- 目标条文覆盖率: {avg_target_coverage:.3f}",
        f"- 关键词覆盖率: {avg_keyword:.3f}",
        f"- 平均延迟: {avg_latency:.1f} ms",
        "",
        "## 分组指标",
    ]
    for case_type, group in grouped_records(records).items():
        group_hit3 = sum(record.hit_at_3 for record in group) / len(group)
        group_hit5 = sum(record.hit_at_5 for record in group) / len(group)
        group_mrr = sum(record.mrr for record in group) / len(group)
        group_target = sum(record.target_coverage for record in group) / len(group)
        group_latency = sum(record.latency_ms for record in group) / len(group)
        lines.append(
            f"- `{case_type}` n={len(group)} Hit@3={group_hit3:.3f} "
            f"Hit@5={group_hit5:.3f} MRR={group_mrr:.3f} "
            f"TargetCoverage={group_target:.3f} latency={group_latency:.1f}ms"
        )
    lines.extend([
        "",
        "## 失败样例",
    ])
    failures = [
        record
        for record in records
        if record.case_type != "refusal" and record.hit_at_5 == 0 and not record.error
    ]
    if not failures:
        lines.append("- 未发现 Hit@5 失败样例。")
    for record in failures[:10]:
        lines.append(f"- `{record.case_id}` sources: {record.sources.splitlines()[:2]}")
    errors = [record for record in records if record.error]
    if errors:
        lines.extend(["", "## 运行错误"])
        for record in errors[:10]:
            lines.append(f"- `{record.case_id}` {record.error}")
    lines.append("")
    return "\n".join(lines)


def grouped_records(records: list[EvalRecord]) -> dict[str, list[EvalRecord]]:
    groups: dict[str, list[EvalRecord]] = defaultdict(list)
    for record in records:
        groups[record.case_type].append(record)
    return dict(sorted(groups.items()))
