from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .experiment_runtime import (
    CACHE_MODES,
    ExperimentContractError,
    canonical_hash,
    canonical_json_bytes,
    validate_experiment_manifest,
)
from .json_utils import (
    reject_duplicate_object_pairs,
    reject_non_finite_json_constant,
    validate_json_unicode,
)


ARTIFACT_SCHEMA_VERSION = 1
ATTEMPT_STATUSES = frozenset({"succeeded", "failed", "interrupted"})
_SAFE_EXPERIMENT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ATTEMPT_FILE = re.compile(r"^attempt-([0-9]{4,6})\.json$")
_TEMP_FILE = re.compile(
    r"^\.(?:attempt-[0-9]{4,6}\.json|complete\.json)\.[A-Za-z0-9_-]+\.tmp$"
)
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CLOCK$",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class ArtifactCorruptionError(ValueError):
    """Raised when an artifact cannot prove its schema, identity, or checksum."""

    def __init__(self, code: str, path: Path, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


class ArtifactConflictError(ValueError):
    """Raised when an immutable artifact path already contains different data."""


class ResumeCompatibilityError(ValueError):
    """Raised before scanning case artifacts when a requested run is incompatible."""

    def __init__(
        self,
        *,
        existing_hash: str,
        requested_hash: str,
        differences: tuple[str, ...],
    ) -> None:
        super().__init__(
            "resume manifest is incompatible: "
            + ", ".join(differences or ("identity",))
        )
        self.existing_hash = existing_hash
        self.requested_hash = requested_hash
        self.differences = differences


@dataclass(frozen=True)
class InventoryProblem:
    code: str
    path: str
    case_id: str | None
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "path": self.path,
            "case_id": self.case_id,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ArtifactInventory:
    expected: int
    case_order: tuple[str, ...]
    succeeded: tuple[str, ...]
    failed: tuple[str, ...]
    missing: tuple[str, ...]
    corrupt: tuple[str, ...]
    not_run: tuple[str, ...]
    interrupted: tuple[str, ...]
    exhausted: tuple[str, ...]
    problems: tuple[InventoryProblem, ...]

    @property
    def pending_commit(self) -> tuple[str, ...]:
        """Cases with a valid succeeded attempt but no complete proof marker."""

        return self.missing

    @property
    def runnable(self) -> tuple[str, ...]:
        """Cases a runner may execute without repairing artifact corruption."""

        if self.global_problems:
            return ()
        runnable = set(self.failed) | set(self.not_run) | set(self.interrupted)
        return tuple(case_id for case_id in self.case_order if case_id in runnable)

    @property
    def blocked(self) -> tuple[str, ...]:
        blocked = set(self.corrupt) | set(self.exhausted)
        return tuple(case_id for case_id in self.case_order if case_id in blocked)

    @property
    def global_problems(self) -> tuple[InventoryProblem, ...]:
        return tuple(problem for problem in self.problems if problem.case_id is None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "expected": self.expected,
            "case_order": list(self.case_order),
            "counts": {
                "succeeded": len(self.succeeded),
                "failed": len(self.failed),
                "missing": len(self.missing),
                "corrupt": len(self.corrupt),
                "not_run": len(self.not_run),
                "interrupted": len(self.interrupted),
                "exhausted": len(self.exhausted),
                "pending_commit": len(self.pending_commit),
            },
            "case_ids": {
                "succeeded": list(self.succeeded),
                "failed": list(self.failed),
                "missing": list(self.missing),
                "corrupt": list(self.corrupt),
                "not_run": list(self.not_run),
                "interrupted": list(self.interrupted),
                "exhausted": list(self.exhausted),
                "pending_commit": list(self.pending_commit),
                "runnable": list(self.runnable),
                "blocked": list(self.blocked),
            },
            "global_problem_count": len(self.global_problems),
            "problems": [problem.to_dict() for problem in self.problems],
        }


def _json_copy(value: Any) -> Any:
    return json.loads(canonical_json_bytes(value).decode("utf-8"))


def _require_non_empty_string(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExperimentContractError(f"{name} must be a non-empty string")
    validate_json_unicode(value)
    return value


def _validate_experiment_id(experiment_id: Any) -> str:
    value = _require_non_empty_string("experiment_id", experiment_id)
    if not _SAFE_EXPERIMENT_ID.fullmatch(value):
        raise ExperimentContractError(
            "experiment_id must be a lowercase ASCII slug using letters, digits, '_' or '-'"
        )
    if value.upper() in _WINDOWS_RESERVED_NAMES:
        raise ExperimentContractError("experiment_id is reserved by Windows")
    return value


def _validate_attempt_number(attempt: Any) -> int:
    if (
        isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or not 1 <= attempt <= 999999
    ):
        raise ExperimentContractError("attempt must be an integer between 1 and 999999")
    return attempt


def _path_entry_exists(path: Path) -> bool:
    """Return whether a directory entry exists, including a dangling reparse point."""

    return os.path.lexists(path)


def _is_reparse_point(path: Path) -> bool:
    """Reject symlinks and Windows junctions without following their targets."""

    try:
        path_stat = path.lstat()
    except OSError:
        return False
    file_attributes = getattr(path_stat, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(file_attributes & reparse_flag)


def _read_strict_json(path: Path) -> Any:
    if _is_reparse_point(path):
        raise ArtifactCorruptionError(
            "unsafe_symlink", path, "artifact path must not be a symbolic link"
        )
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_object_pairs,
            parse_constant=reject_non_finite_json_constant,
        )
        validate_json_unicode(value)
        return value
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ArtifactCorruptionError(
            "invalid_json", path, f"artifact is not strict UTF-8 JSON: {path}"
        ) from exc


def _envelope(payload: Mapping[str, Any]) -> dict[str, Any]:
    copied = _json_copy(dict(payload))
    return {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "payload_sha256": canonical_hash(copied),
        "payload": copied,
    }


def _read_envelope(path: Path) -> dict[str, Any]:
    root = _read_strict_json(path)
    if not isinstance(root, dict):
        raise ArtifactCorruptionError(
            "invalid_envelope", path, f"artifact envelope must be an object: {path}"
        )
    expected_fields = {"artifact_schema_version", "payload_sha256", "payload"}
    if set(root) != expected_fields:
        raise ArtifactCorruptionError(
            "invalid_envelope", path, f"artifact envelope fields are invalid: {path}"
        )
    if (
        type(root["artifact_schema_version"]) is not int
        or root["artifact_schema_version"] != ARTIFACT_SCHEMA_VERSION
    ):
        raise ArtifactCorruptionError(
            "schema_mismatch", path, f"artifact schema version is unsupported: {path}"
        )
    if not isinstance(root["payload"], dict):
        raise ArtifactCorruptionError(
            "invalid_envelope", path, f"artifact payload must be an object: {path}"
        )
    if root["payload_sha256"] != canonical_hash(root["payload"]):
        raise ArtifactCorruptionError(
            "checksum_mismatch", path, f"artifact payload checksum is invalid: {path}"
        )
    return root


def _write_immutable_json(
    path: Path,
    value: Mapping[str, Any],
    *,
    load_existing: Callable[[Path], Mapping[str, Any]],
) -> Path:
    canonical = _json_copy(dict(value))

    def accept_existing() -> Path:
        existing = load_existing(path)
        if canonical_json_bytes(existing) != canonical_json_bytes(canonical):
            raise ArtifactConflictError(
                f"immutable artifact already contains different data: {path}"
            )
        return path

    if _path_entry_exists(path):
        return accept_existing()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ).encode("utf-8")
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp_name, path)
        except FileExistsError:
            return accept_existing()
        return accept_existing()
    finally:
        if temp_name and os.path.lexists(temp_name):
            try:
                os.unlink(temp_name)
            except OSError:
                # A complete, strictly re-read hard-link is already committed. A
                # transient Windows cleanup failure must not reverse that outcome.
                pass
    return path


