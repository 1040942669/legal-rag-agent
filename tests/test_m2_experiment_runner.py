from __future__ import annotations

import json
import threading
import time
from collections import Counter
from copy import deepcopy
from typing import Any

import pytest

from legal_rag.experiment_runner import (
    OBSERVATION_STAGES,
    CaseExecution,
    CaseExecutionError,
    ExperimentRunner,
    RunnerContractError,
    StageObservation,
    plan_work_units,
)
from legal_rag.experiment_runtime import (
    EXTERNAL_CALL_KINDS,
    build_experiment_manifest,
    canonical_hash,
)
from legal_rag.experiment_store import ExperimentStore, ResumeCompatibilityError


def _manifest(
    *,
    experiment_id: str = "m2-runner-test",
    case_specs: tuple[tuple[str, str | None, int], ...] = (
        ("case-a", None, 0),
        ("case-b", None, 0),
        ("case-c", None, 0),
    ),
    concurrency: int = 1,
    max_retries: int = 1,
    provider_limit: int | None = None,
    prompt_version: str = "prompt-v1",
) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    for ordinal, (case_id, session_group, turn_index) in enumerate(case_specs):
        identity = {
            "case_id": case_id,
            "question": f"question-{case_id}",
            "session_group": session_group,
            "turn_index": turn_index,
        }
        case = {
            "ordinal": ordinal,
            "case_id": case_id,
            "case_hash": canonical_hash(identity),
            "question": identity["question"],
            "turn_index": turn_index,
        }
        if session_group is not None:
            case["session_group"] = session_group
        cases.append(case)
    provider_limits = {
        kind: provider_limit if provider_limit is not None else concurrency
        for kind in EXTERNAL_CALL_KINDS
    }
    return build_experiment_manifest(
        experiment_id=experiment_id,
        execution_mode="offline",
        default_cache_mode="fresh",
        code={
            "commit": "c" * 40,
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
            "snapshot_hash": canonical_hash("runner-corpus"),
            "index_hash": canonical_hash("runner-index"),
        },
        dataset={
            "dataset_id": "synthetic-m2-runner",
            "role": "test_fixture",
            "case_file_hash": canonical_hash(cases),
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
                "kind": "fake",
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
            "concurrency": concurrency,
            "max_retries": max_retries,
            "timing_scope": "runner_stage_wall_clock",
            "provider_limits": provider_limits,
            "retry_backoff_ms": 0,
        },
        environment={"python": "3.12.13", "platform": "test"},
        created_at="2026-09-20T00:00:00Z",
    )


def _environment() -> dict[str, str]:
    return {"python": "3.12.13", "platform": "test", "worker": "fake"}


def _observations(
    *,
    origin: str,
    source_other_calls: int,
    duration_ms: float = 1.0,
) -> dict[str, StageObservation]:
    observations = {
        stage: StageObservation.not_run("not_used_by_fake")
        for stage in OBSERVATION_STAGES
    }
    observations["query_analysis"] = StageObservation(
        status="succeeded",
        origin="fresh",
        duration_ms=0.25,
    )
    observations["retrieval"] = StageObservation(
        status="succeeded",
        origin=origin,
        duration_ms=duration_ms,
        cache_key=canonical_hash({"stage": "retrieval", "fixture": "stable"}),
        source_external_calls={"other": source_other_calls},
    )
    return observations


class _HistoryFactory:
    def __init__(self, *, provider_delay: float = 0.0) -> None:
        self.provider_delay = provider_delay
        self.lock = threading.Lock()
        self.factory_calls: list[tuple[tuple[str, ...], dict[str, Any] | None]] = []
        self.execution_counts: Counter[str] = Counter()
        self.runtime_ids: list[int] = []
        self.active_workers = 0
        self.peak_workers = 0

    def __call__(self, work_unit, resume_state, controls):
        with self.lock:
            runtime_id = len(self.runtime_ids) + 1
            self.runtime_ids.append(runtime_id)
            self.factory_calls.append(
                (
                    work_unit.case_ids,
                    deepcopy(resume_state) if resume_state is not None else None,
                )
            )
        return _HistoryRuntime(
            factory=self,
            runtime_id=runtime_id,
            session_group=work_unit.session_group,
            state=resume_state,
            origin=controls.cache_mode,
        )


