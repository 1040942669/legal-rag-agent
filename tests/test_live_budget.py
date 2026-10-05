from __future__ import annotations

import json
import threading
from decimal import Decimal
from pathlib import Path

import pytest

import legal_rag.live_budget as live_budget
from legal_rag.live_budget import (
    BoundedCompletionClient,
    LiveBudgetError,
    LiveCallPolicy,
)
from legal_rag.llm import CompletionUsage


class FakeClient:
    model = "Qwen/Qwen3.5-35B-A3B"
    base_url = "https://api.siliconflow.cn/v1"
    max_tokens = 1536
    enable_thinking = False
    response_format = "json_object"
    hidden_retries_disabled = True
    follow_redirects = False

    def __init__(self):
        self.usage = CompletionUsage()
        self.last_response_metadata = {}
        self.requests = 0
        self.metadata_changes = {}
        self.failure = None
        self.entered = None
        self.release = None

    def complete(self, prompt):
        self.requests += 1
        self.usage.calls += 1
        if self.entered is not None:
            self.entered.set()
            assert self.release.wait(5)
        if self.failure is not None:
            self.usage.failed_calls += 1
            raise self.failure
        self.usage.record_tokens(input_tokens=100, output_tokens=20, total_tokens=120)
        self.last_response_metadata = {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "finish_reason": "stop",
            "returned_model_matches": True,
            "reasoning_content_reported": True,
            "reasoning_content_nonempty": False,
            "reasoning_tokens": 0,
            **self.metadata_changes,
        }
        return '{"answer": "safe"}'


def policy(**changes):
    values = {
        "run_id": "approved-run-001",
        "model": "Qwen/Qwen3.5-35B-A3B",
        "base_url": "https://api.siliconflow.cn/v1",
        "max_calls": 10,
        "budget_cny": Decimal("2"),
        "input_rate_cny_per_million": Decimal("0.40"),
        "output_rate_cny_per_million": Decimal("3.20"),
    }
    return LiveCallPolicy(**{**values, **changes})


def wrap(tmp_path, fake=None, **changes):
    return BoundedCompletionClient(
        fake or FakeClient(), tmp_path / "ledger.json", policy(**changes)
    )


def test_reservation_is_persisted_before_dispatch_and_contains_no_prompt(tmp_path):
    fake = FakeClient()
    original = fake.complete

    def verify_then_complete(prompt):
        saved = json.loads((tmp_path / "ledger.json").read_text(encoding="utf-8"))
        assert saved["attempts"][0]["status"] == "reserved"
        assert saved["attempts"][0]["estimated_cost_cny"] is None
        assert saved["attempts"][0]["reserved_input_tokens"] == len(prompt.encode()) + 1024
        assert prompt not in json.dumps(saved)
        return original(prompt)

    fake.complete = verify_then_complete
    bounded = wrap(tmp_path, fake)
    assert bounded.complete("private-question-123") == '{"answer": "safe"}'
    snapshot = bounded.result_snapshot()
    assert snapshot["known_cost_cny"] == "0.000104"
    assert snapshot["calls_attempted"] == 1
    assert snapshot["currency"] == "CNY"
    assert snapshot["billing_guarantee"] is False
    assert bounded.usage is fake.usage


def test_maximum_calls_cannot_be_bypassed_by_catching_error(tmp_path):
    fake = FakeClient()
    bounded = wrap(tmp_path, fake, max_calls=2)
    bounded.complete("one")
    bounded.complete("two")
    for _ in range(2):
        with pytest.raises(LiveBudgetError, match="call_limit"):
            bounded.complete("three")
    assert fake.requests == 2
    assert bounded.stop_reason == "call_limit"


