from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from .evaluation import validate_eval_cases
from .evaluation_artifacts import (
    eval_case_from_artifact,
    eval_case_to_artifact,
)
from .experiment_runtime import (
    CACHE_MODES,
    EXECUTION_MODES,
    REGISTERED_EXECUTION_MODE_POLICIES,
    canonical_hash,
    canonical_json_bytes,
)
from .json_utils import (
    reject_duplicate_object_pairs,
    reject_non_finite_json_constant,
    validate_json_unicode,
)
from .models import EvalCase

DATASET_REGISTRY_SCHEMA_VERSION = 1
DATASET_ROLES = frozenset({"legacy_regression", "synthetic_fixture"})
EXPOSURE_STATUSES = frozenset(
    {"repeated_development", "synthetic", "unseen_evaluation"}
)
GENERATION_POLICIES = frozenset({"forbidden", "required"})
JUDGE_POLICIES = frozenset({"forbidden", "optional"})
EXTERNAL_CALL_POLICIES = frozenset({"forbidden", "explicit_opt_in"})
MODE_ORDER = (
    "offline",
    "retrieval",
    "smoke-generation",
    "full-regression",
)
_MODE_REQUIREMENTS = REGISTERED_EXECUTION_MODE_POLICIES
_ENTRY_FIELDS = {
    "dataset_id",
    "path",
    "role",
    "source_format",
    "file_hash_normalization",
    "file_sha256",
    "case_schema_version",
    "case_count",
    "case_artifact_set_sha256",
    "manifest_case_set_sha256",
    "type_counts",
    "gold_case_count",
    "expected_behavior_counts",
    "allowed_modes",
    "freeze",
    "provenance",
    "exposure",
    "duplicate_policy",
    "gold_policy",
    "derived_from",
}
_SHA256_LENGTH = 64
_EVAL_CASE_REQUIRED_FIELDS = {"id", "question"}
_EVAL_CASE_ALLOWED_FIELDS = {
    "id",
    "question",
    "type",
    "expected_law",
    "expected_articles",
    "keywords",
    "expected_behavior",
    "expected_answer_mode",
    "session_group",
    "turn_index",
    "schema_version",
}


class DatasetRegistryError(ValueError):
    """Raised when an evaluation dataset cannot prove its registered identity."""


@dataclass(frozen=True)
class DatasetEntry:
    dataset_id: str
    relative_path: str
    role: str
    source_format: str
    file_hash_normalization: str
    file_sha256: str
    case_schema_version: int
    case_count: int
    case_artifact_set_sha256: str
    manifest_case_set_sha256: str
    type_counts: Mapping[str, int]
    gold_case_count: int
    expected_behavior_counts: Mapping[str, int]
    allowed_modes: tuple[str, ...]
    freeze: Mapping[str, Any]
    provenance: Mapping[str, Any]
    exposure: Mapping[str, Any]
    duplicate_policy: Mapping[str, Any]
    gold_policy: Mapping[str, Any]
    derived_from: Mapping[str, Any] | None


@dataclass(frozen=True)
class RegisteredDataset:
    entry: DatasetEntry
    path: Path
    registry_schema_version: int
    registry_file_sha256: str
    _case_artifacts: tuple[bytes, ...]

    @property
    def cases(self) -> tuple[EvalCase, ...]:
        """Return defensive case copies from the immutable verified artifacts."""

        return tuple(
            eval_case_from_artifact(json.loads(artifact.decode("utf-8")))
            for artifact in self._case_artifacts
        )

    def manifest_payload(self) -> dict[str, Any]:
        """Return the immutable dataset portion of an experiment manifest."""

        payload: dict[str, Any] = {
            "dataset_id": self.entry.dataset_id,
            "role": self.entry.role,
            "case_file_hash": self.entry.file_sha256,
            "case_schema_version": self.entry.case_schema_version,
            "case_count": self.entry.case_count,
            "case_artifact_set_sha256": self.entry.case_artifact_set_sha256,
            "case_set_hash": self.entry.manifest_case_set_sha256,
            "allowed_modes": list(self.entry.allowed_modes),
            "source": {
                "path": self.entry.relative_path,
                "file_hash_normalization": self.entry.file_hash_normalization,
            },
            "registry": {
                "schema_version": self.registry_schema_version,
                "file_sha256": self.registry_file_sha256,
                "frozen_at": self.entry.freeze["frozen_at"],
                "immutable": self.entry.freeze["immutable"],
                "exposure_status": self.entry.exposure["status"],
                "is_holdout": self.entry.exposure["is_holdout"],
                "gold_legal_authority_status": self.entry.gold_policy[
                    "legal_authority_status"
                ],
            },
        }
        # Kept lazy to avoid coupling registry parsing to runtime construction.
        from .experiment_adapter import build_manifest_cases

        payload["cases"] = build_manifest_cases(self.cases)
        return payload


