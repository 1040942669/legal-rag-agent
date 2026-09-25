from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass, replace
from typing import Any, Literal, Mapping, Sequence

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    Engine,
    and_,
    cast,
    exists,
    func,
    insert,
    literal_column,
    select,
    text,
    update,
)
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import ClauseElement, Executable

from ..embedding_contracts import EmbeddingProfileIdentity
from ..models import SearchResult
from ..provider_errors import ProviderCallError, raise_sanitized_provider_error
from ..retrieval_contracts import RetrievalBoundary, RetrievalContractError
from .contracts import canonical_json, sha256_json
from .retrieval import (
    PostgresExactRetrievalRepository,
    QueryEncoder,
    RetrievalDataError,
    RetrievalFilters,
    _canonical_query_vector,
    _validate_top_k,
)
from .schema import (
    chunk_embeddings,
    chunks,
    corpus_snapshots,
    embedding_imports,
    embedding_profile_generations,
    index_builds,
    snapshot_chunks,
)


HNSW_VECTOR_MAX_DIMENSIONS = 2_000
MIN_ITERATIVE_SCAN_PGVECTOR_VERSION = (0, 8, 0)
_PROFILE_ID_PATTERN = re.compile(r"[0-9a-f]{64}")
_INDEX_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,62}")


class AnnIndexError(RuntimeError):
    """An ANN index cannot be built or trusted for the requested boundary."""


class AnnIndexUnsupportedError(AnnIndexError):
    """The installed extension or embedding dimensions do not support HNSW."""


class AnnUnderfillError(AnnIndexError):
    """A list-only caller attempted to consume an incomplete ANN outcome."""

    def __init__(self, outcome: AnnSearchOutcome) -> None:
        self.outcome = outcome
        super().__init__(
            f"ANN search did not safely complete: {outcome.status} "
            f"({outcome.underfill_reason})"
        )


