from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from legal_rag import cli
from legal_rag.experiment_aggregation import (
    aggregate_experiment,
    validate_aggregation_bundle,
)
from legal_rag.experiment_lifecycle import (
    ExperimentLifecycleError,
    aggregate_experiment_by_id,
    build_lifecycle_plan,
    publish_aggregation,
    replay_experiment,
    resume_experiment,
    run_experiment,
    validate_no_external_calls,
)
from legal_rag.experiment_runtime import build_experiment_manifest, canonical_json_bytes
from legal_rag.experiment_store import ArtifactConflictError, ExperimentStore


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
OFFLINE_CORPUS = REPOSITORY_ROOT / "tests/fixtures/synthetic/synthetic_non_law.txt"


def _experiment_root(tmp_path: Path) -> Path:
    return tmp_path / "experiments"


def _files(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _rebuild_manifest(
    manifest: dict,
    *,
    contracts: dict,
) -> dict:
    return build_experiment_manifest(
        experiment_id=manifest["experiment_id"],
        execution_mode=manifest["execution_mode"],
        default_cache_mode=manifest["cache_policy"]["default_mode"],
        code=manifest["code"],
        config_summary=manifest["config"]["summary"],
        corpus=manifest["corpus"],
        dataset=manifest["dataset"],
        contracts=contracts,
        runtime=manifest["runtime"],
        environment=manifest["environment"],
        created_at=manifest["created_at"],
    )


def test_plan_is_read_only_registry_backed_and_generation_fails_closed(
    tmp_path: Path,
) -> None:
    experiments = _experiment_root(tmp_path)
    before = _files(tmp_path)
    plan = build_lifecycle_plan(
        experiment_id="offline-plan",
        repository_root=REPOSITORY_ROOT,
    )

    assert _files(tmp_path) == before
    assert not experiments.exists()
    assert plan["runnable"] is True
    assert plan["network_policy"] == "forbidden"
    manifest = plan["manifest"]
    assert manifest["execution_mode"] == "offline"
    assert manifest["dataset"]["dataset_id"] == "synthetic-offline-v1"
    assert manifest["dataset"]["case_count"] == 2
    assert manifest["config"]["summary"]["allow_external_calls"] is False
    assert manifest["contracts"]["retrieval"]["kind"] == "bm25"
    assert manifest["code"]["commit"]
    assert len(manifest["code"]["stage_implementation_fingerprints"]) == 9

    generated = build_lifecycle_plan(
        experiment_id="generation-plan",
        mode="smoke-generation",
        corpus_path=OFFLINE_CORPUS,
        repository_root=REPOSITORY_ROOT,
    )
    assert generated["runnable"] is False
    assert "budgeted provider" in generated["blocked_reason"]
    with pytest.raises(ExperimentLifecycleError, match="budgeted provider"):
        run_experiment(
            experiment_id="generation-run",
            mode="smoke-generation",
            corpus_path=OFFLINE_CORPUS,
            repository_root=REPOSITORY_ROOT,
            experiment_root=experiments,
        )
    assert not (experiments / "generation-run").exists()


def test_retrieval_mode_requires_an_explicit_corpus() -> None:
    with pytest.raises(ExperimentLifecycleError, match="explicit --corpus"):
        build_lifecycle_plan(
            experiment_id="retrieval-plan",
            mode="retrieval",
            repository_root=REPOSITORY_ROOT,
        )


def test_run_resume_skips_completed_cases_and_rejects_existing_run(
    tmp_path: Path,
) -> None:
    experiments = _experiment_root(tmp_path)
    first = run_experiment(
        experiment_id="resume-demo",
        repository_root=REPOSITORY_ROOT,
        experiment_root=experiments,
        stop_after_completed=1,
    )
    assert first.summary.status == "interrupted"
    assert first.summary.completed_case_ids == ("synthetic_offline_amber_token",)

    resumed = resume_experiment(
        experiment_id="resume-demo",
        repository_root=REPOSITORY_ROOT,
        experiment_root=experiments,
    )
    assert resumed.summary.status == "succeeded"
    assert resumed.summary.skipped_case_ids == ("synthetic_offline_amber_token",)
    assert resumed.summary.completed_case_ids == ("synthetic_offline_real_law_refusal",)
    store = ExperimentStore.open(experiments, "resume-demo")
    assert len(store.load_attempts("synthetic_offline_amber_token")) == 1
    assert len(store.load_attempts("synthetic_offline_real_law_refusal")) == 1

    with pytest.raises(ExperimentLifecycleError, match="use resume"):
        run_experiment(
            experiment_id="resume-demo",
            repository_root=REPOSITORY_ROOT,
            experiment_root=experiments,
        )


def test_resume_rebuilds_current_corpus_facts_and_rejects_drift(
    tmp_path: Path,
) -> None:
    experiments = _experiment_root(tmp_path)
    run_experiment(
        experiment_id="drift-demo",
        repository_root=REPOSITORY_ROOT,
        experiment_root=experiments,
        stop_after_completed=1,
    )
    changed = tmp_path / "changed.txt"
    changed.write_text(
        OFFLINE_CORPUS.read_text(encoding="utf-8") + "\n额外的虚构行。\n",
        encoding="utf-8",
    )
    with pytest.raises(ExperimentLifecycleError, match="tracked synthetic corpus"):
        resume_experiment(
            experiment_id="drift-demo",
            repository_root=REPOSITORY_ROOT,
            experiment_root=experiments,
            corpus_path=changed,
        )


def test_fresh_replay_and_aggregation_are_exact_read_only_and_zero_call(
    tmp_path: Path,
) -> None:
    experiments = _experiment_root(tmp_path)
    fresh = run_experiment(
        experiment_id="fresh-source",
        repository_root=REPOSITORY_ROOT,
        experiment_root=experiments,
    )
    source_store = ExperimentStore.open(experiments, "fresh-source")
    source_before = _files(source_store.directory)

    replay = replay_experiment(
        source_experiment_id="fresh-source",
        experiment_id="exact-replay",
        repository_root=REPOSITORY_ROOT,
        experiment_root=experiments,
    )
    assert replay.summary.status == "succeeded"
    assert _files(source_store.directory) == source_before
    replay_store = ExperimentStore.open(experiments, "exact-replay")
    for case_id in replay_store.scan().case_order:
        attempts = replay_store.load_attempts(case_id)
        assert len(attempts) == 1
        assert attempts[0]["cache_mode"] == "replay"
        observations = attempts[0]["result"]["stage_observations"]
        assert observations["query_analysis"]["origin"] == "replay"
        assert observations["retrieval"]["origin"] == "replay"

        fresh_facts = source_store.load_completed(case_id)["result"]["output"][
            "scoring_facts"
        ]
        replay_facts = replay_store.load_completed(case_id)["result"]["output"][
            "scoring_facts"
        ]
        for key in ("analysis", "results", "evidence_check", "adaptive_trace"):
            assert replay_facts[key] == fresh_facts[key]

    validate_no_external_calls([fresh, replay])
    replay_bundle = aggregate_experiment(replay_store)
    assert replay_bundle["attempt_audit"]["actual_calls"]["totals"]["attempted"] == 0
    assert replay_bundle["attempt_audit"]["cache_modes"] == {"replay": 2}

    first_publication = publish_aggregation(replay_store)
    published_before = _files(first_publication.directory)
    second_publication = aggregate_experiment_by_id(
        experiment_id="exact-replay",
        repository_root=REPOSITORY_ROOT,
        experiment_root=experiments,
    )
    assert second_publication.to_dict() == first_publication.to_dict()
    assert _files(first_publication.directory) == published_before
    assert set(first_publication.files) == {
        "summary.json",
        "cases.jsonl",
        "cases.csv",
        "report.md",
        "complete.json",
    }
    summary = json.loads(
        (first_publication.directory / "summary.json").read_text(encoding="utf-8")
    )
    validate_aggregation_bundle(
        summary,
        expected_manifest=replay_store.load_manifest(),
        expected_bundle_hash=first_publication.bundle_hash,
    )

    with pytest.raises(ExperimentLifecycleError, match="new experiment_id"):
        replay_experiment(
            source_experiment_id="fresh-source",
            experiment_id="fresh-source",
            repository_root=REPOSITORY_ROOT,
            experiment_root=experiments,
        )


def test_replay_cache_miss_fails_without_provider_fallback(tmp_path: Path) -> None:
    experiments = _experiment_root(tmp_path)
    run_experiment(
        experiment_id="fresh-source",
        repository_root=REPOSITORY_ROOT,
        experiment_root=experiments,
    )
    empty_cache = tmp_path / "empty-cache"
    with pytest.raises(ExperimentLifecycleError, match="exact replay did not complete"):
        replay_experiment(
            source_experiment_id="fresh-source",
            experiment_id="missing-replay",
            repository_root=REPOSITORY_ROOT,
            experiment_root=experiments,
            cache_root=empty_cache,
        )
    store = ExperimentStore.open(experiments, "missing-replay")
    inventory = store.scan()
    assert inventory.succeeded == ()
    assert inventory.exhausted == (
        "synthetic_offline_amber_token",
        "synthetic_offline_real_law_refusal",
    )
    attempt = store.load_attempts("synthetic_offline_amber_token")[0]
    assert (
        sum(
            item["attempted"]
            for item in attempt["result"]["call_ledger"]["actual"].values()
        )
        == 0
    )
    assert attempt["result"]["error"]["code"] == "replay_cache_miss"


def test_corrupt_case_is_reported_and_publication_conflicts_are_rejected(
    tmp_path: Path,
) -> None:
    experiments = _experiment_root(tmp_path)
    run_experiment(
        experiment_id="corrupt-demo",
        repository_root=REPOSITORY_ROOT,
        experiment_root=experiments,
    )
    store = ExperimentStore.open(experiments, "corrupt-demo")
    attempt_path = store.attempt_paths("synthetic_offline_amber_token")[0]
    attempt_path.write_text("{not-json", encoding="utf-8")

    publication = publish_aggregation(store)
    bundle = json.loads((publication.directory / "summary.json").read_text("utf-8"))
    assert bundle["case_inventory"]["counts"]["corrupt"] == 1
    corrupt = next(item for item in bundle["cases"] if item["status"] == "corrupt")
    assert corrupt["evaluation_record"] is None
    assert corrupt["trusted_attempt_count"] == 0

    report = publication.directory / "report.md"
    report.write_text("tampered", encoding="utf-8")
    with pytest.raises(ArtifactConflictError, match="invalid file"):
        publish_aggregation(store)


def test_stage_contract_invalidation_is_directional() -> None:
    manifest = build_lifecycle_plan(
        experiment_id="stage-contracts",
        repository_root=REPOSITORY_ROOT,
    )["manifest"]

    prompt_contracts = deepcopy(manifest["contracts"])
    prompt_contracts["generation"]["prompt_version"] = "changed-prompt-v2"
    prompt_changed = _rebuild_manifest(manifest, contracts=prompt_contracts)
    assert (
        prompt_changed["stage_contracts"]["embedding"]
        == manifest["stage_contracts"]["embedding"]
    )
    assert (
        prompt_changed["stage_contracts"]["retrieval"]
        == manifest["stage_contracts"]["retrieval"]
    )
    assert (
        prompt_changed["stage_contracts"]["generation"]
        != manifest["stage_contracts"]["generation"]
    )

    embedding_contracts = deepcopy(manifest["contracts"])
    embedding_contracts["embedding"]["revision"] = "changed-embedding-v2"
    embedding_changed = _rebuild_manifest(manifest, contracts=embedding_contracts)
    assert (
        embedding_changed["stage_contracts"]["embedding"]
        != manifest["stage_contracts"]["embedding"]
    )
    assert (
        embedding_changed["stage_contracts"]["retrieval"]
        != manifest["stage_contracts"]["retrieval"]
    )
    assert (
        embedding_changed["stage_contracts"]["generation"]
        == manifest["stage_contracts"]["generation"]
    )

    chunk_contracts = deepcopy(manifest["contracts"])
    chunk_contracts["chunking"]["version"] = "article-v2"
    chunk_changed = _rebuild_manifest(manifest, contracts=chunk_contracts)
    for stage in ("embedding", "retrieval", "rerank"):
        assert (
            chunk_changed["stage_contracts"][stage]
            != manifest["stage_contracts"][stage]
        )


def test_experiment_cli_does_not_load_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def forbidden() -> None:
        raise AssertionError("experiment commands must not load dotenv")

    monkeypatch.setattr(cli, "load_dotenv", forbidden)
    code = cli.main(
        [
            "experiment",
            "plan",
            "--experiment-id",
            "cli-plan",
            "--repository-root",
            str(REPOSITORY_ROOT),
            "--experiment-root",
            str(_experiment_root(tmp_path)),
        ]
    )
    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["manifest"]["experiment_id"] == "cli-plan"
    assert not _experiment_root(tmp_path).exists()

    code = cli.main(
        [
            "experiment",
            "run",
            "--experiment-id",
            "cli-interrupted",
            "--repository-root",
            str(REPOSITORY_ROOT),
            "--experiment-root",
            str(_experiment_root(tmp_path)),
            "--stop-after-completed",
            "1",
        ]
    )
    assert code == 3
    interrupted = json.loads(capsys.readouterr().out)
    assert interrupted["summary"]["status"] == "interrupted"


def test_plan_canonical_json_is_stable_except_creation_time() -> None:
    kwargs = {
        "experiment_id": "stable-plan",
        "repository_root": REPOSITORY_ROOT,
        "created_at": "2026-09-22T00:00:00Z",
        "manifest_environment": {"python": "test", "platform": "test"},
    }
    first = build_lifecycle_plan(**kwargs)
    second = build_lifecycle_plan(**kwargs)
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