class _HistoryRuntime:
    def __init__(
        self,
        *,
        factory: _HistoryFactory,
        runtime_id: int,
        session_group: str | None,
        state: dict[str, Any] | None,
        origin: str,
    ) -> None:
        self.factory = factory
        self.runtime_id = runtime_id
        self.session_group = session_group
        self.history = list((state or {}).get("history", []))
        self.origin = origin

    def execute(self, case, controls) -> CaseExecution:
        case_id = case["case_id"]
        with self.factory.lock:
            self.factory.execution_counts[case_id] += 1
            self.factory.active_workers += 1
            self.factory.peak_workers = max(
                self.factory.peak_workers, self.factory.active_workers
            )
        try:
            if self.origin == "fresh":
                controls.call(
                    "retrieval",
                    "other",
                    lambda: time.sleep(self.factory.provider_delay),
                )
            self.history.append(case_id)
            output = {"case_id": case_id, "history": list(self.history)}
            return CaseExecution(
                output=output,
                stage_observations=_observations(
                    origin=self.origin,
                    source_other_calls=1,
                ),
                session_state_after=(
                    {"history": list(self.history)}
                    if self.session_group is not None
                    else None
                ),
            )
        finally:
            with self.factory.lock:
                self.factory.active_workers -= 1


class _ForbiddenFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, work_unit, resume_state, controls):
        self.calls += 1
        raise AssertionError("runtime factory must not be constructed")


def _runner(
    store: ExperimentStore,
    manifest: dict[str, Any],
    factory,
    *,
    cache_mode: str = "fresh",
) -> ExperimentRunner:
    return ExperimentRunner(
        store=store,
        requested_manifest=manifest,
        runtime_factory=factory,
        cache_mode=cache_mode,
        execution_environment=_environment(),
    )


def test_plan_groups_sessions_as_work_units_in_manifest_order() -> None:
    manifest = _manifest(
        case_specs=(
            ("single-a", None, 0),
            ("g1-0", "g1", 0),
            ("g1-1", "g1", 1),
            ("single-b", None, 0),
            ("g2-0", "g2", 0),
            ("g2-1", "g2", 1),
        )
    )

    units = plan_work_units(manifest)

    assert [unit.case_ids for unit in units] == [
        ("single-a",),
        ("g1-0", "g1-1"),
        ("single-b",),
        ("g2-0", "g2-1"),
    ]
    assert [unit.first_ordinal for unit in units] == [0, 1, 3, 4]


def test_m2_t03_resume_skips_completed_cases_without_reexecution(tmp_path) -> None:
    manifest = _manifest()
    root = tmp_path / "experiments"
    store = ExperimentStore.create(root, manifest)
    factory = _HistoryFactory()

    interrupted = _runner(store, manifest, factory).run(stop_after_completed=1)

    assert interrupted.status == "interrupted"
    assert interrupted.completed_case_ids == ("case-a",)
    assert interrupted.inventory.succeeded == ("case-a",)
    assert interrupted.inventory.not_run == ("case-b", "case-c")
    completed_bytes = (store.attempt_paths("case-a")[0]).read_bytes()

    reopened = ExperimentStore.open(root, manifest["experiment_id"])
    resumed = _runner(reopened, manifest, factory).run()

    assert resumed.status == "succeeded"
    assert resumed.skipped_case_ids == ("case-a",)
    assert resumed.completed_case_ids == ("case-b", "case-c")
    assert resumed.inventory.succeeded == ("case-a", "case-b", "case-c")
    assert factory.execution_counts == Counter({"case-a": 1, "case-b": 1, "case-c": 1})
    assert store.attempt_paths("case-a")[0].read_bytes() == completed_bytes