def test_pre_call_budget_reserves_output_and_conservative_input(tmp_path):
    fake = FakeClient()
    bounded = wrap(tmp_path, fake, budget_cny=Decimal("0.001"))
    with pytest.raises(LiveBudgetError, match="budget_limit"):
        bounded.complete("one")
    assert fake.requests == 0
    assert bounded.result_snapshot()["attempts"] == []


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"prompt_tokens": None}, "usage_unknown"),
        ({"completion_tokens": None}, "usage_unknown"),
        ({"total_tokens": None}, "usage_unknown"),
        ({"prompt_tokens": True}, "usage_invalid"),
        ({"total_tokens": 121}, "usage_invalid"),
        ({"completion_tokens": 1537, "total_tokens": 1637}, "usage_exceeds_reservation"),
        ({"finish_reason": "length"}, "response_truncated"),
        ({"finish_reason": None}, "response_invalid"),
        ({"returned_model_matches": False}, "model_mismatch"),
        ({"reasoning_content_nonempty": True}, "reasoning_returned"),
        ({"reasoning_tokens": 1}, "reasoning_returned"),
        ({"reasoning_content_reported": False, "reasoning_tokens": None}, "reasoning_unknown"),
    ],
)
def test_uncertain_or_abnormal_response_stops_permanently(tmp_path, changes, reason):
    fake = FakeClient()
    fake.metadata_changes = changes
    bounded = wrap(tmp_path, fake)
    with pytest.raises(LiveBudgetError, match=reason):
        bounded.complete("one")
    with pytest.raises(LiveBudgetError, match=reason):
        bounded.complete("two")
    assert fake.requests == 1
    snapshot = bounded.result_snapshot()
    assert snapshot["attempts"][0]["status"] != "succeeded"
    assert Decimal(snapshot["committed_cny"]) >= Decimal("0.0053252")
    assert snapshot["stop_reason"] == reason
    if reason == "usage_unknown":
        assert snapshot["attempts"][0]["estimated_cost_cny"] is None


def test_provider_failure_keeps_reservation_without_raw_error(tmp_path):
    fake = FakeClient()
    fake.failure = RuntimeError("secret-api-key private-provider-body")
    bounded = wrap(tmp_path, fake)
    with pytest.raises(LiveBudgetError, match="provider_error") as caught:
        bounded.complete("private-user-input")
    assert "secret-api-key" not in str(caught.value)
    assert caught.value.__context__ is None
    assert "secret-api-key" not in (tmp_path / "ledger.json").read_text(encoding="utf-8")
    snapshot = bounded.result_snapshot()
    assert snapshot["attempts"][0]["estimated_cost_cny"] is None
    with pytest.raises(LiveBudgetError, match="provider_error"):
        bounded.complete("second")
    assert fake.requests == 1


def test_existing_ledger_is_never_overwritten_or_reused(tmp_path):
    first = wrap(tmp_path)
    first.complete("one")
    before = (tmp_path / "ledger.json").read_bytes()
    other = FakeClient()
    restarted = wrap(tmp_path, other)
    with pytest.raises(LiveBudgetError, match="run_already_exists"):
        restarted.complete("one")
    assert other.requests == 0
    assert (tmp_path / "ledger.json").read_bytes() == before


def test_existing_unknown_reservation_never_dispatches_again(tmp_path):
    first = wrap(tmp_path)
    first._ledger["attempts"].append({"status": "reserved"})
    first._persist()
    before = (tmp_path / "ledger.json").read_bytes()
    restarted = wrap(tmp_path)
    with pytest.raises(LiveBudgetError, match="run_already_exists"):
        restarted.complete("never")
    assert (tmp_path / "ledger.json").read_bytes() == before


def test_concurrent_second_wrapper_stops_before_provider(tmp_path):
    first = wrap(tmp_path)
    lock = Path(str(tmp_path / "ledger.json") + ".lock")
    lock.write_text("busy", encoding="utf-8")
    fake = FakeClient()
    with pytest.raises(LiveBudgetError, match="ledger_locked"):
        wrap(tmp_path, fake)
    assert fake.requests == 0
    assert first.result_snapshot()["calls_attempted"] == 0


