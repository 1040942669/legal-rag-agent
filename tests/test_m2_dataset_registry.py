from __future__ import annotations

import hashlib
import json
import threading
from copy import deepcopy
from pathlib import Path

import pytest

from legal_rag.experiment_datasets import (
    DatasetRegistryError,
    default_dataset_registry_path,
    load_dataset_registry,
)
from legal_rag.experiment_runtime import (
    EXTERNAL_CALL_KINDS,
    ExactStageCache,
    ExperimentContractError,
    StageValue,
    build_experiment_manifest,
    canonical_hash,
)
from legal_rag.experiment_runner import AttemptControls, ProviderController
from legal_rag.experiment_store import ExperimentStore

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _manifest_for_registered_dataset(
    dataset: dict,
    *,
    experiment_id: str,
    execution_mode: str,
    generate: bool | None = None,
    judge_enabled: bool = False,
    allow_external_calls: bool = False,
    default_cache_mode: str = "cache",
) -> dict:
    if generate is None:
        generate = execution_mode in {"smoke-generation", "full-regression"}
    return build_experiment_manifest(
        experiment_id=experiment_id,
        execution_mode=execution_mode,
        default_cache_mode=default_cache_mode,
        code={
            "commit": "a" * 40,
            "dirty": False,
            "stage_implementation_fingerprints": {
                "chunking": "chunking-v1",
                "query_analysis": "query-analysis-v1",
                "embedding": "embedding-v1",
                "retrieval": "retrieval-v1",
                "rerank": "rerank-v1",
                "generation": "generation-v1",
                "verification": "verification-v1",
                "judge": "judge-v1",
                "aggregation": "aggregation-v1",
            },
        },
        config_summary={
            "generate": generate,
            "allow_external_calls": allow_external_calls,
            "top_k": 3,
        },
        corpus={"snapshot_hash": "fixture-corpus", "index_hash": "fixture-index"},
        dataset=dataset,
        contracts={
            "chunking": {"strategy": "article", "version": "article-v1"},
            "embedding": {
                "model": "fixture-embedding",
                "revision": "embedding-v1",
                "dimension": 3,
                "normalized": True,
                "query_text_version": "query-v1",
                "document_text_version": "document-v1",
            },
            "retrieval": {
                "kind": "bm25",
                "parameters": {"top_k": 3},
                "filters": {},
                "scope": {},
            },
            "rerank": {"enabled": False, "config": {}},
            "generation": {
                "model": "fixture-generation",
                "revision": "generation-v1",
                "prompt_version": "prompt-v1",
                "parameters": {"temperature": 0},
            },
            "verification": {"schema_version": 2, "rules_version": "m1"},
            "judge": {"enabled": judge_enabled},
        },
        runtime={
            "random_seed": 42,
            "concurrency": 1,
            "max_retries": 0,
            "timing_scope": "runner_stage_wall_clock",
        },
        environment={"python": "test", "platform": "test", "hardware": "test"},
        created_at="2026-09-20T08:17:01Z",
    )


def _production_payload() -> dict:
    return json.loads(
        default_dataset_registry_path(REPOSITORY_ROOT).read_text(encoding="utf-8")
    )


def _copy_registry_fixture(tmp_path: Path) -> tuple[Path, dict]:
    root = tmp_path / "repo"
    eval_root = root / "eval_cases"
    eval_root.mkdir(parents=True)
    for name in (
        "legal_eval_cases_v3.jsonl",
        "legal_eval_cases_v3_gen_subset.jsonl",
        "synthetic_offline_v1.jsonl",
    ):
        (eval_root / name).write_bytes(
            (REPOSITORY_ROOT / "eval_cases" / name).read_bytes()
        )
    payload = _production_payload()
    path = eval_root / "registry.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path, payload


def test_registry_verifies_legacy_full_subset_and_synthetic_offline_fixture() -> None:
    registry = load_dataset_registry(
        default_dataset_registry_path(REPOSITORY_ROOT),
        repository_root=REPOSITORY_ROOT,
    )

    full = registry.dataset("legal-eval-v3")
    generation = registry.dataset("legal-eval-v3-generation-30")
    offline = registry.dataset("synthetic-offline-v1")

    assert len(full.cases) == 120
    assert len(generation.cases) == 30
    assert len(offline.cases) == 2
    assert full.entry.role == generation.entry.role == "legacy_regression"
    assert full.entry.exposure == {
        "status": "repeated_development",
        "is_holdout": False,
    }
    assert full.entry.gold_case_count == 108
    assert generation.entry.gold_case_count == 27
    assert offline.entry.role == "synthetic_fixture"
    assert offline.entry.exposure == {"status": "synthetic", "is_holdout": False}
    assert [case.case_id for case in offline.cases] == [
        "synthetic_offline_amber_token",
        "synthetic_offline_real_law_refusal",
    ]
    full_by_id = {case.case_id: case for case in full.cases}
    assert all(full_by_id[case.case_id] == case for case in generation.cases)

    manifest_dataset = offline.manifest_payload()
    assert manifest_dataset["case_file_hash"] == offline.entry.file_sha256
    assert (
        manifest_dataset["case_artifact_set_sha256"]
        == offline.entry.case_artifact_set_sha256
    )
    assert manifest_dataset["case_set_hash"] == offline.entry.manifest_case_set_sha256
    assert (
        canonical_hash(manifest_dataset["cases"]) == manifest_dataset["case_set_hash"]
    )
    assert manifest_dataset["registry"]["is_holdout"] is False
    assert len(manifest_dataset["cases"]) == 2


