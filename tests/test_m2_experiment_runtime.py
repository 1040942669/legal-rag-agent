from __future__ import annotations

import json
import threading
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from legal_rag.experiment_runtime import (
    CacheCorruptionError,
    CacheConflictError,
    CacheMissError,
    ExactStageCache,
    ExperimentContractError,
    StageValue,
    build_experiment_manifest,
    build_stage_cache_key,
    canonical_hash,
    validate_experiment_manifest,
)


def _manifest(
    *,
    prompt_version: str = "prompt-v1",
    embedding_revision: str = "embedding-v1",
    chunking_version: str = "article-v1",
    chunking_implementation: str = "chunking-runtime-v1",
    index_hash: str = "index-v1",
    created_at: str = "2026-09-20T00:00:00Z",
    environment_path: str = "C:/workspace-a",
) -> dict[str, Any]:
    return build_experiment_manifest(
        experiment_id="m2-contract-test",
        execution_mode="offline",
        default_cache_mode="fresh",
        code={
            "commit": "a" * 40,
            "dirty": False,
            "stage_implementation_fingerprints": {
                "chunking": chunking_implementation,
                "query_analysis": "query-analysis-v1",
                "embedding": "embedding-runtime-v1",
                "retrieval": "retrieval-runtime-v1",
                "rerank": "rerank-runtime-v1",
                "generation": "generation-runtime-v1",
                "verification": "verification-runtime-v1",
                "judge": "judge-runtime-v1",
                "aggregation": "aggregation-runtime-v1",
            },
        },
        config_summary={"profile": "offline", "top_k": 3},
        corpus={"snapshot_hash": "corpus-v1", "index_hash": index_hash},
        dataset={
            "dataset_id": "synthetic-m2",
            "role": "test_fixture",
            "case_file_hash": "cases-v1",
            "case_schema_version": 1,
        },
        contracts={
            "chunking": {"strategy": "article", "version": chunking_version},
            "embedding": {
                "model": "fake-embedding",
                "revision": embedding_revision,
                "dimension": 3,
                "normalized": True,
                "query_text_version": "query-v1",
                "document_text_version": "document-v1",
            },
            "retrieval": {
                "kind": "dense",
                "parameters": {"top_k": 3},
                "filters": {},
                "scope": {},
            },
            "rerank": {"enabled": False, "config": {}},
            "generation": {
                "model": "fake-generation",
                "revision": "generation-v1",
                "prompt_version": prompt_version,
                "parameters": {"temperature": 0},
            },
            "verification": {"schema_version": 2, "rules_version": "m1"},
            "judge": {"enabled": False},
        },
        runtime={
            "random_seed": 42,
            "concurrency": 1,
            "max_retries": 0,
            "timing_scope": "per_stage_wall_clock",
        },
        environment={
            "python": "3.12.13",
            "platform": "test",
            "hardware": "deterministic-fake",
            "workspace_path": environment_path,
        },
        created_at=created_at,
    )


def test_m2_t01_exact_replay_preserves_output_without_external_calls(tmp_path) -> None:
    manifest = _manifest()
    cache = ExactStageCache(tmp_path / "stage-cache")
    producer_calls = 0

    def produce() -> StageValue:
        nonlocal producer_calls
        producer_calls += 1
        return StageValue(
            payload={
                "query": "synthetic query",
                "results": [{"source_id": "S1", "score": 1.0}],
            },
            external_calls={"embedding": 1},
        )

    case_input = {
        "case_id": "case-1",
        "normalized_query_hash": canonical_hash("synthetic query"),
        "query_representation_hash": canonical_hash([0.1, 0.2, 0.3]),
    }
    fresh = cache.execute(
        manifest=manifest,
        stage="retrieval",
        input_payload=case_input,
        mode="fresh",
        producer=produce,
    )
    replay = cache.execute(
        manifest=manifest,
        stage="retrieval",
        input_payload=case_input,
        mode="replay",
        producer=lambda: (_ for _ in ()).throw(
            AssertionError("replay called producer")
        ),
    )

    assert producer_calls == 1
    assert replay.payload == fresh.payload
    assert replay.cache_key == fresh.cache_key
    assert replay.origin == "replay"
    assert sum(replay.external_calls.values()) == 0
    assert replay.source_external_calls["embedding"] == 1
    assert manifest["identity"]["resume_compatibility_hash"]


