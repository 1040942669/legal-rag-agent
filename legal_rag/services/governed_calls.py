"""One durable completion owner for generator and semantic checker calls.

Reservations are engineering estimates, not a provider billing hard cap.
Unknown delivery/usage retains its reservation and cannot be retried implicitly.
No prompt, response, credential or exception message is persisted here.
"""
from __future__ import annotations

import hashlib
from contextlib import nullcontext
from typing import Any, Callable

from legal_rag.harness.budget import BudgetExhausted
from .execution_policy import GenerationPolicy


class GovernedCompletionClient:
    governed = True
    propagate_control_errors = True
    propagate_provider_errors = True
    hidden_retries_disabled = True

    def __init__(self, client: Any, *, service: Any, policy: GenerationPolicy,
                 run_id: str, worker_id: str, lease_epoch: int,
                 operation: str = "generate_answer", identity: str = "generation-v1",
                 fault_hook: Callable[[str], None] | None = None,
                 _shared_context: dict | None = None) -> None:
        if not isinstance(policy, GenerationPolicy) or not policy.enabled:
            raise ValueError("governed completion requires enabled authority")
        expected = {"model": policy.model, "base_url": policy.base_url,
                    "max_tokens": policy.max_output_tokens, "enable_thinking": False,
                    "response_format": "json_object", "follow_redirects": False,
                    "load_environment_file": False, "hidden_retries_disabled": True}
        if any(getattr(client, key, None) != value for key, value in expected.items()):
            raise ValueError("completion controls do not match frozen authority")
        if operation not in {"generate_answer", "semantic_check"} or not isinstance(identity, str) or not identity:
            raise ValueError("invalid governed operation identity")
        self.client, self.service, self.policy = client, service, policy
        self.run_id, self.worker_id, self.lease_epoch = run_id, worker_id, lease_epoch
        self.operation, self.identity, self.fault_hook = operation, identity, fault_hook
        self._context = _shared_context if _shared_context is not None else {}

    def set_observation_adapter(self, adapter) -> None:
        self._context["observation_adapter"] = adapter

    @property
    def usage(self):
        return getattr(self.client, "usage", None)

    def operation_view(self, operation: str, *, identity: str):
        return type(self)(self.client, service=self.service, policy=self.policy,
                          run_id=self.run_id, worker_id=self.worker_id, lease_epoch=self.lease_epoch,
                          operation=operation, identity=identity, fault_hook=self.fault_hook,
                          _shared_context=self._context)

    def _finish(self, attempt_id: str, *, status: str, response: str = "",
                usage: tuple[int, int] | None = None, reason: str | None = None) -> None:
        values = dict(worker_id=self.worker_id, lease_epoch=self.lease_epoch,
                      status=status, monetary_usage=usage, monetary_unknown_reason=reason)
        if status == "succeeded":
            values["result_hash"] = hashlib.sha256(response.encode("utf-8")).hexdigest()
        else:
            values.update(retryable=False, error_code="external_outcome_unknown", possible_duplicate_cost=True)
        self.service.finish_attempt(attempt_id, **values)

    def complete(self, prompt: str) -> str:
        try:
            reservation_cost = self.policy.reservation(prompt)
        except (ValueError, UnicodeError):
            raise BudgetExhausted("prompt_preflight_rejected") from None
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        identity_hash = hashlib.sha256((self.identity + ":" + prompt_hash).encode("utf-8")).hexdigest()
        reservation = self.service.reserve_attempt(
            self.run_id, worker_id=self.worker_id, lease_epoch=self.lease_epoch,
            operation_kind="model", operation_name=self.operation,
            operation_key=f"{self.operation}:{identity_hash}", request_hash=prompt_hash,
            monetary_reservation=reservation_cost,
        )
        self.service.mark_attempt_dispatched(reservation.attempt_id,
                                            worker_id=self.worker_id, lease_epoch=self.lease_epoch)
        if self.fault_hook:
            self.fault_hook("after_model_attempt_dispatched")
        adapter = self._context.get("observation_adapter")
        # Reuse M6's allowlisted judge labels. An unknown node name is rejected
        # by the best-effort sink and would silently lose the checker fact.
        observed = adapter.attempt("generate" if self.operation == "generate_answer" else "judge",
                                   "generator" if self.operation == "generate_answer" else "judge",
                                   reservation, usage_client=self.client) if adapter else nullcontext()
        with observed:
            return self._complete_dispatched(prompt, reservation, reservation_cost)

    def _complete_dispatched(self, prompt, reservation, reservation_cost) -> str:
        try:
            response = self.client.complete(prompt)
        except Exception:
            self._finish(reservation.attempt_id, status="outcome_unknown", reason="provider_delivery_unknown")
            raise BudgetExhausted("external_outcome_unknown") from None
        metadata = getattr(self.client, "last_response_metadata", None)
        reason, usage = self._inspect_response(response, metadata)
        if reason:
            self._finish(reservation.attempt_id, status="outcome_unknown", usage=usage, reason=reason)
            raise BudgetExhausted("external_outcome_unknown") from None
        assert usage is not None
        overrun = self.policy.cost(*usage) > reservation_cost or usage[1] > self.policy.max_output_tokens
        self._finish(reservation.attempt_id, status="succeeded", response=response, usage=usage)
        if self.fault_hook:
            self.fault_hook("after_model_attempt_succeeded_before_artifact")
        if overrun:
            raise BudgetExhausted("monetary_reservation_overrun")
        return response

    @staticmethod
    def _inspect_response(response: object, metadata: object) -> tuple[str | None, tuple[int, int] | None]:
        if not isinstance(metadata, dict) or metadata.get("returned_model_matches") is not True:
            return "pricing_identity_unknown", None
        inputs, outputs, total = (metadata.get(name) for name in ("input_tokens", "output_tokens", "total_tokens"))
        if any(type(item) is not int or not 0 <= item <= 1000000000 for item in (inputs, outputs, total)) or total != inputs + outputs:
            return "usage_unknown", None
        usage = inputs, outputs
        if metadata.get("finish_reason") != "stop":
            return "completion_not_finished", usage
        if not isinstance(response, str) or not response.strip():
            return "completion_content_unknown", usage
        reasoning_tokens = metadata.get("reasoning_tokens")
        if metadata.get("reasoning_content_nonempty") is True or (type(reasoning_tokens) is int and reasoning_tokens > 0):
            return "reasoning_returned", usage
        if not (metadata.get("reasoning_content_reported") is True and metadata.get("reasoning_content_nonempty") is False) and reasoning_tokens != 0:
            return "reasoning_unknown", usage
        return None, usage
