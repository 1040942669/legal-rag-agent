"""Provider-free, once-exposure execution with post-run typed-target audit.

This is not the M2 dataset registry or its canonical scorer. SQLite protects
normal cooperating processes, not a filesystem administrator who deletes the
ledger. A trusted curator/issuer is an external prerequisite, never fabricated.
"""
from __future__ import annotations

import hashlib
import math
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .evaluation_governance import (
    GovernanceError, ReviewTrustPolicy, _digest, _identifier, canonical_bytes,
    content_identity, hash_payload, question_fingerprints, safe_path, strict_json_loads,
    validate_admission, validate_case_payload, validate_protocol,
)
from .models import ANSWER_MODES, SearchResult

_PUBLIC_CANDIDATE_ERRORS = frozenset({
    "invalid_governed_outcome", "invalid_semantic_status", "invalid_structural_status",
    "invalid_observed_behavior", "external_calls_forbidden", "evidence_artifact_invalid",
    "evidence_boundary_invalid", "evidence_payload_drift", "invalid_result_ranking",
    "corpus_file_drift", "invalid_corpus_bundle", "duplicate_corpus_chunks",
    "invalid_utf8_json", "json_too_large", "missing_regular_file", "unsafe_reparse_path",
    "unsafe_path", "path_outside_root", "not_regular_file", "invalid_fields",
})


def _public_candidate_error(error: Exception) -> str:
    # A callback can construct GovernanceError with arbitrary content. Its
    # class alone does not establish that the diagnostic is safe to publish.
    if isinstance(error, GovernanceError) and error.code in _PUBLIC_CANDIDATE_ERRORS:
        return error.code
    return "candidate_execution_failed"


class ExposureLedger:
    """Append-only event ledger with one irreversible content reservation.

    The database location is trusted application configuration, not a protocol
    field. Each operation opens a separate connection. There is no release,
    deletion, reset, replacement or resume operation.
    """
    def __init__(self, path: str | Path, *, root: str | Path):
        self.root = Path(root).absolute()
        self.path = safe_path(path, root=self.root)

    def _connection(self):
        safe_path(self.path, root=self.root)
        connection = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            safe_path(self.path, root=self.root)
            connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            connection.execute("PRAGMA busy_timeout=10000")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("CREATE TABLE IF NOT EXISTS reservations (content_identity TEXT PRIMARY KEY, protocol_hash TEXT NOT NULL, receipt_hash TEXT NOT NULL, reservation_id TEXT UNIQUE NOT NULL, reserved_at TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS exposure_events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, reservation_id TEXT NOT NULL, event_kind TEXT NOT NULL, event_at TEXT NOT NULL, payload_hash TEXT NOT NULL, FOREIGN KEY(reservation_id) REFERENCES reservations(reservation_id))")
            connection.execute("CREATE TABLE IF NOT EXISTS exposed_questions (question_sha256 TEXT PRIMARY KEY, reservation_id TEXT NOT NULL, FOREIGN KEY(reservation_id) REFERENCES reservations(reservation_id))")
            return connection
        except (sqlite3.Error, OSError) as error:
            if connection is not None:
                connection.close()
            raise GovernanceError("exposure_ledger_unavailable") from error

    def reserve(self, content_identity_sha256: str, protocol_sha256: str, receipt_sha256: str, *, question_sha256: tuple[str, ...] = ()) -> str:
        for value in (content_identity_sha256, protocol_sha256, receipt_sha256):
            _digest(value)
        if not isinstance(question_sha256, tuple) or len(set(_digest(value) for value in question_sha256)) != len(question_sha256):
            raise GovernanceError("invalid_question_fingerprints")
        reservation = uuid.uuid4().hex
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM reservations WHERE content_identity=?", (content_identity_sha256,)).fetchone() is not None:
                raise GovernanceError("already_exposed")
            if question_sha256 and connection.execute("SELECT 1 FROM reservations r WHERE NOT EXISTS (SELECT 1 FROM exposed_questions q WHERE q.reservation_id=r.reservation_id)").fetchone() is not None:
                raise GovernanceError("legacy_exposure_binding_incomplete")
            connection.execute("INSERT INTO reservations VALUES (?, ?, ?, ?, ?)",
                               (content_identity_sha256, protocol_sha256, receipt_sha256, reservation, _now()))
            connection.executemany("INSERT INTO exposed_questions VALUES (?, ?)", ((value, reservation) for value in question_sha256))
            connection.execute("INSERT INTO exposure_events (reservation_id, event_kind, event_at, payload_hash) VALUES (?, 'reserved_exposed', ?, ?)",
                               (reservation, _now(), hash_payload({"content_identity": content_identity_sha256, "protocol": protocol_sha256})))
            connection.commit()
            return reservation
        except sqlite3.IntegrityError as error:
            connection.rollback()
            raise GovernanceError("already_exposed") from error
        except GovernanceError:
            connection.rollback()
            raise
        except sqlite3.Error as error:
            connection.rollback()
            raise GovernanceError("exposure_ledger_unavailable") from error
        finally:
            connection.close()

    def append_event(self, reservation_id: str, kind: str, payload: Any) -> None:
        if kind not in {"opened", "completed", "failed", "interrupted"}:
            raise GovernanceError("invalid_exposure_event")
        _identifier(reservation_id)
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM reservations WHERE reservation_id=?", (reservation_id,)).fetchone() is None:
                raise GovernanceError("unknown_reservation")
            connection.execute("INSERT INTO exposure_events (reservation_id, event_kind, event_at, payload_hash) VALUES (?, ?, ?, ?)",
                               (reservation_id, kind, _now(), hash_payload(payload)))
            connection.commit()
        except sqlite3.Error as error:
            connection.rollback()
            raise GovernanceError("exposure_ledger_unavailable") from error
        finally:
            connection.close()

    def reservations(self) -> list[dict[str, str]]:
        connection = self._connection()
        try:
            rows = connection.execute("SELECT content_identity, protocol_hash, receipt_hash, reservation_id, reserved_at FROM reservations ORDER BY reserved_at, reservation_id").fetchall()
            return [dict(zip(("content_identity", "protocol_hash", "receipt_hash", "reservation_id", "reserved_at"), row)) for row in rows]
        finally:
            connection.close()

    def events(self, reservation_id: str) -> list[dict[str, str | int]]:
        connection = self._connection()
        try:
            rows = connection.execute("SELECT sequence, event_kind, event_at, payload_hash FROM exposure_events WHERE reservation_id=? ORDER BY sequence", (reservation_id,)).fetchall()
            return [dict(zip(("sequence", "event_kind", "event_at", "payload_hash"), row)) for row in rows]
        finally:
            connection.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class GovernedQuery:
    """Only this object is given to an execution callback, never EvalCase/gold."""
    opaque_execution_id: str
    question: str
    top_k: int


