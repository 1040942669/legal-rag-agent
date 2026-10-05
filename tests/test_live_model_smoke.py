from __future__ import annotations

import importlib.util
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from legal_rag.chunking import article_chunks, save_chunks
from legal_rag.data import parse_law_file
from legal_rag.llm import CompletionUsage

_PATH = Path(__file__).resolve().parents[1] / "scripts/live_model_smoke.py"
_SPEC = importlib.util.spec_from_file_location("live_model_smoke", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
smoke = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(smoke)


@pytest.fixture
def public_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    case_path = tmp_path / smoke.CASE_RELATIVE_PATH
    case_path.parent.mkdir(parents=True)
    case_path.write_bytes((smoke.REPOSITORY_ROOT / smoke.CASE_RELATIVE_PATH).read_bytes())
    corpus = tmp_path / smoke.CORPUS_RELATIVE_PATH
    corpus.mkdir(parents=True)
    source = corpus / "中华人民共和国专利法.txt"
    source.write_text(
        "《中华人民共和国专利法》第四十二条规定，发明专利权的期限为二十年，自申请日起计算。\n",
        encoding="utf-8",
    )
    save_chunks(article_chunks(parse_law_file(source)), tmp_path / smoke.INDEX_RELATIVE_PATH)
    monkeypatch.setattr(smoke, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(smoke, "_code_identity", lambda root: {"head": "a" * 40})
    monkeypatch.setattr(
        smoke.chat_module,
        "STRUCTURED_QA_PROMPT_VERSION",
        "m1-structured-qa-citation-alignment-v2",
        raising=False,
    )
    return tmp_path


class FakeProvider:
    hidden_retries_disabled = True

    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)
        self.usage = CompletionUsage()
        self.last_response_metadata = {}
        self.prompts: list[str] = []
        self.failure = None
        self.response_override = None
        self.metadata_override = None

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        self.usage.calls += 1
        if self.failure:
            self.usage.failed_calls += 1
            raise self.failure
        self.usage.record_tokens(input_tokens=120, output_tokens=60, total_tokens=180)
        self.last_response_metadata = {
            "prompt_tokens": 120,
            "completion_tokens": 60,
            "total_tokens": 180,
            "returned_model_matches": True,
            "finish_reason": "stop",
            "reasoning_content_reported": True,
            "reasoning_content_nonempty": False,
            "reasoning_tokens": 0,
        }
        if self.metadata_override:
            self.last_response_metadata.update(self.metadata_override)
        if self.response_override is not None:
            return self.response_override
        if prompt == smoke.PROBE_PROMPT:
            return '{"ok": true}'
        return json.dumps(
            {
                "answer_text": "发明专利权的期限为二十年 [S1]。",
                "answer_mode": "evidence_answer",
                "claims": [
                    {
                        "claim_id": "C1",
                        "text": "发明专利权的期限为二十年",
                        "source_ids": ["S1"],
                    }
                ],
                "limitations": [],
                "clarification_question": None,
            },
            ensure_ascii=False,
        )


@pytest.fixture
def fake_provider(monkeypatch: pytest.MonkeyPatch) -> list[FakeProvider]:
    monkeypatch.setenv("ALLOW_LIVE_MODEL_CALLS", "true")
    providers: list[FakeProvider] = []

    def factory(**kwargs):
        provider = FakeProvider(**kwargs)
        providers.append(provider)
        return provider

    monkeypatch.setattr(smoke, "SiliconFlowClient", factory)
    return providers


def test_default_preflight_never_initializes_provider_or_reads_dotenv(
    public_fixture: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import legal_rag.env

    def forbidden(*args, **kwargs):
        raise AssertionError("provider or dotenv accessed by preflight")

    monkeypatch.setattr(smoke, "SiliconFlowClient", forbidden)
    monkeypatch.setattr(legal_rag.env, "load_dotenv", forbidden)
    monkeypatch.setenv("ALLOW_LIVE_MODEL_CALLS", "true")
    result = smoke.run_smoke(run_id="live_smoke_preflight")
    assert result["status"] == "preflight_passed"
    assert result["live_model_calls"] == 0
    assert result["provider_initialized"] is False
    assert not (public_fixture / smoke.AUTHORIZATION_LEDGER).exists()
    manifest = json.loads(
        (public_fixture / result["artifact_dir"] / "manifest.json").read_text(encoding="utf-8")
    )
    assert len(manifest["rag_cases"]) == 9
    assert {item["case_type"] for item in manifest["rag_cases"]} >= {
        "article_lookup", "semantic_scenario", "hard_negative", "multi_article", "refusal"
    }
    assert manifest["dataset_role"] == "legacy_regression_not_holdout"
    assert manifest["sources"]["legal_validity"] == "not_verified"


@pytest.mark.parametrize("gate", [None, "false", "0", "anything"])
def test_execute_requires_process_opt_in_before_preflight(
    monkeypatch: pytest.MonkeyPatch, gate: str | None
) -> None:
    if gate is None:
        monkeypatch.delenv("ALLOW_LIVE_MODEL_CALLS", raising=False)
    else:
        monkeypatch.setenv("ALLOW_LIVE_MODEL_CALLS", gate)
    monkeypatch.setattr(smoke, "prepare_smoke", lambda *args: pytest.fail("preflight ran"))
    with pytest.raises(smoke.SmokeError, match="live_opt_in_required"):
        smoke.run_smoke(execute=True, run_id="live_smoke_blocked")


def test_execute_requires_explicit_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_LIVE_MODEL_CALLS", "true")
    with pytest.raises(smoke.SmokeError, match="explicit_execute_run_id_required"):
        smoke.run_smoke(execute=True)


def test_frozen_dataset_modification_is_rejected_before_provider(
    public_fixture: Path, fake_provider: list[FakeProvider]
) -> None:
    path = public_fixture / smoke.CASE_RELATIVE_PATH
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(smoke.SmokeError, match="case_file_changed"):
        smoke.run_smoke(execute=True, run_id="live_smoke_changed")
    assert fake_provider == []


def test_index_content_and_external_source_are_rejected(public_fixture: Path) -> None:
    path = public_fixture / smoke.INDEX_RELATIVE_PATH
    original = json.loads(path.read_text(encoding="utf-8"))
    tampered = {**original, "text": "unauthorized private material"}
    path.write_text(json.dumps(tampered, ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(smoke.SmokeError, match="index_content_not_in_local_source"):
        smoke.prepare_smoke(public_fixture)
    tampered = {**original, "source_files": [str(public_fixture / "private.txt")]}
    path.write_text(json.dumps(tampered, ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(smoke.SmokeError, match="index_source_not_allowed"):
        smoke.prepare_smoke(public_fixture)


def test_generated_prompt_uses_basename_not_machine_path(public_fixture: Path) -> None:
    _, items = smoke.prepare_smoke(public_fixture)
    first = items[0]
    provider = FakeProvider()
    first["assistant"].llm = provider
    generated = first["assistant"].generate_turn(first["retrieved"], generate=True)
    assert generated.kind == "model"
    prompt = provider.prompts[0]
    assert "中华人民共和国专利法.txt" in prompt
    assert str(public_fixture) not in prompt
    assert "Chinese-Laws/Chinese-Laws" not in prompt


@pytest.mark.parametrize("run_id", ["../escape", "live_smoke_../escape", "private", "live_smoke_"])
def test_artifact_identity_is_confined(public_fixture: Path, run_id: str) -> None:
    with pytest.raises(smoke.SmokeError, match="invalid_run_id"):
        smoke._new_output_directory(public_fixture, run_id)


def test_existing_output_cannot_be_overwritten(public_fixture: Path) -> None:
    smoke.run_smoke(run_id="live_smoke_same")
    with pytest.raises(FileExistsError):
        smoke.run_smoke(run_id="live_smoke_same")


def test_success_reuses_existing_generation_verification_and_scoring(
    public_fixture: Path,
    fake_provider: list[FakeProvider],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata, items = smoke.prepare_smoke(public_fixture)
    monkeypatch.setattr(smoke, "prepare_smoke", lambda *args: (metadata, items[:1]))
    result = smoke.run_smoke(execute=True, run_id="live_smoke_success")
    assert result["status"] == "completed"
    assert result["probe_status"] == "passed"
    assert result["live_model_calls"] == 2
    assert result["rag_cases_completed"] == 1
    assert result["case_results"][0]["generation_kind"] == "model"
    assert result["case_results"][0]["verifier_passed"] is True
    provider = fake_provider[0]
    assert provider.model == smoke.MODEL
    assert provider.base_url == smoke.BASE_URL
    assert provider.max_tokens == 1536
    assert provider.enable_thinking is False
    assert provider.follow_redirects is False
    assert provider.response_format == "json_object"
    assert provider.request_timeout == 60
    output = public_fixture / result["artifact_dir"]
    artifact = json.loads((output / "case_v3_lookup_patent_term.json").read_text(encoding="utf-8"))
    assert artifact["record"]["assistant_llm_calls"] == 1
    assert artifact["record"]["judge_llm_calls"] == 0
    assert artifact["record"]["normalizer_llm_calls"] == 0
    assert artifact["record"]["metrics_schema_version"] == 2
    assert artifact["trace"]["execution"]["judge"]["status"] == "not_run"
    assert (public_fixture / smoke.AUTHORIZATION_LEDGER).exists()
    assert not (output / "ledger.json").exists()
    summary_text = json.dumps(result, ensure_ascii=False)
    assert "answer_text" not in summary_text
    assert "发明专利权" not in summary_text
    assert str(public_fixture) not in summary_text


def test_second_output_identity_cannot_reset_authorization(
    public_fixture: Path,
    fake_provider: list[FakeProvider],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata, items = smoke.prepare_smoke(public_fixture)
    monkeypatch.setattr(smoke, "prepare_smoke", lambda *args: (metadata, items[:1]))
    smoke.run_smoke(execute=True, run_id="live_smoke_first")
    count = len(fake_provider[0].prompts)
    with pytest.raises(smoke.SmokeError, match="authorization_already_used"):
        smoke.run_smoke(execute=True, run_id="live_smoke_second")
    assert len(fake_provider) == 1
    assert len(fake_provider[0].prompts) == count
    assert not (public_fixture / "artifacts/experiments/live_smoke_second").exists()


@pytest.mark.parametrize(
    "override,reason",
    [
        ({"prompt_tokens": None}, "usage_unknown"),
        ({"finish_reason": "length"}, "response_truncated"),
        ({"reasoning_tokens": 5}, "reasoning_returned"),
        ({"returned_model_matches": False}, "model_mismatch"),
    ],
)
def test_failed_probe_stops_whole_run_without_rag_calls(
    public_fixture: Path,
    monkeypatch: pytest.MonkeyPatch,
    override: dict,
    reason: str,
) -> None:
    monkeypatch.setenv("ALLOW_LIVE_MODEL_CALLS", "true")
    providers = []

    def factory(**kwargs):
        provider = FakeProvider(**kwargs)
        provider.metadata_override = override
        providers.append(provider)
        return provider

    monkeypatch.setattr(smoke, "SiliconFlowClient", factory)
    result = smoke.run_smoke(execute=True, run_id="live_smoke_stop")
    assert result["status"] == "stopped"
    assert result["stop_reason"] == reason
    assert result["rag_cases_completed"] == 0
    assert result["live_model_calls"] == 1
    assert len(providers[0].prompts) == 1
    if reason == "usage_unknown":
        assert result["budget"]["estimated_total_cost_cny"] is None


def test_invalid_generated_schema_is_preserved_then_stops(
    public_fixture: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALLOW_LIVE_MODEL_CALLS", "true")
    providers = []

    class InvalidGeneration(FakeProvider):
        def complete(self, prompt):
            response = super().complete(prompt)
            return response if prompt == smoke.PROBE_PROMPT else '{"answer": "wrong schema"}'

    def factory(**kwargs):
        provider = InvalidGeneration(**kwargs)
        providers.append(provider)
        return provider

    monkeypatch.setattr(smoke, "SiliconFlowClient", factory)
    result = smoke.run_smoke(execute=True, run_id="live_smoke_schema")
    assert result["status"] == "stopped"
    assert result["stop_reason"] == "generated_schema_invalid"
    assert result["live_model_calls"] == 2
    assert result["rag_cases_completed"] == 1
    assert len(providers[0].prompts) == 2
    output = public_fixture / result["artifact_dir"]
    assert (output / "case_v3_lookup_patent_term.json").exists()
    assert (output / "raw_response_02.json").exists()


def test_refusal_case_uses_zero_model_calls(public_fixture: Path) -> None:
    _, items = smoke.prepare_smoke(public_fixture)
    item = next(item for item in items if item["case"].case_type == "refusal")
    generated = item["assistant"].generate_turn(item["retrieved"], generate=True)
    verified = item["assistant"].verify_turn(generated)
    item["assistant"].commit_turn(verified)
    assert generated.kind == "pre_retrieval_refusal"
    assert verified.verification.passed
    assert item["assistant"].llm.__class__ is smoke._NoCallsClient


def test_main_redacts_unexpected_setup_error(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def failed(**kwargs):
        raise ValueError("SENTINEL_KEY_OR_PRIVATE_PROMPT")

    monkeypatch.setattr(smoke, "run_smoke", failed)
    assert smoke.main(["--preflight"]) == 2
    output = capsys.readouterr()
    assert "SENTINEL" not in output.out + output.err
    assert json.loads(output.out)["stop_reason"] == "preflight_or_setup_failed"


def test_post_call_summary_write_failure_preserves_real_call_count(
    public_fixture: Path,
    fake_provider: list[FakeProvider],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata, items = smoke.prepare_smoke(public_fixture)
    monkeypatch.setattr(smoke, "prepare_smoke", lambda *args: (metadata, items[:1]))
    original = smoke._write_json

    def fail_summary(path, payload):
        if path.name == "summary.json":
            raise OSError("SENTINEL_PRIVATE_EXCEPTION")
        original(path, payload)

    monkeypatch.setattr(smoke, "_write_json", fail_summary)
    result = smoke.run_smoke(execute=True, run_id="live_smoke_write_failure")
    assert result["status"] == "stopped"
    assert result["stop_reason"] == "summary_write_failed"
    assert result["live_model_calls"] == 2
    assert result["call_count_status"] == "canonical_ledger_known"
    ledger = json.loads((public_fixture / smoke.AUTHORIZATION_LEDGER).read_text(encoding="utf-8"))
    assert len(ledger["attempts"]) == 2
    with pytest.raises(smoke.SmokeError, match="authorization_already_used"):
        smoke.run_smoke(execute=True, run_id="live_smoke_do_not_retry")
    assert len(fake_provider[0].prompts) == 2
    assert "SENTINEL" not in json.dumps(result)


def test_execute_unexpected_failure_reports_unknown_not_zero(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    def failed(**kwargs):
        raise OSError("SENTINEL_PRIVATE_EXCEPTION")

    monkeypatch.setattr(smoke, "run_smoke", failed)
    assert smoke.main(["--execute", "--run-id", "live_smoke_unknown"]) == 2
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["live_model_calls"] is None
    assert result["call_count_status"] == "unknown"
    assert result["authorization_ledger"] == smoke.AUTHORIZATION_LEDGER.as_posix()
    assert "SENTINEL" not in output.out + output.err


def test_provider_client_is_closed_after_execution(
    public_fixture: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALLOW_LIVE_MODEL_CALLS", "true")
    closed = []

    def factory(**kwargs):
        provider = FakeProvider(**kwargs)
        provider._client = SimpleNamespace(close=lambda: closed.append(True))
        return provider

    monkeypatch.setattr(smoke, "SiliconFlowClient", factory)
    smoke.run_smoke(execute=True, run_id="live_smoke_close")
    assert closed == [True]


def test_probe_rejects_numeric_truth_instead_of_boolean(
    public_fixture: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALLOW_LIVE_MODEL_CALLS", "true")
    providers = []

    def factory(**kwargs):
        provider = FakeProvider(**kwargs)
        provider.response_override = '{"ok": 1}'
        providers.append(provider)
        return provider

    monkeypatch.setattr(smoke, "SiliconFlowClient", factory)
    result = smoke.run_smoke(execute=True, run_id="live_smoke_probe_type")
    assert result["status"] == "stopped"
    assert result["stop_reason"] == "compatibility_probe_invalid"
    assert len(providers[0].prompts) == 1


def test_provider_exception_is_sanitized_and_stops_all_cases(
    public_fixture: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALLOW_LIVE_MODEL_CALLS", "true")
    providers = []

    def factory(**kwargs):
        provider = FakeProvider(**kwargs)
        provider.failure = TimeoutError("SENTINEL_KEY_PRIVATE_PROMPT")
        providers.append(provider)
        return provider

    monkeypatch.setattr(smoke, "SiliconFlowClient", factory)
    result = smoke.run_smoke(execute=True, run_id="live_smoke_timeout")
    assert result["status"] == "stopped"
    assert result["stop_reason"] == "provider_error"
    assert result["live_model_calls"] == 1
    assert result["budget"]["estimated_total_cost_cny"] is None
    assert result["rag_cases_completed"] == 0
    assert len(providers[0].prompts) == 1
    assert "SENTINEL" not in json.dumps(result)


def test_preflight_checks_all_prompt_sizes_without_model(
    public_fixture: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(smoke, "build_qa_prompt", lambda **kwargs: "x" * 24001)
    with pytest.raises(smoke.SmokeError, match="preflight_prompt_limit"):
        smoke.prepare_smoke(public_fixture)


def test_code_identity_records_untracked_critical_hashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The original repository is used here, not a fixture Git checkout.
    repository = _PATH.parents[1]
    result = smoke._code_identity(repository)
    assert len(result["head"]) == 40
    assert len(result["tracked_diff_sha256"]) == 64
    assert result["includes_untracked_critical_files"] is True
    critical = result["critical_file_sha256"]
    assert critical["scripts/live_model_smoke.py"] == smoke._sha256(_PATH)
    assert "legal_rag/live_budget.py" in critical


def test_missing_visible_citation_stops_after_probe_and_first_rag_call(
    public_fixture: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduce the format failure using only the existing synthetic fixture."""
    monkeypatch.setenv("ALLOW_LIVE_MODEL_CALLS", "true")
    providers = []

    class MissingVisibleCitation(FakeProvider):
        def complete(self, prompt: str) -> str:
            response = super().complete(prompt)
            if prompt == smoke.PROBE_PROMPT:
                return response
            envelope = json.loads(response)
            # The claim still binds S1, but the displayed answer does not cite it.
            envelope["answer_text"] = "发明专利权的期限为二十年。"
            assert envelope["claims"][0]["source_ids"] == ["S1"]
            return json.dumps(envelope, ensure_ascii=False)

    def factory(**kwargs):
        provider = MissingVisibleCitation(**kwargs)
        providers.append(provider)
        return provider

    monkeypatch.setattr(smoke, "SiliconFlowClient", factory)
    result = smoke.run_smoke(execute=True, run_id="live_smoke_missing_citation")
    assert result["status"] == "stopped"
    assert result["probe_status"] == "passed"
    assert result["stop_reason"] == "generated_verifier_failed"
    assert result["live_model_calls"] == 2
    assert result["rag_cases_completed"] == 1
    assert result["case_results"][0]["schema_valid"] is True
    assert result["case_results"][0]["verifier_passed"] is False
    assert result["case_results"][0]["final_answer_mode"] == "insufficient_evidence"
    assert len(providers) == 1
    assert len(providers[0].prompts) == 2

    output = public_fixture / result["artifact_dir"]
    assert len(list(output.glob("case_*.json"))) == 1
    artifact = json.loads(
        (output / "case_v3_lookup_patent_term.json").read_text(encoding="utf-8")
    )
    draft = artifact["trace"]["generation_attempt"]["verification"]
    assert draft["schema_valid"] is True
    assert draft["passed"] is False
    assert "citation_ids_invalid" in draft["failure_reasons"]
    assert draft["visible_source_id_count"] == 0
    assert draft["claim_source_id_count"] == 1
    assert artifact["trace"]["generation_attempt"]["status"] == "rejected"
    assert artifact["trace"]["final_response"]["value"]["answer_mode"] == "insufficient_evidence"

    ledger = json.loads(
        (public_fixture / smoke.AUTHORIZATION_LEDGER).read_text(encoding="utf-8")
    )
    # Provider/JSON success does not self-certify the RAG answer's citations.
    assert len(ledger["attempts"]) == 2
    assert all(attempt["status"] == "succeeded" for attempt in ledger["attempts"])
    with pytest.raises(smoke.SmokeError, match="authorization_already_used"):
        smoke.run_smoke(execute=True, run_id="live_smoke_no_citation_retry")
    assert len(providers) == 1
    assert len(providers[0].prompts) == 2
    assert "发明专利权" not in json.dumps(result, ensure_ascii=False)


@pytest.fixture
def repair_evidence(public_fixture: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    evidence = {
        "prior_calls_attempted": 2,
        "prior_committed_cny": "0.0009544",
        "remaining_max_calls": 8,
        "remaining_budget_cny": "1.9990456",
        "prior_probe_status": "passed",
        "prior_manifest_sha256": "a" * 64,
        "prior_summary_sha256": "b" * 64,
        "prior_ledger_sha256": "c" * 64,
    }
    monkeypatch.setattr(smoke, "_repair_allowance", lambda root: evidence, raising=False)
    return evidence


def test_repair_preflight_is_zero_call_and_binds_prior_successful_probe(
    public_fixture: Path,
    repair_evidence: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(**kwargs):
        raise AssertionError("provider initialized during repair preflight")

    monkeypatch.setattr(smoke, "SiliconFlowClient", forbidden)
    result = smoke.run_smoke(repair=True, run_id="live_smoke_repair_preflight")
    assert result["status"] == "preflight_passed"
    assert result["live_model_calls"] == 0
    assert result["prior_live_model_calls"] == 2
    assert result["probe_status"] == "reused_prior_passed"
    assert result["probe_calls_this_run"] == 0
    assert result["prior_evidence"] == repair_evidence
    assert result["authorization_id"] == "qwen35b-repair-smoke-20261003"
    manifest = json.loads(
        (public_fixture / result["artifact_dir"] / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["max_calls"] == 8
    assert manifest["budget_cny"] == "1.9990456"
    assert manifest["probe_calls_maximum"] == 0
    assert manifest["prior_evidence"] == repair_evidence


def test_repair_reuses_prior_probe_and_uses_remaining_global_allowance(
    public_fixture: Path,
    fake_provider: list[FakeProvider],
    repair_evidence: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata, items = smoke.prepare_smoke(public_fixture)
    monkeypatch.setattr(smoke, "prepare_smoke", lambda *args: (metadata, items[:1]))
    result = smoke.run_smoke(execute=True, repair=True, run_id="live_smoke_repair_execution")
    assert result["status"] == "completed"
    assert result["live_model_calls"] == 1
    assert result["prior_live_model_calls"] == 2
    assert result["total_authorized_calls_attempted"] == 3
    assert result["probe_status"] == "reused_prior_passed"
    assert result["probe_calls_this_run"] == 0
    assert len(fake_provider[0].prompts) == 1
    assert smoke.PROBE_PROMPT not in fake_provider[0].prompts
    assert result["budget"]["policy"]["max_calls"] == 8
    assert result["budget"]["policy"]["budget_cny"] == "1.9990456"
    assert result["budget"]["policy"]["run_id"] == "qwen35b-repair-smoke-20261003"
    with pytest.raises(smoke.SmokeError, match="authorization_already_used"):
        smoke.run_smoke(execute=True, repair=True, run_id="live_smoke_repair_cannot_restart")
    assert len(fake_provider) == 1
    assert len(fake_provider[0].prompts) == 1


def test_repair_budget_enforces_eight_calls_including_prior_two(
    public_fixture: Path,
    fake_provider: list[FakeProvider],
    repair_evidence: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata, original_items = smoke.prepare_smoke(public_fixture)
    original = original_items[0]
    items = []
    # Deliberately inject nine generatable synthetic cases to exercise the cap.
    for number in range(9):
        case = replace(
            original["case"],
            case_id=f"synthetic_repair_{number}",
            question=original["case"].question + f" 合成样例标识{chr(65 + number)}",
        )
        assistant = smoke._make_assistant(original["assistant"].retriever)
        prepared = assistant.prepare_question(case.question)
        retrieved = assistant.retrieve_turn(prepared, max_followup_rounds=0)
        assert retrieved.terminal_kind is None
        items.append({"case": case, "assistant": assistant, "retrieved": retrieved})
    monkeypatch.setattr(smoke, "prepare_smoke", lambda *args: (metadata, items))
    result = smoke.run_smoke(execute=True, repair=True, run_id="live_smoke_repair_eight")
    assert result["status"] == "stopped"
    assert result["stop_reason"] == "call_limit"
    assert result["live_model_calls"] == 8
    assert result["total_authorized_calls_attempted"] == 10
    assert result["rag_cases_completed"] == 8
    assert len(fake_provider[0].prompts) == 8
    assert smoke.PROBE_PROMPT not in fake_provider[0].prompts
    assert float(result["cumulative_committed_cny"]) <= 2


def test_repair_unknown_usage_stops_without_reprobe_or_hidden_retry(
    public_fixture: Path,
    repair_evidence: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_LIVE_MODEL_CALLS", "true")
    providers = []

    def factory(**kwargs):
        provider = FakeProvider(**kwargs)
        provider.metadata_override = {"prompt_tokens": None}
        providers.append(provider)
        return provider

    monkeypatch.setattr(smoke, "SiliconFlowClient", factory)
    result = smoke.run_smoke(execute=True, repair=True, run_id="live_smoke_repair_unknown")
    assert result["status"] == "stopped"
    assert result["stop_reason"] == "usage_unknown"
    assert result["probe_status"] == "reused_prior_passed"
    assert result["probe_calls_this_run"] == 0
    assert result["live_model_calls"] == 1
    assert result["total_authorized_calls_attempted"] == 3
    assert result["cumulative_estimated_cost_cny"] is None
    assert len(providers[0].prompts) == 1
    assert smoke.PROBE_PROMPT not in providers[0].prompts


def test_repair_does_not_modify_first_receipts_or_ledger(
    public_fixture: Path,
    fake_provider: list[FakeProvider],
    repair_evidence: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prior_output = public_fixture / "artifacts/experiments" / smoke.PRIOR_RUN_ID
    prior_output.mkdir(parents=True)
    prior_ledger = public_fixture / smoke.AUTHORIZATION_LEDGER
    prior_ledger.parent.mkdir(parents=True)
    prior_paths = [prior_ledger, prior_output / "manifest.json", prior_output / "summary.json"]
    for path in prior_paths:
        path.write_text('{"synthetic_prior_receipt": true}\n', encoding="utf-8")
    hashes = {path: smoke._sha256(path) for path in prior_paths}
    metadata, items = smoke.prepare_smoke(public_fixture)
    monkeypatch.setattr(smoke, "prepare_smoke", lambda *args: (metadata, items[:1]))
    result = smoke.run_smoke(execute=True, repair=True, run_id="live_smoke_repair_immutable")
    assert result["status"] == "completed"
    assert hashes == {path: smoke._sha256(path) for path in prior_paths}
    assert (public_fixture / smoke.REPAIR_AUTHORIZATION_LEDGER).exists()


def test_repair_manifest_hash_mismatch_blocks_before_provider(
    public_fixture: Path,
    fake_provider: list[FakeProvider],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import legal_rag.live_budget

    monkeypatch.setattr(
        legal_rag.live_budget,
        "read_repair_allowance",
        lambda ledger, summary: {"remaining_max_calls": 8},
    )
    prior_output = public_fixture / "artifacts/experiments" / smoke.PRIOR_RUN_ID
    prior_output.mkdir(parents=True)
    (prior_output / "manifest.json").write_text('{"tampered": true}\n', encoding="utf-8")
    with pytest.raises(smoke.SmokeError, match="repair_manifest_hash_mismatch"):
        smoke.run_smoke(execute=True, repair=True, run_id="live_smoke_repair_tampered")
    assert fake_provider == []


def test_cli_repair_without_execute_remains_preflight(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    calls = []

    def fake_run(**kwargs):
        calls.append(kwargs)
        return {"status": "preflight_passed", "live_model_calls": 0}

    monkeypatch.setattr(smoke, "run_smoke", fake_run)
    assert smoke.main(["--repair", "--run-id", "live_smoke_repair_cli"]) == 0
    assert calls == [{"execute": False, "repair": True, "run_id": "live_smoke_repair_cli"}]
    assert json.loads(capsys.readouterr().out)["live_model_calls"] == 0


def test_repair_old_prompt_version_is_blocked_before_provider(
    public_fixture: Path,
    fake_provider: list[FakeProvider],
    repair_evidence: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(smoke.chat_module, "STRUCTURED_QA_PROMPT_VERSION", "old_prompt")
    with pytest.raises(smoke.SmokeError, match="repair_prompt_version_mismatch"):
        smoke.run_smoke(execute=True, repair=True, run_id="live_smoke_repair_old_prompt")
    assert fake_provider == []
    assert not (public_fixture / smoke.REPAIR_AUTHORIZATION_LEDGER).exists()


def test_repair_response_mode_failure_is_not_hidden_by_valid_fallback(
    public_fixture: Path,
    repair_evidence: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Protect existing stop behavior with synthetic output, not live raw data."""
    monkeypatch.setenv("ALLOW_LIVE_MODEL_CALLS", "true")
    metadata, original_items = smoke.prepare_smoke(public_fixture)
    original = original_items[0]
    items = []
    for number in range(9):
        case = replace(
            original["case"],
            case_id=f"synthetic_mode_{number}",
            question=original["case"].question + f" 合成样例标识{chr(65 + number)}",
        )
        assistant = smoke._make_assistant(original["assistant"].retriever)
        prepared = assistant.prepare_question(case.question)
        retrieved = assistant.retrieve_turn(prepared, max_followup_rounds=0)
        assert retrieved.terminal_kind is None
        items.append({"case": case, "assistant": assistant, "retrieved": retrieved})
    monkeypatch.setattr(smoke, "prepare_smoke", lambda *args: (metadata, items))
    providers = []

    class LimitedFifthResponse(FakeProvider):
        def complete(self, prompt: str) -> str:
            response = super().complete(prompt)
            if len(self.prompts) != 5:
                return response
            return json.dumps(
                {
                    "answer_text": "当前检索资料不足，无法给出可靠结论。",
                    "answer_mode": "insufficient_evidence",
                    "claims": [],
                    "limitations": ["合成测试资料不足。"],
                    "clarification_question": None,
                },
                ensure_ascii=False,
            )

    def factory(**kwargs):
        provider = LimitedFifthResponse(**kwargs)
        providers.append(provider)
        return provider

    monkeypatch.setattr(smoke, "SiliconFlowClient", factory)
    result = smoke.run_smoke(execute=True, repair=True, run_id="live_smoke_repair_mode_failure")
    assert result["status"] == "stopped"
    assert result["stop_reason"] == "generated_verifier_failed"
    assert result["probe_status"] == "reused_prior_passed"
    assert result["probe_calls_this_run"] == 0
    assert result["live_model_calls"] == 5
    assert result["total_authorized_calls_attempted"] == 7
    assert result["rag_cases_completed"] == 5
    assert all(case["verifier_passed"] for case in result["case_results"][:4])
    assert result["case_results"][4]["schema_valid"] is True
    assert result["case_results"][4]["verifier_passed"] is False
    assert result["case_results"][4]["final_answer_mode"] == "insufficient_evidence"
    assert len(providers) == 1
    assert len(providers[0].prompts) == 5

    output = public_fixture / result["artifact_dir"]
    assert len(list(output.glob("case_*.json"))) == 5
    artifact = json.loads((output / "case_synthetic_mode_4.json").read_text(encoding="utf-8"))
    draft = artifact["trace"]["generation_attempt"]["verification"]
    assert draft["passed"] is False
    assert draft["schema_valid"] is True
    assert draft["citation_ids_valid"] is True
    assert draft["citation_alignment_valid"] is True
    assert draft["disclaimer_present"] is True
    assert draft["expected_answer_mode"] == "evidence_answer"
    assert draft["actual_answer_mode"] == "insufficient_evidence"
    assert draft["failure_reasons"] == ["response_mode_invalid"]
    final = artifact["trace"]["final_response"]["value"]["verification"]
    assert final["passed"] is True
    assert artifact["trace"]["generation_attempt"]["status"] == "rejected"
    with pytest.raises(smoke.SmokeError, match="authorization_already_used"):
        smoke.run_smoke(execute=True, repair=True, run_id="live_smoke_repair_mode_no_retry")
    assert len(providers[0].prompts) == 5
    assert "合成测试资料不足" not in json.dumps(result, ensure_ascii=False)
