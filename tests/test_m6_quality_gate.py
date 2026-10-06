from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "m6_quality_gate", _ROOT / "scripts" / "quality_gate.py"
)
assert _SPEC is not None and _SPEC.loader is not None
gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gate)


def _valid_receipt(candidate_sha: str = "a" * 40) -> dict[str, object]:
    scenarios: dict[str, object] = {}
    for test_id in gate._M6_RECEIPT_REAL_SCENARIOS:
        evidence = dict(gate._M6_RECEIPT_REQUIRED_EVIDENCE[test_id])
        if test_id == "M6-T03":
            evidence.update({"first_pid": 101, "resume_pid": 202})
        scenarios[test_id] = {
            "status": "passed",
            "test_selectors": gate._M6_REAL_TEST_SELECTORS[test_id],
            "evidence": evidence,
        }
    return {
        "schema_version": 1,
        "milestone": "M6",
        "candidate_sha": candidate_sha,
        "status": "passed",
        "live_model_calls": False,
        "database": {
            "backend": "postgresql",
            "migration_head": "0007_m6_jobs_outbox",
        },
        "broker": {
            "backend": "redis",
            "worker_backend": "celery",
            "real_worker_process": True,
        },
        "scenarios": scenarios,
        "redaction": {
            "contains_credentials": False,
            "contains_private_text": False,
            "contains_database_url": False,
            "contains_redis_url": False,
        },
    }


def test_m6_gate_has_58_cumulative_mandatory_ids_and_real_worker_selectors() -> None:
    assert len(gate.MANDATORY_M5_CHECK_IDS) == 51
    assert len(gate.MANDATORY_M6_CHECK_IDS) == 58
    assert gate.MANDATORY_M5_CHECK_IDS < gate.MANDATORY_M6_CHECK_IDS
    assert set(gate.M6_TEST_SELECTORS) == {f"M6-T{number:02}" for number in range(1, 8)}
    for test_id in gate._M6_RECEIPT_REAL_SCENARIOS:
        assert any(
            selector.startswith("integration_tests/test_m6_worker_real.py::")
            for selector in gate.M6_TEST_SELECTORS[test_id]
        )
    assert "M6-T06" not in gate._M6_RECEIPT_REAL_SCENARIOS


def test_m6_receipt_accepts_only_exact_head_real_components_and_all_scenarios() -> None:
    assert (
        gate.validate_m6_worker_receipt_payload(
            _valid_receipt("f" * 40), expected_sha="f" * 40
        )
        == []
    )


def test_m6_receipt_rejects_wrong_head_fake_broker_and_missing_scenario() -> None:
    payload = _valid_receipt()
    broker = payload["broker"]
    scenarios = payload["scenarios"]
    assert isinstance(broker, dict) and isinstance(scenarios, dict)
    broker["real_worker_process"] = False
    broker["backend"] = "fake"
    scenarios.pop("M6-T03")
    errors = gate.validate_m6_worker_receipt_payload(payload, expected_sha="b" * 40)
    assert "M6 worker receipt candidate_sha does not match checked-out HEAD" in errors
    assert "M6 worker receipt must prove a real worker process" in errors
    assert "M6 worker receipt broker backend must be redis" in errors
    assert any("missing scenarios: M6-T03" in error for error in errors)


def test_m6_receipt_rejects_incomplete_kill_evidence_and_sensitive_fields() -> None:
    payload = _valid_receipt()
    scenarios = payload["scenarios"]
    assert isinstance(scenarios, dict)
    t03 = scenarios["M6-T03"]
    assert isinstance(t03, dict)
    evidence = t03["evidence"]
    assert isinstance(evidence, dict)
    evidence["resume_pid"] = evidence["first_pid"]
    evidence["completed_items_not_recomputed"] = False
    evidence["question"] = "private question"
    errors = gate.validate_m6_worker_receipt_payload(payload, expected_sha="a" * 40)
    assert "M6 worker receipt M6-T03 must prove distinct worker PIDs" in errors
    assert any("completed_items_not_recomputed is invalid" in error for error in errors)
    assert "M6 worker receipt contains a forbidden sensitive-content field" in errors


