from __future__ import annotations

# Kept distinct from the offline module name so both can share one pytest command.

import json
from pathlib import Path

import numpy as np
from sqlalchemy import Engine, func, select

from legal_rag.chunking import article_chunks, save_chunks
from legal_rag.data import load_articles
from legal_rag.embeddings import (
    EMBEDDING_CACHE_SCHEMA_VERSION,
    EmbeddingModelConfig,
    chunk_corpus_fingerprint,
    embedding_contract_fingerprint,
)
from legal_rag.manifest import write_artifact_manifest
from legal_rag.storage.import_workflow import (
    apply_import_plan,
    build_import_plan,
    write_machine_artifact,
)
from legal_rag.storage.schema import active_snapshot_pointers


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _import_artifacts(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "workflow-import"
    dataset = root / "dataset"
    index = root / "index"
    cache = root / "cache"
    dataset.mkdir(parents=True)
    index.mkdir()
    cache.mkdir()
    (dataset / "工作流合成法.txt").write_text(
        "第一条 虚构甲内容。\n第二条 虚构乙内容。\n", encoding="utf-8"
    )
    articles = load_articles(dataset)
    chunks = article_chunks(articles)
    save_chunks(chunks, index / "chunks.jsonl")
    write_artifact_manifest(
        index / "manifest.json",
        artifact_type="index",
        run_id="m3-workflow-index",
        config={
            "strategy": "article",
            "chunking": {"strategy": "article", "fixture": "synthetic"},
        },
        metrics={"chunk_count": len(chunks)},
    )

    model = EmbeddingModelConfig(
        key="m3-workflow-fixture",
        provider="fixture",
        model_name="fixture/model",
        role="synthetic_test",
        revision="fixture-workflow-revision-1",
        dimensions=3,
        normalize=True,
    )
    vectors = np.asarray([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
    np.save(cache / "vectors.npy", vectors)
    _write_json(cache / "chunk_ids.json", [chunk.chunk_id for chunk in chunks])
    metadata = {
        "schema_version": EMBEDDING_CACHE_SCHEMA_VERSION,
        "run_id": "m3-workflow-embedding",
        "embedding_key": model.key,
        "provider": model.provider,
        "model_name": model.model_name,
        "revision": model.revision,
        "role": model.role,
        "chunk_strategy": "article",
        "chunk_count": len(chunks),
        "vector_count": len(chunks),
        "dimension": 3,
        "dtype": "float32",
        "chunk_fingerprint": chunk_corpus_fingerprint(chunks),
        "embedding_contract_fingerprint": embedding_contract_fingerprint(model),
        "normalize": True,
        "trust_remote_code": False,
        "query_prefix": "",
        "document_prefix": "",
        "embed_with_metadata": False,
    }
    _write_json(cache / "metadata.json", metadata)
    write_artifact_manifest(
        cache / "manifest.json",
        artifact_type="embedding_cache",
        run_id="m3-workflow-embedding",
        config={
            "embedding_key": model.key,
            "provider": model.provider,
            "model_name": model.model_name,
            "revision": model.revision,
            "normalize": True,
            "trust_remote_code": False,
            "query_prefix": "",
            "document_prefix": "",
            "embed_with_metadata": False,
        },
        metrics={"chunk_count": len(chunks), "dimension": 3},
    )
    request = {
        "schema_version": 1,
        "snapshot_id": "m3-workflow-import-v1",
        "scope_id": "synthetic-workflow",
        "source_manifest": {
            "schema_version": 1,
            "source": "synthetic-integration-fixture",
        },
        "inputs": {
            "dataset_dir": "dataset",
            "chunks_path": "index/chunks.jsonl",
            "index_manifest_path": "index/manifest.json",
            "embedding_cache_dir": "cache",
        },
        "law_versions": [
            {
                "law_id": "workflow-law",
                "version_id": "workflow-law-v1",
                "title": "工作流合成法",
                "verification_status": "unverified",
                "source_ref": "dataset/工作流合成法.txt",
                "valid_from": None,
                "valid_to": None,
            }
        ],
        "article_version_ids": None,
    }
    manifest = root / "request.json"
    _write_json(manifest, request)
    return root, manifest


def test_import_workflow_receipt_verifies_content_dimension_and_idempotency(
    migrated_engine: Engine,
    tmp_path: Path,
) -> None:
    root, manifest = _import_artifacts(tmp_path)
    prepared = build_import_plan(manifest)
    plan = root / "plan.json"
    write_machine_artifact(plan, prepared.plan)
    with migrated_engine.connect() as connection:
        active_before = int(
            connection.scalar(
                select(func.count()).select_from(active_snapshot_pointers)
            )
            or 0
        )

    first = apply_import_plan(manifest, plan, engine=migrated_engine)
    second = apply_import_plan(manifest, plan, engine=migrated_engine)

    assert first["status"] == "imported"
    assert first["result"]["imported"] is True
    assert first["persisted_validation"]["dimensions"] == 3
    assert first["persisted_validation"]["bundle_hash"] == prepared.bundle.bundle_hash
    assert first["persisted_validation"]["corpus_hash"] == prepared.bundle.corpus_hash
    assert first["persisted_validation"]["chunk_count"] == 2
    assert first["persisted_validation"]["embedding_count"] == 2
    assert second["status"] == "already_present"
    assert second["result"]["imported"] is False
    assert all(value == 0 for value in second["table_counts"]["delta"].values())
    assert first["activation_performed"] is False
    assert second["activation_performed"] is False
    with migrated_engine.connect() as connection:
        active_after = int(
            connection.scalar(
                select(func.count()).select_from(active_snapshot_pointers)
            )
            or 0
        )
    assert active_after == active_before

    rendered = json.dumps(first, ensure_ascii=False)
    assert str(root.resolve()) not in rendered
    assert "postgresql" not in rendered
    assert "虚构甲内容" not in rendered
