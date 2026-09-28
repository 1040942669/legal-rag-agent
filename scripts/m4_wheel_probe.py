from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import venv
import zipfile
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Final, Sequence


EXPECTED_DISTRIBUTION: Final = "legal-rag-assistant"
DEFAULT_EXPECTED_VERSION: Final = "0.5.0"
RECEIPT_SCHEMA_VERSION: Final = 1
MAX_WHEEL_ENTRIES: Final = 10_000
MAX_UNCOMPRESSED_BYTES: Final = 512 * 1024 * 1024
MAX_METADATA_BYTES: Final = 2 * 1024 * 1024

REQUIRED_CONSOLE_SCRIPTS: Final = {
    "legal-rag": "legal_rag.cli:main",
    "legal-rag-api": "legal_rag.api.command:main",
}
REQUIRED_RUNTIME_FILES: Final = frozenset(
    {
        "legal_rag/api/__init__.py",
        "legal_rag/api/app.py",
        "legal_rag/api/auth.py",
        "legal_rag/api/command.py",
        "legal_rag/api/schemas.py",
        "legal_rag/api/settings.py",
        "legal_rag/services/__init__.py",
        "legal_rag/services/run_executor.py",
        "legal_rag/services/run_service.py",
        "legal_rag/services/service_retrieval.py",
        "legal_rag/services/supervisor.py",
        "legal_rag/storage/database.py",
        "legal_rag/storage/migrations.py",
        "legal_rag/storage/schema.py",
        "legal_rag/storage/alembic/versions/0005_m4_api_sessions.py",
    }
)
SERVICE_EXTRA_DEPENDENCIES: Final = frozenset(
    {
        "alembic",
        "fastapi",
        "pgvector",
        "psycopg",
        "pydantic",
        "sqlalchemy",
        "uvicorn",
    }
)

_NORMALIZE_NAME = re.compile(r"[-_.]+")
_SAFE_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)+(?:[A-Za-z0-9.+!_-]*)\Z")
_REQUIREMENT_NAME = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_SERVICE_EXTRA_MARKER = re.compile(
    r"(?:\bextra\s*==\s*['\"]service['\"]|['\"]service['\"]\s*==\s*\bextra\b)",
    re.IGNORECASE,
)