def test_resume_reconciles_pending_success_without_runtime_factory(tmp_path) -> None:
    manifest = _manifest(case_specs=(("case-a", None, 0),))
    root = tmp_path / "experiments"
    store = ExperimentStore.create(root, manifest)
    _runner(store, manifest, _HistoryFactory()).run()
    complete_path = store.attempt_paths("case-a")[0].parent / "complete.json"
    complete_path.unlink()
    assert store.scan().pending_commit == ("case-a",)
    forbidden = _ForbiddenFactory()

    reopened = ExperimentStore.open(root, manifest["experiment_id"])
    summary = _runner(reopened, manifest, forbidden).run()

    assert summary.status == "succeeded"
    assert summary.reconciled_case_ids == ("case-a",)
    assert summary.attempted_case_ids == ()
    assert forbidden.calls == 0
    assert len(reopened.attempt_paths("case-a")) == 1


def test_named_session_resume_restores_checkpoint_without_replaying_prefix(
    tmp_path,
) -> None:
    manifest = _manifest(
        case_specs=(
            ("g-0", "group", 0),
            ("g-1", "group", 1),
            ("g-2", "group", 2),
        )
    )
    root = tmp_path / "experiments"
    store = ExperimentStore.create(root, manifest)
    factory = _HistoryFactory()
    _runner(store, manifest, factory).run(stop_after_completed=1)

    reopened = ExperimentStore.open(root, manifest["experiment_id"])
    resumed = _runner(reopened, manifest, factory).run()

    assert resumed.status == "succeeded"
    assert factory.execution_counts == Counter({"g-0": 1, "g-1": 1, "g-2": 1})
    assert factory.factory_calls[1][1] == {"history": ["g-0"]}
    assert reopened.load_completed("g-1")["result"]["output"]["history"] == [
        "g-0",
        "g-1",
    ]
    assert reopened.load_completed("g-2")["result"]["output"]["history"] == [
        "g-0",
        "g-1",
        "g-2",
    ]


def test_retryable_failure_is_preserved_and_uses_a_fresh_runtime(tmp_path) -> None:
    manifest = _manifest(case_specs=(("case-a", None, 0),), max_retries=1)
    store = ExperimentStore.create(tmp_path / "experiments", manifest)

    class RetryFactory:
        def __init__(self) -> None:
            self.factory_calls = 0
            self.executions = 0

        def __call__(self, work_unit, resume_state, runner_controls):
            self.factory_calls += 1
            parent = self

            class Runtime:
                def execute(self, case, controls):
                    parent.executions += 1
                    if parent.executions == 1:
                        try:
                            controls.call(
                                "retrieval",
                                "other",
                                lambda: (_ for _ in ()).throw(
                                    TimeoutError("controlled timeout")
                                ),
                            )
                        except TimeoutError as exc:
                            raise CaseExecutionError(
                                "provider_timeout", retryable=True
                            ) from exc
                    controls.call("retrieval", "other", lambda: None)
                    return CaseExecution(
                        output={"ok": True},
                        stage_observations=_observations(
                            origin="fresh", source_other_calls=1
                        ),
                    )

            return Runtime()

    factory = RetryFactory()
    summary = _runner(store, manifest, factory).run()

    attempts = store.load_attempts("case-a")
    assert summary.status == "succeeded"
    assert factory.factory_calls == 2
    assert [attempt["status"] for attempt in attempts] == ["failed", "succeeded"]
    assert attempts[0]["result"]["error"] == {
        "code": "provider_timeout",
        "retryable": True,
    }
    assert attempts[0]["result"]["call_ledger"]["actual"]["other"]["failed"] == 1


def test_nonretryable_failure_is_not_retried_on_run_or_resume(tmp_path) -> None:
    manifest = _manifest(case_specs=(("case-a", None, 0),), max_retries=3)
    root = tmp_path / "experiments"
    store = ExperimentStore.create(root, manifest)

    class AuthFailureFactory:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, work_unit, resume_state, runner_controls):
            self.calls += 1

            class Runtime:
                def execute(self, case, controls):
                    raise CaseExecutionError("provider_auth", retryable=False)

            return Runtime()

    first_factory = AuthFailureFactory()
    first = _runner(store, manifest, first_factory).run()
    assert first.status == "completed_with_failures"
    assert first_factory.calls == 1
    assert len(store.load_attempts("case-a")) == 1

    forbidden = _ForbiddenFactory()
    reopened = ExperimentStore.open(root, manifest["experiment_id"])
    resumed = _runner(reopened, manifest, forbidden).run()
    assert resumed.status == "completed_with_failures"
    assert resumed.terminal_failure_case_ids == ("case-a",)
    assert forbidden.calls == 0
    assert len(reopened.load_attempts("case-a")) == 1


