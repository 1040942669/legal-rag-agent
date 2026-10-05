from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from legal_rag.evaluation_governance import (
    GovernanceError, build_cross_pool_duplicate_report, canonical_bytes, content_identity,
    hash_payload, question_fingerprints, write_json_exclusive,
)
from legal_rag.governed_protocol import CandidateExecutor, ExposureLedger, GovernedOutcome, execute_protocol, typed_pair_audit
from legal_rag.models import Chunk, SearchResult
from legal_rag.retrieval_contracts import (
    RetrievalBoundary, RetrievalProvenance, RetrievedArticleProvenance, chunk_payload_fingerprint,
)
from test_evaluation_governance import fixture_materials


def _receipt(protocol, receipt, policy):
    value = deepcopy(receipt)
    value["payload"]["protocol_sha256"] = hash_payload(protocol)
    value["mac_sha256"] = hmac.new(policy.keys[0].secret, canonical_bytes(value["payload"]), hashlib.sha256).hexdigest()
    return value


def _executor(callback, candidate_id="candidate-a"):
    return CandidateExecutor(candidate_id, "d" * 64, "e" * 64, callback)


def _bound_result(*, title="虚构甲法", article_number="第一条", law_id="law-a", version_id="version-a", rank=1):
    boundary = RetrievalBoundary("fixture-scope", "fixture-snapshot", "a" * 64)
    article = RetrievedArticleProvenance("article-" + law_id, law_id, version_id, article_number, title,
                                        "2020-01-01", None, "fixtures/fictional.txt", 1, "verified")
    metadata = {"scope_id": boundary.scope_id, "snapshot_id": boundary.snapshot_id,
                "access_scope_ids": [boundary.scope_id], "profile_id": boundary.profile_id,
                "law_ids": [law_id], "version_ids": [version_id], "article_ids": [article.article_id],
                "article_refs": [article.to_metadata()], "boundary_fingerprint": boundary.fingerprint}
    chunk = Chunk("chunk-" + law_id, "仅用于测试的虚构内容", [title], [article_number],
                  [article.source_ref], [1], "article", metadata)
    provenance = RetrievalProvenance(boundary, boundary.scope_id, boundary.snapshot_id, boundary.profile_id,
                                     chunk.chunk_id, "b" * 64, chunk_payload_fingerprint(chunk), rank - 1, None, (article,))
    return SearchResult(chunk, 1.0, rank, "fixture", {"boundary_fingerprint": boundary.fingerprint}, provenance)


def test_valid_bound_pair_is_scored_without_flat_union():
    cases, *_ = fixture_materials()
    found = _bound_result()
    boundary = {"scope_id": found.provenance.scope_id, "snapshot_id": found.provenance.snapshot_id,
                "profile_id": found.provenance.profile_id}
    metrics = typed_pair_audit(cases["cases"][0], GovernedOutcome((found,)), top_k=5, boundary=boundary)
    assert metrics["typed_hit_at_k"] == 1 and metrics["typed_all_required"] is True


def test_rehashed_wrong_metadata_cannot_be_a_typed_witness_without_protocol_boundary():
    cases, *_ = fixture_materials()
    found = _bound_result()
    found.chunk.metadata["snapshot_id"] = "wrong-snapshot"
    found = replace(found, provenance=replace(found.provenance, chunk_payload_hash=chunk_payload_fingerprint(found.chunk)))
    with pytest.raises(GovernanceError):
        typed_pair_audit(cases["cases"][0], GovernedOutcome((found,)), top_k=5, boundary=None)


def test_question_only_callback_and_gold_only_post_execution(tmp_path):
    cases, protocol, receipt, duplicates, policy = fixture_materials()
    sealed = tmp_path / "sealed.json"
    write_json_exclusive(sealed, cases, root=tmp_path)
    received = []
    def callback(query):
        received.append(query)
        assert not hasattr(query, "targets") and not hasattr(query, "expected_law")
        assert not hasattr(query, "case_id")
        return GovernedOutcome(())
    result = execute_protocol(protocol, receipt, duplicates, policy, sealed_path=sealed,
                              ledger=ExposureLedger(tmp_path / "exposure.sqlite3", root=tmp_path),
                              candidates=(_executor(callback),), root=tmp_path)
    assert len(received) == 1
    assert result["status"] == "completed"
    assert result["legal_holdout_admitted"] is False
    assert result["default_promoted"] is False
    assert result["rows"][0]["metrics"]["typed_hit_at_k"] == 0


