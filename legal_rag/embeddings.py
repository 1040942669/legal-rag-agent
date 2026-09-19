from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .chunking import load_chunks
from .env import load_dotenv, require_live_model_calls_allowed
from .manifest import new_run_id, summarize_path, write_artifact_manifest
from .models import Chunk

EMBEDDING_CACHE_SCHEMA_VERSION = 2


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


@dataclass(frozen=True)
class CacheHealthIssue:
    severity: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }


@dataclass(frozen=True)
class CacheHealthReport:
    cache_dir: Path
    issues: list[CacheHealthIssue]
    metadata: dict[str, Any]

    @property
    def valid(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    @property
    def status(self) -> str:
        if not self.valid:
            return "invalid"
        if self.issues:
            return "warning"
        return "healthy"

    def to_dict(self) -> dict[str, Any]:
        return {
            "cache_dir": str(self.cache_dir.resolve()),
            "status": self.status,
            "valid": self.valid,
            "error_count": sum(issue.severity == "error" for issue in self.issues),
            "warning_count": sum(issue.severity == "warning" for issue in self.issues),
            "issues": [issue.to_dict() for issue in self.issues],
            "metadata": self.metadata,
        }


class SentenceTransformerEncoder:
    def __init__(self, model_config: EmbeddingModelConfig, *, device: str = "auto") -> None:
        require_live_model_calls_allowed("sentence-transformer embedding")
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Could not import sentence-transformers "
                f"(missing module: {exc.name or 'unknown'}). Run `uv sync` and verify the "
                "Torch/Transformers environment, or build embeddings on a GPU server."
            ) from exc

        kwargs: dict[str, Any] = {"trust_remote_code": model_config.trust_remote_code}
        if device and device != "auto":
            kwargs["device"] = device
        self.model_config = model_config
        self.model = SentenceTransformer(model_config.model_name, **kwargs)

    def encode_documents(self, texts: list[str], *, batch_size: int) -> Any:
        require_live_model_calls_allowed("sentence-transformer embedding")
        return self.model.encode(
            [self.model_config.document_prefix + text for text in texts],
            batch_size=batch_size,
            normalize_embeddings=self.model_config.normalize,
            show_progress_bar=True,
        )

    def encode_query(self, text: str) -> Any:
        require_live_model_calls_allowed("sentence-transformer embedding")
        return self.model.encode(
            [self.model_config.query_prefix + text],
            normalize_embeddings=self.model_config.normalize,
            show_progress_bar=False,
        )[0]