@dataclass(frozen=True, slots=True)
class GovernedOutcome:
    results: tuple[SearchResult, ...]
    semantic_status: str = "not_checked"
    structural_passed: bool | None = None
    observed_behavior: str | None = None
    external_call_attempts: int = 0

    def __post_init__(self):
        if not isinstance(self.results, tuple) or any(not isinstance(row, SearchResult) for row in self.results):
            raise GovernanceError("invalid_governed_outcome")
        if self.semantic_status not in {"not_checked", "uncertain", "supported", "unsupported", "error"}:
            raise GovernanceError("invalid_semantic_status")
        if self.structural_passed is not None and type(self.structural_passed) is not bool:
            raise GovernanceError("invalid_structural_status")
        if self.observed_behavior is not None and self.observed_behavior not in ANSWER_MODES:
            raise GovernanceError("invalid_observed_behavior")
        if type(self.external_call_attempts) is not int or self.external_call_attempts != 0:
            raise GovernanceError("external_calls_forbidden")


@dataclass(frozen=True, slots=True)
class CandidateExecutor:
    candidate_id: str
    implementation_sha256: str
    config_sha256: str
    execute: Callable[[GovernedQuery], GovernedOutcome]

    def __post_init__(self):
        _identifier(self.candidate_id)
        _digest(self.implementation_sha256)
        _digest(self.config_sha256)
        if not callable(self.execute):
            raise GovernanceError("invalid_candidate_executor")


