"""Single-authorization, fail-closed accounting for the approved live smoke.

Reservations are conservative estimates, not provider tokenizer or invoice
guarantees. An existing ledger is observation-only: it can never dispatch again.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

from .provider_errors import provider_call_error


class LiveBudgetError(RuntimeError):
    """A safe terminal control error, without prompts or provider response data."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"live smoke stopped: {reason}")


@dataclass(frozen=True)
class LiveCallPolicy:
    run_id: str
    model: str
    base_url: str
    max_calls: int
    budget_cny: Decimal
    input_rate_cny_per_million: Decimal
    output_rate_cny_per_million: Decimal
    max_output_tokens: int = 1536
    max_prompt_utf8_bytes: int = 24000
    prompt_overhead_tokens: int = 1024
    enable_thinking: bool = False
    response_format: str = "json_object"

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", self.run_id
        ):
            raise ValueError("run_id must be a bounded safe identifier")
        if (
            self.model != "Qwen/Qwen3.5-35B-A3B"
            or self.base_url != "https://api.siliconflow.cn/v1"
        ):
            raise ValueError("model and endpoint are outside this authorization")
        for value, maximum in (
            (self.max_calls, 10),
            (self.max_output_tokens, 1536),
            (self.max_prompt_utf8_bytes, 24000),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("numeric limit is outside this authorization")
        if (
            type(self.prompt_overhead_tokens) is not int
            or self.prompt_overhead_tokens != 1024
        ):
            raise ValueError("the conservative prompt overhead must remain 1024")
        for value in (
            self.budget_cny,
            self.input_rate_cny_per_million,
            self.output_rate_cny_per_million,
        ):
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or value <= 0
            ):
                raise ValueError("money and rates must be positive finite Decimal values")
        if self.budget_cny > Decimal("2"):
            raise ValueError("budget exceeds the approved two CNY")
        if self.enable_thinking is not False or self.response_format != "json_object":
            raise ValueError("thinking and response format must match this smoke")

    def snapshot(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "model": self.model,
            "base_url": self.base_url,
            "max_calls": self.max_calls,
            "budget_cny": _money(self.budget_cny),
            "input_rate_cny_per_million": _money(self.input_rate_cny_per_million),
            "output_rate_cny_per_million": _money(self.output_rate_cny_per_million),
            "max_output_tokens": self.max_output_tokens,
            "max_prompt_utf8_bytes": self.max_prompt_utf8_bytes,
            "prompt_overhead_tokens": self.prompt_overhead_tokens,
            "enable_thinking": self.enable_thinking,
            "response_format": self.response_format,
            "follow_redirects": False,
        }


