from __future__ import annotations

import json
import time
from pathlib import Path

from .chunking import build_chunks, save_chunks
from .data import load_articles
from .manifest import new_run_id, summarize_path, write_artifact_manifest


def build_index(
    *,
    dataset_dir: str | Path,
    profile_path: str | Path,
    output_root: str | Path,
    strategy: str,
    chunking_config: dict,
    run_id: str | None = None,
) -> dict:
    profile = Path(profile_path)
    if not profile.exists():
        raise FileNotFoundError(
            f"Data profile is required before building an index. Run `profile-data` first: {profile}"
        )

    started = time.perf_counter()
    articles = load_articles(dataset_dir)
    chunks = build_chunks(
        articles,
        strategy,
        neighbor_window=int(chunking_config.get("neighbor_window", 3)),
        long_split_max_chars=int(chunking_config.get("long_split_max_chars", 450)),
        long_split_overlap_chars=int(chunking_config.get("long_split_overlap_chars", 60)),
        fixed_chars_size=int(chunking_config.get("fixed_chars_size", 500)),
        fixed_chars_overlap=int(chunking_config.get("fixed_chars_overlap", 80)),
    )

    index_dir = Path(output_root) / strategy
    chunks_path = save_chunks(chunks, index_dir / "chunks.jsonl")
    metadata_path = index_dir / "metadata.json"
    manifest_path = index_dir / "manifest.json"
    artifact_run_id = run_id or new_run_id(f"index_{strategy}")
    metadata = {
        "run_id": artifact_run_id,
        "strategy": strategy,
        "article_count": len(articles),
        "chunk_count": len(chunks),
        "chunks_path": str(chunks_path),
        "manifest_path": str(manifest_path),
        "build_seconds": round(time.perf_counter() - started, 3),
    }
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    manifest = write_artifact_manifest(
        manifest_path,
        artifact_type="index",
        run_id=artifact_run_id,
        inputs={
            "dataset_dir": summarize_path(dataset_dir),
            "profile_path": summarize_path(profile_path),
        },
        config={
            "strategy": strategy,
            "chunking": chunking_config,
        },
        outputs={
            "chunks_path": str(chunks_path.resolve()),
            "metadata_path": str(metadata_path.resolve()),
        },
        metrics={
            "article_count": len(articles),
            "chunk_count": len(chunks),
            "build_seconds": metadata["build_seconds"],
        },
    )
    return metadata


def resolve_chunks_path(index_dir: str | Path) -> Path:
    path = Path(index_dir)
    if path.is_file():
        return path
    chunks_path = path / "chunks.jsonl"
    if not chunks_path.exists():
        raise FileNotFoundError(f"Index chunks not found: {chunks_path}")
    return chunks_path