@dataclass(frozen=True)
class ExperimentModePolicy:
    mode: str
    default_dataset_id: str
    generation: str
    judge: str
    external_calls: str


@dataclass(frozen=True)
class ModeSelection:
    mode: str
    dataset: RegisteredDataset
    generate: bool
    judge_enabled: bool
    external_calls_allowed: bool
    cache_mode: str


@dataclass(frozen=True)
class DatasetRegistry:
    path: Path
    repository_root: Path
    schema_version: int
    file_sha256: str
    datasets: Mapping[str, RegisteredDataset]
    modes: Mapping[str, ExperimentModePolicy]

    def dataset(self, dataset_id: str) -> RegisteredDataset:
        try:
            return self.datasets[dataset_id]
        except KeyError as exc:
            raise DatasetRegistryError(f"unknown dataset_id: {dataset_id!r}") from exc

    def resolve_mode(
        self,
        mode: str,
        *,
        dataset_id: str | None = None,
        judge_enabled: bool = False,
        allow_external_calls: bool = False,
        cache_mode: str = "fresh",
    ) -> ModeSelection:
        """Resolve one fail-closed evaluation mode without enabling a provider."""

        if mode not in self.modes:
            raise DatasetRegistryError(f"unsupported experiment mode: {mode!r}")
        if cache_mode not in CACHE_MODES:
            raise DatasetRegistryError(f"unsupported cache mode: {cache_mode!r}")
        if not isinstance(judge_enabled, bool):
            raise DatasetRegistryError("judge_enabled must be a boolean")
        if not isinstance(allow_external_calls, bool):
            raise DatasetRegistryError("allow_external_calls must be a boolean")
        policy = self.modes[mode]
        selected = self.dataset(dataset_id or policy.default_dataset_id)
        if mode not in selected.entry.allowed_modes:
            raise DatasetRegistryError(
                f"dataset {selected.entry.dataset_id!r} is not registered for mode {mode!r}"
            )
        _validate_mode_dataset(mode, selected.entry)
        generate = policy.generation == "required"
        if judge_enabled and policy.judge == "forbidden":
            raise DatasetRegistryError(f"mode {mode!r} forbids judge calls")
        if allow_external_calls and policy.external_calls == "forbidden":
            raise DatasetRegistryError(f"mode {mode!r} forbids external calls")
        if cache_mode == "replay" and allow_external_calls:
            raise DatasetRegistryError("replay mode must declare zero external calls")
        return ModeSelection(
            mode=mode,
            dataset=selected,
            generate=generate,
            judge_enabled=judge_enabled,
            external_calls_allowed=allow_external_calls,
            cache_mode=cache_mode,
        )


def default_dataset_registry_path(
    repository_root: str | Path | None = None,
) -> Path:
    root = (
        Path(repository_root).resolve()
        if repository_root is not None
        else Path(__file__).resolve().parents[1]
    )
    return root / "eval_cases" / "registry.json"


