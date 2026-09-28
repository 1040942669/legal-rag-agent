from __future__ import annotations

import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "m4_wheel_probe.py"
_SPEC = importlib.util.spec_from_file_location("m4_wheel_probe", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
m4_wheel_probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(m4_wheel_probe)


def _metadata(
    *,
    version: str = "0.5.0",
    include_service_extra: bool = True,
    service_requirements: dict[str, str] | None = None,
) -> str:
    requirements = service_requirements or {
        dependency: f'{dependency}>=1; extra == "service"'
        for dependency in m4_wheel_probe.SERVICE_EXTRA_DEPENDENCIES
    }
    lines = [
        "Metadata-Version: 2.4",
        "Name: legal-rag-assistant",
        f"Version: {version}",
    ]
    if include_service_extra:
        lines.append("Provides-Extra: service")
    lines.extend(f"Requires-Dist: {value}" for value in requirements.values())
    return "\n".join(lines) + "\n\n"


def _write_wheel(
    path: Path,
    *,
    version: str = "0.5.0",
    metadata: str | None = None,
    entry_points: str | None = None,
    omit: set[str] | None = None,
    extra_entries: dict[str, str] | None = None,
) -> Path:
    dist_info = f"legal_rag_assistant-{version}.dist-info"
    entries: dict[str, str] = {
        name: "# packaged runtime\n"
        for name in m4_wheel_probe.REQUIRED_RUNTIME_FILES
    }
    entries.update(
        {
            "legal_rag/__init__.py": "\n",
            "legal_rag/cli.py": "def main():\n    return 0\n",
            f"{dist_info}/METADATA": metadata or _metadata(version=version),
            f"{dist_info}/entry_points.txt": entry_points
            or (
                "[console_scripts]\n"
                "legal-rag = legal_rag.cli:main\n"
                "legal-rag-api = legal_rag.api.command:main\n"
            ),
            f"{dist_info}/WHEEL": (
                "Wheel-Version: 1.0\n"
                "Generator: test\n"
                "Root-Is-Purelib: true\n"
                "Tag: py3-none-any\n"
            ),
            f"{dist_info}/RECORD": "",
        }
    )
    entries.update(extra_entries or {})
    for name in omit or set():
        entries.pop(name, None)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return path


def test_valid_wheel_emits_sanitized_static_receipt(tmp_path: Path) -> None:
    wheel = _write_wheel(tmp_path / "candidate-secret-name.whl")

    receipt = m4_wheel_probe.inspect_wheel(wheel)

    raw = json.dumps(receipt, sort_keys=True)
    assert receipt["status"] == "passed"
    assert receipt["distribution"] == {
        "name": "legal-rag-assistant",
        "version": "0.5.0",
    }
    assert receipt["wheel"]["sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert receipt["wheel"]["size_bytes"] == wheel.stat().st_size
    with zipfile.ZipFile(wheel) as archive:
        assert receipt["wheel"]["entry_count"] == len(archive.infolist())
    assert receipt["smoke"] == {"requested": False, "status": "not_requested"}
    assert "candidate-secret-name" not in raw
    assert str(tmp_path) not in raw
    assert "environment" not in raw


def test_default_expected_version_is_0_5_0(tmp_path: Path, capsys: Any) -> None:
    wheel = _write_wheel(tmp_path / "candidate.whl")

    assert m4_wheel_probe.main([str(wheel)]) == 0

    receipt = json.loads(capsys.readouterr().out)
    assert receipt["distribution"]["version"] == "0.5.0"


def test_distribution_version_mismatch_fails_closed(tmp_path: Path) -> None:
    wheel = _write_wheel(tmp_path / "candidate.whl", version="0.4.0")

    with pytest.raises(m4_wheel_probe.ProbeFailure) as raised:
        m4_wheel_probe.inspect_wheel(wheel)

    assert raised.value.code == "distribution_version_mismatch"


@pytest.mark.parametrize(
    "entry_points",
    [
        "[console_scripts]\nlegal-rag = legal_rag.cli:main\n",
        (
            "[console_scripts]\n"
            "legal-rag = legal_rag.cli:main\n"
            "legal-rag-api = legal_rag.api.command:not_main\n"
        ),
    ],
)
def test_both_exact_console_scripts_are_required(
    tmp_path: Path,
    entry_points: str,
) -> None:
    wheel = _write_wheel(
        tmp_path / "candidate.whl",
        entry_points=entry_points,
    )

    with pytest.raises(m4_wheel_probe.ProbeFailure) as raised:
        m4_wheel_probe.inspect_wheel(wheel)

    assert raised.value.code == "console_script_contract_mismatch"


@pytest.mark.parametrize(
    "missing",
    [
        "legal_rag/api/app.py",
        "legal_rag/services/run_service.py",
        "legal_rag/storage/alembic/versions/0005_m4_api_sessions.py",
    ],
)
def test_required_m4_runtime_files_cannot_be_omitted(
    tmp_path: Path,
    missing: str,
) -> None:
    wheel = _write_wheel(tmp_path / "candidate.whl", omit={missing})

    with pytest.raises(m4_wheel_probe.ProbeFailure) as raised:
        m4_wheel_probe.inspect_wheel(wheel)

    assert raised.value.code == "required_runtime_file_missing"


def test_service_dependency_cannot_be_unconditional(tmp_path: Path) -> None:
    requirements = {
        dependency: f'{dependency}>=1; extra == "service"'
        for dependency in m4_wheel_probe.SERVICE_EXTRA_DEPENDENCIES
    }
    requirements["fastapi"] = "fastapi>=1"
    wheel = _write_wheel(
        tmp_path / "candidate.whl",
        metadata=_metadata(service_requirements=requirements),
    )

    with pytest.raises(m4_wheel_probe.ProbeFailure) as raised:
        m4_wheel_probe.inspect_wheel(wheel)

    assert raised.value.code == "unconditional_service_dependency"


def test_every_service_dependency_must_be_bound_to_service_extra(
    tmp_path: Path,
) -> None:
    requirements = {
        dependency: f'{dependency}>=1; extra == "service"'
        for dependency in m4_wheel_probe.SERVICE_EXTRA_DEPENDENCIES
        if dependency != "uvicorn"
    }
    requirements["uvicorn"] = 'uvicorn>=1; extra == "database"'
    wheel = _write_wheel(
        tmp_path / "candidate.whl",
        metadata=_metadata(service_requirements=requirements),
    )

    with pytest.raises(m4_wheel_probe.ProbeFailure) as raised:
        m4_wheel_probe.inspect_wheel(wheel)

    assert raised.value.code == "service_extra_dependency_missing"


@pytest.mark.parametrize(
    "forbidden_name",
    [
        "integration_tests/test_live_database.py",
        "private/release_receipt.json",
        "docs/m4-release-receipt.md",
    ],
)
def test_integration_and_private_receipt_artifacts_are_rejected(
    tmp_path: Path,
    forbidden_name: str,
) -> None:
    wheel = _write_wheel(
        tmp_path / "candidate.whl",
        extra_entries={forbidden_name: "private evidence"},
    )

    with pytest.raises(m4_wheel_probe.ProbeFailure) as raised:
        m4_wheel_probe.inspect_wheel(wheel)

    assert raised.value.code == "forbidden_artifact_present"


def test_failure_receipt_does_not_disclose_path_or_exception(
    tmp_path: Path,
    capsys: Any,
) -> None:
    secret_path = tmp_path / "token-DO-NOT-PRINT.whl"
    secret_path.write_bytes(b"not a zip")

    assert m4_wheel_probe.main([str(secret_path)]) == 1

    captured = capsys.readouterr()
    receipt = json.loads(captured.err)
    assert captured.out == ""
    assert receipt["status"] == "failed"
    assert receipt["error"]["code"] == "invalid_wheel_archive"
    assert "DO-NOT-PRINT" not in captured.err
    assert str(tmp_path) not in captured.err


def test_probe_adds_only_sanitized_smoke_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel = _write_wheel(tmp_path / "candidate.whl")
    calls: list[dict[str, Any]] = []

    def fake_smoke(
        wheel_path: Path,
        *,
        expected_version: str,
        timeout_seconds: int,
        temp_root: Path | None,
    ) -> dict[str, Any]:
        calls.append(
            {
                "wheel_path": wheel_path,
                "expected_version": expected_version,
                "timeout_seconds": timeout_seconds,
                "temp_root": temp_root,
            }
        )
        return {
            "requested": True,
            "status": "passed",
            "base_cli_help": "passed",
            "service_cli_help": "passed",
            "installation": "offline_no_deps",
            "source_checkout_isolated": True,
            "service_configuration_present": False,
            "temporary_venv": True,
        }

    monkeypatch.setattr(m4_wheel_probe, "_smoke_in_temporary_venv", fake_smoke)

    receipt = m4_wheel_probe.probe_wheel(
        wheel,
        smoke=True,
        timeout_seconds=33,
        temp_root=tmp_path,
    )

    assert calls == [
        {
            "wheel_path": wheel.resolve(),
            "expected_version": "0.5.0",
            "timeout_seconds": 33,
            "temp_root": tmp_path,
        }
    ]
    assert receipt["smoke"]["status"] == "passed"
    assert str(wheel) not in json.dumps(receipt)


def test_child_environment_drops_service_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LEGAL_RAG_DATABASE_URL", "postgresql://private")
    monkeypatch.setenv("LEGAL_RAG_API_TOKENS_JSON", "private-token")
    monkeypatch.setenv("PYTHONPATH", "private-source-path")

    environment = m4_wheel_probe._sanitized_child_environment(tmp_path)

    assert not any(name.startswith("LEGAL_RAG_") for name in environment)
    assert "PYTHONPATH" not in environment
    assert "PYTHONHOME" not in environment
    assert environment["PIP_NO_INDEX"] == "1"
    assert environment["PYTHONNOUSERSITE"] == "1"


def test_run_quiet_fails_closed_without_echoing_child_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Completed:
        returncode = 2
        stdout = "credential=private"
        stderr = "database-url=private"

    monkeypatch.setattr(m4_wheel_probe.subprocess, "run", lambda *a, **k: Completed())

    with pytest.raises(m4_wheel_probe.ProbeFailure) as raised:
        m4_wheel_probe._run_quiet(
            ("candidate", "--help"),
            cwd=tmp_path,
            environment={},
            timeout_seconds=10,
            error_code="smoke_failed",
            safe_message="The smoke test failed.",
            require_help=True,
        )

    assert raised.value.code == "smoke_failed"
    assert "private" not in str(raised.value)
