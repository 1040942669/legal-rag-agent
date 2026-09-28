from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, Protocol

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.postgres.base import BasePostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy import Engine, inspect, text

from .state import HarnessState, validate_harness_state


CHECKPOINT_TABLES = frozenset(
    {"checkpoint_migrations", "checkpoints", "checkpoint_blobs", "checkpoint_writes"}
)
EXPECTED_CHECKPOINT_MIGRATION = len(BasePostgresSaver.MIGRATIONS) - 1


class PersistentCheckpointerRequired(RuntimeError):
    """Durable execution was configured with a process-local saver."""


class CheckpointerNotReady(RuntimeError):
    """The locked PostgreSQL checkpointer schema has not been bootstrapped."""


class CheckpointFence(Protocol):
    def assert_execution_fence(
        self,
        run_id: str,
        worker_id: str,
        lease_epoch: int,
    ) -> None: ...

    def bind_checkpoint(
        self,
        *,
        run_id: str,
        worker_id: str,
        lease_epoch: int,
        checkpoint_namespace: str,
        checkpoint_id: str,
        parent_checkpoint_id: str | None,
        state: HarnessState,
    ) -> None: ...


def strict_checkpoint_serializer() -> JsonPlusSerializer:
    """Allow primitives and LangGraph safe built-ins, never pickle fallback."""

    return JsonPlusSerializer(
        pickle_fallback=False,
        allowed_json_modules=None,
        allowed_msgpack_modules=None,
    )


def postgres_conninfo(engine: Engine) -> str:
    if not isinstance(engine, Engine) or engine.dialect.name != "postgresql":
        raise ValueError("PostgreSQL checkpointer requires a PostgreSQL Engine")
    url = engine.url
    if url.drivername != "postgresql+psycopg":
        raise ValueError("PostgreSQL checkpointer requires the psycopg driver")
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@contextmanager
def checkpoint_pool(
    engine: Engine,
    *,
    min_size: int = 1,
    max_size: int = 4,
) -> Iterator[ConnectionPool[Any]]:
    if type(min_size) is not int or type(max_size) is not int:
        raise ValueError("checkpoint pool sizes must be integers")
    if min_size < 1 or max_size < min_size or max_size > 32:
        raise ValueError("checkpoint pool sizes are invalid")
    pool: ConnectionPool[Any] = ConnectionPool(
        postgres_conninfo(engine),
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
        min_size=min_size,
        max_size=max_size,
        open=True,
        name="legal-rag-m5-checkpoints",
    )
    try:
        pool.wait(timeout=30.0)
        yield pool
    finally:
        pool.close(timeout=5.0)


def setup_postgres_checkpointer(engine: Engine) -> None:
    """Run the saver-owned schema migration as an explicit deployment step."""

    with checkpoint_pool(engine, min_size=1, max_size=1) as pool:
        saver = PostgresSaver(pool, serde=strict_checkpoint_serializer())
        saver.setup()


def assert_postgres_checkpointer_ready(engine: Engine) -> None:
    tables = set(inspect(engine).get_table_names())
    missing = CHECKPOINT_TABLES - tables
    if missing:
        raise CheckpointerNotReady(
            "persistent checkpointer tables are missing: " + ", ".join(sorted(missing))
        )
    with engine.connect() as connection:
        version = connection.scalar(text("SELECT max(v) FROM checkpoint_migrations"))
    if version != EXPECTED_CHECKPOINT_MIGRATION:
        raise CheckpointerNotReady(
            "persistent checkpointer migration does not match the locked package"
        )


def require_persistent_checkpointer(checkpointer: Any) -> None:
    if isinstance(checkpointer, InMemorySaver):
        raise PersistentCheckpointerRequired(
            "InMemorySaver cannot satisfy cross-process recovery"
        )
    if not isinstance(checkpointer, PostgresSaver):
        raise PersistentCheckpointerRequired(
            "durable harness requires the PostgreSQL LangGraph saver"
        )


def checkpoint_namespace(
    *,
    graph_version: str,
    schema_version: int,
    lease_epoch: int,
) -> str:
    if not isinstance(graph_version, str) or not graph_version:
        raise ValueError("graph_version must be non-empty")
    if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-_." for character in graph_version):
        raise ValueError("graph_version is unsafe for a checkpoint namespace")
    if type(schema_version) is not int or schema_version < 1:
        raise ValueError("schema_version must be positive")
    if type(lease_epoch) is not int or lease_epoch < 1:
        raise ValueError("lease_epoch must be positive")
    return f"m5:{graph_version}:{schema_version}:lease-{lease_epoch}"


