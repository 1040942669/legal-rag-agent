from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from legal_rag.services.run_service import WorkerLeaseLostError
from legal_rag.services.supervisor import RunSupervisor


def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 2.0,
    interval: float = 0.01,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("condition was not satisfied before the deadline")


@dataclass(frozen=True, slots=True)
class _ClaimedRun:
    run_id: str


@dataclass(frozen=True, slots=True)
class _FrozenInput:
    run_id: str

    def to_execution_input(self) -> _FrozenInput:
        return self


class _FakeService:
    def __init__(
        self,
        run_ids: tuple[str, ...] = (),
        *,
        recovery_outcomes: tuple[BaseException | None, ...] = (),
        recovery_batches: tuple[int, ...] = (),
    ) -> None:
        self._run_ids = list(run_ids)
        self._recovery_outcomes = list(recovery_outcomes)
        self._recovery_batches = list(recovery_batches)
        self._lock = threading.Lock()
        self.successes: list[tuple[str, str, Any]] = []
        self.failures: list[tuple[str, str, str, Any]] = []
        self.stage_events: list[tuple[str, str, Any, str]] = []
        self.recovery_times: list[float] = []

    @property
    def recovery_count(self) -> int:
        with self._lock:
            return len(self.recovery_times)

    def recover_stale_runs(
        self,
        *,
        batch_limit: int = 100,
    ) -> tuple[_ClaimedRun, ...]:
        with self._lock:
            self.recovery_times.append(time.monotonic())
            outcome = (
                self._recovery_outcomes.pop(0)
                if self._recovery_outcomes
                else None
            )
            batch_size = (
                self._recovery_batches.pop(0)
                if self._recovery_batches
                else 0
            )
        if outcome is not None:
            raise outcome
        assert batch_size <= batch_limit
        return tuple(
            _ClaimedRun(f"recovered-{self.recovery_count}-{index}")
            for index in range(batch_size)
        )

    def claim_next_run(
        self,
        worker_id: str,
        lease_seconds: int,
    ) -> _ClaimedRun | None:
        del worker_id, lease_seconds
        with self._lock:
            if not self._run_ids:
                return None
            return _ClaimedRun(self._run_ids.pop(0))

    def load_execution_input(self, run_id: str, worker_id: str) -> _FrozenInput:
        del worker_id
        return _FrozenInput(run_id)

    def publish_success(self, run_id: str, worker_id: str, result: Any) -> None:
        with self._lock:
            self.successes.append((run_id, worker_id, result))

    def fail_run(
        self,
        run_id: str,
        worker_id: str,
        error_code: str,
        payload: Any,
    ) -> None:
        with self._lock:
            self.failures.append((run_id, worker_id, error_code, payload))

    def append_stage_event(
        self,
        run_id: str,
        event_type: str,
        safe_payload: Any,
        *,
        worker_id: str,
    ) -> None:
        with self._lock:
            self.stage_events.append(
                (run_id, event_type, safe_payload, worker_id)
            )


class _FirstExecutionBlocks:
    def __init__(self) -> None:
        self.first_started = threading.Event()
        self.release_first = threading.Event()
        self.first_finished = threading.Event()
        self._call_count = 0
        self._lock = threading.Lock()

    def execute(self, execution_input: _FrozenInput, stage_callback) -> str:
        del stage_callback
        with self._lock:
            self._call_count += 1
            call_number = self._call_count
        if call_number == 1:
            self.first_started.set()
            self.release_first.wait(timeout=5.0)
            self.first_finished.set()
        return f"result:{execution_input.run_id}"


class _LateCallbackExecutor:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.callback_error: BaseException | None = None

    def execute(self, execution_input: _FrozenInput, stage_callback) -> str:
        self.started.set()
        self.release.wait(timeout=5.0)
        try:
            stage_callback("generation.started", {"late": True})
        except BaseException as exc:
            self.callback_error = exc
        finally:
            self.finished.set()
        return f"late:{execution_input.run_id}"


class _NeverExecutor:
    def execute(self, execution_input: _FrozenInput, stage_callback) -> str:
        del execution_input, stage_callback
        raise AssertionError("the executor must not be called")