class SiliconFlowEmbeddingEncoder:
    def __init__(self, model_config: EmbeddingModelConfig) -> None:
        require_live_model_calls_allowed("SiliconFlow embedding")
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
        require_live_model_calls_allowed("SiliconFlow embedding")
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
        require_live_model_calls_allowed("SiliconFlow embedding")
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
        "schema_version": EMBEDDING_CACHE_SCHEMA_VERSION,
        "run_id": artifact_run_id,
        "embedding_key": model_config.key,
        "provider": model_config.provider,
        "api_base_url": model_config.api_base_url,
        "model_name": model_config.model_name,
        "role": model_config.role,
        "chunk_strategy": chunk_strategy,
        "chunk_count": len(chunks),
        "vector_count": int(matrix.shape[0]) if len(matrix.shape) == 2 else 0,
        "dimension": int(matrix.shape[1]) if len(matrix.shape) == 2 else 0,
        "dtype": str(matrix.dtype),
        "chunk_fingerprint": chunk_corpus_fingerprint(chunks),
        "embedding_contract_fingerprint": embedding_contract_fingerprint(model_config),
        "vectors_path": str(vectors_path),
        "chunk_ids_path": str(chunk_ids_path),
        "chunks_path": str(chunks_path),
        "normalize": model_config.normalize,
        "trust_remote_code": model_config.trust_remote_code,
        "query_prefix": model_config.query_prefix,
        "document_prefix": model_config.document_prefix,
        "embed_with_metadata": model_config.embed_with_metadata,
        "manifest_path": str(manifest_path),
        "build_seconds": round(time.perf_counter() - started, 3),
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    write_artifact_manifest(
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
            "trust_remote_code": model_config.trust_remote_code,
            "query_prefix": model_config.query_prefix,
            "document_prefix": model_config.document_prefix,
            "embed_with_metadata": model_config.embed_with_metadata,
            "embedding_contract_fingerprint": metadata["embedding_contract_fingerprint"],
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
            "chunk_fingerprint": metadata["chunk_fingerprint"],
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
    vectors = np.load(root / "vectors.npy", allow_pickle=False)
    return EmbeddingCache(cache_dir=root, metadata=metadata, chunk_ids=chunk_ids, vectors=vectors)


def embedding_cache_dir(output_root: str | Path, chunk_strategy: str, embedding_key: str) -> Path:
    return Path(output_root) / chunk_strategy / embedding_key


def chunk_corpus_fingerprint(chunks: list[Chunk]) -> str:
    """Hash the ordered, embedding-relevant chunk corpus."""
    digest = hashlib.sha256()
    for chunk in chunks:
        payload = {
            "chunk_id": chunk.chunk_id,
            "text": chunk.text,
            "law_names": chunk.law_names,
            "article_numbers": chunk.article_numbers,
            "strategy": chunk.strategy,
            "metadata": chunk.metadata,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def embedding_contract_fingerprint(model_config: EmbeddingModelConfig) -> str:
    payload = {
        "embedding_key": model_config.key,
        "provider": model_config.provider,
        "model_name": model_config.model_name,
        "normalize": model_config.normalize,
        "trust_remote_code": model_config.trust_remote_code,
        "dimensions": model_config.dimensions,
        "query_prefix": model_config.query_prefix,
        "document_prefix": model_config.document_prefix,
        "embed_with_metadata": model_config.embed_with_metadata,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def inspect_embedding_cache(
    cache_dir: str | Path,
    *,
    chunks: list[Chunk] | None = None,
    model_config: EmbeddingModelConfig | None = None,
    expected_chunk_strategy: str | None = None,
    strict_contract: bool = True,
) -> CacheHealthReport:
    root = Path(cache_dir)
    issues: list[CacheHealthIssue] = []
    required = ("metadata.json", "chunk_ids.json", "vectors.npy")
    for filename in required:
        if not (root / filename).is_file():
            issues.append(
                CacheHealthIssue("error", "missing_file", f"Missing required cache file: {filename}")
            )
    if issues:
        return CacheHealthReport(cache_dir=root, issues=issues, metadata={})

    try:
        cache = load_embedding_cache(root)
    except Exception as exc:  # noqa: BLE001 - corruption type depends on JSON/NumPy/filesystem.
        issues.append(CacheHealthIssue("error", "cache_unreadable", str(exc)))
        return CacheHealthReport(cache_dir=root, issues=issues, metadata={})

    issues.extend(
        embedding_cache_issues(
            cache,
            chunks=chunks,
            model_config=model_config,
            expected_chunk_strategy=expected_chunk_strategy,
            strict_contract=strict_contract,
        )
    )
    return CacheHealthReport(cache_dir=root, issues=issues, metadata=cache.metadata)


def embedding_cache_issues(
    cache: EmbeddingCache,
    *,
    chunks: list[Chunk] | None = None,
    model_config: EmbeddingModelConfig | None = None,
    expected_chunk_strategy: str | None = None,
    strict_contract: bool = True,
) -> list[CacheHealthIssue]:
    issues: list[CacheHealthIssue] = []
    metadata = cache.metadata
    vectors = cache.vectors
    schema_version = metadata.get("schema_version")
    if schema_version != EMBEDDING_CACHE_SCHEMA_VERSION:
        severity = "error" if strict_contract else "warning"
        issues.append(
            CacheHealthIssue(
                severity,
                "schema_version",
                f"Expected cache schema {EMBEDDING_CACHE_SCHEMA_VERSION}, got {schema_version!r}. Rebuild the cache.",
            )
        )

    shape = getattr(vectors, "shape", ())
    if len(shape) != 2:
        issues.append(CacheHealthIssue("error", "vector_shape", f"Expected a 2D vector matrix, got {shape!r}."))
    else:
        rows, dimension = int(shape[0]), int(shape[1])
        if rows != len(cache.chunk_ids):
            issues.append(
                CacheHealthIssue(
                    "error",
                    "vector_id_count",
                    f"Vector rows ({rows}) do not match chunk IDs ({len(cache.chunk_ids)}).",
                )
            )
        for key, actual in (("chunk_count", len(cache.chunk_ids)), ("vector_count", rows), ("dimension", dimension)):
            recorded = metadata.get(key)
            if recorded is not None and recorded != actual:
                issues.append(
                    CacheHealthIssue(
                        "error",
                        f"metadata_{key}",
                        f"metadata.{key}={recorded!r}, actual={actual!r}.",
                    )
                )
        recorded_dtype = metadata.get("dtype")
        actual_dtype = str(getattr(vectors, "dtype", "unknown"))
        if recorded_dtype is not None and recorded_dtype != actual_dtype:
            issues.append(
                CacheHealthIssue(
                    "error",
                    "metadata_dtype",
                    f"metadata.dtype={recorded_dtype!r}, actual={actual_dtype!r}.",
                )
            )
        try:
            import numpy as np  # type: ignore

            if not bool(np.isfinite(vectors).all()):
                issues.append(CacheHealthIssue("error", "non_finite_vectors", "Vector matrix contains NaN or infinity."))
            elif metadata.get("normalize") and rows:
                norms = np.linalg.norm(vectors, axis=1)
                max_deviation = float(np.max(np.abs(norms - 1.0)))
                if max_deviation > 0.02:
                    issues.append(
                        CacheHealthIssue(
                            "warning",
                            "normalization_drift",
                            f"Normalized cache has max L2 norm deviation {max_deviation:.4f}.",
                        )
                    )
        except (TypeError, ValueError):
            issues.append(CacheHealthIssue("error", "vector_validation", "Vector matrix could not be validated numerically."))

    if expected_chunk_strategy and metadata.get("chunk_strategy") != expected_chunk_strategy:
        issues.append(
            CacheHealthIssue(
                "error",
                "chunk_strategy",
                f"Expected chunk strategy {expected_chunk_strategy!r}, got {metadata.get('chunk_strategy')!r}.",
            )
        )

    if chunks is not None:
        expected_ids = [chunk.chunk_id for chunk in chunks]
        if cache.chunk_ids != expected_ids:
            issues.append(
                CacheHealthIssue(
                    "error",
                    "chunk_ids",
                    "Cached chunk IDs or ordering do not match the loaded index.",
                )
            )
        expected_fingerprint = chunk_corpus_fingerprint(chunks)
        recorded_fingerprint = metadata.get("chunk_fingerprint")
        if recorded_fingerprint is None:
            severity = "error" if strict_contract else "warning"
            issues.append(
                CacheHealthIssue(
                    severity,
                    "chunk_fingerprint_missing",
                    "Cache has no corpus fingerprint. Rebuild it to detect stale chunk text.",
                )
            )
        elif recorded_fingerprint != expected_fingerprint:
            issues.append(
                CacheHealthIssue(
                    "error",
                    "chunk_fingerprint",
                    "Cached corpus fingerprint does not match the loaded index.",
                )
            )

    if model_config is not None:
        expected_contract = {
            "embedding_key": model_config.key,
            "provider": model_config.provider,
            "model_name": model_config.model_name,
            "normalize": model_config.normalize,
            "trust_remote_code": model_config.trust_remote_code,
            "query_prefix": model_config.query_prefix,
            "document_prefix": model_config.document_prefix,
            "embed_with_metadata": model_config.embed_with_metadata,
            "embedding_contract_fingerprint": embedding_contract_fingerprint(model_config),
        }
        if model_config.dimensions is not None:
            expected_contract["dimension"] = model_config.dimensions
        for key, expected in expected_contract.items():
            if key not in metadata:
                severity = "error" if strict_contract else "warning"
                issues.append(
                    CacheHealthIssue(
                        severity,
                        "contract_field_missing",
                        f"Cache contract field {key!r} is missing. Rebuild the cache.",
                    )
                )
            elif metadata[key] != expected:
                issues.append(
                    CacheHealthIssue(
                        "error",
                        "contract_mismatch",
                        f"Cache contract {key!r} is {metadata[key]!r}; expected {expected!r}.",
                    )
                )
    return issues


def validate_cache_matches_chunks(
    cache: EmbeddingCache,
    chunks: list[Chunk],
    *,
    model_config: EmbeddingModelConfig | None = None,
) -> None:
    issues = embedding_cache_issues(
        cache,
        chunks=chunks,
        model_config=model_config,
        expected_chunk_strategy=chunks[0].strategy if chunks else None,
        strict_contract=True,
    )
    errors = [issue for issue in issues if issue.severity == "error"]
    if errors:
        details = "; ".join(f"{issue.code}: {issue.message}" for issue in errors[:6])
        raise ValueError(f"Embedding cache contract validation failed. {details}")