def load_dataset_registry(
    path: str | Path,
    *,
    repository_root: str | Path | None = None,
) -> DatasetRegistry:
    """Load and fully verify the registry, files, selections, and parent links."""

    requested_registry_path = Path(path).absolute()
    if requested_registry_path.is_symlink():
        raise DatasetRegistryError("dataset registry must not be a symlink")
    registry_path = requested_registry_path.resolve()
    root = (
        Path(repository_root).resolve()
        if repository_root is not None
        else registry_path.parent.parent.resolve()
    )
    if not registry_path.is_relative_to(root):
        raise DatasetRegistryError("dataset registry must be inside repository_root")
    raw = _read_safe_file(registry_path, root=root, label="dataset registry")
    payload = _strict_json_loads(raw.decode("utf-8"), label="dataset registry")
    if not isinstance(payload, dict) or set(payload) != {
        "registry_schema_version",
        "datasets",
        "mode_defaults",
    }:
        raise DatasetRegistryError("dataset registry top-level fields are invalid")
    if (
        type(payload["registry_schema_version"]) is not int
        or payload["registry_schema_version"] != DATASET_REGISTRY_SCHEMA_VERSION
    ):
        raise DatasetRegistryError("unsupported dataset registry schema version")
    raw_entries = payload["datasets"]
    if not isinstance(raw_entries, list) or not raw_entries:
        raise DatasetRegistryError("dataset registry must contain datasets")

    entries: dict[str, DatasetEntry] = {}
    for index, value in enumerate(raw_entries):
        entry = _parse_entry(value, index=index)
        if entry.dataset_id in entries:
            raise DatasetRegistryError(f"duplicate dataset_id: {entry.dataset_id!r}")
        entries[entry.dataset_id] = entry

    modes = _parse_modes(payload["mode_defaults"], entries)
    registry_file_sha256 = hashlib.sha256(_normalized_file_bytes(raw)).hexdigest()
    loaded: dict[str, RegisteredDataset] = {}
    loading: set[str] = set()

    def load_one(dataset_id: str) -> RegisteredDataset:
        if dataset_id in loaded:
            return loaded[dataset_id]
        if dataset_id in loading:
            raise DatasetRegistryError("dataset parent relationship contains a cycle")
        loading.add(dataset_id)
        entry = entries[dataset_id]
        dataset = _load_registered_dataset(
            entry,
            root=root,
            registry_file_sha256=registry_file_sha256,
        )
        parent_ref = entry.derived_from
        if parent_ref is not None:
            parent_id = parent_ref["dataset_id"]
            if parent_id not in entries:
                raise DatasetRegistryError(
                    f"dataset {dataset_id!r} has unknown parent {parent_id!r}"
                )
            parent = load_one(parent_id)
            _validate_derived_dataset(dataset, parent)
        loading.remove(dataset_id)
        loaded[dataset_id] = dataset
        return dataset

    for dataset_id in entries:
        load_one(dataset_id)

    return DatasetRegistry(
        path=registry_path,
        repository_root=root,
        schema_version=DATASET_REGISTRY_SCHEMA_VERSION,
        file_sha256=registry_file_sha256,
        datasets=MappingProxyType(dict(loaded)),
        modes=MappingProxyType(dict(modes)),
    )