class _EveryExecutionBlocks:
    def __init__(self) -> None:
        self.release = threading.Event()

    def execute(self, execution_input: _FrozenInput, stage_callback) -> str:
        del stage_callback
        self.release.wait(timeout=5.0)
        return f"result:{execution_input.run_id}"


def _supervisor(service: _FakeService, executor: object) -> RunSupervisor:
    return RunSupervisor(
        service=service,  # type: ignore[arg-type]
        executor=executor,  # type: ignore[arg-type]
        lease_seconds=1,
        execution_timeout_seconds=0.08,
        poll_seconds=0.01,
        worker_id="test-worker",
    )


def test_timed_out_execution_does_not_poison_the_next_run() -> None:
    service = _FakeService(("run-1", "run-2"))
    executor = _FirstExecutionBlocks()
    supervisor = _supervisor(service, executor)

    try:
        assert supervisor.run_once() is True
        assert executor.first_started.is_set()
        assert service.failures == [
            (
                "run-1",
                "test-worker",
                "model_timeout",
                {"stage": "execution", "retryable": True},
            )
        ]

        assert supervisor.run_once() is True
        assert service.successes == [
            ("run-2", "test-worker", "result:run-2")
        ]
    finally:
        executor.release_first.set()
        assert executor.first_finished.wait(timeout=1.0)


def test_callback_from_a_timed_out_execution_is_fenced() -> None:
    service = _FakeService(("run-1",))
    executor = _LateCallbackExecutor()
    supervisor = _supervisor(service, executor)

    try:
        assert supervisor.run_once() is True
        assert executor.started.is_set()
        assert service.failures[0][2] == "model_timeout"

        executor.release.set()
        assert executor.finished.wait(timeout=1.0)

        assert isinstance(executor.callback_error, WorkerLeaseLostError)
        assert service.stage_events == []
        assert service.successes == []
    finally:
        executor.release.set()


def test_stop_is_prompt_and_restart_is_refused_while_execution_drains() -> None:
    service = _FakeService(("run-1",))
    executor = _FirstExecutionBlocks()
    supervisor = _supervisor(service, executor)

    supervisor.start()
    assert executor.first_started.wait(timeout=1.0)

    started_stopping = time.monotonic()
    assert supervisor.stop(timeout=0.75) is False
    stop_elapsed = time.monotonic() - started_stopping

    try:
        assert stop_elapsed < 0.5
        assert supervisor.is_alive is False
        with pytest.raises(RuntimeError, match="still draining"):
            supervisor.start()
    finally:
        executor.release_first.set()
        assert executor.first_finished.wait(timeout=1.0)
        assert supervisor.stop() is True


def test_periodic_stale_recovery_retries_after_service_failure() -> None:
    service = _FakeService(
        recovery_outcomes=(
            None,
            RuntimeError("simulated database outage"),
            None,
        )
    )
    supervisor = _supervisor(service, _NeverExecutor())

    try:
        supervisor.start()
        _wait_until(lambda: service.recovery_count >= 3, timeout=2.0)
        _wait_until(lambda: supervisor.is_ready, timeout=1.0)

        assert service.recovery_times[1] - service.recovery_times[0] >= 0.4
        assert supervisor.last_loop_error is None
    finally:
        assert supervisor.stop(timeout=0.75) is True


def test_startup_recovers_every_full_stale_batch_before_becoming_ready() -> None:
    service = _FakeService(recovery_batches=(100, 100, 3))
    supervisor = _supervisor(service, _NeverExecutor())

    try:
        supervisor.start()

        assert service.recovery_count >= 3
        assert supervisor.is_ready is True
    finally:
        assert supervisor.stop(timeout=0.75) is True


def test_readiness_fails_closed_when_quarantine_capacity_is_exhausted() -> None:
    service = _FakeService(("run-1", "run-2", "run-3", "run-4"))
    executor = _EveryExecutionBlocks()
    supervisor = _supervisor(service, executor)

    try:
        supervisor.start()
        _wait_until(lambda: len(service.failures) == 4, timeout=2.0)

        assert supervisor.is_alive is True
        assert supervisor.is_ready is False
    finally:
        executor.release.set()
        assert supervisor.stop(timeout=0.75) is True
