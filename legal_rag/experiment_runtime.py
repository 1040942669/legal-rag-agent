from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .json_utils import (
    reject_duplicate_object_pairs,
    reject_non_finite_json_constant,
    validate_json_unicode,
)

EXPERIMENT_MANIFEST_SCHEMA_VERSION = 2
STAGE_CACHE_SCHEMA_VERSION = 1
EXECUTION_MODES = frozenset(
    {"offline", "retrieval", "smoke-generation", "full-regression"}
)
REGISTERED_EXECUTION_MODE_POLICIES = {
    "offline": {
        "generation": "forbidden",
        "judge": "forbidden",
        "external_calls": "forbidden",
    },
    "retrieval": {
        "generation": "forbidden",
        "judge": "forbidden",
        "external_calls": "explicit_opt_in",
    },
    "smoke-generation": {
        "generation": "required",
        "judge": "optional",
        "external_calls": "explicit_opt_in",
    },
    "full-regression": {
        "generation": "required",
        "judge": "optional",
        "external_calls": "explicit_opt_in",
    },
}
CACHE_MODES = frozenset({"fresh", "cache", "replay"})
STAGES = frozenset(
    {
        "query_analysis",
        "embedding",
        "retrieval",
        "rerank",
        "generation",
        "verification",
        "judge",
        "aggregation",
    }
)
IMPLEMENTATION_COMPONENTS = frozenset({*STAGES, "chunking"})
STAGE_REQUIRED_INPUT_FIELDS = {
    "query_analysis": frozenset({"query_hash"}),
    "embedding": frozenset({"content_hash", "text_kind"}),
    "retrieval": frozenset({"normalized_query_hash", "query_representation_hash"}),
    "rerank": frozenset({"query_hash", "candidate_evidence_hash"}),
    "generation": frozenset({"context_history_hash", "evidence_hash"}),
    "verification": frozenset({"draft_hash", "evidence_hash", "scope_hash"}),
    "judge": frozenset({"answer_hash", "evidence_hash"}),
    "aggregation": frozenset({"case_results_hash"}),
}
EXTERNAL_CALL_KINDS = (
    "embedding",
    "normalizer",
    "generation",
    "rerank",
    "judge",
    "other",
)
_SAFE_STAGE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ExperimentContractError(ValueError):
    """Raised when an experiment or cache contract is incomplete or ambiguous."""


class CacheMissError(LookupError):
    """Raised when replay mode cannot find the exact requested artifact."""


class CacheCorruptionError(ValueError):
    """Raised when a cache entry cannot prove its identity and content checksum."""


