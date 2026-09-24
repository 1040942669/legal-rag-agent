from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


NORMALIZED_VECTOR_L2_ATOL = 1e-3
EMBEDDING_QUERY_RECIPE_VERSION = "query-prefix-v1"
EMBEDDING_DOCUMENT_RECIPE_VERSION = "law-article-header-v1"
EMBEDDING_ENCODER_ADAPTER_VERSION = "bound-encoder-v1"


class EmbeddingVectorContractError(ValueError):
    """A vector does not satisfy its declared embedding-space contract."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"embedding identity is not canonical JSON: {exc}") from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EmbeddingProfileIdentity:
    """Immutable identity for one document/query embedding space.

    Runtime connection details and credentials are deliberately excluded. Every
    field that can change vector semantics is included in ``profile_id``.
    """

    provider: str
    model: str
    revision: str
    dimensions: int
    normalization: bool
    query_prefix: str
    document_prefix: str
    embed_with_metadata: bool

    def __post_init__(self) -> None:
        for field_name in ("provider", "model", "revision"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(
                    f"embedding profile {field_name} must be a canonical non-empty string"
                )
        if type(self.dimensions) is not int or self.dimensions <= 0:
            raise ValueError("embedding profile dimensions must be a positive integer")
        if type(self.normalization) is not bool:
            raise ValueError("embedding profile normalization must be a boolean")
        for field_name in ("query_prefix", "document_prefix"):
            if not isinstance(getattr(self, field_name), str):
                raise ValueError(f"embedding profile {field_name} must be a string")
        if type(self.embed_with_metadata) is not bool:
            raise ValueError("embedding profile embed_with_metadata must be a boolean")

    @property
    def recipe(self) -> dict[str, Any]:
        return {
            "encoder_adapter_version": EMBEDDING_ENCODER_ADAPTER_VERSION,
            "query_recipe_version": EMBEDDING_QUERY_RECIPE_VERSION,
            "document_recipe_version": EMBEDDING_DOCUMENT_RECIPE_VERSION,
            "query_prefix": self.query_prefix,
            "document_prefix": self.document_prefix,
            "embed_with_metadata": self.embed_with_metadata,
        }

    @property
    def recipe_hash(self) -> str:
        return _sha256_json(self.recipe)

    @property
    def profile_id(self) -> str:
        return _sha256_json(
            {
                "provider": self.provider,
                "model": self.model,
                "revision": self.revision,
                "dimensions": self.dimensions,
                "normalization": self.normalization,
                "recipe": self.recipe,
            }
        )


def canonicalize_embedding_vector(
    value: Any,
    *,
    expected_dimension: int,
    normalized: bool,
    label: str,
    require_float32: bool = False,
) -> Any:
    """Return the exact little-endian float32 vector after strict validation."""

    try:
        import numpy as np
    except ModuleNotFoundError as exc:  # pragma: no cover - project requires numpy.
        raise RuntimeError("embedding vector validation requires numpy") from exc

    try:
        source = np.asarray(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EmbeddingVectorContractError(
            f"{label} must be a one-dimensional numeric array"
        ) from exc
    if source.ndim != 1 or source.size == 0 or source.dtype.kind not in {"f", "i", "u"}:
        raise EmbeddingVectorContractError(
            f"{label} must be a non-empty one-dimensional numeric array"
        )
    if require_float32 and source.dtype != np.dtype("float32"):
        raise EmbeddingVectorContractError(f"{label} dtype must be float32")
    if not bool(np.isfinite(source).all()):
        raise EmbeddingVectorContractError(f"{label} must contain only finite values")
    try:
        with np.errstate(over="ignore", invalid="ignore"):
            vector = np.ascontiguousarray(source, dtype="<f4")
    except (TypeError, ValueError, OverflowError) as exc:
        raise EmbeddingVectorContractError(
            f"{label} cannot be represented as float32"
        ) from exc
    if not bool(np.isfinite(vector).all()):
        raise EmbeddingVectorContractError(
            f"{label} cannot be represented as finite float32"
        )
    if int(vector.shape[0]) != expected_dimension:
        raise EmbeddingVectorContractError(
            f"{label} dimension {int(vector.shape[0])} does not match profile "
            f"dimension {expected_dimension}"
        )
    if normalized:
        norm = float(np.linalg.norm(vector.astype(np.float64, copy=False)))
        deviation = abs(norm - 1.0)
        if deviation > NORMALIZED_VECTOR_L2_ATOL:
            raise EmbeddingVectorContractError(
                f"{label} L2 norm {norm:.6f} violates normalized profile tolerance "
                f"{NORMALIZED_VECTOR_L2_ATOL}"
            )
    return vector


def canonicalize_embedding_matrix(
    value: Any,
    *,
    expected_dimension: int,
    normalized: bool,
    label: str,
    require_float32: bool = False,
) -> Any:
    """Validate every row and return a contiguous little-endian float32 matrix."""

    try:
        import numpy as np
    except ModuleNotFoundError as exc:  # pragma: no cover - project requires numpy.
        raise RuntimeError("embedding matrix validation requires numpy") from exc

    try:
        source = np.asarray(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EmbeddingVectorContractError(f"{label} must be a numeric matrix") from exc
    if source.ndim != 2:
        raise EmbeddingVectorContractError(f"{label} must be a two-dimensional matrix")
    if require_float32 and source.dtype != np.dtype("float32"):
        raise EmbeddingVectorContractError(f"{label} dtype must be float32")
    rows = [
        canonicalize_embedding_vector(
            row,
            expected_dimension=expected_dimension,
            normalized=normalized,
            label=f"{label} row {index}",
            require_float32=require_float32,
        )
        for index, row in enumerate(source)
    ]
    if not rows:
        return np.empty((0, expected_dimension), dtype="<f4")
    return np.ascontiguousarray(np.stack(rows), dtype="<f4")
