from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .chunking import load_chunks
from .env import load_dotenv
from .manifest import new_run_id, summarize_path, write_artifact_manifest
from .models import Chunk


@dataclass(frozen=True)
class EmbeddingModelConfig:
    key: str
    provider: str
    model_name: str
    role: str
    normalize: bool = True
    trust_remote_code: bool = False
    api_base_url: str = ""
    api_key_env: str = ""
    dimensions: int | None = None
    max_retries: int = 3
    query_prefix: str = ""
    document_prefix: str = ""
    # Prepend law names and article numbers to the chunk text before embedding.
    # LlamaIndex does this by default and it markedly improves legal retrieval.
    embed_with_metadata: bool = False


@dataclass(frozen=True)
class EmbeddingCache:
    cache_dir: Path
    metadata: dict[str, Any]
    chunk_ids: list[str]
    vectors: Any


class SentenceTransformerEncoder:
    def __init__(self, model_config: EmbeddingModelConfig, *, device: str = "auto") -> None:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Embedding generation requires sentence-transformers. Run `uv sync` first, "
                "or build embeddings on a GPU server with the same project code."
            ) from exc

        kwargs: dict[str, Any] = {"trust_remote_code": model_config.trust_remote_code}
        if device and device != "auto":
            kwargs["device"] = device
        self.model_config = model_config
        self.model = SentenceTransformer(model_config.model_name, **kwargs)

    def encode_documents(self, texts: list[str], *, batch_size: int) -> Any:
        return self.model.encode(
            [self.model_config.document_prefix + text for text in texts],
            batch_size=batch_size,
            normalize_embeddings=self.model_config.normalize,
            show_progress_bar=True,
        )

    def encode_query(self, text: str) -> Any:
        return self.model.encode(
            [self.model_config.query_prefix + text],
            normalize_embeddings=self.model_config.normalize,
            show_progress_bar=False,
        )[0]


class SiliconFlowEmbeddingEncoder:
    def __init__(self, model_config: EmbeddingModelConfig) -> None:
        try:
            from openai import OpenAI  # type: ignore
        except ModuleNotFoundError as exc:
            raise RuntimeError("SiliconFlow embedding requires openai. Run `uv sync` first.") from exc

        api_key_env = model_config.api_key_env or "SILICONFLOW_API_KEY"
        load_dotenv()
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise RuntimeError(
                f"Missing SiliconFlow API key. Set `{api_key_env}` in your environment or project .env file."
            )
        self.model_config = model_config
        self.client = OpenAI(
            api_key=api_key,
            base_url=model_config.api_base_url or "https://api.siliconflow.cn/v1",
            max_retries=model_config.max_retries,
        )

    def encode_documents(self, texts: list[str], *, batch_size: int) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            response = self._create_embeddings(
                [self.model_config.document_prefix + text for text in batch]
            )
            vectors.extend([item.embedding for item in response.data])
            print(f"Embedded {min(start + len(batch), len(texts))}/{len(texts)}", flush=True)
        return vectors

    def encode_query(self, text: str) -> list[float]:
        response = self._create_embeddings([self.model_config.query_prefix + text])
        return list(response.data[0].embedding)

    def _create_embeddings(self, texts: list[str]):
        kwargs: dict[str, Any] = {
            "model": self.model_config.model_name,
            "input": texts,
        }
        if self.model_config.dimensions:
            kwargs["dimensions"] = self.model_config.dimensions
        return self.client.embeddings.create(**kwargs)


def resolve_embedding_model(config: dict, embedding_key: str | None = None) -> EmbeddingModelConfig:
    embedding_config = config.get("embedding", {})
    key = embedding_key or embedding_config.get("default") or "bge_large_zh"
    model_map = embedding_config.get("models", {})
    if key not in model_map:
        available = ", ".join(sorted(model_map)) or "<none>"
        raise ValueError(f"Unknown embedding model key `{key}`. Available: {available}")
    item = model_map[key]
    return EmbeddingModelConfig(
        key=key,
        provider=item.get("provider", "sentence_transformers"),
        model_name=item["model_name"],
        role=item.get("role", ""),
        normalize=bool(item.get("normalize", True)),
        trust_remote_code=bool(item.get("trust_remote_code", False)),
        api_base_url=item.get("api_base_url", ""),
        api_key_env=item.get("api_key_env", ""),
        dimensions=item.get("dimensions"),
        max_retries=int(item.get("max_retries", 3)),
        query_prefix=item.get("query_prefix", ""),
        document_prefix=item.get("document_prefix", ""),
        embed_with_metadata=bool(item.get("embed_with_metadata", False)),
    )