def _parse_entry(value: Any, *, index: int) -> DatasetEntry:
    name = f"datasets[{index}]"
    item = _exact_object(name, value, _ENTRY_FIELDS)
    dataset_id = _non_empty_string(f"{name}.dataset_id", item["dataset_id"])
    relative_path = _safe_relative_path(f"{name}.path", item["path"])
    role = _enum(f"{name}.role", item["role"], DATASET_ROLES)
    source_format = _enum(f"{name}.source_format", item["source_format"], {"jsonl"})
    file_hash_normalization = _enum(
        f"{name}.file_hash_normalization",
        item["file_hash_normalization"],
        {"utf8_lf_v1"},
    )
    file_sha256 = _sha256(f"{name}.file_sha256", item["file_sha256"])
    case_schema_version = _positive_integer(
        f"{name}.case_schema_version", item["case_schema_version"]
    )
    if case_schema_version != 1:
        raise DatasetRegistryError(
            f"{name}.case_schema_version is unsupported: {case_schema_version}"
        )
    case_count = _positive_integer(f"{name}.case_count", item["case_count"])
    case_artifact_set_sha256 = _sha256(
        f"{name}.case_artifact_set_sha256", item["case_artifact_set_sha256"]
    )
    manifest_case_set_sha256 = _sha256(
        f"{name}.manifest_case_set_sha256", item["manifest_case_set_sha256"]
    )
    type_counts = _count_map(f"{name}.type_counts", item["type_counts"])
    gold_case_count = _non_negative_integer(
        f"{name}.gold_case_count", item["gold_case_count"]
    )
    expected_behavior_counts = _count_map(
        f"{name}.expected_behavior_counts", item["expected_behavior_counts"]
    )
    allowed_modes = _string_tuple(f"{name}.allowed_modes", item["allowed_modes"])
    if not allowed_modes or not set(allowed_modes).issubset(EXECUTION_MODES):
        raise DatasetRegistryError(f"{name}.allowed_modes contains unsupported modes")

    freeze = _exact_object(f"{name}.freeze", item["freeze"], {"frozen_at", "immutable"})
    _timezone_datetime(f"{name}.freeze.frozen_at", freeze["frozen_at"])
    if freeze["immutable"] is not True:
        raise DatasetRegistryError(f"{name}.freeze.immutable must be true")

    provenance = _exact_object(
        f"{name}.provenance",
        item["provenance"],
        {
            "source_commit",
            "source_documents",
            "annotation_method",
            "annotator_status",
        },
    )
    commit = _non_empty_string(
        f"{name}.provenance.source_commit", provenance["source_commit"]
    )
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise DatasetRegistryError(
            f"{name}.provenance.source_commit must be a lowercase Git SHA"
        )
    source_documents = _string_tuple(
        f"{name}.provenance.source_documents", provenance["source_documents"]
    )
    if not source_documents:
        raise DatasetRegistryError(
            f"{name}.provenance.source_documents must not be empty"
        )
    _non_empty_string(
        f"{name}.provenance.annotation_method", provenance["annotation_method"]
    )
    _enum(
        f"{name}.provenance.annotator_status",
        provenance["annotator_status"],
        {"recorded", "not_recorded"},
    )

    exposure = _exact_object(
        f"{name}.exposure", item["exposure"], {"status", "is_holdout"}
    )
    _enum(f"{name}.exposure.status", exposure["status"], EXPOSURE_STATUSES)
    if not isinstance(exposure["is_holdout"], bool):
        raise DatasetRegistryError(f"{name}.exposure.is_holdout must be a boolean")
    if exposure["is_holdout"]:
        raise DatasetRegistryError(
            f"{role} data cannot be labeled holdout in registry schema v1"
        )
    expected_exposure = {
        "legacy_regression": "repeated_development",
        "synthetic_fixture": "synthetic",
    }[role]
    if exposure["status"] != expected_exposure:
        raise DatasetRegistryError(
            f"{name}.exposure.status is incompatible with role {role!r}"
        )

    duplicate_policy = _exact_object(
        f"{name}.duplicate_policy",
        item["duplicate_policy"],
        {"algorithm", "threshold", "expected_pairs"},
    )
    _enum(
        f"{name}.duplicate_policy.algorithm",
        duplicate_policy["algorithm"],
        {"nfkc_alnum_char_trigram_jaccard"},
    )
    threshold = duplicate_policy["threshold"]
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(float(threshold))
        or not 0 < float(threshold) <= 1
    ):
        raise DatasetRegistryError(
            f"{name}.duplicate_policy.threshold must be in (0, 1]"
        )
    _non_negative_integer(
        f"{name}.duplicate_policy.expected_pairs",
        duplicate_policy["expected_pairs"],
    )

    gold_policy = _exact_object(
        f"{name}.gold_policy",
        item["gold_policy"],
        {"structural_status", "index_presence_status", "legal_authority_status"},
    )
    for field_name in gold_policy:
        _non_empty_string(f"{name}.gold_policy.{field_name}", gold_policy[field_name])
    expected_gold_policy = {
        "legacy_regression": {
            "structural_status": "verified_by_registry_loader",
            "index_presence_status": "verified_only_against_untracked_local_snapshot",
            "legal_authority_status": "not_authoritatively_reviewed",
        },
        "synthetic_fixture": {
            "structural_status": "verified_by_registry_loader",
            "index_presence_status": "bound_to_tracked_synthetic_m0_fixture",
            "legal_authority_status": "not_applicable_synthetic_content",
        },
    }[role]
    if gold_policy != expected_gold_policy:
        raise DatasetRegistryError(
            f"{name}.gold_policy is incompatible with role {role!r}"
        )

    derived_from = item["derived_from"]
    if derived_from is not None:
        derived_from = _exact_object(
            f"{name}.derived_from",
            derived_from,
            {"dataset_id", "selection_case_ids"},
        )
        _non_empty_string(f"{name}.derived_from.dataset_id", derived_from["dataset_id"])
        selection = derived_from["selection_case_ids"]
        if selection is not None:
            selection_ids = _string_tuple(
                f"{name}.derived_from.selection_case_ids", selection
            )
            if not selection_ids:
                raise DatasetRegistryError("derived selection_case_ids cannot be empty")
            derived_from["selection_case_ids"] = list(selection_ids)

    if sum(type_counts.values()) != case_count:
        raise DatasetRegistryError(f"{name}.type_counts does not sum to case_count")
    if sum(expected_behavior_counts.values()) != case_count:
        raise DatasetRegistryError(
            f"{name}.expected_behavior_counts does not sum to case_count"
        )
    if gold_case_count > case_count:
        raise DatasetRegistryError(f"{name}.gold_case_count exceeds case_count")

    return DatasetEntry(
        dataset_id=dataset_id,
        relative_path=relative_path,
        role=role,
        source_format=source_format,
        file_hash_normalization=file_hash_normalization,
        file_sha256=file_sha256,
        case_schema_version=case_schema_version,
        case_count=case_count,
        case_artifact_set_sha256=case_artifact_set_sha256,
        manifest_case_set_sha256=manifest_case_set_sha256,
        type_counts=MappingProxyType(dict(type_counts)),
        gold_case_count=gold_case_count,
        expected_behavior_counts=MappingProxyType(dict(expected_behavior_counts)),
        allowed_modes=allowed_modes,
        freeze=_freeze_mapping(freeze),
        provenance=_freeze_mapping(provenance),
        exposure=_freeze_mapping(exposure),
        duplicate_policy=_freeze_mapping(duplicate_policy),
        gold_policy=_freeze_mapping(gold_policy),
        derived_from=_freeze_mapping(derived_from)
        if derived_from is not None
        else None,
    )


