from __future__ import annotations

import json
import importlib.util
import subprocess
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "quality_gate.py"
_WORKFLOW_PATH = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "quality-gate.yml"
)
_SPEC = importlib.util.spec_from_file_location("m0_quality_gate", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gate)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _valid_state() -> dict[str, object]:
    return {
        "document_schema_version": 1,
        "repository": {
            "full_name": "owner/repository",
            "workspace_head": "a" * 40,
        },
        "execution_status": "in_progress",
        "active_milestone": "M0",
        "stage_status_values": ["not_started", "in_progress", "released"],
        "milestones": [
            {
                "id": "M0",
                "required": True,
                "status": "in_progress",
                "tests": {"status": "in_progress"},
                "tag": None,
                "release_url": None,
                "remote_release_verified": False,
            }
        ],
    }


def _state_with_released_m0_and_active_m1() -> dict[str, object]:
    return {
        "document_schema_version": 1,
        "repository": {
            "full_name": "owner/repository",
            "workspace_head": "b" * 40,
        },
        "execution_status": "in_progress",
        "active_milestone": "M1",
        "stage_status_values": ["not_started", "in_progress", "released"],
        "milestones": [
            {
                "id": "M0",
                "required": True,
                "status": "released",
                "tests": {"status": "passed"},
                "tag": "v0.1.1",
                "release_url": "https://example.invalid/releases/v0.1.1",
                "remote_release_verified": True,
            },
            {
                "id": "M1",
                "required": True,
                "status": "in_progress",
                "tests": {"status": "not_run"},
                "tag": None,
                "release_url": None,
                "remote_release_verified": False,
            },
        ],
    }


def _state_with_released_m0_m1_m2_and_active_m3() -> dict[str, object]:
    milestones: list[dict[str, object]] = []
    for milestone_id, tag in (
        ("M0", "v0.1.1"),
        ("M1", "v0.2.0"),
        ("M2", "v0.3.0"),
    ):
        milestones.append(
            {
                "id": milestone_id,
                "required": True,
                "status": "released",
                "tests": {"status": "passed"},
                "tag": tag,
                "release_url": f"https://example.invalid/releases/{tag}",
                "remote_release_verified": True,
            }
        )
    milestones.append(
        {
            "id": "M3",
            "required": True,
            "status": "in_progress",
            "tests": {"status": "not_run"},
            "tag": None,
            "release_url": None,
            "remote_release_verified": False,
        }
    )
    return {
        "document_schema_version": 1,
        "repository": {
            "full_name": "owner/repository",
            "workspace_head": "c" * 40,
        },
        "execution_status": "in_progress",
        "active_milestone": "M3",
        "stage_status_values": ["not_started", "in_progress", "released"],
        "milestones": milestones,
    }


def _state_with_released_m0_through_m3_and_active_m4() -> dict[str, object]:
    state = _state_with_released_m0_m1_m2_and_active_m3()
    milestones = state["milestones"]
    assert isinstance(milestones, list)
    m3 = milestones[-1]
    assert isinstance(m3, dict)
    m3.update(
        {
            "status": "released",
            "tests": {"status": "passed"},
            "tag": "v0.4.0",
            "release_url": "https://example.invalid/releases/v0.4.0",
            "remote_release_verified": True,
        }
    )
    milestones.append(
        {
            "id": "M4",
            "required": True,
            "status": "in_progress",
            "tests": {"status": "not_run"},
            "tag": None,
            "release_url": None,
            "remote_release_verified": False,
        }
    )
    state["active_milestone"] = "M4"
    state["repository"] = {
        "full_name": "owner/repository",
        "workspace_head": "d" * 40,
    }
    return state


def _state_with_released_m0_through_m4_and_active_m5() -> dict[str, object]:
    state = _state_with_released_m0_through_m3_and_active_m4()
    milestones = state["milestones"]
    assert isinstance(milestones, list)
    m4 = milestones[-1]
    assert isinstance(m4, dict)
    m4.update(
        {
            "status": "released",
            "tests": {"status": "passed"},
            "tag": "v0.5.0",
            "release_url": "https://example.invalid/releases/v0.5.0",
            "remote_release_verified": True,
        }
    )
    milestones.append(
        {
            "id": "M5",
            "required": True,
            "status": "in_progress",
            "tests": {"status": "not_run"},
            "tag": None,
            "release_url": None,
            "remote_release_verified": False,
        }
    )
    state["active_milestone"] = "M5"
    state["repository"] = {
        "full_name": "owner/repository",
        "workspace_head": "e" * 40,
    }
    return state


def _valid_m5_fault_receipt(candidate_sha: str = "a" * 40) -> dict[str, object]:
    evidence: dict[str, dict[str, object]] = {
        test_id: dict(expected)
        for test_id, expected in gate._M5_RECEIPT_REQUIRED_EVIDENCE.items()
    }
    for test_id in ("M5-T01", "M5-T02", "M5-T03", "M5-T10"):
        evidence[test_id].update({"first_pid": 101, "resume_pid": 202})
    evidence["M5-T06"]["contender_pids"] = [301, 302]
    return {
        "schema_version": 1,
        "milestone": "M5",
        "candidate_sha": candidate_sha,
        "status": "passed",
        "live_model_calls": False,
        "database": {
            "backend": "postgresql",
            "checkpointer_backend": "langgraph-postgresql",
            "persistent": True,
            "in_memory": False,
            "migration_head": "0006_m5_harness_recovery",
        },
        "scenarios": {
            test_id: {
                "status": "passed",
                "test_selectors": list(selectors),
                "evidence": evidence[test_id],
            }
            for test_id, selectors in gate.M5_TEST_SELECTORS.items()
        },
        "redaction": {
            "contains_prompts": False,
            "contains_evidence_text": False,
            "contains_credentials": False,
            "contains_database_url": False,
        },
    }


def _valid_run_manifest() -> dict[str, object]:
    return {
        "schema_version": 1,
        "execution": {"mode": "offline", "live_model_calls_allowed": False},
        "generation": {"enabled": False},
        "judge": {"enabled": False},
        "result_counts": {"succeeded": None, "failed": None, "not_run": None},
    }


def test_sanitized_environment_removes_credentials_and_forces_offline() -> None:
    clean = gate.sanitized_environment(
        {
            "PATH": "safe-path",
            "HOME": "safe-home",
            "OPENAI_API_KEY": "do-not-copy",
            "AWS_SECRET_ACCESS_KEY": "do-not-copy-either",
            "HTTP_PROXY": "http://proxy-with-credentials.invalid",
        }
    )

    assert clean["PATH"] == "safe-path"
    assert clean["HOME"] == "safe-home"
    assert clean["UV_OFFLINE"] == "1"
    assert clean["HF_HUB_OFFLINE"] == "1"
    assert clean["ALLOW_LIVE_MODEL_CALLS"] == "false"
    assert clean["LEGAL_RAG_DISABLE_DOTENV"] == "1"
    assert "OPENAI_API_KEY" not in clean
    assert "AWS_SECRET_ACCESS_KEY" not in clean
    assert "HTTP_PROXY" not in clean


def test_uv_command_is_offline_frozen_and_no_sync() -> None:
    assert gate.uv_run_command("pytest", "-q") == [
        "uv",
        "run",
        "--offline",
        "--frozen",
        "--no-sync",
        "pytest",
        "-q",
    ]


def test_integration_environment_preserves_only_database_controls_and_redacts_url() -> (
    None
):
    credential = "gate-" + "password"
    database_url = (
        f"postgresql+psycopg://gate-user:{credential}@127.0.0.1/legal_rag_m3_test"
    )
    clean = gate.sanitized_environment(
        {
            "PATH": "safe-path",
            "LEGAL_RAG_DATABASE_URL": database_url,
            "LEGAL_RAG_EXPECTED_PGVECTOR_VERSION": "0.8.6",
            "LEGAL_RAG_INTEGRATION_TEST": "1",
            "OPENAI_API_KEY": "do-not-copy",
        },
        mode="integration",
    )

    assert clean["LEGAL_RAG_DATABASE_URL"] == database_url
    assert clean["LEGAL_RAG_EXPECTED_PGVECTOR_VERSION"] == "0.8.6"
    assert clean["LEGAL_RAG_INTEGRATION_TEST"] == "1"
    assert "OPENAI_API_KEY" not in clean
    password_label = "pass" + "word"
    summary = gate._process_summary(
        f"could not connect to {database_url}; {password_label}={credential}",
        "",
        sensitive_values=gate._sensitive_environment_values(clean),
    )
    assert database_url not in summary
    assert credential not in summary
    assert "<redacted>" in summary