def checkpoint_thread_id(*, run_id: str, namespace: str) -> str:
    """Return an epoch-isolated LangGraph thread identifier.

    LangGraph reserves the root ``checkpoint_ns`` as an empty string and uses
    non-empty namespaces for nested graphs.  The lease namespace therefore
    lives in the saver thread identifier, while the application checkpoint
    projection retains the explicit logical namespace.
    """

    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id must be non-empty")
    if not isinstance(namespace, str) or not namespace:
        raise ValueError("namespace must be non-empty")
    return f"{run_id}:{namespace}"


class FencedPostgresSaver(PostgresSaver):
    """Project saver writes through a business lease/epoch trust boundary.

    A stale writer can at worst leave an orphan in its old epoch namespace.
    Resume consumes only checkpoints projected by ``CheckpointFence``.
    """

    def __init__(
        self,
        conn: Any,
        *,
        fence: CheckpointFence,
        run_id: str,
        worker_id: str,
        lease_epoch: int,
        namespace: str,
    ) -> None:
        super().__init__(conn, serde=strict_checkpoint_serializer())
        self._fence = fence
        self._run_id = run_id
        self._worker_id = worker_id
        self._lease_epoch = lease_epoch
        self._namespace = namespace
        self._thread_id = checkpoint_thread_id(
            run_id=run_id,
            namespace=namespace,
        )

    @property
    def thread_id(self) -> str:
        return self._thread_id

    def put(
        self,
        config: Mapping[str, Any],
        checkpoint: Mapping[str, Any],
        metadata: Mapping[str, Any],
        new_versions: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._assert_config(config)
        self._fence.assert_execution_fence(
            self._run_id,
            self._worker_id,
            self._lease_epoch,
        )
        saved = super().put(config, checkpoint, metadata, new_versions)
        state = _checkpoint_state(checkpoint)
        if state is not None:
            configurable = dict(saved.get("configurable", {}))
            checkpoint_id = configurable.get("checkpoint_id")
            if not isinstance(checkpoint_id, str) or not checkpoint_id:
                raise RuntimeError("LangGraph saver returned no checkpoint_id")
            source_configurable = dict(config.get("configurable", {}))
            parent_id = source_configurable.get("checkpoint_id")
            if parent_id is not None and not isinstance(parent_id, str):
                raise RuntimeError("LangGraph parent checkpoint_id is invalid")
            self._fence.bind_checkpoint(
                run_id=self._run_id,
                worker_id=self._worker_id,
                lease_epoch=self._lease_epoch,
                checkpoint_namespace=self._namespace,
                checkpoint_id=checkpoint_id,
                parent_checkpoint_id=parent_id,
                state=state,
            )
        return dict(saved)

    def put_writes(
        self,
        config: Mapping[str, Any],
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self._assert_config(config)
        self._fence.assert_execution_fence(
            self._run_id,
            self._worker_id,
            self._lease_epoch,
        )
        super().put_writes(config, writes, task_id, task_path)
        self._fence.assert_execution_fence(
            self._run_id,
            self._worker_id,
            self._lease_epoch,
        )

    def _assert_config(self, config: Mapping[str, Any]) -> None:
        configurable = config.get("configurable")
        if not isinstance(configurable, Mapping):
            raise RuntimeError("LangGraph configurable state is missing")
        if configurable.get("thread_id") != self._thread_id:
            raise RuntimeError("LangGraph thread_id crossed the run boundary")
        if configurable.get("checkpoint_ns", "") != "":
            raise RuntimeError("LangGraph root checkpoint namespace is invalid")


def _checkpoint_state(checkpoint: Mapping[str, Any]) -> HarnessState | None:
    channel_values = checkpoint.get("channel_values")
    if not isinstance(channel_values, Mapping):
        return None
    candidate: Any
    if "payload" in channel_values:
        payload = channel_values["payload"]
        candidate = dict(payload) if isinstance(payload, Mapping) else None
    elif "run_id" in channel_values:
        state_fields = HarnessState.__required_keys__
        if not state_fields.issubset(channel_values):
            return None
        candidate = {field: channel_values[field] for field in state_fields}
    else:
        start = channel_values.get("__start__")
        if isinstance(start, Mapping) and isinstance(start.get("payload"), Mapping):
            candidate = dict(start["payload"])
        else:
            candidate = dict(start) if isinstance(start, Mapping) else None
    if candidate is None:
        return None
    return validate_harness_state(candidate)


__all__ = [
    "CHECKPOINT_TABLES",
    "EXPECTED_CHECKPOINT_MIGRATION",
    "CheckpointFence",
    "CheckpointerNotReady",
    "FencedPostgresSaver",
    "PersistentCheckpointerRequired",
    "assert_postgres_checkpointer_ready",
    "checkpoint_namespace",
    "checkpoint_pool",
    "checkpoint_thread_id",
    "postgres_conninfo",
    "require_persistent_checkpointer",
    "setup_postgres_checkpointer",
    "strict_checkpoint_serializer",
]