def test_same_wrapper_concurrent_attempt_does_not_dispatch_twice(tmp_path):
    fake = FakeClient()
    fake.entered, fake.release = threading.Event(), threading.Event()
    bounded = wrap(tmp_path, fake)
    outcomes = []

    def call():
        try:
            bounded.complete("one")
        except LiveBudgetError as exc:
            outcomes.append(exc.reason)

    worker = threading.Thread(target=call)
    worker.start()
    assert fake.entered.wait(5)
    with pytest.raises(LiveBudgetError, match="concurrent_call"):
        bounded.complete("two")
    fake.release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert fake.requests == 1
    with pytest.raises(LiveBudgetError, match="concurrent_call"):
        bounded.complete("three")


def test_failed_reservation_write_dispatches_zero_calls(tmp_path, monkeypatch):
    fake = FakeClient()
    bounded = wrap(tmp_path, fake)

    def fail():
        raise OSError("sensitive-filesystem-description")

    monkeypatch.setattr(bounded, "_persist", fail)
    with pytest.raises(LiveBudgetError, match="ledger_write_failed"):
        bounded.complete("one")
    assert fake.requests == 0
    assert bounded.stop_reason == "ledger_write_failed"


def test_failed_result_write_prevents_subsequent_dispatch(tmp_path, monkeypatch):
    fake = FakeClient()
    bounded = wrap(tmp_path, fake)
    persist = bounded._persist
    writes = 0

    def fail_after_reservation():
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("disk-full")
        persist()

    monkeypatch.setattr(bounded, "_persist", fail_after_reservation)
    with pytest.raises(LiveBudgetError, match="ledger_write_failed"):
        bounded.complete("one")
    with pytest.raises(LiveBudgetError, match="ledger_write_failed"):
        bounded.complete("two")
    assert fake.requests == 1
    saved = json.loads((tmp_path / "ledger.json").read_text(encoding="utf-8"))
    assert saved["attempts"][0]["status"] == "reserved"


def test_prompt_limit_and_duplicate_prompt_stop_without_extra_calls(tmp_path):
    fake = FakeClient()
    bounded = wrap(tmp_path, fake, max_prompt_utf8_bytes=3)
    with pytest.raises(LiveBudgetError, match="prompt_limit"):
        bounded.complete("中文")
    assert fake.requests == 0


def test_duplicate_prompt_cannot_be_paid_twice_with_same_run(tmp_path):
    fake = FakeClient()
    bounded = wrap(tmp_path, fake)
    bounded.complete("one")
    with pytest.raises(LiveBudgetError, match="duplicate_prompt"):
        bounded.complete("one")
    assert fake.requests == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"max_calls": 11}, {"max_calls": True}, {"budget_cny": Decimal("2.01")},
        {"budget_cny": Decimal("NaN")}, {"input_rate_cny_per_million": Decimal("-1")},
        {"model": "other-model"}, {"base_url": "https://other.example/v1"},
        {"max_output_tokens": 1537}, {"enable_thinking": True},
        {"response_format": None}, {"prompt_overhead_tokens": 0},
    ],
)
def test_invalid_policy_is_rejected_before_dispatch(tmp_path, changes):
    fake = FakeClient()
    with pytest.raises(ValueError):
        wrap(tmp_path, fake, **changes)
    assert fake.requests == 0


@pytest.mark.parametrize("field,value", [("model", "other"), ("max_tokens", None), ("enable_thinking", None), ("hidden_retries_disabled", False)])
def test_client_configuration_cannot_bypass_policy(tmp_path, field, value):
    fake = FakeClient()
    setattr(fake, field, value)
    with pytest.raises(LiveBudgetError, match="client_policy_mismatch"):
        wrap(tmp_path, fake)
    assert fake.requests == 0


