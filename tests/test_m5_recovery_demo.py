from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "m5_recovery_demo.py"
_SPEC = importlib.util.spec_from_file_location("m5_recovery_demo", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
m5_recovery_demo = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(m5_recovery_demo)


def test_recovery_demo_runs_exact_cross_process_selector_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-cross-demo-boundary")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-cross-demo-boundary")
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(m5_recovery_demo.subprocess, "run", fake_run)
    receipt = tmp_path / "receipt.json"
    result = m5_recovery_demo.run_demo(
        database_url="postgresql+psycopg://example.invalid/legal_rag_m3_test",
        candidate_sha="a" * 40,
        receipt_path=receipt,
    )

    assert result == 0
    assert observed["command"] == [
        m5_recovery_demo.sys.executable,
        "-m",
        "pytest",
        "-q",
        "-ra",
        m5_recovery_demo.RECOVERY_SELECTOR,
    ]
    kwargs = observed["kwargs"]
    environment = kwargs["env"]
    assert kwargs["cwd"] == m5_recovery_demo.REPOSITORY_ROOT
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.PIPE
    assert kwargs["timeout"] == 120
    assert environment["LEGAL_RAG_INTEGRATION_TEST"] == "1"
    assert environment["ALLOW_LIVE_MODEL_CALLS"] == "false"
    assert environment["LEGAL_RAG_M5_CANDIDATE_SHA"] == "a" * 40
    assert environment["LEGAL_RAG_M5_FAULT_RECEIPT"] == str(receipt.resolve())
    assert environment["HF_HUB_OFFLINE"] == "1"
    assert environment["TRANSFORMERS_OFFLINE"] == "1"
    assert environment["NO_PROXY"] == "*"
    assert environment["UV_OFFLINE"] == "1"
    assert "OPENAI_API_KEY" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment


@pytest.mark.parametrize(
    "value",
    ["", "sqlite:///tmp/example.db", "https://example.invalid/database"],
)
def test_recovery_demo_rejects_missing_or_non_postgresql_url(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.delenv("LEGAL_RAG_DATABASE_URL", raising=False)
    with pytest.raises(ValueError):
        m5_recovery_demo._database_url(value)


def test_recovery_demo_inherits_only_process_runtime_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("M5_SYNTHETIC_SECRET", "not-inherited")
    environment = m5_recovery_demo._subprocess_environment(
        database_url="postgresql+psycopg://example.invalid/legal_rag_m3_test",
        candidate_sha="b" * 40,
        receipt_path=None,
    )

    assert "M5_SYNTHETIC_SECRET" not in environment
    assert "LEGAL_RAG_M5_FAULT_RECEIPT" not in environment
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert environment["LEGAL_RAG_DISABLE_DOTENV"] == "1"
    assert os.environ["M5_SYNTHETIC_SECRET"] == "not-inherited"


def test_recovery_demo_converts_subprocess_timeout_to_stable_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(*args, **kwargs):
        del args, kwargs
        raise subprocess.TimeoutExpired(["pytest"], timeout=10)

    monkeypatch.setattr(m5_recovery_demo.subprocess, "run", timeout)

    assert (
        m5_recovery_demo.run_demo(
            database_url="postgresql+psycopg://example.invalid/legal_rag_m3_test",
            candidate_sha="c" * 40,
            timeout_seconds=10,
        )
        == 124
    )


@pytest.mark.parametrize("timeout_seconds", [9, 601, True, 10.5])
def test_recovery_demo_rejects_unbounded_timeout(timeout_seconds: object) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        m5_recovery_demo.run_demo(
            database_url="postgresql+psycopg://example.invalid/legal_rag_m3_test",
            candidate_sha="d" * 40,
            timeout_seconds=timeout_seconds,  # type: ignore[arg-type]
        )