def test_four_modes_fail_closed_and_replay_never_declares_external_calls() -> None:
    registry = load_dataset_registry(
        default_dataset_registry_path(REPOSITORY_ROOT),
        repository_root=REPOSITORY_ROOT,
    )

    offline = registry.resolve_mode("offline")
    retrieval = registry.resolve_mode("retrieval")
    smoke = registry.resolve_mode("smoke-generation")
    full = registry.resolve_mode("full-regression")

    assert offline.generate is False
    assert offline.external_calls_allowed is False
    assert retrieval.generate is False
    assert smoke.generate is True
    assert full.generate is True
    assert smoke.external_calls_allowed is False

    with pytest.raises(DatasetRegistryError, match="forbids external calls"):
        registry.resolve_mode("offline", allow_external_calls=True)
    with pytest.raises(DatasetRegistryError, match="forbids judge"):
        registry.resolve_mode("retrieval", judge_enabled=True)
    with pytest.raises(DatasetRegistryError, match="replay"):
        registry.resolve_mode(
            "smoke-generation",
            cache_mode="replay",
            allow_external_calls=True,
        )
    with pytest.raises(DatasetRegistryError, match="not registered"):
        registry.resolve_mode(
            "smoke-generation",
            dataset_id="legal-eval-v3",
        )


def test_registry_rejects_file_tampering_even_if_jsonl_still_parses(tmp_path) -> None:
    registry_path, _ = _copy_registry_fixture(tmp_path)
    case_path = registry_path.parent / "legal_eval_cases_v3.jsonl"
    case_path.write_bytes(case_path.read_bytes() + b"\n")

    with pytest.raises(DatasetRegistryError, match="file SHA-256"):
        load_dataset_registry(registry_path)


def test_registered_file_hash_is_stable_across_lf_and_crlf_checkouts(tmp_path) -> None:
    registry_path, _ = _copy_registry_fixture(tmp_path)
    case_path = registry_path.parent / "legal_eval_cases_v3.jsonl"
    lf_content = case_path.read_bytes().replace(b"\r\n", b"\n")
    case_path.write_bytes(lf_content)
    lf_registry = load_dataset_registry(registry_path)

    case_path.write_bytes(lf_content.replace(b"\n", b"\r\n"))
    crlf_registry = load_dataset_registry(registry_path)

    assert (
        lf_registry.dataset("legal-eval-v3").entry.file_sha256
        == crlf_registry.dataset("legal-eval-v3").entry.file_sha256
    )