def test_client_mutation_between_calls_is_checked_again(tmp_path):
    fake = FakeClient()
    bounded = wrap(tmp_path, fake)
    bounded.complete("one")
    fake.enable_thinking = True
    with pytest.raises(LiveBudgetError, match="client_policy_mismatch"):
        bounded.complete("two")
    assert fake.requests == 1


@pytest.mark.parametrize("response", ["", "not json", "[]", "null"])
def test_invalid_response_cannot_become_a_success(tmp_path, response):
    fake = FakeClient()
    complete = fake.complete

    def invalid(prompt):
        complete(prompt)
        return response

    fake.complete = invalid
    bounded = wrap(tmp_path, fake)
    with pytest.raises(LiveBudgetError, match="response_invalid"):
        bounded.complete("one")
    assert fake.requests == 1
    assert bounded.result_snapshot()["attempts"][0]["status"] == "stopped"


def test_corrupt_existing_ledger_does_not_allow_a_new_request(tmp_path):
    (tmp_path / "ledger.json").write_text("not json", encoding="utf-8")
    fake = FakeClient()
    with pytest.raises(LiveBudgetError, match="ledger_invalid"):
        wrap(tmp_path, fake)
    assert fake.requests == 0


def test_initial_ledger_creation_failure_has_zero_requests(tmp_path, monkeypatch):
    fake = FakeClient()

    def fail(self):
        raise OSError("private-path")

    monkeypatch.setattr(BoundedCompletionClient, "_persist", fail)
    with pytest.raises(LiveBudgetError, match="ledger_write_failed"):
        wrap(tmp_path, fake)
    assert fake.requests == 0


@pytest.mark.parametrize("field", ["prompt_tokens", "completion_tokens", "total_tokens"])
@pytest.mark.parametrize("value", [True, "120", -1, 1.2])
def test_usage_fields_require_strict_nonnegative_integer(tmp_path, field, value):
    fake = FakeClient()
    fake.metadata_changes = {field: value}
    bounded = wrap(tmp_path, fake)
    with pytest.raises(LiveBudgetError, match="usage_invalid"):
        bounded.complete("one")
    assert fake.requests == 1


def test_explicit_zero_reasoning_tokens_is_sufficient_observation(tmp_path):
    fake = FakeClient()
    fake.metadata_changes = {"reasoning_content_reported": False, "reasoning_tokens": 0}
    assert wrap(tmp_path, fake).complete("one")


def test_explicit_empty_reasoning_content_is_sufficient_observation(tmp_path):
    fake = FakeClient()
    fake.metadata_changes = {"reasoning_content_reported": True, "reasoning_tokens": None}
    assert wrap(tmp_path, fake).complete("one")


@pytest.mark.parametrize(
    "field", ["reasoning_content_reported", "reasoning_content_nonempty"]
)
@pytest.mark.parametrize("value", ["true", "false", 1, 0, None])
def test_reasoning_flags_require_strict_boolean(tmp_path, field, value):
    fake = FakeClient()
    fake.metadata_changes = {field: value, "reasoning_tokens": 0}
    bounded = wrap(tmp_path, fake)
    with pytest.raises(LiveBudgetError, match="reasoning_invalid"):
        bounded.complete("one")
    with pytest.raises(LiveBudgetError, match="reasoning_invalid"):
        bounded.complete("two")
    assert fake.requests == 1


@pytest.mark.parametrize(
    "field", ["reasoning_content_reported", "reasoning_content_nonempty"]
)
def test_missing_reasoning_flag_stops_even_with_zero_reasoning_tokens(tmp_path, field):
    fake = FakeClient()
    complete = fake.complete

    def incomplete_metadata(prompt):
        response = complete(prompt)
        fake.last_response_metadata.pop(field)
        return response

    fake.complete = incomplete_metadata
    bounded = wrap(tmp_path, fake)
    with pytest.raises(LiveBudgetError, match="reasoning_invalid"):
        bounded.complete("one")
    assert fake.requests == 1