def test_m6_receipt_file_rejects_duplicate_keys_and_missing_file(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.json"
    assert gate.validate_m6_worker_receipt_file(missing, expected_sha="a" * 40) == [
        "M6 worker receipt is unavailable"
    ]
    bad = tmp_path / "duplicate.json"
    bad.write_text('{"schema_version":1,"schema_version":1}\n', encoding="utf-8")
    assert gate.validate_m6_worker_receipt_file(bad, expected_sha="a" * 40) == [
        "M6 worker receipt is not valid bounded UTF-8 JSON"
    ]


def test_m6_preflight_requires_live_broker_config_and_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gate, "_m5_fault_injection_preflight_errors", lambda *args, **kwargs: []
    )
    monkeypatch.setattr(gate, "_m6_selector_contract_errors", lambda *args: [])
    errors = gate._m6_fault_injection_preflight_errors(
        _ROOT,
        fault_receipt=Path("m5.json"),
        worker_receipt=None,
        source={"ALLOW_LIVE_MODEL_CALLS": "false"},
    )
    assert "LEGAL_RAG_REDIS_URL is required for real broker integration" in errors
    assert any("--m6-receipt is required" in error for error in errors)


def test_m6_gate_fails_closed_before_commands_when_receipt_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gate,
        "_m6_fault_injection_preflight_errors",
        lambda *args, **kwargs: ["missing real worker receipt"],
    )
    monkeypatch.setattr(
        gate,
        "_m0_offline_records",
        lambda *args, **kwargs: pytest.fail("gate executed after preflight failure"),
    )
    report = gate.run_m6_fault_injection(_ROOT)
    assert report["status"] == "failed"
    assert report["exit_code"] == 1
    assert len(report["checks"]) == 58
    assert all(record["status"] == "failed" for record in report["checks"])


def test_m6_cumulative_report_requires_every_prior_and_new_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gate, "_m6_fault_injection_preflight_errors", lambda *args, **kwargs: []
    )
    stage_ids = (
        gate.MANDATORY_M0_CHECK_IDS,
        set(gate.M1_TEST_SELECTORS),
        set(gate.M2_TEST_SELECTORS),
        set(gate.M3_TEST_SELECTORS),
        set(gate.M4_TEST_SELECTORS),
        set(gate.M5_TEST_SELECTORS),
        set(gate.M6_TEST_SELECTORS),
    )
    stage_functions = (
        "_m0_offline_records",
        "_m1_acceptance_records",
        "_m2_acceptance_records",
        "_m3_acceptance_records",
        "_m4_acceptance_records",
        "_m5_acceptance_records",
        "_m6_acceptance_records",
    )
    for name, ids in zip(stage_functions, stage_ids, strict=True):
        monkeypatch.setattr(
            gate,
            name,
            lambda *args, _ids=ids, **kwargs: [
                gate.result_record(
                    test_id=test_id,
                    command="verified fixture",
                    exit_code=0,
                    status="passed",
                    output_summary="verified fixture",
                    artifact_path="fixture.json",
                )
                for test_id in sorted(_ids)
            ],
        )
    report = gate.run_m6_fault_injection(
        _ROOT,
        worker_receipt=Path("fixture.json"),
    )
    assert report["status"] == "passed"
    assert len(report["checks"]) == 58
    assert set(report["mandatory_check_ids"]) == gate.MANDATORY_M6_CHECK_IDS
    failed = [dict(record) for record in report["checks"]]
    failed[-1]["status"] = "skipped"
    assert not gate.gate_succeeded(failed, gate.MANDATORY_M6_CHECK_IDS)
    assert not gate.gate_succeeded(failed[:-1], gate.MANDATORY_M6_CHECK_IDS)


def test_m6_request_mode_and_sanitized_redis_environment() -> None:
    assert gate.validate_request("M6", "fault-injection") == []
    assert gate.validate_request("M6", "offline")
    clean = gate.sanitized_environment(
        {
            "PATH": "safe-path",
            "LEGAL_RAG_DATABASE_URL": "postgresql+psycopg://test@localhost/test",
            "LEGAL_RAG_REDIS_URL": "redis://localhost:6379/15",
            "LEGAL_RAG_JOB_REGISTRY_PATH": "fixture-registry.json",
            "OPENAI_API_KEY": "secret-should-not-pass",
        },
        mode="fault-injection",
    )
    assert clean["LEGAL_RAG_REDIS_URL"] == "redis://localhost:6379/15"
    assert clean["LEGAL_RAG_JOB_REGISTRY_PATH"] == "fixture-registry.json"
    assert "OPENAI_API_KEY" not in clean
    assert clean["ALLOW_LIVE_MODEL_CALLS"] == "false"


