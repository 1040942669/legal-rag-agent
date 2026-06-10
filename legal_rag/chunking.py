from __future__ import annotations

import json
import re
from pathlib import Path

from .data import stable_id
from .models import Chunk, LawArticle


def build_chunks(
    articles: list[LawArticle],
    strategy: str,
    *,
    neighbor_window: int = 3,
    neighbor_stride: int | None = None,
    long_split_max_chars: int = 450,
    long_split_overlap_chars: int = 60,
    fixed_chars_size: int = 500,
    fixed_chars_overlap: int = 80,
) -> list[Chunk]:
    if strategy == "article":
        return article_chunks(articles)
    if strategy == "neighbor":
        return neighbor_chunks(articles, window=neighbor_window, stride=neighbor_stride)
    if strategy == "long_split":
        return long_split_chunks(
            articles,
            max_chars=long_split_max_chars,
            overlap_chars=long_split_overlap_chars,
        )
    if strategy == "fixed_chars":
        return fixed_char_chunks(articles, size=fixed_chars_size, overlap=fixed_chars_overlap)
    raise ValueError(f"Unknown chunk strategy: {strategy}")


def article_chunks(articles: list[LawArticle]) -> list[Chunk]:
    return [chunk_from_articles([article], "article") for article in articles]


def neighbor_chunks(
    articles: list[LawArticle],
    window: int = 3,
    stride: int | None = None,
) -> list[Chunk]:
    if window <= 0:
        raise ValueError("neighbor window must be positive")
    if stride is None:
        stride = window
    if stride <= 0:
        raise ValueError("neighbor stride must be positive")

    grouped: dict[str, list[LawArticle]] = {}
    for article in articles:
        grouped.setdefault(article.source_file, []).append(article)

    chunks: list[Chunk] = []
    for source_file in sorted(grouped):
        items = sorted(grouped[source_file], key=lambda item: item.line_no)
        for start in range(0, len(items), stride):
            window_items = items[start : start + window]
            if not window_items:
                continue
            chunks.append(
                chunk_from_articles(
                    window_items,
                    "neighbor",
                    extra_metadata={
                        "neighbor_window": window,
                        "neighbor_stride": stride,
                        "window_start": start,
                        "window_end": start + len(window_items) - 1,
                    },
                )
            )
    return chunks


def long_split_chunks(
    articles: list[LawArticle],
    *,
    max_chars: int = 450,
    overlap_chars: int = 60,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    for article in articles:
        if len(article.raw_text) <= max_chars:
            chunks.append(chunk_from_articles([article], "long_split"))
            continue
        parts = split_long_text(article.raw_text, max_chars=max_chars, overlap_chars=overlap_chars)
        for part_index, part in enumerate(parts, start=1):
            chunk_id = stable_id(article.article_id, "long_split", str(part_index), part)
            chunks.append(
                Chunk(
                    chunk_id=chunk_id,
                    text=part,
                    law_names=[article.law_name],
                    article_numbers=[article.article_number],
                    source_files=[article.source_file],
                    line_nos=[article.line_no],
                    strategy="long_split",
                    metadata={
                        "part_index": part_index,
                        "part_count": len(parts),
                        "article_id": article.article_id,
                    },
                )
            )
    return chunks


def fixed_char_chunks(
    articles: list[LawArticle],
    *,
    size: int = 500,
    overlap: int = 80,
) -> list[Chunk]:
    if size <= 0:
        raise ValueError("fixed chunk size must be positive")
    if overlap >= size:
        raise ValueError("fixed chunk overlap must be smaller than size")

    grouped: dict[str, list[LawArticle]] = {}
    for article in articles:
        grouped.setdefault(article.source_file, []).append(article)

    chunks: list[Chunk] = []
    for source_file in sorted(grouped):
        items = sorted(grouped[source_file], key=lambda item: item.line_no)
        text = "\n".join(item.raw_text for item in items)
        cursor = 0
        part_index = 1
        while cursor < len(text):
            part = text[cursor : cursor + size].strip()
            if part:
                related = find_related_articles(items, part)
                chunks.append(
                    Chunk(
                        chunk_id=stable_id(source_file, "fixed_chars", str(part_index), part),
                        text=part,
                        law_names=unique([item.law_name for item in related] or [Path(source_file).stem]),
                        article_numbers=unique([item.article_number for item in related if item.article_number]),
                        source_files=[source_file],
                        line_nos=[item.line_no for item in related],
                        strategy="fixed_chars",
                        metadata={"part_index": part_index, "size": size, "overlap": overlap},
                    )
                )
            cursor += size - overlap
            part_index += 1
    return chunks


def split_long_text(text: str, *, max_chars: int, overlap_chars: int) -> list[str]:
    sentences = [part for part in re.split(r"(?<=[。；;])", text) if part]
    if not sentences:
        sentences = [text]

    parts: list[str] = []
    current = ""
    for sentence in sentences:
        if len(current) + len(sentence) <= max_chars:
            current += sentence
            continue
        if current:
            parts.append(current.strip())
        current = sentence
        while len(current) > max_chars:
            parts.append(current[:max_chars].strip())
            start = max(0, max_chars - overlap_chars)
            current = current[start:]
    if current:
        parts.append(current.strip())
    return parts


def find_related_articles(articles: list[LawArticle], chunk_text: str) -> list[LawArticle]:
    related: list[LawArticle] = []
    for article in articles:
        if article.article_number and article.article_number in chunk_text:
            related.append(article)
        elif article.raw_text[:30] in chunk_text:
            related.append(article)
    return related[:10]


def chunk_from_articles(
    articles: list[LawArticle],
    strategy: str,
    *,
    extra_metadata: dict | None = None,
) -> Chunk:
    if not articles:
        raise ValueError("Cannot create a chunk from zero articles")
    text = "\n".join(article.raw_text for article in articles)
    metadata = {
        "article_ids": [article.article_id for article in articles],
        "article_count": len(articles),
        "char_length": len(text),
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    return Chunk(
        chunk_id=stable_id(strategy, *(article.article_id for article in articles)),
        text=text,
        law_names=unique(article.law_name for article in articles),
        article_numbers=unique(article.article_number for article in articles if article.article_number),
        source_files=unique(article.source_file for article in articles),
        line_nos=[article.line_no for article in articles],
        strategy=strategy,
        metadata=metadata,
    )


def unique(values) -> list:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def save_chunks(chunks: list[Chunk], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for chunk in chunks:
            handle.write(json.dumps(chunk_to_dict(chunk), ensure_ascii=False) + "\n")
    return path


def load_chunks(path: str | Path) -> list[Chunk]:
    chunks: list[Chunk] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            item = json.loads(line)
            chunks.append(
                Chunk(
                    chunk_id=item["chunk_id"],
                    text=item["text"],
                    law_names=item["law_names"],
                    article_numbers=item["article_numbers"],
                    source_files=item["source_files"],
                    line_nos=item["line_nos"],
                    strategy=item["strategy"],
                    metadata=item.get("metadata", {}),
                )
            )
    return chunks


def chunk_to_dict(chunk: Chunk) -> dict:
    return {
        "chunk_id": chunk.chunk_id,
        "text": chunk.text,
        "law_names": chunk.law_names,
        "article_numbers": chunk.article_numbers,
        "source_files": chunk.source_files,
        "line_nos": chunk.line_nos,
        "strategy": chunk.strategy,
        "metadata": chunk.metadata,
    }
