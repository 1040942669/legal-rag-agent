"""Best-effort execution facts at real M5 node and external-call boundaries."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from legal_rag.llm import CompletionUsage
from legal_rag.observability.events import Observation, ObservationContext, Observer

from .budget import AttemptReservation, BudgetExhausted, retry_decision


def _usage(client: Any) -> dict[str, int] | None:
    try:
        usage = getattr(client, "usage", None)
        if not isinstance(usage, CompletionUsage):
            return None
        return {
            name: getattr(usage, name)
            for name in (
                "calls",
                "failed_calls",
                "input_tokens",
                "output_tokens",
                "input_usage_calls",
                "output_usage_calls",
            )
        }
    except Exception:
        return None


def _error_category(error: Exception) -> str:
    if isinstance(error, BudgetExhausted):
        return (
            "deadline_exceeded"
            if error.stop_reason == "deadline_exceeded"
            else "budget_exceeded"
        )
    decision = retry_decision(error)
    if decision.error_code == "timeout":
        return "provider_timeout"
    return "other" if decision.error_code == "unknown" else "provider_error"


class HarnessObservationAdapter:
    """Observe without changing checkpoints, budgets, exceptions or results.

    Attempts are counted only around an actual invocation, separately from
    the authoritative reserved budget. Token components are unknown unless
    every client call in this invocation explicitly reported that component.
    """

    def __init__(
        self,
        observer: Observer,
        *,
        run_id: str,
        session_id: str,
        persistence: Any,
        completion_client: Any = None,
    ) -> None:
        self.observer = observer
        self.context = ObservationContext(
            trace_id=run_id, run_id=run_id, session_id=session_id
        )
        self.persistence = persistence
        self.completion_client = completion_client
        self._node: ContextVar[str | None] = ContextVar("observed_node", default=None)

    def _record(self, *, started: float, **fields: Any) -> None:
        try:
            snapshot = self.persistence.get_budget(self.context.run_id)
            budget_used = {
                "retrieval_rounds": snapshot.retrieval_rounds_used,
                "queries": snapshot.queries_used,
                "tool_attempts": snapshot.tool_attempts_used,
                "model_attempts": snapshot.model_attempts_used,
                "embedding_attempts": snapshot.embedding_attempts_used,
            }
            self.observer.record(
                Observation(
                    context=self.context,
                    duration_ms=max(0.0, (time.perf_counter() - started) * 1000),
                    budget_used=budget_used,
                    **fields,
                )
            )
        except Exception:
            # Optional telemetry cannot introduce a new execution dependency.
            return

    def wrap_node(self, name: str, callback: Callable) -> Callable:
        def invoke(envelope):
            started = time.perf_counter()
            token = self._node.set(name)
            try:
                result = callback(envelope)
            except Exception as error:
                self._record(
                    started=started,
                    name="node.completed",
                    node=name,
                    status="failed",
                    error_category=_error_category(error),
                )
                raise
            else:
                state = result["payload"]
                self._record(
                    started=started,
                    name="node.completed",
                    node=name,
                    status="succeeded",
                    evidence_ids=tuple(state.get("retrieved_evidence_refs", ()))[:100],
                )
                return result
            finally:
                self._node.reset(token)

        return invoke

    @contextmanager
    def attempt(
        self,
        node: str,
        tool: str,
        reservation: AttemptReservation,
        *,
        usage_client: Any = None,
    ) -> Iterator[None]:
        started = time.perf_counter()
        client = (
            usage_client
            if usage_client is not None
            else (self.completion_client if tool == "generator" else None)
        )
        before = _usage(client)
        fields: dict[str, Any] = {
            "name": "model.completed"
            if reservation.operation_kind == "model"
            else "tool.completed",
            "node": node,
            "tool": tool,
            "retry_count": reservation.attempt_no - 1,
            "counts": {"tool_calls": 1} if reservation.operation_kind == "tool" else {},
        }
        try:
            yield
        except Exception as error:
            fields.update(status="failed", error_category=_error_category(error))
            raise
        else:
            fields["status"] = "succeeded"
        finally:
            after = _usage(client)
            if before is not None and after is not None:
                calls = after["calls"] - before["calls"]
                if reservation.operation_kind == "model" and calls >= 0:
                    fields["counts"] = {"model_calls": calls}
                if after["failed_calls"] > before["failed_calls"]:
                    fields.update(
                        status="failed",
                        error_category=fields.get("error_category") or "provider_error",
                    )
                for component in ("input", "output"):
                    coverage = (
                        after[f"{component}_usage_calls"]
                        - before[f"{component}_usage_calls"]
                    )
                    if calls > 0 and coverage == calls:
                        fields[f"model_{component}_tokens"] = (
                            after[f"{component}_tokens"] - before[f"{component}_tokens"]
                        )
            self._record(started=started, **fields)

    @contextmanager
    def cache_lookup(self, node: str | None, tool: str) -> Iterator[None]:
        started = time.perf_counter()
        fields: dict[str, Any] = {
            "name": "cache.lookup",
            "node": node or self._node.get(),
            "tool": tool,
        }
        try:
            yield
        except Exception:
            fields.update(
                status="failed",
                cache_status="error",
                error_category="validation_failed",
            )
            raise
        else:
            fields.update(
                status="succeeded", cache_status="hit", counts={"cache_hits": 1}
            )
        finally:
            self._record(started=started, **fields)