def _parse_modes(
    value: Any,
    entries: Mapping[str, DatasetEntry],
) -> dict[str, ExperimentModePolicy]:
    modes = _exact_object("mode_defaults", value, set(MODE_ORDER))
    result: dict[str, ExperimentModePolicy] = {}
    for mode in MODE_ORDER:
        raw = _exact_object(
            f"mode_defaults.{mode}",
            modes[mode],
            {"default_dataset_id", "generation", "judge", "external_calls"},
        )
        dataset_id = _non_empty_string(
            f"mode_defaults.{mode}.default_dataset_id", raw["default_dataset_id"]
        )
        if dataset_id not in entries:
            raise DatasetRegistryError(
                f"mode {mode!r} references unknown dataset {dataset_id!r}"
            )
        if mode not in entries[dataset_id].allowed_modes:
            raise DatasetRegistryError(
                f"mode {mode!r} default dataset does not allow that mode"
            )
        _validate_mode_dataset(mode, entries[dataset_id])
        generation = _enum(
            f"mode_defaults.{mode}.generation",
            raw["generation"],
            GENERATION_POLICIES,
        )
        judge = _enum(f"mode_defaults.{mode}.judge", raw["judge"], JUDGE_POLICIES)
        external_calls = _enum(
            f"mode_defaults.{mode}.external_calls",
            raw["external_calls"],
            EXTERNAL_CALL_POLICIES,
        )
        required = _MODE_REQUIREMENTS[mode]
        actual = {
            "generation": generation,
            "judge": judge,
            "external_calls": external_calls,
        }
        if actual != required:
            raise DatasetRegistryError(
                f"mode {mode!r} weakens or changes the built-in safety contract"
            )
        result[mode] = ExperimentModePolicy(
            mode=mode,
            default_dataset_id=dataset_id,
            generation=generation,
            judge=judge,
            external_calls=external_calls,
        )
    return result


def _validate_mode_dataset(mode: str, entry: DatasetEntry) -> None:
    if mode == "offline" and entry.role != "synthetic_fixture":
        raise DatasetRegistryError("offline mode requires a synthetic_fixture dataset")
    if mode == "smoke-generation" and entry.case_count > 30:
        raise DatasetRegistryError(
            "smoke-generation mode requires a fixed dataset of at most 30 cases"
        )