def test_m2_t05_concurrent_session_units_are_isolated_bounded_and_sorted(
    tmp_path,
) -> None:
    manifest = _manifest(
        case_specs=(
            ("g1-0", "g1", 0),
            ("g1-1", "g1", 1),
            ("single-a", None, 0),
            ("g2-0", "g2", 0),
            ("g2-1", "g2", 1),
            ("single-b", None, 0),
        ),
        concurrency=4,
        provider_limit=2,
    )
    store = ExperimentStore.create(tmp_path / "experiments", manifest)
    factory = _HistoryFactory(provider_delay=0.02)

    summary = _runner(store, manifest, factory).run()

    assert summary.status == "succeeded"
    assert summary.inventory.succeeded == tuple(
        case["case_id"] for case in manifest["dataset"]["cases"]
    )
    assert factory.execution_counts == Counter(
        {case["case_id"]: 1 for case in manifest["dataset"]["cases"]}
    )
    assert len(factory.runtime_ids) == 4
    assert factory.peak_workers > 2
    assert 1 <= summary.provider_peak_in_flight["other"] <= 2
    assert store.load_completed("g1-1")["result"]["output"]["history"] == [
        "g1-0",
        "g1-1",
    ]
    assert store.load_completed("g2-1")["result"]["output"]["history"] == [
        "g2-0",
        "g2-1",
    ]
    assert store.load_completed("single-a")["result"]["output"]["history"] == [
        "single-a"
    ]
    assert summary.inventory.corrupt == ()
    assert summary.inventory.pending_commit == ()


@pytest.mark.parametrize("cache_mode", ["fresh", "cache", "replay"])
def test_m2_t06_origin_timing_and_call_ledgers_are_separate(
    tmp_path,
    cache_mode: str,
) -> None:
    manifest = _manifest(
        experiment_id=f"m2-runner-{cache_mode}",
        case_specs=(("case-a", None, 0),),
    )
    store = ExperimentStore.create(tmp_path / "experiments", manifest)
    summary = _runner(
        store,
        manifest,
        _HistoryFactory(),
        cache_mode=cache_mode,
    ).run()

    result = store.load_completed("case-a")["result"]
    retrieval = result["stage_observations"]["retrieval"]
    actual = result["call_ledger"]["actual"]["other"]
    assert summary.status == "succeeded"
    assert retrieval["origin"] == cache_mode
    assert retrieval["source_external_calls"]["other"] == 1
    assert result["timings_ms"]["queue_wait"] >= 0
    assert result["timings_ms"]["end_to_end"] >= 0
    assert result["timings_ms"]["persistence_included"] is False
    if cache_mode == "fresh":
        assert actual["attempted"] == 1
        assert retrieval["external_calls"]["other"] == 1
    else:
        assert actual["attempted"] == 0
        assert retrieval["external_calls"]["other"] == 0


