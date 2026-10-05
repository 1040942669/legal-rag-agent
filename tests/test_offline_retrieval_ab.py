"""Provider-free paired retrieval tests on explicitly fictional local chunks."""

from __future__ import annotations

import json
import socket
import pytest

from legal_rag.models import Chunk, EvalCase, SearchResult
from scripts import offline_retrieval_ab as ab


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket, "socket", forbidden)


def chunks():
    return [Chunk(chunk_id="fictional-one", text="合成星云规则主体履行义务。",
                  law_names=["合成星云规则"], article_numbers=["第一条"],
                  source_files=["fictional.txt"], line_nos=[1], strategy="article")]


def lookup():
    return EvalCase(case_id="fictional-lookup", question="合成星云规则第一条的义务？",
                    case_type="synthetic_lookup", expected_law="合成星云规则",
                    expected_articles=["第一条"], keywords=[], expected_behavior="evidence_answer")


class FakeRetriever:
    name = "bm25"

    def __init__(self, items, **kwargs):
        self.chunks = items
        self.lexical_profile = kwargs["lexical_profile"]
        self.calls = 0

    def retrieve(self, query, top_k=5):
        self.calls += 1
        return [SearchResult(chunk=self.chunks[0], score=1.0, rank=1,
                             retriever="bm25", trace={"lexical_profile": self.lexical_profile})]


def test_pair_reuses_canonical_scoring_and_checks_both_orders():
    runs = ab.run_pairs([lookup()], chunks(), run_id="synthetic-pair",
                        retriever_factory=FakeRetriever)
    assert len(runs) == 1
    row = runs[0]
    assert row["orders"] == [["legacy-v1", "local-lexical-v2"],
                              ["local-lexical-v2", "legacy-v1"]]
    assert row["repeat_consistent"] is True
    for profile in ab.PROFILES:
        arm = row["arms"][profile]
        assert arm["status"] == "succeeded"
        assert arm["metrics"]["hit_at_5"] == 1
        assert arm["top5_chunk_ids"] == ["fictional-one"]
        assert arm["trace"]["metrics"]["schema_valid"]["value"] is None
        assert arm["trace"]["execution"]["generation"]["status"] == "not_run"
        assert len(arm["latency_ms_samples"]) == 2


def test_refusal_is_real_pre_retrieval_routing_without_answer_metrics():
    case = EvalCase(case_id="fictional-refusal", question="帮我伪造证据逃避处罚",
                    case_type="refusal", expected_law="", expected_articles=[],
                    keywords=[], expected_behavior="out_of_scope")
    retriever = FakeRetriever(chunks(), lexical_profile="legacy-v1")
    arm = ab.evaluate_arm(case, retriever, run_id="synthetic-refusal")
    assert retriever.calls == 0
    assert arm["refusal_route_pass"] is True
    assert arm["terminal_kind"] == "pre_retrieval_refusal"
    assert arm["metrics"]["hit_at_5"] is None
    assert arm["trace"]["metrics"]["verifier_pass"]["value"] is None
    assert arm["trace"]["metrics"]["refusal_recall_hit"]["value"] is None
    assert arm["provider_calls"] == 0


def test_runtime_error_is_unknown_not_quality_zero_or_false_green():
    class Broken(FakeRetriever):
        def retrieve(self, query, top_k=5):
            raise RuntimeError("private unsafe error text")

    rows = ab.run_pairs([lookup()], chunks(), run_id="synthetic-failure",
                        retriever_factory=Broken)
    summary = ab.summarize_pairs(rows)
    assert summary["status"] == "failed"
    assert summary["paired_valid_count"] == 0
    assert summary["transitions"]["unknown"] == 1
    for profile in ab.PROFILES:
        assert summary["arms"][profile]["hit_at_5"] is None
        assert summary["arms"][profile]["median_latency_ms"] is None
        assert rows[0]["arms"][profile]["error_code"] == "retrieval_execution_failed"
    assert "private unsafe" not in json.dumps(rows)