def _validate_executors(protocol: dict[str, Any], candidates: tuple[CandidateExecutor, ...], *, root: Path, simulation: bool, sealed_path: Path) -> None:
    if not isinstance(candidates, tuple) or any(not isinstance(item, CandidateExecutor) for item in candidates):
        raise GovernanceError("invalid_candidate_executors")
    by_id = {candidate.candidate_id: candidate for candidate in candidates}
    if len(by_id) != len(candidates) or set(by_id) != {candidate["candidate_id"] for candidate in protocol["candidates"]}:
        raise GovernanceError("candidate_set_drift")
    for declared in protocol["candidates"]:
        actual = by_id[declared["candidate_id"]]
        if actual.implementation_sha256 != declared["implementation_sha256"] or actual.config_sha256 != declared["config_sha256"]:
            raise GovernanceError("candidate_identity_drift")
        files = declared["implementation_files"]
        for relative, digest in files.items():
            if (root / relative).absolute() == sealed_path.absolute():
                raise GovernanceError("sealed_path_used_as_implementation")
            source = safe_path(root / relative, root=root, must_exist=True)
            if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
                raise GovernanceError("candidate_implementation_drift")
        if files and hash_payload(files) != declared["implementation_sha256"]:
            raise GovernanceError("candidate_file_set_drift")
        if not files and not simulation:
            raise GovernanceError("production_execution_proof_incomplete")


def typed_pair_audit(case: dict[str, Any], outcome: GovernedOutcome, *, top_k: int, boundary: dict[str, str] | None) -> dict[str, Any]:
    """Supplementary versioned metrics; never calls or alters the v1 scorer."""
    from .legal_references import canonical_article_number, canonical_law_title
    from .evaluation_artifacts import search_result_from_artifact, search_result_to_artifact
    from .retrieval import assert_results_match_boundary
    from .retrieval_contracts import RetrievalBoundary, chunk_payload_fingerprint
    targets = case["targets"]
    try:
        selected = [search_result_from_artifact(search_result_to_artifact(item)) for item in outcome.results[:top_k]]
    except (TypeError, ValueError, RuntimeError) as error:
        raise GovernanceError("evidence_artifact_invalid") from error
    expected_boundary = RetrievalBoundary(**boundary) if boundary is not None else None
    if expected_boundary is None and selected and all(row.provenance is not None for row in selected):
        expected_boundary = selected[0].provenance.boundary
    if expected_boundary is not None:
        try:
            assert_results_match_boundary(selected, expected_boundary, stage="governed typed-pair audit")
        except (TypeError, ValueError, RuntimeError) as error:
            raise GovernanceError("evidence_boundary_invalid") from error
    if not targets:
        return {"typed_hit_at_k": None, "typed_target_coverage": None, "typed_all_required": None,
                "unavailable_reason": "no_typed_retrieval_gold"}
    matches = set()
    for result in selected:
        if result.provenance is None:
            return {"typed_hit_at_k": None, "typed_target_coverage": None, "typed_all_required": None,
                    "unavailable_reason": "typed_provenance_not_available"}
        provenance = result.provenance
        if provenance.chunk_payload_hash != chunk_payload_fingerprint(result.chunk):
            raise GovernanceError("evidence_payload_drift")
        if type(result.rank) is not int or result.rank < 1 or isinstance(result.score, bool) or not isinstance(result.score, (int, float)) or not math.isfinite(result.score):
            raise GovernanceError("invalid_result_ranking")
        for article in provenance.articles:
            for index, target in enumerate(targets):
                if canonical_law_title(article.title) != canonical_law_title(target["law_title"]) or canonical_article_number(article.article_number) != canonical_article_number(target["article_number"]):
                    continue
                if target["law_id"] is not None and target["law_id"] != article.law_id:
                    continue
                if target["version_id"] is not None and target["version_id"] != article.version_id:
                    continue
                matches.add(index)
    return {"typed_hit_at_k": int(bool(matches)), "typed_target_coverage": len(matches) / len(targets),
            "typed_all_required": len(matches) == len(targets), "unavailable_reason": None}


def _freeze_runtime_outcome(outcome: GovernedOutcome) -> GovernedOutcome:
    """Freeze execution facts without accepting any gold or target argument."""
    from .evaluation_artifacts import search_result_from_artifact, search_result_to_artifact
    try:
        results = tuple(search_result_from_artifact(search_result_to_artifact(item)) for item in outcome.results)
    except (TypeError, ValueError, RuntimeError) as error:
        raise GovernanceError("evidence_artifact_invalid") from error
    return replace(outcome, results=results)


def _unknown_metrics(reason: str) -> dict[str, Any]:
    return {"typed_hit_at_k": None, "typed_target_coverage": None, "typed_all_required": None,
            "unavailable_reason": reason}


