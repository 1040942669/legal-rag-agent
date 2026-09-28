#!/usr/bin/env python3
"""Verify release wheels for M4 or M5 without importing the source checkout."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import tempfile
import zipfile
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Final, Sequence

_M4_PROBE_PATH = Path(__file__).with_name("m4_wheel_probe.py")
_M4_SPEC = importlib.util.spec_from_file_location(
    "legal_rag_m4_wheel_probe_compat",
    _M4_PROBE_PATH,
)
if _M4_SPEC is None or _M4_SPEC.loader is None:  # pragma: no cover - import guard
    raise RuntimeError("the compatible M4 wheel probe is unavailable")
_m4 = importlib.util.module_from_spec(_M4_SPEC)
_M4_SPEC.loader.exec_module(_m4)


DEFAULT_VERSIONS: Final = {"M4": "0.5.0", "M5": "0.6.0"}
M5_REQUIRED_RUNTIME_FILES: Final = frozenset(
    {
        "legal_rag/harness/__init__.py",
        "legal_rag/harness/budget.py",
        "legal_rag/harness/checkpoint.py",
        "legal_rag/harness/graph.py",
        "legal_rag/harness/nodes.py",
        "legal_rag/harness/runner.py",
        "legal_rag/harness/state.py",
        "legal_rag/harness/tools.py",
        "legal_rag/storage/alembic/versions/0006_m5_harness_recovery.py",
    }
)
M5_SERVICE_EXTRA_DEPENDENCIES: Final = frozenset(
    {
        "langgraph",
        "langgraph-checkpoint-postgres",
    }
)
M5_PROFILE_SERVICE_EXTRA_DEPENDENCIES: Final = frozenset(
    {*_m4.SERVICE_EXTRA_DEPENDENCIES, *M5_SERVICE_EXTRA_DEPENDENCIES}
)
M5_SMOKE_MODULES: Final = (
    "legal_rag.harness",
    "legal_rag.harness.budget",
    "legal_rag.harness.checkpoint",
    "legal_rag.harness.graph",
    "legal_rag.harness.nodes",
    "legal_rag.harness.runner",
    "legal_rag.harness.state",
    "legal_rag.harness.tools",
)
_EXTRA_ONLY_MARKER = re.compile(
    r'(?:extra\s*==\s*["\'](?P<right>[A-Za-z0-9][A-Za-z0-9._-]*)["\']|'
    r'["\'](?P<left>[A-Za-z0-9][A-Za-z0-9._-]*)["\']\s*==\s*extra)',
    re.IGNORECASE,
)
ProbeFailure = _m4.ProbeFailure


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def _m5_metadata_and_names(wheel_path: Path) -> tuple[Any, set[str]]:
    try:
        with zipfile.ZipFile(wheel_path) as archive:
            names = set(archive.namelist())
            metadata_names = sorted(
                name for name in names if name.endswith(".dist-info/METADATA")
            )
            if len(metadata_names) != 1:
                raise ProbeFailure(
                    "invalid_dist_info",
                    "The wheel must contain exactly one distribution metadata file.",
                )
            metadata_raw = archive.read(metadata_names[0])
    except ProbeFailure:
        raise
    except (OSError, KeyError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ProbeFailure(
            "invalid_wheel_archive",
            "The candidate is not a readable wheel archive.",
        ) from exc
    try:
        metadata = BytesParser(policy=policy.default).parsebytes(metadata_raw)
    except (TypeError, ValueError) as exc:
        raise ProbeFailure(
            "invalid_distribution_metadata",
            "The wheel distribution metadata is invalid.",
        ) from exc
    return metadata, names


def _require_m5_runtime_files(names: set[str]) -> None:
    if not M5_REQUIRED_RUNTIME_FILES.issubset(names):
        raise ProbeFailure(
            "m5_required_runtime_file_missing",
            "The wheel is missing one or more required M5 runtime files.",
        )


def _require_m5_service_dependencies(metadata: Any) -> None:
    try:
        from packaging.requirements import InvalidRequirement, Requirement
    except ImportError as exc:  # pragma: no cover - locked CI always provides it
        raise ProbeFailure(
            "requirement_parser_unavailable",
            "The M5 dependency marker parser is unavailable.",
        ) from exc

    provided_extras = {
        _canonical_name(value)
        for value in metadata.get_all("Provides-Extra", failobj=[])
    }
    if "service" not in provided_extras:
        raise ProbeFailure(
            "service_extra_missing",
            "The wheel metadata does not declare the optional service extra.",
        )

    service_bound: set[str] = set()
    for raw_requirement in metadata.get_all("Requires-Dist", failobj=[]):
        try:
            requirement = Requirement(raw_requirement)
        except InvalidRequirement as exc:
            raise ProbeFailure(
                "invalid_requirement_metadata",
                "The wheel contains an invalid dependency declaration.",
            ) from exc
        dependency = _canonical_name(requirement.name)
        if dependency not in M5_PROFILE_SERVICE_EXTRA_DEPENDENCIES:
            continue
        marker = requirement.marker
        if marker is None:
            raise ProbeFailure(
                "unconditional_m5_service_dependency",
                "An M5 service dependency is present in the base dependency set.",
            )
        try:
            base_enabled = marker.evaluate({"extra": ""})
            service_enabled = marker.evaluate({"extra": "service"})
        except (KeyError, TypeError, ValueError) as exc:
            raise ProbeFailure(
                "invalid_requirement_metadata",
                "The wheel contains an invalid dependency declaration.",
            ) from exc
        if base_enabled:
            raise ProbeFailure(
                "unconditional_m5_service_dependency",
                "An M5 service dependency is present in the base dependency set.",
            )
        extra_match = _EXTRA_ONLY_MARKER.fullmatch(str(marker))
        if extra_match is None:
            raise ProbeFailure(
                "noncanonical_m5_service_dependency_marker",
                "An M5 service dependency must be bound only to a declared extra.",
            )
        marker_extra = _canonical_name(
            extra_match.group("right") or extra_match.group("left")
        )
        if marker_extra not in provided_extras:
            raise ProbeFailure(
                "undeclared_m5_dependency_extra",
                "An M5 dependency is bound to an undeclared optional extra.",
            )
        if marker_extra == "service" and service_enabled:
            service_bound.add(dependency)

    if service_bound != M5_PROFILE_SERVICE_EXTRA_DEPENDENCIES:
        raise ProbeFailure(
            "m5_service_extra_dependency_missing",
            "The service extra is missing an M5 runtime dependency.",
        )


def _smoke_m5_modules_with_locked_runtime(
    wheel_path: Path,
    *,
    timeout_seconds: int,
    temp_root: Path | None,
) -> dict[str, Any]:
    """Import wheel-owned M5 modules using only the already locked runtime deps."""

    if not 10 <= timeout_seconds <= 600:
        raise ProbeFailure(
            "invalid_smoke_timeout",
            "The smoke-test timeout must be between 10 and 600 seconds.",
        )
    source_root = Path(__file__).resolve().parents[1]
    if temp_root is not None:
        try:
            resolved_temp_root = temp_root.expanduser().resolve(strict=True)
        except OSError as exc:
            raise ProbeFailure(
                "invalid_temp_root",
                "The requested temporary root is unavailable.",
            ) from exc
        if not resolved_temp_root.is_dir():
            raise ProbeFailure(
                "invalid_temp_root",
                "The requested temporary root is unavailable.",
            )
    else:
        resolved_temp_root = None

    try:
        temporary = tempfile.TemporaryDirectory(
            prefix="m5-wheel-runtime-probe-",
            dir=resolved_temp_root,
        )
    except OSError as exc:
        raise ProbeFailure(
            "temp_runtime_creation_failed",
            "The temporary M5 runtime environment could not be created.",
        ) from exc

    with temporary:
        workspace = Path(temporary.name).resolve()
        if _m4._is_within(workspace, source_root):
            raise ProbeFailure(
                "temp_runtime_inside_source",
                "The M5 runtime smoke environment must be outside the source checkout.",
            )
        target = workspace / "wheel-site"
        target.mkdir()
        try:
            with zipfile.ZipFile(wheel_path) as archive:
                _m4._validate_archive(archive.infolist())
                archive.extractall(target)
        except ProbeFailure:
            raise
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            raise ProbeFailure(
                "offline_wheel_extract_failed",
                "The candidate wheel could not be extracted for the M5 smoke test.",
            ) from exc

        imports = ";".join(
            f"importlib.import_module({module_name!r})"
            for module_name in M5_SMOKE_MODULES
        )
        provenance_code = (
            "import importlib,pathlib,sys;"
            f"r=pathlib.Path({str(target)!r}).resolve();"
            "sys.path.insert(0,str(r));"
            "import legal_rag;"
            "p=pathlib.Path(legal_rag.__file__).resolve();"
            "p.relative_to(r);"
            f"{imports}"
        )
        environment = _m4._sanitized_child_environment(Path(sys.executable).parent)
        environment.update(
            {
                "ALLOW_LIVE_MODEL_CALLS": "false",
                "HF_HUB_OFFLINE": "1",
                "LEGAL_RAG_DISABLE_DOTENV": "1",
                "NO_PROXY": "*",
                "TRANSFORMERS_OFFLINE": "1",
                "no_proxy": "*",
            }
        )
        _m4._run_quiet(
            (sys.executable, "-I", "-c", provenance_code),
            cwd=workspace,
            environment=environment,
            timeout_seconds=timeout_seconds,
            error_code="m5_runtime_module_smoke_failed",
            safe_message=(
                "An M5 runtime module could not be imported from the candidate wheel."
            ),
        )

    return {
        "status": "passed",
        "candidate_package_source": "extracted_wheel",
        "dependency_source": "locked_probe_runtime",
        "module_import_count": len(M5_SMOKE_MODULES),
        "source_checkout_isolated": True,
    }


def probe_release_wheel(
    wheel_path: str | os.PathLike[str],
    *,
    profile: str,
    expected_version: str | None = None,
    smoke: bool = False,
    timeout_seconds: int = 120,
    temp_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Probe an M4 or M5 release wheel and return a sanitized receipt."""

    normalized_profile = profile.strip().upper()
    if normalized_profile not in DEFAULT_VERSIONS:
        raise ProbeFailure(
            "unsupported_release_profile",
            "The requested release-wheel profile is unsupported.",
        )
    version = expected_version or DEFAULT_VERSIONS[normalized_profile]
    path = Path(wheel_path).expanduser().resolve()

    receipt = _m4.probe_wheel(
        path,
        expected_version=version,
        smoke=False,
        timeout_seconds=timeout_seconds,
        temp_root=temp_root,
    )
    receipt["release_profile"] = normalized_profile

    if normalized_profile == "M5":
        metadata, names = _m5_metadata_and_names(path)
        _require_m5_runtime_files(names)
        _require_m5_service_dependencies(metadata)
        receipt["checks"].update(
            {
                "m5_runtime_files": "passed",
                "m5_service_dependencies_optional": "passed",
            }
        )
        receipt["m5_required_runtime_file_count"] = len(M5_REQUIRED_RUNTIME_FILES)
        receipt["m5_service_extra_dependency_count"] = len(
            M5_PROFILE_SERVICE_EXTRA_DEPENDENCIES
        )

    if smoke:
        receipt["smoke"] = _m4._smoke_in_temporary_venv(
            path,
            expected_version=version,
            timeout_seconds=timeout_seconds,
            temp_root=Path(temp_root) if temp_root is not None else None,
        )
        if normalized_profile == "M5":
            receipt["smoke"]["m5_runtime_modules"] = (
                _smoke_m5_modules_with_locked_runtime(
                    path,
                    timeout_seconds=timeout_seconds,
                    temp_root=Path(temp_root) if temp_root is not None else None,
                )
            )
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify a Legal RAG release wheel and emit a sanitized JSON receipt."
        )
    )
    parser.add_argument("wheel", type=Path, help="Candidate .whl file to verify.")
    parser.add_argument(
        "--profile",
        choices=sorted(DEFAULT_VERSIONS),
        default="M5",
        help="Release contract to enforce (default: M5).",
    )
    parser.add_argument(
        "--expected-version",
        default=None,
        help="Required distribution version; defaults from --profile.",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Install outside the checkout and run CLI and runtime import smoke tests.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=120,
        help="Per-command smoke-test timeout, from 10 through 600 seconds.",
    )
    parser.add_argument(
        "--temp-root",
        type=Path,
        default=None,
        help="Optional existing directory for the temporary venv (never receipted).",
    )
    return parser


def _failure_receipt(exc: ProbeFailure) -> dict[str, Any]:
    return {
        "schema_version": _m4.RECEIPT_SCHEMA_VERSION,
        "status": "failed",
        "error": {
            "code": exc.code,
            "message": exc.safe_message,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        receipt = probe_release_wheel(
            args.wheel,
            profile=args.profile,
            expected_version=args.expected_version,
            smoke=args.smoke,
            timeout_seconds=args.timeout_seconds,
            temp_root=args.temp_root,
        )
    except ProbeFailure as exc:
        print(json.dumps(_failure_receipt(exc), sort_keys=True), file=sys.stderr)
        return 1
    except Exception:
        failure = ProbeFailure(
            "internal_probe_error",
            "The release-wheel probe failed without exposing internal details.",
        )
        print(json.dumps(_failure_receipt(failure), sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
