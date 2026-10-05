from decimal import Decimal
from types import SimpleNamespace

import pytest

from legal_rag.harness.budget import BudgetExhausted
from legal_rag.services.execution_policy import GenerationPolicy
from legal_rag.services.governed_calls import GovernedCompletionClient


def policy(**updates):
    values = dict(enabled=True, model="Qwen/test", allowed_scope_ids=("allowed",),
                  pricing_acknowledged=True, egress_acknowledged=True,
                  price_revision="test-r1", input_rate="0.4", output_rate="3.2",
                  budget="0.1", max_output_tokens=100)
    return GenerationPolicy(**(values | updates))


class Ledger:
    def __init__(self):
        self.calls = []

    def reserve_attempt(self, run_id, **values):
        self.calls.append(("reserve", values))
        return SimpleNamespace(attempt_id=str(len(self.calls)))

    def mark_attempt_dispatched(self, attempt_id, **values):
        self.calls.append(("dispatch", values))

    def finish_attempt(self, attempt_id, **values):
        self.calls.append(("finish", values))


class Client:
    hidden_retries_disabled = True
    model = "Qwen/test"
    base_url = "https://api.siliconflow.cn/v1"
    max_tokens = 100
    enable_thinking = False
    response_format = "json_object"
    follow_redirects = False
    load_environment_file = False
    usage = None

    def __init__(self, metadata=None, error=None):
        self.calls = 0
        self.error = error
        self.last_response_metadata = metadata or {
            "input_tokens": 30, "output_tokens": 10, "total_tokens": 40,
            "returned_model_matches": True, "finish_reason": "stop",
            "reasoning_content_reported": True, "reasoning_content_nonempty": False,
        }

    def complete(self, prompt):
        self.calls += 1
        if self.error:
            raise self.error
        return '{"answer":"test"}'


def governed(client=None, ledger=None, **changes):
    return GovernedCompletionClient(client or Client(), service=ledger or Ledger(),
                                    policy=policy(**changes), run_id="run", worker_id="worker", lease_epoch=1)


def test_one_atomic_reservation_and_actual_settlement_without_double_count():
    ledger, client = Ledger(), Client()
    wrapper = governed(client, ledger)
    wrapper.complete("test")
    assert client.calls == 1
    assert [name for name, _ in ledger.calls] == ["reserve", "dispatch", "finish"]
    assert ledger.calls[0][1]["monetary_reservation"] > Decimal("0")
    assert ledger.calls[-1][1]["monetary_usage"] == (30, 10)
    assert ledger.calls[-1][1]["status"] == "succeeded"


def test_semantic_view_shares_ledger_and_has_versioned_operation_identity():
    ledger, client = Ledger(), Client()
    wrapper = governed(client, ledger)
    wrapper.operation_view("semantic_check", identity="revision-2").complete("test")
    reserve = ledger.calls[0][1]
    assert reserve["operation_name"] == "semantic_check"
    assert reserve["operation_key"].startswith("semantic_check:")
    assert len(reserve["operation_key"]) < 255
    assert client.calls == 1


@pytest.mark.parametrize("metadata", [
    {"input_tokens": None},
    {"input_tokens": 30, "output_tokens": 10, "total_tokens": 40, "returned_model_matches": False},
    {"input_tokens": 30, "output_tokens": 10, "total_tokens": 40, "returned_model_matches": True, "finish_reason": "length"},
])
def test_unknown_usage_identity_or_truncation_is_not_refunded_or_retried(metadata):
    ledger, client = Ledger(), Client(metadata)
    with pytest.raises(BudgetExhausted):
        governed(client, ledger).complete("test")
    assert client.calls == 1
    assert ledger.calls[-1][1]["status"] == "outcome_unknown"
    assert ledger.calls[-1][1]["retryable"] is False


def test_timeout_is_unknown_once_and_never_zero_cost():
    ledger, client = Ledger(), Client(error=TimeoutError("private payload"))
    with pytest.raises(BudgetExhausted, match="external_outcome_unknown"):
        governed(client, ledger).complete("test")
    assert client.calls == 1
    finish = ledger.calls[-1][1]
    assert finish["status"] == "outcome_unknown" and finish["monetary_usage"] is None
    assert "private payload" not in repr(ledger.calls)


def test_prompt_and_client_identity_preflight_never_dispatch():
    ledger = Ledger()
    with pytest.raises(BudgetExhausted):
        governed(ledger=ledger, max_prompt_bytes=2).complete("long")
    assert not ledger.calls
    bad = Client()
    bad.max_tokens = None
    with pytest.raises(ValueError):
        governed(bad, ledger)
    assert not ledger.calls


def test_known_overrun_is_recorded_as_actual_cost_then_stops():
    ledger, client = Ledger(), Client({
        "input_tokens": 100000, "output_tokens": 10, "total_tokens": 100010,
        "returned_model_matches": True, "finish_reason": "stop",
        "reasoning_content_reported": True, "reasoning_content_nonempty": False,
    })
    with pytest.raises(BudgetExhausted, match="monetary_reservation_overrun"):
        governed(client, ledger).complete("test")
    assert ledger.calls[-1][1]["monetary_usage"] == (100000, 10)


def test_semantic_operation_view_records_actual_allowed_observations_without_private_payload():
    from legal_rag.harness.observations import HarnessObservationAdapter
    from legal_rag.llm import CompletionUsage
    class ObservedLedger(Ledger):
        def reserve_attempt(self, run_id, **values):
            ref = super().reserve_attempt(run_id, **values)
            return SimpleNamespace(attempt_id=ref.attempt_id, operation_kind="model", attempt_no=1)
        def get_budget(self, run_id):
            return SimpleNamespace(retrieval_rounds_used=1, queries_used=1, tool_attempts_used=1,
                                   model_attempts_used=2, embedding_attempts_used=0)
    class ObservedClient(Client):
        def __init__(self):
            super().__init__()
            self.usage = CompletionUsage()
        def complete(self, prompt):
            self.usage.calls += 1
            self.usage.record_tokens(input_tokens=30, output_tokens=10, total_tokens=40)
            return super().complete(prompt)
    class Collector:
        def __init__(self):
            self.events = []
        def record(self, event):
            self.events.append(event)
    ledger, client, sink = ObservedLedger(), ObservedClient(), Collector()
    wrapper = governed(client, ledger)
    checker_view = wrapper.operation_view("semantic_check", identity="checker-r1")
    wrapper.set_observation_adapter(HarnessObservationAdapter(sink, run_id="run", session_id="session",
                                                             persistence=ledger, completion_client=wrapper))
    wrapper.complete("PRIVATE_SYNTHETIC_PROMPT")
    checker_view.complete("PRIVATE_SYNTHETIC_PROMPT")
    assert [(event.node, event.tool) for event in sink.events] == [("generate", "generator"), ("judge", "judge")]
    assert all(event.counts == {"model_calls": 1} for event in sink.events)
    assert all(event.model_input_tokens == 30 and event.model_output_tokens == 10 for event in sink.events)
    assert client.calls == 2 and len([row for row in ledger.calls if row[0] == "reserve"]) == 2
    assert "PRIVATE_SYNTHETIC_PROMPT" not in repr([event.to_local_record() for event in sink.events])