def test_m6_gate_requires_activation_fence_and_bounded_redrive_regressions() -> None:
    assert {
        "test_queued_job_without_worker_has_bounded_automatic_redelivery",
        "test_queued_recovery_backs_off_between_successful_publications",
    }.issubset(
        {selector.rsplit("::", 1)[-1] for selector in gate.M6_TEST_SELECTORS["M6-T01"]}
    )
    assert any(
        selector.endswith(
            "::test_expired_running_job_has_same_bound_and_remains_claimable"
        )
        for selector in gate.M6_TEST_SELECTORS["M6-T03"]
    )
    assert any(
        selector.endswith("::test_pending_broker_retry_does_not_spend_recovery_budget")
        for selector in gate.M6_TEST_SELECTORS["M6-T04"]
    )
    assert {
        "test_m6_db_stale_activation_cannot_move_pointer_after_takeover",
        "test_m6_db_cancel_before_activation_preserves_pointer",
        "test_m6_db_activation_and_cancel_serialize_on_job_lock",
        "test_m6_db_terminal_failure_rolls_back_catalog_activation",
    }.issubset(
        {selector.rsplit("::", 1)[-1] for selector in gate.M6_TEST_SELECTORS["M6-T05"]}
    )


def test_m6_workflow_has_exact_head_real_postgres_redis_and_receipts() -> None:
    workflow = (_ROOT / ".github" / "workflows" / "quality-gate.yml").read_text(
        encoding="utf-8"
    )
    assert "m6-worker-integration:" in workflow
    m6 = workflow.split("  m6-worker-integration:", maxsplit=1)[1]
    assert "ref: ${{ github.event.pull_request.head.sha }}" in m6
    assert "ref: ${{ github.sha }}" in m6
    assert "persist-credentials: false" in m6
    assert "pgvector/pgvector:" in m6
    assert "redis:" in m6
    assert "integration_tests/test_m6_worker_real.py" in m6
    assert "integration_tests/test_m6_outbox_bounded_recovery_db.py" in m6
    assert "LEGAL_RAG_M6_RECEIPT:" in m6
    assert "LEGAL_RAG_M6_CANDIDATE_SHA:" in m6
    assert "m6-worker-junit.xml" in m6
    assert "_junit_counts" in m6
    assert '("failures", "errors", "skipped")' in m6
    assert "--milestone M6" in m6
    assert "--mode fault-injection" in m6
    assert "--m6-receipt" in m6
    assert "scripts/release_wheel_probe.py" in m6
    assert "--profile M6" in m6
    assert "m6-wheel-probe-receipt.json" in m6
    assert "--fault-receipt" in m6
    assert "m6-quality-gate.json" in m6
    assert "m6-worker-receipt.json" in m6
    assert (
        m6.index("- name: Run the independent real broker and worker suite")
        < m6.index("- name: Reject skipped, xfailed, or absent M6 broker tests")
        < m6.index("- name: Require a non-empty M6 real worker receipt")
        < m6.index("- name: Validate the exact-head M6 worker receipt")
        < m6.index("- name: Run the independent M5 fault suite for cumulative evidence")
        < m6.index(
            "- name: Prepare persistent state for the cumulative PostgreSQL restart check"
        )
        < m6.index("- name: Build the M6 candidate wheel")
        < m6.index("- name: Run the isolated M6 release-wheel probe")
        < m6.index("- name: Run the M6 cumulative real broker quality gate")
    )


def test_m6_receipt_json_roundtrip_is_utf8_and_closed(tmp_path: Path) -> None:
    path = tmp_path / "m6-worker-receipt.json"
    path.write_text(json.dumps(_valid_receipt(), ensure_ascii=False), encoding="utf-8")
    assert gate.validate_m6_worker_receipt_file(path, expected_sha="a" * 40) == []


def test_current_candidate_migration_contract_is_explicit_not_historical_reinterpretation(tmp_path: Path) -> None:
    payload = _valid_receipt()
    payload["database"]["migration_head"] = "0008_execution_money"
    assert gate.validate_m6_worker_receipt_payload(payload, expected_sha="a" * 40)
    assert gate.validate_m6_worker_receipt_payload(
        payload, expected_sha="a" * 40, expected_migration_head="0008_execution_money",
    ) == []
    path = tmp_path / "modern-receipt.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert gate.validate_m6_worker_receipt_file(
        path, expected_sha="a" * 40, expected_migration_head="0008_execution_money",
    ) == []
    assert gate.validate_m6_worker_receipt_payload(
        _valid_receipt(), expected_sha="a" * 40, expected_migration_head="0008_execution_money",
    )
    assert gate.validate_m6_worker_receipt_payload(_valid_receipt(), expected_sha="a" * 40) == []


def test_worker_receipt_cannot_self_select_unrecognized_migration_contract() -> None:
    payload = _valid_receipt()
    payload["database"]["migration_head"] = "invented_head"
    errors = gate.validate_m6_worker_receipt_payload(
        payload, expected_sha="a" * 40, expected_migration_head="invented_head",
    )
    assert "M6 worker receipt expected migration contract is invalid" in errors


def test_candidate_migration_head_is_loaded_from_checked_out_alembic_graph() -> None:
    assert gate.candidate_migration_head(_ROOT) == "0008_execution_money"