def test_registry_rejects_normalized_case_or_parent_drift_after_rehash(
    tmp_path,
) -> None:
    registry_path, payload = _copy_registry_fixture(tmp_path)
    case_path = registry_path.parent / "legal_eval_cases_v3_gen_subset.jsonl"
    lines = case_path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["question"] = "被修改但仍是合法 JSON 的问题"
    lines[0] = json.dumps(first, ensure_ascii=False, separators=(",", ":"))
    case_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    changed_sha = hashlib.sha256(
        case_path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    ).hexdigest()
    changed = deepcopy(payload)
    for entry in changed["datasets"]:
        if entry["path"].endswith("legal_eval_cases_v3_gen_subset.jsonl"):
            entry["file_sha256"] = changed_sha
    registry_path.write_text(
        json.dumps(changed, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    with pytest.raises(DatasetRegistryError, match="normalized case-set hash"):
        load_dataset_registry(registry_path)


def test_registry_rejects_holdout_relabel_and_weakened_mode_contract(tmp_path) -> None:
    registry_path, payload = _copy_registry_fixture(tmp_path)
    payload["datasets"][0]["exposure"]["is_holdout"] = True
    registry_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with pytest.raises(DatasetRegistryError, match="cannot be labeled holdout"):
        load_dataset_registry(registry_path)

    registry_path, payload = _copy_registry_fixture(tmp_path / "second")
    payload["mode_defaults"]["offline"]["external_calls"] = "explicit_opt_in"
    registry_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with pytest.raises(DatasetRegistryError, match="safety contract"):
        load_dataset_registry(registry_path)


def test_loaded_registry_and_cases_are_defensive_immutable_views() -> None:
    registry = load_dataset_registry(
        default_dataset_registry_path(REPOSITORY_ROOT),
        repository_root=REPOSITORY_ROOT,
    )
    dataset = registry.dataset("synthetic-offline-v1")

    with pytest.raises(TypeError):
        registry.datasets["replacement"] = dataset  # type: ignore[index]
    with pytest.raises(TypeError):
        dataset.entry.exposure["is_holdout"] = True  # type: ignore[index]

    first_read = dataset.cases
    first_read[0].keywords.append("caller mutation")
    second_read = dataset.cases
    assert "caller mutation" not in second_read[0].keywords
    assert dataset.manifest_payload()["registry"]["file_sha256"] == registry.file_sha256


def test_registry_rejects_bool_schema_duplicate_keys_and_path_traversal(
    tmp_path,
) -> None:
    registry_path, payload = _copy_registry_fixture(tmp_path)
    payload["registry_schema_version"] = True
    registry_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with pytest.raises(DatasetRegistryError, match="schema version"):
        load_dataset_registry(registry_path)

    registry_path, _ = _copy_registry_fixture(tmp_path / "duplicate")
    text = registry_path.read_text(encoding="utf-8")
    text = text.replace(
        '"registry_schema_version": 1,',
        '"registry_schema_version": 1, "registry_schema_version": 1,',
        1,
    )
    registry_path.write_text(text, encoding="utf-8")
    with pytest.raises(DatasetRegistryError, match="strict JSON"):
        load_dataset_registry(registry_path)

    registry_path, payload = _copy_registry_fixture(tmp_path / "traversal")
    payload["datasets"][0]["path"] = "../outside.jsonl"
    registry_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with pytest.raises(DatasetRegistryError, match="safe repository-relative"):
        load_dataset_registry(registry_path)


def test_offline_mode_cannot_be_reassigned_to_legacy_regression(tmp_path) -> None:
    registry_path, payload = _copy_registry_fixture(tmp_path)
    payload["datasets"][0]["allowed_modes"].append("offline")
    payload["mode_defaults"]["offline"]["default_dataset_id"] = "legal-eval-v3"
    registry_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    with pytest.raises(DatasetRegistryError, match="synthetic_fixture"):
        load_dataset_registry(registry_path)


def test_manifest_and_store_enforce_registry_mode_without_resolve_mode(
    tmp_path,
) -> None:
    registry = load_dataset_registry(
        default_dataset_registry_path(REPOSITORY_ROOT),
        repository_root=REPOSITORY_ROOT,
    )
    synthetic = registry.dataset("synthetic-offline-v1").manifest_payload()
    generation = registry.dataset("legal-eval-v3-generation-30").manifest_payload()
    full = registry.dataset("legal-eval-v3").manifest_payload()

    offline_manifest = _manifest_for_registered_dataset(
        synthetic,
        experiment_id="registry-offline-valid",
        execution_mode="offline",
    )
    offline_store = ExperimentStore.create(tmp_path / "experiments", offline_manifest)
    assert offline_store.scan().expected == 2
    assert offline_manifest["execution_policy"] == {
        "contract": "registered_mode_v1",
        "generation": "forbidden",
        "judge": "forbidden",
        "external_calls": "forbidden",
        "generate": False,
        "judge_enabled": False,
        "external_calls_allowed": False,
    }

    smoke_manifest = _manifest_for_registered_dataset(
        generation,
        experiment_id="registry-smoke-valid",
        execution_mode="smoke-generation",
    )
    smoke_store = ExperimentStore.create(tmp_path / "experiments", smoke_manifest)
    assert smoke_store.scan().expected == 30

    with pytest.raises(ExperimentContractError, match="does not allow"):
        _manifest_for_registered_dataset(
            full,
            experiment_id="registry-offline-invalid",
            execution_mode="offline",
        )
    with pytest.raises(ExperimentContractError, match="does not allow"):
        _manifest_for_registered_dataset(
            full,
            experiment_id="registry-smoke-invalid",
            execution_mode="smoke-generation",
        )


def test_manifest_rejects_registered_mode_behavior_policy_bypass() -> None:
    registry = load_dataset_registry(
        default_dataset_registry_path(REPOSITORY_ROOT),
        repository_root=REPOSITORY_ROOT,
    )
    synthetic = registry.dataset("synthetic-offline-v1").manifest_payload()
    generation = registry.dataset("legal-eval-v3-generation-30").manifest_payload()
    full = registry.dataset("legal-eval-v3").manifest_payload()

    with pytest.raises(ExperimentContractError, match="generate=False"):
        _manifest_for_registered_dataset(
            synthetic,
            experiment_id="registry-offline-generation-invalid",
            execution_mode="offline",
            generate=True,
        )
    with pytest.raises(ExperimentContractError, match="forbids judge"):
        _manifest_for_registered_dataset(
            synthetic,
            experiment_id="registry-offline-judge-invalid",
            execution_mode="offline",
            judge_enabled=True,
        )
    with pytest.raises(ExperimentContractError, match="forbids external calls"):
        _manifest_for_registered_dataset(
            synthetic,
            experiment_id="registry-offline-external-invalid",
            execution_mode="offline",
            allow_external_calls=True,
        )
    with pytest.raises(ExperimentContractError, match="generate=True"):
        _manifest_for_registered_dataset(
            full,
            experiment_id="registry-full-generation-invalid",
            execution_mode="full-regression",
            generate=False,
        )
    with pytest.raises(ExperimentContractError, match="replay"):
        _manifest_for_registered_dataset(
            generation,
            experiment_id="registry-replay-external-invalid",
            execution_mode="smoke-generation",
            allow_external_calls=True,
            default_cache_mode="replay",
        )


def test_registered_external_opt_in_gates_dispatch_but_allows_exact_cache_hits(
    tmp_path: Path,
) -> None:
    registry = load_dataset_registry(
        default_dataset_registry_path(REPOSITORY_ROOT),
        repository_root=REPOSITORY_ROOT,
    )
    dataset = registry.dataset("legal-eval-v3-generation-30").manifest_payload()
    authorized = _manifest_for_registered_dataset(
        dataset,
        experiment_id="registry-external-authorized",
        execution_mode="smoke-generation",
        allow_external_calls=True,
    )
    blocked = _manifest_for_registered_dataset(
        dataset,
        experiment_id="registry-external-blocked",
        execution_mode="smoke-generation",
        allow_external_calls=False,
    )
    controller = ProviderController({kind: 1 for kind in EXTERNAL_CALL_KINDS})
    stop_event = threading.Event()
    authorized_controls = AttemptControls(
        controller,
        stop_event,
        cache_mode="cache",
        external_calls_allowed=True,
    )
    blocked_controls = AttemptControls(
        controller,
        stop_event,
        cache_mode="cache",
        external_calls_allowed=False,
    )
    calls = 0

    def provider_call() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"answer": "seeded"}

    def produce(controls: AttemptControls) -> StageValue:
        return StageValue(
            payload=controls.call(
                "generation",
                "generation",
                provider_call,
            ),
            external_calls={"generation": 1},
        )

    cache = ExactStageCache(tmp_path / "cache")
    input_payload = {
        "context_history_hash": canonical_hash("history"),
        "evidence_hash": canonical_hash("evidence"),
    }
    seeded = cache.execute(
        manifest=authorized,
        stage="generation",
        input_payload=input_payload,
        mode="cache",
        producer=lambda: produce(authorized_controls),
    )
    assert seeded.origin == "fresh"
    assert calls == 1

    cached = cache.execute(
        manifest=blocked,
        stage="generation",
        input_payload=input_payload,
        mode="cache",
        producer=lambda: produce(blocked_controls),
    )
    replayed = cache.execute(
        manifest=blocked,
        stage="generation",
        input_payload=input_payload,
        mode="replay",
        producer=lambda: produce(blocked_controls),
    )
    assert cached.origin == "cache"
    assert replayed.origin == "replay"
    assert (
        cached.external_calls
        == replayed.external_calls
        == {kind: 0 for kind in EXTERNAL_CALL_KINDS}
    )
    assert calls == 1

    missing_input = {
        **input_payload,
        "evidence_hash": canonical_hash("cache-miss"),
    }
    with pytest.raises(ExperimentContractError, match="before provider dispatch"):
        cache.execute(
            manifest=blocked,
            stage="generation",
            input_payload=missing_input,
            mode="cache",
            producer=lambda: produce(blocked_controls),
        )
    with pytest.raises(ExperimentContractError, match="before provider dispatch"):
        cache.execute(
            manifest=blocked,
            stage="generation",
            input_payload=input_payload,
            mode="fresh",
            producer=lambda: produce(blocked_controls),
        )
    assert calls == 1

    for stage, kind in (
        ("query_analysis", "normalizer"),
        ("query_embedding", "embedding"),
        ("rerank", "rerank"),
        ("generation", "generation"),
        ("judge", "judge"),
    ):
        with pytest.raises(
            ExperimentContractError,
            match="before provider dispatch",
        ):
            blocked_controls.call(stage, kind, provider_call)
    assert calls == 1