def _load_registered_dataset(
    entry: DatasetEntry,
    *,
    root: Path,
    registry_file_sha256: str,
) -> RegisteredDataset:
    requested_path = root / PurePosixPath(entry.relative_path)
    if requested_path.is_symlink():
        raise DatasetRegistryError(
            f"dataset {entry.dataset_id!r} must not be a symlink"
        )
    path = requested_path.resolve()
    raw = _read_safe_file(path, root=root, label=f"dataset {entry.dataset_id!r}")
    if hashlib.sha256(_normalized_file_bytes(raw)).hexdigest() != entry.file_sha256:
        raise DatasetRegistryError(
            f"dataset {entry.dataset_id!r} file SHA-256 does not match the registry"
        )
    strict_items = _strict_jsonl(raw, label=f"dataset {entry.dataset_id!r}")
    try:
        loaded_cases = _eval_cases_from_items(strict_items)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DatasetRegistryError(
            f"dataset {entry.dataset_id!r} cases are invalid"
        ) from exc
    cases_by_id = {case.case_id: case for case in loaded_cases}
    selection = (
        entry.derived_from.get("selection_case_ids")
        if entry.derived_from is not None
        else None
    )
    if selection is None:
        cases = tuple(loaded_cases)
    else:
        missing = [case_id for case_id in selection if case_id not in cases_by_id]
        if missing:
            raise DatasetRegistryError(
                f"dataset {entry.dataset_id!r} selection is missing case {missing[0]!r}"
            )
        cases = tuple(cases_by_id[case_id] for case_id in selection)

    if len(cases) != entry.case_count:
        raise DatasetRegistryError(
            f"dataset {entry.dataset_id!r} case_count does not match selected cases"
        )
    if any(case.schema_version != entry.case_schema_version for case in cases):
        raise DatasetRegistryError(
            f"dataset {entry.dataset_id!r} case schema version is inconsistent"
        )
    case_artifact_set_hash = canonical_hash(
        [eval_case_to_artifact(case) for case in cases]
    )
    if case_artifact_set_hash != entry.case_artifact_set_sha256:
        raise DatasetRegistryError(
            f"dataset {entry.dataset_id!r} normalized case-set hash does not match"
        )
    from .experiment_adapter import build_manifest_cases

    manifest_case_set_hash = canonical_hash(build_manifest_cases(cases))
    if manifest_case_set_hash != entry.manifest_case_set_sha256:
        raise DatasetRegistryError(
            f"dataset {entry.dataset_id!r} manifest case-set hash does not match"
        )
    type_counts = dict(sorted(Counter(case.case_type for case in cases).items()))
    if type_counts != dict(sorted(entry.type_counts.items())):
        raise DatasetRegistryError(
            f"dataset {entry.dataset_id!r} type_counts do not match cases"
        )
    behavior_counts = dict(
        sorted(Counter(case.resolved_expected_behavior for case in cases).items())
    )
    if behavior_counts != dict(sorted(entry.expected_behavior_counts.items())):
        raise DatasetRegistryError(
            f"dataset {entry.dataset_id!r} expected_behavior_counts do not match cases"
        )
    _validate_gold_structure(entry.dataset_id, cases)
    gold_count = sum(
        bool(case.expected_law.strip()) or bool(case.expected_articles)
        for case in cases
    )
    if gold_count != entry.gold_case_count:
        raise DatasetRegistryError(
            f"dataset {entry.dataset_id!r} gold_case_count does not match cases"
        )
    duplicate_pairs = _near_duplicate_pairs(
        cases, threshold=float(entry.duplicate_policy["threshold"])
    )
    if len(duplicate_pairs) != entry.duplicate_policy["expected_pairs"]:
        raise DatasetRegistryError(
            f"dataset {entry.dataset_id!r} near-duplicate result drifted"
        )
    return RegisteredDataset(
        entry=entry,
        path=path,
        registry_schema_version=DATASET_REGISTRY_SCHEMA_VERSION,
        registry_file_sha256=registry_file_sha256,
        _case_artifacts=tuple(
            canonical_json_bytes(eval_case_to_artifact(case)) for case in cases
        ),
    )