def test_replay_miss_and_corruption_fail_closed_without_running_producer(
    tmp_path,
) -> None:
    manifest = _manifest()
    cache = ExactStageCache(tmp_path / "stage-cache")
    case_input = {
        "case_id": "case-1",
        "normalized_query_hash": canonical_hash("synthetic query"),
        "query_representation_hash": canonical_hash([0.1, 0.2, 0.3]),
    }
    producer_calls = 0

    def forbidden() -> StageValue:
        nonlocal producer_calls
        producer_calls += 1
        raise AssertionError("replay must not initialize or call a producer")

    with pytest.raises(CacheMissError):
        cache.execute(
            manifest=manifest,
            stage="retrieval",
            input_payload=case_input,
            mode="replay",
            producer=forbidden,
        )
    assert producer_calls == 0

    cache.execute(
        manifest=manifest,
        stage="retrieval",
        input_payload=case_input,
        mode="fresh",
        producer=lambda: StageValue(payload={"results": ["S1"]}),
    )
    cache_path = next((tmp_path / "stage-cache" / "retrieval").glob("*.json"))
    envelope = json.loads(cache_path.read_text(encoding="utf-8"))
    envelope["payload"] = {"results": ["tampered"]}
    cache_path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(CacheCorruptionError):
        cache.execute(
            manifest=manifest,
            stage="retrieval",
            input_payload=case_input,
            mode="replay",
            producer=forbidden,
        )
    assert producer_calls == 0

    envelope["payload"] = {"results": ["S1"]}
    envelope["source_external_calls"]["other"] = 1
    cache_path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(CacheCorruptionError):
        cache.execute(
            manifest=manifest,
            stage="retrieval",
            input_payload=case_input,
            mode="replay",
            producer=forbidden,
        )
    assert producer_calls == 0


def test_m2_t02_stage_keys_invalidate_only_affected_contracts() -> None:
    base = _manifest()
    prompt_changed = _manifest(prompt_version="prompt-v2")
    embedding_changed = _manifest(embedding_revision="embedding-v2")
    chunking_changed = _manifest(chunking_version="article-v2")
    chunking_code_changed = _manifest(chunking_implementation="chunking-runtime-v2")
    index_changed = _manifest(index_hash="index-v2")
    retrieval_input = {
        "case_id": "case-1",
        "normalized_query_hash": canonical_hash("same query"),
        "query_representation_hash": canonical_hash([0.1, 0.2, 0.3]),
    }
    embedding_input = {
        "content_hash": canonical_hash("corpus content"),
        "text_kind": "document",
    }
    generation_input = {
        "case_id": "case-1",
        "context_history_hash": canonical_hash(["context-v1"]),
        "evidence_hash": canonical_hash(["evidence-v1"]),
    }

    base_retrieval = build_stage_cache_key(base, "retrieval", retrieval_input)
    assert (
        prompt_changed["identity"]["resume_compatibility_hash"]
        != base["identity"]["resume_compatibility_hash"]
    )
    assert (
        build_stage_cache_key(prompt_changed, "retrieval", retrieval_input)
        == base_retrieval
    )
    assert build_stage_cache_key(
        prompt_changed, "generation", generation_input
    ) != build_stage_cache_key(base, "generation", generation_input)
    assert (
        build_stage_cache_key(embedding_changed, "retrieval", retrieval_input)
        != base_retrieval
    )
    assert (
        build_stage_cache_key(chunking_changed, "retrieval", retrieval_input)
        != base_retrieval
    )
    base_embedding = build_stage_cache_key(base, "embedding", embedding_input)
    assert (
        build_stage_cache_key(prompt_changed, "embedding", embedding_input)
        == base_embedding
    )
    assert (
        build_stage_cache_key(index_changed, "embedding", embedding_input)
        == base_embedding
    )
    assert (
        build_stage_cache_key(index_changed, "retrieval", retrieval_input)
        != base_retrieval
    )
    assert (
        build_stage_cache_key(embedding_changed, "embedding", embedding_input)
        != base_embedding
    )
    assert (
        build_stage_cache_key(chunking_changed, "embedding", embedding_input)
        != base_embedding
    )
    assert (
        build_stage_cache_key(chunking_code_changed, "embedding", embedding_input)
        != base_embedding
    )
    assert (
        build_stage_cache_key(chunking_code_changed, "retrieval", retrieval_input)
        != base_retrieval
    )


def test_manifest_identity_ignores_provenance_only_fields() -> None:
    first = _manifest(
        created_at="2026-09-20T00:00:00Z",
        environment_path="C:/workspace-a",
    )
    second = _manifest(
        created_at="2026-09-21T00:00:00Z",
        environment_path="D:/workspace-b",
    )

    assert (
        first["identity"]["resume_compatibility_hash"]
        == second["identity"]["resume_compatibility_hash"]
    )
    assert first["identity"]["manifest_hash"] != second["identity"]["manifest_hash"]
    assert validate_experiment_manifest(first) == first


def test_stage_cache_rejects_stale_or_tampered_manifest_fingerprints() -> None:
    manifest = _manifest()
    tampered_contract = deepcopy(manifest)
    tampered_contract["contracts"]["embedding"]["revision"] = "embedding-v2"
    with pytest.raises(ExperimentContractError):
        build_stage_cache_key(
            tampered_contract,
            "retrieval",
            {
                "normalized_query_hash": canonical_hash("query"),
                "query_representation_hash": canonical_hash([0.1]),
            },
        )


