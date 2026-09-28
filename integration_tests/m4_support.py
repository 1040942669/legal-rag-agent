from __future__ import annotations

import socket
import json
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import uvicorn
from fastapi import FastAPI
from sqlalchemy import Engine, func, insert, select, update

from legal_rag.api.auth import ServicePrincipal
from legal_rag.chunking import article_chunks
from legal_rag.embeddings import (
    EMBEDDING_CACHE_SCHEMA_VERSION,
    EmbeddingCache,
    EmbeddingModelConfig,
    chunk_corpus_fingerprint,
    embedding_contract_fingerprint,
)
from legal_rag.models import LawArticle
from legal_rag.storage.catalog import PostgresLegalCatalogRepository
from legal_rag.storage.contracts import LawVersionSpec, build_storage_import_bundle
from legal_rag.storage.repository import PostgresCorpusRepository
from legal_rag.storage.schema import (
    active_snapshot_pointers,
    corpus_snapshots,
    embedding_imports,
    embedding_profiles,
    idempotency_keys,
    messages,
    run_events,
    run_results,
    runs,
    snapshot_activation_events,
)


@dataclass(frozen=True, slots=True)
class SeededBoundary:
    owner: ServicePrincipal
    other: ServicePrincipal
    owner_token: str
    other_token: str


def seed_m4_service_corpus(engine: Engine) -> SeededBoundary:
    """Import and activate one real corpus/profile for the production executor."""

    suffix = uuid.uuid4().hex
    scope_id = f"m4-service-scope-{suffix}"
    snapshot_id = f"m4-service-snapshot-{suffix}"
    law_id = f"m4-service-law-{suffix}"
    version_id = f"{law_id}-v1"
    article = LawArticle(
        article_id=f"m4-service-article-{suffix}",
        law_name="服务端测试法",
        article_number="第一条",
        body="服务端测试法第一条用于验证 PostgreSQL 检索与安全发布链路。",
        raw_text=(
            "第一条 服务端测试法第一条用于验证 PostgreSQL 检索与安全发布链路。"
        ),
        source_file="fixtures/m4-service-law.txt",
        line_no=1,
        parse_status="from_filename",
    )
    chunks = article_chunks([article])
    model = EmbeddingModelConfig(
        key="m4-service-fixture",
        provider="fixture",
        model_name="fixture/m4-service-model",
        role="retrieval",
        revision="m4-service-revision-1",
        normalize=True,
        dimensions=3,
    )
    cache = EmbeddingCache(
        cache_dir=Path("fixture-cache"),
        chunk_ids=[chunk.chunk_id for chunk in chunks],
        vectors=np.asarray([[1.0, 0.0, 0.0]], dtype=np.float32),
        metadata={
            "schema_version": EMBEDDING_CACHE_SCHEMA_VERSION,
            "chunk_count": len(chunks),
            "vector_count": len(chunks),
            "dimension": 3,
            "dtype": "float32",
            "chunk_strategy": chunks[0].strategy,
            "chunk_fingerprint": chunk_corpus_fingerprint(chunks),
            "embedding_key": model.key,
            "provider": model.provider,
            "model_name": model.model_name,
            "revision": model.revision,
            "normalize": model.normalize,
            "trust_remote_code": model.trust_remote_code,
            "query_prefix": model.query_prefix,
            "document_prefix": model.document_prefix,
            "embed_with_metadata": model.embed_with_metadata,
            "embedding_contract_fingerprint": embedding_contract_fingerprint(model),
        },
    )
    bundle = build_storage_import_bundle(
        snapshot_id=snapshot_id,
        scope_id=scope_id,
        source_manifest={
            "schema_version": 1,
            "fixture": "m4-service-wiring",
            "snapshot_id": snapshot_id,
        },
        laws=[
            LawVersionSpec(
                law_id=law_id,
                version_id=version_id,
                title=article.law_name,
                verification_status="verified",
                source_ref="fixtures/m4-service-law.txt",
                valid_from="2026-01-01",
            )
        ],
        articles=[article],
        chunks=chunks,
        embedding_cache=cache,
        model_config=model,
        model_revision=model.revision,
        chunk_recipe={"fixture": "m4-service-wiring"},
        article_version_ids={article.article_id: version_id},
    )
    PostgresCorpusRepository(engine).import_bundle(bundle)
    PostgresLegalCatalogRepository(engine).activate_snapshot(
        scope_id=scope_id,
        snapshot_id=snapshot_id,
        expected_current_snapshot_id=None,
        required_profile_id=bundle.embedding_profile.profile_id,
        actor="m4-integration-test",
        reason="verify production provider-free service wiring",
    )
    return SeededBoundary(
        owner=ServicePrincipal(
            user_id=f"user-a-{suffix}",
            scope_id=scope_id,
            profile_id=bundle.embedding_profile.profile_id,
        ),
        other=ServicePrincipal(
            user_id=f"user-b-{suffix}",
            scope_id=scope_id,
            profile_id=bundle.embedding_profile.profile_id,
        ),
        owner_token=f"m4-owner-{suffix}-{uuid.uuid4().hex}",
        other_token=f"m4-other-{suffix}-{uuid.uuid4().hex}",
    )


