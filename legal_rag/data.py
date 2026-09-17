from __future__ import annotations

import hashlib
import json
import re
import statistics
from collections import Counter
from pathlib import Path
from typing import Iterable

from .models import LawArticle


ARTICLE_NUM_CHARS = r"零一二三四五六七八九十百千万壹贰叁肆伍陆柒捌玖拾佰仟\d"
ARTICLE_NUMBER = rf"第[{ARTICLE_NUM_CHARS}]+\s*条"
ARTICLE_WITH_LAW_MARKER_RE = re.compile(
    rf"《(?P<law>[^》]+)》(?P<article>{ARTICLE_NUMBER})规定[，,]"
)
ARTICLE_WITHOUT_LAW_MARKER_RE = re.compile(rf"(?P<article>{ARTICLE_NUMBER})\s*")


def normalize_article_number(raw: str) -> str:
    return re.sub(r"\s+(?=条)", "", raw.strip())


def split_line_into_articles(line: str, law_from_file: str) -> tuple[str | None, list[tuple[str, str, str, str, str]]]:
    with_law_matches = list(ARTICLE_WITH_LAW_MARKER_RE.finditer(line))
    if with_law_matches:
        leading = line[: with_law_matches[0].start()]
        return (
            leading if leading.strip() else "",
            _build_with_law_segments(line, with_law_matches),
        )

    without_law_matches = list(ARTICLE_WITHOUT_LAW_MARKER_RE.finditer(line))
    if without_law_matches:
        leading = line[: without_law_matches[0].start()]
        return (
            leading if leading.strip() else "",
            _build_without_law_segments(line, without_law_matches, law_from_file),
        )

    return None, []


def _build_with_law_segments(
    line: str,
    matches: list[re.Match[str]],
) -> list[tuple[str, str, str, str, str]]:
    segments: list[tuple[str, str, str, str, str]] = []
    for index, match in enumerate(matches):
        segment_end = matches[index + 1].start() if index + 1 < len(matches) else len(line)
        segment_text = line[match.start() : segment_end].strip()
        body = line[match.end() : segment_end].strip()
        segments.append(
            (
                match.group("law").strip(),
                normalize_article_number(match.group("article")),
                body,
                segment_text,
                "with_law",
            )
        )
    return segments


def _build_without_law_segments(
    line: str,
    matches: list[re.Match[str]],
    law_from_file: str,
) -> list[tuple[str, str, str, str, str]]:
    segments: list[tuple[str, str, str, str, str]] = []
    for index, match in enumerate(matches):
        segment_end = matches[index + 1].start() if index + 1 < len(matches) else len(line)
        segment_text = line[match.start() : segment_end].strip()
        body = line[match.end() : segment_end].strip()
        segments.append(
            (
                law_from_file,
                normalize_article_number(match.group("article")),
                body,
                segment_text,
                "from_filename",
            )
        )
    return segments


def discover_law_files(dataset_dir: str | Path) -> list[Path]:
    root = Path(dataset_dir)
    if not root.exists():
        raise FileNotFoundError(f"Dataset directory not found: {root}")
    return sorted(path for path in root.glob("*.txt") if path.is_file())