def _strict_int(value: Any, *, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class HnswIndexSpec:
    """Immutable physical HNSW build parameters.

    Inner product intentionally matches the exact pgvector baseline. The
    default database retriever remains exact; constructing this object does
    not opt any caller into ANN retrieval.
    """

    m: int = 16
    ef_construction: int = 64

    def __post_init__(self) -> None:
        resolved_m = _strict_int(self.m, label="m", minimum=2, maximum=100)
        resolved_ef = _strict_int(
            self.ef_construction,
            label="ef_construction",
            minimum=4,
            maximum=1_000,
        )
        if resolved_ef < 2 * resolved_m:
            raise ValueError("ef_construction must be at least 2 * m")

    def payload(self) -> dict[str, Any]:
        return {
            "algorithm": "hnsw",
            "distance": "inner_product",
            "operator": "<#>",
            "operator_class": "vector_ip_ops",
            "m": self.m,
            "ef_construction": self.ef_construction,
        }


@dataclass(frozen=True, slots=True)
class AnnSearchPolicy:
    """Bound the experimental HNSW query and its optional exact fallback."""

    ef_search: int = 40
    max_scan_tuples: int = 20_000
    scan_mem_multiplier: int = 1
    iterative_scan: Literal["off", "strict_order"] = "strict_order"
    exact_fallback_on_underfill: bool = True
    exact_fallback_timeout_ms: int = 5_000

    def __post_init__(self) -> None:
        _strict_int(self.ef_search, label="ef_search", minimum=1, maximum=1_000)
        _strict_int(
            self.max_scan_tuples,
            label="max_scan_tuples",
            minimum=1,
            maximum=1_000_000,
        )
        _strict_int(
            self.scan_mem_multiplier,
            label="scan_mem_multiplier",
            minimum=1,
            maximum=1_000,
        )
        if self.iterative_scan not in {"off", "strict_order"}:
            raise ValueError("iterative_scan must be off or strict_order")
        if not isinstance(self.exact_fallback_on_underfill, bool):
            raise TypeError("exact_fallback_on_underfill must be a boolean")
        _strict_int(
            self.exact_fallback_timeout_ms,
            label="exact_fallback_timeout_ms",
            minimum=1,
            maximum=60_000,
        )

    def trace_payload(self) -> dict[str, Any]:
        return {
            "ef_search": self.ef_search,
            "max_scan_tuples": self.max_scan_tuples,
            "scan_mem_multiplier": self.scan_mem_multiplier,
            "iterative_scan": self.iterative_scan,
            "exact_fallback_on_underfill": self.exact_fallback_on_underfill,
            "exact_fallback_timeout_ms": self.exact_fallback_timeout_ms,
        }


@dataclass(frozen=True, slots=True)
class AnnIndexBuild:
    build_id: str
    snapshot_id: str
    profile_id: str
    dimensions: int
    profile_embedding_count: int
    profile_embedding_manifest_hash: str
    physical_instance_id: str
    pgvector_version: str
    postgres_major: int
    physical_index_name: str
    index_params_hash: str
    status: Literal["validated", "active"]
    reused: bool

    def __post_init__(self) -> None:
        if not isinstance(self.build_id, str) or not re.fullmatch(
            r"ann-hnsw-[0-9a-f]{64}", self.build_id
        ):
            raise ValueError("build_id must be a canonical HNSW build identity")
        if not isinstance(self.snapshot_id, str) or not self.snapshot_id:
            raise ValueError("snapshot_id must be a non-empty string")
        if not _PROFILE_ID_PATTERN.fullmatch(self.profile_id):
            raise ValueError("profile_id must be a canonical SHA-256 digest")
        _strict_int(
            self.dimensions,
            label="dimensions",
            minimum=1,
            maximum=16_000,
        )
        _strict_int(
            self.profile_embedding_count,
            label="profile_embedding_count",
            minimum=1,
            maximum=9_223_372_036_854_775_807,
        )
        if not re.fullmatch(r"[0-9a-f]{64}", self.profile_embedding_manifest_hash):
            raise ValueError(
                "profile_embedding_manifest_hash must be a canonical SHA-256 digest"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", self.physical_instance_id):
            raise ValueError(
                "physical_instance_id must be a canonical 256-bit identity"
            )
        if not re.fullmatch(r"\d+(?:\.\d+)+", self.pgvector_version):
            raise ValueError("pgvector_version must be a canonical dotted version")
        _strict_int(
            self.postgres_major,
            label="postgres_major",
            minimum=12,
            maximum=99,
        )
        if not _INDEX_NAME_PATTERN.fullmatch(self.physical_index_name):
            raise ValueError("physical_index_name is unsafe")
        if (
            self.physical_index_name
            != f"ix_ce_hnsw_ip_{self.physical_instance_id[:32]}"
        ):
            raise ValueError(
                "physical_index_name must be derived from physical_instance_id"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", self.index_params_hash):
            raise ValueError("index_params_hash must be a canonical SHA-256 digest")
        if self.status not in {"validated", "active"}:
            raise ValueError("ANN build status must be validated or active")
        if not isinstance(self.reused, bool):
            raise TypeError("reused must be a boolean")


@dataclass(frozen=True, slots=True)
class AnnSearchOutcome:
    results: tuple[SearchResult, ...]
    requested_top_k: int
    ann_returned_count: int
    eligible_count: int | None
    exact_returned_count: int | None
    status: Literal[
        "empty_boundary",
        "ann_complete",
        "ann_underfilled",
        "exact_fallback_complete",
        "exact_fallback_exhausted",
        "exact_fallback_timed_out",
    ]
    underfill_reason: str | None
    exact_fallback_used: bool
    build_id: str
    physical_index_name: str

    def __post_init__(self) -> None:
        reason_by_status = {
            "empty_boundary": "boundary_denies_all",
            "ann_complete": None,
            "ann_underfilled": "ann_underfill_unclassified",
            "exact_fallback_complete": "ann_scan_budget_exhausted",
            "exact_fallback_exhausted": "eligible_population_below_top_k",
            "exact_fallback_timed_out": "exact_fallback_statement_timeout",
        }
        if self.status not in reason_by_status:
            raise ValueError("unsupported ANN outcome status")
        if not isinstance(self.results, tuple) or any(
            not isinstance(item, SearchResult) for item in self.results
        ):
            raise TypeError("results must be a tuple of SearchResult values")
        _strict_int(
            self.requested_top_k,
            label="requested_top_k",
            minimum=1,
            maximum=10_000,
        )
        _strict_int(
            self.ann_returned_count,
            label="ann_returned_count",
            minimum=0,
            maximum=10_000,
        )
        if self.eligible_count is not None:
            _strict_int(
                self.eligible_count,
                label="eligible_count",
                minimum=0,
                maximum=9_223_372_036_854_775_807,
            )
        if self.exact_returned_count is not None:
            _strict_int(
                self.exact_returned_count,
                label="exact_returned_count",
                minimum=0,
                maximum=10_000,
            )
        if self.ann_returned_count > self.requested_top_k:
            raise ValueError("ANN returned count cannot exceed requested top_k")
        if (
            self.eligible_count is not None
            and self.ann_returned_count > self.eligible_count
        ):
            raise ValueError("ANN returned count cannot exceed eligible count")
        if (
            self.exact_returned_count is not None
            and self.exact_returned_count > self.requested_top_k
        ):
            raise ValueError("exact returned count cannot exceed requested top_k")
        if self.underfill_reason != reason_by_status[self.status]:
            raise ValueError("ANN outcome reason does not match its status")
        if not re.fullmatch(r"ann-hnsw-[0-9a-f]{64}", self.build_id):
            raise ValueError("build_id is not a canonical HNSW build identity")
        if not re.fullmatch(r"ix_ce_hnsw_ip_[0-9a-f]{32}", self.physical_index_name):
            raise ValueError("physical_index_name is not a canonical HNSW index name")
        if self.status == "empty_boundary":
            if (
                self.results
                or self.ann_returned_count
                or self.eligible_count != 0
                or self.exact_returned_count is not None
                or self.underfill_reason != "boundary_denies_all"
                or self.exact_fallback_used
            ):
                raise ValueError("empty boundary outcome is inconsistent")
        elif self.status == "ann_complete":
            if self.ann_returned_count != self.requested_top_k:
                raise ValueError("complete ANN outcomes must fill top_k")
            if (
                self.underfill_reason is not None
                or self.exact_fallback_used
                or self.exact_returned_count is not None
            ):
                raise ValueError("complete ANN outcomes cannot report underfill")
        else:
            if self.ann_returned_count >= self.requested_top_k:
                raise ValueError(
                    "underfill outcomes must contain fewer ANN rows than top_k"
                )
            if not self.underfill_reason:
                raise ValueError("underfill outcomes require an explanation")
        fallback_statuses = {
            "exact_fallback_complete",
            "exact_fallback_exhausted",
            "exact_fallback_timed_out",
        }
        if self.status in fallback_statuses:
            if not self.exact_fallback_used:
                raise ValueError("exact fallback status requires an exact fallback")
        elif self.exact_fallback_used:
            raise ValueError("exact fallback usage must be reflected in status")
        if self.status == "ann_underfilled" and self.exact_returned_count is not None:
            raise ValueError("disabled fallback cannot report exact results")
        if self.status == "exact_fallback_complete":
            if self.eligible_count is not None:
                raise ValueError(
                    "complete exact fallback has no exact population count"
                )
            if self.exact_returned_count != self.requested_top_k:
                raise ValueError("complete exact fallback must fill top_k")
        if self.status == "exact_fallback_exhausted":
            if self.eligible_count is None:
                raise ValueError("exhausted fallback requires an eligible count")
            if self.exact_returned_count != self.eligible_count:
                raise ValueError(
                    "exhausted fallback must return the eligible population"
                )
            if self.exact_returned_count >= self.requested_top_k:
                raise ValueError("exhausted fallback cannot fill top_k")
            if self.ann_returned_count > self.exact_returned_count:
                raise ValueError("ANN count cannot exceed a completed exact fallback")
        if self.status == "exact_fallback_timed_out":
            if self.exact_returned_count is not None or self.eligible_count is not None:
                raise ValueError(
                    "timed out fallback cannot report an exact or eligible count"
                )
        if self.status in {"ann_complete", "ann_underfilled"}:
            if self.eligible_count is not None:
                raise ValueError(
                    "ANN-only outcomes cannot report an exact population count"
                )
        if self.status in {
            "ann_complete",
            "ann_underfilled",
            "exact_fallback_timed_out",
        }:
            if len(self.results) != self.ann_returned_count:
                raise ValueError("ANN-backed outcome result count is inconsistent")
        if self.status in {"exact_fallback_complete", "exact_fallback_exhausted"}:
            if len(self.results) != self.exact_returned_count:
                raise ValueError("exact fallback outcome result count is inconsistent")
        if len(self.results) > self.requested_top_k:
            raise ValueError("ANN outcome cannot exceed requested top_k")


def _parse_version(value: str) -> tuple[int, ...]:
    match = re.match(r"^(\d+(?:\.\d+)*)", value.strip())
    if match is None:
        raise AnnIndexUnsupportedError("cannot parse the installed pgvector version")
    return tuple(int(part) for part in match.group(1).split("."))


def _physical_identity(
    *,
    profile_id: str,
    dimensions: int,
    profile_embedding_count: int,
    profile_embedding_manifest_hash: str,
    pgvector_version: str,
    postgres_major: int,
    spec: HnswIndexSpec,
) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", profile_embedding_manifest_hash):
        raise AnnIndexError("profile embedding manifest hash is not canonical")
    if not re.fullmatch(r"\d+(?:\.\d+)+", pgvector_version):
        raise AnnIndexError("pgvector version is not canonical")
    _strict_int(
        postgres_major,
        label="postgres_major",
        minimum=12,
        maximum=99,
    )
    return sha256_json(
        {
            "profile_id": profile_id,
            "dimensions": dimensions,
            "profile_embedding_count": profile_embedding_count,
            "profile_embedding_manifest_hash": profile_embedding_manifest_hash,
            "pgvector_version": pgvector_version,
            "postgres_major": postgres_major,
            "spec": spec.payload(),
        }
    )


def _physical_index_name(
    *,
    physical_instance_id: str,
) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", physical_instance_id):
        raise AnnIndexError("physical HNSW instance identity is not canonical")
    return f"ix_ce_hnsw_ip_{physical_instance_id[:32]}"


def _index_params(
    *,
    profile_id: str,
    dimensions: int,
    profile_embedding_count: int,
    profile_embedding_manifest_hash: str,
    physical_instance_id: str,
    pgvector_version: str,
    postgres_major: int,
    spec: HnswIndexSpec,
) -> dict[str, Any]:
    physical_name = _physical_index_name(
        physical_instance_id=physical_instance_id,
    )
    build_spec_hash = _physical_identity(
        profile_id=profile_id,
        dimensions=dimensions,
        profile_embedding_count=profile_embedding_count,
        profile_embedding_manifest_hash=profile_embedding_manifest_hash,
        pgvector_version=pgvector_version,
        postgres_major=postgres_major,
        spec=spec,
    )
    return {
        **spec.payload(),
        "profile_id": profile_id,
        "dimensions": dimensions,
        "physical_scope": "profile_generation",
        "profile_embedding_count": profile_embedding_count,
        "profile_embedding_manifest_hash": profile_embedding_manifest_hash,
        "pgvector_version": pgvector_version,
        "postgres_major": postgres_major,
        "build_spec_hash": build_spec_hash,
        "physical_instance_id": physical_instance_id,
        "physical_index_name": physical_name,
        "predicate": {
            "profile_id": profile_id,
            "embedding_dimension": dimensions,
        },
        "expression": f"embedding::vector({dimensions})",
    }


def _build_id(*, snapshot_id: str, profile_id: str, params_hash: str) -> str:
    identity = sha256_json(
        {
            "snapshot_id": snapshot_id,
            "profile_id": profile_id,
            "index_params_hash": params_hash,
        }
    )
    return f"ann-hnsw-{identity}"


def _profile_embedding_manifest_hash(
    connection,
    *,
    profile_id: str,
    dimensions: int,
    expected_count: int,
) -> str:
    """Hash one stable profile rowset without loading all vectors into memory."""

    hasher = hashlib.sha256()
    hasher.update(b"legal-rag-profile-embedding-manifest-v1\x00")
    statement = (
        select(
            chunk_embeddings.c.chunk_id,
            chunk_embeddings.c.embedding_hash,
            chunk_embeddings.c.embedding_dimension,
        )
        .where(
            chunk_embeddings.c.profile_id == profile_id,
            chunk_embeddings.c.embedding_dimension == dimensions,
        )
        .order_by(chunk_embeddings.c.chunk_id.asc())
    )
    count = 0
    result = connection.execute(statement.execution_options(stream_results=True))
    for row in result.mappings():
        encoded = canonical_json(
            {
                "chunk_id": str(row["chunk_id"]),
                "embedding_hash": str(row["embedding_hash"]),
                "embedding_dimension": int(row["embedding_dimension"]),
            }
        ).encode("utf-8")
        hasher.update(len(encoded).to_bytes(8, byteorder="big"))
        hasher.update(encoded)
        count += 1
    if count != expected_count:
        raise AnnIndexError(
            "embedding generation count does not match its canonical rowset"
        )
    return hasher.hexdigest()


def _spec_from_index_params(params: Mapping[str, Any]) -> HnswIndexSpec:
    if set(params) != {
        "algorithm",
        "distance",
        "operator",
        "operator_class",
        "m",
        "ef_construction",
        "profile_id",
        "dimensions",
        "physical_scope",
        "profile_embedding_count",
        "profile_embedding_manifest_hash",
        "pgvector_version",
        "postgres_major",
        "build_spec_hash",
        "physical_instance_id",
        "physical_index_name",
        "predicate",
        "expression",
    }:
        raise AnnIndexError("HNSW build receipt has a non-canonical parameter shape")
    try:
        return HnswIndexSpec(
            m=params["m"],
            ef_construction=params["ef_construction"],
        )
    except (TypeError, ValueError) as exc:
        raise AnnIndexError("HNSW build receipt contains invalid parameters") from exc


def _canonical_receipt_params(row: Mapping[str, Any]) -> dict[str, Any]:
    params = row.get("index_params")
    if not isinstance(params, Mapping):
        raise AnnIndexError("HNSW build receipt parameters are missing")
    spec = _spec_from_index_params(params)
    profile_id = row.get("profile_id")
    snapshot_id = row.get("snapshot_id")
    dimensions = params.get("dimensions")
    profile_embedding_count = params.get("profile_embedding_count")
    manifest_hash = params.get("profile_embedding_manifest_hash")
    physical_instance_id = params.get("physical_instance_id")
    pgvector_version = params.get("pgvector_version")
    postgres_major = params.get("postgres_major")
    if not isinstance(profile_id, str) or not _PROFILE_ID_PATTERN.fullmatch(profile_id):
        raise AnnIndexError("HNSW build receipt profile identity is invalid")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise AnnIndexError("HNSW build receipt snapshot identity is invalid")
    try:
        _strict_int(
            dimensions,
            label="receipt dimensions",
            minimum=1,
            maximum=HNSW_VECTOR_MAX_DIMENSIONS,
        )
        _strict_int(
            profile_embedding_count,
            label="receipt profile_embedding_count",
            minimum=1,
            maximum=9_223_372_036_854_775_807,
        )
    except (TypeError, ValueError) as exc:
        raise AnnIndexError("HNSW receipt dimensions or generation is invalid") from exc
    if not isinstance(manifest_hash, str) or not re.fullmatch(
        r"[0-9a-f]{64}", manifest_hash
    ):
        raise AnnIndexError("HNSW receipt manifest hash is invalid")
    if not isinstance(physical_instance_id, str) or not re.fullmatch(
        r"[0-9a-f]{64}", physical_instance_id
    ):
        raise AnnIndexError("HNSW receipt physical instance identity is invalid")
    if not isinstance(pgvector_version, str) or not re.fullmatch(
        r"\d+(?:\.\d+)+", pgvector_version
    ):
        raise AnnIndexError("HNSW receipt pgvector version is invalid")
    try:
        _strict_int(
            postgres_major,
            label="receipt postgres_major",
            minimum=12,
            maximum=99,
        )
    except (TypeError, ValueError) as exc:
        raise AnnIndexError("HNSW receipt PostgreSQL version is invalid") from exc
    expected = _index_params(
        profile_id=profile_id,
        dimensions=dimensions,
        profile_embedding_count=profile_embedding_count,
        profile_embedding_manifest_hash=manifest_hash,
        physical_instance_id=physical_instance_id,
        pgvector_version=pgvector_version,
        postgres_major=postgres_major,
        spec=spec,
    )
    params_hash = sha256_json(expected)
    if (
        canonical_json(params) != canonical_json(expected)
        or row.get("index_params_hash") != params_hash
        or row.get("build_id")
        != _build_id(
            snapshot_id=snapshot_id,
            profile_id=profile_id,
            params_hash=params_hash,
        )
    ):
        raise AnnIndexError("HNSW build receipt identity is not canonical")
    return expected


class _Explain(Executable, ClauseElement):
    inherit_cache = False

    def __init__(self, statement) -> None:
        self.statement = statement


@compiles(_Explain, "postgresql")
def _compile_explain(element, compiler, **kwargs) -> str:
    return "EXPLAIN (FORMAT JSON) " + compiler.process(element.statement, **kwargs)


def _collect_plan_index_names(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        names = {
            str(child)
            for key, child in value.items()
            if key == "Index Name" and isinstance(child, str)
        }
        for child in value.values():
            names.update(_collect_plan_index_names(child))
        return names
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        names: set[str] = set()
        for child in value:
            names.update(_collect_plan_index_names(child))
        return names
    return set()


class PostgresHnswIndexManager:
    """Build and verify profile/dimension-isolated experimental HNSW indexes."""

    def __init__(self, engine: Engine) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("PostgresHnswIndexManager requires PostgreSQL")
        self.engine = engine
        self._exact = PostgresExactRetrievalRepository(engine)

    def ensure_index(
        self,
        *,
        filters: RetrievalFilters,
        expected_profile: EmbeddingProfileIdentity,
        spec: HnswIndexSpec | None = None,
    ) -> AnnIndexBuild:
        resolved_spec = HnswIndexSpec() if spec is None else spec
        if not isinstance(resolved_spec, HnswIndexSpec):
            raise TypeError("spec must be an HnswIndexSpec")
        if filters.profile_id != expected_profile.profile_id:
            raise RetrievalContractError(
                "expected embedding profile does not match bound profile_id"
            )
        if expected_profile.dimensions > HNSW_VECTOR_MAX_DIMENSIONS:
            raise AnnIndexUnsupportedError(
                "HNSW vector indexes support at most 2000 dimensions; keep "
                "pgvector exact retrieval enabled or run a separately validated "
                "representation experiment. Vectors were not truncated."
            )
        if not _PROFILE_ID_PATTERN.fullmatch(expected_profile.profile_id):
            raise RetrievalContractError("profile_id is not a canonical SHA-256 digest")

        with self.engine.begin() as connection:
            profile = self._exact._validated_context(
                connection, filters, expected_profile=expected_profile
            )
            if profile != expected_profile:
                raise RetrievalDataError(
                    "validated embedding profile changed during index build"
                )
            extension_version = connection.scalar(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            )
            if not isinstance(extension_version, str):
                raise AnnIndexUnsupportedError(
                    "the pgvector extension is not installed"
                )
            if _parse_version(extension_version) < MIN_ITERATIVE_SCAN_PGVECTOR_VERSION:
                raise AnnIndexUnsupportedError(
                    "pgvector 0.8.0 or newer is required for bounded iterative "
                    "filtered HNSW scans"
                )
            pgvector_version = ".".join(
                str(part) for part in _parse_version(extension_version)
            )
            server_version_num = connection.scalar(text("SHOW server_version_num"))
            try:
                postgres_major = int(str(server_version_num)) // 10_000
            except (TypeError, ValueError) as exc:
                raise AnnIndexUnsupportedError(
                    "cannot determine the PostgreSQL server major version"
                ) from exc
            _strict_int(
                postgres_major,
                label="postgres_major",
                minimum=12,
                maximum=99,
            )

            # One profile/dimension lock serializes all specs and generations.
            # Take it before the table lock so competing builders cannot both
            # hold SHARE while attempting to retire an earlier physical graph.
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
                {
                    "identity": (
                        "legal-rag-hnsw-profile-v1:"
                        f"{filters.profile_id}:{profile.dimensions}"
                    )
                },
            )
            # Stabilize both the manifest hash and CREATE/DROP INDEX operations.
            connection.exec_driver_sql("LOCK TABLE chunk_embeddings IN SHARE MODE")
            generation = (
                connection.execute(
                    select(embedding_profile_generations)
                    .where(
                        embedding_profile_generations.c.profile_id == filters.profile_id
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if generation is None:
                raise AnnIndexError(
                    "embedding profile generation is missing; re-run the validated "
                    "import before building HNSW"
                )
            profile_embedding_count = generation["embedding_count"]
            if (
                isinstance(profile_embedding_count, bool)
                or not isinstance(profile_embedding_count, int)
                or profile_embedding_count < 1
            ):
                raise AnnIndexError(
                    "embedding profile generation is empty; import vectors before "
                    "building HNSW"
                )

            binding_values = (
                generation["embedding_manifest_hash"],
                generation["ann_physical_instance_id"],
                generation["ann_physical_index_name"],
                generation["ann_index_params_hash"],
            )
            binding_is_empty = all(value is None for value in binding_values)
            if not binding_is_empty and any(value is None for value in binding_values):
                raise AnnIndexError("HNSW generation binding is partially populated")

            if binding_is_empty:
                profile_embedding_manifest_hash = _profile_embedding_manifest_hash(
                    connection,
                    profile_id=filters.profile_id,
                    dimensions=profile.dimensions,
                    expected_count=profile_embedding_count,
                )
                adoptable = self._adoptable_physical_params(
                    connection,
                    profile_id=filters.profile_id,
                    dimensions=profile.dimensions,
                    profile_embedding_count=profile_embedding_count,
                    profile_embedding_manifest_hash=profile_embedding_manifest_hash,
                    pgvector_version=pgvector_version,
                    postgres_major=postgres_major,
                    spec=resolved_spec,
                )
                physical_instance_id = (
                    secrets.token_hex(32)
                    if adoptable is None
                    else str(adoptable["physical_instance_id"])
                )
            else:
                adoptable = None
                profile_embedding_manifest_hash = str(binding_values[0])
                physical_instance_id = str(binding_values[1])

            params = _index_params(
                profile_id=expected_profile.profile_id,
                dimensions=expected_profile.dimensions,
                profile_embedding_count=profile_embedding_count,
                profile_embedding_manifest_hash=profile_embedding_manifest_hash,
                physical_instance_id=physical_instance_id,
                pgvector_version=pgvector_version,
                postgres_major=postgres_major,
                spec=resolved_spec,
            )
            params_hash = sha256_json(params)
            physical_name = str(params["physical_index_name"])
            build_id = _build_id(
                snapshot_id=filters.snapshot_id,
                profile_id=filters.profile_id,
                params_hash=params_hash,
            )
            if not _INDEX_NAME_PATTERN.fullmatch(physical_name):
                raise AnnIndexError("generated HNSW index name is unsafe")
            if not binding_is_empty and (
                binding_values[2] != physical_name or binding_values[3] != params_hash
            ):
                raise AnnIndexError(
                    "this profile generation is already bound to a different HNSW "
                    "specification"
                )
            if binding_is_empty:
                self._retire_stale_boundary_indexes(
                    connection,
                    profile_id=filters.profile_id,
                    dimensions=profile.dimensions,
                    keep_name=physical_name if adoptable is not None else None,
                )
            physical_reused = not binding_is_empty or adoptable is not None

            existing = (
                connection.execute(
                    select(index_builds).where(index_builds.c.build_id == build_id)
                )
                .mappings()
                .one_or_none()
            )
            index_definition = self._index_definition(connection, physical_name)
            if existing is not None:
                self._validate_existing_build(
                    existing,
                    snapshot_id=filters.snapshot_id,
                    profile_id=filters.profile_id,
                    params=params,
                    params_hash=params_hash,
                )
                self._validate_index_definition(
                    index_definition,
                    physical_name=physical_name,
                    profile_id=filters.profile_id,
                    dimensions=profile.dimensions,
                    index_params=params,
                )
                self._validate_singleton_boundary_index(
                    connection,
                    profile_id=filters.profile_id,
                    dimensions=profile.dimensions,
                    expected_name=physical_name,
                )
                if binding_is_empty:
                    self._bind_generation(
                        connection,
                        profile_id=filters.profile_id,
                        profile_embedding_count=profile_embedding_count,
                        profile_embedding_manifest_hash=(
                            profile_embedding_manifest_hash
                        ),
                        physical_instance_id=physical_instance_id,
                        physical_name=physical_name,
                        params_hash=params_hash,
                    )
                return AnnIndexBuild(
                    build_id=build_id,
                    snapshot_id=filters.snapshot_id,
                    profile_id=filters.profile_id,
                    dimensions=profile.dimensions,
                    profile_embedding_count=profile_embedding_count,
                    profile_embedding_manifest_hash=profile_embedding_manifest_hash,
                    physical_instance_id=physical_instance_id,
                    pgvector_version=pgvector_version,
                    postgres_major=postgres_major,
                    physical_index_name=physical_name,
                    index_params_hash=params_hash,
                    status=str(existing["status"]),  # type: ignore[arg-type]
                    reused=True,
                )

            connection.execute(
                insert(index_builds).values(
                    build_id=build_id,
                    snapshot_id=filters.snapshot_id,
                    profile_id=filters.profile_id,
                    index_params=params,
                    index_params_hash=params_hash,
                    status="building",
                )
            )
            if index_definition is None:
                if not binding_is_empty:
                    raise AnnIndexError(
                        "bound HNSW physical instance is missing and cannot be "
                        "silently rebuilt with a different graph"
                    )
                connection.exec_driver_sql(
                    self._create_index_sql(
                        physical_name=physical_name,
                        profile_id=filters.profile_id,
                        dimensions=profile.dimensions,
                        spec=resolved_spec,
                    )
                )
                index_definition = self._index_definition(connection, physical_name)
            self._validate_index_definition(
                index_definition,
                physical_name=physical_name,
                profile_id=filters.profile_id,
                dimensions=profile.dimensions,
                index_params=params,
            )
            self._validate_singleton_boundary_index(
                connection,
                profile_id=filters.profile_id,
                dimensions=profile.dimensions,
                expected_name=physical_name,
            )
            if binding_is_empty:
                self._bind_generation(
                    connection,
                    profile_id=filters.profile_id,
                    profile_embedding_count=profile_embedding_count,
                    profile_embedding_manifest_hash=profile_embedding_manifest_hash,
                    physical_instance_id=physical_instance_id,
                    physical_name=physical_name,
                    params_hash=params_hash,
                )
            connection.execute(
                update(index_builds)
                .where(index_builds.c.build_id == build_id)
                .values(status="validated", completed_at=func.now())
            )
        return AnnIndexBuild(
            build_id=build_id,
            snapshot_id=filters.snapshot_id,
            profile_id=filters.profile_id,
            dimensions=expected_profile.dimensions,
            profile_embedding_count=profile_embedding_count,
            profile_embedding_manifest_hash=profile_embedding_manifest_hash,
            physical_instance_id=physical_instance_id,
            pgvector_version=pgvector_version,
            postgres_major=postgres_major,
            physical_index_name=physical_name,
            index_params_hash=params_hash,
            status="validated",
            reused=physical_reused,
        )

    @staticmethod
    def _create_index_sql(
        *,
        physical_name: str,
        profile_id: str,
        dimensions: int,
        spec: HnswIndexSpec,
    ) -> str:
        if not _INDEX_NAME_PATTERN.fullmatch(physical_name):
            raise AnnIndexError("HNSW index name is unsafe")
        if not _PROFILE_ID_PATTERN.fullmatch(profile_id):
            raise AnnIndexError("HNSW profile predicate is unsafe")
        _strict_int(
            dimensions,
            label="dimensions",
            minimum=1,
            maximum=HNSW_VECTOR_MAX_DIMENSIONS,
        )
        return (
            f'CREATE INDEX "{physical_name}" ON chunk_embeddings '
            f"USING hnsw ((embedding::vector({dimensions})) vector_ip_ops) "
            f"WITH (m = {spec.m}, ef_construction = {spec.ef_construction}) "
            f"WHERE profile_id = '{profile_id}' "
            f"AND embedding_dimension = {dimensions}"
        )

    @staticmethod
    def _index_definition(connection, physical_name: str) -> Mapping[str, Any] | None:
        return (
            connection.execute(
                text(
                    "SELECT i.indisvalid, i.indisready, idx.reloptions, am.amname, "
                    "i.indisunique, i.indnkeyatts, i.indnatts, "
                    "i.indkey::text AS indkey, "
                    "pg_get_indexdef(i.indexrelid) AS indexdef, "
                    "pg_get_expr(i.indexprs, i.indrelid) AS index_expression, "
                    "pg_get_expr(i.indpred, i.indrelid) AS predicate, "
                    "ARRAY(SELECT opc.opcname "
                    "FROM unnest(i.indclass::oid[]) WITH ORDINALITY "
                    "AS classes(opclass_oid, ordinal) "
                    "JOIN pg_opclass AS opc ON opc.oid = classes.opclass_oid "
                    "ORDER BY classes.ordinal) AS operator_classes "
                    "FROM pg_index AS i "
                    "JOIN pg_class AS idx ON idx.oid = i.indexrelid "
                    "JOIN pg_class AS tbl ON tbl.oid = i.indrelid "
                    "JOIN pg_namespace AS ns ON ns.oid = idx.relnamespace "
                    "JOIN pg_am AS am ON am.oid = idx.relam "
                    "WHERE ns.nspname = current_schema() "
                    "AND idx.relname = :index_name "
                    "AND tbl.relname = 'chunk_embeddings'"
                ),
                {"index_name": physical_name},
            )
            .mappings()
            .one_or_none()
        )

    @staticmethod
    def _validate_index_definition(
        definition: Mapping[str, Any] | None,
        *,
        physical_name: str,
        profile_id: str,
        dimensions: int,
        index_params: Mapping[str, Any],
    ) -> None:
        if definition is None:
            raise AnnIndexError(
                f"validated HNSW build is missing physical index {physical_name!r}"
            )
        normalized = " ".join(str(definition["indexdef"]).lower().split())
        expression = re.sub(
            r'[\s()"]+', "", str(definition["index_expression"]).lower()
        )
        predicate = re.sub(
            r"::(?:text|charactervarying)",
            "",
            re.sub(r'[\s()"]+', "", str(definition["predicate"]).lower()),
        )
        expected_predicates = {
            f"profile_id='{profile_id}'andembedding_dimension={dimensions}",
            f"embedding_dimension={dimensions}andprofile_id='{profile_id}'",
        }
        reloptions = set(definition.get("reloptions") or ())
        if (
            type(index_params.get("m")) is not int
            or type(index_params.get("ef_construction")) is not int
        ):
            raise AnnIndexError("HNSW build receipt contains invalid index parameters")
        expected_reloptions = {
            f"m={index_params.get('m')}",
            f"ef_construction={index_params.get('ef_construction')}",
        }
        if (
            definition["indisvalid"] is not True
            or definition["indisready"] is not True
            or definition["indisunique"] is not False
            or int(definition["indnkeyatts"]) != 1
            or int(definition["indnatts"]) != 1
            or str(definition["indkey"]) != "0"
            or str(definition["amname"]) != "hnsw"
            or list(definition["operator_classes"] or ()) != ["vector_ip_ops"]
            or "vector_ip_ops" not in normalized
            or f"vector({dimensions})" not in normalized
            or expression != f"embedding::vector{dimensions}"
            or predicate not in expected_predicates
            or expected_reloptions != reloptions
        ):
            raise AnnIndexError(
                f"physical index {physical_name!r} does not match its build receipt"
            )

    @staticmethod
    def _validate_existing_build(
        row: Mapping[str, Any],
        *,
        snapshot_id: str,
        profile_id: str,
        params: Mapping[str, Any],
        params_hash: str,
    ) -> None:
        if (
            row["snapshot_id"] != snapshot_id
            or row["profile_id"] != profile_id
            or row["index_params_hash"] != params_hash
            or canonical_json(row["index_params"]) != canonical_json(params)
            or row["status"] not in {"validated", "active"}
            or row["completed_at"] is None
        ):
            raise AnnIndexError(
                "existing HNSW build receipt is incomplete or inconsistent"
            )

    @classmethod
    def _adoptable_physical_params(
        cls,
        connection,
        *,
        profile_id: str,
        dimensions: int,
        profile_embedding_count: int,
        profile_embedding_manifest_hash: str,
        pgvector_version: str,
        postgres_major: int,
        spec: HnswIndexSpec,
    ) -> Mapping[str, Any] | None:
        requested_spec_hash = _physical_identity(
            profile_id=profile_id,
            dimensions=dimensions,
            profile_embedding_count=profile_embedding_count,
            profile_embedding_manifest_hash=profile_embedding_manifest_hash,
            pgvector_version=pgvector_version,
            postgres_major=postgres_major,
            spec=spec,
        )
        rows = (
            connection.execute(
                select(index_builds).where(
                    index_builds.c.profile_id == profile_id,
                    index_builds.c.status.in_({"validated", "active"}),
                    index_builds.c.index_params["physical_scope"].as_string()
                    == "profile_generation",
                )
            )
            .mappings()
            .all()
        )
        candidates: dict[str, Mapping[str, Any]] = {}
        for row in rows:
            params = _canonical_receipt_params(row)
            if (
                params["dimensions"] != dimensions
                or params["profile_embedding_count"] != profile_embedding_count
                or params["profile_embedding_manifest_hash"]
                != profile_embedding_manifest_hash
                or params["pgvector_version"] != pgvector_version
                or params["postgres_major"] != postgres_major
            ):
                continue
            if params["build_spec_hash"] != requested_spec_hash:
                raise AnnIndexError(
                    "this profile generation already has a validated different "
                    "HNSW specification"
                )
            candidates[str(params["physical_instance_id"])] = params
        if not candidates:
            return None
        if len(candidates) != 1:
            raise AnnIndexError(
                "multiple physical HNSW instances claim the same build specification"
            )
        params = next(iter(candidates.values()))
        physical_name = str(params["physical_index_name"])
        cls._validate_index_definition(
            cls._index_definition(connection, physical_name),
            physical_name=physical_name,
            profile_id=profile_id,
            dimensions=dimensions,
            index_params=params,
        )
        return params

    @staticmethod
    def _bind_generation(
        connection,
        *,
        profile_id: str,
        profile_embedding_count: int,
        profile_embedding_manifest_hash: str,
        physical_instance_id: str,
        physical_name: str,
        params_hash: str,
    ) -> None:
        try:
            connection.execute(
                text(
                    "SELECT legal_rag_bind_embedding_profile_generation("
                    ":profile_id, :embedding_count, :manifest_hash, "
                    ":physical_instance_id, :physical_name, :params_hash)"
                ),
                {
                    "profile_id": profile_id,
                    "embedding_count": profile_embedding_count,
                    "manifest_hash": profile_embedding_manifest_hash,
                    "physical_instance_id": physical_instance_id,
                    "physical_name": physical_name,
                    "params_hash": params_hash,
                },
            )
        except DBAPIError as exc:
            raise AnnIndexError(
                "embedding generation changed or failed canonical HNSW binding"
            ) from exc

    @staticmethod
    def _all_hnsw_index_definitions(connection) -> list[Mapping[str, Any]]:
        return (
            connection.execute(
                text(
                    "SELECT idx.relname AS index_name, i.indisvalid, "
                    "i.indisready, idx.reloptions, am.amname, i.indisunique, "
                    "i.indnkeyatts, i.indnatts, i.indkey::text AS indkey, "
                    "pg_get_indexdef(i.indexrelid) AS indexdef, "
                    "pg_get_expr(i.indexprs, i.indrelid) AS index_expression, "
                    "pg_get_expr(i.indpred, i.indrelid) AS predicate, "
                    "ARRAY(SELECT opc.opcname "
                    "FROM unnest(i.indclass::oid[]) WITH ORDINALITY "
                    "AS classes(opclass_oid, ordinal) "
                    "JOIN pg_opclass AS opc ON opc.oid = classes.opclass_oid "
                    "ORDER BY classes.ordinal) AS operator_classes "
                    "FROM pg_index AS i "
                    "JOIN pg_class AS idx ON idx.oid = i.indexrelid "
                    "JOIN pg_class AS tbl ON tbl.oid = i.indrelid "
                    "JOIN pg_namespace AS ns ON ns.oid = idx.relnamespace "
                    "JOIN pg_am AS am ON am.oid = idx.relam "
                    "WHERE ns.nspname = current_schema() "
                    "AND tbl.relname = 'chunk_embeddings' "
                    "AND am.amname = 'hnsw'"
                )
            )
            .mappings()
            .all()
        )

    @staticmethod
    def _definition_targets_boundary(
        definition: Mapping[str, Any],
        *,
        profile_id: str,
        dimensions: int,
    ) -> bool:
        expression = re.sub(
            r'[\s()"]+', "", str(definition["index_expression"]).lower()
        )
        predicate = re.sub(
            r"::(?:text|charactervarying)",
            "",
            re.sub(r'[\s()"]+', "", str(definition["predicate"]).lower()),
        )
        return (
            expression == f"embedding::vector{dimensions}"
            and predicate
            in {
                f"profile_id='{profile_id}'andembedding_dimension={dimensions}",
                f"embedding_dimension={dimensions}andprofile_id='{profile_id}'",
            }
            and list(definition["operator_classes"] or ()) == ["vector_ip_ops"]
        )

    @classmethod
    def _matching_hnsw_index_names(
        cls,
        connection,
        *,
        profile_id: str,
        dimensions: int,
    ) -> set[str]:
        return {
            str(definition["index_name"])
            for definition in cls._all_hnsw_index_definitions(connection)
            if cls._definition_targets_boundary(
                definition,
                profile_id=profile_id,
                dimensions=dimensions,
            )
        }

    @classmethod
    def _validate_singleton_boundary_index(
        cls,
        connection,
        *,
        profile_id: str,
        dimensions: int,
        expected_name: str,
    ) -> None:
        matching = cls._matching_hnsw_index_names(
            connection,
            profile_id=profile_id,
            dimensions=dimensions,
        )
        if matching != {expected_name}:
            raise AnnIndexError(
                "HNSW boundary must have exactly one validated physical index; "
                f"expected {expected_name!r}, found {sorted(matching)!r}"
            )

    @classmethod
    def _retire_stale_boundary_indexes(
        cls,
        connection,
        *,
        profile_id: str,
        dimensions: int,
        keep_name: str | None,
    ) -> None:
        rows = (
            connection.execute(
                select(index_builds).where(
                    index_builds.c.profile_id == profile_id,
                    index_builds.c.status.in_({"validated", "active"}),
                    index_builds.c.index_params["physical_scope"].as_string()
                    == "profile_generation",
                )
            )
            .mappings()
            .all()
        )
        params_by_name: dict[str, Mapping[str, Any]] = {}
        build_ids_to_archive: list[str] = []
        for row in rows:
            params = _canonical_receipt_params(row)
            if params["dimensions"] != dimensions:
                continue
            name = str(params["physical_index_name"])
            if name == keep_name:
                continue
            previous = params_by_name.setdefault(name, params)
            if canonical_json(previous) != canonical_json(params):
                raise AnnIndexError(
                    "conflicting HNSW receipts refer to one physical index name"
                )
            build_ids_to_archive.append(str(row["build_id"]))

        matching_names = cls._matching_hnsw_index_names(
            connection,
            profile_id=profile_id,
            dimensions=dimensions,
        )
        drop_names = matching_names - ({keep_name} if keep_name is not None else set())
        for name in sorted(drop_names):
            if not re.fullmatch(r"ix_ce_hnsw_ip_[0-9a-f]{32}", name):
                raise AnnIndexError(
                    f"unexpected HNSW index {name!r} targets the profile boundary"
                )
            params = params_by_name.get(name)
            if params is None:
                raise AnnIndexError(
                    f"HNSW index {name!r} has no canonical build receipt"
                )
            cls._validate_index_definition(
                cls._index_definition(connection, name),
                physical_name=name,
                profile_id=profile_id,
                dimensions=dimensions,
                index_params=params,
            )

        if build_ids_to_archive:
            connection.execute(
                update(index_builds)
                .where(index_builds.c.build_id.in_(build_ids_to_archive))
                .values(status="archived")
            )
        for name in sorted(drop_names):
            connection.exec_driver_sql(f'DROP INDEX "{name}"')


class PostgresAnnRetrievalRepository:
    """Explicit HNSW retrieval with explainable underfill and exact fallback."""

    def __init__(self, engine: Engine) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("PostgresAnnRetrievalRepository requires PostgreSQL")
        self.engine = engine
        self._exact = PostgresExactRetrievalRepository(engine)

    def validate_context(
        self,
        filters: RetrievalFilters,
        *,
        expected_profile: EmbeddingProfileIdentity,
        build: AnnIndexBuild,
    ) -> EmbeddingProfileIdentity:
        with self.engine.connect() as connection:
            profile = self._exact._validated_context(
                connection, filters, expected_profile=expected_profile
            )
            self._validated_build(
                connection,
                filters=filters,
                expected_profile=profile,
                build=build,
            )
            return profile

    def search_vector(
        self,
        query_vector: Any,
        *,
        top_k: int,
        filters: RetrievalFilters,
        expected_profile: EmbeddingProfileIdentity,
        build: AnnIndexBuild,
        policy: AnnSearchPolicy | None = None,
    ) -> AnnSearchOutcome:
        resolved_top_k = _validate_top_k(top_k)
        resolved_policy = AnnSearchPolicy() if policy is None else policy
        if not isinstance(resolved_policy, AnnSearchPolicy):
            raise TypeError("policy must be an AnnSearchPolicy")
        if filters.profile_id != expected_profile.profile_id:
            raise RetrievalContractError(
                "expected embedding profile does not match bound profile_id"
            )
        vector = _canonical_query_vector(query_vector, expected_profile)

        with self.engine.connect() as connection:
            profile = self._exact._validated_context(
                connection, filters, expected_profile=expected_profile
            )
            # Keep the validated profile generation and the HNSW scan in one
            # stable rowset. Importers acquire ROW EXCLUSIVE for inserts and
            # therefore wait until this short read transaction completes.
            connection.exec_driver_sql("LOCK TABLE chunk_embeddings IN SHARE MODE")
            self._validated_build(
                connection,
                filters=filters,
                expected_profile=profile,
                build=build,
            )
            if filters.denies_all:
                rows: Sequence[Mapping[str, Any]] = ()
                relation_rows: Mapping[str, Sequence[Mapping[str, Any]]] = {}
                plan_index_names: tuple[str, ...] = ()
            else:
                self._set_query_policy(connection, resolved_policy)
                statement = self._search_statement(
                    vector=vector.tolist(),
                    top_k=resolved_top_k,
                    filters=filters,
                    dimensions=profile.dimensions,
                )
                plan_index_names = self._validated_query_plan_index_names(
                    connection,
                    statement,
                    expected_name=build.physical_index_name,
                )
                rows = connection.execute(statement).mappings().all()
                relation_rows = self._exact._load_relations(
                    connection, [str(row["chunk_id"]) for row in rows]
                )

        ann_results = self._hydrate_ann_results(
            rows,
            relation_rows=relation_rows,
            filters=filters,
            profile=profile,
            build=build,
            policy=resolved_policy,
            plan_index_names=plan_index_names,
        )
        ann_count = len(ann_results)
        if filters.denies_all:
            return AnnSearchOutcome(
                results=(),
                requested_top_k=resolved_top_k,
                ann_returned_count=0,
                eligible_count=0,
                exact_returned_count=None,
                status="empty_boundary",
                underfill_reason="boundary_denies_all",
                exact_fallback_used=False,
                build_id=build.build_id,
                physical_index_name=build.physical_index_name,
            )
        if ann_count == resolved_top_k:
            return AnnSearchOutcome(
                results=tuple(
                    replace(
                        item,
                        trace={
                            **item.trace,
                            "ann_outcome_status": "ann_complete",
                            "requested_top_k": resolved_top_k,
                            "ann_returned_count": ann_count,
                            "ann_eligible_count": None,
                        },
                    )
                    for item in ann_results
                ),
                requested_top_k=resolved_top_k,
                ann_returned_count=ann_count,
                eligible_count=None,
                exact_returned_count=None,
                status="ann_complete",
                underfill_reason=None,
                exact_fallback_used=False,
                build_id=build.build_id,
                physical_index_name=build.physical_index_name,
            )

        reason = "ann_underfill_unclassified"
        if resolved_policy.exact_fallback_on_underfill:
            try:
                exact_results = self._exact.search_vector(
                    vector,
                    top_k=resolved_top_k,
                    filters=filters,
                    expected_profile=expected_profile,
                    statement_timeout_ms=resolved_policy.exact_fallback_timeout_ms,
                )
            except DBAPIError as exc:
                sqlstate = getattr(exc.orig, "sqlstate", None)
                if sqlstate != "57014":
                    raise
                timed_out_results = tuple(
                    replace(
                        item,
                        trace={
                            **item.trace,
                            "ann_outcome_status": "exact_fallback_timed_out",
                            "requested_top_k": resolved_top_k,
                            "ann_returned_count": ann_count,
                            "ann_eligible_count": None,
                            "ann_initial_underfill_reason": reason,
                            "ann_underfill_reason": "exact_fallback_statement_timeout",
                        },
                    )
                    for item in ann_results
                )
                return AnnSearchOutcome(
                    results=timed_out_results,
                    requested_top_k=resolved_top_k,
                    ann_returned_count=ann_count,
                    eligible_count=None,
                    exact_returned_count=None,
                    status="exact_fallback_timed_out",
                    underfill_reason="exact_fallback_statement_timeout",
                    exact_fallback_used=True,
                    build_id=build.build_id,
                    physical_index_name=build.physical_index_name,
                )
            exact_count = len(exact_results)
            fallback_status = (
                "exact_fallback_complete"
                if exact_count == resolved_top_k
                else "exact_fallback_exhausted"
            )
            reason = (
                "ann_scan_budget_exhausted"
                if exact_count == resolved_top_k
                else "eligible_population_below_top_k"
            )
            eligible_count = (
                exact_count if fallback_status == "exact_fallback_exhausted" else None
            )
            fallback_results = [
                replace(
                    item,
                    retriever="pgvector_hnsw_exact_fallback",
                    trace={
                        **item.trace,
                        "search_mode": "exact_fallback",
                        "ann_algorithm": "hnsw",
                        "ann_build_id": build.build_id,
                        "ann_validated_physical_index_name": (
                            build.physical_index_name
                        ),
                        "ann_physical_instance_id": build.physical_instance_id,
                        "ann_plan_verified": True,
                        "ann_plan_index_names": list(plan_index_names),
                        "ann_physical_scope": "profile_generation",
                        "ann_profile_embedding_count": build.profile_embedding_count,
                        "ann_profile_embedding_manifest_hash": (
                            build.profile_embedding_manifest_hash
                        ),
                        "ann_pgvector_version": build.pgvector_version,
                        "ann_postgres_major": build.postgres_major,
                        "ann_returned_count": ann_count,
                        "ann_eligible_count": eligible_count,
                        "ann_underfill_reason": reason,
                        "ann_policy": resolved_policy.trace_payload(),
                        "ann_outcome_status": fallback_status,
                        "requested_top_k": resolved_top_k,
                        "exact_returned_count": exact_count,
                    },
                )
                for item in exact_results
            ]
            return AnnSearchOutcome(
                results=tuple(fallback_results),
                requested_top_k=resolved_top_k,
                ann_returned_count=ann_count,
                eligible_count=eligible_count,
                exact_returned_count=exact_count,
                status=fallback_status,
                underfill_reason=reason,
                exact_fallback_used=True,
                build_id=build.build_id,
                physical_index_name=build.physical_index_name,
            )
        return AnnSearchOutcome(
            results=tuple(
                replace(
                    item,
                    trace={
                        **item.trace,
                        "ann_returned_count": ann_count,
                        "ann_eligible_count": None,
                        "ann_underfill_reason": reason,
                        "ann_outcome_status": "ann_underfilled",
                        "requested_top_k": resolved_top_k,
                    },
                )
                for item in ann_results
            ),
            requested_top_k=resolved_top_k,
            ann_returned_count=ann_count,
            eligible_count=None,
            exact_returned_count=None,
            status="ann_underfilled",
            underfill_reason=reason,
            exact_fallback_used=False,
            build_id=build.build_id,
            physical_index_name=build.physical_index_name,
        )

    def _validated_build(
        self,
        connection,
        *,
        filters: RetrievalFilters,
        expected_profile: EmbeddingProfileIdentity,
        build: AnnIndexBuild,
    ) -> None:
        extension_version = connection.scalar(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
        if not isinstance(extension_version, str):
            raise AnnIndexUnsupportedError("the pgvector extension is not installed")
        if _parse_version(extension_version) < MIN_ITERATIVE_SCAN_PGVECTOR_VERSION:
            raise AnnIndexUnsupportedError(
                "pgvector 0.8.0 or newer is required for bounded iterative "
                "filtered HNSW scans"
            )
        pgvector_version = ".".join(
            str(part) for part in _parse_version(extension_version)
        )
        server_version_num = connection.scalar(text("SHOW server_version_num"))
        try:
            postgres_major = int(str(server_version_num)) // 10_000
        except (TypeError, ValueError) as exc:
            raise AnnIndexUnsupportedError(
                "cannot determine the PostgreSQL server major version"
            ) from exc
        if not isinstance(build, AnnIndexBuild):
            raise TypeError("build must be an AnnIndexBuild")
        if (
            build.snapshot_id != filters.snapshot_id
            or build.profile_id != filters.profile_id
            or build.profile_id != expected_profile.profile_id
            or build.dimensions != expected_profile.dimensions
            or build.status not in {"validated", "active"}
        ):
            raise AnnIndexError(
                "HNSW build is not bound to the requested snapshot/profile"
            )
        generation = (
            connection.execute(
                select(embedding_profile_generations).where(
                    embedding_profile_generations.c.profile_id == build.profile_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if generation is None:
            raise AnnIndexError("HNSW profile generation binding is missing")
        if generation["embedding_count"] != build.profile_embedding_count:
            raise AnnIndexError(
                "HNSW profile generation changed after validation; build a new "
                "experimental index receipt before ANN retrieval"
            )
        if (
            generation["embedding_manifest_hash"]
            != build.profile_embedding_manifest_hash
            or generation["ann_physical_instance_id"] != build.physical_instance_id
            or generation["ann_physical_index_name"] != build.physical_index_name
            or generation["ann_index_params_hash"] != build.index_params_hash
        ):
            raise AnnIndexError("HNSW profile generation binding is inconsistent")
        row = (
            connection.execute(
                select(index_builds).where(index_builds.c.build_id == build.build_id)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise AnnIndexError("HNSW build receipt is missing or inconsistent")
        params = _canonical_receipt_params(row)
        if (
            row["snapshot_id"] != build.snapshot_id
            or row["profile_id"] != build.profile_id
            or row["index_params_hash"] != build.index_params_hash
            or row["status"] != build.status
            or row["completed_at"] is None
            or params["dimensions"] != build.dimensions
            or params["profile_embedding_count"] != build.profile_embedding_count
            or params["profile_embedding_manifest_hash"]
            != build.profile_embedding_manifest_hash
            or params["physical_instance_id"] != build.physical_instance_id
            or params["physical_index_name"] != build.physical_index_name
            or params["pgvector_version"] != build.pgvector_version
            or params["postgres_major"] != build.postgres_major
            or build.pgvector_version != pgvector_version
            or build.postgres_major != postgres_major
        ):
            raise AnnIndexError("HNSW build receipt is missing or inconsistent")
        definition = PostgresHnswIndexManager._index_definition(
            connection, build.physical_index_name
        )
        PostgresHnswIndexManager._validate_index_definition(
            definition,
            physical_name=build.physical_index_name,
            profile_id=build.profile_id,
            dimensions=build.dimensions,
            index_params=params,
        )
        PostgresHnswIndexManager._validate_singleton_boundary_index(
            connection,
            profile_id=build.profile_id,
            dimensions=build.dimensions,
            expected_name=build.physical_index_name,
        )

    @staticmethod
    def _validated_query_plan_index_names(
        connection,
        statement,
        *,
        expected_name: str,
    ) -> tuple[str, ...]:
        plan = connection.scalar(_Explain(statement))
        names = tuple(sorted(_collect_plan_index_names(plan)))
        if expected_name not in names:
            raise AnnIndexError(
                "PostgreSQL did not plan the validated HNSW physical index; "
                f"expected {expected_name!r}, planned indexes were {list(names)!r}"
            )
        return names

    @staticmethod
    def _set_query_policy(connection, policy: AnnSearchPolicy) -> None:
        # All values are closed enums or range-checked integers before they
        # reach these statements. SET LOCAL prevents connection-pool leakage.
        connection.exec_driver_sql(
            f"SET LOCAL hnsw.iterative_scan = '{policy.iterative_scan}'"
        )
        connection.exec_driver_sql(f"SET LOCAL hnsw.ef_search = {policy.ef_search}")
        connection.exec_driver_sql(
            f"SET LOCAL hnsw.max_scan_tuples = {policy.max_scan_tuples}"
        )
        connection.exec_driver_sql(
            f"SET LOCAL hnsw.scan_mem_multiplier = {policy.scan_mem_multiplier}"
        )
        connection.exec_driver_sql("SET LOCAL plan_cache_mode = force_custom_plan")
        # The explicit ANN path should exercise the matching expression index,
        # even for the deliberately tiny deterministic integration fixtures.
        connection.exec_driver_sql("SET LOCAL enable_seqscan = off")

    def _search_statement(
        self,
        *,
        vector: list[float],
        top_k: int,
        filters: RetrievalFilters,
        dimensions: int,
    ):
        profile_literal = literal_column(f"'{filters.profile_id}'")
        indexed_embedding = cast(chunk_embeddings.c.embedding, VECTOR(dimensions))
        distance = indexed_embedding.max_inner_product(vector).label("distance")
        snapshot_boundary = exists(
            select(1)
            .select_from(
                snapshot_chunks.join(
                    corpus_snapshots,
                    corpus_snapshots.c.snapshot_id == snapshot_chunks.c.snapshot_id,
                ).join(
                    embedding_imports,
                    and_(
                        embedding_imports.c.snapshot_id
                        == snapshot_chunks.c.snapshot_id,
                        embedding_imports.c.profile_id == chunk_embeddings.c.profile_id,
                    ),
                )
            )
            .where(
                snapshot_chunks.c.chunk_id == chunk_embeddings.c.chunk_id,
                snapshot_chunks.c.snapshot_id == filters.snapshot_id,
                corpus_snapshots.c.scope_id == filters.scope_id,
                corpus_snapshots.c.status.in_({"validated", "active"}),
                embedding_imports.c.status == "validated",
            )
        )
        conditions = [
            chunk_embeddings.c.profile_id == profile_literal,
            chunk_embeddings.c.embedding_dimension == dimensions,
            snapshot_boundary,
        ]
        relation_filter = self._exact._relation_filter(
            filters,
            chunk_id_column=chunk_embeddings.c.chunk_id,
        )
        if relation_filter is not None:
            conditions.append(relation_filter)
        candidates = (
            select(
                chunk_embeddings.c.chunk_id.label("candidate_chunk_id"),
                chunk_embeddings.c.embedding.label("candidate_embedding"),
                chunk_embeddings.c.embedding_hash.label("candidate_embedding_hash"),
                distance,
            )
            .select_from(chunk_embeddings)
            .where(*conditions)
            .order_by(distance.asc())
            .limit(top_k)
            .cte("ann_candidates")
            .prefix_with("MATERIALIZED", dialect="postgresql")
        )
        from_clause = candidates.join(
            chunks,
            chunks.c.chunk_id == candidates.c.candidate_chunk_id,
        ).join(
            snapshot_chunks,
            and_(
                snapshot_chunks.c.chunk_id == candidates.c.candidate_chunk_id,
                snapshot_chunks.c.snapshot_id == filters.snapshot_id,
            ),
        )
        return (
            select(
                candidates.c.candidate_chunk_id.label("chunk_id"),
                chunks.c.text,
                chunks.c.strategy,
                chunks.c.metadata.label("chunk_metadata"),
                chunks.c.content_hash.label("chunk_content_hash"),
                chunks.c.recipe_hash.label("chunk_recipe_hash"),
                snapshot_chunks.c.ordinal.label("snapshot_ordinal"),
                candidates.c.candidate_embedding.label("embedding"),
                candidates.c.candidate_embedding_hash.label("embedding_hash"),
                candidates.c.distance.label("distance"),
            )
            .select_from(from_clause)
            .order_by(
                candidates.c.distance.asc(),
                snapshot_chunks.c.ordinal.asc(),
                chunks.c.chunk_id.asc(),
            )
        )

    def _hydrate_ann_results(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        relation_rows: Mapping[str, Sequence[Mapping[str, Any]]],
        filters: RetrievalFilters,
        profile: EmbeddingProfileIdentity,
        build: AnnIndexBuild,
        policy: AnnSearchPolicy,
        plan_index_names: tuple[str, ...],
    ) -> list[SearchResult]:
        results: list[SearchResult] = []
        for rank, row in enumerate(rows, start=1):
            chunk_id = str(row["chunk_id"])
            relations = relation_rows.get(chunk_id)
            if not relations:
                raise RetrievalDataError(
                    f"chunk {chunk_id!r} has no persisted article relations"
                )
            distance_value = float(row["distance"])
            score = -distance_value
            if score == 0.0:
                score = 0.0
            chunk = self._exact._hydrate_chunk(row, relations, filters)
            provenance = self._exact._build_provenance(
                row, relations, filters, chunk, profile
            )
            results.append(
                SearchResult(
                    chunk=chunk,
                    score=score,
                    rank=rank,
                    retriever="pgvector_hnsw",
                    trace={
                        "score_kind": "inner_product",
                        "raw_score": score,
                        "pgvector_distance": distance_value,
                        "pgvector_distance_kind": "negative_inner_product",
                        "pgvector_operator": "<#>",
                        "search_mode": "ann",
                        "ann_algorithm": "hnsw",
                        "ann_build_id": build.build_id,
                        "ann_validated_physical_index_name": (
                            build.physical_index_name
                        ),
                        "ann_physical_instance_id": build.physical_instance_id,
                        "ann_plan_verified": True,
                        "ann_plan_index_names": list(plan_index_names),
                        "ann_physical_scope": "profile_generation",
                        "ann_profile_embedding_count": build.profile_embedding_count,
                        "ann_profile_embedding_manifest_hash": (
                            build.profile_embedding_manifest_hash
                        ),
                        "ann_pgvector_version": build.pgvector_version,
                        "ann_postgres_major": build.postgres_major,
                        "ann_policy": policy.trace_payload(),
                        "snapshot_ordinal": int(row["snapshot_ordinal"]),
                        "tie_break": ["snapshot_ordinal", "chunk_id"],
                        "filters": filters.trace_payload(),
                        "boundary_fingerprint": filters.fingerprint,
                    },
                    provenance=provenance,
                )
            )
        return results


@dataclass(frozen=True, slots=True, init=False)
class PgVectorAnnRetriever:
    """Bind an explicit experimental ANN build to the legacy Retriever API."""

    name = "pgvector_hnsw"
    _repository: PostgresAnnRetrievalRepository
    _encoder: QueryEncoder
    _boundary: RetrievalBoundary
    _profile: EmbeddingProfileIdentity
    _build: AnnIndexBuild
    _policy: AnnSearchPolicy

    def __init__(
        self,
        repository: PostgresAnnRetrievalRepository,
        *,
        encoder: QueryEncoder,
        filters: RetrievalFilters,
        build: AnnIndexBuild,
        policy: AnnSearchPolicy | None = None,
    ) -> None:
        profile = getattr(encoder, "profile", None)
        if not isinstance(profile, EmbeddingProfileIdentity):
            raise RetrievalContractError(
                "query encoder must expose an immutable embedding profile identity"
            )
        resolved_policy = AnnSearchPolicy() if policy is None else policy
        if not isinstance(resolved_policy, AnnSearchPolicy):
            raise TypeError("policy must be an AnnSearchPolicy")
        validated_profile = repository.validate_context(
            filters,
            expected_profile=profile,
            build=build,
        )
        if validated_profile != profile:
            raise RetrievalDataError("validated query encoder profile changed")
        object.__setattr__(self, "_repository", repository)
        object.__setattr__(self, "_encoder", encoder)
        object.__setattr__(self, "_boundary", filters)
        object.__setattr__(self, "_profile", profile)
        object.__setattr__(self, "_build", build)
        object.__setattr__(self, "_policy", resolved_policy)

    @property
    def retrieval_boundary(self) -> RetrievalBoundary:
        return self._boundary

    @property
    def boundary_fingerprint(self) -> str:
        return self._boundary.fingerprint

    @property
    def profile(self) -> EmbeddingProfileIdentity:
        return self._profile

    @property
    def expected_dimension(self) -> int:
        return self._profile.dimensions

    def retrieve_with_outcome(self, query: str, top_k: int = 5) -> AnnSearchOutcome:
        if not isinstance(query, str) or not query.strip():
            raise RetrievalContractError("query must be a non-empty string")
        resolved_top_k = _validate_top_k(top_k)
        if getattr(self._encoder, "profile", None) != self._profile:
            raise RetrievalContractError(
                "query encoder profile changed after retriever construction"
            )
        validated_profile = self._repository.validate_context(
            self._boundary,
            expected_profile=self._profile,
            build=self._build,
        )
        if validated_profile != self._profile:
            raise RetrievalDataError("validated query encoder profile changed")
        try:
            query_vector = self._encoder.encode_query(query)
        except ProviderCallError as exc:
            query = "<redacted>"
            raise_sanitized_provider_error(exc)
        return self._repository.search_vector(
            query_vector,
            top_k=resolved_top_k,
            filters=self._boundary,
            expected_profile=self._profile,
            build=self._build,
            policy=self._policy,
        )

    def retrieve(self, query: str, top_k: int = 5) -> list[SearchResult]:
        outcome = self.retrieve_with_outcome(query, top_k=top_k)
        if outcome.status in {"ann_underfilled", "exact_fallback_timed_out"}:
            raise AnnUnderfillError(outcome)
        return list(outcome.results)