def _validate_gold_structure(dataset_id: str, cases: Sequence[EvalCase]) -> None:
    for case in cases:
        if not case.keywords or any(not keyword.strip() for keyword in case.keywords):
            raise DatasetRegistryError(
                f"dataset {dataset_id!r} has invalid keywords: {case.case_id!r}"
            )
        if len(case.keywords) != len(set(case.keywords)):
            raise DatasetRegistryError(
                f"dataset {dataset_id!r} has duplicate keywords: {case.case_id!r}"
            )
        if any(not article.strip() for article in case.expected_articles):
            raise DatasetRegistryError(
                f"dataset {dataset_id!r} has an empty expected article: {case.case_id!r}"
            )
        if len(case.expected_articles) != len(set(case.expected_articles)):
            raise DatasetRegistryError(
                f"dataset {dataset_id!r} has duplicate expected articles: {case.case_id!r}"
            )
        has_retrieval_gold = bool(case.expected_law.strip() or case.expected_articles)
        if case.resolved_expected_behavior == "out_of_scope" and has_retrieval_gold:
            raise DatasetRegistryError(
                f"dataset {dataset_id!r} refusal case contains retrieval gold: "
                f"{case.case_id!r}"
            )
        if (
            case.resolved_expected_behavior == "evidence_answer"
            and not has_retrieval_gold
        ):
            raise DatasetRegistryError(
                f"dataset {dataset_id!r} answer case lacks retrieval gold: "
                f"{case.case_id!r}"
            )


def _validate_derived_dataset(
    child: RegisteredDataset,
    parent: RegisteredDataset,
) -> None:
    parent_artifacts = {
        case.case_id: eval_case_to_artifact(case) for case in parent.cases
    }
    for case in child.cases:
        expected = parent_artifacts.get(case.case_id)
        if expected is None:
            raise DatasetRegistryError(
                f"derived dataset {child.entry.dataset_id!r} contains case not in parent: "
                f"{case.case_id!r}"
            )
        if eval_case_to_artifact(case) != expected:
            raise DatasetRegistryError(
                f"derived dataset {child.entry.dataset_id!r} changed parent case: "
                f"{case.case_id!r}"
            )


def _near_duplicate_pairs(
    cases: Sequence[EvalCase],
    *,
    threshold: float,
) -> tuple[tuple[str, str], ...]:
    normalized = [(_question_ngrams(case.question), case.case_id) for case in cases]
    pairs: list[tuple[str, str]] = []
    for index, (left, left_id) in enumerate(normalized):
        for right, right_id in normalized[index + 1 :]:
            union = left | right
            score = len(left & right) / len(union) if union else 1.0
            if score >= threshold:
                pairs.append((left_id, right_id))
    return tuple(pairs)


def _question_ngrams(value: str) -> set[str]:
    normalized = "".join(
        char
        for char in unicodedata.normalize("NFKC", value).casefold()
        if char.isalnum()
    )
    if len(normalized) < 3:
        return {normalized}
    return {normalized[index : index + 3] for index in range(len(normalized) - 2)}


def _read_safe_file(path: Path, *, root: Path, label: str) -> bytes:
    if not path.is_relative_to(root):
        raise DatasetRegistryError(f"{label} escapes repository_root")
    if path.is_symlink() or not path.is_file():
        raise DatasetRegistryError(f"{label} must be a regular non-symlink file")
    try:
        raw = path.read_bytes()
        raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise DatasetRegistryError(f"{label} is not readable UTF-8") from exc
    if raw.startswith(b"\xef\xbb\xbf"):
        raise DatasetRegistryError(f"{label} must use UTF-8 without BOM")
    return raw


def _normalized_file_bytes(raw: bytes) -> bytes:
    """Canonicalize platform line endings while preserving all JSON content."""

    return raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _strict_json_loads(value: str, *, label: str) -> Any:
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=reject_duplicate_object_pairs,
            parse_constant=reject_non_finite_json_constant,
        )
        validate_json_unicode(parsed)
        return parsed
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DatasetRegistryError(f"{label} is invalid strict JSON") from exc