def test_fault_injection_environment_preserves_database_controls_without_credentials() -> (
    None
):
    database_url = "postgresql+psycopg://gate-user:private@127.0.0.1/legal_rag_m5"
    clean = gate.sanitized_environment(
        {
            "PATH": "safe-path",
            "LEGAL_RAG_DATABASE_URL": database_url,
            "LEGAL_RAG_EXPECTED_PGVECTOR_VERSION": "0.8.6",
            "LEGAL_RAG_INTEGRATION_TEST": "1",
            "OPENAI_API_KEY": "do-not-copy",
            "ANTHROPIC_API_KEY": "do-not-copy",
        },
        mode="fault-injection",
    )

    assert clean["LEGAL_RAG_DATABASE_URL"] == database_url
    assert clean["LEGAL_RAG_EXPECTED_PGVECTOR_VERSION"] == "0.8.6"
    assert clean["LEGAL_RAG_INTEGRATION_TEST"] == "1"
    assert clean["ALLOW_LIVE_MODEL_CALLS"] == "false"
    assert "OPENAI_API_KEY" not in clean
    assert "ANTHROPIC_API_KEY" not in clean
    summary = gate.environment_summary(
        mode="fault-injection",
        source=clean,
    )
    assert summary["database_url_configured"] is True
    assert summary["database_url_redacted"] is True
    assert summary["integration_test_guard"] is True


