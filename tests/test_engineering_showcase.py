from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import engineering_showcase as showcase


def _junit(path: Path, *, tests: int = 1, skipped: bool = False) -> None:
    skip_count = int(skipped)
    body = '<testcase classname="synthetic" name="test_contract">'
    body += '<skipped type="pytest.xfail"/>' if skipped else ""
    body += "</testcase>" if tests else ""
    if not tests:
        body = ""
    path.write_text(
        f'<testsuites><testsuite tests="{tests}" failures="0" errors="0" skipped="{skip_count}">{body}</testsuite></testsuites>',
        encoding="utf-8",
    )


@pytest.fixture
def synthetic_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    monkeypatch.setattr(showcase, "REPOSITORY_ROOT", root)
    return root


def _passing_runner(command, **kwargs):
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == showcase.GROUP_TIMEOUT_SECONDS
    assert kwargs["env"]["ALLOW_LIVE_MODEL_CALLS"] == "false"
    junit_path = Path(next(item.split("=", 1)[1] for item in command if item.startswith("--junitxml=")))
    _junit(junit_path)
    return SimpleNamespace(returncode=0)


def _identity(*, dirty: bool = False, digest: str = "a") -> dict:
    return {"head": "1" * 40, "dirty": dirty, "source_files": {"legal_rag/synthetic.py": digest * 64}, "source_sha256": digest * 64}


def test_environment_does_not_inherit_credentials_database_or_exporters(monkeypatch):
    for name in (
        "SILICONFLOW_API_KEY", "OPENAI_API_KEY", "LEGAL_RAG_DATABASE_URL",
        "LEGAL_RAG_OBSERVATION_JSONL_PATH", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL",
        "HTTPS_PROXY", "PYTHONPATH", "PYTEST_ADDOPTS",
    ):
        monkeypatch.setenv(name, "private-value")
    monkeypatch.setenv("LEGAL_RAG_LANGFUSE_ENABLED", "1")
    monkeypatch.setenv("LEGAL_RAG_LANGFUSE_EXPORT_ACK", "1")
    monkeypatch.setenv("PATH", "synthetic-path")
    environment = showcase._subprocess_environment()
    assert "private-value" not in environment.values()
    assert environment["PATH"] == "synthetic-path"
    assert environment["ALLOW_LIVE_MODEL_CALLS"] == "false"
    assert environment["LEGAL_RAG_DISABLE_DOTENV"] == "1"
    assert environment["LEGAL_RAG_LANGFUSE_ENABLED"] == "0"
    assert environment["LEGAL_RAG_LANGFUSE_EXPORT_ACK"] == "0"
    assert environment["HF_HUB_OFFLINE"] == environment["UV_OFFLINE"] == "1"
    assert environment["PYTHONUTF8"] == "1"
    assert environment["PYTHONIOENCODING"] == "utf-8"