def seed_m4_service_boundary(engine: Engine) -> SeededBoundary:
    suffix = uuid.uuid4().hex
    scope_id = f"m4-scope-{suffix}"
    snapshot_id = f"m4-snapshot-{suffix}"
    activation_id = uuid.uuid4().hex
    profile_id = suffix * 2
    with engine.begin() as connection:
        connection.execute(
            insert(corpus_snapshots).values(
                snapshot_id=snapshot_id,
                scope_id=scope_id,
                source_manifest={"schema_version": 1, "source": "m4-synthetic"},
                source_manifest_hash="1" * 64,
                corpus_hash="2" * 64,
                status="building",
            )
        )
        connection.execute(
            insert(embedding_profiles).values(
                profile_id=profile_id,
                provider="fixture",
                model="fixture/m4",
                revision="fixture-v1",
                dimensions=3,
                normalization=True,
                query_prefix="",
                document_prefix="",
                embed_with_metadata=True,
                recipe_hash="3" * 64,
            )
        )
        connection.execute(
            insert(embedding_imports).values(
                snapshot_id=snapshot_id,
                profile_id=profile_id,
                bundle_hash="4" * 64,
                status="validated",
            )
        )
        connection.execute(
            update(corpus_snapshots)
            .where(corpus_snapshots.c.snapshot_id == snapshot_id)
            .where(corpus_snapshots.c.status == "building")
            .values(status="validated", validated_at=func.now())
        )
        occurred_at = connection.scalar(select(func.transaction_timestamp()))
        connection.execute(
            insert(snapshot_activation_events).values(
                activation_id=activation_id,
                scope_id=scope_id,
                revision=1,
                operation="initial_activate",
                target_snapshot_id=snapshot_id,
                actor="m4-integration-test",
                reason="synthetic service boundary",
                occurred_at=occurred_at,
            )
        )
        connection.execute(
            update(corpus_snapshots)
            .where(corpus_snapshots.c.snapshot_id == snapshot_id)
            .where(corpus_snapshots.c.status == "validated")
            .values(status="active", activated_at=occurred_at)
        )
        connection.execute(
            insert(active_snapshot_pointers).values(
                scope_id=scope_id,
                snapshot_id=snapshot_id,
                revision=1,
                activation_id=activation_id,
                updated_at=occurred_at,
            )
        )
    return SeededBoundary(
        owner=ServicePrincipal(
            user_id=f"user-a-{suffix}",
            scope_id=scope_id,
            profile_id=profile_id,
        ),
        other=ServicePrincipal(
            user_id=f"user-b-{suffix}",
            scope_id=scope_id,
            profile_id=profile_id,
        ),
        owner_token=f"m4-owner-{suffix}-{uuid.uuid4().hex}",
        other_token=f"m4-other-{suffix}-{uuid.uuid4().hex}",
    )


class LoopbackUvicornServer:
    """Run an ASGI app over a pre-bound loopback TCP socket."""

    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.socket: socket.socket | None = None
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None
        self.base_url: str | None = None

    def __enter__(self) -> LoopbackUvicornServer:
        bound_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        bound_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        bound_socket.bind(("127.0.0.1", 0))
        bound_socket.listen(128)
        port = int(bound_socket.getsockname()[1])
        config = uvicorn.Config(
            self.app,
            host="127.0.0.1",
            log_level="warning",
            access_log=False,
            server_header=False,
            lifespan="on",
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(
            target=server.run,
            kwargs={"sockets": [bound_socket]},
            name=f"m4-test-uvicorn-{port}",
            daemon=True,
        )
        self.socket = bound_socket
        self.server = server
        self.thread = thread
        self.base_url = f"http://127.0.0.1:{port}"
        thread.start()
        deadline = time.monotonic() + 10.0
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not server.started:
            self.__exit__(None, None, None)
            raise RuntimeError("loopback Uvicorn server did not start")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=10.0)
            if self.thread.is_alive():
                raise RuntimeError("loopback Uvicorn server did not stop")
        if self.socket is not None:
            try:
                self.socket.close()
            except OSError:
                pass


def authorization(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def assert_non_enumerating_404(response: httpx.Response) -> None:
    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "resource_not_found",
            "message": "resource was not found",
        }
    }


def wait_for_run_status(
    client: httpx.Client,
    run_id: str,
    token: str,
    expected: set[str],
    *,
    timeout: float = 10.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        response = client.get(
            f"/api/v1/runs/{run_id}",
            headers=authorization(token),
        )
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] in expected:
            return last
        time.sleep(0.02)
    raise AssertionError(f"run did not reach {sorted(expected)}; last={last!r}")


def read_sse_frames(
    response: httpx.Response,
    *,
    stop_after_sequence: int | None = None,
) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    current: dict[str, str] = {}
    for line in response.iter_lines():
        if line == "":
            if current:
                frame = {
                    "id": int(current["id"]),
                    "event": current["event"],
                    "data": json.loads(current["data"]),
                }
                frames.append(frame)
                current = {}
                if (
                    stop_after_sequence is not None
                    and frame["id"] >= stop_after_sequence
                ):
                    break
            continue
        if line.startswith(":"):
            continue
        name, separator, value = line.partition(":")
        if separator:
            current[name] = value.lstrip()
    return frames


def run_counts(engine: Engine, run_id: str) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            "runs": int(
                connection.scalar(
                    select(func.count()).select_from(runs).where(runs.c.run_id == run_id)
                )
                or 0
            ),
            "messages": int(
                connection.scalar(
                    select(func.count())
                    .select_from(messages)
                    .where(messages.c.run_id == run_id)
                )
                or 0
            ),
            "results": int(
                connection.scalar(
                    select(func.count())
                    .select_from(run_results)
                    .where(run_results.c.run_id == run_id)
                )
                or 0
            ),
            "keys": int(
                connection.scalar(
                    select(func.count())
                    .select_from(idempotency_keys)
                    .where(idempotency_keys.c.run_id == run_id)
                )
                or 0
            ),
            "events": int(
                connection.scalar(
                    select(func.count())
                    .select_from(run_events)
                    .where(run_events.c.run_id == run_id)
                )
                or 0
            ),
        }
