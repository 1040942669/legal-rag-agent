"""Versioned evaluation governance, separate from the immutable M2 v1 registry.

MAC verification authenticates a configured issuer, not their humanity, legal
qualification, independence in reality, or the truth of their legal annotation.
No signing interface, credentials loader, model client or automatic approval is
provided here. Test credentials can authorize only explicitly labelled simulation.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import stat
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .json_utils import reject_duplicate_object_pairs, reject_non_finite_json_constant, validate_json_unicode
from .models import ANSWER_MODES

MAX_JSON_BYTES = 16 * 1024 * 1024
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_METRICS = {"typed_hit_at_k", "typed_target_coverage", "typed_all_required"}


class GovernanceError(ValueError):
    """Stable, content-free diagnostic suitable for a public status response."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def canonical_bytes(value: Any) -> bytes:
    try:
        validate_json_unicode(value)
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeError) as error:
        raise GovernanceError("invalid_json_value") from error


def hash_payload(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def strict_json_loads(raw: bytes | str) -> Any:
    try:
        encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
        if not isinstance(encoded, bytes) or len(encoded) > MAX_JSON_BYTES or encoded.startswith(b"\xef\xbb\xbf"):
            raise GovernanceError("invalid_utf8_json")
        value = json.loads(encoded.decode("utf-8"), object_pairs_hook=reject_duplicate_object_pairs,
                           parse_constant=reject_non_finite_json_constant)
        validate_json_unicode(value)
        # Reject exponent overflow such as 1e999, not only NaN literals.
        canonical_bytes(value)
        return value
    except (TypeError, ValueError, RecursionError, UnicodeError) as error:
        raise GovernanceError("invalid_utf8_json") from error


def _object(value: Any, fields: set[str], code: str = "invalid_fields") -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise GovernanceError(code)
    canonical_bytes(value)
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise GovernanceError("invalid_text")
    validate_json_unicode(value)
    return value


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise GovernanceError("invalid_identifier")
    return value


def _digest(value: Any) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise GovernanceError("invalid_digest")
    return value


def _positive(value: Any, maximum: int = 100_000) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise GovernanceError("invalid_positive_integer")
    return value


def _timestamp(value: Any) -> datetime:
    try:
        result = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise GovernanceError("invalid_timestamp")
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError) as error:
        raise GovernanceError("invalid_timestamp") from error