def test_model_mismatch_has_unknown_cost_not_requested_model_price(tmp_path):
    fake = FakeClient()
    fake.metadata_changes = {"returned_model_matches": False}
    bounded = wrap(tmp_path, fake)
    with pytest.raises(LiveBudgetError, match="model_mismatch"):
        bounded.complete("one")
    snapshot = bounded.result_snapshot()
    attempt = snapshot["attempts"][0]
    assert attempt["status"] == "stopped"
    assert attempt["usage"] == {
        "input_tokens": 100, "output_tokens": 20, "total_tokens": 120
    }
    assert attempt["estimated_cost_cny"] is None
    assert snapshot["has_unknown_cost"] is True
    assert snapshot["estimated_total_cost_cny"] is None
    assert snapshot["committed_cny"] == attempt["reserved_cny"]
    with pytest.raises(LiveBudgetError, match="model_mismatch"):
        bounded.complete("two")
    assert fake.requests == 1


def repair_receipts(tmp_path, monkeypatch, *, mutate_ledger=None, mutate_summary=None):
    first_policy = policy(run_id="qwen35b-first-smoke-20261003").snapshot()
    attempts = []
    for number, size, tokens, cost, reservation in (
        (1, 63, (27, 5, 32), "0.0000268", "0.00535"),
        (2, 4922, (967, 169, 1136), "0.0009276", "0.0072936"),
    ):
        attempts.append({
            "attempt": number, "status": "succeeded", "prompt_sha256": str(number) * 64,
            "prompt_utf8_bytes": size, "reserved_input_tokens": size + 1024,
            "reserved_output_tokens": 1536, "reserved_cny": reservation,
            "estimated_cost_cny": cost, "stop_reason": None,
            "usage": dict(zip(("input_tokens", "output_tokens", "total_tokens"), tokens)),
        })
    ledger = {
        "schema_version": 1, "currency": "CNY", "billing_guarantee": False,
        "policy": first_policy, "attempts": attempts, "stop_reason": None,
    }
    if mutate_ledger is not None:
        mutate_ledger(ledger)
    summary = {
        "run_id": "live_smoke_20261003_first",
        "artifact_dir": "artifacts/experiments/live_smoke_20261003_first",
        "authorization_id": "qwen35b-first-smoke-20261003",
        "probe_status": "passed", "status": "stopped",
        "stop_reason": "generated_verifier_failed", "live_model_calls": 2,
        "call_count_status": "canonical_ledger_known", "provider_initialized": True,
        "client_close_status": "closed", "rag_cases_completed": 1,
        "rag_cases_planned": 9, "legal_quality_claim": False,
        "dataset_role": "legacy_regression_not_holdout",
        "case_results": [{
            "case_id": "v3_lookup_patent_term", "generation_kind": "model",
            "model_calls": 1, "schema_valid": True, "verifier_passed": False,
            "semantic_support_status": "not_checked", "final_answer_mode": "insufficient_evidence",
        }],
        "budget": {**ledger, "calls_attempted": 2, "committed_cny": "0.0009544",
                   "known_cost_cny": "0.0009544", "estimated_total_cost_cny": "0.0009544",
                   "has_unknown_cost": False},
    }
    if mutate_summary is not None:
        mutate_summary(summary)
    ledger_path, summary_path = tmp_path / "first.json", tmp_path / "summary.json"
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    monkeypatch.setattr(live_budget, "_FIRST_LEDGER_SHA256", live_budget.hashlib.sha256(ledger_path.read_bytes()).hexdigest(), raising=False)
    monkeypatch.setattr(live_budget, "_FIRST_SUMMARY_SHA256", live_budget.hashlib.sha256(summary_path.read_bytes()).hexdigest(), raising=False)
    return ledger_path, summary_path