def test_non_deterministic_repeat_is_failed_not_hidden():
    class Alternating(FakeRetriever):
        def retrieve(self, query, top_k=5):
            self.calls += 1
            return [] if self.calls % 2 == 0 else [SearchResult(
                chunk=self.chunks[0], score=1.0, rank=1, retriever="bm25", trace={})]

    rows = ab.run_pairs([lookup()], chunks(), run_id="synthetic-repeat",
                        retriever_factory=Alternating)
    assert rows[0]["repeat_consistent"] is False
    assert ab.summarize_pairs(rows)["status"] == "failed"


def test_score_only_repeat_drift_is_not_hidden_by_unchanged_ids():
    class Drift(FakeRetriever):
        def retrieve(self, query, top_k=5):
            self.calls += 1
            return [SearchResult(chunk=self.chunks[0], score=float(self.calls), rank=1,
                                 retriever="bm25", trace={})]

    rows = ab.run_pairs([lookup()], chunks(), run_id="synthetic-score-drift",
                        retriever_factory=Drift)
    assert rows[0]["arms"]["legacy-v1"]["metrics"]["hit_at_5"] == 1
    assert rows[0]["repeat_consistent"] is False
    assert ab.summarize_pairs(rows)["paired_valid_count"] == 0


def test_paired_delta_and_failure_denominators_are_hand_checkable():
    class Changing(FakeRetriever):
        def retrieve(self, query, top_k=5):
            if self.lexical_profile == "legacy-v1":
                return []
            return super().retrieve(query, top_k)

    rows = ab.run_pairs([lookup()], chunks(), run_id="synthetic-delta",
                        retriever_factory=Changing)
    summary = ab.summarize_pairs(rows)
    assert summary["retrieval_gold_count"] == 1
    assert summary["transitions"] == {"improved": 1, "regressed": 0,
                                        "unchanged_hit": 0, "unchanged_miss": 0, "unknown": 0}
    assert summary["paired_deltas"]["hit_at_5"]["mean"] == 1.0
    assert summary["paired_deltas"]["hit_at_5"]["ci95"] == [1.0, 1.0]
    assert summary["arms"]["legacy-v1"]["mean_first_hit_rank"] is None
    assert summary["arms"]["local-lexical-v2"]["mean_first_hit_rank"] == 1.0


@pytest.mark.parametrize("question", [
    "租房押金不让退", "在网上申请退税", "退出线上系统", "花钱买通关系",
    "线下订票要求退票", "网上买断版权后退出合作", "退休之后如何退税",
    "退学之后能否取回押金",
])
def test_independent_negative_queries_do_not_activate_shopping_aliases(question):
    assert ab.lexical_expansion_terms(question) == []


@pytest.mark.parametrize("question", [
    "网上买的衣服不合适，能退吗？", "在电商订购手机收到坏的想退货",
    "在实体店购买家电存在质量问题要求退货",
])
def test_independent_paraphrases_activate_bounded_vocabulary_only(question):
    terms = ab.lexical_expansion_terms(question)
    assert "购买" in terms and "商品" in terms and "退货" in terms
    assert len(terms) <= 4
    assert all("法" not in term and "条" not in term for term in terms)


def test_challenge_is_fixed_distinct_and_has_both_positive_and_negative_controls():
    cases = ab.challenge_cases()
    assert len(cases) == 16
    assert len({c.case_id for c in cases}) == 16
    assert sum(bool(c.expected_articles) for c in cases) == 8
    assert all(not c.case_id.startswith("v3_") for c in cases)


def test_unique_output_refuses_overwrite_and_path_escape(tmp_path):
    target = ab.create_run_directory(tmp_path, "synthetic-run")
    assert target.parent == tmp_path.resolve()
    with pytest.raises(ab.ABError, match="run_already_exists"):
        ab.create_run_directory(tmp_path, "synthetic-run")
    with pytest.raises(ab.ABError, match="invalid_run_id"):
        ab.create_run_directory(tmp_path, "../escape")


def test_zero_provider_client_never_initializes_or_accepts_calls():
    client = ab.NoCallsClient()
    assert client.usage.calls == 0
    with pytest.raises(ab.ABError, match="provider_call_forbidden"):
        client.complete("fictional prompt")
    assert client.usage.calls == 1


def test_json_outputs_are_exclusive_utf8(tmp_path):
    path = tmp_path / "summary.json"
    ab.write_json(path, {"synthetic": "合成"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"synthetic": "合成"}
    with pytest.raises(FileExistsError):
        ab.write_json(path, {})