def _compatibility_projection(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "execution_mode": manifest["execution_mode"],
        "code": manifest["code"],
        "config_hash": manifest["config"]["sha256"],
        "corpus": manifest["corpus"],
        "dataset": manifest["dataset"],
        "contracts": manifest["contracts"],
        "runtime": manifest["runtime"],
    }


def _changed_paths(existing: Any, requested: Any, *, path: str = "$") -> list[str]:
    if type(existing) is not type(requested):
        return [path]
    if isinstance(existing, dict):
        changes: list[str] = []
        for key in sorted(set(existing) | set(requested)):
            child_path = f"{path}.{key}"
            if key not in existing or key not in requested:
                changes.append(child_path)
            else:
                changes.extend(
                    _changed_paths(existing[key], requested[key], path=child_path)
                )
        return changes
    if isinstance(existing, list):
        if len(existing) != len(requested):
            return [f"{path}.length"]
        changes = []
        for index, (old_item, new_item) in enumerate(zip(existing, requested)):
            changes.extend(_changed_paths(old_item, new_item, path=f"{path}[{index}]"))
        return changes
    return [] if existing == requested else [path]


class ExperimentStore:
    """Immutable per-experiment manifests and per-case attempt artifacts."""

    def __init__(self, root: str | Path, experiment_id: str) -> None:
        self.root = Path(root)
        self.experiment_id = _validate_experiment_id(experiment_id)
        self.directory = self.root / self.experiment_id

    def _validate_experiment_directory(self) -> Path:
        if not _path_entry_exists(self.directory):
            raise ArtifactCorruptionError(
                "missing_experiment_directory",
                self.directory,
                f"experiment directory is missing: {self.directory}",
            )
        try:
            escapes_root = not self.directory.resolve().is_relative_to(
                self.root.resolve()
            )
        except OSError as exc:
            raise ArtifactCorruptionError(
                "unsafe_experiment_directory",
                self.directory,
                f"experiment directory cannot be resolved safely: {self.directory}",
            ) from exc
        if (
            _is_reparse_point(self.directory)
            or not self.directory.is_dir()
            or escapes_root
        ):
            raise ArtifactCorruptionError(
                "unsafe_experiment_directory",
                self.directory,
                f"experiment directory is unsafe or escapes artifact root: {self.directory}",
            )
        return self.directory

    @classmethod
    def create(
        cls,
        root: str | Path,
        manifest: Mapping[str, Any],
    ) -> ExperimentStore:
        validated = validate_experiment_manifest(manifest)
        store = cls(root, validated["experiment_id"])
        store.root.mkdir(parents=True, exist_ok=True)
        store.directory.mkdir(parents=True, exist_ok=True)
        store._validate_experiment_directory()
        store._case_catalog(validated)
        manifest_path = store.directory / "manifest.json"
        if _path_entry_exists(manifest_path):
            existing = store.load_manifest()
            store._assert_compatible(existing, validated)
            return store
        try:
            _write_immutable_json(
                manifest_path,
                validated,
                load_existing=store._load_manifest_path,
            )
        except ArtifactConflictError:
            existing = store.load_manifest()
            store._assert_compatible(existing, validated)
        return store

    @classmethod
    def open(cls, root: str | Path, experiment_id: str) -> ExperimentStore:
        store = cls(root, experiment_id)
        store._validate_experiment_directory()
        store.load_manifest()
        return store

    def _load_manifest_path(self, path: Path) -> dict[str, Any]:
        payload = _read_strict_json(path)
        if not isinstance(payload, dict):
            raise ArtifactCorruptionError(
                "manifest_contract",
                path,
                f"experiment manifest must be an object: {path}",
            )
        try:
            validated = validate_experiment_manifest(payload)
            if validated["experiment_id"] != self.experiment_id:
                raise ExperimentContractError(
                    "manifest experiment_id does not match path"
                )
            self._case_catalog(validated)
        except ExperimentContractError as exc:
            raise ArtifactCorruptionError(
                "manifest_contract", path, f"experiment manifest is invalid: {path}"
            ) from exc
        return validated

    def load_manifest(self) -> dict[str, Any]:
        self._validate_experiment_directory()
        path = self.directory / "manifest.json"
        if not _path_entry_exists(path):
            raise ArtifactCorruptionError(
                "missing_manifest", path, f"experiment manifest is missing: {path}"
            )
        if _is_reparse_point(path) or not path.is_file():
            raise ArtifactCorruptionError(
                "unsafe_manifest_path",
                path,
                f"experiment manifest path is unsafe: {path}",
            )
        return self._load_manifest_path(path)

    def _case_catalog(
        self,
        manifest: Mapping[str, Any],
    ) -> dict[str, dict[str, Any]]:
        dataset = manifest.get("dataset")
        if not isinstance(dataset, dict):
            raise ExperimentContractError("manifest.dataset must be an object")
        cases = dataset.get("cases")
        if not isinstance(cases, list):
            raise ExperimentContractError("manifest.dataset.cases must be a list")
        case_count = dataset.get("case_count")
        if (
            isinstance(case_count, bool)
            or not isinstance(case_count, int)
            or case_count < 0
        ):
            raise ExperimentContractError(
                "manifest.dataset.case_count must be a non-negative integer"
            )
        if case_count != len(cases):
            raise ExperimentContractError(
                "manifest.dataset.case_count does not match cases"
            )
        if dataset.get("case_set_hash") != canonical_hash(cases):
            raise ExperimentContractError("manifest.dataset.case_set_hash is invalid")

        result: dict[str, dict[str, Any]] = {}
        seen_hashes: set[str] = set()
        for expected_ordinal, raw_case in enumerate(cases):
            if not isinstance(raw_case, dict):
                raise ExperimentContractError("manifest dataset case must be an object")
            ordinal = raw_case.get("ordinal")
            if (
                isinstance(ordinal, bool)
                or not isinstance(ordinal, int)
                or ordinal != expected_ordinal
            ):
                raise ExperimentContractError(
                    "manifest dataset case ordinals must be contiguous from zero"
                )
            case_id = _require_non_empty_string("case_id", raw_case.get("case_id"))
            case_hash = raw_case.get("case_hash")
            if not isinstance(case_hash, str) or not _SHA256.fullmatch(case_hash):
                raise ExperimentContractError(
                    "case_hash must be a lowercase SHA-256 digest"
                )
            if case_id in result:
                raise ExperimentContractError(
                    f"duplicate case_id in manifest: {case_id}"
                )
            if case_hash in seen_hashes:
                raise ExperimentContractError(
                    f"duplicate case_hash in manifest: {case_hash}"
                )
            copied = _json_copy(raw_case)
            result[case_id] = copied
            seen_hashes.add(case_hash)
        return result

    def _case(self, case_id: str) -> dict[str, Any]:
        manifest = self.load_manifest()
        cases = self._case_catalog(manifest)
        try:
            return cases[case_id]
        except KeyError as exc:
            raise ExperimentContractError(
                f"case_id is not part of the experiment manifest: {case_id!r}"
            ) from exc

    def _case_directory(self, case: Mapping[str, Any]) -> Path:
        case_key = canonical_hash({"case_id": case["case_id"]})
        return self.directory / "cases" / (f"{case['ordinal'] + 1:06d}-{case_key}")

    def _validate_existing_case_directory(
        self,
        case: Mapping[str, Any],
    ) -> Path | None:
        case_directory = self._case_directory(case)
        if not _path_entry_exists(case_directory):
            return None
        if (
            _is_reparse_point(case_directory)
            or not case_directory.is_dir()
            or not case_directory.resolve().is_relative_to(self.directory.resolve())
        ):
            raise ArtifactCorruptionError(
                "unsafe_case_path",
                case_directory,
                "case artifact directory is unsafe or escapes experiment root",
            )
        return case_directory

    def _ensure_case_directory(self, case: Mapping[str, Any]) -> Path:
        cases_root = self.directory / "cases"
        if _path_entry_exists(cases_root) and (
            _is_reparse_point(cases_root)
            or not cases_root.is_dir()
            or not cases_root.resolve().is_relative_to(self.directory.resolve())
        ):
            raise ArtifactCorruptionError(
                "unsafe_cases_root",
                cases_root,
                "cases directory is unsafe or escapes experiment root",
            )
        cases_root.mkdir(parents=True, exist_ok=True)
        case_directory = self._case_directory(case)
        if _path_entry_exists(case_directory) and (
            _is_reparse_point(case_directory)
            or not case_directory.is_dir()
            or not case_directory.resolve().is_relative_to(self.directory.resolve())
        ):
            raise ArtifactCorruptionError(
                "unsafe_case_path",
                case_directory,
                "case directory is unsafe or escapes experiment root",
            )
        case_directory.mkdir(exist_ok=True)
        return case_directory

    def _attempt_path(self, case: Mapping[str, Any], attempt: int) -> Path:
        attempt = _validate_attempt_number(attempt)
        return self._case_directory(case) / f"attempt-{attempt:04d}.json"

    def _max_attempts(self, manifest: Mapping[str, Any] | None = None) -> int:
        selected_manifest = manifest if manifest is not None else self.load_manifest()
        return 1 + selected_manifest["runtime"]["max_retries"]

    def _attempt_payload(
        self,
        case: Mapping[str, Any],
        *,
        attempt: int,
        status: str,
        recorded_at: str,
        cache_mode: str,
        execution_environment: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        manifest = self.load_manifest()
        if status not in ATTEMPT_STATUSES:
            raise ExperimentContractError(f"unsupported attempt status: {status!r}")
        recorded_at = _require_non_empty_string("recorded_at", recorded_at)
        if cache_mode not in CACHE_MODES:
            raise ExperimentContractError(f"unsupported cache mode: {cache_mode!r}")
        if not isinstance(execution_environment, Mapping):
            raise ExperimentContractError("execution_environment must be an object")
        environment_payload = _json_copy(dict(execution_environment))
        if not environment_payload:
            raise ExperimentContractError("execution_environment must not be empty")
        if not isinstance(result, Mapping):
            raise ExperimentContractError("attempt result must be an object")
        return {
            "artifact_kind": "experiment_case_attempt",
            "experiment_id": self.experiment_id,
            "manifest_hash": manifest["identity"]["manifest_hash"],
            "resume_compatibility_hash": manifest["identity"][
                "resume_compatibility_hash"
            ],
            "case_id": case["case_id"],
            "case_hash": case["case_hash"],
            "ordinal": case["ordinal"],
            "attempt": attempt,
            "status": status,
            "recorded_at": recorded_at,
            "cache_mode": cache_mode,
            "execution_environment": environment_payload,
            "environment_fingerprint": canonical_hash(environment_payload),
            "result": _json_copy(dict(result)),
        }

    def _load_attempt_path(
        self,
        path: Path,
        case: Mapping[str, Any],
        attempt: int,
    ) -> dict[str, Any]:
        envelope = _read_envelope(path)
        payload = envelope["payload"]
        manifest = self.load_manifest()
        expected = {
            "artifact_kind": "experiment_case_attempt",
            "experiment_id": self.experiment_id,
            "manifest_hash": manifest["identity"]["manifest_hash"],
            "resume_compatibility_hash": manifest["identity"][
                "resume_compatibility_hash"
            ],
            "case_id": case["case_id"],
            "case_hash": case["case_hash"],
            "ordinal": case["ordinal"],
            "attempt": attempt,
        }
        for field_name, expected_value in expected.items():
            actual_value = payload.get(field_name)
            if (
                type(actual_value) is not type(expected_value)
                or actual_value != expected_value
            ):
                raise ArtifactCorruptionError(
                    "identity_mismatch",
                    path,
                    f"attempt identity mismatch for {field_name}: {path}",
                )
        if payload.get("status") not in ATTEMPT_STATUSES:
            raise ArtifactCorruptionError(
                "invalid_status", path, f"attempt status is invalid: {path}"
            )
        if (
            not isinstance(payload.get("recorded_at"), str)
            or not payload["recorded_at"]
        ):
            raise ArtifactCorruptionError(
                "invalid_recorded_at", path, f"attempt recorded_at is invalid: {path}"
            )
        if not isinstance(payload.get("result"), dict):
            raise ArtifactCorruptionError(
                "invalid_result", path, f"attempt result is invalid: {path}"
            )
        required = {
            "artifact_kind",
            "experiment_id",
            "manifest_hash",
            "resume_compatibility_hash",
            "case_id",
            "case_hash",
            "ordinal",
            "attempt",
            "status",
            "recorded_at",
            "cache_mode",
            "execution_environment",
            "environment_fingerprint",
            "result",
        }
        if set(payload) != required:
            raise ArtifactCorruptionError(
                "invalid_attempt_fields",
                path,
                f"attempt payload fields are invalid: {path}",
            )
        if payload["cache_mode"] not in CACHE_MODES:
            raise ArtifactCorruptionError(
                "invalid_cache_mode", path, f"attempt cache mode is invalid: {path}"
            )
        if (
            not isinstance(payload["execution_environment"], dict)
            or not payload["execution_environment"]
        ):
            raise ArtifactCorruptionError(
                "invalid_execution_environment",
                path,
                f"attempt execution environment is invalid: {path}",
            )
        if not isinstance(
            payload["environment_fingerprint"], str
        ) or not _SHA256.fullmatch(payload["environment_fingerprint"]):
            raise ArtifactCorruptionError(
                "invalid_environment_fingerprint",
                path,
                f"attempt environment fingerprint is invalid: {path}",
            )
        if payload["environment_fingerprint"] != canonical_hash(
            payload["execution_environment"]
        ):
            raise ArtifactCorruptionError(
                "environment_fingerprint_mismatch",
                path,
                f"attempt environment fingerprint does not match its summary: {path}",
            )
        return envelope

    def _validated_case_attempts(
        self,
        case: Mapping[str, Any],
    ) -> dict[int, dict[str, Any]]:
        case_directory = self._case_directory(case)
        if self._validate_existing_case_directory(case) is None:
            return {}
        attempts: dict[int, dict[str, Any]] = {}
        for path in sorted(case_directory.iterdir(), key=lambda item: item.name):
            if path.name == "complete.json":
                continue
            if _TEMP_FILE.fullmatch(path.name):
                continue
            match = _ATTEMPT_FILE.fullmatch(path.name) if path.is_file() else None
            if match is None:
                raise ArtifactCorruptionError(
                    "unexpected_case_artifact",
                    path,
                    f"unexpected artifact in case directory: {path}",
                )
            attempt = int(match.group(1))
            if not 1 <= attempt <= 999999 or path.name != f"attempt-{attempt:04d}.json":
                raise ArtifactCorruptionError(
                    "noncanonical_attempt_path",
                    path,
                    f"attempt filename is not canonical: {path}",
                )
            if attempt in attempts:
                raise ArtifactCorruptionError(
                    "duplicate_attempt",
                    path,
                    f"duplicate numeric attempt identity: {path}",
                )
            attempts[attempt] = self._load_attempt_path(path, case, attempt)
        if attempts:
            expected_attempts = list(range(1, max(attempts) + 1))
            if sorted(attempts) != expected_attempts:
                raise ArtifactCorruptionError(
                    "missing_attempt_history",
                    case_directory,
                    "case attempt history is not contiguous from attempt one",
                )
            if max(attempts) > self._max_attempts():
                raise ArtifactCorruptionError(
                    "retry_budget_exceeded",
                    case_directory,
                    "case attempt history exceeds the manifest retry budget",
                )
            succeeded_attempts = [
                attempt
                for attempt, envelope in attempts.items()
                if envelope["payload"]["status"] == "succeeded"
            ]
            if len(succeeded_attempts) > 1 or (
                succeeded_attempts and succeeded_attempts[0] != max(attempts)
            ):
                raise ArtifactCorruptionError(
                    "invalid_succeeded_history",
                    case_directory,
                    "case attempt history contains ambiguous succeeded attempts",
                )
        return attempts

    def write_attempt(
        self,
        case_id: str,
        *,
        attempt: int,
        status: str,
        recorded_at: str,
        cache_mode: str,
        execution_environment: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> Path:
        case = self._case(case_id)
        attempt = _validate_attempt_number(attempt)
        max_attempts = self._max_attempts()
        if attempt > max_attempts:
            raise ExperimentContractError(
                f"attempt exceeds manifest retry budget of {max_attempts}: {case_id}"
            )
        self._ensure_case_directory(case)
        complete_path = self._case_directory(case) / "complete.json"
        if _path_entry_exists(complete_path):
            self.load_completed(case_id)
            raise ArtifactConflictError(f"case is already complete: {case_id}")
        existing_attempts = self._validated_case_attempts(case)
        if attempt not in existing_attempts:
            expected_attempt = max(existing_attempts, default=0) + 1
            if attempt != expected_attempt:
                raise ExperimentContractError(
                    f"attempt must be the next contiguous number {expected_attempt}: {case_id}"
                )
            if any(
                envelope["payload"]["status"] == "succeeded"
                for envelope in existing_attempts.values()
            ):
                raise ArtifactConflictError(
                    f"case has a succeeded attempt awaiting completion: {case_id}"
                )
        path = self._attempt_path(case, attempt)
        envelope = _envelope(
            self._attempt_payload(
                case,
                attempt=attempt,
                status=status,
                recorded_at=recorded_at,
                cache_mode=cache_mode,
                execution_environment=execution_environment,
                result=result,
            )
        )
        return _write_immutable_json(
            path,
            envelope,
            load_existing=lambda existing_path: self._load_attempt_path(
                existing_path, case, attempt
            ),
        )

    def attempt_paths(self, case_id: str) -> tuple[Path, ...]:
        case = self._case(case_id)
        if self._validate_existing_case_directory(case) is None:
            return ()
        attempts = self._validated_case_attempts(case)
        return tuple(self._attempt_path(case, attempt) for attempt in sorted(attempts))

    def _load_complete_marker(
        self,
        path: Path,
        case: Mapping[str, Any],
    ) -> dict[str, Any]:
        envelope = _read_envelope(path)
        payload = envelope["payload"]
        manifest = self.load_manifest()
        required = {
            "artifact_kind",
            "experiment_id",
            "manifest_hash",
            "case_id",
            "case_hash",
            "ordinal",
            "attempt",
            "attempt_artifact_sha256",
        }
        if set(payload) != required:
            raise ArtifactCorruptionError(
                "invalid_complete_fields",
                path,
                f"complete marker fields are invalid: {path}",
            )
        expected = {
            "artifact_kind": "experiment_case_complete",
            "experiment_id": self.experiment_id,
            "manifest_hash": manifest["identity"]["manifest_hash"],
            "case_id": case["case_id"],
            "case_hash": case["case_hash"],
            "ordinal": case["ordinal"],
        }
        for field_name, expected_value in expected.items():
            actual_value = payload.get(field_name)
            if (
                type(actual_value) is not type(expected_value)
                or actual_value != expected_value
            ):
                raise ArtifactCorruptionError(
                    "identity_mismatch",
                    path,
                    f"complete marker identity mismatch for {field_name}: {path}",
                )
        attempt = payload.get("attempt")
        if (
            isinstance(attempt, bool)
            or not isinstance(attempt, int)
            or not 1 <= attempt <= 999999
        ):
            raise ArtifactCorruptionError(
                "invalid_attempt", path, f"complete marker attempt is invalid: {path}"
            )
        attempt_sha = payload.get("attempt_artifact_sha256")
        if not isinstance(attempt_sha, str) or not _SHA256.fullmatch(attempt_sha):
            raise ArtifactCorruptionError(
                "invalid_attempt_checksum",
                path,
                f"complete marker attempt checksum is invalid: {path}",
            )
        return envelope

    def mark_complete(self, case_id: str, *, attempt: int) -> Path:
        case = self._case(case_id)
        attempt = _validate_attempt_number(attempt)
        self._ensure_case_directory(case)
        complete_path = self._case_directory(case) / "complete.json"
        if _path_entry_exists(complete_path):
            existing = self._load_complete_marker(complete_path, case)
            if existing["payload"]["attempt"] == attempt:
                self.load_completed(case_id)
                return complete_path
            raise ArtifactConflictError(
                f"case already completed by attempt {existing['payload']['attempt']}: {case_id}"
            )

        existing_attempts = self._validated_case_attempts(case)
        if attempt not in existing_attempts:
            raise ExperimentContractError(
                f"cannot complete case without attempt {attempt}: {case_id}"
            )
        if attempt != max(existing_attempts):
            raise ExperimentContractError(
                f"only the latest attempt can complete a case: {case_id}"
            )
        attempt_envelope = existing_attempts[attempt]
        if attempt_envelope["payload"]["status"] != "succeeded":
            raise ExperimentContractError(
                f"only a succeeded attempt can complete a case: {case_id}"
            )
        marker = _envelope(
            {
                "artifact_kind": "experiment_case_complete",
                "experiment_id": self.experiment_id,
                "manifest_hash": self.load_manifest()["identity"]["manifest_hash"],
                "case_id": case["case_id"],
                "case_hash": case["case_hash"],
                "ordinal": case["ordinal"],
                "attempt": attempt,
                "attempt_artifact_sha256": canonical_hash(attempt_envelope),
            }
        )
        return _write_immutable_json(
            complete_path,
            marker,
            load_existing=lambda existing_path: self._load_complete_marker(
                existing_path, case
            ),
        )

    def load_completed(self, case_id: str) -> dict[str, Any]:
        case = self._case(case_id)
        self._validate_existing_case_directory(case)
        complete_path = self._case_directory(case) / "complete.json"
        if not _path_entry_exists(complete_path):
            raise ArtifactCorruptionError(
                "missing_complete",
                complete_path,
                f"complete marker is missing: {complete_path}",
            )
        if _is_reparse_point(complete_path) or not complete_path.is_file():
            raise ArtifactCorruptionError(
                "unsafe_complete_path",
                complete_path,
                f"complete marker path is unsafe: {complete_path}",
            )
        marker = self._load_complete_marker(complete_path, case)
        attempt = marker["payload"]["attempt"]
        existing_attempts = self._validated_case_attempts(case)
        attempt_path = self._attempt_path(case, attempt)
        if attempt not in existing_attempts:
            raise ArtifactCorruptionError(
                "missing_attempt",
                attempt_path,
                f"completed attempt file is missing: {attempt_path}",
            )
        attempt_envelope = existing_attempts[attempt]
        if marker["payload"]["attempt_artifact_sha256"] != canonical_hash(
            attempt_envelope
        ):
            raise ArtifactCorruptionError(
                "complete_attempt_mismatch",
                complete_path,
                f"complete marker does not match its attempt: {complete_path}",
            )
        if attempt_envelope["payload"]["status"] != "succeeded":
            raise ArtifactCorruptionError(
                "complete_attempt_status",
                attempt_path,
                f"complete marker references a non-succeeded attempt: {attempt_path}",
            )
        return _json_copy(attempt_envelope["payload"])

    def _problem(
        self,
        error: ArtifactCorruptionError,
        case_id: str | None,
    ) -> InventoryProblem:
        try:
            relative_path = error.path.relative_to(self.directory).as_posix()
        except ValueError:
            relative_path = error.path.name
        return InventoryProblem(
            code=error.code,
            path=relative_path,
            case_id=case_id,
            detail=f"artifact validation failed: {error.code}",
        )

    def scan(self) -> ArtifactInventory:
        manifest = self.load_manifest()
        cases = self._case_catalog(manifest)
        succeeded: list[str] = []
        failed: list[str] = []
        missing: list[str] = []
        corrupt: list[str] = []
        not_run: list[str] = []
        interrupted: list[str] = []
        exhausted: list[str] = []
        problems: list[InventoryProblem] = []
        expected_directories: set[str] = set()
        max_attempts = self._max_attempts(manifest)

        for case_id, case in cases.items():
            case_directory = self._case_directory(case)
            expected_directories.add(case_directory.name)
            complete_path = case_directory / "complete.json"
            if _path_entry_exists(complete_path):
                try:
                    self.load_completed(case_id)
                except ArtifactCorruptionError as exc:
                    corrupt.append(case_id)
                    problems.append(self._problem(exc, case_id))
                else:
                    succeeded.append(case_id)
                continue

            if not _path_entry_exists(case_directory):
                not_run.append(case_id)
                continue
            try:
                existing_attempts = self._validated_case_attempts(case)
            except ArtifactCorruptionError as exc:
                corrupt.append(case_id)
                problems.append(self._problem(exc, case_id))
                continue
            if not existing_attempts:
                not_run.append(case_id)
                continue
            attempt_payloads = [
                envelope["payload"] for envelope in existing_attempts.values()
            ]
            if any(payload["status"] == "succeeded" for payload in attempt_payloads):
                missing.append(case_id)
                problems.append(
                    InventoryProblem(
                        code="missing_complete_marker",
                        path=case_directory.relative_to(self.directory).as_posix(),
                        case_id=case_id,
                        detail="succeeded attempt has no complete marker",
                    )
                )
            elif len(attempt_payloads) >= max_attempts:
                exhausted.append(case_id)
            elif attempt_payloads[-1]["status"] == "failed":
                failed.append(case_id)
            elif attempt_payloads[-1]["status"] == "interrupted":
                interrupted.append(case_id)
            else:
                missing.append(case_id)

        cases_root = self.directory / "cases"
        if _path_entry_exists(cases_root) and (
            _is_reparse_point(cases_root)
            or not cases_root.is_dir()
            or not cases_root.resolve().is_relative_to(self.directory.resolve())
        ):
            problems.append(
                InventoryProblem(
                    code="unsafe_cases_root",
                    path="cases",
                    case_id=None,
                    detail="cases artifact root is unsafe or escapes experiment root",
                )
            )
        elif cases_root.is_dir():
            for path in sorted(cases_root.iterdir(), key=lambda item: item.name):
                if path.name not in expected_directories:
                    problems.append(
                        InventoryProblem(
                            code="unexpected_case_path",
                            path=path.relative_to(self.directory).as_posix(),
                            case_id=None,
                            detail="artifact path is not declared by the manifest",
                        )
                    )

        return ArtifactInventory(
            expected=len(cases),
            case_order=tuple(cases),
            succeeded=tuple(succeeded),
            failed=tuple(failed),
            missing=tuple(missing),
            corrupt=tuple(corrupt),
            not_run=tuple(not_run),
            interrupted=tuple(interrupted),
            exhausted=tuple(exhausted),
            problems=tuple(problems),
        )

    def _assert_compatible(
        self,
        existing: Mapping[str, Any],
        requested: Mapping[str, Any],
    ) -> None:
        existing_valid = validate_experiment_manifest(existing)
        requested_valid = validate_experiment_manifest(requested)
        for label, manifest in (
            ("existing", existing_valid),
            ("requested", requested_valid),
        ):
            projected_hash = canonical_hash(_compatibility_projection(manifest))
            if projected_hash != manifest["identity"]["resume_compatibility_hash"]:
                raise ExperimentContractError(
                    f"{label} compatibility projection drifted from runtime identity"
                )
        existing_hash = existing_valid["identity"]["resume_compatibility_hash"]
        requested_hash = requested_valid["identity"]["resume_compatibility_hash"]
        if existing_valid["experiment_id"] != requested_valid["experiment_id"]:
            raise ResumeCompatibilityError(
                existing_hash=existing_hash,
                requested_hash=requested_hash,
                differences=("$.experiment_id",),
            )
        if existing_hash == requested_hash:
            return
        differences = tuple(
            _changed_paths(
                _compatibility_projection(existing_valid),
                _compatibility_projection(requested_valid),
            )
        )
        raise ResumeCompatibilityError(
            existing_hash=existing_hash,
            requested_hash=requested_hash,
            differences=differences,
        )

    def prepare_resume(
        self, requested_manifest: Mapping[str, Any]
    ) -> ArtifactInventory:
        requested = validate_experiment_manifest(requested_manifest)
        existing = self.load_manifest()
        self._assert_compatible(existing, requested)
        return self.scan()
