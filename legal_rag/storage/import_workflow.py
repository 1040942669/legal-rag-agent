from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from ..data import load_articles
from ..embeddings import EmbeddingCache, EmbeddingModelConfig
from ..json_utils import (
    reject_duplicate_object_pairs,
    reject_non_finite_json_constant,
    validate_json_unicode,
)
from ..models import Chunk
from .contracts import (
    LawVersionSpec,
    StorageImportBundle,
    canonical_json,
    sha256_json,
    validate_storage_import_bundle,
)


IMPORT_REQUEST_SCHEMA_VERSION = 1
IMPORT_PLAN_SCHEMA_VERSION = 1
IMPORT_RECEIPT_SCHEMA_VERSION = 1
IMPORT_PLAN_KIND = "m3_storage_import_plan"
IMPORT_VALIDATION_KIND = "m3_storage_import_validation"
IMPORT_RECEIPT_KIND = "m3_storage_import_receipt"
_MAX_CONTROL_FILE_BYTES = 16 * 1024 * 1024
_ENVIRONMENT_VARIABLE = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_SENSITIVE_KEY_PARTS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "credential",
        "password",
        "private_key",
        "secret",
        "token",
    }
)


class StorageImportWorkflowError(ValueError):
    """A local import request, plan, or source artifact failed validation."""


@dataclass(frozen=True)
class PreparedStorageImport:
    plan: Mapping[str, Any]
    bundle: StorageImportBundle


def _strict_json_loads(value: str, *, label: str) -> Any:
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=reject_duplicate_object_pairs,
            parse_constant=reject_non_finite_json_constant,
        )
        validate_json_unicode(parsed)
        return parsed
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StorageImportWorkflowError(f"{label} is invalid strict JSON") from exc


def _read_control_json(path: Path, *, root: Path, label: str) -> tuple[Any, bytes]:
    resolved = _resolve_existing_path(path, root=root, label=label, kind="file")
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise StorageImportWorkflowError(f"{label} metadata is not readable") from exc
    if size > _MAX_CONTROL_FILE_BYTES:
        raise StorageImportWorkflowError(f"{label} exceeds the control-file limit")
    try:
        raw = resolved.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raise StorageImportWorkflowError(f"{label} must use UTF-8 without BOM")
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StorageImportWorkflowError(f"{label} is not valid UTF-8") from exc
    except OSError as exc:
        raise StorageImportWorkflowError(f"{label} metadata is not readable") from exc
    return _strict_json_loads(text, label=label), raw