def _string_ids(value: Any, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise GovernanceError("invalid_identifier_list")
    ids = [_identifier(item) for item in value]
    if len(ids) != len(set(ids)):
        raise GovernanceError("duplicate_identifiers")
    return ids


def _is_reparse(path: Path) -> bool:
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def safe_path(path: str | Path, *, root: str | Path, must_exist: bool = False) -> Path:
    """Reject symlinks/junctions in every component, including the trusted root."""
    target = Path(path).absolute()
    owner = Path(root).absolute()
    if not target.is_relative_to(owner):
        raise GovernanceError("path_outside_root")
    try:
        for component in (*reversed(target.parents), target):
            if os.path.lexists(component) and _is_reparse(component):
                raise GovernanceError("unsafe_reparse_path")
        if target.resolve() != target or owner.resolve() != owner:
            raise GovernanceError("unsafe_path")
        if must_exist and not target.is_file():
            raise GovernanceError("missing_regular_file")
        if target.exists() and not target.is_file():
            raise GovernanceError("not_regular_file")
        return target
    except OSError as error:
        raise GovernanceError("unsafe_path") from error


def read_safe_json(path: str | Path, *, root: str | Path) -> Any:
    target = safe_path(path, root=root, must_exist=True)
    try:
        if target.stat().st_size > MAX_JSON_BYTES:
            raise GovernanceError("json_too_large")
        return strict_json_loads(target.read_bytes())
    except OSError as error:
        raise GovernanceError("json_read_failed") from error


def write_json_exclusive(path: str | Path, payload: Any, *, root: str | Path) -> str:
    target = safe_path(path, root=root)
    data = canonical_bytes(payload)
    if len(data) > MAX_JSON_BYTES:
        raise GovernanceError("json_too_large")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        safe_path(target, root=root)
        with target.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as error:
        raise GovernanceError("exclusive_output_failed") from error
    return hashlib.sha256(data).hexdigest()


def validate_case_payload(payload: Any) -> dict[str, Any]:
    result = _object(payload, {"case_schema_version", "cases"})
    if type(result["case_schema_version"]) is not int or result["case_schema_version"] != 1:
        raise GovernanceError("unsupported_case_schema")
    if not isinstance(result["cases"], list) or not result["cases"] or len(result["cases"]) > 100_000:
        raise GovernanceError("invalid_cases")
    ids = []
    for row in result["cases"]:
        _object(row, {"case_id", "question", "stratum", "expected_behavior", "targets"})
        ids.append(_identifier(row["case_id"]))
        _text(row["question"])
        if len(row["question"].encode("utf-8")) > 24_000:
            raise GovernanceError("question_too_large")
        _identifier(row["stratum"])
        if row["expected_behavior"] not in ANSWER_MODES:
            raise GovernanceError("invalid_expected_behavior")
        if not isinstance(row["targets"], list):
            raise GovernanceError("invalid_targets")
        identities = []
        for target in row["targets"]:
            _object(target, {"law_title", "article_number", "law_id", "version_id"})
            _text(target["law_title"])
            _text(target["article_number"])
            for name in ("law_id", "version_id"):
                if target[name] is not None:
                    _identifier(target[name])
            from .legal_references import canonical_article_number, canonical_law_title
            try:
                normalized = dict(target, law_title=canonical_law_title(target["law_title"]),
                                  article_number=canonical_article_number(target["article_number"]))
            except ValueError as error:
                raise GovernanceError("invalid_typed_target") from error
            identities.append(hash_payload(normalized))
        if len(identities) != len(set(identities)):
            raise GovernanceError("duplicate_targets")
        if row["expected_behavior"] == "out_of_scope" and row["targets"]:
            raise GovernanceError("refusal_contains_gold")
        if row["expected_behavior"] == "evidence_answer" and not row["targets"]:
            raise GovernanceError("answer_missing_typed_gold")
    if len(ids) != len(set(ids)):
        raise GovernanceError("duplicate_cases")
    return strict_json_loads(canonical_bytes(result))


def _question_normalized(question: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", _text(question)).casefold() if c.isalnum())


def question_fingerprints(payload: Any) -> list[str]:
    cases = validate_case_payload(payload)["cases"]
    questions = sorted(_question_normalized(row["question"]) for row in cases)
    if any(not question for question in questions) or len(questions) != len(set(questions)):
        raise GovernanceError("duplicate_or_empty_normalized_questions")
    return sorted(hash_payload(question) for question in questions)


def content_identity(payload: Any) -> str:
    # Gold, case IDs, row order, run IDs and protocol IDs cannot reopen exposure.
    return hash_payload({"content_identity_version": 1, "question_sha256": question_fingerprints(payload)})


def _grams(question: str) -> set[str]:
    text = _question_normalized(question)
    return {text} if len(text) < 3 else {text[index:index + 3] for index in range(len(text) - 2)}


def build_cross_pool_duplicate_report(payload: Any, development_pools: Mapping[str, list[str]], *, threshold: float = 0.9) -> dict[str, Any]:
    """Curator-side operation; do not run on sealed developer-unseen questions."""
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold) or not 0 < threshold <= 1:
        raise GovernanceError("invalid_duplicate_threshold")
    cases = validate_case_payload(payload)["cases"]
    pools, pairs = {}, []
    for pool_id, questions in development_pools.items():
        _identifier(pool_id)
        if not isinstance(questions, list) or any(not isinstance(item, str) or not item.strip() for item in questions):
            raise GovernanceError("invalid_development_pool")
        pools[pool_id] = hash_payload(questions)
        for row in cases:
            left = _grams(row["question"])
            for question in questions:
                right = _grams(question)
                union = left | right
                score = len(left & right) / len(union) if union else 1.0
                if score >= threshold:
                    pairs.append({"candidate_question_sha256": hash_payload(_question_normalized(row["question"])),
                                  "pool_id": pool_id, "development_question_sha256": hash_payload(_question_normalized(question)),
                                  "similarity": score})
    return {"duplicate_report_schema_version": 1, "algorithm": "nfkc_alnum_char_trigram_jaccard",
            "threshold": threshold, "content_identity_sha256": content_identity(payload),
            "development_pools": dict(sorted(pools.items())), "near_duplicate_pairs": pairs,
            "unresolved_count": len(pairs), "family_review_complete": False}


@dataclass(frozen=True)
class ReviewKey:
    key_id: str
    secret: bytes = field(repr=False)
    reviewer_id: str
    curator_id: str
    test_only: bool = False

    def __post_init__(self):
        for value in (self.key_id, self.reviewer_id, self.curator_id):
            _identifier(value)
        if not isinstance(self.secret, bytes) or len(self.secret) < 32 or type(self.test_only) is not bool:
            raise GovernanceError("invalid_review_key")


@dataclass(frozen=True)
class ReviewTrustPolicy:
    keys: tuple[ReviewKey, ...]
    development_pools: Mapping[str, str]
    allow_test_credentials: bool = False

    def __post_init__(self):
        if not isinstance(self.keys, tuple) or any(not isinstance(key, ReviewKey) for key in self.keys):
            raise GovernanceError("invalid_trust_policy")
        if len({key.key_id for key in self.keys}) != len(self.keys) or type(self.allow_test_credentials) is not bool:
            raise GovernanceError("invalid_trust_policy")
        if not isinstance(self.development_pools, Mapping):
            raise GovernanceError("invalid_trust_policy")
        detached = {_identifier(name): _digest(value) for name, value in self.development_pools.items()}
        object.__setattr__(self, "development_pools", MappingProxyType(detached))


@dataclass(frozen=True)
class Admission:
    protocol_sha256: str
    content_identity_sha256: str
    receipt_sha256: str
    simulation: bool
    execution_allowed: bool = True

    @property
    def legal_holdout_admitted(self) -> bool:
        return not self.simulation


def validate_protocol(payload: Any) -> dict[str, Any]:
    value = _object(payload, {"protocol_schema_version", "protocol_id", "dataset", "corpus", "review",
                            "duplicate_report_sha256", "exposure", "frozen_at", "developer_ids", "candidates",
                            "metrics", "strata", "thresholds", "budget", "top_k"})
    if type(value["protocol_schema_version"]) is not int or value["protocol_schema_version"] != 1:
        raise GovernanceError("unsupported_protocol_schema")
    _identifier(value["protocol_id"])
    dataset = _object(value["dataset"], {"dataset_id", "role", "case_file_sha256", "case_set_sha256", "content_identity_sha256", "question_sha256", "case_count"})
    _identifier(dataset["dataset_id"])
    if dataset["role"] != "sealed_holdout":
        raise GovernanceError("not_sealed_holdout")
    for name in ("case_file_sha256", "case_set_sha256", "content_identity_sha256"):
        _digest(dataset[name])
    _positive(dataset["case_count"])
    questions = dataset["question_sha256"]
    if not isinstance(questions, list) or len(questions) != dataset["case_count"] or len(set(_digest(item) for item in questions)) != len(questions):
        raise GovernanceError("invalid_question_fingerprints")
    if dataset["content_identity_sha256"] != hash_payload({"content_identity_version": 1, "question_sha256": sorted(questions)}):
        raise GovernanceError("content_fingerprint_binding_invalid")
    corpus = _object(value["corpus"], {"snapshot_sha256", "source_sha256", "license_sha256", "as_of", "boundary"})
    for name in ("snapshot_sha256", "source_sha256", "license_sha256"):
        _digest(corpus[name])
    try:
        date.fromisoformat(_text(corpus["as_of"]))
    except ValueError as error:
        raise GovernanceError("invalid_corpus_date") from error
    if corpus["boundary"] is not None:
        from .retrieval_contracts import RetrievalBoundary
        boundary = _object(corpus["boundary"], {"scope_id", "snapshot_id", "profile_id"})
        try:
            RetrievalBoundary(**boundary)
        except ValueError as error:
            raise GovernanceError("invalid_boundary") from error
    review = _object(value["review"], {"status", "human_review_complete", "legal_currentness_reviewed", "license_review_complete"})
    if review["status"] not in {"pending", "approved", "rejected"} or any(type(review[name]) is not bool for name in review if name != "status"):
        raise GovernanceError("invalid_review")
    _digest(value["duplicate_report_sha256"])
    if value["exposure"] not in {"sealed_unexposed", "exposed", "repeated_development"}:
        raise GovernanceError("invalid_exposure")
    _timestamp(value["frozen_at"])
    _string_ids(value["developer_ids"])
    if not isinstance(value["candidates"], list) or not value["candidates"] or len(value["candidates"]) > 16:
        raise GovernanceError("invalid_candidates")
    candidate_ids = []
    for candidate in value["candidates"]:
        _object(candidate, {"candidate_id", "implementation_sha256", "config_sha256", "implementation_files"})
        candidate_ids.append(_identifier(candidate["candidate_id"]))
        _digest(candidate["implementation_sha256"])
        _digest(candidate["config_sha256"])
        if not isinstance(candidate["implementation_files"], dict):
            raise GovernanceError("invalid_implementation_files")
        for relative, digest in candidate["implementation_files"].items():
            if not isinstance(relative, str) or "\\" in relative or relative.startswith("/") or ":" in relative or any(part in {"", ".", ".."} for part in relative.split("/")) or not relative.endswith(".py"):
                raise GovernanceError("unsafe_implementation_path")
            _digest(digest)
    if len(candidate_ids) != len(set(candidate_ids)):
        raise GovernanceError("duplicate_candidates")
    if set(_string_ids(value["metrics"])) != _METRICS:
        raise GovernanceError("unsupported_metrics")
    _string_ids(value["strata"])
    thresholds = _object(value["thresholds"], {"promotion_requested", "minimum_paired_delta", "maximum_stratum_regressions"})
    if thresholds["promotion_requested"] is not False:
        raise GovernanceError("automatic_promotion_forbidden")
    delta = thresholds["minimum_paired_delta"]
    if isinstance(delta, bool) or not isinstance(delta, (int, float)) or not math.isfinite(delta) or not -1 <= delta <= 1:
        raise GovernanceError("invalid_threshold")
    if type(thresholds["maximum_stratum_regressions"]) is not int or thresholds["maximum_stratum_regressions"] < 0:
        raise GovernanceError("invalid_threshold")
    if _object(value["budget"], {"external_calls"})["external_calls"] != 0 or type(value["budget"]["external_calls"]) is not int:
        raise GovernanceError("external_calls_forbidden")
    _positive(value["top_k"], 20)
    return strict_json_loads(canonical_bytes(value))


def validate_admission(protocol: Any, receipt: Any, duplicate_report: Any, policy: ReviewTrustPolicy) -> Admission:
    value = validate_protocol(protocol)
    if not isinstance(policy, ReviewTrustPolicy):
        raise GovernanceError("invalid_trust_policy")
    if value["review"]["status"] != "approved" or not all(value["review"][name] is True for name in value["review"] if name != "status"):
        raise GovernanceError("human_review_pending")
    if value["exposure"] != "sealed_unexposed":
        raise GovernanceError("already_exposed")
    envelope = _object(receipt, {"receipt_schema_version", "key_id", "payload", "mac_sha256"})
    if type(envelope["receipt_schema_version"]) is not int or envelope["receipt_schema_version"] != 1:
        raise GovernanceError("unsupported_receipt_schema")
    key = next((item for item in policy.keys if item.key_id == envelope["key_id"]), None)
    if key is None:
        raise GovernanceError("untrusted_review_key")
    body = _object(envelope["payload"], {"purpose", "protocol_sha256", "reviewer_id", "curator_id", "independent", "reviewed_at", "expires_at", "test_only"})
    signature = _digest(envelope["mac_sha256"])
    expected = hmac.new(key.secret, canonical_bytes(body), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise GovernanceError("review_mac_invalid")
    if body["purpose"] != "legal_holdout_admission" or body["protocol_sha256"] != hash_payload(value):
        raise GovernanceError("review_binding_invalid")
    if body["reviewer_id"] != key.reviewer_id or body["curator_id"] != key.curator_id or body["independent"] is not True:
        raise GovernanceError("review_identity_invalid")
    if key.reviewer_id in value["developer_ids"] or key.curator_id in value["developer_ids"]:
        raise GovernanceError("review_not_independent")
    if type(body["test_only"]) is not bool or body["test_only"] != key.test_only or key.test_only and not policy.allow_test_credentials:
        raise GovernanceError("test_credentials_forbidden")
    now = datetime.now(timezone.utc)
    reviewed, expires, frozen = _timestamp(body["reviewed_at"]), _timestamp(body["expires_at"]), _timestamp(value["frozen_at"])
    if reviewed > now or frozen > now or not reviewed < expires or expires <= now:
        raise GovernanceError("review_expired_or_future")
    report = _object(duplicate_report, {"duplicate_report_schema_version", "algorithm", "threshold", "content_identity_sha256",
                                       "development_pools", "near_duplicate_pairs", "unresolved_count", "family_review_complete"})
    if hash_payload(report) != value["duplicate_report_sha256"] or report["content_identity_sha256"] != value["dataset"]["content_identity_sha256"]:
        raise GovernanceError("duplicate_report_binding_invalid")
    if type(report["duplicate_report_schema_version"]) is not int or report["duplicate_report_schema_version"] != 1 or report["algorithm"] != "nfkc_alnum_char_trigram_jaccard":
        raise GovernanceError("unsupported_duplicate_report")
    threshold = report["threshold"]
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold) or not 0 < threshold <= 0.9:
        raise GovernanceError("weakened_duplicate_threshold")
    if not policy.development_pools or report["development_pools"] != dict(policy.development_pools):
        raise GovernanceError("development_pool_omitted_or_drifted")
    if report["near_duplicate_pairs"] != [] or type(report["unresolved_count"]) is not int or report["unresolved_count"] != 0:
        raise GovernanceError("near_duplicate_unresolved")
    if type(report["family_review_complete"]) is not bool:
        raise GovernanceError("invalid_duplicate_review_state")
    if report["family_review_complete"] is not True and not key.test_only:
        raise GovernanceError("family_review_pending")
    if not key.test_only and (value["corpus"]["boundary"] is None or any(not item["implementation_files"] for item in value["candidates"])):
        raise GovernanceError("production_execution_proof_incomplete")
    return Admission(hash_payload(value), value["dataset"]["content_identity_sha256"], hash_payload(envelope), key.test_only)


def prepare_review_packet(dataset_id: str, *, repository_root: str | Path) -> dict[str, Any]:
    from .evaluation_artifacts import eval_case_to_artifact
    from .experiment_datasets import default_dataset_registry_path, load_dataset_registry
    registry = load_dataset_registry(default_dataset_registry_path(repository_root), repository_root=repository_root)
    dataset = registry.dataset(dataset_id)
    rows = [{"case_id": case.case_id, "question": case.question, "original_case_sha256": hash_payload(eval_case_to_artifact(case)),
             "legacy_expected_law": case.expected_law, "legacy_expected_articles": list(case.expected_articles),
             "expected_behavior": case.resolved_expected_behavior, "proposed_typed_targets": [],
             "review_state": "pending", "reviewer_id": None, "reviewed_at": None, "decision": None,
             "source_references": [], "rationale": None} for case in dataset.cases]
    packet = {"packet_schema_version": 1, "artifact_kind": "legal_gold_review_packet", "source_dataset_id": dataset_id,
              "source_role": dataset.entry.role, "source_exposure": dict(dataset.entry.exposure),
              "source_registry_sha256": registry.file_sha256, "source_case_file_sha256": dataset.entry.file_sha256,
              "created_at": datetime.now(timezone.utc).isoformat(), "human_review_complete": False,
              "holdout_admitted": False, "legal_currentness_status": "not_verified", "cases": rows}
    return validate_review_packet(packet)


def validate_review_packet(payload: Any) -> dict[str, Any]:
    value = _object(payload, {"packet_schema_version", "artifact_kind", "source_dataset_id", "source_role", "source_exposure",
                            "source_registry_sha256", "source_case_file_sha256", "created_at", "human_review_complete",
                            "holdout_admitted", "legal_currentness_status", "cases"})
    if type(value["packet_schema_version"]) is not int or value["packet_schema_version"] != 1 or value["artifact_kind"] != "legal_gold_review_packet":
        raise GovernanceError("unsupported_review_packet")
    _identifier(value["source_dataset_id"])
    if value["source_role"] not in {"legacy_regression", "synthetic_fixture"}:
        raise GovernanceError("invalid_packet_role")
    _object(value["source_exposure"], {"status", "is_holdout"})
    if value["source_exposure"]["is_holdout"] is not False:
        raise GovernanceError("legacy_cannot_be_holdout")
    expected_exposure = "repeated_development" if value["source_role"] == "legacy_regression" else "synthetic"
    if value["source_exposure"]["status"] != expected_exposure:
        raise GovernanceError("invalid_packet_exposure")
    for name in ("source_registry_sha256", "source_case_file_sha256"):
        _digest(value[name])
    _timestamp(value["created_at"])
    if value["human_review_complete"] is not False or value["holdout_admitted"] is not False or value["legal_currentness_status"] != "not_verified":
        raise GovernanceError("packet_cannot_self_approve")
    if not isinstance(value["cases"], list) or not value["cases"]:
        raise GovernanceError("invalid_packet_cases")
    ids = []
    for row in value["cases"]:
        _object(row, {"case_id", "question", "original_case_sha256", "legacy_expected_law", "legacy_expected_articles",
                      "expected_behavior", "proposed_typed_targets", "review_state", "reviewer_id", "reviewed_at", "decision",
                      "source_references", "rationale"})
        ids.append(_identifier(row["case_id"]))
        _text(row["question"])
        _digest(row["original_case_sha256"])
        if not isinstance(row["legacy_expected_law"], str) or not isinstance(row["legacy_expected_articles"], list) or any(not isinstance(item, str) for item in row["legacy_expected_articles"]):
            raise GovernanceError("invalid_legacy_gold")
        if row["expected_behavior"] not in ANSWER_MODES:
            raise GovernanceError("invalid_expected_behavior")
        if row["proposed_typed_targets"] != [] or row["source_references"] != [] or row["review_state"] != "pending" or any(row[name] is not None for name in ("reviewer_id", "reviewed_at", "decision", "rationale")):
            raise GovernanceError("packet_cannot_self_approve")
    if len(ids) != len(set(ids)):
        raise GovernanceError("duplicate_packet_cases")
    return strict_json_loads(canonical_bytes(value))