def _summarize(rows: list[dict[str, Any]], candidates: list[dict[str, Any]], strata: list[str]) -> dict[str, Any]:
    summaries = {}
    for candidate in candidates:
        name = candidate["candidate_id"]
        chosen = [row for row in rows if row["candidate_id"] == name]
        values = [row["metrics"]["typed_hit_at_k"] for row in chosen if row["metrics"]["typed_hit_at_k"] is not None]
        strata_result = {}
        for stratum in strata:
            subset = [row for row in chosen if row["stratum"] == stratum]
            hits = [row["metrics"]["typed_hit_at_k"] for row in subset if row["metrics"]["typed_hit_at_k"] is not None]
            strata_result[stratum] = {"planned": len(subset), "scored": len(hits), "hit_rate": sum(hits) / len(hits) if hits else None}
        summaries[name] = {"planned": len(chosen), "scored": len(values), "unknown_or_no_gold": len(chosen) - len(values),
                           "hit_rate": sum(values) / len(values) if values else None, "strata": strata_result}
    paired = []
    regressions = {stratum: 0 for stratum in strata}
    if len(candidates) == 2:
        left, right = (item["candidate_id"] for item in candidates)
        by_id = {(row["ordinal"], row["candidate_id"]): row for row in rows}
        for ordinal in sorted({row["ordinal"] for row in rows}):
            a, b = by_id[(ordinal, left)]["metrics"]["typed_hit_at_k"], by_id[(ordinal, right)]["metrics"]["typed_hit_at_k"]
            if a is not None and b is not None:
                paired.append(b - a)
                if b < a:
                    regressions[by_id[(ordinal, left)]["stratum"]] += 1
    return {"candidates": summaries, "paired_scored": len(paired),
            "paired_hit_delta": sum(paired) / len(paired) if paired else None,
            "stratum_hit_regressions": regressions}


def _threshold_assessment(summary: dict[str, Any], *, protocol: dict[str, Any], expected_gold_count: int) -> dict[str, Any]:
    thresholds = protocol["thresholds"]
    if len(protocol["candidates"]) != 2:
        return {"status": "not_evaluable", "reason": "requires_two_predeclared_candidates"}
    if expected_gold_count == 0 or summary["paired_scored"] != expected_gold_count:
        return {"status": "not_evaluable", "reason": "paired_typed_metrics_incomplete"}
    passed = (summary["paired_hit_delta"] >= thresholds["minimum_paired_delta"]
              and all(count <= thresholds["maximum_stratum_regressions"] for count in summary["stratum_hit_regressions"].values()))
    return {"status": "passed" if passed else "failed", "reason": None if passed else "predeclared_threshold_not_met",
            "scope": "frozen retrieval thresholds only, not confidence or legal-answer acceptance"}