def _exact_object(name: str, value: Any, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise StorageImportWorkflowError(f"{name} fields are invalid")
    return dict(value)


def _non_empty_string(name: str, value: Any, *, max_length: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise StorageImportWorkflowError(
            f"{name} must be a non-empty string without surrounding whitespace"
        )
    validate_json_unicode(value)
    if max_length is not None and len(value) > max_length:
        raise StorageImportWorkflowError(f"{name} exceeds maximum length {max_length}")
    return value


def _positive_integer(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise StorageImportWorkflowError(f"{name} must be a positive integer")
    return value


def _safe_relative_path(name: str, value: Any) -> str:
    rendered = _non_empty_string(name, value)
    if "\\" in rendered:
        raise StorageImportWorkflowError(f"{name} must use POSIX separators")
    path = PurePosixPath(rendered)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise StorageImportWorkflowError(
            f"{name} must be a safe source-root-relative path"
        )
    return path.as_posix()


def _resolve_existing_path(path: Path, *, root: Path, label: str, kind: str) -> Path:
    try:
        requested = path.absolute()
        relative = requested.relative_to(root)
    except ValueError as exc:
        raise StorageImportWorkflowError(f"{label} escapes source_root") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise StorageImportWorkflowError(f"{label} must not traverse a symlink")
    try:
        resolved = requested.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise StorageImportWorkflowError(f"{label} does not exist") from exc
    if not resolved.is_relative_to(root):
        raise StorageImportWorkflowError(f"{label} escapes source_root")
    if kind == "file" and not resolved.is_file():
        raise StorageImportWorkflowError(f"{label} must be a regular file")
    if kind == "directory" and not resolved.is_dir():
        raise StorageImportWorkflowError(f"{label} must be a directory")
    return resolved


def _source_root(value: str | Path | None, *, manifest_path: Path) -> Path:
    requested = Path(value) if value is not None else manifest_path.parent
    if requested.is_symlink():
        raise StorageImportWorkflowError("source_root must not be a symlink")
    try:
        resolved = requested.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise StorageImportWorkflowError("source_root does not exist") from exc
    if not resolved.is_dir():
        raise StorageImportWorkflowError("source_root must be a directory")
    return resolved


def _path_from_request(root: Path, relative: str, *, label: str, kind: str) -> Path:
    return _resolve_existing_path(
        root / PurePosixPath(relative), root=root, label=label, kind=kind
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise StorageImportWorkflowError("an input artifact became unreadable") from exc
    return digest.hexdigest()


def _dataset_identity(dataset_dir: Path, *, root: Path) -> tuple[int, str]:
    files = sorted(dataset_dir.glob("*.txt"), key=lambda item: item.name)
    if not files:
        raise StorageImportWorkflowError("dataset_dir contains no .txt source files")
    digest = hashlib.sha256()
    for path in files:
        resolved = _resolve_existing_path(
            path, root=root, label="dataset source file", kind="file"
        )
        relative = resolved.relative_to(root).as_posix().encode("utf-8")
        file_digest = bytes.fromhex(_sha256_file(resolved))
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(file_digest)
    return len(files), digest.hexdigest()


def _reject_sensitive_keys(value: Any, *, path: str = "source_manifest") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in _SENSITIVE_KEY_PARTS or any(
                normalized.endswith(f"_{part}") for part in _SENSITIVE_KEY_PARTS
            ):
                raise StorageImportWorkflowError(
                    f"{path} contains a forbidden sensitive field name"
                )
            _reject_sensitive_keys(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_sensitive_keys(item, path=f"{path}[{index}]")


def _parse_request(value: Any) -> dict[str, Any]:
    request = _exact_object(
        "import request",
        value,
        {
            "schema_version",
            "snapshot_id",
            "scope_id",
            "source_manifest",
            "inputs",
            "law_versions",
            "article_version_ids",
        },
    )
    if request["schema_version"] != IMPORT_REQUEST_SCHEMA_VERSION or isinstance(
        request["schema_version"], bool
    ):
        raise StorageImportWorkflowError("unsupported import request schema version")
    request["snapshot_id"] = _non_empty_string(
        "snapshot_id", request["snapshot_id"], max_length=255
    )
    request["scope_id"] = _non_empty_string(
        "scope_id", request["scope_id"], max_length=255
    )
    if (
        not isinstance(request["source_manifest"], dict)
        or not request["source_manifest"]
    ):
        raise StorageImportWorkflowError("source_manifest must be a non-empty object")
    _reject_sensitive_keys(request["source_manifest"])
    # Ensure the object can enter the canonical storage contract before any
    # potentially large local artifact is loaded.
    canonical_json(request["source_manifest"])

    inputs = _exact_object(
        "inputs",
        request["inputs"],
        {
            "dataset_dir",
            "chunks_path",
            "index_manifest_path",
            "embedding_cache_dir",
        },
    )
    request["inputs"] = {
        name: _safe_relative_path(f"inputs.{name}", inputs[name]) for name in inputs
    }

    raw_laws = request["law_versions"]
    if not isinstance(raw_laws, list) or not raw_laws:
        raise StorageImportWorkflowError("law_versions must be a non-empty list")
    laws: list[dict[str, Any]] = []
    for index, raw_law in enumerate(raw_laws):
        law = _exact_object(
            f"law_versions[{index}]",
            raw_law,
            {
                "law_id",
                "version_id",
                "title",
                "verification_status",
                "source_ref",
                "valid_from",
                "valid_to",
            },
        )
        law["source_ref"] = _safe_relative_path(
            f"law_versions[{index}].source_ref", law["source_ref"]
        )
        # LawVersionSpec owns the effective-date and status invariants.
        spec = LawVersionSpec(**law)
        laws.append(
            {
                **asdict(spec),
                "valid_from": spec.valid_from.isoformat() if spec.valid_from else None,
                "valid_to": spec.valid_to.isoformat() if spec.valid_to else None,
            }
        )
    request["law_versions"] = laws

    mappings = request["article_version_ids"]
    if mappings is not None:
        if not isinstance(mappings, dict):
            raise StorageImportWorkflowError(
                "article_version_ids must be an object or null"
            )
        normalized: dict[str, str] = {}
        for article_id, version_id in mappings.items():
            normalized[_non_empty_string("article_version_ids key", article_id)] = (
                _non_empty_string(
                    f"article_version_ids.{article_id}", version_id, max_length=255
                )
            )
        request["article_version_ids"] = normalized
    return request


def _load_chunks_strict(path: Path, *, root: Path) -> tuple[list[Chunk], bytes]:
    resolved = _resolve_existing_path(
        path, root=root, label="chunks artifact", kind="file"
    )
    try:
        raw = resolved.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raise StorageImportWorkflowError(
                "chunks artifact must use UTF-8 without BOM"
            )
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StorageImportWorkflowError("chunks artifact is not valid UTF-8") from exc
    except OSError as exc:
        raise StorageImportWorkflowError("chunks artifact is not readable") from exc

    chunks: list[Chunk] = []
    fields = {
        "chunk_id",
        "text",
        "law_names",
        "article_numbers",
        "source_files",
        "line_nos",
        "strategy",
        "metadata",
    }
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        item = _strict_json_loads(line, label=f"chunks line {line_number}")
        item = _exact_object(f"chunks line {line_number}", item, fields)
        chunk_id = _non_empty_string(
            f"chunks line {line_number}.chunk_id", item["chunk_id"], max_length=255
        )
        text_value = _non_empty_string(f"chunks line {line_number}.text", item["text"])
        strategy = _non_empty_string(
            f"chunks line {line_number}.strategy", item["strategy"], max_length=64
        )
        string_lists: dict[str, list[str]] = {}
        for field_name in ("law_names", "article_numbers", "source_files"):
            raw_list = item[field_name]
            if not isinstance(raw_list, list) or (
                field_name != "article_numbers" and not raw_list
            ):
                raise StorageImportWorkflowError(
                    f"chunks line {line_number}.{field_name} has an invalid list"
                )
            string_lists[field_name] = [
                _non_empty_string(
                    f"chunks line {line_number}.{field_name}[{index}]", value
                )
                for index, value in enumerate(raw_list)
            ]
        line_nos = item["line_nos"]
        if not isinstance(line_nos, list) or not line_nos:
            raise StorageImportWorkflowError(
                f"chunks line {line_number}.line_nos must be a non-empty list"
            )
        normalized_line_nos = [
            _positive_integer(f"chunks line {line_number}.line_nos[{index}]", value)
            for index, value in enumerate(line_nos)
        ]
        if not isinstance(item["metadata"], dict):
            raise StorageImportWorkflowError(
                f"chunks line {line_number}.metadata must be an object"
            )
        chunks.append(
            Chunk(
                chunk_id=chunk_id,
                text=text_value,
                law_names=string_lists["law_names"],
                article_numbers=string_lists["article_numbers"],
                source_files=string_lists["source_files"],
                line_nos=normalized_line_nos,
                strategy=strategy,
                metadata=item["metadata"],
            )
        )
    if not chunks:
        raise StorageImportWorkflowError("chunks artifact contains no chunks")
    return chunks, raw


def _required_metadata_field(
    metadata: Mapping[str, Any], name: str, expected_type: type
) -> Any:
    if name not in metadata or type(metadata[name]) is not expected_type:
        raise StorageImportWorkflowError(
            f"embedding cache metadata field {name!r} is missing or invalid"
        )
    return metadata[name]


def _load_embedding_cache_strict(
    cache_dir: Path, *, root: Path
) -> tuple[EmbeddingCache, dict[str, str]]:
    resolved = _resolve_existing_path(
        cache_dir, root=root, label="embedding cache", kind="directory"
    )
    files = {
        "manifest": resolved / "manifest.json",
        "metadata": resolved / "metadata.json",
        "chunk_ids": resolved / "chunk_ids.json",
        "vectors": resolved / "vectors.npy",
    }
    manifest, manifest_raw = _read_control_json(
        files["manifest"], root=root, label="embedding cache manifest"
    )
    metadata, metadata_raw = _read_control_json(
        files["metadata"], root=root, label="embedding cache metadata"
    )
    chunk_ids, chunk_ids_raw = _read_control_json(
        files["chunk_ids"], root=root, label="embedding cache chunk IDs"
    )
    vectors_path = _resolve_existing_path(
        files["vectors"], root=root, label="embedding cache vectors", kind="file"
    )
    if not isinstance(metadata, dict):
        raise StorageImportWorkflowError("embedding cache metadata must be an object")
    if not isinstance(chunk_ids, list) or not chunk_ids:
        raise StorageImportWorkflowError(
            "embedding cache chunk IDs must be a non-empty list"
        )
    normalized_ids = [
        _non_empty_string(f"embedding chunk_ids[{index}]", value, max_length=255)
        for index, value in enumerate(chunk_ids)
    ]
    if len(normalized_ids) != len(set(normalized_ids)):
        raise StorageImportWorkflowError("embedding cache chunk IDs contain duplicates")
    _validate_embedding_manifest(manifest, metadata)
    try:
        import numpy as np

        vectors = np.load(vectors_path, allow_pickle=False, mmap_mode="r")
    except (OSError, ValueError) as exc:
        raise StorageImportWorkflowError("embedding cache vectors are invalid") from exc
    cache = EmbeddingCache(
        cache_dir=resolved,
        metadata=dict(metadata),
        chunk_ids=normalized_ids,
        vectors=vectors,
    )
    return cache, {
        "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "metadata_sha256": hashlib.sha256(metadata_raw).hexdigest(),
        "chunk_ids_sha256": hashlib.sha256(chunk_ids_raw).hexdigest(),
        "vectors_sha256": _sha256_file(vectors_path),
    }


def _validate_embedding_manifest(value: Any, metadata: Mapping[str, Any]) -> None:
    if not isinstance(value, dict):
        raise StorageImportWorkflowError("embedding cache manifest must be an object")
    if value.get("manifest_version") != 1 or value.get("artifact_type") != (
        "embedding_cache"
    ):
        raise StorageImportWorkflowError("embedding cache manifest identity is invalid")
    config = value.get("config")
    metrics = value.get("metrics")
    if not isinstance(config, dict) or not isinstance(metrics, dict):
        raise StorageImportWorkflowError(
            "embedding cache manifest sections are invalid"
        )
    comparisons = {
        "embedding_key": "embedding_key",
        "provider": "provider",
        "model_name": "model_name",
        "revision": "revision",
        "normalize": "normalize",
        "trust_remote_code": "trust_remote_code",
        "query_prefix": "query_prefix",
        "document_prefix": "document_prefix",
        "embed_with_metadata": "embed_with_metadata",
    }
    for manifest_name, metadata_name in comparisons.items():
        if config.get(manifest_name) != metadata.get(metadata_name):
            raise StorageImportWorkflowError(
                f"embedding cache manifest {manifest_name!r} does not match metadata"
            )
    if metrics.get("chunk_count") != metadata.get("chunk_count") or metrics.get(
        "dimension"
    ) != metadata.get("dimension"):
        raise StorageImportWorkflowError(
            "embedding cache manifest metrics do not match metadata"
        )


def _embedding_model_from_cache(cache: EmbeddingCache) -> EmbeddingModelConfig:
    metadata = cache.metadata
    schema_version = _required_metadata_field(metadata, "schema_version", int)
    if schema_version != 2:
        raise StorageImportWorkflowError(
            "embedding cache schema_version 2 is required for version-bound import"
        )
    dimension = _required_metadata_field(metadata, "dimension", int)
    if dimension <= 0:
        raise StorageImportWorkflowError("embedding cache dimension must be positive")
    revision = _non_empty_string(
        "embedding cache revision",
        _required_metadata_field(metadata, "revision", str),
    )
    return EmbeddingModelConfig(
        key=_non_empty_string(
            "embedding cache key",
            _required_metadata_field(metadata, "embedding_key", str),
        ),
        provider=_non_empty_string(
            "embedding cache provider",
            _required_metadata_field(metadata, "provider", str),
        ),
        model_name=_non_empty_string(
            "embedding cache model",
            _required_metadata_field(metadata, "model_name", str),
        ),
        role=_non_empty_string(
            "embedding cache role", _required_metadata_field(metadata, "role", str)
        ),
        revision=revision,
        normalize=_required_metadata_field(metadata, "normalize", bool),
        trust_remote_code=_required_metadata_field(metadata, "trust_remote_code", bool),
        dimensions=dimension,
        query_prefix=_required_metadata_field(metadata, "query_prefix", str),
        document_prefix=_required_metadata_field(metadata, "document_prefix", str),
        embed_with_metadata=_required_metadata_field(
            metadata, "embed_with_metadata", bool
        ),
    )


def _validate_index_manifest(
    value: Any, *, chunks: Sequence[Chunk]
) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise StorageImportWorkflowError("index manifest must be an object")
    if value.get("manifest_version") != 1 or value.get("artifact_type") != "index":
        raise StorageImportWorkflowError("index manifest identity is invalid")
    config = value.get("config")
    metrics = value.get("metrics")
    if not isinstance(config, dict) or not isinstance(metrics, dict):
        raise StorageImportWorkflowError("index manifest sections are invalid")
    strategies = {chunk.strategy for chunk in chunks}
    if len(strategies) != 1 or config.get("strategy") not in strategies:
        raise StorageImportWorkflowError(
            "index manifest strategy does not match the chunks artifact"
        )
    if metrics.get("chunk_count") != len(chunks):
        raise StorageImportWorkflowError(
            "index manifest chunk_count does not match the chunks artifact"
        )
    chunking = config.get("chunking")
    if not isinstance(chunking, dict):
        raise StorageImportWorkflowError("index manifest chunking config is invalid")
    canonical_json(chunking)
    return {"strategy": config["strategy"], "chunking": chunking}


def _normalized_articles(dataset_dir: Path, *, root: Path):
    try:
        articles = load_articles(dataset_dir)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise StorageImportWorkflowError(
            "dataset articles could not be parsed"
        ) from exc
    if not articles:
        raise StorageImportWorkflowError("dataset contains no parsed articles")
    normalized = []
    for article in articles:
        source = _resolve_existing_path(
            Path(article.source_file),
            root=root,
            label="article source file",
            kind="file",
        )
        normalized.append(
            replace(article, source_file=source.relative_to(root).as_posix())
        )
    return normalized


def _parse_laws(value: Sequence[Mapping[str, Any]]) -> list[LawVersionSpec]:
    return [LawVersionSpec(**dict(item)) for item in value]


def _artifact_summary(
    *,
    request_sha256: str,
    dataset_file_count: int,
    dataset_sha256: str,
    chunks_raw: bytes,
    index_manifest_raw: bytes,
    cache_hashes: Mapping[str, str],
    bundle: StorageImportBundle,
) -> dict[str, Any]:
    plan_without_hash: dict[str, Any] = {
        "schema_version": IMPORT_PLAN_SCHEMA_VERSION,
        "artifact_kind": IMPORT_PLAN_KIND,
        "request_sha256": request_sha256,
        "inputs": {
            "dataset": {
                "file_count": dataset_file_count,
                "sha256": dataset_sha256,
            },
            "chunks": {
                "byte_count": len(chunks_raw),
                "sha256": hashlib.sha256(chunks_raw).hexdigest(),
            },
            "index_manifest": {
                "sha256": hashlib.sha256(index_manifest_raw).hexdigest(),
            },
            "embedding_cache": dict(sorted(cache_hashes.items())),
        },
        "target": {
            "snapshot_id": bundle.snapshot.snapshot_id,
            "scope_id": bundle.snapshot.scope_id,
            "profile_id": bundle.embedding_profile.profile_id,
            "provider": bundle.embedding_profile.provider,
            "revision": bundle.embedding_profile.revision,
            "dimensions": bundle.embedding_profile.dimensions,
            "normalization": bundle.embedding_profile.normalization,
            "model_identity_sha256": sha256_json(
                {
                    "provider": bundle.embedding_profile.provider,
                    "model": bundle.embedding_profile.model,
                    "revision": bundle.embedding_profile.revision,
                }
            ),
        },
        "expected": {
            "source_manifest_hash": bundle.snapshot.source_manifest_hash,
            "corpus_hash": bundle.corpus_hash,
            "bundle_hash": bundle.bundle_hash,
            "law_version_count": len(bundle.law_versions),
            "article_count": len(bundle.articles),
            "chunk_count": len(bundle.chunks),
            "embedding_count": len(bundle.embeddings),
        },
        "operations": {
            "imports_snapshot": True,
            "imports_embedding_profile": True,
            "creates_or_reuses_immutable_rows": True,
            "activates_snapshot": False,
            "builds_ann_index": False,
            "external_model_calls": 0,
        },
    }
    return {**plan_without_hash, "plan_sha256": sha256_json(plan_without_hash)}


def build_import_plan(
    manifest_path: str | Path,
    *,
    source_root: str | Path | None = None,
) -> PreparedStorageImport:
    requested_manifest = Path(manifest_path)
    root = _source_root(source_root, manifest_path=requested_manifest.absolute())
    manifest = _resolve_existing_path(
        requested_manifest,
        root=root,
        label="import request manifest",
        kind="file",
    )
    raw_request, _ = _read_control_json(
        manifest, root=root, label="import request manifest"
    )
    request = _parse_request(raw_request)
    inputs = request["inputs"]
    dataset_dir = _path_from_request(
        root, inputs["dataset_dir"], label="dataset_dir", kind="directory"
    )
    chunks_path = _path_from_request(
        root, inputs["chunks_path"], label="chunks_path", kind="file"
    )
    index_manifest_path = _path_from_request(
        root,
        inputs["index_manifest_path"],
        label="index_manifest_path",
        kind="file",
    )
    cache_dir = _path_from_request(
        root,
        inputs["embedding_cache_dir"],
        label="embedding_cache_dir",
        kind="directory",
    )

    dataset_file_count, dataset_sha256 = _dataset_identity(dataset_dir, root=root)
    articles = _normalized_articles(dataset_dir, root=root)
    chunks, chunks_raw = _load_chunks_strict(chunks_path, root=root)
    index_manifest, index_manifest_raw = _read_control_json(
        index_manifest_path, root=root, label="index manifest"
    )
    chunk_recipe = _validate_index_manifest(index_manifest, chunks=chunks)
    cache, cache_hashes = _load_embedding_cache_strict(cache_dir, root=root)
    model = _embedding_model_from_cache(cache)

    try:
        from .contracts import build_storage_import_bundle

        bundle = build_storage_import_bundle(
            snapshot_id=request["snapshot_id"],
            scope_id=request["scope_id"],
            source_manifest=request["source_manifest"],
            laws=_parse_laws(request["law_versions"]),
            articles=articles,
            chunks=chunks,
            embedding_cache=cache,
            model_config=model,
            model_revision=model.revision,
            chunk_recipe=chunk_recipe,
            article_version_ids=request["article_version_ids"],
        )
        validate_storage_import_bundle(bundle)
    except StorageImportWorkflowError:
        raise
    except (TypeError, ValueError) as exc:
        raise StorageImportWorkflowError(
            f"local artifacts do not form a valid storage bundle: {exc}"
        ) from exc

    request_sha256 = sha256_json(request)
    plan = _artifact_summary(
        request_sha256=request_sha256,
        dataset_file_count=dataset_file_count,
        dataset_sha256=dataset_sha256,
        chunks_raw=chunks_raw,
        index_manifest_raw=index_manifest_raw,
        cache_hashes=cache_hashes,
        bundle=bundle,
    )
    return PreparedStorageImport(plan=plan, bundle=bundle)


def _read_plan(path: str | Path) -> dict[str, Any]:
    requested = Path(path)
    if requested.is_symlink():
        raise StorageImportWorkflowError("import plan must not be a symlink")
    try:
        plan_path = requested.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise StorageImportWorkflowError("import plan does not exist") from exc
    if not plan_path.is_file():
        raise StorageImportWorkflowError("import plan must be a regular file")
    value, _ = _read_control_json(
        plan_path,
        root=plan_path.parent,
        label="import plan",
    )
    plan = _exact_object(
        "import plan",
        value,
        {
            "schema_version",
            "artifact_kind",
            "request_sha256",
            "inputs",
            "target",
            "expected",
            "operations",
            "plan_sha256",
        },
    )
    if (
        plan["schema_version"] != IMPORT_PLAN_SCHEMA_VERSION
        or plan["artifact_kind"] != IMPORT_PLAN_KIND
    ):
        raise StorageImportWorkflowError("import plan identity is invalid")
    supplied_hash = plan.pop("plan_sha256")
    if supplied_hash != sha256_json(plan):
        raise StorageImportWorkflowError("import plan hash is invalid")
    return {**plan, "plan_sha256": supplied_hash}


def validate_import_plan(
    manifest_path: str | Path,
    plan_path: str | Path,
    *,
    source_root: str | Path | None = None,
) -> PreparedStorageImport:
    requested_manifest = Path(manifest_path)
    root = _source_root(source_root, manifest_path=requested_manifest.absolute())
    supplied = _read_plan(plan_path)
    current = build_import_plan(manifest_path, source_root=root)
    if canonical_json(supplied) != canonical_json(current.plan):
        raise StorageImportWorkflowError(
            "import plan does not match the current request and local artifacts"
        )
    return current


def validation_receipt(prepared: PreparedStorageImport) -> dict[str, Any]:
    return {
        "schema_version": IMPORT_RECEIPT_SCHEMA_VERSION,
        "artifact_kind": IMPORT_VALIDATION_KIND,
        "status": "validated",
        "plan_sha256": prepared.plan["plan_sha256"],
        "target": dict(prepared.plan["target"]),
        "expected": dict(prepared.plan["expected"]),
        "database_connected": False,
        "external_model_calls": 0,
    }


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def apply_import_plan(
    manifest_path: str | Path,
    plan_path: str | Path,
    *,
    engine: Any,
    source_root: str | Path | None = None,
    migration_applied: bool = False,
) -> dict[str, Any]:
    prepared = validate_import_plan(manifest_path, plan_path, source_root=source_root)
    return apply_prepared_import(
        prepared,
        engine=engine,
        migration_applied=migration_applied,
    )


def apply_prepared_import(
    prepared: PreparedStorageImport,
    *,
    engine: Any,
    migration_applied: bool = False,
) -> dict[str, Any]:
    """Apply an already revalidated plan without changing snapshot activation."""

    from .repository import PostgresCorpusRepository

    repository = PostgresCorpusRepository(engine)
    before = repository.table_counts()
    result = repository.import_bundle(prepared.bundle)
    persisted = repository.validate_bundle(prepared.bundle)
    after = repository.table_counts()
    delta = {name: after[name] - before[name] for name in sorted(before)}
    receipt_without_hash: dict[str, Any] = {
        "schema_version": IMPORT_RECEIPT_SCHEMA_VERSION,
        "artifact_kind": IMPORT_RECEIPT_KIND,
        "status": "imported" if result.imported else "already_present",
        "completed_at": _utc_now(),
        "plan_sha256": prepared.plan["plan_sha256"],
        "target": dict(prepared.plan["target"]),
        "result": asdict(result),
        "persisted_validation": asdict(persisted),
        "table_counts": {
            "before": dict(sorted(before.items())),
            "after": dict(sorted(after.items())),
            "delta": delta,
        },
        "migration_applied": migration_applied,
        "activation_performed": False,
        "ann_build_performed": False,
        "external_model_calls": 0,
    }
    return {
        **receipt_without_hash,
        "receipt_sha256": sha256_json(receipt_without_hash),
    }


def write_machine_artifact(path: str | Path, value: Mapping[str, Any]) -> Path:
    destination = Path(path)
    if destination.exists() or destination.is_symlink():
        raise StorageImportWorkflowError(
            "output artifact already exists; refusing to overwrite it"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )
    try:
        with destination.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise StorageImportWorkflowError(
            "output artifact already exists; refusing to overwrite it"
        ) from exc
    return destination


def validate_database_env_name(value: str) -> str:
    if not isinstance(value, str) or not _ENVIRONMENT_VARIABLE.fullmatch(value):
        raise StorageImportWorkflowError(
            "database URL environment variable name is invalid"
        )
    return value


def machine_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