class CacheConflictError(ValueError):
    """Raised when one exact key would map to two different immutable payloads."""


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize a JSON value deterministically and reject ambiguous values."""

    try:
        _validate_json_value(value)
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return rendered.encode("utf-8")
    except ExperimentContractError:
        raise
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ExperimentContractError("value is not canonical JSON") from exc


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _validate_json_value(value: Any, *, path: str = "$") -> None:
    validate_json_unicode(value)
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ExperimentContractError(f"non-finite number at {path}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ExperimentContractError(
                    f"JSON object key must be a string at {path}"
                )
            _validate_json_value(item, path=f"{path}.{key}")
        return
    raise ExperimentContractError(
        f"unsupported JSON value at {path}: {type(value).__name__}"
    )


def _copy_json(value: Any) -> Any:
    return json.loads(canonical_json_bytes(value).decode("utf-8"))


def _require_mapping(name: str, value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ExperimentContractError(f"{name} must be a mapping")
    copied = _copy_json(dict(value))
    if not isinstance(copied, dict):  # pragma: no cover - guarded by Mapping
        raise ExperimentContractError(f"{name} must be a JSON object")
    return copied


def _require_non_empty_string(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExperimentContractError(f"{name} must be a non-empty string")
    validate_json_unicode(value)
    return value


def _require_boolean(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ExperimentContractError(f"{name} must be a boolean")
    return value


def _require_positive_integer(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ExperimentContractError(f"{name} must be a positive integer")
    return value


def _require_contract_fields(
    contract_name: str,
    contract: Mapping[str, Any],
    fields: set[str],
) -> None:
    missing = sorted(field for field in fields if field not in contract)
    if missing:
        raise ExperimentContractError(
            f"contracts.{contract_name} is missing: {','.join(missing)}"
        )


def _validate_stage_contract_payloads(contracts: dict[str, Any]) -> None:
    chunking = contracts["chunking"]
    _require_contract_fields("chunking", chunking, {"strategy", "version"})
    _require_non_empty_string("contracts.chunking.strategy", chunking["strategy"])
    _require_non_empty_string("contracts.chunking.version", chunking["version"])

    embedding = contracts["embedding"]
    _require_contract_fields(
        "embedding",
        embedding,
        {
            "model",
            "revision",
            "dimension",
            "normalized",
            "query_text_version",
            "document_text_version",
        },
    )
    for field_name in (
        "model",
        "revision",
        "query_text_version",
        "document_text_version",
    ):
        _require_non_empty_string(
            f"contracts.embedding.{field_name}", embedding[field_name]
        )
    _require_positive_integer("contracts.embedding.dimension", embedding["dimension"])
    _require_boolean("contracts.embedding.normalized", embedding["normalized"])

    retrieval = contracts["retrieval"]
    _require_contract_fields(
        "retrieval", retrieval, {"kind", "parameters", "filters", "scope"}
    )
    _require_non_empty_string("contracts.retrieval.kind", retrieval["kind"])
    parameters = _require_mapping(
        "contracts.retrieval.parameters", retrieval["parameters"]
    )
    _require_positive_integer(
        "contracts.retrieval.parameters.top_k", parameters.get("top_k")
    )
    _require_mapping("contracts.retrieval.filters", retrieval["filters"])
    _require_mapping("contracts.retrieval.scope", retrieval["scope"])
    if "query_analysis" in retrieval:
        _require_mapping(
            "contracts.retrieval.query_analysis", retrieval["query_analysis"]
        )

    rerank = contracts["rerank"]
    _require_contract_fields("rerank", rerank, {"enabled", "config"})
    rerank_enabled = _require_boolean("contracts.rerank.enabled", rerank["enabled"])
    _require_mapping("contracts.rerank.config", rerank["config"])
    if rerank_enabled:
        _require_contract_fields(
            "rerank", rerank, {"model", "revision", "prompt_version"}
        )
        for field_name in ("model", "revision", "prompt_version"):
            _require_non_empty_string(
                f"contracts.rerank.{field_name}", rerank[field_name]
            )

    generation = contracts["generation"]
    _require_contract_fields(
        "generation",
        generation,
        {"model", "revision", "prompt_version", "parameters"},
    )
    for field_name in ("model", "revision", "prompt_version"):
        _require_non_empty_string(
            f"contracts.generation.{field_name}", generation[field_name]
        )
    _require_mapping("contracts.generation.parameters", generation["parameters"])

    verification = contracts["verification"]
    _require_contract_fields(
        "verification", verification, {"schema_version", "rules_version"}
    )
    _require_positive_integer(
        "contracts.verification.schema_version", verification["schema_version"]
    )
    _require_non_empty_string(
        "contracts.verification.rules_version", verification["rules_version"]
    )

    judge = contracts["judge"]
    _require_contract_fields("judge", judge, {"enabled"})
    judge_enabled = _require_boolean("contracts.judge.enabled", judge["enabled"])
    if judge_enabled:
        _require_contract_fields(
            "judge", judge, {"model", "revision", "prompt_version", "rules_version"}
        )
        for field_name in ("model", "revision", "prompt_version", "rules_version"):
            _require_non_empty_string(
                f"contracts.judge.{field_name}", judge[field_name]
            )


def _validate_stage_input(stage: str, input_payload: Any) -> dict[str, Any]:
    payload = _require_mapping(f"{stage} input", input_payload)
    required = STAGE_REQUIRED_INPUT_FIELDS[stage]
    missing = sorted(required - set(payload))
    if missing:
        raise ExperimentContractError(
            f"{stage} input is missing identity fields: {','.join(missing)}"
        )
    for field_name in required:
        _require_non_empty_string(f"{stage} input.{field_name}", payload[field_name])
    if stage == "embedding" and payload["text_kind"] not in {"query", "document"}:
        raise ExperimentContractError(
            "embedding input.text_kind must be 'query' or 'document'"
        )
    return payload


def _stage_contracts(
    *,
    code: dict[str, Any],
    corpus: dict[str, Any],
    dataset: dict[str, Any],
    contracts: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    required = {
        "chunking",
        "embedding",
        "retrieval",
        "rerank",
        "generation",
        "verification",
        "judge",
    }
    missing = sorted(required - set(contracts))
    unknown = sorted(set(contracts) - required)
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing={','.join(missing)}")
        if unknown:
            details.append(f"unknown={','.join(unknown)}")
        raise ExperimentContractError("invalid stage contracts: " + "; ".join(details))

    implementation_fingerprints = _require_mapping(
        "code.stage_implementation_fingerprints",
        code.get("stage_implementation_fingerprints"),
    )
    missing_implementations = sorted(
        IMPLEMENTATION_COMPONENTS - set(implementation_fingerprints)
    )
    unknown_implementations = sorted(
        set(implementation_fingerprints) - IMPLEMENTATION_COMPONENTS
    )
    if missing_implementations or unknown_implementations:
        details = []
        if missing_implementations:
            details.append(f"missing={','.join(missing_implementations)}")
        if unknown_implementations:
            details.append(f"unknown={','.join(unknown_implementations)}")
        raise ExperimentContractError(
            "invalid stage implementation fingerprints: " + "; ".join(details)
        )
    for stage, fingerprint in implementation_fingerprints.items():
        _require_non_empty_string(
            f"code.stage_implementation_fingerprints.{stage}", fingerprint
        )
    _validate_stage_contract_payloads(contracts)

    chunking_basis = {
        "implementation_fingerprint": implementation_fingerprints["chunking"],
        "corpus_snapshot_hash": corpus["snapshot_hash"],
        "chunking": contracts["chunking"],
    }
    chunking_fingerprint = canonical_hash(chunking_basis)
    embedding_basis = {
        "implementation_fingerprint": implementation_fingerprints["embedding"],
        "chunking_fingerprint": chunking_fingerprint,
        "embedding": contracts["embedding"],
    }
    embedding_fingerprint = canonical_hash(embedding_basis)
    retrieval_basis = {
        "implementation_fingerprint": implementation_fingerprints["retrieval"],
        "corpus_snapshot_hash": corpus["snapshot_hash"],
        "index_hash": corpus["index_hash"],
        "chunking_fingerprint": chunking_fingerprint,
        "embedding_fingerprint": embedding_fingerprint,
        "retrieval": contracts["retrieval"],
    }
    retrieval_fingerprint = canonical_hash(retrieval_basis)
    bases: dict[str, dict[str, Any]] = {
        "query_analysis": {
            "implementation_fingerprint": implementation_fingerprints["query_analysis"],
            "dataset_schema_version": dataset.get("case_schema_version"),
            "rules": contracts["retrieval"].get("query_analysis", {}),
        },
        "embedding": embedding_basis,
        "retrieval": retrieval_basis,
        "rerank": {
            "implementation_fingerprint": implementation_fingerprints["rerank"],
            "retrieval_contract_fingerprint": retrieval_fingerprint,
            "rerank": contracts["rerank"],
        },
        "generation": {
            "implementation_fingerprint": implementation_fingerprints["generation"],
            "corpus_snapshot_hash": corpus["snapshot_hash"],
            "generation": contracts["generation"],
        },
        "verification": {
            "implementation_fingerprint": implementation_fingerprints["verification"],
            "verification": contracts["verification"],
        },
        "judge": {
            "implementation_fingerprint": implementation_fingerprints["judge"],
            "judge": contracts["judge"],
        },
        "aggregation": {
            "implementation_fingerprint": implementation_fingerprints["aggregation"],
            "dataset_schema_version": dataset.get("case_schema_version"),
            "verification": contracts["verification"],
        },
    }
    return {
        stage: {"fingerprint": canonical_hash(basis), "basis": basis}
        for stage, basis in bases.items()
    }


def _validate_registered_dataset_contract(
    dataset: dict[str, Any],
    *,
    execution_mode: str,
    default_cache_mode: str,
    config: dict[str, Any],
    contracts: dict[str, Any],
) -> dict[str, Any]:
    """Bind a registry-produced dataset to the manifest execution mode."""

    role = dataset["role"]
    registry = dataset.get("registry")
    if registry is None:
        if role != "test_fixture":
            raise ExperimentContractError(
                "non-test datasets must include a verified registry contract"
            )
        return {"contract": "unregistered_test_fixture"}
    if role not in {"legacy_regression", "synthetic_fixture"}:
        raise ExperimentContractError("registered dataset role is unsupported")
    if dataset.get("case_schema_version") != 1:
        raise ExperimentContractError(
            "registered dataset case schema version is unsupported"
        )
    registry_payload = _require_mapping("dataset.registry", registry)
    if set(registry_payload) != {
        "schema_version",
        "file_sha256",
        "frozen_at",
        "immutable",
        "exposure_status",
        "is_holdout",
        "gold_legal_authority_status",
    }:
        raise ExperimentContractError("dataset.registry fields are invalid")
    if (
        type(registry_payload["schema_version"]) is not int
        or registry_payload["schema_version"] != 1
    ):
        raise ExperimentContractError("dataset registry schema version is unsupported")
    for field_name in ("file_sha256",):
        value = registry_payload[field_name]
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ExperimentContractError(
                f"dataset.registry.{field_name} must be a lowercase SHA-256"
            )
    _require_non_empty_string(
        "dataset.registry.frozen_at", registry_payload["frozen_at"]
    )
    if registry_payload["immutable"] is not True:
        raise ExperimentContractError("registered dataset must be immutable")
    if registry_payload["is_holdout"] is not False:
        raise ExperimentContractError(
            "registry schema v1 does not support holdout data"
        )
    expected_exposure = {
        "legacy_regression": "repeated_development",
        "synthetic_fixture": "synthetic",
    }[role]
    if registry_payload["exposure_status"] != expected_exposure:
        raise ExperimentContractError(
            "dataset role and exposure status are inconsistent"
        )
    expected_authority = {
        "legacy_regression": "not_authoritatively_reviewed",
        "synthetic_fixture": "not_applicable_synthetic_content",
    }[role]
    if registry_payload["gold_legal_authority_status"] != expected_authority:
        raise ExperimentContractError(
            "dataset role and legal authority status are inconsistent"
        )

    source = _require_mapping("dataset.source", dataset.get("source"))
    if set(source) != {"path", "file_hash_normalization"}:
        raise ExperimentContractError("dataset.source fields are invalid")
    _require_non_empty_string("dataset.source.path", source["path"])
    if source["file_hash_normalization"] != "utf8_lf_v1":
        raise ExperimentContractError(
            "dataset source file hash normalization is unsupported"
        )

    allowed_modes = dataset.get("allowed_modes")
    if (
        not isinstance(allowed_modes, list)
        or not allowed_modes
        or any(mode not in EXECUTION_MODES for mode in allowed_modes)
        or len(set(allowed_modes)) != len(allowed_modes)
    ):
        raise ExperimentContractError("dataset.allowed_modes is invalid")
    if execution_mode not in allowed_modes:
        raise ExperimentContractError(
            f"registered dataset does not allow execution mode {execution_mode!r}"
        )
    if execution_mode == "offline" and role != "synthetic_fixture":
        raise ExperimentContractError(
            "offline mode requires a registered synthetic_fixture dataset"
        )

    case_count = dataset.get("case_count")
    if (
        isinstance(case_count, bool)
        or not isinstance(case_count, int)
        or case_count < 1
    ):
        raise ExperimentContractError(
            "registered dataset.case_count must be a positive integer"
        )
    cases = dataset.get("cases")
    if not isinstance(cases, list) or len(cases) != case_count:
        raise ExperimentContractError(
            "registered dataset.case_count does not match cases"
        )
    case_set_hash = dataset.get("case_set_hash")
    if (
        not isinstance(case_set_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", case_set_hash)
        or case_set_hash != canonical_hash(cases)
    ):
        raise ExperimentContractError("registered dataset.case_set_hash is invalid")
    artifact_set_hash = dataset.get("case_artifact_set_sha256")
    if not isinstance(artifact_set_hash, str) or not re.fullmatch(
        r"[0-9a-f]{64}", artifact_set_hash
    ):
        raise ExperimentContractError(
            "registered dataset.case_artifact_set_sha256 is invalid"
        )
    case_file_hash = dataset.get("case_file_hash")
    if not isinstance(case_file_hash, str) or not re.fullmatch(
        r"[0-9a-f]{64}", case_file_hash
    ):
        raise ExperimentContractError("registered dataset.case_file_hash is invalid")
    if execution_mode == "smoke-generation" and case_count > 30:
        raise ExperimentContractError(
            "smoke-generation requires a fixed dataset of at most 30 cases"
        )

    policy = REGISTERED_EXECUTION_MODE_POLICIES[execution_mode]
    generate = _require_boolean("config_summary.generate", config.get("generate"))
    expected_generate = policy["generation"] == "required"
    if generate is not expected_generate:
        raise ExperimentContractError(
            f"registered {execution_mode} mode requires "
            f"config_summary.generate={expected_generate!r}"
        )
    judge = _require_mapping("contracts.judge", contracts.get("judge"))
    judge_enabled = _require_boolean("contracts.judge.enabled", judge.get("enabled"))
    if policy["judge"] == "forbidden" and judge_enabled:
        raise ExperimentContractError(
            f"registered {execution_mode} mode forbids judge calls"
        )
    external_calls_allowed = _require_boolean(
        "config_summary.allow_external_calls",
        config.get("allow_external_calls"),
    )
    if policy["external_calls"] == "forbidden" and external_calls_allowed:
        raise ExperimentContractError(
            f"registered {execution_mode} mode forbids external calls"
        )
    if default_cache_mode == "replay" and external_calls_allowed:
        raise ExperimentContractError(
            "registered replay mode must declare zero external calls"
        )
    return {
        "contract": "registered_mode_v1",
        "generation": policy["generation"],
        "judge": policy["judge"],
        "external_calls": policy["external_calls"],
        "generate": generate,
        "judge_enabled": judge_enabled,
        "external_calls_allowed": external_calls_allowed,
    }


def build_experiment_manifest(
    *,
    experiment_id: str,
    execution_mode: str,
    default_cache_mode: str,
    code: Mapping[str, Any],
    config_summary: Mapping[str, Any],
    corpus: Mapping[str, Any],
    dataset: Mapping[str, Any],
    contracts: Mapping[str, Any],
    runtime: Mapping[str, Any],
    environment: Mapping[str, Any],
    created_at: str,
) -> dict[str, Any]:
    """Build the immutable identity portion of one M2 experiment.

    The manifest records environment and the default cache policy, while the
    resume compatibility hash intentionally covers only values that can alter
    outputs. Every case attempt records the cache mode actually used.
    """

    experiment_id = _require_non_empty_string("experiment_id", experiment_id)
    if execution_mode not in EXECUTION_MODES:
        raise ExperimentContractError(f"unsupported execution_mode: {execution_mode!r}")
    if default_cache_mode not in CACHE_MODES:
        raise ExperimentContractError(
            f"unsupported default_cache_mode: {default_cache_mode!r}"
        )
    created_at = _require_non_empty_string("created_at", created_at)
    code_payload = _require_mapping("code", code)
    if not isinstance(code_payload.get("dirty"), bool):
        raise ExperimentContractError("code.dirty must be a boolean")
    commit = _require_non_empty_string("code.commit", code_payload.get("commit"))
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit):
        raise ExperimentContractError("code.commit must be a lowercase Git object ID")
    diff_hash = code_payload.get("diff_hash")
    if code_payload["dirty"]:
        if not isinstance(diff_hash, str) or not re.fullmatch(
            r"[0-9a-f]{64}", diff_hash
        ):
            raise ExperimentContractError(
                "dirty code requires a lowercase SHA-256 code.diff_hash"
            )
    elif diff_hash is not None and (
        not isinstance(diff_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", diff_hash)
    ):
        raise ExperimentContractError(
            "code.diff_hash must be null or a lowercase SHA-256 digest"
        )
    config_payload = _require_mapping("config_summary", config_summary)
    corpus_payload = _require_mapping("corpus", corpus)
    dataset_payload = _require_mapping("dataset", dataset)
    contract_payload = _require_mapping("contracts", contracts)
    for contract_name, contract_value in list(contract_payload.items()):
        contract_payload[contract_name] = _require_mapping(
            f"contracts.{contract_name}", contract_value
        )
    runtime_payload = _require_mapping("runtime", runtime)
    environment_payload = _require_mapping("environment", environment)

    for field_name in ("snapshot_hash", "index_hash"):
        _require_non_empty_string(
            f"corpus.{field_name}", corpus_payload.get(field_name)
        )
    for field_name in ("dataset_id", "role", "case_file_hash"):
        _require_non_empty_string(
            f"dataset.{field_name}", dataset_payload.get(field_name)
        )
    case_schema_version = dataset_payload.get("case_schema_version")
    if (
        isinstance(case_schema_version, bool)
        or not isinstance(case_schema_version, int)
        or case_schema_version < 1
    ):
        raise ExperimentContractError(
            "dataset.case_schema_version must be a positive integer"
        )
    execution_policy = _validate_registered_dataset_contract(
        dataset_payload,
        execution_mode=execution_mode,
        default_cache_mode=default_cache_mode,
        config=config_payload,
        contracts=contract_payload,
    )
    concurrency = runtime_payload.get("concurrency")
    if (
        isinstance(concurrency, bool)
        or not isinstance(concurrency, int)
        or concurrency < 1
    ):
        raise ExperimentContractError("runtime.concurrency must be a positive integer")
    max_retries = runtime_payload.get("max_retries")
    if (
        isinstance(max_retries, bool)
        or not isinstance(max_retries, int)
        or max_retries < 0
    ):
        raise ExperimentContractError(
            "runtime.max_retries must be a non-negative integer"
        )
    if isinstance(runtime_payload.get("random_seed"), bool) or not isinstance(
        runtime_payload.get("random_seed"), int
    ):
        raise ExperimentContractError("runtime.random_seed must be an integer")
    _require_non_empty_string(
        "runtime.timing_scope", runtime_payload.get("timing_scope")
    )

    stage_contracts = _stage_contracts(
        code=code_payload,
        corpus=corpus_payload,
        dataset=dataset_payload,
        contracts=contract_payload,
    )
    compatibility_payload = {
        "execution_mode": execution_mode,
        "execution_policy": execution_policy,
        "code": code_payload,
        "config_hash": canonical_hash(config_payload),
        "corpus": corpus_payload,
        "dataset": dataset_payload,
        "contracts": contract_payload,
        "runtime": runtime_payload,
    }
    resume_hash = canonical_hash(compatibility_payload)
    manifest_core = {
        "manifest_schema_version": EXPERIMENT_MANIFEST_SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "created_at": created_at,
        "execution_mode": execution_mode,
        "execution_policy": execution_policy,
        "cache_policy": {
            "default_mode": default_cache_mode,
            "allowed_modes": sorted(CACHE_MODES),
        },
        "code": code_payload,
        "config": {
            "summary": config_payload,
            "sha256": canonical_hash(config_payload),
        },
        "corpus": corpus_payload,
        "dataset": dataset_payload,
        "contracts": contract_payload,
        "stage_contracts": stage_contracts,
        "runtime": runtime_payload,
        "environment": environment_payload,
        "identity": {
            "resume_compatibility_hash": resume_hash,
        },
    }
    manifest_core["identity"]["manifest_hash"] = canonical_hash(manifest_core)
    return manifest_core


def validate_experiment_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild and compare a manifest so stale stored fingerprints fail closed."""

    payload = _require_mapping("manifest", manifest)
    if (
        type(payload.get("manifest_schema_version")) is not int
        or payload.get("manifest_schema_version") != EXPERIMENT_MANIFEST_SCHEMA_VERSION
    ):
        raise ExperimentContractError("unsupported experiment manifest schema version")
    cache_policy = _require_mapping(
        "manifest.cache_policy", payload.get("cache_policy")
    )
    config = _require_mapping("manifest.config", payload.get("config"))
    try:
        rebuilt = build_experiment_manifest(
            experiment_id=payload.get("experiment_id"),
            execution_mode=payload.get("execution_mode"),
            default_cache_mode=cache_policy.get("default_mode"),
            code=payload.get("code"),
            config_summary=config.get("summary"),
            corpus=payload.get("corpus"),
            dataset=payload.get("dataset"),
            contracts=payload.get("contracts"),
            runtime=payload.get("runtime"),
            environment=payload.get("environment"),
            created_at=payload.get("created_at"),
        )
    except (KeyError, TypeError) as exc:
        raise ExperimentContractError("malformed experiment manifest") from exc
    if canonical_json_bytes(payload) != canonical_json_bytes(rebuilt):
        raise ExperimentContractError(
            "experiment manifest identity or derived fingerprints are inconsistent"
        )
    return rebuilt