class ProbeFailure(RuntimeError):
    """A fail-closed probe error whose text is safe for a public receipt."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message


def _canonical_name(value: str) -> str:
    return _NORMALIZE_NAME.sub("-", value).lower()


def _validated_expected_version(value: str) -> str:
    if not _SAFE_VERSION.fullmatch(value):
        raise ProbeFailure(
            "invalid_expected_version",
            "The expected distribution version is not a supported version string.",
        )
    return value


def _safe_archive_name(name: str) -> bool:
    if not name or "\\" in name or "\x00" in name or name.startswith("/"):
        return False
    trimmed = name[:-1] if name.endswith("/") else name
    parts = trimmed.split("/")
    if not trimmed or any(part in {"", ".", ".."} for part in parts):
        return False
    return not (len(parts[0]) >= 2 and parts[0][1] == ":")


def _is_forbidden_artifact(name: str) -> bool:
    parts = tuple(part.casefold() for part in name.rstrip("/").split("/"))
    if "integration_tests" in parts:
        return True
    if any(part in {"private", "receipts", "release_receipts"} for part in parts):
        return True
    filename = parts[-1]
    return "receipt" in filename and filename.endswith(
        (".json", ".jsonl", ".md", ".txt", ".yaml", ".yml")
    )


def _validate_archive(infos: Sequence[zipfile.ZipInfo]) -> set[str]:
    if not infos or len(infos) > MAX_WHEEL_ENTRIES:
        raise ProbeFailure(
            "invalid_wheel_inventory",
            "The wheel entry inventory is empty or exceeds the safety limit.",
        )

    names: set[str] = set()
    folded_names: set[str] = set()
    total_size = 0
    for info in infos:
        name = info.filename
        if not _safe_archive_name(name):
            raise ProbeFailure(
                "unsafe_wheel_entry",
                "The wheel contains an unsafe archive entry name.",
            )
        folded = name.casefold()
        if name in names or folded in folded_names:
            raise ProbeFailure(
                "duplicate_wheel_entry",
                "The wheel contains duplicate or case-colliding archive entries.",
            )
        names.add(name)
        folded_names.add(folded)
        mode = (info.external_attr >> 16) & 0xFFFF
        if mode and stat.S_ISLNK(mode):
            raise ProbeFailure(
                "unsafe_wheel_entry",
                "The wheel contains a symbolic-link archive entry.",
            )
        total_size += info.file_size
        if total_size > MAX_UNCOMPRESSED_BYTES:
            raise ProbeFailure(
                "wheel_expansion_limit_exceeded",
                "The wheel exceeds the uncompressed-size safety limit.",
            )
        if _is_forbidden_artifact(name):
            raise ProbeFailure(
                "forbidden_artifact_present",
                "The wheel contains an integration-test or private receipt artifact.",
            )
    return names


def _read_small_member(
    archive: zipfile.ZipFile,
    name: str,
    *,
    missing_code: str,
) -> bytes:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise ProbeFailure(
            missing_code,
            "The wheel is missing required distribution metadata.",
        ) from exc
    if info.file_size > MAX_METADATA_BYTES:
        raise ProbeFailure(
            "oversized_distribution_metadata",
            "The wheel distribution metadata exceeds the safety limit.",
        )
    return archive.read(info)


def _require_dist_info_root(names: set[str]) -> str:
    metadata_entries = sorted(
        name
        for name in names
        if name.count("/") == 1 and name.endswith(".dist-info/METADATA")
    )
    if len(metadata_entries) != 1:
        raise ProbeFailure(
            "invalid_dist_info",
            "The wheel must contain exactly one top-level dist-info METADATA file.",
        )
    return metadata_entries[0].rsplit("/", 1)[0]


def _require_runtime_files(names: set[str]) -> None:
    if not REQUIRED_RUNTIME_FILES.issubset(names):
        raise ProbeFailure(
            "required_runtime_file_missing",
            "The wheel is missing one or more required M4 runtime files.",
        )


def _parse_console_scripts(raw: bytes) -> dict[str, str]:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    try:
        parser.read_string(raw.decode("utf-8"))
    except (UnicodeDecodeError, configparser.Error) as exc:
        raise ProbeFailure(
            "invalid_entry_points",
            "The wheel console-script metadata is invalid.",
        ) from exc
    if not parser.has_section("console_scripts"):
        raise ProbeFailure(
            "missing_console_scripts",
            "The wheel has no console_scripts entry-point group.",
        )
    return {
        name.strip(): target.strip()
        for name, target in parser.items("console_scripts")
    }


def _require_console_scripts(scripts: dict[str, str]) -> None:
    for name, expected_target in REQUIRED_CONSOLE_SCRIPTS.items():
        if scripts.get(name) != expected_target:
            raise ProbeFailure(
                "console_script_contract_mismatch",
                "The wheel does not expose the required console-script contract.",
            )


def _require_service_extra(metadata: Any) -> None:
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
        match = _REQUIREMENT_NAME.match(raw_requirement)
        if match is None:
            raise ProbeFailure(
                "invalid_requirement_metadata",
                "The wheel contains an invalid dependency declaration.",
            )
        dependency = _canonical_name(match.group(1))
        if dependency not in SERVICE_EXTRA_DEPENDENCIES:
            continue
        _, separator, marker = raw_requirement.partition(";")
        if not separator or "extra" not in marker.casefold():
            raise ProbeFailure(
                "unconditional_service_dependency",
                "A service dependency is present in the unconditional dependency set.",
            )
        if _SERVICE_EXTRA_MARKER.search(marker):
            service_bound.add(dependency)

    if service_bound != SERVICE_EXTRA_DEPENDENCIES:
        raise ProbeFailure(
            "service_extra_dependency_missing",
            "The service extra is missing one or more required direct dependencies.",
        )


def _sha256_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise ProbeFailure(
            "wheel_read_failed",
            "The candidate wheel could not be read.",
        ) from exc
    return digest.hexdigest(), size


def inspect_wheel(
    wheel_path: str | os.PathLike[str],
    *,
    expected_version: str = DEFAULT_EXPECTED_VERSION,
) -> dict[str, Any]:
    """Inspect a candidate wheel without importing from the source checkout."""

    version = _validated_expected_version(expected_version)
    path = Path(wheel_path).expanduser().resolve()
    if path.suffix.casefold() != ".whl" or not path.is_file():
        raise ProbeFailure(
            "candidate_wheel_unavailable",
            "The candidate wheel is unavailable or does not have a .whl suffix.",
        )

    digest, size = _sha256_and_size(path)
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = _validate_archive(infos)
            dist_info_root = _require_dist_info_root(names)
            metadata_raw = _read_small_member(
                archive,
                f"{dist_info_root}/METADATA",
                missing_code="metadata_missing",
            )
            entry_points_raw = _read_small_member(
                archive,
                f"{dist_info_root}/entry_points.txt",
                missing_code="entry_points_missing",
            )
            if not {
                f"{dist_info_root}/WHEEL",
                f"{dist_info_root}/RECORD",
            }.issubset(names):
                raise ProbeFailure(
                    "invalid_dist_info",
                    "The wheel is missing required dist-info records.",
                )
            _require_runtime_files(names)
    except ProbeFailure:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
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
    distribution_name = metadata.get("Name")
    distribution_version = metadata.get("Version")
    if (
        not isinstance(distribution_name, str)
        or _canonical_name(distribution_name) != EXPECTED_DISTRIBUTION
    ):
        raise ProbeFailure(
            "distribution_name_mismatch",
            "The wheel distribution name does not match the release contract.",
        )
    if distribution_version != version:
        raise ProbeFailure(
            "distribution_version_mismatch",
            "The wheel distribution version does not match the expected release.",
        )

    scripts = _parse_console_scripts(entry_points_raw)
    _require_console_scripts(scripts)
    _require_service_extra(metadata)

    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "status": "passed",
        "distribution": {
            "name": EXPECTED_DISTRIBUTION,
            "version": version,
        },
        "wheel": {
            "sha256": digest,
            "size_bytes": size,
            "entry_count": len(infos),
        },
        "checks": {
            "archive_safety": "passed",
            "console_scripts": "passed",
            "forbidden_artifacts": "passed",
            "required_runtime_files": "passed",
            "service_dependencies_optional": "passed",
            "static_inspection": "passed",
        },
        "console_scripts": sorted(REQUIRED_CONSOLE_SCRIPTS),
        "required_runtime_file_count": len(REQUIRED_RUNTIME_FILES),
        "service_extra_dependency_count": len(SERVICE_EXTRA_DEPENDENCIES),
        "smoke": {
            "requested": False,
            "status": "not_requested",
        },
    }


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _sanitized_child_environment(scripts_dir: Path) -> dict[str, str]:
    environment: dict[str, str] = {}
    for name in (
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
    ):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    inherited_path = os.environ.get("PATH", "")
    environment.update(
        {
            "PATH": os.pathsep.join(
                item for item in (str(scripts_dir), inherited_path) if item
            ),
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INDEX": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
        }
    )
    return environment


def _run_quiet(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: int,
    error_code: str,
    safe_message: str,
    require_help: bool = False,
) -> None:
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProbeFailure(error_code, safe_message) from exc
    if completed.returncode != 0:
        raise ProbeFailure(error_code, safe_message)
    if require_help and "usage:" not in completed.stdout.casefold():
        raise ProbeFailure(error_code, safe_message)


def _smoke_in_temporary_venv(
    wheel_path: Path,
    *,
    expected_version: str,
    timeout_seconds: int,
    temp_root: Path | None,
) -> dict[str, Any]:
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
            prefix="m4-wheel-probe-",
            dir=resolved_temp_root,
        )
    except OSError as exc:
        raise ProbeFailure(
            "temp_venv_creation_failed",
            "The temporary smoke-test environment could not be created.",
        ) from exc

    with temporary:
        workspace = Path(temporary.name).resolve()
        if _is_within(workspace, source_root):
            raise ProbeFailure(
                "temp_venv_inside_source",
                "The smoke-test environment must be outside the source checkout.",
            )
        venv_path = workspace / "venv"
        try:
            venv.EnvBuilder(
                with_pip=True,
                clear=True,
                system_site_packages=True,
            ).create(venv_path)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProbeFailure(
                "temp_venv_creation_failed",
                "The temporary smoke-test environment could not be created.",
            ) from exc

        if os.name == "nt":
            scripts_dir = venv_path / "Scripts"
            python = scripts_dir / "python.exe"
            base_cli = scripts_dir / "legal-rag.exe"
            service_cli = scripts_dir / "legal-rag-api.exe"
        else:
            scripts_dir = venv_path / "bin"
            python = scripts_dir / "python"
            base_cli = scripts_dir / "legal-rag"
            service_cli = scripts_dir / "legal-rag-api"

        environment = _sanitized_child_environment(scripts_dir)
        _run_quiet(
            (
                str(python),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                "--force-reinstall",
                str(wheel_path),
            ),
            cwd=workspace,
            environment=environment,
            timeout_seconds=timeout_seconds,
            error_code="offline_wheel_install_failed",
            safe_message="The candidate wheel could not be installed offline.",
        )
        provenance_code = (
            "import importlib.metadata as m,pathlib,sys;"
            f"assert m.version({EXPECTED_DISTRIBUTION!r}) == {expected_version!r};"
            "import legal_rag;"
            "p=pathlib.Path(legal_rag.__file__).resolve();"
            "p.relative_to(pathlib.Path(sys.prefix).resolve())"
        )
        _run_quiet(
            (str(python), "-c", provenance_code),
            cwd=workspace,
            environment=environment,
            timeout_seconds=timeout_seconds,
            error_code="installed_wheel_provenance_failed",
            safe_message="The installed package did not resolve from the temporary venv.",
        )
        _run_quiet(
            (str(base_cli), "--help"),
            cwd=workspace,
            environment=environment,
            timeout_seconds=timeout_seconds,
            error_code="base_cli_smoke_failed",
            safe_message="The installed legal-rag --help smoke test failed.",
            require_help=True,
        )
        _run_quiet(
            (str(service_cli), "--help"),
            cwd=workspace,
            environment=environment,
            timeout_seconds=timeout_seconds,
            error_code="service_cli_smoke_failed",
            safe_message="The installed legal-rag-api --help smoke test failed.",
            require_help=True,
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


def probe_wheel(
    wheel_path: str | os.PathLike[str],
    *,
    expected_version: str = DEFAULT_EXPECTED_VERSION,
    smoke: bool = False,
    timeout_seconds: int = 120,
    temp_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    receipt = inspect_wheel(wheel_path, expected_version=expected_version)
    if smoke:
        receipt["smoke"] = _smoke_in_temporary_venv(
            Path(wheel_path).expanduser().resolve(),
            expected_version=expected_version,
            timeout_seconds=timeout_seconds,
            temp_root=Path(temp_root) if temp_root is not None else None,
        )
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify a Legal RAG M4 candidate wheel and emit a sanitized JSON receipt."
        )
    )
    parser.add_argument("wheel", type=Path, help="Candidate .whl file to verify.")
    parser.add_argument(
        "--expected-version",
        default=DEFAULT_EXPECTED_VERSION,
        help=f"Required distribution version (default: {DEFAULT_EXPECTED_VERSION}).",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help=(
            "Install the wheel offline into a temporary venv outside the checkout "
            "and run both CLI --help commands."
        ),
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
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "status": "failed",
        "error": {
            "code": exc.code,
            "message": exc.safe_message,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        receipt = probe_wheel(
            args.wheel,
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
            "The candidate wheel probe failed without exposing internal details.",
        )
        print(json.dumps(_failure_receipt(failure), sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