def test_manifest_and_stage_inputs_reject_missing_deterministic_identity() -> None:
    valid = _manifest()
    empty_contracts = {
        name: {}
        for name in (
            "chunking",
            "embedding",
            "retrieval",
            "rerank",
            "generation",
            "verification",
            "judge",
        )
    }
    with pytest.raises(ExperimentContractError):
        build_experiment_manifest(
            experiment_id=valid["experiment_id"],
            execution_mode=valid["execution_mode"],
            default_cache_mode=valid["cache_policy"]["default_mode"],
            code=valid["code"],
            config_summary=valid["config"]["summary"],
            corpus=valid["corpus"],
            dataset=valid["dataset"],
            contracts=empty_contracts,
            runtime=valid["runtime"],
            environment=valid["environment"],
            created_at=valid["created_at"],
        )

    for stage in (
        "query_analysis",
        "embedding",
        "retrieval",
        "rerank",
        "generation",
        "verification",
        "judge",
        "aggregation",
    ):
        with pytest.raises(ExperimentContractError):
            build_stage_cache_key(valid, stage, {})

    tampered_implementation = deepcopy(valid)
    tampered_implementation["code"]["stage_implementation_fingerprints"][
        "retrieval"
    ] = "retrieval-runtime-v2"
    with pytest.raises(ExperimentContractError):
        build_stage_cache_key(
            tampered_implementation,
            "retrieval",
            {
                "normalized_query_hash": canonical_hash("query"),
                "query_representation_hash": canonical_hash([0.1]),
            },
        )


def test_dirty_manifest_requires_a_working_tree_diff_hash() -> None:
    valid = _manifest()
    dirty_code = deepcopy(valid["code"])
    dirty_code["dirty"] = True
    with pytest.raises(ExperimentContractError):
        build_experiment_manifest(
            experiment_id=valid["experiment_id"],
            execution_mode=valid["execution_mode"],
            default_cache_mode=valid["cache_policy"]["default_mode"],
            code=dirty_code,
            config_summary=valid["config"]["summary"],
            corpus=valid["corpus"],
            dataset=valid["dataset"],
            contracts=valid["contracts"],
            runtime=valid["runtime"],
            environment=valid["environment"],
            created_at=valid["created_at"],
        )

    dirty_code["diff_hash"] = canonical_hash({"tracked": "diff", "untracked": []})
    dirty_manifest = build_experiment_manifest(
        experiment_id=valid["experiment_id"],
        execution_mode=valid["execution_mode"],
        default_cache_mode=valid["cache_policy"]["default_mode"],
        code=dirty_code,
        config_summary=valid["config"]["summary"],
        corpus=valid["corpus"],
        dataset=valid["dataset"],
        contracts=valid["contracts"],
        runtime=valid["runtime"],
        environment=valid["environment"],
        created_at=valid["created_at"],
    )
    assert (
        dirty_manifest["identity"]["resume_compatibility_hash"]
        != valid["identity"]["resume_compatibility_hash"]
    )


def test_canonical_contract_rejects_ambiguous_values_and_unknown_stages() -> None:
    assert canonical_hash({"b": 2, "a": 1}) == canonical_hash({"a": 1, "b": 2})
    assert canonical_hash([1, 2]) != canonical_hash([2, 1])
    with pytest.raises(ExperimentContractError):
        canonical_hash({"bad": float("nan")})
    with pytest.raises(ExperimentContractError):
        canonical_hash({"bad": "\ud800"})
    with pytest.raises(ExperimentContractError):
        build_stage_cache_key(_manifest(), "unknown", {"case_id": "case-1"})


def test_exact_cache_concurrent_writers_cannot_overwrite_one_another(tmp_path) -> None:
    manifest = _manifest()
    cache = ExactStageCache(tmp_path / "stage-cache")
    barrier = threading.Barrier(2)

    def write(value: str) -> str:
        def produce() -> StageValue:
            barrier.wait(timeout=5)
            return StageValue(payload={"winner": value})

        return cache.execute(
            manifest=manifest,
            stage="retrieval",
            input_payload={
                "case_id": "same-case",
                "normalized_query_hash": canonical_hash("same query"),
                "query_representation_hash": canonical_hash([0.1]),
            },
            mode="fresh",
            producer=produce,
        ).payload["winner"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(write, value) for value in ("first", "second")]
        outcomes: list[str] = []
        conflicts = 0
        for future in futures:
            try:
                outcomes.append(future.result(timeout=10))
            except CacheConflictError:
                conflicts += 1

    assert len(outcomes) == 1
    assert conflicts == 1
    replay = cache.execute(
        manifest=manifest,
        stage="retrieval",
        input_payload={
            "case_id": "same-case",
            "normalized_query_hash": canonical_hash("same query"),
            "query_representation_hash": canonical_hash([0.1]),
        },
        mode="replay",
        producer=lambda: (_ for _ in ()).throw(AssertionError("unexpected producer")),
    )
    assert replay.payload["winner"] == outcomes[0]