def _build_stage_cache_key_validated(
    manifest: Mapping[str, Any],
    stage: str,
    input_payload: Any,
) -> str:
    stage_contract = manifest["stage_contracts"][stage]
    validated_input = _validate_stage_input(stage, input_payload)
    return canonical_hash(
        {
            "cache_schema_version": STAGE_CACHE_SCHEMA_VERSION,
            "stage": stage,
            "contract_fingerprint": stage_contract["fingerprint"],
            "input": validated_input,
        }
    )


def build_stage_cache_key(
    manifest: Mapping[str, Any],
    stage: str,
    input_payload: Any,
) -> str:
    if stage not in STAGES:
        raise ExperimentContractError(f"unsupported stage: {stage!r}")
    manifest_payload = validate_experiment_manifest(manifest)
    stage_contract = manifest_payload.get("stage_contracts", {}).get(stage)
    if not isinstance(stage_contract, dict):
        raise ExperimentContractError(f"manifest is missing the {stage!r} contract")
    fingerprint = stage_contract.get("fingerprint")
    _require_non_empty_string(f"stage_contracts.{stage}.fingerprint", fingerprint)
    return _build_stage_cache_key_validated(manifest_payload, stage, input_payload)


def _empty_external_calls() -> dict[str, int]:
    return {kind: 0 for kind in EXTERNAL_CALL_KINDS}