def execute_protocol(protocol: Any, receipt: Any, duplicate_report: Any, policy: ReviewTrustPolicy, *,
                     sealed_path: str | Path, ledger: ExposureLedger, candidates: tuple[CandidateExecutor, ...],
                     root: str | Path) -> dict[str, Any]:
    """Validate metadata, irreversibly reserve, then open data and execute.

    Callback functions are trusted, provider-free application adapters, not
    sandboxed arbitrary plugins. No callback is constructed from file content.
    Sealed questions and gold are never copied into the returned report.
    """
    frozen = validate_protocol(protocol)
    admission = validate_admission(frozen, receipt, duplicate_report, policy)
    owner = Path(root).absolute()
    _validate_executors(frozen, candidates, root=owner, simulation=admission.simulation, sealed_path=Path(sealed_path))
    # Absolutely no read/stat/hash/parse of sealed_path happens before reserve.
    reservation = ledger.reserve(admission.content_identity_sha256, admission.protocol_sha256, admission.receipt_sha256,
                                 question_sha256=tuple(frozen["dataset"]["question_sha256"]))
    rows = []
    try:
        source = safe_path(sealed_path, root=owner, must_exist=True)
        from .evaluation_governance import MAX_JSON_BYTES
        if source.stat().st_size > MAX_JSON_BYTES:
            raise GovernanceError("json_too_large")
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != frozen["dataset"]["case_file_sha256"]:
            raise GovernanceError("sealed_file_drift")
        dataset = validate_case_payload(strict_json_loads(raw))
        if hash_payload(dataset) != frozen["dataset"]["case_set_sha256"] or content_identity(dataset) != admission.content_identity_sha256 or question_fingerprints(dataset) != sorted(frozen["dataset"]["question_sha256"]) or len(dataset["cases"]) != frozen["dataset"]["case_count"]:
            raise GovernanceError("sealed_case_identity_drift")
        if any(row["stratum"] not in frozen["strata"] for row in dataset["cases"]):
            raise GovernanceError("undeclared_stratum")
        ledger.append_event(reservation, "opened", {"case_set_sha256": hash_payload(dataset)})
        by_id = {candidate.candidate_id: candidate for candidate in candidates}
        completed = []
        for ordinal, case in enumerate(dataset["cases"]):
            # Predeclared balanced candidate order, one score per arm/case.
            declared = frozen["candidates"] if ordinal % 2 == 0 else list(reversed(frozen["candidates"]))
            for candidate in declared:
                query = GovernedQuery(f"{reservation}-{ordinal}-{candidate['candidate_id']}", case["question"], frozen["top_k"])
                try:
                    outcome = by_id[candidate["candidate_id"]].execute(query)
                    if not isinstance(outcome, GovernedOutcome):
                        raise GovernanceError("invalid_governed_outcome")
                    outcome = _freeze_runtime_outcome(outcome)
                    row = {"ordinal": ordinal, "case_id": case["case_id"], "candidate_id": candidate["candidate_id"],
                           "stratum": case["stratum"], "status": "completed", "metrics": _unknown_metrics("postscore_pending"),
                           "semantic_status": outcome.semantic_status, "structural_passed": outcome.structural_passed,
                           "observed_behavior": outcome.observed_behavior, "error_code": None, "external_call_attempts": 0}
                    completed.append((case, outcome, row))
                except Exception as error:
                    reason = _public_candidate_error(error)
                    row = {"ordinal": ordinal, "case_id": case["case_id"], "candidate_id": candidate["candidate_id"],
                           "stratum": case["stratum"], "status": "error", "metrics": _unknown_metrics(reason),
                           "semantic_status": "error", "structural_passed": None, "observed_behavior": None,
                           "error_code": reason, "external_call_attempts": None}
                rows.append(row)
        # Reject a run if frozen local source bytes changed during execution.
        # This is not an attestation of interpreter/dependency administration.
        _validate_executors(frozen, candidates, root=owner, simulation=admission.simulation, sealed_path=Path(sealed_path))
        # Only after every callback finishes does the sidecar receive targets.
        # Prior callbacks cannot see gold-dependent scoring or failure results.
        for case, outcome, row in completed:
            try:
                row["metrics"] = typed_pair_audit(case, outcome, top_k=frozen["top_k"], boundary=frozen["corpus"]["boundary"])
            except Exception as error:
                reason = _public_candidate_error(error)
                row.update(status="error", metrics=_unknown_metrics(reason), semantic_status="error",
                           structural_passed=None, observed_behavior=None, error_code=reason)
        failed = any(row["status"] != "completed" for row in rows)
        summary = _summarize(rows, frozen["candidates"], frozen["strata"])
        result = {"governed_result_schema_version": 1, "metric_rules_version": "typed-pair-audit-v1",
                  "execution_mode": "governed_retrieval_only",
                  "protocol_sha256": admission.protocol_sha256, "receipt_sha256": admission.receipt_sha256,
                  "content_identity_sha256": admission.content_identity_sha256, "reservation_id": reservation,
                "simulation": admission.simulation, "legal_holdout_admitted": admission.legal_holdout_admitted,
                  "legal_quality_accepted": False, "default_promoted": False,
                  "status": "failed" if failed else "completed", "rows": rows,
                  "summary": summary,
                  "threshold_assessment": _threshold_assessment(summary, protocol=frozen,
                                                                 expected_gold_count=sum(bool(row["targets"]) for row in dataset["cases"])),
                  "limits": ["trusted issuer identity is not proof of legal truth or human qualifications",
                             "callbacks are trusted provider-free adapters, not a network sandbox",
                             "callback semantic statuses are observations, not independently validated quality gates",
                             "SQLite cannot protect against a filesystem owner deleting the ledger",
                             "typed target matching is not semantic or legal-answer correctness"]}
        ledger.append_event(reservation, result["status"], {"result_sha256": hash_payload(result)})
        return result
    except BaseException as error:
        kind = "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "failed"
        ledger.append_event(reservation, kind, {"error_code": error.code if isinstance(error, GovernanceError) else "protocol_execution_failed"})
        if isinstance(error, GovernanceError) or isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise GovernanceError("protocol_execution_failed") from error


__all__ = ["CandidateExecutor", "ExposureLedger", "GovernedOutcome", "GovernedQuery", "execute_protocol", "typed_pair_audit"]
