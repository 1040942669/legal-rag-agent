"""Independent provider-free protocol entry; no dotenv, LLM or key generation.

A trusted policy file is explicitly operator supplied. HMAC material is never
printed or included in outputs. Production policy files must be Git ignored;
test-only key material requires --simulation and cannot admit a real holdout.
Corpus execution accepts serialized retrieval evidence, not arbitrary plugins.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from legal_rag.evaluation_governance import (  # noqa: E402
    MAX_JSON_BYTES, GovernanceError, ReviewKey, ReviewTrustPolicy, _object,
    hash_payload, read_safe_json, safe_path, validate_admission, validate_protocol,
    strict_json_loads, write_json_exclusive,
)
from legal_rag.governed_protocol import CandidateExecutor, ExposureLedger, GovernedOutcome, execute_protocol  # noqa: E402

BUILTIN_IMPLEMENTATION_PATHS = (
    "scripts/governed_protocol.py",
    "legal_rag/evaluation_governance.py", "legal_rag/governed_protocol.py",
    "legal_rag/retrieval.py", "legal_rag/retrieval_contracts.py", "legal_rag/models.py",
    "legal_rag/legal_references.py", "legal_rag/evaluation_artifacts.py",
    "legal_rag/experiment_runtime.py", "legal_rag/json_utils.py",
)


def builtin_implementation_files() -> dict[str, str]:
    """Identity of the controlled adapter and sources actually used by it.

    This binds local source bytes, not interpreter packages or arbitrary Python
    execution. Runtime/dependency administration remains a trusted boundary.
    """
    return {relative: hashlib.sha256(safe_path(ROOT / relative, root=ROOT, must_exist=True).read_bytes()).hexdigest()
            for relative in BUILTIN_IMPLEMENTATION_PATHS}


def load_trust_policy(path: Path, *, root: Path, simulation: bool) -> ReviewTrustPolicy:
    target = safe_path(path, root=root, must_exist=True)
    if not simulation:
        checked = subprocess.run(["git", "check-ignore", "--quiet", "--", str(target)], cwd=root,
                                 capture_output=True, check=False, timeout=10)
        if checked.returncode != 0:
            raise GovernanceError("trusted_key_file_must_be_ignored")
    body = _object(read_safe_json(target, root=root), {"trust_schema_version", "algorithm", "keys", "development_pools"})
    if type(body["trust_schema_version"]) is not int or body["trust_schema_version"] != 1 or body["algorithm"] != "hmac-sha256":
        raise GovernanceError("unsupported_trust_policy")
    if not isinstance(body["keys"], list) or not body["keys"]:
        raise GovernanceError("untrusted_review_key")
    keys = []
    for item in body["keys"]:
        _object(item, {"key_id", "shared_key_hex", "reviewer_id", "curator_id", "test_only"})
        try:
            if not isinstance(item["shared_key_hex"], str):
                raise ValueError
            secret = bytes.fromhex(item["shared_key_hex"])
        except ValueError as error:
            raise GovernanceError("invalid_review_key") from error
        keys.append(ReviewKey(item["key_id"], secret, item["reviewer_id"], item["curator_id"], item["test_only"]))
    if simulation and any(not item.test_only for item in keys):
        raise GovernanceError("simulation_requires_test_only_credentials")
    return ReviewTrustPolicy(tuple(keys), body["development_pools"], allow_test_credentials=simulation)


def built_in_candidates(protocol: dict, configurations: dict, *, corpus_path: Path, root: Path,
                        simulation: bool = False) -> tuple[CandidateExecutor, ...]:
    """Build callbacks without reading corpus/sealed data or constructing providers."""
    from legal_rag.evaluation_artifacts import search_result_from_artifact
    from legal_rag.retrieval import BM25Retriever, assert_results_match_boundary
    from legal_rag.retrieval_contracts import RetrievalBoundary
    if not isinstance(configurations, dict) or set(configurations) != {row["candidate_id"] for row in protocol["candidates"]}:
        raise GovernanceError("candidate_set_drift")
    if type(simulation) is not bool:
        raise GovernanceError("invalid_simulation_flag")
    if not simulation:
        if root.absolute() != ROOT.absolute():
            raise GovernanceError("builtin_execution_root_mismatch")
        if any(set(row["implementation_files"]) != set(BUILTIN_IMPLEMENTATION_PATHS) for row in protocol["candidates"]):
            raise GovernanceError("builtin_implementation_proof_incomplete")
        actual_files = builtin_implementation_files()
        for declared in protocol["candidates"]:
            if declared["implementation_files"] != actual_files or declared["implementation_sha256"] != hash_payload(actual_files):
                raise GovernanceError("candidate_implementation_drift")
    result = []
    for declared in protocol["candidates"]:
        config = _object(configurations[declared["candidate_id"]], {"bm25_lexical_profile"})
        if config["bm25_lexical_profile"] not in {"legacy-v1", "generic-v3"}:
            raise GovernanceError("unsupported_builtin_candidate")
        if hash_payload(config) != declared["config_sha256"]:
            raise GovernanceError("candidate_config_drift")
        def callback(query, config=config):
            # First invocation happens only after successful irreversible reserve.
            target = safe_path(corpus_path, root=root, must_exist=True)
            if target.stat().st_size > MAX_JSON_BYTES:
                raise GovernanceError("json_too_large")
            raw = target.read_bytes()
            if hashlib.sha256(raw).hexdigest() != protocol["corpus"]["snapshot_sha256"]:
                raise GovernanceError("corpus_file_drift")
            # Parse the exact bytes whose digest was checked, never a second
            # read that could race a file replacement after hash validation.
            bundle = _object(strict_json_loads(raw), {"corpus_schema_version", "search_results"})
            if type(bundle["corpus_schema_version"]) is not int or bundle["corpus_schema_version"] != 1 or not isinstance(bundle["search_results"], list):
                raise GovernanceError("invalid_corpus_bundle")
            evidence = [search_result_from_artifact(item) for item in bundle["search_results"]]
            by_id = {item.chunk.chunk_id: item for item in evidence}
            if len(by_id) != len(evidence):
                raise GovernanceError("duplicate_corpus_chunks")
            boundary_data = protocol["corpus"]["boundary"]
            boundary = RetrievalBoundary(**boundary_data) if boundary_data is not None else None
            if boundary is not None:
                # The bundle is an unranked corpus; verify each source separately.
                for item in evidence:
                    assert_results_match_boundary([item], boundary, stage="governed builtin corpus")
            retriever = BM25Retriever([item.chunk for item in evidence], lexical_profile=config["bm25_lexical_profile"])
            ranked = []
            for found in retriever.retrieve(query.question, top_k=query.top_k):
                source = by_id[found.chunk.chunk_id]
                trace = dict(found.trace)
                if source.provenance is not None:
                    trace["boundary_fingerprint"] = source.provenance.boundary.fingerprint
                ranked.append(replace(found, provenance=source.provenance, trace=trace))
            if boundary is not None:
                assert_results_match_boundary(ranked, boundary, stage="governed builtin retrieval")
            return GovernedOutcome(tuple(ranked))
        result.append(CandidateExecutor(declared["candidate_id"], declared["implementation_sha256"],
                                        declared["config_sha256"], callback))
    return tuple(result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Versioned governed retrieval protocol, separate from legacy registry and scoring. No live providers.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--inspect", action="store_true", help="Schema only, not admission")
    mode.add_argument("--validate", action="store_true", help="Authenticate metadata only; never reads sealed cases")
    mode.add_argument("--execute", action="store_true", help="Reserve exposure once before opening sealed cases")
    parser.add_argument("--protocol", required=True, type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--duplicate-report", type=Path)
    parser.add_argument("--trusted-policy", type=Path, help="Explicit private ignored MAC-key policy; never taken from protocol")
    parser.add_argument("--simulation", action="store_true", help="Only labelled test keys; never actual legal holdout acceptance")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--sealed-cases", type=Path)
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--candidate-configs", type=Path)
    parser.add_argument("--exposure-ledger", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    root = args.root.absolute()
    def located(value):
        return value if value is None or value.is_absolute() else root / value
    try:
        # Role paths cannot intentionally alias sealed cases before admission.
        if args.sealed_cases is not None:
            sealed = located(args.sealed_cases).absolute()
            inputs = (args.protocol, args.receipt, args.duplicate_report, args.trusted_policy, args.candidate_configs)
            if any(located(value).absolute() == sealed for value in inputs if value is not None):
                raise GovernanceError("sealed_path_used_as_metadata")
        protocol = validate_protocol(read_safe_json(located(args.protocol), root=root))
        if args.inspect:
            print(json.dumps({"status": "schema_valid", "protocol_sha256": hash_payload(protocol),
                              "execution_allowed": False, "legal_holdout_admitted": False}))
            return 0
        if any(value is None for value in (args.receipt, args.duplicate_report, args.trusted_policy)):
            raise GovernanceError("missing_trusted_admission_inputs")
        receipt = read_safe_json(located(args.receipt), root=root)
        duplicates = read_safe_json(located(args.duplicate_report), root=root)
        policy = load_trust_policy(located(args.trusted_policy), root=root, simulation=args.simulation)
        admission = validate_admission(protocol, receipt, duplicates, policy)
        if args.validate:
            print(json.dumps({"status": "simulation_admitted" if admission.simulation else "trusted_metadata_admitted",
                              "execution_allowed": admission.execution_allowed,
                              "legal_holdout_admitted": admission.legal_holdout_admitted,
                              "limits": "Issuer MAC is not proof of human qualifications or legal truth"}))
            return 0
        if any(value is None for value in (args.sealed_cases, args.corpus, args.candidate_configs, args.exposure_ledger, args.output)):
            raise GovernanceError("missing_execution_inputs")
        configs = read_safe_json(located(args.candidate_configs), root=root)
        candidates = built_in_candidates(protocol, configs, corpus_path=located(args.corpus), root=root,
                                         simulation=admission.simulation)
        output = safe_path(located(args.output), root=root)
        if output.exists():
            raise GovernanceError("exclusive_output_failed")
        result = execute_protocol(protocol, receipt, duplicates, policy, sealed_path=located(args.sealed_cases),
                                  ledger=ExposureLedger(located(args.exposure_ledger), root=root),
                                  candidates=candidates, root=root)
        digest = write_json_exclusive(output, result, root=root)
        print(json.dumps({"status": result["status"], "simulation": result["simulation"],
                          "legal_holdout_admitted": result["legal_holdout_admitted"],
                          "legal_quality_accepted": False, "default_promoted": False,
                          "result_sha256": digest, "row_count": len(result["rows"])}))
        return 0 if result["status"] == "completed" else 2
    except (GovernanceError, ValueError, OSError, subprocess.SubprocessError) as error:
        code = error.code if isinstance(error, GovernanceError) else "governed_protocol_failed"
        print(json.dumps({"status": "refused", "error_code": code, "legal_holdout_admitted": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