@pytest.mark.parametrize(
    ("cache_mode", "invalid_origin"),
    [
        ("fresh", "cache"),
        ("cache", "replay"),
        ("replay", "cache"),
    ],
)
def test_invalid_stage_origin_is_rejected_before_success_commit_and_can_resume(
    tmp_path,
    cache_mode: str,
    invalid_origin: str,
) -> None:
    manifest = _manifest(
        experiment_id=f"m2-invalid-origin-{cache_mode}",
        case_specs=(("case-a", None, 0),),
    )
    root = tmp_path / "experiments"
    store = ExperimentStore.create(root, manifest)

    def factory(work_unit, resume_state, runner_controls):
        class Runtime:
            def execute(self, case, controls):
                return CaseExecution(
                    output={"case_id": case["case_id"]},
                    stage_observations=_observations(
                        origin=invalid_origin,
                        source_other_calls=1,
                    ),
                )

        return Runtime()

    summary = _runner(
        store,
        manifest,
        factory,
        cache_mode=cache_mode,
    ).run()

    attempt = store.load_attempts("case-a")[0]
    assert summary.status == "completed_with_failures"
    assert summary.inventory.succeeded == ()
    assert attempt["status"] == "failed"
    assert attempt["result"]["error"] == {
        "code": "executor_contract_error",
        "retryable": False,
    }

    forbidden = _ForbiddenFactory()
    reopened = ExperimentStore.open(root, manifest["experiment_id"])
    resumed = _runner(
        reopened,
        manifest,
        forbidden,
        cache_mode=cache_mode,
    ).run()

    assert resumed.status == "completed_with_failures"
    assert resumed.terminal_failure_case_ids == ("case-a",)
    assert forbidden.calls == 0


def test_incompatible_resume_is_rejected_before_runtime_factory(tmp_path) -> None:
    stored = _manifest(case_specs=(("case-a", None, 0),))
    requested = _manifest(
        case_specs=(("case-a", None, 0),),
        prompt_version="prompt-v2",
    )
    store = ExperimentStore.create(tmp_path / "experiments", stored)
    forbidden = _ForbiddenFactory()

    with pytest.raises(ResumeCompatibilityError):
        _runner(store, requested, forbidden).run()

    assert forbidden.calls == 0
    assert store.scan().not_run == ("case-a",)


def test_corrupt_session_checkpoint_fails_before_resume_factory(tmp_path) -> None:
    manifest = _manifest(case_specs=(("g-0", "group", 0), ("g-1", "group", 1)))
    root = tmp_path / "experiments"
    store = ExperimentStore.create(root, manifest)
    _runner(store, manifest, _HistoryFactory()).run(stop_after_completed=1)
    attempt_path = store.attempt_paths("g-0")[0]
    complete_path = attempt_path.parent / "complete.json"
    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    attempt["payload"]["result"]["session_checkpoint"]["state_after_sha256"] = "0" * 64
    attempt["payload_sha256"] = canonical_hash(attempt["payload"])
    attempt_path.write_text(json.dumps(attempt), encoding="utf-8")
    marker = json.loads(complete_path.read_text(encoding="utf-8"))
    marker["payload"]["attempt_artifact_sha256"] = canonical_hash(attempt)
    marker["payload_sha256"] = canonical_hash(marker["payload"])
    complete_path.write_text(json.dumps(marker), encoding="utf-8")
    forbidden = _ForbiddenFactory()

    reopened = ExperimentStore.open(root, manifest["experiment_id"])
    with pytest.raises(RunnerContractError, match="checkpoint checksum"):
        _runner(reopened, manifest, forbidden).run()

    assert forbidden.calls == 0


def test_terminal_session_turn_rejects_later_committed_artifacts_before_factory(
    tmp_path,
) -> None:
    manifest = _manifest(
        case_specs=(("g-0", "group", 0), ("g-1", "group", 1)),
        max_retries=0,
    )
    store = ExperimentStore.create(tmp_path / "experiments", manifest)
    common = {
        "recorded_at": "2026-09-20T00:00:01Z",
        "cache_mode": "fresh",
        "execution_environment": _environment(),
    }
    store.write_attempt(
        "g-0",
        attempt=1,
        status="failed",
        result={"controlled_fixture": "terminal predecessor"},
        **common,
    )
    store.write_attempt(
        "g-1",
        attempt=1,
        status="succeeded",
        result={"controlled_fixture": "impossible later success"},
        **common,
    )
    store.mark_complete("g-1", attempt=1)
    forbidden = _ForbiddenFactory()

    with pytest.raises(
        RunnerContractError,
        match="later session turns cannot have attempts",
    ):
        _runner(store, manifest, forbidden).run()

    assert forbidden.calls == 0