def _strict_jsonl(raw: bytes, *, label: str) -> tuple[dict[str, Any], ...]:
    text = raw.decode("utf-8")
    items: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        item = _strict_json_loads(line, label=f"{label} line {line_number}")
        if not isinstance(item, dict):
            raise DatasetRegistryError(f"{label} line {line_number} must be an object")
        missing = _EVAL_CASE_REQUIRED_FIELDS - set(item)
        unknown = set(item) - _EVAL_CASE_ALLOWED_FIELDS
        if missing or unknown:
            raise DatasetRegistryError(
                f"{label} line {line_number} has invalid evaluation case fields"
            )
        items.append(item)
    if not items:
        raise DatasetRegistryError(f"{label} must contain at least one case")
    return tuple(items)


def _eval_cases_from_items(items: Sequence[Mapping[str, Any]]) -> list[EvalCase]:
    cases: list[EvalCase] = []
    for item in items:
        case_type = item.get("type", "unknown")
        expected_behavior = item.get("expected_behavior")
        expected_answer_mode = item.get("expected_answer_mode")
        if (
            expected_behavior is not None
            and expected_answer_mode is not None
            and expected_behavior != expected_answer_mode
        ):
            raise DatasetRegistryError(
                f"conflicting expected behavior aliases for {item.get('id')!r}"
            )
        if expected_behavior is None:
            expected_behavior = expected_answer_mode
        if expected_behavior is None:
            expected_behavior = (
                "out_of_scope" if case_type == "refusal" else "evidence_answer"
            )
        cases.append(
            EvalCase(
                case_id=item["id"],
                question=item["question"],
                case_type=case_type,
                expected_law=item.get("expected_law", ""),
                expected_articles=item.get("expected_articles", []),
                keywords=item.get("keywords", []),
                expected_behavior=expected_behavior,
                session_group=item.get("session_group"),
                turn_index=item.get("turn_index", 0),
                schema_version=item.get("schema_version", 1),
            )
        )
    validate_eval_cases(cases)
    return cases


def _exact_object(name: str, value: Any, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise DatasetRegistryError(f"{name} fields are invalid")
    return dict(value)


def _non_empty_string(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DatasetRegistryError(f"{name} must be a non-empty string")
    validate_json_unicode(value)
    return value


def _safe_relative_path(name: str, value: Any) -> str:
    rendered = _non_empty_string(name, value)
    if "\\" in rendered:
        raise DatasetRegistryError(f"{name} must use POSIX separators")
    path = PurePosixPath(rendered)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise DatasetRegistryError(f"{name} must be a safe repository-relative path")
    return path.as_posix()


def _enum(name: str, value: Any, allowed: set[str] | frozenset[str]) -> str:
    rendered = _non_empty_string(name, value)
    if rendered not in allowed:
        raise DatasetRegistryError(f"{name} is unsupported: {rendered!r}")
    return rendered


def _positive_integer(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DatasetRegistryError(f"{name} must be a positive integer")
    return value


def _non_negative_integer(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DatasetRegistryError(f"{name} must be a non-negative integer")
    return value


def _sha256(name: str, value: Any) -> str:
    rendered = _non_empty_string(name, value)
    if len(rendered) != _SHA256_LENGTH or any(
        char not in "0123456789abcdef" for char in rendered
    ):
        raise DatasetRegistryError(f"{name} must be a lowercase SHA-256")
    return rendered


def _string_tuple(name: str, value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise DatasetRegistryError(f"{name} must be a list")
    result = tuple(
        _non_empty_string(f"{name}[{index}]", item) for index, item in enumerate(value)
    )
    if len(set(result)) != len(result):
        raise DatasetRegistryError(f"{name} must not contain duplicates")
    return result


def _count_map(name: str, value: Any) -> dict[str, int]:
    if not isinstance(value, dict) or not value:
        raise DatasetRegistryError(f"{name} must be a non-empty object")
    result: dict[str, int] = {}
    for key, count in value.items():
        normalized_key = _non_empty_string(f"{name} key", key)
        result[normalized_key] = _non_negative_integer(
            f"{name}.{normalized_key}", count
        )
    return result


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(
        {
            key: tuple(item) if isinstance(item, list) else item
            for key, item in value.items()
        }
    )


def _timezone_datetime(name: str, value: Any) -> str:
    rendered = _non_empty_string(name, value)
    try:
        parsed = datetime.fromisoformat(rendered.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DatasetRegistryError(f"{name} must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        raise DatasetRegistryError(f"{name} must include a timezone")
    return rendered
