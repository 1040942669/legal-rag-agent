from __future__ import annotations

from logging.config import fileConfig
import re

from alembic import context
from sqlalchemy import select, text

from legal_rag.storage.ann import AnnIndexError, PostgresHnswIndexManager
from legal_rag.storage.ann import _canonical_receipt_params
from legal_rag.storage.database import DatabaseSettings, create_database_engine
from legal_rag.storage.schema import index_builds, metadata


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata
_RUNTIME_HNSW_INDEX_NAME = re.compile(r"ix_ce_hnsw_ip_[0-9a-f]{32}")
_runtime_hnsw_allowlist: frozenset[str] = frozenset()


def _load_runtime_hnsw_allowlist(connection) -> frozenset[str]:
    """Trust canonical, physically verified, receipt-owned runtime indexes.

    A generation increment invalidates its binding before the old graph can be
    retired by the next managed build. Alembic schema drift is not the garbage
    collector for those data-derived artifacts, so both current and stale
    receipt-owned graphs are ignored here. Retrieval independently rejects
    stale generations and competing physical indexes.
    """

    if (
        connection.scalar(text("SELECT to_regclass('embedding_profile_generations')"))
        is None
    ):
        return frozenset()
    receipts = (
        connection.execute(
            select(index_builds).where(
                index_builds.c.status.in_({"validated", "active", "archived"}),
                index_builds.c.index_params["physical_scope"].as_string()
                == "profile_generation",
            )
        )
        .mappings()
        .all()
    )
    allowlist: set[str] = set()
    params_by_name: dict[str, dict] = {}
    invalid_names: set[str] = set()
    for receipt in receipts:
        try:
            params = _canonical_receipt_params(receipt)
            name = str(params["physical_index_name"])
            previous = params_by_name.setdefault(name, params)
            if previous != params:
                invalid_names.add(name)
                allowlist.discard(name)
                continue
            if name in invalid_names:
                continue
            dimensions = int(params["dimensions"])
            PostgresHnswIndexManager._validate_index_definition(
                PostgresHnswIndexManager._index_definition(connection, name),
                physical_name=name,
                profile_id=str(receipt["profile_id"]),
                dimensions=dimensions,
                index_params=params,
            )
        except (AnnIndexError, TypeError, ValueError):
            continue
        allowlist.add(name)
    return frozenset(allowlist - invalid_names)


def include_object(object_, name, type_, reflected, compare_to) -> bool:
    """Exclude only canonical runtime ANN indexes from schema autogeneration.

    Profile-specific HNSW indexes are data-derived build artifacts, not static
    migration objects. All other unexpected indexes remain visible to
    ``alembic check``.
    """

    dialect_options = getattr(object_, "dialect_options", {})
    postgresql_options = (
        dialect_options.get("postgresql", {}) if hasattr(dialect_options, "get") else {}
    )
    if (
        type_ == "index"
        and reflected
        and compare_to is None
        and isinstance(name, str)
        and name in _runtime_hnsw_allowlist
        and _RUNTIME_HNSW_INDEX_NAME.fullmatch(name)
        and getattr(getattr(object_, "table", None), "name", None) == "chunk_embeddings"
        and postgresql_options.get("using") == "hnsw"
    ):
        return False
    return True


def run_migrations_offline() -> None:
    settings = DatabaseSettings.from_env()
    context.configure(
        url=settings.url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    global _runtime_hnsw_allowlist

    provided_connection = config.attributes.get("connection")
    if provided_connection is not None:
        _runtime_hnsw_allowlist = _load_runtime_hnsw_allowlist(provided_connection)
        context.configure(
            connection=provided_connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = create_database_engine(DatabaseSettings.from_env())
    try:
        with engine.connect() as connection:
            _runtime_hnsw_allowlist = _load_runtime_hnsw_allowlist(connection)
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                include_object=include_object,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