def test_list_is_fixed_and_does_not_create_output_or_launch(monkeypatch, capsys):
    monkeypatch.setattr(showcase, "run_showcase", lambda _: pytest.fail("must not launch"))
    assert showcase.main(["--list"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert [(row["group_id"], row["selector"]) for row in report["groups"]] == list(showcase.GROUPS)
    assert len(report["groups"]) == 6


def test_nonempty_output_is_rejected_without_overwrite(synthetic_root):
    directory = synthetic_root / ".tmp" / "existing"
    directory.mkdir(parents=True)
    evidence = directory / "previous.json"
    evidence.write_text("preserved", encoding="utf-8")
    with pytest.raises(showcase.ShowcaseError, match="output_directory_not_empty"):
        showcase._prepare_output_dir(directory)
    assert evidence.read_text(encoding="utf-8") == "preserved"


def test_output_must_be_a_private_workspace_temporary_subdirectory(synthetic_root):
    with pytest.raises(showcase.ShowcaseError, match="workspace_temporary"):
        showcase._prepare_output_dir(synthetic_root / "publishable")
    first = showcase._prepare_output_dir(None)
    second = showcase._prepare_output_dir(None)
    assert first != second
    assert first.parent == second.parent == synthetic_root / ".tmp"


@pytest.mark.parametrize("junit_kind", ["missing", "empty", "corrupt", "no_cases", "wrong_counts"])
def test_zero_exit_without_valid_nonempty_junit_is_not_passed(tmp_path, monkeypatch, junit_kind):
    def runner(command, **kwargs):
        path = Path(next(item.split("=", 1)[1] for item in command if item.startswith("--junitxml=")))
        if junit_kind == "empty":
            path.write_bytes(b"")
        elif junit_kind == "corrupt":
            path.write_text("not-xml", encoding="utf-8")
        elif junit_kind == "no_cases":
            _junit(path, tests=0)
        elif junit_kind == "wrong_counts":
            _junit(path, tests=2)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(showcase.subprocess, "run", runner)
    record = showcase._run_group("synthetic", "tests/synthetic.py", tmp_path)
    assert record["status"] == "failed"
    assert record["exit_code"] == 0
    assert record["junit_summary"] is None
    assert record["error_codes"]


@pytest.mark.parametrize("shape", ["duplicate", "multiple_outcomes", "repeated_outcome"])
def test_ambiguous_junit_case_accounting_is_rejected(tmp_path, shape):
    path = tmp_path / "ambiguous.xml"
    if shape == "duplicate":
        body = '<testcase classname="same" name="same"/>' * 2
        counts = 'tests="2" failures="0" errors="0" skipped="0"'
    else:
        outcomes = "<failure/><skipped/>" if shape == "multiple_outcomes" else "<failure/><failure/>"
        body = f'<testcase classname="same" name="same">{outcomes}</testcase>'
        counts = f'tests="1" failures="1" errors="0" skipped="{int(shape == "multiple_outcomes")}"'
    path.write_text(f'<testsuites><testsuite {counts}>{body}</testsuite></testsuites>', encoding="utf-8")
    with pytest.raises(showcase.ShowcaseError, match="junit_invalid"):
        showcase._junit_summary(path)


def test_skips_and_expected_failures_do_not_count_as_passed(tmp_path, monkeypatch):
    def runner(command, **kwargs):
        path = Path(next(item.split("=", 1)[1] for item in command if item.startswith("--junitxml=")))
        _junit(path, skipped=True)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(showcase.subprocess, "run", runner)
    record = showcase._run_group("synthetic", "tests/synthetic.py", tmp_path)
    assert record["status"] == "failed"
    assert record["junit_summary"]["skipped"] == 1


@pytest.mark.parametrize("outcome", ["failure", "error"])
def test_junit_nonpassing_cases_are_not_hidden_by_zero_exit(tmp_path, monkeypatch, outcome):
    def runner(command, **kwargs):
        path = Path(next(item.split("=", 1)[1] for item in command if item.startswith("--junitxml=")))
        path.write_text(
            '<testsuites><testsuite tests="1" '
            f'failures="{int(outcome == "failure")}" errors="{int(outcome == "error")}" skipped="0">'
            f'<testcase name="synthetic"><{outcome}/></testcase></testsuite></testsuites>',
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(showcase.subprocess, "run", runner)
    record = showcase._run_group("synthetic", "tests/synthetic.py", tmp_path)
    assert record["status"] == "failed"
    assert record["junit_summary"]["failures" if outcome == "failure" else "errors"] == 1


def test_nonzero_exit_is_not_hidden_by_passing_junit(tmp_path, monkeypatch):
    def runner(command, **kwargs):
        _passing_runner(command, **kwargs)
        return SimpleNamespace(returncode=2)

    monkeypatch.setattr(showcase.subprocess, "run", runner)
    record = showcase._run_group("synthetic", "tests/synthetic.py", tmp_path)
    assert record["status"] == "failed"
    assert record["exit_code"] == 2
    assert "pytest_nonzero_exit" in record["error_codes"]


def test_timeout_is_bounded_nonpassing_and_contains_no_raw_error(tmp_path, monkeypatch):
    def runner(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 180, output="private-timeout")

    monkeypatch.setattr(showcase.subprocess, "run", runner)
    record = showcase._run_group("synthetic", "tests/synthetic.py", tmp_path)
    assert record["status"] == "failed"
    assert record["exit_code"] == 124
    assert "pytest_timeout" in record["error_codes"]
    assert "private-timeout" not in json.dumps(record)


@pytest.mark.parametrize("drift", [False, True])
def test_source_drift_cannot_pass_and_dirty_run_is_never_a_clean_commit(synthetic_root, monkeypatch, drift):
    identities = iter([_identity(dirty=True), _identity(dirty=True, digest="b" if drift else "a")])
    monkeypatch.setattr(showcase, "_source_identity", lambda: next(identities))
    monkeypatch.setattr(showcase.subprocess, "run", _passing_runner)
    manifest, path = showcase.run_showcase()
    assert manifest["source_stable"] is not drift
    assert manifest["status"] == ("failed" if drift else "passed")
    assert manifest["source_context"] == "dirty_worktree"
    assert manifest["live_model_calls_authorized"] is False
    assert manifest["services_started_by_entrypoint"] is False
    assert manifest["os_egress_sandbox"] is False
    assert json.loads(path.read_text(encoding="utf-8")) == manifest
    assert len(manifest["groups"]) == 6
    for group in manifest["groups"]:
        assert group["started_at"] <= group["ended_at"]
        assert group["command"][0] == showcase.sys.executable
        assert "--strict-markers" in group["command"]
        assert group["junit_summary"]["selected_cases"] == [{"classname": "synthetic", "name": "test_contract"}]


def test_head_drift_fails_even_when_selected_source_hashes_match(synthetic_root, monkeypatch):
    after = _identity()
    after["head"] = "2" * 40
    identities = iter([_identity(), after])
    monkeypatch.setattr(showcase, "_source_identity", lambda: next(identities))
    monkeypatch.setattr(showcase.subprocess, "run", _passing_runner)
    manifest, _ = showcase.run_showcase()
    assert manifest["source_stable"] is False
    assert manifest["status"] == "failed"


def test_source_snapshot_hashes_only_fixed_source_inputs(synthetic_root, monkeypatch):
    sources = [selector for _, selector in showcase.GROUPS]
    sources += ["scripts/engineering_showcase.py", "tests/test_engineering_showcase.py", "pyproject.toml", "uv.lock", "legal_rag/example.py"]
    tracked_inputs = ["tests/conftest.py", "tests/test_exact_service_route.py", "tests/fixtures/synthetic.json", "configs/default.yaml", "eval_cases/registry.json"]
    sources += tracked_inputs
    for relative in sources:
        path = synthetic_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic", encoding="utf-8")
    (synthetic_root / ".env").write_text("must-not-read", encoding="utf-8")
    (synthetic_root / "private-corpus.jsonl").write_text("must-not-read", encoding="utf-8")
    def git_runner(command, **kwargs):
        if command[1] == "ls-files":
            return SimpleNamespace(stdout="\0".join(tracked_inputs) + "\0")
        return SimpleNamespace(stdout="1" * 40 if command[1] == "rev-parse" else " M docs/README.md\n")

    monkeypatch.setattr(showcase.subprocess, "run", git_runner)
    identity = showcase._source_identity()
    assert identity["dirty"] is True
    assert set(identity["source_files"]) == set(sources)
    assert len(identity["source_sha256"]) == 64
    (synthetic_root / "tests/fixtures/synthetic.json").write_text("changed fixture", encoding="utf-8")
    assert showcase._source_identity()["source_sha256"] != identity["source_sha256"]
    (synthetic_root / "legal_rag/example.py").write_text("changed", encoding="utf-8")
    assert showcase._source_identity()["source_sha256"] != identity["source_sha256"]


def test_cli_failure_summary_does_not_print_exception_details(monkeypatch, capsys):
    def fail(_):
        raise OSError("private exception details")
    monkeypatch.setattr(showcase, "run_showcase", fail)
    assert showcase.main([]) == 1
    assert json.loads(capsys.readouterr().out) == {"status": "failed", "error_code": "local_evidence_write_failed"}
