from __future__ import annotations

import json
import threading
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

import legal_rag.experiment_store as experiment_store_module
from legal_rag.experiment_runtime import (
    ExperimentContractError,
    build_experiment_manifest,
    canonical_hash,
)
from legal_rag.experiment_store import (
    ArtifactConflictError,
    ArtifactCorruptionError,
    ExperimentStore,
    ResumeCompatibilityError,
)


def _manifest(
    *,
    experiment_id: str = "m2-store-test",
    case_ids: tuple[str, ...] = ("case-a", "case-b", "case-c"),
    prompt_version: str = "prompt-v1",
    corpus_version: str = "corpus-v1",
    created_at: str = "2026-09-20T00:00:00Z",
    environment_label: str = "test-a",
    default_cache_mode: str = "fresh",
) -> dict[str, Any]:
    cases = [
        {
            "ordinal": ordinal,
            "case_id": case_id,
            "case_hash": canonical_hash(
                {"case_id": case_id, "question": f"question-{ordinal}"}
            ),
        }
        for ordinal, case_id in enumerate(case_ids)
    ]
    return build_experiment_manifest(
        experiment_id=experiment_id,
        execution_mode="offline",
        default_cache_mode=default_cache_mode,
        code={
            "commit": "b" * 40,
            "dirty": False,
            "stage_implementation_fingerprints": {
                "chunking": "chunking-runtime-v1",
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
        corpus={
            "snapshot_hash": canonical_hash(corpus_version),
            "index_hash": canonical_hash({"index": corpus_version}),
        },
        dataset={
            "dataset_id": "synthetic-m2-store",
            "role": "test_fixture",
            "case_file_hash": canonical_hash({"file": list(case_ids)}),
            "case_schema_version": 1,
            "case_count": len(cases),
            "case_set_hash": canonical_hash(cases),
            "cases": cases,
        },
        contracts={
            "chunking": {"strategy": "article", "version": "article-v1"},
            "embedding": {
                "model": "fake-embedding",
                "revision": "embedding-v1",
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
            "max_retries": 1,
            "timing_scope": "per_stage_wall_clock",
        },
        environment={
            "python": "3.12.13",
            "platform": "test",
            "label": environment_label,
        },
        created_at=created_at,
    )


def _write_success(
    store: ExperimentStore,
    case_id: str,
    *,
    attempt: int = 1,
) -> Path:
    _write_attempt(
        store,
        case_id,
        attempt=attempt,
        status="succeeded",
        recorded_at=f"2026-09-20T00:00:0{attempt}Z",
        result={"tool_outputs": {"case_id": case_id, "attempt": attempt}},
    )
    return store.mark_complete(case_id, attempt=attempt)


def _rebuild_with_dataset(
    manifest: dict[str, Any],
    dataset: dict[str, Any],
) -> dict[str, Any]:
    return build_experiment_manifest(
        experiment_id=manifest["experiment_id"],
        execution_mode=manifest["execution_mode"],
        default_cache_mode=manifest["cache_policy"]["default_mode"],
        code=manifest["code"],
        config_summary=manifest["config"]["summary"],
        corpus=manifest["corpus"],
        dataset=dataset,
        contracts=manifest["contracts"],
        runtime=manifest["runtime"],
        environment=manifest["environment"],
        created_at=manifest["created_at"],
    )


def _write_attempt(
    store: ExperimentStore,
    case_id: str,
    *,
    attempt: int,
    status: str,
    recorded_at: str,
    result: dict[str, Any],
    cache_mode: str = "fresh",
    environment_label: str = "test-a",
) -> Path:
    return store.write_attempt(
        case_id,
        attempt=attempt,
        status=status,
        recorded_at=recorded_at,
        cache_mode=cache_mode,
        execution_environment={
            "python": "3.12.13",
            "platform": "test",
            "label": environment_label,
        },
        result=result,
    )


def test_store_create_load_and_resume_inventory_skip_completed_cases(tmp_path) -> None:
    manifest = _manifest()
    store = ExperimentStore.create(tmp_path / "experiments", manifest)
    assert store.load_manifest() == manifest

    _write_success(store, "case-a")
    inventory = store.prepare_resume(deepcopy(manifest))

    assert inventory.expected == 3
    assert inventory.succeeded == ("case-a",)
    assert inventory.not_run == ("case-b", "case-c")
    assert inventory.failed == ()
    assert inventory.missing == ()
    assert inventory.corrupt == ()
    assert inventory.runnable == ("case-b", "case-c")
    assert inventory.blocked == ()
    assert store.load_completed("case-a")["result"]["tool_outputs"] == {
        "case_id": "case-a",
        "attempt": 1,
    }


def test_failed_attempt_is_preserved_when_retry_completes(tmp_path) -> None:
    store = ExperimentStore.create(tmp_path / "experiments", _manifest())
    failed_path = _write_attempt(
        store,
        "case-b",
        attempt=1,
        status="failed",
        recorded_at="2026-09-20T00:00:01Z",
        result={"error": {"code": "controlled_failure"}},
    )
    failed_bytes = failed_path.read_bytes()
    failed_inventory = store.scan()
    assert failed_inventory.failed == ("case-b",)
    assert failed_inventory.not_run == ("case-a", "case-c")

    _write_success(store, "case-b", attempt=2)
    final_inventory = store.scan()
    assert final_inventory.succeeded == ("case-b",)
    assert failed_path.read_bytes() == failed_bytes
    assert store.attempt_paths("case-b") == (
        failed_path,
        failed_path.with_name("attempt-0002.json"),
    )


@pytest.mark.parametrize("change", ["prompt", "corpus", "cases"])
def test_incompatible_resume_is_rejected_before_artifact_scan(
    tmp_path,
    monkeypatch,
    change: str,
) -> None:
    original = _manifest()
    store = ExperimentStore.create(tmp_path / "experiments", original)
    if change == "prompt":
        requested = _manifest(prompt_version="prompt-v2")
    elif change == "corpus":
        requested = _manifest(corpus_version="corpus-v2")
    else:
        requested = _manifest(case_ids=("case-a", "case-b", "case-d"))

    scanned = False

    def forbidden_scan():
        nonlocal scanned
        scanned = True
        raise AssertionError("incompatible resume scanned case artifacts")

    monkeypatch.setattr(store, "scan", forbidden_scan)
    with pytest.raises(ResumeCompatibilityError) as captured:
        store.prepare_resume(requested)

    assert scanned is False
    assert captured.value.differences
    assert captured.value.existing_hash != captured.value.requested_hash


def test_corrupt_complete_is_reported_and_never_overwritten(tmp_path) -> None:
    store = ExperimentStore.create(tmp_path / "experiments", _manifest())
    complete_path = _write_success(store, "case-a")
    complete_path.write_bytes(b'{"artifact_schema_version":1,"payload":')
    corrupt_bytes = complete_path.read_bytes()

    inventory = store.scan()
    assert inventory.corrupt == ("case-a",)
    assert inventory.succeeded == ()
    assert inventory.problems[0].code == "invalid_json"
    assert inventory.problems[0].path.endswith("complete.json")

    with pytest.raises(ArtifactCorruptionError):
        store.mark_complete("case-a", attempt=1)
    assert complete_path.read_bytes() == corrupt_bytes


def test_corrupt_attempt_and_duplicate_json_keys_fail_closed(tmp_path) -> None:
    store = ExperimentStore.create(tmp_path / "experiments", _manifest())
    attempt_path = _write_attempt(
        store,
        "case-c",
        attempt=1,
        status="failed",
        recorded_at="2026-09-20T00:00:01Z",
        result={"error": {"code": "first"}},
    )
    attempt_path.write_text(
        '{"artifact_schema_version":1,"artifact_schema_version":1}',
        encoding="utf-8",
    )
    corrupt_bytes = attempt_path.read_bytes()

    inventory = store.scan()
    assert inventory.corrupt == ("case-c",)
    assert inventory.failed == ()
    assert inventory.problems[0].code == "invalid_json"
    with pytest.raises(ArtifactCorruptionError):
        _write_attempt(
            store,
            "case-c",
            attempt=1,
            status="failed",
            recorded_at="2026-09-20T00:00:02Z",
            result={"error": {"code": "replacement"}},
        )
    assert attempt_path.read_bytes() == corrupt_bytes
    with pytest.raises(ArtifactCorruptionError):
        _write_attempt(
            store,
            "case-c",
            attempt=2,
            status="succeeded",
            recorded_at="2026-09-20T00:00:03Z",
            result={"ok": True},
        )
    assert not attempt_path.with_name("attempt-0002.json").exists()


def test_attempt_identity_and_case_paths_are_fail_closed(tmp_path) -> None:
    root = tmp_path / "experiments"
    with pytest.raises(ExperimentContractError):
        ExperimentStore.create(root, _manifest(experiment_id="../escape"))

    store = ExperimentStore.create(root, _manifest(case_ids=("../case-a",)))
    attempt_path = _write_attempt(
        store,
        "../case-a",
        attempt=1,
        status="succeeded",
        recorded_at="2026-09-20T00:00:01Z",
        result={"ok": True},
    )
    assert attempt_path.resolve().is_relative_to(store.directory.resolve())
    assert "../case-a" not in attempt_path.as_posix()
    with pytest.raises(ExperimentContractError):
        _write_attempt(
            store,
            "unknown-case",
            attempt=1,
            status="succeeded",
            recorded_at="2026-09-20T00:00:01Z",
            result={"ok": True},
        )


def test_attempt_and_complete_are_immutable(tmp_path) -> None:
    store = ExperimentStore.create(tmp_path / "experiments", _manifest())
    attempt_path = _write_attempt(
        store,
        "case-a",
        attempt=1,
        status="succeeded",
        recorded_at="2026-09-20T00:00:01Z",
        result={"value": "first"},
    )
    same_path = _write_attempt(
        store,
        "case-a",
        attempt=1,
        status="succeeded",
        recorded_at="2026-09-20T00:00:01Z",
        result={"value": "first"},
    )
    assert same_path == attempt_path
    with pytest.raises(ArtifactConflictError):
        _write_attempt(
            store,
            "case-a",
            attempt=1,
            status="succeeded",
            recorded_at="2026-09-20T00:00:01Z",
            result={"value": "second"},
        )

    complete_path = store.mark_complete("case-a", attempt=1)
    assert store.mark_complete("case-a", attempt=1) == complete_path
    with pytest.raises(ArtifactConflictError):
        store.mark_complete("case-a", attempt=2)
    with pytest.raises(ArtifactConflictError):
        _write_attempt(
            store,
            "case-a",
            attempt=2,
            status="succeeded",
            recorded_at="2026-09-20T00:00:02Z",
            result={"value": "late"},
        )


def test_succeeded_attempt_without_marker_is_pending_not_runnable(tmp_path) -> None:
    store = ExperimentStore.create(tmp_path / "experiments", _manifest())
    _write_attempt(
        store,
        "case-a",
        attempt=1,
        status="succeeded",
        recorded_at="2026-09-20T00:00:01Z",
        result={"value": "ready-to-commit"},
    )
    inventory = store.scan()
    assert inventory.pending_commit == ("case-a",)
    assert inventory.runnable == ("case-b", "case-c")
    assert "case-a" not in inventory.runnable

    loaded_attempts = store.load_attempts("case-a")
    assert loaded_attempts[0]["attempt"] == 1
    loaded_attempts[0]["result"]["value"] = "caller-mutation"
    assert store.load_attempts("case-a")[0]["result"]["value"] == "ready-to-commit"

    complete_path = store.commit_pending("case-a")
    assert complete_path.name == "complete.json"
    assert store.commit_pending("case-a") == complete_path
    assert store.scan().succeeded == ("case-a",)


def test_failed_attempt_cannot_be_marked_complete(tmp_path) -> None:
    store = ExperimentStore.create(tmp_path / "experiments", _manifest())
    _write_attempt(
        store,
        "case-a",
        attempt=1,
        status="failed",
        recorded_at="2026-09-20T00:00:01Z",
        result={"error": {"code": "controlled"}},
    )
    with pytest.raises(ExperimentContractError):
        store.mark_complete("case-a", attempt=1)


def test_complete_marker_binds_attempt_and_all_prior_attempts(tmp_path) -> None:
    store = ExperimentStore.create(tmp_path / "experiments", _manifest())
    first_attempt = _write_attempt(
        store,
        "case-a",
        attempt=1,
        status="failed",
        recorded_at="2026-09-20T00:00:01Z",
        result={"error": {"code": "retryable"}},
    )
    _write_success(store, "case-a", attempt=2)

    first_attempt.write_bytes(b'{"payload":')
    inventory = store.scan()
    assert inventory.corrupt == ("case-a",)
    assert inventory.succeeded == ()

    second_complete_path = _write_success(store, "case-b")
    marker = json.loads(second_complete_path.read_text(encoding="utf-8"))
    marker["payload"]["attempt_artifact_sha256"] = "0" * 64
    marker["payload_sha256"] = canonical_hash(marker["payload"])
    second_complete_path.write_text(json.dumps(marker), encoding="utf-8")
    inventory = store.scan()
    assert inventory.corrupt == ("case-a", "case-b")
    assert {problem.code for problem in inventory.problems} == {
        "invalid_json",
        "complete_attempt_mismatch",
    }


def test_resume_rejects_a_different_experiment_id_even_when_identity_matches(
    tmp_path,
) -> None:
    store = ExperimentStore.create(tmp_path / "experiments", _manifest())
    requested = _manifest(experiment_id="different-experiment")
    with pytest.raises(ResumeCompatibilityError) as captured:
        store.prepare_resume(requested)
    assert captured.value.differences == ("$.experiment_id",)


def test_compatible_provenance_drift_keeps_stored_manifest_identity(tmp_path) -> None:
    stored = _manifest()
    store = ExperimentStore.create(tmp_path / "experiments", stored)
    requested = _manifest(
        created_at="2026-09-21T00:00:00Z",
        environment_label="test-b",
        default_cache_mode="replay",
    )
    assert (
        stored["identity"]["resume_compatibility_hash"]
        == requested["identity"]["resume_compatibility_hash"]
    )
    assert stored["identity"]["manifest_hash"] != requested["identity"]["manifest_hash"]
    store.prepare_resume(requested)

    _write_attempt(
        store,
        "case-a",
        attempt=1,
        status="succeeded",
        recorded_at="2026-09-21T00:00:01Z",
        result={"ok": True},
        cache_mode="replay",
        environment_label="test-b",
    )
    store.mark_complete("case-a", attempt=1)
    completed = store.load_completed("case-a")
    assert completed["manifest_hash"] == stored["identity"]["manifest_hash"]
    assert completed["manifest_hash"] != requested["identity"]["manifest_hash"]
    assert completed["cache_mode"] == "replay"
    assert completed["execution_environment"]["label"] == "test-b"
    assert completed["environment_fingerprint"] == canonical_hash(
        {"python": "3.12.13", "platform": "test", "label": "test-b"}
    )


def test_unknown_case_directory_is_reported_without_changing_expected_counts(
    tmp_path,
) -> None:
    store = ExperimentStore.create(tmp_path / "experiments", _manifest())
    unknown = store.directory / "cases" / "999999-unknown"
    unknown.mkdir(parents=True)
    (unknown / "attempt-0001.json").write_text("{}", encoding="utf-8")
    inventory = store.scan()
    assert inventory.expected == 3
    assert inventory.not_run == ("case-a", "case-b", "case-c")
    assert inventory.problems[-1].code == "unexpected_case_path"
    assert inventory.problems[-1].case_id is None
    assert inventory.global_problems == (inventory.problems[-1],)
    assert inventory.runnable == ()


@pytest.mark.parametrize("experiment_id", ["../escape", "CON", "abc.", "Uppercase"])
def test_unsafe_experiment_ids_are_rejected(tmp_path, experiment_id: str) -> None:
    with pytest.raises(ExperimentContractError):
        ExperimentStore.create(
            tmp_path / "experiments",
            _manifest(experiment_id=experiment_id),
        )


def test_concurrent_different_attempt_writers_never_overwrite(
    tmp_path,
    monkeypatch,
) -> None:
    store = ExperimentStore.create(tmp_path / "experiments", _manifest())
    barrier = threading.Barrier(2)
    real_link = experiment_store_module.os.link

    def racing_link(source, destination):
        barrier.wait(timeout=5)
        return real_link(source, destination)

    monkeypatch.setattr(experiment_store_module.os, "link", racing_link)

    def write(value: str) -> Path:
        return _write_attempt(
            store,
            "case-a",
            attempt=1,
            status="succeeded",
            recorded_at="2026-09-20T00:00:01Z",
            result={"value": value},
        )

    successes: list[Path] = []
    conflicts = 0
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(write, value) for value in ("first", "second")]
        for future in futures:
            try:
                successes.append(future.result(timeout=10))
            except ArtifactConflictError:
                conflicts += 1

    assert len(successes) == 1
    assert conflicts == 1
    inventory = store.scan()
    assert inventory.pending_commit == ("case-a",)
    assert inventory.corrupt == ()


def test_publish_failure_leaves_no_partial_final_artifact(
    tmp_path, monkeypatch
) -> None:
    store = ExperimentStore.create(tmp_path / "experiments", _manifest())

    def denied_link(source, destination):
        raise PermissionError("controlled publish failure")

    monkeypatch.setattr(experiment_store_module.os, "link", denied_link)
    with pytest.raises(PermissionError):
        _write_attempt(
            store,
            "case-a",
            attempt=1,
            status="succeeded",
            recorded_at="2026-09-20T00:00:01Z",
            result={"ok": True},
        )
    assert store.attempt_paths("case-a") == ()
    assert store.scan().not_run == ("case-a", "case-b", "case-c")


def test_manifest_and_case_set_tampering_are_rejected(tmp_path) -> None:
    manifest = _manifest()
    store = ExperimentStore.create(tmp_path / "experiments", manifest)
    manifest_path = store.directory / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["dataset"]["cases"][0]["case_id"] = "tampered"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ArtifactCorruptionError):
        store.load_manifest()


@pytest.mark.parametrize(
    "fault",
    ["boolean_ordinal", "duplicate_case_id", "wrong_count", "wrong_case_set_hash"],
)
def test_case_registry_contract_rejects_ambiguous_identity(
    tmp_path, fault: str
) -> None:
    valid = _manifest()
    dataset = deepcopy(valid["dataset"])
    if fault == "boolean_ordinal":
        dataset["cases"][0]["ordinal"] = False
        dataset["case_set_hash"] = canonical_hash(dataset["cases"])
    elif fault == "duplicate_case_id":
        dataset["cases"][1]["case_id"] = dataset["cases"][0]["case_id"]
        dataset["case_set_hash"] = canonical_hash(dataset["cases"])
    elif fault == "wrong_count":
        dataset["case_count"] += 1
    else:
        dataset["case_set_hash"] = "0" * 64
    malformed = _rebuild_with_dataset(valid, dataset)

    with pytest.raises(ExperimentContractError):
        ExperimentStore.create(tmp_path / "experiments", malformed)


def test_interrupted_attempt_is_runnable_only_within_retry_budget(tmp_path) -> None:
    store = ExperimentStore.create(tmp_path / "experiments", _manifest())
    _write_attempt(
        store,
        "case-a",
        attempt=1,
        status="interrupted",
        recorded_at="2026-09-20T00:00:01Z",
        result={"error": {"code": "controlled_interrupt"}},
    )

    first_inventory = store.scan()
    assert first_inventory.interrupted == ("case-a",)
    assert first_inventory.exhausted == ()
    assert first_inventory.pending_commit == ()
    assert first_inventory.runnable == ("case-a", "case-b", "case-c")

    _write_attempt(
        store,
        "case-a",
        attempt=2,
        status="interrupted",
        recorded_at="2026-09-20T00:00:02Z",
        result={"error": {"code": "second_interrupt"}},
    )
    exhausted_inventory = store.scan()
    assert exhausted_inventory.interrupted == ()
    assert exhausted_inventory.exhausted == ("case-a",)
    assert exhausted_inventory.runnable == ("case-b", "case-c")
    assert exhausted_inventory.blocked == ("case-a",)

    with pytest.raises(ExperimentContractError, match="retry budget"):
        _write_attempt(
            store,
            "case-a",
            attempt=3,
            status="succeeded",
            recorded_at="2026-09-20T00:00:03Z",
            result={"ok": True},
        )
    assert not store.attempt_paths("case-a")[-1].with_name("attempt-0003.json").exists()


def test_out_of_range_complete_attempt_is_reported_as_corrupt(tmp_path) -> None:
    store = ExperimentStore.create(tmp_path / "experiments", _manifest())
    complete_path = _write_success(store, "case-a")
    marker = json.loads(complete_path.read_text(encoding="utf-8"))
    marker["payload"]["attempt"] = 1_000_000
    marker["payload_sha256"] = canonical_hash(marker["payload"])
    complete_path.write_text(json.dumps(marker), encoding="utf-8")

    inventory = store.scan()
    assert inventory.corrupt == ("case-a",)
    assert inventory.problems[0].code == "invalid_attempt"


@pytest.mark.parametrize(
    ("field", "replacement", "expected_code"),
    [
        ("artifact_schema_version", True, "schema_mismatch"),
        ("attempt", True, "identity_mismatch"),
        ("ordinal", False, "identity_mismatch"),
    ],
)
def test_boolean_values_cannot_alias_integer_artifact_identity(
    tmp_path,
    field: str,
    replacement: bool,
    expected_code: str,
) -> None:
    store = ExperimentStore.create(tmp_path / "experiments", _manifest())
    attempt_path = _write_attempt(
        store,
        "case-a",
        attempt=1,
        status="failed",
        recorded_at="2026-09-20T00:00:01Z",
        result={"error": {"code": "controlled"}},
    )
    envelope = json.loads(attempt_path.read_text(encoding="utf-8"))
    if field == "artifact_schema_version":
        envelope[field] = replacement
    else:
        envelope["payload"][field] = replacement
        envelope["payload_sha256"] = canonical_hash(envelope["payload"])
    attempt_path.write_text(json.dumps(envelope), encoding="utf-8")

    inventory = store.scan()
    assert inventory.corrupt == ("case-a",)
    assert inventory.problems[0].code == expected_code


def test_reparse_case_entry_is_corrupt_and_never_runnable(
    tmp_path, monkeypatch
) -> None:
    store = ExperimentStore.create(tmp_path / "experiments", _manifest())
    case_directory = store._case_directory(store._case("case-a"))
    real_entry_exists = experiment_store_module._path_entry_exists
    real_is_reparse = experiment_store_module._is_reparse_point

    monkeypatch.setattr(
        experiment_store_module,
        "_path_entry_exists",
        lambda path: True if path == case_directory else real_entry_exists(path),
    )
    monkeypatch.setattr(
        experiment_store_module,
        "_is_reparse_point",
        lambda path: True if path == case_directory else real_is_reparse(path),
    )

    inventory = store.scan()
    assert inventory.corrupt == ("case-a",)
    assert inventory.problems[0].code == "unsafe_case_path"
    assert "case-a" not in inventory.runnable


def test_reparse_cases_root_blocks_all_runnable_cases(tmp_path, monkeypatch) -> None:
    store = ExperimentStore.create(tmp_path / "experiments", _manifest())
    cases_root = store.directory / "cases"
    real_entry_exists = experiment_store_module._path_entry_exists
    real_is_reparse = experiment_store_module._is_reparse_point

    monkeypatch.setattr(
        experiment_store_module,
        "_path_entry_exists",
        lambda path: True if path == cases_root else real_entry_exists(path),
    )
    monkeypatch.setattr(
        experiment_store_module,
        "_is_reparse_point",
        lambda path: True if path == cases_root else real_is_reparse(path),
    )

    inventory = store.scan()
    assert inventory.global_problems[0].code == "unsafe_cases_root"
    assert inventory.runnable == ()


def test_temp_cleanup_failure_does_not_reverse_a_committed_attempt(
    tmp_path,
    monkeypatch,
) -> None:
    store = ExperimentStore.create(tmp_path / "experiments", _manifest())

    def blocked_cleanup(path):
        raise PermissionError("controlled Windows cleanup failure")

    monkeypatch.setattr(experiment_store_module.os, "unlink", blocked_cleanup)
    attempt_path = _write_attempt(
        store,
        "case-a",
        attempt=1,
        status="succeeded",
        recorded_at="2026-09-20T00:00:01Z",
        result={"ok": True},
    )

    assert attempt_path.is_file()
    assert any(
        path.name.startswith(".attempt-0001.json.")
        for path in attempt_path.parent.iterdir()
    )
    inventory = store.scan()
    assert inventory.pending_commit == ("case-a",)
    assert inventory.corrupt == ()


def test_boolean_attempt_cannot_publish_a_corrupt_complete_marker(tmp_path) -> None:
    store = ExperimentStore.create(tmp_path / "experiments", _manifest())
    _write_attempt(
        store,
        "case-a",
        attempt=1,
        status="succeeded",
        recorded_at="2026-09-20T00:00:01Z",
        result={"ok": True},
    )

    with pytest.raises(ExperimentContractError, match="attempt must be an integer"):
        store.mark_complete("case-a", attempt=True)

    assert not (store.attempt_paths("case-a")[0].parent / "complete.json").exists()
    inventory = store.scan()
    assert inventory.pending_commit == ("case-a",)
    assert inventory.corrupt == ()


@pytest.mark.parametrize("entry_point", ["create", "open"])
def test_experiment_directory_reparse_cannot_escape_artifact_root(
    tmp_path,
    monkeypatch,
    entry_point: str,
) -> None:
    declared_root = tmp_path / "declared"
    declared_root.mkdir()
    unsafe_directory = declared_root / "m2-store-test"
    real_entry_exists = experiment_store_module._path_entry_exists
    real_is_reparse = experiment_store_module._is_reparse_point

    monkeypatch.setattr(
        experiment_store_module,
        "_path_entry_exists",
        lambda path: True if path == unsafe_directory else real_entry_exists(path),
    )
    monkeypatch.setattr(
        experiment_store_module,
        "_is_reparse_point",
        lambda path: True if path == unsafe_directory else real_is_reparse(path),
    )

    with pytest.raises(ArtifactCorruptionError) as captured:
        if entry_point == "create":
            ExperimentStore.create(declared_root, _manifest())
        else:
            ExperimentStore.open(declared_root, "m2-store-test")

    assert captured.value.code == "unsafe_experiment_directory"


def test_noncanonical_attempt_filename_cannot_alias_an_attempt(tmp_path) -> None:
    store = ExperimentStore.create(tmp_path / "experiments", _manifest())
    canonical_path = _write_attempt(
        store,
        "case-a",
        attempt=1,
        status="failed",
        recorded_at="2026-09-20T00:00:01Z",
        result={"error": {"code": "controlled"}},
    )
    canonical_path.rename(canonical_path.with_name("attempt-00001.json"))

    inventory = store.scan()
    assert inventory.corrupt == ("case-a",)
    assert inventory.problems[0].code == "noncanonical_attempt_path"
    assert "case-a" not in inventory.runnable