def test_runtime_factory_cannot_reuse_mutable_runtime_between_work_units(
    tmp_path,
) -> None:
    manifest = _manifest(
        case_specs=(("case-a", None, 0), ("case-b", None, 0)),
        concurrency=1,
    )
    store = ExperimentStore.create(tmp_path / "experiments", manifest)

    class SharedRuntime:
        def __init__(self) -> None:
            self.executions = 0

        def execute(self, case, controls):
            self.executions += 1
            return CaseExecution(
                output={"case_id": case["case_id"]},
                stage_observations=_observations(
                    origin="fresh",
                    source_other_calls=0,
                ),
            )

    shared = SharedRuntime()

    def factory(work_unit, resume_state, controls):
        return shared

    summary = _runner(store, manifest, factory).run()

    assert summary.status == "completed_with_failures"
    assert summary.inventory.succeeded == ("case-a",)
    assert summary.terminal_failure_case_ids == ("case-b",)
    assert shared.executions == 1
    assert store.load_attempts("case-b")[0]["result"]["error"] == {
        "code": "executor_contract_error",
        "retryable": False,
    }


def test_replay_blocks_external_call_before_provider_operation(tmp_path) -> None:
    manifest = _manifest(case_specs=(("case-a", None, 0),))
    store = ExperimentStore.create(tmp_path / "experiments", manifest)
    provider_operations = 0

    def factory(work_unit, resume_state, runner_controls):
        class Runtime:
            def execute(self, case, controls):
                with pytest.raises(AttributeError):
                    controls.cache_mode = "fresh"

                def provider_operation():
                    nonlocal provider_operations
                    provider_operations += 1

                controls.call("retrieval", "other", provider_operation)
                raise AssertionError("replay provider guard did not stop execution")

        return Runtime()

    summary = _runner(store, manifest, factory, cache_mode="replay").run()

    attempt = store.load_attempts("case-a")[0]
    assert summary.status == "completed_with_failures"
    assert provider_operations == 0
    assert summary.provider_peak_in_flight["other"] == 0
    assert attempt["result"]["call_ledger"]["actual"]["other"]["attempted"] == 0
    assert attempt["result"]["error"] == {
        "code": "executor_contract_error",
        "retryable": False,
    }


def test_runtime_factory_never_receives_raw_provider_permits_in_replay(
    tmp_path,
) -> None:
    manifest = _manifest(case_specs=(("case-a", None, 0),))
    store = ExperimentStore.create(tmp_path / "experiments", manifest)
    observed_controls: list[dict[str, Any]] = []
    observed_attempt_capabilities: list[tuple[bool, bool, bool]] = []

    def factory(work_unit, resume_state, runner_controls):
        observed_controls.append(dict(vars(runner_controls)))

        class Runtime:
            def execute(self, case, controls):
                observed_attempt_capabilities.append(
                    (
                        hasattr(controls, "provider_controller"),
                        hasattr(controls, "stop_event"),
                        controls.stop_requested,
                    )
                )
                return CaseExecution(
                    output={"case_id": case["case_id"]},
                    stage_observations=_observations(
                        origin="replay",
                        source_other_calls=1,
                    ),
                )

        return Runtime()

    summary = _runner(store, manifest, factory, cache_mode="replay").run()

    assert summary.status == "succeeded"
    assert observed_controls == [{"cache_mode": "replay"}]
    assert observed_attempt_capabilities == [(False, False, False)]
    assert summary.provider_peak_in_flight == {kind: 0 for kind in EXTERNAL_CALL_KINDS}


def test_replay_detects_private_provider_permit_bypass_before_success_commit(
    tmp_path,
) -> None:
    manifest = _manifest(case_specs=(("case-a", None, 0),))
    store = ExperimentStore.create(tmp_path / "experiments", manifest)
    provider_operations = 0

    def factory(work_unit, resume_state, runner_controls):
        class Runtime:
            def execute(self, case, controls):
                nonlocal provider_operations
                with controls._provider_controller.permit("other"):
                    provider_operations += 1
                return CaseExecution(
                    output={"case_id": case["case_id"]},
                    stage_observations=_observations(
                        origin="replay",
                        source_other_calls=1,
                    ),
                )

        return Runtime()

    summary = _runner(store, manifest, factory, cache_mode="replay").run()

    attempt = store.load_attempts("case-a")[0]
    assert summary.status == "completed_with_failures"
    assert summary.inventory.succeeded == ()
    assert provider_operations == 1
    assert summary.provider_peak_in_flight["other"] == 1
    assert attempt["result"]["error"] == {
        "code": "replay_provider_bypass",
        "retryable": False,
    }