def test_repair_allowance_reuses_original_authorization_not_new_budget(tmp_path, monkeypatch):
    ledger, summary = repair_receipts(tmp_path, monkeypatch)
    before = ledger.read_bytes(), summary.read_bytes()
    result = live_budget.read_repair_allowance(ledger, summary)
    assert result["prior_calls_attempted"] == 2
    assert result["prior_committed_cny"] == "0.0009544"
    assert result["remaining_max_calls"] == 8
    assert result["remaining_budget_cny"] == "1.9990456"
    assert result["prior_authorization_id"] == "qwen35b-first-smoke-20261003"
    assert result["prior_run_id"] == "live_smoke_20261003_first"
    assert result["prior_probe_status"] == "passed"
    assert result["prior_ledger_sha256"] == live_budget._FIRST_LEDGER_SHA256
    assert result["prior_summary_sha256"] == live_budget._FIRST_SUMMARY_SHA256
    assert (ledger.read_bytes(), summary.read_bytes()) == before


@pytest.mark.parametrize("kind", ["ledger", "summary"])
def test_repair_receipt_hash_mismatch_blocks_continuation(tmp_path, monkeypatch, kind):
    ledger, summary = repair_receipts(tmp_path, monkeypatch)
    target = ledger if kind == "ledger" else summary
    target.write_text(target.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(LiveBudgetError, match="repair_receipt_hash_mismatch"):
        live_budget.read_repair_allowance(ledger, summary)


def test_repair_active_prior_lock_blocks_continuation(tmp_path, monkeypatch):
    ledger, summary = repair_receipts(tmp_path, monkeypatch)
    Path(str(ledger) + ".lock").write_text("active", encoding="utf-8")
    with pytest.raises(LiveBudgetError, match="repair_prior_ledger_locked"):
        live_budget.read_repair_allowance(ledger, summary)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda x: x["policy"].update(model="other"),
        lambda x: x["policy"].update(base_url="https://other.example/v1"),
        lambda x: x["policy"].update(input_rate_cny_per_million="0.01"),
        lambda x: x["policy"].update(max_calls=True),
        lambda x: x["policy"].update(enable_thinking=0),
        lambda x: x["policy"].update(run_id="other-authorization"),
        lambda x: x["attempts"][0].update(status="reserved"),
        lambda x: x["attempts"][0].update(estimated_cost_cny=None),
        lambda x: x["attempts"][0]["usage"].update(input_tokens=True),
        lambda x: x["attempts"][0]["usage"].update(total_tokens=33),
        lambda x: x["attempts"][0].update(reserved_cny="0.000001"),
        lambda x: x["attempts"][1].update(attempt=1),
        lambda x: x.update(stop_reason="usage_unknown"),
    ],
)
def test_repair_unknown_or_invalid_ledger_is_never_refunded(tmp_path, monkeypatch, mutate):
    ledger, summary = repair_receipts(tmp_path, monkeypatch, mutate_ledger=mutate)
    with pytest.raises(LiveBudgetError, match="repair_receipt_invalid"):
        live_budget.read_repair_allowance(ledger, summary)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda x: x.update(run_id="other-run"),
        lambda x: x.update(probe_status="failed"),
        lambda x: x.update(status="passed"),
        lambda x: x.update(stop_reason="provider_error"),
        lambda x: x.update(live_model_calls=1),
        lambda x: x["budget"].update(committed_cny="0"),
        lambda x: x["budget"].update(committed_cny=0.0009544),
        lambda x: x["budget"].update(has_unknown_cost=True),
        lambda x: x["case_results"][0].update(verifier_passed=True),
        lambda x: x["budget"].update(attempts=[]),
    ],
)
def test_repair_summary_mismatch_cannot_reopen_a_budget(tmp_path, monkeypatch, mutate):
    ledger, summary = repair_receipts(tmp_path, monkeypatch, mutate_summary=mutate)
    with pytest.raises(LiveBudgetError, match="repair_receipt_invalid"):
        live_budget.read_repair_allowance(ledger, summary)
