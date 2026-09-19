from __future__ import annotations

import json
import importlib.util
import subprocess
from pathlib import Path

import pytest


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "quality_gate.py"
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

    assert any("private.txt" in error and "not a Git candidate" in error for error in errors)
    assert any("missing.md" in error and "not a Git candidate" in error for error in errors)
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


def test_secret_scan_reports_location_and_detector_but_not_value(tmp_path: Path) -> None:
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
    assert record["executed_at"].endswith("Z")


@pytest.mark.parametrize(
    ("milestone", "mode"),
    [("M1", "offline"), ("M0", "integration"), (None, "offline")],
)
def test_unknown_milestone_or_mode_is_rejected(
    milestone: str | None,
    mode: str | None,
) -> None:
    assert gate.validate_request(milestone, mode)


def test_supported_request_is_accepted() -> None:
    assert gate.validate_request("M0", "offline") == []
