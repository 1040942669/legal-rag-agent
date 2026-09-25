from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from legal_rag.chunking import article_chunks, save_chunks
from legal_rag.data import load_articles
from legal_rag.embeddings import (
    EMBEDDING_CACHE_SCHEMA_VERSION,
    EmbeddingModelConfig,
    chunk_corpus_fingerprint,
    embedding_contract_fingerprint,
)
from legal_rag.manifest import write_artifact_manifest
from legal_rag.storage import import_cli
from legal_rag.storage.import_workflow import (
    StorageImportWorkflowError,
    build_import_plan,
    validate_import_plan,
    validation_receipt,
    write_machine_artifact,
)


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _fixture_artifacts(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "local-import"
    dataset_dir = root / "dataset"
    index_dir = root / "index"
    cache_dir = root / "cache"
    dataset_dir.mkdir(parents=True)
    index_dir.mkdir(parents=True)
    cache_dir.mkdir(parents=True)
    (dataset_dir / "合成测试法.txt").write_text(
        "第一条 虚构甲内容。\n第二条 虚构乙内容。\n",
        encoding="utf-8",
    )
    articles = load_articles(dataset_dir)
    chunks = article_chunks(articles)
    chunks_path = save_chunks(chunks, index_dir / "chunks.jsonl")
    index_manifest_path = index_dir / "manifest.json"
    chunking = {"strategy": "article", "fixture": "synthetic_non_law"}
    write_artifact_manifest(
        index_manifest_path,
        artifact_type="index",
        run_id="m3-import-index-fixture",
        inputs={},
        config={"strategy": "article", "chunking": chunking},
        outputs={"chunks_path": str(chunks_path)},
        metrics={"chunk_count": len(chunks)},
    )

    model = EmbeddingModelConfig(
        key="m3-import-fixture",
        provider="fixture",
        model_name="fixture/model",
        role="synthetic_test",
        revision="fixture-revision-1",
        normalize=True,
        dimensions=3,
        query_prefix="query: ",
        document_prefix="passage: ",
        embed_with_metadata=True,
    )
    vectors = np.asarray([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
    np.save(cache_dir / "vectors.npy", vectors)
    _write_json(cache_dir / "chunk_ids.json", [chunk.chunk_id for chunk in chunks])
    metadata = {
        "schema_version": EMBEDDING_CACHE_SCHEMA_VERSION,
        "run_id": "m3-import-embedding-fixture",
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
        "normalize": model.normalize,
        "trust_remote_code": model.trust_remote_code,
        "query_prefix": model.query_prefix,
        "document_prefix": model.document_prefix,
        "embed_with_metadata": model.embed_with_metadata,
    }
    _write_json(cache_dir / "metadata.json", metadata)
    write_artifact_manifest(
        cache_dir / "manifest.json",
        artifact_type="embedding_cache",
        run_id="m3-import-embedding-fixture",
        inputs={},
        config={
            "embedding_key": model.key,
            "provider": model.provider,
            "model_name": model.model_name,
            "revision": model.revision,
            "normalize": model.normalize,
            "trust_remote_code": model.trust_remote_code,
            "query_prefix": model.query_prefix,
            "document_prefix": model.document_prefix,
            "embed_with_metadata": model.embed_with_metadata,
        },
        outputs={},
        metrics={"chunk_count": len(chunks), "dimension": 3},
    )

    request = {
        "schema_version": 1,
        "snapshot_id": "m3-import-fixture-v1",
        "scope_id": "synthetic-public",
        "source_manifest": {
            "schema_version": 1,
            "source": "synthetic-test-fixture",
            "redistribution": "not_applicable_synthetic_content",
        },
        "inputs": {
            "dataset_dir": "dataset",
            "chunks_path": "index/chunks.jsonl",
            "index_manifest_path": "index/manifest.json",
            "embedding_cache_dir": "cache",
        },
        "law_versions": [
            {
                "law_id": "synthetic-law",
                "version_id": "synthetic-law-v1",
                "title": "合成测试法",
                "verification_status": "unverified",
                "source_ref": "dataset/合成测试法.txt",
                "valid_from": None,
                "valid_to": None,
            }
        ],
        "article_version_ids": None,
    }
    manifest_path = root / "import-request.json"
    _write_json(manifest_path, request)
    return root, manifest_path


def test_plan_is_deterministic_private_and_explicitly_non_activating(
    tmp_path: Path,
) -> None:
    root, manifest = _fixture_artifacts(tmp_path)

    first = build_import_plan(manifest)
    second = build_import_plan(manifest)

    assert first.plan == second.plan
    assert first.plan["plan_sha256"] == second.plan["plan_sha256"]
    assert first.plan["target"]["dimensions"] == 3
    assert first.plan["expected"] == {
        "source_manifest_hash": first.bundle.snapshot.source_manifest_hash,
        "corpus_hash": first.bundle.corpus_hash,
        "bundle_hash": first.bundle.bundle_hash,
        "law_version_count": 1,
        "article_count": 2,
        "chunk_count": 2,
        "embedding_count": 2,
    }
    assert first.plan["operations"] == {
        "imports_snapshot": True,
        "imports_embedding_profile": True,
        "creates_or_reuses_immutable_rows": True,
        "activates_snapshot": False,
        "builds_ann_index": False,
        "external_model_calls": 0,
    }
    rendered = json.dumps(first.plan, ensure_ascii=False)
    assert "第一条虚构内容" not in rendered
    assert str(root.resolve()) not in rendered
    assert "postgresql" not in rendered


def test_written_plan_validates_and_detects_artifact_drift(tmp_path: Path) -> None:
    root, manifest = _fixture_artifacts(tmp_path)
    prepared = build_import_plan(manifest)
    plan_path = tmp_path / "operator-receipts" / "import-plan.json"
    write_machine_artifact(plan_path, prepared.plan)

    validated = validate_import_plan(manifest, plan_path)
    assert validated.plan == prepared.plan
    assert validation_receipt(validated)["database_connected"] is False

    np.save(
        root / "cache" / "vectors.npy",
        np.asarray([[0.0, 1.0, 0.0], [1.0, 0.0, 0.0]], dtype=np.float32),
    )
    with pytest.raises(StorageImportWorkflowError, match="does not match"):
        validate_import_plan(manifest, plan_path)


def test_plan_rejects_unknown_fields_sensitive_metadata_and_path_escape(
    tmp_path: Path,
) -> None:
    root, manifest = _fixture_artifacts(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["unknown"] = True
    _write_json(manifest, payload)
    with pytest.raises(StorageImportWorkflowError, match="fields are invalid"):
        build_import_plan(manifest)

    del payload["unknown"]
    payload["source_manifest"]["api_key"] = "must-not-be-persisted"
    _write_json(manifest, payload)
    with pytest.raises(StorageImportWorkflowError, match="sensitive field"):
        build_import_plan(manifest)

    del payload["source_manifest"]["api_key"]
    payload["inputs"]["chunks_path"] = "../outside.jsonl"
    _write_json(manifest, payload)
    with pytest.raises(StorageImportWorkflowError, match="safe source-root-relative"):
        build_import_plan(manifest)
    assert root.exists()


def test_plan_rejects_legacy_cache_without_immutable_revision(tmp_path: Path) -> None:
    root, manifest = _fixture_artifacts(tmp_path)
    metadata_path = root / "cache" / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["revision"] = ""
    _write_json(metadata_path, metadata)
    embedding_manifest_path = root / "cache" / "manifest.json"
    embedding_manifest = json.loads(embedding_manifest_path.read_text(encoding="utf-8"))
    embedding_manifest["config"]["revision"] = ""
    _write_json(embedding_manifest_path, embedding_manifest)

    with pytest.raises(StorageImportWorkflowError, match="revision"):
        build_import_plan(manifest)


def test_cli_plan_validate_and_no_clobber_are_machine_readable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, manifest = _fixture_artifacts(tmp_path)
    plan_path = root / "import-plan.json"

    assert (
        import_cli.main(
            ["dry-run", "--manifest", str(manifest), "--output", str(plan_path)]
        )
        == 0
    )
    plan_stdout = json.loads(capsys.readouterr().out)
    assert plan_stdout["artifact_kind"] == "m3_storage_import_plan"
    assert plan_path.is_file()

    assert (
        import_cli.main(
            ["validate", "--manifest", str(manifest), "--plan", str(plan_path)]
        )
        == 0
    )
    validation = json.loads(capsys.readouterr().out)
    assert validation["status"] == "validated"
    assert validation["database_connected"] is False

    assert (
        import_cli.main(
            ["plan", "--manifest", str(manifest), "--output", str(plan_path)]
        )
        == 2
    )
    error = json.loads(capsys.readouterr().err)
    assert error["error_code"] == "invalid_local_artifact"
    assert "overwrite" in error["message"]


def test_cli_invalid_json_is_sanitized_and_does_not_echo_source_text(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, manifest = _fixture_artifacts(tmp_path)
    manifest.write_text('{"schema_version":1,"private":"秘密正文",}', encoding="utf-8")

    assert import_cli.main(["plan", "--manifest", str(manifest)]) == 2
    error_text = capsys.readouterr().err
    error = json.loads(error_text)
    assert error["status"] == "failed"
    assert "秘密正文" not in error_text
    assert str(root.resolve()) not in error_text


def test_cli_apply_revalidates_locally_before_database_configuration_or_migration(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, manifest = _fixture_artifacts(tmp_path)
    plan_path = root / "import-plan.json"
    write_machine_artifact(plan_path, build_import_plan(manifest).plan)
    np.save(
        root / "cache" / "vectors.npy",
        np.asarray([[0.0, 1.0, 0.0], [1.0, 0.0, 0.0]], dtype=np.float32),
    )
    monkeypatch.delenv("LEGAL_RAG_DATABASE_URL", raising=False)

    assert (
        import_cli.main(
            [
                "apply",
                "--manifest",
                str(manifest),
                "--plan",
                str(plan_path),
                "--migrate",
            ]
        )
        == 2
    )
    error = json.loads(capsys.readouterr().err)
    assert error["error_code"] == "invalid_local_artifact"
    assert "does not match" in error["message"]
