from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any

from .run_executor import ExecutionFailure, RunExecutor
from .run_service import (
    InvalidRunStateError,
    ResourceNotFoundError,
    RunService,
    WorkerLeaseLostError,
)


class _SupervisorStopping(RuntimeError):
    pass


MAX_QUARANTINED_EXECUTIONS = 4


@dataclass(slots=True)
class RunSupervisor:
    """Single-process M4 queue supervisor.

    PostgreSQL remains the source of truth.  The in-process event only reduces
    polling latency, and stopping/restarting this object never claims exact
    provider-call recovery.  A new process marks only expired running leases
    interrupted before it starts claiming queued work.
    """

    service: RunService
    executor: RunExecutor
    lease_seconds: int = 300
    execution_timeout_seconds: float = 180.0
    poll_seconds: float = 0.2
    worker_id: str = field(default_factory=lambda: f"m4-{uuid.uuid4()}")
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _wake: threading.Event = field(default_factory=threading.Event, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _execution_thread: threading.Thread | None = field(default=None, init=False)
    _execution_active: threading.Event | None = field(default=None, init=False)
    _quarantined_threads: list[threading.Thread] = field(
        default_factory=list,
        init=False,
    )
    _last_loop_error: str | None = field(default=None, init=False)
    _recovery_complete: bool = field(default=False, init=False)
    _last_recovery_monotonic: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.lease_seconds, int) or self.lease_seconds < 1:
            raise ValueError("lease_seconds must be a positive integer")
        if self.execution_timeout_seconds <= 0 or self.poll_seconds <= 0:
            raise ValueError("supervisor timeouts must be positive")
        if self.lease_seconds <= self.execution_timeout_seconds:
            raise ValueError("lease_seconds must exceed execution_timeout_seconds")

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_ready(self) -> bool:
        draining = sum(
            1 for thread in self._quarantined_threads if thread.is_alive()
        )
        return (
            self.is_alive
            and self._recovery_complete
            and draining < MAX_QUARANTINED_EXECUTIONS
        )

    @property
    def last_loop_error(self) -> str | None:
        return self._last_loop_error

    def start(self) -> None:
        if self.is_alive:
            return
        self._reap_quarantined_threads()
        if self._execution_thread is not None and self._execution_thread.is_alive():
            raise RuntimeError("a previous supervisor execution is still draining")
        if self._quarantined_threads:
            raise RuntimeError("timed-out executions are still draining")
        self._stop.clear()
        self._wake.clear()
        try:
            self.service.recover_stale_runs()
            self._recovery_complete = True
            self._last_recovery_monotonic = time.monotonic()
        except Exception:
            # Liveness must not depend on database readiness.  The loop keeps
            # retrying the startup reconciliation before it claims new work.
            self._recovery_complete = False
            self._last_loop_error = "startup_recovery_failed"
        self._thread = threading.Thread(
            target=self._loop,
            name="legal-rag-m4-supervisor",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> bool:
        self._stop.set()
        self._wake.set()
        if self._execution_active is not None:
            self._execution_active.clear()
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, timeout))
            if not self._thread.is_alive():
                self._thread = None
        self._reap_quarantined_threads()
        return not self.is_alive

    def wake(self) -> None:
        self._wake.set()

    def run_once(self) -> bool:
        self._reap_quarantined_threads()
        if len(self._quarantined_threads) >= MAX_QUARANTINED_EXECUTIONS:
            # Bound the number of provider calls still draining after their
            # delivery deadlines. Do not claim and falsely time out more work.
            return False
        claimed = self.service.claim_next_run(self.worker_id, self.lease_seconds)
        if claimed is None:
            return False
        self._execute_claimed(claimed.run_id)
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                recovery_interval = min(5.0, self.lease_seconds / 2)
                if (
                    not self._recovery_complete
                    or time.monotonic() - self._last_recovery_monotonic
                    >= recovery_interval
                ):
                    self.service.recover_stale_runs()
                    self._recovery_complete = True
                    self._last_recovery_monotonic = time.monotonic()
                worked = self.run_once()
                self._last_loop_error = None
            except Exception:  # database readiness reports the attributable detail.
                worked = False
                self._recovery_complete = False
                self._last_loop_error = "supervisor_loop_failed"
            if not worked:
                self._wake.wait(self.poll_seconds)
                self._wake.clear()

    def _execute_claimed(self, run_id: str) -> None:
        try:
            frozen = self.service.load_execution_input(run_id, self.worker_id)
            execution_input = frozen.to_execution_input()
            result = self._execute_with_timeout(run_id, execution_input)
            self.service.publish_success(run_id, self.worker_id, result)
        except FutureTimeout:
            self._safe_fail(
                run_id,
                "model_timeout",
                {"stage": "execution", "retryable": True},
            )
        except ExecutionFailure as exc:
            error_code = "model_timeout" if exc.code == "execution_timeout" else exc.code
            self._safe_fail(run_id, error_code, exc.to_safe_dict())
        except (WorkerLeaseLostError, InvalidRunStateError, ResourceNotFoundError):
            # Cancellation, stale recovery, or another terminal transition won
            # the database fence.  A late provider result is intentionally dropped.
            return
        except _SupervisorStopping:
            # Leave the durable run under its finite lease. A later process
            # will classify it as interrupted; M4 does not claim exact resume.
            return
        except Exception:
            self._safe_fail(
                run_id,
                "execution_failed",
                {"stage": "supervisor", "retryable": False},
            )

    def _execute_with_timeout(self, run_id: str, execution_input):
        done = threading.Event()
        active = threading.Event()
        active.set()
        outcome: dict[str, Any] = {}

        def invoke() -> None:
            try:
                outcome["result"] = self.executor.execute(
                    execution_input,
                    self._stage_callback(run_id, active),
                )
            except Exception as exc:  # transported to the supervisor thread.
                outcome["error"] = exc
            finally:
                done.set()

        thread = threading.Thread(
            target=invoke,
            name=f"legal-rag-m4-executor-{run_id[:8]}",
            daemon=True,
        )
        self._execution_thread = thread
        self._execution_active = active
        thread.start()
        deadline = time.monotonic() + self.execution_timeout_seconds
        while not done.wait(timeout=0.05):
            if self._stop.is_set():
                active.clear()
                self._quarantined_threads.append(thread)
                self._execution_thread = None
                self._execution_active = None
                raise _SupervisorStopping()
            if time.monotonic() >= deadline:
                active.clear()
                self._quarantined_threads.append(thread)
                self._execution_thread = None
                self._execution_active = None
                raise FutureTimeout()
        active.clear()
        self._execution_thread = None
        self._execution_active = None
        error = outcome.get("error")
        if error is not None:
            raise error
        return outcome["result"]

    def _stage_callback(self, run_id: str, active: threading.Event):
        def append(event_type, safe_payload) -> None:
            if not active.is_set():
                raise WorkerLeaseLostError("execution delivery deadline passed")
            self.service.append_stage_event(
                run_id,
                event_type,
                safe_payload,
                worker_id=self.worker_id,
            )

        return append

    def _reap_quarantined_threads(self) -> None:
        self._quarantined_threads = [
            thread for thread in self._quarantined_threads if thread.is_alive()
        ]

    def _safe_fail(self, run_id: str, error_code: str, payload) -> None:
        try:
            self.service.fail_run(
                run_id,
                self.worker_id,
                error_code,
                payload,
            )
        except (WorkerLeaseLostError, InvalidRunStateError, ResourceNotFoundError):
            return


__all__ = ["MAX_QUARANTINED_EXECUTIONS", "RunSupervisor"]