def parse_law_file(path: str | Path) -> list[LawArticle]:
    file_path = Path(path)
    law_from_file = file_path.stem
    articles: list[LawArticle] = []

    for line_no, raw_line in enumerate(file_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        leading_text, segments = split_line_into_articles(line, law_from_file)
        if segments:
            if leading_text:
                if articles:
                    last_article = articles[-1]
                    new_body = last_article.body + f"\n{leading_text.strip()}"
                    new_raw_text = last_article.raw_text + f"\n{leading_text.strip()}"
                    articles[-1] = LawArticle(
                        article_id=last_article.article_id,
                        law_name=last_article.law_name,
                        article_number=last_article.article_number,
                        body=new_body,
                        raw_text=new_raw_text,
                        source_file=last_article.source_file,
                        line_no=last_article.line_no,
                        parse_status=last_article.parse_status,
                    )
                else:
                    article_id = stable_id(str(file_path), str(line_no), leading_text.strip())
                    articles.append(
                        LawArticle(
                            article_id=article_id,
                            law_name=law_from_file,
                            article_number="",
                            body=leading_text.strip(),
                            raw_text=leading_text.strip(),
                            source_file=str(file_path),
                            line_no=line_no,
                            parse_status="unmatched",
                        )
                    )

            for segment_index, (law_name, article_number, body, segment_text, parse_status) in enumerate(segments):
                article_id = stable_id(str(file_path), str(line_no), str(segment_index), segment_text)
                articles.append(
                    LawArticle(
                        article_id=article_id,
                        law_name=law_name,
                        article_number=article_number,
                        body=body,
                        raw_text=segment_text,
                        source_file=str(file_path),
                        line_no=line_no,
                        parse_status=parse_status,
                    )
                )
        else:
            if articles:
                last_article = articles[-1]
                new_body = last_article.body + f"\n{line}"
                new_raw_text = last_article.raw_text + f"\n{line}"
                articles[-1] = LawArticle(
                    article_id=last_article.article_id,
                    law_name=last_article.law_name,
                    article_number=last_article.article_number,
                    body=new_body,
                    raw_text=new_raw_text,
                    source_file=last_article.source_file,
                    line_no=last_article.line_no,
                    parse_status=last_article.parse_status,
                )
            else:
                article_id = stable_id(str(file_path), str(line_no), line)
                articles.append(
                    LawArticle(
                        article_id=article_id,
                        law_name=law_from_file,
                        article_number="",
                        body=line,
                        raw_text=line,
                        source_file=str(file_path),
                        line_no=line_no,
                        parse_status="unmatched",
                    )
                )

    return articles


def load_articles(dataset_dir: str | Path, *, deduplicate: bool = True) -> list[LawArticle]:
    """Load all articles. With deduplicate=True, drop exact duplicates that share
    the same law name, article number and normalized text (e.g. the same provision
    appearing in both the law file and a related quotation file). Cross-law
    quotations keep different law names and are preserved."""
    articles: list[LawArticle] = []
    seen: set[tuple[str, str, str]] = set()
    for file_path in discover_law_files(dataset_dir):
        for article in parse_law_file(file_path):
            if deduplicate and article.article_number:
                key = (article.law_name, article.article_number, normalize_text(article.raw_text))
                if key in seen:
                    continue
                seen.add(key)
            articles.append(article)
    return articles


def stable_id(*parts: str) -> str:
    digest = hashlib.sha1("::".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


def profile_dataset(dataset_dir: str | Path, readme_path: str | Path | None = None) -> dict:
    files = discover_law_files(dataset_dir)
    raw_count = len(load_articles(dataset_dir, deduplicate=False))
    articles = load_articles(dataset_dir)
    char_lengths = [len(article.raw_text) for article in articles]
    normalized_texts = [normalize_text(article.raw_text) for article in articles]
    duplicates = sum(count - 1 for count in Counter(normalized_texts).values() if count > 1)
    parse_counts = Counter(article.parse_status for article in articles)
    file_article_counts = Counter(Path(article.source_file).name for article in articles)

    profile = {
        "dataset_dir": str(Path(dataset_dir).resolve()),
        "readme_path": str(Path(readme_path).resolve()) if readme_path else "",
        "readme_summary": read_readme_summary(readme_path),
        "file_count": len(files),
        "article_count": len(articles),
        "exact_duplicate_removed": raw_count - len(articles),
        "duplicate_article_count": duplicates,
        "parse_counts": dict(parse_counts),
        "parse_rate": round((parse_counts["with_law"] + parse_counts["from_filename"]) / max(len(articles), 1), 4),
        "char_length": summarize_lengths(char_lengths),
        "top_files_by_articles": [
            {"file": file_name, "articles": count}
            for file_name, count in file_article_counts.most_common(10)
        ],
        "format_anomalies": collect_anomalies(articles, limit=20),
    }
    profile["chunk_candidates"] = suggest_chunk_candidates(profile)
    return profile


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text.strip())


def summarize_lengths(lengths: Iterable[int]) -> dict:
    values = sorted(lengths)
    if not values:
        return {}
    return {
        "min": values[0],
        "p50": percentile(values, 0.50),
        "p75": percentile(values, 0.75),
        "p90": percentile(values, 0.90),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": values[-1],
        "avg": round(statistics.mean(values), 2),
    }


def percentile(sorted_values: list[int], ratio: float) -> int:
    if not sorted_values:
        return 0
    index = min(int(len(sorted_values) * ratio), len(sorted_values) - 1)
    return sorted_values[index]


def read_readme_summary(readme_path: str | Path | None) -> str:
    if not readme_path:
        return ""
    path = Path(readme_path)
    if not path.exists():
        return ""
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return " ".join(lines[:6])


def collect_anomalies(articles: list[LawArticle], limit: int = 20) -> list[dict]:
    anomalies = [
        {
            "source_file": Path(article.source_file).name,
            "line_no": article.line_no,
            "parse_status": article.parse_status,
            "text": article.raw_text[:180],
        }
        for article in articles
        if article.parse_status == "unmatched"
    ]
    return anomalies[:limit]


def suggest_chunk_candidates(profile: dict) -> list[dict]:
    length = profile.get("char_length", {})
    p95 = int(length.get("p95", 0))
    p99 = int(length.get("p99", 0))
    max_len = int(length.get("max", 0))
    parse_rate = float(profile.get("parse_rate", 0))

    candidates = [
        {
            "name": "article",
            "reason": "README says one article per line, and the observed parse rate is high.",
            "status": "baseline",
        },
        {
            "name": "neighbor",
            "reason": "Legal questions often depend on adjacent provisions, definitions, or exceptions.",
            "status": "experiment",
        },
    ]
    if max_len > max(600, p99):
        candidates.append(
            {
                "name": "long_split",
                "reason": "A small number of long provisions may dilute retrieval focus.",
                "status": "experiment",
                "suggested_max_chars": max(360, min(520, p99 or p95 or 450)),
            }
        )
    else:
        candidates.append(
            {
                "name": "long_split",
                "reason": "Kept as a stress test for unusually long provisions.",
                "status": "optional",
                "suggested_max_chars": 450,
            }
        )
    candidates.append(
        {
            "name": "fixed_chars",
            "reason": "Required as a fixed-size comparison baseline, not as the default decision.",
            "status": "comparison",
            "suggested_size": max(400, min(700, p95 * 2 if p95 else 500)),
        }
    )
    if parse_rate < 0.9:
        candidates.insert(
            0,
            {
                "name": "format_cleanup",
                "reason": "Parse rate is below 90%, so metadata cleanup should precede chunk experiments.",
                "status": "required",
            },
        )
    return candidates


def write_profile_outputs(profile: dict, output_dir: str | Path, report_dir: str | Path) -> tuple[Path, Path]:
    profile_path = Path(output_dir) / "data_profile.json"
    report_path = Path(report_dir) / "data_profile.md"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(render_profile_markdown(profile), encoding="utf-8")
    return profile_path, report_path


def render_profile_markdown(profile: dict) -> str:
    length = profile.get("char_length", {})
    parse_counts = profile.get("parse_counts", {})
    lines = [
        "# 数据画像报告",
        "",
        "## 数据来源",
        f"- 数据目录: `{profile.get('dataset_dir', '')}`",
        f"- README 摘要: {profile.get('readme_summary', '')}",
        "",
        "## 总体统计",
        f"- 文件数: {profile.get('file_count', 0)}",
        f"- 条文记录数: {profile.get('article_count', 0)}",
        f"- 去重移除的完全重复条文: {profile.get('exact_duplicate_removed', 0)}",
        f"- 剩余跨法律重复文本: {profile.get('duplicate_article_count', 0)}",
        f"- 可解析率: {profile.get('parse_rate', 0)}",
        f"- 解析状态: `{json.dumps(parse_counts, ensure_ascii=False)}`",
        "",
        "## 条文长度",
        f"- 平均长度: {length.get('avg', 0)} 字符",
        f"- P50/P75/P90/P95/P99: {length.get('p50', 0)} / {length.get('p75', 0)} / {length.get('p90', 0)} / {length.get('p95', 0)} / {length.get('p99', 0)}",
        f"- 最短/最长: {length.get('min', 0)} / {length.get('max', 0)}",
        "",
        "## Chunk 候选",
    ]
    for candidate in profile.get("chunk_candidates", []):
        lines.append(f"- `{candidate['name']}` [{candidate['status']}]: {candidate['reason']}")
    lines.extend(["", "## 条文数最多的文件"])
    for item in profile.get("top_files_by_articles", []):
        lines.append(f"- {item['file']}: {item['articles']} 条")
    lines.extend(["", "## 格式异常样例"])
    anomalies = profile.get("format_anomalies", [])
    if not anomalies:
        lines.append("- 未发现明显异常。")
    for item in anomalies:
        lines.append(f"- {item['source_file']}:{item['line_no']} `{item['parse_status']}` {item['text']}")
    lines.append("")
    return "\n".join(lines)