def test_bad_admission_does_not_open_sealed_data_or_reserve(tmp_path, monkeypatch):
    _, protocol, receipt, duplicates, policy = fixture_materials()
    receipt["mac_sha256"] = "f" * 64
    ledger = ExposureLedger(tmp_path / "exposure.sqlite3", root=tmp_path)
    with pytest.raises(GovernanceError):
        execute_protocol(protocol, receipt, duplicates, policy, sealed_path=tmp_path / "does-not-exist.json",
                         ledger=ledger, candidates=(_executor(lambda query: pytest.fail("dispatch")),), root=tmp_path)
    assert ledger.reservations() == []


def test_changed_case_file_fails_after_reservation_and_stays_consumed(tmp_path):
    cases, protocol, receipt, duplicates, policy = fixture_materials()
    cases["cases"][0]["question"] = "被改变的合成问题"
    sealed = tmp_path / "sealed.json"
    write_json_exclusive(sealed, cases, root=tmp_path)
    ledger = ExposureLedger(tmp_path / "exposure.sqlite3", root=tmp_path)
    with pytest.raises(GovernanceError):
        execute_protocol(protocol, receipt, duplicates, policy, sealed_path=sealed, ledger=ledger,
                         candidates=(_executor(lambda query: pytest.fail("dispatch")),), root=tmp_path)
    assert len(ledger.reservations()) == 1
    protocol["protocol_id"] = "new-protocol"
    with pytest.raises(GovernanceError, match="already_exposed"):
        execute_protocol(protocol, _receipt(protocol, receipt, policy), duplicates, policy,
                         sealed_path=sealed, ledger=ledger,
                         candidates=(_executor(lambda query: pytest.fail("dispatch")),), root=tmp_path)


def test_execution_failure_is_unknown_not_quality_zero_and_cannot_reopen(tmp_path):
    cases, protocol, receipt, duplicates, policy = fixture_materials()
    sealed = tmp_path / "sealed.json"
    write_json_exclusive(sealed, cases, root=tmp_path)
    ledger = ExposureLedger(tmp_path / "exposure.sqlite3", root=tmp_path)
    def callback(query):
        raise RuntimeError("do not leak provider or secret details")
    result = execute_protocol(protocol, receipt, duplicates, policy, sealed_path=sealed, ledger=ledger,
                              candidates=(_executor(callback),), root=tmp_path)
    assert result["status"] == "failed"
    assert result["rows"][0]["metrics"]["typed_hit_at_k"] is None
    assert "do not leak" not in str(result)
    with pytest.raises(GovernanceError, match="already_exposed"):
        execute_protocol(protocol, receipt, duplicates, policy, sealed_path=sealed, ledger=ledger,
                         candidates=(_executor(callback),), root=tmp_path)


def test_distinct_sqlite_connections_reserve_exactly_once(tmp_path):
    path = tmp_path / "exposure.sqlite3"
    def reserve(_):
        ledger = ExposureLedger(path, root=tmp_path)
        try:
            ledger.reserve("a" * 64, "b" * 64, "c" * 64)
            return True
        except GovernanceError as error:
            assert error.code == "already_exposed"
            return False
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(reserve, range(6)))
    assert results.count(True) == 1
    assert len(ExposureLedger(path, root=tmp_path).reservations()) == 1