def test_git_candidates_exclude_ignored_files(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    _write(tmp_path / ".gitignore", ".env\nprivate/\n")
    _write(tmp_path / "tracked.md", "tracked\n")
    _write(tmp_path / "untracked.md", "untracked\n")
    _write(tmp_path / ".env", "SECRET=private\n")
    _write(tmp_path / "private" / "notes.md", "private\n")
    subprocess.run(
        ["git", "add", ".gitignore", "tracked.md"],
        cwd=tmp_path,
        check=True,
    )

    candidates = {path.as_posix() for path in gate.git_candidate_files(tmp_path)}

    assert ".gitignore" in candidates
    assert "tracked.md" in candidates
    assert "untracked.md" in candidates
    assert ".env" not in candidates
    assert "private/notes.md" not in candidates


def test_markdown_links_only_accept_candidate_targets(tmp_path: Path) -> None:
    _write(
        tmp_path / "docs" / "guide.md",
        "[tracked](../README.md) [web](https://example.com) [anchor](#part)\n"
        "[ignored](../private.txt) [missing](missing.md)\n",
    )
    _write(tmp_path / "README.md", "# Read me\n")
    _write(tmp_path / "private.txt", "exists but is not a Git candidate\n")
    candidates = [Path("docs/guide.md"), Path("README.md")]

    errors = gate.validate_markdown_links(tmp_path, candidates)

    assert any(
        "private.txt" in error and "not a Git candidate" in error for error in errors
    )
    assert any(
        "missing.md" in error and "not a Git candidate" in error for error in errors
    )
    assert not any("README.md" in error for error in errors)
    assert not any("example.com" in error for error in errors)


def test_markdown_link_escape_and_absolute_path_fail(tmp_path: Path) -> None:
    _write(
        tmp_path / "docs" / "guide.md",
        "[escape](../../outside.md)\n[absolute](/local/file.md)\n",
    )

    errors = gate.validate_markdown_links(tmp_path, [Path("docs/guide.md")])

    assert any("escapes repository" in error for error in errors)
    assert any("must be relative" in error for error in errors)


def test_state_invariants_reject_unverified_release() -> None:
    state = _valid_state()
    state["execution_status"] = "released"
    milestone = state["milestones"][0]  # type: ignore[index]
    milestone["status"] = "released"  # type: ignore[index]

    errors = gate.validate_state_payload(state)

    assert any("must record a tag" in error for error in errors)
    assert any("must record a release_url" in error for error in errors)
    assert any("must verify the remote release" in error for error in errors)
    assert any("tests.status must be passed" in error for error in errors)


def test_completed_m0_gate_accepts_a_later_active_milestone() -> None:
    errors = gate.validate_state_payload(
        _state_with_released_m0_and_active_m1(),
        milestone="M0",
    )

    assert errors == []


def test_active_m1_gate_accepts_released_m0_state() -> None:
    errors = gate.validate_state_payload(
        _state_with_released_m0_and_active_m1(),
        milestone="M1",
    )

    assert errors == []


def test_active_m1_gate_rejects_an_unreleased_m0_prerequisite() -> None:
    state = _state_with_released_m0_and_active_m1()
    m0 = state["milestones"][0]  # type: ignore[index]
    m0["status"] = "not_started"  # type: ignore[index]
    m0["tests"] = {"status": "not_run"}  # type: ignore[index]
    m0["tag"] = None  # type: ignore[index]
    m0["release_url"] = None  # type: ignore[index]
    m0["remote_release_verified"] = False  # type: ignore[index]

    errors = gate.validate_state_payload(state, milestone="M1")

    assert any(
        "prerequisite milestone M0 must already be released" in error
        for error in errors
    )


def test_active_m3_gate_requires_all_released_prerequisites() -> None:
    state = _state_with_released_m0_m1_m2_and_active_m3()
    assert gate.validate_state_payload(state, milestone="M3") == []

    m2 = state["milestones"][2]  # type: ignore[index]
    m2["status"] = "in_progress"  # type: ignore[index]
    m2["tests"] = {"status": "in_progress"}  # type: ignore[index]
    m2["tag"] = None  # type: ignore[index]
    m2["release_url"] = None  # type: ignore[index]
    m2["remote_release_verified"] = False  # type: ignore[index]

    errors = gate.validate_state_payload(state, milestone="M3")
    assert "prerequisite milestone M2 must already be released" in errors


def test_active_m4_gate_requires_all_released_prerequisites() -> None:
    state = _state_with_released_m0_through_m3_and_active_m4()
    assert gate.validate_state_payload(state, milestone="M4") == []

    m3 = state["milestones"][3]  # type: ignore[index]
    m3["status"] = "in_progress"
    m3["tests"] = {"status": "in_progress"}
    m3["tag"] = None
    m3["release_url"] = None
    m3["remote_release_verified"] = False

    errors = gate.validate_state_payload(state, milestone="M4")
    assert "prerequisite milestone M3 must already be released" in errors


def test_active_m5_gate_requires_all_released_prerequisites() -> None:
    state = _state_with_released_m0_through_m4_and_active_m5()
    assert gate.validate_state_payload(state, milestone="M5") == []

    m4 = state["milestones"][4]  # type: ignore[index]
    m4["status"] = "in_progress"  # type: ignore[index]
    m4["tests"] = {"status": "in_progress"}  # type: ignore[index]
    m4["tag"] = None  # type: ignore[index]
    m4["release_url"] = None  # type: ignore[index]
    m4["remote_release_verified"] = False  # type: ignore[index]

    errors = gate.validate_state_payload(state, milestone="M5")
    assert "prerequisite milestone M4 must already be released" in errors


def test_execution_status_must_match_the_actual_active_milestone() -> None:
    state = _state_with_released_m0_and_active_m1()
    state["execution_status"] = "released"

    errors = gate.validate_state_payload(state, milestone="M0")

    assert any(
        "execution_status must equal the active milestone status" in error
        for error in errors
    )


def test_state_and_manifest_check_parses_candidate_json(tmp_path: Path) -> None:
    state_path = Path("docs/refactor/STATE.json")
    manifest_path = Path("docs/refactor/templates/RUN_MANIFEST.example.json")
    _write(tmp_path / state_path, json.dumps(_valid_state()))
    _write(tmp_path / manifest_path, json.dumps(_valid_run_manifest()))

    errors, parsed_count = gate.validate_state_and_manifests(
        tmp_path,
        [state_path, manifest_path],
    )

    assert errors == []
    assert parsed_count == 2


def test_state_and_manifest_check_rejects_invalid_json(tmp_path: Path) -> None:
    state_path = Path("docs/refactor/STATE.json")
    manifest_path = Path("RUN_MANIFEST.json")
    _write(tmp_path / state_path, "{not-json}")
    _write(tmp_path / manifest_path, json.dumps(_valid_run_manifest()))

    errors, parsed_count = gate.validate_state_and_manifests(
        tmp_path,
        [state_path, manifest_path],
    )

    assert parsed_count == 1
    assert any("STATE.json" in error and "invalid JSON" in error for error in errors)


def test_secret_scan_reports_location_and_detector_but_not_value(
    tmp_path: Path,
) -> None:
    fake_secret = "sk-" + "A" * 32
    candidate = Path("candidate.txt")
    _write(tmp_path / candidate, f"SERVICE_API_KEY={fake_secret}\n")

    findings, skipped_count = gate.find_high_confidence_secrets(tmp_path, [candidate])

    assert skipped_count == 0
    assert findings
    assert all(finding.startswith("candidate.txt:1:") for finding in findings)
    assert all(fake_secret not in finding for finding in findings)


def test_secret_scan_ignores_placeholders(tmp_path: Path) -> None:
    candidate = Path("example.env")
    _write(
        tmp_path / candidate,
        "SERVICE_API_KEY=your_api_key_here\nPASSWORD=replace_me_before_use\n",
    )

    findings, skipped_count = gate.find_high_confidence_secrets(tmp_path, [candidate])

    assert findings == []
    assert skipped_count == 0


def test_gate_requires_each_mandatory_record_once_and_passed() -> None:
    records = [
        gate.result_record(
            test_id=test_id,
            command="unit-test",
            exit_code=0,
            status="passed",
            output_summary="ok",
        )
        for test_id in sorted(gate.MANDATORY_M0_CHECK_IDS)
    ]
    assert gate.gate_succeeded(records)

    records[0]["status"] = "skipped"
    assert not gate.gate_succeeded(records)
    records[0]["status"] = "passed"
    records.pop()
    assert not gate.gate_succeeded(records)


def test_m1_gate_is_cumulative_and_maps_every_named_acceptance_test() -> None:
    expected_m1_ids = {f"M1-T{index:02d}" for index in range(1, 11)}

    assert set(gate.MANDATORY_M1_CHECK_IDS) == (
        set(gate.MANDATORY_M0_CHECK_IDS) | expected_m1_ids
    )
    assert set(gate.M1_TEST_SELECTORS) == expected_m1_ids
    assert len(gate.M1_TEST_SELECTORS["M1-T03"]) == 2
    assert len(gate.M1_TEST_SELECTORS["M1-T07"]) == 2
    assert len(gate.M1_TEST_SELECTORS["M1-T08"]) == 3
    assert len(gate.M1_TEST_SELECTORS["M1-T10"]) == 2
    assert any(
        "test_m1_synthetic_examples.py" in selector
        for selector in gate.M1_TEST_SELECTORS["M1-T01"]
    )
    assert any(
        "test_m1_synthetic_examples.py" in selector
        for selector in gate.M1_TEST_SELECTORS["M1-T04"]
    )


def test_m2_gate_is_cumulative_and_maps_every_named_acceptance_test() -> None:
    expected_m2_ids = {f"M2-T{index:02d}" for index in range(1, 9)}

    assert set(gate.MANDATORY_M2_CHECK_IDS) == (
        set(gate.MANDATORY_M1_CHECK_IDS) | expected_m2_ids
    )
    assert set(gate.M2_TEST_SELECTORS) == expected_m2_ids
    assert len(gate.M2_TEST_SELECTORS["M2-T01"]) == 2
    assert len(gate.M2_TEST_SELECTORS["M2-T02"]) == 2
    assert len(gate.M2_TEST_SELECTORS["M2-T03"]) == 2
    assert len(gate.M2_TEST_SELECTORS["M2-T04"]) == 2
    assert len(gate.M2_TEST_SELECTORS["M2-T05"]) == 1
    assert len(gate.M2_TEST_SELECTORS["M2-T06"]) == 2
    assert len(gate.M2_TEST_SELECTORS["M2-T07"]) == 2
    assert len(gate.M2_TEST_SELECTORS["M2-T08"]) == 2


def test_m3_gate_is_cumulative_and_maps_every_named_acceptance_test() -> None:
    expected_m3_ids = {f"M3-T{index:02d}" for index in range(1, 9)}

    assert set(gate.MANDATORY_M3_CHECK_IDS) == (
        set(gate.MANDATORY_M2_CHECK_IDS) | expected_m3_ids
    )
    assert len(gate.MANDATORY_M3_CHECK_IDS) == 33
    assert set(gate.M3_TEST_SELECTORS) == expected_m3_ids
    assert all(gate.M3_TEST_SELECTORS[test_id] for test_id in expected_m3_ids)
    assert all(
        any(selector.startswith("integration_tests/") for selector in selectors)
        for selectors in gate.M3_TEST_SELECTORS.values()
    )


def test_m3_report_has_exactly_one_record_for_all_33_mandatory_ids(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def records_for(ids, *, mode):
        return [
            gate.result_record(
                test_id=test_id,
                command="fixture",
                exit_code=0,
                status="passed",
                output_summary="ok",
                duration_ms=1,
                mode=mode,
            )
            for test_id in sorted(ids)
        ]

    monkeypatch.setattr(
        gate,
        "_m0_offline_records",
        lambda repo_root, state_milestone: records_for(
            gate.MANDATORY_M0_CHECK_IDS, mode="offline"
        ),
    )
    monkeypatch.setattr(
        gate,
        "_m1_acceptance_records",
        lambda repo_root: records_for(gate.M1_TEST_SELECTORS, mode="offline"),
    )
    monkeypatch.setattr(
        gate,
        "_m2_acceptance_records",
        lambda repo_root: records_for(gate.M2_TEST_SELECTORS, mode="offline"),
    )
    monkeypatch.setattr(
        gate,
        "_m3_acceptance_records",
        lambda repo_root, restart_receipt=None: records_for(
            gate.M3_TEST_SELECTORS, mode="integration"
        ),
    )

    report = gate.run_m3_integration(
        tmp_path,
        restart_receipt=tmp_path / "restart-receipt.json",
    )

    assert report["status"] == "passed"
    assert report["exit_code"] == 0
    assert report["mode"] == "integration"
    assert report["duration_ms"] == 33
    assert len(report["mandatory_check_ids"]) == 33
    assert len(report["checks"]) == 33
    assert len({record["test_id"] for record in report["checks"]}) == 33


def test_m4_gate_is_cumulative_and_maps_every_named_acceptance_test() -> None:
    expected_m4_ids = {f"M4-T{index:02d}" for index in range(1, 9)}

    assert set(gate.MANDATORY_M4_CHECK_IDS) == (
        set(gate.MANDATORY_M3_CHECK_IDS) | expected_m4_ids
    )
    assert len(gate.MANDATORY_M4_CHECK_IDS) == 41
    assert set(gate.M4_TEST_SELECTORS) == expected_m4_ids
    assert all(gate.M4_TEST_SELECTORS[test_id] for test_id in expected_m4_ids)
    assert all(
        any(selector.startswith("integration_tests/") for selector in selectors)
        for selectors in gate.M4_TEST_SELECTORS.values()
    )
    assert (
        "integration_tests/test_m4_service_wiring.py::test_m4_t08_provider_free_service_wiring_uses_frozen_postgres_corpus"
        in gate.M4_TEST_SELECTORS["M4-T08"]
    )
    assert (
        "tests/test_m4_supervisor.py::test_timed_out_execution_does_not_poison_the_next_run"
        in gate.M4_TEST_SELECTORS["M4-T07"]
    )
    assert (
        "tests/test_m4_supervisor.py::test_callback_from_a_timed_out_execution_is_fenced"
        in gate.M4_TEST_SELECTORS["M4-T07"]
    )


def test_m4_report_has_exactly_one_record_for_all_41_mandatory_ids(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def records_for(ids, *, mode):
        return [
            gate.result_record(
                test_id=test_id,
                command="fixture",
                exit_code=0,
                status="passed",
                output_summary="ok",
                duration_ms=1,
                mode=mode,
            )
            for test_id in sorted(ids)
        ]

    monkeypatch.setattr(
        gate,
        "_m0_offline_records",
        lambda repo_root, state_milestone: records_for(
            gate.MANDATORY_M0_CHECK_IDS, mode="offline"
        ),
    )
    monkeypatch.setattr(
        gate,
        "_m1_acceptance_records",
        lambda repo_root: records_for(gate.M1_TEST_SELECTORS, mode="offline"),
    )
    monkeypatch.setattr(
        gate,
        "_m2_acceptance_records",
        lambda repo_root: records_for(gate.M2_TEST_SELECTORS, mode="offline"),
    )
    monkeypatch.setattr(
        gate,
        "_m3_acceptance_records",
        lambda repo_root, restart_receipt=None: records_for(
            gate.M3_TEST_SELECTORS, mode="integration"
        ),
    )
    monkeypatch.setattr(
        gate,
        "_m4_acceptance_records",
        lambda repo_root: records_for(gate.M4_TEST_SELECTORS, mode="integration"),
    )

    report = gate.run_m4_integration(
        tmp_path,
        restart_receipt=tmp_path / "restart-receipt.json",
    )

    assert report["status"] == "passed"
    assert report["exit_code"] == 0
    assert report["mode"] == "integration"
    assert report["duration_ms"] == 41
    assert len(report["mandatory_check_ids"]) == 41
    assert len(report["checks"]) == 41
    assert len({record["test_id"] for record in report["checks"]}) == 41


def test_m5_gate_is_cumulative_and_maps_every_named_acceptance_test() -> None:
    expected_m5_ids = {f"M5-T{index:02d}" for index in range(1, 11)}

    assert set(gate.MANDATORY_M5_CHECK_IDS) == (
        set(gate.MANDATORY_M4_CHECK_IDS) | expected_m5_ids
    )
    assert len(gate.MANDATORY_M5_CHECK_IDS) == 51
    assert set(gate.M5_TEST_SELECTORS) == expected_m5_ids
    assert all(gate.M5_TEST_SELECTORS[test_id] for test_id in expected_m5_ids)
    assert all(
        any(selector.startswith("integration_tests/") for selector in selectors)
        for selectors in gate.M5_TEST_SELECTORS.values()
    )
    assert len(gate.M5_TEST_SELECTORS["M5-T04"]) == 2
    assert len(gate.M5_TEST_SELECTORS["M5-T05"]) == 4
    assert len(gate.M5_TEST_SELECTORS["M5-T06"]) == 2
    assert len(gate.M5_TEST_SELECTORS["M5-T10"]) == 2
    assert (
        "integration_tests/test_m5_followup_runs.py::test_m5_t04_clarification_followup_creates_new_parent_bound_run"
        in gate.M5_TEST_SELECTORS["M5-T04"]
    )
    assert (
        "tests/test_m5_retry_policy.py::test_m5_t05_retryable_429_and_timeout_retry_once_and_consume_attempts"
        in gate.M5_TEST_SELECTORS["M5-T05"]
    )
    assert (
        "tests/test_m5_tool_security.py::test_m5_t08_prompt_injection_cannot_select_unlisted_tool_or_override_frozen_scope"
        in gate.M5_TEST_SELECTORS["M5-T08"]
    )
    assert (
        "tests/test_m5_configuration.py::test_m5_persistent_recovery_rejects_in_memory_checkpointer"
        in gate.M5_TEST_SELECTORS["M5-T10"]
    )


def test_m5_selector_contract_requires_real_unique_top_level_test_nodes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    valid = tmp_path / "integration_tests" / "test_valid.py"
    _write(valid, "def test_present():\n    pass\n")
    monkeypatch.setattr(
        gate,
        "M5_TEST_SELECTORS",
        {
            "M5-T01": ("integration_tests/test_valid.py::test_present",),
            "M5-T02": ("integration_tests/test_valid.py::test_missing",),
            "M5-T03": ("integration_tests/test_absent.py::test_absent",),
            "M5-T04": ("integration_tests/test_valid.py::test_present",),
        },
    )

    errors = gate._m5_selector_contract_errors(tmp_path)

    assert any("M5-T02 selector node is unavailable" in error for error in errors)
    assert any("M5-T03 selector file is unavailable" in error for error in errors)
    assert any("selector is assigned more than once" in error for error in errors)


def test_checked_in_m5_selectors_resolve_to_real_test_nodes() -> None:
    assert gate._m5_selector_contract_errors(_SCRIPT_PATH.parents[1]) == []


def test_m5_report_has_exactly_one_record_for_all_51_mandatory_ids(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def records_for(ids, *, mode):
        return [
            gate.result_record(
                test_id=test_id,
                command="fixture",
                exit_code=0,
                status="passed",
                output_summary="ok",
                duration_ms=1,
                mode=mode,
            )
            for test_id in sorted(ids)
        ]

    monkeypatch.setattr(
        gate,
        "_m0_offline_records",
        lambda repo_root, state_milestone: records_for(
            gate.MANDATORY_M0_CHECK_IDS, mode="offline"
        ),
    )
    monkeypatch.setattr(
        gate,
        "_m1_acceptance_records",
        lambda repo_root: records_for(gate.M1_TEST_SELECTORS, mode="offline"),
    )
    monkeypatch.setattr(
        gate,
        "_m2_acceptance_records",
        lambda repo_root: records_for(gate.M2_TEST_SELECTORS, mode="offline"),
    )
    monkeypatch.setattr(
        gate,
        "_m3_acceptance_records",
        lambda repo_root, restart_receipt=None: records_for(
            gate.M3_TEST_SELECTORS, mode="integration"
        ),
    )
    monkeypatch.setattr(
        gate,
        "_m4_acceptance_records",
        lambda repo_root: records_for(gate.M4_TEST_SELECTORS, mode="integration"),
    )
    monkeypatch.setattr(
        gate,
        "_m5_acceptance_records",
        lambda repo_root, fault_receipt=None: records_for(
            gate.M5_TEST_SELECTORS, mode="fault-injection"
        ),
    )
    monkeypatch.setattr(
        gate,
        "_m5_fault_injection_preflight_errors",
        lambda repo_root, fault_receipt: [],
    )

    report = gate.run_m5_fault_injection(
        tmp_path,
        restart_receipt=tmp_path / "restart-receipt.json",
        fault_receipt=tmp_path / "fault-receipt.json",
    )

    assert report["status"] == "passed"
    assert report["exit_code"] == 0
    assert report["mode"] == "fault-injection"
    assert report["duration_ms"] == 51
    assert len(report["mandatory_check_ids"]) == 51
    assert len(report["checks"]) == 51
    assert len({record["test_id"] for record in report["checks"]}) == 51


def test_m5_cumulative_gate_runs_no_commands_when_global_preflight_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        gate,
        "_m5_fault_injection_preflight_errors",
        lambda repo_root, fault_receipt: ["fault receipt is invalid"],
    )

    def must_not_run(*args, **kwargs):
        raise AssertionError(f"unexpected gate execution: {args!r} {kwargs!r}")

    for name in (
        "_m0_offline_records",
        "_m1_acceptance_records",
        "_m2_acceptance_records",
        "_m3_acceptance_records",
        "_m4_acceptance_records",
        "_m5_acceptance_records",
    ):
        monkeypatch.setattr(gate, name, must_not_run)

    report = gate.run_m5_fault_injection(
        tmp_path,
        restart_receipt=tmp_path / "restart.json",
        fault_receipt=tmp_path / "fault.json",
    )

    assert report["status"] == "failed"
    assert report["exit_code"] == 1
    assert len(report["checks"]) == 51
    assert {record["test_id"] for record in report["checks"]} == set(
        gate.MANDATORY_M5_CHECK_IDS
    )
    assert all(record["status"] == "failed" for record in report["checks"])
    assert all(
        "no gate commands were executed" in record["output_summary"]
        for record in report["checks"]
    )


def test_mandatory_pytest_check_fails_closed_on_skip_or_xfail(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def fake_subprocess_check(**kwargs):
        command = kwargs["command"]
        junit_index = command.index("--junitxml") + 1
        junit_path = Path(command[junit_index])
        _write(
            junit_path,
            '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="1" /></testsuites>',
        )
        return gate.result_record(
            test_id=kwargs["test_id"],
            command=command,
            exit_code=0,
            status="passed",
            output_summary="pytest returned zero",
            artifact_path=kwargs.get("artifact_path"),
        )

    monkeypatch.setattr(gate, "run_subprocess_check", fake_subprocess_check)

    record = gate.run_pytest_check(
        test_id="M1-T01",
        selectors=("tests/example.py::test_required",),
        repo_root=tmp_path,
        timeout_seconds=30,
    )

    assert record["status"] == "failed"
    assert record["exit_code"] == 1
    assert "skipped or xfailed" in record["output_summary"]


def test_mandatory_pytest_check_fails_closed_on_zero_tests(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def fake_subprocess_check(**kwargs):
        command = kwargs["command"]
        junit_index = command.index("--junitxml") + 1
        junit_path = Path(command[junit_index])
        _write(
            junit_path,
            '<testsuites><testsuite tests="0" failures="0" errors="0" skipped="0" /></testsuites>',
        )
        return gate.result_record(
            test_id=kwargs["test_id"],
            command=command,
            exit_code=0,
            status="passed",
            output_summary="pytest returned zero",
        )

    monkeypatch.setattr(gate, "run_subprocess_check", fake_subprocess_check)
    record = gate.run_pytest_check(
        test_id="M3-T01",
        selectors=("integration_tests/example.py::test_required",),
        repo_root=tmp_path,
        timeout_seconds=30,
        mode="integration",
    )

    assert record["status"] == "failed"
    assert record["exit_code"] == 1
    assert "no tests were executed" in record["output_summary"]


def test_mandatory_pytest_check_fails_closed_when_junit_is_missing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def fake_subprocess_check(**kwargs):
        return gate.result_record(
            test_id=kwargs["test_id"],
            command=kwargs["command"],
            exit_code=0,
            status="passed",
            output_summary="pytest returned zero without an artifact",
        )

    monkeypatch.setattr(gate, "run_subprocess_check", fake_subprocess_check)
    record = gate.run_pytest_check(
        test_id="M3-T01",
        selectors=("integration_tests/example.py::test_required",),
        repo_root=tmp_path,
        timeout_seconds=30,
        mode="integration",
    )

    assert record["status"] == "failed"
    assert record["exit_code"] == 1
    assert "JUnit validation failed" in record["output_summary"]


def test_m3_gate_fails_closed_before_pytest_when_database_is_missing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("LEGAL_RAG_DATABASE_URL", raising=False)
    monkeypatch.delenv("LEGAL_RAG_INTEGRATION_TEST", raising=False)

    def must_not_run(**kwargs):
        raise AssertionError(f"unexpected pytest execution: {kwargs}")

    monkeypatch.setattr(gate, "run_pytest_check", must_not_run)
    records = gate._m3_acceptance_records(tmp_path)

    assert {record["test_id"] for record in records} == set(gate.M3_TEST_SELECTORS)
    assert len(records) == 8
    assert all(record["status"] == "failed" for record in records)
    assert all(record["exit_code"] == 1 for record in records)
    assert all(
        "LEGAL_RAG_DATABASE_URL is required" in record["output_summary"]
        for record in records
    )


def test_m4_gate_fails_closed_before_pytest_when_database_is_missing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("LEGAL_RAG_DATABASE_URL", raising=False)
    monkeypatch.delenv("LEGAL_RAG_INTEGRATION_TEST", raising=False)

    def must_not_run(**kwargs):
        raise AssertionError(f"unexpected pytest execution: {kwargs}")

    monkeypatch.setattr(gate, "run_pytest_check", must_not_run)
    records = gate._m4_acceptance_records(tmp_path)

    assert {record["test_id"] for record in records} == set(gate.M4_TEST_SELECTORS)
    assert len(records) == 8
    assert all(record["status"] == "failed" for record in records)
    assert all(record["exit_code"] == 1 for record in records)
    assert all(
        "LEGAL_RAG_DATABASE_URL is required" in record["output_summary"]
        for record in records
    )


def test_modern_m5_compatibility_receipt_requires_explicit_current_migration(tmp_path: Path) -> None:
    payload = _valid_m5_fault_receipt()
    payload["database"]["migration_head"] = "0008_execution_money"
    assert gate.validate_m5_fault_receipt_payload(payload, expected_sha="a" * 40)
    assert gate.validate_m5_fault_receipt_payload(
        payload, expected_sha="a" * 40, expected_migration_head="0008_execution_money",
    ) == []
    path = tmp_path / "current-fault.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert gate.validate_m5_fault_receipt_file(
        path, expected_sha="a" * 40, expected_migration_head="0008_execution_money",
    ) == []
    assert gate.validate_m5_fault_receipt_payload(_valid_m5_fault_receipt(), expected_sha="a" * 40) == []
    assert gate.validate_m5_fault_receipt_payload(
        _valid_m5_fault_receipt(), expected_sha="a" * 40, expected_migration_head="0008_execution_money",
    )


def test_modern_m5_receipt_rejects_unrecognized_self_selected_migration() -> None:
    payload = _valid_m5_fault_receipt()
    payload["database"]["migration_head"] = "invented_head"
    errors = gate.validate_m5_fault_receipt_payload(
        payload, expected_sha="a" * 40, expected_migration_head="invented_head",
    )
    assert "M5 fault receipt expected migration contract is invalid" in errors


def test_valid_m5_fault_receipt_binds_exact_head_and_all_scenarios() -> None:
    payload = _valid_m5_fault_receipt("f" * 40)

    assert (
        gate.validate_m5_fault_receipt_payload(
            payload,
            expected_sha="f" * 40,
        )
        == []
    )


def test_m5_fault_receipt_requires_real_graph_and_recovery_path_evidence() -> None:
    required = gate._M5_RECEIPT_REQUIRED_EVIDENCE

    assert required["M5-T01"]["graph_executor_used"] is True
    assert required["M5-T01"]["postgres_saver_checkpoint_observed"] is True
    assert required["M5-T02"]["graph_unknown_outcome_path_exercised"] is True
    assert required["M5-T02"]["provider_call_observed"] is True
    assert required["M5-T02"]["post_provider_pre_artifact_crash_exercised"] is True
    assert required["M5-T02"]["orphaned_succeeded_attempt_reconciled"] is True
    assert required["M5-T02"]["duplicate_provider_call_avoided"] is True
    assert required["M5-T02"]["post_planner_pre_checkpoint_crash_exercised"] is True
    assert required["M5-T02"]["orphaned_planner_succeeded_attempt_reconciled"] is True
    assert required["M5-T02"]["duplicate_planner_call_avoided"] is True
    assert required["M5-T03"]["reconciliation_path_exercised"] is True
    assert required["M5-T04"]["graph_loop_exercised"] is True
    assert required["M5-T04"]["clarification_followup_created_new_run"] is True
    assert required["M5-T04"]["parent_run_link_preserved"] is True
    assert required["M5-T04"]["original_run_budget_unchanged"] is True
    assert required["M5-T05"]["node_retry_path_exercised"] is True
    assert required["M5-T06"]["stale_checkpoint_write_rejected"] is True
    assert required["M5-T06"]["stale_terminal_write_rejected"] is True
    assert required["M5-T08"]["graph_security_path_exercised"] is True
    assert required["M5-T09"]["graph_deadline_path_exercised"] is True


@pytest.mark.parametrize(
    ("test_id", "field"),
    [
        ("M5-T01", "graph_executor_used"),
        ("M5-T01", "postgres_saver_checkpoint_observed"),
        ("M5-T02", "graph_unknown_outcome_path_exercised"),
        ("M5-T02", "provider_call_observed"),
        ("M5-T02", "post_provider_pre_artifact_crash_exercised"),
        ("M5-T02", "orphaned_succeeded_attempt_reconciled"),
        ("M5-T02", "duplicate_provider_call_avoided"),
        ("M5-T02", "post_planner_pre_checkpoint_crash_exercised"),
        ("M5-T02", "orphaned_planner_succeeded_attempt_reconciled"),
        ("M5-T02", "duplicate_planner_call_avoided"),
        ("M5-T03", "reconciliation_path_exercised"),
        ("M5-T04", "graph_loop_exercised"),
        ("M5-T04", "clarification_followup_created_new_run"),
        ("M5-T04", "parent_run_link_preserved"),
        ("M5-T04", "original_run_budget_unchanged"),
        ("M5-T05", "node_retry_path_exercised"),
        ("M5-T06", "stale_checkpoint_write_rejected"),
        ("M5-T06", "stale_terminal_write_rejected"),
        ("M5-T08", "graph_security_path_exercised"),
        ("M5-T09", "graph_deadline_path_exercised"),
    ],
)
def test_m5_fault_receipt_rejects_missing_real_execution_evidence(
    test_id: str,
    field: str,
) -> None:
    payload = _valid_m5_fault_receipt()
    scenarios = payload["scenarios"]
    assert isinstance(scenarios, dict)
    scenario = scenarios[test_id]
    assert isinstance(scenario, dict)
    evidence = scenario["evidence"]
    assert isinstance(evidence, dict)
    evidence.pop(field)

    errors = gate.validate_m5_fault_receipt_payload(
        payload,
        expected_sha="a" * 40,
    )

    assert f"M5 fault receipt {test_id}.evidence.{field} is invalid" in errors


def test_m5_fault_receipt_rejects_memory_backend_wrong_head_and_missing_scenario() -> (
    None
):
    payload = _valid_m5_fault_receipt("a" * 40)
    database = payload["database"]
    assert isinstance(database, dict)
    database["checkpointer_backend"] = "InMemorySaver"
    scenarios = payload["scenarios"]
    assert isinstance(scenarios, dict)
    scenarios.pop("M5-T09")

    errors = gate.validate_m5_fault_receipt_payload(
        payload,
        expected_sha="b" * 40,
    )

    assert "M5 fault receipt candidate_sha does not match checked-out HEAD" in errors
    assert any("persistent PostgreSQL" in error for error in errors)
    assert any("missing scenarios: M5-T09" in error for error in errors)


def test_m5_fault_receipt_rejects_boolean_schema_version() -> None:
    payload = _valid_m5_fault_receipt()
    payload["schema_version"] = True

    errors = gate.validate_m5_fault_receipt_payload(
        payload,
        expected_sha="a" * 40,
    )

    assert any("schema_version must equal 1" in error for error in errors)


def test_m5_fault_receipt_rejects_same_process_and_sensitive_content() -> None:
    payload = _valid_m5_fault_receipt()
    scenarios = payload["scenarios"]
    assert isinstance(scenarios, dict)
    t01 = scenarios["M5-T01"]
    assert isinstance(t01, dict)
    evidence = t01["evidence"]
    assert isinstance(evidence, dict)
    evidence["resume_pid"] = evidence["first_pid"]
    evidence["question"] = "must never appear in a public receipt"

    errors = gate.validate_m5_fault_receipt_payload(
        payload,
        expected_sha="a" * 40,
    )

    assert any("M5-T01 must prove distinct process PIDs" in error for error in errors)
    assert any("forbidden sensitive-content field" in error for error in errors)


def test_m5_fault_receipt_uses_closed_schema_and_scans_string_values() -> None:
    payload = _valid_m5_fault_receipt()
    payload["notes"] = "unexpected"
    database = payload["database"]
    assert isinstance(database, dict)
    database["migration_head"] = "prefix-0006-unverified"
    scenarios = payload["scenarios"]
    assert isinstance(scenarios, dict)
    t04 = scenarios["M5-T04"]
    assert isinstance(t04, dict)
    t04["status"] = "Bearer synthetic-token-value"
    evidence = t04["evidence"]
    assert isinstance(evidence, dict)
    evidence["debug"] = "unexpected"

    errors = gate.validate_m5_fault_receipt_payload(
        payload,
        expected_sha="a" * 40,
    )

    assert "M5 fault receipt has unexpected top-level fields" in errors
    assert "M5 fault receipt must identify the 0006 migration head" in errors
    assert any("M5-T04.evidence has unexpected fields" in error for error in errors)
    assert "M5 fault receipt contains a sensitive-looking value" in errors


def test_m5_fault_receipt_file_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    receipt = tmp_path / "m5-fault-receipt.json"
    receipt.write_text(
        '{"schema_version":1,"schema_version":1}\n',
        encoding="utf-8",
    )

    errors = gate.validate_m5_fault_receipt_file(
        receipt,
        expected_sha="a" * 40,
    )

    assert errors == ["M5 fault receipt is not valid bounded UTF-8 JSON"]


def test_m5_fault_receipt_file_fails_closed_on_excessive_json_nesting(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "m5-deep-receipt.json"
    receipt.write_text(
        '{"nested":' * 1500 + "null" + "}" * 1500,
        encoding="utf-8",
    )

    errors = gate.validate_m5_fault_receipt_file(
        receipt,
        expected_sha="a" * 40,
    )

    assert errors == ["M5 fault receipt exceeds the maximum allowed nesting depth"]


def test_m5_gate_fails_closed_before_pytest_without_database_or_receipt(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("LEGAL_RAG_DATABASE_URL", raising=False)
    monkeypatch.delenv("LEGAL_RAG_INTEGRATION_TEST", raising=False)

    def must_not_run(**kwargs):
        raise AssertionError(f"unexpected pytest execution: {kwargs}")

    monkeypatch.setattr(gate, "run_pytest_check", must_not_run)
    records = gate._m5_acceptance_records(tmp_path, fault_receipt=None)

    assert {record["test_id"] for record in records} == set(gate.M5_TEST_SELECTORS)
    assert len(records) == 10
    assert all(record["status"] == "failed" for record in records)
    assert all(record["exit_code"] == 1 for record in records)
    assert all(
        "LEGAL_RAG_DATABASE_URL is required" in record["output_summary"]
        and "--fault-receipt is required" in record["output_summary"]
        for record in records
    )


def test_m5_preflight_rejects_live_model_calls_even_before_receipt() -> None:
    errors = gate._m5_fault_injection_preflight_errors(
        Path.cwd(),
        None,
        source={
            "LEGAL_RAG_DATABASE_URL": "postgresql+psycopg://gate@127.0.0.1/db",
            "LEGAL_RAG_INTEGRATION_TEST": "1",
            "ALLOW_LIVE_MODEL_CALLS": "true",
        },
    )

    assert "ALLOW_LIVE_MODEL_CALLS must be false for M5 fault injection" in errors


def test_m5_acceptance_validates_receipt_before_running_exact_selectors(
    monkeypatch,
    tmp_path: Path,
) -> None:
    candidate_sha = "c" * 40
    receipt = tmp_path / "m5-fault-receipt.json"
    receipt.write_text(
        json.dumps(_valid_m5_fault_receipt(candidate_sha)) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "LEGAL_RAG_DATABASE_URL",
        "postgresql+psycopg://gate@127.0.0.1/legal_rag_m5",
    )
    monkeypatch.setenv("LEGAL_RAG_INTEGRATION_TEST", "1")
    monkeypatch.setenv("ALLOW_LIVE_MODEL_CALLS", "false")
    monkeypatch.setattr(gate, "_git_head_sha", lambda repo_root: candidate_sha)
    monkeypatch.setattr(gate, "_m5_selector_contract_errors", lambda repo_root: [])
    # This test represents the published M5 graph, not the current checkout.
    monkeypatch.setattr(gate, "candidate_migration_head", lambda repo_root: "0006_m5_harness_recovery")
    calls: list[tuple[str, tuple[str, ...], str]] = []

    def fake_pytest_check(**kwargs):
        calls.append(
            (
                kwargs["test_id"],
                tuple(kwargs["selectors"]),
                kwargs["mode"],
            )
        )
        return gate.result_record(
            test_id=kwargs["test_id"],
            command="pytest",
            exit_code=0,
            status="passed",
            output_summary="ok",
            artifact_path=kwargs["artifact_path"],
            mode=kwargs["mode"],
        )

    monkeypatch.setattr(gate, "run_pytest_check", fake_pytest_check)
    records = gate._m5_acceptance_records(tmp_path, fault_receipt=receipt)

    assert len(records) == 10
    assert all(record["status"] == "passed" for record in records)
    assert calls == [
        (test_id, selectors, "fault-injection")
        for test_id, selectors in gate.M5_TEST_SELECTORS.items()
    ]
    assert all(record["artifact_path"] == str(receipt.resolve()) for record in records)


def test_m3_t08_requires_external_restart_receipt(tmp_path: Path) -> None:
    record = gate._m3_t08_record(tmp_path, None)

    assert record["test_id"] == "M3-T08"
    assert record["status"] == "failed"
    assert record["exit_code"] == 1
    assert "actual PostgreSQL service restart" in record["command"]
    assert "--restart-receipt is required" in record["output_summary"]


def test_m3_t08_combines_migrations_with_real_restart_probe_verification(
    monkeypatch,
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "restart-receipt.json"
    receipt.write_text("{}\n", encoding="utf-8")

    def fake_pytest_check(**kwargs):
        return gate.result_record(
            test_id=kwargs["test_id"],
            command=["uv", "run", "pytest", "migration-selector"],
            exit_code=0,
            status="passed",
            output_summary="JUnit: tests=2, failures=0, errors=0, skipped=0",
            artifact_path=kwargs["artifact_path"],
            duration_ms=11,
            mode=kwargs["mode"],
        )

    def fake_subprocess_check(**kwargs):
        assert kwargs["command"][-4:-2] == [
            "scripts/m3_restart_probe.py",
            "verify",
        ]
        assert kwargs["command"][-1] == str(receipt)
        return gate.result_record(
            test_id=kwargs["test_id"],
            command=kwargs["command"],
            exit_code=0,
            status="passed",
            output_summary='{"stage":"verified_after_service_restart"}',
            artifact_path=kwargs["artifact_path"],
            duration_ms=13,
            mode=kwargs["mode"],
        )

    monkeypatch.setattr(gate, "run_pytest_check", fake_pytest_check)
    monkeypatch.setattr(gate, "run_subprocess_check", fake_subprocess_check)

    record = gate._m3_t08_record(tmp_path, receipt)

    assert record["status"] == "passed"
    assert record["exit_code"] == 0
    assert record["duration_ms"] == 24
    assert len(record["command"]) == 2
    assert "m3_restart_probe.py verify" in record["command"][1]
    assert "verified_after_service_restart" in record["output_summary"]
    assert record["artifact_path"] == str(receipt.resolve())


def test_result_record_contains_required_machine_readable_fields() -> None:
    record = gate.result_record(
        test_id="M0-example",
        command=["python", "-V"],
        exit_code=0,
        status="passed",
        output_summary="Python",
    )

    assert gate.REQUIRED_RECORD_FIELDS.issubset(record)
    assert record["environment"]["mode"] == "offline"
    assert record["environment"]["dotenv_loading_disabled"] is True
    assert record["environment"]["live_model_calls_allowed"] is False
    assert record["executed_at"].endswith("Z")
    assert record["duration_ms"] == 0


@pytest.mark.parametrize(
    ("milestone", "mode"),
    [
        ("M3", "offline"),
        ("M0", "integration"),
        ("M4", "offline"),
        ("M5", "offline"),
        ("M5", "integration"),
        ("M4", "fault-injection"),
        (None, "offline"),
    ],
)
def test_unknown_milestone_or_mode_is_rejected(
    milestone: str | None,
    mode: str | None,
) -> None:
    assert gate.validate_request(milestone, mode)


@pytest.mark.parametrize("milestone", ["M0", "M1", "M2"])
def test_supported_request_is_accepted(milestone: str) -> None:
    assert gate.validate_request(milestone, "offline") == []


def test_m3_integration_request_is_accepted() -> None:
    assert gate.validate_request("M3", "integration") == []


def test_m4_integration_request_is_accepted() -> None:
    assert gate.validate_request("M4", "integration") == []


def test_m5_fault_injection_request_is_accepted() -> None:
    assert gate.validate_request("M5", "fault-injection") == []


def test_main_dispatches_the_requested_milestone(monkeypatch, capsys) -> None:
    calls: list[str] = []

    def report_for(milestone: str) -> dict[str, object]:
        calls.append(milestone)
        return {
            "schema_version": 1,
            "milestone": milestone,
            "mode": (
                "fault-injection"
                if milestone == "M5"
                else "integration" if milestone in {"M3", "M4"} else "offline"
            ),
            "started_at": "2026-09-20T00:00:00Z",
            "finished_at": "2026-09-20T00:00:00Z",
            "status": "passed",
            "exit_code": 0,
            "mandatory_check_ids": [],
            "checks": [],
            "artifact_path": None,
        }

    monkeypatch.setattr(gate, "run_m0_offline", lambda repo_root: report_for("M0"))
    monkeypatch.setattr(gate, "run_m1_offline", lambda repo_root: report_for("M1"))
    monkeypatch.setattr(gate, "run_m2_offline", lambda repo_root: report_for("M2"))
    restart_receipts: list[Path | None] = []

    def run_m3(repo_root, *, restart_receipt=None):
        restart_receipts.append(restart_receipt)
        return report_for("M3")

    monkeypatch.setattr(gate, "run_m3_integration", run_m3)

    def run_m4(repo_root, *, restart_receipt=None):
        restart_receipts.append(restart_receipt)
        return report_for("M4")

    monkeypatch.setattr(gate, "run_m4_integration", run_m4)

    fault_receipts: list[Path | None] = []

    def run_m5(repo_root, *, restart_receipt=None, fault_receipt=None):
        restart_receipts.append(restart_receipt)
        fault_receipts.append(fault_receipt)
        return report_for("M5")

    monkeypatch.setattr(gate, "run_m5_fault_injection", run_m5)

    assert gate.main(["--milestone", "M2", "--mode", "offline"]) == 0
    assert calls == ["M2"]
    assert json.loads(capsys.readouterr().out)["milestone"] == "M2"

    restart_receipt = Path("restart-receipt.json")
    assert (
        gate.main(
            [
                "--milestone",
                "M3",
                "--mode",
                "integration",
                "--restart-receipt",
                str(restart_receipt),
            ]
        )
        == 0
    )
    assert calls == ["M2", "M3"]
    assert restart_receipts == [restart_receipt]
    assert json.loads(capsys.readouterr().out)["milestone"] == "M3"

    assert (
        gate.main(
            [
                "--milestone",
                "M4",
                "--mode",
                "integration",
                "--restart-receipt",
                str(restart_receipt),
            ]
        )
        == 0
    )
    assert calls == ["M2", "M3", "M4"]
    assert restart_receipts == [restart_receipt, restart_receipt]
    assert json.loads(capsys.readouterr().out)["milestone"] == "M4"

    fault_receipt = Path("fault-receipt.json")
    assert (
        gate.main(
            [
                "--milestone",
                "M5",
                "--mode",
                "fault-injection",
                "--restart-receipt",
                str(restart_receipt),
                "--fault-receipt",
                str(fault_receipt),
            ]
        )
        == 0
    )
    assert calls == ["M2", "M3", "M4", "M5"]
    assert restart_receipts == [restart_receipt, restart_receipt, restart_receipt]
    assert fault_receipts == [fault_receipt]
    assert json.loads(capsys.readouterr().out)["milestone"] == "M5"


def test_main_returns_configuration_exit_code_for_unsupported_request(capsys) -> None:
    assert gate.main(["--milestone", "M3", "--mode", "offline"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "failed"
    assert report["exit_code"] == 2


def test_ci_runs_the_cumulative_m2_gate_with_a_pinned_report_upload() -> None:
    workflow = _WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "pull_request_target" not in workflow
    assert "secrets." not in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "persist-credentials: false" in workflow
    assert "--milestone M2" in workflow
    assert "--mode offline" in workflow
    assert "--milestone M0" not in workflow
    assert "--milestone M1" not in workflow
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in workflow
    assert (
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in workflow
    )
    assert "${{ github.run_attempt }}" in workflow
    assert "if-no-files-found: error" in workflow


def test_ci_runs_m4_after_mandatory_wheel_and_restart_verification() -> None:
    workflow = _WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "m4-service-integration:" in workflow
    assert workflow.count("--extra service") >= 2
    assert "--extra database" not in workflow
    assert "scripts/m3_restart_probe.py prepare" in workflow
    assert "docker restart" in workflow
    assert "scripts/m3_restart_probe.py verify" in workflow
    assert '--restart-receipt "${RUNNER_TEMP}/m3-restart-receipt.json"' in workflow
    assert "--milestone M4" in workflow
    assert "--mode integration" in workflow
    assert workflow.index("- name: Build the M4 candidate wheel") < workflow.index(
        "- name: Run the M4 cumulative integration quality gate"
    )
    assert workflow.index(
        "- name: Verify M4 wheel resources and console entry points"
    ) < workflow.index("- name: Run the M4 cumulative integration quality gate")
    assert workflow.index(
        "- name: Run the isolated M4 wheel installation probe"
    ) < workflow.index("- name: Run the M4 cumulative integration quality gate")
    assert "scripts/m4_wheel_probe.py" in workflow
    assert 'candidate_version="$(python -c' in workflow
    assert '--expected-version "${candidate_version}"' in workflow
    assert "--smoke" in workflow
    assert "m4-wheel-probe-receipt.json" in workflow
    gate_step = workflow.split(
        "- name: Run the M4 cumulative integration quality gate", maxsplit=1
    )[1].split("- name: Upload the M4 service integration report", maxsplit=1)[0]
    assert "--wheel" not in gate_step
    m4_integration_step = workflow.split(
        "- name: Run service integration tests against a fresh database", maxsplit=1
    )[1].split(
        "- name: Prepare persistent state for the PostgreSQL service restart",
        maxsplit=1,
    )[
        0
    ]
    assert "integration_tests/test_m3_*.py" in m4_integration_step
    assert "integration_tests/test_m4_*.py" in m4_integration_step
    assert "integration_tests/test_m5_" not in m4_integration_step


def test_ci_checks_m4_wheel_runtime_resources_and_entry_points() -> None:
    workflow = _WORKFLOW_PATH.read_text(encoding="utf-8")

    required_resources = {
        "legal_rag/storage/alembic/versions/0005_m4_api_sessions.py",
        "legal_rag/api/app.py",
        "legal_rag/api/auth.py",
        "legal_rag/api/command.py",
        "legal_rag/api/schemas.py",
        "legal_rag/api/settings.py",
        "legal_rag/services/run_service.py",
        "legal_rag/services/run_executor.py",
        "legal_rag/services/service_retrieval.py",
        "legal_rag/services/supervisor.py",
    }
    assert all(resource in workflow for resource in required_resources)
    assert 'scripts.get("legal-rag") == "legal_rag.cli:main"' in workflow
    assert 'scripts.get("legal-rag-api") == "legal_rag.api.command:main"' in workflow


def test_ci_has_an_exact_head_provider_free_m5_fault_job() -> None:
    workflow = _WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "m5-fault-injection:" in workflow
    assert 'ALLOW_LIVE_MODEL_CALLS: "false"' in workflow
    assert "LEGAL_RAG_M5_CANDIDATE_SHA:" in workflow
    assert "LEGAL_RAG_M5_FAULT_RECEIPT:" in workflow
    assert "integration_tests/test_m5_fault_recovery.py" in workflow
    assert "integration_tests/test_m5_budget_and_errors.py" in workflow
    assert "integration_tests/test_m5_concurrent_resume.py" in workflow
    assert "integration_tests/test_m5_checkpoint_compatibility.py" in workflow
    assert "integration_tests/test_m5_followup_runs.py" in workflow
    assert "integration_tests/test_m5_prompt_injection.py" in workflow
    assert "integration_tests/test_m5_schema.py" in workflow
    assert "tests/test_m5_configuration.py" in workflow
    assert "tests/test_m5_graph_runtime.py" in workflow
    assert "tests/test_m5_recovery_demo.py" in workflow
    assert "tests/test_m5_retry_policy.py" in workflow
    assert "tests/test_m5_state_contract.py" in workflow
    assert "tests/test_m5_tool_security.py" in workflow
    assert "m5-fault-junit.xml" in workflow
    assert "m5-fault-receipt.json" in workflow
    assert "scripts/m5_recovery_demo.py" in workflow
    assert "m5-recovery-demo-receipt.json" in workflow
    assert "scripts/release_wheel_probe.py" in workflow
    assert "--profile M5" in workflow
    assert 'm5-wheel-probe-receipt.json" 2>&1' in workflow
    assert "--milestone M5" in workflow
    assert "--mode fault-injection" in workflow
    assert '--fault-receipt "${RUNNER_TEMP}/m5-fault-receipt.json"' in workflow
    assert "legal_rag_m3_test_m5" in workflow
    assert (
        "m5-fault-${{ github.event_name == 'pull_request' "
        "&& github.event.pull_request.head.sha || github.sha }}-${{ github.run_attempt }}"
        in workflow
    )

    m5_workflow = workflow.split("  m5-fault-injection:", maxsplit=1)[1].split(
        "  m6-worker-integration:", maxsplit=1
    )[0]
    assert 'candidate_version="$(python -c' in m5_workflow
    assert '--expected-version "${candidate_version}"' in m5_workflow
    fault_suite = m5_workflow.index("- name: Run the independent M5 fault-injection suite")
    junit_guard = m5_workflow.index("- name: Reject skipped or xfailed M5 suite tests")
    recovery_demo = m5_workflow.index("- name: Run the one-command M5 recovery demo")
    restart_prepare = m5_workflow.rindex(
        "- name: Prepare persistent state for the cumulative PostgreSQL restart check"
    )
    wheel_probe = m5_workflow.index("- name: Run the isolated M5 release-wheel probe")
    cumulative_gate = m5_workflow.index(
        "- name: Run the M5 cumulative fault-injection quality gate"
    )
    assert (
        fault_suite
        < junit_guard
        < recovery_demo
        < restart_prepare
        < wheel_probe
        < cumulative_gate
    )
    fault_suite_step = m5_workflow[fault_suite:junit_guard]
    assert "xfail_strict=true" in fault_suite_step
    assert "--junitxml" in fault_suite_step
    junit_guard_step = m5_workflow[junit_guard:recovery_demo]
    assert "_junit_counts" in junit_guard_step
    assert '("failures", "errors", "skipped")' in junit_guard_step


def test_ci_uploads_only_sanitized_m5_receipts_not_raw_state() -> None:
    workflow = _WORKFLOW_PATH.read_text(encoding="utf-8")
    upload = workflow.split(
        "- name: Upload the M5 fault-injection evidence", maxsplit=1
    )[1]

    assert "m5-quality-gate.json" in upload
    assert "m5-wheel-probe-receipt.json" in upload
    assert "m5-fault-receipt.json" in upload
    assert "m5-fault-junit.xml" in upload
    assert "m5-recovery-demo-receipt.json" in upload
    assert "database.dump" not in upload
    assert "checkpoint.json" not in upload
    assert "process.log" not in upload


def test_ci_checks_out_and_labels_the_exact_event_commit() -> None:
    workflow = _WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "if: github.event_name == 'pull_request'" in workflow
    assert "ref: ${{ github.event.pull_request.head.sha }}" in workflow
    assert "if: github.event_name == 'push'" in workflow
    assert "ref: ${{ github.sha }}" in workflow
    assert (
        "m2-quality-gate-${{ github.event_name == 'pull_request' "
        "&& github.event.pull_request.head.sha || github.sha }}-${{ github.run_attempt }}"
        in workflow
    )
    assert (
        "m4-service-${{ github.event_name == 'pull_request' "
        "&& github.event.pull_request.head.sha || github.sha }}-${{ github.run_attempt }}"
        in workflow
    )