def _normalize_external_calls(value: Mapping[str, int] | None) -> dict[str, int]:
    result = _empty_external_calls()
    if value is None:
        return result
    unknown = sorted(set(value) - set(EXTERNAL_CALL_KINDS))
    if unknown:
        raise ExperimentContractError(
            "unknown external call kinds: " + ", ".join(unknown)
        )
    for kind, count in value.items():
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ExperimentContractError(
                f"external call count for {kind!r} must be a non-negative integer"
            )
        result[kind] = count
    return result


@dataclass(frozen=True)
class StageValue:
    payload: Any
    external_calls: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class StageExecution:
    stage: str
    requested_mode: str
    origin: str
    cache_key: str
    payload: Any
    payload_sha256: str
    duration_ms: float
    external_calls: dict[str, int]
    source_external_calls: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "requested_mode": self.requested_mode,
            "origin": self.origin,
            "cache_key": self.cache_key,
            "payload": self.payload,
            "payload_sha256": self.payload_sha256,
            "duration_ms": self.duration_ms,
            "external_calls": dict(self.external_calls),
            "source_external_calls": dict(self.source_external_calls),
        }


class ExactStageCache:
    """Content-addressed, immutable cache used by cache and replay modes."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def execute(
        self,
        *,
        manifest: Mapping[str, Any],
        stage: str,
        input_payload: Any,
        mode: str,
        producer: Callable[[], StageValue],
    ) -> StageExecution:
        if mode not in CACHE_MODES:
            raise ExperimentContractError(f"unsupported cache mode: {mode!r}")
        if stage not in STAGES:
            raise ExperimentContractError(f"unsupported stage: {stage!r}")
        manifest_payload = validate_experiment_manifest(manifest)
        cache_key = _build_stage_cache_key_validated(
            manifest_payload, stage, input_payload
        )
        contract_fingerprint = manifest_payload["stage_contracts"][stage]["fingerprint"]
        input_sha256 = canonical_hash(input_payload)
        started = time.perf_counter()
        if mode in {"cache", "replay"}:
            cached = self._read(
                stage,
                cache_key,
                expected_contract_fingerprint=contract_fingerprint,
                expected_input_sha256=input_sha256,
            )
            if cached is not None:
                return StageExecution(
                    stage=stage,
                    requested_mode=mode,
                    origin=mode,
                    cache_key=cache_key,
                    payload=cached["payload"],
                    payload_sha256=cached["payload_sha256"],
                    duration_ms=round((time.perf_counter() - started) * 1000, 3),
                    external_calls=_empty_external_calls(),
                    source_external_calls=_normalize_external_calls(
                        cached.get("source_external_calls")
                    ),
                )
            if mode == "replay":
                raise CacheMissError(
                    f"replay requires an exact {stage} cache hit for key {cache_key}"
                )

        produced = producer()
        if not isinstance(produced, StageValue):
            raise ExperimentContractError("stage producer must return StageValue")
        payload = _copy_json(produced.payload)
        source_external_calls = _normalize_external_calls(produced.external_calls)
        payload_sha256 = canonical_hash(payload)
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        entry_core = {
            "cache_schema_version": STAGE_CACHE_SCHEMA_VERSION,
            "stage": stage,
            "cache_key": cache_key,
            "contract_fingerprint": contract_fingerprint,
            "input_sha256": input_sha256,
            "payload": payload,
            "payload_sha256": payload_sha256,
            "source_external_calls": source_external_calls,
        }
        entry = {**entry_core, "entry_sha256": canonical_hash(entry_core)}
        self._write_immutable(stage, cache_key, entry)
        return StageExecution(
            stage=stage,
            requested_mode=mode,
            origin="fresh",
            cache_key=cache_key,
            payload=payload,
            payload_sha256=payload_sha256,
            duration_ms=duration_ms,
            external_calls=source_external_calls,
            source_external_calls=source_external_calls,
        )

    def _path(self, stage: str, cache_key: str) -> Path:
        if stage not in STAGES or not _SAFE_STAGE.fullmatch(stage):
            raise ExperimentContractError(f"unsafe cache stage: {stage!r}")
        if not re.fullmatch(r"[0-9a-f]{64}", cache_key):
            raise ExperimentContractError(
                "cache key must be a lowercase SHA-256 digest"
            )
        return self.root / stage / f"{cache_key}.json"

    def _read(
        self,
        stage: str,
        cache_key: str,
        *,
        expected_contract_fingerprint: str | None = None,
        expected_input_sha256: str | None = None,
    ) -> dict[str, Any] | None:
        path = self._path(stage, cache_key)
        if not path.exists():
            return None
        try:
            payload = json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=reject_duplicate_object_pairs,
                parse_constant=reject_non_finite_json_constant,
            )
            validate_json_unicode(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise CacheCorruptionError(f"invalid cache entry: {path}") from exc
        if not isinstance(payload, dict):
            raise CacheCorruptionError(f"cache entry root must be an object: {path}")
        expected = {
            "cache_schema_version": STAGE_CACHE_SCHEMA_VERSION,
            "stage": stage,
            "cache_key": cache_key,
        }
        for field_name, expected_value in expected.items():
            if payload.get(field_name) != expected_value:
                raise CacheCorruptionError(
                    f"cache identity mismatch for {field_name}: {path}"
                )
        expected_bindings = {
            "contract_fingerprint": expected_contract_fingerprint,
            "input_sha256": expected_input_sha256,
        }
        for field_name, expected_value in expected_bindings.items():
            if expected_value is not None and payload.get(field_name) != expected_value:
                raise CacheCorruptionError(
                    f"cache identity mismatch for {field_name}: {path}"
                )
        if payload.get("payload_sha256") != canonical_hash(payload.get("payload")):
            raise CacheCorruptionError(f"cache payload checksum mismatch: {path}")
        try:
            _normalize_external_calls(payload.get("source_external_calls"))
        except ExperimentContractError as exc:
            raise CacheCorruptionError(f"invalid cache call ledger: {path}") from exc
        entry_sha256 = payload.get("entry_sha256")
        entry_core = {
            key: value for key, value in payload.items() if key != "entry_sha256"
        }
        if not isinstance(entry_sha256, str) or entry_sha256 != canonical_hash(
            entry_core
        ):
            raise CacheCorruptionError(f"cache envelope checksum mismatch: {path}")
        return payload

    def _write_immutable(
        self,
        stage: str,
        cache_key: str,
        entry: dict[str, Any],
    ) -> None:
        path = self._path(stage, cache_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        expected_contract_fingerprint = entry["contract_fingerprint"]
        expected_input_sha256 = entry["input_sha256"]
        existing = self._read(
            stage,
            cache_key,
            expected_contract_fingerprint=expected_contract_fingerprint,
            expected_input_sha256=expected_input_sha256,
        )
        if existing is not None:
            if canonical_hash(existing) != canonical_hash(entry):
                raise CacheConflictError(
                    f"exact cache key already contains a different payload: {cache_key}"
                )
            return
        data = json.dumps(
            entry,
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
                existing = self._read(
                    stage,
                    cache_key,
                    expected_contract_fingerprint=expected_contract_fingerprint,
                    expected_input_sha256=expected_input_sha256,
                )
                if existing is None or canonical_hash(existing) != canonical_hash(
                    entry
                ):
                    raise CacheConflictError(
                        "concurrent writer stored a different payload for exact "
                        f"cache key: {cache_key}"
                    )
        finally:
            if temp_name and os.path.exists(temp_name):
                os.unlink(temp_name)
