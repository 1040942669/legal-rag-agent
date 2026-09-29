"""Server-owned registrations for M6 batch jobs.

HTTP callers provide only an opaque registration ID. Paths, corpus scope and
embedding profile are controlled by this configuration, which is loaded from a
local file on both the API and worker hosts. Neither a broker message nor a job
row contains private source text, credentials or local paths.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Literal, Mapping


class JobRegistryError(ValueError):
    """A server-side job registration is invalid or unsafe."""


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_CONFIG_BYTES = 1_048_576


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise JobRegistryError("job registry JSON has duplicate keys")
        result[key] = value
    return result


def _mapping(value: Any, fields: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise JobRegistryError(f"{label} must have exactly the configured fields")
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise JobRegistryError(f"{label} must be an opaque identifier")
    return value


def _scope(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 255
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise JobRegistryError("scope_id is invalid")
    return value


def _profile(value: Any) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise JobRegistryError("profile_id must be a lowercase SHA-256 identity")
    return value


def _fingerprint(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=True, separators=(",", ":")
        ).encode("ascii")
    ).hexdigest()


def _absolute_directory(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise JobRegistryError(f"{label} must be an absolute directory")
    try:
        resolved = Path(value).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise JobRegistryError(f"{label} cannot be resolved") from exc
    if not resolved.is_dir():
        raise JobRegistryError(f"{label} must be a directory")
    return resolved


def _relative_path(
    root: Path,
    value: Any,
    label: str,
    *,
    kind: Literal["directory", "file"] | None = None,
) -> Path:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or Path(value).is_absolute()
        or PureWindowsPath(value).is_absolute()
        or ":" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise JobRegistryError(f"{label} must be a safe relative path")
    try:
        resolved = (root / value).resolve(strict=kind is not None)
    except (OSError, RuntimeError) as exc:
        raise JobRegistryError(f"{label} cannot be resolved") from exc
    if not resolved.is_relative_to(root):
        raise JobRegistryError(f"{label} escapes the configured artifact root")
    if kind == "directory" and not resolved.is_dir():
        raise JobRegistryError(f"{label} must be a directory")
    if kind == "file" and not resolved.is_file():
        raise JobRegistryError(f"{label} must be a file")
    return resolved


@dataclass(frozen=True, slots=True)
class EvaluationRegistration:
    reference: str
    scope_id: str
    profile_id: str
    dataset_id: str
    repository_root: Path
    experiment_root: Path
    total: int
    fingerprint: str


@dataclass(frozen=True, slots=True)
class IngestionRegistration:
    reference: str
    scope_id: str
    profile_id: str
    source_root: Path
    manifest_path: Path
    plan_path: Path
    snapshot_id: str
    index_mode: Literal["exact", "hnsw"]
    total: int
    fingerprint: str

    @property
    def build_hnsw(self) -> bool:
        return self.index_mode == "hnsw"


class JobRegistry:
    def __init__(
        self,
        *,
        evaluations: Mapping[str, EvaluationRegistration],
        ingestions: Mapping[str, IngestionRegistration],
    ) -> None:
        self._evaluations = dict(evaluations)
        self._ingestions = dict(ingestions)

    @classmethod
    def from_json_file(cls, path: str | Path) -> JobRegistry:
        config_path = Path(path)
        try:
            if config_path.stat().st_size > _MAX_CONFIG_BYTES:
                raise JobRegistryError("job registry is too large")
            config = json.loads(
                config_path.read_text(encoding="utf-8"),
                object_pairs_hook=_unique_object,
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise JobRegistryError("job registry is not readable UTF-8 JSON") from exc
        top = _mapping(
            config,
            {
                "schema_version",
                "artifact_root",
                "repository_root",
                "evaluations",
                "ingestions",
            },
            "job registry",
        )
        if type(top["schema_version"]) is not int or top["schema_version"] != 1:
            raise JobRegistryError("job registry schema_version must be 1")
        artifact_root = _absolute_directory(top["artifact_root"], "artifact_root")
        repository_root = _absolute_directory(top["repository_root"], "repository_root")
        evaluations = top["evaluations"]
        ingestions = top["ingestions"]
        if not isinstance(evaluations, dict) or not isinstance(ingestions, dict):
            raise JobRegistryError("evaluations and ingestions must be objects")

        parsed_evaluations: dict[str, EvaluationRegistration] = {}
        dataset_registry = None
        if evaluations:
            from legal_rag.experiment_datasets import (
                default_dataset_registry_path,
                load_dataset_registry,
            )

            try:
                dataset_registry = load_dataset_registry(
                    default_dataset_registry_path(repository_root),
                    repository_root=repository_root,
                )
            except (OSError, ValueError) as exc:
                raise JobRegistryError(
                    "evaluation dataset registry is unavailable"
                ) from exc
        for raw_ref, raw_entry in evaluations.items():
            ref = _identifier(raw_ref, "evaluation reference")
            entry = _mapping(
                raw_entry,
                {"scope_id", "profile_id", "dataset_id", "experiment_root", "total"},
                "evaluation registration",
            )
            scope_id = _scope(entry["scope_id"])
            profile_id = _profile(entry["profile_id"])
            dataset_id = _identifier(entry["dataset_id"], "dataset_id")
            experiment_root = _relative_path(
                artifact_root, entry["experiment_root"], "experiment_root"
            )
            total = entry["total"]
            if type(total) is not int or not 1 <= total <= 1_000_000:
                raise JobRegistryError("evaluation total must be between 1 and 1000000")
            assert dataset_registry is not None
            try:
                selection = dataset_registry.resolve_mode(
                    "offline",
                    dataset_id=dataset_id,
                    judge_enabled=False,
                    allow_external_calls=False,
                )
            except ValueError as exc:
                raise JobRegistryError(
                    "evaluation dataset is not an approved offline dataset"
                ) from exc
            if selection.dataset.entry.case_count != total:
                raise JobRegistryError(
                    "evaluation total differs from the registered dataset"
                )
            fingerprint = _fingerprint(
                {
                    "schema_version": 1,
                    "kind": "evaluation",
                    "reference": ref,
                    "scope_id": scope_id,
                    "profile_id": profile_id,
                    "dataset_id": dataset_id,
                    "repository_root": str(repository_root),
                    "experiment_root": str(experiment_root),
                    "total": total,
                    "dataset_registry_sha256": dataset_registry.file_sha256,
                    "dataset_file_sha256": selection.dataset.entry.file_sha256,
                }
            )
            parsed_evaluations[ref] = EvaluationRegistration(
                ref,
                scope_id,
                profile_id,
                dataset_id,
                repository_root,
                experiment_root,
                total,
                fingerprint,
            )

        parsed_ingestions: dict[str, IngestionRegistration] = {}
        for raw_ref, raw_entry in ingestions.items():
            ref = _identifier(raw_ref, "ingestion reference")
            entry = _mapping(
                raw_entry,
                {
                    "scope_id",
                    "profile_id",
                    "source_root",
                    "manifest_path",
                    "plan_path",
                    "snapshot_id",
                    "index_mode",
                },
                "ingestion registration",
            )
            scope_id = _scope(entry["scope_id"])
            profile_id = _profile(entry["profile_id"])
            snapshot_id = _identifier(entry["snapshot_id"], "snapshot_id")
            index_mode = entry["index_mode"]
            if index_mode not in {"exact", "hnsw"}:
                raise JobRegistryError("index_mode must be exact or hnsw")
            source_root = _relative_path(
                artifact_root, entry["source_root"], "source_root", kind="directory"
            )
            manifest_path = _relative_path(
                source_root, entry["manifest_path"], "manifest_path", kind="file"
            )
            plan_path = _relative_path(
                source_root, entry["plan_path"], "plan_path", kind="file"
            )
            from legal_rag.storage.import_workflow import validate_import_plan

            try:
                prepared = validate_import_plan(
                    manifest_path, plan_path, source_root=source_root
                )
            except (OSError, ValueError) as exc:
                raise JobRegistryError(
                    "ingestion artifacts do not match their plan"
                ) from exc
            target = prepared.plan["target"]
            if (
                target["scope_id"] != scope_id
                or target["profile_id"] != profile_id
                or target["snapshot_id"] != snapshot_id
            ):
                raise JobRegistryError(
                    "ingestion target differs from registered scope/profile/snapshot"
                )
            fingerprint = _fingerprint(
                {
                    "schema_version": 1,
                    "kind": "ingestion",
                    "reference": ref,
                    "scope_id": scope_id,
                    "profile_id": profile_id,
                    "snapshot_id": snapshot_id,
                    "index_mode": index_mode,
                    "source_root": str(source_root),
                    "plan_sha256": prepared.plan["plan_sha256"],
                }
            )
            parsed_ingestions[ref] = IngestionRegistration(
                ref,
                scope_id,
                profile_id,
                source_root,
                manifest_path,
                plan_path,
                snapshot_id,
                index_mode,
                6,
                fingerprint,
            )
        return cls(evaluations=parsed_evaluations, ingestions=parsed_ingestions)

    def evaluation(
        self, reference: str, *, scope_id: str, profile_id: str
    ) -> EvaluationRegistration | None:
        entry = self._evaluations.get(reference)
        return (
            entry
            if entry and (entry.scope_id, entry.profile_id) == (scope_id, profile_id)
            else None
        )

    def ingestion(
        self, reference: str, *, scope_id: str, profile_id: str
    ) -> IngestionRegistration | None:
        entry = self._ingestions.get(reference)
        return (
            entry
            if entry and (entry.scope_id, entry.profile_id) == (scope_id, profile_id)
            else None
        )

    def resolve(
        self, kind: Literal["evaluation", "ingestion"], reference: str
    ) -> EvaluationRegistration | IngestionRegistration | None:
        if kind == "evaluation":
            return self._evaluations.get(reference)
        if kind == "ingestion":
            return self._ingestions.get(reference)
        return None