def test_distinct_processes_reserve_exactly_once(tmp_path):
    path = tmp_path / "process-exposure.sqlite3"
    code = ("import sys; from pathlib import Path; "
            "from legal_rag.governed_protocol import ExposureLedger; "
            "from legal_rag.evaluation_governance import GovernanceError; "
            "store=ExposureLedger(Path(sys.argv[1]),root=Path(sys.argv[2]));\n"
            "try:\n store.reserve('a'*64,'b'*64,'c'*64); sys.exit(0)\n"
            "except GovernanceError as e:\n sys.exit(3 if e.code=='already_exposed' else 4)\n")
    processes = [subprocess.Popen([sys.executable, "-B", "-c", code, str(path), str(tmp_path)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(4)]
    codes = [process.communicate(timeout=30)[0] or process.returncode for process in processes]
    assert codes.count(0) == 1 and codes.count(3) == 3


def test_adding_a_new_case_cannot_reopen_an_already_exposed_question(tmp_path):
    cases, protocol, receipt, duplicates, policy = fixture_materials()
    ledger = ExposureLedger(tmp_path / "overlap.sqlite3", root=tmp_path)
    first = tmp_path / "first.json"
    write_json_exclusive(first, cases, root=tmp_path)
    callback = _executor(lambda query: GovernedOutcome(()))
    execute_protocol(protocol, receipt, duplicates, policy, sealed_path=first, ledger=ledger,
                     candidates=(callback,), root=tmp_path)
    extra = deepcopy(cases["cases"][0])
    extra["case_id"] = "fixture-new"
    extra["question"] = "全新虚构乙法第二条的文本是什么？"
    cases["cases"].append(extra)
    second = tmp_path / "second.json"
    write_json_exclusive(second, cases, root=tmp_path)
    protocol["protocol_id"] = "other-protocol"
    protocol["dataset"].update(case_count=2, case_file_sha256=hashlib.sha256(canonical_bytes(cases)).hexdigest(),
                               case_set_sha256=hash_payload(cases), content_identity_sha256=content_identity(cases),
                               question_sha256=question_fingerprints(cases))
    duplicates = build_cross_pool_duplicate_report(cases, {"fixture-development": ["完全不同的合成练习"]})
    protocol["duplicate_report_sha256"] = hash_payload(duplicates)
    with pytest.raises(GovernanceError, match="already_exposed"):
        execute_protocol(protocol, _receipt(protocol, receipt, policy), duplicates, policy,
                         sealed_path=second, ledger=ledger, candidates=(callback,), root=tmp_path)


def test_callback_cannot_leak_content_by_inventing_a_governance_error_code(tmp_path):
    cases, protocol, receipt, duplicates, policy = fixture_materials()
    sealed = tmp_path / "sealed.json"
    write_json_exclusive(sealed, cases, root=tmp_path)
    def callback(query):
        raise GovernanceError("private-prompt-or-credential-from-callback")
    result = execute_protocol(protocol, receipt, duplicates, policy, sealed_path=sealed,
                              ledger=ExposureLedger(tmp_path / "exposure.sqlite3", root=tmp_path),
                              candidates=(_executor(callback),), root=tmp_path)
    assert result["rows"][0]["error_code"] == "candidate_execution_failed"
    assert "private-prompt" not in json.dumps(result)


def test_builtin_scores_the_same_corpus_bytes_whose_hash_was_checked(tmp_path, monkeypatch):
    from scripts import governed_protocol as cli
    from legal_rag.evaluation_artifacts import search_result_to_artifact
    from legal_rag.governed_protocol import GovernedQuery
    _, protocol, *_ = fixture_materials()
    bundle = {"corpus_schema_version": 1, "search_results": [search_result_to_artifact(_bound_result())]}
    corpus = tmp_path / "corpus.json"
    write_json_exclusive(corpus, bundle, root=tmp_path)
    protocol["corpus"]["snapshot_sha256"] = hashlib.sha256(corpus.read_bytes()).hexdigest()
    configs = {"candidate-a": {"bm25_lexical_profile": "legacy-v1"}}
    protocol["candidates"][0]["config_sha256"] = hash_payload(configs["candidate-a"])
    original = cli.read_safe_json
    def racing_second_read(path, *, root):
        if path == corpus:
            corpus.write_bytes(canonical_bytes({"corpus_schema_version": 1, "search_results": []}))
        return original(path, root=root)
    monkeypatch.setattr(cli, "read_safe_json", racing_second_read)
    candidate = cli.built_in_candidates(protocol, configs, corpus_path=corpus, root=tmp_path, simulation=True)[0]
    outcome = candidate.execute(GovernedQuery("opaque-fixture", "虚构甲法第一条", 5))
    assert len(outcome.results) == 1


def _freeze_cases(cases, protocol, receipt, policy):
    protocol = deepcopy(protocol)
    protocol["dataset"].update(case_count=len(cases["cases"]),
                               case_file_sha256=hashlib.sha256(canonical_bytes(cases)).hexdigest(),
                               case_set_sha256=hash_payload(cases), content_identity_sha256=content_identity(cases),
                               question_sha256=question_fingerprints(cases))
    duplicates = build_cross_pool_duplicate_report(cases, {"fixture-development": ["完全不同的合成练习"]})
    protocol["duplicate_report_sha256"] = hash_payload(duplicates)
    protocol["strata"] = sorted({case["stratum"] for case in cases["cases"]})
    return protocol, _receipt(protocol, receipt, policy), duplicates


def _run_fixture(tmp_path, *, callback=None, two_candidates=False, minimum_delta=0.0, max_regressions=0):
    cases, protocol, receipt, _, policy = fixture_materials()
    extra = deepcopy(cases["cases"][0])
    extra.update(case_id="fixture-b", question="另一个独立虚构问题是什么？", stratum="paraphrase")
    cases["cases"].append(extra)
    if two_candidates:
        protocol["candidates"].append({**protocol["candidates"][0], "candidate_id": "candidate-b"})
    protocol["thresholds"].update(minimum_paired_delta=minimum_delta, maximum_stratum_regressions=max_regressions)
    protocol, receipt, duplicates = _freeze_cases(cases, protocol, receipt, policy)
    sealed = tmp_path / "sealed.json"
    write_json_exclusive(sealed, cases, root=tmp_path)
    ledger = ExposureLedger(tmp_path / "exposure.sqlite3", root=tmp_path)
    executed = []
    def chosen(name):
        def execute(query):
            executed.append((query.question, name))
            return callback(name, query) if callback else GovernedOutcome((_bound_result(),))
        return _executor(execute, name)
    result = execute_protocol(protocol, receipt, duplicates, policy, sealed_path=sealed, ledger=ledger,
                              candidates=tuple(chosen(item["candidate_id"]) for item in protocol["candidates"]), root=tmp_path)
    return result, executed, ledger


def test_exposure_is_reserved_before_any_sealed_path_check_or_read(tmp_path, monkeypatch):
    import legal_rag.governed_protocol as governed
    cases, protocol, receipt, duplicates, policy = fixture_materials()
    sealed = tmp_path / "sealed.json"
    write_json_exclusive(sealed, cases, root=tmp_path)
    ledger = ExposureLedger(tmp_path / "exposure.sqlite3", root=tmp_path)
    original_safe = governed.safe_path
    original_read = Path.read_bytes
    accessed = []
    def safe(path, **kwargs):
        if Path(path) == sealed:
            assert len(ledger.reservations()) == 1
            accessed.append("path-check")
        return original_safe(path, **kwargs)
    def read(path):
        if path == sealed:
            assert len(ledger.reservations()) == 1
            accessed.append("read")
        return original_read(path)
    monkeypatch.setattr(governed, "safe_path", safe)
    monkeypatch.setattr(Path, "read_bytes", read)
    execute_protocol(protocol, receipt, duplicates, policy, sealed_path=sealed, ledger=ledger,
                     candidates=(_executor(lambda query: GovernedOutcome(())),), root=tmp_path)
    assert accessed == ["path-check", "read"]


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_interruption_is_recorded_and_never_releases_exposure(tmp_path, interrupt):
    cases, protocol, receipt, duplicates, policy = fixture_materials()
    sealed = tmp_path / "sealed.json"
    write_json_exclusive(sealed, cases, root=tmp_path)
    ledger = ExposureLedger(tmp_path / "exposure.sqlite3", root=tmp_path)
    def stop(query):
        raise interrupt()
    with pytest.raises(interrupt):
        execute_protocol(protocol, receipt, duplicates, policy, sealed_path=sealed, ledger=ledger,
                         candidates=(_executor(stop),), root=tmp_path)
    reservation = ledger.reservations()[0]["reservation_id"]
    assert [event["event_kind"] for event in ledger.events(reservation)] == ["reserved_exposed", "opened", "interrupted"]
    with pytest.raises(GovernanceError, match="already_exposed"):
        execute_protocol(protocol, receipt, duplicates, policy, sealed_path=sealed, ledger=ledger,
                         candidates=(_executor(stop),), root=tmp_path)


def test_typed_pairs_cannot_borrow_articles_from_the_other_law():
    cases, *_ = fixture_materials()
    case = cases["cases"][0]
    case["targets"].append({"law_title": "虚构乙法", "article_number": "第二条", "law_id": "law-b", "version_id": "version-b"})
    swapped = (_bound_result(article_number="第二条"),
               _bound_result(title="虚构乙法", article_number="第一条", law_id="law-b", version_id="version-b", rank=2))
    metrics = typed_pair_audit(case, GovernedOutcome(swapped), top_k=5, boundary=None)
    assert metrics["typed_hit_at_k"] == 0
    assert metrics["typed_target_coverage"] == 0 and metrics["typed_all_required"] is False
    correct = (_bound_result(), _bound_result(title="虚构乙法", article_number="第二条", law_id="law-b", version_id="version-b", rank=2))
    assert typed_pair_audit(case, GovernedOutcome(correct), top_k=2, boundary=None)["typed_all_required"] is True
    truncated = typed_pair_audit(case, GovernedOutcome(correct), top_k=1, boundary=None)
    assert truncated["typed_target_coverage"] == 0.5 and truncated["typed_all_required"] is False


def test_related_title_is_not_same_law_and_version_constraint_is_respected():
    cases, *_ = fixture_materials()
    case = cases["cases"][0]
    for found in (_bound_result(title="虚构甲法实施细则"), _bound_result(version_id="other-version")):
        assert typed_pair_audit(case, GovernedOutcome((found,)), top_k=5, boundary=None)["typed_hit_at_k"] == 0
    found = _bound_result(title="中华人民共和国虚构甲法", article_number="第1条")
    assert typed_pair_audit(case, GovernedOutcome((found,)), top_k=5, boundary=None)["typed_hit_at_k"] == 1


def test_mixed_untyped_evidence_is_unknown_not_flat_union_success():
    cases, *_ = fixture_materials()
    untyped = SearchResult(Chunk("untyped", "虚构", ["虚构甲法"], ["第一条"], [], [], "article"), 1.0, 2, "fixture")
    metrics = typed_pair_audit(cases["cases"][0], GovernedOutcome((_bound_result(), untyped)), top_k=5, boundary=None)
    assert metrics["typed_hit_at_k"] is None
    assert metrics["unavailable_reason"] == "typed_provenance_not_available"


def test_no_gold_still_cannot_bypass_scope_validation():
    cases, *_ = fixture_materials()
    case = cases["cases"][0]
    case["targets"] = []
    boundary = {"scope_id": "different-scope", "snapshot_id": "fixture-snapshot", "profile_id": "a" * 64}
    with pytest.raises(GovernanceError, match="evidence_boundary_invalid"):
        typed_pair_audit(case, GovernedOutcome((_bound_result(),)), top_k=5, boundary=boundary)


def test_balanced_abba_is_one_measurement_per_arm_and_predeclared_threshold_passes(tmp_path):
    result, executed, _ = _run_fixture(tmp_path, two_candidates=True)
    assert [name for _, name in executed] == ["candidate-a", "candidate-b", "candidate-b", "candidate-a"]
    assert len({(row["ordinal"], row["candidate_id"]) for row in result["rows"]}) == 4
    assert result["summary"]["paired_scored"] == 2
    assert result["summary"]["paired_hit_delta"] == 0
    assert result["threshold_assessment"]["status"] == "passed"
    assert result["legal_quality_accepted"] is False and result["default_promoted"] is False


def test_stratum_regression_fails_even_when_aggregate_delta_is_zero(tmp_path):
    def callback(name, query):
        first = query.question.startswith("虚构甲法")
        hit = first if name == "candidate-a" else not first
        return GovernedOutcome((_bound_result(),) if hit else ())
    result, _, _ = _run_fixture(tmp_path, callback=callback, two_candidates=True)
    assert result["summary"]["paired_hit_delta"] == 0
    assert result["summary"]["stratum_hit_regressions"] == {"explicit": 1, "paraphrase": 0}
    assert result["threshold_assessment"]["status"] == "failed"


def test_failed_pair_is_unknown_excluded_from_paired_denominator_and_blocks_threshold(tmp_path):
    def callback(name, query):
        if name == "candidate-b" and query.question.startswith("虚构甲法"):
            raise RuntimeError("fictional failure")
        return GovernedOutcome((_bound_result(),))
    result, _, _ = _run_fixture(tmp_path, callback=callback, two_candidates=True)
    assert result["status"] == "failed"
    assert result["summary"]["paired_scored"] == 1
    assert result["summary"]["candidates"]["candidate-b"]["scored"] == 1
    assert result["summary"]["candidates"]["candidate-b"]["unknown_or_no_gold"] == 1
    assert result["threshold_assessment"]["status"] == "not_evaluable"


def test_candidate_implementation_drift_refuses_before_reserve_or_sealed_read(tmp_path):
    _, protocol, receipt, duplicates, policy = fixture_materials()
    implementation = tmp_path / "candidate.py"
    implementation.write_bytes(b"# fictional implementation\n")
    files = {"candidate.py": "f" * 64}
    protocol["candidates"][0]["implementation_files"] = files
    protocol["candidates"][0]["implementation_sha256"] = hash_payload(files)
    candidate = CandidateExecutor("candidate-a", hash_payload(files), "e" * 64, lambda query: pytest.fail("dispatch"))
    ledger = ExposureLedger(tmp_path / "exposure.sqlite3", root=tmp_path)
    with pytest.raises(GovernanceError, match="candidate_implementation_drift"):
        execute_protocol(protocol, _receipt(protocol, receipt, policy), duplicates, policy,
                         sealed_path=tmp_path / "absent.json", ledger=ledger, candidates=(candidate,), root=tmp_path)
    assert ledger.reservations() == []


def test_sealed_data_cannot_be_read_as_an_implementation_file_before_reserve(tmp_path):
    _, protocol, receipt, duplicates, policy = fixture_materials()
    protocol["candidates"][0]["implementation_files"] = {"sealed.py": "f" * 64}
    files_hash = hash_payload(protocol["candidates"][0]["implementation_files"])
    protocol["candidates"][0]["implementation_sha256"] = files_hash
    ledger = ExposureLedger(tmp_path / "exposure.sqlite3", root=tmp_path)
    with pytest.raises(GovernanceError, match="sealed_path_used_as_implementation"):
        execute_protocol(protocol, _receipt(protocol, receipt, policy), duplicates, policy,
                         sealed_path=tmp_path / "sealed.py", ledger=ledger,
                         candidates=(CandidateExecutor("candidate-a", files_hash, "e" * 64, lambda query: pytest.fail("dispatch")),), root=tmp_path)
    assert ledger.reservations() == []


def _cli_fixture(tmp_path):
    from legal_rag.evaluation_artifacts import search_result_to_artifact
    cases, protocol, receipt, duplicates, policy = fixture_materials()
    found = _bound_result()
    corpus = {"corpus_schema_version": 1, "search_results": [search_result_to_artifact(found)]}
    protocol["corpus"]["snapshot_sha256"] = hashlib.sha256(canonical_bytes(corpus)).hexdigest()
    protocol["corpus"]["boundary"] = {"scope_id": found.provenance.scope_id,
                                        "snapshot_id": found.provenance.snapshot_id,
                                        "profile_id": found.provenance.profile_id}
    configs = {"candidate-a": {"bm25_lexical_profile": "legacy-v1"},
               "candidate-b": {"bm25_lexical_profile": "generic-v3"}}
    protocol["candidates"][0]["config_sha256"] = hash_payload(configs["candidate-a"])
    protocol["candidates"].append({**protocol["candidates"][0], "candidate_id": "candidate-b",
                                    "config_sha256": hash_payload(configs["candidate-b"])})
    receipt = _receipt(protocol, receipt, policy)
    key = policy.keys[0]
    trusted = {"trust_schema_version": 1, "algorithm": "hmac-sha256",
               "keys": [{"key_id": key.key_id, "shared_key_hex": key.secret.hex(),
                         "reviewer_id": key.reviewer_id, "curator_id": key.curator_id, "test_only": True}],
               "development_pools": dict(policy.development_pools)}
    materials = {"sealed": cases, "protocol": protocol, "receipt": receipt, "duplicates": duplicates,
                 "trust": trusted, "configs": configs, "corpus": corpus}
    paths = {name: tmp_path / (name + ".json") for name in materials}
    for name, payload in materials.items():
        write_json_exclusive(paths[name], payload, root=tmp_path)
    return paths, key.secret.hex()


def _invoke_cli(tmp_path, paths, mode, *, simulation=True, complete=True, output="result.json"):
    script = Path(__file__).resolve().parents[1] / "scripts" / "governed_protocol.py"
    command = [sys.executable, "-B", str(script), mode, "--root", str(tmp_path), "--protocol", str(paths["protocol"])]
    if complete:
        command.extend(["--receipt", str(paths["receipt"]), "--duplicate-report", str(paths["duplicates"]),
                        "--trusted-policy", str(paths["trust"]), "--sealed-cases", str(paths["sealed"]),
                        "--corpus", str(paths["corpus"]), "--candidate-configs", str(paths["configs"]),
                        "--exposure-ledger", str(tmp_path / "exposure.sqlite3"), "--output", str(tmp_path / output)])
    if simulation:
        command.append("--simulation")
    child = subprocess.run(command, cwd=script.parents[1], capture_output=True, text=True, encoding="utf-8", timeout=30)
    return child, json.loads(child.stdout)


def test_cli_help_and_inspection_are_not_admission_and_do_not_open_sealed_data(tmp_path):
    paths, _ = _cli_fixture(tmp_path)
    script = Path(__file__).resolve().parents[1] / "scripts" / "governed_protocol.py"
    help_result = subprocess.run([sys.executable, "-B", str(script), "--help"], capture_output=True,
                                 text=True, encoding="utf-8", timeout=30)
    assert help_result.returncode == 0 and "--simulation" in help_result.stdout
    paths["sealed"].unlink()
    child, status = _invoke_cli(tmp_path, paths, "--inspect")
    assert child.returncode == 0 and status["status"] == "schema_valid"
    assert status["execution_allowed"] is False and status["legal_holdout_admitted"] is False
    assert not (tmp_path / "exposure.sqlite3").exists()


def test_cli_refuses_pending_or_missing_trusted_admission_without_exposure(tmp_path):
    paths, _ = _cli_fixture(tmp_path)
    child, status = _invoke_cli(tmp_path, paths, "--execute", complete=False)
    assert child.returncode == 2 and status["error_code"] == "missing_trusted_admission_inputs"
    protocol = json.loads(paths["protocol"].read_text(encoding="utf-8"))
    protocol["review"]["status"] = "pending"
    paths["protocol"].write_bytes(canonical_bytes(protocol))
    paths["sealed"].unlink()
    child, status = _invoke_cli(tmp_path, paths, "--execute")
    assert child.returncode == 2 and status["error_code"] == "human_review_pending"
    assert not (tmp_path / "exposure.sqlite3").exists()
    print("CLI_PENDING_REFUSAL", child.stdout.strip())


def test_cli_validation_authenticates_metadata_without_unsealing_or_reserving(tmp_path):
    paths, _ = _cli_fixture(tmp_path)
    paths["sealed"].unlink()
    child, status = _invoke_cli(tmp_path, paths, "--validate")
    assert child.returncode == 0 and status["status"] == "simulation_admitted"
    assert status["legal_holdout_admitted"] is False
    assert not (tmp_path / "exposure.sqlite3").exists()


def test_cli_fictional_simulation_runs_both_profiles_once_and_outputs_no_sensitive_content(tmp_path):
    paths, fake_secret = _cli_fixture(tmp_path)
    child, status = _invoke_cli(tmp_path, paths, "--execute")
    assert child.returncode == 0 and status["status"] == "completed"
    assert status["row_count"] == 2 and status["simulation"] is True
    assert status["legal_holdout_admitted"] is False and status["legal_quality_accepted"] is False
    assert status["default_promoted"] is False
    print("CLI_FICTIONAL_SIMULATION", child.stdout.strip())
    raw = (tmp_path / "result.json").read_text(encoding="utf-8")
    result = json.loads(raw)
    assert result["summary"]["paired_scored"] == 1
    assert result["threshold_assessment"]["status"] == "passed"
    assert result["rows"][0]["metrics"]["typed_hit_at_k"] == 1
    for sensitive in (fake_secret, "虚构甲法第一条说明什么", "仅用于测试的虚构内容", "targets", "law_id"):
        assert sensitive not in raw and sensitive not in child.stdout and sensitive not in child.stderr
    second, refused = _invoke_cli(tmp_path, paths, "--execute", output="other-result.json")
    assert second.returncode == 2 and refused["error_code"] == "already_exposed"
    assert not (tmp_path / "other-result.json").exists()
    print("CLI_REPEAT_REFUSAL", second.stdout.strip())


def test_cli_refuses_historical_or_unknown_profile_before_exposure(tmp_path):
    paths, _ = _cli_fixture(tmp_path)
    configs = json.loads(paths["configs"].read_text(encoding="utf-8"))
    configs["candidate-a"]["bm25_lexical_profile"] = "local-lexical-v2"
    paths["configs"].write_bytes(canonical_bytes(configs))
    child, status = _invoke_cli(tmp_path, paths, "--execute")
    assert child.returncode == 2 and status["error_code"] == "unsupported_builtin_candidate"
    assert not (tmp_path / "exposure.sqlite3").exists()


def test_cli_test_credential_never_silently_becomes_production(tmp_path):
    paths, fake_secret = _cli_fixture(tmp_path)
    child, status = _invoke_cli(tmp_path, paths, "--validate", simulation=False)
    assert child.returncode == 2 and status["legal_holdout_admitted"] is False
    assert status["error_code"] == "trusted_key_file_must_be_ignored"
    assert fake_secret not in child.stdout + child.stderr


def test_cli_existing_output_refuses_without_consuming_unopened_exposure(tmp_path):
    paths, _ = _cli_fixture(tmp_path)
    write_json_exclusive(tmp_path / "result.json", {"prior": "immutable"}, root=tmp_path)
    child, status = _invoke_cli(tmp_path, paths, "--execute")
    assert child.returncode == 2 and status["error_code"] == "exclusive_output_failed"
    assert not (tmp_path / "exposure.sqlite3").exists()


def test_cli_sealed_role_cannot_be_aliased_as_metadata_before_admission(tmp_path):
    paths, _ = _cli_fixture(tmp_path)
    paths["sealed"] = paths["protocol"]
    child, status = _invoke_cli(tmp_path, paths, "--execute")
    assert child.returncode == 2 and status["error_code"] == "sealed_path_used_as_metadata"
    assert not (tmp_path / "exposure.sqlite3").exists()


def test_gold_sidecar_scoring_starts_only_after_all_runtime_callbacks_finish(tmp_path, monkeypatch):
    import legal_rag.governed_protocol as governed
    events = []
    original = governed.typed_pair_audit
    def audit(*args, **kwargs):
        events.append("postscore")
        return original(*args, **kwargs)
    def callback(name, query):
        events.append("execute")
        return GovernedOutcome((_bound_result(),))
    monkeypatch.setattr(governed, "typed_pair_audit", audit)
    _run_fixture(tmp_path, callback=callback, two_candidates=True)
    assert events == ["execute"] * 4 + ["postscore"] * 4


def test_runtime_results_are_defensively_frozen_before_later_callbacks_can_mutate_them(tmp_path):
    found = _bound_result()
    calls = 0
    def callback(name, query):
        nonlocal calls
        calls += 1
        if calls == 1:
            return GovernedOutcome((found,))
        found.chunk.metadata["snapshot_id"] = "later-callback-poison"
        return GovernedOutcome(())
    result, _, _ = _run_fixture(tmp_path, callback=callback, two_candidates=True, max_regressions=2)
    first = result["rows"][0]
    assert first["status"] == "completed" and first["metrics"]["typed_hit_at_k"] == 1


def test_builtin_production_cannot_claim_only_an_unrelated_implementation_file():
    from scripts import governed_protocol as cli
    _, protocol, *_ = fixture_materials()
    configs = {"candidate-a": {"bm25_lexical_profile": "legacy-v1"}}
    protocol["candidates"][0]["config_sha256"] = hash_payload(configs["candidate-a"])
    protocol["candidates"][0]["implementation_files"] = {"unrelated.py": "f" * 64}
    with pytest.raises(GovernanceError, match="builtin_implementation_proof_incomplete"):
        cli.built_in_candidates(protocol, configs, corpus_path=cli.ROOT / "unopened-corpus.json", root=cli.ROOT)


def test_builtin_actual_source_identity_is_accepted_without_opening_any_corpus():
    from scripts import governed_protocol as cli
    _, protocol, *_ = fixture_materials()
    files = cli.builtin_implementation_files()
    config = {"bm25_lexical_profile": "generic-v3"}
    protocol["candidates"][0].update(implementation_files=files, implementation_sha256=hash_payload(files),
                                     config_sha256=hash_payload(config))
    candidate = cli.built_in_candidates(protocol, {"candidate-a": config},
                                        corpus_path=cli.ROOT / "unopened-corpus.json", root=cli.ROOT)[0]
    assert candidate.implementation_sha256 == hash_payload(files)
    assert candidate.config_sha256 == hash_payload(config)


def test_builtin_actual_source_hash_drift_is_refused():
    from scripts import governed_protocol as cli
    _, protocol, *_ = fixture_materials()
    files = cli.builtin_implementation_files()
    files["legal_rag/retrieval.py"] = "f" * 64
    config = {"bm25_lexical_profile": "generic-v3"}
    protocol["candidates"][0].update(implementation_files=files, implementation_sha256=hash_payload(files),
                                     config_sha256=hash_payload(config))
    with pytest.raises(GovernanceError, match="candidate_implementation_drift"):
        cli.built_in_candidates(protocol, {"candidate-a": config},
                                corpus_path=cli.ROOT / "unopened-corpus.json", root=cli.ROOT)


def test_source_drift_during_callbacks_is_consumed_and_fails_before_gold_scoring(tmp_path, monkeypatch):
    import legal_rag.governed_protocol as governed
    cases, protocol, receipt, duplicates, policy = fixture_materials()
    implementation = tmp_path / "candidate.py"
    implementation.write_bytes(b"# fictional frozen implementation\n")
    files = {"candidate.py": hashlib.sha256(implementation.read_bytes()).hexdigest()}
    protocol["candidates"][0].update(implementation_files=files, implementation_sha256=hash_payload(files))
    sealed = tmp_path / "sealed.json"
    write_json_exclusive(sealed, cases, root=tmp_path)
    ledger = ExposureLedger(tmp_path / "exposure.sqlite3", root=tmp_path)
    def callback(query):
        implementation.write_bytes(b"# fictional changed implementation\n")
        return GovernedOutcome(())
    monkeypatch.setattr(governed, "typed_pair_audit", lambda *args, **kwargs: pytest.fail("scoring changed code"))
    with pytest.raises(GovernanceError, match="candidate_implementation_drift"):
        execute_protocol(protocol, _receipt(protocol, receipt, policy), duplicates, policy, sealed_path=sealed,
                         ledger=ledger, candidates=(CandidateExecutor("candidate-a", hash_payload(files), "e" * 64, callback),), root=tmp_path)
    reservation = ledger.reservations()[0]["reservation_id"]
    assert [event["event_kind"] for event in ledger.events(reservation)] == ["reserved_exposed", "opened", "failed"]