def test_replay_private_provider_bypass_overrides_runtime_failure(tmp_path) -> None:
    manifest = _manifest(case_specs=(("case-a", None, 0),))
    store = ExperimentStore.create(tmp_path / "experiments", manifest)

    def factory(work_unit, resume_state, runner_controls):
        class Runtime:
            def execute(self, case, controls):
                with controls._provider_controller.permit("other"):
                    pass
                raise CaseExecutionError("provider_timeout", retryable=True)

        return Runtime()

    summary = _runner(store, manifest, factory, cache_mode="replay").run()

    attempt = store.load_attempts("case-a")[0]
    assert summary.status == "completed_with_failures"
    assert attempt["status"] == "failed"
    assert attempt["result"]["error"] == {
        "code": "replay_provider_bypass",
        "retryable": False,
    }
    assert attempt["result"]["call_ledger"]["actual"]["other"]["attempted"] == 0


def test_retryable_controlled_interrupt_is_not_reported_as_terminal_failure(
    tmp_path,
) -> None:
    manifest = _manifest(case_specs=(("case-a", None, 0),), max_retries=1)
    store = ExperimentStore.create(tmp_path / "experiments", manifest)
    runner_ref: dict[str, ExperimentRunner] = {}

    def factory(work_unit, resume_state, runner_controls):
        class Runtime:
            def execute(self, case, controls):
                runner_ref["runner"].request_stop()
                controls.call("retrieval", "other", lambda: None)
                raise AssertionError("stop guard did not interrupt provider dispatch")

        return Runtime()

    runner = _runner(store, manifest, factory)
    runner_ref["runner"] = runner
    summary = runner.run()

    attempt = store.load_attempts("case-a")[0]
    assert summary.status == "interrupted"
    assert summary.inventory.interrupted == ("case-a",)
    assert summary.terminal_failure_case_ids == ()
    assert attempt["result"]["error"] == {
        "code": "controlled_interrupt",
        "retryable": True,
    }


def test_successful_fresh_stage_rejects_source_calls_not_in_actual_ledger(
    tmp_path,
) -> None:
    manifest = _manifest(case_specs=(("case-a", None, 0),))
    store = ExperimentStore.create(tmp_path / "experiments", manifest)

    def factory(work_unit, resume_state, runner_controls):
        class Runtime:
            def execute(self, case, controls):
                return CaseExecution(
                    output={"case_id": case["case_id"]},
                    stage_observations=_observations(
                        origin="fresh",
                        source_other_calls=1,
                    ),
                )

        return Runtime()

    summary = _runner(store, manifest, factory).run()

    attempt = store.load_attempts("case-a")[0]
    assert summary.status == "completed_with_failures"
    assert summary.inventory.succeeded == ()
    assert attempt["result"]["error"] == {
        "code": "executor_contract_error",
        "retryable": False,
    }


def test_same_process_runner_claim_blocks_duplicate_execution_before_factory(
    tmp_path,
) -> None:
    manifest = _manifest(case_specs=(("case-a", None, 0),))
    root = tmp_path / "experiments"
    store = ExperimentStore.create(root, manifest)
    entered = threading.Event()
    release = threading.Event()
    results = []
    errors: list[BaseException] = []

    def blocking_factory(work_unit, resume_state, runner_controls):
        class Runtime:
            def execute(self, case, controls):
                entered.set()
                if not release.wait(timeout=5):
                    raise AssertionError("duplicate-runner test timed out")
                return CaseExecution(
                    output={"case_id": case["case_id"]},
                    stage_observations=_observations(
                        origin="fresh",
                        source_other_calls=0,
                    ),
                )

        return Runtime()

    first_runner = _runner(store, manifest, blocking_factory)

    def run_first() -> None:
        try:
            results.append(first_runner.run())
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    thread = threading.Thread(target=run_first)
    thread.start()
    assert entered.wait(timeout=5)
    forbidden = _ForbiddenFactory()
    reopened = ExperimentStore.open(root, manifest["experiment_id"])
    try:
        with pytest.raises(RunnerContractError, match="already has an active runner"):
            _runner(reopened, manifest, forbidden).run()
    finally:
        release.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []
    assert [result.status for result in results] == ["succeeded"]
    assert forbidden.calls == 0