def build_embedding_cache(
    *,
    chunks_path: str | Path,
    output_root: str | Path,
    chunk_strategy: str,
    model_config: EmbeddingModelConfig,
    batch_size: int = 16,
    device: str = "auto",
    run_id: str | None = None,
) -> dict[str, Any]:
    chunks = load_chunks(chunks_path)
    encoder = build_encoder(model_config, device=device)
    started = time.perf_counter()
    vectors = encoder.encode_documents(
        [embedding_document_text(chunk, model_config) for chunk in chunks],
        batch_size=batch_size,
    )

    try:
        import numpy as np  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError("Embedding cache requires numpy. Run `uv sync` first.") from exc

    matrix = np.asarray(vectors, dtype="float32")
    cache_dir = embedding_cache_dir(output_root, chunk_strategy, model_config.key)
    cache_dir.mkdir(parents=True, exist_ok=True)
    vectors_path = cache_dir / "vectors.npy"
    chunk_ids_path = cache_dir / "chunk_ids.json"
    metadata_path = cache_dir / "metadata.json"
    manifest_path = cache_dir / "manifest.json"
    artifact_run_id = run_id or new_run_id(f"embeddings_{chunk_strategy}_{model_config.key}")

    np.save(vectors_path, matrix)
    chunk_ids = [chunk.chunk_id for chunk in chunks]
    chunk_ids_path.write_text(json.dumps(chunk_ids, ensure_ascii=False, indent=2), encoding="utf-8")
    metadata = {
        "run_id": artifact_run_id,
        "embedding_key": model_config.key,
        "provider": model_config.provider,
        "api_base_url": model_config.api_base_url,
        "model_name": model_config.model_name,
        "role": model_config.role,
        "chunk_strategy": chunk_strategy,
        "chunk_count": len(chunks),
        "dimension": int(matrix.shape[1]) if len(matrix.shape) == 2 else 0,
        "vectors_path": str(vectors_path),
        "chunk_ids_path": str(chunk_ids_path),
        "chunks_path": str(chunks_path),
        "normalize": model_config.normalize,
        "embed_with_metadata": model_config.embed_with_metadata,
        "manifest_path": str(manifest_path),
        "build_seconds": round(time.perf_counter() - started, 3),
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = write_artifact_manifest(
        manifest_path,
        artifact_type="embedding_cache",
        run_id=artifact_run_id,
        inputs={
            "chunks_path": summarize_path(chunks_path),
        },
        config={
            "embedding_key": model_config.key,
            "provider": model_config.provider,
            "model_name": model_config.model_name,
            "normalize": model_config.normalize,
            "batch_size": batch_size,
            "device": device,
        },
        outputs={
            "vectors_path": str(vectors_path.resolve()),
            "chunk_ids_path": str(chunk_ids_path.resolve()),
            "metadata_path": str(metadata_path.resolve()),
        },
        metrics={
            "chunk_count": len(chunks),
            "dimension": metadata["dimension"],
            "build_seconds": metadata["build_seconds"],
        },
    )
    return metadata


def embedding_document_text(chunk: Chunk, model_config: EmbeddingModelConfig) -> str:
    if not model_config.embed_with_metadata:
        return chunk.text
    law = "、".join(chunk.law_names)
    article = "、".join(chunk.article_numbers)
    header = " ".join(part for part in (law, article) if part)
    return f"{header}\n{chunk.text}" if header else chunk.text


def build_encoder(model_config: EmbeddingModelConfig, *, device: str = "auto"):
    if model_config.provider == "sentence_transformers":
        return SentenceTransformerEncoder(model_config, device=device)
    if model_config.provider == "siliconflow":
        return SiliconFlowEmbeddingEncoder(model_config)
    raise ValueError(f"Unsupported embedding provider: {model_config.provider}")


def load_embedding_cache(cache_dir: str | Path) -> EmbeddingCache:
    try:
        import numpy as np  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError("Loading embedding cache requires numpy. Run `uv sync` first.") from exc

    root = Path(cache_dir)
    metadata = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    chunk_ids = json.loads((root / "chunk_ids.json").read_text(encoding="utf-8"))
    vectors = np.load(root / "vectors.npy")
    return EmbeddingCache(cache_dir=root, metadata=metadata, chunk_ids=chunk_ids, vectors=vectors)


def embedding_cache_dir(output_root: str | Path, chunk_strategy: str, embedding_key: str) -> Path:
    return Path(output_root) / chunk_strategy / embedding_key


def validate_cache_matches_chunks(cache: EmbeddingCache, chunks: list[Chunk]) -> None:
    chunk_ids = [chunk.chunk_id for chunk in chunks]
    if cache.chunk_ids != chunk_ids:
        raise ValueError(
            "Embedding cache does not match the loaded chunks. Rebuild embeddings for this chunk strategy."
        )
