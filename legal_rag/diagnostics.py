from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from .data import summarize_lengths
from .models import Chunk


def build_chunk_diagnostics(chunks: list[Chunk], *, strategy: str) -> dict[str, Any]:
    char_lengths = [len(chunk.text) for chunk in chunks]
    article_counts = [int(chunk.metadata.get("article_count", len(chunk.article_numbers))) for chunk in chunks]
    source_counts = [len(chunk.source_files) for chunk in chunks]
    law_counts = [len(chunk.law_names) for chunk in chunks]
    article_span_lengths = [article_span_length(chunk) for chunk in chunks]
    by_law = Counter(law for chunk in chunks for law in chunk.law_names)

    return {
        "strategy": strategy,
        "chunk_count": len(chunks),
        "char_length": summarize_lengths(char_lengths),
        "article_count_per_chunk": summarize_lengths(article_counts),
        "article_span_length": summarize_lengths(article_span_lengths),
        "source_count_per_chunk": summarize_lengths(source_counts),
        "law_count_per_chunk": summarize_lengths(law_counts),
        "top_laws_by_chunks": [
            {"law": law, "chunks": count}
            for law, count in by_law.most_common(10)
        ],
        "anomalies": collect_chunk_anomalies(chunks),
    }


def article_span_length(chunk: Chunk) -> int:
    if "article_count" in chunk.metadata:
        return int(chunk.metadata.get("article_count") or 0)
    if "article_ids" in chunk.metadata:
        return len(chunk.metadata.get("article_ids") or [])
    return len(chunk.article_numbers)


def collect_chunk_anomalies(chunks: list[Chunk], *, limit: int = 20) -> list[dict[str, Any]]:
    anomalies: list[dict[str, Any]] = []
    if not chunks:
        return [{"type": "empty_index", "message": "No chunks were produced."}]

    lengths = sorted(len(chunk.text) for chunk in chunks)
    p99 = lengths[min(int(len(lengths) * 0.99), len(lengths) - 1)]
    long_threshold = max(1200, p99)

    for chunk in chunks:
        if len(anomalies) >= limit:
            break
        reasons: list[str] = []
        if not chunk.text.strip():
            reasons.append("empty_text")
        if not chunk.law_names:
            reasons.append("missing_law")
        if not chunk.article_numbers:
            reasons.append("missing_article")
        if len(chunk.text) > long_threshold:
            reasons.append("very_long")
        if chunk.strategy == "neighbor" and int(chunk.metadata.get("article_count", 0)) <= 1:
            reasons.append("neighbor_single_article")
        if reasons:
            anomalies.append(
                {
                    "chunk_id": chunk.chunk_id,
                    "reasons": reasons,
                    "law_names": chunk.law_names,
                    "article_numbers": chunk.article_numbers,
                    "source_files": chunk.source_files[:3],
                    "line_nos": chunk.line_nos[:5],
                    "char_length": len(chunk.text),
                    "text_preview": chunk.text[:180],
                }
            )
    return anomalies


def write_chunk_diagnostics(
    diagnostics: dict[str, Any],
    output_dir: str | Path,
) -> tuple[Path, Path]:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "diagnostics.json"
    md_path = out_dir / "diagnostics.md"
    json_path.write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_chunk_diagnostics_markdown(diagnostics), encoding="utf-8")
    return json_path, md_path


def render_chunk_diagnostics_markdown(diagnostics: dict[str, Any]) -> str:
    char_length = diagnostics.get("char_length", {})
    article_count = diagnostics.get("article_count_per_chunk", {})
    article_span = diagnostics.get("article_span_length", {})
    lines = [
        "# Chunk 诊断报告",
        "",
        "## 总览",
        f"- 策略: `{diagnostics.get('strategy', '')}`",
        f"- Chunk 数: {diagnostics.get('chunk_count', 0)}",
        f"- 长度 P50/P90/P99/Max: {char_length.get('p50', 0)} / {char_length.get('p90', 0)} / {char_length.get('p99', 0)} / {char_length.get('max', 0)}",
        f"- 每 chunk 条文数 P50/P90/Max: {article_count.get('p50', 0)} / {article_count.get('p90', 0)} / {article_count.get('max', 0)}",
        f"- 条文 span P50/P90/Max: {article_span.get('p50', 0)} / {article_span.get('p90', 0)} / {article_span.get('max', 0)}",
        "",
        "## Top Laws",
    ]
    for item in diagnostics.get("top_laws_by_chunks", []):
        lines.append(f"- {item.get('law', '')}: {item.get('chunks', 0)} chunks")

    lines.extend(["", "## 异常样例"])
    anomalies = diagnostics.get("anomalies", [])
    if not anomalies:
        lines.append("- 未发现明显异常。")
    for item in anomalies:
        lines.append(
            f"- `{item.get('chunk_id', '')}` {','.join(item.get('reasons', []))} "
            f"{item.get('law_names', [])} {item.get('article_numbers', [])} "
            f"len={item.get('char_length', 0)}"
        )
    lines.append("")
    return "\n".join(lines)