class BoundedCompletionClient:
    hidden_retries_disabled: ClassVar[bool] = True
    propagate_provider_errors: ClassVar[bool] = True
    propagate_control_errors: ClassVar[bool] = True

    def __init__(
        self, base_client: Any, ledger_path: str | Path, policy: LiveCallPolicy
    ):
        self.base_client = base_client
        self.ledger_path = Path(ledger_path)
        self.policy = policy
        self.stop_reason: str | None = None
        self._thread_lock = threading.Lock()
        self._lock_path = Path(str(self.ledger_path) + ".lock")
        self._ledger: dict[str, Any] = {
            "schema_version": 1,
            "currency": "CNY",
            "billing_guarantee": False,
            "policy": policy.snapshot(),
            "attempts": [],
            "stop_reason": None,
        }
        self._validate_client()
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self._acquire_file_lock()
        try:
            if self.ledger_path.exists():
                try:
                    existing = json.loads(self.ledger_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, ValueError):
                    raise LiveBudgetError("ledger_invalid") from None
                if (
                    not isinstance(existing, dict)
                    or existing.get("policy") != policy.snapshot()
                ):
                    raise LiveBudgetError("ledger_policy_mismatch")
                if not isinstance(existing.get("attempts"), list):
                    raise LiveBudgetError("ledger_invalid")
                self._ledger = existing
                self.stop_reason = "run_already_exists"
            else:
                self._save_or_stop()
        finally:
            self._release_file_lock()

    @property
    def usage(self):
        return self.base_client.usage

    def complete(self, prompt: str) -> str:
        if self.stop_reason is not None:
            raise LiveBudgetError(self.stop_reason)
        if not self._thread_lock.acquire(blocking=False):
            self.stop_reason = "concurrent_call"
            raise LiveBudgetError(self.stop_reason)
        locked = False
        try:
            self._acquire_file_lock()
            locked = True
            return self._complete_locked(prompt)
        finally:
            if locked:
                self._release_file_lock()
            self._thread_lock.release()

    def _complete_locked(self, prompt: str) -> str:
        try:
            self._validate_client()
        except LiveBudgetError:
            self._stop("client_policy_mismatch")
        if not isinstance(prompt, str) or not prompt.strip():
            self._stop("prompt_invalid")
        prompt_bytes = prompt.encode("utf-8")
        if len(prompt_bytes) > self.policy.max_prompt_utf8_bytes:
            self._stop("prompt_limit")
        digest = hashlib.sha256(prompt_bytes).hexdigest()
        if any(
            item.get("prompt_sha256") == digest
            for item in self._ledger["attempts"]
        ):
            self._stop("duplicate_prompt")
        if len(self._ledger["attempts"]) >= self.policy.max_calls:
            self._stop("call_limit")
        reserved_input = len(prompt_bytes) + self.policy.prompt_overhead_tokens
        reservation = self._cost(reserved_input, self.policy.max_output_tokens)
        if self._committed() + reservation > self.policy.budget_cny:
            self._stop("budget_limit")
        row = {
            "attempt": len(self._ledger["attempts"]) + 1,
            "status": "reserved",
            "prompt_sha256": digest,
            "prompt_utf8_bytes": len(prompt_bytes),
            "reserved_input_tokens": reserved_input,
            "reserved_output_tokens": self.policy.max_output_tokens,
            "reserved_cny": _money(reservation),
            "estimated_cost_cny": None,
            "usage": None,
            "stop_reason": None,
        }
        self._ledger["attempts"].append(row)
        try:
            self._save_or_stop()
        except LiveBudgetError:
            self._ledger["attempts"].pop()
            raise
        # Everything after the durable reservation may have reached the provider.
        failed = False
        try:
            result = self.base_client.complete(prompt)
        except BaseException as exc:
            row["provider_error"] = provider_call_error(
                exc, provider="siliconflow", operation="completion"
            ).to_safe_dict()
            failed = True
        if failed:
            # Raise outside the provider except block, so the raw SDK exception
            # does not become the control error's implicit exception context.
            prompt = "<redacted>"
            self._stop("provider_error", row=row)
        metadata = getattr(self.base_client, "last_response_metadata", None)
        if not isinstance(metadata, dict):
            self._stop("response_invalid", row=row)
        counts = [
            metadata.get(key)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        ]
        if any(value is None for value in counts):
            self._stop("usage_unknown", row=row)
        if any(type(value) is not int or value < 0 for value in counts):
            self._stop("usage_invalid", row=row)
        input_tokens, output_tokens, total_tokens = counts
        if total_tokens != input_tokens + output_tokens:
            self._stop("usage_invalid", row=row)
        row["usage"] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        }
        # A different model has an unverified price. Keep its reported tokens,
        # but do not turn the requested model's rate into a false known cost.
        if metadata.get("returned_model_matches") is not True:
            self._stop("model_mismatch", row=row)
        row["estimated_cost_cny"] = _money(self._cost(input_tokens, output_tokens))
        if input_tokens > reserved_input or output_tokens > self.policy.max_output_tokens:
            self._stop("usage_exceeds_reservation", row=row)
        if metadata.get("finish_reason") == "length":
            self._stop("response_truncated", row=row)
        if metadata.get("finish_reason") != "stop":
            self._stop("response_invalid", row=row)
        if any(
            type(metadata.get(key)) is not bool
            for key in ("reasoning_content_reported", "reasoning_content_nonempty")
        ):
            self._stop("reasoning_invalid", row=row)
        reasoning_tokens = metadata.get("reasoning_tokens")
        if metadata.get("reasoning_content_nonempty") is True or (
            type(reasoning_tokens) is int and reasoning_tokens > 0
        ):
            self._stop("reasoning_returned", row=row)
        if reasoning_tokens is not None and (
            type(reasoning_tokens) is not int or reasoning_tokens < 0
        ):
            self._stop("reasoning_invalid", row=row)
        explicit_empty = (
            metadata.get("reasoning_content_reported") is True
            and metadata.get("reasoning_content_nonempty") is False
        )
        if not explicit_empty and reasoning_tokens != 0:
            self._stop("reasoning_unknown", row=row)
        try:
            valid_json = isinstance(result, str) and isinstance(json.loads(result), dict)
        except (ValueError, TypeError):
            valid_json = False
        if not valid_json:
            self._stop("response_invalid", row=row)
        if self.stop_reason is not None:
            self._stop(self.stop_reason, row=row)
        row["status"] = "succeeded"
        self._save_or_stop()
        return result

    def _validate_client(self) -> None:
        expected = {
            "model": self.policy.model,
            "base_url": self.policy.base_url,
            "max_tokens": self.policy.max_output_tokens,
            "enable_thinking": False,
            "response_format": "json_object",
            "hidden_retries_disabled": True,
            "follow_redirects": False,
        }
        for key, value in expected.items():
            actual = getattr(self.base_client, key, None)
            if actual != value or (isinstance(value, bool) and actual is not value):
                raise LiveBudgetError("client_policy_mismatch")

    def _cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        return (
            input_tokens * self.policy.input_rate_cny_per_million
            + output_tokens * self.policy.output_rate_cny_per_million
        ) / Decimal("1000000")

    def _committed(self) -> Decimal:
        total = Decimal("0")
        for row in self._ledger["attempts"]:
            actual = row.get("estimated_cost_cny")
            reserved = Decimal(row.get("reserved_cny", "0"))
            if row.get("status") == "succeeded" and actual is not None:
                total += Decimal(actual)
            else:
                total += max(
                    reserved, Decimal(actual) if actual is not None else reserved
                )
        return total

    def result_snapshot(self) -> dict[str, Any]:
        result = copy.deepcopy(self._ledger)
        result["stop_reason"] = self.stop_reason or self._ledger.get("stop_reason")
        result["calls_attempted"] = len(self._ledger["attempts"])
        result["committed_cny"] = _money(self._committed())
        unknown = any(
            row.get("estimated_cost_cny") is None
            for row in self._ledger["attempts"]
        )
        known = sum(
            (
                Decimal(row["estimated_cost_cny"])
                for row in self._ledger["attempts"]
                if row.get("estimated_cost_cny") is not None
            ),
            Decimal("0"),
        )
        result["known_cost_cny"] = _money(known)
        result["estimated_total_cost_cny"] = None if unknown else _money(known)
        result["has_unknown_cost"] = unknown
        return result

    def _stop(self, reason: str, *, row: dict[str, Any] | None = None) -> None:
        self.stop_reason = reason
        self._ledger["stop_reason"] = reason
        if row is not None:
            row["status"] = "stopped"
            row["stop_reason"] = reason
        self._save_or_stop()
        raise LiveBudgetError(reason) from None

    def _save_or_stop(self) -> None:
        try:
            self._persist()
        except (OSError, ValueError, TypeError):
            self.stop_reason = "ledger_write_failed"
            raise LiveBudgetError(self.stop_reason) from None

    def _persist(self) -> None:
        descriptor, temporary = tempfile.mkstemp(
            prefix=".live-ledger-", dir=self.ledger_path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(self._ledger, stream, ensure_ascii=True, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.ledger_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _acquire_file_lock(self) -> None:
        try:
            descriptor = os.open(
                self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            os.close(descriptor)
        except OSError:
            self.stop_reason = "ledger_locked"
            raise LiveBudgetError(self.stop_reason) from None

    def _release_file_lock(self) -> None:
        try:
            self._lock_path.unlink()
        except OSError:
            self.stop_reason = "ledger_lock_release_failed"


def _money(value: Decimal) -> str:
    return format(value, "f")


# These are immutable receipt identities for this one repair continuation, not
# a mechanism for issuing a new authorization or accepting caller-supplied IDs.
_FIRST_LEDGER_SHA256 = "3d841f013f26941bc1c772f972ab4e242b493e611129eaa446a8ef834f3fa1f8"
_FIRST_SUMMARY_SHA256 = "99072a7152d2b29f77dccdfad0d1b25ffa3e77c431808c422821e98a8fa34cb6"
_FIRST_AUTHORIZATION_ID = "qwen35b-first-smoke-20261003"
_FIRST_RUN_ID = "live_smoke_20261003_first"


def read_repair_allowance(
    ledger_path: str | Path, summary_path: str | Path
) -> dict[str, Any]:
    """Validate frozen first-run evidence and subtract it from the original cap.

    This is read-only. It does not dispatch, modify either receipt, issue a new
    authorization, or make an unresolved provider outcome refundable. The
    runner must separately enforce its single canonical repair ledger.
    """
    ledger_path, summary_path = Path(ledger_path), Path(summary_path)
    lock_path = Path(str(ledger_path) + ".lock")
    if lock_path.exists():
        raise LiveBudgetError("repair_prior_ledger_locked")
    ledger = _read_frozen_receipt(ledger_path, _FIRST_LEDGER_SHA256)
    summary = _read_frozen_receipt(summary_path, _FIRST_SUMMARY_SHA256)
    if lock_path.exists():
        raise LiveBudgetError("repair_prior_ledger_locked")
    original = LiveCallPolicy(
        run_id=_FIRST_AUTHORIZATION_ID,
        model="Qwen/Qwen3.5-35B-A3B",
        base_url="https://api.siliconflow.cn/v1",
        max_calls=10,
        budget_cny=Decimal("2"),
        input_rate_cny_per_million=Decimal("0.40"),
        output_rate_cny_per_million=Decimal("3.20"),
    )
    _receipt_require(set(ledger) == {
        "schema_version", "currency", "billing_guarantee", "policy",
        "attempts", "stop_reason",
    })
    for name, expected in {
        "schema_version": 1, "currency": "CNY", "billing_guarantee": False,
        "policy": original.snapshot(), "stop_reason": None,
    }.items():
        _receipt_require(_strict_equal(ledger.get(name), expected))
    attempts = ledger.get("attempts")
    _receipt_require(type(attempts) is list and len(attempts) == 2)
    committed = Decimal("0")
    prompt_hashes: set[str] = set()
    for number, row in enumerate(attempts, start=1):
        _receipt_require(type(row) is dict and set(row) == {
            "attempt", "status", "prompt_sha256", "prompt_utf8_bytes",
            "reserved_input_tokens", "reserved_output_tokens", "reserved_cny",
            "estimated_cost_cny", "usage", "stop_reason",
        })
        _receipt_require(_strict_equal(row.get("attempt"), number))
        _receipt_require(row.get("status") == "succeeded" and row.get("stop_reason") is None)
        digest = row.get("prompt_sha256")
        _receipt_require(type(digest) is str and re.fullmatch(r"[a-f0-9]{64}", digest) is not None)
        _receipt_require(digest not in prompt_hashes)
        prompt_hashes.add(digest)
        size = row.get("prompt_utf8_bytes")
        _receipt_require(type(size) is int and 1 <= size <= original.max_prompt_utf8_bytes)
        reserved_input = size + original.prompt_overhead_tokens
        _receipt_require(_strict_equal(row.get("reserved_input_tokens"), reserved_input))
        _receipt_require(_strict_equal(row.get("reserved_output_tokens"), original.max_output_tokens))
        reserved = _receipt_cost(original, reserved_input, original.max_output_tokens)
        _receipt_require(_strict_equal(row.get("reserved_cny"), _money(reserved)))
        usage = row.get("usage")
        _receipt_require(type(usage) is dict and set(usage) == {
            "input_tokens", "output_tokens", "total_tokens",
        })
        _receipt_require(all(type(value) is int and value >= 0 for value in usage.values()))
        inputs, outputs, total = (
            usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]
        )
        _receipt_require(total == inputs + outputs)
        _receipt_require(inputs <= reserved_input and outputs <= original.max_output_tokens)
        cost = _receipt_cost(original, inputs, outputs)
        _receipt_require(_strict_equal(row.get("estimated_cost_cny"), _money(cost)))
        committed += cost

    # The generation failed its verifier after the two provider calls had
    # settled. A timeout, missing usage, incomplete probe or reserved attempt
    # cannot enter this specific repair path.
    expected_facts = {
        "run_id": _FIRST_RUN_ID,
        "artifact_dir": "artifacts/experiments/" + _FIRST_RUN_ID,
        "authorization_id": _FIRST_AUTHORIZATION_ID,
        "probe_status": "passed", "status": "stopped",
        "stop_reason": "generated_verifier_failed",
        "live_model_calls": len(attempts),
        "call_count_status": "canonical_ledger_known",
        "provider_initialized": True, "client_close_status": "closed",
        "rag_cases_completed": 1, "rag_cases_planned": 9,
        "legal_quality_claim": False,
        "dataset_role": "legacy_regression_not_holdout",
        "case_results": [{
            "case_id": "v3_lookup_patent_term",
            "generation_kind": "model", "model_calls": 1,
            "schema_valid": True, "verifier_passed": False,
            "semantic_support_status": "not_checked",
            "final_answer_mode": "insufficient_evidence",
        }],
    }
    for name, expected in expected_facts.items():
        _receipt_require(_strict_equal(summary.get(name), expected))
    expected_budget = {
        **ledger,
        "calls_attempted": len(attempts),
        "committed_cny": _money(committed),
        "estimated_total_cost_cny": _money(committed),
        "has_unknown_cost": False,
        "known_cost_cny": _money(committed),
    }
    _receipt_require(_strict_equal(summary.get("budget"), expected_budget))
    remaining_calls = original.max_calls - len(attempts)
    remaining_budget = original.budget_cny - committed
    _receipt_require(remaining_calls > 0 and remaining_budget > 0)
    return {
        "prior_authorization_id": _FIRST_AUTHORIZATION_ID,
        "prior_run_id": _FIRST_RUN_ID,
        "prior_probe_status": "passed",
        "prior_calls_attempted": len(attempts),
        "prior_committed_cny": _money(committed),
        "remaining_max_calls": remaining_calls,
        "remaining_budget_cny": _money(remaining_budget),
        "prior_ledger_sha256": _FIRST_LEDGER_SHA256,
        "prior_summary_sha256": _FIRST_SUMMARY_SHA256,
    }


def _read_frozen_receipt(path: Path, expected_sha256: str) -> dict[str, Any]:
    try:
        payload = path.read_bytes()
    except OSError:
        raise LiveBudgetError("repair_receipt_invalid") from None
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise LiveBudgetError("repair_receipt_hash_mismatch")
    try:
        result = json.loads(payload)
    except (UnicodeError, ValueError):
        raise LiveBudgetError("repair_receipt_invalid") from None
    _receipt_require(type(result) is dict)
    return result


def _receipt_require(condition: bool) -> None:
    if not condition:
        raise LiveBudgetError("repair_receipt_invalid")


def _strict_equal(actual: Any, expected: Any) -> bool:
    if type(actual) is not type(expected):
        return False
    if type(expected) is dict:
        return actual.keys() == expected.keys() and all(
            _strict_equal(actual[key], expected[key]) for key in expected
        )
    if type(expected) is list:
        return len(actual) == len(expected) and all(
            _strict_equal(item, target) for item, target in zip(actual, expected)
        )
    return actual == expected


def _receipt_cost(policy: LiveCallPolicy, inputs: int, outputs: int) -> Decimal:
    return (
        inputs * policy.input_rate_cny_per_million
        + outputs * policy.output_rate_cny_per_million
    ) / Decimal("1000000")