def test_completed_inventory_takes_precedence_over_stop_signal(tmp_path) -> None:
    manifest = _manifest(case_specs=(("case-a", None, 0),))
    store = ExperimentStore.create(tmp_path / "experiments", manifest)

    summary = _runner(store, manifest, _HistoryFactory()).run(stop_after_completed=1)

    assert summary.status == "succeeded"
    assert summary.inventory.succeeded == ("case-a",)


def test_resume_rejects_nonretryable_attempt_followed_by_another_attempt(
    tmp_path,
) -> None:
    manifest = _manifest(case_specs=(("case-a", None, 0),), max_retries=1)
    root = tmp_path / "experiments"
    store = ExperimentStore.create(root, manifest)

    class RetryOnceFactory:
        def __init__(self) -> None:
            self.executions = 0

        def __call__(self, work_unit, resume_state, runner_controls):
            parent = self

            class Runtime:
                def execute(self, case, controls):
                    parent.executions += 1
                    if parent.executions == 1:
                        raise CaseExecutionError("temporary_failure", retryable=True)
                    return CaseExecution(
                        output={"ok": True},
                        stage_observations=_observations(
                            origin="fresh",
                            source_other_calls=0,
                        ),
                    )

            return Runtime()

    assert _runner(store, manifest, RetryOnceFactory()).run().status == "succeeded"
    first_attempt_path = store.attempt_paths("case-a")[0]
    first_attempt = json.loads(first_attempt_path.read_text(encoding="utf-8"))
    first_attempt["payload"]["result"]["error"]["retryable"] = False
    first_attempt["payload_sha256"] = canonical_hash(first_attempt["payload"])
    first_attempt_path.write_text(json.dumps(first_attempt), encoding="utf-8")
    forbidden = _ForbiddenFactory()

    reopened = ExperimentStore.open(root, manifest["experiment_id"])
    with pytest.raises(
        RunnerContractError,
        match="non-retryable attempt cannot be followed",
    ):
        _runner(reopened, manifest, forbidden).run()

    assert forbidden.calls == 0


def test_resume_rejects_replay_label_with_fresh_external_call_history(
    tmp_path,
) -> None:
    manifest = _manifest(case_specs=(("case-a", None, 0),))
    root = tmp_path / "experiments"
    store = ExperimentStore.create(root, manifest)
    assert _runner(store, manifest, _HistoryFactory()).run().status == "succeeded"
    attempt_path = store.attempt_paths("case-a")[0]
    complete_path = attempt_path.parent / "complete.json"
    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    attempt["payload"]["cache_mode"] = "replay"
    attempt["payload_sha256"] = canonical_hash(attempt["payload"])
    attempt_path.write_text(json.dumps(attempt), encoding="utf-8")
    marker = json.loads(complete_path.read_text(encoding="utf-8"))
    marker["payload"]["attempt_artifact_sha256"] = canonical_hash(attempt)
    marker["payload_sha256"] = canonical_hash(marker["payload"])
    complete_path.write_text(json.dumps(marker), encoding="utf-8")
    forbidden = _ForbiddenFactory()

    reopened = ExperimentStore.open(root, manifest["experiment_id"])
    with pytest.raises(
        RunnerContractError,
        match="replay attempt cannot contain external calls",
    ):
        _runner(reopened, manifest, forbidden, cache_mode="replay").run()

    assert forbidden.calls == 0
