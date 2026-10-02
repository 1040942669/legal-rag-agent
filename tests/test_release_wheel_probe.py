from __future__ import annotations

import importlib.util
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "release_wheel_probe.py"
)
_SPEC = importlib.util.spec_from_file_location("release_wheel_probe", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
release_probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(release_probe)


def _metadata(
    *,
    version: str,
    profile: str,
    omit_dependency: str | None = None,
    unconditional_dependency: str | None = None,
    dependency_marker: str | None = None,
    marker_overrides: dict[str, str] | None = None,
    duplicate_extra_dependency: str | None = None,
    celery_redis_extra: bool = True,
) -> str:
    dependencies = set(release_probe._m4.SERVICE_EXTRA_DEPENDENCIES)
    if profile in {"M5", "M6"}:
        dependencies.update(release_probe.M5_SERVICE_EXTRA_DEPENDENCIES)
    lines = [
        "Metadata-Version: 2.4",
        "Name: legal-rag-assistant",
        f"Version: {version}",
        "Provides-Extra: service",
    ]
    if profile == "M6":
        lines.append("Provides-Extra: jobs")
    if duplicate_extra_dependency is not None:
        lines.extend(
            [
                "Provides-Extra: database",
                (
                    f"Requires-Dist: {duplicate_extra_dependency}>=1; "
                    'extra == "database"'
                ),
            ]
        )
    for dependency in sorted(dependencies):
        if dependency == omit_dependency:
            continue
        requirement = f"{dependency}>=1"
        if dependency != unconditional_dependency:
            marker = (marker_overrides or {}).get(
                dependency,
                dependency_marker or 'extra == "service"',
            )
            requirement += f"; {marker}"
        lines.append(f"Requires-Dist: {requirement}")
    if profile == "M6":
        for dependency in sorted(release_probe.M6_JOBS_EXTRA_DEPENDENCIES):
            if dependency == omit_dependency:
                continue
            name = (
                "celery[redis]"
                if dependency == "celery" and celery_redis_extra
                else dependency
            )
            requirement = f"{name}>=1"
            if dependency != unconditional_dependency:
                requirement += '; extra == "jobs"'
            lines.append(f"Requires-Dist: {requirement}")
    return "\n".join(lines) + "\n\n"


def _write_wheel(
    path: Path,
    *,
    version: str,
    profile: str,
    omit_file: str | None = None,
    omit_dependency: str | None = None,
    unconditional_dependency: str | None = None,
    dependency_marker: str | None = None,
    marker_overrides: dict[str, str] | None = None,
    duplicate_extra_dependency: str | None = None,
    celery_redis_extra: bool = True,
    jobs_entry_point: str = "legal_rag.jobs.command:main",
) -> Path:
    required = set(release_probe._m4.REQUIRED_RUNTIME_FILES)
    if profile in {"M5", "M6"}:
        required.update(release_probe.M5_REQUIRED_RUNTIME_FILES)
    if profile == "M6":
        required.update(release_probe.M6_REQUIRED_RUNTIME_FILES)
    if omit_file is not None:
        required.discard(omit_file)
    dist_info = f"legal_rag_assistant-{version}.dist-info"
    entries = {name: "# packaged runtime\n" for name in required}
    entries.update(
        {
            "legal_rag/__init__.py": "\n",
            "legal_rag/cli.py": "def main():\n    return 0\n",
            f"{dist_info}/METADATA": _metadata(
                version=version,
                profile=profile,
                omit_dependency=omit_dependency,
                unconditional_dependency=unconditional_dependency,
                dependency_marker=dependency_marker,
                marker_overrides=marker_overrides,
                duplicate_extra_dependency=duplicate_extra_dependency,
                celery_redis_extra=celery_redis_extra,
            ),
            f"{dist_info}/entry_points.txt": (
                "[console_scripts]\n"
                "legal-rag = legal_rag.cli:main\n"
                "legal-rag-api = legal_rag.api.command:main\n"
                + (
                    f"legal-rag-jobs = {jobs_entry_point}\n"
                    if profile == "M6" else ""
                )
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
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return path


def test_m5_profile_verifies_runtime_and_optional_dependencies(tmp_path: Path) -> None:
    wheel = _write_wheel(
        tmp_path / "legal_rag_assistant-0.6.0-py3-none-any.whl",
        version="0.6.0",
        profile="M5",
    )

    receipt = release_probe.probe_release_wheel(wheel, profile="M5")

    assert receipt["status"] == "passed"
    assert receipt["release_profile"] == "M5"
    assert receipt["distribution"]["version"] == "0.6.0"
    assert receipt["checks"]["m5_runtime_files"] == "passed"
    assert receipt["checks"]["m5_service_dependencies_optional"] == "passed"
    assert receipt["m5_required_runtime_file_count"] == len(
        release_probe.M5_REQUIRED_RUNTIME_FILES
    )
    assert receipt["m5_service_extra_dependency_count"] == len(
        release_probe.M5_PROFILE_SERVICE_EXTRA_DEPENDENCIES
    )


def test_m6_profile_verifies_jobs_runtime_entry_point_and_optional_extra(
    tmp_path: Path,
) -> None:
    wheel = _write_wheel(
        tmp_path / "legal_rag_assistant-0.7.1-py3-none-any.whl",
        version="0.7.1",
        profile="M6",
    )
    receipt = release_probe.probe_release_wheel(wheel, profile="M6")
    assert receipt["status"] == "passed"
    assert receipt["release_profile"] == "M6"
    assert receipt["distribution"]["version"] == "0.7.1"
    assert receipt["checks"]["m5_runtime_files"] == "passed"
    assert receipt["checks"]["m6_runtime_files"] == "passed"
    assert receipt["checks"]["m6_jobs_entry_point"] == "passed"
    assert receipt["checks"]["m6_jobs_dependencies_optional"] == "passed"
    assert "legal-rag-jobs" in receipt["console_scripts"]
    assert receipt["m6_required_runtime_file_count"] == len(
        release_probe.M6_REQUIRED_RUNTIME_FILES
    )


@pytest.mark.parametrize(
    ("omit_file", "jobs_entry_point", "omit_dependency", "celery_redis_extra", "expected_code"),
    [
        (
            "legal_rag/jobs/worker.py", "legal_rag.jobs.command:main", None, True,
            "m6_required_runtime_file_missing",
        ),
        (
            "legal_rag/storage/alembic/versions/0007_m6_jobs_outbox.py",
            "legal_rag.jobs.command:main", None, True,
            "m6_required_runtime_file_missing",
        ),
        (
            None, "legal_rag.jobs.command:wrong", None, True,
            "m6_jobs_console_script_missing",
        ),
        (
            None, "legal_rag.jobs.command:main", "celery", True,
            "m6_jobs_extra_dependency_missing",
        ),
        (
            None, "legal_rag.jobs.command:main", None, False,
            "m6_celery_redis_extra_missing",
        ),
    ],
)
def test_m6_profile_rejects_missing_worker_contracts(
    tmp_path: Path,
    omit_file: str | None,
    jobs_entry_point: str,
    omit_dependency: str | None,
    celery_redis_extra: bool,
    expected_code: str,
) -> None:
    wheel = _write_wheel(
        tmp_path / "legal_rag_assistant-0.7.1-py3-none-any.whl",
        version="0.7.1",
        profile="M6",
        omit_file=omit_file,
        jobs_entry_point=jobs_entry_point,
        omit_dependency=omit_dependency,
        celery_redis_extra=celery_redis_extra,
    )
    with pytest.raises(release_probe.ProbeFailure) as raised:
        release_probe.probe_release_wheel(wheel, profile="M6")
    assert raised.value.code == expected_code


def test_m6_jobs_dependency_cannot_be_unconditional(tmp_path: Path) -> None:
    wheel = _write_wheel(
        tmp_path / "legal_rag_assistant-0.7.1-py3-none-any.whl",
        version="0.7.1",
        profile="M6",
        unconditional_dependency="celery",
    )
    with pytest.raises(release_probe.ProbeFailure) as raised:
        release_probe.probe_release_wheel(wheel, profile="M6")
    assert raised.value.code == "unconditional_m6_jobs_dependency"


def test_m4_profile_preserves_the_existing_0_5_0_contract(tmp_path: Path) -> None:
    wheel = _write_wheel(
        tmp_path / "legal_rag_assistant-0.5.0-py3-none-any.whl",
        version="0.5.0",
        profile="M4",
    )

    receipt = release_probe.probe_release_wheel(wheel, profile="M4")

    assert receipt["status"] == "passed"
    assert receipt["release_profile"] == "M4"
    assert receipt["distribution"]["version"] == "0.5.0"
    assert "m5_runtime_files" not in receipt["checks"]


def test_m5_profile_rejects_a_missing_harness_runtime_file(tmp_path: Path) -> None:
    wheel = _write_wheel(
        tmp_path / "legal_rag_assistant-0.6.0-py3-none-any.whl",
        version="0.6.0",
        profile="M5",
        omit_file="legal_rag/harness/graph.py",
    )

    with pytest.raises(release_probe.ProbeFailure) as raised:
        release_probe.probe_release_wheel(wheel, profile="M5")

    assert raised.value.code == "m5_required_runtime_file_missing"


@pytest.mark.parametrize(
    ("omit_dependency", "unconditional_dependency", "expected_code"),
    [
        (
            "langgraph-checkpoint-postgres",
            None,
            "m5_service_extra_dependency_missing",
        ),
        (None, "langgraph", "unconditional_m5_service_dependency"),
    ],
)
def test_m5_dependencies_must_be_bound_to_the_service_extra(
    tmp_path: Path,
    omit_dependency: str | None,
    unconditional_dependency: str | None,
    expected_code: str,
) -> None:
    wheel = _write_wheel(
        tmp_path / "legal_rag_assistant-0.6.0-py3-none-any.whl",
        version="0.6.0",
        profile="M5",
        omit_dependency=omit_dependency,
        unconditional_dependency=unconditional_dependency,
    )

    with pytest.raises(release_probe.ProbeFailure) as raised:
        release_probe.probe_release_wheel(wheel, profile="M5")

    assert raised.value.code == expected_code


def test_m5_profile_rejects_effectively_unconditional_or_markers(
    tmp_path: Path,
) -> None:
    wheel = _write_wheel(
        tmp_path / "legal_rag_assistant-0.6.0-py3-none-any.whl",
        version="0.6.0",
        profile="M5",
        dependency_marker='python_version >= "3.0" or extra == "service"',
    )

    with pytest.raises(release_probe.ProbeFailure) as raised:
        release_probe.probe_release_wheel(wheel, profile="M5")

    assert raised.value.code == "unconditional_m5_service_dependency"


def test_m5_profile_rejects_markers_unconditional_on_another_supported_python(
    tmp_path: Path,
) -> None:
    wheel = _write_wheel(
        tmp_path / "legal_rag_assistant-0.6.0-py3-none-any.whl",
        version="0.6.0",
        profile="M5",
        dependency_marker='python_version == "3.11" or extra == "service"',
    )

    with pytest.raises(release_probe.ProbeFailure) as raised:
        release_probe.probe_release_wheel(wheel, profile="M5")

    assert raised.value.code == "noncanonical_m5_service_dependency_marker"


def test_m5_profile_hardens_inherited_m4_service_dependency_markers(
    tmp_path: Path,
) -> None:
    wheel = _write_wheel(
        tmp_path / "legal_rag_assistant-0.6.0-py3-none-any.whl",
        version="0.6.0",
        profile="M5",
        marker_overrides={"fastapi": 'python_version == "3.11" or extra == "service"'},
    )

    with pytest.raises(release_probe.ProbeFailure) as raised:
        release_probe.probe_release_wheel(wheel, profile="M5")

    assert raised.value.code == "noncanonical_m5_service_dependency_marker"


def test_m5_profile_allows_a_dependency_in_database_and_service_extras(
    tmp_path: Path,
) -> None:
    wheel = _write_wheel(
        tmp_path / "legal_rag_assistant-0.6.0-py3-none-any.whl",
        version="0.6.0",
        profile="M5",
        duplicate_extra_dependency="sqlalchemy",
    )

    receipt = release_probe.probe_release_wheel(wheel, profile="M5")

    assert receipt["checks"]["m5_service_dependencies_optional"] == "passed"


def test_m5_smoke_requests_installed_harness_imports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel = _write_wheel(
        tmp_path / "legal_rag_assistant-0.6.0-py3-none-any.whl",
        version="0.6.0",
        profile="M5",
    )
    calls: list[dict[str, Any]] = []

    def fake_smoke(
        wheel_path: Path,
        *,
        expected_version: str,
        timeout_seconds: int,
        temp_root: Path | None,
    ) -> dict[str, object]:
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
        }

    runtime_calls: list[dict[str, Any]] = []

    def fake_runtime_smoke(
        wheel_path: Path,
        *,
        timeout_seconds: int,
        temp_root: Path | None,
    ) -> dict[str, object]:
        runtime_calls.append(
            {
                "wheel_path": wheel_path,
                "timeout_seconds": timeout_seconds,
                "temp_root": temp_root,
            }
        )
        return {
            "status": "passed",
            "module_import_count": len(release_probe.M5_SMOKE_MODULES),
        }

    monkeypatch.setattr(release_probe._m4, "_smoke_in_temporary_venv", fake_smoke)
    monkeypatch.setattr(
        release_probe,
        "_smoke_m5_modules_with_locked_runtime",
        fake_runtime_smoke,
    )

    receipt = release_probe.probe_release_wheel(
        wheel,
        profile="M5",
        smoke=True,
        timeout_seconds=33,
        temp_root=tmp_path,
    )

    assert calls[0]["wheel_path"] == wheel.resolve()
    assert calls[0]["expected_version"] == "0.6.0"
    assert runtime_calls == [
        {
            "wheel_path": wheel.resolve(),
            "timeout_seconds": 33,
            "temp_root": tmp_path,
        }
    ]
    assert receipt["smoke"]["m5_runtime_modules"]["module_import_count"] == len(
        release_probe.M5_SMOKE_MODULES
    )


def test_m6_smoke_requests_installed_api_jobs_and_migration_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel = _write_wheel(
        tmp_path / "legal_rag_assistant-0.7.1-py3-none-any.whl",
        version="0.7.1",
        profile="M6",
    )
    calls: list[str] = []

    def base_smoke(*args: Any, **kwargs: Any) -> dict[str, object]:
        calls.append("api")
        return {"requested": True, "status": "passed", "service_cli_help": "passed"}

    def m5_smoke(*args: Any, **kwargs: Any) -> dict[str, object]:
        calls.append("m5")
        return {"status": "passed"}

    def m6_smoke(*args: Any, **kwargs: Any) -> dict[str, object]:
        assert kwargs["expected_version"] == "0.7.1"
        calls.append("m6")
        return {
            "status": "passed",
            "jobs_cli_help": "passed",
            "migration_head": "0007_m6_jobs_outbox",
        }

    monkeypatch.setattr(release_probe._m4, "_smoke_in_temporary_venv", base_smoke)
    monkeypatch.setattr(release_probe, "_smoke_m5_modules_with_locked_runtime", m5_smoke)
    monkeypatch.setattr(release_probe, "_smoke_m6_installed_wheel", m6_smoke)
    receipt = release_probe.probe_release_wheel(wheel, profile="M6", smoke=True)
    assert calls == ["api", "m5", "m6"]
    assert receipt["smoke"]["m6_installed_runtime"] == {
        "status": "passed",
        "jobs_cli_help": "passed",
        "migration_head": "0007_m6_jobs_outbox",
    }


def test_m5_runtime_smoke_imports_from_extracted_wheel_not_checkout(
    tmp_path: Path,
) -> None:
    wheel = _write_wheel(
        tmp_path / "legal_rag_assistant-0.6.0-py3-none-any.whl",
        version="0.6.0",
        profile="M5",
    )

    receipt = release_probe._smoke_m5_modules_with_locked_runtime(
        wheel.resolve(),
        timeout_seconds=30,
        temp_root=tmp_path,
    )

    assert receipt == {
        "status": "passed",
        "candidate_package_source": "extracted_wheel",
        "dependency_source": "locked_probe_runtime",
        "module_import_count": len(release_probe.M5_SMOKE_MODULES),
        "source_checkout_isolated": True,
    }


def test_cli_failure_receipt_does_not_disclose_candidate_path(
    tmp_path: Path,
    capsys: Any,
) -> None:
    candidate = tmp_path / "private-name.whl"
    candidate.write_bytes(b"not a wheel")

    assert release_probe.main([str(candidate), "--profile", "M5"]) == 1

    captured = capsys.readouterr()
    receipt = json.loads(captured.err)
    assert receipt["status"] == "failed"
    assert "private-name" not in captured.err
    assert str(tmp_path) not in captured.err
